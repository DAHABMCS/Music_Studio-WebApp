"""Build-time guard: checks that the payload PyInstaller put into dist\\MusicStudio matches the
project files (requirements*.txt byte-for-byte, every shipped .py present and identical).
Run from the project root after PyInstaller (build_installer.bat does it). Exit 1 on any mismatch."""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "MusicStudio"
NOT_PAYLOAD = {"launcher.py", "check_requirements.py", "runtime_hook.py", "verify_dist.py", "collect_prereqs.py"}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def find_payload() -> Path | None:
    for cand in (DIST / "_internal" / "payload", DIST / "payload"):
        if cand.is_dir():
            return cand
    return None


payload = find_payload()
if payload is None:
    print(f"[verify_dist] ERROR: no payload folder found under {DIST}")
    sys.exit(1)

expected = [ROOT / n for n in ("requirements.txt", "requirements-optional.txt") if (ROOT / n).exists()]
expected += [p for p in sorted(ROOT.glob("*.py")) if p.name not in NOT_PAYLOAD]

problems = []
for src in expected:
    dst = payload / src.name
    if not dst.exists():
        problems.append(f"missing in payload: {src.name}")
    elif sha(src) != sha(dst):
        problems.append(f"DIFFERENT from project copy: {src.name}")

if problems:
    print("[verify_dist] ERROR - the installer payload is out of date:")
    for p in problems:
        print("   - " + p)
    sys.exit(1)
print(f"[verify_dist] OK - {len(expected)} payload files match the project.")