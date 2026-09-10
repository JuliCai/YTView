"""Offline browser checks, including Community Cloud's outer app iframe."""

import base64
import html
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from player import render_player
from sources import Track, Video
from streaming import Ticket

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser():
    with playwright.sync_playwright() as p:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if not executable:
            cached = sorted((Path.home() / "Library/Caches/ms-playwright").glob(
                "chromium-*/**/Contents/MacOS/Chromium"))
            executable = str(cached[-1]) if cached else None
        try:
            instance = p.chromium.launch(executable_path=executable, headless=True)
        except playwright.Error:
            pytest.skip("Chromium is not installed; set PLAYWRIGHT_CHROMIUM_EXECUTABLE to an existing browser.")
        yield instance
        instance.close()


def open_player(browser, status="ok", app_path="/~/+/", source_wait=0):
    ticket = Ticket(Video("jNQXAC9IVRw", "Example", 60, "hls",
                          (Track("https://r.googlevideo.com/video", {}, "avc1", 720),)))
    player = render_player(ticket, "_ytview")
    page = browser.new_page()
    page.clock.install()
    page.add_init_script("HTMLMediaElement.prototype.canPlayType = () => '';")
    requests = []
    errors = []
    page.on("pageerror", lambda error: errors.append(error.name))

    def serve(route):
        url = urlsplit(route.request.url)
        requests.append(url.path)
        assert url.netloc == "app.test", "Browser attempted an external request"
        if url.path == "/" and app_path != "/":
            route.fulfill(content_type="text/html", body=f'<iframe src="{app_path}"></iframe>')
        elif url.path == app_path:
            route.fulfill(content_type="text/html", body=f'<iframe srcdoc="{html.escape(player, quote=True)}"></iframe>')
        elif url.path.startswith(app_path + "_ytview/status/"):
            if status == "hung":
                return  # AbortController must end this pending request.
            if status == "html":
                route.fulfill(content_type="text/html", body="<html>Cloud hosting page</html>")
            elif status == "500":
                route.fulfill(status=500, content_type="text/plain", body="Internal server error.")
            else:
                route.fulfill(content_type="application/json", body=json.dumps(
                    {**ticket.snapshot(), "source_wait_seconds": source_wait}))
        elif url.path.startswith(app_path + "_ytview/resource/"):
            route.fulfill(content_type="image/png", body=base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII="))
        elif url.path == app_path + "_ytview/hls.js":
            route.fulfill(content_type="application/javascript", body="""
                window.Hls = class {
                    static isSupported() { return true; }
                    static Events = {ERROR:'error', MANIFEST_LOADED:'master', LEVEL_LOADED:'level', AUDIO_TRACK_LOADED:'audio'};
                    on() {} loadSource() {} attachMedia() {} stopLoad() {} destroy() {}
                };
            """)
        else:
            route.fulfill(status=404, body="Unexpected path")

    page.route("**/*", serve)
    page.goto("https://app.test/")
    frame = page.frame_locator("iframe")
    if app_path != "/":
        frame = frame.frame_locator("iframe")
    playwright.expect(frame.locator("#debug")).to_contain_text("JavaScript running")
    return page, frame, requests, errors


@pytest.mark.parametrize("app_path", ["/", "/~/+/", "/~/+/prefix/"])
def test_player_preserves_app_prefix_in_srcdoc(browser, app_path):
    page, frame, requests, errors = open_player(browser, app_path=app_path)
    playwright.expect(frame.locator("#debug")).to_contain_text("hls.js loaded.")
    playwright.expect(frame.locator("#debug")).to_contain_text("Thumbnail loaded through the instance.")
    assert all(path.startswith(app_path) for path in requests if "_ytview" in path)
    assert not errors
    page.close()


@pytest.mark.parametrize("status, expected", [
    ("html", "HTML/non-JSON instead of the proxy"),
    ("500", "HTTP 500"),
    ("hung", "timed out after 10 seconds"),
])
def test_route_failures_are_visible_before_any_media_fetch(browser, status, expected):
    page, frame, requests, errors = open_player(browser, status=status)
    if status == "hung":
        page.clock.fast_forward(11000)
    playwright.expect(frame.locator("#message")).to_contain_text(expected)
    assert not any("/resource/" in path or path.endswith("hls.js") for path in requests)
    assert not errors
    page.close()


def test_startup_cannot_spin_forever(browser):
    page, frame, _, errors = open_player(browser)
    playwright.expect(frame.locator("#debug")).to_contain_text("hls.js loaded.")
    page.clock.fast_forward(31000)
    playwright.expect(frame.locator("#message")).to_contain_text("startup timed out after 30 seconds")
    assert not errors
    page.close()


def test_source_wait_has_countdown_and_does_not_trip_startup_watchdog(browser):
    page, frame, requests, errors = open_player(browser, source_wait=40)
    playwright.expect(frame.locator("#message")).to_contain_text("pre-playback wait")
    assert not any(path.endswith("hls.js") for path in requests)
    page.clock.fast_forward(31000)
    playwright.expect(frame.locator("#message")).to_contain_text("pre-playback wait")
    assert not any(path.endswith("hls.js") for path in requests)
    page.clock.fast_forward(10000)
    playwright.expect(frame.locator("#debug")).to_contain_text("hls.js loaded.")
    assert not errors
    page.close()