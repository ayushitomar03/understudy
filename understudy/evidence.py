"""The run record: what happened, in order, and why.

Every component writes here and nothing reads it back into the decision path —
evidence is for humans, for debugging, and for the dashboard. It is append-only
JSONL so that a run can be watched while it is still going, which is also what
makes the live view possible without any extra plumbing.

Redaction happens on the way in, not as a later pass. A value declared sensitive
never reaches the file, so there is no window in which the secret exists on disk
and no cleanup step that can be forgotten.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

RUNS = Path("evidence/runs")

# Patterns redacted regardless of declaration — a backstop for values that were
# never declared sensitive but obviously are.
def _luhn(digits: str) -> bool:
    """The checksum every real card number satisfies.

    Needed because shape alone cannot tell a card from a timestamp: the run id
    20260914-142454 is thirteen digits with a separator, and a pattern-only rule
    redacted it — inside a *file path*, so the dashboard's screenshots 404'd.
    Redaction that damages identifiers is not a safe default, it is a bug that
    hides in the evidence.
    """
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def replace(match: re.Match) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        return "<card>" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)

    return re.compile(r"\b(?:\d[ -]?){13,19}\d\b").sub(replace, text)


PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<ssn>"),
    # Requires an actual assignment separator. The looser version matched the
    # word "Password" anywhere and redacted whatever followed it — which ate
    # real words out of the candidate lists the model reads to choose a target
    # ('textbox after "Password" <redacted> nothing'), degrading the very
    # information the loop depends on.
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*(\S+)"),
     r"\1=<redacted>"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Redactor:
    """Knows which literal values must never be written down."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def protect(self, value: str | None) -> None:
        """Register a value as unloggable. Called when a sensitive param is bound."""
        if value and len(value) >= 3:
            self._secrets.add(value)

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            out = obj
            for secret in self._secrets:
                out = out.replace(secret, "<redacted>")
            for pattern, repl in PATTERNS:
                out = pattern.sub(repl, out)
            return _redact_cards(out)
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub(v) for v in obj]
        return obj


class EventLog:
    """One run's evidence directory."""

    def __init__(self, kind: str, goal: str, run_id: str | None = None, root: Path | None = None):
        self.run_id = run_id or f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        self.dir = (root or RUNS) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shots = self.dir / "screens"
        self.shots.mkdir(exist_ok=True)
        self.redactor = Redactor()
        self._path = self.dir / "events.jsonl"
        self._seq = 0
        self._t0 = time.monotonic()

        self.emit("run_started", kind=kind, goal=goal, run_id=self.run_id)

    # -- writing -----------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> dict:
        """Append one event. Returns it, so callers can log and use in one step."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "at": _now(),
            "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
            "event": event,
            **self.redactor.scrub(fields),
        }
        with self._path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return record

    def screenshot_path(self, label: str) -> str:
        return str(self.shots / f"{self._seq:03d}-{label}.png")

    def finish(self, status: str, **fields: Any) -> dict:
        return self.emit("run_finished", status=status, **fields)

    # -- reading (the dashboard, and post-hoc inspection) ------------------

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> list[dict]:
        return list(read_events(self._path))


def read_events(path: Path) -> Iterator[dict]:
    """Tolerant reader: a run being watched live can have a partial last line."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # torn write at the tail; it will be complete next poll


def latest_run(root: Path | None = None) -> Path | None:
    root = root or RUNS
    if not root.exists():
        return None
    runs = [d for d in root.iterdir() if d.is_dir() and (d / "events.jsonl").exists()]
    return max(runs, key=lambda d: d.stat().st_mtime, default=None)
