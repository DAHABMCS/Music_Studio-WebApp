/* ============================================================
   Music Studio — dashboard controller
   ============================================================ */

const state = {
  uploaded: null,      // { file_id, filename, path, size, srt_path }
  background: null,    // { path }
  srt: null,           // e.g. "MySong.srt"
  jobId: null,
  timerStart: null,
  timerInterval: null,
  isProcessing: false,
  activeButtonId: null,   // which action button started the current/last job
  buttonStatus: {         // 'ready' | 'processing' | 'done' — persisted so a
    btnSrt: 'ready', btnKaraoke: 'ready', btnLyricVideo: 'ready',
    btnCreateSong: 'ready', btnLyricsTab: 'ready', btnFull: 'ready',
  },
  reviewed: {             // has the matching Browse button been clicked
    srt: false, karaoke: false, lyrics_tab: false, transcription: false, songs: false,
  },
};

const $ = (id) => document.getElementById(id);

/* ============================================================
   ACTION-BUTTON STATUS COLORS
   ------------------------------------------------------------
   Every job-triggering button starts red (.btn-ready), turns yellow
   (.btn-processing) while its job runs, then green (.btn-done) once
   it finishes. That status is kept in state.buttonStatus AND applied
   to the DOM together (see setActionButtonState) — previously only
   the DOM class was set, with nothing persisted, so a page reload had
   no way to know a button had finished successfully and it silently
   fell back to red every time (see resetAllButtonStates in
   restoreSession). Persisting it here and re-applying it in
   restoreSession fixes that.

   Each of the 5 "Browse ... Folder" buttons is paired with one (or
   two, for Karaoke/Lyric Video sharing one folder) action button and
   mirrors its red/yellow — but its OWN green only ever comes from
   actually being clicked to review that folder (see browseRemote),
   not automatically from the job finishing. That green then persists
   until the paired job is run again.
   ============================================================ */
const ACTION_TO_KIND = {
  btnSrt:        'srt',
  btnKaraoke:    'karaoke',
  btnLyricVideo: 'karaoke',
  btnCreateSong: 'songs',
  btnLyricsTab:  'lyrics_tab',
  btnFull:       'transcription',
};
const ACTION_BUTTON_IDS = Object.keys(ACTION_TO_KIND);

const KIND_TO_BROWSE_BTN = {
  srt:           'browseSrtBtn',
  karaoke:       'browseKaraokeBtn',
  songs:         'browseSongsBtn',
  lyrics_tab:    'browseLyricsTabBtn',
  transcription: 'browseTranscriptionBtn',
};

function setButtonState(id, cls) {
  const el = $(id);
  if (!el) return;
  el.classList.remove('btn-ready', 'btn-processing', 'btn-done');
  el.classList.add(cls);
}

function resetAllButtonStates() {
  ACTION_BUTTON_IDS.forEach(id => {
    state.buttonStatus[id] = 'ready';
    setButtonState(id, 'btn-ready');
  });
  Object.values(KIND_TO_BROWSE_BTN).forEach(id => setButtonState(id, 'btn-ready'));
}

/* Sets one action button's status in BOTH the DOM and state.buttonStatus
   (so it can be persisted/restored), then re-derives its paired Browse
   button's color from the current situation. */
function setActionButtonState(buttonId, statusWord) {
  state.buttonStatus[buttonId] = statusWord;
  setButtonState(buttonId, 'btn-' + statusWord);
  const kind = ACTION_TO_KIND[buttonId];
  if (kind) refreshBrowseButtonColor(kind);
}

/* A Browse button mirrors "processing" from whichever of its paired
   action button(s) is currently running; otherwise it's green if
   already reviewed, or red/ready otherwise. Called any time a paired
   action button's status changes, or reviewed[] changes. */
function refreshBrowseButtonColor(kind) {
  const browseBtnId = KIND_TO_BROWSE_BTN[kind];
  if (!browseBtnId) return;

  const pairedButtonIds = ACTION_BUTTON_IDS.filter(id => ACTION_TO_KIND[id] === kind);
  const anyProcessing = pairedButtonIds.some(id => state.buttonStatus[id] === 'processing');

  if (anyProcessing) {
    setButtonState(browseBtnId, 'btn-processing');
  } else if (state.reviewed[kind]) {
    setButtonState(browseBtnId, 'btn-done');
  } else {
    setButtonState(browseBtnId, 'btn-ready');
  }
}

/* Re-applies every persisted color from state.buttonStatus/reviewed —
   used right after restoring a snapshot on page load, so a page
   reload shows exactly what was true before it closed instead of
   resetting everything to red. */
function applyPersistedButtonColors() {
  ACTION_BUTTON_IDS.forEach(id => {
    setButtonState(id, 'btn-' + (state.buttonStatus[id] || 'ready'));
  });
  Object.keys(KIND_TO_BROWSE_BTN).forEach(refreshBrowseButtonColor);
}

/* ============================================================
   SESSION PERSISTENCE
   ------------------------------------------------------------
   Keeps the user's place across browser closes / tab reloads.
   - localStorage snapshot: uploaded file, background, srt, jobId,
     form field values. Restored on load.
   - If a job was running, we resume polling /api/job/<id>.
   ============================================================ */
