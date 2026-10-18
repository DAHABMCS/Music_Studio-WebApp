#!/usr/bin/env python3
"""
Studio Audio Editor .

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
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import sounddevice as sd
except Exception:  # PortAudio missing -> editing works, playback doesn't
    sd = None

DEFAULT_RATE = 44100
MAX_UNDO = 30


PAL = dict(
    toolbar="#f8f8f9", bottom="#f8f8f9", status="#f0f0f0", sep="#d3d3d8", grip="#a0a0a8",
    btn="#dddee4", btn_hover="#d0d1d9", btn_down="#c0c2cc", btn_active="#c2c4cf", btn_off="#e9e9ec",
    icon="#16161a", icon_off="#a9aab1",
    ruler="#e9e9eb", ruler_fg="#17171a", ruler_tick="#2b2b30",
    panel_sel="#dbe7ff", panel="#e4e6ee", frame_sel="#97a8fd", frame="#9da0b8",
    ctl="#c3d2e0", mute="#e8b04a", solo="#7fcf7f",
    clip_hdr="#cfd6f4", clip_bg="#f0f3ff", clip_sel="#c7cdea",
    wave="#6464d3", wave_sel="#3b3ba8", wave_mute="#b4b4c8",
    empty="#3e4051", empty_sel="#505470", desk="#323545", black="#000000",
    time_bg="#323545", cursor="#000000", play="#2e9d52", thumb="#858585",
    check="#2f6fe0", meter_edge="#c9c9cf")

# Layout metrics were measured on a 2880 px wide screen and scale to the real screen.
U_SCALE = 1.0
HEADER_W = HEAD_W = LANE_H = RULER_H = HDR_H = RIGHT_M = 0
LINE = BAND = INSET = FR_L = FR_R = 0


def u(px):
    return max(1, int(round(px * U_SCALE)))


def fnt(px, bold=False):
    n = max(9, int(round(px * U_SCALE)))
    return ("Segoe UI", -n, "bold") if bold else ("Segoe UI", -n)


def init_metrics(screen_w):
    global U_SCALE, HEADER_W, HEAD_W, LANE_H, RULER_H, HDR_H, RIGHT_M, LINE, BAND, INSET, FR_L, FR_R
    U_SCALE = max(0.5, screen_w / 2880.0)
    HEADER_W, HEAD_W = u(312), u(376)  # control panel / start of the waveform
    LANE_H, RULER_H, HDR_H = u(606), u(56), u(32)
    LINE, BAND = max(2, u(2)), u(6)
    INSET = 2 * LINE + BAND
    FR_L, FR_R = u(8), u(6)
    RIGHT_M = FR_R + INSET


init_metrics(2880)


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


def fmt_hms(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d} h {int(sec % 3600 // 60):02d} m {int(sec % 60):02d} s"


def fmt_hmsms(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 3600):02d} h {int(sec % 3600 // 60):02d} m {sec % 60:06.3f} s"


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
            pass  # try ffmpeg below
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
        b = np.zeros(d + 1);
        b[d] = 1.0
        a = np.zeros(d + 1);
        a[0] = 1.0;
        a[d] = -fb
        acc += signal.lfilter(b, a, xp, axis=0)
    acc /= 4.0
    for dl, g in ((0.005, 0.7), (0.0017, 0.7)):
        d = max(1, int(dl * rate))
        b = np.zeros(d + 1);
        b[0] = -g;
        b[d] = 1.0
        a = np.zeros(d + 1);
        a[0] = 1.0;
        a[d] = -g
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
        self.offset = int(offset)  # start position on the timeline (samples)
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
        self._undo.clear();
        self._redo.clear()
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
        self.volume = 1.0

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
            out[filled:filled + len(chunk)] = chunk * self.volume
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

    def level(self):
        """Peak (left, right) of the audio just played, for the meter."""
        seg = self.buf[max(0, self.pos - 1024):self.pos]
        if not self.playing or len(seg) == 0:
            return 0.0, 0.0
        pk = np.max(np.abs(seg), axis=0) * self.volume
        return float(pk[0]), float(pk[1])

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
        self.level = 0.0

    def start(self, rate):
        if sd is None:
            raise RuntimeError("Recording needs the 'sounddevice' package:  pip install sounddevice")
        self.chunks, self.frames = [], 0

        def cb(data, _f, _t, _s):
            self.chunks.append(data.copy())
            self.level = float(np.max(np.abs(data))) if len(data) else 0.0
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
#  GUI  -  look & feel modelled on  3.x (light theme)
# =====================================================================
def rrect(c, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle polygon."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


_FONTS = {}


def measure(font, text):
    f = _FONTS.get(font)
    if f is None:
        f = _FONTS[font] = tkfont.Font(font=font)
    return f.measure(text)


def ellipsize(text, font, maxw):
    if measure(font, text) <= maxw:
        return text
    while text and measure(font, text + "...") > maxw:
        text = text[:-1]
    return text + "..."


# ---------------------------------------------------------------- icon painters
# painter(canvas, cx, cy, s): draw an icon centred on (cx, cy), about s px big
def _lw(s, k=0.05):
    return max(1, int(round(s * k)))


def p_pause(c, cx, cy, s):
    bw, bh, gap = s * .095, s * .26, s * .12
    for sx in (-1, 1):
        x = cx + sx * (gap / 2 + bw / 2)
        c.create_rectangle(x - bw / 2, cy - bh / 2, x + bw / 2, cy + bh / 2, fill=c.ink(), outline="")


def p_play(c, cx, cy, s):
    col = c.ink("#3f8a5a")
    w, h = s * .26, s * .32
    c.create_polygon(cx - w / 2 + s * .02, cy - h / 2, cx - w / 2 + s * .02, cy + h / 2,
                     cx + w / 2 + s * .02, cy, fill=col, outline=col)


def p_stop(c, cx, cy, s):
    q = s * .2
    col = c.ink("#16161a") if c.enabled else "#9b9ca4"
    c.create_rectangle(cx - q / 2, cy - q / 2, cx + q / 2, cy + q / 2, fill=col, outline="")


def p_skip_start(c, cx, cy, s):
    ink, h = c.ink(), s * .25
    c.create_rectangle(cx - s * .15, cy - h / 2, cx - s * .15 + s * .045, cy + h / 2, fill=ink, outline="")
    c.create_polygon(cx + s * .14, cy - h / 2, cx + s * .14, cy + h / 2, cx - s * .08, cy, fill=ink, outline=ink)


def p_skip_end(c, cx, cy, s):
    ink, h = c.ink(), s * .25
    c.create_rectangle(cx + s * .15 - s * .045, cy - h / 2, cx + s * .15, cy + h / 2, fill=ink, outline="")
    c.create_polygon(cx - s * .14, cy - h / 2, cx - s * .14, cy + h / 2, cx + s * .08, cy, fill=ink, outline=ink)


def p_record(c, cx, cy, s):
    r = s * .17
    red = c.ink("#b3312c")
    c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=red, outline=red)
    t = r * .52
    c.create_polygon(cx - t, cy - t * .6, cx + t, cy - t * .6, cx, cy + t * .85, fill="#ffffff", outline="")


def p_loop(c, cx, cy, s):
    ink, w, h, lw = c.ink(), s * .26, s * .15, _lw(s, .03)
    q = s * .05
    c.create_line(cx - w, cy, cx - w, cy - h, cx + w * .5, cy - h, fill=ink, width=lw, joinstyle="round")
    c.create_polygon(cx + w * .45, cy - h - q, cx + w * .45, cy - h + q, cx + w * .9, cy - h, fill=ink, outline=ink)
    c.create_line(cx + w, cy, cx + w, cy + h, cx - w * .5, cy + h, fill=ink, width=lw, joinstyle="round")
    c.create_polygon(cx - w * .45, cy + h - q, cx - w * .45, cy + h + q, cx - w * .9, cy + h, fill=ink, outline=ink)


def p_ibeam(c, cx, cy, s):
    ink, h, w, lw = c.ink(), s * .34, s * .13, _lw(s, .05)
    c.create_line(cx, cy - h, cx, cy + h, fill=ink, width=lw)
    c.create_line(cx - w, cy - h, cx + w, cy - h, fill=ink, width=lw)
    c.create_line(cx - w, cy + h, cx + w, cy + h, fill=ink, width=lw)


def p_envelope(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .06)
    c.create_line(cx - s * .25, cy + s * .18, cx + s * .25, cy - s * .18, fill=ink, width=lw)
    for sx in (-1, 1):
        r = s * .07
        x, y = cx + sx * s * .25, cy - sx * s * .18
        c.create_oval(x - r, y - r, x + r, y + r, fill=ink, outline=ink)


def p_draw(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .05)
    c.create_line(cx - s * .22, cy + s * .24, cx + s * .2, cy - s * .2, fill=ink, width=lw * 2)
    c.create_polygon(cx - s * .3, cy + s * .32, cx - s * .26, cy + s * .18, cx - s * .16, cy + s * .28,
                     fill=ink, outline=ink)


def p_multi(c, cx, cy, s):
    ink, r, lw = c.ink(), s * .26, _lw(s, .05)
    for ang in (90, 30, 150):
        a = math.radians(ang)
        c.create_line(cx - r * math.cos(a), cy - r * math.sin(a), cx + r * math.cos(a), cy + r * math.sin(a),
                      fill=ink, width=lw)


def _magnifier(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .05)
    x0, y0, r = cx - s * .05, cy - s * .06, s * .21
    c.create_oval(x0 - r, y0 - r, x0 + r, y0 + r, outline=ink, width=lw)
    c.create_line(x0 + r * .72, y0 + r * .72, x0 + r * 1.55, y0 + r * 1.55, fill=ink, width=lw * 2)
    return x0, y0, r, ink, lw


def p_zoom_in(c, cx, cy, s):
    x0, y0, r, ink, lw = _magnifier(c, cx, cy, s)
    c.create_line(x0 - r * .5, y0, x0 + r * .5, y0, fill=ink, width=lw)
    c.create_line(x0, y0 - r * .5, x0, y0 + r * .5, fill=ink, width=lw)


def p_zoom_out(c, cx, cy, s):
    x0, y0, r, ink, lw = _magnifier(c, cx, cy, s)
    c.create_line(x0 - r * .5, y0, x0 + r * .5, y0, fill=ink, width=lw)


def p_zoom_sel(c, cx, cy, s):
    x0, y0, r, ink, lw = _magnifier(c, cx, cy, s)
    c.create_line(x0 - r * .6, y0, x0 + r * .6, y0, fill=ink, width=lw)
    for sx in (-1, 1):
        c.create_line(x0 + sx * r * .6, y0 - r * .45, x0 + sx * r * .6, y0 + r * .45, fill=ink, width=lw)


def p_zoom_fit(c, cx, cy, s):
    x0, y0, r, ink, lw = _magnifier(c, cx, cy, s)
    for dx in (-.5, 0, .5):
        c.create_line(x0 + dx * r, y0 - r * .5, x0 + dx * r, y0 + r * .5, fill=ink, width=lw)


def p_zoom_toggle(c, cx, cy, s):
    x0, y0, r, ink, lw = _magnifier(c, cx, cy, s)
    c.create_line(x0 - r * .55, y0 - r * .25, x0 - r * .05, y0 - r * .25, fill=ink, width=lw)
    c.create_line(x0 + r * .05, y0 + r * .25, x0 + r * .55, y0 + r * .25, fill=ink, width=lw)


def _bars(c, cx, cy, s, heights, x_from, x_to, ink, lw):
    n = len(heights)
    for k, hh in enumerate(heights):
        x = x_from + (x_to - x_from) * (k / max(1, n - 1))
        c.create_line(x, cy - hh * s, x, cy + hh * s, fill=ink, width=lw)


def p_trim(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .05)
    _bars(c, cx, cy, s, (.07, .16, .22, .16, .07), cx - s * .17, cx + s * .17, ink, lw)
    for sx in (-1, 1):
        c.create_line(cx + sx * s * .3, cy - s * .27, cx + sx * s * .3, cy + s * .27, fill=ink, width=lw * 2)


def p_silence(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .05)
    _bars(c, cx, cy, s, (.07, .16, .22), cx - s * .32, cx - s * .18, ink, lw)
    _bars(c, cx, cy, s, (.22, .16, .07), cx + s * .18, cx + s * .32, ink, lw)
    c.create_line(cx - s * .1, cy, cx + s * .1, cy, fill=ink, width=lw)


def p_undo(c, cx, cy, s):
    c.create_text(cx, cy, text="\u21B6", fill=c.ink(), font=("Segoe UI Symbol", -int(s * .72)))


def p_redo(c, cx, cy, s):
    c.create_text(cx, cy, text="\u21B7", fill=c.ink(), font=("Segoe UI Symbol", -int(s * .72)))


def p_gear(c, cx, cy, s):
    c.create_text(cx, cy, text="\u2699", fill=c.ink(), font=("Segoe UI Symbol", -int(s * .78)))


def p_mic(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .06)
    rrect(c, cx - s * .11, cy - s * .34, cx + s * .11, cy + s * .04, s * .11, outline=ink, fill="", width=lw)
    c.create_arc(cx - s * .21, cy - s * .16, cx + s * .21, cy + s * .2, start=180, extent=180,
                 style="arc", outline=ink, width=lw)
    c.create_line(cx, cy + s * .2, cx, cy + s * .32, fill=ink, width=lw)
    c.create_line(cx - s * .1, cy + s * .32, cx + s * .1, cy + s * .32, fill=ink, width=lw)


def p_speaker(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .06)
    c.create_polygon(cx - s * .3, cy - s * .1, cx - s * .16, cy - s * .1, cx, cy - s * .26, cx, cy + s * .26,
                     cx - s * .16, cy + s * .1, cx - s * .3, cy + s * .1, outline=ink, fill="", width=lw)
    for rr in (.16, .3):
        c.create_arc(cx - s * rr, cy - s * rr, cx + s * rr, cy + s * rr, start=-45, extent=90,
                     style="arc", outline=ink, width=lw)


def p_audio_setup(c, cx, cy, s):
    p_speaker(c, cx - s * .12, cy, s)
    c.create_text(cx + s * .42, cy, text="\u25BE", fill=c.ink(), font=("Segoe UI", -int(s * .38), "bold"))


def p_share(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .06)
    c.create_line(cx - s * .3, cy - s * .02, cx - s * .3, cy + s * .3, cx + s * .3, cy + s * .3, cx + s * .3,
                  cy - s * .02, fill=ink, width=lw, joinstyle="round")
    c.create_line(cx, cy + s * .14, cx, cy - s * .34, fill=ink, width=lw)
    c.create_line(cx - s * .14, cy - s * .2, cx, cy - s * .34, cx + s * .14, cy - s * .2, fill=ink, width=lw)


def p_plug(c, cx, cy, s):
    ink, lw = c.ink(), _lw(s, .06)
    c.create_line(cx - s * .04, cy - s * .1, cx + s * .2, cy - s * .34, fill=ink, width=lw)
    c.create_line(cx + s * .08, cy + s * .02, cx + s * .32, cy - s * .22, fill=ink, width=lw)
    c.create_polygon(cx - s * .3, cy + s * .02, cx - s * .04, cy - s * .24, cx + s * .22, cy + s * .02,
                     cx - s * .04, cy + s * .28, outline=ink, fill="", width=lw)
    c.create_line(cx - s * .2, cy + s * .22, cx - s * .34, cy + s * .36, fill=ink, width=lw)


def p_play_speed(c, cx, cy, s):
    col = c.ink("#3f8a5a")
    w, h = s * .3, s * .38
    c.create_polygon(cx - w / 2, cy - h / 2, cx - w / 2, cy + h / 2, cx + w / 2, cy, fill=col, outline=col)


# ---------------------------------------------------------------- small widgets
class IconButton(tk.Canvas):
    """Flat grey toolbar button with a drawn icon (and optional caption)."""

    def __init__(self, master, w, h, painter, command=None, text=None, layout="icon",
                 dim_face=False, bg=None):
        super().__init__(master, width=w, height=h, bg=bg or PAL["toolbar"], highlightthickness=0, bd=0)
        self.w, self.h, self.painter, self.command = w, h, painter, command
        self.text, self.layout, self.dim_face = text, layout, dim_face
        self.enabled, self.active = True, False
        self._hover = self._down = False
        self.bind("<Enter>", lambda e: self._set_state(hover=True))
        self.bind("<Leave>", lambda e: self._set_state(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda e: self._set_state(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.draw()

    def ink(self, col=None):
        return (col or PAL["icon"]) if self.enabled else PAL["icon_off"]

    def _set_state(self, hover=None, down=None):
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down
        self.draw()

    def _release(self, ev):
        was = self._down
        self._set_state(down=False)
        if was and self.enabled and 0 <= ev.x <= self.w and 0 <= ev.y <= self.h and self.command:
            self.command()

    def set(self, enabled=None, active=None):
        changed = False
        if enabled is not None and enabled != self.enabled:
            self.enabled, changed = enabled, True
        if active is not None and active != self.active:
            self.active, changed = active, True
        if changed:
            self.draw()

    def draw(self):
        self.delete("all")
        if not self.enabled and self.dim_face:
            face = PAL["btn_off"]
        elif self._down and self.enabled:
            face = PAL["btn_down"]
        elif self.active:
            face = PAL["btn_active"]
        elif self._hover and self.enabled:
            face = PAL["btn_hover"]
        else:
            face = PAL["btn"]
        rrect(self, 1, 1, self.w - 1, self.h - 1, u(7), fill=face, outline=face)
        if self.layout == "stacked":
            self.painter(self, self.w / 2, self.h * .36, self.h * .46)
            self.create_text(self.w / 2, self.h * .8, text=self.text, fill=self.ink(), font=fnt(27))
        elif self.layout == "inline":
            self.painter(self, self.h * .52, self.h / 2, self.h * .66)
            self.create_text(self.h * 1.02, self.h / 2, text=self.text, anchor="w", fill=self.ink(), font=fnt(28))
        else:
            self.painter(self, self.w / 2, self.h / 2, min(self.w, self.h))


class Gripper(tk.Canvas):
    def __init__(self, master, h=None, bg=None):
        h, w = h or u(34), u(14)
        super().__init__(master, width=w, height=h, bg=bg or PAL["toolbar"], highlightthickness=0)
        r = max(1, u(2))
        for col in (0, 1):
            for row in range(3):
                x, y = w / 2 + (col - .5) * u(6), h / 2 + (row - 1) * u(7)
                self.create_oval(x - r, y - r, x + r, y + r, fill=PAL["grip"], outline="")


class CheckBox(tk.Canvas):
    def __init__(self, master, text, var, command=None):
        self.var, self.command, self.text = var, command, text
        self.box = u(34)
        font = fnt(26)
        super().__init__(master, width=self.box + u(14) + measure(font, text) + u(4), height=self.box + u(6),
                         bg=PAL["bottom"], highlightthickness=0, cursor="arrow")
        self.bind("<ButtonPress-1>", self._toggle)
        self.draw()

    def _toggle(self, _e):
        self.var.set(not self.var.get())
        self.draw()
        if self.command:
            self.command()

    def draw(self):
        self.delete("all")
        b, y = self.box, 3
        on = self.var.get()
        rrect(self, 2, y, b, y + b - 2, u(6), fill=PAL["check"] if on else "#ffffff",
              outline=PAL["check"] if on else "#7a7a80", width=max(1, u(2)))
        if on:
            self.create_line(b * .26, y + b * .5, b * .45, y + b * .68, b * .78, y + b * .3,
                             fill="#ffffff", width=max(2, u(4)), joinstyle="round", capstyle="round")
        self.create_text(b + u(14), y + b / 2 - 1, text=self.text, anchor="w", fill="#16161a", font=fnt(26))


class LevelMeter(tk.Canvas):
    """-style horizontal level meter with the dB scale (-54 ... 0)."""
    LABELS = (-54, -48, -42, -36, -30, -24, -18, -12, 0)

    def __init__(self, master, w, h, knob_db=-6.0, on_knob=None):
        super().__init__(master, width=w, height=h, bg=PAL["toolbar"], highlightthickness=0)
        self.w, self.h = w, h
        self.lv = [0.0, 0.0]
        self.knob_db, self.on_knob = knob_db, on_knob
        self.x54, self.step = u(60), u(45)
        if on_knob:
            self.bind("<ButtonPress-1>", self._drag)
            self.bind("<B1-Motion>", self._drag)
        self.draw()

    def x(self, db):
        return self.x54 + (db + 54) / 6.0 * self.step

    def _drag(self, ev):
        db = min(-3.0, max(-54.0, -54 + (ev.x - self.x54) / self.step * 6.0))
        self.knob_db = db
        self.on_knob(db)
        self.draw()

    def set_levels(self, l, r):
        new = [max(l, self.lv[0] * .82), max(r, self.lv[1] * .82)]
        if new != self.lv:
            self.lv = new
            self.draw()

    def draw(self):
        self.delete("all")
        h, mid = self.h, self.h / 2
        self.create_rectangle(self.x54 - u(6), 1, self.x(0) + u(24), h - 1, outline=PAL["meter_edge"], fill="")
        self.create_text(u(8), h * .30, text="L", anchor="w", fill="#16161a", font=fnt(21))
        self.create_text(u(8), h * .72, text="R", anchor="w", fill="#16161a", font=fnt(21))
        bt = u(7)
        for k, lvl in enumerate(self.lv):
            if lvl > 1e-5:
                db = max(-54.0, 20 * math.log10(lvl))
                col = "#4caf50" if db < -12 else ("#f0b429" if db < -3 else "#e53935")
                y = u(3) if k == 0 else h - u(3) - bt
                self.create_rectangle(self.x54, y, self.x(db), y + bt, fill=col, outline="")
        for db in self.LABELS:
            x = self.x(db)
            self.create_line(x, mid - u(15), x, mid - u(10), fill="#16161a")
            self.create_text(x, mid + u(2), text=str(db), fill="#16161a", font=fnt(22))
        if self.knob_db is not None:
            x, r = self.x(self.knob_db), u(11)
            self.create_oval(x - r, mid - r, x + r, mid + r, fill="#ffffff", outline="#202024", width=max(2, u(2)))


class ThinScroll(tk.Canvas):
    """style scrollbar: a slim grey bar in a light strip."""

    def __init__(self, master, orient, command, thickness):
        self.horiz = orient == "h"
        super().__init__(master, bg=PAL["status"], highlightthickness=0, bd=0,
                         height=thickness if self.horiz else 1, width=1 if self.horiz else thickness)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self._drag = None
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _len(self):
        return self.winfo_width() if self.horiz else self.winfo_height()

    def _span(self):
        n = max(1, self._len())
        a, b = self.first * n, self.last * n
        if b - a < u(30):
            b = a + u(30)
        return a, min(b, n)

    def _draw(self):
        self.delete("all")
        if self._len() <= 2:
            return
        a, b = self._span()
        th = max(2, u(4))
        if self.horiz:
            cy = self.winfo_height() / 2
            self.create_rectangle(a, cy - th / 2, b, cy + th / 2, fill=PAL["thumb"], outline="")
        else:
            cx = self.winfo_width() / 2
            self.create_rectangle(cx - th / 2, a, cx + th / 2, b, fill=PAL["thumb"], outline="")

    def _press(self, ev):
        pos = ev.x if self.horiz else ev.y
        a, b = self._span()
        if a <= pos <= b:
            self._drag = (pos, self.first)
        else:
            self.command("scroll", 1 if pos > b else -1, "pages")

    def _move(self, ev):
        if self._drag:
            pos = ev.x if self.horiz else ev.y
            self.command("moveto", max(0.0, self._drag[1] + (pos - self._drag[0]) / max(1, self._len())))


# ---------------------------------------------------------------- dialogs
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


def speed_resample(buf, speed):
    """Fast varispeed (changes pitch like tape) for 'play at speed'."""
    n = len(buf)
    m = int(n / speed)
    out = np.empty((m, 2), np.float32)
    step = 1 << 20
    for a in range(0, m, step):
        b = min(m, a + step)
        pos = np.arange(a, b) * speed
        i0 = np.minimum(pos.astype(np.int64), n - 1)
        i1 = np.minimum(i0 + 1, n - 1)
        f = (pos - i0).astype(np.float32)[:, None]
        out[a:b] = buf[i0] * (1 - f) + buf[i1] * f
    return out


GRID_CHOICES = ("Bar", "1/2", "1/4", "1/8", "1/16", "1/32", "1/64",
                "Seconds", "Deciseconds", "Centiseconds", "Milliseconds")
MINOR_DIV = {2: 4, 15: 3, 30: 3, 60: 6, 120: 4, 0.2: 4, 0.02: 4, 0.002: 4}
RULER_STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600,
               900, 1800, 3600]


class App(tk.Tk):
    def __init__(self, files=()):
        super().__init__()
        init_metrics(self.winfo_screenwidth())
        self.title("Studio Audio Editor")
        self.configure(bg=PAL["desk"])
        self.minsize(u(1500), u(800))
        try:
            self.state("zoomed")
        except tk.TclError:
            self._center_window(int(self.winfo_screenwidth() * .92), int(self.winfo_screenheight() * .88))
        self.proj = Project()
        self.player, self.recorder = Player(), Recorder()
        self.px_per_sec = 100.0
        self.view_start = 0  # samples at the left edge of the lane area
        self.cursor = 0
        self.sel = None  # (start, end) samples or None
        self.selected = set()  # selected track indices
        self.recording = False
        self.paused = False
        self.record_origin = 0
        self.play_origin = 0
        self.play_speed = 1.0
        self._drag0 = 0
        self._slider = None
        self._hits = []
        self._last_zoom = None
        self.loop_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Stopped.")
        self.time_var = tk.StringVar(value=fmt_hms(0))
        self.sel_a_var = tk.StringVar(value=fmt_hmsms(0))
        self.sel_b_var = tk.StringVar(value=fmt_hmsms(0))
        self.tempo_var = tk.IntVar(value=120)
        self.ts_num_var = tk.IntVar(value=4)
        self.ts_den_var = tk.StringVar(value="4")
        self.snap_var = tk.BooleanVar(value=True)
        self.grid_var = tk.StringVar(value="1/8")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.out_dev = tk.IntVar(value=-1)
        self.in_dev = tk.IntVar(value=-1)
        self._style()
        self._build_menu()
        self._build_toolbar()
        self._build_bottom()
        self._build_canvas()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        if sd is None:
            self.status_var.set("sounddevice not installed - editing works, playback/record disabled")
        self.after(150, lambda: self._import(list(files)))
        self.after(40, self._tick)

    def _center_window(self, w, h):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(w, sw), min(h, sh)
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # ------------------------------------------------------------ setup
    def _style(self):
        st = ttk.Style(self)
        for th in ("vista", "xpnative", "clam"):
            try:
                st.theme_use(th)
                break
            except tk.TclError:
                continue
        self.option_add("*TCombobox*Listbox.font", fnt(26))

    def _build_menu(self):
        m = tk.Menu(self)
        self.config(menu=m)

        def menu(label, items):
            sub = tk.Menu(m, tearoff=0)
            for it in items:
                if it is None:
                    sub.add_separator()
                elif it[0] == "check":
                    sub.add_checkbutton(label=it[1], variable=it[2], command=it[3], accelerator=it[4])
                else:
                    sub.add_command(label=it[0], command=it[1], accelerator=it[2] if len(it) > 2 else "")
            m.add_cascade(label=label, menu=sub)
            return sub

        menu("File", [
            ("New", self.new_project, "Ctrl+N"), ("Open...", self.open_project, "Ctrl+O"),
            None, ("Save Project", self.save_project, "Ctrl+S"),
            ("Save Project As...", lambda: self.save_project(True)),
            None, ("Import Audio...", self.import_dialog, "Ctrl+Shift+I"),
            ("Export Audio...", self.export_dialog, "Ctrl+Shift+E"),
            ("Export Selected Audio...", lambda: self.export_dialog(True)),
            None, ("Exit", self.quit_app, "Ctrl+Q")])
        menu("Edit", [
            ("Undo", self.undo, "Ctrl+Z"), ("Redo", self.redo, "Ctrl+Y"), None,
            ("Cut", self.edit_cut, "Ctrl+X"), ("Copy", self.edit_copy, "Ctrl+C"),
            ("Paste", self.edit_paste, "Ctrl+V"), ("Delete", self.edit_delete, "Del"), None,
            ("Silence Audio", self.edit_silence, "Ctrl+L"), ("Trim Audio", self.edit_trim, "Ctrl+T")])
        menu("Select", [
            ("All", self.select_all, "Ctrl+A"), ("None", self.select_none, "Ctrl+Shift+A"), None,
            ("Cursor to Start", lambda: self.set_cursor(0), "Home"),
            ("Cursor to End", lambda: self.set_cursor(self.proj.length()), "End")])
        menu("View", [
            ("Zoom In", lambda: self.zoom(2.0), "Ctrl+1"), ("Zoom Out", lambda: self.zoom(0.5), "Ctrl+3"), None,
            ("Fit to Selection", self.zoom_sel), ("Fit to Width", self.zoom_fit, "Ctrl+Shift+F"),
            ("Zoom Toggle", self.zoom_toggle, "Shift+Z")])
        menu("Transport", [
            ("Play/Stop", self.toggle_play, "Space"), ("Play Selection", lambda: self.play(True), "Shift+Space"),
            ("Pause", self.pause, "P"), ("Stop", self.stop, "S"), None,
            ("Record", self.toggle_record, "R"), None,
            ("check", "Loop Playback", self.loop_var, self._loop_changed, "Shift+L"), None,
            ("Skip to Start", lambda: self.set_cursor(0), "Home"),
            ("Skip to End", lambda: self.set_cursor(self.proj.length()), "End")])
        menu("Tracks", [
            ("Add New Track", self.add_track), ("Remove Tracks", self.remove_tracks),
            ("Duplicate Tracks", self.duplicate_tracks), None,
            ("Rename Track...", self.rename_track), ("Track Gain...", self.track_gain),
            ("Track Pan...", self.track_pan), ("Time Shift...", self.track_shift)])
        menu("Generate", [
            ("Silence...", self.gen_silence), ("Tone...", self.gen_tone), ("Noise...", self.gen_noise)])
        self.menu_effect = menu("Effect", [
            ("Amplify...", self.fx_amplify), ("Normalize...", self.fx_normalize), None,
            ("Fade In", lambda: self.run_effect(fx_fade)),
            ("Fade Out", lambda: self.run_effect(lambda x: fx_fade(x, False))),
            ("Reverse", lambda: self.run_effect(fx_reverse)),
            ("Invert", lambda: self.run_effect(fx_invert)),
            ("Remove DC Offset", lambda: self.run_effect(fx_dc)), None,
            ("Change Speed...", self.fx_speed), ("Low-Pass Filter...", self.fx_lowpass),
            ("High-Pass Filter...", self.fx_highpass), None,
            ("Echo...", self.fx_echo), ("Reverb...", self.fx_reverb), ("Compressor...", self.fx_compress)])
        menu("Analyze", [("Spectrogram...", self.show_spectrogram)])
        menu("Tools", [("Reset Zoom", self.zoom_fit)])
        menu("Help", [("Keyboard Shortcuts", self.show_help)])
        self.menu_track = tk.Menu(self, tearoff=0)
        for lab, cmd in (("Rename...", self.rename_track), ("Gain...", self.track_gain),
                         ("Pan...", self.track_pan), ("Time Shift...", self.track_shift),
                         ("Duplicate", self.duplicate_tracks), ("Remove", self.remove_tracks)):
            self.menu_track.add_command(label=lab, command=cmd)

    # ------------------------------------------------------------ toolbars
    def _build_toolbar(self):
        P = PAL
        bar = tk.Frame(self, bg=P["toolbar"])
        bar.pack(side="top", fill="x")
        row = tk.Frame(bar, bg=P["toolbar"])
        row.pack(side="left", padx=(u(10), 0), pady=(u(13), u(11)))
        self.btn = {}

        def grip():
            Gripper(row).pack(side="left", padx=(u(6), u(8)))

        def group():
            f = tk.Frame(row, bg=P["toolbar"])
            f.pack(side="left")
            return f

        # --- transport
        g = group()
        bs = u(103)
        spec = (("pause", p_pause, self.pause, 0), ("play", p_play, self.play, 0),
                ("stop", p_stop, self.stop, 0), ("start", p_skip_start, lambda: self.set_cursor(0), 0),
                ("end", p_skip_end, lambda: self.set_cursor(self.proj.length()), 6),
                ("record", p_record, self.toggle_record, 6), ("loop", p_loop, self._toggle_loop, 0))
        for key, painter, cmd, extra in spec:
            b = IconButton(g, bs, bs, painter, cmd)
            b.pack(side="left", padx=(0, u(9 + extra)))
            self.btn[key] = b
        grip()
        # --- tools (2 x 2)
        g = group()
        ts = (u(49), u(48))
        self.btn["tool_sel"] = IconButton(g, *ts, p_ibeam, lambda: self._tool("select"))
        self.btn["tool_env"] = IconButton(g, *ts, p_envelope, lambda: self._tool("Envelope"))
        self.btn["tool_draw"] = IconButton(g, *ts, p_draw, lambda: self._tool("Draw"))
        self.btn["tool_multi"] = IconButton(g, *ts, p_multi, lambda: self._tool("Multi"))
        for k, key in enumerate(("tool_sel", "tool_env", "tool_draw", "tool_multi")):
            self.btn[key].grid(row=k // 2, column=k % 2, padx=u(1), pady=u(1))
        self.btn["tool_sel"].set(active=True)
        grip()
        # --- zoom / edit / undo
        g = group()
        z = (u(49), u(48))
        cells = (("zin", p_zoom_in, lambda: self.zoom(2.0), 0, 0, False),
                 ("zout", p_zoom_out, lambda: self.zoom(0.5), 0, 1, False),
                 ("zsel", p_zoom_sel, self.zoom_sel, 0, 2, False),
                 ("zfit", p_zoom_fit, self.zoom_fit, 0, 3, False),
                 ("ztog", p_zoom_toggle, self.zoom_toggle, 0, 4, False),
                 ("trim", p_trim, self.edit_trim, 1, 0, True),
                 ("silence", p_silence, self.edit_silence, 1, 1, True),
                 ("undo", p_undo, self.undo, 1, 3, True),
                 ("redo", p_redo, self.redo, 1, 4, True))
        for key, painter, cmd, r, cidx, dim in cells:
            b = IconButton(g, *z, painter, cmd, dim_face=dim)
            b.grid(row=r, column=cidx, padx=u(1), pady=u(1))
            self.btn[key] = b
        grip()
        # --- audio setup
        g = group()
        self.btn["setup"] = IconButton(g, u(176), u(103), p_audio_setup, self.audio_setup,
                                       text="Audio Setup", layout="stacked")
        self.btn["setup"].pack()
        grip()
        # --- share / effects
        g = group()
        self.btn["share"] = IconButton(g, u(246), u(50), p_share, lambda: self.export_dialog(),
                                       text="Share Audio", layout="inline")
        self.btn["effects"] = IconButton(g, u(246), u(50), p_plug, self._effects_popup,
                                         text="Get Effects", layout="inline")
        self.btn["share"].grid(row=0, column=0, pady=u(1))
        self.btn["effects"].grid(row=1, column=0, pady=u(1))
        grip()
        # --- meters
        g = group()
        self.meter_in = LevelMeter(g, u(470), u(48), knob_db=-7.0)
        self.meter_out = LevelMeter(g, u(470), u(48), knob_db=-3.0, on_knob=self._set_volume)
        for r, (icon, meter) in enumerate(((p_mic, self.meter_in), (p_speaker, self.meter_out))):
            IconButton(g, u(46), u(48), icon, None).grid(row=r, column=0, padx=(0, u(6)), pady=u(1))
            meter.grid(row=r, column=1, pady=u(1))

    def _build_bottom(self):
        P = PAL
        status = tk.Frame(self, bg=P["status"])
        status.pack(side="bottom", fill="x")
        tk.Frame(status, bg="#dfdfe4", height=max(1, u(2))).pack(fill="x")
        srow = tk.Frame(status, bg=P["status"], height=u(44))
        srow.pack(fill="x")
        srow.pack_propagate(False)
        cell = tk.Frame(srow, bg=P["status"], width=u(335))
        cell.pack(side="left", fill="y")
        cell.pack_propagate(False)
        tk.Label(cell, textvariable=self.status_var, anchor="w", bg=P["status"], fg="#16161a",
                 font=fnt(25)).pack(fill="both", padx=u(14))
        tk.Frame(srow, bg="#c8c8cd", width=max(1, u(2))).pack(side="left", fill="y")

        bt = tk.Frame(self, bg=P["bottom"])
        bt.pack(side="bottom", fill="x")
        tk.Frame(bt, bg="#ffffff", height=max(1, u(2))).pack(fill="x")
        inner = tk.Frame(bt, bg=P["bottom"])
        inner.pack(side="left", padx=(u(8), 0), pady=(u(10), u(12)))
        bgc = P["bottom"]

        def grip(pad=(u(6), u(8))):
            Gripper(inner, bg=bgc).pack(side="left", padx=pad)

        def sep():
            tk.Frame(inner, bg=P["sep"], width=max(1, u(2))).pack(side="left", fill="y", padx=u(14))

        def col():
            f = tk.Frame(inner, bg=bgc)
            f.pack(side="left")
            return f

        lab = lambda p, t: tk.Label(p, text=t, bg=bgc, fg="#16161a", font=fnt(25))
        # tempo
        grip()
        g = col()
        lab(g, "Tempo").pack(anchor="w")
        ttk.Spinbox(g, from_=1, to=999, textvariable=self.tempo_var, width=5, font=fnt(26)).pack(anchor="w", pady=u(4))
        tk.Frame(inner, bg=bgc, width=u(26)).pack(side="left")
        # time signature
        g = col()
        lab(g, "Time Signature").pack(anchor="w")
        r = tk.Frame(g, bg=bgc)
        r.pack(anchor="w", pady=u(4))
        ttk.Spinbox(r, from_=1, to=64, textvariable=self.ts_num_var, width=3, font=fnt(26)).pack(side="left")
        lab(r, " / ").pack(side="left")
        ttk.Combobox(r, textvariable=self.ts_den_var, values=("2", "4", "8", "16"), width=3, state="readonly",
                     font=fnt(26)).pack(side="left")
        sep()
        # snap
        grip(pad=(0, u(8)))
        g = col()
        CheckBox(g, "Snap", self.snap_var).pack(anchor="w")
        ttk.Combobox(g, textvariable=self.grid_var, values=GRID_CHOICES, width=14, state="readonly",
                     font=fnt(26)).pack(anchor="w", pady=u(4))
        sep()
        # big time display
        grip(pad=(0, u(8)))
        tf = tk.Frame(inner, bg=P["time_bg"], highlightthickness=max(1, u(2)), highlightbackground="#8a8c9c")
        tf.pack(side="left")
        tk.Label(tf, textvariable=self.time_var, bg=P["time_bg"], fg="#ffffff", font=fnt(60),
                 padx=u(10)).pack(side="left", pady=u(6))
        tk.Label(tf, text="\u25BE", bg=P["time_bg"], fg="#ffffff", font=fnt(30)).pack(side="left", padx=(0, u(6)))
        sep()
        # selection
        grip(pad=(0, u(8)))
        g = col()
        lab(g, "Selection").grid(row=0, column=0, sticky="e", padx=(0, u(12)))
        gear = IconButton(g, u(48), u(44), p_gear, None, bg=bgc)
        gear.grid(row=1, column=0, sticky="e", padx=(0, u(12)), pady=(u(6), 0))
        for rr, var in enumerate((self.sel_a_var, self.sel_b_var)):
            f = tk.Frame(g, bg=P["time_bg"], highlightthickness=max(1, u(2)), highlightbackground="#8a8c9c")
            f.grid(row=rr, column=1, pady=u(2))
            tk.Label(f, textvariable=var, bg=P["time_bg"], fg="#ffffff", font=fnt(29), padx=u(8)).pack(side="left")
            tk.Label(f, text="\u25BE", bg=P["time_bg"], fg="#ffffff", font=fnt(24)).pack(side="left", padx=(0, u(4)))
        sep()
        # playback speed
        grip(pad=(0, u(8)))
        g = col()
        self.btn["speed"] = IconButton(g, u(74), u(66), p_play_speed, self._play_speed, bg=bgc)
        self.btn["speed"].grid(row=0, column=0, rowspan=2, padx=(0, u(10)))
        lab(g, "\u2013").grid(row=0, column=1, sticky="w")
        lab(g, "+").grid(row=0, column=2, sticky="e")
        ttk.Scale(g, from_=0.25, to=3.0, variable=self.speed_var, length=u(300)).grid(row=1, column=1, columnspan=2)

    def _build_canvas(self):
        box = tk.Frame(self, bg=PAL["desk"])
        box.pack(side="top", fill="both", expand=True)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)
        sbw = u(26)
        self.ruler = tk.Canvas(box, height=RULER_H, bg=PAL["ruler"], highlightthickness=0)
        self.canvas = tk.Canvas(box, bg=PAL["desk"], highlightthickness=0)
        self.vbar = ThinScroll(box, "v", lambda *a: self.canvas.yview(*a), sbw)
        self.hbar = ThinScroll(box, "h", self._hscroll, u(30))
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.ruler.grid(row=0, column=0, sticky="ew")
        tk.Frame(box, bg=PAL["ruler"], width=sbw).grid(row=0, column=1, sticky="ns")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.vbar.grid(row=1, column=1, sticky="ns")
        self.hbar.grid(row=2, column=0, sticky="ew")
        tk.Frame(box, bg=PAL["status"], width=sbw).grid(row=2, column=1, sticky="ns")
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
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
                w = self.focus_get()
                if not typing_ok and w is not None and w.winfo_class() in (
                        "Entry", "TEntry", "TSpinbox", "TCombobox", "Spinbox"):
                    return None
                fn()
                return "break"

            self.bind_all(seq, handler)

        key("<space>", self.toggle_play)
        key("<Shift-space>", lambda: self.play(True))
        key("<Key-p>", self.pause)
        key("<Key-s>", self.stop)
        key("<Key-r>", self.toggle_record)
        key("<Control-z>", self.undo, True)
        key("<Control-y>", self.redo, True)
        key("<Control-x>", self.edit_cut)
        key("<Control-c>", self.edit_copy)
        key("<Control-v>", self.edit_paste)
        key("<Delete>", self.edit_delete)
        key("<Control-a>", self.select_all)
        key("<Control-Shift-A>", self.select_none)
        key("<Control-l>", self.edit_silence)
        key("<Control-t>", self.edit_trim)
        key("<Control-n>", self.new_project, True)
        key("<Control-s>", self.save_project, True)
        key("<Control-o>", self.open_project, True)
        key("<Control-q>", self.quit_app, True)
        key("<Control-Shift-E>", self.export_dialog, True)
        key("<Control-Shift-I>", self.import_dialog, True)
        key("<Control-Key-1>", lambda: self.zoom(2.0), True)
        key("<Control-Key-3>", lambda: self.zoom(0.5), True)
        key("<Control-Shift-F>", self.zoom_fit, True)
        key("<Home>", lambda: self.set_cursor(0))
        key("<End>", lambda: self.set_cursor(self.proj.length()))

    # ------------------------------------------------------------ helpers
    def ask(self, title, fields):
        return ParamDialog(self, title, fields).result

    def targets(self):
        idx = sorted(i for i in self.selected if i < len(self.proj.tracks))
        return [self.proj.tracks[i] for i in (idx or range(len(self.proj.tracks)))]

    def lane_w(self):
        return max(100, self.canvas.winfo_width() - HEAD_W - RIGHT_M)

    def _grid_seconds(self):
        try:
            tempo = max(1.0, float(self.tempo_var.get()))
            num = max(1, int(self.ts_num_var.get()))
            den = int(self.ts_den_var.get())
        except (tk.TclError, ValueError):
            tempo, num, den = 120.0, 4, 4
        beat = 60.0 / tempo
        g = self.grid_var.get()
        fixed = {"Seconds": 1.0, "Deciseconds": .1, "Centiseconds": .01, "Milliseconds": .001}
        if g in fixed:
            return fixed[g]
        if g == "Bar":
            return beat * num
        try:
            return beat * den / int(g.split("/")[1])
        except (IndexError, ValueError):
            return 0.0

    def _snap(self, s):
        if not self.snap_var.get():
            return s
        g = self._grid_seconds()
        if g <= 0:
            return s
        r = self.proj.rate
        return max(0, int(round(round(s / r / g) * g * r)))

    def _x2s(self, x, snap=True):
        s = max(0, int(self.view_start + (x - HEAD_W) / self.px_per_sec * self.proj.rate))
        return self._snap(s) if snap else s

    def _s2x(self, s):
        return HEAD_W + (s - self.view_start) / self.proj.rate * self.px_per_sec

    def _visible_samples(self):
        return self.lane_w() / self.px_per_sec * self.proj.rate

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

    def _has_sel(self):
        return bool(self.sel and self.sel[1] > self.sel[0])

    def _tool(self, name):
        if name != "select":
            self.status_var.set(f"The {name} tool is not available in this editor.")

    def _loop_changed(self):
        self.player.loop = self.loop_var.get()
        self._refresh_toolbar()

    def _toggle_loop(self):
        self.loop_var.set(not self.loop_var.get())
        self._loop_changed()

    def _set_volume(self, db):
        self.player.volume = 10 ** ((db + 3.0) / 20.0)

    def _effects_popup(self):
        b = self.btn["effects"]
        self.menu_effect.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def audio_setup(self):
        m = tk.Menu(self, tearoff=0)
        if sd is None:
            m.add_command(label="sounddevice is not installed", state="disabled")
        else:
            try:
                devs = sd.query_devices()
            except Exception as ex:
                devs = []
                m.add_command(label=str(ex), state="disabled")
            m.add_command(label="Playback device", state="disabled")
            for i, d in enumerate(devs):
                if d["max_output_channels"] > 0:
                    m.add_radiobutton(label=d["name"], variable=self.out_dev, value=i,
                                      command=lambda: self._pick_device(1, self.out_dev.get()))
            m.add_separator()
            m.add_command(label="Recording device", state="disabled")
            for i, d in enumerate(devs):
                if d["max_input_channels"] > 0:
                    m.add_radiobutton(label=d["name"], variable=self.in_dev, value=i,
                                      command=lambda: self._pick_device(0, self.in_dev.get()))
        b = self.btn["setup"]
        m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _pick_device(self, which, idx):
        try:
            cur = list(sd.default.device)
            cur[which] = idx
            sd.default.device = tuple(cur)
        except Exception as ex:
            messagebox.showerror("Audio Setup", str(ex))

    def _refresh_toolbar(self):
        b = self.btn
        playing = self.player.stream is not None
        b["stop"].set(enabled=playing or self.recording or self.paused)
        b["pause"].set(active=self.paused)
        b["play"].set(active=playing and not self.recording)
        b["record"].set(active=self.recording)
        b["loop"].set(active=self.loop_var.get())
        b["undo"].set(enabled=bool(self.proj._undo))
        b["redo"].set(enabled=bool(self.proj._redo))
        b["trim"].set(enabled=self._has_sel())
        b["silence"].set(enabled=self._has_sel())

    # ------------------------------------------------------------ drawing
    def redraw(self):
        c = self.canvas
        c.delete("all")
        W = max(c.winfo_width(), HEAD_W + 50)
        total_h = max(len(self.proj.tracks) * LANE_H, c.winfo_height())
        self._hits = []
        for i, t in enumerate(self.proj.tracks):
            self._draw_track(i, t, W)
        c.configure(scrollregion=(0, 0, W, total_h))
        self._draw_ruler(W)
        self._update_hbar()
        self._update_time_displays()
        self._refresh_toolbar()

    def _update_time_displays(self):
        r = self.proj.rate
        if not (self.recording or self.player.stream is not None):
            self.time_var.set(fmt_hms(self.cursor / r))
        a, b = self.sel if self._has_sel() else (self.cursor, self.cursor)
        self.sel_a_var.set(fmt_hmsms(a / r))
        self.sel_b_var.set(fmt_hmsms(b / r))

    def _slider_x(self, kind, t, x1, x2):
        if kind == "gain":
            db = 20 * math.log10(max(t.gain, 1e-6))
            frac = (min(36.0, max(-36.0, db)) + 36.0) / 72.0
        else:
            frac = (t.pan + 1.0) / 2.0
        return x1 + frac * (x2 - x1)

    def _draw_track(self, i, t, W):
        c, P = self.canvas, PAL
        y0 = i * LANE_H
        sel = i in self.selected
        xl, xr = FR_L, W - FR_R
        yt, yb = y0 + u(2), y0 + LANE_H - u(2)
        c.create_rectangle(xl, yt, xr, yb, fill=P["black"], outline="")
        c.create_rectangle(xl + LINE, yt + LINE, xr - LINE, yb - LINE,
                           fill=P["frame_sel"] if sel else P["frame"], outline="")
        c.create_rectangle(xl + LINE + BAND, yt + LINE + BAND, xr - LINE - BAND, yb - LINE - BAND,
                           fill=P["black"], outline="")
        cx1, cy1, cx2, cy2 = xl + INSET, yt + INSET, xr - INSET, yb - INSET
        black = P["black"]

        # ---------------- control panel
        c.create_rectangle(cx1, cy1, HEAD_W, cy2, fill=P["panel_sel"] if sel else P["panel"], outline="")
        c.create_line(HEADER_W, cy1, HEADER_W, cy2, fill=black, width=LINE)
        c.create_line(HEAD_W, cy1, HEAD_W, cy2, fill=black, width=LINE)
        ink = P["icon"]
        ry = yt + u(35)
        xc = u(37)  # close "x"
        k = u(8)
        c.create_line(xc - k, ry - k, xc + k, ry + k, fill=ink, width=max(1, u(2)))
        c.create_line(xc - k, ry + k, xc + k, ry - k, fill=ink, width=max(1, u(2)))
        self._hits.append((u(22), ry - u(16), u(54), ry + u(16), "close", i, None))
        caret_x, dots_x = u(252), u(288)
        name_font = fnt(26)
        c.create_text(u(60), ry, text=ellipsize(t.name, name_font, caret_x - u(60) - u(18)), anchor="w",
                      fill=ink, font=name_font)
        c.create_line(caret_x - u(8), ry + u(4), caret_x, ry - u(4), caret_x + u(8), ry + u(4), fill=ink,
                      width=max(1, u(2)))
        for dx in (-u(8), 0, u(8)):
            c.create_oval(dots_x + dx - u(2.5), ry - u(2.5), dots_x + dx + u(2.5), ry + u(2.5), fill=ink, outline="")
        self._hits.append((caret_x - u(16), ry - u(16), HEADER_W, ry + u(16), "menu", i, None))
        # mute / solo
        gain_on = (("Mute", t.mute, P["mute"], "mute", u(20), u(162)),
                   ("Solo", t.solo, P["solo"], "solo", u(167), u(304)))
        for lab, on, col, kind, bx1, bx2 in gain_on:
            by1, by2 = yt + u(57), yt + u(92)
            rrect(c, bx1, by1, bx2, by2, u(5), fill=col if on else P["ctl"], outline="")
            c.create_text((bx1 + bx2) / 2, (by1 + by2) / 2, text=lab, fill=ink, font=fnt(26))
            self._hits.append((bx1, by1, bx2, by2, kind, i, None))
        by1, by2 = yt + u(101), yt + u(143)
        rrect(c, u(20), by1, u(304), by2, u(5), fill=P["ctl"], outline="")
        c.create_text((u(20) + u(304)) / 2, (by1 + by2) / 2, text="Effects", fill=ink, font=fnt(26))
        self._hits.append((u(20), by1, u(304), by2, "effects", i, None))
        # gain / pan sliders
        sx1, sx2 = u(31), u(294)
        for kind, y_line, lab_l, lab_r in (("gain", yt + u(177), "\u2013", "+"), ("pan", yt + u(236), "L", "R")):
            c.create_text(sx1 + u(3), y_line - u(23), text=lab_l, anchor="w", fill=ink, font=fnt(20, True))
            c.create_text(sx2 - u(3), y_line - u(23), text=lab_r, anchor="e", fill=ink, font=fnt(20, True))
            c.create_line(sx1, y_line, sx2, y_line, fill="#9aa6bb", width=max(1, u(2)))
            for n in range(13):
                tx = sx1 + (sx2 - sx1) * n / 12.0
                c.create_line(tx, y_line - u(4), tx, y_line + u(4), fill="#9aa6bb")
            kx, kr = self._slider_x(kind, t, sx1, sx2), u(12)
            c.create_oval(kx - kr, y_line - kr, kx + kr, y_line + kr, fill="#ffffff", outline="#303036",
                          width=max(1, u(2)))
            self._hits.append((sx1 - u(14), y_line - u(22), sx2 + u(14), y_line + u(22), kind, i, (sx1, sx2)))

        # ---------------- geometry of the two channels
        ch_total = cy2 - cy1 - HDR_H
        chh = (ch_total - LINE) / 2.0
        ch_top = (cy1 + HDR_H, cy1 + HDR_H + chh + LINE)
        half_h = chh / 2.0 - 1

        # amplitude scale strip
        for ch in (0, 1):
            ct = ch_top[ch]
            cyc = ct + chh / 2.0
            for val, lab in ((1.0, "1.0"), (0.5, "0.5"), (0.0, "0.0"), (-0.5, "-0.5"), (-1.0, "-1.0")):
                yy = cyc - val * half_h
                ty = min(max(yy, ct + u(15)), ct + chh - u(15))
                c.create_text(HEAD_W - u(14), ty, text=lab, anchor="e", fill=ink,
                              font=fnt(24, bold=val in (1.0, 0.0, -1.0)))
                c.create_line(HEAD_W - u(9), yy, HEAD_W, yy, fill=ink)

        # ---------------- lane
        c.create_rectangle(HEAD_W, cy1, cx2, cy2, fill=P["empty"], outline="")
        has_sel = self._has_sel() and (sel or not self.selected)
        if has_sel:
            x1, x2 = max(HEAD_W, self._s2x(self.sel[0])), min(cx2, self._s2x(self.sel[1]))
            if x2 > x1:
                c.create_rectangle(x1, cy1, x2, cy2, fill=P["empty_sel"], outline="")
        ca, cb = max(HEAD_W, self._s2x(t.offset)), min(cx2, self._s2x(t.end))
        if len(t.data) and cb > ca:
            for ch in (0, 1):
                c.create_rectangle(ca, ch_top[ch], cb, ch_top[ch] + chh, fill=P["clip_bg"], outline="")
            sel_x = None
            if has_sel:
                a, b = max(ca, self._s2x(self.sel[0])), min(cb, self._s2x(self.sel[1]))
                if b > a:
                    sel_x = (a, b)
                    for ch in (0, 1):
                        c.create_rectangle(a, ch_top[ch], b, ch_top[ch] + chh, fill=P["clip_sel"], outline="")
            self._draw_wave(t, ch_top, chh, ca, cb, sel_x)
            # clip title bar
            c.create_rectangle(ca, cy1, cb, cy1 + HDR_H, fill=P["clip_hdr"], outline="")
            c.create_line(ca, cy1, cb, cy1, fill="#14151a", width=LINE)
            hfont = fnt(26)
            c.create_text(ca + u(8), cy1 + HDR_H / 2 + 1, text=ellipsize(t.name, hfont, max(10, cb - ca - u(70))),
                          anchor="w", fill=ink, font=hfont)
            for dx in (-u(8), 0, u(8)):
                if cb - ca > u(60):
                    c.create_oval(cb - u(26) + dx - u(2.5), cy1 + HDR_H / 2 - u(2.5),
                                  cb - u(26) + dx + u(2.5), cy1 + HDR_H / 2 + u(2.5), fill=ink, outline="")
            if ca <= HEAD_W + 1 or True:
                c.create_line(ca, cy1, ca, cy2, fill="#14151a", width=LINE)
            if self._s2x(t.end) <= cx2:
                c.create_line(cb, cy1, cb, cy2, fill="#14151a", width=LINE)
        # channel divider
        ydiv = ch_top[0] + chh + LINE / 2.0
        c.create_line(HEADER_W, ydiv, cx2, ydiv, fill=black, width=LINE)
        # edit cursor
        if not self._has_sel():
            x = self._s2x(self.cursor)
            if HEAD_W <= x <= cx2:
                c.create_line(x, cy1, x, cy2, fill=P["cursor"], width=max(1, u(2)))

    def _draw_wave(self, t, ch_top, chh, ca, cb, sel_x):
        c, rate, P = self.canvas, self.proj.rate, PAL
        width = int(cb - ca)
        if width <= 0 or len(t.data) == 0:
            return
        spp = rate / self.px_per_sec  # samples per pixel
        first = self.view_start - t.offset  # local index at HEAD_W
        start_local = first + (ca - HEAD_W) * spp
        lo = max(0, int(start_local))
        hi = min(len(t.data), int(start_local + width * spp) + 2)
        if hi <= lo:
            return
        seg = t.data[lo:hi]
        col = P["wave_mute"] if t.mute else P["wave"]
        col_sel = P["wave_mute"] if t.mute else P["wave_sel"]
        half = chh / 2.0 - 1
        lw = max(1, u(2))

        def parts(xs):
            if sel_x is None:
                return [(np.ones(len(xs), bool), col)]
            a, b = sel_x
            return [(xs < a, col), ((xs >= a) & (xs <= b), col_sel), (xs > b, col)]

        for ch in (0, 1):
            cy = ch_top[ch] + chh / 2.0
            if spp < 1.5:  # zoomed far in: draw the samples themselves
                xs = HEAD_W + ((lo + np.arange(len(seg))) - first) / spp
                ys = cy - np.clip(seg[:, ch], -1, 1) * half
                for m, colr in parts(xs):
                    if m.sum() >= 2:
                        c.create_line(np.column_stack([xs[m], ys[m]]).ravel().tolist(), fill=colr, width=lw)
                if spp < 0.25 and len(seg) < 2500:
                    for x, y in zip(xs.tolist(), ys.tolist()):
                        colr = col_sel if (sel_x and sel_x[0] <= x <= sel_x[1]) else col
                        c.create_line(x, cy, x, y, fill=colr)
                        c.create_oval(x - u(4), y - u(4), x + u(4), y + u(4), fill=colr, outline=colr)
                continue
            cols = np.arange(width)
            idx = ((cols * spp + start_local) - lo).astype(np.int64)
            ok = (idx >= 0) & (idx < len(seg))
            idx, xs = idx[ok], ca + cols[ok]
            if len(idx) < 2:
                continue
            mx = np.clip(np.maximum.reduceat(seg[:, ch], idx), -1, 1)
            mn = np.clip(np.minimum.reduceat(seg[:, ch], idx), -1, 1)
            top = cy - mx * half
            bot = np.maximum(cy - mn * half, top + 1)
            for m, colr in parts(xs):
                if m.sum() >= 2:
                    pts = np.concatenate([np.column_stack([xs[m], top[m]]),
                                          np.column_stack([xs[m], bot[m]])[::-1]])
                    c.create_polygon(pts.ravel().tolist(), fill=colr, outline=colr)

    def _draw_ruler(self, W):
        r, rate, P = self.ruler, self.proj.rate, PAL
        r.delete("all")
        right = W - RIGHT_M
        r.create_rectangle(0, 0, W + 100, RULER_H, fill=P["ruler"], outline="")
        rrect(r, u(26), u(4), u(76), u(52), u(7), fill=P["btn"], outline=P["btn"])
        r.create_text(u(51), u(28), text="\u2699", fill=P["icon"], font=("Segoe UI Symbol", -u(34)))
        pps = self.px_per_sec
        step = next((s for s in RULER_STEPS if s * pps >= u(170)), RULER_STEPS[-1])
        nmin = MINOR_DIV.get(step, 5)
        t0 = self.view_start / rate
        k = int(math.floor(t0 / step))
        tend = t0 + (right - HEAD_W) / pps
        base = RULER_H - LINE
        font = fnt(24, True)
        while k * step <= tend + step:
            t = k * step
            x = HEAD_W + (t - t0) * pps
            for j in range(1, nmin):
                xm = x + j * step / nmin * pps
                if HEAD_W <= xm <= right:
                    r.create_line(xm, base - u(9), xm, base, fill=P["ruler_tick"])
            if t >= -1e-9 and HEAD_W - 1 <= x <= right + 1:
                r.create_line(x, base - u(20), x, base, fill=P["ruler_tick"], width=max(1, u(2)))
                r.create_text(x + u(7), u(20), text=self._ruler_label(t, step), anchor="w",
                              fill=P["ruler_fg"], font=font)
            k += 1
        r.create_line(0, RULER_H - 1, W + 100, RULER_H - 1, fill="#000000", width=LINE)
        cx = self._s2x(self.cursor)
        if HEAD_W <= cx <= right:
            r.create_polygon(cx - u(8), RULER_H - u(17), cx + u(8), RULER_H - u(17), cx, RULER_H - LINE,
                             fill=P["play"], outline="")

    @staticmethod
    def _ruler_label(t, step):
        if t >= 3600:
            return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(round(t % 60)):02d}"
        if t >= 60 or step >= 60:
            m, s = int(t // 60), t - 60 * int(t // 60)
            return f"{m}:{int(round(s)):02d}" if step >= 1 else f"{m}:{s:06.3f}"
        if step >= 1:
            return f"{int(round(t))}"
        dec = 1 if step >= 0.1 else (2 if step >= 0.01 else 3)
        return f"{t:.{dec}f}"

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
        new = min(max(old * factor, 0.5), 20000.0)
        xc = (c - self.view_start) / rate * old
        self.px_per_sec = new
        self.view_start = int(max(0, c - xc / new * rate))
        self.redraw()

    def zoom_fit(self):
        n = self.proj.length()
        w = max(100, self.lane_w() - u(2))
        if n:
            self.px_per_sec = max(0.5, w / (n / self.proj.rate))
        self.view_start = 0
        self.redraw()

    def zoom_sel(self):
        if not self._has_sel():
            return
        w = max(100, self.lane_w() - u(2))
        self.px_per_sec = min(20000.0, w / ((self.sel[1] - self.sel[0]) / self.proj.rate))
        self.view_start = self.sel[0]
        self.redraw()

    def zoom_toggle(self):
        if self._last_zoom is None:
            self._last_zoom = (self.px_per_sec, self.view_start)
            self.zoom_fit()
        else:
            self.px_per_sec, self.view_start = self._last_zoom
            self._last_zoom = None
            self.redraw()

    # ------------------------------------------------------------ mouse
    def _wheel(self, ev):
        d = 1 if (getattr(ev, "delta", 0) > 0 or getattr(ev, "num", 0) == 4) else -1
        if ev.state & 0x4:  # Ctrl = zoom
            self.zoom(2.0 if d > 0 else 0.5, self._x2s(ev.x, False))
        elif ev.state & 0x1:  # Shift = scroll sideways
            self._hscroll("scroll", -d, "units")
        else:
            self.canvas.yview_scroll(-d, "units")

    def _on_press(self, ev):
        if self.recording:
            return
        self._slider = None
        on_canvas = ev.widget is self.canvas
        y = self.canvas.canvasy(ev.y) if on_canvas else 0
        i = int(y // LANE_H) if on_canvas else -1
        if on_canvas and ev.x < HEADER_W:
            return self._header_click(i, ev, y)
        if on_canvas and ev.x < HEAD_W:
            return  # amplitude scale strip
        s = self._x2s(ev.x)
        self._drag0, self.cursor, self.sel = s, s, None
        if 0 <= i < len(self.proj.tracks):
            if ev.state & 0x4:
                self.selected ^= {i}
            else:
                self.selected = {i}
        self.redraw()

    def _on_drag(self, ev):
        if self._slider:
            kind, i, sx1, sx2 = self._slider
            if i < len(self.proj.tracks):
                t = self.proj.tracks[i]
                frac = min(1.0, max(0.0, (ev.x - sx1) / float(sx2 - sx1)))
                if kind == "gain":
                    db = -36.0 + 72.0 * frac
                    t.gain = 1.0 if abs(db) < 1.2 else 10 ** (db / 20.0)
                else:
                    p = -1.0 + 2.0 * frac
                    t.pan = 0.0 if abs(p) < 0.04 else p
                self.redraw()
            return
        if self.recording or ev.x < 0:
            return
        s = self._x2s(ev.x)
        a, b = sorted((self._drag0, s))
        self.cursor, self.sel = a, ((a, b) if b > a else None)
        self.redraw()

    def _on_release(self, _ev):
        self._slider = None

    def _header_click(self, i, ev, y):
        if not (0 <= i < len(self.proj.tracks)):
            return
        t = self.proj.tracks[i]
        for x1, y1, x2, y2, kind, ti, extra in self._hits:
            if ti != i or not (x1 <= ev.x <= x2 and y1 <= y <= y2):
                continue
            if kind == "close":
                self.proj.push_undo()
                del self.proj.tracks[i]
                self.selected = {j - (j > i) for j in self.selected if j != i}
                return self.after_edit()
            if kind == "menu":
                self.selected = {i}
                self.redraw()
                return self.menu_track.tk_popup(ev.x_root, ev.y_root)
            if kind == "effects":
                self.selected = {i}
                self.redraw()
                return self.menu_effect.tk_popup(ev.x_root, ev.y_root)
            if kind in ("mute", "solo"):
                self.proj.push_undo()
                setattr(t, kind, not getattr(t, kind))
                return self.redraw()
            if kind in ("gain", "pan"):
                self.proj.push_undo()
                self._slider = (kind, i, extra[0], extra[1])
                return self._on_drag(ev)
        if ev.state & 0x4:
            self.selected ^= {i}
        else:
            self.selected = {i}
        self.redraw()

    def _on_double(self, ev):
        y = self.canvas.canvasy(ev.y)
        ry = y - int(y // LANE_H) * LANE_H
        if ev.x < HEADER_W and u(14) <= ry <= u(56) and ev.x > u(56):
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
    def _play_range(self, selection_only=False, resume=False):
        n = self.proj.length()
        if resume:
            end = self.sel[1] if self._has_sel() else n
            return (self.cursor if self.cursor < end else 0), end
        if self._has_sel():
            return self.sel
        s = self.cursor if self.cursor < n else 0
        return s, n

    def play(self, selection_only=False, speed=1.0, resume=False):
        if self.recording:
            return
        s, e = self._play_range(selection_only, resume)
        if e <= s:
            return
        try:
            buf = self.proj.mixdown(s, e)
            if abs(speed - 1.0) > 1e-3:
                buf = speed_resample(buf, speed)
            self.player.start(buf, self.proj.rate, self.loop_var.get())
        except Exception as ex:
            messagebox.showerror("Playback", str(ex))
            return
        self.play_origin, self.play_speed, self.paused = s, speed, False
        self.status_var.set("Playing.")
        self._refresh_toolbar()

    def _play_speed(self):
        self.play(speed=max(0.25, min(3.0, float(self.speed_var.get()))))

    def toggle_play(self):
        if self.recording:
            return self.toggle_record()
        if self.player.stream is not None or self.paused:
            self.stop()
        else:
            self.play()

    def pause(self):
        if self.recording:
            return
        if self.player.stream is not None:
            self.cursor = int(self.play_origin + self.player.pos * self.play_speed)
            self.player.stop()
            self.paused = True
            self.status_var.set("Paused.")
            self.redraw()
        elif self.paused:
            self.play(speed=self.play_speed, resume=True)

    def stop(self):
        if self.recording:
            return self._stop_record()
        if self.player.stream is not None:
            self.cursor = int(self.play_origin + self.player.pos * self.play_speed)
            self.player.stop()
        self.paused = False
        self.status_var.set("Stopped.")
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
        self.status_var.set("Recording.")
        self._refresh_toolbar()

    def _stop_record(self):
        data = self.recorder.stop()
        self.recording = False
        if len(data):
            self.proj.push_undo()
            self.proj.new_track(f"Recording {len(self.proj.tracks) + 1}", data, self.record_origin)
            self.selected = {len(self.proj.tracks) - 1}
            self.cursor = self.record_origin + len(data)
        self.status_var.set("Stopped.")
        self.redraw()

    def _tick(self):
        try:
            r = self.proj.rate
            if self.recording:
                self.time_var.set(fmt_hms(self.record_origin / r + self.recorder.frames / r))
                lv = self.recorder.level
                self.meter_in.set_levels(lv, lv)
            else:
                self.meter_in.set_levels(0.0, 0.0)
            if self.player.stream is not None:
                pos = self.play_origin + self.player.pos * self.play_speed
                self.time_var.set(fmt_hms(pos / r))
                l, rr = self.player.level()
                self.meter_out.set_levels(l, rr)
                if not self.player.playing:  # reached the end
                    self.cursor = int(pos)
                    self.player.stop()
                    self.status_var.set("Stopped.")
                    self.redraw()
                else:
                    x = self._s2x(pos)
                    if x > self.canvas.winfo_width() - RIGHT_M - u(10) or x < HEAD_W:
                        self.view_start = int(pos)
                        self.redraw()
                        x = self._s2x(pos)
                    self.canvas.delete("play")
                    for i in range(len(self.proj.tracks)):
                        y0 = i * LANE_H + u(2) + INSET
                        self.canvas.create_line(x, y0, x, y0 + LANE_H - u(4) - 2 * INSET, fill=PAL["play"],
                                                width=max(2, u(3)), tags="play")
            else:
                self.meter_out.set_levels(0.0, 0.0)
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

    def edit_copy(self):
        if self._has_sel():
            self.proj.clip = [t.copy_range(*self.sel) for t in self.targets()]
            self.status_var.set("Copied.")

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
        self.config(cursor="watch")
        self.update_idletasks()
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
        self._fx_dialog("Compressor",
                        [("th", "Threshold (dB)", -18.0), ("ra", "Ratio", 3.0), ("mk", "Make-up gain (dB)", 3.0)],
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
                                       ("f", "Frequency (Hz)", 440.0), ("s", "Seconds", 2.0),
                                       ("a", "Amplitude (0-1)", 0.5)])
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
                self.proj.push_undo()
                self._one().name = name
                self.after_edit()

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
        before = len(self.proj.tracks)
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            for p in paths:
                try:
                    self.proj.import_file(p)
                except Exception as ex:
                    messagebox.showerror("Import failed", f"{os.path.basename(p)}\n\n{ex}")
        finally:
            self.config(cursor="")
        self.selected = set(range(before, len(self.proj.tracks)))
        if first:
            self.zoom_fit()
        self.after_edit()

    def import_dialog(self):
        self._import(list(filedialog.askopenfilenames(
            title="Import audio",
            filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.mp4 *.wma"), ("All", "*.*")])))

    def export_dialog(self, selection=False):
        if not self.proj.tracks:
            return
        if selection and not self._has_sel():
            return messagebox.showinfo("Export selection", "Select a region first.")
        path = filedialog.asksaveasfilename(
            title="Export audio", defaultextension=".wav",
            filetypes=[("WAV", "*.wav"), ("MP3", "*.mp3"), ("FLAC", "*.flac"), ("OGG", "*.ogg"),
                       ("M4A (ffmpeg)", "*.m4a")])
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
                p = Project()
                p.load(path)
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
        cv.image = img  # keep a reference
        nyq = self.proj.rate / 2
        for k in range(5):
            y = 6 + k * (img.height() - 1) / 4
            cv.create_text(54, y, text=f"{nyq * (1 - k / 4) / 1000:.1f}k", anchor="e", fill="#ccc",
                           font=("Segoe UI", 8))
        cv.create_text(60, img.height() + 14, anchor="w", fill="#ccc", font=("Segoe UI", 8),
                       text=f"0 s  ...  {(b - a) / self.proj.rate:.2f} s")

    def show_help(self):
        messagebox.showinfo("Keyboard shortcuts", (
            "Space  play / stop           Shift+Space  play selection\n"
            "P  pause                     S  stop                R  record\n"
            "Ctrl+Z / Ctrl+Y  undo / redo\n"
            "Ctrl+X / C / V  cut / copy / paste\n"
            "Delete  delete selection     Ctrl+A  select all\n"
            "Ctrl+L  silence audio        Ctrl+T  trim audio\n"
            "Ctrl+Shift+I  import   Ctrl+Shift+E  export   Ctrl+S  save   Ctrl+O  open\n"
            "Ctrl+1 / Ctrl+3  zoom in / out        Ctrl+Shift+F  fit to width\n"
            "Home / End  go to start / end\n\n"
            "Mouse: click or drag in a track to set cursor / select.\n"
            "Ctrl+wheel = zoom, Shift+wheel = scroll sideways.\n"
            "Drag the Gain / Pan sliders in a track's control panel.\n"
            "Ctrl+click selects several tracks; edits apply to selected tracks\n"
            "(or all tracks if none is selected)."))


def main():
    try:  # sharper text on Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()