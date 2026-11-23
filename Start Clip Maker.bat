@echo off
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
  echo Run "Install (run once).bat" first.
  pause
  exit /b
)
rem Create or refresh the desktop shortcut (with the Clip Maker icon, no console window)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Clip Maker.lnk')); $s.TargetPath='%~dp0venv\Scripts\pythonw.exe'; $s.Arguments='\"%~dp0server.py\"'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0clipmaker.ico,0'; $s.Description='Clip Maker - robot clipping crew'; $s.Save()" >nul 2>nul
start "" "venv\Scripts\pythonw.exe" server.py
