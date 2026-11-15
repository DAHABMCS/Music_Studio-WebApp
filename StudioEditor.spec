# -*- mode: python ; coding: utf-8 -*-
# Studio audio editor (Tkinter). Built as its own one-folder app and shipped in
# dist\MusicStudio\StudioEditor\ so the installed MusicStudio.exe can start it.
#
# BUILD STAMP: every build records the SHA-256 of studio_audio_editor.py in editor_build.txt (bundled next to
# the exe, and shown in the editor's title bar as "build xxxxxxxx"). check_editor_build.py compares that stamp
# with your current source, so you can prove the exe / installer contains the latest editor.
#
# Also bundled (this is what Tools > Media Check, Repair & Search needs):
#   * Media_Check_and_Replace.py  (imported lazily by the editor)
#   * yt-dlp + certifi + yt_dlp_ejs  (collect_all: yt-dlp loads its site extractors dynamically)
# Optional, only if the file sits next to this spec:
#   * splash_boot.png     -> PyInstaller boot splash (closed by pyi_splash.close() in the editor)
#   * rthook_ffmpeg.py    -> puts the bundled / adjacent ffmpeg.exe on PATH
import hashlib
import time
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

_ROOT = Path(SPECPATH).resolve()
_SRC = _ROOT / 'studio_audio_editor.py'
if not _SRC.is_file():
    raise SystemExit(f'[StudioEditor.spec] {_SRC} not found - run pyinstaller from the project root')
_SHA = hashlib.sha256(_SRC.read_bytes()).hexdigest()
(_ROOT / 'editor_build.txt').write_text(
    f"{_SHA}\nsource: {_SRC}\nsource modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_SRC.stat().st_mtime))}\n"
    f"built: {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding='utf-8')
print(f'[StudioEditor.spec] building {_SRC}  sha256={_SHA[:8]}')

datas = [('music_icon_white_bg.ico', '.'), ('editor_build.txt', '.')]
binaries = []
hiddenimports = ['numpy', 'scipy', 'scipy.signal', 'soundfile', 'sounddevice',
                 'tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox',
                 'Media_Check_and_Replace']

# --- yt-dlp for the YouTube option of Media Check ---------------------------------------
# The build Python must have it installed:   python -m pip install -U yt-dlp
for _pkg in ('yt_dlp', 'certifi', 'yt_dlp_ejs'):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
        print(f'[StudioEditor.spec] bundled {_pkg}')
    except Exception as _exc:
        if _pkg == 'yt_dlp':
            raise SystemExit(
                '[StudioEditor.spec] yt-dlp is NOT installed in the build Python, so the YouTube option of '
                'Media Check would not work in the app.\n'
                '  Fix:  python -m pip install -U yt-dlp   (in the same Python/.buildenv that runs PyInstaller)\n'
                f'  ({_exc})')
        print(f'[StudioEditor.spec] note: {_pkg} not installed - skipped ({_exc})')

if not (_ROOT / 'Media_Check_and_Replace.py').is_file():
    print('[StudioEditor.spec] WARNING: Media_Check_and_Replace.py not found - Media Check will not work')

# --- optional runtime hook + boot splash ------------------------------------------------
_HOOK = _ROOT / 'rthook_ffmpeg.py'
_BOOT = _ROOT / 'splash_boot.png'
if not _HOOK.is_file():
    print('[StudioEditor.spec] note: rthook_ffmpeg.py not found - no ffmpeg PATH hook')
if not _BOOT.is_file():
    print('[StudioEditor.spec] note: splash_boot.png not found - no boot splash')

a = Analysis(
    ['studio_audio_editor.py'],
    pathex=[str(_ROOT)], binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[], hooksconfig={},
    runtime_hooks=[str(_HOOK)] if _HOOK.is_file() else [],
    excludes=['flask', 'torch', 'matplotlib', 'pandas'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)

splash = None
if _BOOT.is_file():
    splash = Splash(str(_BOOT), binaries=a.binaries, datas=a.datas,
                    text_pos=None, always_on_top=False)

exe = EXE(pyz, a.scripts, *([splash] if splash else []), [], exclude_binaries=True, name='StudioEditor',
          debug=False, strip=False, upx=False, console=False,
          icon='music_icon_white_bg.ico')
coll = COLLECT(exe, a.binaries, a.datas, *([splash.binaries] if splash else []),
               strip=False, upx=False, name='StudioEditor')
