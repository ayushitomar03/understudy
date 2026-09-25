"""What discovery has already tried, and what came of it.

The model sees this every turn. Without it, a model handed only the current
screen will retry the same dead click for three turns running — it has no way to
know it already tried.

Everything recorded here is mechanical. `advanced` means the observation digest
changed; it does not mean the step was correct, and nothing in this module looks
at what the page says. Interpretation is the model's job and arrives as its own
annotation on the attempt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..surface.base import Candidate

# Mechanical outcomes only — see the module note.
Verdict = Literal["advanced", "no_op", "unresolved", "blocked", "timeout", "error"]

NON_ADVANCING = {"no_op", "unresolved", "blocked", "timeout", "error"}


class Attempt(BaseModel):
    turn: int
    action: str
    args: dict = Field(default_factory=dict)
    reason: str = ""  # the model's stated why, verbatim
    verdict: Verdict = "error"
    detail: str = ""
    digest_before: str = ""
    digest_after: str = ""
    candidates: list[Candidate] = Field(default_factory=list)

    @property
    def signature(self) -> str:
        """Identity of the attempt, for spotting exact repeats."""
        parts = [self.action] + [f"{k}={v}" for k, v in sorted(self.args.items()) if k != "reason"]
        return "|".join(parts)

    def line(self) -> str:
        head = f"turn {self.turn}: {self.action}({_args(self.args)}) -> {self.verdict}"
        return f"{head} — {self.detail}" if self.detail else head


class Ledger(BaseModel):
    """Run-scoped memory. Deliberately not persisted across runs — see the
    write-up for why cross-run memory is a separate problem."""

    attempts: list[Attempt] = Field(default_factory=list)
    proven: list[str] = Field(default_factory=list, description="subgoals the model says are done")

    # -- recording ---------------------------------------------------------

    def record(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)

    def prove(self, claim: str) -> None:
        if claim not in self.proven:
            self.proven.append(claim)

    # -- what the model is told --------------------------------------------

    def digest(self, limit: int = 14) -> str:
        """A compact briefing, not a transcript.

        Raw history is expensive and biases the model toward repeating whatever
        it just said. What it needs instead is: what is known not to work, what
        the policy refused, and what is already done.
        """
        if not self.attempts:
            return "Nothing tried yet."

        lines: list[str] = []

        dead = self.dead_ends()
        if dead:
            lines.append("Already tried, did not work:")
            lines += [f"  - {d}" for d in dead[:limit]]

        blocked = [a for a in self.attempts if a.verdict == "blocked"]
        if blocked:
            lines.append("Refused by policy:")
            lines += [f"  - {a.action}({_args(a.args)}) — {a.detail}" for a in blocked[:4]]

        if self.proven:
            lines.append("Established so far:")
            lines += [f"  - {p}" for p in self.proven]

        recent = self.attempts[-4:]
        lines.append("Most recent:")
        lines += [f"  - {a.line()}" for a in recent]

        return "\n".join(lines)

    def dead_ends(self) -> list[str]:
        """Distinct attempts that produced nothing, each described once."""
        seen: dict[str, Attempt] = {}
        for a in self.attempts:
            if a.verdict in NON_ADVANCING and a.verdict != "blocked":
                seen.setdefault(a.signature, a)
        return [f"{a.action}({_args(a.args)}) — {a.verdict}" for a in seen.values()]

    # -- refusing pointless repeats ----------------------------------------

    def already_failed(self, action: str, args: dict) -> Attempt | None:
        """An identical attempt that previously went nowhere.

        The runner refuses these before they cost a turn, and tells the model why
        — a silent no-op would leave it guessing.
        """
        probe = Attempt(turn=0, action=action, args=args).signature
        for a in reversed(self.attempts):
            if a.signature == probe and a.verdict in NON_ADVANCING:
                return a
        return None


def _args(args: dict) -> str:
    keep = {k: v for k, v in args.items() if k != "reason"}
    return ", ".join(f"{k}={v!r}" for k, v in keep.items())
