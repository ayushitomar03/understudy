"""Build a capability from the map, with no model involved.

The knowledge base earns its keep twice. Once by making a model cheaper — it can
be told which screen it is on instead of being sent the screen. And once, more
usefully, by making the model unnecessary: if the map already holds a verified
locator for every control on the path to an answer, then "report the savings
balance for member 40021" is not a reasoning problem. It is a walk over a graph
whose edges are controls and whose nodes are screens.

That is worth doing because of what we measured. One model response is not the
unit of cost — the conversation is re-read on every call, so a task that takes
twenty-two responses pays for its own history twenty-two times, and the easiest
task in the set cost $0.13. A task planned from the map costs nothing at all.

Two properties keep this honest:

  * **It refuses rather than guesses.** Every step it emits comes from a locator
    the mapper resolved against the real page. When the goal cannot be matched to a
    value the map knows, or no path reaches the screen holding it, this returns
    None and the model is asked instead. A planner that produces a plausible wrong
    plan is worse than one that declines, because the wrong plan gets recorded.

  * **What it produces is a hypothesis, and verifying it is free.** Replay costs no
    model calls, so a synthesised capability can simply be run: if it returns the
    right answer it was right, and if it does not, fall back. We can afford to be
    wrong here in a way we cannot afford anywhere a model is being paid.
"""

from __future__ import annotations

import re
from collections import deque

from .artifact.schema import (AppRef, Capability, Locator, Output, Param, Recovery,
                              Step, TextMatches, Value)
from .surface.base import AnyOf, InTable, RoleName, RowCount
from .sitemap import Screen, SiteMap

# Words that appear in every label and every goal and so distinguish nothing.
#
# "member", "no" and "number" were in here, which killed the single most important
# match in the application: `member_no` against the field labelled "Member No."
# scored zero because every word in both was discarded. Thirty-two tasks were
# refused for it. A stop-word list built by intuition about English is the wrong
# instrument — these are the words that carry no information *in a label*, and a
# field's name is exactly the information.
_NOISE = {"the", "a", "an", "of", "on", "for", "and", "report", "look", "up",
          "bal", "value", "screen", "says", "exactly", "what", "application",
          "attempt", "confirm", "whether", "any", "read", "off", "file", "shows",
          "holding", "holds", "produce"}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w and w not in _NOISE}


def _score(wanted: str, label: str) -> float:
    """How well a requested output matches a label the map knows.

    Deliberately crude and deliberately strict: the score is the share of the
    label's own distinguishing words that the request mentions. "savings" matches
    "Savings Bal." because `bal` is noise and `savings` is not. "checking" does not
    match "Savings Bal." at all, which is the case that matters — a planner that
    confuses two balances produces a capability that is wrong every time.
    """
    a, b = _words(wanted), _words(label)
    if not a or not b:
        return 0.0
    if b <= a:
        return 1.0

    # One word inside another, which whole-word matching misses: "phone" is what a
    # task asks for and "Telephone" is what the screen calls it. Only allowed for
    # words long enough that containment means something — "no" inside "north"
    # would be worse than useless.
    matched = 0
    for word in b:
        if word in a:
            matched += 1
        elif any(len(w) >= 4 and (w in word or word in w) for w in a):
            matched += 1
    return matched / len(b)


# Which row, said positionally. A position is structural: "the third posted item"
# is the third data row whether the member has three items or none, and the plan
# is wrong in a way that shows rather than a way that lies.
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
             "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1}

# Words that pick a row by comparing rows to each other. The map records where a
# table is and what its columns are called; it does not record how it is sorted,
# and assuming is how "the oldest posted item" was answered with the newest one.
_NEEDS_AN_ORDER = ("oldest", "most recent", "latest", "earliest", "newest",
                   "largest", "smallest", "highest", "lowest", "biggest")


