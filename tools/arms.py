"""The same hundred tasks at three prices.

The question this harness exists to answer is not "can the system do the task" —
the model arm can already do most of them — but *what each one costs*, and how
that cost moves when the system is allowed to remember things.

    arm A — no map, no recordings.   Every task starts knowing nothing about the
                                     application: find the sign-on, work out the
                                     screens, read the answer. This is what a
                                     computer-use agent costs with no system
                                     around it, and it is the number everything
                                     else is measured against.

    arm B — the map.                 The application was surveyed once, by one
                                     model pass, into a verified map: screens,
                                     controls that resolved, what the app's
                                     messages mean. Every task is handed it. The
                                     model still runs every task; it should just
                                     need far fewer turns to do it.

    arm C — the map and recordings.  As B, plus: the first success on a task
                                     *shape* is recorded, and a later task of the
                                     same shape replays that recording with no
                                     model at all. 62 of the hundred tasks share
                                     a shape with another, across 20 shapes, so
                                     at most 42 can come back free — that ceiling
                                     is a property of the task set and is
                                     reported next to the result.

Three things this deliberately does:

  * **Grades the answer, not the run.** Every arm is scored against the value
    Meridian actually holds. A replay that completes having read the wrong cell
    is a failure here, which is the only way a cost saving can be trusted.

  * **Keeps a shape on one worker.** Tasks are dispatched in shape groups, in
    task order, so the first run of a shape always precedes its repeats. A cache
    measured with the repeats racing the recording measures scheduling.

  * **Never lets a saving cost a success.** A recording that fails replay is
    dropped and the model runs the task anyway, so arm C's success rate cannot
    fall below arm B's by more than the noise in the hazards.

    .venv/bin/python tools/arms.py --pilot 12                  # a stratified slice
    .venv/bin/python tools/arms.py --arms A,B,C --workers 4    # all hundred
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from bench import configure, gate, grade, start_apps    # noqa: E402
from tasks100 import TASKS, tier                       # noqa: E402
from tasks_holdout import TASKS as HELD_OUT             # noqa: E402

from understudy import cache                           # noqa: E402
from understudy.evidence import EventLog               # noqa: E402
from understudy.loop import runner as runner_module    # noqa: E402
from understudy.sitemap import load as load_map        # noqa: E402
from understudy.solve import solve                     # noqa: E402

MAPPED = "http://localhost:8090"      # the app the map was built against
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}
OUT = Path("evidence/arms.json")
HELD_OUT_OUT = Path("evidence/arms-holdout.json")
CACHE = Path("evidence/cache")

# A task that asks for a person and does not get one should cost seconds here,
# not the ten minutes a real operator is given. Harness override only.
runner_module.ESCALATION_TIMEOUT_S = 45

ARMS = {
    "A": {"use_map": False, "use_cache": False},
    "B": {"use_map": True, "use_cache": False},
    "C": {"use_map": True, "use_cache": True},
    # What the system can do on its own: a recording of this shape, else a plan
    # synthesised from the map, else nothing. No model is called at any point,
    # so this arm's whole cost is wall-clock. It runs on whatever the earlier
    # arms recorded, which is the situation it is meant to describe — a system
    # that has worked this application before.
    "D": {"use_map": True, "use_cache": True, "use_planner": True, "model": False},
}


def shape_of(task) -> str:
    """The task's shape, keyed without an app so grouping survives the port."""
    return cache.shape(task.goal.format(**task.params), task.params, app="")


def groups(tasks: list) -> list[list]:
    """Tasks gathered by shape, each group in task order, groups in first-seen order."""
    by_shape: dict[str, list] = defaultdict(list)
    for task in tasks:
        by_shape[shape_of(task)].append(task)
    return [sorted(g, key=lambda t: t.n) for g in by_shape.values()]


def pilot(n: int) -> list:
    """A slice of the task set that is still representative and can still hit.

    Round-robin across the difficulty tiers rather than down the list — the
    first twenty tasks are the four easiest tiers and would flatter every arm —
    and within a tier, shapes that repeat come first so the cache has something
    to hit. At most three of any one shape: ten samples of "report the savings
    balance" measures one task ten times.
    """
    per_shape = 3
    members: Counter = Counter(shape_of(t) for t in TASKS)
    pools: dict[str, list] = defaultdict(list)
    for task in sorted(TASKS, key=lambda t: (-members[shape_of(t)], t.n)):
        pools[tier(task)].append(task)

    chosen: list = []
    taken: Counter = Counter()
    while len(chosen) < n and any(pools.values()):
        for t in sorted(pools):
            pool = pools[t]
            while pool:
                task = pool.pop(0)
                if taken[shape_of(task)] < per_shape:
                    chosen.append(task)
                    taken[shape_of(task)] += 1
                    break
            if len(chosen) >= n:
                break
    return sorted(chosen, key=lambda t: t.n)


# ------------------------------------------------------------- one task ----


async def run_one(task, arm: str, sitemap, app: str) -> dict:
    """One task, one arm, one attempt. Graded on the answer."""
    configure(task, app)
    log = EventLog(f"arm{arm}", task.id)
    started = time.monotonic()
    goal = task.goal.format(**task.params)

    try:
        solution = await solve(
            sitemap, goal, task.params, list(task.outputs),
            capability_id=task.id, credentials=CREDENTIALS, patterns=task.patterns,
            policy=gate(task, app, "attended" if task.mutates else "unattended"),
            model_policy=gate(task, app, "attended" if task.mutates else "discover"),
            log=log, **ARMS[arm])
    except Exception as exc:                       # a crashed arm is a failed task
        return {"arm": arm, "n": task.n, "tier": tier(task), "shape": shape_of(task),
                "how": "error", "pass": False, "why": f"error: {exc}"[:120],
                "inferences": 0, "cost_usd": 0.0,
                "seconds": round(time.monotonic() - started, 1), "free": False}

    ok, why = grade(task, solution.outputs)
    return {
        "arm": arm, "n": task.n, "tier": tier(task), "shape": shape_of(task),
        "how": solution.how, "pass": bool(ok), "why": why,
        "inferences": solution.inferences, "cost_usd": round(solution.cost_usd, 4),
        "seconds": round(time.monotonic() - started, 1), "free": solution.free,
    }


