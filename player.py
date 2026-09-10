"""A same-origin player with a restrictive CSP and no external fallback."""

from pathlib import Path
import secrets

from streaming import BUILD, BaseHandler, REGISTRY, Resource, Ticket
from sources import MAX_SOURCE_WAIT


def render_player(ticket: Ticket, prefix: str) -> str:
    """Use document-relative paths: Cloud adds /~/+/ outside server.baseUrlPath."""
    token = ticket.token
    nonce = secrets.token_urlsafe(24)
    poster = ticket.add(Resource(
        f"https://i.ytimg.com/vi/{ticket.video.video_id}/hqdefault.jpg", {}, "thumbnail"
    ), prefix)
    if ticket.video.mode == "hls":
        src = f"{prefix}/master/{token}"
    else:
        track = ticket.video.tracks[0]
        src = ticket.add(Resource(track.url, track.headers), prefix)
    template = Path(__file__).with_name("player.html").read_text()
    for name, value in {"SOURCE": src, "POSTER": poster, "MODE": ticket.video.mode,
                        "BUILD": BUILD, "MAX_SOURCE_WAIT": str(MAX_SOURCE_WAIT),
                        "NONCE": nonce, "STATUS": f"{prefix}/status/{token}",
                        "LIBRARY": f"{prefix}/hls.js"}.items():
        template = template.replace("{{" + name + "}}", value)
    ticket.record("player HTML rendered")
    return template


class PlayerHandler(BaseHandler):
    def get(self, token):
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.finish(render_player(REGISTRY.get(token), ".."))