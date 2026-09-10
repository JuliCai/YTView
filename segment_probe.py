"""Opt-in, bounded comparison of an existing media URL. Never a playback fallback.

The worker uses yt-dlp's real HTTP fragment downloader and the relay's HTTPX
transport. Signed URLs travel over stdin only; stdout contains sanitized JSON.
No extraction, files, cookies, unbounded retries, or unvalidated redirects.
"""

import asyncio
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

from yt_dlp import YoutubeDL
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking.exceptions import HTTPError as YtdlpHTTPError

from sources import QuietLogger, SourceError, validate_upstream
from streaming import BUILD, Relay, Resource, Ticket, upstream_headers

SAMPLE_BYTES = 10_241  # yt-dlp FileDownloader._TEST_FILE_SIZE
WORKER_TIMEOUT = 35
SOCKET_TIMEOUT = 8
MAX_REQUESTS = 4  # Same total request budget as the playback relay.
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_gate = threading.BoundedSemaphore(1)
_MEDIA_TYPES = {"video/mp4", "audio/mp4", "video/mp2t", "audio/aac", "audio/mpeg",
                "application/octet-stream", "binary/octet-stream"}
_OUTCOMES = {"ok", "http_error", "redirect_blocked", "redirect_limit", "invalid_redirect",
             "unsupported_response", "error", "empty"}


def sample_range(headers: dict) -> str:
    original = next((v for k, v in headers.items() if k.lower() == "range"), "")
    if not original:
        return f"bytes=0-{SAMPLE_BYTES - 1}"
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", original)
    if not match:
        raise SourceError("This diagnostic supports start-based byte ranges, not suffix ranges.")
    first = int(match[1])
    last = min(int(match[2]), first + SAMPLE_BYTES - 1) if match[2] else first + SAMPLE_BYTES - 1
    if last < first:
        raise SourceError("Invalid media sample range.")
    return f"bytes={first}-{last}"


def _supported(headers) -> bool:
    return (headers.get("Content-Type", "").split(";")[0].lower() in _MEDIA_TYPES
            and headers.get("Content-Encoding", "identity").lower() == "identity")


def _redirect_target(url: str, location: str | None, result: dict) -> str:
    if not location or not location.strip():
        result["outcome"] = "invalid_redirect"
        raise SourceError("The source returned a redirect without a destination.")
    try:
        target = validate_upstream(urljoin(url, location))
    except (SourceError, ValueError):
        result["outcome"] = "redirect_blocked"
        raise SourceError("The source redirected outside the permitted media hosts.") from None
    if len(result["http_chain"]) >= MAX_REQUESTS:
        result["outcome"] = "redirect_limit"
        raise SourceError("The media redirect chain exceeded the diagnostic request limit.")
    return target


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    handler_order = 100  # Run before yt-dlp's normal redirect handler.

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Disable automatic following: _ProbeYDL validates and follows manually.
        raise urllib.error.HTTPError(req.full_url, code, "Redirect not followed", headers, fp)


class ProbeUrllibRH(UrllibRH):
    def _create_instance(self, *args, **kwargs):
        opener = super()._create_instance(*args, **kwargs)
        opener.add_handler(_NoRedirect())
        return opener


class _ProbeYDL(YoutubeDL):
    def __init__(self, params, result):
        super().__init__(params)
        self.result = result
        self.responses = []
        self.requests_made = 0
        # Keep yt-dlp's urllib transport, disabling automatic redirects so each
        # destination can be validated before any network request.
        self._request_director = self.build_request_director([ProbeUrllibRH])

    def urlopen(self, request):
        validate_upstream(request.url)
        if self.requests_made:
            raise SourceError("Additional native downloader requests are disabled for this sample.")
        self.requests_made += 1
        for _ in range(MAX_REQUESTS):
            validate_upstream(request.url)
            self.cookiejar.clear()
            try:
                response = super().urlopen(request)
            except YtdlpHTTPError as exc:
                self.result["http_status"] = exc.status
                self.result["http_chain"].append(exc.status)
                location = exc.response.headers.get("Location")
                exc.response.close()
                if exc.status in _REDIRECT_STATUSES:
                    target = _redirect_target(request.url, location, self.result)
                    request = request.copy()  # Retain the same bounded Range.
                    request.url = target
                    continue
                raise
            self.responses.append(response)
            self.result["http_status"] = response.status
            self.result["http_chain"].append(response.status)
            if not _supported(response.headers):
                self.result["outcome"] = "unsupported_response"
                raise SourceError("The source did not return uncompressed media.")
            return response
        raise SourceError("The diagnostic request limit was reached.")

    def close(self):
        for response in self.responses:
            response.close()
        super().close()


