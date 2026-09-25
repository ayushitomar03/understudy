"""Twenty cases across every condition Meridian can produce, through the loop as it is.

The repair loop has only ever met one condition — an interruption — nineteen
times. This runs the other kinds past it and records what it does, before
anything new is built, because the interesting question is not whether it can
fix things. It is whether it knows when *not* to.

Three of these conditions must not be repaired at all:

    not found      the application answered correctly. Returning that answer is
                   the job; changing the flow would be changing a correct flow.
    invalid input  the caller sent something the field will not take. Nothing
                   about the flow is wrong.
    not authorised the flow is right and the operator is not allowed. A person
                   has to decide, and retrying is how you lock an account.

So a loop that eagerly repairs everything scores badly here, and one that
diagnoses accurately and then declines to act scores well. Each case records
what replay reported, what the diagnosis said, what would have been changed,
and whether that was the right call.

    .venv/bin/python tools/condition_matrix.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

from understudy.artifact.schema import Capability
from understudy.evidence import EventLog
from understudy.policy import for_app
from understudy.propose import diagnose, grounded, propose
from understudy.replay import Replayer
from understudy.surface.web import WebSurface

APP = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}
CAPABILITY = Path("capabilities/sanity.meridian.json")
OUT = Path("evidence/condition-matrix.json")

# What each condition should produce. `repair` says whether changing the flow is
# the right response — for three of these it is not, and acting would be wrong.
CASES = [
    # the flow working, as a control
    *[{"case": f"happy/{m}", "params": {"member_no": m}, "setup": None,
       "want_outcome": "complete", "repair": False,
       "why": "nothing is wrong"} for m in ("40021", "40055", "40204", "40021")],

    # the application answering correctly that there is no such record
    *[{"case": f"not_found/{m}", "params": {"member_no": m}, "setup": None,
       "want_outcome": "unreachable", "repair": False,
       "why": "a legitimate answer the caller needs, not a defect"}
      for m in ("40999", "50000", "99999", "40000")],

    # the caller sending something the field will not accept
    *[{"case": f"invalid_input/{v!r}", "params": {"member_no": v}, "setup": None,
       "want_outcome": "incomplete_action", "repair": False,
       "why": "the caller's input is wrong, not the flow"}
      for v in ("ABC", "40-021", "", "4002X")],

    # a record this operator may not open
    *[{"case": "not_authorised/40113", "params": {"member_no": "40113"}, "setup": None,
       "want_outcome": "incomplete_action", "repair": False,
       "why": "the flow is right and the operator is not allowed; a person decides"}
      for _ in range(3)],

    # the session dying mid-flow
    *[{"case": "session_expired", "params": {"member_no": "40021"}, "setup": "expire",
       "want_outcome": "incomplete_action", "repair": True,
       "why": "re-authenticate and carry on — the flow is fine"} for _ in range(3)],

    # an interruption standing in the way
    *[{"case": f"interstitial/{int(o*100)}%", "params": {"member_no": "40021"},
       "setup": ("interstitial", o), "want_outcome": "incomplete_action", "repair": True,
       "why": "dismiss it and carry on"} for o in (1.0, 1.0, 1.0, 1.0)],

    # the application itself refusing
    {"case": "unknown_member_shape", "params": {"member_no": "0"}, "setup": None,
     "want_outcome": "unreachable", "repair": False,
     "why": "no such record"},
    {"case": "interstitial/100% again", "params": {"member_no": "40055"},
     "setup": ("interstitial", 1.0), "want_outcome": "incomplete_action", "repair": True,
     "why": "dismiss it and carry on"},
]


def configure(setup) -> None:
    """Put the application into the state this case needs."""
    urllib.request.urlopen(f"{APP}/admin/interstitial?odds=0").read()
    if setup == "expire":
        urllib.request.urlopen(f"{APP}/admin/expire").read()
    elif isinstance(setup, tuple) and setup[0] == "interstitial":
        urllib.request.urlopen(f"{APP}/admin/interstitial?odds={setup[1]}").read()


def run_once(capability: Capability, params: dict, log: EventLog):
    surface = WebSurface()
    try:
        policy = for_app(capability.app.base_url, mode="unattended")
        return Replayer(capability, surface, log, policy).run(params, secrets=CREDENTIALS)
    finally:
        surface.close()


async def main() -> int:
    capability = Capability.load(CAPABILITY.read_text())
    rows = []

    for n, case in enumerate(CASES, 1):
        configure(case["setup"])
        log = EventLog("matrix", case["case"])
        result = await asyncio.to_thread(run_once, capability, case["params"], log)

        row = {
            "n": n, "case": case["case"], "want_outcome": case["want_outcome"],
            "should_repair": case["repair"], "why": case["why"],
            "outcome": result.outcome, "subclass": result.subclass,
            "classified_right": result.outcome == case["want_outcome"],
            "cause": None, "proposed": None, "acted": None, "acted_right": None,
        }

        if result.outcome != "complete":
            d = await diagnose(capability, result)
            row["cause"] = d.cause
            if d.actionable:
                i = await propose(d, capability, result.failure_tree)
                ungrounded = grounded(i, result.failure_tree)
                row["proposed"] = f"{i.kind}: {i.describe()}" + (
                    f"  [rejected: {ungrounded[:40]}]" if ungrounded else "")
                row["acted"] = i.kind != "none" and not ungrounded
            else:
                row["proposed"] = f"declined ({d.cause})"
                row["acted"] = False
        else:
            row["acted"] = False

        row["acted_right"] = row["acted"] == case["repair"]
        rows.append(row)
        print(f"  {n:>2}. {case['case']:<24} {result.outcome}/{result.subclass or '-':<24}"
              f" cause={row['cause'] or '-':<18}"
              f" {'ok' if row['acted_right'] else 'WRONG ACTION'}", flush=True)

    configure(None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=2))

    print(f"\n{'=' * 78}")
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["case"].split("/")[0], []).append(r)
    print(f"{'condition':<22}{'n':>3}{'classified':>13}{'acted correctly':>18}")
    print("-" * 78)
    for name, group in groups.items():
        cls = sum(1 for r in group if r["classified_right"])
        act = sum(1 for r in group if r["acted_right"])
        print(f"{name:<22}{len(group):>3}{cls:>9}/{len(group):<3}{act:>14}/{len(group):<3}")
    print("-" * 78)
    print(f"{'TOTAL':<22}{len(rows):>3}"
          f"{sum(1 for r in rows if r['classified_right']):>9}/{len(rows):<3}"
          f"{sum(1 for r in rows if r['acted_right']):>14}/{len(rows):<3}")
    print(f"\nwritten to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
