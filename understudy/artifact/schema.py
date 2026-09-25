"""The capability artifact — the only thing that crosses from discovery to replay.

Everything the model learned has to fit through this schema or it does not reach
production. No prompts, no transcripts, no session state: replay reads this and
nothing else, which is what makes it cheap to run and possible to review.

Three shapes here were chosen because of what phase 1 measured on ParaBank
rather than from first principles:

  * A `Locator` records a *strategy* — a role plus an anchor — never a CSS
    selector, so nothing web-specific reaches the artifact. ParaBank's inputs
    carry no accessible name, which is why "the control after this text" is the
    primary strategy rather than a fallback.

  * `Check` has a small, closed vocabulary that the Surface can evaluate. Its
    members exist because the real detectors needed them: ParaBank signals a
    failed lookup with a `heading "Error!"` plus a discriminating paragraph, so
    `element_exists` and `text_matches` together express it exactly.

  * `Param.sensitive` is on the parameter, not on a redaction pass downstream.
    Regulated data has to be un-loggable by construction, not cleaned up after.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from ..surface.base import Target

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------
# locating a control
# --------------------------------------------------------------------------


class Locator(BaseModel):
    """How one control is found, and why that way.

    Deliberately a single strategy rather than an ordered chain of fallbacks.
    A chain was built first and then cut: discovery only ever records the
    strategy that worked, so every chain had exactly one entry and the fallback
    machinery never ran. Claiming a robustness story that has never been
    exercised is worse than not having one.

    If drift handling is wanted later, the honest version is for replay to
    *observe* a locator failing and propose an alternative, which is a different
    mechanism from guessing alternatives up front.
    """

    target: Target
    why: str = Field(description="the model's reasoning for this strategy, in its own words")
    verified: bool = Field(
        default=True, description="this resolved during discovery rather than being a guess"
    )

    @property
    def primary(self) -> Target:
        """The target. Kept as a name from when a locator held several, and
        still used by policy.annotate to classify a step's risk."""
        return self.target


# --------------------------------------------------------------------------
# assertions
# --------------------------------------------------------------------------


class TextMatches(BaseModel):
    """Some text is present on the page. The workhorse: it is how ParaBank's
    'Could not find account # 99999' becomes a declared outcome."""

    kind: Literal["text_matches"] = "text_matches"
    pattern: str
    present: bool = True


class ElementExists(BaseModel):
    kind: Literal["element_exists"] = "element_exists"
    target: Target
    present: bool = True


class ValueEquals(BaseModel):
    """A control holds an expected value — used as a postcondition after typing,
    so a step proves it landed instead of assuming the keystrokes arrived."""

    kind: Literal["value_equals"] = "value_equals"
    target: Target
    expected: str | None = None
    from_param: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> ValueEquals:
        if (self.expected is None) == (self.from_param is None):
            raise ValueError("value_equals needs exactly one of expected or from_param")
        return self


class UrlMatches(BaseModel):
    """Weakest of the checks on this app — ParaBank rewrites paths with a
    session id — so it is here for completeness and rarely the right choice."""

    kind: Literal["url_matches"] = "url_matches"
    pattern: str


