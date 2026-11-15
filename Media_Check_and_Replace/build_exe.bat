@echo off
setlocal

rem ============================================================
rem  Builds MediaCheckAndReplace.exe from media_check_and_replace.py
rem
rem  1) Put this file in the same folder as media_check_and_replace.py
rem  2) Double-click it
rem  3) Your program appears in the "dist" folder
rem
rem  Requires Python 3.8+ (python.org, tick "Add Python to PATH").
rem ============================================================

cd /d "%~dp0"

if not exist "media_check_and_replace.py" (
    echo media_check_and_replace.py was not found next to this file.
    pause
    exit /b 1
)

rem --- Locate the icon (allow override via ICON_PATH env var) ---
set "ICON=%~dp0icon.ico"
if defined ICON_PATH set "ICON=%ICON_PATH%"
if not exist "%ICON%" (
    echo WARNING: icon not found at "%ICON%"
    echo The build will continue without a custom icon.
    set "ICON="
)

where python >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install it from python.org and tick "Add Python to PATH".
    pause
    exit /b 1
)

echo.
echo [1/3] Installing build tools...
python -m pip install -U pyinstaller yt-dlp
if errorlevel 1 (
    echo Could not install the required packages.
    pause
    exit /b 1
)

echo.
echo [2/3] Building the program, this takes a few minutes...

set "ICON_ARG="
if defined ICON set "ICON_ARG=--icon "%ICON%""

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    %ICON_ARG% ^
    --name "MediaCheckAndReplace" ^
    --collect-all yt_dlp ^
    media_check_and_replace.py
if errorlevel 1 (
    echo The build failed. Read the messages above.
    pause
    exit /b 1
)

echo.
echo [3/3] Copying FFmpeg next to the program, if it is installed...
call :copytool ffmpeg
call :copytool ffprobe

echo.
echo ============================================
echo  Done.
echo  Program: %~dp0dist\MediaCheckAndReplace.exe
echo  Keep ffmpeg.exe and ffprobe.exe in the same
echo  folder as the program.
echo ============================================
explorer "%~dp0dist"
pause
exit /b 0


:copytool
for /f "delims=" %%P in ('where %~1 2^>nul') do (
    copy /y "%%P" "dist\" >nul
    echo   Copied %~1.exe
    exit /b 0
)
echo   %~1.exe not found in PATH. Copy ffmpeg.exe and ffprobe.exe into the dist folder yourself.
exit /b 0