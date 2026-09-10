"""Same-origin, bounded-memory HLS and HTTP-range relay. No transcoding or video files."""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import re
import secrets
import threading
import time
from urllib.parse import urljoin

import httpx
from tornado.iostream import StreamClosedError
from tornado.web import HTTPError, RequestHandler

from sources import MAX_SOURCE_WAIT, SourceError, Video, validate_upstream

CHUNK_SIZE = 64 * 1024
BUILD = "instance-streaming-v6.1"
MAX_MANIFEST = 2 * 1024 * 1024
MAX_RANGE = 2 * 1024 * 1024
TICKET_TTL = 6 * 60 * 60
HLS_JS_URL = "https://cdn.jsdelivr.net/npm/hls.js@1.6.13/dist/hls.min.js"
HLS_JS_SHA256 = "7c47cd97d7a6e7b98d9623dd8ed9a6d45af4be4085e0c2001cd7175c2b4cfb07"


@dataclass(frozen=True)
class Resource:
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    kind: str = "media"


@dataclass(frozen=True)
class ProbeTarget:
    resource: Resource = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    http_status: int | None = None


@dataclass
class Ticket:
    video: Video
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    created: float = field(default_factory=time.monotonic)
    resources: dict[str, Resource] = field(default_factory=dict, repr=False)
    error: str = ""
    requests: int = 0
    bytes_sent: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=40), repr=False)
    probe_target: ProbeTarget | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def record(self, stage: str, **details):
        # Callers provide only stage names, counts, HTTP codes and exception types.
        # Never put upstream URLs, headers, tokens or raw exceptions here.
        with self.lock:
            self.events.append({"seconds": round(time.monotonic() - self.created, 1),
                                "stage": stage, **details})

    def snapshot(self) -> dict:
        with self.lock:
            return {"build": BUILD, "mode": self.video.mode, "requests": self.requests,
                    "bytes": self.bytes_sent, "error": self.error,
                    "client_profile": self.video.client_profile,
                    "po_token_attached": self.video.po_token_attached,
                    "source_wait_seconds": round(max(0, self.video.available_at - time.time()), 1),
                    "network_policy": "direct IPv4 for extraction and relay",
                    "segment_comparison_available": self.probe_target is not None,
                    "registered_resources": len(self.resources), "events": list(self.events)}

    def add(self, resource: Resource, prefix: str) -> str:
        validate_upstream(resource.url, thumbnail=resource.kind == "thumbnail")
        key = hashlib.sha256((resource.kind + resource.url).encode()).hexdigest()[:32]
        with self.lock:
            if key not in self.resources and len(self.resources) >= 30_000:
                raise SourceError("This video has too many segments for this instance.")
            self.resources[key] = resource
        return f"{prefix}/resource/{self.token}/{key}"


class Registry:
    def __init__(self):
        self.tickets: OrderedDict[str, Ticket] = OrderedDict()
        self.lock = threading.RLock()

    def create(self, video: Video) -> Ticket:
        with self.lock:
            for key, ticket in list(self.tickets.items()):
                if time.monotonic() - ticket.created >= TICKET_TTL:
                    del self.tickets[key]
            if len(self.tickets) >= 32:
                raise SourceError("The instance has too many active videos. Please try again later.")
            ticket = Ticket(video)
            ticket.record("source selected", client_profile=video.client_profile,
                          source_wait_seconds=round(max(0, video.available_at - time.time()), 1))
            self.tickets[ticket.token] = ticket
            return ticket

    def get(self, token: str) -> Ticket:
        with self.lock:
            ticket = self.tickets.get(token)
            if ticket is None or time.monotonic() - ticket.created >= TICKET_TTL:
                self.tickets.pop(token, None)
                raise HTTPError(410, reason="Playback expired. Load the video again.")
            return ticket

    def discard(self, token: str):
        with self.lock:
            self.tickets.pop(token, None)


REGISTRY = Registry()


