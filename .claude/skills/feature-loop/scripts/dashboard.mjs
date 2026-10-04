#!/usr/bin/env node
// feature-loop dashboard: a live page of what the loop's agents are doing.
//
//   node <SKILL_DIR>/scripts/dashboard.mjs [--detach] [--port 8765]     (run in the project root)
//
// It prints its URL (http://127.0.0.1:<port>) and serves one page that refreshes every 5 s:
// the accounts, every agent running now with its latest action, the current build's tickets,
// what finished in the last 24 hours, and the watchdog's log when there is one. With --detach
// it starts itself in the background and returns; when the port already answers it only prints
// the URL. It reads and never writes: the run records in ~/.claude/accounts/runs, the accounts'
// state, the newest orchestrator state file in the temp dir, each running session's transcript,
// and (once a minute) the feature's tickets and PR through `gh`.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { execFile, execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const argv = process.argv.slice(2);
const flag = (name) => { const i = argv.indexOf(name); return i === -1 ? null : argv[i + 1]; };
const PORT = Number(flag('--port')) || 8765;
const URL_ = `http://127.0.0.1:${PORT}`;
const ROOT = path.resolve(flag('--root') || process.cwd());
const TMP = os.tmpdir();
const ACC = path.join(os.homedir(), '.claude', 'accounts');
const RUNS = path.join(ACC, 'runs');
const PROJECTS = path.join(os.homedir(), '.claude', 'projects');

const readJson = (p, fallback) => {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); } catch { return fallback; }
};
const list = (dir) => { try { return fs.readdirSync(dir); } catch { return []; } };
const norm = (p) => path.resolve(p || '').replace(/\\/g, '/').toLowerCase();
// The project's own checkout, or one of its worktrees (`<repo>.worktrees/…`, `.claude/worktrees/…`).
const mine = (cwd) => { const c = norm(cwd); const r = norm(ROOT); return c === r || c.startsWith(`${r}/`) || c.startsWith(`${r}.worktrees/`); };

const role = (name) => (name.startsWith('orch-') ? 'orchestrator'
  : name.startsWith('ticket-') ? 'implementer'
    : name.startsWith('flow-') ? 'flow checker'
      : name.startsWith('research-') ? 'research'
        : name.startsWith('audit-') ? 'audit orchestrator' : 'worker');

