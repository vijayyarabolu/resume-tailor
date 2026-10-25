// resume_tailor Archived: × Delete for good (press twice, Undo), Clear archived (confirm, one person), the auto-clear days field, phone.
// usage: node goal20_rg.mjs <base url> <token> <chrome path> <profile dir> <ids json> <expect json> <download dir>
import { spawn } from "node:child_process";

const [base, token, chromePath, profileDir, idsJson, expectJson, downloadDir] = process.argv.slice(2);
const ids = JSON.parse(idsJson), want = JSON.parse(expectJson);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const port = 9400 + Math.floor(Math.random() * 500);
const chrome = spawn(chromePath, ["--headless=new", "--no-sandbox", `--remote-debugging-port=${port}`, `--user-data-dir=${profileDir}`,
  "--no-first-run", "--window-size=1280,900", "about:blank"], { stdio: "ignore" });
const checks = [], errors = [];
const check = (name, ok, detail = "") => checks.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) });
const same = (a, b) => JSON.stringify([...a].sort((x, y) => x - y)) === JSON.stringify([...b].sort((x, y) => x - y));
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
  const open = async (path) => { await send("Page.navigate", { url: base + path }); await until(`document.querySelector('#who button[aria-pressed="true"]')`); await sleep(200); };
  const tab = (name) => js(`document.querySelector('.tabs button[data-tab="${name}"]').click()`);
  const listed = (sel) => js(`return [...document.querySelectorAll('${sel} li[data-job]')].map(li => +li.dataset.job)`);
  const counts = () => js(`return ['queue', 'ready', 'archived'].map(t => document.querySelector('#count-' + t).textContent)`);
  const who = () => js(`return document.querySelector('#who button[aria-pressed="true"]').textContent`);
  const all = (sel) => `[...document.querySelectorAll('${sel} li[data-job]')].map(li => +li.dataset.job)`;

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadDir });
  const S = JSON.stringify;
  const state = (p) => js(`return (await (await fetch('/api/state?profile=${p}')).json())`);
  const delBtn = (id) => `document.querySelector('#archived li[data-job="${id}"] button[data-act="delete"]')`;
  await open(`/?token=${token}&profile=alex-rivera`);
  await tab("archived");
  await sleep(300);
  const mine = [ids.a1, ids.a2, ids.a3];
  check("Alex's Archived lists theirs three", same(await listed("#archived"), mine), S(await listed("#archived")));
  check("the Ready badge says what it counts", /^\d+ not approved yet · \d+ in Ready$/.test(await js(`return document.querySelector('#count-ready').title`)), await js(`return document.querySelector('#count-ready').title`));
  check("each archived row has × Delete", await js(`return [...document.querySelectorAll('#archived li[data-job]')].every(li => li.querySelector('button[data-act="delete"]')?.textContent === '× Delete')`));
  check("Clear archived shows how many", (await js(`return document.querySelector('#archived [data-act="clear-archived"]')?.textContent`)) === "Clear archived (3)");
  check("the auto-clear field shows 30 days", (await js(`return document.querySelector('#keep-days').value`)) === "30");
  // × Delete: press twice, gone at once, Undo brings it back
  await js(`${delBtn(ids.a1)}.click(); return 1`);
  check("× Delete asks a second press", (await js(`return ${delBtn(ids.a1)}?.textContent`)) === "Yes, delete for good");
  await js(`${delBtn(ids.a1)}.click(); return 1`);
  check("the second press removes it from the list at once", await until(`!document.querySelector('#archived li[data-job="${ids.a1}"]')`, 4000), S(await listed("#archived")));
  check("a toast says so with Undo", await until(`[...document.querySelectorAll('.toast')].some(t => t.textContent.startsWith('Deleted Co a1 for good') && t.querySelector('.toast-act'))`, 3000));
  await js(`[...document.querySelectorAll('.toast')].find(t => t.textContent.startsWith('Deleted Co a1'))?.querySelector('.toast-act')?.click(); return 1`);
  check("Undo brings it back", await until(`document.querySelector('#archived li[data-job="${ids.a1}"]')`, 4000), S(await listed("#archived")));
  // Clear archived: confirm (says how many), only Alex's
  await js(`window.__asked = ''; window.confirm = (m) => { window.__asked = m; return true; }; document.querySelector('#archived [data-act="clear-archived"]').click(); return 1`);
  check("Clear archived asks first, with the number", (await js("return window.__asked")).startsWith("Delete all 3 archived resumes for good?"), await js("return window.__asked"));
  check("then Alex's Archived is empty", await until(`document.querySelector('#archived')?.textContent.includes('Nothing archived.')`, 4000), S(await listed("#archived")));
  check("Sam's archived resume is untouched", (await state("sam-lee")).archived.map((j) => j.id).join() === String(ids.s1), S((await state("sam-lee")).archived.map((j) => j.id)));
  // the auto-clear setting
  await js(`const f = document.querySelector('#keep-days'); f.value = '0'; f.dispatchEvent(new Event('change')); return 1`);
  check("0 days saves 'never'", await until(`(async () => (await (await fetch('/api/state?profile=alex-rivera')).json()).settings.archived_delete_days === 0)()`, 3000));
  // phone
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 3, mobile: true });
  await js(`[...document.querySelectorAll('#who button')].find(b => b.textContent === 'Sam').click(); return 1`);
  await sleep(500);
  await tab("archived");
  await sleep(300);
  const phone = await js(`return { scroll: document.documentElement.scrollWidth, rows: [...document.querySelectorAll('#archived li[data-job]')].map(li => Math.round(li.getBoundingClientRect().right)) }`);
  check("phone: Archived rows with × Delete fit 390px", phone.scroll <= 390 && phone.rows.length === 1 && phone.rows.every((r) => r <= 390), S(phone));
  check("no page errors", errors.length === 0, errors.join(" | "));
} catch (err) {
  check("driver ran", false, err.stack || err);
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(JSON.stringify({ checks }));
}