def master_playlist(ticket: Ticket, prefix: str) -> str:
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    if ticket.video.audio:
        audio = ticket.video.audio
        uri = ticket.add(Resource(audio.url, audio.headers, "manifest"), prefix)
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",DEFAULT=YES,AUTOSELECT=YES,URI="{uri}"')
    for track in ticket.video.tracks:
        bandwidth = track.bandwidth + (ticket.video.audio.bandwidth if ticket.video.audio else 0)
        attributes = f"BANDWIDTH={bandwidth}"
        if ticket.video.audio:
            codecs = f"{track.codec},{ticket.video.audio.codec}"
            if track.codec and ticket.video.audio.codec and re.fullmatch(r"[A-Za-z0-9.,_-]+", codecs):
                attributes += f',CODECS="{codecs}"'
            attributes += ',AUDIO="audio"'
        lines.extend([f"#EXT-X-STREAM-INF:{attributes}",
                      ticket.add(Resource(track.url, track.headers, "manifest"), prefix)])
    return "\n".join(lines) + "\n"


def rewrite_manifest(text: str, source: Resource, ticket: Ticket, prefix: str) -> str:
    if not text.lstrip().startswith("#EXTM3U"):
        raise SourceError("The upstream returned an invalid playlist.")
    playlist_tags = {"#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-RENDITION-REPORT"}
    binary_tags = {"#EXT-X-KEY", "#EXT-X-SESSION-KEY", "#EXT-X-MAP", "#EXT-X-PRELOAD-HINT"}
    output = []
    next_is_playlist = False

    def local(uri: str, kind: str) -> str:
        if "{$" in uri:
            raise SourceError("Playlist URL variables are not supported.")
        return ticket.add(Resource(urljoin(source.url, uri), source.headers, kind), prefix)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            output.append(local(line, "manifest" if next_is_playlist else "media"))
            next_is_playlist = False
            continue
        tag = line.split(":", 1)[0]
        if tag in {"#EXT-X-CONTENT-STEERING", "#EXT-X-DEFINE", "#EXT-X-SESSION-DATA"}:
            raise SourceError("The upstream uses an unsupported playlist extension.")
        if "URI=" in line:
            if tag not in playlist_tags | binary_tags:
                raise SourceError("The upstream uses an unsupported playlist URL.")
            matches = re.findall(r'(?<=[,:])URI="([^"]+)"', line)
            if len(matches) != 1:
                raise SourceError("The upstream returned an invalid playlist URL.")
            line = re.sub(r'(?<=[,:])URI="([^"]+)"',
                          lambda m: 'URI="' + local(m[1], "manifest" if tag in playlist_tags else "media") + '"', line)
        if re.search(r"https?://|//[A-Za-z0-9]", line):
            continue
        output.append(line)
        if tag == "#EXT-X-STREAM-INF":
            next_is_playlist = True
    return "\n".join(output) + "\n"


def upstream_headers(source: Resource, range_header: str | None = None) -> dict[str, str]:
    headers = {k: v for k, v in source.headers.items()
               if k.lower() in {"user-agent", "referer", "origin", "accept", "accept-language"}}
    headers["Accept-Encoding"] = "identity"
    if range_header:
        if len(range_header) > 100 or not re.fullmatch(r"bytes=(\d+-\d*|-\d+)", range_header):
            raise HTTPError(416, reason="Only a single byte range is supported.")
        start, end = range_header[6:].split("-")
        if start:
            first = int(start)
            last = int(end) if end else first + MAX_RANGE - 1
            if last < first:
                raise HTTPError(416, reason="Invalid byte range.")
            # Bound open-ended MP4 reads, but preserve explicit HLS byte ranges.
            range_header = f"bytes={first}-{last}"
        elif int(end) == 0:
            raise HTTPError(416, reason="Invalid byte range.")
        headers["Range"] = range_header
    return headers


class Relay:
    """Created on the server's asyncio loop. Reuses connections across segments."""
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10, pool=5), trust_env=False,
            transport=httpx.AsyncHTTPTransport(
                local_address="0.0.0.0", trust_env=False,
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
            ),
        )
        self.active = 0
        self.hls_js: bytes | None = None
        self.js_lock = asyncio.Lock()

    async def open(self, resource: Resource, headers: dict, method: str = "GET") -> httpx.Response:
        url = resource.url
        for _ in range(4):
            validate_upstream(url, thumbnail=resource.kind == "thumbnail")
            response = await self.client.send(self.client.build_request(method, url, headers=headers), stream=True)
            if response.is_redirect:
                location = response.headers.get("location", "")
                await response.aclose()
                url = urljoin(url, location)
                continue
            return response
        raise SourceError("The upstream redirected too many times.")


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes(CHUNK_SIZE):
        body.extend(chunk)
        if len(body) > limit:
            raise SourceError("The upstream response exceeded the safety limit.")
    return bytes(body)


