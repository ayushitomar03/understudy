"""Deterministic replay: the production execution path.

No model decides anything here. The capability says what to do, this does it,
and the result says how far it got. That is the whole point of the artifact —
discovery is slow, expensive and non-deterministic precisely so that this can
be none of those things.

Three things replay does that a naive "run the steps" would not:

  * It proves each step landed rather than assuming the click worked, and it
    gates success on what the capability actually depends on — its success
    condition, its parameters appearing on the page, and every declared output
    coming back and satisfying its predicate.

  * It classifies how far the task got, so a caller can tell "no such member"
    from "the form rejected your input" from "the app is broken", and route
    accordingly.

  * It reports a structural shape comparison against the approved final state
    as a drift signal, without letting it decide success — the page renders
    differently for records with no transactions, and that is data, not drift.

There is no locator fallback chain. An earlier version had one and this
docstring described it for some time after it was removed, which is worse than
never having claimed it: a reader comparing the two files finds the code
advertising robustness it does not have. Each target is a single recorded
strategy. Drift shows up as a locator that stops resolving, and the honest
version of fallback is for replay to observe that and propose an alternative —
a different mechanism from guessing alternatives in advance.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..artifact.schema import (
    Capability,
    Check,
    ElementExists,
    Locator,
    Step,
    TextMatches,
    UrlMatches,
    ValueEquals,
)
from ..evidence import EventLog
from ..policy import Policy
from ..discovery.tools import describe_change
from ..surface.base import Observation, RoleName, Target
from ..surface.web import WebSurface
from .result import ReplayResult, StepResult

# How many times one declared recovery may fire for a single step.
MAX_RECOVERIES = 4


class Bindings:
    """Parameter and secret values for one invocation.

    Secrets are held apart from parameters so that the substitution used for
    matching text can never leak one: `render` fills both, `render_public` fills
    only parameters.
    """

    def __init__(self, params: dict[str, str], secrets: dict[str, str]):
        self.params = params
        self.secrets = secrets

    def render(self, text: str | None) -> str | None:
        if not text:
            return text
        for name, value in {**self.params, **self.secrets}.items():
            text = text.replace("{" + name + "}", value)
        return text

    def render_public(self, text: str | None) -> str | None:
        if not text:
            return text
        for name, value in self.params.items():
            text = text.replace("{" + name + "}", value)
        return text

    def value_of(self, step: Step) -> str:
        if step.value is None:
            return ""
        if step.value.from_secret:
            return self.secrets.get(step.value.from_secret, "")
        if step.value.from_param:
            return self.params.get(step.value.from_param, "")
        return self.render(step.value.literal) or ""


def substitute(target: Target, bind: Bindings) -> Target:
    """Fill placeholders in a recorded locator with this invocation's values."""
    data = target.model_dump()
    for field in ("name", "anchor", "selector"):
        if isinstance(data.get(field), str):
            data[field] = bind.render_public(data[field])
    return type(target)(**data)


# A snapshot line: indentation, role, then whatever text it carries.
SKELETON = re.compile(r"^(\s*)-\s+([a-z]+)")


def skeleton(tree: str) -> list[str]:
    """The page's structure with its data removed.

    Shape matching asks "is this the same state as the one that was approved",
    and the answer must not depend on the record being displayed. Comparing the
    text failed exactly there: the template was captured on one account and
    included that account's transaction table, so every *other* account scored
    0.76 against it and a correct run was reported as not reaching its goal.

    Structure is what a state is; the values in it are what varies. Content is
    still checked, by the success condition and by the parameter echo — this
    just stops them being checked twice, once wrongly.
    """
    out: list[str] = []
    for line in tree.splitlines():
        match = SKELETON.match(line)
        if not match:
            continue
        node = f"{len(match.group(1))}:{match.group(2)}"
        # Collapse consecutive repeats. A table of three rows and a table of
        # thirty are the same shape, and the row count is data — an account
        # with more transactions than the one the template was captured on is
        # not a different kind of page.
        if not out or out[-1] != node:
            out.append(node)
    return out


