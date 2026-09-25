"""Learn the application once: walk it, and write down what is verifiably there.

This is the same model, the same tools and the same surface as discovery. The
difference is the goal it is given. Discovery is told "book a transfer" and stops
as soon as it has; the mapper is told "find out what this application is" and
stops when it has been everywhere it can reach.

The one rule that makes the output worth having: **nothing is recorded that did
not resolve.** Every control the model names is looked up on the real page before
it enters the map, and one that does not resolve is refused with the reason. A map
of prose would be cheaper and would be wrong in the specific silent ways model
prose about this application has always been wrong — advertising a control by its
title and resolving it by its alt text, naming a label that is split across two
tags, assuming a document that a frameset does not have.

The mapper is also where message classification happens, and it is the most
valuable thing in the map. A legacy app answers everything as text with a 200
status, and the difference between "NO MEMBER ON FILE" (the answer) and "RECORD IN
USE" (wait) and "NOT AUTHORISED" (stop, a person decides) is the difference
between reporting, retrying and locking an account. Asking a model that question
once per application, while it is looking at the screen, is both cheaper and more
consistent than asking it per failure — which measurably disagreed with itself on
identical screens.
"""

from __future__ import annotations

import json
import re
from typing import Any

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              ResultMessage, TextBlock, create_sdk_mcp_server, tool)

from ..artifact.schema import Locator, Step, Value as StepValue
from ..evidence import EventLog
from ..policy import Policy, for_app
from ..sitemap import Control, Message, Screen, SiteMap, Value
from ..surface.base import summarise
from .surface_thread import SurfaceThread
from .tools import TARGET_FIELDS, build_target, render_result

# Twelve screens, each needing a record_screen plus a call per control and value,
# plus the error states provoked deliberately. At 120 the mapper ran out with three
# screens unvisited — and the cost is paid once for every task that follows, so
# truncating it is the wrong economy.
MAX_TURNS = 400

SYSTEM_PROMPT = """You are surveying a legacy back-office application so that later
automation does not have to rediscover it. You are not completing a task. You are
finding out what the application is.

Work like someone documenting a system they will hand to a colleague:

1. Sign on, and call record_entry with the steps that got you in.
2. Visit every screen you can reach. On each one:
     - call record_screen with its id and identifying text
     - call record_control once per button, link and field. **Set leads_to on every
       control that opens another screen** — without it the map lists screens but
       not how to reach them, and nothing can be planned from it
     - call record_value once per labelled value it displays, **including the
       current contents of fields** — a form showing a telephone number holds that
       number, and a later task will be asked to read it.
       **Target a value by its label, not by its contents.** Use strategy
       'after_text' with the label as the anchor. Naming the value itself — "the
       cell that says 4,210.55" — records a locator that works for one record and
       no other, and the map is reused by every later task.
   Each control and value is looked up on the page as you record it, so you find out
   immediately whether it resolved. A refusal tells you what is on the page instead.
3. Provoke the application's error states deliberately — a member number that does
   not exist, a non-numeric one, a record you are not allowed, an amount larger
   than the balance. Call record_message for each answer you get.
4. Call finish_map when you have been everywhere you can reach.

What makes a good map:

  * **leads_to is where the control goes when the request succeeds.** Pressing
    Inquire with a restricted member reaches NOT AUTHORISED, but that is the
    application answering, not where the control leads — record that with
    record_message and set leads_to to the screen a good request reaches.

  * **identifies_by must be text on that screen and no other.** It is how later
    automation knows where it is. A heading is usually right; the product name,
    which is in the chrome of every screen, is useless and will be refused.

  * **A screen with no controls and no values is not mapped**, and finish_map will
    refuse while any screen is in that state. Follow the links you record: a leads_to
    you never visited is a screen missing from the map.

  * **Record the message, not the data in it.** "NO MEMBER ON FILE FOR 40999"
    matches one member; "NO MEMBER ON FILE" matches the condition.

  * **Every control you name is checked against the page before it is recorded.**
    If it is refused, look at the page again rather than rewording it.

  * **Classify messages by what should be done about them**, which is the whole
    point of recording them:
      answer        — the application answered the question. Report it, change nothing.
                      "no such record" is an answer, not a fault. An answer does not
                      clear, so its clears_by is "nothing" — a button that runs the
                      enquiry again is not a way of clearing it.
      caller_error  — the input was wrong. The flow is fine.
      interruption  — something stands in the way and can be dismissed. Name the control.
      busy          — the application is holding something and will release it. There is
                      NOTHING to press, and it clears by waiting: record clears_by
                      "wait" with roughly how many seconds. A hold you record as
                      never clearing teaches nothing about what to do about it.
      permission    — not allowed. A person must decide; it must never be retried.
      session       — no longer signed on.
    If a screen carries a message and offers no control, it is busy or permission,
    never an interruption. Check for a control before deciding.

Do not try to be exhaustive about data. One example per screen is enough; you are
mapping structure, not contents."""


