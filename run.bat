@echo off
REM Content Machine - one command brings the whole thing up.
setlocal
cd /d "%~dp0"

set "VENV=%~dp0.venv\Scripts"
if not exist "%VENV%\python.exe" (
  echo [!] No venv found at %VENV%
  echo     Run: python -m venv .venv  ^&^&  .venv\Scripts\python -m pip install -r requirements.txt
  pause
  exit /b 1
)

REM ffmpeg from winget is not always on the service PATH; add the known location.
set "FFDIR=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin"
if exist "%FFDIR%\ffmpeg.exe" set "PATH=%FFDIR%;%PATH%"
REM claude CLI (npm global) for the ranking/caption brain
set "PATH=%APPDATA%\npm;%PATH%"

REM Keep every model/temp cache off C: - it has very little free space.
set "HF_HOME=%~dp0data\models\hf"
set "HUGGINGFACE_HUB_CACHE=%~dp0data\models\hf\hub"
set "TORCH_HOME=%~dp0data\models\torch"
set "XDG_CACHE_HOME=%~dp0data\models\cache"
set "TMPDIR=%~dp0data\tmp"

for /f "tokens=2 delims==" %%A in ('findstr /b "CM_PORT" .env 2^>nul') do set "CMPORT=%%A"
if "%CMPORT%"=="" set "CMPORT=3000"

REM The server prints the LAN URL banner itself on startup and writes URL.txt,
REM so the bookmark is always recoverable even if DHCP moves this PC.
echo Starting Content Machine (LAN only) on port %CMPORT% ...
echo Ctrl+C to stop.
echo.

"%VENV%\python.exe" -m uvicorn server.app:app --host 0.0.0.0 --port %CMPORT% --timeout-keep-alive 75
endlocal
