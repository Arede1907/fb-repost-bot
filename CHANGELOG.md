# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-04-15

### Added
- **Twitter → Facebook photo sharing.** Paste a public tweet URL and share its
  photos directly to one or more Facebook pages.
- **Video tweet support via frame extraction.** For video tweets, the bot
  downloads the video, extracts N evenly-spaced frames with `ffmpeg`
  (intro/outro skipped), and lets the user pick which frames to post.
  Selected frames are uploaded as a Facebook multi-photo post.
- **X-embed-style tweet preview card.** The preview UI now mimics the official
  X embed: avatar, display name, `@handle`, verified checkmark, tweet body,
  media grid (1–4 photo layouts), timestamp, and a link to open the tweet on X.
- **Activity log controls in the repost bot panel.** New "Fetch full log" and
  "Download (.log)" buttons on the repost bots page.
- **Public `twitter_fetcher.py` module.** Fetches tweets via the public
  Syndication API (`cdn.syndication.twimg.com`) — no auth required. Extracts
  text, photos, video variants (highest-bitrate MP4), author, avatar,
  verified flag, and timestamp.
- **`video_frames.py` module.** Session-scoped frame extraction with path-
  traversal-safe file serving, automatic cleanup after share, and a background
  sweeper that deletes sessions older than one hour.
- **New API endpoints** (`onapp.py`):
  - `POST /api/tw-preview` — fetch tweet metadata
  - `POST /api/tw-share` — share a photo tweet
  - `POST /api/tw-extract-frames` — extract N frames from a video tweet
  - `POST /api/tw-share-frames` — share selected frames as an FB post
  - `GET  /frames/<session_id>/<filename>` — serve extracted frames (login-gated)

### Changed
- **Migrated from Flask dev server to Gunicorn + gevent.** Single worker
  (preserves in-memory bot/progress state), 1000 worker connections, 300s
  request timeout. Fixes long-standing "site unreachable after a while" bug
  caused by SSE saturating the Flask thread pool.
  - New entry point: `wsgi.py` (runs `gevent.monkey.patch_all()` first, then
    initializes token refresh + state restore + periodic save thread).
  - New config: `gunicorn_conf.py`.
- **Reverse-proxy friendly.** Gunicorn now binds `127.0.0.1:5000` (plain HTTP)
  and TLS is terminated at the edge (nginx + Cloudflare Origin Certificate
  in the reference deployment). SSL is no longer handled inside Gunicorn.
- **Twitter video downloads skip the proxy by default.** `video.twimg.com` is
  public and direct downloads are ~12× faster; the proxy is used only as a
  fallback if the direct fetch fails.
- **`facebook_poster.upload_photo_to_page()`** now accepts local file paths
  in addition to URLs. Local paths are uploaded as binary (`files=` upload),
  URLs go through the existing `url=` flow. Enables uploading extracted
  video frames without re-hosting them.
- **Repost activity log UI** now supports fetching the entire log (not just
  the last 500 lines) and downloading it as a `.log` file for offline
  inspection.
- **`tw_to_fb.html` UX polish:**
  - Default frame count lowered from 8 to 3
  - Caption label changed from "Açıklama" to "Caption"
  - Frame thumbnails enlarged (420px) with click-to-select (blue border)
  - Frame numbers (1..N) rendered as a small bottom-center overlay
  - Empty caption falls back to the tweet body automatically

### Fixed
- SSE endpoints no longer exhaust Flask's thread pool; the web UI stays
  reachable after extended sessions.
- Temporary frame directories are now cleaned up on share success, share
  failure, and (as a safety net) by a periodic sweeper.

### Security
- Path-traversal protection on the `/frames/<sid>/<file>` route: both
  `session_id` and `filename` are validated against strict regexes
  (`^[a-f0-9]{6,32}$` and `^frame_\d{1,3}\.jpg$` respectively).
- Gunicorn no longer listens on a public interface in the reference
  deployment — only nginx is exposed, behind Cloudflare.

## [1.0.0] - Initial release

- YouTube Shorts → Facebook Reels automation
- Facebook page repost bot (source → targets, with auto-like)
- Flask web UI with login, bot management, quiet hours, scheduled sharing
- Persistent bot state across restarts
