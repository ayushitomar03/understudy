"""The discovery runner: the model drives, this keeps the books.

Division of labour, enforced here:

  * The model decides everything — which control, which strategy, when to give
    up on one and try another, when it is finished, when it is stuck.

  * The runner decides nothing. It executes what it is asked, judges
    mechanically whether anything happened, records the attempt, and enforces
    limits. It never branches on what the page contains.

The artifact is assembled the same way. The runner knows exactly which actions
worked, so it accumulates the step list; the model supplies the meaning the
runner cannot infer — which typed values are really parameters, which reads are
really outputs, and what proves the goal was reached.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              ResultMessage, TextBlock)

from ..artifact.schema import (
    AppRef,
    BusinessOutcome,
    Capability,
    Check,
    ElementExists as _EE,
    ElementExists,
    Locator,
    Output,
    Param,
    Provenance,
    Step,
    TextMatches,
    Value,
)
from ..evidence import EventLog
from ..handoff import RECORDER_JS, InterventionRequest, Lease
from ..policy import Policy, for_app
from ..surface.web import WebSurface
from ..surface.base import RoleName, Target
from .ledger import Attempt, Ledger
from .surface_thread import SurfaceThread
from .tools import build_target, describe_change, make_tools, render_result

MAX_TURNS = 40
ESCALATION_TIMEOUT_S = 600
OPERATOR_URL = "http://localhost:8765/operator"

# Values that are obviously not parameter names. The field used to be called
# value_is_parameter, which reads like a flag — and the model duly answered it
# like one, creating parameters called "true" and "false".
NOT_A_NAME = {"true", "false", "yes", "no", "none", "null", "0", "1", ""}


def _param_name(raw: str | None) -> str:
    name = (raw or "").strip()
    return "" if name.lower() in NOT_A_NAME else name


SYSTEM_PROMPT = """You are operating a legacy bank back-office web application through a \
small set of tools, the way a human operator would. You cannot call any API and you cannot \
run code — only the tools you have been given.

Your job is to accomplish the goal, and in doing so discover a flow that can be replayed \
later without you.

How to work:

- Start by calling observe to see what is on the page. The result lists every control you \
can target and how to target it.
- Many fields in this application have no accessible name. When a control is listed as \
`textbox after "Username"`, target it with strategy=after_text, role=textbox, anchor=Username.
- Prefer role_name when a control has a real name. Prefer after_text over css. Only use css \
if nothing else can reach the control — it will not work on other surfaces.
- If an action fails, read the list of controls in the error. It tells you what is actually \
there. Do not retry the same target twice.
- When a value is something a caller would vary between runs — an account number, a customer \
id — call declare_parameter for it as soon as you know. Do this even when the value is not \
typed: if you click a link named 13344 to reach an account, the account number is still a \
parameter. Do NOT declare login credentials as parameters; they are configuration, and they \
are handled for you.
- When you read a value the capability should return, set output_name. Target it by the
label next to it, never by the value itself: a locator that searches for "$10.45" finds
nothing for any other account.
- Call note when you establish something worth not re-deriving.
- Call finish as soon as the goal is met, and say what text on the page proves it.
- Call escalate if you are stuck, blocked, or something needs a human decision.

