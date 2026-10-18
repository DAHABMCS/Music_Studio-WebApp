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
    excludes=['flask', 'numpy', 'scipy', 'torch', 'matplotlib', 'pandas'],  # keep it small
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

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
)
