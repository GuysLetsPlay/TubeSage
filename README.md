# TubeSage

TubeSage is a free Windows desktop app for downloading video and audio with
yt-dlp. Paste a YouTube, Twitch, or other supported link, choose a quality,
and download. Its Error Doctor can explain failures and ask Codex, Cline, or
OpenCode to research a fix.

## Install on Windows

1. Open the [latest TubeSage release](https://github.com/GuysLetsPlay/TubeSage/releases/latest).
2. Download **TubeSage-Setup.exe** and open it.
3. Follow the installer steps. It installs just for your Windows account and
   does not require Python or administrator access.
4. Open TubeSage from the Start menu or the optional desktop shortcut.

On first run, TubeSage downloads yt-dlp automatically. To merge high-quality
video and audio or convert to MP3, choose **Settings → Install ffmpeg**.
Windows may show a SmartScreen warning because the installer is not code-signed;
only download it from this repository's Releases page.

## Features

- YouTube, Twitch, playlists, and other sites supported by yt-dlp
- Best, 1080p, 720p, 480p, MP3, M4A, and Opus presets
- Subtitles, embedded thumbnails and metadata, SponsorBlock, and cookies
- Download queue with progress, speed, ETA, cancel, retry, and history
- Resume failed downloads, watch supported videos while they download, and
  browse queue/history as thumbnail cards
- Error Doctor with yt-dlp GitHub issue search, optional YTDLNIS Telegram
  reader, and deep research through an installed Codex, Cline, or OpenCode CLI

The optional Telegram search requires a separately installed reader. Set the
`TUBESAGE_TELEGRAM_READER` environment variable to its folder; TubeSage never
posts to Telegram. The AI CLI feature likewise requires the selected CLI to be
installed and logged in. Downloads work without either integration.

## Data and privacy

Settings, history, thumbnail cache, the yt-dlp/ffmpeg tools, and saved Error
Doctor fixes are stored under `%LOCALAPPDATA%\TubeSage`. Downloads are saved
under your Videos folder by default. Error details are sent to an AI CLI only
when you choose **Deep Search**.

## Run from source

Install Python 3.10 or later and the dependencies, then run:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The Windows installer is built from this source by the GitHub Actions workflow
`.github/workflows/windows-installer.yml`.
