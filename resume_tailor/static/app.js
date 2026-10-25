/* Resume Tailor UI: New / Queue / Ready / Archived for one person at a time, lists grouped by day,
   live via SSE, review panel with PDF (desktop) or page image (phone). */
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const state = { profile: null, picked: false, day: '', profiles: [], jobs: [], archived: [], lastEvent: 0, open: null, worker: false, phone: matchMedia('(max-width: 760px)').matches, fixOpen: new Set(), drafts: {} };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ACTIVE = new Set(['queued', 'generating', 'checking', 'waiting']);
  const PDF_VIEW = 'toolbar=0&navpanes=0&view=FitH';   // the page fills the width, no dark viewer bars (Download PDF is a button)
  const NEEDS = new Set(['needs_you', 'needs_place']);                           // needs_place waits for a city
  const IN_QUEUE = (j) => ACTIVE.has(j.status) || NEEDS.has(j.status);            // live work + needs you
  const ENDED_EMPTY = (j) => j.status === 'cancelled' || j.status === 'failed';
  const LABELS = { queued: 'queued', generating: 'generating', checking: 'checking', ready: 'ready', needs_you: 'needs you', needs_place: 'needs you', failed: 'failed', cancelled: 'cancelled', waiting: 'waiting' };
  const BLUR_RENDER_MS = 400, UNDO_MS = 8000, TOAST_MS = 6000, REFRESH_DEBOUNCE_MS = 300, SSE_RETRY_MS = 3000, TICK_MS = 1000, ARM_MS = 5000;
  const BUSY_VIEW_MS = 15000, BUSY_MAX_MS = 15 * 60000, BUSY_POLL_MS = 100, STEP_POLL_MS = 1000, BUSY_TICK_MS = 1000;
  // the file address carries the version on screen, so no viewer (PDF frame, image, API client) keeps an old copy
  const fileUrl = (j, which, extra = '') => `/api/jobs/${j.id}/file/${which}?v=${encodeURIComponent(j.files_version || j.updated_at || '')}${extra}`;
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone, RECENT_DAYS = 30, DAY_MS = 86400000, PERSON_KEY = 'rg.person';
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };

  async function api(path, body) {
    const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    try {
      const r = await fetch(path, opts);
      const data = await r.json().catch(() => ({ ok: false, error: r.statusText }));
      if (data.ok === undefined) data.ok = r.ok;
      return data;
    } catch (err) { return { ok: false, error: 'server unreachable: ' + err.message }; }
  }

  // ---------- tabs ----------
  document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
  function showTab(name) {
    if (!$(`#tab-${name}`)) name = 'new';
    document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.tab === name));
    document.querySelectorAll('.tab').forEach(s => s.hidden = s.id !== 'tab-' + name);
    $('#view-title').textContent = $(`.tabs [data-tab="${name}"] .lbl`).textContent;    // the phone shows where you are
    store.set('rg.tab', name);
    if (window.RT && RT.onTab) RT.onTab(name);
  }
  // light / dark, remembered per browser
  const setTheme = (t) => { if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; store.set('rg.theme', t || ''); };
  const isDark = () => document.documentElement.dataset.theme === 'dark' || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  setTheme(store.get('rg.theme') || null);
  $('#theme').addEventListener('click', () => setTheme(isDark() ? 'light' : 'dark'));

  // ---------- new ----------
  const profileSel = $('#profile'), baseSel = $('#base'), msg = $('#new-msg');
  profileSel.addEventListener('change', () => setProfile(profileSel.value));
  function fillBases() {
    const p = state.profiles.find(x => x.id === profileSel.value);
    baseSel.innerHTML = '<option value="">auto (best ATS score)</option>' + (p ? p.bases.map(b => `<option value="${esc(b.id)}">${esc(b.label)}</option>`).join('') : '');
  }

  // ---------- whose resumes (one person at a time), remembered per browser, ?profile= in links ----------
  const firstName = (p) => String(p.name || p.id).split(/\s+/)[0];
  function orderedProfiles(first) {
    return state.profiles.filter(p => p.id === first).concat(state.profiles.filter(p => p.id !== first));
  }
  function pickProfile(r) {
    const known = (pid) => pid && r.profiles.some(p => p.id === pid) ? pid : null;
    return known(new URLSearchParams(location.search).get('profile')) || known(store.get(PERSON_KEY)) || known(r.profile) || known(r.viewer)
      || known(r.settings && r.settings.default_profile) || (r.profiles[0] && r.profiles[0].id) || null;
  }
  function paintProfiles() {
    const list = orderedProfiles(state.defaultProfile);
    $('#who').innerHTML = list.map(p => `<button type="button" data-person="${esc(p.id)}" data-initial="${esc(firstName(p).slice(0, 1).toUpperCase())}" aria-pressed="${p.id === state.profile}" aria-label="${esc(firstName(p))}'s resumes" title="Show ${esc(p.name)}'s resumes"><span class="who-label">${esc(firstName(p))}</span></button>`).join('');
    const tracker = $('#tracker-link'), turl = state.settings && state.settings.tracker_url;   // optional, from Settings
    tracker.hidden = !turl;
    if (turl) tracker.href = turl;
    profileSel.innerHTML = list.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    profileSel.value = state.profile;
    fillBases();
  }
  function setProfile(pid) {
    if (!pid || !state.profiles.some(p => p.id === pid)) return;
    const changed = pid !== state.profile;
    state.profile = pid;
    store.set(PERSON_KEY, pid);
    const url = new URL(location.href);
    url.searchParams.delete('token');                       // the cookie holds it now; keep it out of shared links
    url.searchParams.set('profile', pid);
    history.replaceState(null, '', url.pathname + url.search + url.hash);
    if (changed && state.open !== null) closePanel();
    paintProfiles();
    if (!changed) return;
    render();                                               // never leave the other person's rows on screen
    refresh();                                              // /api/state?profile= lists all of this person's jobs
  }
  $('#who').addEventListener('click', (ev) => { const b = ev.target.closest('button[data-person]'); if (b) setProfile(b.dataset.person); });
  function say(text, bad) { msg.textContent = text; msg.className = bad ? 'bad' : 'muted small'; }
  $('#fetch').addEventListener('click', async () => {
    const url = $('#url').value.trim(); if (!url) return;
    say('Fetching…');
    const r = await api('/api/jd/fetch', { url });
    if (!r.ok) return say(r.error, true);
    $('#jd').value = r.text; if (r.company) $('#company').value = r.company; if (r.role) $('#role').value = r.role;
    say(`Fetched (${r.source}); check company and role.`);
  });
  $('#jd').addEventListener('blur', async () => {
    const text = $('#jd').value.trim(); if (text.length < 80 || ($('#company').value && $('#role').value)) return;
    const r = await api('/api/jd/guess', { text });
    if (!r.ok) return;
    if (!$('#company').value) $('#company').value = r.company || ''; if (!$('#role').value) $('#role').value = r.role || '';
  });
  $('#new-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = { profile: profileSel.value, company: $('#company').value, role: $('#role').value, jd_text: $('#jd').value, jd_url: $('#url').value || null, base_override: baseSel.value || null };
    const btn = $('#new-form .primary'); btn.disabled = true;
    const r = await api('/api/jobs', body); btn.disabled = false;
    if (!r.ok) return say(r.error, true);
    say(`Queued #${r.ids.join(', #')}` + (r.worker_awake ? '' : ' (the worker is not running: start the app with python -m resume_tailor serve)'));
    ['#jd', '#company', '#role', '#url'].forEach(s => { $(s).value = ''; });
    await refresh(); showTab('queue');
  });
  $('#load-demo').addEventListener('click', async () => {
    const r = await api('/api/demo/install', {});
    if (!r.ok) return toast(r.error);
    toast(r.claude_backend === 'demo' ? 'Demo profile loaded. No API key yet, so demo mode is on (Settings to change).' : 'Demo profile loaded.');
    await refresh();
  });
  $('#go-settings').addEventListener('click', () => showTab('settings'));

  // ---------- lists ----------
  function elapsed(j) {
    if (j.elapsed_s) return Math.round(j.elapsed_s) + 's';
    if (j.started_at && ACTIVE.has(j.status)) return Math.max(0, Math.round((Date.now() - Date.parse(j.started_at)) / 1000)) + 's';
    return '';
  }
  function chip(j) {
    const approved = j.approved_stale ? ' <span class="chip approved" title="Drive has the version before your edit; Approve again sends this one">approved · older version</span>' : ' <span class="chip approved">approved</span>';
    return `<span class="chip ${j.status}">${LABELS[j.status] || j.status}</span>` + (j.drive_link ? approved : '');
  }
  // ---------- when it was made, and whether it is outdated ----------
  function ago(iso) {
    const min = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
    if (min < 1) return 'just now';
    if (min < 60) return `${min} min ago`;
    const h = Math.floor(min / 60);
    return h < 48 ? `${h} h ago` : `${Math.floor(h / 24)} d ago`;
  }
  function stamp(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} · ${ago(iso)}`;
  }
  function timeLine(j) {
    const t = j.times || {};
    if (ACTIVE.has(j.status)) return `Queued ${stamp(t.queued || j.created_at)}`;
    const made = t.made || j.finished_at;
    return (made ? `Made ${stamp(made)}` : '') + (t.updated ? ` · updated ${stamp(t.updated)}` : '');
  }
  const madeAt = (j) => Date.parse((j.times && j.times.made) || j.finished_at || j.created_at) || 0;

  // ---------- day groups (this browser's time zone), rows numbered newest first, each with its time ----------
  const dayFmt = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' });
  const clockFmt = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit' });
  const shortFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric' });
  const dayOf = (ms) => dayFmt.format(ms);                                        // "2026-10-12" here
  const dayNum = (key) => { const [y, m, d] = key.split('-').map(Number); return Date.UTC(y, m - 1, d); };
  const daysAgo = (key) => Math.round((dayNum(dayOf(Date.now())) - dayNum(key)) / DAY_MS);
  function dayTitle(key) {
    const parts = Object.fromEntries(shortFmt.formatToParts(dayNum(key)).map(x => [x.type, x.value]));
    const date = `${parts.weekday} ${parts.month} ${parts.day}`;
    const n = daysAgo(key);
    return n === 0 ? `Today · ${date}` : n === 1 ? `Yesterday · ${date}` : date;
  }
  const unmade = (j) => ACTIVE.has(j.status) || ENDED_EMPTY(j);                   // listed by when it was queued
  const queuedAt = (j) => j.requeued_at || j.created_at;                         // a retry / regenerate: queued again
  const listedAt = (j) => unmade(j) ? (Date.parse(queuedAt(j)) || 0) : madeAt(j);
  function clockAt(iso, day) {
    const ms = Date.parse(iso); if (!ms) return '';
    const time = clockFmt.format(ms);
    if (dayOf(ms) === day) return time;
    const p = Object.fromEntries(shortFmt.formatToParts(dayNum(dayOf(ms))).map(x => [x.type, x.value]));
    return `${p.month} ${p.day}, ${time}`;
  }
  function byDay(rows, row) {
    const groups = new Map();
    rows.slice().sort((a, b) => listedAt(b) - listedAt(a) || b.id - a.id).forEach(j => {
      const day = dayOf(listedAt(j));
      const key = daysAgo(day) > RECENT_DAYS ? 'older' : day;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(j);
    });
    return [...groups].map(([key, items]) => `<section class="day" data-day="${key}"><h3 class="day-h">${esc(key === 'older' ? `More than ${RECENT_DAYS} days` : dayTitle(key))} <span class="day-n">(${items.length})</span></h3><ol class="list">${items.map((j, i) => row(j, i + 1, dayOf(listedAt(j)))).join('')}</ol></section>`).join('');
  }
  function whenLine(j, day) {
    const t = j.times || {};
    const at = (label, iso) => { const c = iso ? clockAt(iso, day) : ''; return c ? `${label} ${c}` : ''; };
    if (ACTIVE.has(j.status) || j.status === 'needs_place') return at('Queued', queuedAt(j));
    const first = ENDED_EMPTY(j) ? [at('Queued', queuedAt(j)), at(LABELS[j.status], j.finished_at)]
      : [at('Made', t.made || j.finished_at || j.created_at)];
    return first.concat([at('updated', t.updated), at('approved', t.approved), at('downloaded', t.downloaded),
      at('archived', j.archived_at)]).filter(Boolean).join(' · ');
  }
  const num = (n) => `<span class="num" aria-label="number ${n}">${n}</span>`;
  function staleBlock(j) {
    if (!j.badge) return '';
    return `<div class="stale"><span class="chip outdated">${esc(j.badge)}</span><ul class="small">${(j.outdated || []).map(r => `<li>${esc(r)}</li>`).join('')}</ul><button data-act="regen-current" data-id="${j.id}" title="Make the whole resume again from your current base resume and rules (uses Claude)">Make it again from my current resume</button></div>`;
  }
  // the row says what it was made from and how it scores; attempts, time and model are in its tooltip (and the panel)
  function metaLine(j) {
    const r = j.result || {};
    return [r.base ? 'base ' + r.base.label : '', ACTIVE.has(j.status) ? elapsed(j) : '', r.ats ? `ATS ${r.ats.base_pct}% → ${r.ats.tailored_pct}%` : ''].filter(Boolean).join(' · ');
  }
  const techLine = (j) => [j.attempts ? 'attempt ' + j.attempts : '', elapsed(j), j.model || ''].filter(Boolean).join(' · ');
  function jobRow(j, n, day) {
    const acts = [];
    if (ACTIVE.has(j.status)) acts.push(`<button data-act="cancel" data-id="${j.id}">Cancel</button>`);
    if (ENDED_EMPTY(j)) acts.push(`<button data-act="retry" data-id="${j.id}">Retry with note</button>`);
    if (j.status === 'ready') acts.push(`<button data-act="archive" data-id="${j.id}" title="Hide from Ready (files are kept)">Archive</button>`);
    const old = oldProblems(j), oldChip = old.length ? ` <span class="chip outdated" title="${esc('Already there before your edit (a rule added since): ' + old.join('; '))}">problems (${old.length})</span>` : '';
    const accepted = j.result && j.result.accepted ? ' <span class="chip accepted" title="' + esc('accepted despite: ' + j.result.accepted.despite.join('; ')) + '">accepted</span>' : '';
    const err = j.error && j.status !== 'ready' ? ' · ' + esc(j.error.slice(0, 160)) : '';
    return `<li class="card job" data-open="${j.id}" data-job="${j.id}"><div><div class="title">${num(n)}${esc(j.company)} · ${esc(j.role)}</div><div class="meta" title="${esc(techLine(j))}">${esc(metaLine(j))}${err}</div><div class="meta when">${esc(whenLine(j, day))}</div></div><div>${chip(j)}${accepted}${oldChip}</div>${staleBlock(j)}<div class="actions">${acts.join('')}</div></li>`;
  }
  const WAS_IN = { ready: 'Ready', cancelled: 'Queue · cancelled', failed: 'Queue · failed' };
  const DROPPED_TO = { ready: 'Ready', needs_you: 'Queue · needs you' };            // cancelled by you
  // these ask twice ([label, armed label, hint]); both can be undone, but a stray key should not do it
  const ARMED = { drop: ['Cancel resume', 'Yes, cancel it', 'Press Cancel again to drop this resume (Undo stays possible)'],
    undo: ['Undo my edit', 'Yes, undo it', 'Press Undo again to go back to the version before your last edit'],
    delete: ['× Delete', 'Yes, delete for good', 'Press again to delete it for good (Undo for 8 s)'] };
  function archivedRow(j, n, day) {
    const r = j.result || {};
    const where = (j.dropped_from ? DROPPED_TO[j.dropped_from] : (WAS_IN[j.status] || j.status)) + (j.drive_link ? ' · approved' : '');
    return `<li class="card job archived" data-open="${j.id}" data-job="${j.id}"><div><div class="title">${num(n)}${esc(j.company)} · ${esc(j.role)}</div><div class="meta">${esc([r.base ? 'base ' + r.base.label : '', 'returns to ' + where].filter(Boolean).join(' · '))}</div><div class="meta when">${esc(whenLine(j, day))}</div></div><div>${chip(j)}${j.badge ? ` <span class="chip outdated">${esc(j.badge)}</span>` : ''}</div><div class="actions"><button data-act="restore" data-id="${j.id}" title="Put it back where it was; files and Drive copy unchanged">Restore</button><button data-act="delete" data-id="${j.id}" class="danger${state.armed === `delete:${j.id}` ? ' armed' : ''}" title="Delete for good: this resume, its files and its Google Doc copy (the approved PDF in Drive stays). Press twice; Undo for 8 s">${ARMED.delete[state.armed === `delete:${j.id}` ? 1 : 0]}</button></div></li>`;
  }
  function fixForm(id) {
    const open = state.fixOpen.has(String(id));
    return `<form class="fixform" data-fix="${id}"${open ? '' : ' hidden'}><label class="small">What should change? Everything else stays as it is.<textarea name="note" rows="2" placeholder="only make the summary shorter · keep bullet 2 as it was">${esc(state.drafts[id] || '')}</textarea></label><div class="actions"><button type="submit" class="primary">Fix it</button><button type="button" data-act="fixcancel" data-id="${id}" class="ghost">Cancel</button></div></form>`;
  }
  // after a note or Doc edits the person picks: Accept as it is (was Looks good) / Fix with note / Open as Google Doc / Undo / Cancel
  function reviewActions(j, inPanel) {
    const id = j.id, out = [];
    if (j.status === 'needs_you') out.push(`<button class="primary" data-act="accept" data-id="${id}" title="Accept this resume as it is; it moves to Ready (key g)">Accept as it is</button>`);
    out.push(`<button data-act="fixopen" data-id="${id}" title="One Claude call that changes only what your note names (key f)">Fix with note</button>`);
    if (!inPanel && j.status === 'needs_you') out.push(`<button data-act="gdoc" data-id="${id}" title="Edit it yourself in Google Docs, then press Get my Doc edits (key o)">Open as Google Doc</button>`);
    if (j.undo_count) out.push(`<button data-act="undo" data-id="${id}" class="${state.armed === `undo:${id}` ? 'armed' : ''}" title="Back to the version before your last Doc edits or note (key u, twice)">${state.armed === `undo:${id}` ? ARMED.undo[1] : ARMED.undo[0]}</button>`);
    const armed = state.armed === `drop:${id}`;
    out.push(`<button data-act="drop" data-id="${id}" class="danger${armed ? ' armed' : ''}" title="Drop this resume: it goes to Archived (Restore brings it back) and is never uploaded (key x, twice)">${armed ? ARMED.drop[1] : ARMED.drop[0]}</button>`);
    if (!inPanel) out.push(`<button data-act="open" data-id="${id}" class="ghost">Open PDF</button>`);
    return `<div class="actions">${out.join('')}</div>${fixForm(id)}`;
  }
  const lastKind = (r) => (((r && r.attempts) || []).slice(-1)[0] || {}).kind;
  // a re-import whose only problems were there before the edit stays in Ready; else it says where it went
  const reimportOf = (j) => (lastKind(j.result) === 'reimport' && (j.result || {}).reimport) || {};
  const oldProblems = (j) => j.status === 'ready' ? (reimportOf(j).old_problems || []) : [];
  const PLACE = { ready: 'Ready', needs_you: 'Queue · needs you' };
  const plural = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`;
  function placeLine(j, x) {
    const old = (x.old_problems || []).length, fresh = (x.new_problems || []).length;
    if (x.moved && PLACE[x.moved.to]) return `<div class="moved"><b>Moved from ${esc(PLACE[x.moved.from] || x.moved.from)} to ${esc(PLACE[x.moved.to])}</b>${fresh ? `: your edit caused ${plural(fresh, 'new problem')}` : ''}${old ? ` (${plural(old, 'other problem')} ${old === 1 ? 'was' : 'were'} already there before it)` : ''}</div>`;
    if (j.status === 'ready' && old) return `<div class="warnline"><b>Stays in Ready:</b> your edit caused no new problem; ${plural(old, 'problem')} below ${old === 1 ? 'was' : 'were'} already there before it (a rule added since).</div>`;
    return '';
  }
  function editBlock(j) {
    const r = j.result || {}, kind = lastKind(r);
    if (kind === 'fix' && r.fix) {
      const f = r.fix;
      const did = f.applied ? `<div><b>Changed:</b> ${esc((f.changes || []).join(' · ') || 'nothing')}</div>` : `<div class="bad">Not applied: ${esc(f.not_done || '')}; the resume is as it was.</div>`;
      const cannot = f.cannot ? `<div class="warnline">Not done: ${esc(f.cannot)}</div>` : '';
      const saved = (f.saved || []).length ? `<div class="small muted">Saved to your skills for later resumes: ${esc(f.saved.join(', '))}</div>` : '';
      return `<div class="edit" data-edit="fix"><div><b>Your note:</b> “${esc(f.note)}”</div>${did}${cannot}${saved}</div>`;
    }
    if (kind === 'reimport' && r.reimport) {
      const x = r.reimport, at = x.doc && x.doc.modifiedTime ? ` · Doc edited ${esc(stamp(x.doc.modifiedTime))}` : '';
      return `<div class="edit" data-edit="reimport"><div><b>Your Google Doc edits</b> (edit ${x.n}${at})</div><div><b>Changed:</b> ${esc((x.changes || []).join(' · '))}</div>${placeLine(j, x)}<div class="small muted">${esc(x.format_note || '')}</div></div>`;
    }
    return '';
  }
  function needsCard(j, n, day) {
    const r = j.result || {}, files = r.files || {};
    const img = files.png ? `<img class="thumb" src="${esc(fileUrl(j, 'png'))}" alt="page 1 of the resume it produced" data-open="${j.id}" loading="lazy">` : '<div class="thumb none muted small">no page yet</div>';
    const ats = r.ats ? `<div class="score"><span>ATS</span><b>${r.ats.base_pct}%</b><span>base →</span><b>${r.ats.tailored_pct}%</b></div>` : '';
    const why = (j.reasons || []).map(x => `<li><b>${esc(x.area)}:</b> ${esc(x.text)}</li>`).join('');
    const diff = r.diff ? `<details class="small"><summary>What changed vs the base</summary>${diffBody(r.diff)}</details>` : '';
    return `<li class="card needs" data-job="${j.id}"><div class="needs-grid">${img}<div><div class="title">${num(n)}${esc(j.company)} · ${esc(j.role)} ${chip(j)}</div><div class="meta" title="${esc(techLine(j))}">${esc(metaLine(j))}</div><div class="meta when">${esc(whenLine(j, day))}</div>${ats}${editBlock(j)}<p class="warn small">${['fix', 'reimport'].includes(lastKind(r)) ? 'Problems to look at (shown as it is):' : 'Why it stopped:'}</p><ul class="fails">${why || '<li>no reason recorded</li>'}</ul>${diff}${reviewActions(j, false)}</div></div></li>`;
  }
  // the job's city could not be worked out: pick one (or Remote, or type it), then it is queued
  function placeCard(j, n, day) {
    const id = j.id, pick = (v, label, tip) => `<button data-act="place" data-id="${id}" data-place="${esc(v)}" title="${esc(tip)}">${esc(label)}</button>`;
    const btns = (j.place_choices || []).map(c => pick(c, c, `Put ${c} on the resume and queue it`)).join('') + pick('remote', `Remote → ${j.home || 'own city'}`, `A remote job: your own city, ${j.home || ''}`);
    return `<li class="card job needs" data-job="${id}"><div><div class="title">${num(n)}${esc(j.company)} · ${esc(j.role)} ${chip(j)}</div><div class="meta">${esc(j.error || 'needs you: pick the location')}</div><div class="meta when">${esc(whenLine(j, day))}</div><div class="actions">${btns}<button data-act="placeother" data-id="${id}" class="ghost" title="Type the city (Enter saves)">Other…</button><button data-act="cancel" data-id="${id}" class="ghost">Cancel</button></div><form class="placeform" data-place="${id}" hidden><input name="place" placeholder="City, ST" autocomplete="off" aria-label="Other city, as City, ST" title="Type the city as City, ST (Enter saves)"></form></div></li>`;
  }
  // the resume lists' search: company, title, date and status (any word order, case and punctuation ignored,
  // "AI/ML" = "AI ML", a word of 5+ letters may be one edit off; a date as 10/23, Oct 23
  // or 2026-10-23 matches the day it was queued, made or archived)
  const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
  const normText = (s) => (String(s || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase().match(/[a-z0-9]+/g) || []).join(' ');
  function edits(a, b, limit) {
    if (Math.abs(a.length - b.length) > limit) return limit + 1;
    let prev2 = null, prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i];
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] !== b[j - 1]));
        if (prev2 && i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) cur[j] = Math.min(cur[j], prev2[j - 2] + 1);
      }
      [prev2, prev] = [prev, cur];
    }
    return prev[b.length];
  }
  const dayKey = (iso) => { const d = new Date(iso); return isNaN(d) ? null : [d.getFullYear(), d.getMonth() + 1, d.getDate()]; };
  function queryDates(q) {
    const out = [];
    const rest = q.replace(/\b(\d{4})-(\d{1,2})-(\d{1,2})\b/g, (_, y, m, d) => { out.push([+y, +m, +d]); return ' '; })
      .replace(/\b(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?\b/g, (_, m, d, y) => { out.push([y ? (+y < 100 ? 2000 + +y : +y) : null, +m, +d]); return ' '; })
      .replace(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b/gi, (_, mo, d) => { out.push([null, MONTHS.indexOf(mo.toLowerCase().slice(0, 3)) + 1, +d]); return ' '; });
    return [out, rest];
  }
  function found(j, q) {
    const [dates, rest] = queryDates(q);
    const days = [j.created_at, j.finished_at, j.archived_at, j.requeued_at].map(dayKey).filter(Boolean);
    if (!dates.every(([y, m, d]) => days.some(([yy, mm, dd]) => mm === m && dd === d && (y == null || yy === y)))) return false;
    const ws = normText(rest).split(' ').filter(Boolean), tw = normText(`${j.company} ${j.role} ${LABELS[j.status] || j.status} ${j.status}`).split(' ').filter(Boolean);
    const toks = tw.flatMap((w, k) => [w, w + (tw[k + 1] || ''), w + (tw[k + 1] || '') + (tw[k + 2] || '')]);
    return ws.every(w => toks.some(t => t.startsWith(w)) || (w.length >= 5 && tw.some(t => edits(w, t, w.length >= 9 ? 2 : 1) <= (w.length >= 9 ? 2 : 1))));
  }
  state.q = new URLSearchParams(location.search).get('q') || '';
  document.addEventListener('input', (e) => {
    if (!e.target.matches('.rg-search')) return;
    state.q = e.target.value.trim();
    const url = new URL(location.href);
    if (state.q) url.searchParams.set('q', state.q); else url.searchParams.delete('q');
    history.replaceState(history.state, '', url.pathname + url.search + url.hash);
    render();
  });
  function paintSearch(shown, total) {
    document.querySelectorAll('.rg-search').forEach(i => { if (i !== document.activeElement) i.value = state.q; });
    document.querySelectorAll('.rg-found').forEach(p => { p.textContent = state.q ? (shown ? `${shown} of ${total} match “${state.q}”` : `Nothing matches “${state.q}”. Searched company, title, date and status.`) : ''; });
  }
  const typingNote = () => { const a = document.activeElement; return a && ['TEXTAREA', 'INPUT'].includes(a.tagName) && a.closest('form[data-fix], form[data-place]'); };
  function render() {
    if (typingNote()) { state.dirty = true; return; }          // never pull the note box out from under the cursor
    state.dirty = false;
    state.day = dayOf(Date.now());                            // Today / Yesterday labels move at midnight (tick below)
    const mine = (j) => j.profile === state.profile;          // only the chosen person, everywhere
    const mineJobs = state.jobs.filter(mine), mineArchived = state.archived.filter(mine);
    const all = mineJobs.length + mineArchived.length;
    const hit = (j) => !state.q || found(j, state.q);
    const jobs = mineJobs.filter(hit), archived = mineArchived.filter(hit);
    paintSearch(jobs.length + archived.length, all);
    const queueRow = (j, n, day) => j.status === 'needs_you' ? needsCard(j, n, day) : j.status === 'needs_place' ? placeCard(j, n, day) : jobRow(j, n, day);
    const ended = jobs.filter(ENDED_EMPTY), live = jobs.filter(IN_QUEUE), ready = jobs.filter(j => j.status === 'ready');
    const endedBlock = ended.length ? `<details class="ended"><summary>Cancelled &amp; failed (${ended.length}) <button data-act="clear" class="ghost" title="Hide them from the Queue; files are kept">Clear cancelled &amp; failed</button></summary>${byDay(ended, jobRow)}</details>` : '';
    $('#queue').innerHTML = (live.length ? byDay(live, queueRow) : '<p class="muted">Nothing running and nothing waiting for you.</p>') + endedBlock;
    const approved = ready.filter(j => j.drive_link).length, toApprove = ready.length - approved;
    // the side badge counts the ones to approve; say both numbers here so 52 vs 53 never looks like a mismatch
    $('#ready').innerHTML = (approved ? `<div class="toolbar"><span class="muted">${toApprove} to approve · ${approved} approved</span><button data-act="archive-approved" title="Hide every approved resume from Ready (files and Drive copies stay)">Archive all approved (${approved})</button></div>` : '') + (ready.length ? byDay(ready, jobRow) : '<p class="muted">Nothing here.</p>');
    $('#archived').innerHTML = archived.length ? `<div class="toolbar"><button data-act="clear-archived" class="danger" title="Delete every archived resume of this person for good (asks first; Undo for 8 s); approved PDFs in Drive stay">Clear archived (${archived.length})</button></div>` + byDay(archived, archivedRow) : '<p class="muted">Nothing archived.</p>';
    // the Queue number counts what is being made or waiting its turn; needs-you is its own badge
    const liveAll = mineJobs.filter(IN_QUEUE), readyAll = mineJobs.filter(j => j.status === 'ready');   // badges: every one, searched or not
    const making = liveAll.filter(j => ACTIVE.has(j.status)).length, needs = liveAll.length - making;
    const toApproveAll = readyAll.length - readyAll.filter(j => j.drive_link).length;
    $('#count-queue').textContent = making || '';
    $('#count-queue').title = `${making} being made or waiting`;
    $('#count-needs').textContent = needs ? `${needs} need${needs === 1 ? 's' : ''} you` : '';
    $('#count-needs').title = needs ? `${needs} resume${needs === 1 ? '' : 's'} waiting for you (Accept as it is, Fix with note, or pick the location)` : '';
    $('#count-ready').textContent = toApproveAll || '';
    $('#count-ready').title = `${toApproveAll} not approved yet · ${readyAll.length} in Ready`;   // the badge vs the day counts
    $('#count-archived').textContent = mineArchived.length || '';
    const setupNote = state.setup && !state.setup.claude_ready && state.profiles.length
      ? '<div class="banner warn">Claude is not set up yet, so new resumes wait in the Queue. <a href="#settings" data-goto="settings">Open Settings</a> to add an API key or turn on demo mode.</div>' : '';
    $('#banners').innerHTML = setupNote + state.profiles.filter(p => p.pause && p.id === state.profile).map(p => `<div class="banner">${esc(p.name)}: paused until ${new Date(p.pause.until).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} (${esc(p.pause.reason)})</div>`).join('');
    $('#first-run').hidden = state.profiles.length > 0;
    $('#new-form').hidden = state.profiles.length === 0;
    paintWorker();
    paintBusy();
  }
  // one small status in the top bar: is the resume worker on, is it making something
  function paintWorker() {
    const making = state.jobs.filter(j => j.profile === state.profile && ACTIVE.has(j.status)).length;
    const [cls, text, long] = !state.worker ? ['bad', 'Worker off', 'The resume worker is not running: new resumes wait in the Queue until it starts (start the app with python -m resume_tailor serve)']
      : making ? ['warn', `Making ${making}`, `The worker is making or checking ${plural(making, 'resume')}`] : ['', 'Worker on', 'The resume worker is on and idle'];
    $('#worker-status .dot').className = 'dot ' + cls;
    $('#worker').textContent = text;
    $('#worker-status').title = long;
    $('#worker-status').setAttribute('aria-label', 'Status: ' + long);
  }
  const ACTIONS = {
    cancel: async (id) => api(`/api/jobs/${id}/cancel`, {}),
    place: async (id, b) => { const r = await api(`/api/jobs/${id}/place`, { location: b.dataset.place }); if (r.ok) toast(`Queued with ${r.location || 'your own city'}`); return r; },
    placeother: async (id) => { const f = document.querySelector(`form[data-place="${id}"]`); if (f) { f.hidden = false; f.querySelector('input').focus(); } return null; },
    undo: async (id) => {
      const r = await api(`/api/jobs/${id}/undo_edit`, {}); if (r.ok) toast(`Back to the version before your ${r.kind === 'fix' ? 'note' : 'Doc edits'}` + (r.left ? ` (${r.left} more to undo)` : '')); return r; },
    drop: async (id) => {
      const r = await api(`/api/jobs/${id}/cancel`, {});
      if (r.ok) { undoToast(`Cancelled ${nameOf(id)}: moved to Archived`, [Number(id)]); if (String(state.open) === String(id)) closePanel(); }
      return r;
    },
    accept: async (id) => { const r = await api(`/api/jobs/${id}/accept`, {}); if (r.ok) { toast('Accepted: moved to Ready'); if (String(state.open) === String(id)) closePanel(); } return r; },
    fixopen: async (id) => { state.fixOpen.add(String(id)); document.querySelectorAll(`form[data-fix="${id}"]`).forEach(f => { f.hidden = false; }); const t = document.querySelector(`#panel:not([hidden]) form[data-fix="${id}"] textarea`) || document.querySelector(`form[data-fix="${id}"] textarea`); if (t) t.focus(); return null; },
    usedit: async (id, b) => {
      const note = `I've used ${b.dataset.skill}: add it to the right Skills row.`;
      state.drafts[id] = note;
      document.querySelectorAll(`form[data-fix="${id}"] textarea`).forEach(t => { t.value = note; });
      return ACTIONS.fixopen(id);
    },
    fixcancel: async (id) => { state.fixOpen.delete(String(id)); delete state.drafts[id]; document.querySelectorAll(`form[data-fix="${id}"]`).forEach(f => { f.hidden = true; }); return null; },
    open: async (id) => { openJob(id); return null; },
    archive: async (id) => { const r = await api(`/api/jobs/${id}/archive`, {}); if (r.ok) undoToast(`Archived ${nameOf(id)}`, [Number(id)]); return r; },
    restore: async (id) => { const r = await api('/api/unarchive', { ids: [Number(id)] }); if (r.ok) toast(`Restored ${nameOf(id)}`); return r; },
    delete: async (id) => { const name = nameOf(id), r = await api(`/api/jobs/${id}/delete`, {}); if (r.ok) { undeleteToast(`Deleted ${name} for good`, r.deleting); if (String(state.open) === String(id)) closePanel(); } return r; },
    'clear-archived': async () => {
      const n = state.archived.length;
      if (!n || !confirm(`Delete all ${n} archived resume${n === 1 ? '' : 's'} for good? Their files and Google Doc copies go; approved PDFs in Drive stay. Undo works for 8 seconds.`)) return null;
      const r = await api('/api/archived/clear', { profile: state.profile });
      if (r.ok) undeleteToast(`Deleted ${r.deleting.length} archived for good`, r.deleting);
      return r;
    },
    'regen-current': async (id) => { const r = await api(`/api/jobs/${id}/regenerate`, { current: true }); if (r.ok) toast('Queued: a new resume from your current one'); return r; },
    clear: async () => { const r = await api('/api/archive', { what: 'cancelled_failed', profile: state.profile }); if (r.ok) undoToast(`Cleared ${r.archived.length} cancelled / failed`, r.archived); return r; },
    'archive-approved': async () => { const r = await api('/api/archive', { what: 'approved', profile: state.profile }); if (r.ok) undoToast(`Archived ${r.archived.length} approved`, r.archived); return r; },
    retry: async (id) => { const note = prompt('Instruction for this run (optional), e.g. "make it more MLOps-heavy"'); if (note === null) return; return api(`/api/jobs/${id}/retry`, { note }); },
    regen: async (id) => { const note = prompt('Start over: the whole resume is made again from the base resume, with this note:'); if (note === null) return; return api(`/api/jobs/${id}/regenerate`, { note }); },
    approve: async (id) => { const r = await api(`/api/jobs/${id}/approve`, {}); if (r.ok) toast((r.updated ? 'Drive file updated with the edited version: ' : 'Uploaded to Drive: ') + r.drive.name); return r; },   // an error: one toast, below
    gdoc: async (id) => { const r = await api(`/api/jobs/${id}/gdoc`, {}); if (r.ok) window.open(r.link, '_blank'); return r; },
    reimport: async (id) => { toast('Getting your edits from the Google Doc…'); const r = await api(`/api/jobs/${id}/reimport`, {}); if (r.ok) toast(reimportSaid(r)); return r; },
  };
  function reimportSaid(r) {
    const x = (r.job && r.job.result && r.job.result.reimport) || {}, old = (x.old_problems || []).length, fresh = (x.new_problems || []).length;
    if (r.status === 'ready') return old ? `Your Doc edits are in: it stays in Ready (${plural(old, 'older problem')}, none from your edit)` : 'Your Doc edits are in and pass every check';
    return `Your Doc edits are in${x.moved ? ', moved to Queue · needs you' : ''}: ${plural(fresh, 'new problem')} from your edit`;
  }
  document.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button[data-act]');
    if (b) {
      ev.stopPropagation(); ev.preventDefault();
      const act = b.dataset.act, id = b.dataset.id;
      if (id !== undefined && holds(state.busy.get(String(id)), act)) return;
      if (ARMED[act]) {
        if (state.armed !== `${act}:${id}`) { arm(act, id); return; }
        disarm();
      }
      if (BUSY[act]) { await runBusy(act, id, () => ACTIONS[act](id)); return; }
      b.disabled = true;
      const r = await ACTIONS[b.dataset.act](b.dataset.id, b);
      b.disabled = false;
      if (r === null) return;                                   // local UI only (open / close the note box)
      if (r && !r.ok) toast(r.error);
      await refresh();
      if (state.open) openJob(state.open);
      return;
    }
    const row = ev.target.closest('[data-open]');
    if (row) openJob(row.dataset.open);
  });

  document.addEventListener('focusout', () => { if (state.dirty) setTimeout(() => { if (state.dirty && !typingNote()) render(); }, BLUR_RENDER_MS); });
  document.addEventListener('input', (ev) => { const f = ev.target.closest('form[data-fix]'); if (f) state.drafts[f.dataset.fix] = ev.target.value; });
  document.addEventListener('submit', async (ev) => {
    const pf = ev.target.closest('form[data-place]');
    if (pf) {                                                  // Other… (Enter saves)
      ev.preventDefault();
      const r = await api(`/api/jobs/${pf.dataset.place}/place`, { location: pf.elements.place.value });
      if (!r.ok) { toast(r.error); return; }
      toast(`Queued with ${r.location || 'your own city'}`);
      pf.elements.place.blur();
      await refresh();
      return;
    }
    const f = ev.target.closest('form[data-fix]'); if (!f) return;
    ev.preventDefault();
    const id = f.dataset.fix, note = f.querySelector('textarea').value.trim();
    if (!note) return toast('Write what should change first');
    if (state.busy.has(String(id))) return;
    await runBusy('fix', id, async () => {
      const r = await api(`/api/jobs/${id}/fix`, { note });
      if (!r.ok) return r;
      state.fixOpen.delete(String(id)); delete state.drafts[id];
      if (document.activeElement) document.activeElement.blur();    // let the list re-render now
      toast('Fixing: only what your note names');
      return r;
    });
  });

  // ---------- panel ----------
  function diffHtml(pair) {
    if (!pair) return '';
    if (!pair.changed) return `<div class="pair same">${esc(pair.new)}</div>`;
    const html = pair.ops.map(([tag, a, b]) => tag === 'equal' ? esc(a) : (a ? `<del>${esc(a)}</del> ` : '') + (b ? `<ins>${esc(b)}</ins>` : '')).join(' ');
    return `<div class="pair"><div class="base small">${esc(pair.base)}</div><div>${html}</div></div>`;
  }
  function panelButtons(j, id, files) {
    const restore = j.archived_at ? `<div class="actions"><button class="primary" data-act="restore" data-id="${id}">Restore (archived ${esc(stamp(j.archived_at))})</button></div>` : '';
    const review = !j.archived_at && (j.status === 'needs_you' || j.status === 'ready') ? reviewActions(j, true) : '';
    return restore + editBlock(j) + review + fileButtons(j, id, files);
  }
  function fileButtons(j, id, files) {
    const out = [];
    if (j.status === 'ready' && !j.archived_at && (!j.drive_link || j.approved_stale)) out.push(`<button class="primary" data-act="approve" data-id="${id}" title="key a">${j.approved_stale ? 'Approve again → Drive (edited version)' : 'Approve → Drive'}</button>`);
    if (files.pdf) out.push(`<a href="${esc(fileUrl(j, 'pdf', '&dl=1'))}" download>Download PDF</a>`);
    if (files.docx) out.push(`<a href="${esc(fileUrl(j, 'docx', '&dl=1'))}" download>Download .docx</a>`);
    // one control for the Doc (it was "Open as Google Doc" next to "Doc ↗"): a Doc made already opens as itself
    // (the gdoc action makes a new Doc, which would drop the edits in the first)
    if (j.gdoc_link) out.push(`<a href="${esc(j.gdoc_link)}" target="_blank" rel="noopener" title="Your Google Doc: edit it, then press Get my Doc edits">Open my Google Doc ↗</a>`);
    else if (files.docx) out.push(`<button data-act="gdoc" data-id="${id}" title="key o">Open as Google Doc</button>`);
    if (j.gdoc_link && !j.archived_at) out.push(`<button data-act="reimport" data-id="${id}" title="Bring your edits from the Google Doc into this resume (key r)">Get my Doc edits</button>`);
    out.push(`<button data-act="regen" data-id="${id}" title="Make the whole resume again from the base resume, with your note (uses Claude). To change one thing, use Fix with note">Start over with a note</button>`);
    if (j.drive_link) out.push(`<a href="${esc(j.drive_link)}" target="_blank" rel="noopener">Drive: ${esc(j.drive_name)} ↗</a>`);
    return `<div class="actions">${out.join('')}</div>`;
  }
  // JD skills the person lacks are listed, never added; "I've used it" starts a note that confirms one

  function gapsBlock(res, id) {
    const gaps = res.skill_gaps || [];
    if (!gaps.length) return '';
    return `<div class="gaps"><b>Gaps</b> <span class="small muted">JD skills not in your resume or known skills; never added</span><div>${gaps.map(g => `<span class="chip gap">${esc(g)} <button data-act="usedit" data-id="${id}" data-skill="${esc(g)}" class="ghost small" title="Start a note: you have used ${esc(g)}; it goes into Skills and your confirmed skills">I've used it</button></span>`).join(' ')}</div></div>`;
  }
  function hmBlock(res) {
    const h = res.hm_review;
    if (!h) return '';
    const yes = (v, a, b) => v ? `✓ ${a}` : `✗ ${b}`;
    return `<details class="hm"><summary>Hiring manager: ${esc(String(h.score ?? '?'))}/10 · ${yes(h.reads_human, 'reads human', 'reads machine-made')} · ${yes(h.clear_fit, 'clear fit', 'fit not clear')}</summary><ul>${(h.notes || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul><p class="small muted">A report only: it changed nothing.</p></details>`;
  }
  // with rules.toml spare_swaps / skills_add_first: each spare bullet that went in (read from the page by Python, with
  // Claude's reason) and the skills added or removed per row; no approval step, the tags only say what changed
  function tailorBlock(res) {
    const t = res.tailor;
    if (!t) return '';
    const swaps = (t.swaps || []).map(s => `<li><span class="chip swap">⇄ Spare ${esc(s.spare_id)} in</span> ${esc(s.job)} bullet ${esc(String(s.in_at).split('.')[1] || '')}${s.out_bullet ? ` in place of base bullet ${esc(String(s.out_bullet).split('.')[1])}` : ''}${s.reason ? `: ${esc(s.reason)}` : ''}<div class="small muted">${esc(s.tags || '')}</div></li>`);
    const rows = (obj, cls, glyph, word) => Object.entries(obj || {}).map(([row, items]) => `<li><span class="chip ${cls}">${glyph} ${word}</span> ${esc(items.join(', '))} <span class="small muted">(${esc(row)})</span></li>`);
    const items = [...swaps, ...rows(t.skills_added, 'added', '＋', 'Added'), ...rows(t.skills_removed, 'removed', '✕', 'Removed')];
    const missing = (t.missing_jd || []).length ? `<p class="small muted">JD skills you have that are not on the page: ${esc(t.missing_jd.join(', '))}</p>` : '';
    return `<details class="tailor" open><summary>Changes vs your base resume${(t.swaps || []).length ? ` · ${t.swaps.length} spare bullet${t.swaps.length === 1 ? '' : 's'}` : ''}</summary>${items.length ? `<ul class="changes">${items.join('')}</ul>` : '<p class="small muted">No spare bullets and no skills added or removed.</p>'}${missing}</details>`;
  }
  function panelDetails(res, id) {
    const ats = res.ats ? `<div class="score"><span>ATS</span><b>${res.ats.base_pct}%</b><span>base →</span><b>${res.ats.tailored_pct}%</b><span class="muted small">${esc(res.ats.vendor || '')}</span></div>` : '';
    const added = (res.added_from_jd || []).length ? `<p><b>Added from JD:</b> ${res.added_from_jd.map(esc).join(', ')}</p>` : '';
    const gaps = gapsBlock(res, id), hm = hmBlock(res);
    const review = (res.review || []).length ? `<details><summary>Hiring manager / ATS review</summary><ul>${res.review.map(x => `<li>${esc(x)}</li>`).join('')}</ul>${res.match ? `<p class="small">${esc(res.match)}</p>` : ''}${(res.gaps || []).length ? `<p class="small"><b>Gaps:</b> ${res.gaps.map(esc).join('; ')}</p>` : ''}</details>` : '';
    const notes = (res.change_notes || []).length ? `<details><summary>Change notes</summary><ul>${res.change_notes.map(x => `<li>${esc(x)}</li>`).join('')}</ul></details>` : '';
    const attempts = (res.attempts || []).map(a => `<li>${a.kind && a.kind !== 'full' ? a.kind + ' ' : 'attempt '}${a.n}: ${a.duration_s}s, ${esc(a.model || '')}${a.cost_usd != null ? ', $' + Number(a.cost_usd).toFixed(2) : ''}${a.usage && a.usage.output_tokens ? ', ' + a.usage.output_tokens + ' out tokens' : ''}${a.failures.length ? ': ' + esc(a.failures.join('; ').slice(0, 300)) : ', passed'}</li>`).join('');
    const diff = res.diff ? diffBody(res.diff) : '';
    return `${ats}${added}${gaps}${tailorBlock(res)}${hm}${review}${notes}<details><summary>Attempts (${(res.attempts || []).length})</summary><ul>${attempts}</ul></details>${diff}`;
  }
  function diffBody(d) {
    return `<div class="diff"><h4>Headline</h4>${diffHtml(d.headline)}<h4>Summary</h4>${diffHtml(d.summary)}<h4>Skills</h4>${d.skills.map(s => `<div class="pair"><b>${esc(s.category)}:</b> ${diffHtml(s.items).replace(/^<div class="pair[^>]*>|<\/div>$/g, '')}</div>`).join('')}${d.jobs.map(job => `<h4>${esc(job.company)} · ${esc(job.title)} · ${esc(job.dates)}</h4>${job.bullets.map(diffHtml).join('')}`).join('')}</div>`;
  }
  let openSeq = 0;
  async function openJob(id) {
    const seq = ++openSeq;
    const r = await api(`/api/jobs/${id}`); if (!r.ok) return toast(r.error);
    if (seq !== openSeq || (state.open !== null && String(state.open) !== String(id))) return;
    const j = r.job, res = j.result || {}, files = res.files || {};
    state.open = id;
    $('#panel-title').textContent = `${j.company} · ${j.role}`;
    $('#panel-sub').textContent = [timeLine(j), j.profile, LABELS[j.status] || j.status, res.base ? `base ${res.base.label} (${res.base.why || ''})` : '', j.model || '', res.engine ? 'PDF via ' + res.engine : ''].filter(Boolean).join(' · ');
    const view = files.pdf ? (state.phone ? `<img class="pageimg" src="${esc(fileUrl(j, 'png'))}" alt="page 1">` : `<iframe class="pdf" src="${esc(fileUrl(j, 'pdf'))}#${PDF_VIEW}" title="PDF"></iframe>`) : '<p class="muted">No PDF yet.</p>';
    const fails = (res.checks && res.checks.failures && res.checks.failures.length) ? `<p class="warn">Last attempt failed ${res.checks.failures.length} check(s):</p><ul class="fails">${res.checks.failures.map(f => `<li>check ${f.check}: ${esc(f.message)}${(reimportOf(j).old_problems || []).includes(f.message) ? ' <span class="muted">(already there before your edit)</span>' : ''}</li>`).join('')}</ul>` : '';
    $('#panel-body').innerHTML = `${staleBlock(j)}${panelButtons(j, id, files)}${fails}<div class="split"><div>${view}</div><div>${panelDetails(res, id)}</div></div>`;
    $('#panel').hidden = false;
    paintBusy();
  }
  function closePanel() { $('#panel').hidden = true; state.open = null; openSeq++; }
  $('#panel-close').addEventListener('click', closePanel);

  // ---------- Cancel asks twice; keyboard keys (listed under "?") ----------
  function paintArmed() {
    Object.keys(ARMED).forEach(act => document.querySelectorAll(`button[data-act="${act}"]:not([aria-busy])`).forEach(b => { const on = state.armed === `${act}:${b.dataset.id}`; b.classList.toggle('armed', on); b.textContent = ARMED[act][on ? 1 : 0]; }));
  }
  function arm(act, id) { state.armed = `${act}:${id}`; clearTimeout(state.armTimer); state.armTimer = setTimeout(disarm, ARM_MS); paintArmed(); toast(ARMED[act][2]); }
  function disarm() { state.armed = null; clearTimeout(state.armTimer); paintArmed(); }
  const KEYS = [['g', 'Accept as it is (a needs-you resume)', 'accept'], ['f', 'Fix with note', 'fixopen'], ['o', 'Open as Google Doc', 'gdoc'],
    ['r', 'Get my Doc edits (from the Google Doc)', 'reimport'], ['u', 'Undo my edit (press twice)', 'undo'], ['x', 'Cancel this resume (press twice)', 'drop'],
    ['a', 'Approve → Drive', 'approve'], ['Esc', 'Close the panel or this list'], ['?', 'Show or hide these keys']];
  $('#help').innerHTML = `<b>Keyboard shortcuts</b> <span class="small muted">(with a resume open)</span><dl>${KEYS.map(([k, what]) => `<dt><kbd>${esc(k)}</kbd></dt><dd>${esc(what)}</dd>`).join('')}</dl>`;
  const toggleHelp = (show) => { const h = $('#help'); h.hidden = show === undefined ? !h.hidden : !show; $('#keys').setAttribute('aria-expanded', String(!h.hidden)); };
  $('#keys').addEventListener('click', () => toggleHelp());
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { if (!$('#help').hidden) toggleHelp(false); else closePanel(); return; }
    const t = ev.target;
    if (ev.metaKey || ev.ctrlKey || ev.altKey || (t && (['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) || t.isContentEditable))) return;
    if (ev.key === '?') { ev.preventDefault(); toggleHelp(); return; }
    const key = KEYS.find(x => x[2] && x[0] === ev.key.toLowerCase());
    if (!key || state.open === null) return;
    const b = document.querySelector(`#panel button[data-act="${key[2]}"][data-id="${state.open}"]`);
    if (b && !b.disabled) { ev.preventDefault(); b.click(); }
  });

  // ---------- busy buttons: pressed, the button stays busy until its result is on screen ----------
  // [label, long]: a long one runs in the worker (Claude), so it ends when the job has finished and its new files show
  const BUSY = { reimport: ['Getting your Doc edits…', false], undo: ['Undoing…', false], drop: ['Cancelling…', false],
    cancel: ['Cancelling…', false], accept: ['Saving…', false], approve: ['Uploading to Drive…', false],
    regen: ['Starting over…', true], 'regen-current': ['Making it again…', true], fix: ['Fixing with your note…', true] };
  const WHILE_CLAUDE = new Set(['cancel', 'open']);  // still possible while the worker runs a note / regenerate
  const holds = (e, act) => !!e && !(e.long && e.sent && WHILE_CLAUDE.has(act));
  state.busy = new Map();
  const seen = (ev) => { if (ev.target && ev.target.dataset) ev.target.dataset.loaded = '1'; };   // a broken image counts as shown
  document.addEventListener('load', seen, true); document.addEventListener('error', seen, true);
  const findJob = (id) => state.jobs.concat(state.archived).find(j => String(j.id) === String(id));
  const versionOf = (id) => { const j = findJob(id); return j ? `${j.files_version || ''}|${j.updated_at || ''}|${j.status}` : ''; };
  const sleep = (ms) => new Promise(done => setTimeout(done, ms));
  const busyKey = (b) => { const f = b.closest('form[data-fix]'); return f && b.type === 'submit' ? ['fix', f.dataset.fix] : [b.dataset.act, b.dataset.id]; };
  // (user 22:21): the time so far, and a Re-import's step ("reading your Doc", "building the PDF", "checking")
  const busyText = (e) => `${e.label}${e.step ? ' ' + e.step : ''} · ${Math.round((Date.now() - e.started) / 1000)} s`;
  const busyHtml = (e) => `<span class="spin" aria-hidden="true"></span>${esc(busyText(e))}`;
  function paintButton(b) {
    const [act, id] = busyKey(b), e = id === undefined ? null : state.busy.get(String(id)), mine = !!e && e.act === act;
    if (mine && !b.dataset.idle) {
      b.dataset.idle = b.innerHTML; b.innerHTML = busyHtml(e);
      b.classList.add('busy'); b.setAttribute('aria-busy', 'true'); b.disabled = true;
    } else if (mine && b.innerHTML !== busyHtml(e)) {
      b.innerHTML = busyHtml(e);
    } else if (!mine && b.dataset.idle) {
      b.innerHTML = b.dataset.idle; delete b.dataset.idle; b.classList.remove('busy'); b.removeAttribute('aria-busy'); b.disabled = false;
    }
    const held = !mine && holds(e, act);                                                // one thing at a time per resume
    if (held && !b.dataset.held) { b.dataset.held = '1'; b.disabled = true; }
    else if (!held && b.dataset.held) { delete b.dataset.held; b.disabled = false; }
  }
  function paintBusy() {
    document.querySelectorAll('button[data-act], form[data-fix] button[type=submit]').forEach(paintButton);
    document.querySelectorAll('.busyline').forEach(el => {
      const e = state.busy.get(el.dataset.busy);
      if (!e) el.remove(); else if (el.innerHTML !== busyHtml(e)) el.innerHTML = busyHtml(e);
    });
    state.busy.forEach((e, id) => {                 // the button is gone (e.g. while Claude works): say it on the card / panel
      const boxes = [...document.querySelectorAll(`#queue li[data-job="${id}"], #ready li[data-job="${id}"], #archived li[data-job="${id}"]`)];
      if (String(state.open) === id && !$('#panel').hidden) boxes.push($('#panel-body'));
      boxes.forEach(box => {
        if (box.querySelector('.busyline') || [...box.querySelectorAll('[aria-busy]')].some(shown)) return;
        box.insertAdjacentHTML(box.id === 'panel-body' ? 'afterbegin' : 'beforeend', `<div class="busyline" role="status" data-busy="${esc(id)}">${busyHtml(e)}</div>`);
      });
    });
  }
  const shown = (el) => el.getClientRects().length > 0;
  const onScreen = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.bottom > 0 && r.top < innerHeight; };
  const isLoaded = (el) => !!el.dataset.loaded || (el.tagName === 'IMG' && el.complete && el.naturalWidth > 0);
  async function whenShown(id) {                    // the views on screen NOW (the panel may be redrawn meanwhile)
    const j = findJob(id), v = j ? `v=${encodeURIComponent(j.files_version || j.updated_at || '')}` : '';
    const hasFile = !!(j && j.result && j.result.files && j.result.files.pdf);
    const inPanel = () => String(state.open) === String(id) && !$('#panel').hidden;
    const panelViews = () => inPanel() ? [...document.querySelectorAll('#panel iframe.pdf, #panel img.pageimg')] : [];
    const views = () => [...document.querySelectorAll(`li[data-job="${id}"] img.thumb`)].filter(onScreen).concat(panelViews());
    const current = () => !hasFile || !inPanel() || panelViews().some(el => (el.getAttribute('src') || '').includes(v));
    for (const deadline = Date.now() + BUSY_VIEW_MS; Date.now() < deadline; await sleep(BUSY_POLL_MS)) {
      if (current() && views().every(isLoaded)) return;
    }
  }
  const placeOf = (j) => !j ? null : j.archived_at ? 'Archived' : j.status === 'ready' ? 'Ready' : j.status === 'needs_you' ? 'Queue · needs you' : IN_QUEUE(j) ? 'Queue' : null;
  const SAYS_WHERE = new Set(['drop', 'accept', 'cancel', 'reimport']);         // these toasts already say it
  async function finishBusy(id) {
    const e = state.busy.get(String(id)); if (!e || e.finishing) return;
    e.finishing = true;
    try {
      if (String(state.open) === String(id)) await openJob(id);
      await whenShown(id);
      const now = placeOf(findJob(id));
      if (now && e.place && now !== e.place && !SAYS_WHERE.has(e.act)) toast(`${nameOf(id)} moved from ${e.place} to ${now}`);
    } finally { state.busy.delete(String(id)); paintBusy(); }
  }
  function settleLong() {                           // after each refresh: has the worker finished the note / regenerate?
    state.busy.forEach((e, id) => {
      if (!e.long || !e.sent || e.finishing) return;
      const j = findJob(id), late = Date.now() - e.started >= BUSY_MAX_MS;
      if (j && ACTIVE.has(j.status)) { e.sawActive = true; if (!late) return; }
      if (!j || e.sawActive || versionOf(id) !== e.from || late) finishBusy(id);
    });
  }
  async function runBusy(act, id, send) {
    state.busy.set(String(id), { act, label: BUSY[act][0], long: BUSY[act][1], started: Date.now(), from: versionOf(id), place: placeOf(findJob(id)) });
    paintBusy();
    const steps = act === 'reimport' ? setInterval(async () => {          // what the Re-import is doing now
      const v = await api(`/api/jobs/${id}`), e = state.busy.get(String(id));
      if (e && v.ok && v.job && v.job.step) { e.step = v.job.step.step; paintBusy(); }
    }, STEP_POLL_MS) : 0;
    let r;
    try { r = await send(); } catch (err) { r = { ok: false, error: err.message }; } finally { clearInterval(steps); }
    const e = state.busy.get(String(id));
    if (e) e.step = null;
    if (!r || !r.ok) {                              // nothing sent (a prompt dismissed) or refused: back to normal now
      state.busy.delete(String(id)); paintBusy();
      if (r && !r.ok) toast(r.error);
      return r;
    }
    e.sent = true;
    await refresh();
    if (!e.long) await finishBusy(id);
    return r;
  }

  // ---------- live ----------
  function toast(text, action = null, ms = TOAST_MS) {
    const el = document.createElement('div'); el.className = 'toast'; el.textContent = text;
    if (action) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'toast-act'; b.textContent = action.label;
      b.addEventListener('click', async () => { b.disabled = true; await action.run(); el.remove(); });
      el.append(' · ', b); el.classList.add('has-act');
    }
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), ms);
    return el;
  }
  // every archive (one or a batch) can be undone for a few seconds; the Archived tab keeps Restore after
  function undoToast(text, ids) {
    if (!ids || !ids.length) return toast(text);
    return toast(text, { label: 'Undo', run: async () => {
      const r = await api('/api/unarchive', { ids });
      toast(r.ok ? `Restored ${r.restored.length === 1 ? nameOf(r.restored[0]) : r.restored.length + ' resumes'}` : r.error);
      await refresh();
    } }, UNDO_MS);
  }
  // Delete for good / Clear archived wait 15 s on the server before anything is removed; Undo brings them back
  function undeleteToast(text, ids) {
    return toast(text, { label: 'Undo', run: async () => {
      const r = await api('/api/undelete', { ids });
      toast(r.ok ? `Brought back ${r.restored.length === 1 ? nameOf(r.restored[0]) : r.restored.length + ' resumes'}` : r.error);
      await refresh();
    } }, UNDO_MS);
  }
  function nameOf(id) {
    const j = state.jobs.concat(state.archived).find(x => String(x.id) === String(id));
    return j ? j.company : `#${id}`;
  }
  function notify(text) {
    toast(text);
    if ('Notification' in window && Notification.permission === 'granted' && document.hidden) new Notification('Resume Tailor', { body: text });
  }
  async function refresh() {
    const asked = state.profile || 'auto';                  // 'auto': the server answers for the visitor / default
    const r = await api('/api/state?profile=' + encodeURIComponent(asked));
    if (!r.ok) { $('#worker').textContent = 'Offline'; $('#worker-status .dot').className = 'dot bad'; $('#worker-status').title = r.error; return; }
    if (state.picked && asked !== state.profile) return;    // a late answer for the person switched away from
    state.profiles = r.profiles; state.jobs = r.jobs; state.archived = r.archived || []; state.worker = r.worker; state.settings = r.settings || {}; state.setup = r.setup || null;
    if (!state.lastEvent) state.lastEvent = r.last_event_id;
    if (!state.picked) {                                    // first answer: settle whose page this is
      state.picked = true;
      state.defaultProfile = r.settings && r.settings.default_profile;
      const pid = pickProfile(r);
      const url = new URL(location.href);
      if (url.searchParams.has('token')) {                  // the cookie holds it now; keep it out of copied links
        url.searchParams.delete('token');
        history.replaceState(null, '', url.pathname + url.search + url.hash);
      }
      state.profile = pid;
      if (pid) store.set(PERSON_KEY, pid);
      paintProfiles();
      if (pid !== r.profile) return refresh();              // the first answer was for someone else (or no one)
    }
    if (!r.profiles.length) paintProfiles();
    render();
    if (window.RT && RT.onState) RT.onState(r);
    settleLong();
  }
  function listen() {
    const es = new EventSource(`/api/events?since=${state.lastEvent}`);
    let timer = null;
    es.onmessage = (ev) => {
      const e = JSON.parse(ev.data); state.lastEvent = e.id;
      const theirs = e.profile && e.profile !== state.profile;  // the other person's news stays on their page
      if (e.kind === 'toast' && !theirs) notify(e.text);
      if ((e.kind === 'pause' || e.kind === 'resume') && !theirs) toast(e.text);
      clearTimeout(timer); timer = setTimeout(async () => { await refresh(); if (state.open && String(e.job_id) === String(state.open)) openJob(state.open); }, REFRESH_DEBOUNCE_MS);
    };
    es.onopen = () => { if (state.jobs.length) refresh(); };
    es.onerror = () => { es.close(); setTimeout(listen, SSE_RETRY_MS); };
  }
  const typing = () => { const a = document.activeElement; return a && (a.tagName === 'BUTTON' || a.tagName === 'TEXTAREA' || a.tagName === 'INPUT') && a.closest('#queue, #ready'); };
  setInterval(() => { if (state.busy.size) paintBusy(); }, BUSY_TICK_MS);   // the busy time ticks
  setInterval(() => { if (typing()) return; if (state.jobs.some(j => ACTIVE.has(j.status)) || (state.day && dayOf(Date.now()) !== state.day)) render(); }, TICK_MS);
  if ('Notification' in window && Notification.permission === 'default') document.addEventListener('click', () => Notification.requestPermission(), { once: true });
  document.addEventListener('click', (ev) => { const a = ev.target.closest('[data-goto]'); if (a) { ev.preventDefault(); showTab(a.dataset.goto); } });
  window.RT = { api, toast, refresh: () => refresh(), showTab, esc, state };
  showTab(location.hash === '#settings' ? 'settings' : (store.get('rg.tab') || 'new'));
  state.profile = new URLSearchParams(location.search).get('profile') || store.get(PERSON_KEY);   // checked on the first answer
  refresh().then(listen);
})();
