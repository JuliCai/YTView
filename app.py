import logging
import time

import streamlit as st
import streamlit.components.v1 as components
from tornado.web import HTTPError

st.set_page_config(page_title="YTView", layout="centered")
st.title("YTView")
st.caption("YouTube → this instance → you. No conversion queue. No direct YouTube requests from your browser.")

# Render recovery instructions even during a partially applied deployment or when
# Python still has an older module cached. Reloading individual modules is unsafe:
# Tornado's existing handlers can retain the old registry and connection pool.
try:
    from sources import CLIENT_PROFILES, SourceError, extract_video_id, resolve_video
    from player import render_player
    from streaming import BUILD, REGISTRY
    from streamlit_proxy import RestartRequired, ensure_proxy
    from segment_probe import SAMPLE_BYTES, WORKER_TIMEOUT, run_segment_probe
except ImportError as exc:
    logging.getLogger(__name__).error("App module initialization failed: %s", type(exc).__name__)
    st.error(
        f"App modules could not initialize ({type(exc).__name__}). "
        "The deployment may contain older files or cached modules. After the update finishes, "
        "open Streamlit Cloud → Manage app → Reboot app. "
        "Refreshing this page or clearing Streamlit's data cache does not restart Python. "
        "If this persists after rebooting, share the deployment logs."
    )
    st.caption("Startup guard: v2.1 · playback stopped safely")
    st.stop()

try:
    prefix = ensure_proxy()
except RestartRequired as exc:
    st.error(str(exc))
    st.caption("Startup guard: v2.1 · playback stopped safely")
    st.stop()
except Exception as exc:
    logging.getLogger(__name__).error("Streaming route setup failed: %s", type(exc).__name__)
    st.error(f"The instance streaming routes could not start ({type(exc).__name__}; Streamlit {st.__version__}). Check the pinned dependencies and reboot the app. No direct-browser fallback is enabled.")
    st.stop()

with st.form("open_video"):
    url = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=...")
    quality = st.selectbox("Maximum quality", [360, 720, 1080], index=1,
                           format_func=lambda value: f"{value}p" + (" · balanced" if value == 720 else ""))
    profile = st.selectbox("YouTube client profile", list(CLIENT_PROFILES),
                           format_func=CLIENT_PROFILES.__getitem__,
                           help="If a media segment returns 403, try the other profile and click Load video. Both run on the instance; neither contacts YouTube from your browser.")
    submitted = st.form_submit_button("Load video", type="primary")

current = st.session_state.get("playback")
refresh = st.button("Refresh stream", disabled=current is None,
                    help="Resolve fresh server-side URLs if playback expires or stalls. Restarts playback.")

if submitted or refresh:
    video_id = extract_video_id(url) if submitted else current["video_id"]
    height = quality if submitted else current["height"]
    selected_profile = profile if submitted else current.get("client_profile", "auto")
    if not video_id:
        st.error("Enter a valid YouTube watch, Shorts, live, embed or youtu.be link, or an 11-character video ID.")
    else:
        started = time.monotonic()
        with st.spinner("Reading stream metadata on the instance — not downloading the video…"):
            try:
                video = resolve_video(video_id, height, client_profile=selected_profile)
                if current:
                    REGISTRY.discard(current["token"])
                    st.session_state.pop("playback", None)
                    current = None
                ticket = REGISTRY.create(video)
                st.session_state["playback"] = {
                    "token": ticket.token, "video_id": video_id, "height": height,
                    "title": video.title, "mode": video.mode,
                    "client_profile": selected_profile,
                    "actual_height": max(t.height for t in video.tracks),
                    "resolved_seconds": round(time.monotonic() - started, 1),
                }
                st.rerun()
            except SourceError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Stream setup failed on the instance ({type(exc).__name__}). Try again; no direct-browser fallback was attempted.")


@st.fragment(run_every=3)
def show_diagnostics(token):
    with st.expander("Instance diagnostics — updates every 3 seconds", expanded=True):
        st.caption("Safe to share: stages, HTTP codes and counts only; no source URLs or playback tokens.")
        try:
            st.json(REGISTRY.get(token).snapshot(), expanded=True)
        except HTTPError:
            st.warning("Playback expired or the instance restarted. Click Refresh stream.")


@st.fragment
def show_segment_comparison(token):
    with st.expander("Compare media request — yt-dlp vs relay", expanded=True):
        st.caption(
            f"Run after a playback attempt. Tests the captured media URL, not a fresh extraction. "
            f"At most {SAMPLE_BYTES:,} body bytes per client; {WORKER_TIMEOUT}s hard timeout. "
            "Follows only validated media redirects and shows the HTTP status chain. "
            "Runs only on this instance, with no video file or direct-browser request."
        )
        if st.button("Run bounded segment comparison", key="run_segment_comparison"):
            try:
                with st.spinner("Comparing native yt-dlp and HTTPX on the instance…"):
                    result = run_segment_probe(REGISTRY.get(token))
                st.session_state["segment_comparison"] = {"token": token, "result": result}
            except HTTPError:
                st.warning("Playback expired. Load the video again before comparing.")
        saved = st.session_state.get("segment_comparison")
        if saved and saved["token"] == token:
            result = saved["result"]
            st.info(result.get("interpretation") or result.get("message", "Comparison finished."))
            st.json(result, expanded=True)
            st.caption("Share this JSON; it contains no signed URLs, cookies, or playback tokens. Both samples use a capped range, which can differ from the original playback request.")


current = st.session_state.get("playback")
if current:
    try:
        ticket = REGISTRY.get(current["token"])
    except HTTPError:
        st.warning("This playback session expired or the instance restarted. Click Refresh stream.")
    else:
        st.text(current["title"])
        # srcdoc renders even if cloud routing fails, so errors stay visible.
        # Its relative URLs inherit the app document's /~/+/ (or local) base.
        components.html(render_player(ticket, "_ytview"), height=660, scrolling=True)
        st.caption(f"{current['mode'].upper()} · up to {current['actual_height']}p · Metadata resolved in {current['resolved_seconds']}s")
        show_diagnostics(current["token"])
        show_segment_comparison(current["token"])
        with st.expander("Playback troubleshooting"):
            st.write("Try 360p if the instance cannot sustain 720p. Forward/backward seeks fetch only the needed segments or byte ranges. Refresh stream renews expired source URLs.")
            st.write("For a bug report: include whether the thumbnail appears, time until playback, audio, forward/backward seeking, rebuffer count, and any error shown inside the player.")

st.caption(f"Build: {BUILD} · bounded native segment comparison · startup guard v2.1 · no RapidAPI")
