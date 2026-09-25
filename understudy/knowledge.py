"""What the loop learns about an application, carried between tasks.

Until now the loop's memory was run-scoped: each discovery started knowing
nothing, and across our runs it re-derived the same four login steps more than
twenty-five times. That is not a small waste — on the frameset app, signing on
is four of the ten steps, so two fifths of every task's budget went on a
problem already solved.

This is the smallest thing that is honestly *learning* rather than caching:

  * **Verified prefixes.** When two tasks on the same app begin with the same
    sequence of steps and both succeed, that sequence is not a coincidence — it
    is how you get into the application. It gets a name and is replayed for
    every later task. Discovered, never configured.

  * **Anchors that resolved, and anchors that did not.** Per app, because they
    are a property of the app's markup rather than of any one task.

Two properties this deliberately keeps:

  * Nothing is remembered unless it was *verified by a run that succeeded*. A
    prefix from a failed attempt teaches the next attempt to fail the same way.

  * Everything is falsifiable. A prefix that stops working is demoted on the
    next failure rather than trusted forever — stale knowledge is worse than
    none, because the loop believes it.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from .artifact.schema import Capability, Recovery, Step

STORE = Path("evidence/knowledge")

# A prefix has to appear in at least this many successful tasks before it is
# trusted. One is a coincidence; two is how the app works.
CONFIRMATIONS = 2


class Prefix(BaseModel):
    """A step sequence that reliably reaches a useful state."""

    name: str
    steps: list[Step]
    confirmed_by: list[str] = Field(default_factory=list, description="capability ids")
    failures: int = 0

    @property
    def trusted(self) -> bool:
        return len(self.confirmed_by) >= CONFIRMATIONS and self.failures == 0

    def describe(self) -> str:
        return " -> ".join(
            f"{s.action} {_handle(s)}" for s in self.steps[:6]
        )


class AppKnowledge(BaseModel):
    """Everything learned about one application."""

    app: str
    prefixes: list[Prefix] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(
        default_factory=list,
        description=(
            "interruptions this application produces and what to do about them. A property "
            "of the app rather than of any one flow, so every capability here inherits them "
            "instead of meeting the same notice and being repaired for it separately."
        ),
    )
    anchors_worked: dict[str, int] = Field(default_factory=dict)
    anchors_failed: dict[str, int] = Field(default_factory=dict)
    tasks_seen: list[str] = Field(default_factory=list)

    # -- learning ----------------------------------------------------------

    def learn_from(self, capability: Capability, dead_anchors: list[str]) -> str | None:
        """Fold one successful capability in. Returns a note on what was learned."""
        if capability.id not in self.tasks_seen:
            self.tasks_seen.append(capability.id)

        for step in capability.steps:
            anchor = _handle(step)
            if anchor:
                self.anchors_worked[anchor] = self.anchors_worked.get(anchor, 0) + 1
        for anchor in dead_anchors:
            self.anchors_failed[anchor] = self.anchors_failed.get(anchor, 0) + 1

        return self._learn_prefix(capability)

    def _learn_prefix(self, capability: Capability) -> str | None:
        """Find the entry sequence by agreement between tasks.

        The insight is that we do not have to recognise a login — we only have
        to notice that every successful task on this app starts the same way.
        Whatever that shared opening is, replaying it is free progress, and it
        needs no knowledge of what the application does.
        """
        for prefix in self.prefixes:
            shared = _common(prefix.steps, capability.steps)
            if len(shared) >= 2:
                prefix.steps = shared
                if capability.id not in prefix.confirmed_by:
                    prefix.confirmed_by.append(capability.id)
                    if prefix.trusted:
                        return (f"the opening {len(shared)} steps are shared by "
                                f"{len(prefix.confirmed_by)} tasks — treating them as how you "
                                f"get into this app")
                return None

        # First task on this app: propose its opening, unconfirmed.
        opening = [s for s in capability.steps[:6]
                   if s.action in ("navigate", "type", "click")]
        if len(opening) >= 2:
            self.prefixes.append(Prefix(name="entry", steps=opening,
                                        confirmed_by=[capability.id]))
        return None

    def learn_recovery(self, recovery: Recovery) -> bool:
        """Remember an interruption, unless it is already known."""
        if any(r.detect.strip().lower() == recovery.detect.strip().lower()
               for r in self.recoveries):
            return False
        self.recoveries.append(recovery)
        return True

    def prefix_failed(self, name: str = "entry") -> None:
        """Demote a prefix that stopped working. Knowledge the loop believes and
        that is no longer true is worse than no knowledge at all."""
        for prefix in self.prefixes:
            if prefix.name == name:
                prefix.failures += 1

    # -- using ------------------------------------------------------------

    @property
    def entry(self) -> Prefix | None:
        return next((p for p in self.prefixes if p.trusted), None)

    def as_prompt(self) -> str:
        """What a new task on this app is told before it starts."""
        parts: list[str] = []

        if self.entry:
            parts.append(
                f"\n\nYou have worked in this application before. Its first "
                f"{len(self.entry.steps)} steps — {self.entry.describe()} — are how every task "
                "here begins, and they have already been replayed for you. Do not repeat them."
            )

        if self.recoveries:
            parts.append("\nThis application interrupts with: "
                         + "; ".join(f"{r.detect!r} (dismiss by clicking "
                                     f"{getattr(r.target, 'name', 'it')!r})"
                                     for r in self.recoveries[:4])
                         + ". Handle these if you meet them.")

        works = [a for a, n in sorted(self.anchors_worked.items(), key=lambda kv: -kv[1])[:10]]
        if works:
            parts.append("\nThese targets are known to resolve in this application: "
                         + ", ".join(repr(a) for a in works) + ".")

        fails = [a for a, n in sorted(self.anchors_failed.items(), key=lambda kv: -kv[1])[:8]]
        if fails:
            parts.append("These have been tried and do not resolve: "
                         + ", ".join(repr(a) for a in fails) + ".")

        return "\n".join(parts)

    def summary(self) -> str:
        entry = self.entry
        return (f"{len(self.tasks_seen)} tasks · {len(self.recoveries)} known interruptions · "
                f"{'entry prefix of ' + str(len(entry.steps)) + ' steps' if entry else 'no entry prefix yet'} · "
                f"{len(self.anchors_worked)} working anchors")


# --------------------------------------------------------------------------


def _handle(step: Step) -> str:
    if not step.target:
        return ""
    target = step.target.target
    return str(getattr(target, "anchor", None) or getattr(target, "name", None) or "")


def _common(a: list[Step], b: list[Step]) -> list[Step]:
    """The longest opening both share, compared on what they do rather than on
    what they were called — two tasks describe the same click differently."""
    shared: list[Step] = []
    for left, right in zip(a, b):
        if left.action != right.action:
            break
        if _handle(left) != _handle(right):
            break
        if (left.value is None) != (right.value is None):
            break
        if left.value and right.value and left.value.from_secret != right.value.from_secret:
            break
        shared.append(left)
    return shared


def load(app: str, root: Path | None = None) -> AppKnowledge:
    path = (root or STORE) / f"{_slug(app)}.json"
    if path.exists():
        try:
            return AppKnowledge.model_validate_json(path.read_text())
        except ValueError:
            pass
    return AppKnowledge(app=app)


def save(knowledge: AppKnowledge, root: Path | None = None) -> Path:
    directory = root or STORE
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(knowledge.app)}.json"
    path.write_text(knowledge.model_dump_json(indent=2))
    return path


def _slug(app: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in app).strip("-")
