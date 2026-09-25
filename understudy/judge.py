"""Scoring a replay against a rubric, with a small model.

The deterministic score this replaces was `steps_completed / steps_total` —
which measures whether the recorded steps *ran*, not whether the task was
*done*. A capability that clicks through all seven steps and reads the wrong
field scores a perfect 1.00 under it. That happened here: a run returning
'firstvalley' where the answer was 'First Valley CU' was graded perfect, and
only the next orchestration cycle noticed.

So the judge scores two things the step count cannot see:

  * whether each step did what its own stated intent said it would, and
  * whether the values that came back are plausibly what the goal asked for.

The second is the one that earns the model call. Asked for an *organisation
name*, a judge can see that 'firstvalley' is a domain component and
'First Valley CU' is an organisation — without being told the answer. That is
correctness checking that needs no ground truth, which is what makes it usable
on capabilities nobody has hand-graded.

The deterministic signals are computed first and passed in, so the model is
never asked anything that can be measured. Scoring against a written rubric is
close to classification, but the judgement that earns the call — "is this the
right kind of value for what was asked, and does the evidence show a better
one" — turns out to need the reasoning, so it runs on the same model as the
proposer rather than a cheaper one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from .artifact.schema import Capability
from .replay.result import ReplayResult

MODEL = "claude-sonnet-5"

# The rubric is the judge. A vague one produces vague scores, so each band is
# defined by what is observable rather than by adjective.
RUBRIC = """You are grading one run of a recorded UI automation against a rubric.
You are not deciding whether the automation is impressive. You are deciding
whether it did the job.

Score each STEP on two criteria, each 1.0, 0.5 or 0.0:

INTENT MATCH — did the page change in the way this step's stated intent claims?
  1.0  the observed change is what the intent describes
       (intent "submit the login form", change shows the account list appearing)
  0.5  something happened, but it does not clearly match the intent, or the
       change is too small to tell
  0.0  nothing changed when something should have, or the change contradicts
       the intent (intent "open account details", change shows an error)
  n/a  the step legitimately changes nothing visible — reading a value, or
       asserting a condition. Score these 1.0 if the read returned something.

TARGETING — would this step still work if a caller passed a different input?
  1.0  targets by a stable label, role or accessible name; any input-specific
       part is a {placeholder}
  0.5  works, but positionally — an ordinal, or an anchor that could match
       several things
  0.0  the target hard-codes a value specific to this run, so the capability
       only works for one input

Then score the RUN as a whole:

GOAL MET — 1.0 / 0.5 / 0.0. Does the final state and the returned output show
  the stated goal accomplished? A legitimate business answer ("no such account")
  counts as met if that is what the application genuinely reported.

OUTPUTS SOUND — 1.0 / 0.5 / 0.0. Are the returned values what the goal asked for?

  First, if the evidence itself shows a different and more correct value for
  what was asked — a nearby field the goal describes better than the one that
  was read — score 0.0 and say which value should have been returned. Do not
  excuse a wrong value because it is the right *shape*: "asked for the current
  balance, returned the available balance, and the current balance is visible
  on the same page" is a wrong answer, not a plausible one.

  Where the evidence does not contain the correct value, fall back to judging
  the kind of thing returned:
    - asked for a balance, got "$901.10"        -> 1.0
    - asked for a balance, got "SAVINGS"        -> 0.0  (wrong kind of thing)
    - asked for an organisation name, got
      "First Valley CU"                         -> 1.0
    - asked for an organisation name, got
      "firstvalley"                             -> 0.0  (that is a domain
                                                   component, not a name)
    - no outputs were declared                  -> n/a, score 1.0

  You cannot verify a value the evidence does not contain. Say so rather than
  guessing; a value you cannot check, of the right kind, scores 1.0.