class Selection:
    """How the goal picks out of a table: a count, or a row by position."""

    def __init__(self, count: bool = False, row: int = 0) -> None:
        self.count, self.row = count, row

    def __repr__(self) -> str:
        return "a count of the rows" if self.count else f"row {self.row}"


def _needs_an_order(goal: str) -> str | None:
    low = goal.lower()
    return next((w for w in _NEEDS_AN_ORDER if w in low), None)


def _selects(goal: str) -> Selection | None:
    """What this goal wants out of a table, if it wants a table at all."""
    low = goal.lower()
    if "how many" in low or "the number of" in low:
        return Selection(count=True)
    for word, row in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            return Selection(row=row)
    return None


# Words that are part of asking for a count rather than part of what is being
# counted. Everything outside this, the screen's name, the table's own count
# line and the arguments, is something the goal asked for that the plan cannot
# promise — which is why the list is short and the refusal is the default.
_COUNTING = {"how", "many", "report", "number", "total", "are", "is", "on", "file", "in",
             "has", "have", "exist", "exists", "currently", "give", "me", "and",
             "there", "altogether", "count"}


def _unexplained(goal: str, screen: Screen, label: str, params: dict[str, str]) -> set[str]:
    """What the goal asks for that the count on offer does not cover.

    `how many standing orders are active` and a line reading `Active orders: 0`
    agree about what is being counted. `how many INQUIRY entries` and a line
    reading `Entries shown: 4` do not, and answering it returned 4 where the
    answer was 2. The difference is a word in the goal that nothing accounts
    for, so that is what this returns.
    """
    accounted = (_words(label) | _words(screen.title) | _words(screen.id)
                 | set(params) | {w for value in params.values() for w in _words(str(value))}
                 | _COUNTING)
    for name in params:
        accounted |= _words(name)
    return _words(goal) - accounted


def _table_read(sitemap: SiteMap, goal: str, outputs: list[str], picked: Selection,
                params: dict[str, str]) -> tuple[Screen | None, dict, str]:
    """A plan that reads a table, or why the map cannot support one.

    The screen is chosen by what it is called, because a table has no labelled
    values to match an output name against — "how many standing orders" and the
    screen titled Standing Orders is the whole of the reasoning, and it is
    reasoning a person can check.
    """
    if len(outputs) != 1:
        return None, {}, f"a table read answers one output, and {outputs} were asked for"
    having = [s for s in sitemap.screens if s.tables]
    if not having:
        return None, {}, ("the map records no tables — run tools/map_tables.py to add "
                          "them, or this belongs to the model")

    screen = max(having, key=lambda s: _score(goal, s.title or s.id))
    if _score(goal, screen.title or screen.id) < 0.3:
        return None, {}, (f"no screen with a table is named like this goal "
                          f"(closest was {(screen.title or screen.id)!r})")
    table = screen.tables[0]

    if picked.count:
        if (asked := _unexplained(goal, screen, table.summary, params)):
            return None, {}, (f"the goal counts {sorted(asked)}, which "
                              f"{table.summary or screen.id + chr(39) + 's table'!r} does not "
                              "separate — counting everything would answer a different question")
        target = RowCount(column=table.columns[0], summary=table.summary or None)
        why = (f"the count the application states beside {table.summary!r}"
               if table.summary else f"counting the rows of {screen.id}'s table")
    else:
        column = next((c for word in _words(goal) if (c := table.column(word))), None)
        if column is None:
            return None, {}, (f"the goal names no column of {screen.id}'s table "
                              f"({', '.join(table.columns)})")
        target = InTable(column=column, row=picked.row)
        why = f"{column} of {picked!r} on {screen.id}"
    return screen, {outputs[0]: Locator(target=target, why=why, verified=False)}, why


