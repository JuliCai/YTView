import logging
import time

import streamlit as st
import streamlit.components.v1 as components
from tornado.web import HTTPError

from sources import SourceError, extract_video_id, resolve_video
from streaming import REGISTRY
from streamlit_proxy import ensure_proxy

st.set_page_config(page_title="YTView", layout="centered")
st.title("YTView")
st.caption("YouTube → this instance → you. No conversion queue. No direct YouTube requests from your browser.")

try:
    prefix = ensure_proxy()
except Exception as exc:
    logging.getLogger(__name__).error("Streaming route setup failed: %s", type(exc).__name__)
    st.error("The instance streaming routes could not start. Check the pinned dependencies and reboot the app. No direct-browser fallback is enabled.")
    st.stop()

with st.form("open_video"):
    url = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=...")
    quality = st.selectbox("Maximum quality", [360, 720, 1080], index=1,
                           format_func=lambda value: f"{value}p" + (" · balanced" if value == 720 else ""))
    submitted = st.form_submit_button("Load video", type="primary")

current = st.session_state.get("playback")
refresh = st.button("Refresh stream", disabled=current is None,
                    help="Resolve fresh server-side URLs if playback expires or stalls. Restarts playback.")

if submitted or refresh:
    video_id = extract_video_id(url) if submitted else current["video_id"]
    height = quality if submitted else current["height"]
    if not video_id:
        st.error("Enter a valid YouTube watch, Shorts, live, embed or youtu.be link, or an 11-character video ID.")
    else:
        started = time.monotonic()
        with st.spinner("Reading stream metadata on the instance — not downloading the video…"):
            try:
                video = resolve_video(video_id, height)
                if current:
                    REGISTRY.discard(current["token"])
                    st.session_state.pop("playback", None)
                    current = None
                ticket = REGISTRY.create(video)
                st.session_state["playback"] = {
                    "token": ticket.token, "video_id": video_id, "height": height,
                    "title": video.title, "mode": video.mode,
                    "actual_height": max(t.height for t in video.tracks),
                    "resolved_seconds": round(time.monotonic() - started, 1),
                }
                st.rerun()
            except SourceError as exc:
                st.error(str(exc))
            except Exception:
                st.error("Stream setup failed on the instance. Try again; no direct-browser fallback was attempted.")

current = st.session_state.get("playback")
if current:
    try:
        REGISTRY.get(current["token"])
    except HTTPError:
        st.warning("This playback session expired or the instance restarted. Click Refresh stream.")
    else:
        st.text(current["title"])
        components.iframe(f"{prefix}/player/{current['token']}", height=475, scrolling=False)
        st.caption(f"{current['mode'].upper()} · up to {current['actual_height']}p · Metadata resolved in {current['resolved_seconds']}s")
        with st.expander("Playback troubleshooting"):
            st.write("Try 360p if the instance cannot sustain 720p. Forward/backward seeks fetch only the needed segments or byte ranges. Refresh stream renews expired source URLs.")
            st.write("For a bug report: include whether the thumbnail appears, time until playback, audio, forward/backward seeking, rebuffer count, and any error shown inside the player.")

st.caption("Build: instance-streaming-v1 · HLS audio/video + range proxy · no RapidAPI")
