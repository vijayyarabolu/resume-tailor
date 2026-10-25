// The app shell (tests/test_goal21_rg_ui.py): top bar, side menu, person switch, theme, worker status and dialogs;
// Settings as its own view (the model is shown, never changed by opening it); the tracker link only when one is set;
// the phone: the menu becomes a bottom bar, the review panel and its links fit 390 px.
// usage: node goal21_rg.mjs <base url> <token> <chrome path> <profile dir> <ids json> <expect json> <download dir>
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
  const tab = (name) => js(`document.querySelector('.tabs button[data-tab="${name}"]').click()`);
  const S = JSON.stringify;
  const noSideways = () => js("return document.documentElement.scrollWidth <= innerWidth && document.body.scrollWidth <= innerWidth");
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: `${base}/?token=${token}&profile=alex-rivera` });
  await until(`document.querySelector('#who button[aria-pressed="true"]')`);
  await sleep(300);

  // ---- one product: the shared stylesheet and top bar ----
  check("the shared design file is loaded", await js("return [...document.styleSheets].some((s) => (s.href || '').endsWith('/static/ds.css')) && getComputedStyle(document.documentElement).getPropertyValue('--mark').trim() !== ''"));
  check("the top bar: brand, worker status, person switch, theme, keys", await js("return ['.top .brand', '#worker-status .dot', '#who', '#theme', '#keys'].every((s) => document.querySelector(s)?.offsetParent)"));
  check("body text is at least 14 px", parseFloat(await js("return getComputedStyle(document.body).fontSize")) >= 14);
  // ---- the side menu ----
  const menu = await js("return [...document.querySelectorAll('.tabs [data-tab]')].map((b) => [b.dataset.tab, b.querySelector('.lbl').textContent, b.getAttribute('aria-selected')])");
  check("side menu: New resume, Queue, Ready, Archived, Settings", S(menu.map((m) => m[0])) === S(["new", "queue", "ready", "archived", "settings"]), S(menu));
  check("side menu: one active item", menu.filter((m) => m[2] === "true").length === 1, S(menu));
  await tab("ready");
  check("side menu: a click shows that view and marks it", await until("!document.querySelector('#tab-ready').hidden && document.querySelector('.tabs [data-tab=ready]').getAttribute('aria-selected') === 'true'", 3000));
  // ---- the person switch: names kept, initials, one click ----
  const who = await js("return [...document.querySelectorAll('#who button')].map((b) => [b.textContent, b.dataset.initial, b.getAttribute('aria-pressed')])");
  check("person switch: Alex and Sam with their initials", S(who.map((w) => w[0]).sort()) === S(["Alex", "Sam"]) && who.every((w) => w[1] === w[0][0]), S(who));
  await js("[...document.querySelectorAll('#who button')].find((b) => b.textContent === 'Sam').click(); return 1");
  check("person switch: one click switches", await until("document.querySelector('#who button[aria-pressed=true]').textContent === 'Sam' && location.search.includes('profile=sam-lee')", 4000));
  check("no tracker link until one is set in Settings", await js("return document.querySelector('#tracker-link').hidden"));
  // ---- worker status: one dot + a word ----
  const worker = await js("return [document.querySelector('#worker').textContent, document.querySelector('#worker-status .dot').className, document.querySelector('#worker-status').getAttribute('aria-label')]");
  check("worker status: a word, a dot and a label", /^(Worker on|Worker off|Making \d+)$/.test(worker[0]) && worker[1].startsWith("dot") && (worker[2] || "").startsWith("Status:"), S(worker));
  // ---- Settings: its own view; the model is shown as it is ----
  const model = (await js("return (await (await fetch('/api/state?profile=alex-rivera')).json()).settings.model"));
  await tab("settings");
  check("Settings: the status panel lists Claude, PDF, Google, profile and worker", await until("document.querySelectorAll('#status-list .srow').length === 5", 4000));
  check("Settings: its own view with the model and the archive days", await until("!document.querySelector('#tab-settings').hidden && document.querySelector('#model').offsetParent && document.querySelector('#keep-days').offsetParent", 3000));
  await until(`document.querySelector('#model').value === ${JSON.stringify(model)}`, 4000);
  check("Settings: the model shown is the one in use", (await js("return document.querySelector('#model').value")) === model, `${await js("return document.querySelector('#model').value")} vs ${model}`);
  check("Settings: archived resumes are kept 30 days", (await js("return document.querySelector('#keep-days').value")) === "30");
  // ---- theme and keys ----
  const theme = await js("return document.documentElement.dataset.theme || ''");
  await js("document.querySelector('#theme').click(); return 1");
  check("the theme switches on one click", (await js("return document.documentElement.dataset.theme || ''")) !== theme);
  await js("document.querySelector('#theme').click(); return 1");
  await js("document.querySelector('#keys').click(); return 1");
  check("the keys card opens and fits the screen", await js("const h = document.querySelector('#help'); const r = h.getBoundingClientRect(); return !h.hidden && r.right <= innerWidth && r.bottom <= innerHeight"));
  await js("document.querySelector('#keys').click(); return 1");
  check("desktop: no sideways scroll", await noSideways());

  // ---- the phone ----
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 3, mobile: true });
  await js("[...document.querySelectorAll('#who button')].find((b) => b.textContent === 'Alex').click(); return 1");
  await sleep(500);
  await tab("ready");
  await sleep(300);
  const bar = await js("return [...document.querySelectorAll('.tabs [data-tab]')].map((b) => { const r = b.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom), Math.round(r.height)]; })");
  check("phone: the menu is a bottom bar of five, 44 px or taller", bar.length === 5 && bar.every((b) => b[1] <= 844 && b[1] >= 780 && b[2] >= 44), S(bar));
  check("phone: the top bar names the view", (await js("return document.querySelector('#view-title').textContent")) === "Ready");
  check("phone: Ready has no sideways scroll", await noSideways());
  await js(`document.querySelector('#ready li[data-job="${ids.long}"]').click(); return 1`);
  check("phone: the review panel opens", await until("!document.querySelector('#panel').hidden", 4000));
  await sleep(400);
  const wide = await js("return [...document.querySelectorAll('#panel .actions > *')].filter((e) => e.getBoundingClientRect().right > innerWidth + 0.5).map((e) => e.textContent.slice(0, 40))");
  check("phone: every panel button and link fits 390 px (a long Drive name included)", wide.length === 0, S(wide));
  check("no page errors", errors.length === 0, errors.join(" | "));
} catch (err) {
  check("driver ran", false, err.stack || err);
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(JSON.stringify({ checks }));
}