const STORAGE_KEY = 'musicStudio.session.v1';

function saveSession() {
  const snapshot = {
    uploaded:  state.uploaded,
    background: state.background,
    srt:       state.srt,
    jobId:     state.jobId,
    activeButtonId: state.activeButtonId,
    buttonStatus: state.buttonStatus,
    reviewed:  state.reviewed,
    timestamp: Date.now(),
    form: {
      language:    $('language')?.value,
      model:       $('model')?.value,
      chordMethod: $('chordMethod')?.value,
      useDemucs:   $('useDemucs')?.checked,
      outputPath:  $('outputFilePath')?.value,
      bgPath:      $('backgroundPath')?.value,
      inputPath:   $('inputFilePath')?.value,
    },
  };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
  } catch (e) { /* quota / private mode — ignore */ }
}

function clearSession() {
  try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
}

async function restoreSession() {
  let snap;
  try {
    snap = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
  } catch { snap = null; }
  if (!snap) return;

  // 1. Restore in-memory state
  state.uploaded   = snap.uploaded   || null;
  state.background = snap.background || null;
  state.srt        = snap.srt        || null;
  state.jobId      = snap.jobId      || null;
  state.activeButtonId = snap.activeButtonId || null;
  state.buttonStatus = Object.assign(
    { btnSrt: 'ready', btnKaraoke: 'ready', btnLyricVideo: 'ready',
      btnCreateSong: 'ready', btnLyricsTab: 'ready', btnFull: 'ready' },
    snap.buttonStatus || {}
  );
  state.reviewed   = Object.assign(
    { srt: false, karaoke: false, lyrics_tab: false, transcription: false, songs: false },
    snap.reviewed || {}
  );

  // Re-apply every button's persisted color exactly as it was before
  // the browser closed — previously this called resetAllButtonStates()
  // here, which forced every action button back to red regardless of
  // whether its job had actually finished successfully; only the
  // Browse buttons' green ("reviewed") survived a reload. Using the
  // restored buttonStatus map instead fixes that.
  applyPersistedButtonColors();

  // 2. Restore form fields
  const f = snap.form || {};
  if (f.language     && $('language'))           $('language').value = f.language;
  if (f.model        && $('model'))              $('model').value = f.model;
  if (f.chordMethod  && $('chordMethod'))        $('chordMethod').value = f.chordMethod;
  if (f.useDemucs !== undefined && $('useDemucs')) $('useDemucs').checked = f.useDemucs;
  if (f.outputPath   && $('outputFilePath'))     $('outputFilePath').value = f.outputPath;
  if (f.bgPath       && $('backgroundPath'))     $('backgroundPath').value = f.bgPath;
  if (f.inputPath    && $('inputFilePath'))      $('inputFilePath').value = f.inputPath;

  // 3. If a job was running, resume polling or show final result
  if (state.jobId) {
    try {
      const r = await fetch(`/api/job/${state.jobId}`);
      const j = await r.json();
      if (j.status === 'running') {
        lockButtons(true);
        startTimer();
        setProgress(j.progress || 0);
        setStatus(j.message || 'Resuming…');
        if (state.activeButtonId) setActionButtonState(state.activeButtonId, 'processing');
        pollJob();
        setStatus('Resumed previous job');
        saveSession();
        return;
      }
      if (j.status === 'done') {
        handleResult(j.result);
        setStatus('Previous job finished while you were away');
        if (state.activeButtonId) setActionButtonState(state.activeButtonId, 'done');
        state.jobId = null;
        saveSession();
      } else if (j.status === 'error') {
        setStatus('Previous job failed: ' + (j.error || ''));
        if (state.activeButtonId) setActionButtonState(state.activeButtonId, 'ready');
        state.jobId = null;
        saveSession();
      } else {
        // cancelled / unknown — drop it
        if (state.activeButtonId) setActionButtonState(state.activeButtonId, 'ready');
        state.jobId = null;
        saveSession();
      }
    } catch (e) {
      // Server lost the job (restart) — clear it
      state.jobId = null;
      saveSession();
    }
  }

  // 4. Restore the SRT preview if we had one on screen
  if (state.srt) {
    try {
      const r = await fetch(`/api/output_preview?path=${encodeURIComponent(state.srt)}`);
      if (r.ok) {
        const t = await r.text();
        $('srtPreview').textContent = t.slice(0, 8000);
      }
    } catch { /* ignore */ }
  }
}

/* ---------- Non-blocking notifications ----------
   Replaces alert()/confirm()-style popups. Those are native, blocking
   dialogs — the whole page (including the running-job status/progress
   updates) is frozen until someone clicks OK, which is disruptive for
   messages that don't need a decision (e.g. "Karaoke video ready").
   These toasts show the same information but never block input and
   dismiss themselves automatically; clicking one dismisses it early. */
