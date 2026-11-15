@echo off
setlocal
cd /d "%~dp0"

if not exist music_icon_white_bg.ico echo NOTE: music_icon_white_bg.ico not found here - the exe will have the default icon.
if not exist ffmpeg.exe echo NOTE: ffmpeg.exe not found here - the exe will need ffmpeg on PATH for mp3/m4a/ac3.

rem ---- Find Python: "py" if installed, otherwise "python" ----
set PYCMD=
where py >nul 2>nul && set PYCMD=py -3.11
if not defined PYCMD (
    where python >nul 2>nul && set PYCMD=python
)
if not defined PYCMD (
    echo Python was not found. Install Python 3.11 from python.org and tick "Add to PATH".
    goto :err
)
echo Using: %PYCMD%

rem ---- 1. Build the standalone exe in a clean venv ----
if not exist buildenv\Scripts\activate.bat (
    %PYCMD% -m venv buildenv || goto :err
)
call buildenv\Scripts\activate.bat || goto :err
python -m pip install --upgrade pip
pip install pyinstaller numpy scipy soundfile sounddevice || goto :err

pyinstaller --clean --noconfirm studio_audio_editor.spec || goto :err
if not exist dist\studio_audio_editor.exe goto :err

echo.
echo EXE OK: dist\studio_audio_editor.exe

rem ---- 2. Build the installer with Inno Setup ----
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"

if not defined ISCC (
    echo.
    echo Inno Setup 6 not found - skipping installer. Install it, or open Studio_Audio_Editor.iss manually.
    goto :done
)

echo.
echo Building installer with: %ISCC%
"%ISCC%" Studio_Audio_Editor.iss || goto :err

echo.
echo INSTALLER OK: Output\Studio_Audio_Editor_Setup.exe

:done
echo.
echo ALL DONE
pause
exit /b 0

:err
echo.
echo BUILD FAILED
pause
exit /b 1
