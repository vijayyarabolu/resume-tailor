// UI test: headless Chrome over the DevTools protocol (no packages). Prints {"checks": [{name, ok, detail}]}.
// usage: node goal4_ui.mjs <base url> <token> <chrome path> <profile dir> <ids json> <expect json> <download dir>
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
  const open = async (path) => { await send("Page.navigate", { url: base + path }); await until(`document.querySelector('#who button[aria-pressed="true"]')`); await sleep(200); };
  const tab = (name) => js(`document.querySelector('.tabs button[data-tab="${name}"]').click()`);
  const listed = (sel) => js(`return [...document.querySelectorAll('${sel} li[data-job]')].map(li => +li.dataset.job)`);
  const counts = () => js(`return ['queue', 'ready', 'archived'].map(t => document.querySelector('#count-' + t).textContent)`);
  const who = () => js(`return document.querySelector('#who button[aria-pressed="true"]').textContent`);
  const all = (sel) => `[...document.querySelectorAll('${sel} li[data-job]')].map(li => +li.dataset.job)`;

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloadDir });

  // 1. first open: no link, nothing remembered, -> the first person (Alex), only Alex's jobs
  await open(`/?token=${token}`);
  check("the switch reads Alex | Sam", (await js(`return [...document.querySelectorAll('#who button')].map(b => b.textContent).join('|')`)) === "Alex|Sam");
  check("default opens Alex", (await who()) === "Alex", await who());
  const calls = await js(`return performance.getEntriesByType('resource').map(e => e.name).filter(n => n.includes('/api/state')).map(n => new URL(n).search)`);
  check("a fresh browser asks once for 'auto', never for everyone's list", calls.filter(c => c === "?profile=auto").length === 1 && calls.every(c => /^\?profile=(auto|alex-rivera)$/.test(c)), JSON.stringify(calls));
  check("the first load drops ?token= from the address", await js(`return !location.search.includes('token')`), await js(`return location.search`));
  const vReady = [ids.v_today1, ids.v_today2, ids.v_yday, ids.v_3d, ids.v_old];
  check("Alex's Ready: only theirs", same(await listed("#ready"), vReady), JSON.stringify(await listed("#ready")));
  check("Alex's Queue: only theirs", same(await listed("#queue"), [ids.v_live]), JSON.stringify(await listed("#queue")));
  check("Alex's Archived: only theirs", same(await listed("#archived"), [ids.v_arch]), JSON.stringify(await listed("#archived")));
  check("Alex's counts: queue 1, ready 4 (not approved), archived 1", JSON.stringify(await counts()) === '["1","4","1"]', JSON.stringify(await counts()));
  check("New resume is for Alex", (await js(`return document.querySelector('#profile').value`)) === "alex-rivera");
  check("Alex's Archive all approved counts only theirs (1)", (await js(`return document.querySelector('#ready [data-act="archive-approved"]')?.textContent`)) === "Archive all approved (1)");

  // 2. day groups, numbers, times
  await tab("ready");
  const heads = await js(`return [...document.querySelectorAll('#ready .day-h')].map(h => h.textContent.trim())`);
  check("Ready day headers: Today, Yesterday, a date, More than 30 days", JSON.stringify(heads) === JSON.stringify([`${want.today} (2)`, `${want.yday} (1)`, `${want.d3} (1)`, "More than 30 days (1)"]), JSON.stringify(heads));
  const today = await js(`return [...document.querySelectorAll('#ready .day')[0].querySelectorAll('li[data-job]')].map(li => ({ id: +li.dataset.job, n: li.querySelector('.num').textContent, when: li.querySelector('.meta.when').textContent }))`);
  check("Today: newest first, numbered 1, 2", today.map(r => r.id).join() === [ids.v_today2, ids.v_today1].join() && today.map(r => r.n).join() === "1,2", JSON.stringify(today));
  check("each row has its time (h:mm AM/PM)", today.every(r => /^Made \d{1,2}:\d\d [AP]M/.test(r.when)), today.map(r => r.when).join(" | "));
  check("an approved card says approved <time>", today[0].when.includes(`approved ${want.approved}`), today[0].when);
  check("numbering restarts in each day", (await js(`return [...document.querySelectorAll('#ready .day')].map(d => d.querySelector('.num').textContent).join()`)) === "1,1,1,1");
  check("existing badges kept (ready, approved)", await js(`return !!document.querySelector('#ready [data-job="${ids.v_today2}"] .chip.ready') && !!document.querySelector('#ready [data-job="${ids.v_today2}"] .chip.approved')`));
  check("Archive buttons kept", await js(`return !!document.querySelector('#ready [data-job="${ids.v_today1}"] [data-act="archive"]')`));
  check("Queue and Archived are grouped too", await js(`return !!document.querySelector('#queue .day .day-h') && !!document.querySelector('#archived .day .day-h') && /^Today · /.test(document.querySelector('#archived .day-h').textContent)`));

  // 3. Download PDF stamps "downloaded <time>"
  await js(`document.querySelector('#ready [data-job="${ids.v_today1}"]').click()`);
  await until(`document.querySelector('#panel:not([hidden]) a[href*="file/pdf?"][href*="dl=1"]')`);
  await js(`document.querySelector('#panel a[href*="file/pdf?"][href*="dl=1"]').click()`);
  check("Download PDF: the card says downloaded <time>", await until(`/downloaded \\d{1,2}:\\d\\d [AP]M/.test(document.querySelector('#ready [data-job="${ids.v_today1}"] .meta.when').textContent)`), await js(`return document.querySelector('#ready [data-job="${ids.v_today1}"] .meta.when').textContent`));
  await js(`document.querySelector('#panel-close').click()`);

  // 4. switch to Sam: every tab and count is theirs; the address carries ?profile=sam-lee
  await js(`document.querySelector('#who button[data-person="sam-lee"]').click()`);
  await until(`document.querySelector('#who button[data-person="sam-lee"][aria-pressed="true"]')`);
  check("the switch never shows Alex's rows under Sam", await js(`return !document.querySelector('#ready [data-job="${ids.v_today1}"]')`));
  await until(`document.querySelector('#ready [data-job="${ids.s_today}"]')`);
  check("Sam's Ready: only theirs", same(await listed("#ready"), [ids.s_today]), JSON.stringify(await listed("#ready")));
  check("Sam's Queue: only theirs (live + needs you)", same(await listed("#queue"), [ids.s_live, ids.s_needs]), JSON.stringify(await listed("#queue")));
  check("Sam's Archived: only theirs", same(await listed("#archived"), [ids.s_arch]), JSON.stringify(await listed("#archived")));
  // the Queue number is what is being made; her needs-you resume has its own badge
  check("Sam's counts: queue 2, ready none, archived 1", JSON.stringify(await counts()) === '["1","","1"]'
    && (await js(`return document.querySelector('#count-needs').textContent`)) === "1 needs you", JSON.stringify(await counts()));
  check("New resume follows the switch", (await js(`return document.querySelector('#profile').value`)) === "sam-lee");
  check("the address carries ?profile=sam-lee and no token", await js(`return location.search === '?profile=sam-lee'`), await js(`return location.search`));
  check("Archive all approved shows Sam's count (1)", (await js(`return document.querySelector('#ready [data-act="archive-approved"]')?.textContent`)) === "Archive all approved (1)");
  await js(`document.querySelector('#ready [data-act="archive-approved"]').click()`);
  check("Archive all approved empties Sam's Ready", await until(`!document.querySelector('#ready li[data-job]')`));
  check("Sam's archived count is 2", await until(`document.querySelector('#count-archived').textContent === '2'`));

  // 4b. a slow answer for the person just switched away from never blanks the page
  await js(`window.__f = window.fetch; window.fetch = (u, o) => String(u).includes('profile=sam-lee') ? new Promise(r => setTimeout(() => r(window.__f(u, o)), 1500)) : window.__f(u, o)`);
  await js(`document.querySelector('#who button[data-person="alex-rivera"]').click()`);
  await until(`document.querySelector('#ready [data-job="${ids.v_today1}"]')`);
  await js(`document.querySelector('#who button[data-person="sam-lee"]').click()`);
  await sleep(100);
  await js(`document.querySelector('#who button[data-person="alex-rivera"]').click()`);
  await sleep(2000);                                                   // Sam's slow answer lands now
  check("a late answer for the other person is ignored", (await who()) === "Alex" && same(await listed("#queue"), [ids.v_live]) && (await counts())[1] === "4", JSON.stringify(await counts()));
  await js(`window.fetch = window.__f`);
  await js(`document.querySelector('#who button[data-person="sam-lee"]').click()`);
  await until(`document.querySelector('#queue [data-job="${ids.s_live}"]')`);

  // 5. remembered per browser; a ?profile= link wins; the New form's Person select is the same switch
  await open(`/`);
  check("reload without a link stays on Sam (remembered)", (await who()) === "Sam", await who());
  await open(`/?profile=alex-rivera`);
  check("?profile=alex-rivera opens Alex", (await who()) === "Alex", await who());
  check("Alex's approved resume was not archived by Sam's button", await js(`return !!document.querySelector('#ready [data-job="${ids.v_today2}"]')`));
  await js(`const s = document.querySelector('#profile'); s.value = 'sam-lee'; s.dispatchEvent(new Event('change'))`);
  await until(`document.querySelector('#queue [data-job="${ids.s_live}"]')`);
  check("choosing Sam in New resume switches the page", (await who()) === "Sam" && same(await listed("#queue"), [ids.s_live, ids.s_needs]));

  // 6. phone width: no sideways scroll, the switch and day headers fit
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await tab("ready");
  await sleep(300);
  const phone = await js(`const sw = document.querySelector('#who').getBoundingClientRect(), tabs = document.querySelector('.tabs'); return { scroll: document.documentElement.scrollWidth, sw: sw.right, tabs: tabs.scrollWidth <= tabs.clientWidth, rows: [...document.querySelectorAll('#ready li[data-job]')].every(li => li.getBoundingClientRect().right <= 390) }`);
  check("phone: no sideways scroll; switch, tabs and rows fit 390px", phone.scroll <= 390 && phone.sw <= 390 && phone.rows && phone.tabs, JSON.stringify(phone));
  check("no page errors", errors.length === 0, errors.join(" | "));
} catch (err) {
  check("driver ran", false, err.stack || err);
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
  console.log(JSON.stringify({ checks }));
}