let _noticeContainer = null;
function _ensureNoticeContainer() {
  if (_noticeContainer && document.body.contains(_noticeContainer)) return _noticeContainer;
  _noticeContainer = document.createElement('div');
  _noticeContainer.id = 'noticeContainer';
  Object.assign(_noticeContainer.style, {
    position: 'fixed', top: '16px', right: '16px', zIndex: '9999',
    display: 'flex', flexDirection: 'column', gap: '8px',
    maxWidth: '380px',
  });
  document.body.appendChild(_noticeContainer);
  return _noticeContainer;
}

function showNotice(message, type = 'info', opts = {}) {
  const container = _ensureNoticeContainer();
  const palette = {
    info:    { bg: '#262a33', border: '#2b303b', fg: '#e1e4ea' },
    success: { bg: '#14321a', border: '#2b8a3e', fg: '#b8e6c1' },
    warn:    { bg: '#3a2f14', border: '#c98a1f', fg: '#ffe3b0' },
    error:   { bg: '#3a1a1a', border: '#ff3b30', fg: '#ffcfcc' },
  };
  const c = palette[type] || palette.info;

  const el = document.createElement('div');
  Object.assign(el.style, {
    background: c.bg, border: `1px solid ${c.border}`, color: c.fg,
    padding: '10px 14px', borderRadius: '8px', fontSize: '13px',
    lineHeight: '1.4', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
    boxShadow: '0 8px 22px rgba(0,0,0,0.35)', cursor: 'pointer',
    fontFamily: 'inherit', pointerEvents: 'auto',
  });
  el.textContent = message;
  el.title = 'Click to dismiss';
  el.onclick = () => el.remove();
  container.appendChild(el);

  const duration = opts.duration || (type === 'error' ? 8000 : type === 'warn' ? 4000 : 5000);
  setTimeout(() => { if (el.parentNode) el.remove(); }, duration);
}

/* ---------- Input file picker ---------- */
$('inputFilePicker').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  $('inputFilePath').value = f.name;
  await uploadFile(f, 'input');
});

/* ---------- Background picker ---------- */
$('backgroundPicker').addEventListener('change', async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  $('backgroundPath').value = f.name;
  const r = await uploadFile(f, 'background');
  if (r) state.background = r;
  saveSession();
});

/* ---------- Upload helper ---------- */
async function uploadFile(file, kind = 'input') {
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const j = await res.json();
    if (j.error) {
      showNotice('Upload failed: ' + j.error, 'error');
      return null;
    }
    if (kind === 'input') {
      state.uploaded = j;
      $('outputFilePath').value = j.srt_path || (file.name.replace(/\.[^.]+$/, '') + '.srt');
      state.srt = null;
    }
    saveSession();
    return j;
  } catch (err) {
    showNotice('Upload failed: ' + err.message, 'error');
    return null;
  }
}

/* ---------- Timer ---------- */
function startTimer() {
  state.timerStart = Date.now();
  $('elapsedTime').textContent = '00:00';
  state.timerInterval = setInterval(() => {
    const s = Math.floor((Date.now() - state.timerStart) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    $('elapsedTime').textContent = `${mm}:${ss}`;
  }, 500);
}

function stopTimer() {
  if (state.timerInterval) clearInterval(state.timerInterval);
  state.timerInterval = null;
  $('elapsedTime').textContent = '00:00';
}

/* ---------- Status / progress ---------- */
function setStatus(text)  { $('statusText').textContent = text; }
function setProgress(p)   { $('progressBar').style.width = (p || 0) + '%'; }

/* ---------- Button locking ---------- */
function lockButtons(lock) {
  state.isProcessing = lock;
  ['btnSrt', 'btnKaraoke', 'btnLyricVideo', 'btnCreateSong', 'btnLyricsTab', 'btnFull'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = lock;
  });
  const stop = $('btnStop');
  if (stop) stop.disabled = !lock;
  setGearSpinning(lock);
}

/* ---------- Gear animation: plays only while a job is running ---------- */
function setGearSpinning(on) {
  const v = $('gearVideo');
  if (!v) return;
  if (on) {
    const p = v.play();
    if (p && p.catch) p.catch(() => {});
  } else {
    v.pause();
  }
}

/* ---------- Job polling ---------- */
async function pollJob() {
  if (!state.jobId) return;
  try {
    const r = await fetch(`/api/job/${state.jobId}`);
    const j = await r.json();
    setProgress(j.progress);
    setStatus(j.message || j.status);

    if (j.status === 'done') {
      state.jobId = null;
      lockButtons(false);
      stopTimer();
      if (state.activeButtonId) setActionButtonState(state.activeButtonId, 'done');
      handleResult(j.result);
      saveSession();
      return;
    }
    if (j.status === 'error') {
      state.jobId = null;
      lockButtons(false);
      stopTimer();
      if (state.activeButtonId) setActionButtonState(state.activeButtonId, 'ready');
      showNotice('Error: ' + j.error, 'error');
      saveSession();
      return;
    }
    setTimeout(pollJob, 800);
  } catch (e) {
    setTimeout(pollJob, 2000);
  }
}

