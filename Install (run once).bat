@echo off
setlocal
cd /d "%~dp0"
set "LOG=%~dp0install_log.txt"
echo Clip Maker setup started %date% %time% > "%LOG%"
echo ================================================
echo   Clip Maker - one-time setup
echo   Progress is saved to install_log.txt
echo ================================================
echo.

echo "%~dp0" | find /i "\Temp\" >nul
if not errorlevel 1 (
  echo It looks like you opened this from inside the zip file.
  echo Right-click the zip, choose "Extract All...", then run this file from the extracted folder.
  pause
  exit /b
)

where winget >nul 2>nul
if errorlevel 1 (
  echo This needs "App Installer" from the Microsoft Store ^(it provides winget^).
  echo Install it, then double-click this file again.
  start ms-windows-store://pdp/?productid=9NBLGGH4NNS1
  echo NO_WINGET >> "%LOG%"
  pause
  exit /b
)

rem ---------- find or install Python 3.13
call :findpy
if not defined PY (
  echo [1/5] Installing Python 3.13...
  echo STEP install python >> "%LOG%"
  winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements >> "%LOG%" 2>&1
  call :findpy
)
if not defined PY (
  echo Couldn't install Python automatically.
  echo Install Python 3.13 from https://www.python.org/downloads/ then run this file again.
  echo INSTALL_FAILED nopy >> "%LOG%"
  pause
  exit /b
)
echo [1/5] Python found.

"%PY%" -3.13 -c "print(1)" >nul 2>nul
if errorlevel 1 (
  echo [1/5] Getting Python 3.13 ^(your other Python stays as is^)...
  echo STEP py313 >> "%LOG%"
  "%PY%" install 3.13 >> "%LOG%" 2>&1
)
"%PY%" -3.13 -c "print(1)" >nul 2>nul
if errorlevel 1 (
  winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements >> "%LOG%" 2>&1
)

rem ---------- FFmpeg and Deno
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [2/5] Installing FFmpeg ^(video tool^)...
  echo STEP ffmpeg >> "%LOG%"
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements >> "%LOG%" 2>&1
) else (
  echo [2/5] FFmpeg already installed.
)

where deno >nul 2>nul
if errorlevel 1 (
  echo [3/5] Installing Deno ^(needed for YouTube downloads^)...
  echo STEP deno >> "%LOG%"
  winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements >> "%LOG%" 2>&1
) else (
  echo [3/5] Deno already installed.
)

rem ---------- Clip Maker's own Python environment
echo [4/5] Setting up Clip Maker's Python...
echo STEP venv >> "%LOG%"
if not exist "venv\Scripts\python.exe" "%PY%" -3.13 -m venv venv >> "%LOG%" 2>&1
if not exist "venv\Scripts\python.exe" "%PY%" -V:3.13 -m venv venv >> "%LOG%" 2>&1
if not exist "venv\Scripts\python.exe" (
  echo Couldn't set up Python 3.13. Send install_log.txt to whoever gave you this.
  echo INSTALL_FAILED venv >> "%LOG%"
  pause
  exit /b
)

echo [5/5] Installing Python packages ^(5-10 minutes, please wait^)...
echo STEP pip >> "%LOG%"
venv\Scripts\python -m pip install --upgrade pip >> "%LOG%" 2>&1
venv\Scripts\python -m pip install --prefer-binary -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  echo.
  echo Something went wrong installing packages. Send install_log.txt to whoever gave you this.
  echo INSTALL_FAILED pip >> "%LOG%"
  pause
  exit /b
)
venv\Scripts\python -c "import faster_whisper, cv2, yt_dlp, anthropic, requests; print('IMPORTS_OK')" >> "%LOG%" 2>&1
echo INSTALL_OK >> "%LOG%"

echo.
echo ================================================
echo   All set! Restart your computer once,
echo   then double-click "Start Clip Maker.bat".
echo ================================================
pause
exit /b

:findpy
set "PY="
for /f "delims=" %%p in ('where py 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PY if exist "%WINDIR%\py.exe" set "PY=%WINDIR%\py.exe"
if not defined PY if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe" set "PY=%LOCALAPPDATA%\Microsoft\WindowsApps\py.exe"
exit /b
