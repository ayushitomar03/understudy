"""The loop: run it, and if it fails, find out why and change something.

    replay (no model)  ──fails──▶  diagnose  ──▶  propose  ──▶  apply
         ▲                                                        │
         └──────────────── the next version ◀─────────────────────┘
                                  │
                   still failing after N rounds ──▶ a human

What separates this from retrying: a retry changes nothing, so its only hope is
that the application behaves differently this time. Repeated attempts produced a
flat line for exactly that reason. Here each round ends in a *change to the
artifact*, and the next round measures whether that change helped.

Three properties that keep it honest:

  * **Reliability is sampled.** One replay cannot grade a flow against an
    application that fails intermittently — a capability that never handles an
    interstitial passes outright whenever the coin lands right. The score is the
    pass rate over several runs.

  * **A change is kept only if it helps.** A version that scores no better than
    the one before it is rolled back. A proposer that is confidently wrong
    should cost one round, not the whole capability.

  * **Replay stays free of the model.** Diagnosis and proposal happen between
    runs, never inside one. The production path executes a recorded flow and
    consults nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from .artifact.schema import Capability
from .evidence import EventLog
from .judge import judge
from .policy import Policy, for_app
from . import knowledge as knowledge_store
from .propose import (Diagnosis, Intervention, apply, diagnose, grounded,
                      propose)
from .replay import Replayer
from .replay.result import ReplayResult
from .surface.web import WebSurface

RELIABLE = 0.9          # a flow that fails one run in ten is not fixed


def improved(before_passed: int, after_passed: int, samples: int) -> bool:
    """Is this better, or is it the same flow measured twice?

    `after > before` compares two noisy point estimates. On five samples of a
    genuinely 40%-reliable flow, one measurement said 100% and the next said 0%
    with nothing changed in between — and the loop kept a change on the strength
    of that difference.

    So a change has to clear what chance alone produces at this sample size:
    strictly better, and by more than one sample's worth. Small samples
    therefore demand a large effect to be believed, which is the correct
    trade — not a reason to believe small ones.
    """
    margin = max(1, round(samples ** 0.5))
    return after_passed >= before_passed + margin


class Round(BaseModel):
    n: int
    version: int
    reliability: float
    passed: int
    of: int
    cause: str | None = None
    statement: str = ""
    change: str = ""
    kept: bool | None = Field(default=None, description="None when no change was made")


class Repair(BaseModel):
    capability: str
    rounds: list[Round] = Field(default_factory=list)
    final_version: int = 1
    final_reliability: float = 0.0
    escalated: str = ""

    def table(self) -> str:
        rows = [f"{'round':<7}{'version':<9}{'reliability':<13}{'diagnosis':<26}{'change'}",
                "-" * 92]
        for r in self.rounds:
            mark = "" if r.kept is None else ("  kept" if r.kept else "  rolled back")
            rows.append(f"{r.n:<7}v{r.version:<8}{r.passed}/{r.of} = {r.reliability:<7.0%}"
                        f"{(r.cause or '—')[:24]:<26}{r.change[:34]}{mark}")
        return "\n".join(rows)


async def _measure(capability: Capability, params: dict[str, str], secrets: dict[str, str],
                   samples: int, log: EventLog,
                   policy: Policy | None = None) -> tuple[float, ReplayResult | None]:
    """Replay it several times. Return the pass rate and one failure to learn from.

    Each replay goes to a worker thread: the surface is synchronous and
    Playwright refuses to start inside a running event loop, while diagnosis and
    proposal are async because they call a model.
    """
    def once() -> ReplayResult:
        surface = WebSurface()
        try:
            # Unattended is the right default and the wrong assumption to hard-code:
            # a flow that posts a transfer is refused under it, so a caller measuring
            # one was measuring the gate rather than the flow. The gate is the
            # caller's decision, and the default still denies.
            gate = policy or for_app(capability.app.base_url, mode="unattended")
            return Replayer(capability, surface, log, gate).run(params, secrets=secrets)
        finally:
            surface.close()

    failure = None
    passed = 0
    for _ in range(samples):
        result = await asyncio.to_thread(once)
        if result.outcome == "complete":
            passed += 1
        elif failure is None:
            failure = result
    return passed / samples, failure


async def repair(capability: Capability, params: dict[str, str],
                 secrets: dict[str, str] | None = None, *,
                 samples: int = 5, rounds: int = 4,
                 policy: Policy | None = None,
                 on_round=None) -> tuple[Capability, Repair]:
    """Improve a capability until it is reliable, or until a person is needed."""
    secrets = secrets or {}
    record = Repair(capability=capability.id)
    current = capability
    proposed_before: list[str] = []

    for n in range(1, rounds + 1):
        log = EventLog("repair", f"{capability.id} round {n}")
        reliability, failure = await _measure(current, params, secrets, samples, log, policy)
        passed = round(reliability * samples)
        entry = Round(n=n, version=current.version, reliability=reliability,
                      passed=passed, of=samples)

        if reliability >= RELIABLE:
            record.rounds.append(entry)
            break

        if failure is None:
            entry.statement = "failing, but no failing run was captured to learn from"
            record.rounds.append(entry)
            record.escalated = entry.statement
            break

        # Why did it fail, and what single change would fix that cause.
        diagnosis = await diagnose(current, failure)
        entry.cause, entry.statement = diagnosis.cause, diagnosis.statement
        log.emit("diagnosis", **diagnosis.model_dump())

        intervention = await propose(diagnosis, current, failure.failure_tree)
        entry.change = intervention.describe()
        log.emit("intervention", **intervention.model_dump())

        # Reject a proposal the page cannot support before paying to measure it.
        if (ungrounded := grounded(intervention, failure.failure_tree)):
            entry.change = f"rejected: {ungrounded}"
            entry.kept = False
            record.rounds.append(entry)
            log.emit("intervention_rejected", why=ungrounded, **intervention.model_dump())
            continue

        # The proposer repeating itself means it is out of ideas, not that the
        # fix needs another go. Five rounds once produced the same diagnosis and
        # the same change five times over.
        if intervention.signature in proposed_before:
            record.rounds.append(entry)
            record.escalated = (
                f"the same change was proposed again without helping — "
                f"{diagnosis.statement} The proposer has no further ideas, so this "
                f"needs a person to decide what the flow should do here.")
            log.emit("escalation_requested", why=record.escalated,
                     needed="decide what this flow should do when this screen appears",
                     repeated=intervention.describe())
            break
        proposed_before.append(intervention.signature)

        patched = apply(current, intervention, diagnosis, run_id=log.run_id)
        if patched is None:
            # Either nothing expressible as a change, or a change the artifact
            # already carries. Both mean the same thing: another round of the
            # same reasoning will not help.
            record.rounds.append(entry)
            record.escalated = (f"{diagnosis.statement} — no further change to the flow "
                                f"addresses this ({intervention.kind}: {intervention.why})")
            log.emit("escalation_requested", why=record.escalated,
                     needed="a person should decide what this flow should do here")
            break

        # Keep the change only if it measurably helped.
        after, _ = await _measure(patched, params, secrets, samples, log, policy)
        after_passed = round(after * samples)
        entry.kept = improved(passed, after_passed, samples)
        entry.reliability = reliability
        record.rounds.append(entry)
        log.emit("revision_measured", before=reliability, after=after,
                 before_passed=passed, after_passed=after_passed,
                 margin_needed=max(1, round(samples ** 0.5)), kept=entry.kept)

        if entry.kept:
            current = patched
            # A recovery learned here is a fact about the application, not about
            # this one flow. Recording it means the next capability on this app
            # inherits it instead of rediscovering it from its own failures.
            if intervention.kind == "add_recovery" and patched.recoveries:
                known = knowledge_store.load(capability.app.base_url)
                if known.learn_recovery(patched.recoveries[-1]):
                    knowledge_store.save(known)
                    log.emit("recovery_remembered", app=capability.app.base_url,
                             detect=patched.recoveries[-1].detect)
        elif n == rounds:
            record.escalated = (f"{rounds} rounds and nothing improved it; last attempt: "
                                f"{entry.change}")

        if on_round:
            on_round(entry, after)

    record.final_version = current.version
    record.final_reliability = record.rounds[-1].reliability if record.rounds else 0.0
    return current, record
