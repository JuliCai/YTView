"""Version-pinned adapter for Community Cloud's single exposed port.

Streamlit 1.54 doesn't expose its Tornado Application. Discover the application
by its websocket handler AND Runtime identity, then use Tornado's public
add_handlers API on the server event loop. Never start an inaccessible second port.
"""

import asyncio
import gc
import re
import threading

import streamlit as st
from streamlit.runtime import Runtime
from streamlit.web.server.browser_websocket_handler import BrowserWebSocketHandler
from tornado.web import Application

from player import PlayerHandler
from streaming import BUILD, REGISTRY, HlsScriptHandler, ManifestHandler, Relay, ResourceHandler, StatusHandler

_lock = threading.Lock()
_prefix: str | None = None


class RestartRequired(RuntimeError):
    """Safe user-facing notice for an incompatible in-process code upgrade."""


def mount_routes(application: Application, prefix: str) -> None:
    marker = "ytview.relay"
    generation = (prefix, BUILD, REGISTRY, PlayerHandler, ManifestHandler,
                  StatusHandler, ResourceHandler, HlsScriptHandler)
    if marker in application.settings:
        if application.settings.get("ytview.route_generation") != generation:
            raise RestartRequired(
                "The app code changed, but the server still holds older streaming handlers. "
                "Open Streamlit Cloud → Manage app → Reboot app. A browser refresh is not enough. "
                "After rebooting, load the video again."
            )
        return
    relay = Relay()
    args = {"relay": relay, "prefix": prefix}
    base = re.escape(prefix)
    token = r"([A-Za-z0-9_-]{43})"
    application.add_handlers(r".*", [
        (base + r"/player/" + token, PlayerHandler, args),
        (base + r"/master/" + token, ManifestHandler, args),
        (base + r"/status/" + token, StatusHandler, args),
        (base + r"/resource/" + token + r"/([a-f0-9]{32})", ResourceHandler, args),
        (base + r"/hls.js", HlsScriptHandler, args),
    ])
    application.settings[marker] = relay
    application.settings["ytview.route_generation"] = generation


async def _install(runtime: Runtime, prefix: str):
    candidates = []
    for obj in gc.get_objects():
        if not isinstance(obj, Application):
            continue
        for rule in obj.wildcard_router.rules:
            if (rule.target is BrowserWebSocketHandler
                    and rule.target_kwargs.get("runtime") is runtime):
                candidates.append(obj)
                break
    if len(candidates) != 1:
        raise RuntimeError("Cannot locate Streamlit's streaming server. No external fallback was enabled.")
    mount_routes(candidates[0], prefix)


def ensure_proxy() -> str:
    global _prefix
    with _lock:
        if _prefix is not None:
            return _prefix
        if st.__version__ != "1.54.0":
            raise RuntimeError("Streaming requires the pinned Streamlit 1.54.0. Reinstall requirements and reboot the app.")
        base = st.get_option("server.baseUrlPath").strip("/")
        prefix = ("/" + base if base else "") + "/_ytview"
        runtime = Runtime.instance()
        loop = runtime._get_async_objs().eventloop
        future = asyncio.run_coroutine_threadsafe(_install(runtime, prefix), loop)
        future.result(timeout=10)
        _prefix = prefix
        return prefix