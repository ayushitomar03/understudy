"""A browser Surface, built on Playwright.

This module is the only place in the system allowed to know about the DOM, CSS,
XPath or Playwright. Everything it exposes upward is in the vocabulary of
base.py — roles, names, anchors — so the artifact never records anything that
could not also be said about a desktop window.

Where the mapping is lossy it is lossy here, deliberately and in one place.
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError, sync_playwright

from .base import (
    ActionResult,
    AfterText,
    AnyOf,
    InTable,
    RowCount,
    Candidate,
    Css,
    Observation,
    Ordinal,
    Resolution,
    RoleName,
    Target,
    summarise,
)

# How each abstract role is realised in HTML. Callers name roles; only this
# table knows about tags.
#
# The read-only roles matter as much as the interactive ones: ParaBank renders
# account data as label/value cell pairs, so "the cell after the text Balance:"
# is how an output gets extracted. Omitting them made reads raise instead of
# resolve.
ROLE_XPATH = {
    "textbox": "(self::input[not(@type) or @type='text' or @type='password'] | self::textarea)",
    "combobox": "self::select",
    "button": "(self::button | self::input[@type='submit' or @type='button'])",
    "link": "self::a[@href]",
    "checkbox": "self::input[@type='checkbox']",
    "radio": "self::input[@type='radio']",
    "cell": "(self::td | self::th)",
    "row": "self::tr",
    "heading": "(self::h1 | self::h2 | self::h3 | self::h4 | self::h5 | self::h6)",
    "option": "self::option",
}

# One line of an aria snapshot: "- button \"Log In\"", "- textbox",
# "- paragraph: Username", "- heading \"Account Details\" [level=1]".
NODE = re.compile(r'^\s*-\s+([a-z]+)(?:\s+"([^"]*)")?(?:\s+\[[^\]]*\])?:?\s*(.*)$')

# What the model may point at.
TARGETABLE = {"textbox", "combobox", "button", "link", "checkbox", "radio", "option"}

# What an unnamed control can be anchored to.
TEXT_ROLES = {"paragraph", "text", "cell", "heading"}

# Shorter than this is punctuation, not a label — ParaBank's Find Transactions
# form anchors four different fields on ":" alone.
MIN_ANCHOR = 3

DEFAULT_TIMEOUT = 5_000

# Observation stability: how many times to re-read the tree waiting for it to
# stop changing, and how long to pause between reads.
STABILITY_RETRIES = 4
STABILITY_PAUSE_MS = 120


class WebSurface:
    """A live browser page, presented as a Surface."""

    def __init__(self, headless: bool = True, viewport: tuple[int, int] = (1280, 900)):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._page: Page = self._browser.new_page(
            viewport={"width": viewport[0], "height": viewport[1]}
        )
        self._page.set_default_timeout(DEFAULT_TIMEOUT)

    # -- frames ------------------------------------------------------------

    def _documents(self) -> list[tuple[str, Any]]:
        """Every document on the page, named.

        A frameset has no <body> of its own — the content lives in child
        documents — so anything that reads one document sees nothing at all.
        Measured on a frameset app: `locator("body")` timed out after five
        seconds and the surface could not perceive the page.

        Frames are named where the markup names them, because the name is the
        stable handle: a flow that acts in the navigation frame and one that
        acts in the work frame are different flows, and the artifact has to be
        able to say which.
        """
        page = self._page
        if not page.frames:
            return [("", page.main_frame)]

        out: list[tuple[str, Any]] = []
        for frame in page.frames:
            try:
                if frame.parent_frame is None and len(page.frames) > 1:
                    # The frameset shell itself holds no content.
                    if frame.locator("frameset").count():
                        continue
                name = frame.name or ""
                if not name and frame.parent_frame is not None:
                    name = (frame.url or "").rsplit("/", 1)[-1][:24]
                out.append((name, frame))
            except PlaywrightError:
                continue
        return out or [("", page.main_frame)]

    @staticmethod
    def _root(frame) -> Any:
        """The element to snapshot. Frameset shells have no body."""
        body = frame.locator("body")
        return body if body.count() else frame.locator(":root")

    # -- perception --------------------------------------------------------

    def observe(self) -> Observation:
        """Snapshot the page, waiting for it to stop changing first.

        This is not defensive padding. The `no_op` verdict — did this action do
        anything? — is computed by comparing observation digests, so an
        observation taken mid-render reports a change that never happened, and
        every action starts looking successful. Measured on ParaBank's overview
        page, a bare domcontentloaded read returned 110 or 180 tree lines
        depending on whether the accounts table had arrived yet.

        When the page genuinely will not settle, say so rather than returning a
        digest nothing should be compared against.
        """
        tree = self._snapshot()
        stable = False
        for _ in range(STABILITY_RETRIES):
            self._page.wait_for_timeout(STABILITY_PAUSE_MS)
            again = self._snapshot()
            if again == tree:
                stable = True
                break
            tree = again

        return Observation(
            url=self._page.url,
            tree=tree,
            candidates=self._candidates(),
            stable=stable,
        )

    def _snapshot(self) -> str:
        """The accessibility tree of the whole page, frames included.

        Each document is introduced by a marker so that a reader — the model —
        can tell that two controls with the same name live in different frames.
        """
        documents = self._documents()
        parts = []
        for name, frame in documents:
            try:
                snapshot = self._root(frame).aria_snapshot()
            except PlaywrightError:
                continue
            if len(documents) > 1:
                parts.append(f"# frame: {name or 'unnamed'}\n{snapshot}")
            else:
                parts.append(snapshot)
        return "\n".join(parts)

    def _candidates(self) -> list[Candidate]:
        """Everything targetable, read from the same view that does the targeting.

        This used to walk the DOM and compute accessible names by hand, which
        meant two different name algorithms: ours checked `title` first, while
        `get_by_role` follows the accname spec and takes an image link's name
        from the img `alt`. On phpLDAPadmin — `<a title="Login to ldap"><img
        alt="login"></a>` — we advertised "Login to ldap" to the model and then
        could not resolve it. The model was being shown controls that did not
        exist as far as targeting was concerned.

        Deriving candidates from the aria snapshot removes the possibility
        rather than fixing the instance: it is literally what `get_by_role`
        matches against, so anything described here is reachable.
        """
        counts: dict[str, int] = {}
        out: list[Candidate] = []
        documents = self._documents()

        for frame_name, frame in documents:
            try:
                snapshot = self._root(frame).aria_snapshot()
            except PlaywrightError:
                continue
            found = self._candidates_in(snapshot, counts,
                                        frame_name if len(documents) > 1 else None)
            # Verify inside the document the controls came from. Checking them
            # against the top-level page silently downgraded every anchor on a
            # frameset app — the elements are simply not in that document, so
            # nothing resolved and every labelled field became a bare ordinal.
            out += self._verify(found, frame)
        return out

    def _candidates_in(self, snapshot: str, counts: dict[str, int],
                       frame: str | None) -> list[Candidate]:
        out: list[Candidate] = []
        last_text: str | None = None

        for line in snapshot.splitlines():
            match = NODE.match(line)
            if not match:
                continue
            role, name, trailing = match.group(1), match.group(2), (match.group(3) or "").strip()

            # Text-bearing nodes are not targets, but they are what unnamed
            # controls get anchored to.
            if role in TEXT_ROLES:
                # Keep the text exactly as it renders. Trimming the trailing
                # colon off "Payee Name:" produced an anchor that matched no
                # DOM text node, quietly making ten Bill Pay fields
                # unreachable while still advertising them.
                content = (name or trailing).strip(' "')
                if len(content) >= MIN_ANCHOR:
                    last_text = content
                continue

            if role not in TARGETABLE:
                continue

            counts[role] = counts.get(role, 0) + 1
            out.append(Candidate(
                role=role,
                name=name or None,
                after_text=None if name else last_text,
                # A snapshot renders a filled field as `textbox: some value`.
                value=trailing or None,
                frame=frame,
                ordinal=counts[role],
            ))
        return out

    def _verify(self, candidates: list[Candidate], frame=None) -> list[Candidate]:
        """Drop any anchor that does not actually resolve.

        The invariant is that everything described to the model is reachable by
        what we described. Anchors are derived from the accessibility snapshot
        while resolution runs against the DOM, so a mismatch is always possible
        — legacy markup produces plenty. Rather than chasing each quirk, the
        anchor is tested and silently downgraded to an ordinal when it fails,
        which is always resolvable.

        Checked in one page.evaluate rather than one resolve per candidate:
        observe() runs three times per action, so fifty round trips would cost
        more than the check is worth.
        """
        anchored = [(i, c) for i, c in enumerate(candidates)
                    if not c.name and c.after_text and c.role in ROLE_XPATH]
        if not anchored:
            return candidates

        paths = [_anchor_xpath(c.after_text, ROLE_XPATH[c.role], 1) for _, c in anchored]
        scope = frame if frame is not None else self._page
        try:
            ok = scope.evaluate(
                """paths => paths.map(p => {
                    try {
                        const r = document.evaluate(p, document, null,
                            XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                        return !!r.singleNodeValue;
                    } catch (e) { return false; }
                })""", paths)
        except PlaywrightError:
            return candidates

        for (index, _), resolved in zip(anchored, ok):
            if not resolved:
                candidates[index].after_text = None   # falls back to the ordinal
        return candidates

    # -- targeting ---------------------------------------------------------

    def _locator(self, target: Target, frame=None) -> Locator:
        scope = frame if frame is not None else self._frame_for(target)
        if isinstance(target, RoleName):
            return scope.get_by_role(target.role, name=target.name, exact=target.exact)
        if isinstance(target, AfterText):
            step = ROLE_XPATH[target.role]
            anchor = _xq(target.anchor)
            # Match the innermost element whose *whole* text is the anchor, not
            # its first text node. Legacy markup wraps labels — <td><b>Find by
            # Amount</b>:</td> — so the visible label is spread across nodes and
            # a text()-based match finds neither half. The not(.//*) clause
            # keeps it to the innermost such element, since every ancestor up to
            # <body> also contains that text.
            # normalize-space() collapses ASCII whitespace only, and legacy
            # table markup is built out of &nbsp;. Without translating U+00A0
            # first, "Select an account:&nbsp;" never equals "Select an
            # account:" and the field is advertised but unreachable.
            return scope.locator("xpath=" + _anchor_xpath(target.anchor, step, target.index))
        if isinstance(target, Ordinal):
            step = ROLE_XPATH[target.role]
            return scope.locator(f"xpath=(//*[{step}])[{target.index}]")
        if isinstance(target, Css):
            return scope.locator(target.selector)
        if isinstance(target, InTable):
            return scope.locator("xpath=" + _table_cell_xpath(target))
        if isinstance(target, RowCount):
            return scope.locator("xpath=" + _table_rows_xpath(target.column))
        if isinstance(target, AnyOf):
            return scope.locator("xpath=" + _any_of_xpath(target.texts))
        raise TypeError(f"unknown target: {target!r}")

    def resolve(self, target: Target) -> Resolution:
        loc, count = None, 0
        wanted = getattr(target, "frame", None)
        try:
            for name, frame in self._documents():
                # A target may name its frame. When it does not, every document
                # is searched in order — the common case, and the one that keeps
                # artifacts recorded on single-document apps working unchanged.
                if wanted and name != wanted:
                    continue
                candidate = self._locator(target, frame)
                if candidate.count():
                    loc, count = candidate, candidate.count()
                    break
        except PlaywrightError as exc:
            return Resolution(found=False, candidates=self._candidates(), describe=str(exc)[:160])

        if count == 0:
            return Resolution(found=False, candidates=self._candidates())

        # Describe the element that actually matched, using the same
        # accessibility view everything else here uses. Callers classify what is
        # under the cursor rather than the words used to reach it — a policy
        # that reads the request instead of the element can be walked around by
        # switching targeting strategy.
        name = role = None
        try:
            match = NODE.match(loc.first.aria_snapshot().splitlines()[0])
            if match:
                role, name = match.group(1), match.group(2)
        except (PlaywrightError, IndexError):
            pass
        return Resolution(found=True, ambiguous=count, describe=_describe(target),
                          name=name, role=role)

    def _frame_for(self, target: Target):
        """The document a target lives in: the one it names, else the first
        that can resolve it."""
        wanted = getattr(target, "frame", None)
        documents = self._documents()
        if wanted:
            for name, frame in documents:
                if name == wanted:
                    return frame
        for _, frame in documents:
            try:
                if self._locator(target, frame).count():
                    return frame
            except PlaywrightError:
                continue
        return documents[0][1]

    # -- acting ------------------------------------------------------------

    def navigate(self, url: str) -> ActionResult:
        try:
            self._page.goto(url, wait_until="domcontentloaded")
            self._settle()
            return ActionResult(ok=True, detail=f"navigated to {url}")
        except (TimeoutError, PlaywrightError) as exc:
            return ActionResult(ok=False, error=str(exc)[:200])

    def click(self, target: Target) -> ActionResult:
        return self._act(target, lambda loc: loc.click(), "clicked")

    def type(self, target: Target, text: str) -> ActionResult:
        return self._act(target, lambda loc: loc.fill(text), "typed into")

    def select(self, target: Target, option: str) -> ActionResult:
        return self._act(target, lambda loc: loc.select_option(option), f"selected {option!r} in")

    def tables(self) -> list[tuple[list[str], int]]:
        """The tables on this page: their column headers and how many rows.

        A header row is taken to be one whose cells are all emboldened and of
        which there is more than one — which is what separates Meridian's data
        tables from the nested tables it uses for layout, without a model being
        asked to tell them apart.
        """
        found: list[tuple[list[str], int]] = []
        for _, frame in self._documents():
            try:
                seen = frame.evaluate("""() => [...document.querySelectorAll('table')]
                    .map(t => {
                        // t.rows and row.cells, rather than walking children:
                        // the browser inserts a <tbody> that legacy markup never
                        // wrote, and a hand-rolled traversal sees one cell where
                        // the table has three.
                        if (t.rows.length < 2) return null;
                        const cells = [...t.rows[0].cells];
                        const header = cells.length > 1 &&
                            cells.every(c => c.tagName === 'TH' || c.querySelector('b'));
                        if (!header) return null;
                        return {columns: cells.map(c => c.innerText.trim()),
                                rows: t.rows.length - 1};
                    }).filter(Boolean)""")
            except PlaywrightError:
                continue
            for table in seen:
                found.append((table["columns"], table["rows"]))
        return found


    def _stated_count(self, target: RowCount, res: Resolution) -> ActionResult:
        """The count the application states for itself, beside its table."""
        pattern = re.compile(re.escape(target.summary) + r"\D{0,3}(\d+)(?:\s+of\s+(\d+))?",
                             re.IGNORECASE)
        for _, frame in self._documents():
            try:
                text = " ".join((frame.locator("body").inner_text() or "").split())
            except (TimeoutError, PlaywrightError):
                continue
            found = pattern.search(text)
            if found:
                # "3 of 3" states the shown count and the real one. The real one
                # is the answer; the difference is what truncation looks like.
                value = found.group(2) or found.group(1)
                return ActionResult(ok=True, value=value, resolution=res,
                                    detail=f"read the count the application states "
                                           f"beside {target.summary!r}")
        return ActionResult(ok=False, resolution=res,
                            error=f"the application states no count beside {target.summary!r}")


    def read(self, target: Target) -> ActionResult:
        res = self.resolve(target)
        if not res.found:
            return _unresolved(target, res)
        if isinstance(target, RowCount):
            if target.summary:
                return self._stated_count(target, res)
            # Counting is not reading a cell: the value is how many rows matched.
            try:
                rows = self._locator(target).count()
            except (TimeoutError, PlaywrightError) as exc:
                return ActionResult(ok=False, error=str(exc)[:200], resolution=res)
            return ActionResult(ok=True, detail=f"counted {_describe(target)}",
                                value=str(rows), resolution=res)
        if isinstance(target, AnyOf):
            # Return the line as the application wrote it, not the text the map
            # filed it under: the screen says NO MEMBER ON FILE FOR 40999 and the
            # map knows it as NO MEMBER ON FILE, and the caller asked what the
            # application said.
            try:
                shown = (self._locator(target).first.inner_text() or "")
            except (TimeoutError, PlaywrightError) as exc:
                return ActionResult(ok=False, error=str(exc)[:200], resolution=res)
            lines = [" ".join(line.split()) for line in shown.splitlines()]
            hit = ""
            for known in sorted(target.texts, key=len, reverse=True):
                hit = next((line for line in lines if known.lower() in line.lower()), "")
                if hit:
                    break
            return ActionResult(ok=bool(hit), value=hit,
                                detail=f"read the message {hit!r}" if hit else "",
                                error="" if hit else "none of the known messages were shown",
                                resolution=res)
        try:
            element = self._locator(target).first
            # Form fields hold their value in a property, not in their text.
            # Legacy admin consoles routinely display data in editable inputs —
            # phpLDAPadmin renders every LDAP attribute that way — so reading
            # inner_text() there returns an empty string and the caller believes
            # the field was genuinely blank.
            tag = element.evaluate("e => e.tagName.toLowerCase()")
            if tag in ("input", "textarea", "select"):
                value = (element.input_value() or "").strip()
            else:
                value = (element.inner_text() or "").strip()
            return ActionResult(ok=True, detail=f"read {_describe(target)}", value=value, resolution=res)
        except (TimeoutError, PlaywrightError) as exc:
            return ActionResult(ok=False, error=str(exc)[:200], resolution=res)

    def _act(self, target: Target, fn, verb: str) -> ActionResult:
        res = self.resolve(target)
        if not res.found:
            return _unresolved(target, res)
        try:
            fn(self._locator(target).first)
            self._settle()
            return ActionResult(ok=True, detail=f"{verb} {_describe(target)}", resolution=res)
        except (TimeoutError, PlaywrightError) as exc:
            # resolved but could not be acted on: disabled, covered, detached
            return ActionResult(
                ok=False,
                error=f"{_describe(target)} resolved but could not be actioned: {str(exc)[:140]}",
                resolution=res,
            )

    def _settle(self) -> None:
        """Let a form submission actually begin before anyone looks at the page.

        networkidle alone is not enough: immediately after a click the browser
        may not have started the POST yet, so the wait returns instantly, the
        page is observed unchanged, and the action is judged to have done
        nothing. Measured on ParaBank's open-account form — the account was
        created and the click was still reported as a no-op.

        That misreport is dangerous rather than merely wrong: told its
        irreversible action had no effect, the obvious next move is to repeat
        it.
        """
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT)
        except TimeoutError:
            pass
        try:
            self._page.wait_for_load_state("networkidle", timeout=DEFAULT_TIMEOUT)
        except TimeoutError:
            pass  # a busy page is still a page; the observation will show it

    def wait_stable(self, timeout_ms: int = 1_500) -> bool:
        """Wait for the tree to stop changing, without building candidates.

        observe() already does this, but it also enumerates and verifies every
        control, which replay does not need between steps. Replay used to
        resolve immediately after a click and miss content that arrives in a
        second request — phpLDAPadmin loads an entry's form that way, so the
        read found nothing on a page that visibly had it.
        """
        deadline = timeout_ms
        previous = None
        while deadline > 0:
            current = self._snapshot()
            if current == previous:
                return True
            previous = current
            self._page.wait_for_timeout(STABILITY_PAUSE_MS)
            deadline -= STABILITY_PAUSE_MS
        return False

    def digest_changed_from(self, baseline: str, timeout_ms: int = 2_500) -> bool:
        """Poll until the page differs from `baseline`, or give up.

        Used to confirm a no-op is genuine rather than premature. Only the
        negative answer costs the full timeout, and only when nothing happened.
        """
        deadline = timeout_ms
        while deadline > 0:
            if self.observe().digest != baseline:
                return True
            self._page.wait_for_timeout(STABILITY_PAUSE_MS)
            deadline -= STABILITY_PAUSE_MS
        return False

    # -- evidence ----------------------------------------------------------

    def evaluate(self, script: str):
        """Run a script in every document. Used to install the recorder that
        captures what a human does while they hold the session — which has to
        reach the frame they are actually clicking in, not just the shell."""
        results = []
        for _, frame in self._documents():
            try:
                results.append(frame.evaluate(script))
            except PlaywrightError:
                continue
        return next((r for r in results if r), results[0] if results else None)

    def screenshot(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=path, full_page=True)
        return path

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


# --------------------------------------------------------------------------


def _anchor_xpath(anchor: str, role_step: str, index: int) -> str:
    """The first control of a role following a piece of visible text.

    Two details exist because of real legacy markup. `normalize-space(.)` reads
    the element's whole text rather than its first text node, since labels come
    wrapped — `<td><b>Find by Amount</b>:</td>` splits the visible label across
    nodes. And translate() strips U+00A0 first, because normalize-space only
    collapses ASCII whitespace while legacy tables are built out of &nbsp;.
    """
    quoted = _xq(anchor)
    norm = "normalize-space(translate(., '\u00a0', ' '))"
    return (f"(//*[{norm}={quoted}][not(.//*[{norm}={quoted}])]"
            f"/following::*[{role_step}])[{index}]")


_NORM = "normalize-space(translate(., '\u00a0', ' '))"


def _header_xpath(column: str) -> str:
    """The header cell naming a column, matched on the cell and not its wrapper."""
    quoted = _xq(column)
    return f"//td[{_NORM}={quoted}]"


def _table_rows_xpath(column: str) -> str:
    """Every data row of the table this column heads.

    A data row is a sibling of the header's row. Meridian keeps its totals line
    outside the table — `Items shown: 3 of 3` is not a row — so siblings are
    exactly the data and nothing needs to be excluded by guesswork.
    """
    return f"({_header_xpath(column)}/parent::tr/following-sibling::tr)"


def _table_cell_xpath(target: InTable) -> str:
    """One cell, addressed by its column and its row rather than by its contents."""
    rows = _table_rows_xpath(target.column)
    # The column's position, counted on the header itself. XPath evaluates this
    # as a number, so the whole address stays a single expression.
    index = f"count({_header_xpath(target.column)}/preceding-sibling::td)+1"
    if target.where:
        quoted = _xq(target.where)
        row = f"{rows}[td[contains({_NORM}, {quoted})]][1]"
    elif target.row < 0:
        row = f"{rows}[last(){target.row + 1 if target.row < -1 else ''}]"
    else:
        row = f"{rows}[{target.row}]"
    return f"{row}/td[{index}]"


def _any_of_xpath(texts: list[str]) -> str:
    """The innermost element showing any one of these strings."""
    conds = " or ".join(f"contains({_NORM}, {_xq(t)})" for t in texts) or "false()"
    return f"//*[({conds}) and not(.//*[{conds}])]"


def _describe(target: Target) -> str:
    if isinstance(target, RoleName):
        return f'{target.role} "{target.name}"'
    if isinstance(target, AfterText):
        return f'{target.role} after "{target.anchor}"'
    if isinstance(target, InTable):
        where = f"the row containing {target.where!r}" if target.where else f"row {target.row}"
        return f"the {target.column!r} cell of {where}"
    if isinstance(target, RowCount):
        return f"the rows of the table headed {target.column!r}"
    if isinstance(target, AnyOf):
        return f"whichever of {len(target.texts)} known messages is shown"
    if isinstance(target, Ordinal):
        return f"{target.role} #{target.index}"
    return f"css {target.selector!r}"


def _unresolved(target: Target, res: Resolution) -> ActionResult:
    """The failure message that teaches. See the note in base.py."""
    return ActionResult(
        ok=False,
        resolution=res,
        error=f"{_describe(target)} matched nothing — on this page: {summarise(res.candidates)}",
    )


def _xq(value: str) -> str:
    """Quote a string for XPath, which has no escape syntax of its own."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in parts) + ")"
