import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

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
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-follow")
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


@pytest.mark.parametrize("path, expected, status_code", [
    ("/media", "ok", 200), ("/denied", "http_error", 403), ("/redirect", "redirect_not_followed", 302),
])
def test_real_native_and_httpx_transports_match_without_following_redirects(path, expected, status_code):
    with local_origin() as (base, calls):
        native = probe._native_sample(base + path, HEADERS)
        relay = asyncio.run(probe._httpx_sample(base + path, HEADERS))
    assert native["outcome"] == relay["outcome"] == expected
    assert native["http_status"] == relay["http_status"] == status_code
    assert calls == [(path, HEADERS["Range"]), (path, HEADERS["Range"])]
    if expected == "ok":
        assert native["bytes_read"] == relay["bytes_read"] == probe.SAMPLE_BYTES


def test_sample_ranges_are_bounded_and_preserve_start():
    assert probe.sample_range({}) == HEADERS["Range"]
    assert probe.sample_range({"Range": "bytes=50-"}) == f"bytes=50-{50 + probe.SAMPLE_BYTES - 1}"
    assert probe.sample_range({"Range": "bytes=50-90"}) == "bytes=50-90"
    with pytest.raises(SourceError):
        probe.sample_range({"Range": "bytes=-100"})


def test_worker_receives_secrets_only_on_stdin_and_only_safe_fields_return():
    sample = {"outcome": "http_error", "http_status": 403, "bytes_read": 0, "seconds": 0.2,
              "url": URL, "error_body": "private data"}
    with patch("segment_probe.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps({"native_ytdlp": sample, "relay_httpx": sample}))) as run:
        result = probe.run_segment_probe(ticket())
    assert result["state"] == "complete"
    assert "Both clients were denied" in result["interpretation"]
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