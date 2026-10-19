/* Settings: the status panel with Test buttons, Claude (key / Claude Code / demo), the PDF engine, Connect Google,
   the profile files, the tracker token. Talks to /api/setup and friends; app.js owns the rest of the page. */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const RT = window.RT, api = RT.api, toast = RT.toast, esc = RT.esc;
  const STATE_LABEL = { ok: 'Ready', missing: 'Not set up', failing: 'Failing', optional: 'Optional', demo: 'Demo mode' };
  const ROWS = [['claude', 'Claude', 'claude'], ['pdf', 'PDF engine', 'pdf'], ['google', 'Google Drive', 'google'], ['profiles', 'Profile', null], ['worker', 'Worker', null]];
  const view = { status: null, file: 'profile.toml', profile: null, files: null, dirty: false };

  async function load() {
    const r = await api('/api/setup');
    if (!r.ok) { $('#status-list').innerHTML = `<li class="bad">${esc(r.error)}</li>`; return; }
    view.status = r;
    paint();
  }

  function paint() {
    const st = view.status;
    $('#home-path').textContent = st.home;
    $('#status-list').innerHTML = ROWS.map(([key, label, test]) => {
      const row = st[key];
      return `<li class="srow"><span class="sname">${label}</span><span class="tag st-${row.state}">${key === 'google' && row.state === 'ok' ? 'Connected' : STATE_LABEL[row.state] || row.state}</span>`
        + `<span class="sdetail">${esc(row.detail)}</span>`
        + (test ? `<button type="button" data-test="${test}">Test</button>` : '') + '</li>';
    }).join('');
    // Claude
    const c = st.claude;
    $$('input[name="backend"]').forEach(i => { i.checked = i.value === c.backend; i.disabled = c.forced_demo; });
    $('#forced-demo').hidden = !c.forced_demo;
    $$('[data-backend]').forEach(el => { el.hidden = el.dataset.backend !== c.backend; });
    $('#key-state').textContent = c.key ? `Saved: ${c.key}` : 'No key saved. The ANTHROPIC_API_KEY environment variable also works.';
    const models = c.models.some(m => m[0] === c.model) ? c.models : c.models.concat([[c.model, c.model]]);
    $('#model').innerHTML = models.map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('');
    $('#model').value = c.model;
    $('#model').disabled = c.backend === 'demo';
    // PDF
    const p = st.pdf, a = p.available;
    $('#pdf-engine').value = p.engine;
    $('#pdf-avail').textContent = `On this computer: LibreOffice ${a.libreoffice ? 'found' : 'not found'}, Word ${a.word ? 'found' : 'not found'}, Google ${a.google ? 'connected' : 'not connected'}.`
      + (p.poppler_missing.length ? ` Missing for the checks: ${p.poppler_missing.join(', ')} (poppler).` : '');
    // Google
    const g = st.google;
    $('#g-redirect').textContent = g.redirect_uri;
    $('#g-state').textContent = !g.client ? 'No OAuth client saved yet.' : g.connected ? `Client ${g.client_id} saved. Connected as ${g.email || 'your account'}.` : `Client ${g.client_id} saved. Not connected yet.`;
    $('#g-connect').hidden = !g.client;
    $('#g-connect').textContent = g.connected ? 'Connect again' : 'Connect Google';
    $('#g-disconnect').hidden = !g.connected;
    $('#google-steps').open = !g.client;
    // profiles
    const list = st.profiles.list || [];
    const pick = $('#prof-pick');
    if (!view.profile || !list.some(x => x.id === view.profile)) view.profile = (RT.state.profile && list.some(x => x.id === RT.state.profile)) ? RT.state.profile : (list[0] && list[0].id) || null;
    pick.innerHTML = list.length ? list.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('') : '<option value="">(no profile yet)</option>';
    pick.value = view.profile || '';
    $('#prof-demo').hidden = list.some(x => x.id === 'alex-rivera');
    if (view.profile && (!view.files || view.files.id !== view.profile)) loadProfile();
    if (!view.profile) { view.files = null; paintProfile(); }
    $$('[data-out]').forEach(el => {                          // the last Test result, if any
      const t = st[el.dataset.out] && st[el.dataset.out].tested;
      if (t && !el.textContent) out(el.dataset.out, t);
    });
  }

  function out(what, res) {
    const el = $(`[data-out="${what}"]`);
    if (!el) return;
    el.textContent = res.ok ? res.detail : res.error;
    el.className = 'hint test-out ' + (res.ok ? 'good' : 'bad');
  }

  async function test(what, btn) {
    const buttons = $$(`[data-test="${what}"]`);
    buttons.forEach(b => { b.disabled = true; b.dataset.label = b.textContent; b.textContent = 'Testing…'; });
    const r = await api('/api/setup/test', { what });
    buttons.forEach(b => { b.disabled = false; b.textContent = b.dataset.label; });
    if (!r.ok) return toast(r.error);
    out(what, r.result);
    toast(r.result.ok ? r.result.detail : r.result.error);
    await load();
  }
  document.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-test]');
    if (b && b.closest('#tab-settings')) test(b.dataset.test, b);
  });

  async function settings(body, said) {
    const r = await api('/api/settings', body);
    toast(r.ok ? said : r.error);
    await load();
    RT.refresh();
  }

  // ---------- Claude ----------
  $$('input[name="backend"]').forEach(i => i.addEventListener('change', () => settings({ claude_backend: i.value },
    { api: 'Using your Anthropic API key', cli: 'Using Claude Code on this computer', demo: 'Demo mode on: no AI calls' }[i.value])));
  $('#save-key').addEventListener('click', async () => {
    const key = $('#api-key').value.trim();
    if (!key) return toast('Paste the key first');
    const r = await api('/api/setup/anthropic_key', { key });
    if (!r.ok) return toast(r.error);
    $('#api-key').value = '';
    toast('Key saved. Press Test Claude to check it.');
    await load(); RT.refresh();
  });
  $('#remove-key').addEventListener('click', async () => {
    const r = await api('/api/setup/anthropic_key', { key: '' });
    toast(r.ok ? 'Key removed' : r.error);
    await load(); RT.refresh();
  });
  $('#model').addEventListener('change', () => settings({ model: $('#model').value }, 'Model: ' + $('#model').selectedOptions[0].textContent));

  // ---------- PDF ----------
  $('#pdf-engine').addEventListener('change', () => settings({ pdf_engine: $('#pdf-engine').value }, 'PDF engine: ' + $('#pdf-engine').selectedOptions[0].textContent));

  // ---------- Google ----------
  $('#g-save').addEventListener('click', async () => {
    const r = await api('/api/setup/google_client', { json: $('#g-json').value, client_id: $('#g-id').value, client_secret: $('#g-secret').value });
    if (!r.ok) return toast(r.error);
    ['#g-json', '#g-id', '#g-secret'].forEach(s => { $(s).value = ''; });
    toast('Client saved. Now press Connect Google.');
    await load();
  });
  $('#g-remove').addEventListener('click', async () => {
    const r = await api('/api/setup/google_client', { remove: true });
    toast(r.ok ? 'Google client removed' : r.error);
    await load();
  });
  $('#g-disconnect').addEventListener('click', async () => {
    const r = await api('/api/setup/google/disconnect', {});
    toast(r.ok ? (r.revoked ? 'Disconnected, and Google revoked the access' : 'Disconnected here (Google could not be reached to revoke)') : r.error);
    await load();
  });
  $('#g-connect').addEventListener('click', () => {           // the consent screen opens in a new tab; check back
    let n = 0;
    const timer = setInterval(async () => { n += 1; await load(); if ((view.status && view.status.google.connected) || n > 60) clearInterval(timer); }, 3000);
  });
  window.addEventListener('message', (ev) => { if (ev.data === 'google-auth') load(); });

  // ---------- profile files ----------
  async function loadProfile() {
    if (!view.profile) return;
    const r = await api(`/api/profiles/${encodeURIComponent(view.profile)}/files`);
    if (!r.ok) return toast(r.error);
    view.files = r; view.dirty = false;
    paintProfile();
  }
  function paintProfile() {
    const f = view.files;
    $('#prof-text').disabled = !f;
    $('#prof-save').disabled = !f;
    $('#prof-upload').disabled = !f;
    $$('.file-tabs [data-file]').forEach(b => b.setAttribute('aria-selected', b.dataset.file === view.file));
    $('#prof-text').value = f ? (f.files[view.file] || '') : '';
    $('#prof-problem').hidden = !(f && f.problem);
    $('#prof-problem').textContent = f && f.problem ? 'This profile has a problem: ' + f.problem : '';
    $('#prof-resumes').innerHTML = f ? (f.resumes.length ? f.resumes.map(x => `<li><code>${esc(x.file)}</code> <span class="hint">${Math.round(x.size / 1024)} KB${x.pdf ? ', has its own PDF' : ''}</span></li>`).join('') : '<li class="hint">No base resume yet: upload one below.</li>') : '';
    $('#prof-msg').textContent = '';
  }
  $('#prof-pick').addEventListener('change', () => { view.profile = $('#prof-pick').value; view.files = null; loadProfile(); });
  $$('.file-tabs [data-file]').forEach(b => b.addEventListener('click', () => {
    if (view.dirty && !confirm('Leave this file without saving?')) return;
    view.file = b.dataset.file; view.dirty = false; paintProfile();
  }));
  $('#prof-text').addEventListener('input', () => { view.dirty = true; $('#prof-msg').textContent = 'Not saved'; });
  $('#prof-save').addEventListener('click', async () => {
    const r = await api(`/api/profiles/${encodeURIComponent(view.profile)}/file`, { name: view.file, text: $('#prof-text').value });
    $('#prof-msg').textContent = r.ok ? 'Saved. The next resume uses it.' : r.error;
    $('#prof-msg').className = 'hint ' + (r.ok ? 'good' : 'bad');
    if (r.ok) { view.dirty = false; view.files.files[view.file] = $('#prof-text').value; await load(); RT.refresh(); }
  });
  $('#prof-upload').addEventListener('change', async () => {
    const file = $('#prof-upload').files[0];
    if (!file) return;
    const data = await new Promise((ok, bad) => { const fr = new FileReader(); fr.onload = () => ok(String(fr.result).split(',')[1]); fr.onerror = bad; fr.readAsDataURL(file); });
    const r = await api(`/api/profiles/${encodeURIComponent(view.profile)}/resume`, { filename: file.name, data });
    $('#prof-upload').value = '';
    toast(r.ok ? `Saved ${r.file}` : r.error);
    if (r.ok) { view.files = null; await load(); await loadProfile(); RT.refresh(); }
  });
  $('#prof-demo').addEventListener('click', async () => {
    const r = await api('/api/demo/install', {});
    if (!r.ok) return toast(r.error);
    view.profile = r.profile; view.files = null;
    toast('Demo profile (Alex Rivera, a made-up person) loaded');
    await load(); RT.refresh();
  });
  $('#prof-new').addEventListener('click', async () => {
    const name = prompt('Full name for the new profile (as it appears on the resume)');
    if (!name) return;
    const id = name.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'me';
    const r = await api('/api/profiles', { id, name });
    if (!r.ok) return toast(r.error);
    view.profile = r.profile; view.files = null; view.file = 'facts.md';
    toast('Profile created. Upload your base resume, then fill in facts.md.');
    await load(); RT.refresh();
  });

  // ---------- tracker + housekeeping ----------
  $('#show-token').addEventListener('click', async () => {
    const input = $('#api-token');
    if (!input.value) { const r = await api('/api/token'); if (r.ok) input.value = r.token; }
    input.type = input.type === 'password' ? 'text' : 'password';
    $('#show-token').textContent = input.type === 'password' ? 'Show' : 'Hide';
  });
  $('#copy-token').addEventListener('click', async () => {
    const r = await api('/api/token');
    if (!r.ok) return toast(r.error);
    try { await navigator.clipboard.writeText(r.token); toast('Token copied'); } catch (e) { $('#api-token').value = r.token; $('#api-token').type = 'text'; toast('Select and copy the token'); }
  });
  $('#tracker-url').addEventListener('change', () => settings({ tracker_url: $('#tracker-url').value }, $('#tracker-url').value ? 'Tracker link saved' : 'Tracker link removed'));
  $('#keep-days').addEventListener('change', () => {
    const days = Number($('#keep-days').value);
    settings({ archived_delete_days: days }, days ? `Archived resumes older than ${days} days are deleted for good` : 'Archived resumes are kept for good');
  });
  $('#max-calls').addEventListener('change', () => settings({ max_claude_calls: Number($('#max-calls').value) }, `Up to ${$('#max-calls').value} Claude calls per resume`));

  RT.onState = (r) => {
    const s = r.settings || {};
    if (document.activeElement !== $('#keep-days')) $('#keep-days').value = s.archived_delete_days ?? 30;
    if (document.activeElement !== $('#max-calls')) $('#max-calls').value = s.max_claude_calls ?? 3;
    if (document.activeElement !== $('#tracker-url')) $('#tracker-url').value = s.tracker_url || '';
  };
  RT.onTab = (name) => { if (name === 'settings') load(); };
  if (!$('#tab-settings').hidden) load();
})();
