#!/usr/bin/env python3
"""TuneUp - free PC cleaner & optimizer for Windows (stdlib only).
Run:  python TuneUp.py        (right-click > run as admin for system-wide cleaning/tools)
Scheduled:  python TuneUp.py --auto   (silent junk clean; log in %LOCALAPPDATA%\\TuneUp)
"""
import os, sys, re, csv, glob, time, shutil, ctypes, hashlib, threading, subprocess, io

try:
    import winreg
except ImportError:
    winreg = None
try:
    from send2trash import send2trash
except ImportError:
    send2trash = None
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
E = lambda k: os.environ.get(k, "")
LA, WIN, SD = E("LOCALAPPDATA"), E("SystemRoot") or "C:\\Windows", E("SystemDrive") or "C:"
CHROMIUM = ["Cache", "Code Cache", "GPUCache"]
CATS = [
    ("User temp files", [E("TEMP"), E("TMP")]),
    ("Windows temp files", [WIN + "\\Temp"]),
    ("Windows Update download cache", [WIN + "\\SoftwareDistribution\\Download"]),
    ("Crash dumps & error reports", [LA + "\\CrashDumps", LA + "\\Microsoft\\Windows\\WER",
                                     SD + "\\ProgramData\\Microsoft\\Windows\\WER"]),
    ("Thumbnail cache", [LA + "\\Microsoft\\Windows\\Explorer\\thumbcache_*.db"]),
    ("Chrome cache", [LA + f"\\Google\\Chrome\\User Data\\*\\{c}" for c in CHROMIUM]),
    ("Edge cache", [LA + f"\\Microsoft\\Edge\\User Data\\*\\{c}" for c in CHROMIUM]),
    ("Firefox cache", [LA + "\\Mozilla\\Firefox\\Profiles\\*\\cache2"]),
    ("Recycle Bin", [SD + "\\$Recycle.Bin\\*\\$R*"]),
]


def chromium_priv(root):
    base = LA + root + "\\User Data\\*\\"
    return [base + x for x in ("History", "History-journal", "Cookies", "Cookies-journal", "Network\\Cookies",
                               "Network\\Cookies-journal", "Visited Links", "Top Sites", "Favicons")]


# Privacy items are NOT pre-selected (cookies log you out). Passwords/bookmarks are never touched.
PRIVACY = [
    ("Privacy: Chrome history & cookies", chromium_priv("\\Google\\Chrome")),
    ("Privacy: Edge history & cookies", chromium_priv("\\Microsoft\\Edge")),
    ("Privacy: Firefox cookies & form history",
     [E("APPDATA") + "\\Mozilla\\Firefox\\Profiles\\*\\" + x for x in ("cookies.sqlite", "formhistory.sqlite")]),
    ("Privacy: Recent files & jump lists", [E("APPDATA") + "\\Microsoft\\Windows\\Recent\\*"]),
]
ALL = CATS + PRIVACY


def fmt(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def safe_pat(p):                       # never touch a drive root or top-level folder
    return bool(p) and bool(os.path.splitdrive(p)[0]) and len(os.path.normpath(p).split(os.sep)) >= 3


def scan_cat(patterns):
    files, size = [], 0
    for pat in filter(safe_pat, patterns):
        for p in glob.glob(pat):
            walk = [(os.path.dirname(p), [], [os.path.basename(p)])] if os.path.isfile(p) else os.walk(p)
            for r, _, fs in walk:
                for f in fs:
                    fp = os.path.join(r, f)
                    try:
                        size += os.path.getsize(fp)
                        files.append(fp)
                    except OSError:
                        pass
    return files, size


def clean_cat(name, files, patterns):
    freed = failed = 0
    if name == "Recycle Bin":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"], creationflags=NOWIN)
    for f in files:
        try:
            sz = os.path.getsize(f)
            os.remove(f)
            freed += sz
        except OSError:
            failed += 1                # file in use - skipped safely
    for pat in filter(safe_pat, patterns):
        for p in glob.glob(pat):
            if os.path.isdir(p):
                for r, ds, _ in os.walk(p, topdown=False):
                    for d in ds:
                        try:
                            os.rmdir(os.path.join(r, d))
                        except OSError:
                            pass
    return freed, failed


# ---- registry helpers
def rval(key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPR = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"


def startup_items():
    out = []
    if not winreg:
        return out
    srcs = [(winreg.HKEY_CURRENT_USER, "User", RUN, APPR + r"\Run"),
            (winreg.HKEY_LOCAL_MACHINE, "All users", RUN, APPR + r"\Run"),
            (winreg.HKEY_LOCAL_MACHINE, "All users (32-bit)",
             r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", APPR + r"\Run32")]
    for hive, scope, run, appr in srcs:
        try:
            k = winreg.OpenKey(hive, run)
        except OSError:
            continue
        try:
            ak = winreg.OpenKey(hive, appr)
        except OSError:
            ak = None
        for i in range(winreg.QueryInfoKey(k)[1]):
            name, cmd, _ = winreg.EnumValue(k, i)
            st = rval(ak, name) if ak else None
            enabled = not (isinstance(st, bytes) and st and st[0] & 1)
            out.append((hive, appr, name, str(cmd), scope, enabled))
    return out


def installed_programs():
    out, seen = [], set()
    if not winreg:
        return out
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, base),
                      (winreg.HKEY_LOCAL_MACHINE, base.replace("SOFTWARE", r"SOFTWARE\WOW6432Node")),
                      (winreg.HKEY_CURRENT_USER, base)):
        try:
            k = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                sk = winreg.OpenKey(k, winreg.EnumKey(k, i))
            except OSError:
                continue
            name, cmd = rval(sk, "DisplayName"), rval(sk, "UninstallString")
            if not name or not cmd or rval(sk, "SystemComponent") == 1 or name in seen:
                continue
            seen.add(name)
            out.append((name, rval(sk, "Publisher") or "", rval(sk, "DisplayVersion") or "",
                        (rval(sk, "EstimatedSize") or 0) * 1024, cmd))
    return sorted(out, key=lambda x: x[0].lower())


def shred_file(p, passes=3):
    size = os.path.getsize(p)
    with open(p, "r+b") as fh:
        for _ in range(passes):
            fh.seek(0)
            left = size
            while left > 0:
                n = min(left, 1 << 20)
                fh.write(os.urandom(n))
                left -= n
            fh.flush()
            os.fsync(fh.fileno())
    os.remove(p)


def self_cmd():
    if getattr(sys, "frozen", False):
        return f'\\"{sys.executable}\\" --auto'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'\\"{pyw}\\" \\"{os.path.abspath(sys.argv[0])}\\" --auto'


def auto_clean():                     # used by the scheduled task: python TuneUp.py --auto
    freed = failed = 0
    for n, pats in CATS:
        files, _ = scan_cat(pats)
        a, b = clean_cat(n, files, pats)
        freed += a
        failed += b
    d = os.path.join(LA or os.path.expanduser("~"), "TuneUp")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "autoclean.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M')}  freed {fmt(freed)}, skipped {failed} in-use files\n")


class MEM(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong)] + \
               [(n, ctypes.c_ulonglong) for n in ("tp", "ap", "tpf", "apf", "tv", "av", "ex")]


def mem_info():
    """(used bytes, total bytes, percent in use)"""
    m = MEM()
    m.len = ctypes.sizeof(MEM)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.tp - m.ap, m.tp, m.load


