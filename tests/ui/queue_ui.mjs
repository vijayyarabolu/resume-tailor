// UI test: headless Chrome over the DevTools protocol (no packages). Prints {"checks": [{name, ok, detail}]}.
// usage: node queue_ui.mjs <base url> <token> <chrome path> <profile dir> <ids json>
import { spawn } from "node:child_process";

const [base, token, chromePath, profileDir, idsJson] = process.argv.slice(2);
const ids = JSON.parse(idsJson);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = 9400 + Math.floor(Math.random() * 500);
const chrome = spawn(chromePath, ["--headless=new", "--no-sandbox", `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`,
  "--no-first-run", "--window-size=1280,900", "about:blank"], { stdio: "ignore" });
const checks = [], errors = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) });
let ws;
try {
  let targets;
  for (let i = 0; i < 100 && !targets; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); } catch { await sleep(150); }
  }
  ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let seq = 0;
  const pending = new Map();
  const send = (method, params = {}) => new Promise((r) => { const i = ++seq; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || m.error); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  });
  const js = async (expr) => (await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true }))?.result?.value;
  const until = async (expr, ms = 8000) => { for (let t = 0; t < ms; t += 100) { if (await js(`return !!(${expr})`)) return true; await sleep(100); } return false; };
  const card = (id) => `document.querySelector('#queue [data-job="${id}"]')`;

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.navigate", { url: `${base}/?token=${token}` });
  await until(`document.querySelector('.tabs button[data-tab="queue"]')`);
  await js(`document.querySelector('.tabs button[data-tab="queue"]').click()`);
  await until(`document.querySelector('#queue .card')`);

  // 1. the Queue shows only live work + needs you; cancelled / failed only in the collapsed block
  const inQueue = await js(`return [...document.querySelectorAll('#queue li[data-job]')].filter(li => !li.closest('details.ended')).map(li => +li.dataset.job)`);
  const want = [ids.needs_a, ids.needs_b, ids.live].sort();
  check("queue shows live + needs-you only", JSON.stringify([...inQueue].sort()) === JSON.stringify(want), `got ${JSON.stringify(inQueue)} want ${JSON.stringify(want)}`);
  check("ready job is not in the Queue", !inQueue.includes(ids.ready) && !inQueue.includes(ids.approved));
  const ended = await js(`return [...document.querySelectorAll('#queue details.ended li[data-job]')].map(li => +li.dataset.job).sort()`);
  check("cancelled + failed sit in the collapsed block", JSON.stringify(ended) === JSON.stringify([ids.cancelled, ids.failed].sort()), JSON.stringify(ended));
  const endedWhen = await js(`return [...document.querySelectorAll('#queue details.ended li[data-job] .meta.when')].map(e => e.textContent)`);
  check("cancelled / failed rows say queued + ended, never made", endedWhen.length === 2 && endedWhen.every(t => /^Queued .+ · (cancelled|failed) /.test(t)), endedWhen.join(" | "));
  // the Queue number = being made or waiting; the needs-you ones have their own badge
  check("queue count = live only; needs you its own badge", (await js(`return document.querySelector('#count-queue').textContent + '|' + document.querySelector('#count-needs').textContent`)) === "1|2 need you");

  // 2. a needs-you card: page image, plain reasons, ATS, diff, two actions
  const info = await js(`const c = ${card(ids.needs_a)}; return { img: !!c.querySelector('img.thumb'), why: c.querySelector('.fails').textContent, ats: c.querySelector('.score')?.textContent || '', diff: !!c.querySelector('details .diff'), buttons: [...c.querySelectorAll('.actions button')].map(b => b.textContent) }`);
  check("needs-you card shows the page image", info.img);
  check("needs-you card states the reason in plain words", info.why.includes("Writing:") && info.why.includes("Bullet 5 at Lumen Freight has 26 words"), info.why);
  check("needs-you card shows ATS before → after", /ATS\s*70%\s*base →\s*75%/.test(info.ats), info.ats);
  check("needs-you card has the diff", info.diff);
  check("needs-you card has Looks good + Fix with note", info.buttons.includes("Accept as it is") && info.buttons.includes("Fix with note"), info.buttons.join("|"));

  // 3. Looks good -> ready, leaves the Queue, shows in Ready with "accepted"
  await js(`${card(ids.needs_a)}.querySelector('[data-act="accept"]').click()`);
  check("Looks good: the card leaves the Queue", await until(`!${card(ids.needs_a)}`));
  check("Looks good: it is in Ready, marked accepted", await until(`document.querySelector('#ready [data-job="${ids.needs_a}"] .chip.accepted')`));

  // 4. Fix with note -> note box -> queued with the note, no longer a needs-you card
  await js(`${card(ids.needs_b)}.querySelector('[data-act="fixopen"]').click()`);
  check("Fix with note opens the note box", await until(`${card(ids.needs_b)} && !${card(ids.needs_b)}.querySelector('form.fixform').hidden`));
  await js(`const t = ${card(ids.needs_b)}.querySelector('textarea'); t.focus(); t.value = 'only make the summary shorter'; t.dispatchEvent(new Event('input', { bubbles: true }))`);
  await sleep(1300);                                              // a 1 s tick must not wipe the note while typing
  check("the note survives a live re-render", (await js(`return ${card(ids.needs_b)}.querySelector('textarea').value`)) === "only make the summary shorter");
  await js(`${card(ids.needs_b)}.querySelector('form.fixform button[type=submit]').click()`);
  check("Fix with note: the card becomes a queued row", await until(`${card(ids.needs_b)} && ${card(ids.needs_b)}.querySelector('.chip.queued') && !${card(ids.needs_b)}.classList.contains('needs')`));
  const fixNote = await js(`return (await (await fetch('/api/jobs/${ids.needs_b}')).json()).job.fix_note`);
  check("Fix with note: the note reached the job", fixNote === "only make the summary shorter", fixNote);

  // 5. Clear cancelled & failed
  await js(`document.querySelector('#queue details.ended [data-act="clear"]').click()`);
  check("Clear cancelled & failed empties the block", await until(`!document.querySelector('#queue details.ended')`));
  const undoClick = `const b = [...document.querySelectorAll('#toasts .toast-act')].pop(); b && b.click(); return !!b`;
  check("Clear shows an Undo toast", await js(`return [...document.querySelectorAll('#toasts .toast')].some(t => /Cleared 2 cancelled/.test(t.textContent) && t.querySelector('.toast-act')?.textContent === 'Undo')`));
  await js(undoClick);
  check("Undo brings the whole cleared batch back", await until(`document.querySelectorAll('#queue details.ended li[data-job]').length === 2`));
  await js(`document.querySelector('#queue details.ended [data-act="clear"]').click()`);
  await until(`!document.querySelector('#queue details.ended')`);

  // 6. when it was made, newest first, and the Outdated badge with its reason + Regenerate
  await js(`document.querySelector('.tabs button[data-tab="ready"]').click()`);
  await until(`document.querySelector('#ready [data-job="${ids.stale}"]')`);
  const when = await js(`return [...document.querySelectorAll('#ready li[data-job] .meta.when')].map(e => e.textContent)`);
  check("every Ready card says when it was made", when.length >= 3 && when.every(t => /^Made ([A-Z][a-z]{2} \d+, )?\d{1,2}:\d\d [AP]M/.test(t)), when.join(" | "));
  check("queued cards say when they were queued", /^Queued /.test(await js(`return document.querySelector('#queue [data-job="${ids.live}"] .meta.when')?.textContent || ''`)));
  const order = await js(`return [...document.querySelectorAll('#ready li[data-job]')].map(li => +li.dataset.job)`);
  check("Ready is newest first (the 2-day-old one last)", order[order.length - 1] === ids.stale, JSON.stringify(order));
  const stale = await js(`const c = document.querySelector('#ready [data-job="${ids.stale}"]'); return { badge: c.querySelector('.chip.outdated')?.textContent, why: c.querySelector('.stale ul')?.textContent, btn: c.querySelector('[data-act="regen-current"]')?.textContent }`);
  check("an old-base resume shows the Outdated badge", stale.badge === "Outdated: made from your old resume", stale.badge);
  check("the badge lists the reason", (stale.why || "").includes("made from your old resume"), stale.why);
  check("current resumes have no badge", await js(`return !document.querySelector('#ready [data-job="${ids.ready}"] .chip.outdated')`));
  await js(`document.querySelector('#ready [data-job="${ids.stale}"] [data-act="regen-current"]').click()`);
  check("Regenerate with current resume queues it again", await until(`document.querySelector('#queue [data-job="${ids.stale}"] .chip.queued')`));

  // 7. Ready: Archive all approved, then one Archive
  await js(`document.querySelector('.tabs button[data-tab="ready"]').click()`);
  check("Ready has Archive all approved", await until(`document.querySelector('#ready [data-act="archive-approved"]')`));
  await js(`document.querySelector('#ready [data-act="archive-approved"]').click()`);
  check("Archive all approved hides the approved one", await until(`!document.querySelector('#ready [data-job="${ids.approved}"]') && !document.querySelector('#ready [data-act="archive-approved"]')`));
  await js(`document.querySelector('#ready [data-job="${ids.ready}"] [data-act="archive"]').click()`);
  check("Archive hides one ready resume", await until(`!document.querySelector('#ready [data-job="${ids.ready}"]')`));
  check("Archive shows 'Archived <Company> · Undo'", await until(`[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.startsWith('Archived Co ready') && t.querySelector('.toast-act'))`));
  await js(undoClick);
  check("Undo puts the single archive back in Ready", await until(`document.querySelector('#ready [data-job="${ids.ready}"]')`));
  await js(`document.querySelector('#ready [data-job="${ids.ready}"] [data-act="archive"]').click()`);
  await until(`!document.querySelector('#ready [data-job="${ids.ready}"]')`);
  // Archived view: newest first, when it was archived, Restore returns it unchanged (approved + Drive link kept)
  await js(`document.querySelector('.tabs button[data-tab="archived"]').click()`);
  await until(`document.querySelector('#archived [data-job="${ids.approved}"]')`);
  const arch = await js(`return [...document.querySelectorAll('#archived li[data-job]')].map(li => ({ id: +li.dataset.job, when: li.querySelector('.meta.when').textContent }))`);
  check("Archived lists single + batch archives", arch.length === 4 && [ids.ready, ids.approved, ids.cancelled, ids.failed].every(id => arch.some(a => a.id === id)), JSON.stringify(arch.map(a => a.id)));
  check("Archived rows say when they were archived", arch.every(a => / · archived ([A-Z][a-z]{2} \d+, )?\d{1,2}:\d\d [AP]M$/.test(a.when)), arch.map(a => a.when).join(" | "));
  check("Archived count badge", (await js(`return document.querySelector('#count-archived').textContent`)) === "4");
  await js(`document.querySelector('#archived [data-job="${ids.approved}"] [data-act="restore"]').click()`);
  check("Restore takes it out of Archived", await until(`!document.querySelector('#archived [data-job="${ids.approved}"]')`));
  await js(`document.querySelector('.tabs button[data-tab="ready"]').click()`);
  check("Restored resume is back in Ready, still approved", await until(`document.querySelector('#ready [data-job="${ids.approved}"] .chip.approved')`));
  const back = await js(`return (await (await fetch('/api/jobs/${ids.approved}')).json()).job`);
  check("Restored resume keeps its Drive link and status", back.status === "ready" && back.drive_link === "https://drive.example/1" && back.archived_at === null, JSON.stringify({ s: back.status, d: back.drive_link, a: back.archived_at }));
  check("the accepted one is still in Ready", await js(`return !!document.querySelector('#ready [data-job="${ids.needs_a}"]')`));
  // 8. search: company, title, date or status; a typo one edit off still matches; the query is kept in the address
  const search = (q) => js(`const i = document.querySelector('#tab-ready .rg-search'); i.value = ${JSON.stringify(q)}; i.dispatchEvent(new Event('input', { bubbles: true }))`);
  const readyIds = `[...document.querySelectorAll('#ready li[data-job]')].map(li => +li.dataset.job).join(',')`;
  await search("co aproved");
  check("search finds by company, one typo off", await until(`${readyIds} === '${ids.approved}'`), await js(`return ${readyIds}`));
  check("search says how many match and keeps the query in the address", (await js(`return document.querySelector('#tab-ready .rg-found').textContent`)).startsWith("1 of ") && (await js(`return new URLSearchParams(location.search).get('q')`)) === "co aproved");
  check("the side badges count every resume, searched or not", (await js(`return document.querySelector('#count-archived').textContent`)) === "3");
  const d = new Date();
  await search(`${d.getMonth() + 1}/${d.getDate()} ready`);
  check("search finds by date and status", await until(`${readyIds}.split(',').length === 2`), await js(`return ${readyIds}`));
  await search("nothing like this");
  check("no match says what was searched", await until(`document.querySelector('#ready li[data-job]') === null && /^Nothing matches/.test(document.querySelector('#tab-ready .rg-found').textContent)`));
  await search("");
  check("an empty search shows everything and leaves the address", await until(`${readyIds}.split(',').length === 2 && !new URLSearchParams(location.search).has('q')`));
  check("no page errors", errors.length === 0, errors.join(" | "));
} catch (err) {
  check("driver ran", false, err.stack || err);
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(JSON.stringify({ checks }));
}
