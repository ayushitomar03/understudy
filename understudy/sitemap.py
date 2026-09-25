"""What the application is, learned once and reused by every task.

Today every task starts knowing nothing and re-derives the same things. Across
our runs the same four sign-on steps were rediscovered more than twenty-five
times, and a repair that taught one capability how to handle a maintenance notice
taught nothing to the next. Both are the same waste: knowledge that belongs to
the *application* being stored per task.

So this is a map of the application, built once:

    screens      what exists, how each one identifies itself, and what is on it
    controls     every control with a locator that RESOLVED against the real page
    transitions  which control leads to which screen
    messages     the strings the app answers with, and what each one means

Two decisions worth defending, because the obvious version of this is worse.

**It is verified, not written.** The obvious knowledge base is prose: "click the
Sign On button, enter the member number in the Member No. field". On this
application that sentence is wrong in three ways at once — the button is an image
whose only name is an attribute, the field has no label tying it to its text, and
both live inside a frame a one-document reader never sees. Model prose about a
screen has been wrong in exactly those silent ways every time we have relied on
it. So nothing enters this map unless the locator was resolved against the page
first, and `verified` says which.

**Messages are classified, not just collected.** A legacy application answers
everything as text on a screen with a 200 status: NO MEMBER ON FILE, NOT
AUTHORISED, RECORD IN USE, SESSION EXPIRED. The single most consequential thing the
system can know is which of those means "the app answered your question", which
means "you got it wrong", and which means "come back in a moment" — because the
correct response to the three is respectively to report it, to stop, and to wait.
Getting that wrong means "fixing" a flow that was already correct. Storing
it per application rather than re-asking a model per failure makes the answer
consistent, which a model call measurably is not: on identical screens, `'ABC'`
was diagnosed as a business outcome and `'40-021'` as an unexpected screen.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .artifact.schema import Locator, Step
from .surface.base import Target

STORE = Path("evidence/sitemaps")

# What a message means, which decides what may be done about it. These are the
# distinctions that change the correct action, and no others: adding a category
# nothing acts on differently is taxonomy for its own sake.
Meaning = Literal[
    "answer",        # the application answered the question. Report it; change nothing.
    "caller_error",  # the input was wrong. The flow is fine; the caller is not.
    "interruption",  # something stands in the way and can be dismissed.
    "busy",          # the app is holding something. Nothing to press; wait.
    "permission",    # not allowed. A person decides; retrying locks accounts.
    "session",       # no longer signed in. Re-authenticate, and beware of replaying
                     # a mutating flow from the top.
    "unknown",       # seen, not yet understood. Better recorded than guessed at.
]

# How it clears, which is the actionable half. `wait` exists because without it
# both held-record tasks scored 0/10.
Clears = Literal["nothing", "click", "wait", "reauthenticate"]


class Message(BaseModel):
    """A string the application answers with, and what to do about it."""

    text: str = Field(description="the shortest distinctive substring that identifies it")
    means: Meaning = "unknown"
    clears_by: Clears = "nothing"
    control: str | None = Field(default=None, description="what to press, when clears_by=click")
    seconds: float = Field(default=2.0, description="how long to wait, when clears_by=wait")
    seen_on: list[str] = Field(default_factory=list, description="screen ids")
    why: str = ""

    @property
    def actionable(self) -> bool:
        """Whether a flow meeting this should do something other than stop.

        An answer and a caller error are not defects: a flow that "recovers" from
        NO MEMBER ON FILE has invented a record. Permission is not retried, ever.
        """
        return self.means in ("interruption", "busy", "session")


class Control(BaseModel):
    """Something on a screen that can be acted on, with a way to find it."""

    name: str = Field(description="what it says, as an operator would name it")
    role: str = ""
    locator: Locator
    leads_to: str | None = Field(default=None, description="screen id this opens, if known")
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.locator.verified


class Value(BaseModel):
    """A labelled value a screen displays, and how to read it."""

    label: str
    locator: Locator
    example: str = Field(default="", description="what it held when the map was built")


class Table(BaseModel):
    """A table on a screen, described by its columns rather than its contents.

    A labelled value and a table column look the same in markup and mean
    opposite things: `Savings Bal.` names one cell, `Amount` names a column
    whose cells differ per row and per member. Recording columns separately is
    what lets a plan say "the third row of Amount" instead of reading whichever
    row came first — the failure that answered `14/09` when asked for the oldest
    item. Columns are read off the page, never written down by a model.
    """

    columns: list[str] = Field(description="header cells, in order")
    rows_when_mapped: int = Field(
        default=0, description="how many data rows it held when surveyed, for context only")
    summary: str = Field(
        default="",
        description=(
            "the label of the line beside the table where the application states the count "
            "itself — 'Items shown', 'Active orders'. Worth more than the rows: it is right "
            "when the table is truncated, and it is the count the application means."))

    def column(self, wanted: str) -> str | None:
        want = "".join(c for c in wanted.lower() if c.isalnum())
        for name in self.columns:
            bare = "".join(c for c in name.lower() if c.isalnum())
            if bare and (bare == want or bare.startswith(want) or want.startswith(bare)):
                return name
        return None


class Screen(BaseModel):
    """One state of the application."""

    id: str = Field(description="short slug, e.g. member_record")
    title: str = Field(default="", description="the heading an operator would name it by")
    identifies_by: str = Field(
        description=(
            "the shortest text that is on this screen and no other. Used to tell where a "
            "flow actually is, which is the question a legacy app answers only in prose."
        )
    )
    url: str = Field(default="", description="relative path, when it has a stable one")
    controls: list[Control] = Field(default_factory=list)
    values: list[Value] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list, description="message texts seen here")
    needs_signin: bool = True
    note: str = ""

    def control(self, name: str) -> Control | None:
        want = name.strip().lower()
        return next((c for c in self.controls if c.name.strip().lower() == want), None)


class SiteMap(BaseModel):
    """Everything known about one application, verified against it."""

    app: str
    product: str = ""
    built_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    built_by: str = Field(default="", description="the run that produced it")
    screens: list[Screen] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    entry: list[Step] = Field(
        default_factory=list,
        description="the steps that get from nothing to a signed-on screen, verified",
    )
    entry_lands_on: str = ""

    # -- reading -----------------------------------------------------------

    def screen(self, id_or_title: str) -> Screen | None:
        want = id_or_title.strip().lower()
        return next((s for s in self.screens
                     if s.id.lower() == want or s.title.lower() == want), None)

    def message(self, text: str) -> Message | None:
        """The message matching what is on a page, longest first.

        Longest first because the specific string must win: an app with both
        "NOT AUTHORISED" and "NOT AUTHORISED FOR THIS BRANCH" would otherwise
        classify the second as the first and stop when it should escalate.
        """
        page = text.lower()
        for msg in sorted(self.messages, key=lambda m: -len(m.text)):
            if msg.text.lower() in page:
                return msg
        return None

    def where(self, tree: str) -> Screen | None:
        """Which screen this page is, by its own identifying text."""
        page = tree.lower()
        for screen in sorted(self.screens, key=lambda s: -len(s.identifies_by)):
            if screen.identifies_by and screen.identifies_by.lower() in page:
                return screen
        return None

    @property
    def verified_controls(self) -> int:
        return sum(1 for s in self.screens for c in s.controls if c.verified)

    @property
    def total_controls(self) -> int:
        return sum(len(s.controls) for s in self.screens)

    def summary(self) -> str:
        classified = sum(1 for m in self.messages if m.means != "unknown")
        return (f"{len(self.screens)} screens · "
                f"{self.verified_controls}/{self.total_controls} controls verified · "
                f"{classified}/{len(self.messages)} messages classified · "
                f"{'entry of ' + str(len(self.entry)) + ' steps' if self.entry else 'no entry'}")

    # -- what a task is told ----------------------------------------------

    def as_prompt(self, goal: str = "") -> str:
        """The map, written for the model that is about to record a task.

        Deliberately not the whole map. A model given forty screens spends its
        attention on reading rather than acting, and the screens it needs are
        usually few — so this leads with how to get in and what the app answers
        with, which every task needs, and then the screens.
        """
        parts = ["\n\n--- what is already known about this application ---"]

        if self.entry:
            parts.append(
                f"\nGetting in: {len(self.entry)} verified steps, already replayed for you. "
                f"They land on {self.entry_lands_on or 'the first screen after sign-on'}. "
                "Do not repeat them."
            )

        actionable = [m for m in self.messages if m.actionable]
        if actionable:
            lines = []
            for m in actionable:
                how = {"click": f"dismiss it by clicking {m.control!r}",
                       "wait": f"wait about {m.seconds:g}s and try again — there is nothing "
                               "to press",
                       "reauthenticate": "sign on again",
                       "nothing": "it does not clear"}[m.clears_by]
                lines.append(f"  - {m.text!r}: {how}")
            parts.append("\nThis application interrupts with these, and they are handled, "
                         "not reported:\n" + "\n".join(lines))

        final = [m for m in self.messages if not m.actionable and m.means != "unknown"]
        if final:
            lines = []
            for m in final:
                how = {"answer": "this is the application's answer — report it and stop",
                       "caller_error": "the input was wrong — report it and stop",
                       "permission": "not allowed — stop and say so; do not retry"}.get(
                           m.means, "stop")
                lines.append(f"  - {m.text!r}: {how}")
            parts.append("\nThese are answers, not faults. Do not work around them:\n"
                         + "\n".join(lines))

        for screen in self.screens:
            controls = ", ".join(f"{c.name!r}" + (f" -> {c.leads_to}" if c.leads_to else "")
                                 for c in screen.controls if c.verified)
            values = ", ".join(v.label for v in screen.values)
            tables = "; ".join(", ".join(t.columns) for t in screen.tables)
            body = f"\n  {screen.id}"
            if screen.url:
                body += f" at {screen.url}"
            body += f" — recognise it by {screen.identifies_by!r}"
            if controls:
                body += f"\n    controls: {controls}"
            if values:
                body += f"\n    shows: {values}"
            if tables:
                body += f"\n    table columns: {tables}"
            parts.append(body)

        parts.append("\nEvery locator here resolved against the real page when the map was "
                     "built. Prefer them over guessing, and say so if one no longer resolves.")
        return "\n".join(parts)


def scrub(sitemap: SiteMap, secrets: dict[str, str]) -> list[str]:
    """Remove any credential the survey wrote down. Returns what was removed.

    A `Value.example` records what a field held when the map was built, which is
    useful for every field on the application except two: on the sign-on screen
    those fields held the operator id and the pass code, so the map came out
    carrying the password as data. It was found by grepping the repository before
    publishing it, which is not a control anyone should rely on.

    The rule is exact rather than a guess at which fields look secret: a surveyor
    is *given* the credentials it signs on with, so anything equal to one of them
    is a credential, wherever it turns up. Fields are compared whole — a balance
    that happens to contain a short pass code is not a leak, and blanking it
    would be.
    """
    values = {v for v in secrets.values() if v}
    removed: list[str] = []
    if not values:
        return removed
    for screen in sitemap.screens:
        for value in screen.values:
            if value.example in values:
                removed.append(f"{screen.id}.{value.label}")
                value.example = ""
        for table in screen.tables:
            table.summary = "" if table.summary in values else table.summary
    return removed


# -- persistence -----------------------------------------------------------


def _slug(app: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in app).strip("-")


def load(app: str, root: Path | None = None) -> SiteMap | None:
    path = (root or STORE) / f"{_slug(app)}.json"
    if not path.exists():
        return None
    try:
        return SiteMap.model_validate_json(path.read_text())
    except ValueError:
        return None


def save(sitemap: SiteMap, root: Path | None = None) -> Path:
    directory = root or STORE
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_slug(sitemap.app)}.json"
    path.write_text(sitemap.model_dump_json(indent=2))
    return path
