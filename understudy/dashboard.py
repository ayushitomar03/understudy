"""A live view over the evidence directory.

Rebuilt around what someone actually needs to know, which is not what the first
version showed. That one streamed the newest run's events — useful for watching
a discovery happen, useless for the question "is this working", and blank for a
replay, because it only understood discovery's event vocabulary.

The questions, in the order they get asked:

  1. Is anything broken?          -> fleet view: every capability, its recent
                                     outcomes, its pass rate
  2. Is a human needed right now? -> pending escalations, surfaced first
  3. Why did that one fail?       -> one run, step by step, whatever its kind
  4. What is happening now?       -> the live run, if one is in progress

It reads the same append-only JSONL every run already writes, so watching costs
the run nothing and works identically on a finished run.

    .venv/bin/python cli.py dashboard
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .evidence import RUNS, read_events

PORT = 8765

# Outcomes worth colouring. Everything else renders neutral.
GOOD = {"complete", "success"}
BAD = {"failed", "hard_failure"}
PARTIAL = {"incomplete_action", "incomplete", "unreachable", "escalated", "refused"}

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Understudy</title>
<style>
  :root{
    --bg:#0d1416; --panel:#141e20; --line:#25353a; --ink:#e6edec; --dim:#8a9d9f;
    --teal:#5fb8b2; --green:#63b98c; --amber:#d3a95a; --red:#e0806f; --violet:#93a6dd;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:13px/1.55 "IBM Plex Sans",-apple-system,system-ui,sans-serif}
  a{color:inherit;text-decoration:none}
  header{padding:13px 20px;border-bottom:1px solid var(--line);
         display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
  h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
  h1 a:hover{color:var(--teal)}
  .stat{font:11px/1 "IBM Plex Mono",monospace;letter-spacing:.06em;color:var(--dim)}
  .stat b{color:var(--ink);font-weight:500}
  .stat.good b{color:var(--green)} .stat.bad b{color:var(--red)}
  .live{font:11px/1 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.09em;
        color:var(--teal);border:1px solid var(--teal);padding:4px 8px;border-radius:3px}
  main{padding:18px 20px 60px;max-width:1180px}
  h2{font:11px/1 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.1em;
     color:var(--dim);margin:0 0 11px;font-weight:500}
  section{margin-bottom:30px}

  /* escalation */
  .alert{background:#2a2312;border:1px solid var(--amber);border-radius:7px;
         padding:13px 16px;margin-bottom:18px}
  .alert b{color:var(--amber)}
  .alert a{color:var(--teal);text-decoration:underline}

  /* capability rows */
  .cap{background:var(--panel);border:1px solid var(--line);border-radius:7px;
       padding:13px 16px;margin-bottom:9px}
  .cap__top{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
  .cap__name{font:500 13px/1.3 "IBM Plex Mono",monospace;color:var(--teal)}
  .cap__meta{font:11px/1 "IBM Plex Mono",monospace;color:var(--dim);margin-left:auto}
  .cap__goal{color:var(--dim);margin-top:3px;font-size:12.5px}
  .strip{display:flex;gap:3px;margin-top:9px;flex-wrap:wrap}
  .tick{width:17px;height:17px;border-radius:3px;display:block;
        border:1px solid transparent;cursor:pointer}
  .tick.good{background:#1d4634;border-color:var(--green)}
  .tick.partial{background:#3a301a;border-color:var(--amber)}
  .tick.bad{background:#3a211c;border-color:var(--red)}
  .tick.other{background:#1e2b2d;border-color:var(--line)}
  .rate{font:11px/1 "IBM Plex Mono",monospace;margin-left:7px}
  .rate.good{color:var(--green)} .rate.mid{color:var(--amber)} .rate.bad{color:var(--red)}
  .rate.unknown{color:var(--dim)}
  /* an unjudged run is not a passing run — it is an unknown one, and it should
     not read as green just because its steps executed */
  .tick.unjudged{opacity:.45;border-style:dashed}

  /* run detail */
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;font:500 10.5px/1 "IBM Plex Mono",monospace;text-transform:uppercase;
     letter-spacing:.07em;color:var(--dim);padding:0 10px 6px 0;border-bottom:1px solid var(--line)}
  td{padding:7px 10px 7px 0;border-bottom:1px solid #1b2729;vertical-align:top}
  .mono{font-family:"IBM Plex Mono",monospace;font-size:11.5px}
  .ok{color:var(--green)} .no{color:var(--red)} .warn{color:var(--amber)}
  .said{border-left:2px solid var(--violet);padding-left:12px;margin:9px 0;color:#c3cde8;
        white-space:pre-wrap;font-size:12.5px}
  img{max-width:100%;border:1px solid var(--line);border-radius:4px;margin-top:9px}
  .empty{color:var(--dim);font-style:italic}
  .back{font:11px/1 "IBM Plex Mono",monospace;color:var(--dim)}
  .back:hover{color:var(--teal)}
  .grid{display:grid;grid-template-columns:1fr 340px;gap:20px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
</style>
<header>
  <h1><a href="/">Understudy</a></h1>
  <span class="stat" id="caps"></span>
  <span class="stat" id="runs"></span>
  <span class="stat" id="rate"></span>
  <span class="live" id="live" style="display:none"></span>
</header>
<main id="main"><p class="empty">loading…</p></main>
<script>
const esc = s => String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const cls = o => ["complete","success"].includes(o) ? "good"
  : ["incomplete_action","incomplete","unreachable","escalated","refused"].includes(o) ? "partial"
  : ["failed","hard_failure"].includes(o) ? "bad" : "other";

async function poll(){
  const run = new URLSearchParams(location.search).get("run");
  try{
    const r = await fetch(run ? `/api/run?run=${encodeURIComponent(run)}` : "/api/fleet");
    const data = await r.json();
    run ? renderRun(data) : renderFleet(data);
  }catch(e){ document.getElementById("main").innerHTML =
    `<p class="empty">cannot reach the evidence directory</p>`; }
  setTimeout(poll, 1500);
}

function header(s){
  document.getElementById("caps").innerHTML = `<b>${s.capabilities}</b> capabilities`;
  document.getElementById("runs").innerHTML = `<b>${s.total_runs}</b> runs`;
  const el = document.getElementById("rate");
  el.className = "stat " + (s.pass_rate >= 0.9 ? "good" : s.pass_rate >= 0.6 ? "" : "bad");
  el.innerHTML = `<b>${Math.round(s.pass_rate*100)}%</b> of recent runs complete`;
  const live = document.getElementById("live");
  if (s.live) { live.style.display = ""; live.textContent = "● " + s.live; }
  else live.style.display = "none";
}

function renderFleet(s){
  header(s);
  let html = "";

  if (s.escalations.length) html += `<section>` + s.escalations.map(e => `
    <div class="alert"><b>A human is needed.</b> ${esc(e.needed)}<br>
      <span class="mono">${esc(e.run)}</span> &middot;
      <a href="/operator">take the session</a> &middot;
      <a href="?run=${encodeURIComponent(e.run)}">see the run</a></div>`).join("") + `</section>`;

  html += `<section><h2>Capabilities &mdash; <span style="text-transform:none;letter-spacing:0">
    "ran" means the steps executed; "judged" means the job was done. Faded ticks were never judged.
    </span></h2>` + (s.caps.length ? s.caps.map(c => `
    <div class="cap">
      <div class="cap__top">
        <span class="cap__name">${esc(c.name)}</span>
        <span class="rate ${c.rate>=0.9?"good":c.rate>=0.6?"mid":"bad"}">
          ${c.runs.length ? "ran "+Math.round(c.rate*100)+"%" : "never run"}</span>
        <span class="rate ${c.judged===null?"unknown":c.judged>=0.9?"good":c.judged>=0.6?"mid":"bad"}"
              title="did the job, judged against the rubric">
          ${c.judged===null ? "never judged" : "judged "+c.judged.toFixed(2)}</span>
        ${c.unjudged ? `<span class="rate unknown">${c.unjudged} unjudged</span>` : ""}
        <span class="cap__meta">${esc(c.last_seen)}</span>
      </div>
      <div class="cap__goal">${esc(c.goal)}</div>
      <div class="strip">${c.runs.map(r => {
        // A run that executed but was judged wrong is not a pass. Execution
        // alone is what made a broken capability show as 100% green.
        const bad = r.judged !== null && r.judged < 0.6;
        const k = bad ? "bad" : cls(r.outcome);
        return `<a class="tick ${k}${r.judged===null?" unjudged":""}"
            href="?run=${encodeURIComponent(r.run)}"
            title="${esc(r.outcome)}${r.judged===null?" · not judged":" · judged "+r.judged}"></a>`;
      }).join("")}</div>
    </div>`).join("") : `<p class="empty">no capabilities have run yet</p>`) + `</section>`;

  if (s.orphans.length) html += `<section><h2>Discovery runs</h2>` + s.orphans.map(r => `
    <div class="cap"><div class="cap__top">
      <span class="cap__name"><a href="?run=${encodeURIComponent(r.run)}">${esc(r.run)}</a></span>
      <span class="rate ${cls(r.outcome)==="good"?"good":cls(r.outcome)==="bad"?"bad":"mid"}">
        ${esc(r.outcome)}</span>
      <span class="cap__meta">${esc(r.when)}</span></div>
      <div class="cap__goal">${esc(r.goal)}</div></div>`).join("") + `</section>`;

  document.getElementById("main").innerHTML = html;
}

function renderRun(s){
  header(s.fleet);
  const rows = s.steps.map(st => `<tr>
    <td class="mono">${st.index ?? ""}</td>
    <td class="mono ${st.ok===true?"ok":st.ok===false?"no":""}">${esc(st.label)}</td>
    <td>${esc(st.intent)}</td>
    <td class="mono ${st.ok===false?"no":""}">${esc(st.detail)}</td>
    <td class="mono">${st.ms ? st.ms+"ms" : ""}</td></tr>`).join("");

  document.getElementById("main").innerHTML = `
    <p><a class="back" href="/">&larr; all capabilities</a></p>
    <section>
      <h2>${esc(s.kind)} &middot; ${esc(s.run)}</h2>
      <p style="margin:0 0 6px"><b class="${cls(s.outcome)==="good"?"ok":cls(s.outcome)==="bad"?"no":"warn"}">
        ${esc(s.outcome)}</b> &nbsp;<span class="mono">${esc(s.detail)}</span></p>
      <p class="cap__goal" style="margin:0 0 14px">${esc(s.goal)}</p>
      <div class="grid">
        <div>
          ${s.said.map(t => `<p class="said">${esc(t)}</p>`).join("")}
          <table><thead><tr><th>#</th><th>what</th><th>intent</th><th>result</th><th>ms</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="5" class="empty">no steps recorded</td></tr>`}</tbody></table>
        </div>
        <div>
          ${s.outputs && Object.keys(s.outputs).length ? `<h2>Outputs</h2><table>` +
            Object.entries(s.outputs).map(([k,v]) =>
              `<tr><td class="mono">${esc(k)}</td><td class="mono ok">${esc(v)}</td></tr>`).join("") +
            `</table>` : ""}
          ${s.screenshot ? `<h2 style="margin-top:16px">Final screen</h2>
            <img src="/shot?p=${encodeURIComponent(s.screenshot)}">` : ""}
        </div>
      </div>
    </section>`;
}
poll();
</script>
"""