class MapContext:
    """Holds the surface, the map being built, and the verification rule."""

    def __init__(self, base_url: str, log: EventLog, surface: SurfaceThread,
                 policy: Policy, credentials: dict[str, str] | None):
        self.base_url = base_url.rstrip("/")
        self.log = log
        self.surface = surface
        self.policy = policy
        self.credentials = credentials or {}
        self.map = SiteMap(app=self.base_url, built_by=log.run_id)
        self.turn = 0
        self.finished = False
        self._seen_urls: set[str] = set()
        self._screen_at: dict[str, str] = {}
        self._steps: list[Step] = []
        self._last_url = ""
        self.refused = 0

    # -- helpers ----------------------------------------------------------

    async def _observe(self):
        obs = await self.surface.call(lambda s: s.observe())
        self._last_url = obs.url
        rel = self._relative(obs.url)
        # A frameset shell is not a screen, and neither is an image.
        if rel not in ("/", "/index.htm") and not rel.startswith("/img"):
            self._seen_urls.add(rel)
        return obs

    def _relative(self, url: str) -> str:
        return url[len(self.base_url):] or "/" if url.startswith(self.base_url) else url

    async def _verify(self, args: dict) -> tuple[Locator | None, str]:
        """Resolve a named control before it is allowed into the map.

        A refusal has to say what *is* there, not only that this was not. The
        first version passed the target to summarise(), which takes a sequence of
        candidates — so every failed resolution raised instead of reporting, and
        the model spent thirty-four turns trying every strategy against an error
        message that told it nothing. It gave up before signing on.
        """
        try:
            target = build_target(args)
        except Exception as exc:
            return None, f"that target is not expressible: {exc}"

        resolution = await self.surface.call(lambda s: s.resolve(target))
        if not resolution.found:
            self.refused += 1
            found = summarise(resolution.candidates) if resolution.candidates else "nothing"
            return None, (f"{_describe(target)} does not resolve here, so it is not recorded.\n"
                          f"What is on this page: {found}\n"
                          "Name one of those, or observe again.")
        return Locator(target=target, why=args.get("note", "")[:200], verified=True), ""

    # -- the tools --------------------------------------------------------

    async def do_observe(self, _: dict) -> dict:
        self.turn += 1
        obs = await self._observe()
        known = self.map.where(obs.tree)
        self.log.emit("observation", url=obs.url, digest=obs.digest,
                      recognised=known.id if known else None)
        return render_result(
            verdict="observed", url=obs.url, candidates=obs.candidates,
            extra=(f"This looks like the screen you already recorded as {known.id!r}.\n\n"
                   if known else "") + obs.tree)

    async def do_navigate(self, args: dict) -> dict:
        self.turn += 1
        url = args.get("url", "")
        full = url if url.startswith("http") else f"{self.base_url}{url}"
        verdict = self.policy.check_navigation(full)
        if not verdict.allowed:
            self.log.emit("policy_blocked", action="navigate", url=full, why=verdict.why)
            return _text(f"refused: {verdict.why}")
        await self.surface.call(lambda s: s.navigate(full))
        await self.surface.call(lambda s: s.wait_stable())
        self._steps.append(Step(index=len(self._steps) + 1, action="navigate",
                                intent=args.get("reason") or f"go to {url}",
                                url=self._relative(full)))
        obs = await self._observe()
        return render_result(verdict="navigated", url=obs.url, candidates=obs.candidates,
                             extra=obs.tree)

    async def do_click(self, args: dict) -> dict:
        self.turn += 1
        locator, why = await self._verify(args)
        if locator is None:
            return _text(why)
        target = locator.target
        name = getattr(target, "name", None) or getattr(target, "anchor", None)
        verdict = self.policy.check_action("click", name, getattr(target, "role", None))
        if not verdict.allowed:
            self.log.emit("policy_blocked", action="click", control=name, why=verdict.why)
            return _text(f"refused: {verdict.why}. Map the rest of the application instead; "
                         "a screen behind an irreversible action is not worth taking it.")
        before = (await self._observe()).digest
        result = await self.surface.call(lambda s: s.click(target))
        await self.surface.call(lambda s: s.wait_stable())
        obs = await self._observe()
        self._steps.append(Step(index=len(self._steps) + 1, action="click",
                                intent=args.get("reason") or f"click {name}",
                                target=locator))
        return render_result(verdict="clicked" if result.ok else "failed", url=obs.url,
                             error=result.error, candidates=obs.candidates,
                             extra=("the page did not change\n\n" if obs.digest == before else "")
                                   + obs.tree)

    async def do_type(self, args: dict) -> dict:
        self.turn += 1
        locator, why = await self._verify(args)
        if locator is None:
            return _text(why)
        value = args.get("text", "")
        secret = next((k for k, v in self.credentials.items() if v == value), None)
        result = await self.surface.call(lambda s: s.type(locator.target, value))
        self._steps.append(Step(
            index=len(self._steps) + 1, action="type",
            intent=args.get("reason") or "enter a value", target=locator,
            value=StepValue(from_secret=secret) if secret else StepValue(literal=value)))
        obs = await self._observe()
        return render_result(verdict="typed" if result.ok else "failed", url=obs.url,
                             error=result.error, extra=obs.tree)

    async def do_record_entry(self, args: dict) -> dict:
        """The verified way in. Recorded from the steps actually taken, not described."""
        self.turn += 1
        if not self._steps:
            return _text("nothing has been done yet, so there is no entry sequence to record")
        self.map.entry = list(self._steps)
        self.map.entry_lands_on = args.get("lands_on", "")
        self.log.emit("entry_recorded", steps=len(self.map.entry),
                      lands_on=self.map.entry_lands_on)
        return _text(f"recorded {len(self.map.entry)} verified steps as the way in")

    async def do_record_screen(self, args: dict) -> dict:
        """Register the screen. What is on it arrives one control at a time."""
        self.turn += 1
        obs = await self._observe()
        ident = (args.get("identifies_by") or "").strip()
        screen_id = (args.get("id") or "").strip()
        if not ident:
            return _text("identifies_by is required — it is how automation knows where it is")
        if not screen_id:
            return _text("id is required")
        if ident.lower() not in obs.tree.lower():
            return _text(f"{ident!r} is not on this page, so it cannot identify it. "
                         "Use text you can see in the tree.")

        # It has to identify this screen and no other, or it identifies nothing.
        # An earlier run recorded 'MERIDIAN' — the product name in every screen's
        # chrome — for two screens at once, and called them main_menu and main_menu2.
        clash = next((sc for sc in self.map.screens
                      if sc.id != screen_id and sc.identifies_by.lower() == ident.lower()),
                     None)
        if clash is not None:
            return _text(f"{ident!r} already identifies the screen you recorded as "
                         f"{clash.id!r}, so it cannot tell them apart. Find text that is on "
                         "this screen and not on that one — a heading usually works, and the "
                         "product name in the chrome never does.")

        existing = self.map.screen(screen_id)
        screen = Screen(
            id=screen_id, title=args.get("title", ""), identifies_by=ident,
            url=self._relative(obs.url),
            controls=existing.controls if existing else [],
            values=existing.values if existing else [],
            needs_signin=bool(args.get("needs_signin", True)),
            note=args.get("note", ""))
        self.map.screens = [sc for sc in self.map.screens if sc.id != screen_id] + [screen]
        self._screen_at[self._relative(obs.url)] = screen_id
        self.log.emit("screen_recorded", id=screen_id, url=screen.url)
        return _text(f"recorded {screen_id}. Now add what is on it: record_control for each "
                     "button, link and field, and record_value for each labelled value it "
                     "shows. Include where each link leads.")

    def _screen_for(self, args: dict) -> tuple[Screen | None, str]:
        wanted = (args.get("screen_id") or "").strip()
        if not wanted:
            return None, "screen_id is required"
        screen = self.map.screen(wanted)
        if screen is None:
            return None, (f"no screen recorded as {wanted!r}. Recorded so far: "
                          + (", ".join(sc.id for sc in self.map.screens) or "none"))
        return screen, ""

    async def do_record_control(self, args: dict) -> dict:
        """One control, verified against the page before it is kept."""
        self.turn += 1
        screen, why = self._screen_for(args)
        if screen is None:
            return _text(why)
        name = (args.get("name") or args.get("anchor") or "").strip()
        if not name:
            return _text("give the control a name — what it says on screen")

        locator, why = await self._verify(args)
        if locator is None:
            return _text(why)

        # leads_to says "pressing this opens that screen". A field is filled, not
        # pressed, so an edge through one is a route that cannot be walked — and it
        # is recorded as verified, because the field itself resolves perfectly well.
        role = (args.get("role") or "").strip().lower()
        if args.get("leads_to") and role in ("textbox", "combobox", "checkbox", "radio"):
            return _text(
                f"a {role} is filled in, not pressed, so it cannot be what opens "
                f"{args['leads_to']!r}. Record the field without leads_to, and put "
                "leads_to on the button or link that submits it — that is the control a "
                "route has to press.")

        screen.controls = [c for c in screen.controls
                           if c.name.strip().lower() != name.lower()]
        screen.controls.append(Control(
            name=name, role=args.get("role", ""), locator=locator,
            leads_to=(args.get("leads_to") or "").strip() or None,
            note=args.get("note", "")))
        self.log.emit("control_recorded", screen=screen.id, name=name,
                      leads_to=args.get("leads_to") or None)
        return _text(f"{name!r} resolved and recorded on {screen.id} "
                     f"({len(screen.controls)} controls there now)")

    async def do_record_value(self, args: dict) -> dict:
        """One labelled value, read as well as resolved — a locator that resolves to
        an empty control is verified and useless."""
        self.turn += 1
        screen, why = self._screen_for(args)
        if screen is None:
            return _text(why)
        label = (args.get("label") or "").strip()
        if not label:
            return _text("label is required")

        locator, why = await self._verify(args)
        if locator is None:
            return _text(why)

        read = await self.surface.call(lambda sf: sf.read(locator.target))

        # A locator must not be the value it reads. The mapper recorded the savings
        # balance as "the cell named 4,210.55" — true of member 40021 and of nobody
        # else — so every task about another member failed to find it. The map looked
        # complete and was single-use.
        #
        # This is the same mistake discovery already guards against; the mapper had
        # no equivalent check, and it is more dangerous here because a map is reused
        # by every task rather than by one.
        named = str(getattr(locator.target, "name", "")
                    or getattr(locator.target, "anchor", "")).strip()
        value_text = (read.value or "").strip()
        if named and value_text and (named == value_text
                                     or (len(named) > 2 and named in value_text
                                         and named.lower() != label.lower())):
            self.refused += 1
            return _text(
                f"that locator finds the value by *being* the value: it names "
                f"{named!r}, which is what this field happens to hold for this record. "
                f"It would not resolve for any other record.\n\n"
                f"Target it by its label instead — strategy 'after_text' with anchor "
                f"{label!r}, which is what an operator reads it by and what stays true "
                "for every record.")

        screen.values = [v for v in screen.values if v.label.strip().lower() != label.lower()]
        screen.values.append(Value(label=label, locator=locator,
                                   example=(read.value or "")[:80]))
        self.log.emit("value_recorded", screen=screen.id, label=label,
                      example=(read.value or "")[:60])
        return _text(f"{label!r} recorded on {screen.id}, currently reads "
                     f"{(read.value or '')[:40]!r}")

    async def do_record_message(self, args: dict) -> dict:
        self.turn += 1
        text = (args.get("text") or "").strip()
        if not text:
            return _text("text is required")
        # 'NO MEMBER ON FILE FOR 1001' would only ever match that one member. The
        # message is the part that repeats; the number is the part that does not.
        # 'NOTICE' would match a page that merely contains the word. A detector has
        # to be specific enough that firing on it means something.
        if len(text) < 12 and len(text.split()) < 3:
            return _text(f"{text!r} is too short to identify a condition on its own — it "
                         "would fire on any page containing that word. Use the distinctive "
                         "part of the sentence.")
        if re.search(r"\d{3,}", text):
            return _text(f"{text!r} contains data from this particular run, so a rule "
                         "matching it would fire for nothing else. Record only the part "
                         "that is the same every time.")
        means = args.get("means", "unknown")
        clears = args.get("clears_by", "nothing")
        # The one consistency rule worth enforcing: a screen with nothing to press
        # is not an interruption, whatever it is called. Getting this wrong is how
        # a permission denial gets retried forever.
        # An answer does not clear. The first run recorded 'NO MEMBER ON FILE' as
        # an answer that clears by clicking Inquire, which would have taught every
        # later task to press a button and retry a question the app had answered.
        if means in ("answer", "caller_error", "permission") and clears != "nothing":
            return _text(f"a {means} is not something that clears — it is what the "
                         "application has to say. Record it with clears_by 'nothing'. "
                         "Only an interruption, a hold, or a lost session clears.")

        if means == "interruption" and clears != "click":
            return _text("an interruption is something that can be dismissed. If there is "
                         "no control to press, this is 'busy' (it clears on its own) or "
                         "'permission' (it does not clear). Look at the screen again.")

        # A hold that never clears is not a hold. Recorded as busy/nothing it yields
        # no recovery at all, which is how a map that had correctly *identified*
        # RECORD IN USE still taught nothing about what to do about it.
        if means == "busy" and clears != "wait":
            return _text("'busy' means the application is holding something and will let go "
                         "of it. Record it with clears_by 'wait' and say roughly how many "
                         "seconds. If it never clears on its own it is not busy — it is "
                         "'permission' if a person must act, or an 'interruption' if there "
                         "is something to press.")

        if means == "session" and clears != "reauthenticate":
            return _text("a lost session clears by signing on again — record clears_by "
                         "'reauthenticate'.")
        if clears == "click" and not args.get("control"):
            return _text("say which control dismisses it")

        self.map.messages = [m for m in self.map.messages
                             if m.text.strip().lower() != text.lower()]
        self.map.messages.append(Message(
            text=text, means=means, clears_by=clears,
            control=args.get("control") or None,
            seconds=float(args.get("seconds") or 2.0),
            seen_on=[args.get("seen_on")] if args.get("seen_on") else [],
            why=args.get("why", "")))
        self.log.emit("message_recorded", text=text, means=means, clears_by=clears)
        return _text(f"recorded {text!r} as {means} (clears by {clears})")

    def _landing(self) -> str:
        """The screen entry lands on, by id — the root of every route."""
        wanted = (self.map.entry_lands_on or "").strip().lower()
        for screen in self.map.screens:
            if screen.id.lower() == wanted or screen.title.lower() == wanted:
                return screen.id
        return self.map.screens[0].id if self.map.screens else ""

    def _unreachable(self) -> list[str]:
        """Screens with no route to them from the landing screen."""
        root = self._landing()
        if not root:
            return []
        reached, frontier = {root}, [root]
        while frontier:
            screen = self.map.screen(frontier.pop())
            if screen is None:
                continue
            for control in screen.controls:
                if control.leads_to and control.leads_to not in reached:
                    reached.add(control.leads_to)
                    frontier.append(control.leads_to)
        # The sign-on screen is before the landing screen, so it is never reached
        # from it and that is correct rather than a gap.
        return sorted(sc.id for sc in self.map.screens
                      if sc.id not in reached and sc.needs_signin)

    async def do_finish(self, args: dict) -> dict:
        self.turn += 1

        # The first run visited four screens and stopped, leaving eight unmapped.
        # It had been everywhere it *chose* to go, which is not the same thing.
        unrecorded = sorted(self._seen_urls - {s.url for s in self.map.screens})
        if unrecorded:
            return _text(
                "not yet — you have been to these and not recorded them:\n  "
                + "\n  ".join(unrecorded)
                + "\n\nAlso follow the links on the screens you have recorded: a control "
                  "with a leads_to you have not visited is a screen missing from the map.")

        bare = [s.id for s in self.map.screens if not s.controls and not s.values]
        if bare:
            return _text(f"these screens were recorded with nothing on them: {bare}. "
                         "A screen with no controls and no values is a screen you have not "
                         "actually mapped. Go back and record what is on them.")

        # The map exists so that a route through the application can be worked out
        # from it. Without leads_to on the controls that navigate, it is a list of
        # screens with no edges — and a list of screens cannot answer "how do I get
        # to the member record". The first complete-looking map had 25 verified
        # controls, none of them connected, and nothing could be planned from it.
        stranded = self._unreachable()
        if stranded:
            return _text(
                f"these screens cannot be reached from {self._landing()!r} by following "
                f"the controls you recorded: {stranded}.\n\n"
                "Set leads_to on the control that opens each one. Without it the map "
                "says what exists but not how to get there, which is the half that "
                "makes it useful.")

        # A field that already shows something is a value as well as a control. The
        # contact screen recorded four controls and one value, so the telephone and
        # post code it displays were nowhere in the map.
        thin = [sc.id for sc in self.map.screens
                if sum(1 for c in sc.controls if c.role in ("textbox", "combobox")) > len(sc.values)
                and any(c.role == "textbox" for c in sc.controls)]
        if thin:
            return _text(
                f"these screens have fields whose contents are not recorded: {thin}. "
                "A field showing an existing value is a value as well as a control — "
                "record it with record_value so a later task can read it.")

        self.finished = True
        self.log.emit("map_finished", **{"screens": len(self.map.screens),
                                         "messages": len(self.map.messages),
                                         "note": args.get("note", "")[:500]})
        return _text("map closed")


