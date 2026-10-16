#!/usr/bin/env python3
"""
Studio Audio Editor - a multitrack audio editor in the spirit of Audacity.

Install once:      pip install numpy scipy soundfile sounddevice
Run:               python studio_audio_editor.py [song.wav ...]

Features
  * Multitrack timeline: per-track mute / solo / gain / pan / time-shift
  * Import wav, flac, ogg, mp3 (and m4a, mp4, wma ... when ffmpeg is installed)
  * Export mixdown or selection: wav, flac, ogg, mp3 (+ more through ffmpeg)
  * Waveform view with zoom, scroll, ruler, mouse selection
  * Play / loop / play selection, record from microphone
  * Cut, copy, paste, delete, silence, trim, undo / redo (30 steps)
  * Effects: amplify, normalize, fade in/out, reverse, invert, remove DC,
    change speed, low-pass, high-pass, echo, reverb, compressor
  * Generate: silence, tone (sine/square/saw/triangle), white noise
  * Spectrogram window
  * Save / open projects (.npz)
"""
import os
import sys
import json
import math
import shutil
import tempfile
import subprocess
from fractions import Fraction

import numpy as np
from scipy import signal
import soundfile as sf
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import sounddevice as sd
except Exception:          # PortAudio missing -> editing works, playback doesn't
    sd = None

DEFAULT_RATE = 44100
MAX_UNDO = 30
HEADER_W = 170             # track-control panel
HEAD_W = 204               # panel + amplitude scale strip (waveform starts here)
LANE_H = 130               # height of one track
RULER_H = 26
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".studio_audio_editor.json")

THEMES = {
    "Dark": dict(root="#2b2b2b", panel="#383838", text="#e8e8e8", sub="#a8a8a8",
                 ruler_bg="#383838", ruler_fg="#d0d0d0", head="#4b4b4b", head_sel="#5e5e5e",
                 head_edge="#222222", btn="#6a6a6a", mute="#d9a23a", solo="#4caf50",
                 lane="#232323", lane_sel="#2b2e3a", lane_edge="#111111", center="#3c3c3c",
                 sel="#454b66", peak="#5276e0", rms="#a6bdf7", peak_mute="#555a66",
                 rms_mute="#7a7f8a", cursor="#ffffff", play="#4cd964", scale="#9a9a9a",
                 time="#5cdc7b"),
    "Light": dict(root="#dcdcdc", panel="#e4e4e4", text="#202020", sub="#555555",
                  ruler_bg="#e4e4e4", ruler_fg="#333333", head="#cfcfcf", head_sel="#bdbdbd",
                  head_edge="#8f8f8f", btn="#e8e8e8", mute="#e0a030", solo="#4caf50",
                  lane="#f2f2f2", lane_sel="#ffffff", lane_edge="#a0a0a0", center="#cfcfcf",
                  sel="#b9c4e8", peak="#5a7fdc", rms="#2f50b0", peak_mute="#a8acb5",
                  rms_mute="#80848d", cursor="#000000", play="#1e9e3a", scale="#555555",
                  time="#1e7a35"),
}


def load_theme_name():
    try:
        with open(SETTINGS_FILE) as f:
            n = json.load(f).get("theme", "Dark")
        return n if n in THEMES else "Dark"
    except Exception:
        return "Dark"