OPERATOR = """<!doctype html>
<meta charset="utf-8"><title>Understudy — operator</title>
<style>
  :root{--bg:#0d1416;--panel:#141e20;--line:#25353a;--ink:#e6edec;--dim:#8a9d9f;
        --teal:#5fb8b2;--green:#63b98c;--amber:#d3a95a;--red:#e0806f}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 "IBM Plex Sans",system-ui,sans-serif;
       display:flex;justify-content:center;padding:32px}
  .card{max-width:760px;width:100%;background:var(--panel);border:1px solid var(--line);
        border-radius:10px;padding:26px 30px}
  h1{margin:0 0 4px;font-size:17px}
  .sub{color:var(--dim);margin:0 0 22px;font-size:13px}
  .label{font:11px/1 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.09em;
         color:var(--dim);display:block;margin-bottom:5px}
  .field{margin-bottom:16px}
  .why{border-left:3px solid var(--amber);padding:10px 14px;color:#e8d5ab}
  .needed{border-left:3px solid var(--teal);padding:10px 14px;color:#b9e3e0}
  pre{font:11px/1.5 "IBM Plex Mono",monospace;color:var(--dim);white-space:pre-wrap;margin:0;
      max-height:190px;overflow:auto}
  img{width:100%;border:1px solid var(--line);border-radius:4px;margin-top:6px}
  .row{display:flex;gap:10px;margin-top:22px}
  button{flex:1;padding:12px;border-radius:6px;border:1px solid var(--line);cursor:pointer;
         font:600 13px/1 "IBM Plex Sans",sans-serif;background:var(--panel);color:var(--ink)}
  .go{background:#14291f;border-color:var(--green);color:var(--green)}
  .stop{background:#2c1a17;border-color:var(--red);color:var(--red)}
  .note{margin-top:18px;padding-top:16px;border-top:1px solid var(--line);color:var(--dim);font-size:12.5px}
  .idle{color:var(--dim);text-align:center;padding:40px}
</style>
<div class="card" id="card"><p class="idle">No intervention is waiting.</p></div>
<script>
const esc = s => String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
let current = null;
async function poll(){
  try{
    const s = await (await fetch("/api/intervention")).json();
    if (s.waiting) render(s); else if (current){ current=null; idle(s.holder); }
  }catch(e){}
  setTimeout(poll, 800);
}
function idle(holder){
  document.getElementById("card").innerHTML =
    `<p class="idle">No intervention is waiting.${holder?` Session held by <b>${esc(holder)}</b>.`:""}</p>`;
}
function render(s){
  if (current === s.run_id) return;
  current = s.run_id;
  const r = s.request;
  document.getElementById("card").innerHTML = `
    <h1>The agent needs you</h1>
    <p class="sub">run ${esc(s.run_id)} &middot; stopped at step ${esc(r.step)} &middot; you hold the session</p>
    <div class="field"><span class="label">Why it stopped</span><div class="why">${esc(r.why)}</div></div>
    <div class="field"><span class="label">What it needs</span><div class="needed">${esc(r.needed)}</div></div>
    <div class="field"><span class="label">Where it is</span><pre>${esc(r.url)}</pre>
      ${r.screenshot?`<img src="/shot?p=${encodeURIComponent(r.screenshot)}">`:""}</div>
    <div class="field"><span class="label">What it already tried</span><pre>${esc(r.ledger)}</pre></div>
    <div class="row">
      <button class="go" onclick="decide('resume')">I have done it — resume</button>
      <button class="stop" onclick="decide('abort')">Abort the run</button>
    </div>
    <p class="note">The browser window is the agent&rsquo;s own session, still open on this page.
       Do what is needed there, then come back. Your clicks are recorded as evidence.</p>`;
}
async function decide(d){
  await fetch("/api/decide",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({decision:d})});
  current=null; idle();
}
poll();
</script>
"""


