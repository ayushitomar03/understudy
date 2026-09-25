"""The seam between "how we perceive and act on a surface" and everything above it.

Nothing above this module may know that the surface is a browser. The artifact
records *intent* — a role and an anchor, never a CSS selector — and each Surface
implementation is responsible for resolving that intent on its own platform. A
desktop surface would implement the same three methods against the OS
accessibility API without the schema changing.

Two design notes that the ParaBank reconnaissance forced (see evidence/probe/):

  * Targets are a small, closed vocabulary of *strategies*, not selectors. The
    app's inputs carry no accessible name, so `AfterText` — the first control of
    a given role following some visible text — is the primary strategy here, not
    a fallback. It is also the one that ports to desktop, where "the field after
    this label" is equally meaningful.

  * A failed resolution returns the candidates it could see. This is the loop's
    entire learning mechanism: `not found` teaches the model nothing, whereas
    `not found — nearby: textbox after "Username", button "Log In"` turns a dead
    end into a menu. The runner never interprets the page, but it is obliged to
    describe it.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Protocol, Sequence, Union

from pydantic import BaseModel, Field

# Every role perception can advertise must be expressible as a Target. When
# these drift apart the model is offered controls it cannot name — observed on
# phpLDAPadmin, where `checkbox after "Anonymous"` was advertised and then
# rejected as an invalid role, costing two turns and pushing the model to CSS.
Role = Literal["textbox", "combobox", "button", "link", "checkbox", "radio",
               "heading", "cell", "row", "option"]

# Per-request values that change without the page changing. Stripped before
# hashing an observation so that "did anything happen?" stays answerable.
# The leading separator is part of the match on purpose: ParaBank writes
# "index.htm;jsessionid=ABC" cold and "index.htm" warm, so dropping the token
# but keeping the ";" would still hash the two differently.
VOLATILE = re.compile(
    r"[;?&]?(?:jsessionid|sessionid|session_id|csrf[_-]?token|_token|nonce|requestid)"
    r"\s*[=:]\s*[A-Za-z0-9._~+/-]+",
    re.IGNORECASE,
)


def _scrub(tree: str) -> str:
    """Remove volatile tokens so that equal pages hash equal."""
    return VOLATILE.sub("", tree)


# --------------------------------------------------------------------------
# targeting
# --------------------------------------------------------------------------


class RoleName(BaseModel):
    """A control identified by its role and accessible name. The most portable
    strategy, and the first one worth trying — when the app provides names."""

    kind: Literal["role_name"] = "role_name"
    role: Role
    name: str
    exact: bool = False
    frame: str | None = None


class AfterText(BaseModel):
    """The nth control of `role` following `anchor` in reading order.

    ParaBank's forms leave inputs unnamed and label them with a preceding
    paragraph, so this carries most of the weight on this app. The anchor is
    visible text, which means it is also the thing most likely to differ between
    tenants running the same product — a tradeoff recorded honestly rather than
    hidden, and the reason `Ordinal` exists as an alternative.
    """

    kind: Literal["after_text"] = "after_text"
    anchor: str
    role: Role
    index: int = 1


class Ordinal(BaseModel):
    """The nth control of a role on the page, counting from 1.

    Positional and therefore brittle, but it survives text changes — which makes
    it the better choice precisely where `AfterText` is weakest, such as an
    anchor containing a tenant-configurable amount.
    """

    kind: Literal["ordinal"] = "ordinal"
    role: Role
    index: int


class Css(BaseModel):
    """Last resort. Web-only by construction, so recording one is an admission
    that the flow will not port to another surface unchanged."""

    kind: Literal["css"] = "css"
    selector: str


class InTable(BaseModel):
    """A cell in a table, found by what it is rather than by what it says.

    `AfterText('Amount')` on a table reads the cell after the column *header* —
    row one, whichever row that happens to be. That is how a replay reported
    14/09 as the oldest posted item and called it a success. This names the
    column and says which row structurally, so the same locator means the same
    thing for a member with three items and a member with none.
    """

    kind: Literal["in_table"] = "in_table"
    column: str = Field(description="the column header, exactly as it is written")
    row: int = Field(default=1, description="1-based data row; -1 is the last")
    where: str | None = Field(
        default=None,
        description="instead of a position, the data row containing this text")


class RowCount(BaseModel):
    """How many data rows a table has. Reading it returns the number.

    Counting is not reading a cell, and pretending it is was the other half of
    the same failure: `how many items are posted` was answered by reading a row
    and reporting whatever was in it.
    """

    kind: Literal["row_count"] = "row_count"
    column: str = Field(description="any column header of the table to count")
    summary: str | None = Field(
        default=None,
        description=(
            "the label of the line where the application states the count itself, such as "
            "'Items shown' or 'Active orders'. Preferred when present: it survives a table "
            "that is paged or truncated, and it is the application's own arithmetic rather "
            "than ours — 'Active orders: 0' is the answer to how many are active, which "
            "counting the rows of a table holding one cancelled order is not."))


class AnyOf(BaseModel):
    """Whichever of these strings the page is showing.

    Some answers are not in a field. A refused lookup says NOT AUTHORISED, an
    absent record says NO MEMBER ON FILE, and which one appears depends on the
    argument — so a locator pinned to one of them works for one member and fails
    for the next. The survey already classified every message this application
    gives; this reads whichever of them is on screen and returns that message,
    not the surrounding text.
    """

    kind: Literal["any_of"] = "any_of"
    texts: list[str] = Field(description="the known messages, matched longest first")


Target = Union[RoleName, AfterText, Ordinal, Css, InTable, RowCount, AnyOf]


# --------------------------------------------------------------------------
# observation
# --------------------------------------------------------------------------


class Candidate(BaseModel):
    """One interactable thing the surface can see, described the way the model
    would need to target it."""

    role: str
    name: str | None = None
    after_text: str | None = None  # nearest preceding text, for unnamed controls
    value: str | None = None       # what a form field currently holds
    frame: str | None = None       # which document it lives in, on a frameset app
    ordinal: int

    def describe(self) -> str:
        """How the model would name this control, plus what it contains.

        The value matters on edit-in-place admin consoles, where data is shown
        in input boxes and the labels are useless — phpLDAPadmin labels an LDAP
        attribute with its one-letter name, so `textbox #4` is untargetable in
        practice while `textbox #4 holding "First Valley CU"` is obvious.
        """
        if self.name:
            base = f'{self.role} "{self.name}"'
        elif self.after_text:
            base = f'{self.role} after "{self.after_text}"'
        else:
            base = f"{self.role} #{self.ordinal}"
        if self.value:
            base += f' holding "{self.value[:40]}"'
        if self.frame:
            base += f" in frame {self.frame}"
        return base


class Observation(BaseModel):
    """What the surface looks like right now."""

    url: str
    tree: str  # the accessibility tree, as text
    candidates: list[Candidate] = Field(default_factory=list)
    screenshot: str | None = None  # path, not bytes — evidence lives on disk
    stable: bool = True  # False when the page never stopped changing

    @property
    def digest(self) -> str:
        """Stable hash of what is on screen, used to tell "something happened"
        from "nothing happened".

        Two things are deliberately excluded. The URL, because ParaBank rewrites
        paths with a jsessionid and a changed URL does not mean a changed page.
        And session tokens *inside* the tree, because they appear in link hrefs
        until the session cookie is established — measured on ParaBank, the first
        load of a page hashes differently from every later load of the same page.

        Legacy enterprise apps are full of per-request tokens like this, and any
        of them landing in the digest would make every action look like it
        advanced. The scrubbing applies to hashing only: the model always sees
        the unmodified tree.
        """
        return hashlib.sha256(_scrub(self.tree).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# acting
# --------------------------------------------------------------------------


class Resolution(BaseModel):
    """The outcome of pointing a Target at a live page.

    `name` and `role` describe what was *actually found*, not what was asked
    for. Policy classifies on these: a gate that reads the request instead of
    the element has a hole shaped like "use a different targeting strategy",
    and that hole was exploited in a live run — a click refused as
    role_name "Send Payment" went through as css input[value="Send Payment"],
    and moved $5.
    """

    found: bool
    describe: str | None = None
    name: str | None = None   # accessible name of the element that matched
    role: str | None = None   # its role
    candidates: list[Candidate] = Field(default_factory=list)
    ambiguous: int = 0  # >1 when the target matched several things


class ActionResult(BaseModel):
    """Mechanical facts about an attempt. No interpretation of page content —
    that judgement belongs to the model, not to the runner."""

    ok: bool
    detail: str = ""
    resolution: Resolution | None = None
    value: str | None = None  # for reads
    error: str | None = None


# --------------------------------------------------------------------------
# the seam itself
# --------------------------------------------------------------------------


class Surface(Protocol):
    """Three methods. Everything above this line is platform-neutral."""

    def observe(self) -> Observation: ...

    def resolve(self, target: Target) -> Resolution: ...

    def navigate(self, url: str) -> ActionResult: ...

    def click(self, target: Target) -> ActionResult: ...

    def type(self, target: Target, text: str) -> ActionResult: ...

    def select(self, target: Target, option: str) -> ActionResult: ...

    def read(self, target: Target) -> ActionResult: ...

    def screenshot(self, path: str) -> str: ...

    def close(self) -> None: ...


def summarise(candidates: Sequence[Candidate], limit: int = 12) -> str:
    """Render candidates for the model. Used verbatim in failure messages, which
    is what makes a failed attempt informative rather than just discouraging."""
    shown = [c.describe() for c in candidates[:limit]]
    extra = len(candidates) - len(shown)
    tail = f", +{extra} more" if extra > 0 else ""
    return ", ".join(shown) + tail
