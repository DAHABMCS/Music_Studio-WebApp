# Building the Music Studio installer

Everything below happens in your **project root** (the folder with `app_old.py`, `templates\`, `static\`).

## 1. Put the files in the root
Copy the contents of `deploy\` into the project root, so you have:

    app.py  (the merged one)  subtitle_engine.py  user_store.py  MUSIC.py  midi_refine.py ...  (yours)
    launcher.py   MusicStudio.spec   build_installer.bat   installer.iss
    collect_prereqs.py   check_requirements.py   requirements.txt   requirements-optional.txt
    extras\README.txt   extras\start_ace_step.bat   extras\install_ace_step.bat

Not used any more (leave or delete): `MUSIC-STUDIO-WebbApp.spec`, `runtime_hook.py`,
`verify_install.bat`, `install_prereqs.bat`, old `BUILD.md`.

## 2. Build
Double-click **`build_installer.bat`**. It:
1. downloads `ffmpeg` to `runtime\` and the VC++ runtime to `prereqs\` (kept if already there)
2. runs `check_requirements.py` - warns about imports missing from `requirements.txt`
3. creates `.buildenv`, installs PyInstaller, runs `MusicStudio.spec`
4. adds README + ACE-Step scripts next to the exe
5. builds `installer_output\MusicStudio_Setup.exe` (needs Inno Setup 6) or a portable `.zip`

You do **not** need torch/demucs/etc. on the build PC any more - they are installed on the user's PC.

## 3. Release checklist
- Bump `APP_VERSION` in `launcher.py` **and** `AppVersion` in `installer.iss`.
- Test on a clean Windows PC/VM: (a) with an NVIDIA GPU, (b) without one.
- Check `%LOCALAPPDATA%\MusicStudio\logs\setup.log` if setup fails.
- `lyrics_ai.env` (API keys) and `users.json` are never put in the installer.
