"""Twenty tasks, baseline against loop, run in parallel.

The sequential version took about two hours, and almost all of it was waiting:
one LLM attempt on a legacy screen is 40 seconds when the app behaves and up to
nine minutes when it does not, and there were sixty of them in a row.

Nothing about the measurement needs to be sequential. What made it sequential was
shared state — the hazard switches are global to an application and posting a
transfer changes balances the next task reads — so the fix is not threads over one
app, it is *an application per worker*. Meridian is a hundred lines of stdlib and
holds its data in memory, so N instances on N ports are N independent banks that
cannot contaminate each other.

    baseline   one LLM attempt per task, no artifact. This is also the recording
               the loop arm starts from, so it is run once and used twice.
    loop       replay that recording, and if it is unreliable, repair it and
               measure again.

    .venv/bin/python tools/ab20.py [--workers 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from ab_twenty import (B_ROUNDS, B_SAMPLES, CREDENTIALS, RELIABLE, TASKS, Task,
                       grade, read_values)
from understudy.artifact.schema import Capability
from understudy.evidence import EventLog
from understudy.loop.runner import discover
from understudy.policy import for_app
from understudy.repair import repair
from understudy.replay import Replayer
from understudy.surface.web import WebSurface

BASE_PORT = 8100
OUT = Path("evidence/ab20.json")


# ------------------------------------------------------------ an app each ----


def start_apps(n: int) -> list[str]:
    """One Meridian per worker. Independent memory means independent hazards."""
    procs, urls = [], []
    for i in range(n):
        port = BASE_PORT + i
        subprocess.Popen([".venv/bin/python", "targets/meridian/app.py", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        urls.append(f"http://localhost:{port}")
    for url in urls:                          # wait for each to answer
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/admin/hazards", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit(f"{url} never came up")
    print(f"  {n} independent Meridian instances on {BASE_PORT}-{BASE_PORT + n - 1}\n")
    return urls


def configure(task: Task, app: str) -> None:
    urllib.request.urlopen(app + "/admin/reset").read()
    for name, value in task.hazards.items():
        if name == "interstitial":
            urllib.request.urlopen(f"{app}/admin/interstitial?odds={value}").read()
        else:
            urllib.request.urlopen(f"{app}/admin/hazard?name={name}&on=1").read()


def gate(task: Task, app: str, mode: str):
    return for_app(app, mode=mode, allow_irreversible=task.mutates)


# ------------------------------------------------------------- the two arms --


async def baseline(task: Task, app: str) -> tuple[dict, Capability | None]:
    """One LLM attempt, from nothing. Also the recording the loop arm uses."""
    configure(task, app)
    started = time.monotonic()
    capability, log = await discover(
        goal=task.goal.format(**task.params), base_url=app,
        capability_id=task.id, parameters=task.params,
        outputs=list(task.outputs), output_patterns=task.patterns,
        credentials=CREDENTIALS,
        policy=gate(task, app, "attended" if task.mutates else "discover"))

    values = read_values(log)
    ok, why = grade(task, values)
    return ({"pass": bool(ok and capability is not None), "why": why,
             "finished": capability is not None,
             "turns": _turns(log), "seconds": round(time.monotonic() - started, 1),
             "read": values, "run": log.run_id}, capability)


def _turns(log: EventLog) -> int:
    events = [json.loads(l) for l in (log.dir / "events.jsonl").read_text().splitlines()
              if l.strip().startswith("{")]
    for e in events:
        if e.get("event") == "run_finished" and e.get("turns") is not None:
            return int(e["turns"])
    return sum(1 for e in events if e.get("event") in
               ("observation", "output_declared", "param_declared", "model_said"))


def replay_once(task: Task, capability: Capability, app: str, log: EventLog):
    surface = WebSurface()
    try:
        return Replayer(capability, surface, log,
                        gate(task, app, "attended" if task.mutates else "unattended")
                        ).run(task.params, secrets=CREDENTIALS)
    finally:
        surface.close()


async def measure(task: Task, capability: Capability, app: str, n: int) -> dict:
    """Replay n times. Graded on the answer, not on whether the run finished."""
    log = EventLog("ab20", f"{task.id} v{capability.version}")
    ran = correct = 0
    whys: list[str] = []
    started = time.monotonic()
    for _ in range(n):
        configure(task, app)
        result = await asyncio.to_thread(replay_once, task, capability, app, log)
        if result.outcome == "complete":
            ran += 1
            ok, why = grade(task, result.outputs or {})
            correct += ok
            if not ok:
                whys.append(why)
        else:
            whys.append(f"{result.outcome}/{result.subclass or '-'}")
    return {"ran": ran, "correct": correct, "of": n,
            "seconds": round((time.monotonic() - started) / n, 1),
            "why": "; ".join(sorted(set(whys))[:3]) or "clean"}


async def loop_arm(task: Task, capability: Capability | None, app: str) -> dict:
    if capability is None:
        return {"pass": False, "correct": 0, "of": B_SAMPLES, "version": 0,
                "rounds": [], "why": "nothing was recorded to replay", "seconds": 0.0}

    before = await measure(task, capability, app, B_SAMPLES)
    rounds, current, after = [], capability, before

    if before["correct"] / before["of"] < RELIABLE:
        # Ten rather than five. The wait recovery measured 3/15 -> 14/15 in
        # isolation and the loop still rolled it back, because at five samples the
        # margin `improved()` demands is two, and on a hazard that fires half the
        # time a lucky "before" swallows a real fix. The margin is right; the
        # sample count was too small to earn it.
        current, report = await repair(
            capability, task.params, CREDENTIALS, samples=10, rounds=B_ROUNDS,
            policy=gate(task, app, "attended" if task.mutates else "unattended"))
        rounds = [{"round": r.n, "cause": r.cause, "change": r.change, "kept": r.kept,
                   "statement": r.statement[:240]} for r in report.rounds]
        if current.version != capability.version:
            after = await measure(task, current, app, B_SAMPLES)

    return {"pass": after["correct"] >= 9, "correct": after["correct"], "of": after["of"],
            "version": current.version, "rounds": rounds, "why": after["why"],
            "seconds": after["seconds"], "before": before["correct"],
            "escalated": getattr(report, "escalated", "") if rounds else ""}


# ------------------------------------------------------------------- pool ----


async def worker(name: int, app: str, queue: asyncio.Queue, results: dict) -> None:
    while True:
        try:
            task = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            b, capability = await baseline(task, app)
            l = await loop_arm(task, capability, app)
            results[task.n] = {"n": task.n, "goal": task.goal.format(**task.params),
                               "hazard": ",".join(task.hazards) or "-",
                               "baseline": b, "loop": l}
            print(f"  {task.n:>2}. baseline {'PASS' if b['pass'] else 'fail':<4}"
                  f" {b['turns']:>3} turns {b['seconds']:>6.0f}s"
                  f"   loop {'PASS' if l['pass'] else 'fail':<4}"
                  f" {l['correct']:>2}/{l['of']} v{l['version']} {l['seconds']:>5.1f}s"
                  f"   {l['why'][:40]}", flush=True)
        except Exception as exc:              # one task must not take the run down
            results[task.n] = {"n": task.n, "goal": task.goal.format(**task.params),
                               "hazard": ",".join(task.hazards) or "-",
                               "baseline": {"pass": False, "turns": 0, "seconds": 0,
                                            "why": f"harness error: {exc}"},
                               "loop": {"pass": False, "correct": 0, "of": B_SAMPLES,
                                        "version": 0, "rounds": [],
                                        "why": f"harness error: {exc}", "seconds": 0}}
            print(f"  {task.n:>2}. HARNESS ERROR: {exc}", flush=True)
        finally:
            OUT.write_text(json.dumps([results[k] for k in sorted(results)],
                                      indent=2, default=str))


async def main(workers: int, which: list[int] | None) -> int:
    tasks = [t for t in TASKS if which is None or t.n in which]
    apps = start_apps(min(workers, len(tasks)))

    queue: asyncio.Queue = asyncio.Queue()
    # Longest first, so the nine-minute tasks start before the forty-second ones
    # and no worker is left holding one at the end.
    for task in sorted(tasks, key=lambda t: (not t.mutates, not t.hazards, t.n)):
        queue.put_nowait(task)

    results: dict[int, dict] = {}
    started = time.monotonic()
    await asyncio.gather(*(worker(i, apps[i], queue, results)
                           for i in range(len(apps))))
    elapsed = time.monotonic() - started

    rows = [results[k] for k in sorted(results)]
    report(rows, elapsed, len(apps))
    return 0


def report(rows: list[dict], elapsed: float, workers: int) -> None:
    print(f"\n{'=' * 96}")
    print(f"{'#':>3}  {'task':<44} {'hazard':<13} {'BASELINE':<9} {'WITH LOOP':<13}")
    print("-" * 96)
    for r in rows:
        loop = r["loop"]
        mark = ("PASS " if loop["pass"] else "fail ") + f"{loop['correct']}/{loop['of']}"
        if loop.get("version", 1) > 1:
            mark += f" v{loop['version']}"
        print(f"{r['n']:>3}  {r['goal'][:43]:<44} {r['hazard']:<13} "
              f"{'PASS' if r['baseline']['pass'] else 'fail':<9} {mark:<13}")
    print("-" * 96)
    bp = sum(r["baseline"]["pass"] for r in rows)
    lp = sum(r["loop"]["pass"] for r in rows)
    turns = sum(r["baseline"]["turns"] for r in rows)
    bsec = sum(r["baseline"]["seconds"] for r in rows)
    lsec = sum(r["loop"]["seconds"] for r in rows)
    print(f"{'':>3}  {'TOTAL':<44} {'':<13} {bp}/{len(rows):<7} {lp}/{len(rows)}")
    print(f"\n  baseline   {turns} model turns, {turns / len(rows):.0f} per execution — "
          f"paid again on every run;  {bsec / len(rows):.0f}s per execution")
    print(f"  loop       0 model turns per execution after the recording;  "
          f"{lsec / len(rows):.1f}s per execution")
    print(f"\n  wall clock {elapsed / 60:.0f} min on {workers} workers")
    print(f"  written to {OUT}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--tasks")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.workers,
                              [int(n) for n in a.tasks.split(",")] if a.tasks else None)))
