import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import token_provider as provider
from sources import SourceError, resolve_video
from streaming import Ticket


def archive_bytes(name=None, *, symlink=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name or f"bgutil-ytdlp-pot-provider-{provider.COMMIT}/server/src/generate_once.ts")
        if symlink:
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/private"
        else:
            entry.size = 4
        archive.addfile(entry, None if symlink else io.BytesIO(b"test"))
    return stream.getvalue()


def test_archive_hash_is_verified_before_extracting(tmp_path):
    with pytest.raises(provider.ProviderSetupError, match="integrity"):
        provider._unpack(b"untrusted", tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name, symlink", [
    ("../outside", False), ("/absolute", False), ("wrong-root/file", False),
    (f"bgutil-ytdlp-pot-provider-{provider.COMMIT}/link", True),
])
def test_archive_rejects_traversal_and_links(tmp_path, name, symlink):
    body = archive_bytes(name, symlink=symlink)
    with patch.object(provider, "ARCHIVE_SHA256", hashlib.sha256(body).hexdigest()):
        with pytest.raises(provider.ProviderSetupError, match="unsafe"):
            provider._unpack(body, tmp_path)


def setup_mocks(tmp_path, completed):
    body = archive_bytes()
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_bytes.return_value = [body]
    return (patch.object(provider, "CACHE", tmp_path / "cache"),
            patch.object(provider, "ARCHIVE_SHA256", hashlib.sha256(body).hexdigest()),
            patch.object(provider.httpx, "stream", return_value=response),
            patch.object(provider, "run_private", return_value=completed))


def test_setup_once_and_cached_afterwards(tmp_path):
    cache, checksum, download, install = setup_mocks(tmp_path, SimpleNamespace(returncode=0))
    with cache, checksum, download as get, install as run:
        first = provider.prepare_provider("/safe/deno")
        assert first == provider.prepare_provider("/safe/deno")
        assert (first / "src" / "generate_once.ts").is_file()
    get.assert_called_once()
    run.assert_called_once()
    assert get.call_args.args[1] == provider.ARCHIVE_URL
    assert get.call_args.kwargs["trust_env"] is False
    assert run.call_args.args[0] == ["/safe/deno", "install", "--allow-scripts=npm:canvas", "--frozen"]
    assert run.call_args.kwargs["timeout"] == provider.SETUP_TIMEOUT


def test_failed_setup_cleans_temporary_files_and_can_retry(tmp_path):
    cache, checksum, download, install = setup_mocks(tmp_path, SimpleNamespace(returncode=1, stderr="secret"))
    with cache, checksum, download, install as run:
        with pytest.raises(provider.ProviderSetupError) as error:
            provider.prepare_provider("/safe/deno")
        assert "secret" not in str(error.value)
        assert not (provider.CACHE / "ready").exists()
        assert not list(provider.CACHE.glob("setup-*"))
        run.return_value = SimpleNamespace(returncode=0)
        assert provider.prepare_provider("/safe/deno").is_dir()


def test_private_process_captures_stdout_and_stderr():
    result = provider.run_private(
        [sys.executable, "-c", "import sys; print('data'); print('private', file=sys.stderr)"], timeout=5)
    assert result.returncode == 0 and result.stdout.strip() == "data" and result.stderr.strip() == "private"


def test_private_timeout_terminates_process_group():
    process = MagicMock(pid=4321)
    process.__enter__.return_value = process
    process.communicate.side_effect = [subprocess.TimeoutExpired("worker", 1), ("", "")]
    with patch.object(provider.subprocess, "Popen", return_value=process) as spawn, \
            patch.object(provider.os, "killpg") as kill:
        with pytest.raises(subprocess.TimeoutExpired):
            provider.run_private(["worker"], timeout=1)
    assert spawn.call_args.kwargs["start_new_session"] is True
    kill.assert_called_once_with(4321, provider.signal.SIGKILL)
    assert process.communicate.call_count == 2


def source_info(*, token=True):
    return {"title": "Example", "duration": 60, "formats": [{
        "url": "https://r.googlevideo.com/video?signature=private" + ("&pot=private-token" if token else ""),
        "protocol": "https", "ext": "mp4", "height": 360, "vcodec": "avc1.4D401F", "acodec": "mp4a.40.2",
    }]}


def test_mobile_profile_uses_local_provider_and_metadata_only_worker():
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(source_info()), stderr="private provider log")
    with patch("sources.prepare_provider", return_value=Path("/safe/provider/server")), \
            patch("sources.run_private", return_value=completed) as run:
        video = resolve_video("jNQXAC9IVRw", client_profile="mweb_pot")
    assert video.po_token_attached and video.client_profile == "mweb_pot" and video.mode == "mp4"
    assert "private" not in repr(video)
    snapshot = Ticket(video).snapshot()
    assert snapshot["po_token_attached"] is True
    assert "private" not in json.dumps(snapshot)
    payload = json.loads(run.call_args.kwargs["input"])
    options = payload["options"]
    assert options["extractor_args"]["youtube"] == {"player_client": ["mweb"], "fetch_pot": ["always"]}
    assert options["extractor_args"]["youtubepot-bgutilscript"]["server_home"] == ["/safe/provider/server"]
    assert options["skip_download"] is True and options["proxy"] == "" and options["source_address"] == "0.0.0.0"
    assert not any("cookie" in key for key in options)


def test_token_profile_refuses_silent_tokenless_fallback():
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(source_info(token=False)))
    with patch("sources.prepare_provider", return_value=Path("/safe/provider/server")), \
            patch("sources.run_private", return_value=completed), \
            pytest.raises(SourceError, match="token-bearing"):
        resolve_video("jNQXAC9IVRw", client_profile="mweb_pot")


@pytest.mark.parametrize("completed", [
    SimpleNamespace(returncode=1, stdout="private", stderr="secret"),
    SimpleNamespace(returncode=0, stdout="private-not-json", stderr="secret"),
])
def test_provider_errors_never_expose_child_output(completed):
    with patch("sources.prepare_provider", return_value=Path("/safe/provider/server")), \
            patch("sources.run_private", return_value=completed), pytest.raises(SourceError) as error:
        resolve_video("jNQXAC9IVRw", client_profile="mweb_pot")
    assert "private" not in str(error.value) and "secret" not in str(error.value)


def test_token_extraction_timeout_is_safe():
    with patch("sources.prepare_provider", return_value=Path("/safe/provider/server")), \
            patch("sources.run_private", side_effect=subprocess.TimeoutExpired("worker", 90, stderr="secret")), \
            pytest.raises(SourceError, match="90 seconds"):
        resolve_video("jNQXAC9IVRw", client_profile="mweb_pot")


@pytest.mark.parametrize("profile", ["auto", "web_safari"])
def test_ordinary_profiles_do_not_prepare_or_fetch_tokens(profile):
    ydl = MagicMock()
    ydl.__enter__.return_value.extract_info.return_value = source_info(token=False)
    with patch("sources.YoutubeDL", return_value=ydl) as factory, \
            patch("sources.prepare_provider") as prepare, patch("sources.run_private") as worker:
        video = resolve_video("jNQXAC9IVRw", client_profile=profile)
    prepare.assert_not_called()
    worker.assert_not_called()
    assert factory.call_args.args[0]["extractor_args"]["youtube"]["fetch_pot"] == ["never"]
    assert not video.po_token_attached