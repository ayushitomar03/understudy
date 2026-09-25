"""Runs the loop again and again at one task, and keeps the best result.

A single discovery run is a coin toss: the model may take a bad anchor, declare
the wrong output, or burn its budget exploring. Measured across our runs on the
same goal, one attempt produced a capability that replayed perfectly and the
next produced one hardcoded to a single account. Picking whichever run happened
to finish is not a strategy.

So attempts are repeated, each one graded by *replaying what it produced*, and
only a capability that replays better than the incumbent is promoted. The score
comes from execution rather than from the model's opinion of itself, which is
the only reason it can be trusted to gate anything.

What carries between attempts is deliberately small: the targets already known
not to work, and the steps of the current champion. That is enough to stop the
loop rediscovering the same dead ends, without handing it a script to follow —
if the next attempt simply replayed the champion it would never find anything
better.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .artifact.schema import Capability
from .evidence import EventLog, read_events
from .judge import judge
from . import knowledge as knowledge_store
from .loop.runner import discover
from .policy import Policy, for_app
from .replay import Replayer
from .surface.web import WebSurface

RUNS = Path("evidence/orchestrations")


class AttemptRecord(BaseModel):
    """One pass of the loop, and how good its output turned out to be."""

    n: int
    run_id: str
    cold: bool = True
    seconds: float
    turns: int
    dead_ends: int
    produced: bool = False
    steps: int | None = None
    replay_score: float | None = Field(
        default=None,
        description="fraction of sampled replays that completed — reliability, not step count")
    replay_outcome: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    outputs_correct: bool | None = None
    judged: float | None = Field(default=None, description="the rubric's score, when no ground truth was given")
    promoted: bool = False
    note: str = ""

    @property
    def score(self) -> float:
        """Correctness, from execution.

        Replaying to completion is necessary but not sufficient — a capability
        that finishes every step and returns the wrong value scores worse than
        one that returns the right one. We shipped exactly that: an output
        reading 'firstvalley' where the answer was 'First Valley CU', which a
        steps-completed score calls perfect.
        """
        if not self.produced:
            return 0.0
        base = self.replay_score or 0.0
        if self.replay_outcome != "complete":
            return round(base * 0.5, 3)      # got partway, did not finish
        if self.outputs_correct is False:
            return round(base * 0.6, 3)      # finished, wrong answer
        # Where the rubric judged it, that judgement is the score: it weighs
        # whether the job was done, which step completion cannot see.
        return self.judged if self.judged is not None else base

    @property
    def rank(self) -> tuple:
        """How attempts are compared. Correctness first, then economy.

        Among capabilities that are equally correct, the shorter one is better:
        fewer steps is less to execute and less to break. Without this tie-break
        every perfect attempt scores 1.00, nothing is ever promoted, and a
        seven-step flow loses to the twelve-step one that happened to run first.
        """
        return (self.score, -(self.steps or 999), -self.dead_ends)


class Orchestration(BaseModel):
    goal: str
    capability_id: str
    params: dict[str, str] = Field(default_factory=dict)
    expect: dict[str, str] = Field(default_factory=dict)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    champion_path: str | None = None
    champion_score: float = 0.0
    champion_rank: tuple = (0.0, -999, -999)
    started: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def table(self) -> str:
        head = (f"{'#':<4}{'start':<10}{'turns':<7}{'dead':<6}{'steps':<7}{'reliable':<8}"
                f"{'right':<7}{'score':<7}")
        rows = [head, "-" * (len(head) + 12)]
        for a in self.attempts:
            right = "-" if a.outputs_correct is None else ("yes" if a.outputs_correct else "NO")
            mark = "  <- promoted" if a.promoted else ""
            rate = f"{(a.replay_score or 0) * 100:.0f}%"
            rows.append(
                f"{a.n:<4}{('cold' if a.cold else 'resumed'):<10}{a.turns:<7}"
                f"{a.dead_ends:<6}{str(a.steps or '-'):<7}{rate:<8}"
                f"{right:<7}{a.score:<7.2f}{mark}"
            )
        return "\n".join(rows)


class Lessons(BaseModel):
    """What one attempt hands to the next."""

    dead_targets: list[str] = Field(default_factory=list)
    champion_steps: list[str] = Field(default_factory=list)
    champion_score: float = 0.0

    def as_prompt(self) -> str:
        if not self.dead_targets and not self.champion_steps:
            return ""
        parts = ["\n\nYou have attempted this task before. From those attempts:"]
        if self.dead_targets:
            parts.append("\nThese targets were tried and did not work — do not repeat them:")
            parts += [f"  - {t}" for t in self.dead_targets[:12]]
        if self.champion_steps:
            parts.append(
                f"\nA previous attempt reached a working flow scoring "
                f"{self.champion_score:.2f}. Its steps were:"
            )
            parts += [f"  {i}. {s}" for i, s in enumerate(self.champion_steps, 1)]
            parts.append(
                "\nThat flow is a starting point, not a script. Follow it where it is right, "
                "and improve on it where it is not — a better attempt is one that reaches the "
                "goal in fewer steps, or extracts the correct values where the previous one "
                "extracted the wrong ones."
            )
        return "\n".join(parts)


def _lessons_from(run_dir: Path, champion: Capability | None, score: float) -> Lessons:
    dead: list[str] = []
    for event in read_events(run_dir / "events.jsonl"):
        if event.get("event") == "attempt" and event.get("verdict") in ("unresolved", "error", "blocked"):
            args = event.get("args") or {}
            handle = args.get("anchor") or args.get("name") or args.get("selector")
            if handle:
                entry = f"{args.get('role', '')} {handle!r} via {args.get('strategy', '?')}"
                if entry not in dead:
                    dead.append(entry)

    steps: list[str] = []
    if champion:
        for step in champion.steps:
            target = step.target.target if step.target else None
            who = ""
            if target is not None:
                who = (getattr(target, "name", None) or getattr(target, "anchor", None)
                       or f"#{getattr(target, 'index', '')}")
                who = f" {getattr(target, 'kind', '')} {who!r}"
            steps.append(f"{step.action}{who} — {step.intent[:70]}")

    return Lessons(dead_targets=dead, champion_steps=steps, champion_score=score)


def _run_stats(run_dir: Path) -> tuple[int, int, float]:
    turns = dead = 0
    elapsed = 0.0
    for event in read_events(run_dir / "events.jsonl"):
        turns = max(turns, event.get("turn", 0))
        if event.get("event") == "attempt" and event.get("verdict") in (
                "unresolved", "no_op", "blocked", "error", "timeout"):
            dead += 1
        elapsed = event.get("elapsed_ms", 0) / 1000
    return turns, dead, elapsed


async def _grade(capability: Capability, params: dict[str, str], secrets: dict[str, str],
                 log: EventLog, samples: int = 1) -> tuple[float, str, dict[str, str], object]:
    """Replay what the attempt produced, and let that be its grade.

    Run on a thread because the surface is synchronous, and in its own browser
    so that the discovery session's cookies cannot make a capability look more
    reproducible than it is.

    Replayed `samples` times where the application is not deterministic. One
    sample cannot grade a flaky capability — a flow that breaks on the 60% of
    member lookups showing an interstitial passes outright whenever the coin
    lands right, and an attempt that never handles the interstitial would be
    promoted on luck. The score becomes the pass rate, which is what
    reliability means.
    """
    def run():
        surface = WebSurface()
        try:
            policy = for_app(capability.app.base_url, mode="unattended")
            return Replayer(capability, surface, log, policy).run(params, secrets=secrets)
        finally:
            surface.close()

    results = [await asyncio.to_thread(run) for _ in range(max(samples, 1))]
    passed = [r for r in results if r.outcome == "complete"]
    best = passed[0] if passed else results[0]
    rate = len(passed) / len(results)
    outcome = best.outcome if passed else results[0].outcome
    return rate, outcome, best.outputs, best


async def orchestrate(*, goal: str, capability_id: str, base_url: str,
                      params: dict[str, str], credentials: dict[str, str],
                      expect: dict[str, str] | None = None,
                      min_attempts: int = 5, max_attempts: int = 8,
                      max_turns: int = 30, model: str = "claude-sonnet-5",
                      samples: int = 1,
                      outputs: list[str] | None = None,
                      output_patterns: dict[str, str] | None = None,
                      learn: bool = True, on_update=None) -> Orchestration:
    """Keep attempting the goal, grading each attempt, keeping the best."""
    RUNS.mkdir(parents=True, exist_ok=True)
    expect = expect or {}
    known = knowledge_store.load(base_url) if learn else None
    state = Orchestration(goal=goal, capability_id=capability_id, params=params, expect=expect)
    state_path = RUNS / f"{capability_id}-{state.started:%Y%m%d-%H%M%S}.json"

    champion: Capability | None = None
    lessons = Lessons()

    for n in range(1, max_attempts + 1):
        started = time.monotonic()

        # Resume from the champion, except every third attempt, which starts
        # cold. Refining from a champion biases every attempt toward it — if
        # the champion is subtly wrong, refinement entrenches the mistake
        # instead of escaping it. The cold attempt is the control.
        cold = (n % 3 == 0) or champion is None
        capability, log = await discover(
            goal=goal + lessons.as_prompt(),
            stated_goal=goal,
            base_url=base_url,
            capability_id=capability_id,
            parameters=params,
            outputs=outputs or list(expect) or None,
            output_patterns=output_patterns,
            knowledge=known if cold else None,
            credentials=credentials,
            policy=for_app(base_url, mode="discover"),
            model=model,
            max_turns=max_turns,
            prelude=None if cold else champion,
        )

        turns, dead, _ = _run_stats(log.dir)
        record = AttemptRecord(
            n=n, run_id=log.run_id, cold=cold, seconds=round(time.monotonic() - started, 1),
            turns=turns, dead_ends=dead, produced=capability is not None,
            steps=len(capability.steps) if capability else None,
        )
        record.note = "cold start" if cold else "resumed from champion"

        if capability is not None:
            grade_log = EventLog("grade", f"attempt {n} of {capability_id}")
            score, outcome, outputs, result = await _grade(
                capability, params, credentials, grade_log, samples=samples)
            record.replay_score, record.replay_outcome, record.outputs = score, outcome, outputs

            if not expect:
                # No ground truth, so correctness has to be judged. This is the
                # case the rubric exists for, and the champion gate is the one
                # place it matters: without it, an attempt that reads the wrong
                # field replays cleanly and gets promoted. Skipped when the
                # caller supplied expected values, which are exact and free.
                verdict = await judge(capability, result, params, log=grade_log)
                record.judged = verdict.score
                record.outputs_correct = verdict.outputs_sound >= 0.5
                record.note = verdict.verdict[:120]
            if expect:
                # Compare on value, not just on name. The name is now part of
                # the commission, but a run that still renames an output should
                # be marked as a naming problem rather than silently graded as
                # a wrong answer.
                got = {k: v.strip() for k, v in outputs.items()}
                want = {k: v.strip() for k, v in expect.items()}
                by_name = all(got.get(k) == v for k, v in want.items())
                by_value = sorted(got.values()) == sorted(want.values())
                record.outputs_correct = by_name
                if by_value and not by_name:
                    record.note = f"right values, wrong output names: {sorted(got)}"

            # Champion-gated on rank, not raw score: correctness first, then the
            # leaner capability. A new one has to actually be better, so ties go
            # to the incumbent.
            if record.rank > state.champion_rank:
                champion = capability
                state.champion_score = record.score
                state.champion_rank = record.rank
                record.promoted = True
                path = RUNS / f"{capability_id}-champion.json"
                path.write_text(capability.model_dump_json(indent=2))
                state.champion_path = str(path)
                record.note = "promoted"
            else:
                record.note = f"kept champion ({state.champion_score:.2f}, {state.champion_rank[1] * -1} steps)"
        else:
            record.note = "produced no capability"

        state.attempts.append(record)
        state_path.write_text(state.model_dump_json(indent=2))
        if on_update:
            on_update(state, record)

        lessons = _lessons_from(log.dir, champion, state.champion_score)

        if known is not None and record.promoted and capability is not None:
            note = known.learn_from(capability, lessons.dead_targets)
            knowledge_store.save(known)
            if note:
                record.note = f"{record.note}; learned: {note}"

        # Keep going until the minimum is met; stop early only once the minimum
        # is satisfied and the champion replays perfectly.
        if n >= min_attempts and state.champion_score >= 1.0 and not record.promoted:
            break

    return state
