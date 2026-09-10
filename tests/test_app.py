from pathlib import Path
import sys
from types import ModuleType
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
        player = app.get("iframe")[0].proto
        assert not player.src
        assert "Player HTML loaded" in player.srcdoc
        assert f"_ytview/status/{app.session_state['playback']['token']}" in player.srcdoc
        assert "fetch('/_ytview/" not in player.srcdoc
        assert app.json
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


def test_old_cached_player_shows_reboot_instructions_not_import_traceback():
    old_player = ModuleType("player")
    old_player.__file__ = str(Path("player.py").resolve())
    with patch.dict(sys.modules, {"player": old_player}):
        app = AppTest.from_file("app.py").run()
        assert not app.exception
        assert "Reboot app" in app.error[0].value
        assert "ImportError" in app.error[0].value
        assert not app.text_input
        assert not app.get("iframe")


def test_stale_routes_show_reboot_instructions():
    from streamlit_proxy import RestartRequired

    with patch("streamlit_proxy.ensure_proxy", side_effect=RestartRequired("Manage app → Reboot app")):
        app = AppTest.from_file("app.py").run()
        assert not app.exception
        assert "Reboot app" in app.error[0].value
        assert not app.text_input


def test_selected_client_profile_is_preserved_on_refresh():
    video = Video("jNQXAC9IVRw", "Example", 60, "hls",
                  (Track("https://r.googlevideo.com/video", {}, "avc1", 720),))
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), patch("sources.resolve_video", return_value=video) as resolve:
        app = AppTest.from_file("app.py").run()
        app.text_input[0].set_value("jNQXAC9IVRw")
        app.selectbox[1].select("web_safari")
        app.button[0].click().run()
        assert not app.exception
        resolve.assert_called_with("jNQXAC9IVRw", 720, client_profile="web_safari")
        app.button[1].click().run()
        assert not app.exception and resolve.call_count == 2
        resolve.assert_called_with("jNQXAC9IVRw", 720, client_profile="web_safari")
        REGISTRY.discard(app.session_state["playback"]["token"])