def _message_read(sitemap: SiteMap, goal: str,
                  outputs: list[str]) -> tuple[Screen | None, dict, str]:
    """A plan whose answer is whatever the application said.

    A refusal is not on the screen as a field. It is the screen — NOT AUTHORISED
    for a restricted record, NO MEMBER ON FILE for one that is not there — and
    which one appears depends on the argument. The survey already classified
    every message this application gives, so the plan reads whichever of them is
    showing instead of pinning itself to the one the recording happened to meet.
    """
    if len(outputs) != 1:
        return None, {}, f"a message answers one output, and {outputs} were asked for"
    texts = [m.text for m in sitemap.messages
             if m.means in ("answer", "caller_error", "permission")]
    if not texts:
        return None, {}, "the map has classified no messages to read"

    # The deepest screen the goal names: deepest because the message replaces the
    # screen it was going to, so the flow still has to go there to provoke it.
    ranked = sorted(sitemap.screens,
                    key=lambda s: (_score(goal, s.title or s.id), len(s.controls)),
                    reverse=True)
    screen = ranked[0]
    if _score(goal, screen.title or screen.id) < 0.3:
        return None, {}, f"no screen is named like this goal (closest {screen.id!r})"
    return screen, {outputs[0]: Locator(
        target=AnyOf(texts=sorted({_headline(t) for t in texts}, key=len, reverse=True)),
        why="whichever message the application shows", verified=False)}, \
        f"reading whichever of {len(texts)} known messages appears on {screen.id}"


def _single_use(value) -> bool:
    """Does this locator find the value by being the value?

    A map can contain "the cell named 4,210.55" as the savings balance. Planning
    from it produces a capability that works for exactly one record, and the
    failure arrives as control_not_found on every other one — which reads like a
    broken flow rather than a bad locator.

    The test is whether the locator refers to the *label*. A value addressed by the
    words an operator reads it by holds for every record; one addressed by anything
    else is addressing this record's contents. The first version compared the
    locator against the recorded example instead, which is truncated to eighty
    characters — so it cleared 'Savings Bal.' -> '4,210.55' as fine, because the
    balance fell outside the truncation.

    A locator with no name at all — an ordinal, a css selector — is not judged
    here: it may be fragile but it is not single-record.
    """
    named = str(getattr(value.locator.target, "name", "")
                or getattr(value.locator.target, "anchor", "")).strip()
    if not named:
        return False
    return _score(named, value.label) < 0.5


def _headline(message: str) -> str:
    """The part of a message that is on the screen as one run of text.

    The survey wrote NOT AUTHORISED down as `NOT AUTHORISED — This record is
    restricted. Refer to a supervisor.`, joining with a dash what the page
    renders as two blocks separated by <br><br>. Searched for whole, it is never
    found; searched for as far as the dash, it is exactly the words the screen
    leads with.
    """
    for splitter in ("\u2014", "\u2013", "\n"):
        if splitter in message:
            return message.split(splitter)[0].strip()
    return message.strip()


def _value_on(screen: Screen, wanted: str) -> tuple[Locator | None, float]:
    best, score = None, 0.0
    for value in screen.values:
        if _single_use(value):
            continue
        s = _score(wanted, value.label)
        if s > score:
            best, score = value.locator, s
    return best, score


def _outcome_screen(sitemap: SiteMap, screen_id: str) -> bool:
    """Is this screen an answer rather than a place to pass through?

    Two signals, because neither alone is reliable. A screen whose identifying text
    is one of the application's recorded non-clearing messages is something it is
    saying. And a screen holding no values, whose only way out is back to the menu,
    is a dead end whatever it is called — which catches the ones the message match
    misses, and the message match missed NOT AUTHORISED because the recorded message
    is a whole sentence and the screen is identified by its first two words.
    """
    screen = sitemap.screen(screen_id)
    if screen is None:
        return True

    ident = screen.identifies_by.lower()
    for message in sitemap.messages:
        text = message.text.lower()
        if not message.actionable and (text in ident or ident[:24] in text):
            return True

    if screen.values:
        return False
    forward = [c for c in screen.controls
               if c.leads_to and c.leads_to not in (screen_id, "main_menu")]
    return not forward


