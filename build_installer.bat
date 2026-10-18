@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build Music Studio installer
echo ============================================================
echo   Music Studio - build the deployable installer
echo   Run this in your PROJECT ROOT (next to app.py).
echo ============================================================
echo.

rem ---- 1. Python 3.10+ on the developer PC -------------------------------------------
where python >nul 2>&1
if errorlevel 1 ( echo [ERROR] Python was not found on PATH. Install Python 3.10 or 3.11 first. & goto :fail )
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 ( echo [ERROR] Python 3.10 or newer is required to build. & goto :fail )

rem ---- 2. Required project files -------------------------------------------------------
for %%F in (launcher.py MusicStudio.spec Securit_Management.spec StudioEditor.spec studio_audio_editor.py collect_prereqs.py check_requirements.py verify_dist.py app.py app_paths.py user_store.py Securit_Management.py requirements.txt) do (
  if not exist "%%F" ( echo [ERROR] Missing file in the project root: %%F & goto :fail )
)
for %%D in (templates static) do (
  if not exist "%%D\" ( echo [ERROR] Missing folder in the project root: %%D & goto :fail )
)
if exist "MUSIC-STUDIO-WebbApp.spec" echo [note] The old MUSIC-STUDIO-WebbApp.spec is NOT used any more - MusicStudio.spec replaces it.
if not exist "extras\README.txt" echo [note] extras\ folder not found - README / ACE-Step scripts will not be added to the installer.

rem ---- 2b. Catch syntax errors now, not after a 20-minute build ------------------------
python -m py_compile app.py app_paths.py user_store.py Securit_Management.py studio_audio_editor.py
if errorlevel 1 ( echo [ERROR] A project .py file has a syntax error - fix it and run again & goto :fail )

rem ---- 3. Collect what the installer ships (ffmpeg + VC++ runtime) ---------------------
echo.
echo [1/5] Collecting ffmpeg and the Visual C++ runtime ...
python collect_prereqs.py

rem ---- 4. Are all imported packages listed in requirements.txt? ------------------------
echo.
echo [2/5] Checking requirements ...
python check_requirements.py
if errorlevel 1 (
  if "%ALLOW_MISSING_REQS%"=="1" (
    echo [WARN] Continuing because ALLOW_MISSING_REQS=1
  ) else (
    echo.
    echo [ERROR] Some imports are not listed in requirements.txt - fix the list ^(or set ALLOW_MISSING_REQS=1 to override^).
    goto :fail
  )
)

rem ---- 5. Build environment + PyInstaller ----------------------------------------------
echo.
echo [3/5] Preparing the build environment ...

rem -- Clean any broken leftover venv ---------------------------------------
if exist ".buildenv" (
  if not exist ".buildenv\Scripts\python.exe" (
    echo [note] Removing incomplete .buildenv ...
    rmdir /s /q ".buildenv"
  )
)

rem -- Try to create the venv, show real error if it fails ------------------
if not exist ".buildenv\Scripts\python.exe" (
  echo Creating virtual environment in .buildenv ...
  python -m venv .buildenv
  if errorlevel 1 (
    echo.
    echo [WARN] Could not create .buildenv ^(see error above^).
    echo [WARN] Falling back to the current Python environment.
    echo [WARN] PyInstaller will be installed/used in the currently active Python.
    echo.
    set "BUILD_PY=python"
    goto :have_python
  )
)
set "BUILD_PY=.buildenv\Scripts\python.exe"
:have_python

echo Using build Python: %BUILD_PY%
"%BUILD_PY%" -m pip install --upgrade pip pyinstaller
if errorlevel 1 ( echo [ERROR] Could not install PyInstaller & goto :fail )

echo.
echo [4/5] Running PyInstaller ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
"%BUILD_PY%" -m PyInstaller --noconfirm --clean MusicStudio.spec
if errorlevel 1 ( echo [ERROR] PyInstaller failed & goto :fail )
if not exist "dist\MusicStudio\MusicStudio.exe" ( echo [ERROR] dist\MusicStudio\MusicStudio.exe was not created & goto :fail )