function startJob(res, buttonId) {
  if (!res || res.error) {
    showNotice(res && res.error ? res.error : 'Unknown error', 'error');
    return;
  }
  state.jobId = res.job_id;
  state.activeButtonId = buttonId || null;
  const kind = ACTION_TO_KIND[buttonId];
  if (kind) {
    // A fresh run invalidates any earlier "reviewed" green for this
    // kind's Browse button — the folder is about to change.
    state.reviewed[kind] = false;
  }
  if (buttonId) setActionButtonState(buttonId, 'processing');
  lockButtons(true);
  startTimer();
  setProgress(0);
  setStatus('Starting…');
  saveSession();
  pollJob();
}

function handleResult(result) {
  if (!result) return;
  if (result.preview) {
    $('srtPreview').textContent = result.preview;
  }
  if (result.srt) {
    state.srt = result.srt;
  }
  if (result.video) showNotice('Karaoke video ready: ' + result.video, 'success');
  if (result.lyric_video) showNotice('Lyric video ready: ' + result.lyric_video, 'success');
  if (result.pdf && result.mp3) showNotice(`Ready:\n${result.pdf}\n${result.mp3}`, 'success');
  else if (result.pdf) showNotice('PDF ready: ' + result.pdf, 'success');
  if (result.tab_pdf) showNotice('Guitar tab ready: ' + result.tab_pdf, 'success');
  if (result.folder) {
    showNotice(`Transcription ready in folder: ${result.folder}\n\n` +
          (result.files || []).join('\n'));
  }
  if (result.song) showNotice('Song ready: ' + result.song, 'success');
  saveSession();
}

/* ---------- Actions ---------- */
$('btnSrt').onclick = async () => {
  if (!state.uploaded) { showNotice('Choose an input file first', 'warn'); return; }
  const r = await fetch('/api/generate_srt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      language: $('language').value,
      model: $('model').value,
      isolate_vocals: true, // always isolate vocals before transcribing — no longer user-toggled
    }),
  });
  startJob(await r.json(), 'btnSrt');
};

$('btnKaraoke').onclick = async () => {
  if (!state.uploaded || !state.srt) { showNotice('Generate the SRT first', 'warn'); return; }
  const r = await fetch('/api/generate_karaoke', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      background: state.background ? state.background.path : null,
    }),
  });
  startJob(await r.json(), 'btnKaraoke');
};

$('btnLyricVideo').onclick = async () => {
  if (!state.uploaded || !state.srt) { showNotice('Generate the SRT first', 'warn'); return; }
  const r = await fetch('/api/generate_lyric_video', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      background: state.background ? state.background.path : null,
    }),
  });
  startJob(await r.json(), 'btnLyricVideo');
};

$('btnLyricsTab').onclick = async () => {
  if (!state.uploaded || !state.srt) { showNotice('Generate the SRT first', 'warn'); return; }
  const r = await fetch('/api/export_lyrics_and_tab', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      chord_method: $('chordMethod').value,
      use_demucs: $('useDemucs').checked,
    }),
  });
  startJob(await r.json(), 'btnLyricsTab');
};

$('btnFull').onclick = async () => {
  if (!state.uploaded) { showNotice('Choose an input file first', 'warn'); return; }
  const r = await fetch('/api/full_transcription', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: state.uploaded.path }),
  });
  startJob(await r.json(), 'btnFull');
};

$('btnCreateSong').onclick = () => {
  openCreateSongModal();
};

$('btnStop').onclick = async () => {
  if (state.jobId) {
    try { await fetch(`/api/job/${state.jobId}/cancel`, { method: 'POST' }); }
    catch (e) {}
  }
  state.jobId = null;
  lockButtons(false);
  stopTimer();
  setStatus('Stopped');
  saveSession();
};

$('btnClear').onclick = () => {
  $('srtPreview').textContent = '—';
  setProgress(0);
  setStatus('Ready');
  stopTimer();
  state.activeButtonId = null;
  state.reviewed = { srt: false, karaoke: false, lyrics_tab: false, transcription: false, songs: false };
  resetAllButtonStates();
  clearSession();
};

$('btnEdit').onclick = () => {
  const srt = $('srtPreview').textContent;
  if (!srt || srt === '—') { showNotice('Nothing to edit yet', 'warn'); return; }
  openEditor(srt);
};

function copySrtPath() {
  const v = $('outputFilePath').value;
  if (!v) { showNotice('No output path yet', 'warn'); return; }

  // navigator.clipboard only exists in secure contexts (https, or
  // localhost). This dashboard is typically opened over plain http on
  // a LAN IP (see app.py's own startup message), where
  // navigator.clipboard is undefined — calling .writeText on it throws
  // synchronously, before any .catch() runs, so the button silently
  // does nothing. Fall back to a hidden-textarea + execCommand copy
  // in that case.
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(v)
      .then(() => setStatus('SRT path copied'))
      .catch(() => copyViaFallback(v));
  } else {
    copyViaFallback(v);
  }
}

function copyViaFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    const ok = document.execCommand('copy');
    setStatus(ok ? 'SRT path copied' : 'Copy failed');
  } catch (e) {
    setStatus('Copy failed');
  } finally {
    document.body.removeChild(ta);
  }
}