def uptime_secs():
    k = ctypes.windll.kernel32
    k.GetTickCount64.restype = ctypes.c_ulonglong
    return k.GetTickCount64() // 1000


def drives_info():
    out = []
    for c in "CDEFGHIJ":
        if os.path.exists(c + ":\\"):
            try:
                u = shutil.disk_usage(c + ":\\")
                out.append((c, u.total, u.free))
            except OSError:
                pass
    return out


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def os_name():
    try:
        v = sys.getwindowsversion()
        return f"Windows {'11' if v.build >= 22000 else v.major}  (build {v.build})"
    except Exception:
        return sys.platform


def decode_output(raw):
    """Console output -> text: UTF-16 (some tools), UTF-8, else the Windows OEM code page."""
    if not raw:
        return ""
    if b"\x00" in raw:
        return raw.decode("utf-16-le", "ignore").replace("\x00", "")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("oem", "replace")
        except LookupError:
            return raw.decode("latin-1", "replace")


def dur(sec):
    return f"{sec // 86400}d {sec % 86400 // 3600}h {sec % 3600 // 60}m"


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def file_hash(p, limit=None):
    try:
        h = hashlib.md5()
        with open(p, "rb") as fh:
            if limit:
                h.update(fh.read(limit))
            else:
                for b in iter(lambda: fh.read(1 << 20), b""):
                    h.update(b)
        return h.hexdigest()
    except OSError:
        return None


REG_BK = os.path.join(LA or os.path.expanduser("~"), "TuneUp", "RegBackup")


def extract_exe(cmd):
    c = os.path.expandvars(str(cmd).strip())
    m = re.match(r'^"([^"]+)"', c) or re.match(r"^(.+?\.(?:exe|bat|cmd|com))(?=\s|$|,)", c, re.I)
    return os.path.normpath(m.group(1)) if m else None


def reg_scan():
    """Only clearly orphaned entries: they point to files that no longer exist."""
    if not winreg:
        return []
    H = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    out = []

    def gone(cmd):
        exe = extract_exe(cmd) if cmd else None
        d = os.path.splitdrive(exe)[0] if exe else ""
        return exe if exe and d and os.path.isdir(d + "\\") and not os.path.exists(exe) else None

    for hn, sub in (("HKCU", RUN), ("HKLM", RUN),
                    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")):
        try:
            k = winreg.OpenKey(H[hn], sub)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[1]):
            name, cmd, _ = winreg.EnumValue(k, i)
            m = gone(cmd)
            if m:
                out.append(("Dead startup entry", hn, sub, name, f"{name}  ->  missing: {m}"))
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hn, sub in (("HKLM", base), ("HKLM", base.replace("SOFTWARE", r"SOFTWARE\WOW6432Node")),
                    ("HKCU", base.replace("SOFTWARE", "Software"))):
        try:
            k = winreg.OpenKey(H[hn], sub)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                kn = winreg.EnumKey(k, i)
                sk = winreg.OpenKey(k, kn)
            except OSError:
                continue
            cmd, loc = rval(sk, "UninstallString"), rval(sk, "InstallLocation")
            if cmd and gone(cmd) and not (loc and os.path.exists(os.path.expandvars(str(loc)))):
                out.append(("Orphan uninstall entry", hn, sub + "\\" + kn, None, rval(sk, "DisplayName") or kn))
    ap = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    try:
        k = winreg.OpenKey(H["HKLM"], ap)
        for i in range(winreg.QueryInfoKey(k)[0]):
            kn = winreg.EnumKey(k, i)
            m = gone(rval(winreg.OpenKey(k, kn), ""))
            if m:
                out.append(("Dead App Paths entry", "HKLM", ap + "\\" + kn, None, f"{kn}  ->  missing: {m}"))
    except OSError:
        pass
    return out


def reg_fix(items):
    """Export every affected key to a .reg backup first; skip the item if the backup fails."""
    bk = os.path.join(REG_BK, time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(bk, exist_ok=True)
    saved, fixed, skipped = {}, 0, 0
    for kind, hn, key, val, _ in items:
        full = f"{hn}\\{key}"
        if full not in saved:
            r = subprocess.run(["reg", "export", full, os.path.join(bk, f"{len(saved) + 1:03d}.reg"), "/y"],
                               capture_output=True, creationflags=NOWIN)
            saved[full] = r.returncode == 0
        if not saved[full]:
            skipped += 1
            continue
        cmd = ["reg", "delete", full] + (["/v", val] if val else []) + ["/f"]
        r = subprocess.run(cmd, capture_output=True, creationflags=NOWIN)
        fixed, skipped = (fixed + 1, skipped) if r.returncode == 0 else (fixed, skipped + 1)
    return fixed, skipped, bk


class FT(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]


def cpu_times():
    i, k, u = FT(), FT(), FT()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
    f = lambda t: (t.hi << 32) | t.lo
    return f(i), f(k), f(u)              # idle, kernel (includes idle), user


def ram_load():
    return mem_info()[2]


def trim_ram():
    """Ask Windows to trim every process's working set (frees standby RAM; it refills as needed)."""
    psapi, k32 = ctypes.windll.psapi, ctypes.windll.kernel32
    arr, need = (ctypes.c_ulong * 4096)(), ctypes.c_ulong()
    psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(need))
    ok = 0
    for pid in arr[:need.value // ctypes.sizeof(ctypes.c_ulong)]:
        h = k32.OpenProcess(0x0100 | 0x0400, False, pid)   # SET_QUOTA | QUERY_INFORMATION
        if h:
            ok += 1 if psapi.EmptyWorkingSet(h) else 0
            k32.CloseHandle(h)
    return ok


# ============================================================
# THEME
# ============================================================
BG, SIDE, CARD, CARD2, LINE = "#0b1220", "#0e1626", "#152033", "#1c2a42", "#26344d"
FG, MUTED = "#e5e7eb", "#94a3b8"
GOOD, WARN, BAD = "#34d399", "#fbbf24", "#f87171"
ACC = {"dash": "#38bdf8", "junk": "#34d399", "start": "#fbbf24", "apps": "#fb7185", "proc": "#a78bfa",
       "files": "#2dd4bf", "reg": "#fb923c", "shred": "#f87171", "tools": "#818cf8"}
BTN = {"Blue": "#2563eb", "Green": "#059669", "Amber": "#d97706", "Red": "#dc2626", "Violet": "#7c3aed",
       "Teal": "#0d9488", "Rose": "#e11d48", "Orange": "#ea580c", "Indigo": "#4f46e5", "Slate": "#334155"}
NAV = [("dash", "\u25c8", "Dashboard"), ("junk", "\u267b", "Junk & privacy"), ("start", "\u26a1", "Startup"),
       ("apps", "\u25a3", "Uninstall"), ("proc", "\u2630", "Processes"), ("files", "\u2756", "Large & duplicates"),
       ("reg", "\u2637", "Registry"), ("shred", "\u2702", "File shredder"), ("tools", "\u2699", "Tools")]
FONT, FONTB = ("Segoe UI", 10), ("Segoe UI Semibold", 10)


def shade(c, f):
    """f > 1 lightens, f < 1 darkens."""
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    if f > 1:
        r, g, b = (int(v + (255 - v) * (f - 1)) for v in (r, g, b))
    else:
        r, g, b = (int(v * f) for v in (r, g, b))
    return "#%02x%02x%02x" % (r, g, b)


def level_color(p):
    return GOOD if p < 60 else WARN if p < 85 else BAD


def setup_style(root):
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, fieldbackground=CARD2, bordercolor=LINE,
                 lightcolor=BG, darkcolor=BG, troughcolor=BG, font=FONT, focuscolor=BG)
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("TEntry", fieldbackground=CARD2, foreground=FG, insertcolor=FG, bordercolor=LINE,
                 lightcolor=CARD2, darkcolor=CARD2, padding=7)
    st.map("TEntry", bordercolor=[("focus", "#38bdf8")])
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=30,
                 borderwidth=0, font=FONT)
    st.map("Treeview", background=[("selected", "#2563eb")], foreground=[("selected", "#ffffff")])
    st.configure("Treeview.Heading", background=CARD2, foreground="#cbd5e1", relief="flat",
                 font=("Segoe UI Semibold", 10), padding=(10, 8), bordercolor=CARD2,
                 lightcolor=CARD2, darkcolor=CARD2)
    st.map("Treeview.Heading", background=[("active", "#26395a")])
    for o in ("Vertical", "Horizontal"):
        st.configure(f"{o}.TScrollbar", background="#2a3a57", troughcolor=BG, bordercolor=BG,
                     arrowcolor=MUTED, lightcolor="#2a3a57", darkcolor="#2a3a57", gripcount=0)
        st.map(f"{o}.TScrollbar", background=[("active", "#3b4f75")])
    for name, c in BTN.items():
        st.configure(f"{name}.TButton", background=c, foreground="#ffffff", bordercolor=c, lightcolor=c,
                     darkcolor=c, borderwidth=1, focusthickness=0, padding=(16, 8), font=("Segoe UI Semibold", 10))
        h, p = shade(c, 1.18), shade(c, 0.8)
        st.map(f"{name}.TButton", background=[("pressed", p), ("active", h)], bordercolor=[("pressed", p), ("active", h)],
               lightcolor=[("pressed", p), ("active", h)], darkcolor=[("pressed", p), ("active", h)])
        st.configure(f"Soft{name}.TButton", background=CARD2, foreground=FG, bordercolor=CARD2, lightcolor=CARD2,
                     darkcolor=CARD2, borderwidth=1, focusthickness=0, padding=(12, 5), anchor="w", font=FONT)
        st.map(f"Soft{name}.TButton", background=[("pressed", shade(c, 0.8)), ("active", c)],
               foreground=[("active", "#ffffff")], bordercolor=[("active", c)],
               lightcolor=[("active", c)], darkcolor=[("active", c)])
    st.configure("Bar.Horizontal.TProgressbar", background="#38bdf8", troughcolor=LINE, bordercolor=LINE,
                 lightcolor="#38bdf8", darkcolor="#38bdf8")


