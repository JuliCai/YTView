from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.networking.common import Response

import native_probe as probe

VIDEO_ID = "jNQXAC9IVRw"


@contextmanager
def native_origin(protocol="https", *, denied=False, html=False, init=False, known_length=True,
                  metadata_denied=False, unsupported_hls=False):
    """Fake only the site extractor; real yt-dlp processing, networking and downloaders."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("Cookie"), self.headers.get("Range")))
            if self.path == "/metadata":
                self.send_response(403 if metadata_denied else 200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", "session=private-cookie; Path=/")
                self.end_headers()
                self.wfile.write(b"fixture metadata")
                return
            if self.headers.get("Cookie") != "session=private-cookie":
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/media?signature=private-signature")
                self.end_headers()
                return
            if self.path == "/playlist":
                body = ("#EXTM3U\n#EXT-X-TARGETDURATION:10\n"
                        + ('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="/key"\n' if unsupported_hls else "")
                        + ('#EXT-X-MAP:URI="/init"\n' if init else "")
                        + "#EXTINF:10,\n/redirect\n#EXTINF:10,\n/never-requested\n#EXT-X-ENDLIST\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            elif denied:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"private error body")
                return
            else:
                body = b"init" if self.path == "/init" else b"x" * (probe.SAMPLE_LIMIT * 8)
                self.send_response(200)  # Intentionally ignore Range.
                self.send_header("Content-Type", "text/html" if html else "video/mp4")
            if known_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    class FixtureIE(InfoExtractor):
        _VALID_URL = r"https://www\.youtube\.com/watch\?v=(?P<id>[A-Za-z0-9_-]{11})"

        def _real_extract(self, url):
            video_id = self._match_id(url)
            self._download_webpage(base + "/metadata", video_id)
            return {"id": video_id, "title": "private title", "duration": 20,
                    "formats": [{"format_id": "fixture", "url": base + ("/playlist" if protocol == "m3u8_native" else "/redirect"),
                                 "protocol": protocol, "ext": "mp4", "height": 360,
                                 "vcodec": "avc1.4D401F", "acodec": "mp4a.40.2"}]}

    def register_fixture(ydl):
        ydl.add_info_extractor(FixtureIE(ydl))

    try:
        with patch.object(probe._NativeYDL, "add_default_info_extractors", register_fixture), \
                patch("sources.resolve_video", side_effect=AssertionError("YTView resolver used")), \
                patch("streaming.Relay.open", side_effect=AssertionError("YTView relay used")):
            yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("protocol", ["https", "m3u8_native"])
@pytest.mark.parametrize("known_length", [True, False])
def test_fresh_extraction_native_download_preserves_cookies_and_caps_reads(tmp_path, protocol, known_length):
    with native_origin(protocol, known_length=known_length) as calls:
        result = probe._run_native(VIDEO_ID, 720, "auto", tmp_path)
    assert result["outcome"] == ("sample_ok" if protocol == "https" else "sample_limit"), result
    assert result["stage"] == "download" and result["protocol"] == protocol
    assert result["media_bytes"] == (10_241 if protocol == "https" else probe.SAMPLE_LIMIT)
    assert calls[0] == ("/metadata", None, None)
    assert all(cookie == "session=private-cookie" for _, cookie, _ in calls[1:])
    assert any(path == "/media?signature=private-signature" for path, _, _ in calls)
    assert not any(path == "/never-requested" for path, _, _ in calls)
    assert all(path.stat().st_size <= probe.SAMPLE_LIMIT for path in tmp_path.iterdir())
    assert "private" not in json.dumps(result)
    assert probe._clean_result(result) == result


@pytest.mark.parametrize("protocol", ["https", "m3u8_native"])
def test_native_download_403_is_distinct_from_extraction_failure(tmp_path, protocol):
    with native_origin(protocol, denied=True):
        result = probe._run_native(VIDEO_ID, 720, "auto", tmp_path)
    assert result["outcome"] == "http_error" and result["stage"] == "download", result
    assert result["media_bytes"] == 0
    assert result["http_events"][-1] == {"stage": "download", "status": 403}
    assert "also denied" in probe._interpret(result)
    assert "private" not in json.dumps(result)


def test_native_extraction_403_does_not_claim_download_attempted(tmp_path):
    with native_origin(metadata_denied=True) as calls:
        result = probe._run_native(VIDEO_ID, 720, "auto", tmp_path)
    assert result["stage"] == "extraction" and result["outcome"] == "http_error"
    assert len(calls) == 1 and result["protocol"] is None
    assert "Inconclusive" in probe._interpret(result)


def test_html_success_response_is_not_counted_as_media(tmp_path):
    with native_origin(html=True):
        result = probe._run_native(VIDEO_ID, 720, "auto", tmp_path)
    assert result["outcome"] == "unsupported" and result["media_bytes"] == 0


def test_hls_native_test_may_only_download_init_fragment(tmp_path):
    with native_origin("m3u8_native", init=True) as calls:
        result = probe._run_native(VIDEO_ID, 720, "auto", tmp_path)
    assert result["outcome"] == "sample_ok" and result["media_bytes"] == 4, result
    assert [path for path, _, _ in calls] == ["/metadata", "/playlist", "/init"]
    assert "initialization fragment" in probe._interpret(result)


def test_unsupported_hls_never_launches_ffmpeg(tmp_path):
    with native_origin("m3u8_native", unsupported_hls=True), \
            patch("yt_dlp.downloader.external.Popen", side_effect=AssertionError("External process attempted")) as launch:
        result = probe._run_native(VIDEO_ID, 720, "auto", tmp_path)
    assert result["outcome"] not in {"sample_ok", "sample_limit"}
    assert launch.mock_calls == []


@pytest.mark.parametrize("extra", [
    {"is_live": True}, {"live_status": "post_live"}, {"has_drm": True},
    {"requested_formats": [{"url": "https://example.invalid/"}]}, {"protocol": "http_dash_segments"},
    {"available_at": float("inf")}, {"available_at": 10**12},
])
def test_pre_download_guard_rejects_unsupported_or_unbounded_work(tmp_path, extra):
    report = probe._Report()
    with probe._NativeYDL(probe._options(720, "auto", tmp_path), report) as ydl:
        pp = probe._BeforeDownloadPP(ydl, report)
        with pytest.raises(probe._Stop):
            pp.run({"protocol": "https", "url": "https://r.googlevideo.com/video", **extra})
    assert report.stop in {"unsupported", "limit"}
    assert report.media_bytes == 0


@pytest.mark.parametrize("url,expected", [
    ("https://r.googlevideo.com/video", False),
    ("https://r.googlevideo.com/video?pot=private", True),
    ("https://r.googlevideo.com/pot/private/video", True),
])
def test_token_flag_observes_native_selection_instead_of_assuming_profile_success(tmp_path, url, expected):
    report = probe._Report()
    with probe._NativeYDL(probe._options(720, "auto", tmp_path), report) as ydl:
        probe._BeforeDownloadPP(ydl, report).run({"protocol": "https", "url": url})
    assert report.po_token_attached is expected


def test_request_budget_and_sticky_stop_prevent_extra_network_requests():
    report = probe._Report()
    with probe._NativeYDL({"quiet": True, "logger": probe._QuietLogger()}, report) as ydl:
        report.requests = probe.MAX_REQUESTS
        with patch.object(probe.YoutubeDL, "urlopen") as network:
            for _ in range(2):
                with pytest.raises(probe._Stop):
                    ydl.urlopen("https://www.youtube.com/")
            network.assert_not_called()
    assert report.stop == "limit"


def test_response_read_budget_is_independent_of_native_test_flag():
    report = probe._Report()
    body = io.BytesIO(b"x" * probe.SAMPLE_LIMIT * 4)
    response = probe._BoundedResponse(Response(body, "https://example.invalid/", {}, 200), report, True)
    assert len(response.read()) == probe.SAMPLE_LIMIT
    assert body.tell() == probe.SAMPLE_LIMIT
    with pytest.raises(probe._Stop):
        response.read(100)
    assert report.stop == "sample_limit"
    response.close()
    assert body.closed


def test_metadata_read_limit_fails_instead_of_parsing_truncated_response():
    report = probe._Report()
    report.metadata_bytes = probe.METADATA_LIMIT - 5
    response = probe._BoundedResponse(Response(io.BytesIO(b"x" * 100), "https://example.invalid/", {}), report, False)
    with pytest.raises(probe._Stop):
        response.read()
    assert report.metadata_bytes == probe.METADATA_LIMIT and report.stop == "limit"
    response.close()


def safe_report():
    return {"outcome": "sample_ok", "stage": "download", "protocol": "https",
            "po_token_attached": False, "native_completed": True, "media_bytes": 10241,
            "metadata_bytes": 20, "http_events": [{"stage": "download", "status": 200}], "seconds": 0.1}


def test_parent_passes_only_id_settings_and_cleans_native_output():
    directories = []

    def worker(command, *, input, timeout):
        payload = json.loads(input)
        assert set(payload) == {"video_id", "height", "profile", "directory"}
        assert payload["video_id"] == VIDEO_ID and payload["profile"] == "auto"
        assert command[-1] == "--worker" and VIDEO_ID not in command
        assert timeout == probe.WORKER_TIMEOUT
        directory = Path(payload["directory"])
        directories.append(directory)
        (directory / "sample.mp4-Frag1").write_bytes(b"private media")
        raw = {**safe_report(), "url": "https://private.invalid/secret", "exception": "private log"}
        raw["http_events"][0]["url"] = "private URL"
        return SimpleNamespace(returncode=0, stdout=json.dumps(raw), stderr="private provider error")

    with patch.object(probe, "run_private", side_effect=worker):
        result = probe.run_native_probe(VIDEO_ID)
    assert result["state"] == "complete" and result["experiment"] == "fresh_native_session"
    assert "private" not in json.dumps(result)
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "exit"])
def test_parent_cleans_samples_and_releases_gate_on_failure(failure):
    directories = []

    def worker(command, *, input, timeout):
        directory = Path(json.loads(input)["directory"])
        directories.append(directory)
        (directory / "partial").write_bytes(b"private")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout, stderr="private")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout="private-not-json")

    with patch.object(probe, "run_private", side_effect=worker):
        result = probe.run_native_probe(VIDEO_ID)
    assert result["state"] == ("timeout" if failure == "timeout" else "error")
    assert "private" not in json.dumps(result)
    assert all(not directory.exists() for directory in directories)
    assert probe._gate.acquire(blocking=False)
    probe._gate.release()


@pytest.mark.parametrize("key,value", [
    ("outcome", "private error"), ("protocol", "https://private/"), ("stage", "private"),
    ("media_bytes", probe.SAMPLE_LIMIT + 1), ("media_bytes", True),
    ("metadata_bytes", -1), ("native_completed", "private"), ("seconds", float("nan")),
    ("http_events", [{"stage": "download", "status": "private"}]),
])
def test_parent_rejects_unsafe_worker_fields(key, value):
    with pytest.raises(ValueError):
        probe._clean_result({**safe_report(), key: value})


@pytest.mark.parametrize("video,height,profile", [
    ("https://example.invalid/", 720, "auto"), (VIDEO_ID, 999, "auto"),
    (VIDEO_ID, 720, "unknown"), (VIDEO_ID, True, "auto"),
])
def test_invalid_input_does_not_start_worker(video, height, profile):
    with patch.object(probe, "run_private") as worker:
        result = probe.run_native_probe(video, height, client_profile=profile)
    assert result["state"] == "error"
    worker.assert_not_called()


def test_gate_admits_only_one_native_worker():
    probe._gate.acquire()
    try:
        with patch.object(probe, "run_private") as worker:
            assert probe.run_native_probe(VIDEO_ID)["state"] == "busy"
        worker.assert_not_called()
    finally:
        probe._gate.release()


@pytest.mark.parametrize("profile", ["auto", "web_safari", "mweb_pot"])
def test_profiles_preserve_one_session_options_without_source_reuse(tmp_path, profile):
    with patch.object(probe, "prepare_provider", return_value=tmp_path / "provider") as setup:
        options = probe._options(720, profile, tmp_path)
    assert options["test"] is True and options["skip_download"] is False
    assert options["proxy"] == "" and options["source_address"] == "0.0.0.0"
    assert options["check_formats"] is False and options["fixup"] == "never"
    assert options["postprocessors"] == [] and "+" not in options["format"]
    assert not any("cookie" in key for key in options)
    if profile == "mweb_pot":
        setup.assert_called_once()
        assert options["extractor_args"]["youtube"] == {"player_client": ["mweb"], "fetch_pot": ["always"]}
    else:
        setup.assert_not_called()
        assert options["extractor_args"]["youtube"]["fetch_pot"] == ["never"]


def test_worker_never_prints_raw_extraction_output(capsys, tmp_path):
    payload = {"video_id": VIDEO_ID, "height": 720, "profile": "auto", "directory": str(tmp_path)}

    def noisy_native(*args):
        print("private signed URL from a dependency")
        return safe_report()

    with patch.object(probe.sys, "stdin", io.StringIO(json.dumps(payload))), \
            patch.object(probe, "_run_native", side_effect=noisy_native):
        assert probe._worker() == 0
    captured = capsys.readouterr()
    assert "private" not in captured.out and "private" in captured.err
    assert json.loads(captured.out) == safe_report()