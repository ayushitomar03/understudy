"""Twenty different tasks, two ways: a model every time, or a model once.

The question is narrow and the comparison is the whole point, so it is worth
saying exactly what each arm is.

    arm A — a model every time.   Each execution is a fresh autonomous attempt:
                                  observe, decide, act, read the answer. Nothing
                                  is remembered between executions. This is what
                                  you have if you skip the artifact.

    arm B — a model once.         The first attempt is recorded as a capability.
                                  Every execution after that replays it with no
                                  model in the decision loop, and if replay is
                                  unreliable the repair loop changes the artifact
                                  and measures whether that helped.

Arm B's first attempt *is* arm A's first sample. The arms share it and diverge
afterwards, which keeps the comparison honest about where arm B's cost goes: it
pays the same price as arm A once, and then stops paying.

Two things this deliberately measures that a pass rate alone would hide:

  * **Whether the answer was right, not whether the run finished.** A replay can
    report `complete` having read the wrong cell. Every sample here is graded
    against the value Meridian actually holds, so "it ran" and "it was correct"
    are separate columns.

  * **What it cost.** Model turns per execution, which is the number arm B
    exists to change.

The twenty tasks are all *achievable* — the correct outcome is a completed flow
returning a value. Tasks whose correct answer is a refusal (a restricted record,
no such member, a non-numeric member number) are measured separately in
tools/condition_matrix.py: mixing them in here would average "did it succeed"
together with "did it correctly decline", which are opposite questions.

Difficulty comes from the hazards, because a legacy application does these things
and an application with all of them switched off is not the thing being tested.

    .venv/bin/python tools/ab_twenty.py            # everything
    .venv/bin/python tools/ab_twenty.py --tasks 1,4,18
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from understudy.artifact.schema import Capability
from understudy.loop import runner as runner_module
from understudy.evidence import EventLog
from understudy.loop.runner import discover
from understudy.policy import for_app
from understudy.replay import Replayer
from understudy.repair import improved, repair
from understudy.surface.web import WebSurface

APP = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}

# A run that asks for a human and does not get one should cost seconds, not the
# ten minutes an operator is really given. This is a harness override, not a
# change to the system: ESCALATION_TIMEOUT_S stays 600 everywhere else.
runner_module.ESCALATION_TIMEOUT_S = 45

A_SAMPLES = 3       # a model every time, and each one costs a discovery
B_SAMPLES = 10      # replays are free, so measure them properly
B_ROUNDS = 3
RELIABLE = 0.9

OUT = Path("evidence/ab-twenty.json")


@dataclass
class Task:
    n: int
    goal: str
    outputs: dict[str, str]                      # name -> expected value
    params: dict[str, str] = field(default_factory=dict)
    hazards: dict[str, float | bool] = field(default_factory=dict)
    patterns: dict[str, str] = field(default_factory=dict)
    mutates: bool = False
    note: str = ""

    def policy(self, mode: str):
        """The gate this task runs under.

        A flow that posts a transfer is refused by default and asks for a person,
        which is the property worth having and which the first run of this harness
        demonstrated by escalating three times. Measuring those flows at all
        therefore needs an opt-in, so the opt-in is explicit, scoped to the four
        tasks that move money, and recorded in the result: fake money, on a fake
        bank, on localhost, for a run the operator asked for.

        Note what this does *not* do: it does not touch `classify`, so the action
        is still recognised as irreversible and still recorded as such. The opt-in
        changes who may authorise it, not what it is.
        """
        return for_app(APP, mode=mode, allow_irreversible=self.mutates)

    @property
    def id(self) -> str:
        return f"ab{self.n:02d}"


# Ground truth is Meridian's opening position, which /admin/reset restores.
TASKS = [
    # -- reading one field off a record ------------------------------------
    Task(1, "Look up member {member_no} and report the savings balance",
         {"savings_balance": "4,210.55"}, {"member_no": "40021"}),
    Task(2, "Look up member {member_no} and report the checking balance",
         {"checking_balance": "0.00"}, {"member_no": "40055"}),
    Task(3, "Look up member {member_no} and report the account status",
         {"status": "DORMANT"}, {"member_no": "40204"}),
    Task(4, "Look up member {member_no} and report which branch holds the record",
         {"branch": "NORTHGATE"}, {"member_no": "40055"},
         hazards={"interstitial": 0.7},
         note="a notice stands in front of the record most times, and acknowledging "
              "it returns to the same request, which can raise it again"),
    Task(5, "Look up member {member_no} and report the member's name",
         {"member_name": "R. ACHTERBERG"}, {"member_no": "40021"},
         hazards={"interstitial": 0.5},
         note="the notice appears about half the time, so one sample proves nothing"),
    Task(6, "Look up member {member_no} and report the savings balance",
         {"savings_balance": "0.00"}, {"member_no": "40204"},
         hazards={"lock": True},
         note="the record is held by another terminal and frees itself; nothing to click"),
    Task(7, "Look up member {member_no} and report the checking balance",
         {"checking_balance": "812.03"}, {"member_no": "40021"},
         hazards={"lock": True}, note="the same, on a different field"),
    Task(8, "Look up member {member_no} and report the account status",
         {"status": "ACTIVE"}, {"member_no": "40055"},
         hazards={"slow": True}, note="a screen slower than the settle wait"),

    # -- reading the contact screen ----------------------------------------
    Task(9, "Report the telephone number on file for member {member_no}",
         {"telephone": "0114 496 0021"}, {"member_no": "40021"}),
    Task(10, "Report the post code on file for member {member_no}",
         {"post_code": "S1 055"}, {"member_no": "40055"}),
    Task(11, "Report the e-mail address on file for member {member_no}",
         {"email": "member40204@firstvalley.test"}, {"member_no": "40204"},
         hazards={"interstitial": 0.7},
         note="the notice is on the way to the contact screen, not on it"),

    # -- the posted items list ---------------------------------------------
    Task(12, "Report how many items are posted for member {member_no}",
         {"item_count": "3"}, {"member_no": "40021"}),
    Task(13, "Report the amount of the most recent posted item for member {member_no}",
         {"latest_amount": "1,200.00"}, {"member_no": "40021"}),
    Task(14, "Report the date of the oldest posted item for member {member_no}",
         {"oldest_date": "02/09"}, {"member_no": "40021"},
         hazards={"paging": True},
         note="the answer is on the second screen, behind a Next key"),
    Task(15, "Report how many items are posted for member {member_no}",
         {"item_count": "1"}, {"member_no": "40055"},
         hazards={"slow": True}, note="a slow screen carrying a list"),
    Task(16, "Confirm whether member {member_no} has any posted items and report what the "
             "screen says",
         {"items_message": "NO ITEMS POSTED IN PERIOD."}, {"member_no": "40204"},
         note="an empty result that is a real answer, not a failure"),

    # -- flows that change something ---------------------------------------
    Task(17, "Transfer {amount} from savings to checking for member {member_no} and report "
             "the transaction reference",
         {"reference": ""}, {"member_no": "40021", "amount": "10.00"},
         patterns={"reference": r"8841-\d{4}"}, mutates=True,
         note="the reference is different every time, so it is graded by shape"),
    Task(18, "Transfer {amount} from savings to checking for member {member_no} and report "
             "the new savings balance",
         {"new_savings": "123.00"}, {"member_no": "40055", "amount": "5.00"},
         hazards={"confirm": True}, mutates=True,
         note="the first press does not post; the screen it returns carries the same button"),
    Task(19, "Change the telephone number for member {member_no} to {phone} and report the "
             "telephone number the confirmation screen shows",
         {"saved_phone": "0114 496 9999"},
         {"member_no": "40021", "phone": "0114 496 9999"}, mutates=True),
    Task(20, "Change the post code for member {member_no} to {post_code} and report the post "
             "code the confirmation screen shows",
         {"saved_post_code": "S2 7Q"},
         {"member_no": "40055", "post_code": "S2 7QQ"},
         hazards={"truncate": True}, mutates=True,
         note="the field keeps five characters, so the right answer is not what was sent"),
]


# --------------------------------------------------------------- the app ----


def _admin(path: str) -> None:
    urllib.request.urlopen(APP + path).read()


def configure(task: Task) -> None:
    """Opening position, then this task's hazards. Reset restores balances and the
    ledger as well as clearing hazards — without that a transfer in one sample
    decides the expected answer in the next."""
    _admin("/admin/reset")
    for name, value in task.hazards.items():
        if name == "interstitial":
            _admin(f"/admin/interstitial?odds={value}")
        else:
            _admin(f"/admin/hazard?name={name}&on=1")


# --------------------------------------------------------------- grading ----


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def grade(task: Task, got: dict[str, str]) -> tuple[bool, str]:
    """Is this the answer Meridian holds? Separate from whether the run finished.

    The answer counts if it appears in what was read as a value in its own right,
    rather than only when the read isolated it exactly. This is not leniency for
    its own sake: on a screen like

        POSTED ITEMS  Member 40204  NO ITEMS POSTED IN PERIOD.

    there is no cell containing only the message, and on the amended-details screen
    the telephone number only exists inside a sentence. Demanding an exact match
    marked a dozen correct answers wrong, which is a measurement of the grader.

    The bound that keeps it from being meaningless: the expected value must not be
    flanked by digits, letters or decimal punctuation, so "0.00" does not match
    inside "10.00" and "1" does not match inside "128.00".
    """
    for name, expected in task.outputs.items():
        actual = _norm(got.get(name, ""))
        if not actual:
            return False, f"{name} empty"
        if (pattern := task.patterns.get(name)):
            if not re.search(pattern, actual):
                return False, f"{name}={actual!r} does not match {pattern}"
            continue
        want = _norm(expected)
        if want == actual:
            continue
        if not re.search(r"(?<![\w.,])" + re.escape(want) + r"(?![\w.,])", actual):
            return False, f"{name}={actual!r} does not contain {want!r}"
    return True, "correct"


def read_values(log: EventLog) -> dict[str, str]:
    """What the model said the answers were, from its own event log."""
    values: dict[str, str] = {}
    for event in _events(log):
        if event.get("event") == "output_declared":
            values[event["name"]] = event.get("value") or ""
    return values


# ----------------------------------------------------------------- arm A ----


async def arm_a(task: Task, samples: int) -> dict:
    """A model every time. Each sample is a fresh attempt that remembers nothing."""
    attempts = []
    first_capability: Capability | None = None

    for i in range(1, samples + 1):
        configure(task)
        started = time.monotonic()
        capability, log = await discover(
            goal=task.goal.format(**task.params), base_url=APP,
            capability_id=f"{task.id}.s{i}", parameters=task.params,
            outputs=list(task.outputs), output_patterns=task.patterns,
            credentials=CREDENTIALS,
            policy=task.policy("attended" if task.mutates else "discover"))
        elapsed = time.monotonic() - started

        values = read_values(log)
        ok, why = grade(task, values)
        finished = capability is not None
        attempts.append({"n": i, "finished": finished, "correct": ok and finished,
                         "why": why if finished else "did not finish",
                         "turns": _turns(log), "seconds": round(elapsed, 1),
                         "values": values, "run": log.run_id})
        verdict = "correct" if ok and finished else "WRONG"
        reason = why if finished else f"never finished (value was {'right' if ok else 'wrong'})"
        print(f"     A{i}: {verdict:<8} {_turns(log):>3} turns {elapsed:>5.0f}s  {reason}",
              flush=True)

        if first_capability is None and capability is not None and ok:
            first_capability = capability      # arm B starts from here

    correct = sum(1 for a in attempts if a["correct"])
    return {"attempts": attempts, "correct": correct, "of": samples,
            "rate": correct / samples,
            "turns_total": sum(a["turns"] for a in attempts),
            "turns_each": round(sum(a["turns"] for a in attempts) / samples, 1),
            "seconds_each": round(sum(a["seconds"] for a in attempts) / samples, 1),
            "capability": first_capability}


def _turns(log: EventLog) -> int:
    """Model turns: the count the runner itself keeps, which is one per tool call
    the model chose to make. Not a token count — a comparable unit of "the model
    was consulted", which is the quantity arm B exists to change."""
    events = _events(log)
    for event in events:
        if event.get("event") == "run_finished" and event.get("turns") is not None:
            return int(event["turns"])
    # A run that escalated finishes without a turn count, and reporting 0 there
    # says the model was never consulted — the opposite of what happened. Count
    # what the model actually did instead.
    return sum(1 for e in events
               if e.get("event") in ("observation", "output_declared", "param_declared",
                                     "model_said", "control_released"))