# --------------------------------------------------------------------------
# reading the evidence
# --------------------------------------------------------------------------


def _summarise(run_dir: Path) -> dict | None:
    """One run, reduced to what the fleet view needs.

    Handles every kind of run. The first version only understood discovery's
    vocabulary, so a replay — which emits `step` and `replay_result` and nothing
    else — rendered as an empty page.
    """
    events = list(read_events(run_dir / "events.jsonl"))
    if not events:
        return None

    started = next((e for e in events if e["event"] == "run_started"), {})
    finished = next((e for e in events if e["event"] == "run_finished"), None)
    replayed = next((e for e in events if e["event"] == "replay_result"), None)
    judged = next((e for e in events if e["event"] == "judgement"), None)
    written = next((e for e in events if e["event"] == "capability_written"), None)

    kind = started.get("kind", run_dir.name.split("-")[0])
    goal = started.get("goal", "")

    if replayed:
        outcome = replayed.get("outcome", "?")
        detail = f"{replayed.get('subclass') or ''} score={replayed.get('score')}"
        # Prefer what the event recorded; fall back to the goal only for runs
        # written before the event carried it.
        capability = replayed.get("capability") or (
            goal.split(" ")[0] if kind in ("replay", "invoke") and goal else None)
    elif finished:
        outcome = finished.get("status", "?")
        detail = str(finished.get("reason") or finished.get("why") or "")
        capability = None
    else:
        outcome = "running"
        detail = ""
        capability = None

    # Two different questions, kept separate on purpose. `outcome` says the
    # recorded steps ran; `judged` says the job was done. A capability that
    # returns the wrong value completes every step, so treating completion as
    # health is how a broken capability shows as 100% green.
    return {
        "run": run_dir.name,
        "kind": kind,
        "judged": round(judged["score"], 2) if judged else None,
        "verdict": (judged or {}).get("verdict", ""),
        "goal": goal,
        "outcome": outcome,
        "detail": detail,
        "capability": (written or {}).get("path", "").split("/")[-1].replace(".json", "")
                      or capability,
        "when": started.get("at", "")[:19].replace("T", " "),
        "mtime": run_dir.stat().st_mtime,
        "running": finished is None and replayed is None,
    }