def _route(sitemap: SiteMap, start: str, goal: str) -> list[tuple[Screen, str]] | None:
    """Shortest path of (screen, control to press) from one screen to another.

    The subtlety is that a form's destination depends on its data. Pressing Inquire
    reaches the member record, or NOT AUTHORISED, or the form again with an error,
    according to what was typed — and the survey recorded whichever it happened to
    see. So a button on a screen that has fields is treated as leading wherever the
    route needs to go, while a plain link is only believed for the destination the
    map actually recorded.

    Without that, the one usable edge out of the lookup screen pointed at a refusal
    and no task in the set could be planned at all.
    """
    if start == goal:
        return []

    seen = {start}
    queue: deque[tuple[str, list[tuple[Screen, str]]]] = deque([(start, [])])
    while queue:
        at, route = queue.popleft()
        screen = sitemap.screen(at)
        if screen is None:
            continue
        submits = any(c.role in ("textbox", "combobox") for c in screen.controls)
        # Where the submit button of this form actually goes. The survey recorded
        # `Inquire -> not_authorised` because it happened to look up the restricted
        # member — but it also recorded `Member No. -> member_detail` on the field,
        # which is the destination a good request reaches. The map holds the right
        # answer attached to the wrong control, so take it from whichever control on
        # the screen records a destination that is a real place.
        submit_goes_to = next(
            (c.leads_to for c in screen.controls
             if c.leads_to and c.leads_to != at
             and not _outcome_screen(sitemap, c.leads_to)),
            None)

        for control in screen.controls:
            if not control.verified:
                continue
            # A field is filled, never pressed. The map recorded leads_to on the
            # Member No. textbox, so the route typed the number in and then clicked
            # the same box: nothing navigated and the next step failed.
            if control.role in ("textbox", "combobox", "checkbox", "radio"):
                continue

            here = route + [(screen, control.name)]

            # The submit button of a form reaches a data-dependent screen.
            if submits and control.role == "button":
                destination = submit_goes_to or goal
                if destination == goal:
                    return here
                if destination not in seen:
                    seen.add(destination)
                    queue.append((destination, here))
                continue

            if not control.leads_to or control.leads_to in seen:
                continue
            if control.leads_to == goal:
                return here
            if _outcome_screen(sitemap, control.leads_to):
                continue
            seen.add(control.leads_to)
            queue.append((control.leads_to, here))
    return None


def _fill(screen: Screen, params: dict[str, str], used: set[str]) -> list[tuple[Locator, str]]:
    """Which parameter goes in which field on this screen.

    A form is only traversable if every field on it can be filled from a named
    parameter. One field and one unused parameter is unambiguous; anything else is
    refused, because guessing which value belongs in which box is precisely how a
    plan becomes confidently wrong.
    """
    fields = [c for c in screen.controls
              if c.verified and c.role in ("textbox", "combobox")]
    if not fields:
        return []

    assigned: list[tuple[Locator, str, str]] = []
    for field in fields:
        candidates = [(name, _score(name, field.name)) for name in params
                      if name not in used]
        candidates = [(n, sc) for n, sc in candidates if sc > 0]
        if len(candidates) != 1:
            return []                      # ambiguous or unfillable: refuse
        name, _ = candidates[0]
        used.add(name)
        # A dropdown is chosen from, not typed into. Emitting `type` against the
        # statement period combobox meant the period was never selected, the
        # figures never appeared, and all seven statement tasks failed with
        # control_not_found.
        action = "select" if field.role == "combobox" else "type"
        assigned.append((field.locator, name, action))
    return assigned


