"""Handing the live session to a person, and taking it back.

The brief is specific that the human must operate *the same session* the
automation was using, not a fresh one. So nothing is torn down: the browser
stays open on the page where the run stopped, with its cookies and its
half-filled form, and what changes hands is permission to touch it.

That permission is a lease with exactly one holder — `agent`, `human`, or
`none`. Every automated action asserts it before running, which turns "who is
in control" from a convention into something that is checked. Two things
driving one session is the failure this prevents, and it is not hypothetical:
the moment a person starts clicking while automation is mid-step, the run's
record of what happened stops being true.

The lease lives in a file rather than in memory because the operator is a
different process. That is also what makes the handoff inspectable after the
fact: `control.json` is evidence.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Holder = Literal["agent", "human", "none"]

# Injected while a human holds the lease. Records what they actually did, so the
# run's history stays continuous across the handoff instead of containing a gap
# labelled "a person did something here".
RECORDER_JS = """
(() => {
  if (window.__understudyRecorder) return;
  window.__understudyRecorder = [];
  const describe = (el) => {
    if (!el || !el.tagName) return 'unknown';
    const tag = el.tagName.toLowerCase();
    const name = el.getAttribute('aria-label') || el.value || el.name || el.id ||
                 (el.innerText || '').trim().slice(0, 40);
    return `${tag}${name ? ' "' + name + '"' : ''}`;
  };
  document.addEventListener('click', (e) => {
    window.__understudyRecorder.push({at: Date.now(), action: 'click', on: describe(e.target)});
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    const secret = el.type === 'password';
    window.__understudyRecorder.push({
      at: Date.now(), action: 'set', on: describe(el),
      value: secret ? '<redacted>' : String(el.value || '').slice(0, 60),
    });
  }, true);
})();
"""


class InterventionRequest(BaseModel):
    """What a human is being asked to do, and everything needed to do it."""

    run_id: str
    capability: str | None = None
    goal: str = ""
    step: int | None = None
    why: str
    needed: str
    url: str = ""
    screenshot: str | None = None
    ledger: str = ""
    raised_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Control(BaseModel):
    """The lease itself, as it sits on disk."""

    holder: Holder = "agent"
    request: InterventionRequest | None = None
    decision: Literal["pending", "resume", "abort"] | None = None
    human_actions: list[dict] = Field(default_factory=list)
    released_at: datetime | None = None
    returned_at: datetime | None = None


class Lease:
    """File-backed control of one session."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "control.json"
        self._write(Control())

    # -- state -------------------------------------------------------------

    def read(self) -> Control:
        try:
            return Control.model_validate_json(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return Control()

    def _write(self, control: Control) -> None:
        self.path.write_text(control.model_dump_json(indent=2, exclude_none=False))

    @property
    def holder(self) -> Holder:
        return self.read().holder

    def assert_agent(self) -> None:
        """Called before every automated action. The whole point of the lease."""
        holder = self.holder
        if holder != "agent":
            raise PermissionError(
                f"the agent does not hold this session (holder={holder}); "
                "acting now would mean two drivers on one browser"
            )

    # -- handing over ------------------------------------------------------

    def release(self, request: InterventionRequest) -> None:
        control = self.read()
        control.holder = "none"
        control.request = request
        control.decision = "pending"
        control.released_at = datetime.now(timezone.utc)
        self._write(control)

    def take_human(self) -> None:
        control = self.read()
        control.holder = "human"
        self._write(control)

    def decide(self, decision: Literal["resume", "abort"], actions: list[dict] | None = None) -> None:
        control = self.read()
        control.decision = decision
        control.human_actions = actions or control.human_actions
        control.holder = "agent" if decision == "resume" else "none"
        control.returned_at = datetime.now(timezone.utc)
        self._write(control)

    # -- waiting -----------------------------------------------------------

    def wait_for_decision(self, timeout_s: float = 600, poll_s: float = 0.5) -> Control:
        """Block until a person decides, or until the wait is abandoned.

        A timeout is not a decision: it leaves the lease unheld and the run
        aborted, because resuming on the assumption that somebody probably
        dealt with it is exactly how a double-submit happens.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            control = self.read()
            if control.decision in ("resume", "abort"):
                return control
            time.sleep(poll_s)

        self.decide("abort")
        return self.read()
