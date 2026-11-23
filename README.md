# Clip Maker

Turns a long video or stream into ready-to-post vertical (9:16) clips with word-by-word captions, for clipping campaigns (Content Rewards and similar).

Paste a video link and the app downloads it, transcribes it, picks the best moments, cuts 9:16 clips with captions, writes a post caption for each, and logs everything to `output/tracker.csv`. It can optionally post through Upload-Post and use a Claude API key for smarter clip picking.

## Files

| File | What it does |
| --- | --- |
| `app.py` | Desktop launcher and settings window |
| `server.py` | Local web server behind the app's UI |
| `ui/index.html` | The app interface (Floor, Clips and other pages) |
| `pipeline.py` | Download, transcribe, pick moments, cut and caption clips |
| `requirements_reader.py` | Reads a campaign's rules page (optionally with Claude) |
| `poster.py` | Posts clips through the Upload-Post API |
| `Install (run once).bat` | One-time Windows setup |
| `Start Clip Maker.bat` | Starts the app |
| `Update downloader.bat` | Updates yt-dlp |

## Setup (Windows 10/11)

1. Run `Install (run once).bat` and wait for "All set!".
2. Copy `settings.example.json` to `settings.json` (the app also creates it on first save).
3. Run `Start Clip Maker.bat`.

Full user instructions are in `READ ME FIRST.txt`.

## Not in this repo

`venv/`, `output/`, `work/` (downloaded videos and clips), logs, and `settings.json`, which stores your API keys. These stay on your computer.