/* ---------- Remote folder browser (in-page, no window.open) ----------
   NOTE: this used to be `window.open('/api/browse/${kind}', '_blank')`,
   which opened a NEW BROWSER TAB pointed at a backend route that in turn
   opened a native Explorer/Finder window on the SERVER machine. That's
   what caused the "browser closes" / hangs behavior — the new tab was
   waiting on a request tied to a GUI window on a remote machine you
   can't see or interact with. This version fetches a JSON file list and
   renders it inline, in the current page. */
async function browseRemote(kind) {
  let data;
  try {
    const r = await fetch(`/api/browse/${kind}`);
    data = await r.json();
  } catch (e) {
    showNotice('Could not load folder: ' + e.message, 'error');
    return;
  }
  if (data.error) {
    showNotice('Could not load folder: ' + data.error, 'error');
    return;
  }

  // Reviewing the folder is what "confirms" it — the Browse button
  // turns green right here and stays green (surviving reloads, via
  // saveSession) until this kind's job is run again, regardless of
  // what the paired action button is doing at that moment.
  state.reviewed[kind] = true;
  setButtonState(KIND_TO_BROWSE_BTN[kind], 'btn-done');
  saveSession();

  // If the server is running on this same machine, /api/browse just
  // opened the real OS folder window directly — nothing more to show
  // in the page. Only render the in-page listing when it couldn't
  // (e.g. this request came from another device on the network).
  if (data.opened) {
    setStatus(`Opened ${data.path} folder`);
    return;
  }
  openBrowseModal(kind, data);
}

function openBrowseModal(kind, data) {
  // Remove any existing browse modal first
  document.querySelectorAll('.browse-modal').forEach(m => m.remove());

  const modal = document.createElement('div');
  modal.className = 'editor-modal browse-modal'; // reuse existing modal styling

  const itemsHtml = (data.files && data.files.length)
    ? data.files.map(f => `
        <li class="browse-item">
          <span class="browse-name">${escapeHtml(f.name)}</span>
          <span class="browse-size">${f.size_mb} MB</span>
          <a class="btn" href="/api/download_output/${encodeURIComponent(f.rel)}"
             target="_blank" rel="noopener">Download</a>
        </li>`).join('')
    : `<li class="browse-empty">This folder is empty.</li>`;

  modal.innerHTML = `
    <div class="editor-box">
      <h3>${escapeHtml(kind)} folder</h3>
      <ul class="browse-list">${itemsHtml}</ul>
      <div class="editor-actions">
        <button class="btn" id="browseClose">Close</button>
      </div>
    </div>`;

  document.body.appendChild(modal);
  modal.querySelector('#browseClose').onclick = () => modal.remove();
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove(); // click outside box to close
  });
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

/* ---------- Create Song modal ----------
   Two panels: Lyrics (verses/chorus/etc, free text) and Style (genre,
   instruments, singer). "Generate" posts both to /api/create_song and
   feeds the job into the same status/progress/polling pipeline every
   other action button uses (see startJob/pollJob). "Browse Output"
   just calls the existing browseRemote('songs') used by the
   "Browse Songs Folder" action-grid button.

   Fields persist across modal opens via localStorage (same pattern as
   STORAGE_KEY above): "Save" writes the current fields to
   SONG_DRAFT_KEY, and every time the modal is opened it auto-loads
   whatever was last saved there. "Clear" wipes both the on-screen
   fields and the saved draft. Nothing is auto-saved on close/cancel —
   only explicit Save persists, per how this was asked for. */
const SONG_DRAFT_KEY = 'musicStudio.songDraft.v1';

const INSTRUMENT_OPTIONS = [
  'Guitar', 'Piano', 'Drums', 'Bass', 'Strings',
  'Synth', 'Saxophone', 'Violin', 'Acoustic Guitar', 'Choir',
];

function loadSongDraft() {
  try {
    return JSON.parse(localStorage.getItem(SONG_DRAFT_KEY) || 'null');
  } catch (e) {
    return null;
  }
}

/* Song language -> reading direction. Arabic, Persian and Urdu read right to
   left; "auto" lets the browser decide line by line from the letters typed. */
const SONG_RTL_LANGS = ['ar', 'fa', 'ur', 'he'];

function songLangDir(lang) {
  if (!lang || lang === 'auto') return 'auto';
  return SONG_RTL_LANGS.includes(lang) ? 'rtl' : 'ltr';
}

function applySongDirection(modal) {
  const langEl = modal.querySelector('#songLanguage');
  const dir = songLangDir(langEl ? langEl.value : 'auto');
  const lyrics = modal.querySelector('#songLyrics');
  if (lyrics) {
    lyrics.setAttribute('dir', dir);
    lyrics.style.textAlign = 'start';          // right edge when rtl, left when ltr
    lyrics.style.unicodeBidi = dir === 'auto' ? 'plaintext' : 'normal';
  }
  // Title and style text boxes always follow the letters typed into them.
  ['#songTitle', '#songStyle'].forEach(sel => {
    const el = modal.querySelector(sel);
    if (el) el.setAttribute('dir', 'auto');
  });
}

