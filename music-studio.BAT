@echo off
setlocal
rem ============================================================
rem  Build MUSIC_STUDIO_WEB with PyInstaller (same settings as the JSON)
rem  Put this file in C:\Users\dahab\Music_Studio-webapp and double-click it
rem ============================================================

set "PROJ=C:\Users\dahab\Music_Studio-webapp"
set "PY=%PROJ%\.venv\Scripts\python.exe"
set "OUT=C:\Users\dahab\Music_Studio-build"
set "ICON=C:\Users\dahab\PyCharm_2026.2.23\New_Bollinger_bands\foldermusic.ico"

cd /d "%PROJ%"

if not exist "%PY%" (
    echo ERROR: Python not found at %PY%
    echo Edit the PY line at the top of this file to point to your venv python.exe
    pause
    exit /b 1
)

echo.
echo === Checking that the packages import in THIS python ===
"%PY%" -c "import flask, werkzeug.http, flask_login, jinja2; print('flask/werkzeug OK')"
if errorlevel 1 (
    echo.
    echo ERROR: flask/werkzeug are missing or broken in this venv.
    echo Run:  "%PY%" -m pip install --upgrade --force-reinstall flask werkzeug flask-login
    pause
    exit /b 1
)

"%PY%" -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo ERROR: could not install/upgrade pyinstaller
    pause
    exit /b 1
)

set "FFMPEG_ARG="
if exist "%PROJ%\bin\ffmpeg.exe" (
    set "FFMPEG_ARG=--add-binary "%PROJ%\bin\ffmpeg.exe;.""
) else (
    echo WARNING: %PROJ%\bin\ffmpeg.exe not found - building WITHOUT ffmpeg
)

set "ICON_ARG="
if exist "%ICON%" set "ICON_ARG=--icon "%ICON%""

echo.
echo === Cleaning old build ===
if exist "%OUT%\dist\MUSIC_STUDIO_WEB" rmdir /s /q "%OUT%\dist\MUSIC_STUDIO_WEB"
if exist "%OUT%\build" rmdir /s /q "%OUT%\build"
if exist "%PROJ%\dist" echo NOTE: old %PROJ%\dist folder still exists - the new build goes to %OUT%\dist

echo.
echo === Building (this can take a long time) ===
"%PY%" -m PyInstaller ^
  --noconfirm --clean --noupx ^
  --onedir --console ^
  --name MUSIC_STUDIO_WEB ^
  %ICON_ARG% ^
  --distpath "%OUT%\dist" ^
  --workpath "%OUT%\build" ^
  --specpath "%OUT%" ^
  --collect-all flask ^
  --collect-all flask_login ^
  --collect-all werkzeug ^
  --collect-all jinja2 ^
  --collect-all torch ^
  --collect-all torchaudio ^
  --collect-all whisper ^
  --collect-all tiktoken ^
  --collect-all tiktoken_ext ^
  --collect-all librosa ^
  --collect-all soundfile ^
  --collect-all numba ^
  --collect-all llvmlite ^
  --collect-all sklearn ^
  --collect-all scipy ^
  --collect-all reportlab ^
  --collect-all demucs ^
  --collect-all madmom ^
  --collect-all music21 ^
  --hidden-import werkzeug.http ^
  --hidden-import werkzeug.serving ^
  --hidden-import werkzeug.routing ^
  --hidden-import werkzeug.middleware ^
  --hidden-import werkzeug.security ^
  --hidden-import tiktoken_ext.openai_public ^
  --hidden-import markupsafe ^
  --hidden-import itsdangerous ^
  --hidden-import click ^
  --hidden-import blinker ^
  --hidden-import dotenv ^
  --hidden-import waitress ^
  --hidden-import tqdm ^
  --hidden-import more_itertools ^
  --hidden-import joblib ^
  --hidden-import threadpoolctl ^
  --hidden-import jsonpickle ^
  --hidden-import webcolors ^
  --hidden-import chardet ^
  --hidden-import PIL ^
  --hidden-import numpy ^
  --add-data "%PROJ%\assets;assets" ^
  --add-data "%PROJ%\static;static" ^
  --add-data "%PROJ%\templates;templates" ^
  %FFMPEG_ARG% ^
  --exclude-module IPython ^
  --exclude-module notebook ^
  --exclude-module jupyter ^
  --exclude-module jupyter_client ^
  --exclude-module jupyter_core ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module wx ^
  --exclude-module seaborn ^
  --exclude-module pandas ^
  --exclude-module pandas_ta ^
  --exclude-module ta ^
  --exclude-module xgboost ^
  --exclude-module backtesting ^
  --exclude-module ccxt ^
  --exclude-module okx ^
  --exclude-module pyttsx3 ^
  --exclude-module pygame ^
  "%PROJ%\app.py"

if errorlevel 1 (
    echo.
    echo BUILD FAILED - read the last lines above and the file:
    echo %OUT%\build\MUSIC_STUDIO_WEB\warn-MUSIC_STUDIO_WEB.txt
    pause
    exit /b 1
)

echo.
echo === Copying uploads folder next to the exe ===
if exist "%PROJ%\uploads" (
    xcopy "%PROJ%\uploads" "%OUT%\dist\MUSIC_STUDIO_WEB\uploads\" /E /I /Y >nul
) else (
    mkdir "%OUT%\dist\MUSIC_STUDIO_WEB\uploads"
)

echo.
echo === DONE ===
echo Exe: %OUT%\dist\MUSIC_STUDIO_WEB\MUSIC_STUDIO_WEB.exe
pause
exit /b 0