def fleet(root: Path) -> dict:
    """Every capability, its recent outcomes, and anything needing a human."""
    runs = []
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and (d / "events.jsonl").exists():
                summary = _summarise(d)
                if summary:
                    runs.append(summary)
    runs.sort(key=lambda r: r["mtime"], reverse=True)

    # group the execution runs by the capability they exercised
    by_capability: dict[str, list[dict]] = defaultdict(list)
    orphans = []
    for run in runs:
        if run["kind"] in ("replay", "invoke", "grade") and run["capability"]:
            by_capability[run["capability"]].append(run)
        elif run["kind"] == "discovery":
            orphans.append(run)

    caps = []
    for name, group in by_capability.items():
        recent = group[:24]
        passed = sum(1 for r in recent if r["outcome"] in GOOD)
        scored = [r["judged"] for r in recent if r["judged"] is not None]
        caps.append({
            "name": name,
            "goal": next((r["goal"] for r in recent if r["goal"]), ""),
            "runs": [{"run": r["run"], "outcome": r["outcome"], "judged": r["judged"]}
                     for r in reversed(recent)],
            "rate": passed / len(recent) if recent else 0.0,
            "judged": round(sum(scored) / len(scored), 2) if scored else None,
            "unjudged": len(recent) - len(scored),
            "last_seen": recent[0]["when"] if recent else "",
        })
    caps.sort(key=lambda c: (c["rate"], -len(c["runs"])))

    executions = [r for r in runs if r["kind"] in ("replay", "invoke")][:60]
    complete = sum(1 for r in executions if r["outcome"] in GOOD)

    return {
        "capabilities": len(caps),
        "total_runs": len(runs),
        "pass_rate": complete / len(executions) if executions else 0.0,
        "live": next((f"{r['kind']} running" for r in runs[:3] if r["running"]), None),
        "escalations": _escalations(root),
        "caps": caps,
        "orphans": orphans[:12],
    }