def from_map(sitemap: SiteMap, goal: str, params: dict[str, str],
             outputs: list[str], capability_id: str) -> tuple[Capability | None, str]:
    """A capability for this task, or None and the reason it could not be built."""
    if not sitemap.entry:
        return None, "the map has no verified way into the application"
    if not outputs:
        return None, "no outputs were requested, so there is nothing to plan towards"

    # Ordering is the one thing a map cannot record. It knows where a table is
    # and what its columns are called, not how it is sorted — so "the oldest
    # posted item" stays with the model, where it was answered with the newest
    # one before this refusal existed.
    if (unordered := _needs_an_order(goal)):
        return None, (f"the goal asks for the {unordered!r} row, which needs to know how "
                      "the table is sorted. The map records where a table is and what its "
                      "columns are called, not its order")

    reading_a_table = _selects(goal)
    if reading_a_table is not None:
        target, locators, why = _table_read(sitemap, goal, outputs, reading_a_table, params)
        if target is None:
            return None, why
    else:
        # Which screen holds the answers, and how well.
        best: tuple[Screen | None, float] = (None, 0.0)
        for screen in sitemap.screens:
            total = sum(_value_on(screen, name)[1] for name in outputs)
            if total > best[1]:
                best = (screen, total)
        target, confidence = best

        if target is None or confidence < 0.9 * len(outputs):
            # No labelled value answers this. Before giving up: the answer may not
            # be a field at all, but the message the application replies with.
            target, locators, why = _message_read(sitemap, goal, outputs)
            if target is None:
                return None, (f"no screen in the map holds all of {outputs} "
                              f"(best match scored {confidence:.2f}); {why}")
        else:
            locators = {}
            for name in outputs:
                locator, score = _value_on(target, name)
                if locator is None or score < 0.9:
                    return None, f"{name!r} does not match a value on {target.id!r}"
                locators[name] = locator

    landing = sitemap.entry_lands_on or (sitemap.screens[0].id if sitemap.screens else "")
    landing_screen = sitemap.screen(landing)
    if landing_screen is None:
        # entry_lands_on is a title rather than an id in some maps
        landing_screen = next((s for s in sitemap.screens
                               if s.title.lower() == landing.lower()), None)
    if landing_screen is None:
        return None, f"the map does not describe the screen entry lands on ({landing!r})"

    route = _route(sitemap, landing_screen.id, target.id)
    if route is None:
        return None, f"no path from {landing_screen.id!r} to {target.id!r} in the map"

    steps = [s.model_copy(deep=True) for s in sitemap.entry]
    used: set[str] = set()
    for screen, control_name in route:
        for locator, param, action in _fill(screen, params, used):
            steps.append(Step(index=0, action=action, target=locator,
                              value=Value(from_param=param),
                              intent=f"{'choose' if action == 'select' else 'enter'} "
                                     f"{param} on {screen.id}"))
        control = screen.control(control_name)
        if control is None:
            return None, f"{control_name!r} vanished from {screen.id!r}"
        steps.append(Step(index=0, action="click", target=control.locator,
                          intent=f"{control_name} -> {control.leads_to}"))

    at_step = {}
    for name, locator in locators.items():
        steps.append(Step(index=0, action="read", target=locator,
                          intent=f"read {name}"))
        at_step[name] = len(steps)

    for i, step in enumerate(steps, 1):
        step.index = i

    required = {p for p in params if p in used}
    unfilled = set(params) - used
    if unfilled:
        # A parameter the plan never uses means the plan is not doing what was asked
        # — an amount that never reaches a field, a period never selected.
        return None, f"the map gives nowhere to put {sorted(unfilled)}"

    capability = Capability(
        id=capability_id,
        app=AppRef(product=sitemap.product, base_url=sitemap.app),
        goal=goal,
        steps=steps,
        params=[Param(name=n, example=params[n]) for n in sorted(required)],
        outputs=[Output(name=n, locator=locators[n], at_step=at_step[n])
                 for n in outputs],
        # A message plan is going somewhere it may never arrive: the refusal
        # replaces the screen. Its read carries the proof instead — if none of the
        # application's known messages is showing, the read fails and so does the
        # plan.
        success=(TextMatches(pattern="", present=True)
                 if any(isinstance(l.target, AnyOf) for l in locators.values())
                 else _arrived(target)),
        recoveries=_recoveries(sitemap),
        planned_from_map=True,
    )
    return capability, f"planned from the map: {len(steps)} steps to {target.id}"


def _recoveries(sitemap: SiteMap) -> list[Recovery]:
    """The interruptions this application is known to produce, attached up front.

    A discovered capability learns these one failure at a time, and each lesson is
    private to that capability. The map already classified them — which of the
    application's messages stand in the way, and whether each clears by pressing
    something or by waiting — so a planned capability can carry them from the start
    rather than being repaired into knowing them.

    Only the actionable ones. An answer or a permission denial is not something to
    recover from: a flow that "recovers" from NOT AUTHORISED is retrying a refusal,
    which is how an account gets locked.

    What this returns is therefore only as good as the survey. Surveying this
    application against a quiet instance produced exactly one actionable message —
    session expiry — so a planned capability starts knowing how to sign on again
    and nothing else. The hazards a real back office produces were never met, and
    that shows up in the results as the T7 tier.
    """
    rules: list[Recovery] = []
    known: set[str] = set()
    for message in sitemap.messages:
        if message.text.strip().lower() in known:
            continue
        if not message.actionable:
            continue
        if message.clears_by == "click" and message.control:
            rules.append(Recovery(
                code=_slug(message.text), detect=message.text, action="click",
                target=RoleName(role="link", name=message.control),
                why=message.why or f"known interruption: {message.means}"))
        elif message.clears_by == "wait":
            rules.append(Recovery(
                code=_slug(message.text), detect=message.text, action="wait",
                seconds=message.seconds,
                why=message.why or "the application releases this on its own"))
    return rules


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "condition"


def _arrived(screen: Screen) -> TextMatches:
    """What proves the flow arrived.

    The screen's own identifying text, which the mapper confirmed is present on it
    and absent from every other screen — so it is exactly the assertion "we are
    here". The first version asked for a heading with that name, which can never be
    satisfied on this application: MEMBER RECORD is a <b> inside a <font>, not a
    heading, so all fifty-one plans failed with success_condition_unmet.

    Worth noting this is stronger than what discovery produces. A discovered
    capability's success condition is an empty text pattern, which matches anything;
    a planned one names text that distinguishes its screen from all the others.
    """
    return TextMatches(pattern=screen.identifies_by[:60], present=True)


# -- is this map good enough to plan from? ---------------------------------


def probes(sitemap: SiteMap) -> list[tuple[str, str, dict[str, str], list[str]]]:
    """A task per readable screen, invented from the map itself.

    The point is to ask the map to do the thing it exists for, without needing a
    test suite that knows the application. For every screen holding values, this
    asks for one of them — which exercises the whole chain: a route to that screen,
    a parameter in every field on the way, and a locator that reads the value.
    """
    out = []
    for screen in sitemap.screens:
        readable = [v for v in screen.values if not _single_use(v)]
        if not readable:
            continue
        label = readable[0].label
        name = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "value"
        out.append((screen.id, f"Report the {label} shown on {screen.id}", {}, [name]))
    return out


def self_check(sitemap: SiteMap) -> tuple[list[str], list[str]]:
    """Which screens the map can reach and read, and which it cannot.

    Static — no browser, no model — so it can run every time a map is written.
    This is the check that was missing: three maps in a row were saved looking
    complete, and the first sign that one of them recorded every value by its own
    contents was a task failing on a different member two runs later. A map should
    be able to say whether it is usable.
    """
    can, cannot = [], []
    for screen_id, goal, params, outputs in probes(sitemap):
        capability, why = from_map(sitemap, goal, params, outputs, f"probe.{screen_id}")
        (can if capability is not None else cannot).append(
            screen_id if capability is not None else f"{screen_id}: {why[:70]}")
    return can, cannot


def single_use_values(sitemap: SiteMap) -> list[str]:
    """Values whose locator is the value — usable for one record and no other."""
    return [f"{s.id}.{v.label}" for s in sitemap.screens for v in s.values
            if _single_use(v)]
