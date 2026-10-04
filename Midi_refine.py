"""
midi_refine.py  --  beat-aligned, polyphonic guitar MIDI transcription
======================================================================

Replaces the pyin (monophonic) melody -> MIDI step for the "Full
Transcription" job with:

  1. Guitar-focused stem   Demucs htdemucs_6s (guitar [+ other]) so drums/bass/
                           vocals don't pollute pitch detection. Optional.
  2. Beat / tempo map      librosa beat tracking (on the drum stem if present),
                           octave-corrected, jitter-smoothed. Steady songs get
                           one clean tempo; live-drifting songs get a tempo map
                           so the MIDI stays in sync with the audio.
  3. Polyphonic notes      Spotify Basic Pitch (onset-aware, handles chords,
                           double stops and fast runs).
  4. Quantisation          notes snap to a per-song grid (16ths or triplets),
                           chosen automatically from how well the onsets fit.
  5. Lead / rhythm split   strums -> RHYTHM track (real hit timing, not one
                           chord per beat); single-note lines -> SOLO track.

Output files (same names the app already uses, plus one extra):
    03_GUITAR_SOLO.mid      lead line / solos / riffs  (monophonic)
    04_RHYTHM_CHORDS.mid    chord hits with their real strumming rhythm
    07_GUITAR_FULL.mid      every detected note, quantised (for manual editing)

MIDI tick 0 == audio time 0 and the tempo map is written so the MIDI plays
in sync with the source audio, while notes sit exactly on beat-grid ticks
(bar lines line up in any DAW).

Dependencies:  pip install basic-pitch mido librosa soundfile numpy scipy
Optional:      demucs  (for the guitar stem)
"""
import sys
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

PPQ = 480                      # MIDI ticks per quarter note
GUITAR_LOW, GUITAR_HIGH = 38, 90     # D2 (drop-D) .. F#6
SR = 22050
# Register where a guitar MELODY lives. Anything below LEAD_LOW is bass /
# chord-root material and goes to the rhythm track; anything above
# LEAD_HIGH is almost always an overtone or a Basic-Pitch ghost.
LEAD_LOW, LEAD_HIGH = 55, 79          # G3 .. G5


# ----------------------------------------------------------------------
# data
# ----------------------------------------------------------------------
@dataclass
class Note:
    start: float        # seconds
    end: float          # seconds
    pitch: int
    amp: float          # 0..1
    qs: float = 0.0     # quantised start, in beats
    qe: float = 0.0     # quantised end, in beats


def _noop(*_a, **_k):
    pass


# ----------------------------------------------------------------------
# 1. stems
# ----------------------------------------------------------------------
def separate_guitar_stems(wav_path, work_dir, log=_noop):
    """Run Demucs htdemucs_6s in-process.

    Returns (guitar_stem_path | None, drums_stem_path | None).
    Any failure (demucs missing, no GPU memory, ...) returns (None, None)
    so the caller can fall back to whatever audio it already has.
    """
    try:
        import soundfile as sf
        from demucs.separate import main as demucs_main
    except Exception as e:                      # demucs not installed
        log(f"[midi_refine] demucs unavailable ({e}); skipping stem split")
        return None, None

    wav_path = Path(wav_path)
    out_root = Path(work_dir) / "_demucs6"
    try:
        demucs_main(["-n", "htdemucs_6s", "-o", str(out_root), str(wav_path)])
        d = out_root / "htdemucs_6s" / wav_path.stem
        guitar, other, drums = d / "guitar.wav", d / "other.wav", d / "drums.wav"
        if not guitar.exists():
            return None, None

        g, sr = sf.read(str(guitar), always_2d=True)
        # The 6-stem model sometimes files a guitar under "other".
        # If the guitar stem is much quieter than "other", mix them.
        if other.exists():
            o, _ = sf.read(str(other), always_2d=True)
            n = min(len(g), len(o))
            rms = lambda x: float(np.sqrt(np.mean(np.square(x))) + 1e-9)
            if rms(g[:n]) < 0.35 * rms(o[:n]):
                log("[midi_refine] guitar stem is quiet -> mixing guitar + other")
                mix = g[:n] + o[:n]
                peak = np.max(np.abs(mix))
                if peak > 0.99:
                    mix = mix / peak * 0.99
                guitar = d / "guitar_plus_other.wav"
                sf.write(str(guitar), mix, sr)
        return guitar, (drums if drums.exists() else None)
    except BaseException as e:                  # demucs may call sys.exit()
        log(f"[midi_refine] demucs failed ({e}); skipping stem split")
        return None, None