class BaseHandler(RequestHandler):
    def initialize(self, relay: Relay, prefix: str):
        self.relay = relay
        self.prefix = prefix
        self.task = None

    def set_default_headers(self):
        self.set_header("Cache-Control", "private, no-store")
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("Referrer-Policy", "no-referrer")
        self.set_header("Cross-Origin-Resource-Policy", "same-origin")

    def prepare(self):
        if self.request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise HTTPError(403)

    def write_error(self, status_code, **kwargs):
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.finish(f"Playback request failed ({status_code}). Refresh the stream in YTView.")

    def on_connection_close(self):
        if self.task and not self.task.done():
            self.task.cancel()


class ManifestHandler(BaseHandler):
    def get(self, token):
        ticket = REGISTRY.get(token)
        ticket.record("master playlist requested")
        self.set_header("Content-Type", "application/vnd.apple.mpegurl")
        # Relative to /_ytview/master/<token>, preserving any cloud edge prefix.
        self.finish(master_playlist(ticket, ".."))


class StatusHandler(BaseHandler):
    def get(self, token):
        ticket = REGISTRY.get(token)
        ticket.record("browser status probe")
        self.finish(ticket.snapshot())


class ResourceHandler(BaseHandler):
    async def head(self, token, key):
        await self.get(token, key)

    async def get(self, token, key):
        ticket = REGISTRY.get(token)
        with ticket.lock:
            source = ticket.resources.get(key)
        if source is None:
            raise HTTPError(404)
        if self.relay.active >= 20:
            self.set_header("Retry-After", "2")
            self.set_status(503)
            self.finish("The instance is busy. Retry shortly.")
            return
        headers = upstream_headers(source, self.request.headers.get("Range"))
        self.task = asyncio.current_task()
        self.relay.active += 1
        response = None
        try:
            # yt-dlp's downloader honors available_at before touching media.
            # Enforce it here too; never rely solely on the browser countdown.
            if source.kind != "thumbnail":
                delay = max(0, ticket.video.available_at - time.time())
                if delay > MAX_SOURCE_WAIT:
                    raise SourceError("YouTube requires a wait longer than two minutes. Try loading this video later.")
                if delay:
                    ticket.record("waiting for source availability", wait_seconds=round(delay, 1))
                    await asyncio.sleep(delay)
            ticket.record("upstream request", kind=source.kind, method=self.request.method,
                          range_requested="Range" in headers,
                          url_reencoded=str(httpx.URL(source.url)) != source.url)
            if source.kind == "media" and self.request.method == "GET":
                with ticket.lock:
                    if ticket.probe_target is None:
                        ticket.probe_target = ProbeTarget(source, dict(headers))
            response = await self.relay.open(source, headers, self.request.method)
            if source.kind == "media" and self.request.method == "GET":
                with ticket.lock:
                    target = ticket.probe_target
                    if target and (target.resource == source or
                                   (response.status_code == 403 and target.http_status != 403)):
                        ticket.probe_target = ProbeTarget(source, dict(headers), response.status_code)
            ticket.record("upstream response", kind=source.kind, http=response.status_code)
            with ticket.lock:
                ticket.requests += 1
            if response.status_code == 416:
                if re.fullmatch(r"bytes \*/\d+", response.headers.get("content-range", "")):
                    self.set_header("Content-Range", response.headers["content-range"])
                self.set_status(416)
                self.finish()
                return
            if response.status_code not in {200, 206}:
                if response.status_code == 403 and source.kind == "media":
                    raise SourceError(
                        "YouTube denied the media segment (HTTP 403), even though its playlist was accessible. "
                        "If Automatic and Safari both fail, select Mobile web + instance PO token and click Load video. "
                        "A token does not guarantee access; persistent denial may require a different hosting IP. "
                        "No direct-browser fallback was attempted."
                    )
                raise SourceError(f"The video source returned HTTP {response.status_code}. Refresh the stream; if it repeats, report this code.")
            if headers.get("Range") and response.status_code != 206:
                raise SourceError("The source ignored a seek request. No full-download fallback was attempted.")
            if response.status_code == 206 and not re.fullmatch(r"bytes \d+-\d+/\d+", response.headers.get("content-range", "")):
                raise SourceError("The source returned an invalid byte range.")
            if response.status_code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers["content-range"])
                assert match is not None
                first, last, total = map(int, match.groups())
                if not 0 <= first <= last < total:
                    raise SourceError("The source returned inconsistent byte range bounds.")
                requested = headers.get("Range", "")[6:].split("-")
                if len(requested) == 2 and requested[0] and first != int(requested[0]):
                    raise SourceError("The source returned the wrong seek position.")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or int(length) != last - first + 1):
                    raise SourceError("The source returned an inconsistent byte range length.")
            if response.headers.get("content-encoding", "identity") != "identity":
                raise SourceError("The source returned an unsupported content encoding.")
            mime = response.headers.get("content-type", "").split(";")[0].lower()
            if source.kind == "manifest":
                if self.request.method == "HEAD":
                    self.set_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.finish()
                    return
                body = await read_bounded(response, MAX_MANIFEST)
                final_source = Resource(str(response.url), source.headers, source.kind)
                # This response lives at /_ytview/resource/<token>/<key>.
                rewritten = rewrite_manifest(body.decode("utf-8-sig"), final_source, ticket, "../..")
                ticket.record("playlist rewritten", size=len(body))
                self.set_header("Content-Type", "application/vnd.apple.mpegurl")
                self.finish(rewritten)
                return
            if source.kind == "thumbnail":
                if mime not in {"image/jpeg", "image/png", "image/webp"}:
                    raise SourceError("The source returned an invalid thumbnail.")
                body = await read_bounded(response, MAX_MANIFEST)
                ticket.record("thumbnail relayed", size=len(body))
                self.set_header("Content-Type", mime)
                self.finish(body)
                return
            if mime not in {"video/mp4", "audio/mp4", "video/mp2t", "audio/aac", "audio/mpeg",
                            "application/octet-stream", "binary/octet-stream"}:
                raise SourceError("The source returned an unsupported media response.")
            self.set_status(response.status_code)
            self.set_header("Content-Type", mime)
            for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if name in response.headers:
                    self.set_header(name, response.headers[name])
            self.set_header("X-Accel-Buffering", "no")
            if self.request.method != "HEAD":
                async for chunk in response.aiter_raw(CHUNK_SIZE):
                    self.write(chunk)
                    await self.flush()
                    with ticket.lock:
                        ticket.bytes_sent += len(chunk)
            self.finish()
        except (SourceError, httpx.HTTPError, UnicodeError) as exc:
            message = str(exc) if isinstance(exc, SourceError) else "The instance lost its upstream connection. Refresh the stream."
            ticket.record("upstream failure", kind=source.kind, exception=type(exc).__name__)
            with ticket.lock:
                ticket.error = message
            if not self._headers_written:
                raise HTTPError(502) from None
            if self.request.connection:
                self.request.connection.close()
        except (asyncio.CancelledError, StreamClosedError):
            pass
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                self.relay.active -= 1


class HlsScriptHandler(BaseHandler):
    async def get(self):
        async with self.relay.js_lock:
            if self.relay.hls_js is None:
                try:
                    async with self.relay.client.stream("GET", HLS_JS_URL) as response:
                        response.raise_for_status()
                        script = await read_bounded(response, MAX_MANIFEST)
                        if hashlib.sha256(script).hexdigest() != HLS_JS_SHA256:
                            raise SourceError("Player library integrity check failed.")
                        self.relay.hls_js = script
                except (httpx.HTTPError, SourceError):
                    raise HTTPError(502) from None
        self.set_header("Content-Type", "application/javascript")
        self.set_header("Cache-Control", "public, max-age=86400")
        self.finish(self.relay.hls_js)