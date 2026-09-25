"""Capabilities kept by the shape of the task, so a familiar task is free.

The map tells a model how the application works. That cuts the turns a task
costs, but it does not cut them to zero: the model still has to be asked. This
is what takes the second run of a task the system has already done and charges
nothing for it.

The key is the *shape* of the goal, not the goal: "look up member 40021 and
report the savings balance" and the same sentence about member 40055 are one
task asked twice, and a store keyed on the sentence would miss that. So the
argument values are masked back out of the goal before it is keyed — which
works in production because a caller that passed arguments knows what they
were.

Two properties worth stating, because the tempting version of this is worse:

  * **Nothing is cached that was not observed to work.** An entry here is a
    recording of a run that completed and returned the values it was asked for.
    A plan synthesised from the map and never executed is a hypothesis, and
    caching hypotheses is how a store becomes confidently wrong.

  * **An entry that fails is deleted, not retried.** Replay is cheap, so a
    stale recording costs one failed replay to discover. Keeping it costs one
    on every later call, and the model would have had to run anyway.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .artifact.schema import Capability, Locator

STORE = Path("evidence/cache")

# A label an operator would name a field by is short. Anything longer than this
# is the screen's contents rather than its furniture, and the contents are what
# changes between one argument and the next.
LABEL = 30


def shape(goal: str, params: dict[str, str], app: str = "") -> str:
    """A key for the task this is an instance of.

    Longest values first: masking '4' before '40021' would leave '{x}0021'.
    """
    masked = goal
    for name, value in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
        if str(value):
            masked = masked.replace(str(value), "{" + name + "}")
    masked = re.sub(r"\s+", " ", masked.strip().lower())
    digest = hashlib.sha1(f"{_slug(app)}|{masked}".encode()).hexdigest()[:8]
    return f"{_words(masked)[:70]}-{digest}"


def fit(capability: Capability, sitemap=None) -> tuple[Capability | None, str]:
    """Whether this recording will still read the right thing for other arguments.

    The three recordings dropped in the first measured run failed the same way,
    and none of them failed on the hazards. Each had been anchored to the answer
    the recording run happened to get:

        role=row name='Date Type Amount 11/09 DEPOSIT 128.00 Items shown: 1 of 1'
        role=cell name='NO MEMBER ON FILE FOR {member_no}'

    The first is member 40055's only posted item, and member 40204 has none. The
    second is one of two refusals this application gives, and the restricted
    member gets the other. Both were recorded, both replayed, both found nothing,
    and each cost a wasted replay *and* the model run it was meant to save — which
    is how a cache ends up more expensive than no cache at all.

    The recordings that survived were anchored to a label: `after_text 'Savings
    Bal.'`, `after_text 'Telephone'`. So a read is trusted when it is anchored to
    the furniture of the screen and suspected when it is anchored to the data.

    Suspicion is not the end of it. The survey already recorded a verified
    label-anchored locator for every field on every screen, so where the map has
    one for this output, it is swapped in and the recording is kept. Only when the
    map has nothing to offer is the recording declined — and declining costs
    exactly what not caching costs, which is the price the model arm pays anyway.
    """
    notes: list[str] = []
    fixed = capability.model_copy(deep=True)

    for output in fixed.outputs:
        why = _risky(output.locator, sitemap)
        if not why:
            continue
        better = _labelled(sitemap, output.name) if sitemap else None
        if better is None:
            return None, f"{output.name}: {why}, and the map has no labelled read for it"
        _swap(fixed, output.locator, better)
        output.locator = better.model_copy(deep=True)
        notes.append(f"{output.name}: {why} — using the map's {_text(better)!r}")

    return fixed, "; ".join(notes) or "reads are label-anchored"


def _risky(locator: Locator, sitemap=None) -> str:
    """Why this read is unlikely to survive a different argument, if it is."""
    text = _text(locator)
    kind = getattr(locator.target, "kind", "")
    if kind in ("in_table", "row_count", "any_of"):
        # Structural by construction: a column and a row position, the rows of a
        # named table, or the application's own message vocabulary. None of them
        # is anchored to what this run happened to read, which is the only thing
        # this check exists to catch.
        return ""
    if not text:
        return "the read has no anchor"
    if kind == "after_text" and len(text) <= LABEL:
        if sitemap is not None and not _is_a_field_label(
                sitemap, text, getattr(locator.target, "role", "")):
            return (f"anchored to {text[:30]!r}, which the survey never recorded as a "
                    "field label — on a table that reads row one, not the answer")
        return ""
    if sitemap is not None and sitemap.message(text):
        return f"anchored to an answer, {text[:40]!r}"
    if any(c.isdigit() for c in text):
        return f"anchored to data, {text[:40]!r}"
    if len(text) > LABEL:
        return f"anchor is screen contents, not a label: {text[:40]!r}"
    return ""


def _is_a_field_label(sitemap, text: str, role: str = "") -> bool:
    """Whether the survey saw this as the name of one value, not a column header.

    `after_text 'Savings Bal.'` and `after_text 'Amount'` are the same shape and
    mean opposite things. The first is a form label naming one cell. The second
    is a column header, and the cell after it is whichever row happens to come
    first — which is how a replay returned 14/09 for the *oldest* posted item and
    reported success. A drop is recoverable; a confident wrong answer is not, and
    this is the only distinction available for free: the survey recorded which
    labels name a value, and a table's headers are not among them — and where a
    header borrows a real label's name, what kind of control it is settles it.
    """
    want = _bare(text)
    for screen in getattr(sitemap, "screens", []):
        for value in screen.values:
            if _bare(value.label) != want:
                continue
            # The role settles a collision the name cannot: `Amount` is the
            # transfer form's textbox *and* the posted-items column header, and
            # only the first names one value. A read that agrees with the survey
            # on the name but not on what kind of thing it is, is the other one.
            if not role or role == getattr(value.locator.target, "role", ""):
                return True
    return False


def _labelled(sitemap, name: str) -> Locator | None:
    """A verified, label-anchored read for this output, from the survey."""
    for screen in getattr(sitemap, "screens", []):
        for value in screen.values:
            if value.locator.verified and _same(name, value.label):
                if not _risky(value.locator):
                    return value.locator
    return None


def _swap(capability: Capability, old: Locator, new: Locator) -> None:
    """Point the read step at the same place the output now reads from."""
    want = _text(old)
    for step in capability.steps:
        if step.action == "read" and step.target and _text(step.target) == want:
            step.target = new.model_copy(deep=True)


def _text(locator: Locator) -> str:
    target = locator.target
    return str(getattr(target, "anchor", None) or getattr(target, "name", None) or "")


def _same(name: str, label: str) -> bool:
    """`savings` is `Savings Bal.`; `item_count` is not `Items`."""
    a, b = _bare(name), _bare(label)
    return bool(a) and bool(b) and (a == b or b.startswith(a) or a.startswith(b))


def _bare(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def load(key: str, root: Path | None = None) -> Capability | None:
    path = (root or STORE) / f"{key}.json"
    if not path.exists():
        return None
    try:
        return Capability.model_validate_json(path.read_text())
    except ValueError:
        path.unlink(missing_ok=True)      # unreadable is the same as absent
        return None


def save(capability: Capability, key: str, root: Path | None = None) -> Path:
    directory = root or STORE
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    path.write_text(capability.model_dump_json(indent=2))
    return path


def forget(key: str, root: Path | None = None) -> bool:
    """Drop a recording that stopped working. Returns whether one was there."""
    path = (root or STORE) / f"{key}.json"
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def known(root: Path | None = None) -> list[str]:
    directory = root or STORE
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text).strip("-")


def _words(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-")
