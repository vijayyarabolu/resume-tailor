// UI test: headless Chrome over the DevTools protocol (no packages). Prints {"checks": [{name, ok, detail}]}.
// usage: node goal5_ui.mjs <base url> <token> <chrome path> <profile dir> <ids json> <expect json>
import { spawn } from "node:child_process";

const [base, token, chromePath, profileDir, idsJson, expectJson] = process.argv.slice(2);
const ids = JSON.parse(idsJson), want = JSON.parse(expectJson);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = 9400 + Math.floor(Math.random() * 500);
const chrome = spawn(chromePath, ["--headless=new", "--no-sandbox", `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`,
  "--no-first-run", "--window-size=1280,900", "about:blank"], { stdio: "ignore" });
const checks = [], errors = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) });
let ws;
try {
  let targets;
  for (let i = 0; i < 400 && !targets?.some((t) => t.type === "page"); i++) {   // a cold Chrome on CI can take 15 s+
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); } catch { /* not up yet */ }
    if (!targets?.some((t) => t.type === "page")) await sleep(150);
  }
  if (!targets?.some((t) => t.type === "page")) throw new Error(`Chrome did not open its debug port ${port} within 60 s`);
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
  const tab = (name) => js(`document.querySelector('.tabs button[data-tab="${name}"]').click()`);
  const card = (id) => `document.querySelector('#queue li[data-job="${id}"]')`;
  const labels = (id) => js(`return [...${card(id)}.querySelectorAll('.actions button')].map(b => b.textContent)`);
  const click = (sel) => js(`document.querySelector(${JSON.stringify(sel)}).click()`);
  const key = (k) => js(`document.dispatchEvent(new KeyboardEvent('keydown', { key: ${JSON.stringify(k)}, bubbles: true }))`);
  const toastUndo = () => js(`const b = [...document.querySelectorAll('#toasts .toast-act')].pop(); if (b) b.click(); return !!b`);

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.navigate", { url: `${base}/?token=${token}&profile=alex-rivera` });
  await until(`document.querySelector('#who button[aria-pressed="true"]')`);
  await tab("queue");
  await until(card(ids.v_note));

  // 1. after a Fix with note: what changed, the problems, and the five choices
  const edit = await js(`return ${card(ids.v_note)}.querySelector('.edit')?.textContent || ''`);
  check("the card says what the note changed", edit.includes("Your note:") && edit.includes(want.note) && edit.includes("Added Data Warehousing to Skills (Data Engineering) · Northwind Analytics bullet 3"), edit);
  check("…and what was saved for later resumes", edit.includes("Saved to your skills for later resumes: Data Warehousing"), edit);
  check("problems are listed, not repaired", (await js(`return ${card(ids.v_note)}.textContent`)).includes("Problems to look at") && (await js(`return ${card(ids.v_note)}.querySelector('.fails').textContent`)).includes("widow"));
  const acts = await labels(ids.v_note);
  check("choices: Looks good / Fix with note / Open as Google Doc / Undo my edit / Cancel", ["Accept as it is", "Fix with note", "Open as Google Doc", "Undo my edit", "Cancel resume"].every(l => acts.includes(l)), acts.join(" | "));
  const thumb = await js(`return ${card(ids.v_note)}.querySelector('img.thumb')?.getAttribute('src')`);
  check("the page image URL carries the version (no stale image)", thumb === `/api/jobs/${ids.v_note}/file/png?v=${encodeURIComponent(want.v_note_version)}`, thumb);
  check("the version-less job keeps no Undo button", !(await labels(ids.v_plain)).includes("Undo my edit"), (await labels(ids.v_plain)).join(" | "));

  // 2. Cancel asks twice, then Undo puts it back
  await click(`#queue li[data-job="${ids.v_plain}"] button[data-act="drop"]`);
  check("Cancel arms first ('Yes, cancel it')", (await js(`return ${card(ids.v_plain)}.querySelector('button[data-act="drop"]').textContent`)) === "Yes, cancel it");
  await click(`#queue li[data-job="${ids.v_plain}"] button[data-act="drop"]`);
  check("the second press drops it from the Queue", await until(`!${card(ids.v_plain)}`), "still listed");
  check("…into Archived, returning to the Queue", await until(`document.querySelector('#archived li[data-job="${ids.v_plain}"]')?.textContent.includes('returns to Queue · needs you')`));
  check("an Undo toast offers it back", await toastUndo());
  check("Undo restores it as needs you", await until(`${card(ids.v_plain)}?.querySelector('.chip.needs_you')`), "not back");

  // 3. Undo my edit: back to the version before the note (it was ready then)
  await click(`#queue li[data-job="${ids.v_note}"] button[data-act="undo"]`);
  check("Undo my edit asks first ('Yes, undo it')", (await js(`return ${card(ids.v_note)}.querySelector('button[data-act="undo"]').textContent`)) === "Yes, undo it");
  await click(`#queue li[data-job="${ids.v_note}"] button[data-act="undo"]`);
  check("Undo my edit takes it back to Ready", await until(`!${card(ids.v_note)} && document.querySelector('#ready li[data-job="${ids.v_note}"]')`));
  check("…with a toast saying so", await until(`[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('Back to the version before your note'))`));

  // 4. a re-imported edit in the panel: shown with the new file, and Approve sends it again
  await tab("ready");
  check("the row says Drive has the older version", (await js(`return document.querySelector('#ready li[data-job="${ids.v_doc}"]').textContent`)).includes("approved · older version"));
  await click(`#ready li[data-job="${ids.v_doc}"]`);
  await until(`!document.querySelector('#panel').hidden && document.querySelector('#panel .edit')`);
  const pe = await js(`return document.querySelector('#panel .edit').textContent`);
  check("panel: what the Doc edit changed + the formatting note", pe.includes("Your Google Doc edit") && pe.includes("Location on the top line: Phoenix, AZ") && pe.includes("Formatting edits"), pe);
  const frame = await js(`return document.querySelector('#panel iframe.pdf')?.getAttribute('src')`);
  check("panel PDF URL carries the version", frame === `/api/jobs/${ids.v_doc}/file/pdf?v=${encodeURIComponent(want.v_doc_version)}#toolbar=0&navpanes=0&view=FitH`, frame);
  check("Approve again → Drive (edited version)", (await js(`return document.querySelector('#panel button[data-act="approve"]')?.textContent`)) === "Approve again → Drive (edited version)");
  check("download links carry the version and ?dl=1", (await js(`return document.querySelector('#panel a[download]').getAttribute('href')`)).endsWith(`v=${encodeURIComponent(want.v_doc_version)}&dl=1`));

  // 5. keys: ? lists them, Esc hides, x twice cancels the open resume
  await key("?");
  const help = await js(`return document.querySelector('#help').hidden ? '' : document.querySelector('#help').textContent`);
  check("? shows the keys (Undo, Cancel, Fix, Looks good, Esc)", ["Undo my edit", "Cancel this resume", "Fix with note", "Accept as it is", "Esc"].every(t => help.includes(t)), help);
  await key("Escape");
  check("Esc hides the keys", await js(`return document.querySelector('#help').hidden`));
  await key("x");
  check("x arms Cancel in the panel", (await js(`return document.querySelector('#panel button[data-act="drop"]').textContent`)) === "Yes, cancel it");
  await key("x");
  check("x again cancels it (gone from Ready, panel closed)", await until(`!document.querySelector('#ready li[data-job="${ids.v_doc}"]') && document.querySelector('#panel').hidden`));
  check("…and Archived says it returns to Ready", await until(`document.querySelector('#archived li[data-job="${ids.v_doc}"]')?.textContent.includes('returns to Ready')`));

  // 6. Sam's page: her needs-you card has the same choices, none of Alex's rows
  await click('#who button[data-person="sam-lee"]');
  await tab("queue");
  await until(card(ids.s_note));
  const sActs = await labels(ids.s_note);
  check("Sam: the same choices on her card", ["Accept as it is", "Fix with note", "Open as Google Doc", "Undo my edit", "Cancel resume"].every(l => sActs.includes(l)), sActs.join(" | "));
  check("Sam: none of Alex's rows", await js(`return ![...document.querySelectorAll('li[data-job]')].some(li => [${ids.v_note}, ${ids.v_plain}, ${ids.v_doc}].includes(+li.dataset.job))`));

  // 7. phone width
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await sleep(300);
  const phone = await js(`const c = ${card(ids.s_note)}; return { scroll: document.documentElement.scrollWidth, acts: [...c.querySelectorAll('.actions button')].every(b => b.getBoundingClientRect().right <= 390), edit: c.querySelector('.edit').getBoundingClientRect().right <= 390 }`);
  check("phone: no sideways scroll; choices and the change note fit 390px", phone.scroll <= 390 && phone.acts && phone.edit, JSON.stringify(phone));
  check("no page errors", errors.length === 0, errors.join(" | "));
} catch (err) {
  check("driver ran", false, err.stack || err);
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(JSON.stringify({ checks }));
}