def _escalations(root: Path) -> list[dict]:
    """Runs currently waiting on a person. Surfaced above everything else,
    because it is the only thing on the page that is blocked on the viewer."""
    out = []
    if not root.exists():
        return out
    for path in sorted(root.glob("*/control.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            control = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if control.get("decision") == "pending" and control.get("request"):
            out.append({"run": path.parent.name,
                        "needed": control["request"].get("needed", ""),
                        "why": control["request"].get("why", "")})
    return out[:4]


def one_run(root: Path, name: str) -> dict:
    """One run in detail, whatever kind it is."""
    run_dir = root / name
    events = list(read_events(run_dir / "events.jsonl"))
    summary = _summarise(run_dir) or {"run": name, "kind": "?", "outcome": "?",
                                      "goal": "", "detail": ""}

    steps, said, outputs, screenshot = [], [], {}, None
    for event in events:
        kind = event["event"]
        if kind == "step":                      # replay and invoke
            steps.append({
                "index": event.get("index"),
                "label": event.get("action", ""),
                "intent": event.get("intent", ""),
                "detail": (event.get("error") or event.get("detail") or "")[:220],
                "ok": event.get("ok"),
                "ms": event.get("ms"),
            })
        elif kind == "attempt":                 # discovery
            args = event.get("args") or {}
            handle = args.get("anchor") or args.get("name") or args.get("url") or ""
            steps.append({
                "index": event.get("turn"),
                "label": f"{event.get('action','')} {str(handle)[:34]}",
                "intent": event.get("reason", ""),
                "detail": f"{event.get('verdict','')} {(event.get('detail') or '')[:160]}",
                "ok": event.get("verdict") == "advanced",
                "ms": None,
            })
        elif kind == "observation":
            steps.append({"index": event.get("turn"), "label": "observe", "intent": event.get("reason", ""),
                          "detail": f"{event.get('controls', 0)} controls", "ok": None, "ms": None})
            screenshot = event.get("screenshot") or screenshot
        elif kind == "model_said":
            said.append(event.get("text", ""))
        elif kind == "replay_result":
            outputs = event.get("outputs") or {}
            screenshot = event.get("screenshot") or screenshot
        elif kind in ("model_finished", "escalation_requested"):
            screenshot = event.get("screenshot") or screenshot

    return {**summary, "steps": steps, "said": said[-6:], "outputs": outputs,
            "screenshot": screenshot, "fleet": fleet(root)}


# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    runs_root: Path = RUNS

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE.encode())
        if parsed.path == "/operator":
            return self._send(200, "text/html; charset=utf-8", OPERATOR.encode())
        if parsed.path == "/api/fleet":
            return self._json(fleet(self.runs_root))
        if parsed.path == "/api/run":
            name = (query.get("run") or [""])[0]
            if not name or "/" in name:
                return self._send(400, "application/json", b'{"error":"bad run"}')
            return self._json(one_run(self.runs_root, name))
        if parsed.path == "/api/intervention":
            return self._json(self._intervention())
        if parsed.path == "/shot":
            path = Path(unquote((query.get("p") or [""])[0]))
            if path.exists() and path.suffix == ".png":
                return self._send(200, "image/png", path.read_bytes())
            return self._send(404, "text/plain", b"gone")
        self._send(404, "text/plain", b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/decide":
            return self._send(404, "text/plain", b"not found")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        decision = body.get("decision")
        waiting = _escalations(self.runs_root)
        if not waiting or decision not in ("resume", "abort"):
            return self._send(400, "application/json", b'{"error":"nothing waiting"}')

        path = self.runs_root / waiting[0]["run"] / "control.json"
        control = json.loads(path.read_text())
        control["decision"] = decision
        path.write_text(json.dumps(control, indent=2))
        self._json({"ok": True})

    def _intervention(self) -> dict:
        waiting = _escalations(self.runs_root)
        if not waiting:
            return {"waiting": False}
        run = waiting[0]["run"]
        control = json.loads((self.runs_root / run / "control.json").read_text())
        return {"waiting": True, "run_id": run, "holder": control.get("holder"),
                "request": control.get("request") or {}}

    def _json(self, payload: dict) -> None:
        self._send(200, "application/json", json.dumps(payload, default=str).encode())

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # keep the console for the run itself
        pass


def serve(run_id: str | None = None, port: int = PORT, root: Path | None = None) -> None:
    Handler.runs_root = root or RUNS
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler))
    print(f"dashboard: http://localhost:{port}   operator: http://localhost:{port}/operator")
    server.serve_forever()


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else None)
