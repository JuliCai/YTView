"""Resolve metadata only. Upstream URLs must never be rendered in Streamlit."""

from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from deno import find_deno_bin
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from token_provider import ProviderSetupError, prepare_provider, run_private

CLIENT_PROFILES = {"auto": "Automatic (yt-dlp default)", "web_safari": "Safari HLS (server-side)",
                   "mweb_pot": "Mobile web + instance PO token"}
MAX_SOURCE_WAIT = 120
TOKEN_EXTRACT_TIMEOUT = 90


class SourceError(Exception):
    """A safe, user-facing extraction error (no signed URLs or cookies)."""


def extract_video_id(value: str) -> str | None:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            return None
        host = parsed.hostname
        parts = parsed.path.strip("/").split("/")
        if host == "youtu.be" and len(parts) == 1:
            candidate = parts[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                candidate = parse_qs(parsed.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
            else:
                return None
        else:
            return None
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None
    except ValueError:
        return None


def validate_upstream(url: str, *, thumbnail: bool = False) -> str:
    """Allow only YouTube's media CDN, never arbitrary client-supplied destinations."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = host == "i.ytimg.com" if thumbnail else (
            host == "googlevideo.com" or host.endswith(".googlevideo.com")
        )
        if (parsed.scheme != "https" or not allowed or parsed.port not in {None, 443}
                or parsed.username or parsed.password or parsed.fragment
                or any(c in url for c in "\r\n\\")):
            raise ValueError
    except ValueError:
        raise SourceError("The source returned an unsupported media address.") from None
    return url


@dataclass(frozen=True)
class Track:
    url: str = field(repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    codec: str = ""
    height: int = 0
    bandwidth: int = 1_000_000
    available_at: float = 0.0


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    duration: float
    mode: str
    tracks: tuple[Track, ...]
    audio: Track | None = None
    client_profile: str = "auto"
    po_token_attached: bool = False

    @property
    def available_at(self) -> float:
        tracks = (*self.tracks, self.audio) if self.audio else self.tracks
        return max((track.available_at for track in tracks), default=0.0)


class QuietLogger:
    # yt-dlp messages may contain signed URLs. Never forward them to the UI/logs.
    def debug(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def select_video(info: dict, video_id: str, max_height: int) -> Video:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise SourceError("Use a finished video for now; live/DVR streams are not supported yet.")

    def track(fmt: dict) -> Track:
        try:
            available_at = float(fmt.get("available_at") or 0)
        except (TypeError, ValueError):
            raise SourceError("YouTube returned invalid playback availability timing.") from None
        if not math.isfinite(available_at):
            raise SourceError("YouTube returned invalid playback availability timing.")
        return Track(
            validate_upstream(fmt["url"]),
            {**info.get("http_headers", {}), **fmt.get("http_headers", {})},
            (fmt.get("vcodec") if fmt.get("vcodec") != "none" else fmt.get("acodec")) or "",
            int(fmt.get("height") or 0),
            max(64_000, int((fmt.get("tbr") or (128 if fmt.get("vcodec") == "none" else 1000)) * 1000)),
            available_at=available_at,
        )

    formats = [f for f in info.get("formats", []) if f.get("url") and not f.get("has_drm")]
    hls = [f for f in formats if f.get("protocol") in {"m3u8", "m3u8_native"}]
    audios = [f for f in hls if f.get("vcodec") == "none"
                and (str(f.get("acodec", "")).startswith("mp4a")
                    or (f.get("acodec") is None and f.get("ext") in {"mp4", "m4a"}))]
    videos = [f for f in hls if str(f.get("vcodec", "")).startswith("avc1")
              and 0 < (f.get("height") or 0) <= max_height]
    audio = max(audios, key=lambda f: (f.get("language_preference") or 0, f.get("tbr") or 0)) if audios else None
    usable = [f for f in videos if f.get("acodec") not in {None, "none"} or audio]
    if usable:
        by_height: dict[int, dict] = {}
        for fmt in sorted(usable, key=lambda f: (f.get("fps") or 0, f.get("tbr") or 0)):
            by_height[int(fmt["height"])] = fmt
        chosen = [by_height[h] for h in sorted(by_height)]
        # Don't mix muxed and separate-audio variants within one audio group.
        separate = [f for f in chosen if f.get("acodec") in {None, "none"}]
        if separate and audio:
            return Video(video_id, info.get("title") or video_id, float(info.get("duration") or 0),
                         "hls", tuple(track(f) for f in separate), track(audio))
        return Video(video_id, info.get("title") or video_id, float(info.get("duration") or 0),
                     "hls", tuple(track(f) for f in chosen))

    progressive = [f for f in formats if f.get("protocol") == "https"
                   and f.get("ext") == "mp4" and str(f.get("vcodec", "")).startswith("avc1")
                   and f.get("acodec") not in {None, "none"}
                   and 0 < (f.get("height") or 0) <= max_height]
    if progressive:
        fmt = max(progressive, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return Video(video_id, info.get("title") or video_id, float(info.get("duration") or 0),
                     "mp4", (track(fmt),))
    raise SourceError("YouTube did not provide a compatible stream with audio. Try another video.")


def resolve_video(video_id: str, max_height: int = 720, *, client_profile: str = "auto") -> Video:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise SourceError("Invalid video ID.")
    if client_profile not in CLIENT_PROFILES:
        raise SourceError("Unsupported YouTube client profile.")
    options = {
        "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "skip_download": True, "noplaylist": True, "cachedir": False,
        "socket_timeout": 12, "retries": 1, "extractor_retries": 1,
        # Match the relay: don't resolve IP-bound URLs through an environment
        # proxy or a different address family than the media requests.
        "proxy": "", "source_address": "0.0.0.0",
        "js_runtimes": {"deno": {"path": str(find_deno_bin())}},
        # Installing a plugin must not turn the existing profiles into hidden
        # token-generation attempts. Only the explicit token profile opts in.
        "extractor_args": {"youtube": {"fetch_pot": ["never"]}},
    }
    if client_profile == "mweb_pot":
        try:
            server = prepare_provider(options["js_runtimes"]["deno"]["path"])
        except ProviderSetupError as exc:
            raise SourceError(str(exc)) from None
        options["extractor_args"] = {
            "youtube": {"player_client": ["mweb"], "fetch_pot": ["always"]},
            "youtubepot-bgutilscript": {"server_home": [str(server)]},
        }
    elif client_profile != "auto":
        options["extractor_args"]["youtube"]["player_client"] = [client_profile]
    try:
        if client_profile == "mweb_pot":
            # Some provider subprocesses write raw errors to stderr themselves.
            # Isolate extraction so neither those logs nor signed URLs reach UI/logs.
            worker_options = {k: v for k, v in options.items() if k != "logger"}
            try:
                result = run_private(
                    [sys.executable, str(Path(__file__).resolve()), "--token-worker"],
                    input=json.dumps({"video_id": video_id, "options": worker_options}),
                    timeout=TOKEN_EXTRACT_TIMEOUT,
                )
                if result.returncode:
                    raise SourceError("Token-enabled extraction failed on the instance. No raw provider logs were exposed.")
                info = json.loads(result.stdout)
            except subprocess.TimeoutExpired:
                raise SourceError("Token-enabled extraction exceeded 90 seconds. Its worker and token process were stopped.") from None
            except (ValueError, OSError):
                raise SourceError("Token-enabled extraction returned an invalid result. No raw provider logs were exposed.") from None
        else:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except DownloadError as exc:
        message = str(exc).lower()
        if any(word in message for word in ("bot", "sign in", "403", "429")):
            raise SourceError(
                "YouTube refused this instance's request (sign-in/bot check or rate limit). "
                "No direct-browser fallback was attempted. Try another video and report this message."
            ) from None
        raise SourceError("The instance could not read this video from YouTube. It may be unavailable or restricted.") from None
    if not isinstance(info, dict):
        raise SourceError("YouTube returned no video metadata.")
    if client_profile == "mweb_pot":
        # Do not silently return to tokenless playback when a provider fails.
        # yt-dlp appends GVS tokens to HTTPS queries or HLS manifest paths.
        def has_token(fmt):
            parsed = urlsplit(fmt.get("url") or "")
            return bool(parse_qs(parsed.query).get("pot") or re.search(r"/pot/[^/]+", parsed.path))

        info = {**info, "formats": [fmt for fmt in info.get("formats", []) if has_token(fmt)]}
        if not info["formats"]:
            raise SourceError("The instance did not obtain a token-bearing media URL. Token generation or mobile-web extraction failed; no tokenless fallback was attempted.")
    video = select_video(info, video_id, max_height)
    return replace(video, client_profile=client_profile, po_token_attached=client_profile == "mweb_pot")


def _token_worker() -> int:
    try:
        payload = json.load(sys.stdin)
        video_id = payload["video_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return 1
        options = payload["options"]
        options["logger"] = QuietLogger()
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            print(json.dumps(ydl.sanitize_info(info)))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_token_worker() if sys.argv[1:] == ["--token-worker"] else 2)