Check = Annotated[
    Union[TextMatches, ElementExists, ValueEquals, UrlMatches],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------
# the agent-facing contract
# --------------------------------------------------------------------------

ParamType = Literal["string", "integer", "money", "date", "enum"]


class Param(BaseModel):
    """One input the calling agent supplies per invocation."""

    name: str
    type: ParamType = "string"
    required: bool = True
    pattern: str | None = Field(default=None, description="regex the value must satisfy")
    choices: list[str] | None = None
    sensitive: bool = Field(
        default=False,
        description="never written to evidence, artifacts or logs — enforced at capture, not cleanup",
    )
    example: str | None = None
    description: str | None = None


class Output(BaseModel):
    """One value the capability returns.

    ParaBank renders account data as label/value cell pairs, so an output is a
    locator chain plus a type — the same machinery as any other target, not a
    separate extraction language.
    """

    name: str
    type: ParamType = "string"
    locator: Locator
    at_step: int | None = Field(default=None, description="read here; defaults to the final state")
    pattern: str | None = Field(
        default=None,
        description=(
            "regex the returned value must satisfy. The cheapest thing a commissioner can "
            "supply that generalises: not the answer, which is different for every input, but "
            "the shape of one — a balance is money-shaped for every member. It is what turns "
            "'a value came back' into 'the right kind of value came back' without an oracle."
        ),
    )
    description: str | None = None

    def check(self, value: str) -> str | None:
        """Why this value fails its predicate, or None if it holds."""
        import re

        if not self.pattern:
            return None
        return (None if re.fullmatch(self.pattern, (value or "").strip())
                else f"{self.name}={value!r} does not match {self.pattern}")


class BusinessOutcome(BaseModel):
    """A legitimate non-success the caller needs to be told about.

    The brief calls conflating these with failures the most common design
    mistake in this problem, so they are first-class in the schema: declared up
    front, detected explicitly, and returned as a result rather than raised.
    """

    code: str = Field(description="stable machine-readable code, e.g. account_not_found")
    detect: list[Check] = Field(min_length=1, description="all must hold for this outcome to fire")
    meaning: str = Field(description="what the caller should understand from it")
    at_step: int | None = Field(
        default=None,
        description=(
            "the step this outcome is detectable at. A detector harvested part-way through a "
            "flow is meaningless later in it: 'the link for this account is absent' is true on "
            "every page except the list it was read from, so evaluated against the final state "
            "it fires on success."
        ),
    )


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

ActionKind = Literal["navigate", "click", "type", "select", "read", "assert"]
Risk = Literal["safe", "reversible", "irreversible"]


class Value(BaseModel):
    """What a step types or selects.

    Three sources, and the third is the point: a credential is neither a
    constant nor a caller-supplied parameter. It is looked up by name from
    runtime config at replay, so the artifact records *that a password goes
    here* without ever recording the password. §3.4 asks that secrets never
    reach an artifact; this is how that is made structurally impossible rather
    than merely intended.
    """

    literal: str | None = None
    from_param: str | None = None
    from_secret: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> Value:
        sources = [self.literal, self.from_param, self.from_secret]
        if sum(x is not None for x in sources) != 1:
            raise ValueError("a value needs exactly one of literal, from_param or from_secret")
        return self


class Step(BaseModel):
    """One action in the recorded flow."""

    index: int
    intent: str = Field(description="what this step is for, in plain words, for a human reviewer")
    action: ActionKind
    target: Locator | None = None
    value: Value | None = None
    url: str | None = None

    risk: Risk = Field(
        default="safe",
        description="irreversible steps are refused in unattended replay and escalate instead",
    )
    requires_human: bool = Field(
        default=False,
        description="discovery could not complete this step alone; the capability cannot run unattended",
    )
    postcondition: Check | None = Field(
        default=None, description="proves the step landed instead of assuming the click worked"
    )
    timeout_ms: int = 5_000

    @model_validator(mode="after")
    def shape_matches_action(self) -> Step:
        needs_target = {"click", "type", "select", "read"}
        needs_value = {"type", "select"}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"{self.action} needs a target")
        if self.action in needs_value and self.value is None:
            raise ValueError(f"{self.action} needs a value")
        if self.action == "navigate" and not self.url:
            raise ValueError("navigate needs a url")
        return self


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------


class AppRef(BaseModel):
    product: str
    product_version: str | None = None
    tenant: str | None = None
    base_url: str


class Provenance(BaseModel):
    """Where this came from, so any step can be traced back to the run that
    discovered it."""

    discovered_at: datetime
    model: str
    run_id: str
    evidence_path: str | None = None
    steps_attempted: int | None = Field(
        default=None, description="including dead ends — the ratio to len(steps) is a difficulty signal"
    )