// The command lines of every process, to tell a run that is alive from a record a kill left
// behind as `running: true`. null when the list cannot be read (then the record is trusted).
function commandLines() {
  try {
    if (process.platform === 'win32') {
      return execFileSync('powershell.exe', ['-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | ForEach-Object { $_.CommandLine }"],
      { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, windowsHide: true });
    }
    return execFileSync('ps', ['-eo', 'args'], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  } catch { return null; }
}

function transcript(session) {
  for (const dir of list(PROJECTS)) {
    const p = path.join(PROJECTS, dir, `${session}.jsonl`);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

// The last thing a session did: its newest tool call or line of text, from the transcript's tail.
function lastActivity(session) {
  const p = transcript(session);
  if (!p) return null;
  const stat = fs.statSync(p);
  const size = Math.min(stat.size, 200_000);
  const buf = Buffer.alloc(size);
  const fd = fs.openSync(p, 'r');
  fs.readSync(fd, buf, 0, size, stat.size - size);
  fs.closeSync(fd);
  for (const line of buf.toString('utf8').split('\n').reverse()) {
    let entry;
    try { entry = JSON.parse(line); } catch { continue; }
    const content = entry?.message?.content;
    if (entry?.type !== 'assistant' || !Array.isArray(content)) continue;
    for (const part of [...content].reverse()) {
      if (part.type === 'tool_use') {
        const i = part.input || {};
        const what = i.description || i.command || i.file_path || i.pattern || i.prompt || i.skill || '';
        return { at: stat.mtimeMs, text: `${part.name}: ${String(what).replace(/\s+/g, ' ').slice(0, 220)}` };
      }
      if (part.type === 'text' && part.text.trim()) {
        return { at: stat.mtimeMs, text: part.text.trim().replace(/\s+/g, ' ').slice(0, 220) };
      }
    }
  }
  return { at: stat.mtimeMs, text: '' };
}

// The newest orchestrator state file (TMP/feature-loop-<slug>.json) whose build runs in this project.
function newestState(records) {
  const states = [];
  for (const f of list(TMP)) {
    const m = /^feature-loop-([a-z0-9-]+)\.json$/.exec(f);
    if (!m || f.startsWith('feature-loop-ctx-guard-')) continue;
    const s = readJson(path.join(TMP, f), null);
    if (!s?.tickets) continue;
    const slug = s.slug || m[1];
    const orch = records.find((r) => r.name === `orch-${slug}` || r.name.startsWith(`orch-${slug}-`));
    if (orch && !mine(orch.rec.cwd)) continue;
    states.push({ ...s, slug, file: f, changed: fs.statSync(path.join(TMP, f)).mtimeMs, known: Boolean(orch) });
  }
  states.sort((a, b) => Number(b.known) - Number(a.known) || b.changed - a.changed);
  return states[0] || null;
}

let issues = { at: 0, feature: null, items: [], pr: null };
function refreshIssues(feature, slug) {
  if (!feature || (Date.now() - issues.at < 60_000 && issues.feature === feature)) return;
  issues = { ...(issues.feature === feature ? issues : { items: [], pr: null }), at: Date.now(), feature };
  const opts = { cwd: ROOT, windowsHide: true };
  execFile('gh', ['api', `repos/{owner}/{repo}/issues/${feature}/sub_issues`, '--jq',
    '[.[] | {number, title, state, labels: [.labels[].name]}]'], opts, (err, out) => {
    if (!err) { try { issues.items = JSON.parse(out); } catch { /* keep the old list */ } }
  });
  execFile('gh', ['pr', 'list', '--state', 'all', '--head', `feature/${slug}`, '--json', 'number,state'], opts, (err, out) => {
    if (!err) { try { issues.pr = JSON.parse(out)[0] || null; } catch { /* keep */ } }
  });
}

let cache = { at: 0, data: null };
function collect() {
  if (Date.now() - cache.at < 4000) return cache.data;
  const now = Date.now();
  const state = { spent: {}, ...readJson(path.join(ACC, 'state.json'), {}) };
  const lines = commandLines();
  const records = list(RUNS).filter((f) => f.endsWith('.json'))
    .map((f) => ({ name: f.slice(0, -5), file: path.join(RUNS, f), rec: readJson(path.join(RUNS, f), null) }))
    .filter((r) => r.rec);
  const agents = [];
  const finished = [];
  for (const { name, file, rec } of records) {
    const alive = rec.running && rec.session && (lines === null || lines.includes(rec.session));
    if (alive) {
      agents.push({ name, role: role(name), account: rec.account, model: `${rec.model} ${rec.effort}`, here: mine(rec.cwd),
        cwd: rec.cwd, turn: fs.statSync(file).mtimeMs, doing: lastActivity(rec.session) });
    } else if (rec.last && mine(rec.cwd) && now - Date.parse(rec.last.at) < 24 * 3600 * 1000) {
      finished.push({ name, account: rec.account, at: Date.parse(rec.last.at), exit: rec.last.exit,
        result: String(rec.last.result || '').replace(/\s+/g, ' ').slice(0, 400) });
    }
  }
  const rank = (a) => (a.here ? 0 : 2) + (a.role.includes('orchestrator') ? 0 : 1);
  agents.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  finished.sort((a, b) => b.at - a.at);
  const names = [...new Set([...Object.keys(state.spent || {}), ...Object.keys(state.last || {}), ...agents.map((a) => a.account), 'max'])]
    .filter(Boolean).sort((a, b) => (a === 'max') - (b === 'max') || a.localeCompare(b));
  const accounts = names.map((n) => ({ name: n, running: agents.filter((a) => a.account === n).length,
    spentUntil: state.spent?.[n] > now ? state.spent[n] : null }));
  const build = newestState(records);
  if (build) refreshIssues(build.feature, build.slug);
  const logs = list(TMP).filter((f) => /^feature-loop-orch-watch-.*\.log$/.test(f))
    .map((f) => ({ f, m: fs.statSync(path.join(TMP, f)).mtimeMs })).sort((a, b) => b.m - a.m);
  const watchdog = logs.length ? fs.readFileSync(path.join(TMP, logs[0].f), 'utf8').trim().split('\n').slice(-3) : [];
  cache = { at: now, data: { now, root: ROOT, accounts, agents, finished: finished.slice(0, 12), build,
    tickets: build && issues.feature === build.feature ? issues.items : [],
    pr: build && issues.feature === build.feature ? issues.pr : null, watchdog } };
  return cache.data;
}

const PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feature loop agents</title>
<style>
:root { --bg:#f7f7f5; --card:#fff; --ink:#1c1c1a; --dim:#6b6b66; --line:#e3e3de; --ok:#1f7a3d; --warn:#a15c00; --bad:#b3261e; --accent:#3b5bdb; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --card:#1f1f1d; --ink:#ecece8; --dim:#9a9a93; --line:#33332f; --ok:#6fcf8e; --warn:#e6a850; --bad:#f28b82; --accent:#8fa6ff; } }
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui, "Segoe UI", sans-serif; }
h1 { font-size:18px; margin:0 0 2px; } h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin:22px 0 8px; }
.sub { color:var(--dim); font-size:12px; }
.row { display:flex; gap:10px; flex-wrap:wrap; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.acct { min-width:130px; flex:1; } .acct b { font-size:16px; }
.agent { margin-bottom:8px; } .agent .top { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.name { font-weight:600; } .pill { font-size:11px; padding:1px 7px; border-radius:10px; border:1px solid var(--line); color:var(--dim); white-space:nowrap; }
.pill.ok { color:var(--ok); border-color:var(--ok); } .pill.warn { color:var(--warn); border-color:var(--warn); } .pill.bad { color:var(--bad); border-color:var(--bad); } .pill.acc { color:var(--accent); border-color:var(--accent); }
.doing { margin-top:4px; font:12px/1.4 ui-monospace, Consolas, monospace; color:var(--dim); overflow-wrap:anywhere; }
table { width:100%; border-collapse:collapse; } td, th { text-align:left; padding:5px 8px; border-top:1px solid var(--line); vertical-align:top; } th { color:var(--dim); font-weight:500; font-size:12px; border-top:0; }
.scroll { overflow-x:auto; } .empty { color:var(--dim); font-style:italic; }
pre { margin:0; font:12px/1.4 ui-monospace, Consolas, monospace; color:var(--dim); white-space:pre-wrap; overflow-wrap:anywhere; }
</style></head><body>
<h1>Feature loop agents</h1><div class="sub" id="stamp">loading…</div>
<h2>Accounts</h2><div class="row" id="accounts"></div>
<h2>Running now</h2><div id="agents"></div>
<h2 id="buildTitle">Build</h2><div class="card scroll" id="build"></div>
<h2>Finished in the last 24 hours</h2><div class="card scroll" id="finished"></div>
<h2>Watchdog</h2><div class="card"><pre id="watchdog"></pre></div>
<script>
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const ago = (ms, now) => { if (!ms) return ''; const s = Math.max(0, Math.round((now - ms) / 1000)); return s < 90 ? s + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h'; };
const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const fill = (id, nodes) => document.getElementById(id).replaceChildren(...nodes);
function render(d) {
  document.getElementById('stamp').textContent = d.root + ' · updated ' + new Date(d.now).toLocaleTimeString() + ' · refreshes every 5 s';
  fill('accounts', d.accounts.map((a) => { const c = el('div', 'card acct'); c.append(el('b', '', a.name), el('div', '', a.running + ' running'), el('div', 'sub', a.spentUntil ? 'spent until ' + clock(a.spentUntil) : 'available')); return c; }));
  fill('agents', d.agents.length ? d.agents.map((a) => { const c = el('div', 'card agent'); const top = el('div', 'top');
    top.append(el('span', 'name', a.name), el('span', 'pill', a.role), el('span', 'pill acc', 'account ' + a.account), el('span', 'pill', a.model));
    if (!a.here) top.append(el('span', 'pill warn', 'other project'));
    top.append(el('span', 'sub', 'this turn ' + ago(a.turn, d.now) + (a.doing ? ' · last action ' + ago(a.doing.at, d.now) + ' ago' : '')));
    c.append(top, el('div', 'doing', a.doing && a.doing.text ? a.doing.text : 'no transcript yet'));
    if (!a.here) c.append(el('div', 'sub', a.cwd)); return c; }) : [el('div', 'card empty', 'Nothing is running.')]);
  const b = d.build; const box = [];
  document.getElementById('buildTitle').textContent = b ? 'Build: feature #' + b.feature + ' (' + b.slug + ')' : 'Build';
  if (!b) box.push(el('div', 'empty', 'No orchestrator state file for this project yet.'));
  else { const t = el('table'); const h = el('tr'); ['Ticket', 'Title', 'Wave', 'Status', 'Worker', 'Account', 'Failures', 'GitHub'].forEach((x) => h.append(el('th', '', x))); t.append(h);
    const gh = Object.fromEntries((d.tickets || []).map((x) => [x.number, x]));
    const nums = [...new Set([...Object.keys(b.tickets), ...Object.keys(gh)])].sort((x, y) => x - y);
    for (const n of nums) { const s = b.tickets[n] || {}; const g = gh[n]; const r = el('tr'); const st = s.status || 'waiting';
      const cls = st === 'closed' ? 'ok' : st === 'parked' ? 'bad' : st === 'waiting' ? '' : 'warn'; const sp = el('td'); sp.append(el('span', 'pill ' + cls, st));
      const marks = g ? [g.state.toLowerCase(), g.labels.includes('in-progress') && 'in progress', g.labels.includes('ready-for-human') && 'parked for you', g.labels.includes('needs-info') && 'needs info'].filter(Boolean).join(', ') : '';
      r.append(el('td', '', '#' + n), el('td', '', g ? g.title : ''), el('td', '', s.wave ?? ''), sp, el('td', '', s.agent || ''), el('td', '', s.account || ''), el('td', '', s.failures ?? ''), el('td', 'sub', marks));
      t.append(r); }
    box.push(t, el('div', 'sub', 'State file ' + b.file + ', changed ' + ago(b.changed, d.now) + ' ago' + (d.pr ? ' · PR #' + d.pr.number + ' ' + d.pr.state.toLowerCase() : ' · no PR yet') + (b.flow_checks ? ' · flow checks: ' + b.flow_checks : ''))); }
  fill('build', box);
  if (!d.finished.length) fill('finished', [el('div', 'empty', 'Nothing finished yet.')]);
  else { const t = el('table'); const h = el('tr'); ['Ended', 'Agent', 'Account', 'Result', 'Reply'].forEach((x) => h.append(el('th', '', x))); t.append(h);
    for (const f of d.finished) { const r = el('tr'); const res = el('td'); res.append(el('span', 'pill ' + (f.exit === 0 ? 'ok' : f.exit === 10 ? 'warn' : 'bad'), f.exit === 0 ? 'replied' : f.exit === 10 ? 'usage limit' : 'stopped (' + f.exit + ')'));
      r.append(el('td', '', clock(f.at)), el('td', '', f.name), el('td', '', f.account), res, el('td', 'sub', f.result)); t.append(r); }
    fill('finished', [t]); }
  document.getElementById('watchdog').textContent = d.watchdog.join('\\n') || 'no watchdog log';
}
async function tick() { try { render(await (await fetch('/data')).json()); } catch (e) { document.getElementById('stamp').textContent = 'The dashboard server is not answering: ' + e.message; } }
tick(); setInterval(tick, 5000);
</script></body></html>`;

const answering = () => new Promise((resolve) => {
  const req = http.get(`${URL_}/data`, { timeout: 1500 }, (res) => { res.resume(); resolve(res.statusCode === 200); });
  req.on('error', () => resolve(false));
  req.on('timeout', () => { req.destroy(); resolve(false); });
});

if (await answering()) {
  console.log(`dashboard already running: ${URL_}`);
} else if (argv.includes('--detach')) {
  const rest = argv.filter((a) => a !== '--detach');
  spawn(process.execPath, [fileURLToPath(import.meta.url), ...rest, ...(flag('--root') ? [] : ['--root', ROOT])],
    { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  console.log(`dashboard started: ${URL_}`);
} else {
  http.createServer((req, res) => {
    if (req.url === '/data') {
      let body;
      try { body = JSON.stringify(collect()); } catch (e) { res.writeHead(500); res.end(String(e)); return; }
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(body);
    } else {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(PAGE);
    }
  }).listen(PORT, '127.0.0.1', () => console.log(`dashboard: ${URL_}`));
}
