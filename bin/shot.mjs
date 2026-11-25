// Ganzseiten-Screenshot per Chrome DevTools Protocol (Node >= 22, kein npm-Paket noetig).
// Aufruf: node shot.mjs <url> <breite> <dpr> <mobile 0|1> <out.png> [feste_hoehe]
// feste_hoehe: nur diesen Ausschnitt aufnehmen (z. B. Querformat-Viewport), sonst ganze Seite
// Misst die echte Seitenhoehe im gerenderten Layout und nimmt alles auf (captureBeyondViewport).
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url, w, dpr, mobile, out] = [process.argv[2], +process.argv[3], +process.argv[4], process.argv[5] === "1", process.argv[6]];
const fixH = +process.argv[7] || 0;
const chrome = process.env.CHROME || "google-chrome";
const prof = mkdtempSync(join(tmpdir(), "cd-shot-"));
const port = 9400 + Math.floor(Math.random() * 400);
const ch = spawn(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
  `--remote-debugging-port=${port}`, `--user-data-dir=${prof}`, "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const done = code => { try { ch.kill("SIGKILL"); } catch {} try { rmSync(prof, { recursive: true, force: true }); } catch {} process.exit(code); };
setTimeout(() => { console.error("timeout"); done(2); }, 60000);

let tabs;
for (let i = 0; i < 60 && !tabs; i++) { try { tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(250); } }
if (!tabs) { console.error("chrome startet nicht"); done(1); }
const ws = new WebSocket(tabs.find(t => t.type === "page").webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));
let id = 0; const pend = {};
ws.onmessage = e => { const m = JSON.parse(e.data); if (pend[m.id]) { pend[m.id](m); delete pend[m.id]; }
  // DEBUG=1: JavaScript-Fehler der Seite auf stderr ausgeben
  if (process.env.DEBUG && m.method === "Runtime.exceptionThrown") console.error("JS-Fehler:", m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  if (process.env.DEBUG && m.method === "Runtime.consoleAPICalled" && m.params.type === "error") console.error("console.error:", m.params.args.map(a => a.value ?? a.description).join(" ")); };
const send = (method, params = {}) => new Promise(r => { pend[++id] = r; ws.send(JSON.stringify({ id, method, params })); });
const evalJs = async expr => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result.result.value;

await send("Emulation.setDeviceMetricsOverride", { width: w, height: fixH || 900, deviceScaleFactor: dpr, mobile });
if (mobile) await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });  // pointer:coarse wie am Handy
if (process.env.DEBUG) await send("Runtime.enable");
await send("Page.navigate", { url });
// warten bis die Daten gerendert sind (Karten vorhanden), max. ~10 s
for (let i = 0; i < 40; i++) { if (await evalJs("!!(typeof LAST!=='undefined' && LAST && document.querySelector('#nodes .card'))")) break; await sleep(250); }
await sleep(800);
const h = fixH || await evalJs("Math.ceil(document.documentElement.scrollHeight)");
// Viewport auf volle Hoehe ziehen, damit fixe Hintergruende die ganze Seite abdecken
await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: dpr, mobile });
await sleep(500);
const r = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true,
  clip: { x: 0, y: 0, width: w, height: h, scale: 1 } });
if (!r.result?.data) { console.error("capture fehlgeschlagen", JSON.stringify(r.error || {})); done(1); }
writeFileSync(out, Buffer.from(r.result.data, "base64"));
console.log(JSON.stringify({ w, h, dpr }));
done(0);
