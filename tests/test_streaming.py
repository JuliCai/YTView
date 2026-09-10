import asyncio
from contextlib import aclosing
from unittest.mock import patch
from urllib.parse import urljoin

import httpx
import pytest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application, HTTPError, RequestHandler

from sources import SourceError, Track, Video
from streaming import (BUILD, CHUNK_SIZE, MAX_RANGE, REGISTRY, Registry, Resource, Ticket,
                       master_playlist, rewrite_manifest, upstream_headers)
from streamlit_proxy import mount_routes

BASE = "/prefix/_ytview"
VIDEO = Video("jNQXAC9IVRw", "Example", 60, "hls",
              (Track("https://r.googlevideo.com/video.m3u8", {}, "avc1.4D401F", 720),),
              Track("https://r.googlevideo.com/audio.m3u8", {}, "mp4a.40.2"))
SOURCE = Resource("https://r.googlevideo.com/path/index.m3u8", {}, "manifest")


def test_all_playlist_urls_are_local_and_stable():
    ticket = Ticket(VIDEO)
    text = '''#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-KEY:METHOD=AES-128,URI="https://r.googlevideo.com/key"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:5,
https://r.googlevideo.com/segment.ts?signature=secret
#EXT-X-BYTERANGE:100@200
//r.googlevideo.com/next.ts
#EXT-X-ENDLIST
'''
    result = rewrite_manifest(text, SOURCE, ticket, BASE)
    assert "googlevideo" not in result and "secret" not in result
    assert result.count(BASE + "/resource/") == 4
    assert "#EXT-X-BYTERANGE:100@200" in result
    assert result == rewrite_manifest(text, SOURCE, ticket, BASE)
    assert len(ticket.resources) == 4


def test_nested_playlists_marked_for_rewriting():
    ticket = Ticket(VIDEO)
    text = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nvideo.m3u8\n'
    rewrite_manifest(text, SOURCE, ticket, BASE)
    assert all(r.kind == "manifest" for r in ticket.resources.values())
    master = master_playlist(ticket, BASE)
    assert 'AUDIO="audio"' in master and 'CODECS="avc1.4D401F,mp4a.40.2"' in master
    assert "https:" not in master


@pytest.mark.parametrize("body", ["<html>Error</html>", '#EXTM3U\n#EXT-X-CONTENT-STEERING:SERVER-URI="https://evil.test"',
                                 '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://127.0.0.1/key"',
                                 '#EXTM3U\n#EXT-X-KEY:URI=https://r.googlevideo.com/key',
                                 '#EXTM3U\n#EXT-X-UNKNOWN:URI="https://r.googlevideo.com/foo"'])
def test_bad_manifests_fail_closed(body):
    with pytest.raises(SourceError):
        rewrite_manifest(body, SOURCE, Ticket(VIDEO), BASE)


@pytest.mark.parametrize("value", ["bytes=0-1,20-30", "bytes=20-1", "bytes=-0", "bytes=-", "items=0-3"])
def test_bad_ranges_rejected(value):
    with pytest.raises(HTTPError):
        upstream_headers(SOURCE, value)


def test_range_and_sensitive_headers():
    source = Resource(SOURCE.url, {"Cookie": "secret", "Authorization": "secret", "User-Agent": "ytview"})
    headers = upstream_headers(source, "bytes=100-")
    assert headers["Range"] == f"bytes=100-{100 + MAX_RANGE - 1}"
    assert "Cookie" not in headers and "Authorization" not in headers
    assert upstream_headers(source, "bytes=-128")["Range"] == "bytes=-128"
    assert upstream_headers(source, "bytes=0-9999999")["Range"] == "bytes=0-9999999"


def test_expiration_and_unknown_tokens():
    registry = Registry()
    ticket = registry.create(VIDEO)
    ticket.created -= 7 * 60 * 60
    with pytest.raises(HTTPError) as exc:
        registry.get(ticket.token)
    assert exc.value.status_code == 410
    assert not registry.tickets


class CatchAll(RequestHandler):
    def get(self, path):
        self.finish("Streamlit UI")


