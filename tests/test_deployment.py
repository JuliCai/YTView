from pathlib import Path
import re
import subprocess

from streamlit import config
from streamlit.watcher.path_watcher import NoOpPathWatcher, get_default_path_watcher_class

ROOT = Path(__file__).resolve().parents[1]


def test_cloud_package_list_has_only_bare_debian_package_names():
    # Cloud passes these lines through xargs, not a comment-aware parser.
    lines = (ROOT / "packages.txt").read_text().splitlines()
    assert lines
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", line) for line in lines)


def test_cloud_package_list_parses_with_xargs():
    text = (ROOT / "packages.txt").read_text()
    result = subprocess.run(["xargs", "-n", "1", "printf", "%s\n"], input=text,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == text.splitlines()


def test_persistent_streaming_handlers_do_not_use_in_process_file_reload():
    assert config.get_option("server.fileWatcherType") == "none"
    assert get_default_path_watcher_class() is NoOpPathWatcher