def _describe(target: Any) -> str:
    """A target as the model asked for it, for a message about why it did not work."""
    role = getattr(target, "role", "") or ""
    name = getattr(target, "name", None) or getattr(target, "anchor", None)
    if name:
        return f"{role or 'control'} {name!r}".strip()
    selector = getattr(target, "selector", None)
    if selector:
        return f"css {selector!r}"
    return f"{role or 'control'} #{getattr(target, 'index', '?')}"


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


# One control per call, with flat fields.
#
# Two runs were lost to the nested version. The SDK's tool schemas do not carry an
# array of objects: the flat fields on the same tool arrived fine and the controls
# array arrived empty every time, with nothing refused because nothing was sent.
# The model could see that its controls were vanishing, and spent its turns probing
# — recording screens called "test3" and "probe6" — trying to find which field was
# at fault.
#
# Flat is also the better design here. A control verified the moment it is named
# gets an immediate yes or no, and a refusal names what is on the page instead, so
# the next attempt is informed rather than another guess.
TARGETING = {
    "strategy": {"type": "string", "enum": ["role_name", "after_text", "ordinal", "css"],
                 "description": "role_name when it has a visible name; after_text for a "
                                "control following visible text, which is what unlabelled "
                                "legacy fields need; ordinal for the nth of a role; css last"},
    "role": {"type": "string",
             "description": "button, link, textbox, combobox, checkbox, cell, heading"},
    "name": {"type": "string", "description": "the accessible name, for role_name"},
    "anchor": {"type": "string", "description": "the visible text it follows, for after_text"},
    "index": {"type": "integer", "description": "1-based, for ordinal"},
    "selector": {"type": "string", "description": "for css, last resort"},
}