def migrate(raw: dict) -> dict:
    """Bring an artifact written by an older schema up to the current one.

    `schema_version` is only meaningful if something reads it. Without this, a
    capability recorded before locator chains were replaced by a single locator
    simply failed to load — every artifact older than the last schema change was
    dead weight, which is the opposite of what "versioned" is supposed to buy.

    Migrations are one-way and lossy where the old shape carried more: a chain
    of fallbacks collapses to its first verified tier, because that is the one
    discovery actually proved.
    """
    def fix(locator: dict | None) -> dict | None:
        if not locator or "tiers" not in locator:
            return locator
        tiers = locator.get("tiers") or []
        chosen = next((t for t in tiers if t.get("verified")), tiers[0] if tiers else None)
        if chosen is None:
            return None
        return {"target": chosen["target"], "why": chosen.get("why", "migrated from a chain"),
                "verified": chosen.get("verified", True)}

    for step in raw.get("steps", []):
        step["target"] = fix(step.get("target"))
    for output in raw.get("outputs", []):
        output["locator"] = fix(output.get("locator"))
    raw["schema_version"] = SCHEMA_VERSION
    return raw


class Recovery(BaseModel):
    """A known interruption and what to do about it.

    The recoverable class §3.3 asks for, expressed so that replay can act on it
    with no model involved: a condition to look for and a control to act on.
    Both come from a failure that was diagnosed, so a rule only exists because
    the application actually did this once.

    Bounded by construction — one attempt per rule per step. A recovery that
    can retry indefinitely is a flow that spins instead of failing.
    """

    code: str = Field(description="short name, e.g. maintenance_notice")
    detect: str = Field(description="visible text that identifies the interruption")
    action: Literal["click", "wait"] = "click"
    target: Target | None = Field(default=None, description="what to act on, for click")
    retry_from: int | None = Field(
        default=None,
        description=(
            "for action='wait', the step to resume from after waiting. A hold is raised by "
            "the request that asked for the record, and the step that *fails* is a read "
            "further down the flow — so waiting and re-running the read looks at the same "
            "hold screen, which never reloaded. Defaults to the last navigation at or "
            "before the failure, which is the request worth re-issuing."
        ),
    )
    seconds: float = Field(
        default=2.0,
        description=(
            "for action='wait', how long to let pass before trying the step again. A "
            "condition that clears on its own — a record held by another terminal, a "
            "batch still posting — has no control to press, and the only thing that "
            "resolves it is time. Bounded by MAX_RECOVERIES in the engine."
        ),
    )
    why: str = ""
    learned_from: str | None = Field(default=None, description="the run that produced it")


class Revision(BaseModel):
    """Why a version differs from the one before it.

    The artifact is versioned, so every change carries the failure that caused
    it and the change that was made. A reviewer reading v3 can see what v2 got
    wrong without re-running anything.
    """

    version: int
    cause: str
    statement: str
    change: str
    reliability_before: float | None = None
    reliability_after: float | None = None


