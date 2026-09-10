import builtins
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

import pytest
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


@pytest.mark.parametrize("missing_key", ["streaming", "unrelated_configuration_key"])
def test_streaming_import_keyerror_has_narrow_recovery(missing_key):
    actual_import = builtins.__import__

    def interrupted_import(name, *args, **kwargs):
        if name == "streaming":
            raise KeyError(missing_key)
        return actual_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=interrupted_import):
        app = AppTest.from_file("app.py").run()
    if missing_key != "streaming":
        assert app.exception
        assert not app.error
        return
    assert not app.exception
    assert "KeyError" in app.error[0].value
    assert "Reboot app" in app.error[0].value
    assert not app.text_input and not app.get("iframe")


@pytest.mark.parametrize("profile", ["web_safari", "mweb_pot"])
def test_selected_client_profile_is_preserved_on_refresh(profile):
    video = Video("jNQXAC9IVRw", "Example", 60, "hls",
                  (Track("https://r.googlevideo.com/video", {}, "avc1", 720),))
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), patch("sources.resolve_video", return_value=video) as resolve:
        app = AppTest.from_file("app.py").run()
        app.text_input[0].set_value("jNQXAC9IVRw")
        app.selectbox[1].select(profile)
        app.button[0].click().run()
        assert not app.exception
        resolve.assert_called_with("jNQXAC9IVRw", 720, client_profile=profile)
        next(button for button in app.button if button.key == "refresh_stream").click().run()
        assert not app.exception and resolve.call_count == 2
        resolve.assert_called_with("jNQXAC9IVRw", 720, client_profile=profile)
        REGISTRY.discard(app.session_state["playback"]["token"])


def test_segment_probe_runs_only_on_click_and_result_survives_rerun():
    video = Video("jNQXAC9IVRw", "Example", 60, "hls", (Track("https://r.googlevideo.com/video"),))
    report = {"state": "complete", "interpretation": "Both clients were denied", "sample_limit_bytes_per_client": 10241}
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), \
            patch("sources.resolve_video", return_value=video) as resolve, \
            patch("segment_probe.run_segment_probe", return_value=report) as probe:
        app = AppTest.from_file("app.py").run()
        app.text_input[0].set_value("jNQXAC9IVRw")
        app.button[0].click().run()
        probe.assert_not_called()
        next(button for button in app.button if button.key == "run_segment_comparison").click().run()
        assert not app.exception and probe.call_count == 1
        assert any("Both clients were denied" in message.value for message in app.info)
        app.run()
        assert not app.exception and probe.call_count == resolve.call_count == 1
        assert app.session_state["segment_comparison"]["result"] == report
        REGISTRY.discard(app.session_state["playback"]["token"])


def test_fresh_native_test_needs_no_playback_and_never_calls_resolver():
    report = {"state": "complete", "interpretation": "Fresh native sample read", "media_bytes": 10241}
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), \
            patch("sources.resolve_video") as resolve, \
            patch("native_probe.run_native_probe", return_value=report) as native, \
            patch("segment_probe.run_segment_probe") as captured:
        app = AppTest.from_file("app.py").run()
        native.assert_not_called()
        app.text_input[0].set_value("https://youtu.be/jNQXAC9IVRw")
        app.selectbox[1].select("mweb_pot")
        next(button for button in app.button if button.key == "run_native_probe").click().run()
        assert not app.exception
        native.assert_called_once_with("jNQXAC9IVRw", 720, client_profile="mweb_pot")
        resolve.assert_not_called()
        captured.assert_not_called()
        assert not app.get("iframe")
        assert any("Fresh native sample read" in item.value for item in app.info)
        assert app.session_state["native_test"]["profile"] == "mweb_pot"
        app.run()
        assert not app.exception and native.call_count == 1
        assert app.session_state["native_test"]["result"]["media_bytes"] == 10241


def test_invalid_native_input_does_not_run_any_download():
    with patch("streamlit_proxy.ensure_proxy", return_value="/_ytview"), \
            patch("sources.resolve_video") as resolve, patch("native_probe.run_native_probe") as native:
        app = AppTest.from_file("app.py").run()
        app.text_input[0].set_value("https://example.invalid/private")
        next(button for button in app.button if button.key == "run_native_probe").click().run()
        assert not app.exception and app.error
        native.assert_not_called()
        resolve.assert_not_called()