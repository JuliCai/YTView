import re
import time
import urllib.parse

import requests
import streamlit as st

# ──────────────────────────── config ────────────────────────────
RAPIDAPI_HOST = "youtube-info-download-api.p.rapidapi.com"
DOWNLOAD_ENDPOINT = f"https://{RAPIDAPI_HOST}/ajax/download.php"

# ──────────────────────────── page ──────────────────────────────
st.set_page_config(page_title="YTView", layout="centered")
st.title("YTView")
st.caption("Paste a YouTube link and watch it right here.")

# ── API key (loaded from .streamlit/secrets.toml) ──────────────
api_key = st.secrets["RAPIDAPI_KEY"]


# ──────────────────────────── helpers ───────────────────────────
def extract_video_id(url: str) -> str | None:
    """Return the YouTube video ID from a variety of URL formats."""
    patterns = [
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/)([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def request_download(youtube_url: str, api_key: str) -> dict:
    """Kick off the server-side download job and return the JSON response."""
    params = {
        "format": "1080",
        "add_info": "0",
        "url": youtube_url,
        "audio_quality": "128",
        "allow_extended_duration": "false",
        "no_merge": "false",
        "audio_language": "en",
    }
    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": api_key,
    }
    resp = requests.get(DOWNLOAD_ENDPOINT, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def poll_progress(progress_url: str, placeholder) -> str:
    """Poll the progress endpoint until the file is ready. Returns the MP4 URL."""
    bar = placeholder.progress(0, text="Processing video…")
    while True:
        resp = requests.get(progress_url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        progress = int(data.get("progress", 0))
        pct = min(progress / 10, 100)  # progress goes 0 → 1000
        bar.progress(int(pct), text=f"Processing video… {int(pct)}%")

        if progress >= 1000:
            bar.progress(100, text="Done!")
            raw_url: str = data["download_url"]
            # The API sometimes returns doubled slashes in the path – clean them up
            # but keep the double slash after the scheme (https://)
            clean_url = re.sub(r"(?<!:)/{2,}", "/", raw_url)
            return clean_url

        time.sleep(2)


# ──────────────────────────── main ──────────────────────────────
url_input = st.text_input("YouTube URL", placeholder="https://www.youtube.com/watch?v=...")

if url_input:
    video_id = extract_video_id(url_input)
    if not video_id:
        st.error("Could not parse a valid YouTube video ID from that URL.")
        st.stop()

    # Normalise to a full watch URL so the API always gets a consistent format
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"

    # ── Cache key so we don't re-fetch on every Streamlit rerun ──
    cache_key = f"mp4_{video_id}"

    if cache_key not in st.session_state:
        with st.spinner("Requesting download…"):
            try:
                dl_data = request_download(canonical_url, api_key)
            except requests.HTTPError as exc:
                st.error(f"API request failed: {exc}")
                st.stop()

            if not dl_data.get("success"):
                st.error(f"API error: {dl_data}")
                st.stop()

            title = dl_data.get("title", "")
            thumb = dl_data.get("info", {}).get("image", "")
            progress_url = dl_data.get("progress_url", "")

        if title:
            st.subheader(title)
        if thumb:
            st.image(thumb, use_container_width=True)

        if not progress_url:
            st.error("No progress URL returned by the API.")
            st.stop()

        progress_placeholder = st.empty()
        try:
            mp4_url = poll_progress(progress_url, progress_placeholder)
        except Exception as exc:
            st.error(f"Error while waiting for video: {exc}")
            st.stop()

        st.session_state[cache_key] = {
            "mp4_url": mp4_url,
            "title": title,
            "thumb": thumb,
        }
        st.rerun()  # rerun so the cached path renders cleanly
    else:
        data = st.session_state[cache_key]
        if data["title"]:
            st.subheader(data["title"])
        st.video(data["mp4_url"])
        st.markdown(
            f"[Download MP4]({data['mp4_url']})",
            unsafe_allow_html=True,
        )