class _CountingSink:
    """Discard bytes immediately, but independently enforce the sample cap."""
    def __init__(self):
        self.count = 0

    def write(self, data):
        if self.count + len(data) > SAMPLE_BYTES:
            raise SourceError("The native downloader exceeded its sample limit.")
        self.count += len(data)
        return len(data)

    def close(self):
        pass


class _MemoryHttpFD(HttpFD):
    def __init__(self, ydl, params, sink):
        super().__init__(ydl, params)
        self.sink = sink

    def sanitize_open(self, filename, open_mode):
        return self.sink, filename  # No file, stdout stream, or temporary video.


def _native_sample(url: str, headers: dict) -> dict:
    result = {"outcome": "error", "http_status": None, "http_chain": [], "bytes_read": 0}
    started = time.monotonic()
    sink = _CountingSink()
    params = {"quiet": True, "no_warnings": True, "noprogress": True,
              "logger": QuietLogger(), "cachedir": False, "proxy": "",
              "source_address": "0.0.0.0", "socket_timeout": SOCKET_TIMEOUT,
              "test": True, "retries": 0, "continuedl": False, "nopart": True,
              "updatetime": False, "http_headers": headers}
    try:
        validate_upstream(url)
        with _ProbeYDL(params, result) as ydl:
            downloader = _MemoryHttpFD(ydl, params, sink)
            if downloader._TEST_FILE_SIZE != SAMPLE_BYTES:
                raise SourceError("The pinned yt-dlp test-size contract changed.")
            # Same native HTTP downloader HlsFD uses for fragments; test mode
            # clamps body reads even if the origin ignores Range or length.
            ok = downloader.real_download("memory-probe", {"url": url, "http_headers": headers})
            result["outcome"] = "ok" if ok and sink.count else "empty"
    except YtdlpHTTPError:
        result["outcome"] = "http_error"
    except Exception as exc:
        result["exception"] = type(exc).__name__  # Never str(exc): it may include signed URLs.
    result["bytes_read"] = sink.count
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


async def _httpx_sample(url: str, headers: dict) -> dict:
    result = {"outcome": "error", "http_status": None, "http_chain": [], "bytes_read": 0}
    started = time.monotonic()
    relay = Relay()
    try:
        for _ in range(MAX_REQUESTS):
            validate_upstream(url)
            relay.client.cookies.clear()
            async with relay.client.stream("GET", url, headers=headers,
                                           follow_redirects=False, timeout=SOCKET_TIMEOUT) as response:
                result["http_status"] = response.status_code
                result["http_chain"].append(response.status_code)
                if response.status_code in _REDIRECT_STATUSES:
                    url = _redirect_target(url, response.headers.get("Location"), result)
                    continue  # The context closes this hop without reading its body.
                if response.status_code not in {200, 206}:
                    result["outcome"] = "http_error"
                elif not _supported(response.headers):
                    result["outcome"] = "unsupported_response"
                else:
                    async for chunk in response.aiter_raw(SAMPLE_BYTES):
                        result["bytes_read"] += min(len(chunk), SAMPLE_BYTES - result["bytes_read"])
                        if result["bytes_read"] >= SAMPLE_BYTES:
                            break
                    result["outcome"] = "ok" if result["bytes_read"] else "empty"
                break
    except Exception as exc:
        result["exception"] = type(exc).__name__
    finally:
        await relay.client.aclose()
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def _clean_result(raw: dict) -> dict:
    """Allow only fixed labels/numbers across the worker-to-UI boundary."""
    result = {"outcome": raw["outcome"], "http_status": raw.get("http_status"),
              "http_chain": raw["http_chain"], "bytes_read": raw["bytes_read"], "seconds": raw["seconds"]}
    chain = result["http_chain"]
    if (result["outcome"] not in _OUTCOMES
            or not isinstance(chain, list) or len(chain) > MAX_REQUESTS
            or any(not isinstance(code, int) or not 100 <= code <= 599 for code in chain)
            or (chain and chain[-1] != result["http_status"])
            or (not chain and result["http_status"] is not None)
            or not isinstance(result["bytes_read"], int) or not 0 <= result["bytes_read"] <= SAMPLE_BYTES
            or not isinstance(result["seconds"], (int, float))
            or not math.isfinite(result["seconds"]) or not 0 <= result["seconds"] <= WORKER_TIMEOUT
            or (result["http_status"] is not None and
                (not isinstance(result["http_status"], int) or not 100 <= result["http_status"] <= 599))):
        raise ValueError("Invalid diagnostic result")
    exception = raw.get("exception", "")
    if exception and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception):
        result["exception"] = exception
    return result


