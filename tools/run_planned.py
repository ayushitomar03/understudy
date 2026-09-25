"""Replay every task the map could plan, and grade the answers.

This is the number the whole idea turns on. A plan built from the map costs no
model calls, so the only question is whether it is right — and finding out is also
free, because replay consults no model either. A synthesised plan is a hypothesis
that can be tested for nothing.

    .venv/bin/python tools/run_planned.py [--workers 4]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from ab20 import configure, gate, start_apps
from ab_twenty import grade
from tasks100 import TASKS, tier
from understudy.evidence import EventLog
from understudy.plan import from_map
from understudy.replay import Replayer
from understudy.sitemap import load as load_map
from understudy.surface.web import WebSurface

CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}
OUT = Path("evidence/planned.json")
SAMPLES = 3       # the hazarded ones are probabilistic, so more than one run


def replay(task, capability, app: str, log: EventLog):
    surface = WebSurface()
    try:
        return Replayer(capability, surface, log,
                        gate(task, app, "attended" if task.mutates else "unattended")
                        ).run(task.params, secrets=CREDENTIALS)
    finally:
        surface.close()


async def worker(app: str, queue: asyncio.Queue, results: dict) -> None:
    while True:
        try:
            task, capability = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        log = EventLog("planned", task.id)
        passes, whys = 0, []
        started = time.monotonic()
        for _ in range(SAMPLES):
            configure(task, app)
            try:
                result = await asyncio.to_thread(replay, task, capability, app, log)
            except Exception as exc:
                whys.append(f"error: {exc}")
                continue
            if result.outcome == "complete":
                ok, why = grade(task, result.outputs or {})
                passes += ok
                if not ok:
                    whys.append(why)
            else:
                whys.append(f"{result.outcome}/{result.subclass or '-'}")
        results[task.n] = {
            "n": task.n, "tier": tier(task), "goal": task.goal.format(**task.params),
            "hazard": ",".join(task.hazards) or "-", "steps": len(capability.steps),
            "passes": passes, "of": SAMPLES, "pass": passes == SAMPLES,
            "why": "; ".join(sorted(set(whys))[:2]) or "clean",
            "seconds": round((time.monotonic() - started) / SAMPLES, 1),
        }
        r = results[task.n]
        print(f"  {task.n:>3} [{r['tier']:<4}] {'PASS' if r['pass'] else 'fail':<4} "
              f"{r['passes']}/{r['of']}  {r['seconds']:>5.1f}s  {r['why'][:44]}", flush=True)
        OUT.write_text(json.dumps([results[k] for k in sorted(results)], indent=2))


async def main(workers: int) -> int:
    sitemap = load_map("http://localhost:8090")
    if sitemap is None:
        print("no map on disk")
        return 1
    print(f"  map: {sitemap.summary()}\n")

    plans = []
    for task in TASKS:
        capability, why = from_map(sitemap, task.goal.format(**task.params),
                                  task.params, list(task.outputs), task.id)
        if capability is not None:
            plans.append((task, capability))
    print(f"  {len(plans)}/{len(TASKS)} tasks planned from the map, 0 model calls\n")

    apps = start_apps(min(workers, len(plans)))
    for app in apps:                      # the plan carries the mapping port
        pass
    queue: asyncio.Queue = asyncio.Queue()
    for task, capability in plans:
        queue.put_nowait((task, capability))

    results: dict[int, dict] = {}
    started = time.monotonic()

    async def run(i: int) -> None:
        # each worker replays against its own application
        while True:
            try:
                task, capability = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            local = capability.model_copy(deep=True)
            local.app.base_url = apps[i]
            await worker_one(task, local, apps[i], results)

    async def worker_one(task, capability, app, results) -> None:
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait((task, capability))
        await worker(app, q, results)

    await asyncio.gather(*(run(i) for i in range(len(apps))))
    elapsed = time.monotonic() - started

    rows = [results[k] for k in sorted(results)]
    by_tier: dict[str, list[dict]] = {}
    for r in rows:
        by_tier.setdefault(r["tier"], []).append(r)

    print(f"\n{'=' * 74}")
    print(f"  {'tier':<6}{'planned':>9}{'correct':>9}   what failed")
    print("-" * 74)
    for name in sorted(by_tier):
        group = by_tier[name]
        good = sum(1 for r in group if r["pass"])
        whys = {r["why"] for r in group if not r["pass"]}
        print(f"  {name:<6}{len(group):>9}{good:>9}   "
              f"{'; '.join(sorted(whys))[:38] if whys else ''}")
    print("-" * 74)
    good = sum(1 for r in rows if r["pass"])
    print(f"  {'TOTAL':<6}{len(rows):>9}{good:>9}")
    print(f"\n  {good}/{len(TASKS)} of all one hundred tasks answered correctly "
          f"with ZERO model calls")
    print(f"  {sum(r['seconds'] for r in rows) / max(len(rows), 1):.1f}s per task"
          f" · {elapsed / 60:.0f} min wall clock · written to {OUT}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.workers)))