Work one action at a time and check the result before the next one."""


class Budget:
    """What one task may spend before a person is asked instead.

    Measured reason for this existing: one task burned 39 model responses and 530
    seconds and produced nothing, and nothing in the system objected. Cost grows
    with the square of the turns in a task — every call re-reads the whole
    conversation so far — so a run that has already taken thirty turns is not
    merely slow, it is the expensive part of the bill.

    The cap is on model responses rather than dollars because dollars only arrive
    at the end of the stream, and a guard that learns the price after paying it is
    not a guard.
    """

    def __init__(self, inferences: int = 40):
        self.limit = inferences
        self.used = 0

    def spend(self) -> bool:
        """Count one model response. False when the budget is gone."""
        self.used += 1
        return self.used <= self.limit

    @property
    def exhausted(self) -> bool:
        return self.used > self.limit


class RunContext:
    """Everything one discovery run owns. The tools are thin wrappers over this."""

    def __init__(self, *, goal: str, base_url: str, log: EventLog, surface: SurfaceThread,
                 stated_goal: str | None = None,
                 credentials: dict[str, str] | None = None,
                 parameters: dict[str, str] | None = None,
                 policy: Policy | None = None):
        self.goal = goal
        # What the capability publishes as its purpose. The prompt may carry
        # coaching — prior dead ends, a champion's steps — and none of that is
        # part of the contract an agent reads. Without the split, a capability's
        # public description became a wall of internal orchestration notes.
        self.stated_goal = stated_goal or goal
        self.base_url = base_url.rstrip("/")
        self.log = log
        self.surface = surface
        self.ledger = Ledger()
        self.policy = policy or Policy(mode="discover")
        self.credentials = credentials or {}

        self.lease = Lease(log.dir)
        self.sitemap = None        # set by discover() when a map is available
        self.budget = Budget()
        self.turn = 0
        self.finished = False
        self.self_checks_left = 2      # bounded: a model that cannot fix it should stop trying
        self._pending_id = "capability"
        self._pending_model = "unknown"
        self.escalated: dict | None = None
        self.summary = ""
        self.success_text = ""

        # the draft flow, accumulated from actions that actually worked
        self.steps: list[Step] = []
        self.params: dict[str, Param] = {}
        # Parameters the caller commissioned, name -> the example value used in
        # this run. Seeded up front because whoever asks for a capability
        # already knows its signature; leaving the model to infer which literals
        # are variable is a guess it has no reason to get right.
        self.param_values: dict[str, str] = dict(parameters or {})
        self.secret_names: dict[str, str] = {v: k for k, v in (credentials or {}).items()}
        self.outputs: list[Output] = []
        self.business_outcomes: list[BusinessOutcome] = []
        self.output_patterns: dict[str, str] = {}
        self.success_template: str | None = None
        self._output_values: dict[str, str] = {}
        self._last_digest = ""
        self._last_tree = ""

        for name, value in self.param_values.items():
            self.params[name] = Param(name=name, example=value,
                                      description="supplied by the caller per invocation")

        for value in self.credentials.values():
            self.log.redactor.protect(value)

    # -- helpers -----------------------------------------------------------

    def _mask(self, tree: str) -> str:
        """Mask everything that legitimately varies between invocations.

        Parameters become their placeholder; extracted outputs become {out},
        since their whole point is to differ per run. What is left is the part
        of the page that should look the same every time.
        """
        # Redact before templating, never after. A parameter value can be a
        # substring of a credential — the DN "cn=admin,dc=firstvalley,dc=test"
        # contains the org_dn parameter — so templating first rewrites the
        # secret into "cn=admin,{org_dn}", which no longer matches the string
        # redaction is looking for. The credential fragment then ships in the
        # artifact.
        masked = self.log.redactor.scrub(tree)
        masked = self._templatise(masked) or masked
        for value in sorted(self._output_values.values(), key=len, reverse=True):
            if value and len(value) >= 2:
                masked = masked.replace(value, "{out}")
        return masked

    def _templatise(self, text: str | None) -> str | None:
        """Replace declared parameter values with their placeholders.

        The runner does this rather than asking the model to write placeholders
        itself: it knows exactly which literal was used, so the substitution
        cannot be got wrong or forgotten in one of the three places the value
        appears (a typed value, a link name, a URL).
        """
        if not text:
            return text
        for name, value in self.param_values.items():
            if value and value in text:
                text = text.replace(value, "{" + name + "}")
        return text

    def _chain(self, target: Target, why: str) -> Locator:
        """Record a locator, with any declared parameter values templated out."""
        data = target.model_dump()
        for field in ("name", "anchor", "selector"):
            if field in data and isinstance(data[field], str):
                data[field] = self._templatise(data[field])
        target = type(target)(**data)
        return Locator(target=target, why=why or "verified in discovery", verified=True)

    def _page_for_model(self, obs, *, force_tree: bool = False) -> str:
        """The page as the model needs to see it.

        The tree is about 450 tokens and it is returned after every action, so on a
        nine-action task it enters the conversation nine times and is re-read by
        every later call. That is the single largest avoidable cost we measured.

        When the map recognises the screen, the model does not need it described: it
        needs to know which screen it is and what is on it, which is a tenth of the
        size and more useful. The tree is still sent whenever the screen is unknown,
        which is exactly when the model has to read it.
        """
        if force_tree or self.sitemap is None:
            return f"accessibility tree:\n{obs.tree}"

        screen = self.sitemap.where(obs.tree)
        if screen is None:
            return ("This screen is not in the map — read it carefully.\n\n"
                    f"accessibility tree:\n{obs.tree}")

        controls = ", ".join(f"{c.name!r}" + (f" -> {c.leads_to}" if c.leads_to else "")
                             for c in screen.controls if c.verified)
        values = ", ".join(v.label for v in screen.values)
        lines = [f"You are on {screen.id!r}, which is in the map."]
        if controls:
            lines.append(f"Controls here: {controls}")
        if values:
            lines.append(f"Values shown here: {values}")
        lines.append("Act on these by name. Ask to observe if you need the full tree.")
        return "\n".join(lines)

    async def _observe(self):
        return await self.surface.call(lambda s: s.observe())

    async def _capture(self, label: str) -> str | None:
        try:
            path = self.log.screenshot_path(label)
            return await self.surface.call(lambda s: s.screenshot(path))
        except Exception:  # pragma: no cover
            return None

    def _record(self, action: str, args: dict, verdict: str, detail: str,
                before: str = "", after: str = "", candidates=None) -> Attempt:
        attempt = Attempt(
            turn=self.turn,
            action=action,
            args={k: v for k, v in args.items() if k not in ("reason",) and v not in (None, "")},
            reason=args.get("reason", ""),
            verdict=verdict,  # type: ignore[arg-type]
            detail=detail,
            digest_before=before,
            digest_after=after,
            candidates=candidates or [],
        )
        self.ledger.record(attempt)
        self.log.emit(
            "attempt",
            turn=self.turn,
            action=action,
            args=attempt.args,
            reason=attempt.reason,
            verdict=verdict,
            detail=detail,
            url=self._last_url,
        )
        return attempt

    _last_url = ""

    # -- the tool bodies ---------------------------------------------------

    async def run_prelude(self, capability) -> tuple[int, str]:
        """Replay a known-good prefix deterministically, then hand over.

        Five independent attempts re-walk the same solved ground: roughly forty
        of every sixty seconds went on redoing login and navigation that already
        worked. Executing the champion's steps here spends the model's budget
        only where the outcome is still uncertain.

        It stops at the first step that does not reproduce, which is itself the
        useful signal — that step is where the flow is fragile, and it is
        exactly where the model should pick up.
        """
        done = 0
        for step in capability.steps:
            target = None
            if step.target:
                data = step.target.target.model_dump()
                for field in ("name", "anchor", "selector"):
                    if isinstance(data.get(field), str):
                        data[field] = self._fill(data[field])
                target = type(step.target.target)(**data)

            if step.action == "navigate":
                url = self._fill(step.url or "")
                full = url if url.startswith("http") else f"{self.base_url}/{url.lstrip('/')}"
                result = await self.surface.call(lambda s, u=full: s.navigate(u))
            elif target is None:
                continue
            elif step.action == "click":
                result = await self.surface.call(lambda s, t=target: s.click(t))
            elif step.action == "type":
                value = self._value_for(step)
                result = await self.surface.call(lambda s, t=target, v=value: s.type(t, v))
            elif step.action == "select":
                value = self._value_for(step)
                result = await self.surface.call(lambda s, t=target, v=value: s.select(t, v))
            elif step.action == "read":
                result = await self.surface.call(lambda s, t=target: s.read(t))
            else:
                continue

            if not result.ok:
                self.log.emit("prelude_stopped", at_step=step.index, error=(result.error or "")[:200])
                break
            self.steps.append(step.model_copy(deep=True))
            done += 1

        obs = await self._observe()
        self._last_digest, self._last_tree, self._last_url = obs.digest, obs.tree, obs.url
        await self._capture("prelude")
        self.log.emit("prelude", steps_replayed=done, of=len(capability.steps), url=obs.url)
        return done, obs.url

    def _fill(self, text: str) -> str:
        for name, value in self.param_values.items():
            text = text.replace("{" + name + "}", value)
        return text

    def _value_for(self, step) -> str:
        if step.value is None:
            return ""
        if step.value.from_secret:
            return self.credentials.get(step.value.from_secret, "")
        if step.value.from_param:
            return self.param_values.get(step.value.from_param, "")
        return self._fill(step.value.literal or "")

    async def do_observe(self, reason: str) -> dict:
        self.turn += 1
        obs = await self._observe()
        self._last_digest, self._last_tree, self._last_url = obs.digest, obs.tree, obs.url
        shot = await self._capture("observe")
        self.log.emit("observation", turn=self.turn, url=obs.url, digest=obs.digest,
                      stable=obs.stable, screenshot=shot, reason=reason,
                      controls=len(obs.candidates))
        note = "" if obs.stable else "NOTE: the page was still changing when read."
        return render_result(verdict="observed", url=obs.url, candidates=obs.candidates,
                             extra=f"{note}\n\n{self._page_for_model(obs)}")

    async def do_navigate(self, url: str, reason: str) -> dict:
        self.turn += 1
        full = url if url.startswith("http") else f"{self.base_url}/{url.lstrip('/')}"

        verdict = self.policy.check_navigation(full)
        if not verdict.allowed:
            self._record("navigate", {"url": url, "reason": reason}, "blocked", verdict.why)
            self.log.emit("policy_blocked", action="navigate", url=url, why=verdict.why)
            return render_result(verdict="blocked", url=self._last_url, error=verdict.why)

        before = await self._observe()
        result = await self.surface.call(lambda s: s.navigate(full))
        after = await self._observe()
        self._last_digest, self._last_tree, self._last_url = after.digest, after.tree, after.url

        verdict = "advanced" if after.digest != before.digest else "no_op"
        if not result.ok:
            verdict = "error"
        self._record("navigate", {"url": url, "reason": reason}, verdict,
                     result.error or "", before.digest, after.digest)
        await self._capture("navigate")
        if result.ok:
            # Store the path, not the absolute URL. An artifact that hard-codes
            # http://localhost:8080 is bound to one instance forever — the
            # multi-tenant story in the brief is impossible if the very first
            # step names a host.
            recorded = self._templatise(url) or ""
            if recorded.startswith(self.base_url):
                recorded = recorded[len(self.base_url):].lstrip("/") or "/"
            self.steps.append(Step(index=len(self.steps) + 1, intent=reason or f"Go to {url}",
                                   action="navigate", url=recorded))
        return render_result(verdict=verdict, url=after.url, error=result.error,
                             change=describe_change(before.tree, after.tree),
                             candidates=after.candidates)

    async def do_click(self, args: dict) -> dict:
        return await self._interact("click", args, lambda s, t: s.click(t))

    async def do_type(self, args: dict) -> dict:
        text = args.get("text", "")
        secret = self.secret_names.get(text)
        param = "" if secret else _param_name(args.get("parameter_name"))

        if secret:
            self.log.redactor.protect(text)
        elif param:
            self.param_values.setdefault(param, text)
            self.params.setdefault(param, Param(name=param, example=text,
                                                description="value typed during discovery"))
        return await self._interact("type_text", args, lambda s, t: s.type(t, text),
                                    param=param, literal=text, secret=secret)

    async def do_select(self, args: dict) -> dict:
        option = args.get("option", "")
        param = _param_name(args.get("parameter_name"))
        if param:
            self.params.setdefault(param, Param(name=param, example=option))
        return await self._interact("select_option", args, lambda s, t: s.select(t, option),
                                    param=param, literal=option)

    async def do_read(self, args: dict) -> dict:
        self.turn += 1
        try:
            target = build_target(args)
        except ValueError as exc:
            self._record("read_value", args, "error", str(exc))
            return render_result(verdict="error", url=self._last_url, error=str(exc))

        result = await self.surface.call(lambda s: s.read(target))
        obs = await self._observe()
        self._last_url = obs.url
        verdict = "advanced" if result.ok else "unresolved"
        self._record("read_value", args, verdict, result.error or f"value={result.value!r}",
                     self._last_digest, obs.digest,
                     candidates=result.resolution.candidates if result.resolution else [])

        if result.ok:
            name = (args.get("output_name") or "").strip()

            # An output located by its own value is self-defeating: the locator
            # encodes the answer it is meant to discover, so it resolves for the
            # discovery input and nothing else. Seen live — the balance cell was
            # targeted by searching for "$10.45", and every other account failed
            # at that step. Refused here rather than at replay, because here the
            # model can still fix it.
            handle = (getattr(target, "name", "") or getattr(target, "anchor", "") or "")
            value = (result.value or "").strip()

            pattern = self.output_patterns.get(name) if name else None
            problem = None
            if pattern and value and not re.fullmatch(pattern, value):
                problem = f"{name}={value!r} does not match {pattern}"
            if problem:
                self._record("read_value", args, "blocked", problem)
                return render_result(
                    verdict="blocked", url=obs.url,
                    error=(f"{problem}. That is not the right kind of value for {name!r} — "
                           "you have read the wrong control. Find the one that holds it."))

            if name and not value:
                # Declaring an output that read nothing produces a capability
                # that returns empty strings and reports success. Seen live:
                # four outputs declared, every one of them "".
                self._record("read_value", args, "blocked", f"output {name!r} read an empty value")
                return render_result(
                    verdict="blocked", url=obs.url,
                    error=(f"that target read an empty value, so it is not the one holding the "
                           f"data. Do not declare it as output {name!r}. Find the control that "
                           f"actually contains the value and read that."))
            if name and value and len(value) > 1 and value in handle:
                self._record("read_value", args, "blocked",
                             f"output {name!r} was targeted by its own value")
                self.log.emit("self_referential_locator", output=name, handle=handle)
                return render_result(
                    verdict="blocked", url=obs.url,
                    error=(f"you located this value by its own content ({handle!r}). That only "
                           f"works for this one input. Target it by the label beside it instead "
                           f"— for example strategy=after_text with the label as the anchor."))

            chain = self._chain(target, args.get("reason", ""))
            self.steps.append(Step(index=len(self.steps) + 1,
                                   intent=args.get("reason") or f"Read {name or 'a value'}",
                                   action="read", target=chain))
            if name:
                # A later read of the same output replaces the earlier one. The
                # model often declares on its first attempt, sees the value is
                # wrong and reads the right control next — silently keeping the
                # first declaration discards exactly that correction, and ships
                # a capability that returns the wrong field.
                self.outputs = [o for o in self.outputs if o.name != name]
                self.outputs.append(Output(name=name, locator=chain, at_step=len(self.steps),
                                           pattern=self.output_patterns.get(name)))
                self._output_values[name] = result.value or ""
                self.log.emit("output_declared", name=name, value=result.value,
                              replaced=name in self._output_values)

        return render_result(verdict=verdict, url=obs.url, value=result.value,
                             error=result.error,
                             candidates=result.resolution.candidates if result.resolution and not result.ok else None)

    async def _interact(self, action: str, args: dict, fn, *, param: str = "", literal: str = "",
                        secret: str | None = None) -> dict:
        self.turn += 1

        if repeat := self.ledger.already_failed(action, {k: v for k, v in args.items()
                                                         if k not in ("reason",) and v not in (None, "")}):
            self._record(action, args, "blocked", f"identical attempt already {repeat.verdict} on turn {repeat.turn}")
            return render_result(
                verdict="blocked", url=self._last_url,
                error=f"you already tried this exact target on turn {repeat.turn} and it was {repeat.verdict}. "
                      "Pick a different target or strategy.",
            )

        try:
            target = build_target(args)
        except ValueError as exc:
            self._record(action, args, "error", str(exc))
            return render_result(verdict="error", url=self._last_url, error=str(exc))

        kind = {"type_text": "type", "select_option": "select"}.get(action, action)

        # Resolve first, then judge what was found. Classifying on the caller's
        # description lets the same button be refused under one strategy and
        # permitted under another.
        found = await self.surface.call(lambda s, t=target: s.resolve(t))
        control = found.name if found.found else (args.get("name") or args.get("anchor"))
        role = found.role if found.found else args.get("role")

        verdict = self.policy.check_action(kind, control, role)
        if not verdict.allowed:
            self._record(action, args, "blocked", verdict.why)
            self.log.emit("policy_blocked", action=kind, control=control, role=role,
                          asked_as=args.get("strategy"), risk=verdict.risk, why=verdict.why)
            return render_result(
                verdict="blocked", url=self._last_url, error=verdict.why,
                extra="If this action is genuinely required, call escalate — a human decides it.")

        self.lease.assert_agent()
        before = await self._observe()
        result = await self.surface.call(lambda s: fn(s, target))
        after = await self._observe()
        self._last_digest, self._last_tree, self._last_url = after.digest, after.tree, after.url

        if not result.ok:
            verdict = "unresolved" if result.resolution and not result.resolution.found else "error"
        elif after.digest == before.digest:
            # Confirm before reporting nothing happened. A premature read of a
            # form submission looks identical to a genuine no-op, and telling
            # the model an irreversible action did nothing invites a repeat.
            changed = await self.surface.call(lambda s: s.digest_changed_from(before.digest))
            if changed:
                after = await self._observe()
                self._last_digest, self._last_tree, self._last_url = after.digest, after.tree, after.url
                verdict = "advanced"
            else:
                verdict = "no_op"
        else:
            verdict = "advanced"

        self._record(action, args, verdict, result.error or result.detail,
                     before.digest, after.digest,
                     candidates=result.resolution.candidates if result.resolution else [])
        await self._capture(action)

        if result.ok:
            value = None
            if action in ("type_text", "select_option"):
                if secret:
                    value = Value(from_secret=secret)     # the literal is never recorded
                elif param:
                    value = Value(from_param=param)
                else:
                    value = Value(literal=self._templatise(literal))
            self.steps.append(Step(
                index=len(self.steps) + 1,
                intent=args.get("reason") or action,
                action="type" if action == "type_text" else ("select" if action == "select_option" else "click"),
                target=self._chain(target, args.get("reason", "")),
                value=value,
            ))

        return render_result(verdict=verdict, url=after.url, error=result.error,
                             change=describe_change(before.tree, after.tree),
                             candidates=after.candidates if not result.ok else None)

    async def do_declare_param(self, args: dict) -> dict:
        name = (args.get("name") or "").strip()
        value = args.get("value") or ""
        if not name or not value:
            return render_result(verdict="error", url=self._last_url,
                                 error="declare_parameter needs both name and value")
        if value in self.secret_names:
            return render_result(
                verdict="blocked", url=self._last_url,
                error="that value is a configured credential, not a caller input. It is already "
                      "handled — do not declare it as a parameter.")

        self.param_values[name] = value
        self.params.setdefault(name, Param(name=name, example=value,
                                           description=args.get("description") or None))
        self._retemplatise()
        self.log.emit("param_declared", name=name, description=args.get("description", ""))
        return render_result(verdict="declared", url=self._last_url,
                             extra=f"{value!r} will be recorded as {{{name}}} everywhere it appears.")

    def _retemplatise(self) -> None:
        """Apply a newly declared parameter to steps already recorded.

        A parameter is usually recognised after it has been used — the model
        clicks the account link, then realises the account number is an input.
        Without this, the earlier steps keep the hard-coded literal and the
        capability silently only works for one account.
        """
        for step in self.steps:
            if step.url:
                step.url = self._templatise(step.url)
            if step.value and step.value.literal:
                templated = self._templatise(step.value.literal)
                if templated != step.value.literal:
                    name = templated.strip("{}")
                    step.value = Value(from_param=name) if name in self.param_values else step.value
            if step.target:
                data = step.target.target.model_dump()
                changed = False
                for field in ("name", "anchor", "selector"):
                    if isinstance(data.get(field), str):
                        new = self._templatise(data[field])
                        if new != data[field]:
                            data[field], changed = new, True
                if changed:
                    step.target.target = type(step.target.target)(**data)

    async def probe_failure_states(self) -> None:
        """Re-run the flow with a deliberately wrong parameter and record how
        the application says no.

        Declared outcomes have to match what the app really does. Written from
        imagination they look thorough and never fire, and a discovery run that
        succeeds never sees the failure path at all — so it gets provoked.

        Two shapes come back, and the structural one is the common case. This
        flow reaches an account by clicking its link, so a bad id does not
        produce an error page: the link simply is not there. "The control that
        carries the parameter does not resolve" is a better detector than any
        string, because it needs no knowledge of the app's error vocabulary.
        """
        if not self.param_values or not self.steps:
            return

        name = next(iter(self.param_values))
        bogus = "99999" if self.param_values[name].isdigit() else "zzz-no-such-record"
        placeholder = "{" + name + "}"
        self.log.emit("probing_failure", param=name, value=bogus)

        success_lines = set((self.success_template or "").splitlines())
        missing_target = None
        missing_at: int | None = None

        for step in self.steps:
            target = step.target.target if step.target else None
            carries_param = False
            if target is not None:
                data = target.model_dump()
                for field in ("name", "anchor", "selector"):
                    if isinstance(data.get(field), str) and placeholder in data[field]:
                        carries_param = True
                        data[field] = data[field].replace(placeholder, bogus)
                probe_target = type(target)(**data)

            if step.action == "navigate":
                url = (step.url or "").replace(placeholder, bogus)
                full = url if url.startswith("http") else f"{self.base_url}/{url.lstrip('/')}"
                await self.surface.call(lambda s, u=full: s.navigate(u))
                continue
            if target is None:
                continue

            found = await self.surface.call(lambda s, t=probe_target: s.resolve(t).found)
            if not found and carries_param:
                # The control that carries the parameter is absent. That is the
                # application saying the record does not exist.
                missing_target, missing_at = step.target.target, step.index
                break
            if not found:
                # A step that does not carry the parameter and does not resolve
                # usually means the session is already past it — discovery ends
                # logged in, so the login fields are gone. Skip rather than
                # stop, or the probe never reaches the step under test.
                continue

            if step.action == "click":
                await self.surface.call(lambda s, t=probe_target: s.click(t))
            elif step.action == "type":
                value = (self.credentials.get(step.value.from_secret, "")
                         if step.value and step.value.from_secret
                         else (bogus if step.value and step.value.from_param == name else ""))
                await self.surface.call(lambda s, t=probe_target, v=value: s.type(t, v))

        obs = await self._observe()
        await self._capture("failure-probe")

        if missing_target is not None:
            self.business_outcomes.append(BusinessOutcome(
                code=f"{name}_not_found",
                detect=[_EE(target=missing_target, present=False)],
                meaning=f"No record matched the {name} supplied. A result, not an error.",
                at_step=missing_at,
            ))
            self.log.emit("business_outcome_harvested", code=f"{name}_not_found",
                          detector="structural: the control carrying the parameter is absent")
            return

        # The other shape: the app rendered something it does not render on the
        # happy path. Take the most specific line of it as the detector.
        novel = [l.strip() for l in obs.tree.splitlines()
                 if l.strip() and l not in success_lines and len(l.strip()) > 24]
        marker = next((l for l in novel if any(w in l.lower() for w in
                       ("could not", "not find", "error", "invalid", "no such"))), None)
        if not marker:
            self.log.emit("no_failure_signal",
                          note="the app gave no distinguishable signal for a bad input")
            return

        pattern = re.sub(r"^-\s*(paragraph|text|heading|cell):?\s*", "", marker).strip(' "\'')
        pattern = pattern.replace(bogus, "").strip().rstrip("#").strip()
        self.business_outcomes.append(BusinessOutcome(
            code=f"{name}_not_found",
            detect=[TextMatches(pattern=pattern)],
            meaning=f"No record matched the {name} supplied. A result, not an error.",
        ))
        self.log.emit("business_outcome_harvested", code=f"{name}_not_found", pattern=pattern)

    async def _self_check(self, capability) -> tuple[bool, str]:
        """Replay the capability that was just built, in a clean browser.

        Grading an attempt after the fact tells the orchestrator which one to
        keep; it never tells the model it was wrong. Observed: an attempt read
        the wrong field, declared it as the output, reported success, and only
        the next orchestration cycle noticed. Replaying here closes that gap
        while the model can still fix it.

        A fresh browser on purpose — reusing the discovery session would let
        cookies and an already-loaded page make a capability look more
        reproducible than it is.
        """
        from ..replay import Replayer          # local: replay must not import runner

        def run():
            surface = WebSurface(headless=True)
            try:
                log = EventLog("selfcheck", f"{capability.id} attempt", root=self.log.dir / "checks")
                policy = for_app(self.base_url, mode="unattended")
                return Replayer(capability, surface, log, policy).run(
                    self.param_values, secrets=self.credentials)
            finally:
                surface.close()

        try:
            result = await asyncio.to_thread(run)
        except Exception as exc:                # noqa: BLE001 - reported to the model
            return False, f"the capability could not be replayed at all: {exc}"

        if result.outcome != "complete":
            return False, (f"replaying it reached only {result.steps_completed}/{result.steps_total} "
                           f"steps ({result.outcome}/{result.subclass}). It failed at step "
                           f"{result.failed_step}: {result.observed[:160]}")

        # The values it returns must match what was read during discovery.
        drifted = [
            f"{name}: discovery read {self._output_values.get(name)!r}, replay got {value!r}"
            for name, value in result.outputs.items()
            if name in self._output_values and value != self._output_values[name]
        ]
        missing = [o.name for o in self.outputs if o.name not in result.outputs]
        if drifted or missing:
            parts = []
            if drifted:
                parts.append("these outputs came back different: " + "; ".join(drifted))
            if missing:
                parts.append(f"these outputs could not be read at all: {missing}")
            return False, " ".join(parts)

        return True, f"replayed cleanly, {result.steps_total} steps, outputs {result.outputs}"

    async def do_finish(self, args: dict) -> dict:
        self.finished = True
        self.summary = args.get("summary", "")
        self.success_text = self._templatise(args.get("success_text", "")) or ""

        # The state the goal was actually reached in, with everything that varies
        # per invocation masked out. This is the only record of what "right"
        # looked like at a moment it was verifiably right, so replay can compare
        # shape instead of trusting a sentence the model wrote about itself.
        obs = await self._observe()
        self.success_template = self._mask(obs.tree)
        self.success_text = self._mask(self.success_text) or ""
        await self._capture("final")
        self.log.emit("model_finished", summary=self.summary, success_text=self.success_text,
                      template_lines=len(self.success_template.splitlines()))

        # Prove it before accepting it.
        if self.self_checks_left > 0 and self.steps:
            self.self_checks_left -= 1
            try:
                capability = self.build_capability(self._pending_id, self._pending_model)
            except ValueError as exc:
                self.finished = False
                return render_result(verdict="rejected", url=self._last_url, error=str(exc))

            ok, detail = await self._self_check(capability)
            self.log.emit("self_check", passed=ok, detail=detail[:400],
                          checks_left=self.self_checks_left)
            if not ok:
                self.finished = False
                obs = await self._observe()
                self._last_url = obs.url
                return render_result(
                    verdict="rejected", url=obs.url, candidates=obs.candidates,
                    error=f"the flow you recorded does not reproduce: {detail}",
                    extra=("Fix it and call finish again. The likely cause is a target that only "
                           "works from the state you happened to be in, or an output read from "
                           "the wrong control. You are back on the page now:\n\n" + obs.tree))
        return render_result(verdict="finished", url=self._last_url,
                             extra="Recorded. The run will now be written up as a capability.")

    async def do_escalate(self, why: str, needed: str) -> dict:
        """Hand the live session to a person, wait, and take it back.

        The browser is not closed and the page is not reloaded: whatever is on
        screen — including a form the agent has already filled — is what the
        human finds. That is the requirement, and it is also the only version
        that is useful; a fresh session would mean redoing the work.
        """
        self.escalated = {"why": why, "needed": needed, "turn": self.turn}
        shot = await self._capture("escalation")

        request = InterventionRequest(
            run_id=self.log.run_id, goal=self.goal, step=self.turn, why=why,
            needed=needed, url=self._last_url, screenshot=shot,
            ledger=self.ledger.digest(),
        )
        self.lease.release(request)
        self.log.emit("control_released", holder="none", why=why, needed=needed,
                      url=self._last_url, screenshot=shot, operator_url=OPERATOR_URL)

        # start recording whatever the person does on this page
        await self.surface.call(lambda s: s.evaluate(RECORDER_JS))
        self.lease.take_human()
        self.log.emit("control_taken", holder="human")

        print(f"\n  ⏸  waiting for a human — {OPERATOR_URL}\n     {needed}", flush=True)
        control = await asyncio.get_running_loop().run_in_executor(
            None, self.lease.wait_for_decision, ESCALATION_TIMEOUT_S)

        actions = []
        try:
            actions = await self.surface.call(
                lambda s: s.evaluate("window.__understudyRecorder || []")) or []
        except Exception:  # pragma: no cover - the page may have navigated away
            pass
        self.lease.decide(control.decision or "abort", actions)

        after = await self._observe()
        await self._capture("after-handoff")
        self.log.emit("control_returned", holder=self.lease.holder,
                      decision=control.decision, human_actions=actions,
                      url=after.url, digest=after.digest)

        if control.decision != "resume":
            return render_result(verdict="aborted", url=after.url,
                                 extra="The human aborted. Stop here.")

        # Do not trust that they did exactly what was asked — re-observe and let
        # the model decide what is left, rather than resuming from a remembered
        # step index.
        self.escalated = None
        summary = ", ".join(f"{a.get('action')} {a.get('on')}" for a in actions[:8]) or "nothing recorded"
        return render_result(
            verdict="resumed", url=after.url, candidates=after.candidates,
            extra=(f"A human took the session and did: {summary}\n"
                   f"You have control again. Look at the page before doing anything —\n"
                   f"they may have done more or less than asked.\n\n{after.tree}"))

    # -- the artifact ------------------------------------------------------

    def build_capability(self, capability_id: str, model: str) -> Capability:
        """Assemble what the run produced.

        The steps are the runner's — it knows which actions worked. The success
        condition and the parameter names are the model's, because they are
        judgements about meaning rather than facts about execution.
        """
        # Every string on this artifact that the model authored — the summary,
        # the success text, each step's intent — is free prose and can contain
        # anything it happened to mention. The leak that prompted this was the
        # model writing "logged in as john/demo" into its own summary, which is
        # not something a field-by-field rule would have predicted.
        scrub = self.log.redactor.scrub
        for step in self.steps:
            step.intent = scrub(step.intent)

        success = self._derive_success()
        capability = Capability(
            id=capability_id,
            app=AppRef(product="ParaBank", base_url=self.base_url),
            goal=scrub(self.stated_goal),
            description=scrub(self.summary) or None,
            params=list(self.params.values()),
            outputs=self.outputs,
            steps=self.steps,
            success=success,
            business_outcomes=self.business_outcomes,
            success_template=self.success_template,
            provenance=Provenance(
                discovered_at=datetime.now(timezone.utc),
                model=model,
                run_id=self.log.run_id,
                evidence_path=str(self.log.dir),
                steps_attempted=len(self.ledger.attempts),
            ),
        )

        # A last, structural check rather than a promise. If a credential can
        # still be found anywhere in the serialised artifact, the artifact does
        # not get written — §3.4 is not a thing to be careful about, it is a
        # thing that has to be impossible.
        # Stamp the real risk profile on. Until this ran, every step defaulted
        # to "safe" — including the one that sent a payment.
        self.policy.annotate(capability)
        self._assert_no_secrets(capability)
        return capability

    def _derive_success(self) -> Check:
        """Take the success condition from the recorded state, not from prose.

        Measured: the model's own sentence was "Account Details page shows:
        Account Number: {account_id}, Account Type: CHECKING, Balance: $10.45"
        — it baked the discovery run's *answers* into the test for success, so
        every other account failed it while the structural template matched at
        98%.

        A heading from the approved final state is stable across invocations by
        construction, because it is the part of the page that does not carry
        the answer.
        """
        # Prefer the most specific heading available. ParaBank puts an h2
        # "Account Services" in the nav of every signed-in page, so taking the
        # first heading yields a condition that passes on the wrong page — the
        # page-level h1 is the one that identifies where you actually are.
        headings: list[tuple[int, str]] = []
        for line in (self.success_template or "").splitlines():
            match = re.match(r'\s*-\s+heading "([^"]+)"(?:\s+\[level=(\d+)\])?', line)
            if match and "{" not in match.group(1):
                headings.append((int(match.group(2) or 9), match.group(1)))
        if headings:
            headings.sort(key=lambda h: h[0])
            return ElementExists(target=RoleName(role="heading", name=headings[0][1]))

        if self.success_text and "{out}" not in self.success_text:
            return TextMatches(pattern=self.success_text)

        # Nothing stable to assert on. Say so rather than inventing a check that
        # will pass for the wrong reasons.
        self.log.emit("no_stable_success_condition",
                      note="falling back to structural shape matching alone")
        return TextMatches(pattern="")

    def _assert_no_secrets(self, capability: Capability) -> None:
        blob = capability.model_dump_json()

        # Check the templated forms too, for the same reason the masking order
        # matters: a secret that has had a parameter substituted into it is
        # still a leaked secret.
        variants: dict[str, str] = {}
        for value, name in self.secret_names.items():
            if not value or len(value) < 3:
                continue
            variants[value] = name
            templated = self._templatise(value)
            if templated and templated != value:
                variants[templated] = name

        leaked = sorted({name for value, name in variants.items() if value in blob})
        if leaked:
            self.log.emit("secret_leak_blocked", fields=leaked)
            raise ValueError(
                f"refusing to write the capability: credential(s) {leaked} appear in it"
            )


async def discover(*, goal: str, base_url: str, capability_id: str,
                   stated_goal: str | None = None,
                   parameters: dict[str, str] | None = None,
                   outputs: list[str] | None = None,
                   output_patterns: dict[str, str] | None = None,
                   credentials: dict[str, str] | None = None,
                   policy: Policy | None = None,
                   prelude=None,
                   knowledge=None,
                   headless: bool = True, model: str = "claude-sonnet-5",
                   max_turns: int = MAX_TURNS,
                   max_inferences: int = 40) -> tuple[Capability | None, EventLog]:
    """Run one autonomous discovery. The model drives; this returns what it built."""
    log = EventLog("discovery", goal)
    surface = SurfaceThread(headless=headless)
    policy = policy or Policy(mode="discover")
    log.emit("policy", mode=policy.mode, allow_irreversible=policy.allow_irreversible,
             origins=policy.allowed_origins, denied=policy.denied_paths)
    ctx = RunContext(goal=goal, base_url=base_url, log=log, surface=surface,
                     stated_goal=stated_goal, credentials=credentials,
                     parameters=parameters, policy=policy)
    ctx._pending_id, ctx._pending_model = capability_id, model
    ctx.output_patterns = output_patterns or {}
    # A SiteMap lets the runner name screens instead of re-sending them. Anything
    # else passed as knowledge (the incidental store) has no screens to match on.
    ctx.sitemap = knowledge if getattr(knowledge, "screens", None) else None
    ctx.budget = Budget(max_inferences)

    hint = ""
    if credentials:
        # Name the credentials the caller actually supplied. This used to read
        # `credentials.get("username")`, so an app whose login field is a DN was
        # told to use username None — the model duly reported that None is not
        # valid DN syntax, fell back to an anonymous bind, and lost the read
        # access the task needed.
        listed = "\n".join(f"  - {k}: {v!r}" for k, v in credentials.items())
        hint = ("\n\nIf the application asks you to log in, these are the credentials for it. "
                "Match each one to the field it belongs in by its name:\n" + listed +
                "\nDo not log in anonymously or as a guest — the task needs this identity.")

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"surface": make_tools(ctx)},
        allowed_tools=[f"mcp__surface__{n}" for n in
                       ("observe", "navigate", "click", "type_text", "select_option",
                        "read_value", "note", "finish", "escalate")],
        max_turns=max_turns,
        model=model,
    )

    output_note = ""
    if outputs:
        # The caller names the outputs for the same reason it names the
        # parameters: it is the one defining the contract. Left to itself the
        # model invents a name per run — org_name one attempt, organisation_name
        # the next — and two identical capabilities stop being comparable.
        output_note = ("\n\nThe capability must return exactly these outputs, using these names "
                       "verbatim when you call read_value: " + ", ".join(outputs) + ".")

    param_note = ""
    if parameters:
        listed = ", ".join(f"{k} (this run: {v!r})" for k, v in parameters.items())
        param_note = (
            f"\n\nThis capability takes these inputs, which a caller will vary between runs: "
            f"{listed}. Use the values given for this run; they are recorded as parameters "
            "automatically wherever they appear, so you do not need to do anything special."
        )

    prompt = (
        f"Goal: {goal}\n\n"
        f"The application is at {base_url}. Start by navigating there and observing."
        f"{param_note}{output_note}{hint}"
    )

    # What the loop already knows about this application, learned from earlier
    # tasks. The entry prefix is replayed rather than described: telling the
    # model how to log in still costs it the turns to do it.
    if knowledge is not None:
        prompt += knowledge.as_prompt()
        if prelude is None and knowledge.entry:
            from ..artifact.schema import AppRef, Capability as _Cap, ElementExists, Param
            from ..surface.base import RoleName as _RN

            # The learned prefix can legitimately reach past the login — on
            # this app every task also looks a member up, so the shared opening
            # carries a parameter. Declare whatever it references, or the
            # capability fails validation for using a param nobody declared.
            entry_steps = getattr(knowledge.entry, "steps", knowledge.entry)
            referenced = {step.value.from_param for step in entry_steps
                          if step.value and step.value.from_param}
            prelude = _Cap(
                id="knowledge.entry", app=AppRef(product="", base_url=base_url),
                goal="enter the application", steps=entry_steps,
                params=[Param(name=n, example=(parameters or {}).get(n, ""))
                        for n in sorted(referenced)],
                success=ElementExists(target=_RN(role="heading", name="")))

    if prelude is not None:
        replayed, at_url = await ctx.run_prelude(prelude)
        if replayed:
            prompt += (
                f"\n\nA previously working version of this flow has already been replayed for "
                f"you: its first {replayed} steps ran successfully and you are now at {at_url}. "
                "Those steps are already recorded — do not repeat them. Observe where you are "
                "and carry on from there. If the remaining part of that flow was wrong, this is "
                "where to do it differently."
            )

    capability: Capability | None = None
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            inferences = 0
            closing = False
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    inferences += 1
                    if not ctx.budget.spend() and not (ctx.finished or ctx.escalated):
                        ctx.escalated = {
                            "why": (f"this task used its whole budget of "
                                    f"{ctx.budget.limit} model responses without finishing"),
                            "needed": "a person should look at what it is stuck on, or the "
                                      "budget should be raised deliberately",
                            "turn": ctx.turn}
                        log.emit("budget_exhausted", limit=ctx.budget.limit,
                                 turns=ctx.turn, url=ctx._last_url)
                        closing = True
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            log.emit("model_said", text=block.text.strip()[:2000])
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
                if ctx.finished or ctx.escalated:
                    # Do not break here. The SDK sends ResultMessage last, carrying
                    # the token counts and the actual dollar cost — the only honest
                    # answer to "how many times did you call the model, and what did
                    # it cost". Breaking the moment the tool fired meant that message
                    # never arrived, so cost was never recorded. Stop consuming once
                    # it does, or once the stream ends on its own.
                    closing = True
                if closing and isinstance(message, ResultMessage):
                    break

        if ctx.finished and ctx.steps:
            await ctx.probe_failure_states()
            capability = ctx.build_capability(capability_id, model)
            path = log.dir / "capability.json"
            path.write_text(capability.model_dump_json(indent=2))
            log.emit("capability_written", path=str(path), steps=len(capability.steps),
                     params=[p.name for p in capability.params],
                     outputs=[o.name for o in capability.outputs])
            log.finish("success", turns=ctx.turn)
        elif ctx.escalated:
            log.finish("escalated", **ctx.escalated)
        else:
            log.finish("incomplete", turns=ctx.turn, reason="model stopped without calling finish")
    finally:
        await surface.close()

    return capability, log