def _events(log: EventLog) -> list[dict]:
    out = []
    for line in (log.dir / "events.jsonl").read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------- arm B ----


def replay_once(task: Task, capability: Capability, params: dict[str, str], log: EventLog):
    surface = WebSurface()
    try:
        # Replay of a mutating flow runs attended for the same reason discovery
        # does. Worth naming: the engine gates on `verdict.allowed` and ignores
        # `verdict.requires_human`, so an attended replay posts without asking —
        # which is how this arm can run at all, and is a gap, not a feature.
        return Replayer(capability, surface, log,
                        task.policy("attended" if task.mutates else "unattended")
                        ).run(params, secrets=CREDENTIALS)
    finally:
        surface.close()


async def measure_b(task: Task, capability: Capability, samples: int) -> dict:
    """Replay it, and grade the answer as well as the outcome.

    The two are separated because they disagree: a replay that walks the flow and
    reads the wrong cell reports `complete`, and a pass rate built on `complete`
    would call that a success.
    """
    log = EventLog("ab", f"{task.id} replay v{capability.version}")
    ran = correct = 0
    failure = None
    whys: list[str] = []
    started = time.monotonic()

    for _ in range(samples):
        configure(task)
        result = await asyncio.to_thread(replay_once, task, capability, task.params, log)
        if result.outcome == "complete":
            ran += 1
            ok, why = grade(task, result.outputs or {})
            correct += ok
            if not ok:
                whys.append(why)
        else:
            whys.append(f"{result.outcome}/{result.subclass or '-'}")
            if failure is None:
                failure = result

    return {"ran": ran, "correct": correct, "of": samples,
            "rate": correct / samples, "ran_rate": ran / samples,
            "seconds_each": round((time.monotonic() - started) / samples, 1),
            "why": sorted(set(whys))[:4], "failure": failure}


