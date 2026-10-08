"""
Create_PyInstaller_Spec.py - Scan a project root and generate a PyInstaller
build config (<NAME>.json) and a ready-to-run <NAME>.spec.

Run in PyCharm: open this file and press Shift+F10. No arguments needed.
By default it scans the folder this script sits in.
Optional argument: path to another project root.

Build afterwards with:   pyinstaller --noconfirm --clean <NAME>.spec

How it works
  1. Picks the entry script (ENTRY_SCRIPT, else the first candidate found).
  2. Follows local imports from that entry script, so tools that are NOT part
     of the app (spec generators, tkinter admin tools, tests) are not bundled.
  3. Collects third-party imports from that graph, including string-based
     dynamic imports such as importlib.import_module("x").
  4. Adds data folders (templates, static, assets, ...) as bundled data.
  5. Keeps hand-edited keys (icon, extra_dist_files, ...) from an existing
     JSON so re-running never wipes your manual settings.
"""

import ast
import importlib.util
import json
import os
import sys
from datetime import datetime

# ----------------------------- SETTINGS ---------------------------------
PROJECT_ROOT = None            # None = folder containing this script
APP_NAME = None                # exe name; None = "<project folder name>"
OUTPUT_NAME = None             # base name of the .json/.spec files; None = folder name
                               # (set to "music_studio_web" to update your existing file)
ENTRY_SCRIPT = None            # e.g. "app.py"; None = auto-detect
ENTRY_CANDIDATES = ["Desktop_app.py", "app.py", "main.py", "run.py", "wsgi.py"]

ONEFILE = True
CONSOLE = False
ICON = None                    # e.g. "D:/guitar.ico"
UPX = False                    # UPX can corrupt torch/CUDA DLLs; keep False

# Folders skipped while scanning (big vendored trees, caches, outputs)
EXCLUDE_DIRS = {
    ".git", ".idea", ".vscode", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", ".mypy_cache", ".pytest_cache",
    "runtime", "ACE-Step", "ACE-Step-1.5", "outputs", "uploads", "jobs",
}
# Folders bundled as data if they exist in the root (src -> dest same name)
DATA_DIRS = ["templates", "static", "assets"]
# Single files bundled as data if they exist (relative to root)
DATA_FILES = []
# Files/folders copied NEXT to the exe after the build (not handled by
# PyInstaller itself; kept in the JSON for your build helper)
EXTRA_DIST_FILES = []
# Modules never bundled
EXCLUDE_MODULES = [
    "IPython", "notebook", "jupyter", "jupyter_client", "jupyter_core",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
]
# Packages that need collect_all (data files / binaries / lazy submodules)
KNOWN_COLLECT_ALL = {
    "torch", "torchaudio", "whisper", "stable_whisper", "tiktoken",
    "tiktoken_ext", "librosa", "soundfile", "numba", "llvmlite", "sklearn",
    "scipy", "reportlab", "demucs", "madmom", "music21", "flask",
    "flask_login", "jinja2", "werkzeug", "transformers", "basic_pitch",
    "webview", "certifi",
}
# Extra hidden imports pulled in when the key package is used
KNOWN_EXTRA_HIDDEN = {
    "tiktoken": ["tiktoken_ext", "tiktoken_ext.openai_public"],
    "numba": ["llvmlite", "llvmlite.binding"],
    "whisper": ["more_itertools", "tqdm"],
    "waitress": ["waitress.server"],
    "flask": ["flask.json", "flask.templating"],
}
# Keys in an existing JSON that are preserved (hand-edited) on re-run
PRESERVE_KEYS = ["name", "icon", "extra_dist_files", "exclude_modules",
                 "console", "upx", "onefile", "datas_extra"]
# ------------------------------------------------------------------------

STDLIB = set(getattr(sys, "stdlib_module_names", []))


def resolve_root():
    if len(sys.argv) > 1:
        return os.path.abspath(sys.argv[1])
    if PROJECT_ROOT:
        return os.path.abspath(PROJECT_ROOT)
    return os.path.dirname(os.path.abspath(__file__))


def fwd(p):
    return p.replace("\\", "/")


def scan_py(root):
    files = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for n in sorted(names):
            if n.endswith(".py"):
                files.append(os.path.relpath(os.path.join(dirpath, n), root)
                             .replace(os.sep, "/"))
    return files


