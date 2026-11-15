@echo off
setlocal
cd /d "%~dp0"
set APP=MusicStudio
set DIST=dist\%APP%

echo === 1/3 PyInstaller ===
pyinstaller --noconfirm --clean app.spec || goto :fail

echo === 2/3 ACE-Step runtime (source + uv.exe, NOT the venv) ===
rem app.py looks for  <exe folder>\runtime\uv.exe  and  runtime\ACE-Step-1.5\
rem Excluded on purpose: .venv (not relocatable - uv rebuilds it on first run),
rem uv's python/cache, and the downloaded models.
rem To ship models offline, remove the hf_cache line from /XD.
robocopy runtime "%DIST%\runtime" /E /NFL /NDL /NJH /NJS ^
  /XD .venv .git __pycache__ "%CD%\runtime\python" "%CD%\runtime\uv_cache" "%CD%\runtime\hf_cache"
if errorlevel 8 goto :fail

echo === 3/3 Files app.py reads from next to the exe ===
copy /Y MUSIC.py "%DIST%\" >nul
if exist assets xcopy /E /I /Y assets "%DIST%\assets" >nul
if exist runtime\bin\ffmpeg.exe (echo ffmpeg found in runtime\bin - copied with runtime) else echo WARNING: put ffmpeg.exe in runtime\bin before building

echo.
echo Done. Run: %DIST%\%APP%.exe
exit /b 0

:fail
echo BUILD FAILED
exit /b 1
