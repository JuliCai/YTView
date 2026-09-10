import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from yt_dlp import YoutubeDL
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError as YtdlpHTTPError

import segment_probe as probe
from sources import SourceError, Track, Video
from streaming import ProbeTarget, Resource, Ticket

URL = "https://r.googlevideo.com/segment?signature=private-test-value"
HEADERS = {"Accept-Encoding": "identity", "Range": f"bytes=0-{probe.SAMPLE_BYTES - 1}"}


def ticket():
    item = Ticket(Video("jNQXAC9IVRw", "Example", 60, "hls", (Track(URL),)))
    item.probe_target = ProbeTarget(Resource(URL, {"Cookie": "private-cookie"}), {}, 403)
    return item


class CountedBody(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.bytes_read = 0

    def read(self, size=-1):
        value = super().read(size)
        self.bytes_read += len(value)
        return value


@pytest.mark.parametrize("known_length", [True, False])
def test_actual_native_downloader_is_bounded_when_range_is_ignored(known_length):
    body = CountedBody(b"x" * (probe.SAMPLE_BYTES * 4))
    headers = {"Content-Type": "video/mp4"}
    if known_length:
        headers["Content-Length"] = str(probe.SAMPLE_BYTES * 4)
    response = Response(body, url=URL, headers=headers, status=200)
    with patch.object(YoutubeDL, "urlopen", return_value=response) as request, \
            patch("yt_dlp.downloader.common.sanitize_open", side_effect=AssertionError("Disk write attempted")):
        result = probe._native_sample(URL, HEADERS)
    assert result["outcome"] == "ok"
    assert result["bytes_read"] == body.bytes_read == probe.SAMPLE_BYTES
    assert response.closed
    assert request.call_count == 1
    assert request.call_args.args[0].headers["Range"] == HEADERS["Range"]


def test_native_http_error_is_closed_and_sanitized():
    response = Response(io.BytesIO(b"secret remote error"), url=URL, headers={}, status=403)
    with patch.object(YoutubeDL, "urlopen", side_effect=YtdlpHTTPError(response)):
        result = probe._native_sample(URL, HEADERS)
    assert result["http_status"] == 403 and result["bytes_read"] == 0
    assert result["outcome"] == "http_error" and response.closed
    assert "secret" not in json.dumps(result) and "signature" not in json.dumps(result)


def test_native_html_response_is_not_media_success():
    body = CountedBody(b"<html>secret</html>")
    response = Response(body, url=URL, headers={"Content-Type": "text/html"}, status=200)
    with patch.object(YoutubeDL, "urlopen", return_value=response):
        result = probe._native_sample(URL, HEADERS)
    assert result["outcome"] == "unsupported_response"
    assert body.bytes_read == 0 and response.closed


def test_unsafe_target_is_rejected_before_native_networking():
    with patch.object(YoutubeDL, "urlopen") as request:
        result = probe._native_sample("https://127.0.0.1/private", HEADERS)
    assert result["outcome"] == "error"
    request.assert_not_called()


@contextmanager
def local_origin():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("Range")))
            if self.path.startswith("/redirect-code-"):
                self.send_response(int(self.path.rsplit("-", 1)[-1]))
                self.send_header("Location", "/media")
                self.end_headers()
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/media")
                self.end_headers()
            elif self.path == "/redirect-denied":
                self.send_response(302)
                self.send_header("Location", "/denied")
                self.end_headers()
            elif self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
            elif self.path == "/unsafe":
                self.send_response(302)
                self.send_header("Location", "https://untrusted.invalid/private?signature=secret")
                self.end_headers()
            elif self.path == "/missing":
                self.send_response(302)
                self.end_headers()
            elif self.path.startswith("/chain/"):
                index = int(self.path.rsplit("/", 1)[-1])
                self.send_response(302)
                self.send_header("Location", f"/chain/{index + 1}" if index < 2 else "/media")
                self.end_headers()
            elif self.path == "/denied":
                self.send_response(403)
                self.end_headers()
            else:
                body = b"x" * (probe.SAMPLE_BYTES * 2)
                self.send_response(200)  # Deliberately ignore Range.
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def test_only_validate(url):
        if not url.startswith(base + "/"):
            raise SourceError("Not the offline fixture")
        return url

    try:
        with patch("segment_probe.validate_upstream", side_effect=test_only_validate):
            yield base, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("path, expected, chain, paths", [
    ("/media", "ok", [200], ["/media"]),
    ("/denied", "http_error", [403], ["/denied"]),
    ("/redirect", "ok", [302, 200], ["/redirect", "/media"]),
    ("/redirect-denied", "http_error", [302, 403], ["/redirect-denied", "/denied"]),
])
def test_real_native_and_httpx_transports_follow_validated_redirects(path, expected, chain, paths):
    with local_origin() as (base, calls):
        native = probe._native_sample(base + path, HEADERS)
        relay = asyncio.run(probe._httpx_sample(base + path, HEADERS))
    assert native["outcome"] == relay["outcome"] == expected
    assert native["http_status"] == relay["http_status"] == chain[-1]
    assert native["http_chain"] == relay["http_chain"] == chain
    assert calls == [(request_path, HEADERS["Range"]) for request_path in paths * 2]
    if expected == "ok":
        assert native["bytes_read"] == relay["bytes_read"] == probe.SAMPLE_BYTES


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_each_redirect_status_preserves_sample_range(code):
    path = f"/redirect-code-{code}"
    with local_origin() as (base, calls):
        native = probe._native_sample(base + path, HEADERS)
        relay = asyncio.run(probe._httpx_sample(base + path, HEADERS))
    assert native["http_chain"] == relay["http_chain"] == [code, 200]
    assert native["outcome"] == relay["outcome"] == "ok"
    assert calls == [(request_path, HEADERS["Range"]) for request_path in [path, "/media"] * 2]