def make_map_tools(ctx: MapContext) -> Any:
    @tool("observe", "Look at the current screen.", {})
    async def observe(args): return await ctx.do_observe(args)

    @tool("navigate", "Go to a path on this application.",
          {"url": {"type": "string"}, "reason": {"type": "string"}})
    async def navigate(args): return await ctx.do_navigate(args)

    @tool("click", "Click a control.", {**TARGET_FIELDS, "reason": {"type": "string"}})
    async def click(args): return await ctx.do_click(args)

    @tool("type_text", "Type into a field.",
          {**TARGET_FIELDS, "text": {"type": "string"}, "reason": {"type": "string"}})
    async def type_text(args): return await ctx.do_type(args)

    @tool("record_entry", "Record the steps that got you signed on as the verified way in.",
          {"lands_on": {"type": "string", "description": "screen id you end up on"}})
    async def record_entry(args): return await ctx.do_record_entry(args)

    @tool("record_screen",
          "Record the screen you are looking at, then add what is on it with "
          "record_control and record_value.",
          {"id": {"type": "string", "description": "short slug, e.g. member_record"},
           "title": {"type": "string"},
           "identifies_by": {"type": "string",
                             "description": "shortest text on this screen and no other"},
           "needs_signin": {"type": "boolean"},
           "note": {"type": "string"}})
    async def record_screen(args): return await ctx.do_record_screen(args)

    @tool("record_control",
          "Add one control to a screen you have recorded. It is looked up on the page "
          "first; if it does not resolve you are told what is there instead.",
          {"screen_id": {"type": "string"},
           **TARGETING,
           "leads_to": {"type": "string", "description": "screen id this opens, if known"},
           "note": {"type": "string"}})
    async def record_control(args): return await ctx.do_record_control(args)

    @tool("record_value",
          "Add one labelled value a screen displays. Checked the same way.",
          {"screen_id": {"type": "string"},
           "label": {"type": "string", "description": "what the label says"},
           **TARGETING})
    async def record_value(args): return await ctx.do_record_value(args)

    @tool("record_message",
          "Record a message the application answers with, and what should be done about it.",
          {"text": {"type": "string", "description": "shortest distinctive substring"},
           "means": {"type": "string",
                     "enum": ["answer", "caller_error", "interruption", "busy",
                              "permission", "session", "unknown"]},
           "clears_by": {"type": "string",
                         "enum": ["nothing", "click", "wait", "reauthenticate"]},
           "control": {"type": "string", "description": "what to press, if clears_by=click"},
           "seconds": {"type": "number", "description": "how long to wait, if clears_by=wait"},
           "seen_on": {"type": "string", "description": "screen id"},
           "why": {"type": "string"}})
    async def record_message(args): return await ctx.do_record_message(args)

    @tool("finish_map", "You have been everywhere you can reach.",
          {"note": {"type": "string"}})
    async def finish_map(args): return await ctx.do_finish(args)

    return create_sdk_mcp_server(
        name="sitemap", version="1.0.0",
        tools=[observe, navigate, click, type_text, record_entry, record_screen,
               record_control, record_value, record_message, finish_map])


