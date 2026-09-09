"""A same-origin player with a restrictive CSP and no external fallback."""

from pathlib import Path
import secrets

from streaming import BaseHandler, REGISTRY, Resource


class PlayerHandler(BaseHandler):
    def get(self, token):
        ticket = REGISTRY.get(token)
        nonce = secrets.token_urlsafe(24)
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Content-Security-Policy", (
            f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; img-src 'self'; media-src 'self' blob:; "
            "connect-src 'self'; worker-src blob:; base-uri 'none'; form-action 'none'"
        ))
        poster = ticket.add(Resource(
            f"https://i.ytimg.com/vi/{ticket.video.video_id}/hqdefault.jpg", {}, "thumbnail"
        ), self.prefix)
        if ticket.video.mode == "hls":
            src = f"{self.prefix}/master/{token}"
        else:
            track = ticket.video.tracks[0]
            src = ticket.add(Resource(track.url, track.headers), self.prefix)
        template = Path(__file__).with_name("player.html").read_text()
        for name, value in {"SOURCE": src, "POSTER": poster, "MODE": ticket.video.mode,
                            "NONCE": nonce,
                            "STATUS": f"{self.prefix}/status/{token}",
                            "LIBRARY": f"{self.prefix}/hls.js"}.items():
            template = template.replace("{{" + name + "}}", value)
        self.finish(template)