@echo off
cd /d "%~dp0"
echo Updating the video downloader (fixes most "download failed" errors)...
venv\Scripts\python -m pip install --upgrade "yt-dlp[default]"
echo Done.
pause
