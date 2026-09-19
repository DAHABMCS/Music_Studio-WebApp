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
    btnLyrics: 'ready', btnTab: 'ready', btnFull: 'ready',
  },
  reviewed: {             // has the matching Browse button been clicked
    srt: false, karaoke: false, lyrics: false, tabs: false, transcription: false,
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
  btnLyrics:     'lyrics',
  btnTab:        'tabs',
  btnFull:       'transcription',
};
const ACTION_BUTTON_IDS = Object.keys(ACTION_TO_KIND);

const KIND_TO_BROWSE_BTN = {
  srt:           'browseSrtBtn',
  karaoke:       'browseKaraokeBtn',
  lyrics:        'browseLyricsBtn',
  tabs:          'browseTabsBtn',
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
      btnLyrics: 'ready', btnTab: 'ready', btnFull: 'ready' },
    snap.buttonStatus || {}
  );
  state.reviewed   = Object.assign(
    { srt: false, karaoke: false, lyrics: false, tabs: false, transcription: false },
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
  ['btnSrt', 'btnKaraoke', 'btnLyricVideo', 'btnLyrics', 'btnTab', 'btnFull'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = lock;
  });
  const stop = $('btnStop');
  if (stop) stop.disabled = !lock;
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
  if (result.folder) {
    showNotice(`Transcription ready in folder: ${result.folder}\n\n` +
          (result.files || []).join('\n'));
  }
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

$('btnLyrics').onclick = async () => {
  if (!state.uploaded || !state.srt) { showNotice('Generate the SRT first', 'warn'); return; }
  const r = await fetch('/api/export_lyrics', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      chord_method: $('chordMethod').value,
    }),
  });
  startJob(await r.json(), 'btnLyrics');
};

$('btnTab').onclick = async () => {
  if (!state.uploaded) { showNotice('Choose an input file first', 'warn'); return; }
  const r = await fetch('/api/export_guitar_tab', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      use_demucs: $('useDemucs').checked,
    }),
  });
  startJob(await r.json(), 'btnTab');
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
  state.reviewed = { srt: false, karaoke: false, lyrics: false, tabs: false, transcription: false };
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