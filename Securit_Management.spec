# -*- mode: python ; coding: utf-8 -*-
# User-manager GUI (Tkinter). Built as a one-folder app and shipped inside
# dist\MusicStudio\UserManager\ by build_installer.bat.

a = Analysis(
    ['Securit_Management.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'user_store', 'app_paths',
        'werkzeug.security',
        'cryptography', 'cryptography.fernet',
        'tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'tkinter.simpledialog',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
<<<<<<< HEAD
    excludes=[
        'flask', 'numpy', 'scipy', 'torch', 'matplotlib', 'pandas',   # keep it small
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',                        # fixes "multiple Qt bindings" abort
        'IPython', 'PIL.ImageQt',
    ],
=======
    excludes=['flask', 'numpy', 'scipy', 'torch', 'matplotlib', 'pandas'],  # keep it small
>>>>>>> 8d8f2974f9a335c664739db6340522fd15b773fa
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
<<<<<<< HEAD
=======

>>>>>>> 8d8f2974f9a335c664739db6340522fd15b773fa
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Securit_Management',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='UserManager',
<<<<<<< HEAD
)
=======
)
>>>>>>> 8d8f2974f9a335c664739db6340522fd15b773fa
