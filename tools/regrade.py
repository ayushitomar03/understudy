"""Regrade both arms of the 20-task run under one rule, without paying twice.

The first report graded the baseline on stored values with a corrected rule and
the loop arm on the rule that ran during the measurement. Same tasks, two
graders, and the loop penalised — which is not a result. This puts both arms
under the rule in ab_twenty.grade().

The baseline is regraded from the values it recorded, so it costs nothing. The
loop arm has to be replayed, which also costs nothing: replay makes no model
calls. Only the tasks where replay is unreliable pay for a repair.

    .venv/bin/python tools/regrade.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ab_twenty import (APP, B_ROUNDS, B_SAMPLES, CREDENTIALS, RELIABLE, TASKS,
                       configure, grade, measure_b)
from understudy.artifact.schema import Capability
from understudy.repair import repair

OUT = Path("evidence/ab-twenty-regraded.json")


def capabilities_for(task) -> list[Capability]:
    """Every capability today's run discovered for this task, oldest first."""
    found = []
    for path in sorted(Path("evidence/runs").glob("discovery-*/capability.json")):
        try:
            cap = Capability.load(path.read_text())
        except Exception:
            continue
        if cap.id.split(".")[0] == task.id:
            found.append((path.stat().st_mtime, cap))
    return [cap for _, cap in sorted(found)]


def baseline(task, stored: dict) -> dict:
    """The first LLM attempt, regraded from what it read."""
    first = stored["a"]["attempts"][0]
    ok, why = grade(task, first.get("values") or {})
    return {"pass": bool(ok and first["finished"]), "why": why,
            "finished": first["finished"], "turns": first["turns"],
            "seconds": first["seconds"], "read": first.get("values") or {}}


async def loop_arm(task) -> dict:
    caps = capabilities_for(task)
    if not caps:
        return {"pass": False, "why": "no capability was ever discovered",
                "correct": 0, "of": B_SAMPLES, "version": 0, "rounds": []}

    capability = caps[0]
    before = await measure_b(task, capability, B_SAMPLES)
    rounds, current, after = [], capability, before

    if before["rate"] < RELIABLE:
        current, report = await repair(
            capability, task.params, CREDENTIALS, samples=5, rounds=B_ROUNDS,
            policy=task.policy("attended" if task.mutates else "unattended"))
        rounds = [{"round": r.n, "cause": r.cause, "change": r.change,
                   "kept": r.kept, "statement": r.statement[:200]}
                  for r in report.rounds]
        if current.version != capability.version:
            after = await measure_b(task, current, B_SAMPLES)

    return {"pass": after["correct"] >= 9, "correct": after["correct"],
            "of": after["of"], "version": current.version, "rounds": rounds,
            "why": "; ".join(after["why"]) or "clean",
            "seconds": after["seconds_each"]}


async def main() -> int:
    stored = {r["task"]: r for r in json.load(open("evidence/ab-twenty.json"))}
    rows = []

    for task in TASKS:
        if task.n not in stored:
            continue
        configure(task)
        b = baseline(task, stored[task.n])
        l = await loop_arm(task)
        rows.append({"n": task.n, "goal": task.goal.format(**task.params),
                     "hazard": ",".join(task.hazards) or "-",
                     "baseline": b, "loop": l})
        print(f"  {task.n:>2}. baseline {'PASS' if b['pass'] else 'fail'} "
              f"({b['turns']} turns)   loop {'PASS' if l['pass'] else 'fail'} "
              f"{l['correct']}/{l['of']} v{l['version']}   {l['why'][:44]}", flush=True)
        OUT.write_text(json.dumps(rows, indent=2, default=str))

    configure(TASKS[0])
    print(f"\n{'#':>3}  {'task':<46} {'hazard':<13} {'BASELINE':<9} {'LOOP':<12}")
    print("-" * 92)
    for r in rows:
        print(f"{r['n']:>3}  {r['goal'][:45]:<46} {r['hazard']:<13} "
              f"{'PASS' if r['baseline']['pass'] else 'fail':<9} "
              f"{('PASS ' if r['loop']['pass'] else 'fail ') + str(r['loop']['correct']) + '/' + str(r['loop']['of']):<12}")
    print("-" * 92)
    bp = sum(r["baseline"]["pass"] for r in rows)
    lp = sum(r["loop"]["pass"] for r in rows)
    turns = sum(r["baseline"]["turns"] for r in rows)
    print(f"{'':>3}  {'TOTAL':<46} {'':<13} {bp}/{len(rows):<7} {lp}/{len(rows)}")
    print(f"\nbaseline: {turns} model turns for {len(rows)} executions "
          f"({turns / len(rows):.0f} per execution, every execution)")
    print(f"loop:     0 model turns per execution after the recording")
    print(f"written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