def interpretation(native: dict, relay: dict) -> str:
    if {native["outcome"], relay["outcome"]} & {"redirect_blocked", "redirect_limit", "invalid_redirect"}:
        return "Inconclusive: a redirect was unsafe, missing a destination, or exceeded the request limit. No unvalidated destination was contacted; inspect the HTTP chains."
    if native["outcome"] == relay["outcome"] == "ok":
        return "Both clients read media bytes now. The earlier failure may be transient, expired, or request-specific; full playback is not yet proven."
    if native["outcome"] == "ok" and relay["http_status"] == 403:
        return "yt-dlp read media, but HTTPX was denied on the same signed URL and sample range. Investigate transport/header differences before blaming a blanket IP block."
    if native["http_status"] == relay["http_status"] == 403:
        return "Both clients were denied on the existing signed URL. This is not unique to HTTPX; URL validity, source authorization, IP/session binding or attestation still need investigation."
    if relay["outcome"] == "ok":
        return "HTTPX read media, but the native sample did not. Check the native status/error; this does not demonstrate a relay-specific denial."
    return "Inconclusive: compare the individual status codes and exception types. No full-video download or browser fallback was attempted."


def run_segment_probe(ticket: Ticket) -> dict:
    with ticket.lock:
        target = ticket.probe_target
    if target is None:
        return {"state": "not_ready", "message": "No media request captured yet. Attempt playback first, then run this diagnostic."}
    remaining = max(0, ticket.video.available_at - time.time())
    if remaining:
        return {"state": "not_ready", "message": f"The source availability wait has {math.ceil(remaining)} seconds remaining. Try after the countdown."}
    if not _gate.acquire(blocking=False):
        return {"state": "busy", "message": "Another segment comparison is running on this instance."}
    try:
        validate_upstream(target.resource.url)
        headers = upstream_headers(target.resource)
        headers["Range"] = sample_range(target.headers)
        payload = json.dumps({"url": target.resource.url, "headers": headers})
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker"], input=payload,
            text=True, capture_output=True, timeout=WORKER_TIMEOUT, check=False,
        )
        if completed.returncode:
            return {"state": "error", "message": "The isolated diagnostic worker failed. No raw output was exposed."}
        raw = json.loads(completed.stdout)
        native, relay = _clean_result(raw["native_ytdlp"]), _clean_result(raw["relay_httpx"])
        return {"state": "complete", "build": BUILD, "client_profile": ticket.video.client_profile,
                "sample_limit_bytes_per_client": SAMPLE_BYTES, "sample_range": headers["Range"],
                "original_http_status": target.http_status,
                "original_request_had_range": any(k.lower() == "range" for k in target.headers),
                "max_requests_per_client": MAX_REQUESTS,
                "scope": "Same existing URL; matched bounded range; direct IPv4; redirects validated before every hop (HTTPS Googlevideo only). Native yt-dlp HTTP fragment downloader (urllib), not fresh extraction or full HLS playback.",
                "native_ytdlp": native, "relay_httpx": relay,
                "interpretation": interpretation(native, relay)}
    except subprocess.TimeoutExpired:
        return {"state": "timeout", "message": f"The comparison exceeded {WORKER_TIMEOUT} seconds. Its worker was terminated; no background probe remains."}
    except Exception as exc:
        return {"state": "error", "message": f"Segment comparison failed ({type(exc).__name__}). No raw output was exposed."}
    finally:
        _gate.release()


def _worker():
    try:
        payload = json.load(sys.stdin)
        url = validate_upstream(payload["url"])
        headers = upstream_headers(Resource(url, payload["headers"]))
        headers["Range"] = sample_range(payload["headers"])
        native = _native_sample(url, headers)
        relay = asyncio.run(_httpx_sample(url, headers))
        print(json.dumps({"native_ytdlp": native, "relay_httpx": relay}, allow_nan=False))
    except Exception:
        # Parent reports a fixed error; traceback/URLs must never reach UI logs.
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker())