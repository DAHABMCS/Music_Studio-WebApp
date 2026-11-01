
"""
Media Check and Replace
-----------------------
OPTION A - Check existing files
    Select MP3 / MP4 files. Each is tested by fully decoding it with FFmpeg.
    Damaged files are repaired if possible; if not, the internet is searched
    (using the file's tags or file name) and a replacement is downloaded.

OPTION B - Search by song name
    Type a song or video name, choose MP3 or MP4, and the program searches
    the web and lists every version it finds (song name, singer, length,
    source, license). You pick one, it is downloaded and tested, and then you
    press "Save to output folder" to keep it.

Sources (your choice in the window):
    - Free and open sources only: Internet Archive items that carry a
      Creative Commons or public-domain license
    - YouTube (anything findable, through yt-dlp)
    - Free sources first, then YouTube if nothing free is found

After a file is saved it is opened in your web browser so you can play it.
Every file saved is a plain  name.mp3  or  name.mp4  (nothing else is ever
left in the output folder). Originals are never modified or deleted. If a
file with the same name already exists, a number is added: name (1).mp3

Requirements:
    - Python 3.8+
    - FFmpeg and ffprobe in your PATH   (https://ffmpeg.org)
    - yt-dlp (only for the YouTube option):   python -m pip install -U yt-dlp

Only download material you have the right to copy, and respect the terms of
the sites you download from.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# Windowed .exe builds have no console, so stdout/stderr can be None
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# Let the program find ffmpeg.exe / ffprobe.exe placed next to it
APP_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
# FFmpeg packed inside a one-file .exe is unpacked to sys._MEIPASS
_BUNDLE_DIR = getattr(sys, "_MEIPASS", "")
os.environ["PATH"] = (str(APP_DIR) + os.pathsep
                      + (str(_BUNDLE_DIR) + os.pathsep if _BUNDLE_DIR else "")
                      + os.environ.get("PATH", ""))

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
MIN_GOOD_SIZE = 1024  # anything smaller than 1 KB is treated as broken
UA = {"User-Agent": "MediaCheckAndReplace/1.0"}
FREE_LICENSE_HINTS = ("creativecommons.org", "publicdomain", "public domain")


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def run(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, errors="replace",
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
    )


def sanitize_filename(name):
    """Make text safe to use as a Windows file name (no extension)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120] or "download"


def unique_path(folder, stem, ext):
    """folder/stem.ext, or folder/stem (1).ext if that name is taken."""
    path = Path(folder) / f"{stem}{ext}"
    n = 1
    while path.exists():
        path = Path(folder) / f"{stem} ({n}){ext}"
        n += 1
    return path


def is_damaged(path):
    """Fully decode the file with FFmpeg. Returns (damaged, reason)."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < MIN_GOOD_SIZE:
        return True, "file is empty or tiny"
    r = run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "null", "-"])
    err = r.stderr.strip()
    if r.returncode != 0 or err:
        return True, (err.splitlines()[0] if err else "decode error")
    return False, ""


def is_playable(path, kind):
    """Lenient check for downloaded files: has the right stream and the first
    seconds decode. Harmless decoder warnings do not count as failure."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < MIN_GOOD_SIZE:
        return False, "file is empty or tiny"
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(p)])
    try:
        streams = json.loads(r.stdout).get("streams", [])
    except Exception:
        return False, "not a valid media file"
    want = "video" if kind == "mp4" else "audio"
    if not any(st.get("codec_type") == want for st in streams):
        return False, f"no {want} stream found in the file"
    r = run(["ffmpeg", "-v", "error", "-t", "15", "-i", str(p), "-f", "null", "-"])
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()
        return False, err[0] if err else "could not decode the file"
    return True, ""


def try_repair(src, work_dir):
    """Attempt to rebuild a damaged file inside work_dir. Returns path or None."""
    src = Path(src)
    out = Path(work_dir) / f"repaired{src.suffix.lower()}"
    if src.suffix.lower() == ".mp3":
        attempts = [
            ["ffmpeg", "-v", "error", "-y", "-err_detect", "ignore_err", "-i", str(src),
             "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)],
        ]
    else:
        base = ["ffmpeg", "-v", "error", "-y", "-err_detect", "ignore_err",
                "-fflags", "+genpts+discardcorrupt", "-i", str(src)]
        attempts = [
            base + ["-c", "copy", "-movflags", "+faststart", str(out)],
            base + ["-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-movflags", "+faststart", str(out)],
        ]
    for cmd in attempts:
        run(cmd)
        if out.exists() and out.stat().st_size > MIN_GOOD_SIZE and not is_damaged(out)[0]:
            return out
        if out.exists():
            out.unlink()
    return None


def guess_query(path):
    """Build a search string from the file's tags, else from its file name."""
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)])
    try:
        tags = {k.lower(): v for k, v in json.loads(r.stdout).get("format", {}).get("tags", {}).items()}
        title, artist = tags.get("title"), tags.get("artist")
        if title:
            return f"{artist} - {title}" if artist else title
    except Exception:
        pass
    name = re.sub(r"_+", " ", Path(path).stem)
    return re.sub(r"\s+", " ", name).strip()


def fmt_duration(sec):
    if not sec:
        return "unknown length"
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def split_title(title, channel):
    """Split 'Artist - Song' video titles into (song, singer)."""
    t = title or "?"
    ch = re.sub(r"\s*-\s*Topic$", "", channel or "", flags=re.I).strip()
    m = re.match(r"^\s*(.+?)\s+[-\u2013\u2014]\s+(.+)$", t)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return t, ch or "?"


def short_license(url):
    if not url:
        return "-"
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z\-]+)/?([\d.]+)?", url.lower())
    if not m:
        return "Free / open"
    kind, code, ver = m.groups()
    if kind == "publicdomain":
        return "Public domain (CC0)" if code == "zero" else "Public domain"
    return ("CC " + code.upper() + (" " + ver if ver else "")).strip()


# ---------------------------- YouTube ---------------------------------
def search_web(query, count=8):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
    results = []
    for e in info.get("entries", []) or []:
        if not e:
            continue
        song, singer = split_title(e.get("title", "?"), e.get("channel") or e.get("uploader") or "")
        results.append({
            "source": "yt",
            "license": "",
            "title": e.get("title", "?"),
            "song": song,
            "singer": singer,
            "channel": e.get("channel") or e.get("uploader") or "?",
            "duration": e.get("duration"),
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
        })
    return results


def download_youtube(url, kind, work_dir):
    """Download into work_dir as dl.mp3 / dl.mp4. Returns the path."""
    opts = {"outtmpl": str(Path(work_dir) / "dl.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True}
    if kind == "mp3":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio",
                                "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    else:
        opts.update({
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "merge_output_format": "mp4",
        })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return Path(work_dir) / f"dl.{kind}"


# ------------------------- Internet Archive ---------------------------
def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def is_free_license(lic):
    lic = (lic or "").lower()
    return any(h in lic for h in FREE_LICENSE_HINTS)


STOPWORDS = {"the", "and", "for", "with", "from", "not", "official", "video", "audio",
             "lyrics", "hd", "hq", "mp3", "mp4", "of", "in", "to", "or", "a", "an"}


def _tokens(text):
    """Lower-case search words, no duplicates, order kept, stop words removed."""
    seen, out = set(), []
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(t) >= 2 and t not in STOPWORDS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def relevance(query, *texts):
    """(share of query words found in the texts, number of matching words)."""
    q = _tokens(query)
    if not q:
        return 0.0, 0
    have = set(_tokens(" ".join(t or "" for t in texts)))
    hits = sum(1 for t in q if t in have)
    return hits / len(q), hits


def _archive_query(words, kind, count, op="AND"):
    mediatype = "audio" if kind == "mp3" else "movies"
    q = "(" + f" {op} ".join(words) + f") AND mediatype:{mediatype}"
    params = [("q", q), ("rows", str(count)), ("output", "json")]
    for fld in ("identifier", "title", "creator", "licenseurl"):
        params.append(("fl[]", fld))
    url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)
    return http_json(url).get("response", {}).get("docs", [])


def search_archive(query, kind, count=60):
    """Search the Internet Archive. Keeps only Creative Commons / public domain items
    whose title or author really matches the search words, best match first."""
    tokens = _tokens(query)
    if not tokens:
        return []
    attempts = [(tokens[:6], "AND")]
    if len(tokens) > 3:
        attempts.append((tokens[:3], "AND"))
    attempts.append((tokens[:6], "OR"))
    results, seen = [], set()
    for words, op in attempts:
        for d in _archive_query(words, kind, count, op):
            ident = d.get("identifier")
            if not ident or ident in seen:
                continue
            lic = _first(d.get("licenseurl"))
            if not is_free_license(lic):
                continue
            title = _first(d.get("title")) or ident
            creator = _first(d.get("creator"))
            score, hits = relevance(query, title, creator)
            # must really match: at least half the words, or at least 3 words
            if score < 0.5 and hits < 3:
                continue
            seen.add(ident)
            results.append({
                "source": "ia",
                "id": ident,
                "title": title,
                "song": title,
                "singer": creator or "Unknown",
                "channel": creator or "?",
                "duration": None,
                "license": lic,
                "score": score,
                "url": f"https://archive.org/details/{ident}",
            })
        if results:
            break
    results.sort(key=lambda r: -r["score"])
    return results[:15]


def download_archive(item_id, kind, work_dir):
    """Download the best file of an Internet Archive item into work_dir as dl.mp3 / dl.mp4."""
    work_dir = Path(work_dir)
    meta = http_json(f"https://archive.org/metadata/{urllib.parse.quote(item_id)}")
    files = meta.get("files", [])
    if kind == "mp3":
        direct = {".mp3"}
        convert = {".flac", ".ogg", ".oga", ".wav", ".m4a", ".opus"}
    else:
        direct = {".mp4", ".m4v"}
        convert = {".mkv", ".webm", ".ogv", ".avi", ".mov", ".mpg", ".mpeg"}

    def ext(f):
        return Path(f.get("name", "")).suffix.lower()

    def size(f):
        try:
            return int(f.get("size", 0))
        except (TypeError, ValueError):
            return 0

    good = [f for f in files if "thumb" not in f.get("name", "").lower()]
    pool = [f for f in good if ext(f) in direct] or [f for f in good if ext(f) in convert]
    if not pool:
        raise RuntimeError("no usable audio/video file inside this item")
    pick = max(pool, key=size)

    url = (f"https://archive.org/download/{urllib.parse.quote(item_id)}/"
           f"{urllib.parse.quote(pick['name'])}")
    raw = work_dir / f"source{ext(pick)}"
    final = work_dir / f"dl.{kind}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(raw, "wb") as fh:
        shutil.copyfileobj(r, fh, 256 * 1024)

    if ext(pick) in direct:
        raw.replace(final)
    else:
        if kind == "mp3":
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-vn",
                   "-c:a", "libmp3lame", "-q:a", "2", str(final)]
        else:
            cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c:v", "libx264",
                   "-preset", "fast", "-crf", "23", "-c:a", "aac", str(final)]
        run(cmd)
    return final


# ----------------------------------------------------------------------
# Look and feel
# ----------------------------------------------------------------------
C = {
    "bg": "#eaf0ff", "card": "#ffffff", "ink": "#1e293b", "muted": "#64748b",
    "indigo": "#4f46e5", "teal": "#0d9488", "green": "#16a34a",
    "orange": "#f97316", "pink": "#db2777", "gray": "#9ca3af","red":"#e74c3c",
    "log_bg": "#0f172a", "log_fg": "#e2e8f0",
}
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(r, g, b):
    return "#%02x%02x%02x" % (r, g, b)


def lighten(h, a):
    return _hex(*(int(v + (255 - v) * a) for v in _rgb(h)))


def darken(h, a):
    return _hex(*(int(v * (1 - a)) for v in _rgb(h)))


class FancyButton(tk.Button):
    """Flat coloured button with a hover effect and a greyed-out disabled look."""

    def __init__(self, master, text, command, color, big=False):
        super().__init__(
            master, text=text, command=command, bg=color, fg="white",
            activebackground=darken(color, 0.15), activeforeground="white",
            disabledforeground="#f1f5f9", relief="flat", bd=0, cursor="hand2",
            font=FONT_B if big else FONT, padx=18 if big else 12, pady=8 if big else 4)
        self.base = color
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _enter(self, _e):
        if str(self["state"]) == "normal":
            self.config(bg=lighten(self.base, 0.18))

    def _leave(self, _e):
        self.config(bg=self.base if str(self["state"]) == "normal" else C["gray"])

    def set_enabled(self, on):
        self.config(state="normal" if on else "disabled",
                    bg=self.base if on else C["gray"],
                    cursor="hand2" if on else "arrow")


# Logo (Aboul Dahab), pre-sized for the header and embedded so no extra file is needed
LOGO_BG = "#1c88f4"
LOGO_B64 = (
    "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"
    "guSzezE7GwAAAABJRU5ErkJggg=="
)


class Header(tk.Canvas):
    """Banner with a left-to-right colour gradient."""

    def __init__(self, master, title, subtitle, c1, c2, logo=None):
        super().__init__(master, height=64, highlightthickness=0, bd=0)
        self.c1, self.c2, self.title, self.subtitle = c1, c2, title, subtitle
        self.logo = logo          # keep a reference so Tk does not discard it
        self.bind("<Configure>", self.redraw)

    def redraw(self, e):
        self.delete("all")
        w, h, step = max(e.width, 1), 64, 4
        a, b = _rgb(self.c1), _rgb(self.c2)
        for x in range(0, w, step):
            t = min(x / (w * 0.6), 1.0)   # gradient first, then solid colour behind the logo
            col = _hex(*(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
            self.create_rectangle(x, 0, x + step, h, fill=col, outline=col)
        self.create_text(18, 23, text=self.title, anchor="w", fill="white",
                         font=("Segoe UI", 17, "bold"))
        self.create_text(19, 46, text=self.subtitle, anchor="w", fill="#e0f2fe",
                         font=("Segoe UI", 9))
        if self.logo is not None:
            self.create_image(w - 12, h // 2, image=self.logo, anchor="e")


def make_card(parent, title, color):
    """Card with a coloured title bar and border. Returns (outer, body)."""
    outer = tk.Frame(parent, bg=color)
    tk.Label(outer, text=title, bg=color, fg="white", anchor="w",
             font=("Segoe UI", 11, "bold"), padx=12, pady=4).pack(fill="x")
    body = tk.Frame(outer, bg=C["card"], padx=12, pady=8)
    body.pack(fill="both", expand=True, padx=2, pady=(0, 2))
    return outer, body


def readable_name(name):
    """Plain readable text: underscores become spaces, extra spaces are removed."""
    return re.sub(r"\s+", " ", (name or "").replace("_", " ")).strip()


def add_edit_menu(entry):
    """Ctrl+V / C / X / A that work with ANY keyboard layout (Tk's built-in shortcuts
    fail with non-English layouts such as Arabic), plus a right-click menu."""
    def do(event_name):
        entry.focus_set()
        entry.event_generate(event_name)

    def select_all():
        entry.focus_set()
        entry.select_range(0, "end")
        entry.icursor("end")

    def paste():
        """Replace any selected text with the clipboard text (one line only)."""
        entry.focus_set()
        try:
            text = entry.clipboard_get()
        except tk.TclError:                # clipboard empty or not text
            return
        text = re.sub(r"[\r\n\t]+", " ", text)
        try:
            if entry.selection_present():
                entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        entry.insert("insert", text)

    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Cut", command=lambda: do("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: do("<<Copy>>"))
    menu.add_command(label="Paste", command=paste)
    menu.add_separator()
    menu.add_command(label="Select All", command=select_all)

    def popup(e):
        entry.focus_set()
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def on_ctrl_key(e):
        if os.name == "nt":
            if e.state & 0x20000:          # Alt held: AltGr typing, not a shortcut
                return None
            code = e.keycode               # physical key, same on every layout
        else:
            code = {"v": 86, "c": 67, "x": 88, "a": 65}.get((e.keysym or "").lower(), 0)
        if code == 86:
            paste()
        elif code == 67:
            do("<<Copy>>")
        elif code == 88:
            do("<<Cut>>")
        elif code == 65:
            select_all()
        else:
            return None
        return "break"                      # stop Tk pasting a second time

    entry.bind("<Control-KeyPress>", on_ctrl_key)
    entry.bind("<Button-3>", popup)


def make_entry(parent, var):
    entry = tk.Entry(parent, textvariable=var, font=FONT, relief="flat", bg="#f8fafc",
                     fg=C["ink"], insertbackground=C["ink"], highlightthickness=1,
                     highlightbackground="#cbd5e1", highlightcolor=C["indigo"])
    add_edit_menu(entry)
    return entry


def make_radio(parent, text, var, value, fg=None):
    return tk.Radiobutton(parent, text=text, variable=var, value=value, bg=C["card"],
                          activebackground=C["card"], fg=fg or C["ink"],
                          activeforeground=fg or C["ink"], selectcolor="white",
                          font=FONT, anchor="w", cursor="hand2")


def make_check(parent, text, var):
    return tk.Checkbutton(parent, text=text, variable=var, bg=C["card"],
                          activebackground=C["card"], fg=C["ink"],
                          activeforeground=C["ink"], selectcolor="white",
                          font=FONT, anchor="w", cursor="hand2")


# ----------------------------------------------------------------------
# User manual (stored inside the program, so it is part of the .exe)
#   h = section heading (also listed in the topic list)   p = paragraph
#   li = bullet   n = numbered step   q = question/problem
#   tip / note / warn = coloured call-out boxes
# ----------------------------------------------------------------------
MANUAL = [
    ("h", "1.  Welcome"),
    ("p", "Media Check and Replace tests your MP3 and MP4 files, repairs the ones that are "
          "damaged and, when a repair is not possible, finds and downloads a replacement from "
          "the internet. You can also just search for a song or video by name and download it."),
    ("tip", "Your original files are never changed or deleted. Everything the program saves "
            "goes into one output folder that you choose."),

    ("h", "2.  What you need"),
    ("li", "An internet connection for searching and downloading. Checking and repairing files "
           "works without it."),
    ("li", "FFmpeg and ffprobe. They are either built into this program or must be in the same "
           "folder as the program file (ffmpeg.exe and ffprobe.exe). If they are missing, a red "
           "message appears in the Progress area and the action buttons are switched off."),
    ("li", "yt-dlp, used for the YouTube option. It is normally built into the program. If the "
           "Progress area says it is not installed, the YouTube option is unavailable but the "
           "free-sources option still works."),

    ("h", "3.  The main window"),
    ("p", "From top to bottom the window contains:"),
    ("li", "The toolbar, with the User Manual button (you can also press F1)."),
    ("li", "Settings: the output folder and where to look for replacements."),
    ("li", "Option A (left): check files you already have."),
    ("li", "Option B (right): search for a song or video by name."),
    ("li", "Progress: a colour-coded log of everything the program does."),

    ("h", "4.  Settings"),
    ("q", "Folder for the saved files"),
    ("p", "This is where every repaired or downloaded file is stored. The default is the folder "
          "Media Replacements inside your Downloads folder."),
    ("li", "Browse... lets you pick a different folder."),
    ("li", "Open Folder opens the current output folder in Windows Explorer so you can see your "
           "files. The folder is created if it does not exist yet."),
    ("q", "Where to look for a replacement"),
    ("li", "Free and open sources only: the Internet Archive, limited to items with a Creative "
           "Commons or public-domain license."),
    ("li", "Free sources and YouTube: free versions are listed first, then YouTube versions."),
    ("li", "YouTube only: anything that can be found on YouTube."),
    ("q", "Option A: let me choose from the results"),
    ("p", "Ticked: a list of versions is shown and you pick one. Not ticked: the first match is "
          "downloaded automatically."),
    ("q", "Open each saved file in my web browser"),
    ("p", "When ticked, every saved file opens in your default web browser so you can play it "
          "straight away."),

    ("h", "5.  Option A - check existing files"),
    ("n", "1.  Click Select files... and choose one or more MP3 / MP4 files. Hold Ctrl or Shift to "
          "choose several. The chosen files appear in the list."),
    ("n", "2.  Leave \"Try to repair damaged files first\" ticked if you want a repair attempted "
          "before searching online."),
    ("n", "3.  Click \"Check files and fix damaged ones\"."),
    ("p", "For every file the program then does the following:"),
    ("li", "The file is fully decoded with FFmpeg. If there is no error it is reported as HEALTHY "
           "and left alone."),
    ("li", "A file that gives errors, is empty or is smaller than 1 KB is reported as DAMAGED."),
    ("li", "If repair is ticked, the program tries to rebuild the file. MP3 files are re-encoded; "
           "MP4 files are first re-wrapped and, if that fails, re-encoded. A repaired copy is "
           "saved in the output folder."),
    ("li", "If the file cannot be repaired, the program searches for a replacement using the "
           "artist and title stored in the file, or the file name if there are no tags. The "
           "download is tested to make sure it really plays before it is kept."),
    ("li", "If a file can be neither repaired nor replaced, its name (without the folder and the "
           "extension) is copied into the Option B box as plain readable text (underscores "
           "become spaces), and MP3 / MP4 is set to match, so you can search for it there "
           "with one click. If several files fail, the first one is "
           "copied and the others are listed in the Progress area."),
    ("li", "Files that are not MP3 or MP4 are skipped."),
    ("p", "When all files are done, a summary line shows how many were healthy, repaired, "
          "downloaded or failed."),

    ("h", "6.  Option B - search by song name"),
    ("n", "1.  Type or paste the song or video name, for example: Artist - Title. Paste with "
          "Ctrl+V (it works with any keyboard language) or right-click the box and choose "
          "Paste."),
    ("n", "2.  Choose MP3 (audio) or MP4 (video)."),
    ("n", "3.  Click \"Search the web\" (or press Enter)."),
    ("n", "4.  Pick a version in the list that appears and download it. The file is downloaded "
          "and tested, then held ready. The line under the buttons shows its name."),
    ("n", "5.  Click \"Save to output folder\" to store it in your output folder. A message "
          "confirms where it was saved, and it opens in your browser if that option is ticked."),
    ("note", "Until you click Save, nothing is added to the output folder. If you start a new "
             "search or close the program while a file is waiting, you are asked first."),
    ("p", "The Save button is grey until a download is ready and the program has finished "
          "working. Option B always shows the list of versions so you can choose exactly the "
          "one you want."),

    ("h", "7.  Choosing a version"),
    ("p", "The list shows the song name, singer or channel, length, source and license of every "
          "version found. Green rows come from free and open sources; orange rows come from "
          "YouTube."),
    ("li", "Select a row and click \"Download selected\" (or double-click the row)."),
    ("li", "\"Open page in browser\" opens the page of the selected version so you can preview it "
           "first."),
    ("li", "Cancel closes the list without downloading anything."),

    ("h", "8.  While the program is working"),
    ("p", "A \"Please be patient\" window appears with a moving bar and a line telling you what "
          "is happening right now. Checking, repairing and downloading can take a few minutes "
          "depending on the file size and your internet speed."),
    ("li", "Do not close the program while this window is showing."),
    ("li", "The two action buttons are greyed out until the work is finished."),
    ("li", "The window closes by itself when the work is done. It hides briefly whenever the list "
           "of versions is shown."),

    ("h", "9.  Where your files are saved"),
    ("li", "Every file is saved as a plain name.mp3 or name.mp4. Nothing else is left in the "
           "output folder."),
    ("li", "If a file with that name already exists, a number is added: name (1).mp3."),
    ("li", "Original files are never modified or deleted."),
    ("li", "Option A saves automatically. Option B saves when you click \"Save to output "
           "folder\"."),
    ("li", "In Option B the file name is your search text, made safe for Windows."),
    ("li", "Use the Open Folder button to see the results."),

    ("h", "10.  Reading the Progress log"),
    ("li", "Green: good news (healthy, repaired, downloaded, done)."),
    ("li", "Red: problems (damaged file, failed search or download, setup problems)."),
    ("li", "Amber: warnings and tips (repair did not work, cancelled, skipped)."),
    ("li", "Cyan: file headings and the final summary."),
    ("li", "White: ordinary progress messages."),

    ("h", "11.  Troubleshooting"),
    ("q", "\"FFmpeg / ffprobe not found\""),
    ("p", "Download FFmpeg from ffmpeg.org, copy ffmpeg.exe and ffprobe.exe into the same folder "
          "as this program, and start the program again."),
    ("q", "\"yt-dlp is not installed\""),
    ("p", "The YouTube option cannot be used. Choose \"Free and open sources only\" instead."),
    ("q", "\"No close match found\""),
    ("p", "Free sources only contain Creative Commons and public-domain items, so most "
          "commercial songs will not be found there. Choose \"Free sources and YouTube\", or use "
          "Option B with a clearer name such as Artist - Title."),
    ("q", "\"Download failed\" or \"Downloaded file failed the check\""),
    ("p", "The connection may have dropped or the site may have changed. Try again or choose "
          "another version. YouTube changes often, so an updated version of the program may "
          "be needed."),
    ("q", "\"Repair did not work\""),
    ("p", "The file is too badly damaged. The program carries on and looks for a replacement."),
    ("q", "The Save button is grey"),
    ("p", "It turns on only after Option B has downloaded a version successfully and the "
          "program has finished working. Search, choose a version and wait for the "
          "\"Ready to save\" line."),
    ("q", "The program looks frozen"),
    ("p", "If the \"Please be patient\" window is showing, it is still working. Large videos take "
          "longer."),

    ("h", "12.  Legal notice"),
    ("warn", "Only download material that you have the right to copy, and respect the terms of "
             "the sites you download from."),

    ("h", "13.  About"),
    ("p", "Media Check and Replace  -  Aboul Dahab."),
]


class ManualWindow(tk.Toplevel):
    """The user manual: topic list on the left, formatted text on the right."""

    def __init__(self, app):
        super().__init__(app.root)
        self.title("User Manual - Media Check and Replace")
        self.configure(bg=C["bg"])
        root = app.root
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1000, sw - 20), min(680, sh - 90)
        x = max(min(root.winfo_x() + 20, sw - w - 10), 0)
        y = max(min(root.winfo_y() + 10, sh - h - 60), 0)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(640, 400)

        Header(self, "User Manual", "Everything you need to know about Media Check and Replace",
               "#4f46e5", LOGO_BG, app.logo_img).pack(fill="x")

        bottom = tk.Frame(self, bg=C["bg"], pady=8)
        bottom.pack(side="bottom", fill="x", padx=10)
        FancyButton(bottom, "Close", self.destroy, "#dc2626").pack(side="right")
        tk.Label(bottom, text="Click a topic on the left to jump to it.", bg=C["bg"],
                 fg=C["muted"], font=("Segoe UI", 9)).pack(side="left")

        main = tk.Frame(self, bg=C["bg"])
        main.pack(fill="both", expand=True, padx=10, pady=(8, 0))

        nav = tk.Frame(main, bg="#1e1b4b", width=275)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        tk.Label(nav, text="TOPICS", bg="#1e1b4b", fg="#a5b4fc",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        self.nav = tk.Listbox(nav, bg="#1e1b4b", fg="#e0e7ff", font=FONT, relief="flat",
                              highlightthickness=0, borderwidth=0, activestyle="none",
                              selectbackground=C["orange"], selectforeground="white",
                              exportselection=False, cursor="hand2")
        self.nav.pack(fill="both", expand=True, padx=(8, 4), pady=(0, 10))

        box = tk.Frame(main, bg="white")
        box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical")
        sb.pack(side="right", fill="y")
        self.text = tk.Text(box, wrap="word", bg="white", fg=C["ink"], font=FONT,
                            relief="flat", padx=26, pady=14, cursor="arrow",
                            yscrollcommand=sb.set, highlightthickness=0)
        self.text.pack(side="left", fill="both", expand=True)
        sb.config(command=self.text.yview)

        t = self.text
        t.tag_configure("h", font=("Segoe UI", 14, "bold"), foreground=C["indigo"],
                        spacing1=18, spacing3=6)
        t.tag_configure("p", spacing3=6)
        t.tag_configure("li", lmargin1=14, lmargin2=32, spacing3=3)
        t.tag_configure("n", lmargin1=14, lmargin2=36, spacing3=4)
        t.tag_configure("q", font=("Segoe UI", 10, "bold"), foreground="#be185d",
                        spacing1=8, spacing3=2)
        for name, bg, fg in (("tip", "#dcfce7", "#166534"), ("note", "#fef3c7", "#92400e"),
                             ("warn", "#fee2e2", "#991b1b")):
            t.tag_configure(name, background=bg, foreground=fg, lmargin1=10, lmargin2=10,
                            rmargin=10, spacing1=6, spacing3=8, font=FONT_B)

        sec = 0
        for style, txt in MANUAL:
            if style == "h":
                t.mark_set(f"sec{sec}", "end-1c")
                t.mark_gravity(f"sec{sec}", "left")
                self.nav.insert("end", "  " + txt)
                sec += 1
            prefix = "\u2022  " if style == "li" else ""
            t.insert("end", prefix + txt + "\n", style)
        t.config(state="disabled")
        t.bind("<Button-1>", lambda e: t.focus_set())

        self.nav.bind("<<ListboxSelect>>", self.jump)
        self.nav.selection_set(0)

    def jump(self, _event=None):
        sel = self.nav.curselection()
        if sel:
            self.text.yview(f"sec{sel[0]}")


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Media Check and Replace")
        root.configure(bg=C["bg"])
        self.files = []
        self.out_var = tk.StringVar(value=str(Path.home() / "Downloads" / "Media Replacements"))
        self.repair_var = tk.BooleanVar(value=True)
        self.confirm_var = tk.BooleanVar(value=True)
        self.source_var = tk.StringVar(value="free")
        self.open_var = tk.BooleanVar(value=True)
        self.open_after = True
        self.song_var = tk.StringVar()
        self.song_kind = tk.StringVar(value="mp3")
        self.manual_win = None
        self.pending = None        # Option B download waiting for the Save button
        self.buttons_on = True
        self.busy_win = None
        self.busy_bar = None
        self.busy_status = None
        self.setup_styles()

        try:
            self.logo_img = tk.PhotoImage(data="".join(LOGO_B64))
        except tk.TclError:
            self.logo_img = None
        Header(root, "Media Check, Replace & Search",
               "Test your MP3 / MP4 files, repair them, or find a replacement online",
               "#4f46e5", LOGO_BG, self.logo_img).pack(fill="x")

        bar = tk.Frame(root, bg="#1e1b4b")
        bar.pack(fill="x")
        FancyButton(bar, "User Manual", self.show_manual, C["green"]).pack(
            side="left", padx=(12, 8), pady=5
        )
        tk.Label(bar, text="Press F1 at any time to open the manual", bg="#1e1b4b",
                 fg="#a5b4fc", font=("Segoe UI", 9)).pack(side="left")
        root.bind("<F1>", lambda e: self.show_manual())

        body = tk.Frame(root, bg=C["bg"], padx=12, pady=6)
        body.pack(fill="both", expand=True)

        # ---- Shared settings
        card, s = make_card(body, "Settings", C["teal"])
        card.pack(fill="x")
        tk.Label(s, text="Folder for the saved files:", font=FONT_B,
                 bg=C["card"], fg=C["ink"]).pack(anchor="w")
        row = tk.Frame(s, bg=C["card"])
        row.pack(fill="x", pady=(2, 6))
        FancyButton(row, "Open Folder", self.open_output_folder, "#7c3aed").pack(side="right", padx=(8, 0))
        FancyButton(row, "Browse...", self.pick_folder, C["teal"]).pack(side="right", padx=(8, 0))
        make_entry(row, self.out_var).pack(side="left", fill="x", expand=True, ipady=4)

        tk.Label(s, text="Where to look for a replacement:", font=FONT_B,
                 bg=C["card"], fg=C["ink"]).pack(anchor="w")
        for value, label, colour in [
            ("free", "Free and open sources only (Internet Archive, Creative Commons / public domain)", "#15803d"),
            ("both", "Free sources and YouTube (free versions are listed first)", "#6d28d9"),
            ("yt", "YouTube only", "#b91c1c"),
        ]:
            make_radio(s, label, self.source_var, value, colour).pack(anchor="w")
        checks = tk.Frame(s, bg=C["card"])
        checks.pack(anchor="w", pady=(4, 0))
        make_check(checks, "Option A: let me choose from the results (otherwise the first match is used)",
                   self.confirm_var).pack(side="left")
        make_check(checks, "Open each saved file in my web browser",
                   self.open_var).pack(side="left", padx=(16, 0))

        # ---- Options A and B side by side (saves vertical space)
        cols = tk.Frame(body, bg=C["bg"])
        cols.pack(fill="x", pady=6)
        cols.columnconfigure(0, weight=1, uniform="c")
        cols.columnconfigure(1, weight=1, uniform="c")

        ca, a = make_card(cols, "Option A:  check existing MP3 / MP4 files", C["indigo"])
        ca.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        FancyButton(a, "Select files...", self.pick_files, C["indigo"]).pack(anchor="w")
        self.listbox = tk.Listbox(a, height=3, font=FONT, bg="#f8fafc", fg=C["ink"],
                                  relief="flat", highlightthickness=1,
                                  highlightbackground="#cbd5e1", selectbackground=C["indigo"])
        self.listbox.pack(fill="x", pady=6)
        make_check(a, "Try to repair damaged files first", self.repair_var).pack(anchor="w")
        self.run_btn = FancyButton(a, "Check files and fix damaged ones",
                                   self.start_files, C["green"], big=True)
        self.run_btn.pack(anchor="w", pady=(6, 0))

        cb, b = make_card(cols, "Option B:  search by song name", C["pink"])
        cb.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(b, text="Song or video name (for example: Artist - Title):",
                 bg=C["card"], fg=C["ink"], font=FONT).pack(anchor="w")
        self.song_entry = make_entry(b, self.song_var)
        self.song_entry.pack(fill="x", pady=6, ipady=4)
        self.song_entry.bind("<Return>", lambda e: self.start_song())
        r2 = tk.Frame(b, bg=C["card"])
        r2.pack(anchor="w")
        tk.Label(r2, text="Save as:", bg=C["card"], fg=C["ink"], font=FONT_B).pack(side="left")
        make_radio(r2, "MP3 (audio)", self.song_kind, "mp3", "#be185d").pack(side="left")
        make_radio(r2, "MP4 (video)", self.song_kind, "mp4", "#c2410c").pack(side="left")
        brow = tk.Frame(b, bg=C["card"])
        brow.pack(anchor="w", pady=(6, 0))
        self.song_btn = FancyButton(brow, "Search the web", self.start_song,
                                    C["orange"], big=True)
        self.song_btn.pack(side="left")
        self.save_btn = FancyButton(brow, "Save to output folder", self.save_pending,
                                    C["green"], big=True)
        self.save_btn.pack(side="left", padx=(8, 0))
        self.pending_lbl = tk.Label(b, text="", bg=C["card"], anchor="w", justify="left",
                                    wraplength=400, font=("Segoe UI", 9, "bold"))
        self.pending_lbl.pack(anchor="w", pady=(6, 0))

        self.buttons = [self.run_btn, self.song_btn]

        # ---- Progress log (dark, colour-coded)
        tk.Label(body, text="Progress", font=FONT_B, bg=C["bg"],
                 fg=C["indigo"]).pack(anchor="w")
        self.logbox = scrolledtext.ScrolledText(
            body, state="disabled", height=8, wrap="word", relief="flat",
            bg=C["log_bg"], fg=C["log_fg"], insertbackground=C["log_fg"],
            font=("Consolas", 11), padx=8, pady=6)
        self.logbox.pack(fill="both", expand=True)
        self.logbox.tag_configure("head", foreground="#67e8f9", font=("Consolas", 9, "bold"))
        self.logbox.tag_configure("ok", foreground="#4ade80")
        self.logbox.tag_configure("bad", foreground="#f87171")
        self.logbox.tag_configure("warn", foreground="#fbbf24")

        missing = []
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            missing.append("FFmpeg / ffprobe not found in PATH (https://ffmpeg.org)")
        if missing:
            self.log("SETUP PROBLEMS:\n  - " + "\n  - ".join(missing))
            self.set_buttons("disabled")
        if yt_dlp is None:
            self.log(
                "Note: yt-dlp is not installed for THIS Python:\n"
                f"      {sys.executable}\n"
                "    The YouTube option will not work until you run:\n"
                f'      "{sys.executable}" -m pip install -U yt-dlp\n'
                "    The free-sources option works without it."
            )

        self.refresh_save()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.fit_to_screen()

    def setup_styles(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background="white", fieldbackground="white",
                     foreground=C["ink"], rowheight=26, font=FONT, borderwidth=0)
        st.configure("Treeview.Heading", background=C["indigo"], foreground="white",
                     font=FONT_B, relief="flat", padding=6)
        st.map("Treeview.Heading", background=[("active", C["teal"])])
        st.configure("Busy.Horizontal.TProgressbar", troughcolor="#e0e7ff",
                     background=C["orange"], bordercolor="#e0e7ff",
                     lightcolor=C["orange"], darkcolor=C["orange"])
        st.map("Treeview", background=[("selected", C["indigo"])],
               foreground=[("selected", "white")])

    def fit_to_screen(self):
        """Size the window to its content, never taller than the screen, and place
        it at the top centre so the bottom is never pushed off-screen."""
        r = self.root
        r.update_idletasks()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w = min(max(r.winfo_reqwidth(), 900), sw - 20)
        h = min(max(r.winfo_reqheight(), 600), sh - 90)   # room for the taskbar
        x = max((sw - w) // 2, 0)
        r.geometry(f"{w}x{h}+{x}+8")
        r.minsize(min(w, 760), min(h, 520))

    def fill_option_b(self, name, kind):
        """Put a file name (no folder, no extension) into the Option B box,
        as plain readable text (underscores become spaces)."""
        self.song_var.set(readable_name(name))
        if kind in ("mp3", "mp4"):
            self.song_kind.set(kind)
        self.song_entry.icursor("end")

    # ---- Option B: hold the download, save on request ----
    def refresh_save(self):
        """Enable the Save button only when a finished download is waiting."""
        ready = bool(self.pending) and self.buttons_on
        self.save_btn.set_enabled(ready)
        if self.pending:
            self.pending_lbl.config(
                text=f"Ready to save:  {Path(self.pending['path']).name}", fg="#15803d")
        else:
            self.pending_lbl.config(text="No downloaded file is waiting to be saved.",
                                    fg=C["muted"])

    def discard_pending(self):
        if self.pending:
            shutil.rmtree(self.pending["tmp"], ignore_errors=True)
        self.pending = None
        self.refresh_save()

    def save_pending(self):
        p = self.pending
        if not p:
            return
        held = Path(p["path"])
        if not held.exists():
            messagebox.showerror("Save failed", "The downloaded file is no longer available.\n"
                                 "Please search and download it again.")
            self.discard_pending()
            return
        out_dir = self.get_out_dir()
        if not out_dir:
            return
        self.open_after = self.open_var.get()
        final = unique_path(out_dir, p["stem"], f".{p['kind']}")
        try:
            shutil.move(str(held), str(final))
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        self.discard_pending()            # removes the temporary folder and resets the button
        self.log(f"\nSAVED -> {final}")
        self.open_result(final)
        messagebox.showinfo("Saved", f"The file was saved in the output folder:\n\n{final}")

    def on_close(self):
        if self.pending and not messagebox.askyesno(
                "Unsaved file",
                "A downloaded file has not been saved yet.\nQuit without saving it?"):
            return
        self.discard_pending()
        self.root.destroy()

    # ---- user manual ----
    def show_manual(self):
        if self.manual_win is not None and self.manual_win.winfo_exists():
            self.manual_win.deiconify()
            self.manual_win.lift()
            self.manual_win.focus_force()
            return
        self.manual_win = ManualWindow(self)

    # ---- "please be patient" dialog ----
    def show_busy(self):
        """Small non-blocking window shown while a job is running."""
        if self.busy_win is not None:
            return
        win = tk.Toplevel(self.root)
        win.title("Please wait")
        win.configure(bg=C["card"])
        win.transient(self.root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", lambda: None)   # cannot be closed by hand
        tk.Label(win, text="Please be patient...", bg=C["orange"], fg="white",
                 font=("Segoe UI", 15, "bold"), padx=24, pady=12).pack(fill="x")
        tk.Label(win, text=("Your file is being checked, repaired or downloaded.\n"
                            "This can take a few minutes, depending on the file size\n"
                            "and your internet speed. Please do not close the program."),
                 bg=C["card"], fg=C["ink"], font=FONT, justify="center",
                 padx=24, pady=14).pack()
        self.busy_status = tk.Label(win, text="Starting...", bg=C["card"], fg=C["muted"],
                                    font=("Segoe UI", 9, "italic"), wraplength=420)
        self.busy_status.pack(padx=24)
        self.busy_bar = ttk.Progressbar(win, mode="indeterminate", length=400,
                                        style="Busy.Horizontal.TProgressbar")
        self.busy_bar.pack(padx=24, pady=(8, 20))
        self.busy_bar.start(12)
        self.busy_win = win
        self._center_on_root(win)
        win.lift()

    def hide_busy(self):
        if self.busy_win is None:
            return
        try:
            self.busy_bar.stop()
            self.busy_win.destroy()
        except tk.TclError:
            pass
        self.busy_win = self.busy_bar = self.busy_status = None

    def _busy_pause(self):
        """Hide the dialog while the version picker is open."""
        if self.busy_win is not None:
            self.busy_win.withdraw()

    def _busy_resume(self):
        if self.busy_win is not None:
            self.busy_win.deiconify()
            self._center_on_root(self.busy_win)
            self.busy_win.lift()

    def _center_on_root(self, win):
        win.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def job_finished(self):
        self.set_buttons("normal")
        self.hide_busy()

    # ---- open the output folder in the file explorer ----
    def open_output_folder(self):
        folder = Path(self.out_var.get())
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Folder error", str(e))
            return
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as e:
            messagebox.showerror("Cannot open folder", str(e))

    # ---- UI helpers ----
    @staticmethod
    def _log_tag(text):
        t = text.strip()
        up = t.upper()
        if t.startswith("=") or t.startswith("Healthy:"):
            return "head"
        if up.startswith(("HEALTHY", "REPAIRED", "DOWNLOADED", "DONE", "FOUND", "OPENED", "READY", "SAVED")):
            return "ok"
        if up.startswith(("DAMAGED", "DOWNLOAD FAILED", "SEARCH FAILED", "SETUP PROBLEMS",
                          "DOWNLOADED FILE FAILED", "NO CLOSE MATCH", "NO FILE WAS SAVED", "NO FILE WAS DOWNLOADED",
                          "COULD NOT")):
            return "bad"
        if up.startswith(("REPAIR DID NOT", "CANCELLED", "SKIPPED", "NOTE:", "TIP:",
                          "YOUTUBE SEARCH SKIPPED", "NO MATCHES", "USE OPTION B",
                          "OTHER FILES THAT COULD NOT")):
            return "warn"
        return ""

    def log(self, text):
        tag = self._log_tag(text)

        def _do():
            line = next((l.strip() for l in reversed(text.splitlines()) if l.strip()), "")
            if self.busy_status is not None and line and not line.startswith("="):
                self.busy_status.config(text=line[:90])
            self.logbox.config(state="normal")
            self.logbox.insert("end", text + "\n", tag)
            self.logbox.see("end")
            self.logbox.config(state="disabled")
        self.root.after(0, _do)

    def set_buttons(self, state):
        self.buttons_on = (state == "normal")
        for b in self.buttons:
            b.set_enabled(self.buttons_on)
        self.refresh_save()

    def ask(self, title, message):
        """Ask yes/no from the worker thread safely."""
        result, done = {}, threading.Event()

        def _do():
            result["v"] = messagebox.askyesno(title, message)
            done.set()
        self.root.after(0, _do)
        done.wait()
        return result["v"]

    def pick_files(self):
        picked = filedialog.askopenfilenames(
            title="Select MP3 / MP4 files",
            filetypes=[("MP3 / MP4 files", "*.mp3 *.mp4"), ("MP3", "*.mp3"), ("MP4", "*.mp4")])
        if picked:
            self.files = list(picked)
            self.listbox.delete(0, "end")
            for f in self.files:
                self.listbox.insert("end", f)

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.out_var.set(d)

    def get_out_dir(self):
        out_dir = Path(self.out_var.get())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Folder error", str(e))
            return None
        return out_dir

    # ---- start buttons ----
    def start_files(self):
        if not self.files:
            messagebox.showinfo("No files", "Select at least one MP3 or MP4 file first.")
            return
        out_dir = self.get_out_dir()
        if not out_dir:
            return
        self.open_after = self.open_var.get()
        self.set_buttons("disabled")
        self.show_busy()
        threading.Thread(
            target=self.process_files,
            args=(list(self.files), out_dir, self.repair_var.get(),
                  self.confirm_var.get(), self.source_var.get()),
            daemon=True,
        ).start()

    def start_song(self):
        name = self.song_var.get().strip()
        if not name:
            messagebox.showinfo("No name", "Type a song or video name first.")
            return
        if self.pending:
            if not messagebox.askyesno(
                    "Unsaved file",
                    "A downloaded file has not been saved yet.\n"
                    "Search again and discard it?"):
                return
            self.discard_pending()
        out_dir = self.get_out_dir()
        if not out_dir:
            return
        self.open_after = self.open_var.get()
        self.set_buttons("disabled")
        self.show_busy()
        threading.Thread(
            target=self.process_song,
            args=(name, self.song_kind.get(), out_dir,
                  self.confirm_var.get(), self.source_var.get()),
            daemon=True,
        ).start()

    def open_result(self, path):
        """Open a saved file in the default web browser."""
        if not self.open_after:
            return
        try:
            webbrowser.open(Path(path).resolve().as_uri())
            self.log("  Opened in your web browser")
        except Exception as e:
            self.log(f"  Could not open the browser: {e}")

    # ---- version picker window ----
    def choose_result(self, results, kind, header):
        """Show every version found; return the one the user selects (or None)."""
        holder, done = {"v": None}, threading.Event()

        def _build():
            self._busy_pause()
            win = tk.Toplevel(self.root)
            win.title("Choose a version to download")
            win.configure(bg=C["bg"])
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            ww, wh = min(960, sw - 20), min(470, sh - 110)
            wx = max(min(self.root.winfo_x() + 30, sw - ww - 10), 0)
            wy = max(min(self.root.winfo_y() + 40, sh - wh - 70), 0)
            win.geometry(f"{ww}x{wh}+{wx}+{wy}")
            win.transient(self.root)

            tk.Label(win, text=header, justify="left", anchor="w", padx=12, pady=8,
                     bg=C["indigo"], fg="white", font=FONT_B).pack(fill="x")
            tk.Label(win, text="Green rows = free / open sources      Orange rows = YouTube",
                     anchor="w", padx=12, pady=3, bg=C["bg"], fg=C["muted"],
                     font=("Segoe UI", 9)).pack(fill="x")
            frame = tk.Frame(win, bg=C["bg"])
            frame.pack(fill="both", expand=True, padx=10)

            cols = ("song", "singer", "length", "source", "license")
            heads = {"song": ("Song name", 320), "singer": ("Singer / channel", 200),
                     "length": ("Length", 70), "source": ("Source", 130),
                     "license": ("License", 150)}
            tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
            for c in cols:
                tree.heading(c, text=heads[c][0])
                tree.column(c, width=heads[c][1], anchor="w")
            sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            tree.tag_configure("ia", background="#dcfce7")
            tree.tag_configure("yt", background="#ffedd5")
            for i, r in enumerate(results):
                src = "Internet Archive (free)" if r["source"] == "ia" else "YouTube"
                tree.insert("", "end", iid=str(i), values=(
                    r.get("song", r["title"]), r.get("singer", r["channel"]),
                    fmt_duration(r["duration"]).replace("unknown length", "-"),
                    src, short_license(r.get("license"))),
                    tags=("ia" if r["source"] == "ia" else "yt",))
            if results:
                tree.selection_set("0")
                tree.focus("0")

            def finish(choice):
                holder["v"] = choice
                done.set()
                win.destroy()
                self._busy_resume()

            def selected():
                sel = tree.selection()
                return results[int(sel[0])] if sel else None

            def on_download(event=None):
                r = selected()
                if r:
                    finish(r)

            def on_open():
                r = selected()
                if r:
                    webbrowser.open(r["url"])

            bar = tk.Frame(win, bg=C["bg"], pady=8)
            bar.pack(fill="x", padx=10)
            FancyButton(bar, f"Download selected as {kind.upper()}", on_download,
                        C["green"], big=True).pack(side="left")
            FancyButton(bar, "Open page in browser", on_open, C["indigo"]).pack(side="left", padx=8)
            FancyButton(bar, "Cancel", lambda: finish(None), "#dc2626").pack(side="right")

            tree.bind("<Double-1>", on_download)
            win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
            win.focus_force()

        self.root.after(0, _build)
        done.wait()
        return holder["v"]

    # ---- shared search + download ----
    def find_and_download(self, query, kind, stem, out_dir, choose, source, original="",
                          always_choose=False, stage=False):
        """Search, list every version for selection, download the pick.
        Saves out_dir/stem.kind. Returns True on success.
        With stage=True the tested file is only held ready (self.pending); the
        Save button then moves it to the output folder."""
        order = {"free": ["ia"], "both": ["ia", "yt"], "yt": ["yt"]}[source]
        results = []
        for src in order:
            if src == "yt" and yt_dlp is None:
                self.log("  YouTube search skipped: yt-dlp is not installed")
                continue
            label = "free / open sources" if src == "ia" else "YouTube"
            self.log(f"  Searching {label} for: {query}")
            try:
                found = search_archive(query, kind) if src == "ia" else search_web(query)
            except Exception as e:
                self.log(f"  Search failed: {e}")
                continue
            if not found:
                self.log("  No matches found there")
                continue
            self.log(f"  Found {len(found)} version(s)")
            results.extend(found)
        if not results:
            self.log("  No close match found.")
            self.log("  Tip: switch the source to include YouTube, or edit the name "
                     "and search again with Option B.")
            return False

        if choose or always_choose:
            head = (f"Original file: {original}\n" if original else "")
            head += (f"Search: {query}\n{len(results)} version(s) found. Select one and "
                     f"click Download.")
            chosen = self.choose_result(results, kind, head)
            if not chosen:
                self.log("  Cancelled, nothing downloaded")
                return False
        else:
            chosen = results[0]

        self.log(f"  Downloading: {chosen.get('song', chosen['title'])} - "
                 f"{chosen.get('singer', chosen['channel'])}")
        tmp = tempfile.mkdtemp(prefix="mcr_")
        keep = False
        try:
            try:
                if chosen["source"] == "ia":
                    got = download_archive(chosen["id"], kind, tmp)
                else:
                    got = download_youtube(chosen["url"], kind, tmp)
            except Exception as e:
                self.log(f"  Download failed: {e}")
                return False
            ok, why = is_playable(got, kind) if got.exists() else (False, "no file was produced")
            if not ok:
                self.log(f"  Downloaded file failed the check: {why}")
                return False
            if stage:
                held = Path(tmp) / f"{stem}.{kind}"
                shutil.move(str(got), str(held))
                self.pending = {"path": held, "tmp": tmp, "stem": stem, "kind": kind}
                keep = True
                self.log(f"  READY -> {held.name} was downloaded and tested.")
                self.log('  Click "Save to output folder" to keep it.')
                return True
            final = unique_path(out_dir, stem, f".{kind}")
            shutil.move(str(got), str(final))
        finally:
            if not keep:
                shutil.rmtree(tmp, ignore_errors=True)
        self.log(f"  DOWNLOADED -> {final}")
        self.open_result(final)
        return True

    # ---- Option A worker ----
    def process_files(self, files, out_dir, do_repair, confirm, source):
        stats = {"healthy": 0, "repaired": 0, "downloaded": 0, "failed": 0}
        failed_files = []          # (file name without extension, "mp3"/"mp4")
        try:
            for f in files:
                p = Path(f)
                kind = p.suffix.lower().lstrip(".")
                if kind not in ("mp3", "mp4"):
                    self.log(f"\n=== {p.name}\n  Skipped: not an MP3 or MP4 file")
                    continue
                self.log(f"\n=== {p.name}")
                self.log("  Testing...")
                damaged, why = is_damaged(p)
                if not damaged:
                    self.log("  HEALTHY, nothing to do")
                    stats["healthy"] += 1
                    continue
                self.log(f"  DAMAGED: {why}")

                if do_repair:
                    self.log("  Trying to repair...")
                    with tempfile.TemporaryDirectory(prefix="mcr_") as tmp:
                        fixed = try_repair(p, tmp)
                        if fixed:
                            final = unique_path(out_dir, p.stem, f".{kind}")
                            shutil.move(str(fixed), str(final))
                            self.log(f"  REPAIRED -> {final}")
                            self.open_result(final)
                            stats["repaired"] += 1
                            continue
                    self.log("  Repair did not work")

                ok = self.find_and_download(guess_query(p), kind, p.stem, out_dir,
                                            confirm, source, original=p.name)
                stats["downloaded" if ok else "failed"] += 1
                if not ok:
                    failed_files.append((p.stem, kind))
        finally:
            self.log("\n" + "=" * 44)
            self.log(f" Healthy: {stats['healthy']}   Repaired: {stats['repaired']}   "
                     f"Downloaded: {stats['downloaded']}   Failed: {stats['failed']}")
            self.log(f" Output folder: {out_dir}")
            self.log("=" * 44)
            if failed_files:
                name, kind = failed_files[0]
                self.log(f'\nUse Option B to search again: "{readable_name(name)}" was '
                         f"copied into the Option B box.")
                if len(failed_files) > 1:
                    self.log("Other files that could not be replaced: "
                             + ", ".join(readable_name(n) for n, _ in failed_files[1:]))
                self.root.after(0, lambda: self.fill_option_b(name, kind))
            self.root.after(0, self.job_finished)

    # ---- Option B worker ----
    def process_song(self, name, kind, out_dir, confirm, source):
        try:
            self.log(f"\n=== Search by name: {name}  ({kind.upper()})")
            ok = self.find_and_download(name, kind, sanitize_filename(name), out_dir,
                                        confirm, source, always_choose=True, stage=True)
            if ok:
                self.log('\nDone. Click "Save to output folder" to store the file in:\n'
                         f"  {out_dir}")
            else:
                self.log("\nNo file was downloaded.")
        finally:
            self.root.after(0, self.job_finished)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()