async def arm_b(task: Task, capability: Capability | None) -> dict:
    """A model once. Replay, and repair the artifact if replay is unreliable."""
    if capability is None:
        return {"discovered": False, "rate": 0.0, "rounds": [], "turns": 0,
                "escalated": "no first attempt succeeded, so there is nothing to replay"}

    before = await measure_b(task, capability, B_SAMPLES)
    print(f"     B : v{capability.version} {before['correct']}/{before['of']} correct"
          f" ({before['ran']}/{before['of']} ran)  {before['seconds_each']}s each"
          f"  {'; '.join(before['why']) or 'clean'}", flush=True)

    rounds: list[dict] = []
    current, after = capability, before

    if before["rate"] < RELIABLE:
        # The repair loop grades on `complete`, which is the wrong bar here, so
        # each round is re-graded on correctness before it is believed.
        def note(entry, measured):
            rounds.append({"round": entry.n, "version": entry.version,
                           "cause": entry.cause, "statement": entry.statement,
                           "change": entry.change, "kept": entry.kept,
                           "loop_before": entry.reliability, "loop_after": measured})
            print(f"       repair {entry.n}: {entry.cause} -> {entry.change[:64]}"
                  f"  {'kept' if entry.kept else 'rolled back'}", flush=True)

        current, report = await repair(capability, task.params, CREDENTIALS,
                                      samples=5, rounds=B_ROUNDS, on_round=note,
                                      policy=task.policy(
                                          "attended" if task.mutates else "unattended"))

        # Every round the loop recorded, including the ones it reached by
        # `continue` and never announced.
        rounds = [{"round": r.n, "version": r.version, "cause": r.cause,
                   "statement": r.statement, "change": r.change, "kept": r.kept,
                   "reliability": r.reliability} for r in report.rounds]
        for r in rounds:
            outcome = ("kept" if r["kept"] else
                       "rolled back" if r["kept"] is False else "no change made")
            print(f"       round {r['round']}: {r['cause'] or '-'} -> "
                  f"{r['change'][:70] or '(nothing proposed)'}  [{outcome}]", flush=True)
        after = await measure_b(task, current, B_SAMPLES)
        print(f"     B': v{current.version} {after['correct']}/{after['of']} correct"
              f"  {'; '.join(after['why']) or 'clean'}", flush=True)
        escalated = report.escalated
    else:
        escalated = ""

    return {"discovered": True, "version": current.version,
            "before": {k: v for k, v in before.items() if k != "failure"},
            "after": {k: v for k, v in after.items() if k != "failure"},
            "rate": after["rate"], "rounds": rounds, "escalated": escalated,
            "recoveries": [r.detect for r in current.recoveries],
            "seconds_each": after["seconds_each"]}


