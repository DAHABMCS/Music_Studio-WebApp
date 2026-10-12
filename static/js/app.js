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
    try {
      const r2 = await fetch(`/api/output_abs?path=${encodeURIComponent(state.srt)}`);
      if (r2.ok) {
        const a = await r2.json();
        if (a.path && $('outputFilePath')) $('outputFilePath').value = a.path;
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
      $('outputFilePath').value = j.srt_full_path || j.srt_path || (file.name.replace(/\.[^.]+$/, '') + '.srt');
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
  if (result.srt_abs && $('outputFilePath')) {
    $('outputFilePath').value = result.srt_abs;   // complete path, not just the name
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
  if (result.tracks) {
    showNotice('Files ready:\n' + Object.entries(result.tracks)
      .map(([k, v]) => k + ': ' + v).join('\n'), 'success');
  } else if (result.song) showNotice('Song ready: ' + result.song, 'success');
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

if ($('btnAiLyrics')) {
  $('btnAiLyrics').onclick = () => {
    openAiLyricsModal();
  };
}

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

function _isAbsolutePath(p) {
  return /^[A-Za-z]:[\\/]/.test(p) || p.startsWith('/') || p.startsWith('\\\\'.slice(0,2));
}

async function copySrtPath() {
  let v = ($('outputFilePath').value || '').trim();
  if (!v) { showNotice('No output path yet', 'warn'); return; }

  // The box can still hold a bare "Song.srt" (older saved session, or the
  // file hasn't been generated yet). Ask the server for the complete path
  // so what lands on the clipboard is never just the file name.
  if (!_isAbsolutePath(v)) {
    try {
      const r = await fetch(`/api/output_abs?path=${encodeURIComponent(state.srt || v)}`);
      if (r.ok) {
        const a = await r.json();
        if (a.path) { v = a.path; $('outputFilePath').value = v; }
      }
    } catch (e) { /* fall through with what we have */ }
  }

  // navigator.clipboard only exists in secure contexts (https, or
  // localhost). This dashboard is typically opened over plain http on
  // a LAN IP, where navigator.clipboard is undefined, so fall back to a
  // hidden-textarea + execCommand copy in that case.
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(v)
      .then(() => setStatus('SRT path copied: ' + v))
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
    setStatus(ok ? 'SRT path copied: ' + text : 'Copy failed');
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

const ARABIC_DIALECT_CODES = ['egyptian', 'gulf', 'levantine', 'iraqi', 'maghrebi', 'msa'];

function readSongFields(modal) {
  return {
    title:       modal.querySelector('#songTitle').value.trim(),
    lyrics:      modal.querySelector('#songLyrics').value,
    language:    modal.querySelector('#songLanguage').value,
    style:       modal.querySelector('#songStyle').value.trim(),
    singer:      modal.querySelector('#songSinger').value,
    backend:     modal.querySelector('#songBackend').value,
    instruments: Array.from(modal.querySelectorAll('.songInstrument:checked')).map(el => el.value),
    // The two option checkboxes are saved with everything else (both default ON).
    dialect:     (modal.querySelector('#songDialect') || { value: 'egyptian' }).value,
    include_title_lyrics: !!(modal.querySelector('#songPresetLyrics') || { checked: true }).checked,
    backtrack:            !!(modal.querySelector('#songStems') || { checked: true }).checked,
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
  const diaEl = modal.querySelector('#songDialect');
  if (diaEl) diaEl.value = ARABIC_DIALECT_CODES.includes(fields.dialect) ? fields.dialect : 'egyptian';
  const incEl = modal.querySelector('#songPresetLyrics');
  if (incEl) incEl.checked = fields.include_title_lyrics !== false;
  const bkEl = modal.querySelector('#songStems');
  if (bkEl) bkEl.checked = fields.backtrack !== false;
}

/* ---------- AI Lyrics modal ----------
   Style + a short description of the subject + language -> the server
   (/api/generate_lyrics_ai) asks an AI to write the lyrics. The result is
   editable here, and "Use in AI Song" drops it (with the style, language
   and title) into the Create Song window through the same saved-draft
   mechanism that window already auto-loads (SONG_DRAFT_KEY). */
const AI_LYRICS_LANGUAGES = [
  ['auto', 'Auto - same language as the description'],
  ['ar', 'Arabic - عربي (right to left)'],
  ['en', 'English'],
  ['fa', 'Persian - فارسی (right to left)'],
  ['ur', 'Urdu - اردو (right to left)'],
  ['tr', 'Turkish'], ['fr', 'French'], ['es', 'Spanish'], ['de', 'German'],
  ['it', 'Italian'], ['pt', 'Portuguese'], ['hi', 'Hindi'], ['ru', 'Russian'],
  ['ja', 'Japanese'], ['ko', 'Korean'], ['zh', 'Chinese'],
];

function openAiLyricsModal() {
  document.querySelectorAll('.ailyrics-modal').forEach(m => m.remove());
  const draft = loadSongDraft() || {};

  const modal = document.createElement('div');
  modal.className = 'editor-modal ailyrics-modal';
  const langOptions = AI_LYRICS_LANGUAGES
    .map(([code, label]) => `<option value="${code}">${label}</option>`).join('');

  modal.innerHTML = `
    <div class="editor-box song-box" style="max-width:560px; width:92%; max-height:88vh; overflow-y:auto; box-sizing:border-box; padding:12px 16px;">
      <h3 style="margin:0 0 6px; font-size:16px;">AI Lyrics</h3>
      <div class="song-field">
        <label for="aiLyStyle">Style / genre / mood</label>
        <input type="text" id="aiLyStyle" style="width:100%; box-sizing:border-box; padding:4px 6px; font-size:13px;" placeholder="e.g. romantic pop ballad, slow, emotional">
      </div>
      <div class="song-field">
        <label for="aiLySubject">What is the song about?</label>
        <textarea id="aiLySubject" rows="2" data-dir-fixed dir="auto" spellcheck="false"
          style="width:100%; box-sizing:border-box; height:52px; min-height:52px; max-height:90px; overflow-y:auto; resize:vertical; padding:4px 6px; font-size:13px;"
          placeholder="e.g. missing someone who lives far away, but promising to meet again"></textarea>
      </div>
      <div class="song-field">
        <label for="aiLyLanguage">Language of the lyrics</label>
        <select id="aiLyLanguage" style="width:100%; box-sizing:border-box; padding:3px 6px; font-size:13px;">${langOptions}</select>
      </div>
      <div class="song-field" id="aiLyDialectRow">
        <label for="aiLyDialect">Arabic dialect</label>
        <select id="aiLyDialect" style="width:100%; box-sizing:border-box; padding:3px 6px; font-size:13px;">
          <option value="egyptian">Egyptian - مصري</option>
              <option value="gulf">Gulf - خليجي</option>
              <option value="levantine">Levantine - شامي</option>
              <option value="iraqi">Iraqi - عراقي</option>
              <option value="maghrebi">Maghrebi - مغاربي</option>
              <option value="msa">Modern Standard - فصحى</option>
        </select>
      </div>
      <div class="song-field">
        <label for="aiLyTitle">Title <span class="muted">(optional - the AI suggests one)</span></label>
        <input type="text" id="aiLyTitle" dir="auto" style="width:100%; box-sizing:border-box; padding:4px 6px; font-size:13px;" placeholder="Song title">
      </div>
      <div class="song-field">
        <label for="aiLyResult">Lyrics <span class="muted">(you can edit them before using them)</span></label>
        <textarea id="aiLyResult" rows="8" data-dir-fixed dir="auto" spellcheck="false"
          style="width:100%; box-sizing:border-box; height:170px; min-height:90px; max-height:45vh; overflow-y:auto; resize:vertical; padding:4px 6px; font-size:13px; line-height:1.4;"
          placeholder="The lyrics will appear here..."></textarea>
        <div id="aiLyNote" class="muted" style="margin-top:4px; font-size:11px;"></div>
      </div>
      <details id="aiLyKeyBox" style="margin:2px 0 6px; font-size:12px;">
        <summary style="cursor:pointer;">AI key settings <span id="aiLyKeyState" class="muted"></span></summary>
        <div style="display:flex; gap:6px; margin-top:4px;">
          <input type="password" id="aiLyKey" autocomplete="off" placeholder="Optional: paste an Anthropic key (sk-ant-...) for better lyrics"
                 style="flex:1; box-sizing:border-box; padding:4px 6px; font-size:12px;">
          <button class="btn" id="aiLyKeySave">Save key</button>
        </div>
      </details>
      <div class="editor-actions" style="position:sticky; bottom:-12px; padding:8px 0 4px; background:inherit; display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end;">
        <div id="aiLyProg" style="display:none; width:100%; margin-bottom:2px;">
          <div style="position:relative; height:8px; background:rgba(255,255,255,0.15); border-radius:4px; overflow:hidden;">
            <div id="aiLyBar" style="position:absolute; top:0; left:0; height:100%; width:30%; background:#e8b100; border-radius:4px;"></div>
          </div>
          <div id="aiLyProgText" style="font-size:12px; margin-top:3px;"></div>
        </div>
        <button class="btn" id="aiLyCopy">Copy</button>
        <button class="btn" id="aiLySave" title="Save the lyrics as a text file">Save</button>
        <button class="btn accent" id="aiLyGenerate">Generate Lyrics</button>
        <button class="btn" id="aiLyUse" title="Open AI Song with these lyrics filled in">Use in AI Song</button>
        <button class="btn" id="aiLyClose">Close</button>
      </div>
    </div>`;
  document.body.appendChild(modal);

  const q = (sel) => modal.querySelector(sel);
  modal.querySelectorAll('.song-field').forEach(f => {
    f.style.margin = '0 0 6px';
    const lb = f.querySelector('label');
    if (lb) { lb.style.fontSize = '12px'; lb.style.marginBottom = '2px'; lb.style.display = 'block'; }
  });
  q('#aiLyStyle').value    = draft.style || '';
  q('#aiLyLanguage').value = draft.language || 'auto';
  if (q('#aiLyLanguage').value !== (draft.language || 'auto')) q('#aiLyLanguage').value = 'auto';
  q('#aiLyStyle').setAttribute('dir', 'auto');
  q('#aiLyDialect').value = ARABIC_DIALECT_CODES.includes(draft.dialect) ? draft.dialect : 'egyptian';
  const applyDialectRow = () => {
    q('#aiLyDialectRow').style.display = q('#aiLyLanguage').value === 'ar' ? '' : 'none';
  };
  applyDialectRow();
  q('#aiLyLanguage').addEventListener('change', applyDialectRow);

  const applyDir = () => {
    const dir = songLangDir(q('#aiLyLanguage').value);
    const res = q('#aiLyResult');
    res.setAttribute('dir', dir);
    res.style.textAlign = 'start';
    res.style.unicodeBidi = dir === 'auto' ? 'plaintext' : 'normal';
  };
  applyDir();
  q('#aiLyLanguage').addEventListener('change', applyDir);

  const refreshKeyState = async () => {
    try {
      const r = await (await fetch('/api/lyrics_ai_key')).json();
      q('#aiLyKeyState').textContent = r.has_key
        ? '- Anthropic key saved (' + r.hint + ')'
        : '- optional (no key: using your local Ollama model)';
    } catch (e) {}
  };
  refreshKeyState();
  q('#aiLyKeySave').onclick = async () => {
    const key = q('#aiLyKey').value.trim();
    if (!key) { showNotice('Paste your Anthropic key first', 'warn'); return; }
    try {
      const r = await fetch('/api/lyrics_ai_key', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }) });
      const res = await r.json().catch(() => ({}));
      if (!r.ok || res.error) throw new Error(res.error || 'Could not save the key');
      q('#aiLyKey').value = '';
      showNotice('Key saved - you can generate lyrics now', 'success');
      refreshKeyState();
    } catch (e) { showNotice(e.message, 'error'); }
  };

  // ---- Save as a .txt file, and ask before closing unsaved lyrics ----
  let dirty = false;                       // lyrics changed since last save / use
  q('#aiLyResult').addEventListener('input', () => { dirty = true; });

  const saveLyricsFile = () => {
    const lyrics = q('#aiLyResult').value;
    if (!lyrics.trim()) { showNotice('There are no lyrics to save yet', 'warn'); return false; }
    const title = q('#aiLyTitle').value.trim();
    const safe = (title || 'lyrics').replace(/[\\/:*?"<>|]+/g, '').trim().slice(0, 60) || 'lyrics';
    const text = (title ? title + '\r\n\r\n' : '') + lyrics.replace(/\r?\n/g, '\r\n');
    // BOM so Notepad shows Arabic / other scripts correctly
    const blob = new Blob(['\ufeff' + text], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = safe + '.txt';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    dirty = false;
    showNotice('Lyrics saved as ' + safe + '.txt (check your Downloads folder)', 'success');
    return true;
  };
  q('#aiLySave').onclick = saveLyricsFile;

  const askSaveBeforeClose = () => {
    if (modal.querySelector('.ailyrics-confirm')) return;
    const ov = document.createElement('div');
    ov.className = 'ailyrics-confirm';
    ov.style.cssText = 'position:fixed; inset:0; z-index:10000; display:flex; align-items:center; ' +
                       'justify-content:center; background:rgba(0,0,0,0.6);';
    ov.innerHTML = `
      <div style="background:#161b22; color:#e6edf3; border:1px solid #444; border-radius:10px;
                  padding:16px 18px; max-width:380px; width:90%;">
        <div style="font-size:14px; margin-bottom:12px;">
          These lyrics have not been saved. Do you want to save them before closing?
        </div>
        <div style="display:flex; gap:6px; justify-content:flex-end; flex-wrap:wrap;">
          <button class="btn accent" id="aiLyCfSave">Save &amp; close</button>
          <button class="btn" id="aiLyCfDiscard">Close without saving</button>
          <button class="btn" id="aiLyCfCancel">Cancel</button>
        </div>
      </div>`;
    modal.appendChild(ov);
    ov.querySelector('#aiLyCfSave').onclick = () => { if (saveLyricsFile()) modal.remove(); else ov.remove(); };
    ov.querySelector('#aiLyCfDiscard').onclick = () => modal.remove();
    ov.querySelector('#aiLyCfCancel').onclick = () => ov.remove();
  };
  const tryClose = () => {
    if (dirty && q('#aiLyResult').value.trim()) askSaveBeforeClose();
    else modal.remove();
  };
  q('#aiLyClose').onclick = tryClose;
  modal.addEventListener('click', (e) => { if (e.target === modal) tryClose(); });

  q('#aiLyCopy').onclick = async () => {
    const text = q('#aiLyResult').value;
    if (!text.trim()) { showNotice('There are no lyrics to copy yet', 'warn'); return; }
    try {
      await navigator.clipboard.writeText(text);
      showNotice('Lyrics copied', 'success');
    } catch (e) {
      q('#aiLyResult').select();
      showNotice('Select the lyrics and press Ctrl+C to copy them', 'info');
    }
  };

  q('#aiLyGenerate').onclick = async () => {
    const subject = q('#aiLySubject').value.trim();
    if (!subject) { showNotice('Describe what the song is about first', 'warn'); return; }
    if (q('#aiLyResult').value.trim() &&
        !confirm('Replace the lyrics below with newly written ones?')) return;

    const btn = q('#aiLyGenerate');
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Writing lyrics...';
    q('#aiLyNote').textContent = '';
    setButtonState('btnAiLyrics', 'btn-processing');

    // The server gives no real percentage, so show a moving bar plus the
    // elapsed time - proof the AI is still working.
    const prog = q('#aiLyProg'), bar = q('#aiLyBar'), ptxt = q('#aiLyProgText');
    const t0 = Date.now();
    let pos = 0, dirn = 1;
    prog.style.display = 'block';
    const barTimer = setInterval(() => {
      pos += dirn * 2.2;
      if (pos >= 70) dirn = -1;
      if (pos <= 0) dirn = 1;
      bar.style.left = pos + '%';
    }, 30);
    const tickText = () => {
      const sec = Math.floor((Date.now() - t0) / 1000);
      const mm = String(Math.floor(sec / 60)).padStart(2, '0');
      const ss = String(sec % 60).padStart(2, '0');
      ptxt.textContent = 'AI is writing your lyrics... ' + mm + ':' + ss +
        (sec > 45 ? '  (a local model can take a few minutes - please wait)' : '');
    };
    tickText();
    const textTimer = setInterval(tickText, 1000);
    try {
      const r = await fetch('/api/generate_lyrics_ai', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          style: q('#aiLyStyle').value.trim(),
          subject,
          language: q('#aiLyLanguage').value,
          dialect: q('#aiLyDialect').value,
          title: q('#aiLyTitle').value.trim(),
        }),
      });
      let res = null;
      try { res = await r.json(); } catch (e) { res = null; }
      if (!res) throw new Error('Unexpected reply from the server (are you still signed in?)');
      if (res.error) throw new Error(res.error);
      q('#aiLyResult').value = res.lyrics || '';
      dirty = !!(res.lyrics || '').trim();
      if (res.title && !q('#aiLyTitle').value.trim()) q('#aiLyTitle').value = res.title;
      const lines = (res.lyrics || '').split('\n').filter(l => l.trim() && !/^\[.*\]$/.test(l.trim())).length;
      if (!lines) throw new Error('The AI answered but wrote no lyrics. Press Generate Lyrics again.');
      q('#aiLyNote').style.color = '';
      q('#aiLyNote').textContent = `${lines} sung lines. Edit anything you like, then press "Use in AI Song".`;
    } catch (e) {
      // Keep the error INSIDE this window (a corner toast vanishes after a few seconds).
      let msg = e.message || String(e);
      if (/failed to fetch|networkerror|load failed/i.test(msg)) {
        msg = 'The connection to the app was lost while waiting (is the app still running?). ' +
              'Check the black server window for a line starting with [lyrics-ai].';
      }
      q('#aiLyNote').style.color = '#ff6b60';
      q('#aiLyNote').textContent = 'Could not write lyrics: ' + msg;
      showNotice('Could not write lyrics: ' + msg, 'error', { duration: 15000 });
    } finally {
      clearInterval(barTimer);
      clearInterval(textTimer);
      prog.style.display = 'none';
      btn.disabled = false;
      btn.textContent = label;
      setButtonState('btnAiLyrics', 'btn-ready');
    }
  };

  q('#aiLyUse').onclick = () => {
    const lyrics = q('#aiLyResult').value;
    if (!lyrics.trim()) { showNotice('Generate (or type) some lyrics first', 'warn'); return; }
    const merged = Object.assign({}, loadSongDraft() || {}, {
      title:    q('#aiLyTitle').value.trim() || (loadSongDraft() || {}).title || '',
      lyrics:   lyrics,
      style:    q('#aiLyStyle').value.trim(),
      language: q('#aiLyLanguage').value,
      dialect:  q('#aiLyDialect').value,
    });
    try { localStorage.setItem(SONG_DRAFT_KEY, JSON.stringify(merged)); } catch (e) {}
    dirty = false;
    window.__songDraftFresh = true;
    modal.remove();
    openCreateSongModal();
    showNotice('Lyrics added to AI Song - choose a singer, then press Generate', 'success');
  };
}

/* ---------- Floating hover tips ----------
   Any element with data-tip="text" (or data-tip-from="#selector", which
   reads live text from another element) shows a floating dialog box next
   to the mouse while hovered / focused. Replaces always-visible helper
   text so panels stay compact. */
function initFloatingTips() {
  if (window._floatTipInit) return;
  window._floatTipInit = true;

  const st = document.createElement('style');
  st.textContent = `
    #floatTip { position: fixed; z-index: 2147483000; left: 0; top: 0; max-width: 300px;
      padding: 9px 12px; border-radius: 8px; font: 12.5px/1.45 system-ui, -apple-system, 'Segoe UI', sans-serif;
      color: #f3f4f6; background: #1b2230; border: 1px solid rgba(120,160,255,.55);
      box-shadow: 0 8px 28px rgba(0,0,0,.45); pointer-events: none; opacity: 0;
      transform: translateY(4px); transition: opacity .12s ease, transform .12s ease; white-space: normal; }
    #floatTip.on { opacity: 1; transform: none; }
    .sc-i { display: inline-flex; align-items: center; justify-content: center; width: 14px; height: 14px;
      margin-left: 5px; border-radius: 50%; border: 1px solid currentColor; font: italic 700 9px/1 Georgia, serif;
      opacity: .55; vertical-align: middle; cursor: help; }
    [data-tip]:hover > label .sc-i, [data-tip]:hover .sc-i { opacity: 1; }
  `;
  document.head.appendChild(st);

  const tip = document.createElement('div');
  tip.id = 'floatTip';
  tip.setAttribute('role', 'tooltip');
  document.body.appendChild(tip);

  let cur = null;
  const textFor = (el) => {
    if (el.dataset.tipFrom) {
      const s = document.querySelector(el.dataset.tipFrom);
      return s ? s.textContent.trim() : '';
    }
    return el.dataset.tip || '';
  };
  const place = (x, y) => {
    const pad = 14, r = tip.getBoundingClientRect();
    let left = x + pad, top = y + pad + 4;
    if (left + r.width > window.innerWidth - 8) left = Math.max(8, x - r.width - pad);
    if (top + r.height > window.innerHeight - 8) top = Math.max(8, y - r.height - pad);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  };
  const hide = () => { cur = null; tip.classList.remove('on'); };
  const show = (el, x, y) => {
    const t = textFor(el);
    if (!t) { hide(); return; }
    cur = el;
    tip.textContent = t;
    tip.classList.add('on');
    place(x, y);
  };
  const find = (n) => (n && n.closest) ? n.closest('[data-tip],[data-tip-from]') : null;

  document.addEventListener('mouseover', (e) => {
    const el = find(e.target);
    if (el === cur) return;
    if (!el) { hide(); return; }
    show(el, e.clientX, e.clientY);
  });
  document.addEventListener('mousemove', (e) => {
    if (!cur) return;
    if (!document.body.contains(cur)) { hide(); return; }
    place(e.clientX, e.clientY);
  });
  document.addEventListener('mouseout', (e) => {
    if (cur && !cur.contains(e.relatedTarget)) hide();
  });
  document.addEventListener('mousedown', hide);
  document.addEventListener('focusin', (e) => {
    const el = find(e.target);
    if (!el) return;
    const r = el.getBoundingClientRect();
    show(el, r.left + 12, r.bottom - 6);
  });
  document.addEventListener('focusout', hide);
}

function openCreateSongModal() {
  initFloatingTips();
  document.querySelectorAll('.song-modal').forEach(m => m.remove());

  const modal = document.createElement('div');
  modal.className = 'editor-modal song-modal song-create';

  const instrumentsHtml = INSTRUMENT_OPTIONS.map((name, i) => `
    <label><input type="checkbox" class="songInstrument" value="${name}"> ${name}</label>
  `).join('');

  modal.innerHTML = `
    <style>
      .song-create .song-box { width: min(760px, 96vw) !important; max-width: 96vw !important;
        max-height: 94vh !important; overflow-y: auto; box-sizing: border-box; padding: 12px 16px 14px; }
      .sc-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
      .sc-head h3 { margin: 0; }
      .sc-presets { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 8px;
        margin-bottom: 10px; border: 1px solid rgba(128,128,128,.28); border-radius: 8px; }
      .sc-presets select { flex: 1 1 150px; min-width: 0; }
      .sc-presets .btn { padding: 4px 10px; }
      .sc-presets .preset-check { font-size: 12px; white-space: nowrap; }
      .sc-sec { border: 1px solid rgba(128,128,128,.28); border-radius: 10px; padding: 8px 12px 10px; margin: 0 0 10px; }
      .sc-sec > h4 { margin: 0 0 8px; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; opacity: .7; }
      .sc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
      .sc-full { grid-column: 1 / -1; }
      .song-create .song-field { margin: 0; min-width: 0; }
      .song-create .song-field > label, .sc-lab { display: block; font-size: 12px; margin-bottom: 3px; opacity: .92; }
      .song-create .sc-grid input[type=text], .song-create .sc-grid select, .song-create .sc-grid textarea {
        width: 100%; box-sizing: border-box; }
      .sc-row { display: flex; gap: 8px; align-items: center; }
      .sc-row select { flex: 1 1 0; min-width: 0; width: auto !important; }
      .sc-dial { flex: 1 1 0; min-width: 0; }
      .song-create #songLyrics { height: clamp(100px, 22vh, 220px) !important; min-height: 0 !important; resize: vertical; }
      .sc-tools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
      .sc-tools .btn, .sc-btnrow .btn { padding: 4px 10px; }
      .sc-btnrow { display: flex; align-items: center; gap: 6px; }
      .sc-name { display: block; font-size: 11px; opacity: .75; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; margin-top: 3px; }
      .sc-chips { display: flex; flex-wrap: wrap; gap: 5px 6px; }
      .sc-chip { display: inline-flex; align-items: center; gap: 5px; padding: 2px 9px; font-size: 12px;
        border: 1px solid rgba(128,128,128,.4); border-radius: 999px; cursor: pointer; user-select: none; }
      .sc-chip:has(input:checked) { background: rgba(80,140,255,.22); border-color: rgba(80,140,255,.75); }
      .sc-chip input { margin: 0; }
      .sc-check { display: flex; align-items: center; gap: 6px; font-size: 12px; }
      .song-create .editor-actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; margin-top: 2px; }
      @media (max-width: 560px) { .sc-grid { grid-template-columns: 1fr; } }
    </style>
    <div class="editor-box song-box">
      <div class="sc-head"><h3>Create Song</h3></div>

      <div class="sc-presets song-presets">
        <label for="songPresetSelect">Saved setups:</label>
        <select id="songPresetSelect"><option value="">Loading...</option></select>
        <button class="btn" type="button" id="songPresetLoad">Load</button>
        <button class="btn" type="button" id="songPresetSave">Save setup...</button>
        <button class="btn" type="button" id="songPresetDelete">Delete</button>
        <label class="preset-check" data-tip="Also save / use the title and lyrics text (saved setups, drafts and the song folder).">
          <input type="checkbox" id="songPresetLyrics" checked> title &amp; lyrics <i class="sc-i">i</i>
        </label>
      </div>

      <div class="sc-sec">
        <h4>Lyrics</h4>
        <div class="sc-grid">
          <div class="song-field sc-full">
            <label for="songTitle">Title</label>
            <input type="text" id="songTitle" placeholder="Song title">
          </div>
          <div class="song-field sc-full" data-tip="Sung language and reading direction. Auto detects it from the lyrics. The dialect box appears for Arabic and sets the singer's accent.">
            <label for="songLanguage">Language <i class="sc-i">i</i></label>
            <div class="sc-row">
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
              <div class="sc-dial" id="songDialectRow">
                <select id="songDialect" aria-label="Arabic dialect">
                  <option value="egyptian">Egyptian - مصري</option>
                  <option value="gulf">Gulf - خليجي</option>
                  <option value="levantine">Levantine - شامي</option>
                  <option value="iraqi">Iraqi - عراقي</option>
                  <option value="maghrebi">Maghrebi - مغاربي</option>
                  <option value="msa">Modern Standard - فصحى</option>
                </select>
              </div>
            </div>
          </div>
          <div class="song-field sc-full" data-tip="Verses, Chorus, Bridge, etc. Mark sections with tags like [Verse 1], [Chorus], [Bridge] on their own line.">
            <label for="songLyrics">Lyrics <i class="sc-i">i</i></label>
            <textarea id="songLyrics" data-dir-fixed dir="auto" spellcheck="false"
              placeholder="[Verse 1]&#10;...&#10;&#10;[Chorus]&#10;...&#10;&#10;[Verse 2]&#10;...&#10;&#10;[Bridge]&#10;..."></textarea>
          </div>
          <div class="song-field sc-full sc-tools">
            <input type="file" id="songLyricsFile" accept=".txt,.text,text/plain" hidden>
            <button class="btn" type="button" id="songLyricsPick" data-tip="Fill the lyrics box from a .txt file on this computer.">Load lyrics from file...</button>
            <span id="songLenNote" class="muted"></span>
          </div>
        </div>
      </div>

      <div class="sc-sec">
        <h4>Sound</h4>
        <div class="sc-grid">
          <div class="song-field" data-tip-from="#songBackendNote">
            <label for="songBackend">Backend <i class="sc-i">i</i></label>
            <select id="songBackend">
              <option value="ace">ACE-Step — lyrics + sung vocals</option>
              <option value="musicgen">MusicGen — instrumental only, faster</option>
            </select>
            <div id="songBackendNote" hidden></div>
          </div>
          <div class="song-field" data-tip="Who sings. Auto lets the model choose; Instrumental produces no vocals.">
            <label for="songSinger">Singer <i class="sc-i">i</i></label>
            <select id="songSinger">
              <option value="auto">Auto</option>
              <option value="male">Male</option>
              <option value="female">Female</option>
              <option value="duet">Duet (Male + Female)</option>
              <option value="instrumental">Instrumental (no vocals)</option>
            </select>
          </div>
          <div class="song-field sc-full">
            <label for="songStyle">Genre / mood / description</label>
            <input type="text" id="songStyle" placeholder="e.g. upbeat pop rock, 120bpm, anthemic">
          </div>
          <div class="song-field sc-full">
            <label>Instruments</label>
            <div class="sc-chips song-instruments">${instrumentsHtml}</div>
          </div>
          <div class="song-field" id="songMusicRow" data-tip="Generate new music from your style, or sing the lyrics over a backing track you provide.">
            <label for="songMusicSource">Music <i class="sc-i">i</i></label>
            <select id="songMusicSource">
              <option value="generate">Generate new music</option>
              <option value="backing">Use my own backing track</option>
            </select>
          </div>
          <div class="song-field" id="songVoiceRow" data-tip="Optional. A 10-30 s clip of the voice you want (e.g. a previous song's vocals). Steers the singer toward it every time; not an exact clone.">
            <label>Voice reference <i class="sc-i">i</i></label>
            <input type="file" id="songVoiceFile" accept="audio/*,.mp3,.wav,.flac,.m4a,.aac,.ogg,.opus,.wma" hidden>
            <div class="sc-btnrow">
              <button class="btn" type="button" id="songVoicePick">Choose voice sample...</button>
              <button class="btn" type="button" id="songVoiceClear" style="display:none;">Remove</button>
            </div>
            <span id="songVoiceName" class="sc-name"></span>
          </div>
          <div class="song-field sc-full" id="songBackingBox" style="display:none;"
               data-tip="Lyrics are sung over your track (its tempo and structure) instead of new music. Instruments and backtrack are not used. Mixing takes a few extra minutes.">
            <input type="file" id="songBackingFile" accept="audio/*,.mp3,.wav,.flac,.m4a,.aac,.ogg,.opus,.wma" hidden>
            <div class="sc-btnrow">
              <button class="btn" type="button" id="songBackingPick">Choose backing track...</button>
              <span id="songBackingName" class="muted"></span>
            </div>
            <audio id="songBackingPlayer" controls style="display:none; width:100%; margin-top:6px;"></audio>
            <label class="sc-check" style="margin-top:6px;" data-tip="Keeps your music exactly as it is and lays the singing on top of it.">
              <input type="checkbox" id="songMixOriginal" checked> Put the singing on top of my original track <i class="sc-i">i</i>
            </label>
          </div>
          <label id="songStemsRow" class="sc-check sc-full" data-tip="Makes two files: the full song and a clean instrumental backtrack of the same length and style. Takes about twice as long. Both are volume-matched.">
            <input type="checkbox" id="songStems" checked> Also create a separate backtrack (music only) <i class="sc-i">i</i>
          </label>
        </div>
      </div>

      <div class="editor-actions">
        <button class="btn" id="songBrowseOutput" data-tip="Open the folder where generated songs are saved.">Browse Output</button>
        <button class="btn" id="songSave" data-tip="Remember these fields in this browser (loads automatically next time).">Save Draft</button>
        <button class="btn" id="songClear" data-tip="Empty every field in this window.">Clear</button>
        <button class="btn accent" id="songGenerate" data-tip="Start generating the song with these settings.">Generate</button>
        <button class="btn" id="songCancel" data-tip="Close this window without generating.">Cancel</button>
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

  /* ----- Arabic dialect dropdown: only shown when the song is Arabic ----- */
  function updateDialectVisibility() {
    const row = modal.querySelector('#songDialectRow');
    if (!row) return;
    const lang = modal.querySelector('#songLanguage').value;
    const text = modal.querySelector('#songLyrics').value;
    const isArabic = lang === 'ar' || (lang === 'auto' && /[\u0600-\u06FF\u0750-\u077F]{3}/.test(text));
    row.style.display = isArabic ? '' : 'none';
  }
  modal.querySelector('#songLanguage').addEventListener('change', updateDialectVisibility);
  modal.querySelector('#songLyrics').addEventListener('input', updateDialectVisibility);
  updateDialectVisibility();

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

  async function refreshPresets(selectName, pickLast) {
    try {
      const data = await presetApi('/api/song_presets');
      const list = data.presets || [];
      presetSelect.innerHTML = list.length
        ? list.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('')
        : '<option value="">- none saved yet -</option>';
      if (selectName) presetSelect.value = selectName;
      else if (pickLast && list.length) {
        // Startup: select the setup used last time (else the newest saved one).
        const wanted = data.last && list.some(p => p.name === data.last) ? data.last : list[0].name;
        presetSelect.value = wanted;
      }
    } catch (e) {
      presetSelect.innerHTML = '<option value="">- could not load list -</option>';
    }
    return presetSelect.value;
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

  const SETUP_DEFAULTS = { title: '', lyrics: '', language: 'auto', style: '', singer: 'auto',
    backend: 'ace', instruments: [], dialect: 'egyptian', include_title_lyrics: true, backtrack: true };

  async function loadSelectedPreset(fromDropdown, quiet, replace) {
    const name = presetSelect.value;
    if (!name) {
      // Auto-load on the dropdown ignores the placeholder entries silently.
      if (!fromDropdown) showNotice('Pick a saved setup first', 'warn');
      return;
    }
    try {
      const data = await presetApi('/api/song_presets/' + encodeURIComponent(name));
      // Merge, so a setup saved without lyrics does not wipe the lyrics you have typed.
      // replace=true (startup): every field comes from the setup, nothing stale is kept.
      // Otherwise merge, so a setup saved without lyrics keeps what you typed.
      const base = replace ? Object.assign({}, SETUP_DEFAULTS) : readSongFields(modal);
      applySongFields(modal, Object.assign(base, data.settings || {}));
      applySongDirection(modal);
      updateBackendNote();
      updateStemsVisibility();
      updateLenNote();
      updateDialectVisibility();
      if (!quiet) showNotice('Setup "' + name + '" loaded', 'success');
    } catch (e) {
      showNotice('Could not load setup: ' + e.message, 'error');
    }
  }

  // Picking a setup in the dropdown loads it straight away and fills the fields.
  presetSelect.addEventListener('change', () => loadSelectedPreset(true));
  // The Load button still works (handy to reload the same setup after editing).
  modal.querySelector('#songPresetLoad').onclick = () => loadSelectedPreset(false);

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

  // Startup: select the last-used setup and load ALL of its data (title, lyrics, style,
  // singer, instruments, ...) so the dropdown name always matches what is in the boxes.
  // The one exception: lyrics just sent over from the AI Lyrics window. Those are shown,
  // and the dropdown says "current draft" so it never names a setup that is not loaded.
  const freshDraft = !!window.__songDraftFresh;
  window.__songDraftFresh = false;
  refreshPresets(null, true).then(name => {
    if (freshDraft) {
      presetSelect.insertAdjacentHTML('afterbegin',
        '<option value="">- current draft (not a saved setup) -</option>');
      presetSelect.value = '';
      return;
    }
    if (name) return loadSelectedPreset(true, true, true);
  });
  updateLenNote();

  modal.querySelector('#songSave').onclick = () => {
    try {
      const draft = readSongFields(modal);
      if (!draft.include_title_lyrics) { delete draft.lyrics; delete draft.title; }
      localStorage.setItem(SONG_DRAFT_KEY, JSON.stringify(draft));
      showNotice('Draft saved in this browser — it loads automatically next time. Use "Save setup..." for named setups.', 'success');
    } catch (e) {
      showNotice('Could not save draft: ' + e.message, 'error');
    }
  };

  modal.querySelector('#songClear').onclick = () => {
    applySongFields(modal, { title: '', lyrics: '', language: 'auto', style: '', singer: 'auto', backend: 'ace', instruments: [] });
    try { localStorage.removeItem(SONG_DRAFT_KEY); } catch (e) {}
    resetAudioInputs();
    updateBackendNote();
    updateLenNote();
    updateDialectVisibility();
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
  const stemsRow = modal.querySelector('#songStemsRow');
  const musicSource = modal.querySelector('#songMusicSource');
  const musicRow = modal.querySelector('#songMusicRow');
  const voiceRow = modal.querySelector('#songVoiceRow');
  const backingBox = modal.querySelector('#songBackingBox');
  const backingFile = modal.querySelector('#songBackingFile');
  const voiceFile = modal.querySelector('#songVoiceFile');
  // Own backing track / voice reference are ACE-Step features (MusicGen has no vocals).
  const usingBacking = () => backendSelect.value === 'ace' && musicSource.value === 'backing';
  const updateStemsVisibility = () => {
    const isAce = backendSelect.value === 'ace';
    // The separate "backtrack" copy is pointless when the user brought the music.
    const show = isAce && !usingBacking();
    stemsRow.style.display = show ? 'block' : 'none';
    musicRow.style.display = isAce ? '' : 'none';
    voiceRow.style.display = isAce ? '' : 'none';
    backingBox.style.display = usingBacking() ? 'block' : 'none';
  };
  musicSource.onchange = updateStemsVisibility;
  backendSelect.onchange = () => { updateBackendNote(); updateStemsVisibility(); };
  updateStemsVisibility();
  updateBackendNote();

  /* ----- audio file pickers (backing track + voice reference) ----- */
  const MAX_SONG_AUDIO_MB = 150;
  let backingUrl = null;
  const backingNameEl = modal.querySelector('#songBackingName');
  const backingPlayer = modal.querySelector('#songBackingPlayer');
  const voiceNameEl = modal.querySelector('#songVoiceName');
  const voiceClearBtn = modal.querySelector('#songVoiceClear');
  function fileLabel(f) { return ' ' + f.name + ' (' + (f.size / 1048576).toFixed(1) + ' MB)'; }
  function resetAudioInputs() {
    backingFile.value = '';
    voiceFile.value = '';
    backingNameEl.textContent = '';
    voiceNameEl.textContent = '';
    voiceClearBtn.style.display = 'none';
    backingPlayer.removeAttribute('src');
    backingPlayer.style.display = 'none';
    if (backingUrl) { URL.revokeObjectURL(backingUrl); backingUrl = null; }
    musicSource.value = 'generate';
    updateStemsVisibility();
  }
  modal.querySelector('#songBackingPick').onclick = () => backingFile.click();
  backingFile.onchange = () => {
    if (backingUrl) { URL.revokeObjectURL(backingUrl); backingUrl = null; }
    const f = backingFile.files && backingFile.files[0];
    if (!f) { backingNameEl.textContent = ''; backingPlayer.style.display = 'none'; return; }
    if (f.size > MAX_SONG_AUDIO_MB * 1048576) {
      showNotice('That audio file is too large (' + MAX_SONG_AUDIO_MB + ' MB max)', 'warn');
      backingFile.value = '';
      backingNameEl.textContent = '';
      backingPlayer.style.display = 'none';
      return;
    }
    backingNameEl.textContent = fileLabel(f);
    backingUrl = URL.createObjectURL(f);
    backingPlayer.src = backingUrl;               // lets the user check it is the right track
    backingPlayer.style.display = 'block';
  };
  modal.querySelector('#songVoicePick').onclick = () => voiceFile.click();
  voiceFile.onchange = () => {
    const f = voiceFile.files && voiceFile.files[0];
    if (f && f.size > MAX_SONG_AUDIO_MB * 1048576) {
      showNotice('That audio file is too large (' + MAX_SONG_AUDIO_MB + ' MB max)', 'warn');
      voiceFile.value = '';
    }
    const g = voiceFile.files && voiceFile.files[0];
    voiceNameEl.textContent = g ? fileLabel(g) : '';
    voiceClearBtn.style.display = g ? '' : 'none';
  };
  voiceClearBtn.onclick = () => {
    voiceFile.value = '';
    voiceNameEl.textContent = '';
    voiceClearBtn.style.display = 'none';
  };

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

    const ownTrack = usingBacking() ? backingFile.files[0] : null;
    const voiceSample = backend === 'ace' ? voiceFile.files[0] : null;
    if (usingBacking() && !ownTrack) {
      showNotice('Choose your backing track first (or set Music back to "Generate new music")', 'warn');
      return;
    }
    if (ownTrack && singer === 'instrumental') {
      showNotice('A backing track is for sung lyrics - pick a singer other than Instrumental', 'warn');
      return;
    }

    const genBtn = modal.querySelector('#songGenerate');
    genBtn.disabled = true;
    let res;
    try {
      const payload = { title, lyrics, language, style, singer, instruments, backend,
                        stems: backend === 'ace' && modal.querySelector('#songStems').checked,
                        backtrack: modal.querySelector('#songStems').checked,
                        dialect: modal.querySelector('#songDialect').value,
                        include_title_lyrics: modal.querySelector('#songPresetLyrics').checked,
                        mix_original: modal.querySelector('#songMixOriginal').checked };
      let init;
      if (ownTrack || voiceSample) {
        // Audio attached -> multipart: the usual fields travel as one JSON "payload".
        const fd = new FormData();
        fd.append('payload', JSON.stringify(payload));
        if (ownTrack) fd.append('backing_track', ownTrack);
        if (voiceSample) fd.append('voice_ref', voiceSample);
        init = { method: 'POST', body: fd };          // browser sets the multipart header
        setStatus('Uploading audio...');
      } else {
        init = { method: 'POST', headers: { 'Content-Type': 'application/json' },
                 body: JSON.stringify(payload) };
      }
      const r = await fetch('/api/create_song', init);
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