async def build_map(*, base_url: str, credentials: dict[str, str] | None = None,
                    product: str = "", policy: Policy | None = None,
                    headless: bool = True, model: str = "claude-sonnet-5",
                    max_turns: int = MAX_TURNS) -> tuple[SiteMap, EventLog]:
    """Walk the application once and return what was verifiably found."""
    log = EventLog("sitemap", base_url)
    surface = SurfaceThread(headless=headless)
    policy = policy or for_app(base_url, mode="discover")
    ctx = MapContext(base_url, log, surface, policy, credentials)
    ctx.map.product = product

    hint = ""
    if credentials:
        listed = "\n".join(f"  - {k}: {v!r}" for k, v in credentials.items())
        hint = ("\n\nCredentials for signing on, matched to fields by name:\n" + listed)

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"sitemap": make_map_tools(ctx)},
        allowed_tools=[f"mcp__sitemap__{n}" for n in
                       ("observe", "navigate", "click", "type_text", "record_entry",
                        "record_screen", "record_control", "record_value",
                        "record_message", "finish_map")],
        max_turns=max_turns, model=model)

    prompt = (f"Survey the application at {base_url}. Start by navigating there and "
              f"observing.{hint}")

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            inferences = 0
            closing = False
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    inferences += 1
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            log.emit("model_said", text=block.text.strip()[:1500])
                if isinstance(message, ResultMessage):
                    # What the model actually cost, from the SDK rather than from
                    # counting tool calls. A tool-call count is neither the number
                    # of inferences nor the price: one response can carry several
                    # tool calls, and a response can carry none.
                    usage = message.usage or {}
                    log.emit(
                        "model_cost",
                        inferences=inferences,
                        sdk_turns=message.num_turns,
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        cache_read_tokens=usage.get("cache_read_input_tokens"),
                        cache_write_tokens=usage.get("cache_creation_input_tokens"),
                        cost_usd=message.total_cost_usd,
                        api_ms=message.duration_api_ms,
                        wall_ms=message.duration_ms,
                    )
                if ctx.finished:
                    # Do not break here. The SDK sends ResultMessage last, carrying
                    # the token counts and the actual dollar cost — the only honest
                    # answer to "how many times did you call the model, and what did
                    # it cost". Breaking the moment the tool fired meant that message
                    # never arrived, so cost was never recorded. Stop consuming once
                    # it does, or once the stream ends on its own.
                    closing = True
                if closing and isinstance(message, ResultMessage):
                    break
        # The status has to say whether the model finished, not whether anything was
        # recorded. An earlier run was reported "success" with nine screens and no
        # controls: it had spent its whole turn budget being refused.
        log.finish("success" if ctx.finished else "incomplete",
                   turns=ctx.turn, screens=len(ctx.map.screens),
                   messages=len(ctx.map.messages), refused=ctx.refused)
    finally:
        await surface.close()

    return ctx.map, log