# ------------------------------------------------------------------ main ----


async def main(which: list[int] | None) -> int:
    tasks = [t for t in TASKS if which is None or t.n in which]
    results = []

    for task in tasks:
        print(f"\n{'=' * 78}\n {task.n:>2}. {task.goal.format(**task.params)}", flush=True)
        if task.hazards:
            print(f"     hazard: {task.hazards}  — {task.note}", flush=True)

        a = await arm_a(task, A_SAMPLES)
        b = await arm_b(task, a.pop("capability"))

        results.append({"task": task.n, "goal": task.goal.format(**task.params),
                        "hazards": task.hazards, "note": task.note,
                        "mutates": task.mutates, "a": a, "b": b})
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(results, indent=2, default=str))

    _admin("/admin/reset")
    summarise(results)
    print(f"\nwritten to {OUT}")
    return 0


def summarise(results: list[dict]) -> None:
    print(f"\n{'=' * 90}")
    print(f"{'#':>3} {'task':<34}{'A correct':>11}{'B correct':>11}"
          f"{'A turns':>9}{'B turns':>9}")
    print("-" * 90)
    for r in results:
        a, b = r["a"], r["b"]
        print(f"{r['task']:>3} {r['goal'][:33]:<34}"
              f"{a['correct']}/{a['of']:<9}"
              f"{b.get('after', {}).get('correct', 0)}/{b.get('after', {}).get('of', 0):<9}"
              f"{a['turns_each']:>9}{0:>9}")
    print("-" * 90)
    a_rate = sum(r["a"]["rate"] for r in results) / len(results)
    b_rate = sum(r["b"]["rate"] for r in results) / len(results)
    a_turns = sum(r["a"]["turns_total"] for r in results)
    print(f"    arm A: {a_rate:.0%} correct, {a_turns} model turns over "
          f"{len(results) * A_SAMPLES} executions")
    print(f"    arm B: {b_rate:.0%} correct, 0 model turns on the replay path")
    print(f"    arm B escalated on: "
          f"{[r['task'] for r in results if r['b'].get('escalated')] or 'nothing'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", help="comma-separated task numbers")
    args = parser.parse_args()
    picked = [int(n) for n in args.tasks.split(",")] if args.tasks else None
    sys.exit(asyncio.run(main(picked)))