class RelayHTTPTest(AsyncHTTPTestCase):
    def get_app(self):
        self.application = Application([(r"/(.*)", CatchAll)])
        mount_routes(self.application, BASE)
        self.relay = self.application.settings["ytview.relay"]
        self.ticket = REGISTRY.create(VIDEO)
        self.path = self.ticket.add(Resource("https://r.googlevideo.com/media", {}), BASE)
        self.upstream_calls = []
        return self.application

    def tearDown(self):
        self.io_loop.run_sync(self.relay.client.aclose)
        REGISTRY.discard(self.ticket.token)
        super().tearDown()

    def fake(self, code=200, body=b"media", headers=None):
        def handler(request):
            self.upstream_calls.append(request)
            return httpx.Response(code, headers=headers or {"Content-Type": "video/mp4"}, stream=httpx.ByteStream(body))
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_same_port_route_precedes_streamlit_catchall(self):
        before = len(self.application.default_router.rules)
        mount_routes(self.application, BASE)
        assert len(self.application.default_router.rules) == before
        assert self.fetch("/").body == b"Streamlit UI"
        response = self.fetch(f"{BASE}/master/{self.ticket.token}")
        assert response.code == 200 and response.body.startswith(b"#EXTM3U")
        assert b"googlevideo" not in response.body

    def test_player_csp_and_no_remote_urls(self):
        response = self.fetch(f"{BASE}/player/{self.ticket.token}")
        assert response.code == 200
        assert b'http-equiv="Content-Security-Policy"' in response.body
        assert b"connect-src 'self'" in response.body
        assert b"googlevideo" not in response.body and b"ytimg" not in response.body
        assert b"https://" not in response.body and b"{{" not in response.body

    def test_untrusted_player_library_rejected(self):
        with patch.object(self.relay, "client", self.fake(body=b"tampered script")):
            response = self.fetch(f"{BASE}/hls.js")
        assert response.code == 502
        assert self.relay.hls_js is None

    def test_saturation_preserves_retry_header(self):
        self.relay.active = 20
        response = self.fetch(self.path)
        assert response.code == 503
        assert response.headers["Retry-After"] == "2"

    def test_ranges_forward_and_backward(self):
        for first, last in [(700, 799), (0, 1), (100, 199)]:
            with patch.object(self.relay, "client", self.fake(206, b"x" * (last-first+1),
                              {"Content-Type": "video/mp4", "Content-Range": f"bytes {first}-{last}/1000"})):
                response = self.fetch(self.path, headers={"Range": f"bytes={first}-{last}"})
            assert response.code == 206
            assert response.headers["Content-Range"] == f"bytes {first}-{last}/1000"
            assert len(response.body) == last-first+1

    def test_head_has_no_body(self):
        with patch.object(self.relay, "client", self.fake(headers={"Content-Type": "video/mp4", "Content-Length": "5"})):
            response = self.fetch(self.path, method="HEAD")
        assert response.code == 200 and not response.body
        assert self.upstream_calls[0].method == "HEAD"

    def test_ignored_range_does_not_download_entire_file(self):
        with patch.object(self.relay, "client", self.fake()):
            response = self.fetch(self.path, headers={"Range": "bytes=100-200"})
        assert response.code == 502 and b"media" not in response.body

    def test_bad_partial_response_rejected(self):
        for content_range in ["", "bytes 99-98/100", "bytes 0-999/100", "bytes 20-30/100"]:
            with patch.object(self.relay, "client", self.fake(206, b"x", {
                "Content-Type": "video/mp4", "Content-Range": content_range,
            })):
                response = self.fetch(self.path, headers={"Range": "bytes=10-30"})
            assert response.code == 502

    def test_unsatisfiable_range_preserves_size(self):
        with patch.object(self.relay, "client", self.fake(416, headers={"Content-Range": "bytes */1000"})):
            response = self.fetch(self.path, headers={"Range": "bytes=5000-"})
        assert response.code == 416
        assert response.headers["Content-Range"] == "bytes */1000"

    def test_upstream_manifest_rewritten_over_http(self):
        path = self.ticket.add(SOURCE, BASE)
        with patch.object(self.relay, "client", self.fake(body=b"#EXTM3U\n#EXTINF:5,\nhttps://r.googlevideo.com/seg\n#EXT-X-ENDLIST\n")):
            response = self.fetch(path)
        assert response.code == 200
        assert response.headers["Content-Type"] == "application/vnd.apple.mpegurl"
        assert b"googlevideo" not in response.body
        assert b"../../resource/" in response.body

    def test_cloud_prefix_survives_master_and_nested_playlist_urls(self):
        public_origin = "https://app.test/~/+"
        response = self.fetch(f"{BASE}/master/{self.ticket.token}")
        master_url = public_origin + f"{BASE}/master/{self.ticket.token}"
        variant_path = response.body.decode().splitlines()[-1]
        variant_url = urljoin(master_url, variant_path)
        assert variant_url.startswith(public_origin + BASE + "/resource/")
        with patch.object(self.relay, "client", self.fake(body=b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:5,\nsegment.ts\n')):
            variant = self.fetch(variant_url.removeprefix(public_origin))
        segment_path = variant.body.decode().splitlines()[-1]
        assert urljoin(variant_url, segment_path).startswith(public_origin + BASE + "/resource/")
        assert b'URI="../../resource/' in variant.body
        assert b"googlevideo" not in variant.body

    def test_diagnostics_are_bounded_and_do_not_expose_urls_or_tokens(self):
        for _ in range(60):
            self.ticket.record("upstream response", kind="media", http=403)
        response = self.fetch(f"{BASE}/status/{self.ticket.token}")
        assert response.code == 200
        data = __import__("json").loads(response.body)
        assert data["build"] == BUILD and len(data["events"]) == 40
        assert b"googlevideo" not in response.body
        assert self.ticket.token.encode() not in response.body
        assert b"Cookie" not in response.body

    @gen_test
    async def test_runtime_discovery_uses_identity(self):
        from streamlit.web.server.browser_websocket_handler import BrowserWebSocketHandler
        from streamlit_proxy import _install

        runtime = object()
        app = Application([(r"/_stcore/stream", BrowserWebSocketHandler, {"runtime": runtime})])
        await _install(runtime, "/_ytview")
        assert "ytview.relay" in app.settings
        await app.settings["ytview.relay"].client.aclose()

    def test_redirects_do_not_escape_proxy(self):
        with patch.object(self.relay, "client", self.fake(302, headers={"Location": "https://127.0.0.1/secret"})):
            response = self.fetch(self.path, follow_redirects=False)
        assert response.code == 502
        assert "Location" not in response.headers and len(self.upstream_calls) == 1

    def test_error_bodies_and_cookies_are_not_forwarded(self):
        with patch.object(self.relay, "client", self.fake(403, b"https://youtube.com/private?secret=x", {"Set-Cookie": "secret"})):
            response = self.fetch(self.path)
        assert response.code == 502 and b"secret" not in response.body
        assert "Set-Cookie" not in response.headers

    def test_expired_cross_origin_and_invalid_resource(self):
        assert self.fetch(self.path, headers={"Sec-Fetch-Site": "cross-site"}).code == 403
        assert self.fetch(f"{BASE}/resource/{self.ticket.token}/" + "0" * 32).code == 404
        self.ticket.created -= 7 * 3600
        assert self.fetch(self.path).code == 410

    @gen_test
    async def test_first_chunk_arrives_before_upstream_finishes(self):
        release = asyncio.Event()
        first_chunk = asyncio.Event()

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * CHUNK_SIZE
                await release.wait()
                yield b"end"

        async def handler(request):
            return httpx.Response(200, headers={"Content-Type": "video/mp4"}, stream=SlowBody())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(self.relay, "client", client):
                result = self.http_client.fetch(self.get_url(self.path), streaming_callback=lambda chunk: first_chunk.set())
                try:
                    await asyncio.wait_for(first_chunk.wait(), 2)
                    assert not result.done()
                finally:
                    release.set()
                assert (await result).code == 200

    @gen_test
    async def test_disconnect_closes_upstream_and_releases_slot(self):
        first_chunk = asyncio.Event()
        closed = asyncio.Event()

        class PendingBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * CHUNK_SIZE
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        async def handler(request):
            return httpx.Response(200, headers={"Content-Type": "video/mp4"}, stream=PendingBody())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            with patch.object(self.relay, "client", upstream):
                async with httpx.AsyncClient() as browser:
                    async with browser.stream("GET", self.get_url(self.path)) as response:
                        async with aclosing(response.aiter_bytes()) as chunks:
                            await anext(chunks)
                            first_chunk.set()
                await asyncio.wait_for(closed.wait(), 2)
                assert first_chunk.is_set()
                assert self.relay.active == 0