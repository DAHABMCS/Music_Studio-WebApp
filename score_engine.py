#!/usr/bin/env python3
"""
score_engine.py - MIDI -> professional sheet music PDFs (Guitar Pro / MuseScore style)
======================================================================================

Drop next to app.py.  Pure Python: needs only  reportlab + mido + numpy  (all already
required by Music Studio).  No MuseScore / LilyPond / music21 needed.

What it draws (all vector, no music font required)
  * Standard notation: treble / bass / grand staff / percussion staff, key signature,
    time signature, tempo mark ("Moderate = 155"), bar numbers, ties, beams, rests,
    multi-bar rests, accidentals, chord symbols, triplets (swing / shuffle feel).
  * TAB under guitar and bass notation (fret numbers, tied notes in brackets).
  * Drum notation with a drum-number TAB row (36 = kick, 38 = snare, 42/51 = hats/ride).
  * Vocal sheet: melody with a lyric line under the notes + a Vocal Data Sheet
    (range, tessitura, pitch chart, timed lyrics, full note table).

Main entry point used by app.py:

    import score_engine as se
    result = se.generate_sheets(out_dir, parts, title, tempo_bpm, srt_text=..., log=print)

`parts` = list of dicts  {key, kind, midi, time_mode}
    kind      : drums | guitar_solo | guitar_rhythm | guitar | bass | piano | other | vocals
    time_mode : "ticks"   -> MIDI is already on a beat grid (Midi_refine.py output)
                "seconds" -> MIDI is in real time (transcription_extras / MUSIC.py output)
"""
from __future__ import annotations

import bisect
import csv
import math
import os
import re
from dataclasses import dataclass, field

import numpy as np

SP = 5.5                    # staff space (pt)
TSP = 7.0                   # TAB line spacing (pt)
HEAD_W = SP * 1.3
NAT_PC = [0, 2, 4, 5, 7, 9, 11]
SHARP_ORDER = [3, 0, 4, 1, 5, 2, 6]
FLAT_ORDER = [6, 2, 5, 1, 4, 0, 3]
NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
MAJOR_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}
KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

TUNINGS = {"guitar": [64, 59, 55, 50, 45, 40],      # string 1 (high e) first
           "bass": [43, 38, 33, 28]}

# General-MIDI drum -> (staff step, head).  step 0 = bottom line of the 5-line staff.
DRUM_STAFF = {35: (1, "o"), 36: (1, "o"), 38: (5, "o"), 40: (5, "o"), 37: (5, "x"), 39: (5, "x"),
              41: (2, "o"), 43: (3, "o"), 45: (4, "o"), 47: (6, "o"), 48: (7, "o"), 50: (7, "o"),
              42: (9, "x"), 44: (-1, "x"), 46: (9, "xo"), 49: (10, "x"), 57: (10, "x"),
              51: (9, "x"), 59: (9, "x"), 53: (9, "x"), 52: (10, "x"), 55: (10, "x"), 56: (6, "x")}
PLAUSIBLE = {"vocals": (40, 88), "bass": (23, 64), "guitar_solo": (38, 100),
             "guitar_rhythm": (38, 100), "guitar": (38, 100), "piano": (21, 108), "other": (21, 108)}
CYMBALS = {42, 44, 46, 49, 51, 52, 53, 55, 57, 59}


def midi_name(p, flats=False):
    return f"{(NAMES_FLAT if flats else NAMES_SHARP)[int(p) % 12]}{int(p) // 12 - 1}"


def midi_hz(p):
    return 440.0 * 2 ** ((p - 69) / 12.0)


