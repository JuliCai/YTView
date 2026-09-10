"""Fresh yt-dlp extraction + native download, isolated from YTView playback.

Only a video ID, quality cap and fixed profile enter the worker. yt-dlp owns
format selection, cookies, headers, redirects and HLS parsing in one session.
Read guards bound the sample; temporary native output is removed by the parent,
including after a timeout. No signed URL or raw log leaves the private worker.
"""

from contextlib import redirect_stdout
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from deno import find_deno_bin
from yt_dlp import YoutubeDL
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadError

from token_provider import prepare_provider, run_private

SAMPLE_LIMIT = 64 * 1024
METADATA_LIMIT = 32 * 1024 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024
WORKER_TIMEOUT = 240  # Includes cold token-provider setup and native source waits.
MAX_REQUESTS = 80  # Calls into yt-dlp networking; native redirect hops aren't counted.
MAX_EVENTS = 40
_PROFILES = {"auto", "web_safari", "mweb_pot"}
_PROTOCOLS = {None, "https", "m3u8_native"}
_STAGES = {"setup", "extraction", "download"}
_OUTCOMES = {"sample_ok", "sample_limit", "http_error", "extraction_error",
             "download_error", "unsupported", "limit", "empty", "error"}
_MEDIA_TYPES = {"video/mp4", "audio/mp4", "video/webm", "audio/webm", "video/mp2t",
                "audio/aac", "audio/mpeg", "application/octet-stream", "binary/octet-stream"}
_MANIFEST_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
                   "audio/x-mpegurl"}
_gate = threading.BoundedSemaphore(1)


class _Stop(Exception):
    """Fixed safety outcome, never an upstream message."""


class _QuietLogger:
    def debug(self, message):
        pass

    warning = error = debug


class _Report:
    def __init__(self):
        self.stage = "setup"
        self.protocol = None
        self.po_token_attached = False
        self.media_bytes = 0
        self.metadata_bytes = 0
        self.events = []
        self.requests = 0
        self.stop = None

    def halt(self, reason):
        self.stop = reason
        raise _Stop(reason)

    def record(self, status):
        self.events.append({"stage": self.stage, "status": status})
        self.events = self.events[-MAX_EVENTS:]


class _BoundedResponse(Response):
    def __init__(self, response, report, media):
        super().__init__(response, response.url, response.headers, response.status,
                         response.reason, response.extensions)
        self.report = report
        self.media = media
        self.consumed = 0

    def read(self, amt=None):
        if amt == 0:
            return b""
        report = self.report
        remaining = (SAMPLE_LIMIT - report.media_bytes if self.media else
                     min(METADATA_LIMIT - report.metadata_bytes, RESPONSE_LIMIT - self.consumed))
        if remaining <= 0:
            report.halt("sample_limit" if self.media else "limit")
        data = self.fp.read(remaining if amt is None or amt < 0 else min(amt, remaining))
        self.consumed += len(data)
        if self.media:
            report.media_bytes += len(data)
        else:
            report.metadata_bytes += len(data)
            if len(data) == remaining:
                # Do not parse a silently truncated manifest/player response.
                report.halt("limit")
        return data


class _BeforeDownloadPP(PostProcessor):
    def __init__(self, ydl, report):
        super().__init__(ydl)
        self.report = report

    def run(self, info):
        report = self.report
        report.stage = "download"
        protocol = info.get("protocol")
        if (info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
                or info.get("has_drm") or info.get("requested_formats")
                or protocol not in {"https", "m3u8_native"}
                or get_suitable_downloader(info, self._downloader.params) not in {HttpFD, HlsFD}):
            report.halt("unsupported")
        report.protocol = protocol
        parsed = urlsplit(info["url"])
        report.po_token_attached = bool(parse_qs(parsed.query).get("pot")
                                        or re.search(r"/pot/[^/]+", parsed.path))
        # Native FileDownloader honors available_at; reject rather than shorten
        # unusually long waits. The parent also enforces a wall-clock deadline.
        wait = float(info.get("available_at") or 0) - time.time()
        if not math.isfinite(wait) or wait > 120:
            report.halt("limit")
        return [], info