# =====================================================================
#  AUDIO HELPERS
# =====================================================================
def to_stereo(a):
    """Any array -> float32 (n, 2)."""
    a = np.asarray(a, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    if a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    elif a.shape[1] > 2:
        a = a[:, :2]
    return np.ascontiguousarray(a)


def resample(a, src, dst):
    if int(src) == int(dst) or len(a) == 0:
        return a
    g = math.gcd(int(src), int(dst))
    return signal.resample_poly(a, int(dst) // g, int(src) // g, axis=0).astype(np.float32)


def fmt_time(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


def _ffmpeg():
    return shutil.which("ffmpeg")


def load_audio(path):
    """-> (float32 stereo array, sample rate). Falls back to ffmpeg."""
    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        ff = _ffmpeg()
        if not ff:
            raise RuntimeError("This format needs ffmpeg installed (or use wav/flac/ogg/mp3).")
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            subprocess.run([ff, "-y", "-i", path, "-vn", "-acodec", "pcm_f32le", tmp],
                           check=True, capture_output=True)
            data, sr = sf.read(tmp, dtype="float32", always_2d=True)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return to_stereo(data), int(sr)


def save_audio(path, data, rate):
    ext = os.path.splitext(path)[1].lower()
    data = np.clip(data, -1.0, 1.0)
    fmts = {".wav": ("WAV", "PCM_16"), ".flac": ("FLAC", "PCM_16"),
            ".ogg": ("OGG", "VORBIS"), ".mp3": ("MP3", None)}
    if ext in fmts:
        fmt, sub = fmts[ext]
        try:
            sf.write(path, data, rate, format=fmt, **({"subtype": sub} if sub else {}))
            return
        except Exception:
            pass                              # try ffmpeg below
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError(f"Cannot write {ext or 'this format'} without ffmpeg installed.")
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        sf.write(tmp, data, rate, format="WAV", subtype="PCM_16")
        subprocess.run([ff, "-y", "-i", tmp, path], check=True, capture_output=True)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# =====================================================================
#  EFFECTS  (pure functions: float32 (n,2) in -> float32 (m,2) out)
# =====================================================================
def fx_amplify(x, db):
    return (x * (10 ** (db / 20))).astype(np.float32)


def fx_normalize(x, peak_db=-1.0):
    pk = float(np.max(np.abs(x))) if len(x) else 0.0
    return x if pk < 1e-9 else (x * (10 ** (peak_db / 20) / pk)).astype(np.float32)


def fx_fade(x, fade_in=True):
    if len(x) == 0:
        return x
    ramp = np.linspace(0, 1, len(x), dtype=np.float32)
    return x * (ramp if fade_in else ramp[::-1])[:, None]


def fx_reverse(x):
    return x[::-1].copy()


def fx_invert(x):
    return -x


def fx_dc(x):
    return (x - x.mean(axis=0, keepdims=True)).astype(np.float32)


def fx_speed(x, factor):
    """factor 2.0 = twice as fast (and an octave higher)."""
    fr = Fraction(float(factor)).limit_denominator(1000)
    return signal.resample_poly(x, fr.denominator, fr.numerator, axis=0).astype(np.float32)


def fx_filter(x, rate, kind, freq, order=4):
    if len(x) < 200:
        return x
    nyq = rate / 2.0
    freq = min(max(float(freq), 10.0), nyq * 0.99)
    sos = signal.butter(order, freq / nyq, btype=kind, output="sos")
    return signal.sosfiltfilt(sos, x, axis=0).astype(np.float32)


def fx_echo(x, rate, delay=0.3, decay=0.5, repeats=5):
    d = max(1, int(delay * rate))
    out = np.zeros((len(x) + d * repeats, 2), np.float32)
    out[:len(x)] += x
    for k in range(1, repeats + 1):
        out[k * d:k * d + len(x)] += x * (decay ** k)
    return out


def fx_reverb(x, rate, room=0.5, wet=0.3):
    """Schroeder reverb: 4 parallel combs + 2 allpass filters."""
    tail = int(rate * (0.5 + 2 * room))
    xp = np.concatenate([x, np.zeros((tail, 2), np.float32)]).astype(np.float64)
    fb = 0.6 + 0.35 * room
    acc = np.zeros_like(xp)
    for dl in (0.0297, 0.0371, 0.0411, 0.0437):
        d = int(dl * rate)
        b = np.zeros(d + 1); b[d] = 1.0
        a = np.zeros(d + 1); a[0] = 1.0; a[d] = -fb
        acc += signal.lfilter(b, a, xp, axis=0)
    acc /= 4.0
    for dl, g in ((0.005, 0.7), (0.0017, 0.7)):
        d = max(1, int(dl * rate))
        b = np.zeros(d + 1); b[0] = -g; b[d] = 1.0
        a = np.zeros(d + 1); a[0] = 1.0; a[d] = -g
        acc = signal.lfilter(b, a, acc, axis=0)
    return (xp * (1 - wet) + acc * wet).astype(np.float32)


def fx_compress(x, rate, thresh_db=-18.0, ratio=3.0, makeup_db=0.0):
    if len(x) == 0:
        return x
    env = np.max(np.abs(x), axis=1)
    a = math.exp(-1.0 / (0.01 * rate))
    env = signal.lfilter([1 - a], [1, -a], env)
    lvl = 20 * np.log10(np.maximum(env, 1e-6))
    over = np.maximum(lvl - thresh_db, 0)
    gain_db = -over * (1 - 1 / max(ratio, 1.0)) + makeup_db
    return (x * (10 ** (gain_db / 20))[:, None]).astype(np.float32)


def gen_tone(rate, secs, freq, shape="sine", amp=0.5):
    t = np.arange(int(rate * secs)) / rate
    if shape == "square":
        y = np.sign(np.sin(2 * np.pi * freq * t))
    elif shape == "saw":
        y = 2 * ((freq * t) % 1) - 1
    elif shape == "triangle":
        y = 2 * np.abs(2 * ((freq * t) % 1) - 1) - 1
    else:
        y = np.sin(2 * np.pi * freq * t)
    return to_stereo(y * amp)


def gen_noise(rate, secs, amp=0.3):
    return to_stereo(np.random.uniform(-1, 1, int(rate * secs)) * amp)


def spectrogram_ppm(x, rate, W=900, H=320):
    """Return PPM bytes of a spectrogram image (no PIL needed)."""
    mono = x.mean(axis=1)
    if len(mono) < 2048:
        mono = np.pad(mono, (0, 2048 - len(mono)))
    _, _, S = signal.spectrogram(mono, rate, nperseg=1024, noverlap=768, mode="magnitude")
    db = 20 * np.log10(S + 1e-10)
    v = np.clip((db - (db.max() - 90)) / 90, 0, 1)
    cols = np.linspace(0, v.shape[1] - 1, W).astype(int)
    rows = np.linspace(0, v.shape[0] - 1, H).astype(int)
    img = (v[np.ix_(rows, cols)][::-1] * 255).astype(np.float32)
    r = np.clip(img * 2, 0, 255)
    g = np.clip((img - 100) * 2, 0, 255)
    b = np.where(img < 128, img * 2, 255 - (img - 128) * 2)
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    return b"P6 %d %d 255\n" % (W, H) + rgb.tobytes()


# =====================================================================
#  PROJECT MODEL  (arrays are treated as immutable -> cheap undo)
# =====================================================================
class Track:
    def __init__(self, name="Track", data=None, offset=0):
        self.name = name
        self.data = to_stereo(data) if data is not None else np.zeros((0, 2), np.float32)
        self.offset = int(offset)          # start position on the timeline (samples)
        self.gain, self.pan = 1.0, 0.0
        self.mute = self.solo = False

    @property
    def end(self):
        return self.offset + len(self.data)

    def clone(self):
        t = Track(self.name, None, self.offset)
        t.data, t.gain, t.pan, t.mute, t.solo = self.data, self.gain, self.pan, self.mute, self.solo
        return t

    def local(self, s, e):
        return max(0, int(s) - self.offset), min(len(self.data), int(e) - self.offset)

    def copy_range(self, s, e):
        a, b = self.local(s, e)
        return self.data[a:b].copy() if b > a else np.zeros((0, 2), np.float32)

    def apply(self, s, e, fn):
        a, b = self.local(s, e)
        if b <= a:
            return
        self.data = np.concatenate([self.data[:a], to_stereo(fn(self.data[a:b])), self.data[b:]])

    def silence(self, s, e):
        a, b = self.local(s, e)
        if b > a:
            d = self.data.copy()
            d[a:b] = 0
            self.data = d

    def delete(self, s, e):
        """Remove [s, e) and close the gap (later audio moves left)."""
        a, b = self.local(s, e)
        before = max(0, min(int(e), self.offset) - int(s))
        if b > a:
            self.data = np.concatenate([self.data[:a], self.data[b:]])
        self.offset = max(0, self.offset - before)

    def trim(self, s, e):
        """Keep only [s, e); the kept audio stays at the same time position."""
        a, b = self.local(s, e)
        if b <= a:
            self.data = np.zeros((0, 2), np.float32)
        else:
            self.data = self.data[a:b].copy()
            self.offset += a

    def insert(self, at, clip):
        clip = to_stereo(clip)
        at = int(at)
        if len(self.data) == 0:
            self.offset, self.data = at, clip
        elif at <= self.offset:
            gap = np.zeros((self.offset - at, 2), np.float32)
            self.data = np.concatenate([clip, gap, self.data])
            self.offset = at
        elif at >= self.end:
            gap = np.zeros((at - self.end, 2), np.float32)
            self.data = np.concatenate([self.data, gap, clip])
        else:
            i = at - self.offset
            self.data = np.concatenate([self.data[:i], clip, self.data[i:]])


class Project:
    def __init__(self):
        self.rate = DEFAULT_RATE
        self.tracks = []
        self.clip = []
        self.dirty = False
        self.path = None
        self._undo, self._redo = [], []

    # ---- undo / redo
    def _snap(self):
        return [t.clone() for t in self.tracks]

    def push_undo(self):
        self._undo.append(self._snap())
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self.dirty = True

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self._snap())
        self.tracks = self._undo.pop()
        self.dirty = True
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self._snap())
        self.tracks = self._redo.pop()
        self.dirty = True
        return True

    # ---- tracks
    def new_track(self, name=None, data=None, offset=0):
        t = Track(name or f"Track {len(self.tracks) + 1}", data, offset)
        self.tracks.append(t)
        return t

    def length(self):
        return max((t.end for t in self.tracks), default=0)

    def import_file(self, path, offset=0):
        data, sr = load_audio(path)
        if not self.tracks:
            self.rate = sr
        data = resample(data, sr, self.rate)
        return self.new_track(os.path.splitext(os.path.basename(path))[0], data, offset)

    def mixdown(self, s=0, e=None):
        e = self.length() if e is None else int(e)
        s = int(s)
        out = np.zeros((max(0, e - s), 2), np.float32)
        any_solo = any(t.solo for t in self.tracks)
        for t in self.tracks:
            if t.mute or (any_solo and not t.solo):
                continue
            a, b = max(s, t.offset), min(e, t.end)
            if b <= a:
                continue
            seg = t.data[a - t.offset:b - t.offset]
            lg = t.gain * min(1.0, 1.0 - t.pan)
            rg = t.gain * min(1.0, 1.0 + t.pan)
            out[a - s:b - s, 0] += seg[:, 0] * lg
            out[a - s:b - s, 1] += seg[:, 1] * rg
        return np.clip(out, -1.0, 1.0, out=out)

    # ---- project files
    def save(self, path):
        meta = {"rate": self.rate, "tracks": [
            {"name": t.name, "offset": t.offset, "gain": t.gain, "pan": t.pan,
             "mute": t.mute, "solo": t.solo} for t in self.tracks]}
        arrays = {f"t{i}": t.data for i, t in enumerate(self.tracks)}
        with open(path, "wb") as f:
            np.savez_compressed(f, meta=np.array(json.dumps(meta)), **arrays)
        self.path, self.dirty = path, False

    def load(self, path):
        z = np.load(path, allow_pickle=False)
        meta = json.loads(str(z["meta"]))
        self.rate = int(meta["rate"])
        self.tracks = []
        for i, m in enumerate(meta["tracks"]):
            t = Track(m["name"], z[f"t{i}"], m["offset"])
            t.gain, t.pan, t.mute, t.solo = m["gain"], m["pan"], m["mute"], m["solo"]
            self.tracks.append(t)
        self._undo.clear(); self._redo.clear()
        self.path, self.dirty = path, False


# =====================================================================
#  PLAYBACK / RECORDING
# =====================================================================
class Player:
    def __init__(self):
        self.stream = None
        self.buf = np.zeros((0, 2), np.float32)
        self.pos = 0
        self.loop = False
        self.playing = False

    def start(self, buf, rate, loop=False):
        if sd is None:
            raise RuntimeError("Playback needs the 'sounddevice' package:  pip install sounddevice")
        self.stop()
        self.buf, self.pos, self.loop, self.playing = buf, 0, loop, True
        self.stream = sd.OutputStream(samplerate=rate, channels=2, dtype="float32",
                                      callback=self._cb, finished_callback=self._fin)
        self.stream.start()

    def _cb(self, out, frames, _ti, _status):
        n, filled = len(self.buf), 0
        while filled < frames and n:
            chunk = self.buf[self.pos:self.pos + frames - filled]
            out[filled:filled + len(chunk)] = chunk
            filled += len(chunk)
            self.pos += len(chunk)
            if self.pos >= n:
                if self.loop:
                    self.pos = 0
                else:
                    break
        if filled < frames:
            out[filled:] = 0
            raise sd.CallbackStop

    def _fin(self):
        self.playing = False

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        self.playing = False


class Recorder:
    def __init__(self):
        self.stream = None
        self.chunks = []
        self.frames = 0

    def start(self, rate):
        if sd is None:
            raise RuntimeError("Recording needs the 'sounddevice' package:  pip install sounddevice")
        self.chunks, self.frames = [], 0

        def cb(data, _f, _t, _s):
            self.chunks.append(data.copy())
            self.frames += len(data)
        self.stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", callback=cb)
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        data = np.concatenate(self.chunks) if self.chunks else np.zeros((0, 1), np.float32)
        return to_stereo(data)


# =====================================================================
#  GUI
# =====================================================================
class ParamDialog(tk.Toplevel):
    """fields = [(key, label, default)]; a list/tuple default = drop-down choices."""

    def __init__(self, parent, title, fields):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.vars = {}
        for r, (key, label, default) in enumerate(fields):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", padx=10, pady=5)
            if isinstance(default, (list, tuple)):
                v = tk.StringVar(value=default[0])
                w = ttk.Combobox(self, textvariable=v, values=list(default), state="readonly", width=14)
            else:
                v = tk.StringVar(value=str(default))
                w = ttk.Entry(self, textvariable=v, width=16)
            w.grid(row=r, column=1, padx=10, pady=5)
            self.vars[key] = (v, default)
        bar = ttk.Frame(self)
        bar.grid(row=len(fields), column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="OK", command=self._ok).pack(side="left", padx=6)
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        out = {}
        try:
            for k, (v, d) in self.vars.items():
                if isinstance(d, (list, tuple)):
                    out[k] = v.get()
                elif isinstance(d, int):
                    out[k] = int(float(v.get()))
                elif isinstance(d, float):
                    out[k] = float(v.get())
                else:
                    out[k] = v.get()
        except ValueError:
            messagebox.showerror("Invalid value", "Please enter numbers only.", parent=self)
            return
        self.result = out
        self.destroy()


class App(tk.Tk):
    def __init__(self, files=()):
        super().__init__()
        self.title("Studio Audio Editor")
        self._center_window(2500, 1360)          # <-- was: self.geometry("1250x680")
        self.theme_name = load_theme_name()
        self.C = THEMES[self.theme_name]
        self.configure(bg=self.C["root"])
        self.proj = Project()
        self.player, self.recorder = Player(), Recorder()
        self.px_per_sec = 100.0
        self.view_start = 0            # samples at the left edge of the lane area
        self.cursor = 0
        self.sel = None                # (start, end) samples or None
        self.selected = set()          # selected track indices
        self.recording = False
        self.record_origin = 0
        self.play_origin = 0
        self._drag0 = 0
        self.loop_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")
        self.time_var = tk.StringVar(value=fmt_time(0))
        self._style()
        self._build_menu()
        self._build_toolbar()
        self._build_canvas()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        if sd is None:
            self.status_var.set("sounddevice not installed - editing works, playback/record disabled")
        self.after(150, lambda: self._import(list(files)))
        self.after(40, self._tick)

    def _center_window(self, w, h):
        """Place the window centred on the screen, clamped to fit."""
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w, h = min(w, sw), min(h, sh)             # never larger than the screen
        x, y = (sw - w) // 2, (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ------------------------------------------------------------ setup
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        C = self.C
        st.configure("TFrame", background=C["panel"])
        st.configure("TLabel", background=C["panel"], foreground=C["text"])
        st.configure("Time.TLabel", background=C["panel"], foreground=C["time"])
        st.configure("TButton", padding=5, background=C["head"], foreground=C["text"],
                     bordercolor=C["head_edge"], lightcolor=C["head"], darkcolor=C["head"])
        st.map("TButton", background=[("active", C["head_sel"])])
        st.configure("TCheckbutton", background=C["panel"], foreground=C["text"])
        st.map("TCheckbutton", background=[("active", C["panel"])])
        st.configure("TScrollbar", background=C["head"], troughcolor=C["root"],
                     bordercolor=C["root"], arrowcolor=C["text"])

    def _build_menu(self):
        m = tk.Menu(self)
        self.config(menu=m)

        def menu(label, items):
            sub = tk.Menu(m, tearoff=0)
            for it in items:
                if it is None:
                    sub.add_separator()
                else:
                    sub.add_command(label=it[0], command=it[1], accelerator=it[2] if len(it) > 2 else "")
            m.add_cascade(label=label, menu=sub)

        menu("File", [
            ("New project", self.new_project), ("Open project...", self.open_project, "Ctrl+O"),
            ("Save project", self.save_project, "Ctrl+S"), ("Save project as...", lambda: self.save_project(True)),
            None, ("Import audio...", self.import_dialog, "Ctrl+I"),
            ("Export audio...", self.export_dialog, "Ctrl+E"),
            ("Export selection...", lambda: self.export_dialog(True)),
            None, ("Quit", self.quit_app)])
        menu("Edit", [
            ("Undo", self.undo, "Ctrl+Z"), ("Redo", self.redo, "Ctrl+Y"), None,
            ("Cut", self.edit_cut, "Ctrl+X"), ("Copy", self.edit_copy, "Ctrl+C"),
            ("Paste", self.edit_paste, "Ctrl+V"), ("Delete", self.edit_delete, "Del"),
            ("Silence selection", self.edit_silence), ("Trim to selection", self.edit_trim), None,
            ("Select all", self.select_all, "Ctrl+A"), ("Select none", self.select_none)])
        menu("View", [
            ("Zoom in", lambda: self.zoom(2.0)), ("Zoom out", lambda: self.zoom(0.5)),
            ("Fit project", self.zoom_fit), ("Zoom to selection", self.zoom_sel), None,
            ("Spectrogram...", self.show_spectrogram), None,
            ("Theme: Dark (Audacity)", lambda: self.set_theme("Dark")),
            ("Theme: Light", lambda: self.set_theme("Light"))])
        menu("Transport", [
            ("Play / Stop", self.toggle_play, "Space"), ("Play selection", lambda: self.play(True)),
            ("Record / Stop", self.toggle_record, "R"), None,
            ("Go to start", lambda: self.set_cursor(0), "Home"),
            ("Go to end", lambda: self.set_cursor(self.proj.length()), "End")])
        menu("Generate", [
            ("Silence...", self.gen_silence), ("Tone...", self.gen_tone), ("White noise...", self.gen_noise)])
        menu("Effect", [
            ("Amplify...", self.fx_amplify), ("Normalize...", self.fx_normalize), None,
            ("Fade in", lambda: self.run_effect(fx_fade)),
            ("Fade out", lambda: self.run_effect(lambda x: fx_fade(x, False))),
            ("Reverse", lambda: self.run_effect(fx_reverse)),
            ("Invert", lambda: self.run_effect(fx_invert)),
            ("Remove DC offset", lambda: self.run_effect(fx_dc)), None,
            ("Change speed...", self.fx_speed), ("Low-pass filter...", self.fx_lowpass),
            ("High-pass filter...", self.fx_highpass), None,
            ("Echo...", self.fx_echo), ("Reverb...", self.fx_reverb), ("Compressor...", self.fx_compress)])
        menu("Track", [
            ("Add empty track", self.add_track), ("Remove track(s)", self.remove_tracks),
            ("Duplicate track(s)", self.duplicate_tracks), None,
            ("Rename...", self.rename_track), ("Gain...", self.track_gain), ("Pan...", self.track_pan),
            ("Time shift...", self.track_shift)])
        menu("Help", [("Keyboard shortcuts", self.show_help)])
        self.menu_track = tk.Menu(self, tearoff=0)
        for lab, cmd in (("Rename...", self.rename_track), ("Gain...", self.track_gain),
                         ("Pan...", self.track_pan), ("Time shift...", self.track_shift),
                         ("Duplicate", self.duplicate_tracks), ("Remove", self.remove_tracks)):
            self.menu_track.add_command(label=lab, command=cmd)

    def _build_toolbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        for text, cmd in (("\u25B6 Play", self.toggle_play), ("\u25A0 Stop", self.stop),
                          ("\u25CF Record", self.toggle_record)):
            ttk.Button(bar, text=text, command=cmd, takefocus=False).pack(side="left", padx=3, pady=4)
        ttk.Checkbutton(bar, text="Loop", variable=self.loop_var, takefocus=False).pack(side="left", padx=8)
        for text, cmd in (("Zoom +", lambda: self.zoom(2.0)), ("Zoom -", lambda: self.zoom(0.5)),
                          ("Fit", self.zoom_fit)):
            ttk.Button(bar, text=text, command=cmd, takefocus=False, width=7).pack(side="left", padx=2)
        ttk.Label(bar, textvariable=self.time_var, font=("Consolas", 16, "bold"),
                  style="Time.TLabel").pack(side="left", padx=18)
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=8)

    def _build_canvas(self):
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)
        self.ruler = tk.Canvas(box, height=RULER_H, bg=self.C["ruler_bg"], highlightthickness=0)
        self.canvas = tk.Canvas(box, bg=self.C["root"], highlightthickness=0)
        self.vbar = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        self.hbar = ttk.Scrollbar(box, orient="horizontal", command=self._hscroll)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.ruler.grid(row=0, column=0, sticky="ew")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.vbar.grid(row=1, column=1, sticky="ns")
        self.hbar.grid(row=2, column=0, sticky="ew")
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Button-3>", self._on_right)
        self.canvas.bind("<Button-2>", self._on_right)
        self.ruler.bind("<ButtonPress-1>", self._on_press)
        self.ruler.bind("<B1-Motion>", self._on_drag)
        for w in (self.canvas, self.ruler):
            w.bind("<MouseWheel>", self._wheel)
            w.bind("<Button-4>", self._wheel)
            w.bind("<Button-5>", self._wheel)

    def _bind_keys(self):
        def key(seq, fn, typing_ok=False):
            def handler(_e):
                if not typing_ok and isinstance(self.focus_get(), tk.Entry):
                    return None
                fn()
                return "break"
            self.bind_all(seq, handler)
        key("<space>", self.toggle_play)
        key("<Control-z>", self.undo, True); key("<Control-y>", self.redo, True)
        key("<Control-x>", self.edit_cut); key("<Control-c>", self.edit_copy)
        key("<Control-v>", self.edit_paste); key("<Delete>", self.edit_delete)
        key("<Control-a>", self.select_all)
        key("<Control-s>", self.save_project, True); key("<Control-o>", self.open_project, True)
        key("<Control-e>", self.export_dialog, True); key("<Control-i>", self.import_dialog, True)
        key("<Home>", lambda: self.set_cursor(0)); key("<End>", lambda: self.set_cursor(self.proj.length()))
        key("<r>", self.toggle_record)

    # ------------------------------------------------------------ helpers
    def ask(self, title, fields):
        return ParamDialog(self, title, fields).result

    def targets(self):
        idx = sorted(i for i in self.selected if i < len(self.proj.tracks))
        return [self.proj.tracks[i] for i in (idx or range(len(self.proj.tracks)))]

    def _x2s(self, x):
        return max(0, int(self.view_start + (x - HEAD_W) / self.px_per_sec * self.proj.rate))

    def _s2x(self, s):
        return HEAD_W + (s - self.view_start) / self.proj.rate * self.px_per_sec

    def _visible_samples(self):
        w = max(100, self.canvas.winfo_width() - HEAD_W)
        return w / self.px_per_sec * self.proj.rate

    def set_cursor(self, s):
        self.cursor, self.sel = int(max(0, s)), None
        self.redraw()

    def select_all(self):
        n = self.proj.length()
        self.sel = (0, n) if n else None
        self.redraw()

    def select_none(self):
        self.sel = None
        self.redraw()

    def set_theme(self, name):
        self.theme_name, self.C = name, THEMES[name]
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump({"theme": name}, f)
        except Exception:
            pass
        self.configure(bg=self.C["root"])
        self._style()
        self.ruler.configure(bg=self.C["ruler_bg"])
        self.canvas.configure(bg=self.C["root"])
        self.redraw()

    # ------------------------------------------------------------ drawing
    def redraw(self):
        c = self.canvas
        c.delete("all")
        W = max(c.winfo_width(), HEAD_W + 50)
        total_h = max(len(self.proj.tracks) * LANE_H, c.winfo_height())
        for i, t in enumerate(self.proj.tracks):
            self._draw_track(i, t, W)
        x = self._s2x(self.cursor)
        if HEAD_W <= x <= W:
            c.create_line(x, 0, x, total_h, fill=self.C["cursor"], tags="cursor")
        c.configure(scrollregion=(0, 0, W, total_h))
        self._draw_ruler(W)
        self._update_hbar()
        self.time_var.set(fmt_time(self.cursor / self.proj.rate))
        if not (self.recording or self.player.stream):
            if self.sel and self.sel[1] > self.sel[0]:
                r = self.proj.rate
                self.status_var.set(f"Selection {fmt_time(self.sel[0] / r)} - {fmt_time(self.sel[1] / r)}"
                                    f"  ({(self.sel[1] - self.sel[0]) / r:.3f} s)   |   {r} Hz")
            else:
                self.status_var.set(f"{len(self.proj.tracks)} track(s)   |   {self.proj.rate} Hz")

    def _draw_track(self, i, t, W):
        c, y0, C = self.canvas, i * LANE_H, self.C
        is_sel = i in self.selected
        # --- control panel (gray, like Audacity)
        c.create_rectangle(0, y0, HEADER_W, y0 + LANE_H, fill=C["head_sel"] if is_sel else C["head"], outline=C["head_edge"])
        c.create_rectangle(0, y0, HEADER_W, y0 + 22, fill=C["head_edge"] if self.theme_name == "Dark" else C["head_sel"], outline=C["head_edge"])
        c.create_text(10, y0 + 11, text=t.name, anchor="w", fill=C["text"], font=("Segoe UI", 9, "bold"))
        for k, (lab, on, col) in enumerate((("Mute", t.mute, C["mute"]), ("Solo", t.solo, C["solo"]))):
            x = 10 + k * 72
            c.create_rectangle(x, y0 + 30, x + 64, y0 + 50, fill=col if on else C["btn"], outline=C["head_edge"])
            c.create_text(x + 32, y0 + 40, text=lab, fill="#111111" if on else C["text"], font=("Segoe UI", 9))
        gdb = 20 * math.log10(max(t.gain, 1e-6))
        for k, txt in enumerate((f"Gain {gdb:+.1f} dB", f"Pan {t.pan:+.2f}", f"{self.proj.rate} Hz  32-bit float")):
            c.create_text(10, y0 + 68 + k * 16, text=txt, anchor="w", fill=C["sub"], font=("Segoe UI", 8))
        # --- amplitude scale strip
        c.create_rectangle(HEADER_W, y0, HEAD_W, y0 + LANE_H, fill=C["panel"], outline=C["head_edge"])
        for ch in (0, 1):
            cy = y0 + LANE_H * (0.25 + 0.5 * ch)
            amp = LANE_H * 0.23
            for val, lab in ((1.0, "1.0"), (0.0, "0.0"), (-1.0, "-1.0")):
                yy = cy - val * amp
                c.create_line(HEAD_W - 5, yy, HEAD_W, yy, fill=C["scale"])
                lo_, hi_ = (y0 + 7, y0 + LANE_H / 2 - 8) if ch == 0 else (y0 + LANE_H / 2 + 8, y0 + LANE_H - 7)
                c.create_text(HEAD_W - 7, min(max(yy, lo_), hi_), text=lab, anchor="e",
                              fill=C["scale"], font=("Segoe UI", 7))
        # --- lane
        c.create_rectangle(HEAD_W, y0, W, y0 + LANE_H, fill=C["lane_sel"] if is_sel else C["lane"], outline=C["lane_edge"])
        if self.sel and self.sel[1] > self.sel[0] and (is_sel or not self.selected):
            x1, x2 = max(HEAD_W, self._s2x(self.sel[0])), min(W, self._s2x(self.sel[1]))
            if x2 > x1:
                c.create_rectangle(x1, y0, x2, y0 + LANE_H, fill=C["sel"], outline="")
        for ch in (0, 1):
            cy = y0 + LANE_H * (0.25 + 0.5 * ch)
            c.create_line(HEAD_W, cy, W, cy, fill=C["center"])
        c.create_line(HEAD_W, y0 + LANE_H / 2, W, y0 + LANE_H / 2, fill=C["lane_edge"])
        self._draw_wave(t, y0, W)

    def _draw_wave(self, t, y0, W):
        c, rate = self.canvas, self.proj.rate
        width = int(W - HEAD_W)
        if width <= 0 or len(t.data) == 0:
            return
        spp = rate / self.px_per_sec                      # samples per pixel
        first = self.view_start - t.offset                # local index at left edge
        lo = max(0, int(first))
        hi = min(len(t.data), int(first + width * spp) + 2)
        if hi <= lo:
            return
        seg = t.data[lo:hi]
        color = self.C["peak_mute"] if t.mute else self.C["peak"]
        core = self.C["rms_mute"] if t.mute else self.C["rms"]
        amp = LANE_H * 0.23
        for ch in (0, 1):
            cy = y0 + LANE_H * (0.25 + 0.5 * ch)
            if spp < 1.5:                                  # zoomed in: draw the samples
                xs = HEAD_W + ((lo + np.arange(len(seg))) - first) / spp
                ys = cy - np.clip(seg[:, ch], -1, 1) * amp
                if len(xs) >= 2:
                    c.create_line(np.column_stack([xs, ys]).ravel().tolist(), fill=core)
                continue
            idx = ((np.arange(width) * spp + first) - lo).astype(np.int64)
            ok = (idx >= 0) & (idx < len(seg))
            idx, xs = idx[ok], np.arange(width)[ok] + HEAD_W
            if len(idx) < 2:
                continue
            mx = np.clip(np.maximum.reduceat(seg[:, ch], idx), -1, 1)
            mn = np.clip(np.minimum.reduceat(seg[:, ch], idx), -1, 1)
            top, bot = cy - mx * amp, np.maximum(cy - mn * amp, cy - mx * amp + 1)
            pts = np.concatenate([np.column_stack([xs, top]), np.column_stack([xs, bot])[::-1]])
            c.create_polygon(pts.ravel().tolist(), fill=color, outline=color)
            counts = np.diff(np.append(idx, len(seg)))
            rms = np.sqrt(np.add.reduceat(seg[:, ch].astype(np.float64) ** 2, idx) / np.maximum(counts, 1))
            rms = np.minimum(rms, np.maximum(np.abs(mx), np.abs(mn))) * amp
            rpts = np.concatenate([np.column_stack([xs, cy - rms]), np.column_stack([xs, cy + rms + 1])[::-1]])
            c.create_polygon(rpts.ravel().tolist(), fill=core, outline=core)

    def _draw_ruler(self, W):
        r, rate = self.ruler, self.proj.rate
        r.delete("all")
        C = self.C
        r.create_rectangle(0, 0, W + 50, RULER_H, fill=C["ruler_bg"], outline="")
        r.create_line(0, RULER_H - 1, W + 50, RULER_H - 1, fill=C["head_edge"])
        steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]
        step = next((s for s in steps if s * self.px_per_sec >= 80), steps[-1])
        t0 = self.view_start / rate
        t = math.floor(t0 / step) * step
        tend = t0 + (W - HEAD_W) / self.px_per_sec
        while t <= tend + step:
            x = HEAD_W + (t - t0) * self.px_per_sec
            if t >= -1e-9 and x >= HEAD_W:
                label = f"{int(t // 60)}:{t % 60:06.3f}" if step < 1 else f"{int(t // 60)}:{int(t % 60):02d}"
                r.create_line(x, RULER_H - 10, x, RULER_H, fill=C["ruler_fg"])
                r.create_text(x + 3, 9, text=label, anchor="w", fill=C["ruler_fg"], font=("Segoe UI", 8))
            t += step
        cx = self._s2x(self.cursor)
        if HEAD_W <= cx <= W:
            r.create_polygon(cx - 5, RULER_H - 9, cx + 5, RULER_H - 9, cx, RULER_H, fill=C["play"])

    def _update_hbar(self):
        vis = self._visible_samples()
        total = max(self.proj.length(), self.view_start + vis, 1)
        self.hbar.set(self.view_start / total, min(1.0, (self.view_start + vis) / total))

    def _hscroll(self, *args):
        vis = self._visible_samples()
        total = max(self.proj.length(), vis, 1)
        if args[0] == "moveto":
            self.view_start = float(args[1]) * total
        elif args[0] == "scroll":
            step = vis * (0.9 if args[2] == "pages" else 0.1)
            self.view_start += int(args[1]) * step
        self.view_start = int(max(0, min(self.view_start, max(0, total - vis * 0.1))))
        self.redraw()

    # ------------------------------------------------------------ zoom
    def zoom(self, factor, center=None):
        rate = self.proj.rate
        c = self.cursor if center is None else center
        if not (self.view_start <= c <= self.view_start + self._visible_samples()):
            c = int(self.view_start + self._visible_samples() / 2)
        old = self.px_per_sec
        new = min(max(old * factor, 1.0), 20000.0)
        xc = (c - self.view_start) / rate * old
        self.px_per_sec = new
        self.view_start = int(max(0, c - xc / new * rate))
        self.redraw()

    def zoom_fit(self):
        n = self.proj.length()
        w = max(100, self.canvas.winfo_width() - HEAD_W - 20)
        if n:
            self.px_per_sec = max(1.0, w / (n / self.proj.rate))
        self.view_start = 0
        self.redraw()

    def zoom_sel(self):
        if not self.sel or self.sel[1] <= self.sel[0]:
            return
        w = max(100, self.canvas.winfo_width() - HEAD_W - 20)
        self.px_per_sec = min(20000.0, w / ((self.sel[1] - self.sel[0]) / self.proj.rate))
        self.view_start = self.sel[0]
        self.redraw()

    # ------------------------------------------------------------ mouse
    def _wheel(self, ev):
        d = 1 if (getattr(ev, "delta", 0) > 0 or getattr(ev, "num", 0) == 4) else -1
        if ev.state & 0x4:                                   # Ctrl = zoom
            self.zoom(2.0 if d > 0 else 0.5, self._x2s(ev.x))
        elif ev.state & 0x1:                                 # Shift = scroll sideways
            self._hscroll("scroll", -d, "units")
        else:
            self.canvas.yview_scroll(-d, "units")

    def _on_press(self, ev):
        if self.recording:
            return
        y = self.canvas.canvasy(ev.y) if ev.widget is self.canvas else 0
        i = int(y // LANE_H) if ev.widget is self.canvas else -1
        if ev.widget is self.canvas and ev.x < HEADER_W:
            return self._header_click(i, ev, y)
        s = self._x2s(ev.x)
        self._drag0, self.cursor, self.sel = s, s, None
        if 0 <= i < len(self.proj.tracks):
            ctrl = ev.state & 0x4
            if ctrl:
                self.selected ^= {i}
            else:
                self.selected = {i}
        elif ev.widget is self.ruler:
            pass
        self.redraw()

    def _on_drag(self, ev):
        if self.recording or ev.x < 0:
            return
        s = self._x2s(ev.x)
        a, b = sorted((self._drag0, s))
        self.cursor, self.sel = a, ((a, b) if b > a else None)
        self.redraw()

    def _header_click(self, i, ev, y):
        if not (0 <= i < len(self.proj.tracks)):
            return
        t, ry = self.proj.tracks[i], y - i * LANE_H
        if 30 <= ry <= 50 and 10 <= ev.x <= 74:
            self.proj.push_undo(); t.mute = not t.mute
        elif 30 <= ry <= 50 and 82 <= ev.x <= 146:
            self.proj.push_undo(); t.solo = not t.solo
        elif ev.state & 0x4:
            self.selected ^= {i}
        else:
            self.selected = {i}
        self.redraw()

    def _on_double(self, ev):
        if ev.x < HEADER_W:
            self.rename_track()

    def _on_right(self, ev):
        y = self.canvas.canvasy(ev.y)
        i = int(y // LANE_H)
        if 0 <= i < len(self.proj.tracks):
            if i not in self.selected:
                self.selected = {i}
                self.redraw()
            self.menu_track.tk_popup(ev.x_root, ev.y_root)

    # ------------------------------------------------------------ transport
    def _play_range(self, selection_only=False):
        n = self.proj.length()
        if self.sel and self.sel[1] > self.sel[0]:
            return self.sel
        s = self.cursor if self.cursor < n else 0
        return s, n

    def play(self, selection_only=False):
        if self.recording:
            return
        s, e = self._play_range(selection_only)
        if e <= s:
            return
        try:
            self.player.start(self.proj.mixdown(s, e), self.proj.rate, self.loop_var.get())
        except Exception as ex:
            messagebox.showerror("Playback", str(ex))
            return
        self.play_origin = s
        self.status_var.set("Playing...")

    def toggle_play(self):
        if self.recording:
            return self.toggle_record()
        if self.player.stream is not None:
            self.stop()
        else:
            self.play()

    def stop(self):
        if self.recording:
            return self._stop_record()
        if self.player.stream is not None:
            self.cursor = self.play_origin + self.player.pos
            self.player.stop()
            self.redraw()

    def toggle_record(self):
        if self.recording:
            return self._stop_record()
        self.stop()
        try:
            self.recorder.start(self.proj.rate)
        except Exception as ex:
            messagebox.showerror("Record", str(ex))
            return
        self.recording, self.record_origin = True, self.cursor
        self.status_var.set("Recording...  (press R or Stop to finish)")

    def _stop_record(self):
        data = self.recorder.stop()
        self.recording = False
        if len(data):
            self.proj.push_undo()
            t = self.proj.new_track(f"Recording {len(self.proj.tracks) + 1}", data, self.record_origin)
            self.selected = {len(self.proj.tracks) - 1}
            self.cursor = self.record_origin + len(data)
        self.redraw()

    def _tick(self):
        try:
            if self.recording:
                self.time_var.set(fmt_time(self.recorder.frames / self.proj.rate))
            elif self.player.stream is not None:
                pos = self.play_origin + self.player.pos
                self.time_var.set(fmt_time(pos / self.proj.rate))
                if not self.player.playing:                    # reached the end
                    self.cursor = pos
                    self.player.stop()
                    self.redraw()
                else:
                    x = self._s2x(pos)
                    if x > self.canvas.winfo_width() - 10 or x < HEAD_W:
                        self.view_start = int(pos)
                        self.redraw()
                        x = self._s2x(pos)
                    h = max(len(self.proj.tracks) * LANE_H, self.canvas.winfo_height())
                    if self.canvas.find_withtag("play"):
                        self.canvas.coords("play", x, 0, x, h)
                    else:
                        self.canvas.create_line(x, 0, x, h, fill=self.C["play"], width=2, tags="play")
        finally:
            self.after(40, self._tick)

    # ------------------------------------------------------------ editing
    def after_edit(self):
        self.selected = {i for i in self.selected if i < len(self.proj.tracks)}
        self.redraw()

    def undo(self):
        if self.proj.undo():
            self.after_edit()

    def redo(self):
        if self.proj.redo():
            self.after_edit()

    def _has_sel(self):
        return bool(self.sel and self.sel[1] > self.sel[0])

    def edit_copy(self):
        if self._has_sel():
            self.proj.clip = [t.copy_range(*self.sel) for t in self.targets()]
            self.status_var.set("Copied")

    def edit_cut(self):
        if self._has_sel():
            self.edit_copy()
            self.edit_delete()

    def edit_delete(self):
        if not self._has_sel():
            return
        s, e = self.sel
        self.proj.push_undo()
        for t in self.targets():
            t.delete(s, e)
        self.cursor, self.sel = s, None
        self.after_edit()

    def edit_silence(self):
        if self._has_sel():
            self.proj.push_undo()
            for t in self.targets():
                t.silence(*self.sel)
            self.after_edit()

    def edit_trim(self):
        if self._has_sel():
            self.proj.push_undo()
            for t in self.targets():
                t.trim(*self.sel)
            self.after_edit()

    def edit_paste(self):
        if not self.proj.clip:
            return
        self.proj.push_undo()
        tg = self.targets() if self.proj.tracks else []
        while len(tg) < len(self.proj.clip) and not self.proj.tracks:
            tg.append(self.proj.new_track())
        for t, clip in zip(tg, self.proj.clip):
            t.insert(self.cursor, clip)
        self.cursor += len(self.proj.clip[0])
        self.sel = None
        self.after_edit()

    def run_effect(self, fn):
        if not self.proj.tracks:
            return
        self.proj.push_undo()
        self.config(cursor="watch"); self.update_idletasks()
        try:
            for t in self.targets():
                s, e = self.sel if self._has_sel() else (t.offset, t.end)
                t.apply(s, e, fn)
        except Exception as ex:
            self.proj.undo()
            messagebox.showerror("Effect failed", str(ex))
        finally:
            self.config(cursor="")
        self.after_edit()

    def _fx_dialog(self, title, fields, make):
        p = self.ask(title, fields)
        if p is not None:
            self.run_effect(make(p, self.proj.rate))

    def fx_amplify(self):
        self._fx_dialog("Amplify", [("db", "Amplification (dB)", 6.0)],
                        lambda p, r: lambda x: fx_amplify(x, p["db"]))

    def fx_normalize(self):
        self._fx_dialog("Normalize", [("db", "Peak level (dB)", -1.0)],
                        lambda p, r: lambda x: fx_normalize(x, p["db"]))

    def fx_speed(self):
        self._fx_dialog("Change speed (also changes pitch)", [("f", "Speed factor (2.0 = twice as fast)", 1.25)],
                        lambda p, r: lambda x: fx_speed(x, max(0.1, min(p["f"], 10.0))))

    def fx_lowpass(self):
        self._fx_dialog("Low-pass filter", [("hz", "Cut-off frequency (Hz)", 3000.0)],
                        lambda p, r: lambda x: fx_filter(x, r, "low", p["hz"]))

    def fx_highpass(self):
        self._fx_dialog("High-pass filter", [("hz", "Cut-off frequency (Hz)", 100.0)],
                        lambda p, r: lambda x: fx_filter(x, r, "high", p["hz"]))

    def fx_echo(self):
        self._fx_dialog("Echo", [("d", "Delay (seconds)", 0.3), ("k", "Decay (0-1)", 0.5), ("n", "Repeats", 5)],
                        lambda p, r: lambda x: fx_echo(x, r, p["d"], min(p["k"], 0.95), max(1, p["n"])))

    def fx_reverb(self):
        self._fx_dialog("Reverb", [("room", "Room size (0-1)", 0.5), ("wet", "Wet amount (0-1)", 0.3)],
                        lambda p, r: lambda x: fx_reverb(x, r, min(max(p["room"], 0), 1), min(max(p["wet"], 0), 1)))

    def fx_compress(self):
        self._fx_dialog("Compressor", [("th", "Threshold (dB)", -18.0), ("ra", "Ratio", 3.0), ("mk", "Make-up gain (dB)", 3.0)],
                        lambda p, r: lambda x: fx_compress(x, r, p["th"], p["ra"], p["mk"]))

    # ------------------------------------------------------------ generate
    def _gen_into(self, clip):
        self.proj.push_undo()
        sel = [self.proj.tracks[i] for i in sorted(self.selected) if i < len(self.proj.tracks)]
        t = sel[0] if sel else self.proj.new_track()
        t.insert(self.cursor, clip)
        self.after_edit()

    def gen_silence(self):
        p = self.ask("Generate silence", [("s", "Seconds", 1.0)])
        if p:
            self._gen_into(np.zeros((int(p["s"] * self.proj.rate), 2), np.float32))

    def gen_tone(self):
        p = self.ask("Generate tone", [("shape", "Waveform", ["sine", "square", "saw", "triangle"]),
                                       ("f", "Frequency (Hz)", 440.0), ("s", "Seconds", 2.0), ("a", "Amplitude (0-1)", 0.5)])
        if p:
            self._gen_into(gen_tone(self.proj.rate, p["s"], p["f"], p["shape"], min(p["a"], 1.0)))

    def gen_noise(self):
        p = self.ask("Generate white noise", [("s", "Seconds", 2.0), ("a", "Amplitude (0-1)", 0.3)])
        if p:
            self._gen_into(gen_noise(self.proj.rate, p["s"], min(p["a"], 1.0)))

    # ------------------------------------------------------------ tracks
    def add_track(self):
        self.proj.push_undo()
        self.proj.new_track()
        self.after_edit()

    def remove_tracks(self):
        idx = sorted(self.selected)
        if not idx:
            return
        self.proj.push_undo()
        self.proj.tracks = [t for i, t in enumerate(self.proj.tracks) if i not in idx]
        self.selected = set()
        self.after_edit()

    def duplicate_tracks(self):
        idx = sorted(i for i in self.selected if i < len(self.proj.tracks))
        if not idx:
            return
        self.proj.push_undo()
        for i in idx:
            c = self.proj.tracks[i].clone()
            c.name += " copy"
            self.proj.tracks.append(c)
        self.after_edit()

    def _one(self):
        idx = sorted(i for i in self.selected if i < len(self.proj.tracks))
        return self.proj.tracks[idx[0]] if idx else None

    def rename_track(self):
        t = self._one()
        if t:
            name = simpledialog.askstring("Rename track", "Track name:", initialvalue=t.name, parent=self)
            if name:
                self.proj.push_undo(); self._one().name = name; self.after_edit()

    def track_gain(self):
        t = self._one()
        p = t and self.ask("Track gain", [("db", "Gain (dB)", round(20 * math.log10(max(t.gain, 1e-6)), 1))])
        if p:
            self.proj.push_undo()
            for x in self.targets():
                x.gain = 10 ** (p["db"] / 20)
            self.after_edit()

    def track_pan(self):
        t = self._one()
        p = t and self.ask("Track pan", [("p", "Pan (-1 = left, 0 = centre, 1 = right)", float(t.pan))])
        if p:
            self.proj.push_undo()
            for x in self.targets():
                x.pan = max(-1.0, min(1.0, p["p"]))
            self.after_edit()

    def track_shift(self):
        t = self._one()
        p = t and self.ask("Time shift", [("s", "Move by (seconds, negative = earlier)", 0.0)])
        if p:
            self.proj.push_undo()
            for x in self.targets():
                x.offset = max(0, x.offset + int(p["s"] * self.proj.rate))
            self.after_edit()

    # ------------------------------------------------------------ files
    def _import(self, paths):
        if not paths:
            return
        self.proj.push_undo()
        first = not self.proj.tracks
        self.config(cursor="watch"); self.update_idletasks()
        try:
            for p in paths:
                try:
                    self.proj.import_file(p)
                except Exception as ex:
                    messagebox.showerror("Import failed", f"{os.path.basename(p)}\n\n{ex}")
        finally:
            self.config(cursor="")
        if first:
            self.zoom_fit()
        self.after_edit()

    def import_dialog(self):
        self._import(list(filedialog.askopenfilenames(
            title="Import audio", filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.mp4 *.wma"), ("All", "*.*")])))

    def export_dialog(self, selection=False):
        if not self.proj.tracks:
            return
        if selection and not self._has_sel():
            return messagebox.showinfo("Export selection", "Select a region first.")
        path = filedialog.asksaveasfilename(
            title="Export audio", defaultextension=".wav",
            filetypes=[("WAV", "*.wav"), ("MP3", "*.mp3"), ("FLAC", "*.flac"), ("OGG", "*.ogg"), ("M4A (ffmpeg)", "*.m4a")])
        if not path:
            return
        s, e = self.sel if selection else (0, self.proj.length())
        try:
            save_audio(path, self.proj.mixdown(s, e), self.proj.rate)
            self.status_var.set(f"Exported {os.path.basename(path)}")
        except Exception as ex:
            messagebox.showerror("Export failed", str(ex))

    def _confirm_discard(self):
        if self.proj.dirty and self.proj.tracks:
            a = messagebox.askyesnocancel("Unsaved changes", "Save the project first?")
            if a is None:
                return False
            if a:
                self.save_project()
        return True

    def new_project(self):
        if self._confirm_discard():
            self.stop()
            self.proj, self.sel, self.selected, self.cursor, self.view_start = Project(), None, set(), 0, 0
            self.redraw()

    def open_project(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(title="Open project", filetypes=[("Studio project", "*.npz")])
        if path:
            try:
                p = Project(); p.load(path)
            except Exception as ex:
                return messagebox.showerror("Open failed", str(ex))
            self.stop()
            self.proj, self.sel, self.selected, self.cursor = p, None, set(), 0
            self.zoom_fit()

    def save_project(self, as_new=False):
        path = self.proj.path
        if as_new or not path:
            path = filedialog.asksaveasfilename(title="Save project", defaultextension=".npz",
                                                filetypes=[("Studio project", "*.npz")])
        if path:
            try:
                self.proj.save(path)
                self.status_var.set(f"Saved {os.path.basename(path)}")
            except Exception as ex:
                messagebox.showerror("Save failed", str(ex))

    def quit_app(self):
        if self._confirm_discard():
            self.player.stop()
            if self.recording:
                self.recorder.stop()
            self.destroy()

    # ------------------------------------------------------------ extras
    def show_spectrogram(self):
        tg = self.targets()
        if not tg:
            return
        t = tg[0]
        a, b = t.local(*self.sel) if self._has_sel() else (0, len(t.data))
        b = min(b, a + self.proj.rate * 600)
        if b - a < 256:
            return messagebox.showinfo("Spectrogram", "Nothing to analyse here.")
        w = tk.Toplevel(self)
        w.title(f"Spectrogram - {t.name}")
        img = tk.PhotoImage(data=spectrogram_ppm(t.data[a:b], self.proj.rate), format="PPM")
        cv = tk.Canvas(w, width=img.width() + 70, height=img.height() + 24, bg="#111", highlightthickness=0)
        cv.pack()
        cv.create_image(60, 6, anchor="nw", image=img)
        cv.image = img                                    # keep a reference
        nyq = self.proj.rate / 2
        for k in range(5):
            y = 6 + k * (img.height() - 1) / 4
            cv.create_text(54, y, text=f"{nyq * (1 - k / 4) / 1000:.1f}k", anchor="e", fill="#ccc", font=("Segoe UI", 8))
        cv.create_text(60, img.height() + 14, anchor="w", fill="#ccc", font=("Segoe UI", 8),
                       text=f"0 s  ...  {(b - a) / self.proj.rate:.2f} s")

    def show_help(self):
        messagebox.showinfo("Keyboard shortcuts", (
            "Space  play / stop           R  record\n"
            "Ctrl+Z / Ctrl+Y  undo / redo\n"
            "Ctrl+X / C / V  cut / copy / paste\n"
            "Delete  delete selection     Ctrl+A  select all\n"
            "Ctrl+I  import   Ctrl+E  export   Ctrl+S  save   Ctrl+O  open\n"
            "Home / End  go to start / end\n\n"
            "Mouse: click or drag in a track to set cursor / select.\n"
            "Ctrl+wheel = zoom, Shift+wheel = scroll sideways.\n"
            "Click M / S on a track to mute / solo. Right-click a track for more.\n"
            "Ctrl+click selects several tracks; edits apply to selected tracks\n"
            "(or all tracks if none is selected)."))


def main():
    try:                                   # sharper text on Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()