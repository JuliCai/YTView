# YTView

Instance-proxied YouTube playback on Streamlit Community Cloud. The RapidAPI
conversion/polling service has been removed; no API key is required.

## Playback path

1. `yt-dlp` reads stream metadata **on the instance**. Deno and the bundled
	 `yt-dlp-ejs` scripts handle current YouTube player challenges.
2. YTView constructs an HLS master playlist with separate audio and H.264 video
	 variants up to the selected quality cap (720p by default).
3. Every playlist, segment, audio track, key, initialization segment and thumbnail
	 is requested from **the same app origin**. Safari uses native HLS; other modern
	browsers use hls.js, fetched, SHA-256 verified and served by the instance, not a
	browser CDN request.
4. The relay writes 64 KiB chunks with backpressure. Playback does not wait for a
	 completed video download. There is **no transcoding, ffmpeg job, full-video
	 in-memory buffer, or temporary video file**.
5. Seeking requests the relevant HLS segments. When YouTube offers an already-muxed
	 MP4 instead, the relay supports `Range`, `206`, `Content-Range`, and `HEAD`.
	 Open-ended MP4 reads are capped at 2 MiB; explicit HLS ranges remain intact.

Adaptive HLS can lower quality when throughput drops. The quality selector is a
maximum, not a forced resolution. Try 360p on a bandwidth-constrained instance.

## Privacy boundary

- Signed upstream URLs/headers live only in server memory. The browser gets opaque,
	random, six-hour playback tokens and local resource paths, not YouTube URLs.
- Only HTTPS Googlevideo media hosts and the fixed thumbnail host are permitted;
	every upstream redirect is checked and never forwarded to the client.
- Remote error bodies, cookies, authorization headers, and video-title Markdown
	are not forwarded. The player CSP restricts media, image, and network requests
	to the app origin (plus local media blobs).
- There is **no external player/link fallback**, including on extraction errors.
- Playback tokens are bearer capabilities, not user authentication. Keep the app
	private in Community Cloud if access needs restricting. Don't share player URLs.

## Run / deploy

Use Python 3.10 or newer, install requirements.txt into a virtual environment,
then launch `streamlit run app.py`. The same command/entrypoint works locally on
macOS and on Linux in Community Cloud. No extra exposed port is needed.

Community Cloud should track **JuliCai/YTView → main → app.py**. Pushing to that
branch triggers its update; dependency changes can require a rebuild/reboot. The
footer **Build: instance-streaming-v3** identifies this deployment. Existing
`RAPIDAPI_KEY` secrets can be removed; the application no longer reads them.

**After code updates, use Manage app → Reboot app in Community Cloud.** Python
may retain older imported modules, while Tornado's installed handlers retain their
registry and connection pool even when Streamlit reloads scripts. A browser refresh
or clearing Streamlit's data cache is not a process restart. Startup guard v2.1
shows a recovery notice for import mismatches and refuses to reuse stale routes.
An error such as `cannot import name 'render_player'` despite the function being
present in the pushed code calls for a full reboot after the update finishes. If
it persists, check that Cloud deployed the current commit and share its startup logs.

**Keep Streamlit pinned to 1.54.0 and the Tornado backend enabled.** This version
does not expose a public route-registration hook. The small adapter in
streamlit_proxy.py finds the running Tornado application by its Streamlit websocket
handler and Runtime identity, then calls Tornado's public `add_handlers` on the
server event loop. It is tested but relies on private Streamlit internals. Changing
Streamlit versions requires revalidating this adapter. Failure stops playback,
never bypasses the proxy. Community Cloud's edge routing/buffering still needs a
deployment test; a local test cannot prove those platform properties.

Community Cloud embeds the app under `/~/+/`. All player/playlist URLs are
document-relative so this hosting prefix (and any configured base path) survives
every request. The player HTML is sent through Streamlit as `srcdoc`, not fetched
from an origin-root URL that Cloud replaces with its hosting page.

The player displays startup stages and stops silent loading after 30 seconds
(10 seconds for the initial proxy check). **Instance diagnostics** updates every
3 seconds and shows bounded stage/HTTP/error-type history even when the browser
cannot reach the proxy. Neither diagnostic panel exposes signed URLs or tokens.

### Playlist loads, but a media segment returns 403

Build v3 preserves yt-dlp's `available_at` deadline. YouTube can require a
pre-playback wait even while its playlists are already accessible. The player
shows a countdown, and the relay independently enforces the deadline. Waiting is
capped at two minutes, with the usual 30-second startup timeout beginning after
that wait. There is no video download or conversion during the countdown.

Metadata extraction and relaying both use direct IPv4, ignoring environment
proxies, to reduce mismatches for IP-bound media URLs. This does not guarantee a
stable public egress IP on a shared host and cannot fix an IP block.

If segments still return 403, select the other **YouTube client profile** and click
**Load video**. Automatic uses yt-dlp's defaults; Safari explicitly selects
`web_safari` server-side. Refresh stream keeps the profile used to load the video.
See the [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
for current client/token requirements. These vary over time; a server-side PO token
provider or different hosting may be needed if both profiles fail. None of these
options sends YouTube requests from the user's browser.

Diagnostics include the selected profile, initial/remaining source wait, request
range presence, and whether HTTPX re-encoded the URL (a boolean, never its contents).
An accessible playlist does not prove that its signed segments will be accepted.

## Current limitations

- Finished public videos only; live/DVR, restricted/login-only videos and streams
	without compatible HLS audio/video or muxed MP4 fail explicitly.
- YouTube may deny the cloud's IP or change extraction behavior. Server-only routing
	cannot cure an IP block. Update the paired yt-dlp/EJS dependencies when needed;
	the app does not silently return to the old conversion service.
- Source URLs can expire before a playback token does. **Refresh stream** resolves
	new URLs and restarts playback. It also recovers after a server restart.
- Streamlit Cloud CPU/bandwidth still limits throughput; this change removes the
	conversion bottleneck, not the platform limits. At most 20 simultaneous relays,
	32 playback tickets, and 30,000 resource references per ticket are admitted.
- HLS buffers are bounded; media segments are not persistently cached. A seek back
	outside the browser buffer fetches the required segments again.

## Verification

Install requirements-dev.txt and run `python -m pytest -q`. The tests are offline:
mock upstreams verify incremental delivery, byte ranges, seeking, `HEAD`, unsafe
redirect rejection, playlist rewriting, expiration, and same-port route precedence.
They do not require a working local YouTube connection.
Browser regressions emulate Cloud's nested iframe, path prefix, wrong HTML
responses, HTTP failures and hung requests. They use an existing Chromium browser
(optionally selected by `PLAYWRIGHT_CHROMIUM_EXECUTABLE`); they skip if none is installed.

Cloud acceptance checklist:

1. Confirm the build footer, load a normal video at 720p, and check its thumbnail.
2. Note the metadata resolution time and time from pressing Play to moving video.
3. Confirm sound, seek well ahead into unbuffered video, then seek backwards.
4. Watch for a minute and note the displayed rebuffer count; compare 360p if needed.
5. In browser DevTools, confirm media/playlist/image requests use the app origin;
	 there should be no YouTube, Googlevideo or Ytimg requests from the browser.
6. Report the exact player/setup error if it fails. Do not share signed URLs or tokens.
