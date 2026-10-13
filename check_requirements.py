"""Scans every .py file in the project and lists third-party imports missing from requirements*.txt.
Usage:  python check_requirements.py        (run from the project folder)"""
import ast, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP = {"launcher.py", "check_requirements.py", "verify_dist.py", "collect_prereqs.py"}
# import name -> pip name (only where they differ)
PIP_NAME = {"cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn",
            "faster_whisper": "faster-whisper", "whisper": "openai-whisper", "bs4": "beautifulsoup4",
            "Crypto": "pycryptodome", "dotenv": "python-dotenv", "arabic_reshaper": "arabic-reshaper",
            "bidi": "python-bidi", "pretty_midi": "pretty-midi", "basic_pitch": "basic-pitch",
            "webview": "pywebview", "stable_whisper": "stable-ts"}
PROVIDED = {"torch", "torchaudio"}                       # installed by the launcher itself
local = {p.stem for p in ROOT.glob("*.py")} | {p.name for p in ROOT.iterdir() if p.is_dir()}

found = {}
for f in ROOT.glob("*.py"):
    if f.name in SKIP:
        continue
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as e:
        print(f"!! cannot parse {f.name}: {e}")
        continue
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n not in sys.stdlib_module_names and n not in local and n not in PROVIDED:
                found.setdefault(n, set()).add(f.name)

listed = set()
for rf in ("requirements.txt", "requirements-optional.txt"):
    p = ROOT / rf
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line.split("#")[0])
            if m:
                listed.add(m.group(1).lower().replace("_", "-"))

missing = []
for imp, files in sorted(found.items()):
    pip = PIP_NAME.get(imp, imp).lower().replace("_", "-")
    if pip not in listed and imp.lower() not in listed:
        missing.append((imp, pip, sorted(files)))

if missing:
    print("Imported but NOT in requirements.txt / requirements-optional.txt:")
    for imp, pip, files in missing:
        print(f"   {imp:<18} (pip: {pip:<18}) used in {', '.join(files)}")
    print("\nAdd the ones you need (optional imports inside try/except can go in requirements-optional.txt).")
    sys.exit(1)
print("OK - every third-party import is covered by the requirements files.")