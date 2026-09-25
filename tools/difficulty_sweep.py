"""Does the repair loop fix a flow, and does it still work when the app is worse?

Three difficulty levels on one application, each one measured properly. The
earlier runs used five samples, where a genuinely 40%-reliable flow measured
anywhere from 0% to 60% — differences inside that band were being read as
improvement. Fifty samples puts the noise band near +-7%, which is narrow
enough for the numbers to mean something.

Difficulty is the rate at which Meridian interrupts a member lookup with a
maintenance notice. At 0.2 most runs never meet it; at 0.8 most do, and a flow
that handles it once is not enough because acknowledging returns to the same
request, which can interrupt again.

What is recorded per level: the starting reliability, the reliability after
each repair round, and what the loop changed. Three rounds, because a loop that
cannot fix something in three has not understood it and should be asking a
person instead of grinding.

    .venv/bin/python tools/difficulty_sweep.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

from understudy import knowledge as knowledge_store
from understudy.artifact.schema import Capability
from understudy.repair import _measure, repair

APP = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}
PARAMS = {"member_no": "40021"}
BASE = Path("capabilities/sanity.meridian.json")

SAMPLES = 50
ROUNDS = 3
LEVELS = [
    (0.2, "mild — most runs never meet the notice"),
    (0.5, "even — half of them do"),
    (0.8, "harsh — most do, and it recurs after acknowledging"),
]

OUT = Path("evidence/difficulty-sweep.json")


def set_difficulty(odds: float) -> None:
    urllib.request.urlopen(f"{APP}/admin/interstitial?odds={odds}").read()


async def level(odds: float, note: str) -> dict:
    """One difficulty level, from scratch: forget what was learned, measure the
    unrepaired flow, then repair it."""
    store = knowledge_store.STORE / f"{knowledge_store._slug(APP)}.json"
    store.unlink(missing_ok=True)      # each level starts knowing nothing
    set_difficulty(odds)

    capability = Capability.load(BASE.read_text())
    print(f"\n{'=' * 70}\ninterstitial {odds:.0%}  —  {note}\n{'=' * 70}", flush=True)

    rounds: list[dict] = []

    def record(entry, after):
        rounds.append({"round": entry.n, "version": entry.version,
                       "before": entry.reliability, "after": after,
                       "cause": entry.cause, "change": entry.change,
                       "kept": entry.kept})
        print(f"  round {entry.n}: v{entry.version} "
              f"{entry.passed}/{entry.of} = {entry.reliability:.0%} -> {after:.0%}  "
              f"{'KEPT' if entry.kept else 'rolled back'}\n"
              f"      {entry.cause}: {entry.change}", flush=True)

    fixed, report = await repair(capability, PARAMS, CREDENTIALS,
                                samples=SAMPLES, rounds=ROUNDS, on_round=record)

    # Grade the finished flow on a fresh sample rather than reusing the
    # measurement the loop stopped on — the loop chose to stop there, which is
    # exactly the kind of selection that flatters a number.
    from understudy.evidence import EventLog
    final, _ = await _measure(fixed, PARAMS, CREDENTIALS, SAMPLES,
                              EventLog("sweep", f"final at {odds}"))

    start = report.rounds[0].reliability if report.rounds else 0.0
    print(f"\n  start {start:.0%}  ->  final {final:.0%}   "
          f"(v{fixed.version}, {len(fixed.recoveries)} recoveries)", flush=True)
    if report.escalated:
        print(f"  escalated: {report.escalated[:140]}", flush=True)

    return {"odds": odds, "note": note, "start": start, "final": final,
            "version": fixed.version, "rounds": rounds,
            "escalated": report.escalated,
            "recoveries": [r.detect for r in fixed.recoveries]}


async def main() -> int:
    results = []
    try:
        for odds, note in LEVELS:
            results.append(await level(odds, note))
    finally:
        set_difficulty(0)   # leave the app deterministic for the test suite
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(results, indent=2))

    print(f"\n{'=' * 70}")
    print(f"{'difficulty':<14}{'start':>9}{'final':>9}{'rounds':>9}  what it did")
    print("-" * 70)
    for r in results:
        kept = sum(1 for x in r["rounds"] if x["kept"])
        print(f"{r['odds']:<14.0%}{r['start']:>9.0%}{r['final']:>9.0%}"
              f"{len(r['rounds']):>9}  {kept} change(s) kept")
    print(f"\n{SAMPLES} samples per measurement · written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