def analyze(path):
    """Return (imports, dynamic_imports, has_main) for one .py file."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            src = f.read()
        tree = ast.parse(src)
    except Exception:
        return set(), set(), False
    imps, dyn = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imps.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imps.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("import_module", "__import__", "find_spec") and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    dyn.add(a.value.split(".")[0])
    return imps, dyn, "__main__" in src


def pick_entry(root, py_files):
    if ENTRY_SCRIPT:
        return ENTRY_SCRIPT
    top = [f for f in py_files if "/" not in f]
    for c in ENTRY_CANDIDATES:
        if c in top:
            return c
    for f in top:
        if analyze(os.path.join(root, f))[2]:
            return f
    return None


def local_module_map(py_files):
    """top-level import name -> relative file/package path."""
    m = {}
    for f in py_files:
        parts = f.split("/")
        if len(parts) == 1:
            m[parts[0][:-3]] = f
        elif parts[-1] == "__init__.py":
            m.setdefault(parts[0], f)
    return m


def build_graph(root, entry, local):
    """Follow local imports from entry. Returns (reached files, third-party)."""
    seen, third = set(), set()
    queue = [entry]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        imps, dyn, _ = analyze(os.path.join(root, rel))
        for name in imps | dyn:
            if name in local:
                queue.append(local[name])
            elif name not in STDLIB and name != "__future__":
                third.add(name)
    return seen, third


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def load_existing(json_path):
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def build_config(root):
    py_files = scan_py(root)
    entry = pick_entry(root, py_files)
    if not entry:
        sys.exit("ERROR: no entry script found. Set ENTRY_SCRIPT at the top.")
    local = local_module_map(py_files)
    reached, third = build_graph(root, entry, local)

    hidden = set(third)
    for pkg in list(third):
        hidden.update(KNOWN_EXTRA_HIDDEN.get(pkg, []))
    collect = sorted(p for p in third if p in KNOWN_COLLECT_ALL)

    datas = []
    for d in DATA_DIRS:
        if os.path.isdir(os.path.join(root, d)):
            datas.append(f"{fwd(os.path.join(root, d))};{d}")
    for f in DATA_FILES:
        if os.path.exists(os.path.join(root, f)):
            datas.append(f"{fwd(os.path.join(root, f))};.")

    name = APP_NAME or os.path.basename(root)
    cfg = {
        "script_path": fwd(os.path.join(root, entry)),
        "name": name,
        "onefile": ONEFILE,
        "console": CONSOLE,
        "icon": ICON,
        "hidden_imports": sorted(hidden),
        "collect_all": collect,
        "datas": datas,
        "extra_dist_files": list(EXTRA_DIST_FILES),
        "exclude_modules": list(EXCLUDE_MODULES),
        "upx": UPX,
        "clean": True,
        "debug": False,
    }

    info = {
        "entry": entry,
        "bundled_local_files": sorted(reached),
        "not_bundled_local_files": sorted(set(py_files) - reached),
        "third_party_not_installed_here": sorted(p for p in third if not installed(p)),
    }
    return cfg, info


def write_spec(cfg, root, out_path):
    pathex = fwd(root)
    L = [
        "# -*- mode: python ; coding: utf-8 -*-",
        f"# Generated by Create_PyInstaller_Spec.py on "
        f"{datetime.now().isoformat(timespec='seconds')}",
        "from PyInstaller.utils.hooks import collect_all",
        "",
        f"SCRIPT = {cfg['script_path']!r}",
        f"NAME = {cfg['name']!r}",
        f"ICON = {cfg['icon']!r}",
        f"DATAS = [tuple(s.split(';', 1)) for s in {cfg['datas']!r}]",
        f"HIDDEN = {cfg['hidden_imports']!r}",
        f"COLLECT_ALL = {cfg['collect_all']!r}",
        f"EXCLUDES = {cfg['exclude_modules']!r}",
        "",
        "datas, binaries, hiddenimports = list(DATAS), [], list(HIDDEN)",
        "for pkg in COLLECT_ALL:",
        "    d, b, h = collect_all(pkg)",
        "    datas += d; binaries += b; hiddenimports += h",
        "",
        "a = Analysis(",
        "    [SCRIPT],",
        f"    pathex=[{pathex!r}],",
        "    binaries=binaries,",
        "    datas=datas,",
        "    hiddenimports=sorted(set(hiddenimports)),",
        "    hookspath=[],",
        "    runtime_hooks=[],",
        "    excludes=EXCLUDES,",
        "    noarchive=False,",
        ")",
        "pyz = PYZ(a.pure)",
        "",
    ]
    if cfg["onefile"]:
        L += [
            "exe = EXE(",
            "    pyz, a.scripts, a.binaries, a.datas, [],",
            "    name=NAME,",
            f"    debug={cfg['debug']},",
            f"    upx={cfg['upx']},",
            f"    console={cfg['console']},",
            "    icon=ICON,",
            ")",
        ]
    else:
        L += [
            "exe = EXE(",
            "    pyz, a.scripts, [],",
            "    exclude_binaries=True,",
            "    name=NAME,",
            f"    debug={cfg['debug']},",
            f"    upx={cfg['upx']},",
            f"    console={cfg['console']},",
            "    icon=ICON,",
            ")",
            "coll = COLLECT(exe, a.binaries, a.datas, strip=False,",
            f"               upx={cfg['upx']}, name=NAME)",
        ]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    root = resolve_root()
    if not os.path.isdir(root):
        sys.exit(f"ERROR: folder not found: {root}")
    print(f"Scanning: {root}")

    out = OUTPUT_NAME or APP_NAME or os.path.basename(root)
    json_path = os.path.join(root, f"{out}.json")
    spec_path = os.path.join(root, f"{out}.spec")

    cfg, info = build_config(root)

    old = load_existing(json_path)
    kept = []
    for k in PRESERVE_KEYS:
        if k in old and old[k] not in (None, [], ""):
            cfg[k] = old[k]
            kept.append(k)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
    write_spec(cfg, root, spec_path)

    print(f"Entry script:        {info['entry']}")
    print(f"Local files bundled: {len(info['bundled_local_files'])}")
    print(f"Local files skipped: {len(info['not_bundled_local_files'])} "
          f"(not imported by the entry script)")
    print(f"Hidden imports:      {len(cfg['hidden_imports'])}")
    print(f"collect_all:         {cfg['collect_all']}")
    print(f"Data entries:        {cfg['datas']}")
    if kept:
        print(f"Kept from old JSON:  {kept}")
    if info["third_party_not_installed_here"]:
        print("WARNING - imported but not installed in this Python environment "
              "(build will miss them):")
        print("  ", info["third_party_not_installed_here"])
    print(f"\nWrote: {json_path}\nWrote: {spec_path}")
    print(f"Build: pyinstaller --noconfirm --clean \"{spec_path}\"")


if __name__ == "__main__":
    main()