# -*- mode: python ; coding: utf-8 -*-
# studio_audio_editor.spec - single-file, windowed, standalone build.
# Build with:  pyinstaller --clean studio_audio_editor.spec   (or run build_standalone.bat)
import hashlib
from pathlib import Path

ROOT = Path(SPECPATH)
SCRIPT = ROOT / "studio_audio_editor.py"

# Build stamp: the app shows this in its title bar (see editor_build_id()).
STAMP = ROOT / "editor_build.txt"
STAMP.write_text(hashlib.sha256(SCRIPT.read_bytes()).hexdigest()[:8] + "\n", encoding="utf-8")
print(f"[studio_audio_editor.spec] building {SCRIPT}")

datas = [(str(STAMP), ".")]
binaries = []

# Bundle ffmpeg so MP3/M4A/AC-3 export works on machines without it.
for exe in ("ffmpeg.exe", "ffprobe.exe"):
    p = ROOT / exe
    if p.exists():
        binaries.append((str(p), "."))
    else:
        print(f"[studio_audio_editor.spec] WARNING: {exe} not found next to the spec - not bundled")

if not (ROOT / "Media_Check_and_Replace.py").exists():
    print("[studio_audio_editor.spec] WARNING: Media_Check_and_Replace.py not found - Media Check will not work in the exe")

# Program icon: music_icon_white_bg.ico in the project folder (falls back to icon.ico)
ICON = ROOT / "music_icon_white_bg.ico"
if not ICON.exists():
    ICON = ROOT / "icon.ico"
print(f"[studio_audio_editor.spec] icon: {ICON if ICON.exists() else 'NONE'}")

a = Analysis(
    [str(SCRIPT)],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "sounddevice", "soundfile",
        "scipy.signal", "scipy.ndimage", "scipy.interpolate",
        "Media_Check_and_Replace", "yt_dlp",   # Tools > Media Check, Repair & Search
    ],
    hookspath=[],
    runtime_hooks=[str(ROOT / "rthook_ffmpeg.py")],
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "kivy", "kivymd", "pygame",
        "IPython", "jedi", "parso", "black", "blib2to3",
        "pytest", "astroid", "zmq", "nbformat", "jsonschema",
        "matplotlib", "pandas", "lxml", "cryptography",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Boot splash: shown while the one-file exe unpacks itself (the editor then shows its own
# splash and closes this one). Needs splash_boot.png next to this spec.
BOOT = ROOT / "splash_boot.png"
splash = Splash(str(BOOT), binaries=a.binaries, datas=a.datas, text_pos=None,
                always_on_top=False) if BOOT.exists() else None
if splash is None:
    print("[studio_audio_editor.spec] NOTE: splash_boot.png not found - no boot splash")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,      # onefile: everything is packed into the single exe
    a.datas,
    *([splash, splash.binaries] if splash else []),
    [],
    name="studio_audio_editor",
    debug=False,
    strip=False,
    upx=False,       # UPX-packed exes trigger antivirus and break Inno Setup icon updates
    console=False,   # windowed (no black console window)
    icon=str(ICON) if ICON.exists() else None,
)
