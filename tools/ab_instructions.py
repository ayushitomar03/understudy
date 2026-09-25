"""Does knowing the application first make the work cheaper and more reliable?

Two arms, same tasks, same grader, same application state:

    without   the goal and the application. Nothing else. The model opens the app
              and works it out, which is what it has done all along.
    with      the same, plus the verified screen map — what the screens are, how
              each identifies itself, which controls are on them and resolved, and
              what each of the application's messages means.

Both arms are one run per task, because that is the question: does the *first*
attempt get better. Success is graded on the value Meridian actually holds, and
model turns are reported beside it, because the map's claim is about cost as much
as correctness — a run that succeeds in eight turns rather than thirty-six has
improved even though both columns say PASS.

One application per worker, so the arms cannot contaminate each other through
hazard switches or posted transfers.

    .venv/bin/python tools/ab_instructions.py [--workers 4] [--tasks 1,4,18]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from ab20 import _turns, configure, gate, start_apps
from ab_twenty import CREDENTIALS, Task, grade, read_values
from tasks100 import TASKS
from understudy.loop.runner import discover
from understudy.sitemap import SiteMap, load as load_map

OUT = Path("evidence/ab-instructions.json")


async def attempt(task: Task, app: str, sitemap: SiteMap | None) -> dict:
    """One run, with or without the map in front of it."""
    configure(task, app)
    started = time.monotonic()
    suffix = "map" if sitemap is not None else "cold"
    capability, log = await discover(
        goal=task.goal.format(**task.params), base_url=app,
        capability_id=f"{task.id}.{suffix}", parameters=task.params,
        outputs=list(task.outputs), output_patterns=task.patterns,
        credentials=CREDENTIALS, knowledge=sitemap,
        policy=gate(task, app, "attended" if task.mutates else "discover"))

    values = read_values(log)
    ok, why = grade(task, values)
    return {"pass": bool(ok and capability is not None), "why": why,
            "finished": capability is not None, "turns": _turns(log),
            "seconds": round(time.monotonic() - started, 1),
            "read": values, "run": log.run_id}


async def worker(app: str, queue: asyncio.Queue, sitemap: SiteMap | None,
                 results: dict, arms: tuple[str, ...]) -> None:
    while True:
        try:
            task = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        row = {"n": task.n, "goal": task.goal.format(**task.params),
               "hazard": ",".join(task.hazards) or "-"}
        try:
            # Cold first, so a shared-state mistake would hurt the map arm rather
            # than flatter it.
            if "without" in arms:
                row["without"] = await attempt(task, app, None)
            if "with" in arms:
                row["with"] = await attempt(task, app, sitemap)
        except Exception as exc:
            row["error"] = str(exc)
            row.setdefault("without", {"pass": False, "turns": 0, "seconds": 0,
                                       "why": f"harness error: {exc}"})
            row.setdefault("with", {"pass": False, "turns": 0, "seconds": 0,
                                    "why": f"harness error: {exc}"})
        results[task.n] = row
        parts = []
        for arm in arms:
            a = row.get(arm) or {}
            parts.append(f"{arm} {'PASS' if a.get('pass') else 'fail':<4} "
                         f"{a.get('turns', 0):>3} turns {a.get('seconds', 0):>5.0f}s")
        print(f"  {task.n:>3}. " + "    ".join(parts), flush=True)
        OUT.write_text(json.dumps([results[k] for k in sorted(results)],
                                  indent=2, default=str))


def report(rows: list[dict], elapsed: float, arms=("without", "with")) -> None:
    """One row per task, one column per arm that was run.

    Written to handle a single arm because the arms run separately: the baseline
    needs no map, so it can be measured while the map is still being built.
    """
    rows = [r for r in rows if all(r.get(a) for a in arms)]
    if not rows:
        print("\nno rows completed the requested arms")
        return

    label = {"without": "WITHOUT MAP", "with": "WITH MAP"}

    def cell(arm: dict) -> str:
        return f"{'PASS' if arm['pass'] else 'fail'}  {arm['turns']:>3} turns"

    print(f"\n{'=' * 96}")
    print(f"{'#':>4}  {'task':<40} {'hazard':<12} "
          + " ".join(f"{label[a]:<17}" for a in arms))
    print("-" * 96)
    for r in rows:
        print(f"{r['n']:>4}  {r['goal'][:39]:<40} {r['hazard']:<12} "
              + " ".join(f"{cell(r[a]):<17}" for a in arms))
    print("-" * 96)

    n = len(rows)
    stats = {a: {"pass": sum(r[a]["pass"] for r in rows),
                 "turns": sum(r[a]["turns"] for r in rows),
                 "secs": sum(r[a]["seconds"] for r in rows)} for a in arms}

    print()
    for a in arms:
        st = stats[a]
        print(f"  {label[a]:<13} {st['pass']:>3}/{n} correct · {st['turns']:>5} LLM turns "
              f"({st['turns'] / n:.1f} per task) · {st['secs'] / n:.0f}s per task")

    if len(arms) == 2 and stats["without"]["turns"]:
        w, m = stats["without"], stats["with"]
        saved = w["turns"] - m["turns"]
        print(f"\n  turns {-saved / w['turns']:+.0%}  ({saved:+d} over {n} tasks, "
              f"{saved / n:+.1f} per task)")
        print(f"  time  {(m['secs'] - w['secs']) / w['secs']:+.0%}")
        print(f"  correct {m['pass'] - w['pass']:+d} of {n}")

    print(f"\n  wall clock {elapsed / 60:.0f} min · written to {OUT}")


async def main(workers: int, which: list[int] | None, arms: tuple[str, ...],
               out: Path) -> int:
    global OUT
    OUT = out

    sitemap = None
    if "with" in arms:
        sitemap = load_map("http://localhost:8090")
        if sitemap is None or not sitemap.screens:
            print("no site map on disk — run tools/map_app.py first")
            return 1
        print(f"  map: {sitemap.summary()}")

    tasks = [t for t in TASKS if which is None or t.n in which]
    apps = start_apps(min(workers, len(tasks)))

    # The map was built against one port and the workers run on others. Its steps
    # are recorded relative, so only the declared base needs rewriting.
    per_worker = []
    for app in apps:
        if sitemap is None:
            per_worker.append(None)
            continue
        copy = sitemap.model_copy(deep=True)
        copy.app = app
        per_worker.append(copy)

    queue: asyncio.Queue = asyncio.Queue()
    # Slowest first: the mutating and hazarded tasks take minutes and the plain
    # reads take under a minute, so starting the long ones early stops a worker
    # being left holding one after everything else has finished.
    for task in sorted(tasks, key=lambda t: (not t.mutates, not t.hazards, t.n)):
        queue.put_nowait(task)

    results: dict[int, dict] = {}
    started = time.monotonic()
    await asyncio.gather(*(worker(apps[i], queue, per_worker[i], results, arms)
                           for i in range(len(apps))))
    report([results[k] for k in sorted(results)], time.monotonic() - started, arms)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--tasks")
    p.add_argument("--arms", default="without,with",
                   help="which arms to run: 'without', 'with', or both")
    p.add_argument("--out", default="evidence/ab-instructions.json")
    a = p.parse_args()
    sys.exit(asyncio.run(main(
        a.workers,
        [int(n) for n in a.tasks.split(",")] if a.tasks else None,
        tuple(x.strip() for x in a.arms.split(",")),
        Path(a.out))))
