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
};

const $ = (id) => document.getElementById(id);

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
});

/* ---------- Upload helper ---------- */
async function uploadFile(file, kind = 'input') {
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const j = await res.json();
    if (j.error) {
      alert('Upload failed: ' + j.error);
      return null;
    }
    if (kind === 'input') {
      state.uploaded = j;
      $('outputFilePath').value = j.srt_path || (file.name.replace(/\.[^.]+$/, '') + '.srt');
      state.srt = null;
    }
    return j;
  } catch (err) {
    alert('Upload failed: ' + err.message);
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
      handleResult(j.result);
      return;
    }
    if (j.status === 'error') {
      state.jobId = null;
      lockButtons(false);
      stopTimer();
      alert('Error: ' + j.error);
      return;
    }
    setTimeout(pollJob, 800);
  } catch (e) {
    setTimeout(pollJob, 2000);
  }
}

function startJob(res) {
  if (!res || res.error) {
    alert(res && res.error ? res.error : 'Unknown error');
    return;
  }
  state.jobId = res.job_id;
  lockButtons(true);
  startTimer();
  setProgress(0);
  setStatus('Starting…');
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
  if (result.video) alert('Karaoke video ready: ' + result.video);
  if (result.lyric_video) alert('Lyric video ready: ' + result.lyric_video);
  if (result.pdf && result.mp3) alert(`Ready:\n${result.pdf}\n${result.mp3}`);
  else if (result.pdf) alert('PDF ready: ' + result.pdf);
  if (result.folder) {
    alert(`Transcription ready in folder: ${result.folder}\n\n` +
          (result.files || []).join('\n'));
  }
}

/* ---------- Actions ---------- */
$('btnSrt').onclick = async () => {
  if (!state.uploaded) return alert('Choose an input file first');
  const r = await fetch('/api/generate_srt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      language: $('language').value,
      model: $('model').value,
      isolate_vocals: $('isolateVocals').checked,
    }),
  });
  startJob(await r.json());
};

$('btnKaraoke').onclick = async () => {
  if (!state.uploaded || !state.srt) return alert('Generate the SRT first');
  const r = await fetch('/api/generate_karaoke', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      background: state.background ? state.background.path : null,
    }),
  });
  startJob(await r.json());
};

$('btnLyricVideo').onclick = async () => {
  if (!state.uploaded || !state.srt) return alert('Generate the SRT first');
  const r = await fetch('/api/generate_lyric_video', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      background: state.background ? state.background.path : null,
    }),
  });
  startJob(await r.json());
};

$('btnLyrics').onclick = async () => {
  if (!state.uploaded || !state.srt) return alert('Generate the SRT first');
  const r = await fetch('/api/export_lyrics', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      srt: state.srt,
      chord_method: $('chordMethod').value,
    }),
  });
  startJob(await r.json());
};

$('btnTab').onclick = async () => {
  if (!state.uploaded) return alert('Choose an input file first');
  const r = await fetch('/api/export_guitar_tab', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: state.uploaded.path,
      use_demucs: $('useDemucs').checked,
    }),
  });
  startJob(await r.json());
};

$('btnFull').onclick = async () => {
  if (!state.uploaded) return alert('Choose an input file first');
  const r = await fetch('/api/full_transcription', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: state.uploaded.path }),
  });
  startJob(await r.json());
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
};

$('btnClear').onclick = () => {
  $('srtPreview').textContent = '—';
  setProgress(0);
  setStatus('Ready');
  stopTimer();
};

$('btnEdit').onclick = () => {
  const srt = $('srtPreview').textContent;
  if (!srt || srt === '—') return alert('Nothing to edit yet');
  openEditor(srt);
};

function copySrtPath() {
  const v = $('outputFilePath').value;
  if (!v) return alert('No output path yet');
  navigator.clipboard.writeText(v)
    .then(() => setStatus('SRT path copied'))
    .catch(() => setStatus('Copy failed'));
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
    alert('Could not load folder: ' + e.message);
    return;
  }
  if (data.error) {
    alert('Could not load folder: ' + data.error);
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
    if (j.error) return alert('Save failed: ' + j.error);
    $('srtPreview').textContent = newText;
    setStatus('SRT saved');
    modal.remove();
  };
}

/* ---------- Init ---------- */
setStatus('Ready');
lockButtons(false);