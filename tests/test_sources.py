import pytest
from unittest.mock import MagicMock, patch

from sources import SourceError, extract_video_id, resolve_video, select_video, validate_upstream

ID = "jNQXAC9IVRw"


@pytest.mark.parametrize("value", [ID, f"https://youtu.be/{ID}?t=3", f"https://www.youtube.com/watch?v={ID}&list=x",
                                  f"youtube.com/shorts/{ID}", f"https://m.youtube.com/live/{ID}",
                                  f"https://www.youtube.com/embed/{ID}"])
def test_video_ids(value):
    assert extract_video_id(value) == ID


@pytest.mark.parametrize("value", [f"https://evil.test/watch?v={ID}", f"https://youtube.com.evil.test/watch?v={ID}",
                                  f"https://youtube.com@evil.test/watch?v={ID}", ID + "X", "", "file:///etc/passwd"])
def test_reject_untrusted_input(value):
    assert extract_video_id(value) is None


@pytest.mark.parametrize("url", ["http://r.googlevideo.com/v", "https://127.0.0.1/v", "https://r.googlevideo.com.evil.test/v",
                                "https://r.googlevideo.com:8443/v", "https://x@r.googlevideo.com/v",
                                "https://youtube.com/watch?v=x", "file:///etc/passwd"])
def test_upstream_allowlist(url):
    with pytest.raises(SourceError):
        validate_upstream(url)


def format_info(height=720, **extra):
    return {"url": "https://r.googlevideo.com/video", "protocol": "m3u8_native", "ext": "mp4",
            "height": height, "vcodec": "avc1.4D401F", "acodec": "none", **extra}


def test_youtube_audio_without_declared_codec():
    # Real yt-dlp 2026.8.19 response: YouTube HLS audio itags 233/234 have acodec=None.
    info = {"formats": [format_info(360), format_info(720), format_info(1080),
                        format_info(0, url="https://r.googlevideo.com/audio", format_id="234", vcodec="none", acodec=None)]}
    video = select_video(info, ID, 720)
    assert video.mode == "hls"
    assert video.audio is not None
    assert [t.height for t in video.tracks] == [360, 720]


def test_never_silently_select_silent_video():
    with pytest.raises(SourceError, match="audio"):
        select_video({"formats": [format_info()]}, ID, 720)


def test_progressive_fallback_is_already_muxed():
    video = select_video({"formats": [format_info(protocol="https", acodec="mp4a.40.2")]}, ID, 720)
    assert video.mode == "mp4"


def test_live_rejected():
    with pytest.raises(SourceError, match="live"):
        select_video({"is_live": True}, ID, 720)


def test_preplayback_availability_is_preserved_for_audio_and_video():
    info = {"formats": [format_info(720, available_at=2_000_000_010),
                        format_info(0, url="https://r.googlevideo.com/audio", vcodec="none",
                                    acodec="mp4a.40.2", available_at=2_000_000_020)]}
    video = select_video(info, ID, 720)
    assert video.tracks[0].available_at == 2_000_000_010
    assert video.audio is not None
    assert video.audio.available_at == 2_000_000_020
    assert video.available_at == 2_000_000_020


def test_extractor_and_relay_use_same_direct_ipv4_policy():
    ydl = MagicMock()
    ydl.__enter__.return_value.extract_info.return_value = {
        "formats": [format_info(acodec="mp4a.40.2")]}
    with patch("sources.YoutubeDL", return_value=ydl) as factory:
        resolve_video(ID)
    options = factory.call_args.args[0]
    assert options["proxy"] == ""
    assert options["source_address"] == "0.0.0.0"


def test_safari_profile_is_explicit_and_server_side():
    ydl = MagicMock()
    ydl.__enter__.return_value.extract_info.return_value = {
        "formats": [format_info(acodec="mp4a.40.2")]}
    with patch("sources.YoutubeDL", return_value=ydl) as factory:
        video = resolve_video(ID, client_profile="web_safari")
    assert factory.call_args.args[0]["extractor_args"] == {"youtube": {"player_client": ["web_safari"], "fetch_pot": ["never"]}}
    assert video.client_profile == "web_safari"
    ydl.__enter__.return_value.extract_info.assert_called_once_with(
        f"https://www.youtube.com/watch?v={ID}", download=False)


def test_unknown_profile_does_not_start_extraction():
    with patch("sources.YoutubeDL") as factory, pytest.raises(SourceError, match="profile"):
        resolve_video(ID, client_profile="untrusted")
    factory.assert_not_called()


@pytest.mark.parametrize("timing", [float("nan"), float("inf"), "not-a-time"])
def test_invalid_source_timing_rejected(timing):
    with pytest.raises(SourceError, match="timing"):
        select_video({"formats": [format_info(acodec="mp4a.40.2", available_at=timing)]}, ID, 720)