# TubeSage 🎬🩺

A friendly Windows desktop app for downloading videos/audio with **yt-dlp**,
oriented at the Android app **YTDLnis** — plus its killer feature: the
**Error Doctor**, which explains and fixes yt-dlp errors for you.

## Run

```
run.bat          (or: python main.py)
```

First start: TubeSage downloads `yt-dlp.exe` into `bin\` automatically.
Click **Settings → Install ffmpeg** once (≈90 MB) to enable merging of
high-quality video+audio and MP3 conversion.

## Features

- Paste any URL (YouTube, playlists, thousands of other sites — anything yt-dlp supports)
- Quality presets: Best / 1080p / 720p / 480p / MP3 / M4A / Opus
- Whole playlists, subtitles, embedded thumbnail+metadata, SponsorBlock skip
- Cookies support for age-restricted / bot-checked videos
- Extra yt-dlp arguments for power users
- Queue with live progress, speed, ETA, cancel, retry
- Download history
- Auto-updating engine (Settings → Install / Update yt-dlp)

## Dream features (home-screen checkboxes)

- **👀 Watch while downloading** — start watching a video (e.g. a 12-hour VOD)
  while it is still downloading. TubeSage prefers single-file (progressive)
  formats so one `.part` file grows as you watch, and opens it in VLC/mpv
  (Windows Media Player as fallback) once ~2 % is downloaded. There is also a
  **▶ Watch now** button on the Queue tab for any job, any time — it locates
  the file even if the recorded path went stale (yt-dlp deletes the separate
  audio/video streams after merging), by scanning the output folder for the
  newest matching media file. The download
  is never stopped — it keeps running in the background. Caveat: if the
  platform has no progressive format at the chosen quality, the final file
  only exists after the merge (audio+video) completes.
- **▦ Thumbnail views (Queue & History)** — the "▦ Thumbnails" button (bottom
  right of the Queue and History tabs) switches to YTDLnis-style cards: the
  video's thumbnail image with its full title underneath, so you can see at a
  glance what to watch. Queue cards also show live progress/speed/ETA or the
  status. Thumbnails are fetched once and cached in `thumb_cache\`; without a
  network (or for non-YouTube sites) a placeholder play-card is shown.
  Double-clicking a finished card opens it for watching; an errored card opens
  the Error Doctor. The chosen view is remembered. History entries now also
  store the video URL. A `TubeSage.lnk` shortcut can be created on the desktop
  (points at `run.bat`).
- **🔁 Continue failed downloads (reuse partial files)** — when a long
  download errors out, Retry reuses what is already on disk instead of
  starting over (`--continue`, `--live-from-start` so livestreams are grabbed
  from their beginning, and long file-access retries for antivirus locks).
  Keep the same output folder and quality. TubeSage also auto-recovers
  yt-dlp's Windows edge case where a completed `.part` file gets stuck with
  "WinError 32" / "HTTP Error 416" after a failed download.

## 🩺 Error Doctor (the star feature)

yt-dlp errors are often cryptic. TubeSage explains them:

1. **Every failed download offers "Find a fix"** — automatically searches:
   - the **YTDLNIS Telegram group** via the authorized MTProto reader in
     `C:\Dev\telegram-reader` (most yt-dlp errors are posted, explained and
     fixed there by users and the developer), and
   - **yt-dlp GitHub issues** (public REST API, no key needed).
2. **Deep Search (opens AI CLI)** — opens a visible terminal window running
   your installed CLI agent (`codex` / `cline` / `opencode`, auto-detected)
   with a carefully engineered prompt (`AGENT_PROMPT.md`). The agent searches
   the Telegram group + GitHub thoroughly and writes a step-by-step fix into
   `knowledge\pending\`, which the app imports automatically.
3. **Feedback loop** — after trying a fix, rate it with
   ✅ *This fixed it* / ❌ *Didn't help*. Confirmed solutions are stored in
   `knowledge\solutions.json` and are offered first when a similar error
   happens again.

## Project layout

See `AGENTS.md` (also the manual for AI agents working on this repo).

## Security notes

- The Telegram session lives in `C:\Dev\telegram-reader\tg_session.session`
  and is **read-only used** — TubeSage never posts to Telegram.
- `bin\`, `settings.json` and the knowledge base are local-only (git-ignored).