Reply with JSON only, no prose around it:
{"steps":[{"index":1,"intent_match":1.0,"targeting":1.0,"why":"<one short clause>"}],
 "goal_met":1.0,"outputs_sound":1.0,
 "verdict":"<one sentence on what this run did or failed to do>"}"""


class StepScore(BaseModel):
    index: int
    intent_match: float = 1.0
    targeting: float = 1.0
    why: str = ""


class Judgement(BaseModel):
    """What the rubric says about one run."""

    capability: str
    executed: float = Field(description="steps that ran / steps total — the old score, kept as one input")
    shape_match: float | None = Field(default=None, description="how much of the approved final state was present")
    intent_match: float = 1.0
    targeting: float = 1.0
    goal_met: float = 1.0
    outputs_sound: float = 1.0
    steps: list[StepScore] = Field(default_factory=list)
    verdict: str = ""
    judged_by: str = MODEL
    model_called: bool = True

    @property
    def score(self) -> float:
        """One number, weighted by what actually matters.

        Goal and outputs dominate: a run that executes cleanly and returns the
        wrong thing has failed, and no amount of clean execution redeems it.
        Targeting is weighted lowest because it predicts future breakage rather
        than describing this run.
        """
        return round(
            0.15 * self.executed
            + 0.15 * self.intent_match
            + 0.10 * self.targeting
            + 0.35 * self.goal_met
            + 0.25 * self.outputs_sound,
            3,
        )

    def table(self) -> str:
        rows = [
            f"  executed        {self.executed:>5.2f}   (steps that ran — measured, not judged)",
            f"  intent match    {self.intent_match:>5.2f}   (did each step do what it said)",
            f"  targeting       {self.targeting:>5.2f}   (would it work for another input)",
            f"  goal met        {self.goal_met:>5.2f}   (does the end state show the goal done)",
            f"  outputs sound   {self.outputs_sound:>5.2f}   (are the returned values what was asked for)",
        ]
        if self.shape_match is not None:
            rows.insert(1, f"  shape match     {self.shape_match:>5.2f}   (final state vs the approved one)")
        rows += ["", f"  SCORE           {self.score:>5.2f}", "", f"  {self.verdict}"]
        if any(s.intent_match < 1 or s.targeting < 1 for s in self.steps):
            rows.append("")
            for s in self.steps:
                if s.intent_match < 1 or s.targeting < 1:
                    rows.append(f"    step {s.index}: intent {s.intent_match} targeting "
                                f"{s.targeting} — {s.why}")
        return "\n".join(rows)


def _brief(capability: Capability, result: ReplayResult, arguments: dict[str, str]) -> str:
    """Everything the judge is allowed to see. Deliberately compact — the rubric
    is the expensive part of the prompt, not the evidence."""
    steps = []
    for step, outcome in zip(capability.steps, result.steps):
        target = step.target.target if step.target else None
        how = ""
        if target is not None:
            kind = getattr(target, "kind", "")
            handle = (getattr(target, "name", None) or getattr(target, "anchor", None)
                      or f"#{getattr(target, 'index', '')}")
            how = f" via {kind} {handle!r}"
        steps.append({
            "index": step.index,
            "intent": step.intent,
            "action": step.action + how,
            "ran": outcome.ok,
            "change": (outcome.change or "")[:300],
            "error": outcome.error,
        })

    return json.dumps({
        "goal": capability.goal,
        "arguments": arguments,
        "steps": steps,
        "declared_outputs": [o.name for o in capability.outputs],
        "returned_outputs": result.outputs,
        "replay_outcome": f"{result.outcome}/{result.subclass or ''}",
    }, indent=1)[:9000]


async def judge(capability: Capability, result: ReplayResult,
                arguments: dict[str, str] | None = None,
                log=None) -> Judgement:
    """Score one replay against the rubric, and record the verdict.

    Recording matters as much as scoring. While the verdict was only printed,
    the dashboard had no correctness signal to read and reported a capability
    that returns the wrong value as 100% healthy — the judge had already caught
    it and the finding went nowhere.
    """
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ClaudeSDKClient, TextBlock)

    executed = result.steps_completed / max(result.steps_total, 1)
    base = Judgement(capability=capability.id, executed=round(executed, 3),
                     shape_match=result.shape_match)

    options = ClaudeAgentOptions(system_prompt=RUBRIC, model=MODEL, max_turns=1)
    reply = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(_brief(capability, result, arguments or {}))
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        reply += block.text

    verdict = _parse(reply)
    if verdict is None:
        base.verdict = "the judge did not return usable JSON; deterministic signals only"
        base.model_called = False
        if log:
            log.emit("judgement", **base.model_dump(exclude={"steps"}), score=base.score)
        return base

    base.steps = [StepScore(**s) for s in verdict.get("steps", []) if "index" in s]
    if base.steps:
        base.intent_match = round(sum(s.intent_match for s in base.steps) / len(base.steps), 3)
        base.targeting = round(sum(s.targeting for s in base.steps) / len(base.steps), 3)
    base.goal_met = float(verdict.get("goal_met", 1.0))
    base.outputs_sound = float(verdict.get("outputs_sound", 1.0))
    base.verdict = str(verdict.get("verdict", ""))[:400]
    if log:
        log.emit("judgement", **base.model_dump(exclude={"steps"}), score=base.score,
                 steps=[s.model_dump() for s in base.steps])
    return base


def _parse(reply: str) -> dict[str, Any] | None:
    """Pull the JSON out, whether or not the model wrapped it in prose."""
    for candidate in (reply.strip(), *re.findall(r"\{.*\}", reply, re.S)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None