class _NativeYDL(YoutubeDL):
    def __init__(self, options, report):
        self.report = report
        self.responses = []
        super().__init__(options)
        self.add_post_processor(_BeforeDownloadPP(self, report), when="before_dl")

    def urlopen(self, request):
        report = self.report
        if report.stop:
            report.halt(report.stop)
        report.requests += 1
        if report.requests > MAX_REQUESTS:
            report.halt("limit")
        try:
            # Intentionally retain yt-dlp's default transport, cookie jar,
            # request headers and automatic redirects throughout extraction.
            response = super().urlopen(request)
        except HTTPError as exc:
            report.record(exc.status)
            exc.response.close()
            raise
        report.record(response.status)
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        media = content_type in _MEDIA_TYPES
        if report.stage == "download" and not media and content_type not in _MANIFEST_TYPES:
            response.close()
            report.halt("unsupported")
        wrapped = _BoundedResponse(response, report, media)
        self.responses.append(wrapped)
        return wrapped

    def close(self):
        for response in self.responses:
            response.close()
        super().close()


def _options(height, profile, directory):
    # A single native format, never video+audio merging or an external downloader.
    # Do not reuse select_video(), Track headers, or YTView's source filtering.
    selectors = [f"{kind}[protocol={protocol}][height<=?{height}]"
                 for kind in ("b", "bv", "ba") for protocol in ("https", "m3u8_native")]
    deno = str(find_deno_bin())
    options = {
        "quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger(),
        "noplaylist": True, "cachedir": False, "socket_timeout": 12,
        "retries": 0, "fragment_retries": 0, "extractor_retries": 0,
        "skip_unavailable_fragments": False, "concurrent_fragment_downloads": 1,
        "continuedl": False, "nopart": True, "updatetime": False,
        "test": True, "check_formats": False, "skip_download": False,
        "format": "/".join(selectors), "outtmpl": str(directory / "sample.%(ext)s"),
        "hls_prefer_native": True, "external_downloader": {"default": "native"},
        "fixup": "never", "postprocessors": [],
        "ffmpeg_location": str(directory / "no-external-downloader"),
        "proxy": "", "source_address": "0.0.0.0",
        "js_runtimes": {"deno": {"path": deno}},
        "extractor_args": {"youtube": {"fetch_pot": ["never"]}},
    }
    if profile == "mweb_pot":
        server = prepare_provider(deno)
        options["extractor_args"] = {
            "youtube": {"player_client": ["mweb"], "fetch_pot": ["always"]},
            "youtubepot-bgutilscript": {"server_home": [str(server)]},
        }
    elif profile == "web_safari":
        options["extractor_args"]["youtube"]["player_client"] = [profile]
    return options


def _run_native(video_id, height, profile, directory):
    report = _Report()
    started = time.monotonic()
    completed = False
    outcome = "error"
    try:
        options = _options(height, profile, directory)
        report.stage = "extraction"
        with _NativeYDL(options, report) as ydl:
            # One call, one session: yt-dlp selects and downloads its fresh source.
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            completed = info is not None and report.stage == "download"
        outcome = "sample_ok" if completed and report.media_bytes else "empty"
    except _Stop:
        outcome = report.stop
    except (DownloadError, HTTPError):
        outcome = "extraction_error" if report.stage == "extraction" else "download_error"
        if report.events and report.events[-1]["status"] >= 400:
            outcome = "http_error"
    except Exception:
        outcome = "error"
    # yt-dlp may wrap a guard exception. Retain its fixed, known reason.
    outcome = report.stop or outcome
    return {"outcome": outcome, "stage": report.stage, "protocol": report.protocol,
            "po_token_attached": report.po_token_attached, "native_completed": completed,
            "media_bytes": report.media_bytes, "metadata_bytes": report.metadata_bytes,
            "http_events": report.events, "seconds": round(time.monotonic() - started, 2)}