# ============================================================
# CUSTOM WIDGETS
# ============================================================
_U = {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}


def sort_key(txt):
    m = re.match(r"^\s*([\d,.]+)\s*(B|KB|MB|GB|TB)?\s*$", txt)
    if m:
        try:
            return (0, float(m.group(1).replace(",", "")) * _U.get(m.group(2) or "B", 1), "")
        except ValueError:
            pass
    return (1, 0, txt.lower())


class Tree(ttk.Treeview):
    """Treeview with zebra rows and colour tags."""

    def __init__(s, parent, **kw):
        super().__init__(parent, **kw)
        s._n = 0
        s.tag_configure("odd", background="#19263c")
        s.tag_configure("even", background=CARD)
        s.tag_configure("group", foreground=WARN, font=("Segoe UI Semibold", 10))
        s.tag_configure("priv", foreground=WARN)
        s.tag_configure("off", foreground="#64748b")

    def insert(s, parent, index, iid=None, **kw):
        v = [str(x) for x in kw.get("values", ())]
        tags = ["odd" if s._n % 2 else "even"]
        if v and v[0].startswith("=="):
            tags = ["group"]
        else:
            if v and v[0].startswith("Privacy:"):
                tags.append("priv")
            if "Disabled" in v:
                tags.append("off")
        kw["tags"] = tuple(tags)
        if parent == "":
            s._n += 1
        return super().insert(parent, index, iid=iid, **kw)

    def delete(s, *items):
        super().delete(*items)
        s._n = len(s.get_children(""))

    def restripe(s):
        for n, i in enumerate(s.get_children("")):
            t = s.item(i, "tags")
            t = [x for x in (t.split() if isinstance(t, str) else t) if x not in ("odd", "even", "")]
            if "group" not in t:
                t.append("odd" if n % 2 else "even")
            s.item(i, tags=tuple(t))


class Gauge(tk.Canvas):
    def __init__(s, parent, size=132):
        super().__init__(parent, width=size, height=size, bg=CARD, highlightthickness=0)
        p = 12
        box = (p, p, size - p, size - p)
        s.create_arc(*box, start=225, extent=-270, style="arc", width=11, outline=LINE)
        s.arc = s.create_arc(*box, start=225, extent=-0.01, style="arc", width=11, outline=GOOD)
        s.txt = s.create_text(size / 2, size / 2 - 5, text="--", fill=FG, font=("Segoe UI Semibold", 20))
        s.sub = s.create_text(size / 2, size / 2 + 20, text="", fill=MUTED, font=("Segoe UI", 8))

    def set(s, v, sub=None):
        v = max(0.0, min(100.0, float(v)))
        col = level_color(v)
        s.itemconfigure(s.arc, extent=-max(0.01, 270 * v / 100), outline=col)
        s.itemconfigure(s.txt, text=f"{v:.0f}%", fill=col)
        if sub is not None:
            s.itemconfigure(s.sub, text=sub)


class Chart(tk.Canvas):
    N = 90

    def __init__(s, parent):
        super().__init__(parent, bg=CARD, highlightthickness=0, height=190)
        s.cpu, s.ram = [], []
        s.bind("<Configure>", lambda e: s.draw())

    def push(s, c, r):
        s.cpu = (s.cpu + [c])[-s.N:]
        s.ram = (s.ram + [r])[-s.N:]
        s.draw()

    def draw(s):
        s.delete("all")
        W, H = s.winfo_width(), s.winfo_height()
        if W < 80 or H < 60:
            return
        pl, pr, pt, pb = 34, 12, 12, 10
        w, h = W - pl - pr, H - pt - pb
        for p in (0, 25, 50, 75, 100):
            y = pt + h * (1 - p / 100)
            s.create_line(pl, y, W - pr, y, fill=LINE)
            s.create_text(pl - 8, y, text=str(p), fill=MUTED, anchor="e", font=("Segoe UI", 7))
        step = w / (s.N - 1)
        for data, col in ((s.ram, "#fb923c"), (s.cpu, "#38bdf8")):
            if len(data) < 2:
                continue
            n, pts = len(data), []
            for i, v in enumerate(data):
                pts += [pl + w - (n - 1 - i) * step, pt + h * (1 - max(0, min(100, v)) / 100)]
            s.create_polygon(pts + [pl + w, pt + h, pts[0], pt + h], fill=col, stipple="gray12", outline="")
            s.create_line(*pts, fill=col, width=2, smooth=True)
        x = W - pr - 110
        for label, col in (("CPU", "#38bdf8"), ("RAM", "#fb923c")):
            s.create_rectangle(x, pt + 4, x + 10, pt + 14, fill=col, outline="")
            s.create_text(x + 15, pt + 9, text=label, anchor="w", fill=FG, font=("Segoe UI", 8, "bold"))
            x += 55


class DriveBars(tk.Canvas):
    def __init__(s, parent, width=320):
        super().__init__(parent, bg=CARD, highlightthickness=0, width=width, height=120)
        s.rows = []
        s.bind("<Configure>", lambda e: s.draw())

    def set(s, rows):
        s.rows = rows
        s.config(height=max(60, len(rows) * 46 + 6))
        s.draw()

    def draw(s):
        s.delete("all")
        W = s.winfo_width()
        y = 4
        for letter, total, free in s.rows:
            pct = (total - free) / total * 100 if total else 0
            col = GOOD if pct < 70 else WARN if pct < 90 else BAD
            s.create_text(0, y, anchor="nw", text=f"{letter}:", fill=FG, font=("Segoe UI Semibold", 10))
            s.create_text(W, y + 1, anchor="ne", text=f"{fmt(free)} free of {fmt(total)}", fill=MUTED,
                          font=("Segoe UI", 9))
            s.create_rectangle(0, y + 24, W, y + 32, fill=LINE, outline="")
            s.create_rectangle(0, y + 24, max(2, W * pct / 100), y + 32, fill=col, outline="")
            y += 46


def tool_groups():
    sc = self_cmd()
    ps = "powershell -NoProfile -Command "
    return [
        ("System repair", "Blue", [
            ("SFC: check system files *", "sfc /scannow"),
            ("DISM: repair Windows image *", "DISM /Online /Cleanup-Image /RestoreHealth"),
            ("Create restore point *", ps + '"Checkpoint-Computer -Description TuneUp -RestorePointType MODIFY_SETTINGS"'),
            ("Check disk C: (read-only)", "chkdsk C:"),
            ("Optimize / TRIM drive C: *", "defrag C: /O"),
            ("Windows Disk Cleanup", "cleanmgr")]),
        ("Security", "Rose", [
            ("Defender quick scan", ps + '"Start-MpScan -ScanType QuickScan"'),
            ("Defender FULL scan", ps + '"Start-MpScan -ScanType FullScan"'),
            ("Update Defender signatures", ps + '"Update-MpSignature"'),
            ("Security status", ps + '"Get-MpComputerStatus | '
             'Select AntivirusEnabled,RealTimeProtectionEnabled,AntivirusSignatureLastUpdated | Format-List; '
             'Get-NetFirewallProfile | Select Name,Enabled | Format-Table"'),
            ("Ransomware shield ON *", ps + '"Set-MpPreference -EnableControlledFolderAccess Enabled"'),
            ("Ransomware shield OFF *", ps + '"Set-MpPreference -EnableControlledFolderAccess Disabled"')]),
        ("Updates & drivers", "Violet", [
            ("List outdated apps", "winget upgrade --accept-source-agreements"),
            ("Update ALL apps", "winget upgrade --all --silent "
             "--accept-source-agreements --accept-package-agreements"),
            ("Drivers: list (oldest first)", ps + '"Get-CimInstance '
             'Win32_PnPSignedDriver | Where-Object DeviceName | Sort-Object DriverDate | Select-Object -First 40 '
             'DeviceName,DriverVersion,DriverDate,Manufacturer | Format-Table -AutoSize | Out-String -Width 200"'),
            ("Drivers: open Windows Update", "start ms-settings:windowsupdate"),
            ("Disk health (SMART status)", ps + '"Get-PhysicalDisk | Select-Object '
             'FriendlyName,MediaType,HealthStatus,OperationalStatus,@{n=\'GB\';e={[int]($_.Size/1GB)}} | '
             'Format-Table -AutoSize"')]),
        ("Performance & power", "Amber", [
            ("Power plan: High performance", "powercfg /setactive SCHEME_MIN"),
            ("Power plan: Balanced", "powercfg /setactive SCHEME_BALANCED"),
            ("Battery report (laptops)", 'powercfg /batteryreport /output "%USERPROFILE%\\battery.html"')]),
        ("Network", "Teal", [
            ("Flush DNS cache", "ipconfig /flushdns"),
            ("Reset network stack *", "netsh winsock reset && netsh int ip reset"),
            ("Internet check (ping 3 DNS)", ps + '"foreach($h in '
             "'1.1.1.1','8.8.8.8','9.9.9.9'){$r=Test-Connection $h -Count 3 -ErrorAction SilentlyContinue;"
             "'{0}: {1} ms avg' -f $h,($r|Measure-Object ResponseTime -Average).Average}\""),
            ("Show open network connections", "netstat -ano")]),
        ("Scheduled auto-clean", "Green", [
            ("Schedule DAILY at 12:00", f'schtasks /Create /SC DAILY /ST 12:00 /TN TuneUpAutoClean /TR "{sc}" /F'),
            ("Schedule WEEKLY (Sunday)",
             f'schtasks /Create /SC WEEKLY /D SUN /ST 12:00 /TN TuneUpAutoClean /TR "{sc}" /F'),
            ("Remove scheduled auto-clean", "schtasks /Delete /TN TuneUpAutoClean /F")]),
    ]


# ============================================================
# APPLICATION
# ============================================================
class App:
    def __init__(s, root):
        s.root, s.cats, s.su, s.apps = root, {}, {}, {}
        s.reg_issues, s.ld_groups, s.pages, s.nav, s.cur = [], [], {}, {}, None
        s._busy, s._sort, s.cpu0 = 0, {}, None
        root.title("TuneUp - free PC cleaner & optimizer")
        root.geometry("1180x780")
        root.minsize(1020, 660)
        root.configure(bg=BG)
        setup_style(root)
        s._sidebar()
        right = tk.Frame(root, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        s._statusbar(right)
        s.body = tk.Frame(right, bg=BG)
        s.body.pack(fill="both", expand=True)
        s._build_dashboard()
        s._build_junk()
        s._build_startup()
        s._build_apps()
        s._build_procs()
        s._build_files()
        s._build_registry()
        s._build_shredder()
        s._build_tools()
        if os.name != "nt":
            messagebox.showwarning("TuneUp", "TuneUp is built for Windows; most features will not work here.")
        s.refresh_info()
        s.load_startup()
        s.load_apps()
        s.load_procs()
        s.tick()
        s.show("dash")
        s.st("Ready. Open 'Junk & privacy' and press Scan.")

    # ---------- chrome ----------
    def _sidebar(s):
        s.side = tk.Frame(s.root, bg=SIDE, width=232)
        s.side.pack(side="left", fill="y")
        s.side.pack_propagate(False)
        brand = tk.Frame(s.side, bg=SIDE)
        brand.pack(fill="x", padx=18, pady=(22, 18))
        logo = tk.Canvas(brand, width=40, height=40, bg=SIDE, highlightthickness=0)
        logo.pack(side="left")
        logo.create_oval(1, 5, 31, 35, fill="#7c3aed", outline="")
        logo.create_oval(9, 1, 39, 31, fill="#38bdf8", outline="")
        logo.create_text(24, 16, text="T", fill="#0b1220", font=("Segoe UI", 15, "bold"))
        tb = tk.Frame(brand, bg=SIDE)
        tb.pack(side="left", padx=10)
        tk.Label(tb, text="TuneUp", bg=SIDE, fg="#f8fafc", font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(tb, text="PC care & optimizer", bg=SIDE, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w")
        for key, icon, text in NAV:
            s._nav_item(key, icon, text)
        foot = tk.Frame(s.side, bg=SIDE)
        foot.pack(side="bottom", fill="x", padx=16, pady=16)
        adm = is_admin()
        tk.Label(foot, text="\u25cf Administrator" if adm else "\u25cf Standard user", bg=SIDE,
                 fg=GOOD if adm else WARN, font=("Segoe UI Semibold", 9)).pack(anchor="w")
        tk.Label(foot, text="All features unlocked" if adm else "Run as admin for system-wide cleaning",
                 bg=SIDE, fg=MUTED, font=("Segoe UI", 8), wraplength=190, justify="left").pack(anchor="w")

    def _nav_item(s, key, icon, text):
        row = tk.Frame(s.side, bg=SIDE, cursor="hand2")
        row.pack(fill="x", pady=1)
        bar = tk.Frame(row, bg=SIDE, width=4)
        bar.pack(side="left", fill="y")
        ic = tk.Label(row, text=icon, bg=SIDE, fg=ACC[key], font=("Segoe UI Symbol", 13), width=3)
        ic.pack(side="left", pady=10)
        tx = tk.Label(row, text=text, bg=SIDE, fg=MUTED, font=FONT, anchor="w")
        tx.pack(side="left", fill="x", expand=True)
        s.nav[key] = (row, bar, ic, tx)
        for w in (row, bar, ic, tx):
            w.bind("<Button-1>", lambda e, k=key: s.show(k))
            w.bind("<Enter>", lambda e, k=key: s._paint(k, True))
            w.bind("<Leave>", lambda e, k=key: s._paint(k, False))

    def _paint(s, k, hover=False):
        row, bar, ic, tx = s.nav[k]
        on = k == s.cur
        bg = "#17243b" if on else ("#121d31" if hover else SIDE)
        for w in (row, ic, tx):
            w.config(bg=bg)
        bar.config(bg=ACC[k] if on else bg)
        tx.config(fg="#ffffff" if on else (FG if hover else MUTED), font=FONTB if on else FONT)

    def show(s, key):
        if s.cur:
            s.pages[s.cur].pack_forget()
        s.cur = key
        s.pages[key].pack(fill="both", expand=True)
        for k in s.nav:
            s._paint(k)

    def _statusbar(s, parent):
        bar = tk.Frame(parent, bg=SIDE)
        bar.pack(side="bottom", fill="x")
        s.dot = tk.Label(bar, text="\u25cf", bg=SIDE, fg=GOOD, font=("Segoe UI", 9))
        s.dot.pack(side="left", padx=(14, 4))
        s.status = tk.Label(bar, text="", bg=SIDE, fg=MUTED, anchor="w", justify="left", font=("Segoe UI", 9))
        s.status.pack(side="left", fill="x", expand=True, pady=7)
        s.status.bind("<Configure>", lambda e: s.status.config(wraplength=max(200, e.width - 8)))
        s.pb = ttk.Progressbar(bar, style="Bar.Horizontal.TProgressbar", mode="indeterminate", length=130)

    def page(s, key, icon, title, sub):
        color = ACC[key]
        fr = tk.Frame(s.body, bg=BG)
        hdr = tk.Frame(fr, bg=BG)
        hdr.pack(fill="x", padx=26, pady=(20, 10))
        tk.Label(hdr, text=icon, bg=color, fg="#0b1220", font=("Segoe UI Symbol", 17), width=3).pack(side="left", padx=(0, 14))
        box = tk.Frame(hdr, bg=BG)
        box.pack(side="left")
        tk.Label(box, text=title, bg=BG, fg="#f8fafc", font=("Segoe UI Semibold", 18)).pack(anchor="w")
        tk.Label(box, text=sub, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        tk.Frame(fr, bg=color, height=2).pack(fill="x", padx=26)
        body = tk.Frame(fr, bg=BG)
        body.pack(fill="both", expand=True, padx=26, pady=14)
        s.pages[key] = fr
        return body

    def card(s, parent, title, color):
        c = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        tk.Frame(c, bg=color, height=3).pack(fill="x")
        tk.Label(c, text=title, bg=CARD, fg=color, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        return c

    def btn(s, parent, text, color="Blue", cmd=None):
        b = ttk.Button(parent, text=text, style=f"{color}.TButton", command=cmd, cursor="hand2")
        b.pack(side="left", padx=(0, 8))
        return b

    def toolbar(s, parent):
        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x", pady=(0, 10))
        return f

    def mk_tree(s, parent, cols, sortable=True):
        fr = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        fr.pack(fill="both", expand=True)
        t = Tree(fr, columns=[c[0] for c in cols], show="headings", selectmode="extended")
        for cid, title, w in cols:
            anc = "w" if cid in ("name", "cmd", "pub", "scope", "kind", "path", "state") else "e"
            t.heading(cid, text=title, anchor=anc, command=(lambda c=cid, tt=t: s.sort_by(tt, c)) if sortable else "")
            t.column(cid, width=w, anchor=anc,
                     stretch=(cid == cols[-1][0] or cid == "name"))
        sb = ttk.Scrollbar(fr, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)
        return t

    def sort_by(s, t, col):
        rev = s._sort.get((str(t), col), False)
        rows = sorted(((sort_key(t.set(i, col)), i) for i in t.get_children("")), reverse=rev)
        for n, (_, i) in enumerate(rows):
            t.move(i, "", n)
        s._sort[(str(t), col)] = not rev
        t.restripe()

    def st(s, text):
        s.status.config(text=text)

    def bg(s, fn, done=None):
        s._busy += 1
        s.pb.pack(side="right", padx=14)
        s.pb.start(12)
        s.dot.config(fg=WARN)

        def run():
            try:
                r = fn()
            except Exception as ex:
                r = ex

            def fin():
                s._busy = max(0, s._busy - 1)
                if not s._busy:
                    s.pb.stop()
                    s.pb.pack_forget()
                    s.dot.config(fg=GOOD)
                if done:
                    done(r)
            s.root.after(0, fin)
        threading.Thread(target=run, daemon=True).start()

    def admin(s):
        args = " ".join(f'"{a}"' for a in sys.argv[1:])
        if not getattr(sys, "frozen", False):
            args = f'"{os.path.abspath(sys.argv[0])}" ' + args
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)
        s.root.destroy()

    # ---------- dashboard ----------
    def _build_dashboard(s):
        b = s.page("dash", "\u25c8", "Dashboard", "Live overview of your PC's health")
        top = tk.Frame(b, bg=BG)
        top.pack(fill="x")
        top.columnconfigure(2, weight=1)
        c1 = s.card(top, "CPU", "#38bdf8")
        c1.grid(row=0, column=0, padx=(0, 12), sticky="ns")
        s.g_cpu = Gauge(c1)
        s.g_cpu.pack(padx=26, pady=(2, 14))
        c2 = s.card(top, "MEMORY", "#fb923c")
        c2.grid(row=0, column=1, padx=(0, 12), sticky="ns")
        s.g_ram = Gauge(c2)
        s.g_ram.pack(padx=26, pady=(2, 14))
        c3 = s.card(top, "SYSTEM", "#a78bfa")
        c3.grid(row=0, column=2, sticky="nsew")
        g = tk.Frame(c3, bg=CARD)
        g.pack(fill="both", expand=True, padx=18, pady=(6, 12))
        g.columnconfigure(1, weight=1)
        s.kv = {}
        for r, k in enumerate(("Windows", "CPU cores", "Uptime", "Startup apps", "Installed programs")):
            tk.Label(g, text=k, bg=CARD, fg=MUTED, font=FONT, anchor="w").grid(row=r, column=0, sticky="w", pady=4)
            s.kv[k] = tk.Label(g, text="-", bg=CARD, fg=FG, font=FONTB, anchor="e")
            s.kv[k].grid(row=r, column=1, sticky="e", padx=(24, 0))
        mid = tk.Frame(b, bg=BG)
        mid.pack(fill="both", expand=True, pady=12)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        c4 = s.card(mid, "LIVE USAGE", "#38bdf8")
        c4.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        s.chart = Chart(c4)
        s.chart.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        c5 = s.card(mid, "DRIVES", "#34d399")
        c5.grid(row=0, column=1, sticky="nsew")
        s.drives = DriveBars(c5)
        s.drives.pack(fill="x", padx=16, pady=(6, 14))
        row = tk.Frame(b, bg=BG)
        row.pack(fill="x")
        s.btn(row, "\u2726  ONE-CLICK CARE  (clean all junk)", "Green", s.one_click)
        s.btn(row, "Free RAM now", "Violet", s.free_ram)
        s.btn(row, "Refresh", "Slate", s.refresh_info)
        s.btn(row, "Run as administrator", "Amber", s.admin)
        tk.Label(b, bg=BG, fg=MUTED, justify="left", wraplength=900, font=("Segoe UI", 9), text=(
            "Registry tab removes only clearly orphaned entries (pointing to files that no longer exist) and "
            "saves a .reg backup first. 'Free RAM now' trims working sets - a temporary effect, Windows refills "
            "RAM as needed. Biggest real gains: junk removal, fewer startup programs, free disk space.")
                 ).pack(anchor="w", pady=(10, 0))

    def refresh_info(s):
        try:
            s.kv["Windows"].config(text=os_name())
            s.kv["CPU cores"].config(text=str(os.cpu_count()))
            s.kv["Uptime"].config(text=dur(uptime_secs()) + "   (restart now and then!)")
        except Exception:
            pass
        try:
            s.kv["Startup apps"].config(text=f"{sum(1 for i in startup_items() if i[5])} enabled")
            s.kv["Installed programs"].config(text=str(len(installed_programs())))
            s.drives.set(drives_info())
        except Exception:
            pass

    def tick(s):
        try:
            used, total, load = mem_info()
            s.g_ram.set(load, f"{fmt(used)} / {fmt(total)}")
            i, k, u = cpu_times()
            if s.cpu0:
                di, tot = i - s.cpu0[0], (k - s.cpu0[1]) + (u - s.cpu0[2])
                cpu = max(0, min(100, 100 * (1 - di / tot))) if tot else 0
                s.g_cpu.set(cpu, f"{os.cpu_count()} cores")
                s.chart.push(cpu, load)
            s.cpu0 = (i, k, u)
        except Exception:
            pass
        s.root.after(1500, s.tick)

    def free_ram(s):
        before = ram_load()
        s.st("Trimming memory...")
        s.bg(trim_ram, lambda r: s.st(f"Trimmed {r} processes. RAM use {before}% -> {ram_load()}% "
                                      "(Windows refills it as programs need it).")
             if not isinstance(r, Exception) else s.st(f"Error: {r}"))

    # ---------- junk ----------
    def _build_junk(s):
        b = s.page("junk", "\u267b", "Junk & privacy", "Remove temporary files, caches and browsing traces")
        bar = s.toolbar(b)
        s.btn(bar, "Scan", "Blue", s.scan_junk)
        s.btn(bar, "Clean selected", "Green", s.clean_junk)
        box = tk.Frame(bar, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        box.pack(side="right")
        s.junk_big = tk.Label(box, text="-", bg=CARD, fg=GOOD, font=("Segoe UI Semibold", 18))
        s.junk_big.pack(side="left", padx=(16, 8), pady=6)
        tk.Label(box, text="removable\njunk found", bg=CARD, fg=MUTED, font=("Segoe UI", 8), justify="left"
                 ).pack(side="left", padx=(0, 16))
        s.t_junk = s.mk_tree(b, [("name", "Category", 420), ("size", "Size", 120), ("files", "Files", 100)])
        tk.Label(b, bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w",
                 text="Junk rows are pre-selected. Privacy rows (amber) are not - cookies will sign you out of websites."
                 ).pack(fill="x", pady=(8, 0))

    def scan_junk(s):
        s.st("Scanning for junk files...")
        s.bg(lambda: {n: (*scan_cat(p), p) for n, p in ALL}, s.junk_done)

    def junk_done(s, res):
        if isinstance(res, Exception):
            return s.st(f"Scan failed: {res}")
        s.cats = res
        s.t_junk.delete(*s.t_junk.get_children())
        for n, (files, size, _) in res.items():
            s.t_junk.insert("", "end", iid=n, values=(n, fmt(size), f"{len(files):,}"))
        s.t_junk.selection_set([n for n, _ in CATS])
        total = sum(v[1] for v in res.values())
        s.junk_big.config(text=fmt(total))
        s.st(f"Found {fmt(total)} of removable junk. Junk is pre-selected; tick Privacy rows yourself. Close browsers first.")

    def do_clean(s, names):
        freed = failed = 0
        for n in names:
            files, _, pats = s.cats[n]
            a, b = clean_cat(n, files, pats)
            freed += a
            failed += b
        return freed, failed

    def clean_junk(s):
        names = list(s.t_junk.selection())
        if not names:
            return s.st("Scan first, then select categories.")
        if not messagebox.askyesno("Clean", "Permanently delete the selected junk files?"):
            return
        s.st("Cleaning...")
        s.bg(lambda: s.do_clean(names), lambda r: (
            s.st(f"Freed {fmt(r[0])}; {r[1]} files were in use and skipped.") if not isinstance(r, Exception)
            else s.st(f"Error: {r}"), s.scan_junk()))

    def one_click(s):
        if not messagebox.askyesno("One-click care", "Scan and permanently delete all junk categories?"):
            return

        def work():
            s.cats = {n: (*scan_cat(p), p) for n, p in CATS}
            return s.do_clean(list(s.cats))

        s.st("Cleaning everything...")
        s.bg(work, lambda r: (s.st(f"Done: freed {fmt(r[0])}.") if not isinstance(r, Exception)
                              else s.st(f"Error: {r}"), s.refresh_info()))

    # ---------- startup ----------
    def _build_startup(s):
        b = s.page("start", "\u26a1", "Startup programs",
                   "Fewer startup apps means a faster boot - disable what you don't need")
        bar = s.toolbar(b)
        s.btn(bar, "Enable / Disable selected", "Amber", s.su_toggle)
        s.btn(bar, "Reload", "Slate", s.load_startup)
        s.t_su = s.mk_tree(b, [("name", "Program", 210), ("state", "State", 90),
                               ("scope", "Scope", 130), ("cmd", "Command", 420)])

    def load_startup(s):
        s.t_su.delete(*s.t_su.get_children())
        s.su = {}
        for i, (hive, appr, name, cmd, scope, en) in enumerate(startup_items()):
            s.su[str(i)] = (hive, appr, name, en)
            s.t_su.insert("", "end", iid=str(i), values=(name, "Enabled" if en else "Disabled", scope, cmd))

    def su_toggle(s):
        for iid in s.t_su.selection():
            hive, appr, name, en = s.su[iid]
            try:
                k = winreg.CreateKeyEx(hive, appr, 0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(k, name, 0, winreg.REG_BINARY, (b"\x03" if en else b"\x02") + b"\x00" * 11)
                winreg.CloseKey(k)
            except OSError as ex:
                messagebox.showerror("Startup", f"{name}: {ex}\n(All-users entries need 'Run as administrator')")
        s.load_startup()

    # ---------- uninstall ----------
    def _build_apps(s):
        b = s.page("apps", "\u25a3", "Uninstall programs", "Remove software you no longer use")
        bar = s.toolbar(b)
        s.btn(bar, "Uninstall selected", "Rose", s.uninstall)
        s.btn(bar, "Reload", "Slate", s.load_apps)
        s.t_app = s.mk_tree(b, [("name", "Program", 330), ("pub", "Publisher", 210),
                                ("ver", "Version", 110), ("size", "Size", 100)])

    def load_apps(s):
        s.t_app.delete(*s.t_app.get_children())
        s.apps = {}
        for i, (n, p, v, sz, cmd) in enumerate(installed_programs()):
            s.apps[str(i)] = (n, cmd)
            s.t_app.insert("", "end", iid=str(i), values=(n, p, v, fmt(sz) if sz else ""))

    def uninstall(s):
        sel = s.t_app.selection()
        if sel and messagebox.askyesno("Uninstall", f"Start the uninstaller for '{s.apps[sel[0]][0]}'?"):
            subprocess.Popen(s.apps[sel[0]][1], shell=True)

    # ---------- processes ----------
    def _build_procs(s):
        b = s.page("proc", "\u2630", "Running processes", "See what is using your memory - click Memory to sort")
        bar = s.toolbar(b)
        s.btn(bar, "Refresh", "Slate", s.load_procs)
        s.btn(bar, "End selected process", "Red", s.kill)
        s.t_proc = s.mk_tree(b, [("name", "Process", 380), ("pid", "PID", 100), ("mem", "Memory", 130)])

    def load_procs(s):
        s.t_proc.delete(*s.t_proc.get_children())
        try:
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                 errors="replace", creationflags=NOWIN).stdout
        except OSError:
            return
        rows = []
        for r in csv.reader(io.StringIO(out)):
            if len(r) >= 5:
                kb = int("".join(c for c in r[4] if c.isdigit()) or 0)
                rows.append((kb, r[0], r[1]))
        for kb, name, pid in sorted(rows, reverse=True):
            s.t_proc.insert("", "end", iid=pid, values=(name, pid, fmt(kb * 1024)))

    def kill(s):
        sel = s.t_proc.selection()
        if sel and messagebox.askyesno("End process", f"Force-close {len(sel)} process(es)? Unsaved work is lost."):
            for pid in sel:
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, creationflags=NOWIN)
            s.load_procs()

    # ---------- large & duplicate files ----------
    def _build_files(s):
        b = s.page("files", "\u2756", "Large & duplicate files", "Find what is eating your disk space")
        s.ld_path, s.ld_mb = tk.StringVar(value=os.path.expanduser("~")), tk.StringVar(value="10")
        r1 = s.toolbar(b)
        ttk.Entry(r1, textvariable=s.ld_path).pack(side="left", fill="x", expand=True, padx=(0, 8))
        s.btn(r1, "Browse...", "Slate", s.ld_browse)
        tk.Label(r1, text="Min MB:", bg=BG, fg=MUTED, font=FONT).pack(side="left", padx=(8, 4))
        ttk.Entry(r1, textvariable=s.ld_mb, width=6).pack(side="left")
        r2 = s.toolbar(b)
        s.btn(r2, "Find large files", "Teal", s.find_large)
        s.btn(r2, "Find duplicates", "Blue", s.find_dups)
        s.btn(r2, "Select all but first copy", "Slate", s.dup_select)
        s.btn(r2, "Delete selected", "Red", s.ld_delete)
        s.t_ld = s.mk_tree(b, [("name", "File / group", 540), ("size", "Size", 110), ("mod", "Modified", 150)],
                           sortable=False)

    def ld_browse(s):
        d = filedialog.askdirectory()
        if d:
            s.ld_path.set(os.path.normpath(d))

    def _ld_show(s, rows):
        s.t_ld.delete(*s.t_ld.get_children())
        for iid, text, size, mod in rows:
            try:
                s.t_ld.insert("", "end", iid=iid, values=(text, size, mod))
            except tk.TclError:
                pass

    @staticmethod
    def _mt(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    def find_large(s):
        root, mn = s.ld_path.get(), num(s.ld_mb.get(), 10) * 1048576
        s.st("Searching for large files...")

        def work():
            res = []
            for r, _, fs in os.walk(root):
                for f in fs:
                    fp = os.path.join(r, f)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    if st.st_size >= mn:
                        res.append((st.st_size, st.st_mtime, fp))
            return sorted(res, reverse=True)[:500]

        def done(r):
            if isinstance(r, Exception):
                return s.st(f"Error: {r}")
            s.ld_groups = []
            s._ld_show([(fp, fp, fmt(sz), s._mt(mt)) for sz, mt, fp in r])
            s.st(f"{len(r)} large files (top 500 shown)")

        s.bg(work, done)

    def find_dups(s):
        root, mn = s.ld_path.get(), max(1, num(s.ld_mb.get(), 10) * 1048576)
        s.st("Looking for duplicates (size -> quick hash -> full hash)...")

        def work():
            by = {}
            for r, _, fs in os.walk(root):
                for f in fs:
                    fp = os.path.join(r, f)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    if sz >= mn:
                        by.setdefault(sz, []).append(fp)
            groups = []
            for sz, ps in by.items():
                if len(ps) < 2:
                    continue
                quick = {}
                for p in ps:
                    h = file_hash(p, 65536)
                    if h:
                        quick.setdefault(h, []).append(p)
                for ps2 in quick.values():
                    if len(ps2) < 2:
                        continue
                    full = {}
                    for p in ps2:
                        h = file_hash(p)
                        if h:
                            full.setdefault(h, []).append(p)
                    groups += [(sz, g) for g in full.values() if len(g) > 1]
            groups.sort(key=lambda g: -g[0] * (len(g[1]) - 1))
            return groups[:1000]

        def done(r):
            if isinstance(r, Exception):
                return s.st(f"Error: {r}")
            s.ld_groups = [g for _, g in r]
            rows = []
            for i, (sz, g) in enumerate(r):
                rows.append((f"g{i}", f"== {len(g)} identical copies - wasted {fmt(sz * (len(g) - 1))}", "", ""))
                for p in g:
                    try:
                        rows.append((p, "      " + p, fmt(sz), s._mt(os.path.getmtime(p))))
                    except OSError:
                        pass
            s._ld_show(rows)
            s.st(f"{len(r)} duplicate groups - {fmt(sum(sz * (len(g) - 1) for sz, g in r))} could be freed. "
                 "Use 'Select all but first copy'.")

        s.bg(work, done)

    def dup_select(s):
        s.t_ld.selection_set([p for g in s.ld_groups for p in g[1:] if s.t_ld.exists(p)])

    def ld_delete(s):
        ps = [i for i in s.t_ld.selection() if os.path.isfile(i)]
        how = "to the Recycle Bin" if send2trash else "PERMANENTLY (pip install send2trash for Recycle Bin)"
        if not ps or not messagebox.askyesno("Delete", f"Delete {len(ps)} file(s) {how}?\n\n" + "\n".join(ps[:8])):
            return
        n = 0
        for p in ps:
            try:
                send2trash(p) if send2trash else os.remove(p)
                s.t_ld.delete(p)
                n += 1
            except OSError as ex:
                messagebox.showerror("Delete failed", f"{p}\n{ex}")
        s.st(f"Deleted {n} file(s).")

    # ---------- registry ----------
    def _build_registry(s):
        b = s.page("reg", "\u2637", "Registry cleaner", "Removes only entries that point to files which no longer exist")
        bar = s.toolbar(b)
        s.btn(bar, "Scan", "Orange", s.reg_scan_ui)
        s.btn(bar, "Fix selected (backs up first)", "Green", s.reg_fix_ui)
        s.btn(bar, "Open backup folder", "Slate", s.reg_open_bk)
        s.t_reg = s.mk_tree(b, [("kind", "Problem", 190), ("name", "Entry", 520), ("hive", "Hive", 80)])
        tk.Label(b, bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w",
                 text="Each fix is saved as a .reg file in the backup folder - double-click it to restore. "
                      "All-users entries need 'Run as administrator'.").pack(fill="x", pady=(8, 0))

    def reg_scan_ui(s):
        s.st("Scanning registry for orphaned entries...")

        def done(r):
            if isinstance(r, Exception):
                return s.st(f"Scan failed: {r}")
            s.reg_issues = r
            s.t_reg.delete(*s.t_reg.get_children())
            for i, it in enumerate(r):
                s.t_reg.insert("", "end", iid=str(i), values=(it[0], it[4], it[1]))
            s.t_reg.selection_set(s.t_reg.get_children())
            s.st(f"{len(r)} orphaned registry entries found.")

        s.bg(reg_scan, done)

    def reg_fix_ui(s):
        sel = [s.reg_issues[int(i)] for i in s.t_reg.selection()]
        if not sel or not messagebox.askyesno("Registry", f"Remove {len(sel)} orphaned entries?\n"
                                              "A .reg backup is saved first."):
            return

        def done(r):
            if isinstance(r, Exception):
                return s.st(f"Error: {r}")
            s.st(f"Fixed {r[0]}, skipped {r[1]} (no permission / backup failed). Backup: {r[2]}")
            s.reg_scan_ui()

        s.bg(lambda: reg_fix(sel), done)

    def reg_open_bk(s):
        os.makedirs(REG_BK, exist_ok=True)
        os.startfile(REG_BK)

    # ---------- shredder ----------
    def _build_shredder(s):
        b = s.page("shred", "\u2702", "File shredder", "Overwrite files so they cannot be recovered")
        bar = s.toolbar(b)
        s.btn(bar, "Add files...", "Blue", s.shred_add)
        s.btn(bar, "Clear list", "Slate", lambda: s.shred_list.delete(0, "end"))
        s.btn(bar, "SHRED  (3 passes, irreversible)", "Red", s.shred_go)
        fr = tk.Frame(b, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        fr.pack(fill="both", expand=True)
        s.shred_list = tk.Listbox(fr, selectmode="extended", bg=CARD, fg=FG, selectbackground="#2563eb",
                                  selectforeground="#fff", borderwidth=0, highlightthickness=0,
                                  activestyle="none", font=FONT)
        s.shred_list.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Label(b, bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w",
                 text="Overwrites files before deleting. On SSDs overwriting is not guaranteed - "
                      "use full-disk encryption (BitLocker) for that.").pack(fill="x", pady=(8, 0))

    def shred_add(s):
        for p in filedialog.askopenfilenames():
            s.shred_list.insert("end", os.path.normpath(p))

    def shred_go(s):
        ps = list(s.shred_list.get(0, "end"))
        if not ps or not messagebox.askyesno("Shred", f"Irreversibly destroy {len(ps)} file(s)?"):
            return

        def work():
            ok = 0
            for p in ps:
                try:
                    shred_file(p)
                    ok += 1
                except OSError:
                    pass
            return ok

        s.bg(work, lambda r: (s.shred_list.delete(0, "end"), s.st(f"Shredded {r}/{len(ps)} files.")))

    # ---------- tools ----------
    def _build_tools(s):
        b = s.page("tools", "\u2699", "Tools", "One-click maintenance, security and network utilities   (* = needs Run as administrator)")
        pw = ttk.PanedWindow(b, orient="vertical")      # drag the divider to resize the output area
        pw.pack(fill="both", expand=True)
        holder = tk.Frame(pw, bg=BG)
        cv = tk.Canvas(holder, bg=BG, highlightthickness=0, height=240)
        sb = ttk.Scrollbar(holder, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(cv, bg=BG)
        cv.create_window((0, 0), window=inner, anchor="nw", tags="w")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfigure("w", width=e.width))
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", lambda ev: cv.yview_scroll(int(-ev.delta / 120), "units")))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
        for c in range(3):
            inner.columnconfigure(c, weight=1, uniform="g")
        for n, (title, color, tools) in enumerate(tool_groups()):
            col = BTN[color]
            card = tk.Frame(inner, bg=CARD, highlightthickness=1, highlightbackground=LINE)
            card.grid(row=n // 3, column=n % 3, sticky="nsew", padx=6, pady=6)
            tk.Frame(card, bg=col, height=3).pack(fill="x")
            tk.Label(card, text=title.upper(), bg=CARD, fg=shade(col, 1.35), font=("Segoe UI", 8, "bold")
                     ).pack(anchor="w", padx=12, pady=(10, 4))
            for t, c in tools:
                ttk.Button(card, text=t, style=f"Soft{color}.TButton", cursor="hand2",
                           command=lambda c=c, t=t: s.run_tool(t, c)).pack(fill="x", padx=10, pady=2)
            tk.Frame(card, bg=CARD, height=8).pack()
        lf = tk.Frame(pw, bg="#080d18", highlightthickness=1, highlightbackground=LINE)
        pw.add(holder, weight=3)
        pw.add(lf, weight=2)
        s.log = tk.Text(lf, height=10, wrap="word", bg="#080d18", fg="#86efac", insertbackground="#86efac", borderwidth=0,
                        highlightthickness=0, font=("Consolas", 9), padx=10, pady=8)
        ls = ttk.Scrollbar(lf, command=s.log.yview)
        s.log.configure(yscrollcommand=ls.set)
        ls.pack(side="right", fill="y")
        s.log.pack(fill="both", expand=True)
        s.log.insert("end", "Output of tools appears here.\n")

    def run_tool(s, title, cmd):
        s.log.insert("end", f"\n> {title}\n")
        s.st(f"Running: {title} ...")

        def work():
            p = subprocess.run(cmd, shell=True, capture_output=True, creationflags=NOWIN)   # bytes, decoded below
            return decode_output(p.stdout + p.stderr)

        def done(r):
            s.log.insert("end", (str(r).strip() or "(finished)") + "\n")
            s.log.see("end")
            s.st(f"Finished: {title}")

        s.bg(work, done)


if __name__ == "__main__":
    if "--auto" in sys.argv:
        auto_clean()
        sys.exit(0)
    r = tk.Tk()
    App(r)
    r.mainloop()