def shape_match(template: str, actual: str) -> float:
    """How much of the approved state's structure is still present."""
    if not template:
        return 1.0
    want = skeleton(template)
    if not want:
        return 1.0

    got = skeleton(actual)
    # Longest common subsequence, so inserted or missing rows cost only
    # themselves rather than shifting everything after them out of alignment.
    previous = [0] * (len(got) + 1)
    for w in want:
        current = [0]
        for j, g in enumerate(got):
            current.append(previous[j] + 1 if w == g else max(current[j], previous[j + 1]))
        previous = current
    return previous[-1] / len(want)


class Replayer:
    """One capability, one set of inputs, one run."""

    def __init__(self, capability: Capability, surface: WebSurface, log: EventLog,
                 policy: Policy | None = None):
        self.cap = capability
        self.surface = surface
        self.log = log
        self.policy = policy or Policy(mode="unattended")

    # -- running -----------------------------------------------------------

    def run(self, params: dict[str, str], secrets: dict[str, str] | None = None) -> ReplayResult:
        bind = Bindings(params, secrets or {})
        for value in (secrets or {}).values():
            self.log.redactor.protect(value)

        self._validate_params(params)

        steps: list[StepResult] = []
        failed_at: int | None = None
        observed = expected = ""

        for step in self.cap.steps:
            outcome = self._run_step(step, bind)

            # A declared recovery may fire a bounded number of times for one
            # step, and only for a condition the capability already knows about.
            # No model is consulted: the rule names the text to look for and the
            # control to act on, which keeps this execution path free of
            # decisions.
            #
            # Bounded at several rather than one because an interruption can
            # recur: acknowledging Meridian's maintenance notice returns to the
            # same request, which can show it again. A single attempt capped the
            # flow at 0.4 + 0.6x0.4 = 64%, which is exactly what it measured —
            # the recovery was right and under-applied. Still bounded, because a
            # recovery that can retry forever is a flow that spins instead of
            # failing.
            for _ in range(MAX_RECOVERIES):
                if outcome.ok:
                    break
                rule = self._recovery_for()
                if rule is None:
                    break
                self.log.emit("recovering", code=rule.code, at_step=step.index, why=rule.why)
                if not self._recover(rule, bind, step.index):
                    break
                outcome = self._run_step(step, bind)
                self.log.emit("recovered", code=rule.code, at_step=step.index,
                              worked=outcome.ok)

            steps.append(outcome)
            self.log.emit("step", index=step.index, action=step.action, intent=step.intent,
                          ok=outcome.ok, detail=outcome.detail,
                          error=outcome.error, ms=outcome.ms)
            if not outcome.ok:
                failed_at = step.index
                expected = step.intent
                observed = outcome.error or "step did not complete"
                break

        final = self.surface.observe()
        shot = self.surface.screenshot(self.log.screenshot_path("final"))
        outputs = self._extract_outputs(bind) if failed_at is None else {}

        result = self._classify(steps, failed_at, final, bind, outputs, expected, observed)
        result.evidence_path = str(self.log.dir)
        if result.outcome != "complete":
            result.failure_tree = final.tree
        # Name the capability in the event. Without it, anything reading the
        # evidence has to guess from the run's goal string — which produced a
        # phantom capability called "attempt", from grade runs whose goal reads
        # "attempt 3 of <id>".
        self.log.emit("replay_result", capability=self.cap.id, version=self.cap.version,
                      outcome=result.outcome, subclass=result.subclass,
                      score=result.score, outputs=outputs, shape=result.shape_match,
                      screenshot=shot)
        return result

    def _recovery_for(self):
        """The rule matching what is on screen, if any."""
        tree = self.surface.observe().tree
        return next((r for r in self.cap.recoveries
                     if r.detect and r.detect.lower() in tree.lower()), None)

    def _recover(self, rule, bind: Bindings, at_index: int) -> bool:
        """Act on a known interruption.

        The recorded role is a hint, not a requirement. Whoever proposes a
        recovery knows the control by the words on it — "click Acknowledge" —
        and not whether the page built it as a button or a link. Measured: the
        proposer guessed `button` on two occasions out of three for a control
        that is a link, so the rule matched the screen, resolved nothing, and
        the flow failed identically while appearing to have a fix.

        So the name is authoritative and the role is tried first, then the
        others. Still no model: this is a fixed list, walked in order.
        """
        if rule.action == "wait":
            # wait_stable returns as soon as the page stops changing, and a screen
            # saying RECORD IN USE BY ANOTHER TERMINAL never started changing — so
            # this used to return instantly and burn all four attempts inside a
            # millisecond while the hold was still held. A condition that clears
            # on its own needs time to actually pass.
            time.sleep(rule.seconds)
            self.surface.wait_stable()
            return self._reissue(rule, bind, at_index)
        if rule.target is None:
            return False

        target = substitute(rule.target, bind)
        name = getattr(target, "name", None)
        attempts = [target]
        if name:
            declared = getattr(target, "role", None)
            attempts += [RoleName(role=role, name=name)
                         for role in ("link", "button", "option")
                         if role != declared]

        for candidate in attempts:
            if self.surface.resolve(candidate).found:
                result = self.surface.click(candidate)
                self.surface.wait_stable()
                return result.ok
        return False

    def _reissue(self, rule, bind: Bindings, at_index: int) -> bool:
        """After waiting, ask for the screen again.

        Waiting alone cannot clear a hold, because the hold is raised by the
        request and the browser is still displaying the answer to it. The flow
        has to re-issue the request that got here — which is a navigation, the
        one action on a legacy app that is safe to repeat.

        Two bounds, and both matter:

          * Only steps from the resumption point up to (not including) the failing
            step are re-run, so this cannot restart a whole flow.
          * If any of them commits something irreversible, nothing is re-run. A
            hold means the action did not take effect, but "means" is an assumption
            about the application, and re-posting a transfer on the strength of an
            assumption is exactly the failure this system exists not to have.
        """
        start = rule.retry_from or self._last_navigation_before(at_index)
        if start is None:
            return False

        window = [s for s in self.cap.steps if start <= s.index < at_index]
        for step in window:
            name = role = None
            if step.target is not None:
                target = getattr(step.target, "target", None)
                name = getattr(target, "name", None) or getattr(target, "anchor", None)
                role = getattr(target, "role", None)
            if self.policy.check_action(step.action, name, role).risk == "irreversible":
                self.log.emit("reissue_refused", at_step=step.index, control=name,
                              why="re-running an irreversible step to clear a hold would "
                                  "risk committing it twice")
                return False

        self.log.emit("reissuing", from_step=start, steps=[s.index for s in window],
                      why=rule.why or "the hold is cleared by asking again")
        for step in window:
            if not self._run_step(step, bind).ok:
                return False
        return True

    def _last_navigation_before(self, index: int | None) -> int | None:
        """The most recent navigation at or before a step — the request to re-issue."""
        candidates = [s.index for s in self.cap.steps
                      if s.action == "navigate" and (index is None or s.index <= index)]
        return candidates[-1] if candidates else None

    def _validate_params(self, params: dict[str, str]) -> None:
        """Reject bad inputs before touching a browser.

        A caller that omits a required parameter or sends one the wrong shape
        should learn that in milliseconds, not after nine form fields.
        """
        for p in self.cap.params:
            if p.required and p.name not in params:
                raise ValueError(f"missing required parameter {p.name!r}")
            if p.name in params and p.pattern and not re.fullmatch(p.pattern, params[p.name]):
                raise ValueError(
                    f"parameter {p.name!r}={params[p.name]!r} does not match {p.pattern}"
                )

    def _run_step(self, step: Step, bind: Bindings) -> StepResult:
        started = time.monotonic()
        # Captured so that a step can be judged on what it did to the page, not
        # merely on whether it raised. "Executed without erroring" and "did what
        # it said it would" are different questions.
        before = self.surface.observe().tree

        def done(ok: bool, detail: str = "", error: str | None = None):
            after = self.surface.observe()
            return StepResult(index=step.index, intent=step.intent, action=step.action, ok=ok,
                              detail=detail, error=error,
                              url=after.url, change=describe_change(before, after.tree),
                              ms=int((time.monotonic() - started) * 1000))

        # the gate applies here exactly as it does in discovery
        if step.action == "navigate":
            url = bind.render_public(step.url) or ""
            full = url if url.startswith("http") else f"{self.cap.app.base_url}/{url.lstrip('/')}"
            verdict = self.policy.check_navigation(full)
            if not verdict.allowed:
                return done(False, error=f"policy: {verdict.why}")
            result = self.surface.navigate(full)
            return done(result.ok, detail=result.detail, error=result.error)

        if step.target is None:
            return done(False, error=f"{step.action} has no target")

        # Let whatever the previous step started actually finish. Replay has no
        # observe() between steps, so without this it resolves against a page
        # that is still assembling itself.
        self.surface.wait_stable()

        target = self._resolve(step.target, bind)
        if target is None:
            return done(False, error=f"the control for {step.intent!r} was not found")

        # Gate on the resolved element, exactly as discovery does.
        found = self.surface.resolve(target)
        verdict = self.policy.check_action(step.action, found.name, found.role)
        if not verdict.allowed:
            return done(False, error=f"policy: {verdict.why}")

        if step.action == "click":
            r = self.surface.click(target)
        elif step.action == "type":
            r = self.surface.type(target, bind.value_of(step))
        elif step.action == "select":
            r = self.surface.select(target, bind.value_of(step))
        elif step.action == "read":
            r = self.surface.read(target)
        else:
            return done(False, error=f"unsupported action {step.action!r}")

        if not r.ok:
            return done(False, error=r.error)

        if step.postcondition and not self._check(step.postcondition, bind):
            return done(False, error=f"postcondition failed after step {step.index}")

        return done(True, detail=r.detail)

    def _resolve(self, locator: Locator, bind: Bindings) -> Target | None:
        """Fill in this invocation's values and point it at the page."""
        target = substitute(locator.target, bind)
        return target if self.surface.resolve(target).found else None

    # -- checks ------------------------------------------------------------

    def _check(self, check: Check, bind: Bindings, obs: Observation | None = None) -> bool:
        obs = obs or self.surface.observe()
        if isinstance(check, TextMatches):
            if not check.pattern:
                return True  # no stable condition was derivable; shape carries it
            pattern = bind.render_public(check.pattern) or ""
            present = pattern.lower() in obs.tree.lower()
            return present == check.present
        if isinstance(check, ElementExists):
            return self.surface.resolve(substitute(check.target, bind)).found == check.present
        if isinstance(check, ValueEquals):
            expected = check.expected if check.expected is not None else bind.params.get(check.from_param or "", "")
            got = self.surface.read(substitute(check.target, bind))
            return bool(got.ok and got.value == expected)
        if isinstance(check, UrlMatches):
            return re.search(check.pattern, obs.url) is not None
        return False

    def _params_echo(self, bind: Bindings, final: Observation) -> bool:
        """Does the final page actually mention what we asked for?

        Only parameters that appeared in the approved template are checked: one
        that never showed on screen cannot be expected to now.
        """
        template = self.cap.success_template or ""
        for name, value in bind.params.items():
            if "{" + name + "}" in template and value not in final.tree:
                return False
        return True

    def _extract_outputs(self, bind: Bindings) -> dict[str, str]:
        out: dict[str, str] = {}
        for spec in self.cap.outputs:
            target = self._resolve(spec.locator, bind)
            if target is None:
                continue
            r = self.surface.read(target)
            if r.ok and r.value is not None:
                out[spec.name] = r.value
        return out

    # -- classification ----------------------------------------------------

    def _classify(self, steps: list[StepResult], failed_at: int | None, final: Observation,
                  bind: Bindings, outputs: dict[str, str], expected: str,
                  observed: str) -> ReplayResult:
        completed = sum(1 for s in steps if s.ok)
        total = len(self.cap.steps)
        shape = shape_match(self.cap.success_template or "", final.tree)

        base: dict[str, Any] = dict(
            capability_id=self.cap.id, capability_version=self.cap.version,
            run_id=self.log.run_id, steps_total=total, steps_completed=completed,
            score=round(completed / total, 3), steps=steps, outputs=outputs,
            shape_match=round(shape, 3), failed_at=failed_at,
        )
        base.pop("failed_at")

        # A declared outcome explains a failure; it can never override a
        # success. Checked first and against the final state, they misclassified
        # completed runs — the harvested detector "the link for this account is
        # absent" is true on every page except the list it came from, so a run
        # that navigated onward to the right answer tripped its own not-found
        # rule while returning correct outputs.
        #
        # `at_step` scopes it further: an outcome detectable part-way through a
        # flow means nothing at a different point in it.
        if failed_at is not None:
            for outcome in self.cap.business_outcomes:
                if outcome.at_step is not None and outcome.at_step != failed_at:
                    continue
                if all(self._check(c, bind, final) for c in outcome.detect):
                    return ReplayResult(outcome="unreachable", subclass=outcome.code,
                                        failed_step=failed_at, expected=expected,
                                        observed=outcome.meaning, **base)

        # What actually decides success: every step ran, the declared success
        # condition holds, the page is about the parameters we passed, and every
        # declared output came back with a value.
        #
        # Whole-page shape is reported but no longer gates. It was measuring the
        # wrong thing at the wrong granularity: an account with no transactions
        # renders without the transaction table at all, 32 structural nodes
        # lighter, and scored 0.76 against a template captured on an account
        # that had some. That is the same page showing different data, and the
        # capability never touched that table. Drift in the controls a
        # capability *does* depend on shows up where it matters — as a locator
        # that no longer resolves.
        echoed = self._params_echo(bind, final)
        outputs_complete = len(outputs) == len(self.cap.outputs) and all(outputs.values())

        # A declared predicate is checked before anything is called a success.
        # Without it, "the read returned something" is the whole test, and a
        # capability that reads the wrong field passes as long as that field
        # was not empty.
        violations = [problem for spec in self.cap.outputs
                      if (problem := spec.check(outputs.get(spec.name, "")))]
        if (failed_at is None and self._check(self.cap.success, bind, final)
                and echoed and outputs_complete and not violations):
            return ReplayResult(outcome="complete", **base)

        if failed_at is None and violations:
            return ReplayResult(
                outcome="incomplete_action", subclass="output_failed_predicate",
                expected="outputs matching their declared shape",
                observed="; ".join(violations), **base)

        if failed_at is None and not outputs_complete:
            missing = [o.name for o in self.cap.outputs if not outputs.get(o.name)]
            return ReplayResult(
                outcome="incomplete_action", subclass="outputs_missing",
                expected=f"values for {[o.name for o in self.cap.outputs]}",
                observed=f"these came back empty or unresolved: {missing}", **base)

        if failed_at is None and not echoed:
            # The page is the right shape but is not about our request — a stale
            # session landing on a different record looks exactly like success
            # until the parameters are checked against what is on screen.
            return ReplayResult(
                outcome="incomplete_action", subclass="wrong_record",
                expected="the page to show the requested values",
                observed="the final page does not echo the parameters supplied", **base)

        if failed_at is None:
            return ReplayResult(
                outcome="incomplete_action", subclass="success_condition_unmet",
                expected="the approved final state",
                observed=f"every step ran but the final state does not match it (shape {shape:.0%})",
                **base)

        # Did we get anywhere at all? A failure on the opening steps means we
        # never arrived; a failure later means we arrived and could not act.
        subclass = _subclass_for(steps, final)
        klass = "unreachable" if completed <= 1 or subclass in _ARRIVAL_FAILURES else "incomplete_action"
        return ReplayResult(outcome=klass, subclass=subclass, failed_step=failed_at,
                            expected=expected, observed=observed, **base)


# Conditions that mean the flow never got where it was going. A policy refusal
# belongs here as a *class* of stop, though it is not a business outcome — the
# caller needs to know the system declined, not that the bank said no. That
# distinction is drawn where the result is built, in catalog.Invocation.
_ARRIVAL_FAILURES = {"auth_required", "policy_refused", "page_error"}


def _subclass_for(steps: list[StepResult], final: Observation) -> str:
    """Name the failure using signals any form application produces.

    Deliberately not a catalogue of this app's error strings: an unfamiliar
    failure produces no signal, falls through to `step_failed`, and is treated
    as the expensive case rather than being guessed at.
    """
    last = next((s for s in reversed(steps) if not s.ok), None)
    error = (last.error or "") if last else ""

    if error.startswith("policy:"):
        return "policy_refused"
    if 'heading "Error!"' in final.tree:
        return "page_error"
    if "Username" in final.tree and "Password" in final.tree:
        return "auth_required"
    if "was not found" in error:
        return "control_not_found"
    if "postcondition failed" in error:
        return "step_did_not_land"
    return "step_failed"