def _clean_result(raw):
    if not isinstance(raw, dict):
        raise ValueError
    if (raw.get("outcome") not in _OUTCOMES or raw.get("stage") not in _STAGES
            or raw.get("protocol") not in _PROTOCOLS):
        raise ValueError
    clean = {key: raw[key] for key in ("outcome", "stage", "protocol")}
    for key in ("po_token_attached", "native_completed"):
        if type(raw.get(key)) is not bool:
            raise ValueError
        clean[key] = raw[key]
    for key, limit in (("media_bytes", SAMPLE_LIMIT), ("metadata_bytes", METADATA_LIMIT)):
        if type(raw.get(key)) is not int or not 0 <= raw[key] <= limit:
            raise ValueError
        clean[key] = raw[key]
    seconds = raw.get("seconds")
    if type(seconds) not in {float, int} or not math.isfinite(seconds) or not 0 <= seconds <= WORKER_TIMEOUT:
        raise ValueError
    clean["seconds"] = seconds
    events = raw.get("http_events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError
    clean["http_events"] = []
    for event in events:
        if (not isinstance(event, dict) or event.get("stage") not in _STAGES
                or type(event.get("status")) is not int or not 100 <= event["status"] <= 599):
            raise ValueError
        clean["http_events"].append({"stage": event["stage"], "status": event["status"]})
    return clean


def _interpret(result):
    if result["outcome"] in {"sample_ok", "sample_limit"} and result["media_bytes"]:
        return ("Fresh native yt-dlp read sample bytes without YTView's resolver or relay. "
                "This justifies investigating a session/integration difference, but does not prove "
                "playback or seeking. An HLS sample may be only an initialization fragment.")
    if result["stage"] == "download" and result["outcome"] == "http_error":
        return ("The fresh native download was also denied. YTView's resolver, playlist rewriting "
                "and HTTPX relay were not used. This still does not identify the authorization "
                "failure or prove an IP block.")
    return ("Inconclusive for playback: check the stage and outcome. Setup/extraction errors, "
            "unsupported formats and safety limits do not establish that native media access is denied.")


def run_native_probe(video_id: str, max_height: int = 720, *, client_profile: str = "auto") -> dict:
    if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            or type(max_height) is not int or max_height not in {360, 720, 1080}
            or client_profile not in _PROFILES):
        return {"state": "error", "message": "Enter a valid video ID, quality and native-test profile."}
    if not _gate.acquire(blocking=False):
        return {"state": "busy", "message": "Another fresh native test is running on this instance."}
    try:
        # Parent owns cleanup so even SIGKILL/timeout removes native fragments.
        with tempfile.TemporaryDirectory(prefix="ytview-native-") as directory:
            payload = {"video_id": video_id, "height": max_height, "profile": client_profile,
                       "directory": directory}
            completed = run_private([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                    input=json.dumps(payload), timeout=WORKER_TIMEOUT)
            if completed.returncode or len(completed.stdout) > 32 * 1024:
                return {"state": "error", "message": "The native worker failed. No raw logs were exposed."}
            result = _clean_result(json.loads(completed.stdout))
        return {"state": "complete", "experiment": "fresh_native_session",
                "client_profile": client_profile, "max_height": max_height,
                "sample_limit_bytes": SAMPLE_LIMIT, "worker_timeout_seconds": WORKER_TIMEOUT,
                "scope": "Fresh ID-only extraction and single-format native download in one yt-dlp session. "
                         "Native cookies, headers, transport, redirects and HLS parser; direct IPv4. "
                         "HTTP statuses are final responses, not redirect chains. No YTView source/relay reuse.",
                **result, "interpretation": _interpret(result)}
    except subprocess.TimeoutExpired:
        return {"state": "timeout", "message": f"The native test exceeded {WORKER_TIMEOUT} seconds. "
                "Its worker/process group was terminated and temporary samples removed; result is inconclusive."}
    except Exception:
        return {"state": "error", "message": "The native test returned an invalid result. No raw logs were exposed."}
    finally:
        _gate.release()


def _worker():
    try:
        payload = json.load(sys.stdin)
        if (not re.fullmatch(r"[A-Za-z0-9_-]{11}", payload["video_id"])
                or payload["height"] not in {360, 720, 1080} or payload["profile"] not in _PROFILES):
            return 1
        # Provider/native stdout is private stderr; only our safe JSON uses stdout.
        with redirect_stdout(sys.stderr):
            result = _run_native(payload["video_id"], payload["height"], payload["profile"],
                                 Path(payload["directory"]))
        print(json.dumps(_clean_result(result), allow_nan=False))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker() if sys.argv[1:] == ["--worker"] else 2)