function readSongFields(modal) {
  return {
    title:       modal.querySelector('#songTitle').value.trim(),
    lyrics:      modal.querySelector('#songLyrics').value,
    language:    modal.querySelector('#songLanguage').value,
    style:       modal.querySelector('#songStyle').value.trim(),
    singer:      modal.querySelector('#songSinger').value,
    backend:     modal.querySelector('#songBackend').value,
    instruments: Array.from(modal.querySelectorAll('.songInstrument:checked')).map(el => el.value),
  };
}

function applySongFields(modal, fields) {
  if (!fields) return;
  modal.querySelector('#songTitle').value   = fields.title || '';
  modal.querySelector('#songLyrics').value  = fields.lyrics || '';
  modal.querySelector('#songStyle').value   = fields.style || '';
  modal.querySelector('#songSinger').value  = fields.singer || 'auto';
  modal.querySelector('#songBackend').value = fields.backend || 'ace';
  const langEl = modal.querySelector('#songLanguage');
  if (langEl) {
    langEl.value = fields.language || 'auto';
    if (langEl.value !== (fields.language || 'auto')) langEl.value = 'auto';   // unknown code
  }
  applySongDirection(modal);
  const chosen = new Set(fields.instruments || []);
  modal.querySelectorAll('.songInstrument').forEach(el => {
    el.checked = chosen.has(el.value);
  });
}

