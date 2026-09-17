"""
subtitle_engine.py
Pure-logic port of Create_SRT.py — no GUI dependencies.
Every method that used to call self.update_progress(x, msg) now takes
progress_cb / status_callback and calls it instead.
"""
import os
import sys
import shutil
import subprocess
import tempfile
import re
from pathlib import Path

import whisper


class SubtitleEngine:
    # ---------- constants (copied from original) ----------
    MAX_CHARS_PER_CUE = 42
    MAX_DURATION_SECONDS = 6.0
    MAX_GAP_SECONDS = 0.6
    MIN_INSTRUMENTAL_GAP_SECONDS = 3.0

    MUSIC_HALLUCINATION_TOKENS = {
        "موسيقى", "موسيقي", "music", "[music]", "(music)", "♪", "♪♪",
    }

    LANGUAGE_PROMPTS = {
        "ar": "الكلام ده أغنية باللهجة العامية المصرية، والكلام واضح ومكتوب صح.",
    }

    SENTENCE_END_CHARS = ('.', '!', '?', '؟', '۔')

    NOTE_ORDER = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

    STRING_ORDER = ['E', 'A', 'D', 'G', 'B', 'e']
    STRING_OPEN_MIDI = {'E': 40, 'A': 45, 'D': 50, 'G': 55, 'B': 59, 'e': 64}
    MAX_FRET = 22

    VIDEO_EXTENSIONS = ('.mp4', '.mov', '.mkv', '.webm', '.avi')

    # ============================================================
    # PUBLIC ENTRY POINTS
    # ============================================================

    def generate_srt(self, input_path, output_path, *,
                     model_size="tiny", language="auto",
                     isolate_vocals=True, progress_cb=None):
        def _p(p, m):
            if progress_cb:
                progress_cb(p, m)

        _p(5, "Loading Whisper model (first run downloads it)...")
        model = whisper.load_model(model_size)

        _p(10, "Preparing audio...")
        temp_audio_path = None
        if input_path.lower().endswith(('.mp4', '.mov', '.mkv', '.webm', '.avi')):
            if shutil.which('ffmpeg') is None:
                raise RuntimeError("ffmpeg is required to extract audio from video.")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
            tmp.close()
            temp_audio_path = tmp.name
            cmd = ['ffmpeg', '-y', '-i', input_path, '-vn',
                   '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
                   temp_audio_path]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg failed: {proc.stderr[-800:]}")
            audio_path = temp_audio_path
        else:
            audio_path = input_path

        demucs_vocals_dir = None
        transcribe_audio_path = audio_path

        if isolate_vocals:
            if shutil.which('ffmpeg') is None:
                _p(15, "ffmpeg not found — skipping vocal isolation.")
            else:
                try:
                    _p(15, "Isolating vocals with Demucs...")
                    vocals_path, demucs_vocals_dir = self.separate_vocals_stem(
                        audio_path, status_callback=_p
                    )
                    transcribe_audio_path = vocals_path
                except Exception as e:
                    _p(15, f"Vocal isolation skipped ({e}). Using full mix.")
                    transcribe_audio_path = audio_path

        _p(25, "Transcribing (this is the slow step)...")
        whisper_language = None if language == "auto" else language
        initial_prompt = self.LANGUAGE_PROMPTS.get(whisper_language)

        try:
            result = model.transcribe(
                transcribe_audio_path,
                language=whisper_language,
                word_timestamps=True,
                task="transcribe",
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
                beam_size=5,
                best_of=5,
                temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                compression_ratio_threshold=2.6,
                logprob_threshold=-1.2,
                no_speech_threshold=0.5,
            )

            _p(85, "Formatting subtitles...")
            total_duration = self.get_audio_duration(audio_path)
            srt_content = self.create_srt(result["segments"], total_duration=total_duration)

            os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(srt_content)

            _p(100, "SRT ready")
            return srt_content

        finally:
            if temp_audio_path and os.path.exists(temp_audio_path):
                try:
                    os.unlink(temp_audio_path)
                except OSError:
                    pass
            if demucs_vocals_dir:
                shutil.rmtree(demucs_vocals_dir, ignore_errors=True)

    # ============================================================
    # TIMING HELPERS
    # ============================================================

    def get_audio_duration(self, file_path):
        if shutil.which('ffprobe') is None:
            return None
        try:
            cmd = ['ffprobe', '-v', 'error',
                   '-show_entries', 'format=duration',
                   '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0 and proc.stdout.strip():
                return float(proc.stdout.strip())
        except Exception:
            pass
        return None

    def format_timestamp(self, seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    # ============================================================
    # SRT BUILDING
    # ============================================================

    def create_srt(self, segments, total_duration=None):
        words = []
        for segment in segments:
            seg_words = segment.get('words')
            if seg_words:
                for w in seg_words:
                    text = w.get('word', '').strip()
                    if text:
                        words.append({'text': text, 'start': w['start'], 'end': w['end']})
            else:
                text = segment['text'].strip()
                if text:
                    words.append({'text': text, 'start': segment['start'], 'end': segment['end']})

        if not words:
            if total_duration and total_duration > 0:
                return self._render_cues([{
                    'start': 0.0, 'end': total_duration,
                    'text': "[Instrumental]", 'is_instrumental': True,
                }])
            return ""

        cues, current = [], []

        def cur_len(lst):
            return sum(len(w['text']) + 1 for w in lst)

        for w in words:
            if current:
                gap = w['start'] - current[-1]['end']
                proj_len = cur_len(current) + len(w['text']) + 1
                proj_dur = w['end'] - current[0]['start']
                ends_sentence = current[-1]['text'].endswith(self.SENTENCE_END_CHARS)
                if (gap > self.MAX_GAP_SECONDS
                        or proj_len > self.MAX_CHARS_PER_CUE
                        or proj_dur > self.MAX_DURATION_SECONDS
                        or ends_sentence):
                    cues.append(current)
                    current = []
            current.append(w)
        if current:
            cues.append(current)

        entries = []
        for cue_words in cues:
            start = cue_words[0]['start']
            end = cue_words[-1]['end']
            text = " ".join(w['text'] for w in cue_words).strip()
            normalized = text.strip(" .!?؟۔-–—").lower()
            if normalized in self.MUSIC_HALLUCINATION_TOKENS:
                entries.append({'start': start, 'end': end, 'text': "[Instrumental]",
                                'is_instrumental': True})
            else:
                entries.append({'start': start, 'end': end, 'text': text,
                                'is_instrumental': False})

        entries = self._fill_instrumental_gaps(entries, total_duration)
        entries = self._merge_adjacent_instrumentals(entries)
        return self._render_cues(entries)

    def _fill_instrumental_gaps(self, entries, total_duration):
        if not entries:
            return entries
        filled = []
        t = self.MIN_INSTRUMENTAL_GAP_SECONDS
        if entries[0]['start'] > t:
            filled.append({'start': 0.0, 'end': entries[0]['start'],
                           'text': "[Instrumental Intro]", 'is_instrumental': True})
        for i, entry in enumerate(entries):
            filled.append(entry)
            if i + 1 < len(entries):
                gap = entries[i + 1]['start'] - entry['end']
                if gap > t:
                    filled.append({'start': entry['end'], 'end': entries[i + 1]['start'],
                                   'text': "[Instrumental]", 'is_instrumental': True})
        if total_duration and total_duration - entries[-1]['end'] > t:
            filled.append({'start': entries[-1]['end'], 'end': total_duration,
                           'text': "[Instrumental Outro]", 'is_instrumental': True})
        return filled

    @staticmethod
    def _merge_adjacent_instrumentals(entries):
        merged = []
        for e in entries:
            if merged and e.get('is_instrumental') and merged[-1].get('is_instrumental'):
                merged[-1]['end'] = e['end']
            else:
                merged.append(dict(e))
        return merged

    def _render_cues(self, entries):
        lines = []
        for i, e in enumerate(entries, 1):
            lines.append(str(i))
            lines.append(f"{self.format_timestamp(e['start'])} --> {self.format_timestamp(e['end'])}")
            lines.append(e['text'])
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _parse_srt_cues(srt_content):
        ts_re = re.compile(
            r'(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})'
        )

        def to_seconds(h, m, s, ms):
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

        lines = srt_content.splitlines()
        cues, i = [], 0
        while i < len(lines):
            m = ts_re.search(lines[i])
            if m:
                h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
                start = to_seconds(h1, m1, s1, ms1)
                end = to_seconds(h2, m2, s2, ms2)
                i += 1
                parts = []
                while i < len(lines) and lines[i].strip() != '':
                    parts.append(lines[i].strip())
                    i += 1
                text = " ".join(parts).strip()
                if text:
                    cues.append({'start': start, 'end': end, 'text': text})
            else:
                i += 1
        return cues

    # ============================================================
    # CHORD DETECTION
    # ============================================================

    def detect_chords_enhanced(self, input_path, cues, method="advanced"):
        if method == "madmom":
            try:
                chords = self.detect_chords_madmom(input_path, cues)
                if chords and any(c is not None for c in chords):
                    return chords
            except Exception as e:
                print(f"madmom failed: {e}, falling back")
                method = "basic"

        if method == "advanced":
            try:
                chords = self.detect_chords_advanced(input_path, cues)
                if chords and any(c is not None for c in chords):
                    return chords
            except Exception as e:
                print(f"advanced failed: {e}, falling back")
                method = "basic"

        if method == "hybrid":
            try:
                c1 = self.detect_chords_madmom(input_path, cues)
                c2 = self.detect_chords_basic(input_path, cues)
                if c1 and any(c is not None for c in c1):
                    return self._merge_chord_detections(c1, c2, cues)
                elif c2 and any(c is not None for c in c2):
                    return c2
            except Exception as e:
                print(f"hybrid failed: {e}, falling back")
                method = "basic"

        return self.detect_chords_basic(input_path, cues)

    _CHORD_TEMPLATES = {
        'C': [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0],
        'C#': [0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0],
        'D': [0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0, 0],
        'D#': [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0],
        'E': [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1],
        'F': [1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0],
        'F#': [0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0],
        'G': [0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1],
        'G#': [1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0],
        'A': [0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0],
        'A#': [0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 0],
        'B': [0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1],
        'Cm': [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
        'C#m': [0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
        'Dm': [0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0],
        'D#m': [0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0],
        'Em': [0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1],
        'Fm': [1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0],
        'F#m': [0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0],
        'Gm': [0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0],
        'G#m': [0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1],
        'Am': [1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0],
        'A#m': [0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0],
        'Bm': [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        'C7': [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0],
        'D7': [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
        'E7': [0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1],
        'G7': [0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0],
        'A7': [1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0],
    }

    def _chord_match(self, chroma, sr, hop, cues, smooth=False):
        import numpy as np
        if smooth:
            from scipy.ndimage import gaussian_filter1d
            chroma = gaussian_filter1d(chroma, sigma=2, axis=1)

        names = list(self._CHORD_TEMPLATES.keys())
        mat = np.array(list(self._CHORD_TEMPLATES.values()), dtype=float)
        mat = mat / (np.sum(mat, axis=1, keepdims=True) + 1e-8)

        def best(start, end):
            sf = min(int(start * sr / hop), chroma.shape[1] - 1)
            ef = min(int(end * sr / hop), chroma.shape[1] - 1)
            if sf >= ef:
                return None
            vec = np.mean(chroma[:, sf:ef], axis=1)
            vec = vec / (np.sum(vec) + 1e-8)
            sims = mat @ vec
            idx = int(np.argmax(sims))
            return names[idx] if sims[idx] > 0.10 else None

        return [best(c['start'], c['end']) for c in cues]

    def detect_chords_basic(self, input_path, cues):
        try:
            import librosa
            y, sr = librosa.load(input_path, sr=22050, mono=True)
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
            return self._chord_match(chroma, sr, 512, cues)
        except ImportError:
            return self._generate_simulated_chords(cues)
        except Exception as e:
            print(f"basic chord detection failed: {e}")
            return self._generate_simulated_chords(cues)

    def detect_chords_advanced(self, input_path, cues):
        try:
            import librosa
            import numpy as np
            y, sr = librosa.load(input_path, sr=22050, mono=True)
            cqt = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
            stft = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=512)
            chroma = 0.7 * cqt + 0.3 * stft
            chords = self._chord_match(chroma, sr, 512, cues, smooth=True)
            prev = None
            for i, c in enumerate(chords):
                if c is None and prev is not None:
                    chords[i] = prev
                elif c is not None:
                    prev = c
            return chords
        except Exception as e:
            print(f"advanced failed: {e}")
            return self._generate_simulated_chords(cues)

    def detect_chords_madmom(self, input_path, cues):
        try:
            from madmom.audio.chroma import DeepChromaProcessor
            from madmom.features.chords import DeepChromaChordRecognitionProcessor
            wav_path, is_temp = self._ensure_wav_for_analysis(input_path)
            try:
                proc = DeepChromaChordRecognitionProcessor()
                chroma = DeepChromaProcessor()(wav_path)
                segments = proc(chroma)
                return [self._chord_label_for_span(segments, c['start'], c['end']) for c in cues]
            finally:
                if is_temp and os.path.exists(wav_path):
                    try:
                        os.unlink(wav_path)
                    except OSError:
                        pass
        except ImportError:
            print("madmom not available")
            return [None] * len(cues)
        except Exception as e:
            print(f"madmom failed: {e}")
            return [None] * len(cues)

    def _merge_chord_detections(self, c1, c2, cues):
        out = []
        for a, b in zip(c1, c2):
            if a == b:
                out.append(a)
            elif a is not None and b is not None:
                out.append(a if len(str(a)) >= len(str(b)) else b)
            else:
                out.append(a or b)
        return out

    def _ensure_wav_for_analysis(self, input_path):
        if input_path.lower().endswith('.wav'):
            return input_path, False
        if shutil.which('ffmpeg') is None:
            raise RuntimeError("ffmpeg is required for chord analysis.")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        tmp.close()
        cmd = ['ffmpeg', '-y', '-i', input_path, '-vn', '-ac', '1', '-ar', '44100', tmp.name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[-800:]}")
        return tmp.name, True

    @staticmethod
    def _madmom_label_to_chord_name(label):
        if not label or label == 'N':
            return None
        if ':' not in label:
            return label
        root, quality = label.split(':', 1)
        quality = quality.lower()
        if quality in ('maj', ''):
            return root
        if quality == 'min':
            return f"{root}m"
        if quality.startswith('maj'):
            return f"{root}{quality[3:]}"
        if quality.startswith('min'):
            return f"{root}m{quality[3:]}"
        return f"{root}{quality}"

    @staticmethod
    def _chord_label_for_span(segments, cue_start, cue_end):
        best, best_ov = None, 0.0
        for seg in segments:
            s, e, label = float(seg['start']), float(seg['end']), str(seg['label'])
            ov = min(e, cue_end) - max(s, cue_start)
            if ov > best_ov:
                best_ov = ov
                best = label
        return SubtitleEngine._madmom_label_to_chord_name(best)

    def _generate_simulated_chords(self, cues):
        import random
        progressions = [
            ['C', 'G', 'Am', 'F'], ['G', 'D', 'Em', 'C'],
            ['D', 'A', 'Bm', 'G'], ['A', 'E', 'F#m', 'D'],
            ['E', 'B', 'C#m', 'A'], ['C', 'F', 'G', 'C'],
            ['G', 'C', 'D', 'G'],
        ]
        prog = random.choice(progressions)
        return [prog[i % len(prog)] for i in range(len(cues))]

    # ============================================================
    # LYRICS + MP3 EXPORT
    # ============================================================

    def export_lyrics_and_mp3(self, input_path, cues, song_title,
                              pdf_path, mp3_path,
                              chord_method="advanced",
                              status_callback=None):
        def _s(msg):
            if status_callback:
                status_callback(msg)

        _s("Detecting chords...")
        chords = self.detect_chords_enhanced(input_path, cues, method=chord_method)
        detected = sum(1 for c in chords if c is not None)

        cue_lines = []
        for i, cue in enumerate(cues):
            chord = chords[i] if i < len(chords) else None
            cue_lines.append((str(chord) if chord else "", cue['text']))

        _s("Writing lyrics PDF...")
        try:
            self.build_lyrics_pdf(song_title, cue_lines, pdf_path)
        except ImportError:
            raise RuntimeError(
                "reportlab is required for PDF generation.\nInstall: pip install reportlab"
            )

        _s("Exporting MP3...")
        if shutil.which('ffmpeg') is None:
            raise RuntimeError("ffmpeg is required to export MP3.")
        if input_path.lower().endswith('.mp3'):
            shutil.copy2(input_path, mp3_path)
        else:
            self._convert_audio_to_mp3(input_path, mp3_path)

        _s("Done")
        return detected, len(cues)

    def _convert_audio_to_mp3(self, input_path, mp3_path):
        cmd = ['ffmpeg', '-y', '-i', input_path, '-vn',
               '-c:a', 'libmp3lame', '-q:a', '0',
               '-map_metadata', '0', mp3_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg mp3 failed:\n{proc.stderr[-800:]}")

    # ============================================================
    # LYRICS PDF BUILDER (chord diagrams above words)
    # ============================================================

    @classmethod
    def _parse_chord_name(cls, name):
        if not name:
            return None, None
        name = name.strip()
        if len(name) > 1 and name[1] == '#':
            root, suffix = name[:2], name[2:]
        else:
            root, suffix = name[:1], name[1:]
        if root not in cls.NOTE_ORDER:
            return None, None
        if suffix.startswith('m') and not suffix.startswith('maj'):
            quality = 'm'
        elif suffix.startswith('7'):
            quality = '7'
        else:
            quality = ''
        return root, quality

    @classmethod
    def _get_chord_fingering(cls, name):
        root, quality = cls._parse_chord_name(name)
        if root is None:
            return None
        root_idx = cls.NOTE_ORDER.index(root)
        e_off = (root_idx - cls.NOTE_ORDER.index('E')) % 12
        a_off = (root_idx - cls.NOTE_ORDER.index('A')) % 12
        e_shapes = {'': [0, 2, 2, 1, 0, 0], 'm': [0, 2, 2, 0, 0, 0], '7': [0, 2, 0, 1, 0, 0]}
        a_shapes = {'': [None, 0, 2, 2, 2, 0], 'm': [None, 0, 2, 2, 1, 0],
                    '7': [None, 0, 2, 0, 2, 0]}
        use_e = e_off <= a_off
        offset = e_off if use_e else a_off
        shape = (e_shapes if use_e else a_shapes)[quality]
        return [None if f is None else f + offset for f in shape]

    def _draw_chord_diagram(self, c, x, y, name, frets):
        from reportlab.lib.colors import black
        w, gap = 62, 62 / 5
        fret_gap = (78 - 14) / 4
        top = y - 14
        left = x

        fretted = [f for f in frets if f not in (None, 0)]
        start_fret = min(fretted) if fretted else 1
        if start_fret < 1:
            start_fret = 1

        c.setFont("Helvetica-Bold", 12)
        c.setFillColor(black)
        c.drawCentredString(left + w / 2, y + 4, name)

        c.setFont("Helvetica-Bold", 8)
        for i, f in enumerate(frets):
            sx = left + i * gap
            if f is None:
                c.drawCentredString(sx, top + 4, "X")
            elif f == 0:
                c.drawCentredString(sx, top + 4, "O")

        if start_fret == 1:
            c.setLineWidth(2.5)
            c.line(left, top, left + w, top)
        else:
            c.setLineWidth(1)
            c.line(left, top, left + w, top)
            c.setFont("Helvetica", 7)
            c.drawString(left + w + 3, top - 8, f"{start_fret}fr")

        c.setLineWidth(1)
        for row in range(1, 5):
            yy = top - row * fret_gap
            c.line(left, yy, left + w, yy)
        for i in range(6):
            sx = left + i * gap
            c.line(sx, top, sx, top - 4 * fret_gap)

        barre = [i for i, f in enumerate(frets) if f == start_fret]
        row1_y = top - fret_gap / 2
        drawn = set()
        if len(barre) >= 3:
            c.setLineWidth(7)
            c.line(left + min(barre) * gap, row1_y, left + max(barre) * gap, row1_y)
            c.setLineWidth(1)
            drawn = set(barre)

        for i, f in enumerate(frets):
            if f in (None, 0) or i in drawn:
                continue
            rel = f - start_fret + 1
            sx = left + i * gap
            dy = top - (rel - 0.5) * fret_gap
            c.setFillColor(black)
            c.circle(sx, dy, 4.2, fill=1, stroke=0)
        return y - 78

    def _draw_chord_reference(self, c, chord_names, top_y, left_margin, content_width):
        from reportlab.lib.colors import HexColor
        w, gap = 62, 26
        col_w = w + gap
        per_row = max(1, int((content_width + gap) // col_w))

        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(HexColor("#555555"))
        c.drawString(left_margin, top_y, "Chord Reference")

        y = top_y - 24
        col = 0
        bottom = y - 78
        for name in chord_names:
            shape = self._get_chord_fingering(name)
            if not shape:
                continue
            x = left_margin + col * col_w
            b = self._draw_chord_diagram(c, x, y, name, shape)
            bottom = min(bottom, b)
            col += 1
            if col >= per_row:
                col = 0
                y = b - 30
                bottom = y - 78
        return bottom

    def build_lyrics_pdf(self, song_title, cue_lines, pdf_path):
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.colors import HexColor, black
        from reportlab.lib.units import inch
        from datetime import datetime

        def is_arabic(t):
            return bool(re.search(r'[\u0600-\u06FF]', t or ""))

        def rtl(t):
            return ' '.join(w[::-1] for w in t.split()) if t else t

        LEFT = RIGHT = TOP = BOTTOM = 0.8 * inch
        LYRIC_FONT, CHORD_FONT = "Helvetica", "Helvetica-Bold"
        LYRIC_SIZE, CHORD_SIZE = 13, 11
        CHORD_COLOR = HexColor('#ff3b30')
        LINE_GAP = 34
        MERGE_GAP = 8
        CHORD_OFFSET = 14

        pw, ph = letter
        content_w = pw - LEFT - RIGHT
        c = canvas.Canvas(pdf_path, pagesize=letter)
        y = ph - TOP

        def draw_header(first):
            nonlocal y
            if first:
                if is_arabic(song_title):
                    c.setFont("Helvetica-Bold", 24)
                    c.setFillColor(black)
                    c.drawRightString(pw - LEFT, ph - 70, rtl(song_title))
                else:
                    c.setFont("Helvetica-Bold", 24)
                    c.setFillColor(black)
                    c.drawCentredString(pw / 2, ph - 70, song_title)

                c.setFont("Helvetica-Oblique", 11)
                c.setFillColor(HexColor('#555555'))
                c.drawCentredString(pw / 2, ph - 88, "Lyrics & Chords")

                c.setFont("Helvetica", 9)
                c.setFillColor(HexColor('#888888'))
                c.drawCentredString(pw / 2, ph - 104,
                                    f"Auto-transcribed · {datetime.now().strftime('%Y-%m-%d')}")

                c.setStrokeColor(HexColor('#cccccc'))
                c.setLineWidth(1)
                c.line(pw * 0.2, ph - 116, pw * 0.8, ph - 116)

                unique_chords = []
                for ch, _ in cue_lines:
                    if ch and ch.strip() and ch not in unique_chords:
                        if self._get_chord_fingering(ch):
                            unique_chords.append(ch)

                if unique_chords:
                    bottom = self._draw_chord_reference(c, unique_chords,
                                                       ph - 146, LEFT, content_w)
                    y = bottom - 55
                else:
                    y = ph - 150
            else:
                y = ph - TOP

        draw_header(True)

        def ensure_space(need=LINE_GAP):
            nonlocal y
            if y - need < BOTTOM:
                c.showPage()
                draw_header(False)

        cue_texts = [(ch, t) for ch, t in cue_lines if t and t.strip()]

        i = 0
        while i < len(cue_texts):
            ch_a, t_a = cue_texts[i]
            words_a = t_a.split()
            word_info = []
            total_w = 0
            for idx, w in enumerate(words_a):
                chord = ch_a if idx == 0 and ch_a.strip() else None
                ww = c.stringWidth(w + " ", LYRIC_FONT, LYRIC_SIZE)
                word_info.append((chord, w, ww, False))
                total_w += ww

            if i + 1 < len(cue_texts):
                ch_b, t_b = cue_texts[i + 1]
                word_info.append(("__GAP__", None, MERGE_GAP, True))
                total_w += MERGE_GAP
                for idx, w in enumerate(t_b.split()):
                    chord = ch_b if idx == 0 and ch_b.strip() else None
                    ww = c.stringWidth(w + " ", LYRIC_FONT, LYRIC_SIZE)
                    word_info.append((chord, w, ww, False))
                    total_w += ww
                i += 2
            else:
                i += 1

            ensure_space(LINE_GAP)
            row_is_ar = any(w and is_ar(w) for _, w, _, _ in word_info if w)
            cursor_x = (pw - RIGHT - total_w) if row_is_ar else LEFT

            for chord, word, wwidth, is_gap in word_info:
                if is_gap:
                    if not row_is_ar:
                        cursor_x += wwidth
                    continue
                if not word:
                    continue
                if not row_is_ar and cursor_x + wwidth > LEFT + content_w and cursor_x > LEFT:
                    y -= LINE_GAP
                    ensure_space(LINE_GAP)
                    cursor_x = LEFT

                if chord and chord.strip():
                    c.setFont(CHORD_FONT, CHORD_SIZE)
                    c.setFillColor(CHORD_COLOR)
                    c.drawString(cursor_x, y + CHORD_OFFSET, chord)

                c.setFont(LYRIC_FONT, LYRIC_SIZE)
                c.setFillColor(black)
                c.drawString(cursor_x, y, word)
                cursor_x += wwidth

            y -= LINE_GAP

        c.showPage()
        c.save()

    # ============================================================
    # GUITAR TAB PIPELINE
    # ============================================================

    def export_guitar_tab_pipeline(self, input_path, start_sec, end_sec,
                                   song_title, pdf_path,
                                   use_demucs=True, progress_cb=None):
        def _p(p, m):
            if progress_cb:
                progress_cb(p, m)

        temp_paths, temp_dirs = [], []
        try:
            _p(5, "Trimming audio...")
            trimmed = self._trim_audio_segment(input_path, start_sec, end_sec)
            temp_paths.append(trimmed)

            if use_demucs:
                _p(15, "Isolating guitar with Demucs (AI)...")
                guitar, out_dir = self.separate_guitar_stem(
                    trimmed,
                    status_callback=lambda m: _p(30, m)
                )
                temp_dirs.append(out_dir)
                audio = guitar
            else:
                audio = trimmed

            _p(50, "Transcribing guitar notes...")
            phrases = self.transcribe_guitar_solo(
                audio, 0.0, None,
                status_callback=lambda m: _p(60, m)
            )

            for phrase in phrases:
                for n in phrase:
                    n['start'] += start_sec
                    n['end'] += start_sec

            _p(85, "Rendering TAB PDF...")
            self.build_guitar_tab_pdf(song_title, phrases,
                                      start_sec, end_sec, pdf_path)
            _p(100, "Guitar tab ready")
        finally:
            for p in temp_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            for d in temp_dirs:
                shutil.rmtree(d, ignore_errors=True)

    def _trim_audio_segment(self, input_path, start_sec, end_sec):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        tmp.close()
        cmd = ['ffmpeg', '-y', '-i', input_path,
               '-ss', str(max(0.0, start_sec)), '-to', str(end_sec),
               '-vn', tmp.name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise RuntimeError(f"ffmpeg failed to trim:\n{proc.stderr[-800:]}")
        return tmp.name

    def separate_guitar_stem(self, input_path, status_callback=None):
        if shutil.which('ffmpeg') is None:
            raise RuntimeError("Demucs requires ffmpeg on PATH.")
        if status_callback:
            status_callback("Isolating guitar (2-5 min)...")
        out_dir = tempfile.mkdtemp(prefix='demucs_guitar_')
        cmd = [sys.executable, '-m', 'demucs', '-n', 'htdemucs_6s',
               '--two-stems=guitar', '-o', out_dir, input_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError(
                f"Demucs failed:\n{proc.stderr[-800:]}\n\n"
                "Install with: pip install demucs"
            )
        matches = list(Path(out_dir).rglob("guitar.wav"))
        if not matches:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError("Demucs produced no guitar.wav. See server logs.")
        return str(matches[0]), out_dir

    def separate_vocals_stem(self, input_path, status_callback=None):
        if shutil.which('ffmpeg') is None:
            raise RuntimeError("Demucs requires ffmpeg on PATH.")
        if status_callback:
            status_callback("Isolating vocals (1-3 min)...")
        out_dir = tempfile.mkdtemp(prefix='demucs_vocals_')
        cmd = [sys.executable, '-m', 'demucs', '-n', 'htdemucs',
               '--two-stems=vocals', '-o', out_dir, input_path]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError(
                f"Demucs failed:\n{proc.stderr[-800:]}\n\n"
                "Install with: pip install demucs"
            )
        matches = list(Path(out_dir).rglob("vocals.wav"))
        if not matches:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise RuntimeError("Demucs produced no vocals.wav.")
        return str(matches[0]), out_dir

    def transcribe_guitar_solo(self, audio_path, start_time, end_time,
                               status_callback=None):
        times, midi, voiced, onsets = self._extract_pitch_track(
            audio_path, start_time, end_time, status_callback
        )
        if status_callback:
            status_callback("Detecting notes...")
        notes = self._pitchtrack_to_notes(times, midi, voiced)
        if not notes:
            raise RuntimeError("No clear guitar notes detected.")
        notes = self._split_internal_legato(notes)
        for n in notes:
            n['_art_info'] = self._classify_articulation(n)
        notes = self._assign_frets(notes)
        for n in notes:
            art = n.pop('_art_info', {'type': None})
            if art['type'] == 'bend':
                open_midi = self.STRING_OPEN_MIDI[self.STRING_ORDER[n['string']]]
                target = art['target_midi'] - open_midi
                n['articulation'] = f"b{target}" + ('r' if art['released'] else '')
            elif art['type'] == 'vibrato':
                n['articulation'] = '~'
            else:
                n['articulation'] = ''
        notes = self._detect_legato_transitions(notes, onsets)
        for n in notes:
            n['start'] += start_time
            n['end'] += start_time
        return self._group_into_phrases(notes)

    def _extract_pitch_track(self, audio_path, start_time, end_time,
                             status_callback=None):
        if status_callback:
            status_callback("Loading audio...")
        try:
            import librosa
            import numpy as np
        except ImportError:
            raise RuntimeError("librosa/numpy required. pip install librosa numpy")
        duration = None if end_time is None else max(0.1, end_time - start_time)
        y, sr = librosa.load(audio_path, sr=22050, mono=True,
                             offset=max(0.0, start_time), duration=duration)
        if y.size == 0:
            raise RuntimeError("No audio loaded.")
        if status_callback:
            status_callback("Tracking pitch (this can take a minute)...")
        hop = 512
        f0, voiced, _ = librosa.pyin(
            y, fmin=librosa.note_to_hz('E2'),
            fmax=librosa.note_to_hz('E6'),
            sr=sr, hop_length=hop
        )
        times = librosa.times_like(f0, sr=sr, hop_length=hop)
        midi = np.full_like(f0, np.nan, dtype=float)
        valid = ~np.isnan(f0)
        midi[valid] = librosa.hz_to_midi(f0[valid])
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        onset_env = np.clip(np.diff(rms, prepend=rms[0]), 0, None)
        onset_frames = librosa.util.peak_pick(
            onset_env, pre_max=3, post_max=3, pre_avg=5, post_avg=5,
            delta=0.015, wait=5
        )
        onset_times = librosa.frames_to_time(onset_frames, sr=sr,
                                             hop_length=hop).tolist()
        return times, midi, voiced, onset_times

    def _pitchtrack_to_notes(self, times, midi, voiced,
                             min_dur=0.06, semitone_thresh=1.4):
        import numpy as np
        notes, cur = [], None
        for t, m, v in zip(times, midi, voiced):
            is_v = bool(v) and not np.isnan(m)
            if not is_v:
                if cur is not None:
                    notes.append(cur)
                    cur = None
                continue
            if cur is None:
                cur = {'start': float(t), 'end': float(t), 'pitches': [float(m)]}
            elif abs(m - cur['pitches'][-1]) < semitone_thresh:
                cur['end'] = float(t)
                cur['pitches'].append(float(m))
            else:
                notes.append(cur)
                cur = {'start': float(t), 'end': float(t), 'pitches': [float(m)]}
        if cur is not None:
            notes.append(cur)

        result = []
        for n in notes:
            if n['end'] - n['start'] < min_dur:
                continue
            p = n['pitches']
            skip = min(1, max(0, len(p) - 1))
            window = sorted(p[skip:skip + 3]) or p[:1]
            n['base_midi'] = round(window[len(window) // 2])
            result.append(n)
        return result

    def _split_internal_legato(self, notes, min_total=1.8):
        out = []
        for n in notes:
            p = n['pitches']
            if len(p) < 6:
                out.append(n)
                continue
            base = n['base_midi']
            rel = [x - base for x in p]
            net = rel[-1]
            peak = max(rel, key=abs)
            released = abs(peak) >= 0.5 and abs(net) < abs(peak) * 0.4
            if released or abs(net) < min_total:
                out.append(n)
                continue
            deltas = [abs(p[i + 1] - p[i]) for i in range(len(p) - 1)]
            start = 1 if len(deltas) > 3 else 0
            split = deltas.index(max(deltas[start:]), start) + 1
            if split < 2 or split > len(p) - 2:
                out.append(n)
                continue
            first, second = p[:split], p[split:]
            fb = sorted(first[:3]); sb = sorted(second[-3:])
            b1 = round(fb[len(fb) // 2]); b2 = round(sb[len(sb) // 2])
            if b1 == b2:
                out.append(n)
                continue
            t_split = n['start'] + (split / len(p)) * (n['end'] - n['start'])
            n1 = {'start': n['start'], 'end': t_split, 'pitches': first, 'base_midi': b1}
            n2 = {'start': t_split, 'end': n['end'], 'pitches': second, 'base_midi': b2}
            out.extend(self._split_internal_legato([n1], min_total))
            out.extend(self._split_internal_legato([n2], min_total))
        return out

    def _classify_articulation(self, note):
        p = note['pitches']
        base = note['base_midi']
        skip = min(1, max(0, len(p) - 3))
        p = p[skip:]
        if len(p) < 3:
            return {'type': None}
        rel = [x - base for x in p]
        max_rise = max(rel)
        end_val = rel[-1]
        diffs = [b - a for a, b in zip(rel, rel[1:]) if abs(b - a) > 1e-6]
        signs = sum(1 for a, b in zip(diffs, diffs[1:]) if (a > 0) != (b > 0))
        if signs >= 4 and max_rise < 1.5:
            return {'type': 'vibrato'}
        if max_rise >= 0.5:
            return {'type': 'bend', 'target_midi': base + round(max_rise),
                    'released': end_val < max_rise * 0.4}
        return {'type': None}

    def _assign_frets(self, notes):
        out = []
        prev_str, prev_fret = None, None
        for n in notes:
            cands = []
            for i, name in enumerate(self.STRING_ORDER):
                fret = n['base_midi'] - self.STRING_OPEN_MIDI[name]
                if 0 <= fret <= self.MAX_FRET:
                    cands.append((i, fret))
            if not cands:
                continue
            if prev_str is None:
                cands.sort(key=lambda c: (abs(c[0] - 4), abs(c[1] - 12)))
            else:
                cands.sort(key=lambda c: abs(c[1] - prev_fret) + 0.5 * abs(c[0] - prev_str))
            s, f = cands[0]
            prev_str, prev_fret = s, f
            out.append({**n, 'string': s, 'fret': f})
        return out

    def _detect_legato_transitions(self, notes, onsets,
                                   tol=0.045, max_gap=0.06, slide_min=4):
        if not notes:
            return notes
        out = [dict(notes[0])]
        for nxt in notes[1:]:
            prev = out[-1]
            gap = nxt['start'] - prev['end']
            diff = nxt['base_midi'] - prev['base_midi']
            has_onset = any(abs(nxt['start'] - o) <= tol for o in onsets)
            if gap <= max_gap and abs(diff) >= 1 and not has_onset:
                forced = nxt['base_midi'] - self.STRING_OPEN_MIDI[self.STRING_ORDER[prev['string']]]
                if 0 <= forced <= self.MAX_FRET:
                    if abs(diff) >= slide_min:
                        marker = '/' if diff > 0 else '\\'
                    else:
                        marker = 'h' if diff > 0 else 'p'
                    if prev.get('merged_label'):
                        base = prev['merged_label']
                    else:
                        art = prev.get('articulation', '')
                        is_bend = art.startswith('b') and not art.endswith('r')
                        base = str(prev['fret']) if is_bend else f"{prev['fret']}{art}"
                    prev['merged_label'] = f"{base}{marker}{forced}"
                    prev['end'] = nxt['end']
                    continue
            out.append(dict(nxt))
        return out

    def _group_into_phrases(self, notes, gap_thresh=1.1, max_phrase=7.0):
        phrases, cur = [], []
        for n in notes:
            if cur:
                gap = n['start'] - cur[-1]['end']
                length = n['start'] - cur[0]['start']
                if gap > gap_thresh or length > max_phrase:
                    phrases.append(cur)
                    cur = []
            cur.append(n)
        if cur:
            phrases.append(cur)
        return phrases

    @staticmethod
    def _format_mmss(seconds):
        seconds = max(0, seconds)
        return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"

    def build_guitar_tab_pdf(self, song_title, phrases, solo_start, solo_end,
                             pdf_path, tuning_name="Standard (E A D G B E)"):
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.colors import HexColor, black
        from reportlab.lib.units import inch
        from datetime import datetime

        LEFT = RIGHT = TOP = BOTTOM = 0.7 * inch
        TAB_FONT, TAB_BOLD = "Courier", "Courier-Bold"
        TAB_SIZE = 10
        LINE_H = TAB_SIZE + 3
        PHRASE_GAP = 20
        CPS = 6
        MIN_ROW = 40

        pw, ph = letter
        content_w = pw - LEFT - RIGHT
        char_w = TAB_SIZE * 0.6
        max_chars = int(content_w / char_w) - 4

        c = canvas.Canvas(pdf_path, pagesize=letter)
        y = ph - TOP

        def draw_header():
            nonlocal y
            c.setFont("Helvetica-Bold", 20)
            c.setFillColor(black)
            c.drawCentredString(pw / 2, y - 20, song_title)
            c.setFont("Helvetica-Oblique", 11)
            c.setFillColor(HexColor('#555555'))
            c.drawCentredString(pw / 2, y - 38, "Guitar Solo Transcription & Tablature")
            c.setFont("Helvetica", 9)
            c.setFillColor(HexColor('#888888'))
            c.drawCentredString(pw / 2, y - 54,
                                f"Solo: {self._format_mmss(solo_start)} - "
                                f"{self._format_mmss(solo_end)}    Tuning: {tuning_name}")
            c.setFont("Helvetica-Oblique", 8)
            c.setFillColor(HexColor('#aaaaaa'))
            c.drawCentredString(pw / 2, y - 68,
                                f"Auto-transcribed · verify by ear · "
                                f"{datetime.now().strftime('%Y-%m-%d')}")
            c.setStrokeColor(HexColor('#cccccc'))
            c.setLineWidth(1)
            c.line(pw * 0.15, y - 78, pw * 0.85, y - 78)
            y -= 100

        def ensure(need):
            nonlocal y
            if y - need < BOTTOM:
                c.showPage()
                y = ph - TOP

        draw_header()

        for pi, phrase in enumerate(phrases, 1):
            ps, pe = phrase[0]['start'], phrase[-1]['end']
            chars = max(MIN_ROW, min(max_chars, int((pe - ps) * CPS)))
            rows = {i: ['-'] * chars for i in range(6)}
            for n in phrase:
                frac = (n['start'] - ps) / max(0.001, pe - ps)
                col = min(chars - 1, int(frac * (chars - 1)))
                label = n.get('merged_label') or f"{n['fret']}{n.get('articulation', '')}"
                row = rows[n['string']]
                for i, ch in enumerate(label):
                    pos = col + i
                    if pos < chars:
                        row[pos] = ch

            ensure(24 + 6 * LINE_H + PHRASE_GAP)

            c.setFont("Helvetica-Bold", 10)
            c.setFillColor(HexColor('#ff3b30'))
            c.drawString(LEFT, y, f"Phrase {pi} ({self._format_mmss(ps)} - "
                                  f"{self._format_mmss(pe)})")
            y -= 16

            c.setFont(TAB_FONT, TAB_SIZE)
            c.setFillColor(black)
            for i in reversed(range(6)):
                line = ''.join(rows[i])
                c.drawString(LEFT, y, f"{self.STRING_ORDER[i]}|{line}|")
                y -= LINE_H
            y -= PHRASE_GAP

        ensure(90)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(HexColor('#555555'))
        c.drawString(LEFT, y, "TABLATURE LEGEND")
        y -= 16
        c.setFont(TAB_FONT, 9)
        for sym, desc in [
            ("b", "Bend up"), ("r", "Release bend"), ("~", "Vibrato"),
            ("h / p", "Hammer-on / Pull-off"), ("/ \\", "Slide up / down"),
        ]:
            c.setFillColor(HexColor('#ff3b30'))
            c.drawString(LEFT, y, sym)
            c.setFillColor(black)
            c.drawString(LEFT + 50, y, desc)
            y -= 13

        c.showPage()
        c.save()

    # ============================================================
    # KARAOKE VIDEO
    # ============================================================

    def build_karaoke_video(self, background_path, audio_path, srt_path,
                            video_output_path, background_is_video=False,
                            progress_cb=None):
        def _p(p, m):
            if progress_cb:
                progress_cb(p, m)

        _p(30, "Preparing background...")
        generated = False
        if background_path is None:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
            tmp.close()
            background_path = tmp.name
            self.create_cloud_background(background_path)
            generated = True
            background_is_video = False
        elif background_path.lower().endswith(self.VIDEO_EXTENSIONS):
            background_is_video = True

        _p(60, "Rendering video (ffmpeg)...")
        escaped = self._escape_path_for_ffmpeg_filter(srt_path)
        style = ("FontName=Arial,FontSize=26,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H80000000,BorderStyle=1,Outline=2,Shadow=1,"
                 "Alignment=2,MarginV=60")
        vf = f"subtitles='{escaped}':force_style='{style}'"

        if background_is_video:
            cmd = ['ffmpeg', '-y', '-stream_loop', '-1', '-i', background_path,
                   '-i', audio_path, '-vf', vf,
                   '-map', '0:v:0', '-map', '1:a:0',
                   '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                   '-c:a', 'aac', '-b:a', '192k',
                   '-shortest', video_output_path]
        else:
            cmd = ['ffmpeg', '-y', '-loop', '1', '-framerate', '24',
                   '-i', background_path, '-i', audio_path, '-vf', vf,
                   '-map', '0:v:0', '-map', '1:a:0',
                   '-c:v', 'libx264', '-tune', 'stillimage',
                   '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
                   '-shortest', video_output_path]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed (libass subtitles filter needed):\n{proc.stderr[-1000:]}"
            )
        if generated:
            try:
                os.unlink(background_path)
            except OSError:
                pass
        _p(100, "Karaoke ready")

    def create_cloud_background(self, output_path, width=1280, height=720):
        try:
            from PIL import Image, ImageDraw, ImageFilter
            import random
            img = Image.new("RGB", (width, height))
            draw = ImageDraw.Draw(img)
            top, bottom = (25, 30, 45), (10, 12, 18)
            for yy in range(height):
                t = yy / height
                draw.line([(0, yy), (width, yy)],
                          fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
            cloud = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            cd = ImageDraw.Draw(cloud)
            rng = random.Random(42)
            for _ in range(16):
                cx, cy = rng.randint(0, width), rng.randint(0, int(height * 0.75))
                for _ in range(7):
                    ox, oy = cx + rng.randint(-80, 80), cy + rng.randint(-28, 28)
                    r = rng.randint(45, 95)
                    cd.ellipse([ox - r, oy - r, ox + r, oy + r], fill=(255, 59, 48, 60))
            cloud = cloud.filter(ImageFilter.GaussianBlur(15))
            img = img.convert("RGBA")
            img.alpha_composite(cloud)
            img.convert("RGB").save(output_path)
            return
        except ImportError:
            pass
        cmd = ['ffmpeg', '-y', '-f', 'lavfi',
               '-i', f'color=c=0x15171C:size={width}x{height}',
               '-frames:v', '1', output_path]
        subprocess.run(cmd, capture_output=True, text=True)

    def _escape_path_for_ffmpeg_filter(self, path):
        p = os.path.abspath(path).replace('\\', '/')
        return p.replace(':', '\\:')