@pytest.mark.parametrize("path, outcome, count", [
    ("/unsafe", "redirect_blocked", 1),
    ("/missing", "invalid_redirect", 1),
    ("/loop", "redirect_limit", probe.MAX_REQUESTS),
])
def test_redirect_safety_and_request_limits(path, outcome, count):
    with local_origin() as (base, calls):
        native = probe._native_sample(base + path, HEADERS)
        relay = asyncio.run(probe._httpx_sample(base + path, HEADERS))
    assert native["outcome"] == relay["outcome"] == outcome
    assert native["http_chain"] == relay["http_chain"] == [302] * count
    assert native["bytes_read"] == relay["bytes_read"] == 0
    assert len(calls) == 2 * count
    assert all(request_path == path for request_path, _ in calls)
    assert "untrusted" not in json.dumps([native, relay])
    assert "secret" not in json.dumps([native, relay])


def test_three_redirects_can_reach_media_within_four_request_budget():
    with local_origin() as (base, calls):
        native = probe._native_sample(base + "/chain/0", HEADERS)
        relay = asyncio.run(probe._httpx_sample(base + "/chain/0", HEADERS))
    assert native["http_chain"] == relay["http_chain"] == [302, 302, 302, 200]
    assert native["outcome"] == relay["outcome"] == "ok"
    assert native["bytes_read"] == relay["bytes_read"] == probe.SAMPLE_BYTES
    assert len(calls) == 8


def test_native_redirect_responses_are_closed_without_reading_bodies():
    body = CountedBody(b"secret redirect body")
    redirect = Response(body, url=URL, headers={"Location": "/final?signature=secret"}, status=302)
    final = Response(io.BytesIO(b"media"), url=URL, headers={"Content-Type": "video/mp4", "Content-Length": "5"}, status=200)
    with patch.object(YoutubeDL, "urlopen", side_effect=[YtdlpHTTPError(redirect), final]):
        result = probe._native_sample(URL, HEADERS)
    assert result["outcome"] == "ok" and result["http_chain"] == [302, 200]
    assert redirect.closed and final.closed and body.bytes_read == 0


class CountedStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.bytes_read = 0
        self.closed = False

    async def __aiter__(self):
        self.bytes_read += len(self.body)
        yield self.body

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("location, allowed", [
    ("/next?signature=secret", True),
    ("//other.googlevideo.com/next?signature=secret", True),
    ("https://other.googlevideo.com/next?signature=secret", True),
    ("https://127.0.0.1/private", False),
    ("http://r.googlevideo.com/private", False),
    ("https://r.googlevideo.com.untrusted.invalid/private", False),
    ("https://user@r.googlevideo.com/private", False),
    ("https://r.googlevideo.com:444/private", False),
    ("https://i.ytimg.com/private", False),
    ("file:///private", False),
])
def test_real_allowlist_validates_each_redirect_before_either_transport_sends(location, allowed):
    redirect_body = CountedBody(b"secret redirect body")
    redirect = Response(redirect_body, url=URL, headers={"Location": location}, status=302)
    final = Response(io.BytesIO(b"media"), url=URL, headers={"Content-Type": "video/mp4", "Content-Length": "5"}, status=200)
    with patch.object(YoutubeDL, "urlopen", side_effect=[YtdlpHTTPError(redirect), final]) as request:
        native = probe._native_sample(URL, HEADERS)
    assert request.call_count == (2 if allowed else 1)
    assert all(call.args[0].headers["Range"] == HEADERS["Range"] for call in request.call_args_list)
    assert redirect.closed and redirect_body.bytes_read == 0
    assert final.closed if allowed else not final.closed
    final.close()

    redirect_stream = CountedStream(b"secret redirect body")
    media_stream = CountedStream(b"media")
    requests = []

    def send(request):
        requests.append(request)
        assert request.headers["Range"] == HEADERS["Range"]
        assert "Cookie" not in request.headers
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": location, "Set-Cookie": "probe=secret; Path=/"},
                                  stream=redirect_stream)
        assert allowed
        return httpx.Response(200, headers={"Content-Type": "video/mp4", "Content-Length": "5"}, stream=media_stream)

    async def run_httpx():
        client = httpx.AsyncClient(transport=httpx.MockTransport(send), trust_env=False)
        with patch("segment_probe.Relay", return_value=SimpleNamespace(client=client)):
            return await probe._httpx_sample(URL, HEADERS)

    relay = asyncio.run(run_httpx())
    assert native["outcome"] == relay["outcome"] == ("ok" if allowed else "redirect_blocked")
    assert native["http_chain"] == relay["http_chain"] == ([302, 200] if allowed else [302])
    assert len(requests) == (2 if allowed else 1)
    assert redirect_stream.closed and redirect_stream.bytes_read == 0
    assert media_stream.closed == allowed
    assert "secret" not in json.dumps([native, relay])


def test_sample_ranges_are_bounded_and_preserve_start():
    assert probe.sample_range({}) == HEADERS["Range"]
    assert probe.sample_range({"Range": "bytes=50-"}) == f"bytes=50-{50 + probe.SAMPLE_BYTES - 1}"
    assert probe.sample_range({"Range": "bytes=50-90"}) == "bytes=50-90"
    with pytest.raises(SourceError):
        probe.sample_range({"Range": "bytes=-100"})


def test_worker_receives_secrets_only_on_stdin_and_only_safe_fields_return():
    sample = {"outcome": "http_error", "http_status": 403, "bytes_read": 0, "seconds": 0.2,
              "http_chain": [302, 403],
              "url": URL, "error_body": "private data"}
    with patch("segment_probe.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps({"native_ytdlp": sample, "relay_httpx": sample}))) as run:
        result = probe.run_segment_probe(ticket())
    assert result["state"] == "complete"
    assert "Both clients were denied" in result["interpretation"]
    assert result["native_ytdlp"]["http_chain"] == [302, 403]
    assert "signature" not in json.dumps(result) and "private" not in json.dumps(result)
    args, kwargs = run.call_args
    assert URL not in " ".join(args[0])
    payload = json.loads(kwargs["input"])
    assert payload["url"] == URL and "Cookie" not in payload["headers"]
    assert kwargs["timeout"] == probe.WORKER_TIMEOUT


def test_timeout_is_visible_without_leaking_subprocess_input_or_output():
    with patch("segment_probe.subprocess.run", side_effect=subprocess.TimeoutExpired(
            "worker", probe.WORKER_TIMEOUT, output=URL, stderr="private error")):
        result = probe.run_segment_probe(ticket())
    assert result["state"] == "timeout"
    assert "private" not in json.dumps(result) and URL not in json.dumps(result)
    assert probe._gate.acquire(blocking=False)
    probe._gate.release()


def test_bad_worker_output_is_not_forwarded():
    with patch("segment_probe.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=URL)):
        result = probe.run_segment_probe(ticket())
    assert result["state"] == "error"
    assert URL not in json.dumps(result)


def test_redirect_chain_fields_cannot_leak_urls():
    raw = {"outcome": "http_error", "http_status": 403, "bytes_read": 0, "seconds": 0.2,
           "http_chain": [302, URL, 403]}
    with pytest.raises(ValueError):
        probe._clean_result(raw)


@pytest.mark.parametrize("chain", [[302] * (probe.MAX_REQUESTS + 1), [302, 200], [], [302, 600]])
def test_malformed_or_oversized_worker_chains_are_rejected(chain):
    raw = {"outcome": "http_error", "http_status": 403, "bytes_read": 0, "seconds": 0.2,
           "http_chain": chain}
    with pytest.raises(ValueError):
        probe._clean_result(raw)


def test_missing_target_and_pending_source_wait_do_not_launch_worker():
    item = ticket()
    with patch("segment_probe.subprocess.run") as run:
        with patch("segment_probe.time.time", return_value=-10):
            assert probe.run_segment_probe(item)["state"] == "not_ready"
        item.probe_target = None
        assert probe.run_segment_probe(item)["state"] == "not_ready"
    run.assert_not_called()


def test_only_one_comparison_can_run_per_instance():
    with probe._gate, patch("segment_probe.subprocess.run") as run:
        assert probe.run_segment_probe(ticket())["state"] == "busy"
    run.assert_not_called()


def test_differential_interpretation_does_not_assert_an_ip_block():
    native = {"outcome": "ok", "http_status": 206}
    denied = {"outcome": "http_error", "http_status": 403}
    assert "Investigate transport/header differences" in probe.interpretation(native, denied)
    assert "full playback is not yet proven" in probe.interpretation(native, native)