function openCreateSongModal() {
  document.querySelectorAll('.song-modal').forEach(m => m.remove());

  const modal = document.createElement('div');
  modal.className = 'editor-modal song-modal';

  const instrumentsHtml = INSTRUMENT_OPTIONS.map((name, i) => `
    <label><input type="checkbox" class="songInstrument" value="${name}"> ${name}</label>
  `).join('');

  modal.innerHTML = `
    <div class="editor-box song-box">
      <h3>Create Song</h3>
      <div class="song-presets">
        <label for="songPresetSelect">Saved setups:</label>
        <select id="songPresetSelect"><option value="">Loading...</option></select>
        <button class="btn" type="button" id="songPresetLoad">Load</button>
        <button class="btn" type="button" id="songPresetSave">Save setup...</button>
        <button class="btn" type="button" id="songPresetDelete">Delete</button>
        <label class="preset-check" title="Also store the title and lyrics text in the saved setup">
          <input type="checkbox" id="songPresetLyrics"> include title &amp; lyrics
        </label>
      </div>
      <div class="song-panels">

        <div class="song-panel">
          <h4>Lyrics</h4>
          <div class="song-field">
            <label for="songTitle">Title</label>
            <input type="text" id="songTitle" placeholder="Song title">
          </div>
          <div class="song-field">
            <label for="songLanguage">Language <span class="muted">(sung language &amp; reading direction)</span></label>
            <select id="songLanguage">
              <option value="auto">Auto - detect from the lyrics</option>
              <option value="ar">Arabic - عربي (right to left)</option>
              <option value="en">English</option>
              <option value="fa">Persian - فارسی (right to left)</option>
              <option value="ur">Urdu - اردو (right to left)</option>
              <option value="tr">Turkish</option>
              <option value="fr">French</option>
              <option value="es">Spanish</option>
              <option value="de">German</option>
              <option value="it">Italian</option>
              <option value="pt">Portuguese</option>
              <option value="hi">Hindi</option>
              <option value="ru">Russian</option>
              <option value="ja">Japanese</option>
              <option value="ko">Korean</option>
              <option value="zh">Chinese</option>
            </select>
          </div>
          <div class="song-field">
            <label for="songLyrics">Verses, Chorus, Bridge, etc.</label>
            <textarea id="songLyrics" data-dir-fixed dir="auto" spellcheck="false"
              placeholder="[Verse 1]&#10;...&#10;&#10;[Chorus]&#10;...&#10;&#10;[Verse 2]&#10;...&#10;&#10;[Bridge]&#10;..."></textarea>
          </div>
          <div class="song-field song-lyrics-tools">
            <input type="file" id="songLyricsFile" accept=".txt,.text,text/plain" hidden>
            <button class="btn" type="button" id="songLyricsPick">Load lyrics from file...</button>
            <span id="songLenNote" class="muted"></span>
          </div>
        </div>

        <div class="song-panel">
          <h4>Style</h4>
          <div class="song-field">
            <label for="songBackend">Backend</label>
            <select id="songBackend">
              <option value="ace">ACE-Step — lyrics + sung vocals</option>
              <option value="musicgen">MusicGen — instrumental only, faster</option>
            </select>
            <div id="songBackendNote" class="muted" style="margin-top:4px; font-size:11px;"></div>
          </div>
          <div class="song-field">
            <label for="songStyle">Genre / mood / description</label>
            <input type="text" id="songStyle" placeholder="e.g. upbeat pop rock, 120bpm, anthemic">
          </div>
          <div class="song-field">
            <label for="songSinger">Singer</label>
            <select id="songSinger">
              <option value="auto">Auto</option>
              <option value="male">Male</option>
              <option value="female">Female</option>
              <option value="duet">Duet (Male + Female)</option>
              <option value="instrumental">Instrumental (no vocals)</option>
            </select>
          </div>
          <div class="song-field">
            <label>Instruments</label>
            <div class="song-instruments">${instrumentsHtml}</div>
          </div>
        </div>

      </div>
      <div class="editor-actions">
        <button class="btn" id="songBrowseOutput">Browse Output</button>
        <button class="btn" id="songSave" title="Remember these fields in this browser (loads automatically next time)">Save Draft</button>
        <button class="btn" id="songClear">Clear</button>
        <button class="btn accent" id="songGenerate">Generate</button>
        <button class="btn" id="songCancel">Cancel</button>
      </div>
    </div>`;

  document.body.appendChild(modal);

  // Auto-load whatever was last saved (see SONG_DRAFT_KEY note above).
  applySongFields(modal, loadSongDraft());
  applySongDirection(modal);
  modal.querySelector('#songLanguage').addEventListener('change', () => {
    applySongDirection(modal);
    updateLenNote();
  });

  modal.querySelector('#songCancel').onclick = () => modal.remove();
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.remove();
  });

  modal.querySelector('#songBrowseOutput').onclick = () => browseRemote('songs');

  /* ----- Estimated song length (mirrors the server's estimate) ----- */
  function updateLenNote() {
    const note = modal.querySelector('#songLenNote');
    const text = modal.querySelector('#songLyrics').value;
    const lang = modal.querySelector('#songLanguage').value;
    const isArabic = lang === 'ar' || (lang === 'auto' && /[\u0600-\u06FF\u0750-\u077F]{3}/.test(text));
    const wps = isArabic ? 1.1 : 0.85;                 // seconds per sung word
    let words = 0, secs = 12;                           // 12s = intro + outro
    text.split(/\r?\n/).forEach(line => {
      const t = line.trim();
      if (!t) return;
      if (/^\[.*\]$/.test(t)) { secs += /inst|intro/i.test(t) ? 10 : 4; return; }
      const w = t.split(/\s+/).length;
      words += w;
      secs += Math.max(2.5, w * wps + 0.8);
    });
    if (!words) { note.textContent = ''; note.style.color = ''; return; }
    secs = Math.round(secs);
    const mins = (secs / 60).toFixed(1);
    if (secs > 90) {
      note.textContent = `~${words} words - about ${mins} min (long songs are made in parts and joined, so it takes longer)`;
      note.style.color = '#ffb020';
    } else {
      note.textContent = `~${words} words - about ${mins} min`;
      note.style.color = '';
    }
  }
  modal.querySelector('#songLyrics').addEventListener('input', updateLenNote);

  /* ----- Load lyrics from a text file on this computer ----- */
  const lyricsFile = modal.querySelector('#songLyricsFile');
  modal.querySelector('#songLyricsPick').onclick = () => lyricsFile.click();
  lyricsFile.onchange = async () => {
    const f = lyricsFile.files && lyricsFile.files[0];
    if (!f) return;
    if (f.size > 500000) {
      showNotice('That file is too large to be lyrics', 'warn');
      lyricsFile.value = '';
      return;
    }
    try {
      let text = await f.text();
      text = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');
      modal.querySelector('#songLyrics').value = text;
      const titleEl = modal.querySelector('#songTitle');
      if (!titleEl.value.trim()) {
        titleEl.value = f.name.replace(/\.[^.]+$/, '').replace(/_+/g, ' ');
      }
      updateLenNote();
      showNotice('Loaded lyrics from ' + f.name, 'success');
    } catch (e) {
      showNotice('Could not read that file: ' + e.message, 'error');
    }
    lyricsFile.value = '';
  };

  /* ----- Named saved setups (stored on the server, per user) ----- */
  const presetSelect = modal.querySelector('#songPresetSelect');
  const presetLyricsBox = modal.querySelector('#songPresetLyrics');

  async function presetApi(path, opts) {
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); }
    catch (e) { throw new Error('not logged in, or the server did not answer'); }
    if (!r.ok || (data && data.error)) throw new Error((data && data.error) || ('HTTP ' + r.status));
    return data;
  }

  async function refreshPresets(selectName) {
    try {
      const data = await presetApi('/api/song_presets');
      const list = data.presets || [];
      presetSelect.innerHTML = list.length
        ? list.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('')
        : '<option value="">- none saved yet -</option>';
      if (selectName) presetSelect.value = selectName;
    } catch (e) {
      presetSelect.innerHTML = '<option value="">- could not load list -</option>';
    }
  }

  modal.querySelector('#songPresetSave').onclick = async () => {
    const suggested = presetSelect.value || modal.querySelector('#songTitle').value.trim();
    const name = (window.prompt('Name for this setup:', suggested) || '').trim();
    if (!name) return;
    const settings = readSongFields(modal);
    if (!presetLyricsBox.checked) { delete settings.lyrics; delete settings.title; }
    try {
      const res = await presetApi('/api/song_presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, settings }),
      });
      await refreshPresets(res.name);
      showNotice('Setup "' + res.name + '" saved', 'success');
    } catch (e) {
      showNotice('Could not save setup: ' + e.message, 'error');
    }
  };

  modal.querySelector('#songPresetLoad').onclick = async () => {
    const name = presetSelect.value;
    if (!name) { showNotice('Pick a saved setup first', 'warn'); return; }
    try {
      const data = await presetApi('/api/song_presets/' + encodeURIComponent(name));
      // Merge, so a setup saved without lyrics does not wipe the lyrics you have typed.
      applySongFields(modal, Object.assign(readSongFields(modal), data.settings || {}));
      applySongDirection(modal);
      updateBackendNote();
      updateLenNote();
      showNotice('Setup "' + name + '" loaded', 'success');
    } catch (e) {
      showNotice('Could not load setup: ' + e.message, 'error');
    }
  };

  modal.querySelector('#songPresetDelete').onclick = async () => {
    const name = presetSelect.value;
    if (!name) { showNotice('Pick a saved setup first', 'warn'); return; }
    if (!window.confirm('Delete saved setup "' + name + '"?')) return;
    try {
      await presetApi('/api/song_presets/' + encodeURIComponent(name), { method: 'DELETE' });
      await refreshPresets();
      showNotice('Setup "' + name + '" deleted', 'success');
    } catch (e) {
      showNotice('Could not delete setup: ' + e.message, 'error');
    }
  };

  refreshPresets();
  updateLenNote();

  modal.querySelector('#songSave').onclick = () => {
    try {
      localStorage.setItem(SONG_DRAFT_KEY, JSON.stringify(readSongFields(modal)));
      showNotice('Draft saved in this browser — it loads automatically next time. Use "Save setup..." for named setups.', 'success');
    } catch (e) {
      showNotice('Could not save draft: ' + e.message, 'error');
    }
  };

  modal.querySelector('#songClear').onclick = () => {
    applySongFields(modal, { title: '', lyrics: '', language: 'auto', style: '', singer: 'auto', backend: 'ace', instruments: [] });
    try { localStorage.removeItem(SONG_DRAFT_KEY); } catch (e) {}
    updateBackendNote();
    updateLenNote();
    showNotice('Song fields cleared', 'success');
  };

  // MusicGen has no lyrics/vocals input at all — make that obvious right
  // in the modal instead of letting people discover it after a job runs.
  const backendSelect = modal.querySelector('#songBackend');
  const backendNote = modal.querySelector('#songBackendNote');
  const updateBackendNote = () => {
    backendNote.textContent = backendSelect.value === 'musicgen'
      ? 'MusicGen ignores Lyrics and Singer — instrumental output only, from Style + Instruments.'
      : 'ACE-Step uses your Lyrics and Singer choice to generate sung vocals.';
  };
  backendSelect.onchange = updateBackendNote;
  updateBackendNote();

  modal.querySelector('#songGenerate').onclick = async () => {
    const backend = backendSelect.value;
    const lyrics = modal.querySelector('#songLyrics').value.trim();
    if (backend === 'ace' && !lyrics) {
      showNotice('Enter some lyrics first (or switch to MusicGen for instrumental-only)', 'warn');
      return;
    }
    if (state.isProcessing) { showNotice('Another job is already running', 'warn'); return; }

    const title = modal.querySelector('#songTitle').value.trim();
    const style = modal.querySelector('#songStyle').value.trim();
    const singer = modal.querySelector('#songSinger').value;
    const language = modal.querySelector('#songLanguage').value;
    const instruments = Array.from(modal.querySelectorAll('.songInstrument:checked'))
      .map(el => el.value);

    const genBtn = modal.querySelector('#songGenerate');
    genBtn.disabled = true;
    let res;
    try {
      const r = await fetch('/api/create_song', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, lyrics, language, style, singer, instruments, backend }),
      });
      res = await r.json();
    } catch (e) {
      showNotice('Request failed: ' + e.message, 'error');
      genBtn.disabled = false;
      return;
    }
    if (res.error) {
      showNotice(res.error, 'error');
      genBtn.disabled = false;
      return;
    }
    startJob(res, 'btnCreateSong');
    modal.remove();
  };
}

