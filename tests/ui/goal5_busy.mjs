// busy buttons: headless Chrome over the DevTools protocol (no packages). The PDF / page-image requests are held
// back (Fetch domain) to prove a pressed button stays busy until the NEW file is on screen, not just until the API
// answers. Prints {"checks": [...]}; prints "FINISH_FIX" and waits for a line on stdin while the harness plays the
// worker finishing the note round.
// usage: node goal5_busy.mjs <base url> <token> <chrome path> <profile dir> <ids json>
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const [base, token, chromePath, profileDir, idsJson] = process.argv.slice(2);
const ids = JSON.parse(idsJson);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const stdinLines = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const port = 9900 + Math.floor(Math.random() * 500);
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

  // held file requests (the new PDF / page image) and slowed API calls
  const net = { hold: false, held: [], slow: {} };
  ws.addEventListener("message", async (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result || m.error); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === "Page.javascriptDialogOpening") send("Page.handleJavaScriptDialog", { accept: true, promptText: "more MLOps" });
    if (m.method !== "Fetch.requestPaused") return;
    const { requestId, request } = m.params;
    const api = Object.keys(net.slow).find((k) => request.url.includes(k));
    if (api) {
      const rule = net.slow[api];
      await sleep(rule.ms);
      if (rule.body) {
        send("Fetch.fulfillRequest", { requestId, responseCode: 200, responseHeaders: [{ name: "Content-Type", value: "application/json" }],
          body: Buffer.from(JSON.stringify(rule.body)).toString("base64") });
      } else send("Fetch.continueRequest", { requestId });
      return;
    }
    if (net.hold && /\/file\/(pdf|png)/.test(request.url)) { net.held.push({ requestId, url: request.url }); return; }
    send("Fetch.continueRequest", { requestId });
  });
  const release = async () => { net.hold = false; const all = net.held.splice(0); for (const h of all) await send("Fetch.continueRequest", { requestId: h.requestId }); };
  const js = async (expr) => (await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true }))?.result?.value;
  const until = async (expr, ms = 8000) => { for (let t = 0; t < ms; t += 50) { if (await js(`return !!(${expr})`)) return true; await sleep(50); } return false; };
  const click = (sel) => js(`document.querySelector(${JSON.stringify(sel)}).click()`);
  const tab = (name) => js(`document.querySelector('.tabs button[data-tab="${name}"]').click()`);
  const busyText = (scope) => js(`const b = [...document.querySelectorAll('${scope} [aria-busy="true"], ${scope} .busyline')].find(x => x.getClientRects().length); return b ? b.textContent.replace(/ · \\d+ s$/, '').replace(/… .*$/, '…') + (b.matches('button') ? (b.disabled ? '|disabled' : '|enabled') + (b.querySelector('.spin') ? '|spin' : '') : '|line') : ''`);
  const idle = (scope) => `!document.querySelector('${scope} [aria-busy="true"], ${scope} .busyline')`;
  const panelSrc = () => js(`return document.querySelector('#panel iframe.pdf')?.getAttribute('src') || ''`);
  const heldFor = (id) => `/api/jobs/${id}/file/pdf`;

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Fetch.enable", { patterns: [{ urlPattern: "*/file/*", requestStage: "Request" }, { urlPattern: "*/api/jobs/*/*", requestStage: "Request" }] });
  await send("Page.navigate", { url: `${base}/?token=${token}&profile=alex-rivera` });
  await until(`document.querySelector('#who button[aria-pressed="true"]')`);

  // 1. Undo my edit: busy through the API AND until the version it brings back is loaded in the panel
  await tab("ready");
  await until(`document.querySelector('#ready li[data-job="${ids.v_ready}"]')`);
  await click(`#ready li[data-job="${ids.v_ready}"]`);
  await until(`document.querySelector('#panel iframe.pdf')?.dataset.loaded`);
  const before = await panelSrc();
  net.hold = true;
  net.slow[`/api/jobs/${ids.v_ready}/undo_edit`] = { ms: 700 };
  await click(`#panel button[data-act="undo"]`);
  await click(`#panel button[data-act="undo"]`);
  await sleep(250);
  check("Undo: the pressed button says Undoing…, disabled, with a spinner", (await busyText("#panel")) === "Undoing…|disabled|spin", await busyText("#panel"));
  check("Undo: the other buttons of that resume are held too", await js(`return [...document.querySelectorAll('#panel button[data-act="approve"], #panel button[data-act="reimport"]')].every(b => b.disabled)`));
  check("Undo: the new PDF is asked for", await until(`document.querySelector('#panel iframe.pdf')?.getAttribute('src') !== ${JSON.stringify(before)}`), await panelSrc());
  await sleep(600);
  check("Undo: still busy while the new PDF has not loaded (old view never looks finished)", net.held.some((h) => h.url.includes(heldFor(ids.v_ready))) && (await busyText("#panel")).startsWith("Undoing…"), `${await busyText("#panel")} held=${net.held.length}`);
  await release();
  check("Undo: busy ends once the new PDF is on screen", await until(idle("#panel")), await busyText("#panel"));
  check("…and the panel shows the version it went back to", (await panelSrc()) !== before && await js(`return !!document.querySelector('#panel iframe.pdf').dataset.loaded`));
  delete net.slow[`/api/jobs/${ids.v_ready}/undo_edit`];

  // 2. Approve again: busy while the server works
  net.slow[`/api/jobs/${ids.v_ready}/approve`] = { ms: 900, body: { ok: true, updated: true, drive: { name: "x.pdf" } } };
  await click(`#panel button[data-act="approve"]`);
  await sleep(250);
  check("Approve: Uploading to Drive…, disabled, spinner", (await busyText("#panel")) === "Uploading to Drive…|disabled|spin", await busyText("#panel"));
  check("Approve: back to normal after", await until(idle("#panel"), 6000), await busyText("#panel"));
  delete net.slow[`/api/jobs/${ids.v_ready}/approve`];

  // 2b. Re-import (the server's real endpoint; the harness's re-import takes ~1 s): an edit that caused a new problem
  // moves it to needs you: the open panel updates in place (no reload) and says where it went
  const beforeRe = await panelSrc();
  net.hold = true;
  await click(`#panel button[data-act="reimport"]`);
  await sleep(250);
  check("Re-import: Getting your Doc edits…, disabled, spinner", (await busyText("#panel")) === "Getting your Doc edits…|disabled|spin", await busyText("#panel"));
  check("Re-import: key r does nothing while busy", await js(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'r', bubbles: true })); return true`) && (await busyText("#panel")) === "Getting your Doc edits…|disabled|spin");
  check("Re-import: the open panel asks for the new PDF by itself", await until(`document.querySelector('#panel iframe.pdf')?.getAttribute('src') !== ${JSON.stringify(beforeRe)}`, 8000), await panelSrc());
  await sleep(400);
  check("Re-import: still busy while the new PDF has not loaded", (await busyText("#panel")).startsWith("Getting your Doc edits…"), await busyText("#panel"));
  await release();
  check("Re-import: busy ends once the new PDF is on screen", await until(idle("#panel")), await busyText("#panel"));
  const moved = await js(`return document.querySelector('#panel').hidden ? '' : document.querySelector('#panel .edit')?.textContent || ''`);
  check("Re-import: the panel stays open and says where it went", moved.includes("Moved from Ready to Queue · needs you") && moved.includes("your edit caused 1 new problem") && moved.includes("OP, KS"), moved);
  check("Re-import: the older problem is marked as there before the edit", (await js(`return document.querySelector('#panel .fails')?.textContent || ''`)).includes("(already there before your edit)"));
  check("Re-import: a toast says it moved", await until(`[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('moved to Queue · needs you'))`));
  check("Re-import: the card is in the Queue now, not in Ready", await until(`document.querySelector('#queue li[data-job="${ids.v_ready}"]') && !document.querySelector('#ready li[data-job="${ids.v_ready}"]')`));

  // 2c. Re-import whose only problems were there before the edit: stays in Ready with a problems chip
  await js(`document.querySelector('#panel-close').click()`);
  await until(`document.querySelector('#ready li[data-job="${ids.v_keep}"]')`);
  await click(`#ready li[data-job="${ids.v_keep}"]`);
  await until(`!document.querySelector('#panel').hidden && document.querySelector('#panel button[data-act="reimport"][data-id="${ids.v_keep}"]')`);
  await click(`#panel button[data-act="reimport"]`);
  check("Re-import (old problems only): busy, then done", (await busyText("#panel")).startsWith("Getting your Doc edits…") && await until(idle("#panel"), 8000), await busyText("#panel"));
  const kept = await js(`return document.querySelector('#panel .edit')?.textContent || ''`);
  check("Re-import (old problems only): Stays in Ready, says why", kept.includes("Stays in Ready") && kept.includes("caused no new problem"), kept);
  check("Re-import (old problems only): the Ready row has a problems (1) chip", await until(`document.querySelector('#ready li[data-job="${ids.v_keep}"] .chip.outdated')?.textContent === 'problems (1)'`));
  check("Re-import (old problems only): the toast says it stays in Ready", await until(`[...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('stays in Ready'))`));
  await js(`document.querySelector('#panel-close').click()`);

  // 3. Fix with note: busy while Claude works (the worker), until the new page and "Your note" box are shown
  await tab("queue");
  await js(`document.querySelector('#panel-close').click()`);
  await until(`document.querySelector('#queue li[data-job="${ids.v_fix}"]')`);
  await click(`#queue li[data-job="${ids.v_fix}"] button[data-act="fixopen"]`);
  await js(`const t = document.querySelector('form[data-fix="${ids.v_fix}"] textarea'); t.value = 'I know data warehousing'; t.dispatchEvent(new Event('input', { bubbles: true }))`);
  net.slow[`/api/jobs/${ids.v_fix}/fix`] = { ms: 500 };
  await js(`document.querySelector('form[data-fix="${ids.v_fix}"]').requestSubmit()`);
  await sleep(200);
  const fixBtn = await js(`const b = document.querySelector('form[data-fix="${ids.v_fix}"] button[type=submit]'); return b ? b.textContent.replace(/ · \\d+ s$/, '') + '|' + b.disabled : ''`);
  check("Fix: the Fix it button says Fixing with your note…, disabled", fixBtn === "Fixing with your note…|true", fixBtn);
  check("Fix: once queued, the card keeps saying so", await until(`document.querySelector('#queue li[data-job="${ids.v_fix}"] .busyline')?.textContent.startsWith('Fixing with your note…')`), await busyText(`#queue li[data-job="${ids.v_fix}"]`));
  await sleep(1200);
  check("Fix: still busy while Claude works", (await busyText(`#queue li[data-job="${ids.v_fix}"]`)) === "Fixing with your note…|line");
  check("Fix: Cancel of the running job stays possible", await js(`const b = document.querySelector('#queue li[data-job="${ids.v_fix}"] button[data-act="cancel"]'); return !!b && !b.disabled`));
  net.hold = true;
  console.log("FINISH_FIX");
  await stdinLines.next();                                  // the harness has written the note round's result
  check("Fix: the new card with the Your note box arrives", await until(`document.querySelector('#queue li[data-job="${ids.v_fix}"] .edit[data-edit="fix"]')`), "");
  await sleep(400);
  check("Fix: still busy while its new page image has not loaded", (await busyText(`#queue li[data-job="${ids.v_fix}"]`)) === "Fixing with your note…|line" && net.held.length > 0, `${await busyText(`#queue li[data-job="${ids.v_fix}"]`)} held=${net.held.length}`);
  await release();
  check("Fix: busy ends when the new page is shown", await until(idle(`#queue li[data-job="${ids.v_fix}"]`)), await busyText(`#queue li[data-job="${ids.v_fix}"]`));
  delete net.slow[`/api/jobs/${ids.v_fix}/fix`];

  // 4. Cancel resume (asks twice): Cancelling… until it has left the list
  await click(`#queue li[data-job="${ids.v_drop}"] button[data-act="open"]`);
  await until(`!document.querySelector('#panel').hidden && document.querySelector('#panel button[data-act="drop"][data-id="${ids.v_drop}"]')`);
  net.slow[`/api/jobs/${ids.v_drop}/cancel`] = { ms: 700 };
  await click(`#panel button[data-act="drop"]`);
  await click(`#panel button[data-act="drop"]`);
  await sleep(250);
  check("Cancel: Cancelling…, disabled, spinner", (await busyText("#panel")) === "Cancelling…|disabled|spin", await busyText("#panel"));
  check("Cancel: then gone from the Queue, panel closed", await until(`!document.querySelector('#queue li[data-job="${ids.v_drop}"]') && document.querySelector('#panel').hidden`));
  delete net.slow[`/api/jobs/${ids.v_drop}/cancel`];

  // 5. Regenerate with note: Regenerating… stays while it is queued for Claude
  await tab("ready");
  await click(`#ready li[data-job="${ids.v_keep}"]`);
  await until(`!document.querySelector('#panel').hidden && document.querySelector('#panel button[data-act="regen"][data-id="${ids.v_keep}"]')`);
  await click(`#panel button[data-act="regen"]`);
  check("Regenerate: Regenerating… once sent", await until(`document.querySelector('#panel [aria-busy="true"], #panel .busyline')?.textContent.startsWith('Starting over…')`), await busyText("#panel"));
  await sleep(1200);
  check("Regenerate: still busy while it waits for Claude", (await busyText("#panel")).startsWith("Starting over…"), await busyText("#panel"));

  // 6. phone width (page image instead of the PDF): Looks good says Saving… and the button fits the screen
  await js(`document.querySelector('#panel-close').click()`);
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await send("Page.reload");
  await until(`document.querySelector('#who button[aria-pressed="true"]')`);
  await tab("queue");
  await until(`document.querySelector('#queue li[data-job="${ids.v_fix}"] button[data-act="open"]')`);
  await click(`#queue li[data-job="${ids.v_fix}"] button[data-act="open"]`);
  await until(`!document.querySelector('#panel').hidden && document.querySelector('#panel img.pageimg') && document.querySelector('#panel button[data-act="accept"][data-id="${ids.v_fix}"]')`);
  net.slow[`/api/jobs/${ids.v_fix}/accept`] = { ms: 700 };
  await click(`#panel button[data-act="accept"]`);
  await sleep(250);
  const phone = await js(`const b = document.querySelector('#panel button[aria-busy="true"]'); return b ? { text: b.textContent, fits: b.getBoundingClientRect().right <= 390, scroll: document.documentElement.scrollWidth } : null`);
  check("phone: Looks good says Saving…, and fits 390px", phone && phone.text.startsWith("Saving…") && phone.fits && phone.scroll <= 390, JSON.stringify(phone));
  check("phone: then moved to Ready, panel closed", await until(`!document.querySelector('#queue li[data-job="${ids.v_fix}"]') && document.querySelector('#panel').hidden`));
  check("no page errors", errors.length === 0, errors.join(" | "));
} catch (err) {
  check("driver ran", false, err.stack || err);
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(JSON.stringify({ checks }));
  process.exit(0);
}
