from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from sources import SourceError, Track, Video
from streaming import REGISTRY


def test_ui_uses_only_local_player_and_does_not_resolve_on_rerun():
    video = Video("jNQXAC9IVRw", "![untrusted](https://i.ytimg.com/image)", 60, "hls",
                  (Track("https://r.googlevideo.com/video", {}, "avc1", 720),))
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), patch("sources.resolve_video", return_value=video) as resolve:
        app = AppTest.from_file("app.py").run()
        assert not app.exception
        assert resolve.call_count == 0
        app.text_input[0].set_value("https://youtu.be/jNQXAC9IVRw")
        app.button[0].click().run()
        assert not app.exception and resolve.call_count == 1
        assert app.text[0].value == video.title
        assert app.get("iframe")[0].proto.src.startswith("/_ytview/player/")
        app.run()
        assert resolve.call_count == 1
        REGISTRY.discard(app.session_state["playback"]["token"])


def test_extraction_failure_does_not_create_external_player():
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), patch("sources.resolve_video", side_effect=SourceError("Instance blocked")):
        app = AppTest.from_file("app.py").run()
        app.text_input[0].set_value("jNQXAC9IVRw")
        app.button[0].click().run()
        assert not app.exception
        assert app.error[0].value == "Instance blocked"
        assert not app.get("iframe") and not app.get("video")


def test_proxy_startup_failure_stops_before_input():
    with patch("streamlit_proxy.ensure_proxy", side_effect=RuntimeError("No server")):
        app = AppTest.from_file("app.py").run()
        assert not app.exception
        assert app.error
        assert not app.text_input