/* ---------- Editor modal ---------- */
function openEditor(text) {
  const modal = document.createElement('div');
  modal.className = 'editor-modal';
  modal.innerHTML = `
    <div class="editor-box">
      <h3>Edit SRT</h3>
      <textarea id="editorText" spellcheck="false"></textarea>
      <div class="editor-actions">
        <button class="btn accent" id="editorSave">Save</button>
        <button class="btn" id="editorCancel">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector('#editorText').value = text;
  modal.querySelector('#editorCancel').onclick = () => modal.remove();
  modal.querySelector('#editorSave').onclick = async () => {
    const newText = modal.querySelector('#editorText').value;
    const r = await fetch('/api/save_srt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ srt: state.srt, content: newText }),
    });
    const j = await r.json();
    if (j.error) { showNotice('Save failed: ' + j.error, 'error'); return; }
    $('srtPreview').textContent = newText;
    setStatus('SRT saved');
    saveSession();
    modal.remove();
  };
}

/* ---------- Init ---------- */
setStatus('Ready');
lockButtons(false);
resetAllButtonStates();

// Persist setting changes so they survive reloads
['language', 'model', 'chordMethod', 'useDemucs'].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener('change', saveSession);
});

// Restore whatever the user was doing before they closed the browser
restoreSession();