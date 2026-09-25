"""What a replay returns to its caller.

The outcome classes are named for how far the task got, not for what went
wrong. That keeps the set closed — a task either finished, got to the right
place and stalled, or never arrived — while every specific error slots in
underneath as a subclass. A new failure mode nobody anticipated needs a new
subclass, never a new class, and never a change to how callers branch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Outcome = Literal["complete", "incomplete_action", "unreachable"]


class StepResult(BaseModel):
    index: int
    intent: str
    action: str
    ok: bool
    detail: str = ""
    error: str | None = None
    ms: int = 0
    url: str = ""
    change: str = Field(default="", description="what appeared or disappeared on the page")


class ReplayResult(BaseModel):
    """The contract an agent calls this system through."""

    capability_id: str
    capability_version: int
    run_id: str

    outcome: Outcome
    subclass: str | None = Field(
        default=None, description="why, within the class — auth_failed, account_not_found, …"
    )
    score: float = Field(description="steps completed / steps total; reproducible, no model involved")

    steps_total: int
    steps_completed: int
    failed_step: int | None = None

    outputs: dict[str, str] = Field(default_factory=dict)
    expected: str = ""
    observed: str = ""

    shape_match: float | None = Field(
        default=None, description="fraction of the approved success template still present"
    )
    reasoning: str | None = Field(
        default=None, description="from the judge; only populated when the outcome is not complete"
    )
    judged_by: str | None = None

    evidence_path: str = ""
    failure_tree: str = Field(
        default="",
        description=(
            "the accessibility tree at the point of failure. Anything reasoning about why a "
            "run failed needs to see the page it failed on: a proposer given only step "
            "summaries guessed that a control was a button when the page plainly showed a "
            "link, and invented a detector from text it had never seen rendered."
        ),
    )
    steps: list[StepResult] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "complete"

    def summary(self) -> str:
        head = f"{self.outcome}"
        if self.subclass:
            head += f" / {self.subclass}"
        head += f"  score={self.score:.2f}  ({self.steps_completed}/{self.steps_total} steps)"
        if self.outputs:
            head += "\n  outputs: " + ", ".join(f"{k}={v!r}" for k, v in self.outputs.items())
        if self.failed_step is not None:
            head += f"\n  failed at step {self.failed_step}: {self.observed[:160]}"
        if self.reasoning:
            head += f"\n  judge: {self.reasoning}"
        return head
