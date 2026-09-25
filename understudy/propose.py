"""Turning a failure into a change.

The loop this completes: a task runs; if it succeeds nothing is needed; if it
fails, something has to say *why* and something has to act on that. Without the
second half, a retry is another roll of the dice — which is what the orchestrator
was doing, and why repeated attempts produced a flat line rather than a curve.

Three roles, deliberately separate:

    diagnose   why did it fail?          one statement, one cause
    propose    what change would fix it?  one intervention, targeted
    apply      make the change            to memory, instruction, or the flow

Keeping diagnosis apart from proposal matters. A model asked "fix this" writes a
plausible fix for a cause it never established; asked "why did this fail" it
looks at the evidence. The proposal is then constrained by the diagnosis rather
than by imagination.

And an intervention is a *typed change to a named thing*, never free advice.
"try to be more careful" cannot be applied, cannot be tested, and cannot be
rolled back when it turns out to be wrong.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from .artifact.schema import Capability
from .replay.result import ReplayResult

# Proposing a fix is reasoning about an application's behaviour, not
# classification against a supplied rubric. On Haiku the proposer guessed the
# wrong element role two times in three, built a detector out of text too
# specific to match, and proposed the identical change three rounds running —
# all reasoning failures rather than formatting ones.
MODEL = "claude-sonnet-5"

# What can go wrong, at the granularity a fix can act on. Not an error
# catalogue — a list of things a *different kind of change* would repair.
Cause = Literal[
    "unexpected_screen",     # something appeared the flow does not know about
    "wrong_control",         # the step targeted the wrong thing
    "fragile_target",        # it resolved during discovery and not now
    "missing_step",          # the flow is incomplete for this state
    "value_not_there",       # the read found nothing where it expected something
    "session_lost",          # the application stopped considering us signed in
    "resource_busy",         # the app is holding something and will release it; wait
    "business_outcome",      # the app legitimately said no; not a defect
    "unclear",               # the evidence does not support a diagnosis
]

# How a cause gets repaired. Each is a change to something addressable.
Kind = Literal["add_recovery", "retarget_step", "insert_step", "amend_guidance", "none"]

DIAGNOSE = """You are looking at one failed run of a recorded UI automation and
saying why it failed. You are not fixing it and not guessing at intent.

Use only the evidence given. If it does not support a diagnosis, say so — an
"unclear" verdict is worth more than a confident wrong one, because the next
step acts on what you say.

The causes, and what distinguishes them:

  unexpected_screen  the page shown is not the one the flow expected, and it is
                     an interruption rather than the goal state — a notice, a
                     confirmation, an interstitial standing between the flow and
                     where it was going
  wrong_control      the step acted on something, but the wrong something
  fragile_target     the target resolved when this was recorded and does not now,
                     though the screen is the one expected
  missing_step       the flow simply does not cover the state it is in
  value_not_there    a read resolved but the value is absent or empty
  session_lost       the application is showing sign-on where the flow expected
                     to be signed in already
  resource_busy      the application is holding something and says so — a record
                     in use by another terminal, a batch still posting. Nothing is
                     broken, there is nothing to press, and it clears on its own.
                     Distinguish this from unexpected_screen by whether the screen
                     offers any control: an interstitial does, a hold does not
  business_outcome   the application gave a legitimate answer — no such record,
                     not authorised. Nothing is broken and nothing should change.

`page_when_it_failed` is the accessibility tree of the screen the run stopped
on. It is the strongest evidence you have — prefer what it shows over what the
step summaries imply.

Reply with JSON only:
{"cause":"<one of the above>","statement":"<one sentence on what happened>",
 "evidence":"<the specific thing in the evidence that shows it>",
 "at_step":<step number or null>}"""

PROPOSE = """You are given a diagnosis of why a recorded automation failed, and
you propose exactly one change that would fix it.

A proposal changes a named thing. It is never advice.

  add_recovery    the flow meets a known interruption it should handle: name
                  what identifies that screen and what to do about it, then the
                  flow continues from where it was. Two shapes:
                    - a control to press, when the screen offers one
                    - the literal action "wait", when the condition clears on its
                      own and there is nothing to press. A record held by another
                      terminal, a batch still posting, a resource in use: the
                      screen names no control because pressing is not the answer.
                      Give wait_seconds if a few seconds is not right
  retarget_step   a step targets the wrong control or a fragile one: give the
                  step number and a better way to find it. Prefer a stable
                  visible label over a position
  insert_step     the flow is missing an action: give the step number to insert
                  before, and the action
  amend_guidance  the failure is about how the task is approached rather than
                  any one step: one sentence that would be added to the
                  instructions for the next attempt
  none            nothing should change — the application behaved correctly, or
                  the evidence does not justify a change

You are given `page_when_it_failed`, the accessibility tree of the screen the
run stopped on. Every control you name and every text you match on must appear
in it — read it rather than inferring from the step descriptions.

Two things that decide whether a fix works:

  * **Detect on the shortest text that identifies the condition and nothing
    else.** A whole sentence including a button label is brittle; two or three
    distinctive words are not. You are matching a substring of the page.

  * **Name a control by the words on it.** Do not worry about whether it is a
    button or a link — say which you think it is, but the name is what matters
    and the role is treated as a hint.

  * **Look for a control before you propose waiting, and do not invent one.**
    If the screen offers nothing to press, "wait" is the correct proposal and
    naming a control that is not there is the failure mode to avoid.

Propose the smallest change that addresses the stated cause. Do not fix things
the diagnosis did not mention.

Reply with JSON only:
{"kind":"<one of the above>","at_step":<number or null>,
 "detects":"<visible text identifying the condition, for add_recovery>",
 "action":"<what to do: for add_recovery either the control to click or the
            literal \"wait\"; for retarget_step the anchor or name to use; for
            insert_step the action to take>",
 "role":"<button|link|textbox|cell|heading, when a control is named>",
 "wait_seconds":<number, only with action \"wait\"; omit for the default>,
 "guidance":"<the sentence to add, for amend_guidance>",
 "why":"<one sentence on why this fixes the diagnosed cause>"}"""


class Diagnosis(BaseModel):
    cause: Cause = "unclear"
    statement: str = ""
    evidence: str = ""
    at_step: int | None = None

    @property
    def actionable(self) -> bool:
        """A legitimate business outcome is not a defect, and an unclear one
        gives a proposer nothing to work from."""
        return self.cause not in ("business_outcome", "unclear")


class Intervention(BaseModel):
    kind: Kind = "none"
    at_step: int | None = None
    detects: str = ""
    action: str = ""
    role: str = ""
    wait_seconds: float | None = None
    guidance: str = ""
    why: str = ""

    @property
    def is_wait(self) -> bool:
        """A recovery that does nothing but let time pass."""
        return (self.kind == "add_recovery"
                and self.action.strip().lower().split()[0:1] == ["wait"])

    @property
    def signature(self) -> str:
        """Identity of the change, for spotting a proposer repeating itself."""
        return "|".join([self.kind, str(self.at_step or ""),
                         self.detects.strip().lower(), self.action.strip().lower()])

    def describe(self) -> str:
        if self.kind == "add_recovery":
            return f'handle "{self.detects}" by {self.action}'
        if self.kind == "retarget_step":
            return f"retarget step {self.at_step} to {self.action!r}"
        if self.kind == "insert_step":
            return f"insert before step {self.at_step}: {self.action}"
        if self.kind == "amend_guidance":
            return f"guidance: {self.guidance[:80]}"
        return "no change"


class Attempted(BaseModel):
    """One turn of the loop, kept so the improvement can be read back."""

    attempt: int
    reliability: float
    diagnosis: Diagnosis | None = None
    intervention: Intervention | None = None
    applied: bool = False


async def _ask(system: str, payload: str) -> dict[str, Any] | None:
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ClaudeSDKClient, TextBlock)

    reply = ""
    async with ClaudeSDKClient(options=ClaudeAgentOptions(
            system_prompt=system, model=MODEL, max_turns=1)) as client:
        await client.query(payload)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        reply += block.text

    for candidate in (reply.strip(), *re.findall(r"\{.*\}", reply, re.S)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _brief(capability: Capability, result: ReplayResult) -> str:
    """What the diagnosis is allowed to see: the goal, the steps, where it
    stopped, and what the page showed when it did."""
    steps = []
    for step, outcome in zip(capability.steps, result.steps):
        target = step.target.target if step.target else None
        how = ""
        if target is not None:
            how = (f" via {getattr(target, 'kind', '')} "
                   f"{getattr(target, 'name', None) or getattr(target, 'anchor', None) or ''!r}")
        steps.append({"step": step.index, "intent": step.intent,
                      "action": step.action + how, "ran": outcome.ok,
                      "page_changed": (outcome.change or "")[:260],
                      "error": outcome.error})
    return json.dumps({
        "goal": capability.goal,
        "stopped_at_step": result.failed_step,
        "expected": result.expected,
        "observed": result.observed,
        "outcome": f"{result.outcome}/{result.subclass or ''}",
        "outputs_returned": result.outputs,
        "steps": steps,
        # The page as it actually was. Naming a control without seeing it is
        # guessing, and the guesses were wrong in the ways guesses are.
        "page_when_it_failed": (result.failure_tree or "")[:4000],
    }, indent=1)[:14000]


def grounded(intervention: Intervention, failure_tree: str) -> str | None:
    """Why this proposal cannot be true of the page, or None if it might be.

    Checked before it is applied, because applying it costs a full measurement
    — five or more replays — to discover that the named control was never
    there. This is free and catches the whole class: a detector invented from
    text the page does not contain, a control named from the step description
    rather than the screen.
    """
    if not failure_tree or intervention.kind == "none":
        return None
    page = failure_tree.lower()

    if intervention.detects and intervention.detects.strip().lower() not in page:
        return (f"the page does not contain {intervention.detects!r}, so a rule "
                "detecting it would never fire")

    # A wait names no control on purpose, so requiring one on the page would
    # reject the only correct proposal for a condition that clears by itself.
    if intervention.is_wait:
        return None

    if intervention.kind in ("add_recovery", "insert_step") and intervention.action:
        if intervention.action.strip().lower() not in page:
            return (f"no control named {intervention.action!r} is on the page the run "
                    "failed on")
    return None


async def diagnose(capability: Capability, result: ReplayResult) -> Diagnosis:
    """Say why this failed, from the evidence."""
    raw = await _ask(DIAGNOSE, _brief(capability, result))
    if not raw:
        return Diagnosis(statement="the diagnosis did not return usable JSON")
    try:
        return Diagnosis(**{k: raw.get(k) for k in
                            ("cause", "statement", "evidence", "at_step")
                            if raw.get(k) is not None})
    except ValueError:
        return Diagnosis(statement=str(raw)[:200])


async def propose(diagnosis: Diagnosis, capability: Capability,
                  failure_tree: str = "") -> Intervention:
    """Say what single change would fix the diagnosed cause."""
    if not diagnosis.actionable:
        return Intervention(kind="none",
                            why=f"nothing to fix: {diagnosis.cause}")

    payload = json.dumps({
        "diagnosis": diagnosis.model_dump(),
        "goal": capability.goal,
        "steps": [{"step": s.index, "intent": s.intent, "action": s.action}
                  for s in capability.steps],
        "already_handles": [{"detect": r.detect, "action": r.action}
                            for r in capability.recoveries],
        "page_when_it_failed": (failure_tree or "")[:4000],
    }, indent=1)
    raw = await _ask(PROPOSE, payload)
    if not raw:
        return Intervention(kind="none", why="the proposer did not return usable JSON")
    try:
        return Intervention(**{k: v for k, v in raw.items()
                               if k in Intervention.model_fields and v is not None})
    except ValueError:
        return Intervention(kind="none", why=str(raw)[:200])


def apply(capability: Capability, intervention: Intervention,
          diagnosis: Diagnosis, run_id: str = "") -> Capability | None:
    """Make the change, as a new version of the artifact.

    A repair is a version, not an edit. The brief asks the artifact to be
    versioned and reviewable, and a repaired flow is precisely where that earns
    its keep: v3 carries what v2 got wrong and what was done about it, so a
    reviewer can read the history without re-running anything.

    Returns None when the intervention cannot be expressed as a change to the
    artifact — which is the signal to stop repairing and involve a person,
    rather than to try again and hope.
    """
    from .artifact.schema import Locator, Recovery, Revision, Step
    from .surface.base import AfterText, RoleName

    patched = capability.model_copy(deep=True)
    change = intervention.describe()

    if intervention.kind == "add_recovery":
        if not intervention.detects or not intervention.action:
            return None
        # A rule for this condition already exists and the flow still fails, so
        # the diagnosis was right and the fix is not working. Adding it again
        # is not a repair — v5 accumulated four rules, three of them identical,
        # because nothing checked.
        if any(r.detect.strip().lower() == intervention.detects.strip().lower()
               for r in capability.recoveries):
            return None
        # The wait branch used to be unreachable: the guard above rejects an
        # empty action, and the only way to reach action="wait" was for the
        # action to be empty. So a condition with nothing to press had no
        # expressible fix, which is why both held-record tasks scored 0/10.
        waiting = intervention.is_wait
        target = (None if waiting else
                  RoleName(role=intervention.role or "button", name=intervention.action))
        patched.recoveries.append(Recovery(
            code=_slug(intervention.detects),
            detect=intervention.detects,
            action="wait" if waiting else "click",
            target=target,
            seconds=float(intervention.wait_seconds or 2.0),
            why=intervention.why,
            learned_from=run_id or None,
        ))

    elif intervention.kind == "retarget_step":
        step = _step(patched, intervention.at_step)
        if step is None or not intervention.action or step.target is None:
            return None
        existing = getattr(step.target.target, "anchor", None) or getattr(
            step.target.target, "name", None)
        if existing and existing.strip().lower() == intervention.action.strip().lower():
            return None   # already targeted that way and still failing
        role = intervention.role or getattr(step.target.target, "role", "cell")
        step.target = Locator(
            target=AfterText(anchor=intervention.action, role=role),
            why=f"retargeted after failure: {intervention.why}"[:200],
            verified=False,
        )

    elif intervention.kind == "insert_step":
        at = intervention.at_step
        if at is None or not intervention.action:
            return None
        inserted = Step(index=at, intent=f"recovery: {intervention.why}"[:120],
                        action="click",
                        target=Locator(target=RoleName(role=intervention.role or "button",
                                                       name=intervention.action),
                                       why=intervention.why[:200], verified=False))
        steps = [s for s in patched.steps]
        steps.insert(max(at - 1, 0), inserted)
        for i, step in enumerate(steps, 1):
            step.index = i
        patched.steps = steps

    elif intervention.kind == "amend_guidance":
        # Guidance changes how the next *discovery* is approached. It is not a
        # change to the flow, so it does not make a new version of it.
        return None

    else:
        return None

    patched.version = capability.version + 1
    patched.approval = "draft"   # a repaired flow has not been reviewed
    patched.revisions.append(Revision(
        version=patched.version, cause=diagnosis.cause,
        statement=diagnosis.statement[:240], change=change[:240],
    ))
    return patched


def _step(capability: Capability, index: int | None):
    return next((s for s in capability.steps if s.index == index), None) if index else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "condition"
