#!/usr/bin/env python
"""
patch_spec_generator.py - one-time patch for Create_SPEC_JSON.py

Makes the generated .spec:
  * add a PyInstaller boot splash if splash_boot.png is in the project root
  * add rthook_ffmpeg.py as a runtime hook if it is in the project root
Both are optional: if the files are missing, the spec is generated as before.

Usage (project root):   python patch_spec_generator.py
Safe: checks every anchor first, writes Create_SPEC_JSON.py.bak, and changes
nothing if any anchor is missing or already patched.
"""
import re
import shutil
import sys

TARGET = "Create_SPEC_JSON.py"

with open(TARGET, "r", encoding="utf-8", newline="") as f:
    raw = f.read()

nl = "\r\n" if "\r\n" in raw else "\n"
src = raw.replace("\r\n", "\n")

if "_has(root," in src:
    print("Already patched - nothing to do.")
    sys.exit(0)

problems = []

# ---- 1. helper, placed right after the STDLIB line -------------------------
m_std = re.search(r'^STDLIB = .*$', src, re.M)
if not m_std:
    problems.append("could not find the 'STDLIB = ...' line")

# ---- 2. runtime hook line ---------------------------------------------------
pat_hook = re.compile(r'^(\s*)"    runtime_hooks=\[\],",[ \t]*$', re.M)
if len(pat_hook.findall(src)) != 1:
    problems.append('expected exactly one  "    runtime_hooks=[],",  line')

# ---- 3. Splash object, inserted before PYZ ---------------------------------
pat_pyz = re.compile(r'^(\s*)"pyz = PYZ\(a\.pure\)",[ \t]*$', re.M)
if len(pat_pyz.findall(src)) != 1:
    problems.append('expected exactly one  "pyz = PYZ(a.pure)",  line')

# ---- 4. EXE( blocks ---------------------------------------------------------
pat_exe = re.compile(r'^(\s*)"exe = EXE\(",[ \t]*$', re.M)
exe_matches = list(pat_exe.finditer(src))
if len(exe_matches) != 2:
    problems.append('expected two  "exe = EXE(",  lines (onefile + onedir)')

# ---- 5. COLLECT line --------------------------------------------------------
pat_coll = re.compile(
    r'^(\s*)"coll = COLLECT\(exe, a\.binaries, a\.datas, strip=False,",[ \t]*$', re.M)
if len(pat_coll.findall(src)) != 1:
    problems.append('expected exactly one  "coll = COLLECT(exe, a.binaries, a.datas, strip=False,",  line')

if problems:
    print("NOT PATCHED - this file differs from what I expected:")
    for p in problems:
        print("  -", p)
    print("Send Create_SPEC_JSON.py and I will adapt the patch.")
    sys.exit(1)

# ------------------------------- apply ---------------------------------------
out = src

# helper
out = re.sub(
    r'^(STDLIB = .*)$',
    r'\1\n\n\ndef _has(root, name):\n'
    r'    """True if <root>/<name> exists (optional build assets)."""\n'
    r'    return os.path.isfile(os.path.join(root, name))',
    out, count=1, flags=re.M)

# runtime hook
out = pat_hook.sub(
    lambda m: (m.group(1) + '"    runtime_hooks=" + repr([fwd(os.path.join(root, "rthook_ffmpeg.py"))] '
               'if _has(root, "rthook_ffmpeg.py") else []) + ",",'),
    out, count=1)

# Splash object before PYZ
def add_splash(m):
    ind = m.group(1)
    return (
        ind + '*([\n'
        + ind + '    "splash = Splash(" + repr(fwd(os.path.join(root, "splash_boot.png"))) +\n'
        + ind + '    ", binaries=a.binaries, datas=a.datas, text_pos=None, text_size=12,"\n'
        + ind + '    " minify_script=True, always_on_top=True)",\n'
        + ind + '] if _has(root, "splash_boot.png") else []),\n'
        + m.group(0)
    )
out = pat_pyz.sub(add_splash, out, count=1)

# EXE blocks: onedir is the one with exclude_binaries within the next 4 lines
def patch_exe(text):
    lines = text.split("\n")
    res, i = [], 0
    while i < len(lines):
        line = lines[i]
        res.append(line)
        if re.match(r'^\s*"exe = EXE\(",\s*$', line):
            ind = re.match(r'^(\s*)', line).group(1)
            window = "\n".join(lines[i + 1:i + 5])
            onedir = "exclude_binaries" in window
            arg = '"    splash,"' if onedir else '"    splash, splash.binaries,"'
            res.append(f'{ind}*([{arg}] if _has(root, "splash_boot.png") else []),')
        i += 1
    return "\n".join(res)
out = patch_exe(out)

# COLLECT: onedir needs splash.binaries
out = pat_coll.sub(
    lambda m: (m.group(1) + '"coll = COLLECT(exe, a.binaries, a.datas, " + '
               '("splash.binaries, " if _has(root, "splash_boot.png") else "") + "strip=False,",'),
    out, count=1)

shutil.copyfile(TARGET, TARGET + ".bak")
with open(TARGET, "w", encoding="utf-8", newline="") as f:
    f.write(out.replace("\n", nl))

print("Patched. Backup saved as", TARGET + ".bak")
print("Now put splash_boot.png and rthook_ffmpeg.py in the project root and re-run the build.")