class Capability(BaseModel):
    """A reusable, reviewable, parameterised flow."""

    schema_version: str = SCHEMA_VERSION
    id: str = Field(description="stable dotted name, e.g. parabank.accounts.read_balance")
    version: int = 1
    approval: Literal["draft", "approved"] = Field(
        default="draft", description="unattended replay is gated on approval"
    )

    app: AppRef
    goal: str
    description: str | None = None

    planned_from_map: bool = Field(
        default=False,
        description=(
            "built from the application map with no model involved. Worth recording on "
            "the artifact rather than only in a log: a reviewer reading this should know "
            "whether a model chose these steps or whether they were composed from "
            "locators the mapper had already verified, because the two have different "
            "ways of being wrong."
        ),
    )
    params: list[Param] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Check
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    revisions: list[Revision] = Field(default_factory=list)

    success_template: str | None = Field(
        default=None,
        description=(
            "the accessibility tree of the final state at discovery, with param and output "
            "values masked. Replay compares shape against this — it is the only record of "
            "what 'right' looked like when the goal was genuinely reached."
        ),
    )

    provenance: Provenance | None = None

    # -- invariants --------------------------------------------------------

    @model_validator(mode="after")
    def references_resolve(self) -> Capability:
        """Every param reference names a declared param, and steps are ordered.

        Caught here rather than at replay time: an artifact that references a
        parameter nobody supplies is broken whether or not anyone runs it.
        """
        declared = {p.name for p in self.params}

        for step in self.steps:
            if step.value and step.value.from_param and step.value.from_param not in declared:
                raise ValueError(f"step {step.index} uses undeclared param {step.value.from_param!r}")
            if isinstance(step.postcondition, ValueEquals):
                ref = step.postcondition.from_param
                if ref and ref not in declared:
                    raise ValueError(f"step {step.index} postcondition uses undeclared param {ref!r}")

        if [s.index for s in self.steps] != sorted(s.index for s in self.steps):
            raise ValueError("steps must be in ascending index order")

        indices = {s.index for s in self.steps}
        for out in self.outputs:
            if out.at_step is not None and out.at_step not in indices:
                raise ValueError(f"output {out.name!r} reads at step {out.at_step}, which does not exist")

        return self

    # -- the agent-facing view --------------------------------------------

    @property
    def unattended_safe(self) -> bool:
        """Whether an agent may invoke this without a human standing by."""
        return (
            self.approval == "approved"
            and not any(s.requires_human for s in self.steps)
            and not any(s.risk == "irreversible" for s in self.steps)
        )

    @classmethod
    def load(cls, text: str) -> Capability:
        """Parse an artifact, migrating it forward if it was written earlier."""
        import json

        raw = json.loads(text)
        # Decide from the parsed structure, not by scanning the document for a
        # word: a locator's `why` is free prose written by the model, so any
        # capability whose reasoning happened to mention tiers was migrated for
        # no reason.
        needs = raw.get("schema_version") != SCHEMA_VERSION or any(
            isinstance(step.get("target"), dict) and "tiers" in step["target"]
            for step in raw.get("steps", []))
        return cls.model_validate(migrate(raw) if needs else raw)

    def call_schema(self) -> dict:
        """JSON Schema for the arguments, so the capability can be published as
        a callable tool an agent discovers by name."""
        props, required = {}, []
        for p in self.params:
            spec: dict = {"type": "integer" if p.type == "integer" else "string"}
            if p.description:
                spec["description"] = p.description
            if p.pattern:
                spec["pattern"] = p.pattern
            if p.choices:
                spec["enum"] = p.choices
            if p.example:
                spec["examples"] = [p.example]
            props[p.name] = spec
            if p.required:
                required.append(p.name)
        return {
            "name": self.id,
            "description": self.goal,
            "input_schema": {"type": "object", "properties": props, "required": required},
        }


# --------------------------------------------------------------------------
# multi-tenant (§3.7)
# --------------------------------------------------------------------------


class Overlay(BaseModel):
    """Per-tenant specialisation of a capability recorded elsewhere.

    The reuse story in one object: hundreds of tenants run the same vendor
    product, so the flow is recorded once against a base app and a tenant
    supplies only what differs. Overriding a *target* is the common case —
    ParaBank's open-account form anchors on a sentence containing "$100.00",
    which a tenant with a different minimum would not match.
    """

    capability_id: str
    capability_version: int
    tenant: str
    base_url: str | None = None
    step_targets: dict[int, Locator] = Field(
        default_factory=dict, description="step index -> replacement locator chain"
    )
    note: str | None = None

    def apply(self, cap: Capability) -> Capability:
        if cap.id != self.capability_id:
            raise ValueError(f"overlay is for {self.capability_id!r}, not {cap.id!r}")
        out = cap.model_copy(deep=True)
        if self.base_url:
            old = out.app.base_url.rstrip("/")
            out.app = out.app.model_copy(update={"base_url": self.base_url, "tenant": self.tenant})
            # Rewrite any absolute URL still pointing at the tenant this was
            # recorded on. Without this the flow navigates straight back to the
            # instance it came from, and the policy for the new tenant refuses
            # it at step one — which is how this was found.
            for step in out.steps:
                if step.url and step.url.startswith(old):
                    step.url = step.url[len(old):].lstrip("/") or "/"
        for step in out.steps:
            if replacement := self.step_targets.get(step.index):
                step.target = replacement
        return out
