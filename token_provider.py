"""Install the pinned, local BgUtils script provider on first opt-in use.

Only dependency setup happens here; yt-dlp invokes the script to mint tokens.
No public server, account cookies, or user-controlled executable/URL is used.
"""

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import tarfile
import tempfile
import threading

import httpx

VERSION = "2.0.0"
COMMIT = "37169ee2656e08c5c2e5dc9df4c598c0cb4c88a8"
ARCHIVE_URL = f"https://codeload.github.com/Brainicism/bgutil-ytdlp-pot-provider/tar.gz/{COMMIT}"
ARCHIVE_SHA256 = "aa92bac728aebe2d3dbf2081b27523da95ef0fbc6a97540b44b398ba5383ccc6"
CACHE = Path(__file__).resolve().parent / ".cache" / "pot-provider" / VERSION
SETUP_TIMEOUT = 180
_lock = threading.Lock()


class ProviderSetupError(Exception):
    """Only fixed, safe setup messages; never raw subprocess output."""


def run_private(command: list[str], *, timeout: int, input: str | None = None, cwd=None, env=None):
    """Capture child/plugin logs; terminate the process group on timeout (POSIX)."""
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=cwd, env=env,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            # Setup may launch native build tools; extraction may launch Deno.
            # Kill descendants too, not just the parent Python/Deno process.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _unpack(body: bytes, directory: Path) -> Path:
    if hashlib.sha256(body).hexdigest() != ARCHIVE_SHA256:
        raise ProviderSetupError("Token-provider archive failed its integrity check.")
    root = f"bgutil-ytdlp-pot-provider-{COMMIT}"
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 4096 or sum(m.size for m in members) > 20 * 1024 * 1024:
            raise ProviderSetupError("Token-provider archive exceeded the size limit.")
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != root or not (member.isfile() or member.isdir())):
                raise ProviderSetupError("Token-provider archive contained an unsafe entry.")
        archive.extractall(directory, members=members, filter="data")
    return directory / root


def prepare_provider(deno: str) -> Path:
    """Cache a verified provider installation; retry safely after setup failure."""
    with _lock:
        ready = CACHE / "ready"
        server = CACHE / "provider" / "server"
        if ready.is_file() and (server / "src" / "generate_once.ts").is_file():
            return server
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            with httpx.stream("GET", ARCHIVE_URL, timeout=30, trust_env=False) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise ProviderSetupError("Token-provider download exceeded the size limit.")
            with tempfile.TemporaryDirectory(prefix="setup-", dir=CACHE) as temporary:
                root = _unpack(bytes(body), Path(temporary))
                env = {**os.environ, "DENO_NO_PROMPT": "1", "DENO_NO_UPDATE_CHECK": "1"}
                completed = run_private(
                    [deno, "install", "--allow-scripts=npm:canvas", "--frozen"],
                    cwd=root / "server", env=env, timeout=SETUP_TIMEOUT,
                )
                if completed.returncode:
                    raise ProviderSetupError(
                        "Token-provider dependency setup failed. Check the Cloud system dependencies and reboot. "
                        "No raw installer output was exposed."
                    )
                root.rename(CACHE / "provider")
            ready.touch()
            return server
        except ProviderSetupError:
            raise
        except subprocess.TimeoutExpired:
            raise ProviderSetupError("Token-provider setup exceeded three minutes. Try loading again.") from None
        except Exception as exc:
            raise ProviderSetupError(f"Token-provider setup failed ({type(exc).__name__}). Try loading again.") from None