def fmt_clock(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def tempo_word(bpm):
    for lim, w in ((40, "Grave"), (60, "Largo"), (66, "Larghetto"), (76, "Adagio"), (108, "Andante"),
                   (120, "Moderate"), (156, "Allegro"), (176, "Vivace")):
        if bpm < lim:
            return w
    return "Presto"


# =====================================================================
#  MODEL
# =====================================================================
@dataclass
class N:
    sec0: float
    sec1: float
    b0: float                  # start in beats (bar 1 beat 1 == 0.0 after alignment)
    b1: float
    pitch: int
    vel: int = 80
    lyric: str = ""


@dataclass
class Ev:
    pos: int                   # grid-unit position inside the bar
    dur: int                   # grid units
    pitches: list              # [] = rest
    tie: bool = False          # tied into the next event
    tie_in: bool = False       # tied from the previous event
    lyric: str = ""
    full_rest: bool = False
    chord: str = ""
    tabs: list = field(default_factory=list)      # [(string_idx, fret)] aligned with pitches


@dataclass
class Staff:
    clef: str                  # treble | bass | perc
    bars: list


KINDS = {
    "drums": dict(label="Drums", abbr="drm.", mode="drums", clef="perc", tab="drums"),
    "guitar_solo": dict(label="Lead Guitar - Solo", abbr="gtr.", mode="mono", clef="treble",
                        tab="guitar", transpose=12),
    "guitar_rhythm": dict(label="Rhythm Guitar - Chords", abbr="rhy.", mode="poly", clef="treble",
                          tab="guitar", transpose=12, chords=True, max_poly=6),
    "guitar": dict(label="Guitar", abbr="gtr.", mode="poly", clef="treble", tab="guitar",
                   transpose=12, chords=True, max_poly=6),
    "bass": dict(label="Bass Guitar", abbr="bass", mode="mono", clef="bass", tab="bass", transpose=12),
    "piano": dict(label="Piano", abbr="pno.", mode="poly", clef="grand", max_poly=4),
    "other": dict(label="Keys / Synths / Other", abbr="syn.", mode="poly", clef="grand", max_poly=4),
    "vocals": dict(label="Vocal Melody with Lyrics", abbr="voc.", mode="mono", clef="treble", lyrics=True),
}


# =====================================================================
#  KEY / SPELLING
# =====================================================================
def key_accs(f):
    acc = [0] * 7
    if f > 0:
        for L in SHARP_ORDER[:f]:
            acc[L] = 1
    elif f < 0:
        for L in FLAT_ORDER[:-f]:
            acc[L] = -1
    return acc


def spelling_table(f):
    acc = key_accs(f)
    table = {}
    for L in range(7):
        table[(NAT_PC[L] + acc[L]) % 12] = (L, acc[L])
    for pc in range(12):
        if pc in table:
            continue
        if pc in NAT_PC:
            table[pc] = (NAT_PC.index(pc), 0)
        elif f >= 0:
            table[pc] = (NAT_PC.index((pc - 1) % 12), 1)
        else:
            table[pc] = (NAT_PC.index((pc + 1) % 12), -1)
    return table


def spell(pitch, table):
    L, a = table[pitch % 12]
    return L, a, (pitch - a) // 12 - 1


def detect_key(notes):
    """Krumhansl-Schmuckler on duration-weighted pitch classes -> (tonic, mode, fifths, name)."""
    hist = np.zeros(12)
    for n in notes:
        hist[n.pitch % 12] += max(0.05, n.sec1 - n.sec0) * (0.5 + n.vel / 127)
    if hist.sum() <= 0:
        return 0, "major", 0, "C major"
    best, tonic, mode = -2.0, 0, "major"
    for t in range(12):
        for prof, m in ((KS_MAJOR, "major"), (KS_MINOR, "minor")):
            c = np.corrcoef(hist, np.roll(prof, t))[0, 1]
            if c > best:
                best, tonic, mode = c, t, m
    mt = tonic if mode == "major" else (tonic + 3) % 12
    f = MAJOR_FIFTHS[mt]
    return tonic, mode, f, f"{(NAMES_FLAT if f < 0 else NAMES_SHARP)[tonic]} {mode}"


# =====================================================================
#  CHORD NAMES + TAB FRETTING
# =====================================================================
CHORD_TPL = [("", (0, 4, 7)), ("m", (0, 3, 7)), ("7", (0, 4, 7, 10)), ("maj7", (0, 4, 7, 11)),
             ("m7", (0, 3, 7, 10)), ("dim", (0, 3, 6)), ("aug", (0, 4, 8)), ("sus4", (0, 5, 7)),
             ("sus2", (0, 2, 7)), ("5", (0, 7)), ("6", (0, 4, 7, 9)), ("m6", (0, 3, 7, 9))]


def chord_name(pitches, flats=False):
    pcs = {p % 12 for p in pitches}
    if len(pcs) < 2:
        return ""
    bass = min(pitches) % 12
    best, name = -99.0, ""
    for r in range(12):
        for suf, tpl in CHORD_TPL:
            T = {(r + i) % 12 for i in tpl}
            m, miss, extra = len(pcs & T), len(T - pcs), len(pcs - T)
            if m < 2:
                continue
            s = 2.0 * m - 1.6 * miss - 1.3 * extra + (0.9 if r == bass else 0) - 0.05 * len(tpl)
            if s > best:
                best, name = s, f"{(NAMES_FLAT if flats else NAMES_SHARP)[r]}{suf}"
    return name if best >= 2.0 else ""


def tab_assign(pitches, tuning, prev):
    """Choose (string_index, fret) for each pitch, minimising hand movement. -> (list, new_prev)"""
    cands = []
    for p in pitches:
        q = p
        while q < min(tuning) and q + 12 <= 127:
            q += 12
        while q > max(tuning) + 22:
            q -= 12
        opts = [(i, q - s) for i, s in enumerate(tuning) if 0 <= q - s <= 22]
        if not opts:
            opts = [(0, max(0, min(22, q - tuning[0])))]
        cands.append(opts)
    best, best_cost = None, 1e9

    def rec(i, used, cur, fr):
        nonlocal best, best_cost
        if i == len(cands):
            fretted = [f for f in fr if f > 0]
            span = (max(fretted) - min(fretted)) if fretted else 0
            mean = (sum(fretted) / len(fretted)) if fretted else prev
            cost = 4.0 * max(0, span - 3) + 0.35 * abs(mean - prev) + 0.04 * sum(fr)
            if cost < best_cost:
                best, best_cost = list(cur), cost
            return
        for s, f in cands[i]:
            if s in used:
                continue
            used.add(s)
            cur.append((s, f))
            fr.append(f)
            rec(i + 1, used, cur, fr)
            fr.pop()
            cur.pop()
            used.discard(s)
    if len(pitches) > 6:
        pitches = pitches[:6]
        cands = cands[:6]
    rec(0, set(), [], [])
    if best is None:                       # more notes than strings: keep distinct strings only
        best = [cands[i][0] for i in range(len(cands))]
    fretted = [f for _, f in best if f > 0]
    return best, (min(fretted) if fretted else prev)


# =====================================================================
#  GRID: quantise notes -> bars of events   (upb = grid units per beat: 4 = 16ths, 3 = triplets)
# =====================================================================
def decompose(p, length, rest, upb):
    """Split `length` grid units starting at bar position p into printable note values."""
    if upb == 4:
        allowed = (16, 8, 4, 2, 1) if rest else (16, 12, 8, 6, 4, 3, 2, 1)

        def can(d, pos):
            if d == 1:
                return True
            if d in (2, 3, 4, 6):
                return pos % 2 == 0 and (not rest or d != 4 or pos % 4 == 0)
            return pos % 4 == 0
    else:
        allowed = (12, 6, 3, 2, 1) if rest else (12, 9, 6, 3, 2, 1)

        def can(d, pos):
            if d == 1:
                return True
            if d == 2:
                return pos % 3 in (0, 1)
            if d == 3:
                return pos % 3 == 0
            if d == 6:
                return pos % 6 == 0
            if d == 9:
                return pos % 3 == 0
            return pos == 0
    out = []
    while length > 0:
        for d in allowed:
            if d <= length and can(d, p):
                out.append((p, d))
                p += d
                length -= d
                break
        else:
            out.append((p, 1))
            p += 1
            length -= 1
    return out


def events_to_bars(events, bar_units, n_bars, upb):
    """events: sorted, non-overlapping (u0, u1, pitches, lyric)  ->  list[list[Ev]] with rests and ties."""
    total = n_bars * bar_units
    flat, t = [], 0
    for u0, u1, pit, lyr in events:
        u0, u1 = max(u0, t), min(u1, total)
        if u1 <= u0:
            continue
        if u0 > t:
            flat.append((t, u0, [], ""))
        flat.append((u0, u1, pit, lyr))
        t = u1
    if t < total:
        flat.append((t, total, [], ""))
    bars = [[] for _ in range(n_bars)]
    for a, b, pit, lyr in flat:
        pieces = []
        while a < b:
            bar = a // bar_units
            end = min(b, (bar + 1) * bar_units)
            if not pit and a == bar * bar_units and end - a == bar_units:
                pieces.append((bar, Ev(0, bar_units, [], full_rest=True)))
            else:
                for pp, d in decompose(a - bar * bar_units, end - a, not pit, upb):
                    pieces.append((bar, Ev(pp, d, list(pit))))
            a = end
        for j, (bar, ev) in enumerate(pieces):
            if pit:
                ev.tie = j < len(pieces) - 1
                ev.tie_in = j > 0
                ev.lyric = lyr if j == 0 else ""
            bars[bar].append(ev)
    return bars


def _u(b, upb):
    return int(round(b * upb))


def quantize_mono(notes, upb, absorb):
    items = []
    for n in sorted(notes, key=lambda n: (n.b0, -n.vel)):
        u0, u1 = _u(n.b0, upb), _u(n.b1, upb)
        if u0 < 0:
            continue
        u1 = max(u1, u0 + 1)
        if items and u0 == items[-1][0]:
            if n.vel > items[-1][4]:
                items[-1] = [u0, u1, [n.pitch], n.lyric, n.vel]
            continue
        if items and u0 < items[-1][1]:
            items[-1][1] = u0
        items.append([u0, u1, [n.pitch], n.lyric, n.vel])
    if absorb:                                   # noisy audio-derived MIDI: close tiny gaps
        for i in range(len(items) - 1):
            if 0 < items[i + 1][0] - items[i][1] <= 1:
                items[i][1] = items[i + 1][0]
    return [(a, b, p, ly) for a, b, p, ly, _v in items if b > a]


def quantize_poly(notes, upb, max_notes, absorb):
    buckets = {}
    for n in notes:
        u0, u1 = _u(n.b0, upb), _u(n.b1, upb)
        if u0 < 0:
            continue
        buckets.setdefault(u0, []).append((n.vel, n.pitch, max(u1, u0 + 1)))
    keys = sorted(buckets)
    out = []
    for i, u0 in enumerate(keys):
        chord = sorted(buckets[u0], reverse=True)[:max_notes]
        u1 = int(np.median([c[2] for c in chord]))
        if i + 1 < len(keys):
            u1 = min(u1, keys[i + 1])
            if absorb and 0 < keys[i + 1] - u1 <= 1:
                u1 = keys[i + 1]
        out.append((u0, max(u1, u0 + 1), sorted({c[1] for c in chord}), ""))
    return out


def quantize_drums(notes, upb):
    buckets = {}
    for n in notes:
        u0 = _u(n.b0, upb)
        if u0 >= 0:
            buckets.setdefault(u0, set()).add(n.pitch)
    keys = sorted(buckets)
    out = []
    for i, u0 in enumerate(keys):
        gap = (keys[i + 1] - u0) if i + 1 < len(keys) else upb
        if upb == 4:
            d = 1 if gap == 1 else (2 if gap < 4 else 4)
        else:
            d = 1 if gap == 1 else (2 if gap == 2 else 3)
        out.append((u0, u0 + d, sorted(buckets[u0]), ""))
    return out


def build_staves(cfg, notes, bar_units, n_bars, upb, absorb, flats):
    mode, clef = cfg["mode"], cfg["clef"]
    tr = cfg.get("transpose", 0)
    maxn = cfg.get("max_poly", 4)

    def lift(ev_list):                            # transpose for written pitch
        return [(a, b, [p + tr for p in ps], ly) for a, b, ps, ly in ev_list]
    if mode == "drums":
        return [Staff("perc", events_to_bars(quantize_drums(notes, upb), bar_units, n_bars, upb))]
    if clef == "grand":
        hi = [n for n in notes if n.pitch >= 60]
        lo = [n for n in notes if n.pitch < 60]
        return [Staff("treble", events_to_bars(quantize_poly(hi, upb, maxn, absorb), bar_units, n_bars, upb)),
                Staff("bass", events_to_bars(quantize_poly(lo, upb, maxn, absorb), bar_units, n_bars, upb))]
    ev = quantize_mono(notes, upb, absorb) if mode == "mono" else quantize_poly(notes, upb, maxn, absorb)
    staves = [Staff(clef, events_to_bars(ev, bar_units, n_bars, upb))]
    # chord symbols + TAB use the *sounding* pitches; notation uses the written (transposed) pitches
    tab = cfg.get("tab")
    if tab in ("guitar", "bass"):
        tuning = TUNINGS[tab]
        prev = 3
        prev_name = ""
        for bar in staves[0].bars:
            for e in bar:
                if e.pitches:
                    e.tabs, prev = tab_assign(e.pitches, tuning, prev)
                    if cfg.get("chords") and len(e.pitches) >= 3:
                        nm = chord_name(e.pitches, flats)
                        if nm and nm != prev_name:
                            e.chord = nm
                        prev_name = nm or prev_name
    if tr:
        for bar in staves[0].bars:
            for e in bar:
                e.pitches = [p + tr for p in e.pitches]
    return staves


# =====================================================================
#  FONTS + VECTOR GLYPHS  (all drawn as paths - no music font needed)
# =====================================================================
_FONT_CACHE = {}


def _fonts():
    """-> (regular, bold, unicode_ok).  Registers a Unicode TTF when one is found."""
    if "f" in _FONT_CACHE:
        return _FONT_CACHE["f"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    cands = [
        ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ]
    for reg, bold in cands:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont("SAE-Reg", reg))
                pdfmetrics.registerFont(TTFont("SAE-Bold", bold if os.path.exists(bold) else reg))
                _FONT_CACHE["f"] = ("SAE-Reg", "SAE-Bold", True)
                return _FONT_CACHE["f"]
            except Exception:
                continue
    _FONT_CACHE["f"] = ("Helvetica", "Helvetica-Bold", False)
    return _FONT_CACHE["f"]


def shape_text(s):
    """Make text safe for the PDF: shape Arabic if the helpers exist, drop unprintable chars."""
    _reg, _bold, uni = _fonts()
    if not s:
        return ""
    if re.search(r"[\u0600-\u06FF]", s):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            s = get_display(arabic_reshaper.reshape(s))
        except Exception:
            pass
    if not uni:
        s = s.encode("latin-1", "replace").decode("latin-1")
    return s


class PdfDoc:
    """Landscape-A4 canvas wrapper with a running footer."""

    def __init__(self, path, title, footer):
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4, landscape
        self.W, self.H = landscape(A4)
        self.c = canvas.Canvas(path, pagesize=(self.W, self.H))
        self.c.setTitle(title)
        self.c.setAuthor("Studio Audio Editor")
        self.title, self.footer, self.page = title, footer, 1
        self.margin = 36
        self.top = self.H - 40
        self.bottom = 40
        self.reg, self.bold, _ = _fonts()

    def _footer(self):
        c = self.c
        c.setFont(self.reg, 7)
        c.setFillGray(0.45)
        c.drawString(self.margin, 22, shape_text(self.footer))
        c.drawRightString(self.W - self.margin, 22, f"{shape_text(self.title)}  -  page {self.page}")
        c.setFillGray(0)

    def new_page(self):
        self._footer()
        self.c.showPage()
        self.page += 1

    def finish(self):
        self._footer()
        self.c.save()


# ---------- vector glyphs ----------
def g_head(c, x, y, kind):
    c.saveState()
    c.translate(x, y)
    c.rotate(18)
    w, h = HEAD_W / 2, SP * 0.46
    if kind == "whole":
        c.setLineWidth(1.5)
        c.ellipse(-w * 1.12, -h, w * 1.12, h, stroke=1, fill=0)
    elif kind == "open":
        c.setLineWidth(1.15)
        c.ellipse(-w, -h, w, h, stroke=1, fill=0)
    else:
        c.ellipse(-w, -h, w, h, stroke=0, fill=1)
    c.restoreState()


def g_xhead(c, x, y):
    d = SP * 0.5
    c.setLineWidth(1.1)
    c.line(x - d, y - d, x + d, y + d)
    c.line(x - d, y + d, x + d, y - d)


def g_sharp(c, x, y):
    c.setLineWidth(0.8)
    c.line(x - 1.5, y - SP * 1.05, x - 1.5, y + SP * 1.2)
    c.line(x + 1.5, y - SP * 1.2, x + 1.5, y + SP * 1.05)
    c.setLineWidth(1.7)
    c.line(x - 3.3, y + 0.30 * SP, x + 3.3, y + 0.62 * SP)
    c.line(x - 3.3, y - 0.62 * SP, x + 3.3, y - 0.30 * SP)


def g_flat(c, x, y):
    c.setLineWidth(0.9)
    c.line(x - 1.6, y - SP * 0.9, x - 1.6, y + SP * 1.6)
    p = c.beginPath()
    p.moveTo(x - 1.6, y - SP * 0.9)
    p.curveTo(x + 4.2, y - SP * 0.3, x + 3.4, y + SP * 0.9, x - 1.6, y + SP * 0.45)
    c.setLineWidth(1.0)
    c.drawPath(p, stroke=1, fill=0)


def g_natural(c, x, y):
    c.setLineWidth(0.8)
    c.line(x - 1.6, y - SP * 0.4, x - 1.6, y + SP * 1.5)
    c.line(x + 1.6, y - SP * 1.5, x + 1.6, y + SP * 0.4)
    c.setLineWidth(1.4)
    c.line(x - 1.6, y - 0.1 * SP, x + 1.6, y - 0.4 * SP)
    c.line(x - 1.6, y + 0.45 * SP, x + 1.6, y + 0.15 * SP)


def g_acc(c, kind, x, y):
    {1: g_sharp, -1: g_flat, 0: g_natural}[kind](c, x, y)


def g_treble(c, x, yb):
    u = SP

    def P(a, b):
        return (x + a * u, yb + b * u)
    c.saveState()
    c.setLineWidth(1.45)
    c.setLineCap(1)
    c.setLineJoin(1)
    p = c.beginPath()
    p.moveTo(*P(-0.35, -1.05))
    seq = [((-0.80, -1.70), (0.40, -2.00), (0.50, -0.85)),
           ((0.62, 0.30), (0.50, 2.20), (0.42, 3.80)),
           ((0.34, 5.15), (0.52, 6.15), (0.85, 5.95)),
           ((1.25, 5.60), (1.20, 4.40), (0.60, 3.30)),
           ((-0.20, 2.40), (-1.00, 2.00), (-0.95, 1.10)),
           ((-0.90, -0.05), (0.40, -0.35), (0.95, 0.50)),
           ((1.35, 1.20), (0.90, 2.00), (0.30, 1.90)),
           ((-0.10, 1.80), (-0.30, 1.20), (0.20, 0.95))]
    for a, b, d in seq:
        p.curveTo(*P(*a), *P(*b), *P(*d))
    c.drawPath(p, stroke=1, fill=0)
    c.circle(*P(-0.38, -1.05), 0.32 * u, stroke=0, fill=1)
    c.restoreState()


def g_bass(c, x, yb):
    u = SP

    def P(a, b):
        return (x + a * u, yb + b * u)
    c.saveState()
    c.setLineWidth(1.6)
    c.setLineCap(1)
    p = c.beginPath()
    p.moveTo(*P(0.15, 3.0))
    p.curveTo(*P(0.55, 4.15), *P(1.85, 4.0), *P(1.65, 2.5))
    p.curveTo(*P(1.5, 1.2), *P(0.7, 0.4), *P(-0.1, 0.0))
    c.drawPath(p, stroke=1, fill=0)
    c.circle(*P(0.12, 3.0), 0.34 * u, stroke=0, fill=1)
    c.circle(*P(2.25, 3.5), 0.22 * u, stroke=0, fill=1)
    c.circle(*P(2.25, 2.5), 0.22 * u, stroke=0, fill=1)
    c.restoreState()


def g_perc(c, x, yb):
    c.saveState()
    c.setLineWidth(2.2)
    c.line(x + 1, yb + 1.5 * SP, x + 1, yb + 2.5 * SP)
    c.line(x + 6, yb + 1.5 * SP, x + 6, yb + 2.5 * SP)
    c.restoreState()


def g_rest(c, x, yb, d, full=False):
    """Rests: whole/half as blocks, quarter as a zig-zag, 8th/16th as flagged strokes."""
    u = SP
    mid = yb + 2 * u
    c.saveState()
    if full or d == 16:
        c.rect(x - u * 0.7, yb + 3 * u - u * 0.5, u * 1.4, u * 0.5, stroke=0, fill=1)
    elif d == 8:
        c.rect(x - u * 0.7, yb + 2 * u, u * 1.4, u * 0.5, stroke=0, fill=1)
    elif d == 4:
        c.setLineWidth(1.7)
        c.setLineCap(1)
        c.setLineJoin(1)
        p = c.beginPath()
        p.moveTo(x + 0.55 * u, mid + 1.55 * u)
        p.lineTo(x - 0.45 * u, mid + 0.65 * u)
        p.lineTo(x + 0.45 * u, mid - 0.25 * u)
        p.lineTo(x - 0.45 * u, mid - 1.05 * u)
        p.curveTo(x + 0.9 * u, mid - 1.05 * u, x + 0.8 * u, mid - 1.9 * u, x - 0.3 * u, mid - 1.55 * u)
        c.drawPath(p, stroke=1, fill=0)
    else:
        n = 1 if d == 2 else 2
        top = mid + 1.1 * u
        bot = mid - 1.15 * u - (n - 1) * 1.0 * u
        c.setLineWidth(1.2)
        c.setLineCap(1)
        c.line(x + 0.55 * u, top, x - 0.35 * u - (n - 1) * 0.45 * u, bot)
        for i in range(n):
            dx = x - 0.5 * u - i * 0.45 * u
            dy = mid + 0.45 * u - i * 1.0 * u
            c.circle(dx, dy, 0.3 * u, stroke=0, fill=1)
            p = c.beginPath()
            p.moveTo(dx, dy)
            p.curveTo(dx + 0.5 * u, dy - 0.45 * u, dx + 1.0 * u, dy - 0.05 * u, dx + 1.05 * u - i * 0.0, dy + 0.6 * u)
            c.drawPath(p, stroke=1, fill=0)
    c.restoreState()


def g_tie(c, x1, x2, y, up, flat=False):
    if x2 - x1 < 4:
        return
    h = min(SP * (0.7 if flat else 1.3), (x2 - x1) * 0.28) * (1 if up else -1)
    p = c.beginPath()
    p.moveTo(x1, y)
    p.curveTo(x1 + (x2 - x1) * 0.25, y + h * 1.35, x1 + (x2 - x1) * 0.75, y + h * 1.35, x2, y)
    p.curveTo(x1 + (x2 - x1) * 0.75, y + h * 1.0, x1 + (x2 - x1) * 0.25, y + h * 1.0, x1, y)
    p.close()
    c.drawPath(p, stroke=0, fill=1)




# =====================================================================
#  NOTE SHAPES / LAYOUT
# =====================================================================
def shape(dur, upb):
    """-> (head, flags, dots) for a duration expressed in grid units."""
    if upb == 4:
        return {1: ("n", 2, 0), 2: ("n", 1, 0), 3: ("n", 1, 1), 4: ("n", 0, 0), 6: ("n", 0, 1),
                8: ("open", 0, 0), 12: ("open", 0, 1), 16: ("whole", 0, 0)}.get(dur, ("n", 0, 0))
    return {1: ("n", 1, 0), 2: ("n", 0, 0), 3: ("n", 0, 0), 6: ("open", 0, 0),
            9: ("open", 0, 1), 12: ("whole", 0, 0)}.get(dur, ("n", 0, 0))


def rest_code(dur, upb):
    if upb == 4:
        return dur if dur in (16, 8, 4, 2, 1) else 4
    return {12: 16, 6: 8, 3: 4, 2: 4, 1: 2}.get(dur, 4)


@dataclass
class Cell:
    bar: int
    n: int = 1
    xs: dict = field(default_factory=dict)
    width: float = 0.0
    multirest: bool = False


def _tw(text, size, bold=False):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    reg, bd, _ = _fonts()
    return stringWidth(shape_text(text), bd if bold else reg, size)


def _need(ev, perc, upb):
    if not ev.pitches:
        return 11.0
    w = HEAD_W + 3.5
    if shape(ev.dur, upb)[2]:
        w += 4
    if not perc and any(p % 12 in (1, 3, 6, 8, 10) for p in ev.pitches):
        w += 6.5
    if ev.lyric:
        w = max(w, _tw(ev.lyric, 7.5) + 3.5)
    if ev.chord:
        w = max(w, _tw(ev.chord, 8.5, True) + 4)
    if ev.tabs:
        w = max(w, 12)
    return w


def layout_cells(staves, n_bars, bar_units, upb):
    perc = staves[0].clef == "perc"
    prop = 6.4 if upb == 4 else 8.5
    cells, b = [], 0

    def empty(bar):
        return all(len(st.bars[bar]) == 1 and st.bars[bar][0].full_rest for st in staves)
    while b < n_bars:
        if empty(b):
            run = b
            while run < n_bars and empty(run):
                run += 1
            if run - b >= 2:
                cells.append(Cell(b, run - b, {}, 64.0, True))
                b = run
                continue
        poss = sorted({e.pos for st in staves for e in st.bars[b]})
        xs, prev_x, prev_need = {}, None, 0.0
        for pos in poss:
            need = max(_need(e, perc, upb) for st in staves for e in st.bars[b] if e.pos == pos)
            x = 14 + pos * prop
            if prev_x is not None:
                x = max(x, prev_x + prev_need)
            xs[pos] = x
            prev_x, prev_need = x, need
        width = max(14 + bar_units * prop + 6, (prev_x or 0) + prev_need + 6)
        if len(poss) == 1 and staves[0].bars[b][0].full_rest:
            width = max(width, 70)
        cells.append(Cell(b, 1, xs, width))
        b += 1
    return cells


def pack_systems(cells, avail_first, avail_rest, max_bars=5):
    systems, cur, w = [], [], 0.0
    for cell in cells:
        avail = avail_first if not systems else avail_rest
        if cur and (w + cell.width > avail or len(cur) >= max_bars):
            systems.append((cur, w))
            cur, w = [], 0.0
        cur.append(cell)
        w += cell.width
    if cur:
        systems.append((cur, w))
    return systems


SHARP_STEPS = {"treble": [8, 5, 9, 6, 3, 7, 4], "bass": [6, 3, 7, 4, 1, 5, 2]}
FLAT_STEPS = {"treble": [4, 7, 3, 6, 2, 5, 1], "bass": [2, 5, 1, 4, 0, 3, 6]}
CLEF_BOTTOM_D = {"treble": 4 * 7 + 2, "bass": 2 * 7 + 4}


def _prep(ev, clef, table, acc_state, keyacc):
    heads = []
    if clef == "perc":
        for p in sorted(ev.pitches):
            step, kind = DRUM_STAFF.get(p, (5, "o"))
            heads.append(dict(step=step, acc=None, kind=kind, pitch=p))
        return heads, True
    for p in sorted(ev.pitches):
        L, a, octv = spell(p, table)
        step = octv * 7 + L - CLEF_BOTTOM_D[clef]
        cur = acc_state.get((L, octv), keyacc[L])
        show = None
        if a != cur:
            show = a
            acc_state[(L, octv)] = a
        heads.append(dict(step=step, acc=show, kind="n", pitch=p))
    avg = sum(h["step"] for h in heads) / len(heads)
    return heads, avg < 4


@dataclass
class Ctx:
    title: str
    tempo: float
    upb: int
    fifths: int
    key_name: str
    n_bars: int
    footer: str = "Auto-transcribed by Music Studio - check against the recording."


# =====================================================================
#  THE DRAWER
# =====================================================================
class SheetDrawer:
    def __init__(self, doc, ctx, cfg):
        self.doc, self.c, self.ctx, self.cfg = doc, doc.c, ctx, cfg
        self.upb = ctx.upb
        self.bar_units = 4 * ctx.upb
        self.table = spelling_table(ctx.fifths)
        self.keyacc = key_accs(ctx.fifths)
        self.tab = cfg.get("tab")
        self.lyrics = bool(cfg.get("lyrics"))
        self.n_tab = {"guitar": 6, "drums": 6, "bass": 4}.get(self.tab, 0)

    # ---------------- header ----------------
    def header(self, y, subtitle):
        d, c, ctx = self.doc, self.c, self.ctx
        c.setFont(d.bold, 22)
        c.drawCentredString(d.W / 2, y, shape_text(ctx.title))
        c.setFont(d.reg, 13)
        c.setFillGray(0.25)
        c.drawCentredString(d.W / 2, y - 19, shape_text(subtitle))
        c.setFont(d.reg, 8)
        c.setFillGray(0.45)
        c.drawRightString(d.W - d.margin, y, "Music Studio")
        c.drawRightString(d.W - d.margin, y - 11, f"Key: {ctx.key_name}   Time: 4/4")
        c.setFillGray(0)
        ty = y - 46
        x = d.margin + 4
        c.setFont("Times-Bold", 13)
        word = tempo_word(ctx.tempo)
        c.drawString(x, ty, word)
        x += c.stringWidth(word, "Times-Bold", 13) + 8
        g_head(c, x + 3, ty + 2.6, "n")
        c.setLineWidth(1.0)
        c.line(x + 3 + HEAD_W / 2 - 0.4, ty + 3, x + 3 + HEAD_W / 2 - 0.4, ty + 13)
        c.drawString(x + 14, ty, f"= {ctx.tempo:.0f}")
        if ctx.upb == 3:
            c.setFont("Times-Italic", 9)
            c.setFillGray(0.3)
            c.drawString(x + 14 + c.stringWidth(f"= {ctx.tempo:.0f}", "Times-Bold", 13) + 12, ty + 1,
                         "(triplet / swing feel: eighths = quarter + eighth of a triplet)")
            c.setFillGray(0)
        return ty - 16

    def system_height(self, n_staves):
        h = 38 + 4 * SP
        if n_staves == 2:
            h += 34 + 4 * SP
        if self.n_tab:
            h += 42 + (self.n_tab - 1) * TSP
        elif self.lyrics:
            h += 24
        return h + 20

    def _head_width(self, clef, first):
        f = self.ctx.fifths
        w = 4 + (22 if clef != "perc" else 14)
        if clef != "perc":
            w += abs(f) * 7.5 + 4
        if first:
            w += 18
        return w + 4

    # ---------------- one system ----------------
    def _draw_frame(self, y_top, staves, cells, scale, x0, first):
        c, d, ctx = self.c, self.doc, self.ctx
        n = len(staves)
        ys = [y_top - 38]
        if n == 2:
            ys.append(ys[0] - 4 * SP - 34)
        yb = [y - 4 * SP for y in ys]
        tab_top = (yb[-1] - 42) if self.n_tab else None
        tab_bot = (tab_top - (self.n_tab - 1) * TSP) if self.n_tab else None
        head_w = self._head_width(staves[0].clef, first)
        x_end = x0 + head_w + sum(ce.width for ce in cells) * scale
        c.setLineWidth(0.55)
        for i in range(n):
            for k in range(5):
                c.line(x0, yb[i] + k * SP, x_end, yb[i] + k * SP)
        if self.n_tab:
            for k in range(self.n_tab):
                c.line(x0, tab_top - k * TSP, x_end, tab_top - k * TSP)
        # left system line(s) + brace
        c.setLineWidth(1.2)
        c.line(x0, ys[0], x0, yb[-1])
        if self.n_tab:
            c.line(x0, tab_top, x0, tab_bot)
        if n == 2:
            self._brace(x0 - 3, ys[0], yb[1])
        # clef / key / time
        f = ctx.fifths
        for i, st in enumerate(staves):
            cx = x0 + 4
            {"treble": g_treble, "bass": g_bass, "perc": g_perc}[st.clef](c, cx, yb[i])
            if self.cfg.get("transpose") and st.clef in ("treble", "bass") and n == 1:
                c.setFont(self.doc.reg, 6.5)
                c.drawCentredString(cx + 8, yb[i] - 8 if st.clef == "treble" else yb[i] - 4, "8")
            kx = cx + (22 if st.clef != "perc" else 14)
            if st.clef != "perc":
                steps = (SHARP_STEPS if f > 0 else FLAT_STEPS)[st.clef]
                for j in range(abs(f)):
                    (g_sharp if f > 0 else g_flat)(c, kx + j * 7.5, yb[i] + steps[j] * SP / 2)
                kx += abs(f) * 7.5 + 4
            if first:
                c.setFont(d.bold, 15)
                c.drawCentredString(kx + 6, yb[i] + 2 * SP + 1.5, "4")
                c.drawCentredString(kx + 6, yb[i] + 0 * SP + 1.5, "4")
        if self.n_tab:                                   # "T A B"
            c.setFont("Helvetica-Bold", 10)
            mid = (tab_top + tab_bot) / 2
            for k, ch in enumerate("TAB"):
                c.drawCentredString(x0 + 11, mid + 8 - k * 10.5, ch)
        if first:                                        # instrument label
            c.saveState()
            c.translate(x0 - 14, (ys[0] + (tab_bot if self.n_tab else yb[-1])) / 2)
            c.rotate(90)
            c.setFont(d.reg, 9)
            c.drawCentredString(0, 0, shape_text(self.cfg["abbr"]))
            c.restoreState()
        # bar lines + bar numbers + multirests
        x = x0 + head_w
        for ce in cells:
            w = ce.width * scale
            if ce.multirest:
                for i in range(n):
                    self._multirest(x, w, yb[i], ce.n)
            if ce is cells[0]:
                c.setFont(d.reg, 7.5)
                c.setFillGray(0.35)
                c.drawString(x + 2, ys[0] + 22, str(ce.bar + 1))
                c.setFillGray(0)
            x += w
            c.setLineWidth(0.8)
            c.line(x, ys[0], x, yb[-1])
            if self.n_tab:
                c.line(x, tab_top, x, tab_bot)
        return ys, yb, tab_top, head_w, x_end

    def _brace(self, x, y1, y2):
        c = self.c
        mid = (y1 + y2) / 2
        c.saveState()
        c.setLineWidth(1.6)
        p = c.beginPath()
        p.moveTo(x + 5, y1)
        p.curveTo(x - 4, y1 - 10, x + 6, mid + 8, x - 4, mid)
        p.curveTo(x + 6, mid - 8, x - 4, y2 + 10, x + 5, y2)
        c.drawPath(p, stroke=1, fill=0)
        c.restoreState()

    def _multirest(self, x, w, yb, n):
        c, d = self.c, self.doc
        mid = yb + 2 * SP
        a, b = x + 8, x + w - 8
        c.saveState()
        c.setLineWidth(3.4)
        c.line(a, mid, b, mid)
        c.setLineWidth(1.0)
        c.line(a, mid - SP, a, mid + SP)
        c.line(b, mid - SP, b, mid + SP)
        c.setFont(d.bold, 11)
        c.drawCentredString((a + b) / 2, yb + 4 * SP + 7, str(n))
        c.restoreState()

    def _bracket(self, x1, x2, y, above):
        c = self.c
        sgn = -1 if above else 1
        mid = (x1 + x2) / 2
        c.saveState()
        c.setLineWidth(0.7)
        c.line(x1, y + sgn * 4, x1, y)
        c.line(x1, y, mid - 6, y)
        c.line(mid + 6, y, x2, y)
        c.line(x2, y, x2, y + sgn * 4)
        c.setFont("Times-BoldItalic", 9)
        c.drawCentredString(mid, y - 3, "3")
        c.restoreState()

    # ---------------- notation cell ----------------
    def _cell_notation(self, st, ce, xoff, scale, yb, placed, show_chords):
        c, d, upb = self.c, self.doc, self.upb
        evs = st.bars[ce.bar]
        if ce.multirest:
            return
        acc_state, preps = {}, []
        for ev in evs:
            if not ev.pitches:
                preps.append(None)
                continue
            heads, up = _prep(ev, st.clef, self.table, acc_state, self.keyacc)
            preps.append(dict(ev=ev, heads=heads, up=up, flags=shape(ev.dur, upb)[1]))
        # beam groups: consecutive flagged notes inside one beat
        groups, cur = [], []

        def flush():
            nonlocal cur
            if len(cur) >= 2:
                groups.append(cur)
            cur = []
        for p in preps:
            ok = p is not None and p["flags"] > 0
            if ok and cur and not (cur[-1]["ev"].pos + cur[-1]["ev"].dur == p["ev"].pos
                                   and cur[-1]["ev"].pos // upb == p["ev"].pos // upb):
                flush()
            if ok:
                cur.append(p)
            else:
                flush()
        flush()
        gdir = {}
        for g in groups:
            if st.clef == "perc":
                up = True
            else:
                ref = sum(sum(h["step"] for h in p["heads"]) / len(p["heads"]) for p in g) / len(g)
                up = ref < 4
            for p in g:
                p["up"] = up
            gdir[id(g)] = up
        stem_len = 3.4 * SP
        evx = []
        for ev, p in zip(evs, preps):
            if p is None:
                x = xoff + (ce.width * scale) / 2 if ev.full_rest else xoff + ce.xs[ev.pos] * scale
                g_rest(c, x, yb, rest_code(ev.dur, upb), ev.full_rest)
                evx.append((ev, x, None, None))
                continue
            x = xoff + ce.xs[ev.pos] * scale
            heads, up = p["heads"], p["up"]
            ys = [yb + h["step"] * SP / 2 for h in heads]
            offs = [0.0] * len(heads)
            order = range(len(heads)) if up else range(len(heads) - 1, -1, -1)
            last = None
            for i in order:
                if last is not None and abs(heads[i]["step"] - heads[last]["step"]) == 1 and offs[last] == 0:
                    offs[i] = HEAD_W * 0.92 * (1 if up else -1)
                last = i
            hk, _fl, dots = shape(ev.dur, upb)
            c.setFillGray(0)
            for h, yy, o in zip(heads, ys, offs):
                hx, step = x + o, h["step"]
                c.setLineWidth(0.7)
                if step <= -2:
                    for s in range(-2, step - 1, -2):
                        c.line(hx - HEAD_W * 0.85, yb + s * SP / 2, hx + HEAD_W * 0.85, yb + s * SP / 2)
                if step >= 10:
                    for s in range(10, step + 1, 2):
                        c.line(hx - HEAD_W * 0.85, yb + s * SP / 2, hx + HEAD_W * 0.85, yb + s * SP / 2)
                if h["kind"] in ("x", "xo"):
                    g_xhead(c, hx, yy)
                    if h["kind"] == "xo":
                        c.setLineWidth(0.8)
                        c.circle(hx, yy + SP * 0.9, 1.9, stroke=1, fill=0)
                else:
                    g_head(c, hx, yy, hk)
                if h["acc"] is not None:
                    g_acc(c, h["acc"], hx - HEAD_W / 2 - 5.5, yy)
                if dots:
                    c.circle(hx + HEAD_W / 2 + 3.2, yy + (SP / 2 if step % 2 == 0 else 0), 1.1, stroke=0, fill=1)
            p.update(x=x, ys=ys, stem_x=x + ((HEAD_W / 2 - 0.5) if up else -(HEAD_W / 2 - 0.5)))
            tip = None
            if hk != "whole":
                tip = (max(ys) + stem_len) if up else (min(ys) - stem_len)
            p["tip"] = tip
            if self.lyrics and ev.lyric:
                c.setFont(d.reg, 7.5)
                c.drawString(x - 2, yb - 17, shape_text(ev.lyric))
            if show_chords and ev.chord:
                c.setFont(d.bold, 8.5)
                c.drawString(x - 3, yb + 4 * SP + 27, shape_text(ev.chord))
            placed.append(dict(ev=ev, x=x, ys=ys, up=up))
            evx.append((ev, x, p, tip))
        # stems / beams / flags
        done = set()
        for g in groups:
            up = gdir[id(g)]
            beam_y = max(max(p["ys"]) + stem_len for p in g) if up else min(min(p["ys"]) - stem_len for p in g)
            bt = SP * 0.55
            c.setLineWidth(0.8)
            for p in g:
                y0 = min(p["ys"]) if up else max(p["ys"])
                c.line(p["stem_x"], y0, p["stem_x"], beam_y)
                p["tip"] = beam_y
                done.add(id(p))
            sgn = -1 if up else 1

            def beam(xa, xb2, off):
                pth = c.beginPath()
                y1 = beam_y + sgn * off
                pth.moveTo(xa, y1)
                pth.lineTo(xb2, y1)
                pth.lineTo(xb2, y1 + sgn * bt)
                pth.lineTo(xa, y1 + sgn * bt)
                pth.close()
                c.drawPath(pth, stroke=0, fill=1)
            beam(g[0]["stem_x"], g[-1]["stem_x"], 0)
            for i, p in enumerate(g):
                if p["flags"] < 2:
                    continue
                left = i > 0 and g[i - 1]["flags"] >= 2
                right = i + 1 < len(g) and g[i + 1]["flags"] >= 2
                if right:
                    beam(p["stem_x"], g[i + 1]["stem_x"], bt + SP * 0.35)
                elif not left:
                    other = g[i + 1]["stem_x"] if i + 1 < len(g) else g[i - 1]["stem_x"]
                    beam(p["stem_x"], p["stem_x"] + (SP if other > p["stem_x"] else -SP), bt + SP * 0.35)
        for p in preps:
            if p is None or id(p) in done or p["tip"] is None:
                continue
            up, ys = p["up"], p["ys"]
            y0 = min(ys) if up else max(ys)
            c.setLineWidth(0.8)
            c.line(p["stem_x"], y0, p["stem_x"], p["tip"])
            sgn = -1 if up else 1
            for k in range(p["flags"]):
                ty = p["tip"] + (-k * SP * 0.95 if up else k * SP * 0.95)
                pth = c.beginPath()
                sx = p["stem_x"]
                pth.moveTo(sx, ty)
                pth.curveTo(sx + 0.3 * SP, ty + sgn * 0.5 * SP, sx + 1.5 * SP, ty + sgn * 1.2 * SP,
                            sx + 1.1 * SP, ty + sgn * 2.5 * SP)
                c.setLineWidth(1.3)
                c.drawPath(pth, stroke=1, fill=0)
        # triplet brackets
        if upb == 3:
            by_beat = {}
            for item in evx:
                by_beat.setdefault(item[0].pos // 3, []).append(item)
            for lst in by_beat.values():
                if len(lst) == 1 and lst[0][0].dur >= 3:
                    continue
                if len(lst) < 2 or lst[0][0].full_rest:
                    continue
                xs_ = [it[1] for it in lst]
                notes_ = [it for it in lst if it[2] is not None and it[3] is not None]
                if notes_:
                    above = sum(1 for it in notes_ if it[2]["up"]) >= len(notes_) / 2
                    tips = [it[3] for it in notes_]
                    y = (max(tips) + 6) if above else (min(tips) - 6)
                    if above:
                        y = max(y, yb + 4 * SP + 7)
                    else:
                        y = min(y, yb - 7)
                else:
                    above, y = True, yb + 4 * SP + 8
                self._bracket(min(xs_) - HEAD_W / 2, max(xs_) + HEAD_W / 2, y, above)

    # ---------------- TAB cell ----------------
    def _cell_tab(self, st, ce, xoff, scale, tab_top):
        c = self.c
        if ce.multirest:
            return
        for ev in st.bars[ce.bar]:
            if not ev.pitches:
                continue
            x = xoff + ce.xs[ev.pos] * scale
            if self.tab == "drums":
                used = set()
                for p in sorted(ev.pitches, reverse=True):
                    line = 0 if p in CYMBALS else 2
                    while line in used and line < 5:
                        line += 1
                    used.add(line)
                    self._tab_num(x, tab_top - line * TSP, str(p))
            else:
                for (s, f) in ev.tabs:
                    self._tab_num(x, tab_top - s * TSP, f"({f})" if ev.tie_in else str(f))

    def _tab_num(self, x, y, text):
        c = self.c
        c.saveState()
        w = c.stringWidth(text, "Helvetica", 8) + 2
        c.setFillGray(1)
        c.rect(x - w / 2, y - 3.6, w, 7.2, stroke=0, fill=1)
        c.setFillGray(0)
        c.setFont("Helvetica", 8)
        c.drawCentredString(x, y - 2.8, text)
        c.restoreState()

    # ---------------- whole score ----------------
    def draw_score(self, y, staves):
        d, c, ctx = self.doc, self.c, self.ctx
        cells = layout_cells(staves, ctx.n_bars, self.bar_units, self.upb)
        x0 = d.margin + 10
        W = d.W - d.margin - x0
        first_w = self._head_width(staves[0].clef, True)
        rest_w = self._head_width(staves[0].clef, False)
        systems = pack_systems(cells, W - first_w - 4, W - rest_w - 4)
        sys_h = self.system_height(len(staves))
        carry = [False] * len(staves)
        for si, (cs, w) in enumerate(systems):
            if y - sys_h < d.bottom:
                d.new_page()
                y = d.top
            first = si == 0
            head_w = first_w if first else rest_w
            scale = max(0.75, (W - head_w) / w)
            if si == len(systems) - 1:
                scale = min(scale, 1.35)
            ys, yb, tab_top, head_w, x_end = self._draw_frame(y, staves, cs, scale, x0, first)
            x = x0 + head_w
            placed = [[] for _ in staves]
            for ce in cs:
                for i, st in enumerate(staves):
                    self._cell_notation(st, ce, x, scale, yb[i], placed[i], self.cfg.get("chords") and i == 0)
                if self.n_tab:
                    self._cell_tab(staves[0], ce, x, scale, tab_top)
                x += ce.width * scale
            c.setFillGray(0)
            for i in range(len(staves)):
                pl = placed[i]
                for j, e in enumerate(pl):
                    if e["ev"].tie:
                        x1 = e["x"] + HEAD_W / 2 + (5 if shape(e["ev"].dur, self.upb)[2] else 1.5)
                        x2 = pl[j + 1]["x"] - HEAD_W / 2 - 1.5 if j + 1 < len(pl) else x_end - 3
                        for yy in e["ys"]:
                            g_tie(c, x1, x2, yy + (-SP * 0.55 if e["up"] else SP * 0.55),
                                  not e["up"], len(e["ys"]) > 1)
                if carry[i] and pl:
                    yy = pl[0]["ys"][0]
                    g_tie(c, x0 + head_w - 4, pl[0]["x"] - HEAD_W / 2 - 1.5,
                          yy + (-SP * 0.55 if pl[0]["up"] else SP * 0.55), not pl[0]["up"])
                carry[i] = bool(pl and pl[-1]["ev"].tie)
            if si == len(systems) - 1:
                c.setLineWidth(2.4)
                c.line(x_end + 1.2, ys[0], x_end + 1.2, yb[-1])
                if self.n_tab:
                    c.line(x_end + 1.2, tab_top, x_end + 1.2, tab_top - (self.n_tab - 1) * TSP)
            y -= sys_h
        return y


def render_sheet(path, kind, notes, ctx, bar_units_ignored=None, absorb=False, subtitle=None, extra=None):
    """Engrave one instrument. `notes` = list[N] (b0/b1 already aligned to bar 1)."""
    cfg = dict(KINDS[kind])
    if kind == "vocals" and notes and float(np.median([n.pitch for n in notes])) < 58:
        cfg["transpose"] = 12          # low / male voice: written an octave up, treble clef with "8"
    doc = PdfDoc(path, f"{ctx.title} - {cfg['label']}", ctx.footer)
    doc.margin = 44
    flats = ctx.fifths < 0
    staves = build_staves(cfg, notes, 4 * ctx.upb, ctx.n_bars, ctx.upb, absorb, flats)
    sd = SheetDrawer(doc, ctx, cfg)
    y = sd.header(doc.top - 4, subtitle or cfg["label"])
    sd.draw_score(y - 6, staves)
    doc.finish()
    return path


# =====================================================================
#  MIDI LOADING + BEAT MAP
# =====================================================================
def _read_midi(path):
    import mido
    mid = mido.MidiFile(str(path))
    tpb = mid.ticks_per_beat
    tempos, raw = [], []
    for tr in mid.tracks:
        t, active = 0, {}
        for msg in tr:
            t += msg.time
            if msg.type == "set_tempo":
                tempos.append((t, msg.tempo))
            elif msg.type == "note_on" and msg.velocity > 0:
                active.setdefault((msg.channel, msg.note), []).append((t, msg.velocity))
            elif msg.type in ("note_off", "note_on"):
                lst = active.get((msg.channel, msg.note))
                if lst:
                    s, v = lst.pop(0)
                    raw.append((s, max(t, s + 1), msg.note, v))
        for (ch, nt), lst in active.items():          # unterminated notes
            for s, v in lst:
                raw.append((s, t, nt, v))
    tempos = sorted(set(tempos))
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0, 500000))
    return tpb, tempos, raw


def _tick_to_sec(tpb, tempos):
    ticks = [t for t, _ in tempos]
    secs = [0.0]
    for i in range(1, len(tempos)):
        secs.append(secs[-1] + (ticks[i] - ticks[i - 1]) * tempos[i - 1][1] / 1e6 / tpb)

    def f(t):
        i = max(0, bisect.bisect_right(ticks, t) - 1)
        return secs[i] + (t - ticks[i]) * tempos[i][1] / 1e6 / tpb
    return f


def beat_times_from_midi(path, extra_beats=16):
    """Recover the song's beat grid (seconds per beat) from a Midi_refine.py tempo map."""
    tpb, tempos, raw = _read_midi(path)
    f = _tick_to_sec(tpb, tempos)
    last = max([r[1] for r in raw] + [tempos[-1][0]])
    n = int(math.ceil(last / tpb)) + extra_beats
    return np.array([f(k * tpb) for k in range(n)], dtype=float)


def _sec_to_beat(t, bt):
    t = np.asarray(t, dtype=float)
    idx = np.arange(len(bt), dtype=float)
    out = np.interp(t, bt, idx)
    lo, hi = t < bt[0], t > bt[-1]
    if np.any(lo):
        out = np.where(lo, (t - bt[0]) / max(bt[1] - bt[0], 1e-3), out)
    if np.any(hi):
        out = np.where(hi, (len(bt) - 1) + (t - bt[-1]) / max(bt[-1] - bt[-2], 1e-3), out)
    return out


def load_notes(path, mode="seconds", bt=None, tempo_bpm=120.0):
    """MIDI file -> list[N].  mode 'ticks': tick/PPQ are beats.  'seconds': map real time onto bt."""
    tpb, tempos, raw = _read_midi(path)
    f = _tick_to_sec(tpb, tempos)
    out = []
    for s, e, p, v in raw:
        s_sec, e_sec = f(s), f(e)
        if mode == "ticks":
            b0, b1 = s / tpb, e / tpb
        elif bt is not None:
            b0, b1 = (float(x) for x in _sec_to_beat([s_sec, e_sec], bt))
        else:
            b0, b1 = s_sec * tempo_bpm / 60.0, e_sec * tempo_bpm / 60.0
        out.append(N(s_sec, e_sec, b0, b1, int(p), int(v)))
    out.sort(key=lambda n: (n.b0, n.pitch))
    return out


def choose_upb(all_notes):
    """4 = straight 16ths, 3 = triplets / swing.  Based on how well onsets fit each grid."""
    pos = np.array([n.b0 for n in all_notes if n.vel >= 40], dtype=float)
    if len(pos) < 12:
        return 4
    if len(pos) > 5000:
        pos = pos[np.linspace(0, len(pos) - 1, 5000).astype(int)]
    sc = {}
    for sub in (4, 3):
        r = np.abs(pos * sub - np.round(pos * sub)) / sub
        sc[sub] = float(np.mean(r)) / (1.0 / (4.0 * sub))
    return 3 if sc[3] < sc[4] - 0.15 else 4


def estimate_downbeat(parts_notes):
    """Which beat (0..3) is bar-beat 1?  Kick / bass favour 1 and 3, snare favours 2 and 4."""
    score = np.zeros(4)
    for kind, notes in parts_notes:
        for n in notes:
            k = int(round(n.b0))
            if abs(n.b0 - k) > 0.2:
                continue
            w = n.vel / 127.0
            if kind == "drums":
                if n.pitch in (35, 36):
                    score[k % 4] += w
                    score[(k - 2) % 4] += 0.6 * w
                elif n.pitch in (38, 40):
                    score[(k - 1) % 4] += w
                    score[(k - 3) % 4] += w
            elif kind == "bass" or (kind in ("guitar_rhythm", "piano", "other") and n.pitch < 48):
                score[k % 4] += 0.7 * w
    return int(np.argmax(score)) if score.max() > 0 else 0


# =====================================================================
#  LYRICS  (SRT -> syllables on the vocal notes)
# =====================================================================
def parse_srt(text):
    def tsec(s):
        h, m, rest = s.strip().replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    out = []
    for block in re.split(r"\r?\n\s*\r?\n", (text or "").strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        for i, l in enumerate(lines):
            if "-->" in l:
                try:
                    a, b = l.split("-->")
                    t = " ".join(x.strip() for x in lines[i + 1:])
                    t = re.sub(r"<[^>]+>", "", t).strip()
                    if t and not re.fullmatch(r"[\[(].*[\])]", t):
                        out.append((tsec(a), tsec(b), t))
                except Exception:
                    pass
                break
    return out


def syllabify(word):
    low = word.lower()
    stem = low.rstrip(".,;:!?\"')")
    if not re.fullmatch(r"[a-z'\u2019]+", stem) or len(stem) <= 3:
        return [word]
    groups = [(m.start(), m.end()) for m in re.finditer(r"[aeiouy]+", stem)]
    if (len(groups) > 1 and stem.endswith("e") and not stem.endswith("le")
            and groups[-1] == (len(stem) - 1, len(stem))):
        groups.pop()
    if len(groups) <= 1:
        return [word]
    cuts = [e1 if s2 - e1 <= 1 else e1 + 1 for (_s, e1), (s2, _e) in zip(groups, groups[1:])]
    pieces, prev = [], 0
    for c in cuts:
        pieces.append(word[prev:c])
        prev = c
    pieces.append(word[prev:])
    return [p for p in pieces if p]


def attach_lyrics(notes, blocks):
    """Spread each timed lyric line over the vocal notes that fall inside it. -> words placed"""
    notes.sort(key=lambda n: n.sec0)
    starts = [n.sec0 for n in notes]
    placed = 0
    for (a, b, text) in blocks:
        words = text.split()
        if not words:
            continue
        syl = []
        for w in words:
            parts = syllabify(w)
            for i, s in enumerate(parts):
                syl.append(s + ("-" if i < len(parts) - 1 else ""))
        lo = bisect.bisect_left(starts, a - 0.3)
        hi = bisect.bisect_left(starts, b + 0.1)
        cand = [n for n in notes[lo:hi] if not n.lyric]
        if not cand:
            continue
        m, k = len(syl), len(cand)
        if k >= m:                        # more notes than syllables: spread them out, rest are melisma
            for i, s in enumerate(syl):
                cand[min(k - 1, int(round(i * (k - 1) / max(1, m - 1))) if m > 1 else 0)].lyric = s
        else:                             # fewer notes: group syllables
            for j in range(k):
                chunk = syl[j * m // k:(j + 1) * m // k]
                cand[j].lyric = "".join(chunk) if len(chunk) > 1 else (chunk[0] if chunk else "")
        placed += len(words)
    return placed


# =====================================================================
#  VOCAL DATA SHEET
# =====================================================================
def _bar_beat(n_b):
    return int(n_b // 4) + 1, (n_b % 4) + 1


def vocal_stats(notes, blocks, duration, flats):
    st = {}
    if not notes:
        return st
    pit = np.array([n.pitch for n in notes])
    lens = np.array([n.sec1 - n.sec0 for n in notes])
    lo, hi = int(pit.min()), int(pit.max())
    st["lowest"] = f"{midi_name(lo, flats)}  ({midi_hz(lo):.0f} Hz)"
    st["highest"] = f"{midi_name(hi, flats)}  ({midi_hz(hi):.0f} Hz)"
    st["range"] = f"{hi - lo} semitones ({(hi - lo) / 12:.1f} octaves)"
    st["tessitura"] = (f"{midi_name(int(np.percentile(pit, 25)), flats)} - "
                       f"{midi_name(int(np.percentile(pit, 75)), flats)}  (middle 50% of notes)")
    st["average"] = midi_name(int(round(float(pit.mean()))), flats)
    st["notes"] = str(len(notes))
    st["median"] = f"{float(np.median(lens)):.2f} s"
    phr = []
    for n in sorted(notes, key=lambda n: n.sec0):
        if phr and n.sec0 - phr[-1][1] < 0.9:
            phr[-1][1] = max(phr[-1][1], n.sec1)
        else:
            phr.append([n.sec0, n.sec1])
    sing = sum(b - a for a, b in phr)
    st["phrases"] = str(len(phr))
    st["singing"] = f"{sing:.0f} s  ({100 * sing / max(duration, 1):.0f}% of the song)"
    st["lines"] = str(len(blocks)) if blocks else "-"
    st["words"] = str(sum(len(t.split()) for _a, _b, t in blocks)) if blocks else "-"
    return st


def render_vocal_data_sheet(path, ctx, notes, blocks, duration, meta, phrases):
    from reportlab.lib.utils import simpleSplit
    doc = PdfDoc(path, f"{ctx.title} - Vocal data sheet", ctx.footer)
    c, m, W = doc.c, doc.margin, doc.W
    reg, bold = doc.reg, doc.bold
    flats = ctx.fifths < 0
    stats = vocal_stats(notes, blocks, duration, flats)
    y = doc.top - 6
    c.setFont(bold, 22)
    c.drawString(m, y, "Vocal Data Sheet")
    c.setFont(reg, 13)
    c.setFillGray(0.25)
    c.drawString(m, y - 20, shape_text(ctx.title))
    c.setFillGray(0)
    y -= 48
    rows = [("Key", ctx.key_name), ("Tempo", f"{ctx.tempo:.0f} BPM"), ("Time signature", "4/4"),
            ("Song length", fmt_clock(duration)), ("Lowest note", stats.get("lowest", "-")),
            ("Highest note", stats.get("highest", "-")), ("Vocal range", stats.get("range", "-")),
            ("Tessitura", stats.get("tessitura", "-")), ("Average pitch", stats.get("average", "-")),
            ("Notes detected", stats.get("notes", "0")), ("Median note length", stats.get("median", "-")),
            ("Phrases", stats.get("phrases", "-")), ("Time singing", stats.get("singing", "-")),
            ("Lyric lines", stats.get("lines", "-")), ("Words", stats.get("words", "-")),
            ("Lyrics source", meta.get("lyrics_source", "-")), ("Rhythm grid", meta.get("grid", "-")),
            ("Melody source", meta.get("melody", "-"))]
    colw = (W - 2 * m) / 2
    for i, (k, v) in enumerate(rows):
        col, r = divmod(i, 9)
        c.setFont(bold, 9)
        c.drawString(m + col * colw, y - r * 15, k)
        c.setFont(reg, 9)
        c.drawString(m + col * colw + 105, y - r * 15, shape_text(v)[:70])
    y -= 9 * 15 + 14
    if notes:
        c.setFont(bold, 10)
        c.drawString(m, y, "Pitch distribution (how often each note is sung)")
        y -= 8
        pit = [n.pitch for n in notes]
        lo, hi = min(pit) - 1, max(pit) + 1
        counts = {p: 0 for p in range(lo, hi + 1)}
        for p in pit:
            counts[p] += 1
        cmax = max(counts.values())
        ch_h, ch_w = 130, W - 2 * m
        bw = ch_w / len(counts)
        base = y - ch_h - 6
        c.setStrokeGray(0.7)
        c.line(m, base, m + ch_w, base)
        for i, p in enumerate(range(lo, hi + 1)):
            h = counts[p] / cmax * (ch_h - 16)
            if p % 12 in (1, 3, 6, 8, 10):
                c.setFillColorRGB(0.6, 0.62, 0.88)
            else:
                c.setFillColorRGB(0.35, 0.38, 0.75)
            c.rect(m + i * bw + 1, base, max(1, bw - 2), h, stroke=0, fill=1)
            c.setFillGray(0)
            if counts[p] and bw > 9:
                c.setFont(reg, 6)
                c.drawCentredString(m + i * bw + bw / 2, base + h + 2, str(counts[p]))
            if p % 12 == 0 or p in (lo, hi) or bw > 14:
                c.setFont(reg, 6.5)
                c.drawCentredString(m + i * bw + bw / 2, base - 9, midi_name(p, flats))
        c.setStrokeGray(0)

    def table_header(y_, cols, labels):
        c.setFillGray(0.9)
        c.rect(m - 2, y_ - 4, W - 2 * m + 4, 15, stroke=0, fill=1)
        c.setFillGray(0)
        c.setFont(bold, 8.5)
        for x_, lab in zip(cols, labels):
            c.drawString(x_, y_, lab)

    # ---- lyrics with timing ----
    doc.new_page()
    y = doc.top - 6
    c.setFont(bold, 15)
    c.drawString(m, y, "Lyrics with timing")
    y -= 24
    cols = [m, m + 28, m + 108, m + 168, m + 240]
    labels = ["#", "Time", "Bars", "Range", "Lyrics"]
    table_header(y, cols, labels)
    y -= 17
    lines = blocks or [(a, b, "(lyrics not available - add an SRT / lyrics file)") for a, b in phrases]
    text_w = W - m - cols[4]
    for i, (a, b, text) in enumerate(lines):
        wrapped = simpleSplit(shape_text(text), reg, 9.5, text_w) or [""]
        h = len(wrapped) * 11.5 + 5
        if y - h < doc.bottom:
            doc.new_page()
            y = doc.top - 6
            table_header(y, cols, labels)
            y -= 17
        inside = [n for n in notes if a - 0.15 <= n.sec0 <= b + 0.1]
        rng = (f"{midi_name(min(n.pitch for n in inside), flats)}-{midi_name(max(n.pitch for n in inside), flats)}"
               if inside else "-")
        if inside:
            b1, b2 = _bar_beat(inside[0].b0)[0], _bar_beat(inside[-1].b0)[0]
            bars = f"{b1}" if b1 == b2 else f"{b1}-{b2}"
        else:
            bars = "-"
        c.setFont(reg, 9)
        c.drawString(cols[0], y, str(i + 1))
        c.drawString(cols[1], y, f"{fmt_clock(a)}-{fmt_clock(b)}"[:16])
        c.drawString(cols[2], y, bars)
        c.drawString(cols[3], y, rng)
        c.setFont(reg, 9.5)
        for k, ln in enumerate(wrapped):
            c.drawString(cols[4], y - k * 11.5, ln)
        c.setStrokeGray(0.85)
        c.line(m, y - h + 8, W - m, y - h + 8)
        c.setStrokeGray(0)
        y -= h

    # ---- full note table ----
    doc.new_page()
    y = doc.top - 6
    c.setFont(bold, 15)
    c.drawString(m, y, "Note data")
    c.setFont(reg, 8)
    c.setFillGray(0.35)
    c.drawString(m + 90, y, "(complete list also saved as VOCAL_NOTES.csv)")
    c.setFillGray(0)
    first_y = y - 22
    block_w = (W - 2 * m) / 3
    labels = ["#", "Time", "Bar.Beat", "Note", "Beats", "Syllable"]
    offs = [0, 24, 62, 104, 138, 172]
    shown = notes[:1380]
    i = 0
    while i < len(shown):
        for blk in range(3):
            x0 = m + blk * block_w
            c.setFillGray(0.9)
            c.rect(x0 - 2, first_y - 3, block_w - 8, 13, stroke=0, fill=1)
            c.setFillGray(0)
            c.setFont(bold, 7.5)
            for ox, lab in zip(offs, labels):
                c.drawString(x0 + ox, first_y, lab)
            c.setFont(reg, 7.5)
            for r in range(46):
                if i >= len(shown):
                    break
                n = shown[i]
                bar, beat = _bar_beat(n.b0)
                vals = [str(i + 1), fmt_clock(n.sec0), f"{bar}.{beat:.2f}", midi_name(n.pitch, flats),
                        f"{n.b1 - n.b0:.2f}", n.lyric]
                for ox, v in zip(offs, vals):
                    c.drawString(x0 + ox, first_y - 14 - r * 10, shape_text(v)[:14])
                i += 1
        if i < len(shown):
            doc.new_page()
            first_y = doc.top - 6
    doc.finish()


def write_vocal_csv(path, notes, flats):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["index", "start_s", "end_s", "duration_s", "midi", "note", "hz", "bar", "beat", "syllable"])
        for i, n in enumerate(notes, 1):
            bar, beat = _bar_beat(n.b0)
            w.writerow([i, f"{n.sec0:.3f}", f"{n.sec1:.3f}", f"{n.sec1 - n.sec0:.3f}", n.pitch,
                        midi_name(n.pitch, flats), f"{midi_hz(n.pitch):.1f}", bar, f"{beat:.2f}", n.lyric])


# =====================================================================
#  TOP-LEVEL ENTRY POINT
# =====================================================================
def _slug(s):
    s = re.sub(r"[^\w\- ]+", "", s, flags=re.UNICODE).strip().replace(" ", "_")
    return s or "song"


def generate_sheets(out_dir, parts, title, tempo_bpm, srt_text=None, beat_source=None,
                    log=print, progress=None):
    """
    out_dir     : folder that receives SHEET_*.pdf, VOCAL_DATA_SHEET.pdf, VOCAL_NOTES.csv
    parts       : [{key, kind, midi, time_mode}]   (see module docstring)
    title       : song title printed on every sheet
    tempo_bpm   : tempo to print (e.g. refined["tempo_bpm"])
    srt_text    : optional SRT text -> lyrics under the vocal melody + vocal data sheet
    beat_source : optional path of a Midi_refine.py MIDI; its tempo map gives the beat grid
                  that real-time (seconds) MIDIs are aligned to
    Returns dict(sheets={key: pdf}, vocal_data=pdf|None, vocal_csv=csv|None, key, grid, warnings)
    """
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    warnings = []

    def prog(f, m):
        if progress:
            progress(f, m)
    bt = None
    if beat_source and os.path.exists(str(beat_source)):
        try:
            bt = beat_times_from_midi(beat_source)
        except Exception as e:
            warnings.append(f"Could not read beat map ({e}); using constant tempo.")
    tempo_bpm = float(tempo_bpm or 120.0)
    if bt is not None and len(bt) > 8:
        tempo_bpm = tempo_bpm or 60.0 / float(np.median(np.diff(bt[1:-1])))

    loaded = []
    for p in parts:
        kind = p["kind"]
        if kind not in KINDS or not p.get("midi") or not os.path.exists(str(p["midi"])):
            continue
        try:
            notes = load_notes(p["midi"], p.get("time_mode", "seconds"), bt, tempo_bpm)
        except Exception as e:
            warnings.append(f"{p.get('key', kind)}: cannot read MIDI ({type(e).__name__}: {e})")
            continue
        lo, hi = PLAUSIBLE.get(kind, (0, 127))
        secs = p.get("time_mode", "seconds") == "seconds"
        notes = [n for n in notes if (kind == "drums" or lo <= n.pitch <= hi)
                 and (not secs or kind == "drums" or n.sec1 - n.sec0 >= 0.05)]
        if len(notes) < 4:
            warnings.append(f"{p.get('key', kind)}: too few notes for a sheet.")
            continue
        loaded.append(dict(key=p.get("key", kind), kind=kind, notes=notes,
                           absorb=p.get("time_mode", "seconds") == "seconds"))
    if not loaded:
        raise RuntimeError("No MIDI parts could be loaded for sheet music.")

    all_notes = [n for lp in loaded for n in lp["notes"]]
    exact = [n for lp in loaded if not lp["absorb"] for n in lp["notes"]]
    upb = choose_upb(exact if len(exact) >= 24 else all_notes)
    d = estimate_downbeat([(lp["kind"], lp["notes"]) for lp in loaded])
    min_b = min(n.b0 for n in all_notes)
    shift = d + 4 * math.floor((min_b - d) / 4.0)
    for n in all_notes:
        n.b0 -= shift
        n.b1 -= shift
    n_bars = int(min(500, max(1, math.ceil(max(n.b1 for n in all_notes) / 4.0))))
    pitched = [n for lp in loaded if lp["kind"] != "drums" for n in lp["notes"]]
    tonic, mode, fifths, key_name = detect_key(pitched)
    ctx = Ctx(title=title, tempo=tempo_bpm, upb=upb, fifths=fifths, key_name=key_name, n_bars=n_bars)
    grid_name = "triplets (swing feel)" if upb == 3 else "16th notes"
    log(f"[score_engine] {len(loaded)} parts, {n_bars} bars, key {key_name}, grid {grid_name}, "
        f"{tempo_bpm:.0f} BPM")

    blocks = parse_srt(srt_text) if srt_text else []
    sheets, vocal_data, vocal_csv = {}, None, None
    names = {"vocals": "vocals_lyrics"}
    kinds_count = {}
    for lp in loaded:
        kinds_count[lp["kind"]] = kinds_count.get(lp["kind"], 0) + 1
    for i, lp in enumerate(loaded):
        kind, key = lp["kind"], lp["key"]
        prog(i / len(loaded), f"Engraving {KINDS[kind]['label']} sheet...")
        if kind == "vocals" and blocks:
            attach_lyrics(lp["notes"], blocks)
        stem = names.get(kind, kind) if kinds_count[kind] == 1 else f"{kind}_{key}"
        fn = out / f"SHEET_{stem}.pdf"
        try:
            render_sheet(str(fn), kind, lp["notes"], ctx, absorb=lp["absorb"])
            sheets[key] = str(fn)
        except Exception as e:
            import traceback
            traceback.print_exc()
            warnings.append(f"{key}: sheet failed ({type(e).__name__}: {e})")
            continue
        if kind == "vocals":
            vn = sorted(lp["notes"], key=lambda n: n.sec0)
            dur = max(n.sec1 for n in vn)
            phr = []
            for n in vn:
                if phr and n.sec0 - phr[-1][1] < 0.9:
                    phr[-1][1] = max(phr[-1][1], n.sec1)
                else:
                    phr.append([n.sec0, n.sec1])
            meta = dict(lyrics_source=("SRT subtitle file" if blocks else "not available"),
                        grid=grid_name, melody="vocals stem -> MIDI")
            try:
                vd = out / "VOCAL_DATA_SHEET.pdf"
                render_vocal_data_sheet(str(vd), ctx, vn, blocks, dur, meta, phr)
                vocal_data = str(vd)
                vc = out / "VOCAL_NOTES.csv"
                write_vocal_csv(str(vc), vn, fifths < 0)
                vocal_csv = str(vc)
            except Exception as e:
                import traceback
                traceback.print_exc()
                warnings.append(f"vocal data sheet failed ({type(e).__name__}: {e})")
    if any(lp["kind"] == "vocals" for lp in loaded) and not blocks:
        warnings.append("No lyrics (SRT) available - vocal sheet shows the melody only.")
    prog(1.0, "Sheets done")
    return dict(sheets=sheets, vocal_data=vocal_data, vocal_csv=vocal_csv, key=key_name,
                grid=grid_name, bars=n_bars, warnings=warnings)