# ----------------------------------------------------------------------
# 2. beats / tempo map
# ----------------------------------------------------------------------
def track_beats(y, sr=SR, tempo_hint=None):
    """Return beat times in seconds (octave-corrected into 70..160 BPM)."""
    import librosa
    hop = 256
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    start = tempo_hint if tempo_hint and 50 < tempo_hint < 220 else 120.0
    _, frames = librosa.beat.beat_track(
        onset_envelope=env, sr=sr, hop_length=hop,
        start_bpm=start, tightness=120, trim=False)
    times = librosa.frames_to_time(frames, sr=sr, hop_length=hop)

    if len(times) < 8:                          # tracker failed -> flat grid
        bpm = start
        first = float(librosa.frames_to_time(
            int(np.argmax(env > 0.5 * env.max())), sr=sr, hop_length=hop))
        dur = len(y) / sr
        return np.arange(first, dur, 60.0 / bpm)

    # octave correction
    for _ in range(3):
        bpm = 60.0 / np.median(np.diff(times))
        if bpm < 70:
            mids = (times[:-1] + times[1:]) / 2
            times = np.sort(np.concatenate([times, mids]))
        elif bpm > 160:
            times = times[::2]
        else:
            break
    return times


def regularize_beats(times):
    """Steady click-like song -> perfectly even grid; otherwise keep the
    performance's drift but remove tracker jitter / outlier beats."""
    times = np.asarray(times, dtype=float)
    n = len(times)
    idx = np.arange(n)
    keep = np.ones(n, dtype=bool)
    for _ in range(3):                          # robust line fit (drop outliers)
        slope, icpt = np.polyfit(idx[keep], times[keep], 1)
        resid = times - (slope * idx + icpt)
        keep = np.abs(resid) < 0.10 * slope
        if keep.sum() < max(4, 0.6 * n):
            keep = np.ones(n, dtype=bool)
            break
    fit = slope * idx + icpt
    resid = times - fit
    if keep.mean() >= 0.85 and np.max(np.abs(resid[keep])) < 0.12 * slope:
        return fit                              # steady: one clean tempo
    resid = np.where(keep, resid, 0.0)          # outliers -> trust the fit
    k = 5                                       # moving-average the deviation
    pad = np.pad(resid, (k // 2, k // 2), mode="edge")
    smooth = np.convolve(pad, np.ones(k) / k, mode="valid")
    return fit + smooth


def extend_beats(times, duration):
    """Make the beat list start at t=0 and run past the end of the audio.

    bt[0] is forced to exactly 0.0, so MIDI tick 0 == audio time 0 and the
    first (pre-roll) beat just absorbs the offset to the first real beat.
    """
    times = np.asarray(times, dtype=float)
    t_head = float(np.median(np.diff(times[:5]))) if len(times) > 5 else float(np.diff(times)[0])
    t_tail = float(np.median(np.diff(times[-5:]))) if len(times) > 5 else t_head
    head = []
    t = times[0]
    while t - t_head >= 0.02:
        t -= t_head
        head.append(t)
    head = head[::-1]
    out = list(head) + list(times)
    t = out[-1]
    while t < duration + 2 * t_tail:
        t += t_tail
        out.append(t)
    out = np.array(out)
    # Real beats stay exactly where they were tracked. If the first one is
    # not at t=0, add a short pre-roll "beat" so MIDI tick 0 == audio time 0
    # (keeps the MIDI in sync with the audio when both start together).
    if out[0] > 0.02:
        out = np.concatenate([[0.0], out])
    else:
        out[0] = 0.0
    return out


def beat_position(t, bt):
    return np.interp(t, bt, np.arange(len(bt)))


# ----------------------------------------------------------------------
# 3. notes (Basic Pitch)
# ----------------------------------------------------------------------
def transcribe_notes(audio_path, onset_threshold=0.45, frame_threshold=0.28,
                     min_note_ms=50.0, log=_noop):
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH

    _, _, events = predict(
        str(audio_path),
        ICASSP_2022_MODEL_PATH,
        onset_threshold=onset_threshold,
        frame_threshold=frame_threshold,
        minimum_note_length=min_note_ms,
        minimum_frequency=65.0,        # ~C2
        maximum_frequency=1500.0,      # ~F#6
        multiple_pitch_bends=False,
        melodia_trick=True,
    )
    notes = [Note(float(s), float(e), int(p), float(a)) for s, e, p, a, *_ in events]
    log(f"[midi_refine] basic-pitch raw notes: {len(notes)}")
    return notes


def clean_notes(notes):
    """Range filter, merge split sustains, drop octave/harmonic ghosts."""
    notes = [n for n in notes if GUITAR_LOW <= n.pitch <= GUITAR_HIGH and n.end > n.start]
    if not notes:
        return notes
    amps = np.array([n.amp for n in notes])
    floor = max(0.12, 0.25 * float(np.percentile(amps, 75)))
    notes = [n for n in notes if n.amp >= floor]

    # merge same-pitch notes separated by a tiny gap (sustain split in two)
    notes.sort(key=lambda n: (n.pitch, n.start))
    merged = []
    for n in notes:
        if merged and merged[-1].pitch == n.pitch and n.start - merged[-1].end < 0.04 \
                and n.amp < 1.15 * merged[-1].amp:      # not a re-pick
            merged[-1].end = max(merged[-1].end, n.end)
        else:
            merged.append(n)

    # drop a note that is a quiet octave/12th above a much louder simultaneous note
    merged.sort(key=lambda n: n.start)
    keep = []
    for i, n in enumerate(merged):
        ghost = False
        for m in merged[max(0, i - 12): i + 12]:
            if m is n:
                continue
            if abs(m.start - n.start) < 0.035 and n.pitch - m.pitch in (12, 19, 24) \
                    and n.amp < 0.55 * m.amp:
                ghost = True
                break
        if not ghost:
            keep.append(n)
    keep.sort(key=lambda n: (n.start, n.pitch))
    return keep


# ----------------------------------------------------------------------
# 4. quantisation
# ----------------------------------------------------------------------
def choose_grid(positions, forced=None):
    """Pick subdivisions-per-beat: 4 = 16ths, 3 = 8th triplets, 6 = 16th
    triplets, 8 = 32nds. Score = mean onset error relative to what random
    onsets would give for that grid (lower is better)."""
    if forced:
        return int(forced)
    pos = np.asarray(positions, dtype=float)
    if len(pos) < 8:
        return 4
    scores = {}
    for sub in (4, 3, 6, 8):
        r = np.abs(pos * sub - np.round(pos * sub)) / sub       # in beats
        scores[sub] = float(np.mean(r)) / (1.0 / (4.0 * sub))   # 1.0 == random
    best = min(scores, key=scores.get)
    # straight 16ths unless another grid is clearly better
    if best != 4 and scores[best] > scores[4] - 0.15:
        best = 4
    # 16th-triplets only if plain 8th-triplets are clearly worse
    if best == 6 and scores[3] < scores[6] + 0.1:
        best = 3
    return best


def quantize(notes, bt, sub):
    step = 1.0 / sub
    for n in notes:
        sb = float(beat_position(n.start, bt))
        eb = float(beat_position(n.end, bt))
        n.qs = round(sb / step) * step
        n.qe = max(n.qs + step, round(eb / step) * step)
    # remove exact duplicates created by snapping
    seen, out = set(), []
    for n in sorted(notes, key=lambda n: (n.qs, n.pitch, -n.amp)):
        k = (round(n.qs / step), n.pitch)
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    # no overlapping repeats of the same pitch
    last = {}
    for n in sorted(out, key=lambda n: n.qs):
        p = last.get(n.pitch)
        if p is not None and p.qe > n.qs:
            p.qe = max(p.qs + step, n.qs)
        last[n.pitch] = n
    return out


# ----------------------------------------------------------------------
# 5. lead / rhythm split
# ----------------------------------------------------------------------
def _pick_lead(group):
    """The melody note of a simultaneous group: the highest pitch among the
    loud notes (so a quiet stray harmonic can't win over the real note)."""
    top_amp = max(n.amp for n in group)
    loud = [n for n in group if n.amp >= 0.6 * top_amp]
    return max(loud, key=lambda n: n.pitch)


def _classify_event(group):
    """Return (lead_notes, rhythm_notes) for one grid slot.

    Notes below LEAD_LOW (bass) or above LEAD_HIGH (overtones) can never be
    lead.  Bass notes go to the rhythm track, overtones are dropped.  The
    remaining in-register notes are classified as before:
      * 3+ packed notes            -> strum (rhythm)
      * 3+ with a separated top    -> chord + lead on top
      * 2 notes, low 5th/octave    -> rhythm
      * 1-2 other notes            -> lead (+ double stop)
    """
    bass = [n for n in group if n.pitch < LEAD_LOW]
    mid = [n for n in group if LEAD_LOW <= n.pitch <= LEAD_HIGH]
    if not mid:
        return [], bass                       # bass-only slot -> rhythm
    ps = sorted(mid, key=lambda n: n.pitch)
    if len(ps) >= 3:
        gap = ps[-1].pitch - ps[-2].pitch
        lead = _pick_lead(mid)
        if gap > 7 and lead is ps[-1]:
            return [lead], bass + [n for n in ps if n is not lead]
        return [], bass + ps
    if len(ps) == 2:
        lo, hi = ps
        if lo.pitch < 60 and (hi.pitch - lo.pitch) in (5, 7, 12, 19):
            return [], bass + ps
        lead = _pick_lead(mid)
        other = lo if lead is hi else hi
        if other.amp >= 0.7 * lead.amp and 3 <= abs(lead.pitch - other.pitch) <= 12:
            return [lead, other], bass
        return [lead], bass
    return [ps[0]], bass


def continuity_filter(lead, window=3, max_dev=9):
    """Drop lead notes that sit far away from their neighbours (a melody
    moves mostly by step; an isolated note > ~a sixth from the median of
    the surrounding notes is almost always a bass/overtone/octave error)."""
    ordered = sorted(lead, key=lambda n: (n.qs, n.pitch))
    keep = []
    for i, n in enumerate(ordered):
        nb = [ordered[j].pitch
              for j in range(max(0, i - window), min(len(ordered), i + window + 1))
              if j != i]
        if not nb or abs(n.pitch - float(np.median(nb))) <= max_dev:
            keep.append(n)
    return keep


def split_lead_rhythm(notes, sub, beats_per_bar=4, log=_noop):
    step = 1.0 / sub
    groups = {}
    for n in notes:
        groups.setdefault(round(n.qs / step), []).append(n)
    ev = [(k * step, groups[k]) for k in sorted(groups)]

    split = [(t, g, *_classify_event(g)) for t, g in ev]

    # bars that are clearly strummed (many strums, few single notes):
    # stray single notes there are muted strums / noise, not melody
    bars = {}
    for t, g, ld, rh in split:
        c = bars.setdefault(int(t // beats_per_bar), [0, 0])
        c[0] += 1
        c[1] += int(bool(rh) and not ld)
    strum_bar = {b: (c[1] >= 4 and c[1] / c[0] >= 0.7) for b, c in bars.items()}

    lead, rhythm = [], []
    for t, g, ld, rh in split:
        if ld and strum_bar.get(int(t // beats_per_bar), False) and len(g) == 1:
            rhythm.extend(ld)
            continue
        lead.extend(ld)
        rhythm.extend(rh)

    # Safety net: a solo track must never come out (almost) empty.
    # If the split starved it, fall back to the plain skyline of everything.
    if len(lead) < max(8, 0.10 * len(ev)):
        log("[midi_refine] few lead notes found -> using skyline of all notes as solo")
        lead = []
        for _, g in ev:
            mid = [n for n in g if LEAD_LOW <= n.pitch <= LEAD_HIGH]
            if mid:
                lead.append(_pick_lead(mid))

    lead = continuity_filter(lead)

    # lead is a single line (plus double stops): cut each note at the next onset
    lead.sort(key=lambda n: (n.qs, -n.pitch))
    onsets = sorted({n.qs for n in lead})
    for n in lead:
        nxt = next((s for s in onsets if s > n.qs + 1e-9), None)
        if nxt is not None:
            n.qe = min(n.qe, nxt)
        if n.qe <= n.qs:
            n.qe = n.qs + step

    # rhythm chords ring until the next chord hit at most
    starts = sorted({n.qs for n in rhythm})
    for n in rhythm:
        nxt = next((s for s in starts if s > n.qs + 1e-9), None)
        if nxt is not None:
            n.qe = min(n.qe, nxt)
        n.qe = min(n.qe, n.qs + 4.0)
        if n.qe <= n.qs:
            n.qe = n.qs + step
    return lead, rhythm


# ----------------------------------------------------------------------
# 6. MIDI writer (with tempo map)
# ----------------------------------------------------------------------
def write_midi(path, tracks, bt, amp_ref=1.0):
    """tracks: list of (name, program, [Note])."""
    import mido

    mid = mido.MidiFile(type=1, ticks_per_beat=PPQ)

    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("track_name", name="Tempo map", time=0))
    meta.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    events, last_us = [], None
    for i in range(len(bt) - 1):
        us = int(round((bt[i + 1] - bt[i]) * 1e6))
        us = max(100000, min(2000000, us)) if i > 0 else max(100000, min(4000000, us))
        if last_us is None or abs(us - last_us) / last_us > 0.002:
            events.append((i * PPQ, us))
            last_us = us
    prev = 0
    for tick, us in events:
        meta.append(mido.MetaMessage("set_tempo", tempo=us, time=tick - prev))
        prev = tick
    mid.tracks.append(meta)

    for name, program, notes in tracks:
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage("track_name", name=name, time=0))
        tr.append(mido.Message("program_change", program=program, channel=0, time=0))
        ev = []
        for n in notes:
            on = int(round(n.qs * PPQ))
            off = max(on + 1, int(round(n.qe * PPQ)))
            vel = int(np.clip(45 + 75 * (n.amp / amp_ref), 35, 120))
            ev.append((on, 1, mido.Message("note_on", note=n.pitch, velocity=vel, channel=0)))
            ev.append((off, 0, mido.Message("note_off", note=n.pitch, velocity=0, channel=0)))
        ev.sort(key=lambda e: (e[0], e[1]))         # note_off before note_on
        prev = 0
        for tick, _, msg in ev:
            msg.time = tick - prev
            prev = tick
            tr.append(msg)
        tr.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(tr)
    mid.save(str(path))


# ----------------------------------------------------------------------
# public entry point
# ----------------------------------------------------------------------
def refine_transcription(audio_path, out_dir, *, drums_path=None,
                         tempo_hint=None, grid=None, progress=_noop, log=print):
    """Run beat tracking + Basic Pitch + quantisation and write the MIDIs.

    audio_path : guitar-focused audio (guitar stem, or the instrumental/full mix)
    drums_path : optional drum stem, used only for beat tracking
    grid       : force 4 (16ths) / 3 (8th triplets) / 6 / 8, else auto
    Returns a dict with tempo, grid, counts and the files written.
    """
    import librosa

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    progress("Tracking beats...")
    y_beat = librosa.load(str(drums_path or audio_path), sr=SR, mono=True)[0]
    duration = len(y_beat) / SR
    raw_beats = track_beats(y_beat, SR, tempo_hint)
    bt = extend_beats(regularize_beats(raw_beats), duration)
    bpm = 60.0 / float(np.median(np.diff(bt[1:-1])))
    steady = np.std(np.diff(bt[1:-1])) < 0.004 * np.median(np.diff(bt[1:-1]))

    progress("Detecting notes (polyphonic)...")
    notes = clean_notes(transcribe_notes(audio_path, log=log))
    if not notes:
        raise RuntimeError("No guitar notes were detected in the audio.")

    progress("Quantising to the beat grid...")
    pos = [float(beat_position(n.start, bt)) for n in notes if n.amp >= 0.3] or \
          [float(beat_position(n.start, bt)) for n in notes]
    sub = choose_grid(pos, grid)
    notes = quantize(notes, bt, sub)
    lead, rhythm = split_lead_rhythm(notes, sub, log=log)

    amp_ref = float(np.percentile([n.amp for n in notes], 95)) or 1.0
    files = {}
    progress("Writing MIDI files...")
    files["solo"] = out_dir / "03_GUITAR_SOLO.mid"
    write_midi(files["solo"], [("Lead / Solo", 29, lead)], bt, amp_ref)
    files["rhythm"] = out_dir / "04_RHYTHM_CHORDS.mid"
    write_midi(files["rhythm"], [("Rhythm", 27, rhythm)], bt, amp_ref)
    files["full"] = out_dir / "07_GUITAR_FULL.mid"
    write_midi(files["full"], [("Lead / Solo", 29, lead), ("Rhythm", 27, rhythm)], bt, amp_ref)

    # solo notes back in seconds (grid-snapped) so MUSIC.py's TAB / piano /
    # fingerstyle PDFs are built from the same notes as the MIDI
    beat_idx = np.arange(len(bt))
    lead_notes = [
        {"start": float(np.interp(n.qs, beat_idx, bt)),
         "end":   float(np.interp(n.qe, beat_idx, bt)),
         "midi":  int(n.pitch)}
        for n in sorted(lead, key=lambda n: (n.qs, n.pitch))
    ]

    info = dict(
        lead_notes=lead_notes,
        tempo_bpm=round(bpm, 1),
        tempo_steady=bool(steady),
        grid={4: "16th notes", 3: "8th-note triplets", 6: "16th-note triplets", 8: "32nd notes"}.get(sub, str(sub)),
        n_lead=len(lead), n_rhythm=len(rhythm), n_total=len(notes),
        files={k: str(v) for k, v in files.items()},
    )
    log(f"[midi_refine] {info}")
    return info