async def run_arm(arm: str, tasks: list, apps: list[str], rows: list) -> None:
    """One arm over every task, shape groups in parallel across the app instances."""
    if arm == "C":                       # each arm starts with nothing remembered
        shutil.rmtree(CACHE, ignore_errors=True)
    # D deliberately does not: it is measured on what the system already knows.

    queue: asyncio.Queue = asyncio.Queue()
    for group in groups(tasks):
        queue.put_nowait(group)

    async def worker(app: str) -> None:
        sitemap = load_map(MAPPED)
        sitemap = sitemap.model_copy(deep=True)
        sitemap.app = app                # the map is the app's, not the port's
        while True:
            try:
                group = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            for task in group:           # in order: the recording before its repeats
                row = await run_one(task, arm, sitemap, app)
                rows.append(row)
                mark = "free" if row["free"] else f"{row['inferences']:>3} calls"
                print(f"  {arm} {row['n']:>3} [{row['tier']:<4}] "
                      f"{'PASS' if row['pass'] else 'fail':<4} {mark:>9} "
                      f"${row['cost_usd']:<7.4f} {row['seconds']:>6.1f}s  "
                      f"{row['how']:<7} {row['why'][:38]}", flush=True)
                OUT.write_text(json.dumps(rows, indent=2))

    await asyncio.gather(*(worker(app) for app in apps))


# --------------------------------------------------------------- report ----


def report(rows: list[dict], tasks: list) -> None:
    ceiling = sum(len(g) - 1 for g in groups(tasks))
    print(f"\n  {len(tasks)} tasks · {len(groups(tasks))} shapes · "
          f"at most {ceiling} can ever come back free\n")
    print(f"  {'arm':<4} {'correct':>9} {'model calls':>12} {'cost':>9} "
          f"{'free':>6} {'s/task':>8}")
    for arm in ARMS:
        got = [r for r in rows if r["arm"] == arm]
        if not got:
            continue
        calls = sum(r["inferences"] for r in got)
        cost = sum(r["cost_usd"] for r in got)
        free = sum(1 for r in got if r["free"])
        print(f"  {arm:<4} {sum(r['pass'] for r in got):>4}/{len(got):<4} "
              f"{calls:>12} {cost:>8.2f}$ {free:>6} "
              f"{sum(r['seconds'] for r in got) / len(got):>7.1f}s")

    print(f"\n  by tier (correct / of):")
    tiers = sorted({r["tier"] for r in rows})
    print("       " + "".join(f"{t:>8}" for t in tiers))
    for arm in ARMS:
        got = [r for r in rows if r["arm"] == arm]
        if not got:
            continue
        cells = []
        for t in tiers:
            here = [r for r in got if r["tier"] == t]
            cells.append(f"{sum(r['pass'] for r in here)}/{len(here)}" if here else "-")
        print(f"  {arm:<4} " + "".join(f"{c:>8}" for c in cells))


async def main(which_arms: list[str], tasks: list, workers: int) -> int:
    if load_map(MAPPED) is None:
        print("no map on disk — build one first (tools/map_app.py)")
        return 1

    print(f"\n  {len(tasks)} tasks · arms {','.join(which_arms)} · {workers} workers")
    apps = start_apps(workers)
    # Keep arms measured in earlier invocations: a run of one arm is a run of
    # one arm, not a reason to lose the other three.
    rows: list[dict] = []
    if OUT.exists():
        kept = [r for r in json.loads(OUT.read_text()) if r["arm"] not in which_arms]
        rows.extend(kept)
        if kept:
            print(f"  keeping {len(kept)} rows from arms "
                  f"{','.join(sorted({r['arm'] for r in kept}))}")
    started = time.monotonic()

    for arm in which_arms:
        print(f"\n  === arm {arm} "
              f"{'(no map, no recordings)' if arm == 'A' else ''}"
              f"{'(the map)' if arm == 'B' else ''}"
              f"{'(the map and recordings)' if arm == 'C' else ''}"
              f"{'(no model at all)' if arm == 'D' else ''}")
        await run_arm(arm, tasks, apps, rows)

    report(rows, tasks)
    print(f"\n  {round(time.monotonic() - started)}s wall clock · {OUT}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--tasks", default="", help="comma-separated task numbers")
    parser.add_argument("--pilot", type=int, default=0, help="a stratified slice of N")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--set", default="trained", choices=("trained", "holdout"),
                        help="holdout: tasks the recordings were never made from")
    args = parser.parse_args()

    pool = TASKS if args.set == "trained" else HELD_OUT
    if args.set == "holdout":
        OUT = HELD_OUT_OUT

    if args.tasks:
        wanted = {int(n) for n in args.tasks.split(",")}
        selected = [t for t in pool if t.n in wanted]
    elif args.pilot:
        selected = pilot(args.pilot)
    else:
        selected = list(pool)

    raise SystemExit(asyncio.run(main(
        [a.strip().upper() for a in args.arms.split(",") if a.strip()],
        selected, args.workers)))