rem -- the payload inside dist must match the project files (catches a stale requirements.txt)
python verify_dist.py
if errorlevel 1 ( echo [ERROR] dist payload does not match the project files & goto :fail )

rem ---- 4b. Build the User Manager (Securit_Management) and ship it inside the app folder
echo.
echo [4b/5] Building User Manager ...
"%BUILD_PY%" -m PyInstaller --noconfirm --clean --distpath "build\um_dist" --workpath "build\um_work" Securit_Management.spec
if errorlevel 1 ( echo [ERROR] User Manager build failed & goto :fail )
if not exist "build\um_dist\UserManager\Securit_Management.exe" ( echo [ERROR] Securit_Management.exe was not created & goto :fail )
xcopy /e /i /y /q "build\um_dist\UserManager" "dist\MusicStudio\UserManager" >nul
if errorlevel 1 ( echo [ERROR] Could not copy User Manager into dist & goto :fail )

rem ---- 4c. Build the Studio audio editor from the CURRENT studio_audio_editor.py
echo.
echo [4c/5] Building Studio Editor ...
if exist "build\ed_dist" rmdir /s /q "build\ed_dist"
if exist "dist\MusicStudio\StudioEditor" rmdir /s /q "dist\MusicStudio\StudioEditor"
"%BUILD_PY%" -m pip install numpy scipy soundfile sounddevice
"%BUILD_PY%" -m PyInstaller --noconfirm --clean --distpath "build\ed_dist" --workpath "build\ed_work" StudioEditor.spec
if errorlevel 1 ( echo [ERROR] Studio Editor build failed & goto :fail )
if not exist "build\ed_dist\StudioEditor\StudioEditor.exe" ( echo [ERROR] StudioEditor.exe was not created & goto :fail )
xcopy /e /i /y /q "build\ed_dist\StudioEditor" "dist\MusicStudio\StudioEditor" >nul
if errorlevel 1 ( echo [ERROR] Could not copy Studio Editor into dist & goto :fail )
for %%I in (studio_audio_editor.py) do echo [note] Editor source used: %%~fI  ^(%%~tI^)

rem files users should SEE next to the exe
if exist "extras\README.txt"           copy /y "extras\README.txt"           "dist\MusicStudio\" >nul
if exist "extras\start_ace_step.bat"   copy /y "extras\start_ace_step.bat"   "dist\MusicStudio\" >nul
if exist "extras\install_ace_step.bat" copy /y "extras\install_ace_step.bat" "dist\MusicStudio\" >nul
if exist "extras\install_optional_tools.bat" copy /y "extras\install_optional_tools.bat" "dist\MusicStudio\" >nul

rem ---- 6. Installer (Inno Setup) or portable zip ---------------------------------------
echo.
echo [5/5] Creating the installer ...
if not exist installer_output mkdir installer_output
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC goto :nozip_inno
"%ISCC%" installer.iss
if errorlevel 1 ( echo [ERROR] Inno Setup failed & goto :fail )
echo.
echo DONE.  Give your users:  installer_output\MusicStudio_Setup.exe
goto :end

:nozip_inno
echo Inno Setup 6 not found - creating a portable ZIP instead.
echo (Install Inno Setup from https://jrsoftware.org/isdl.php and run this file again for a real setup.exe)
powershell -NoProfile -Command "Compress-Archive -Path 'dist\MusicStudio\*' -DestinationPath 'installer_output\MusicStudio_portable.zip' -Force"
if errorlevel 1 ( echo [ERROR] Could not create the zip & goto :fail )
echo.
echo DONE.  Give your users:  installer_output\MusicStudio_portable.zip  (unzip anywhere, run MusicStudio.exe)
goto :end

:fail
echo.
echo *** BUILD FAILED ***
endlocal
if not defined NOPAUSE pause
exit /b 1

:end
endlocal
if not defined NOPAUSE pause
exit /b 0