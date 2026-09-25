"""What the agent is permitted to do, decided before it does it.

Two jobs, and they are deliberately not the model's:

  * An allowlist of where it may go and what kinds of action it may take. The
    brief asks that the agent *cannot* act outside it, which is why this is a
    gate every action passes through rather than an instruction in a prompt.
    The typed tool vocabulary is what makes that possible — there is no "run
    this code" escape hatch to check.

  * A risk classification of each action. The model knows perfectly well that
    "Send Payment" is irreversible, but a safety property must not depend on
    the thing being constrained choosing to declare itself dangerous. So the
    classification is deterministic here, and the model's opinion may only
    raise a risk level, never lower it.

This module exists because of a real incident rather than a theory: during
development, discovery runs opened three accounts and sent a payment — $325 of
genuine, irreversible transactions — and every step was recorded `risk: safe`,
because nothing ever set it.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .artifact.schema import Risk

# How much supervision the caller has. Discovery needs to be able to complete a
# flow; unattended replay must never commit anything on its own.
Mode = Literal["discover", "attended", "unattended"]

# Control names that commit something. Matched against the accessible name of
# whatever is being clicked, which is the only signal available before the
# click happens — and the one a human operator reads too.
IRREVERSIBLE_NAMES = re.compile(
    r"\b(send|submit|pay|transfer|withdraw|delete|remove|close|cancel|"
    r"confirm|apply|authorize|authorise|approve|open new|initialize|initialise|clean|shutdown)\b",
    re.IGNORECASE,
)

# Names that change state but can be undone by doing the opposite.
REVERSIBLE_NAMES = re.compile(r"\b(update|edit|save|add|create|register|enable|disable)\b", re.IGNORECASE)


class Verdict(BaseModel):
    allowed: bool
    risk: Risk = "safe"
    why: str = ""
    requires_human: bool = False


class Policy(BaseModel):
    """The rules, as data. Loadable from JSON so an operator can change them
    without touching code."""

    mode: Mode = "unattended"

    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:8080", "http://localhost:8081"]
    )
    allowed_paths: list[str] = Field(
        default_factory=lambda: ["/parabank", "/parabank/", "/parabank/*"],
        description=(
            "glob patterns; a path matching none of these is refused. The bare prefix is listed "
            "as well as the wildcard because /parabank does not match /parabank/* — a false "
            "refusal of the application's own entry point."
        ),
    )
    denied_paths: list[str] = Field(
        default_factory=lambda: ["*/admin.htm*", "*/logout.htm*"],
        description=(
            "checked before allowed_paths. ParaBank's admin page can wipe the database and "
            "change the minimum deposit — nothing a capability should ever reach."
        ),
    )
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["navigate", "click", "type", "select", "read", "observe"]
    )

    allow_irreversible: bool = Field(
        default=False,
        description="irreversible actions proceed only when a caller has explicitly opted in",
    )

    # -- the gate ----------------------------------------------------------

    def check_navigation(self, url: str) -> Verdict:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin not in self.allowed_origins:
            return Verdict(allowed=False, why=f"{origin} is not an allowed origin")

        path = parsed.path or "/"
        for pattern in self.denied_paths:
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(url, pattern):
                return Verdict(allowed=False, why=f"{path} is explicitly denied")

        if not any(fnmatch.fnmatch(path, p) for p in self.allowed_paths):
            return Verdict(allowed=False, why=f"{path} is outside the allowed paths")

        return Verdict(allowed=True, risk="safe")

    def check_action(self, action: str, control_name: str | None = None,
                     role: str | None = None) -> Verdict:
        if action not in self.allowed_actions:
            return Verdict(allowed=False, why=f"action {action!r} is not permitted")

        risk = self.classify(action, control_name, role)

        if risk == "irreversible":
            if self.mode == "unattended" or not self.allow_irreversible:
                return Verdict(
                    allowed=False,
                    risk=risk,
                    requires_human=True,
                    why=(
                        f"{control_name or action!r} commits something that cannot be undone. "
                        "Refused without an explicit opt-in; this is the class of action a human "
                        "decides."
                    ),
                )
            return Verdict(allowed=True, risk=risk, requires_human=self.mode != "unattended",
                           why=f"irreversible, permitted because the caller opted in ({self.mode})")

        return Verdict(allowed=True, risk=risk)

    # -- classification ----------------------------------------------------

    @staticmethod
    def classify(action: str, control_name: str | None = None, role: str | None = None) -> Risk:
        """Deterministic, and conservative where it is uncertain.

        Filling a field is safe: typing into a form commits nothing. It is the
        click that submits it that does, so the risk lives in the control being
        clicked — the same cue a human operator reads.

        Role matters as much as name. A *link* called "Bill Pay" navigates to a
        section; a *button* called "Send Payment" moves money. Classifying on
        the name alone blocked the navigation link, which teaches an operator
        that the gate is noisy — and a noisy gate gets switched off.

        Reachability is not this function's problem. Blocking a link does not
        make its destination unreachable, since a URL reaches it directly; that
        is what the allowlist is for. This decides commits.
        """
        if action in ("navigate", "read", "observe", "type", "select"):
            return "safe"
        if action == "click":
            if role == "link":
                return "safe"  # navigation; the allowlist governs where it may go
            name = control_name or ""
            if IRREVERSIBLE_NAMES.search(name):
                return "irreversible"
            if REVERSIBLE_NAMES.search(name):
                return "reversible"
            return "safe"
        return "reversible"

    # -- for the artifact --------------------------------------------------

    def annotate(self, capability) -> None:
        """Stamp the real risk onto a capability's steps.

        Applied after discovery, so a recorded flow carries its own risk profile
        and `unattended_safe` means something. Without this every step defaults
        to safe and a money-moving capability claims it can run unsupervised.
        """
        for step in capability.steps:
            name = role = None
            if step.target:
                primary = step.target.primary
                name = getattr(primary, "name", None) or getattr(primary, "anchor", None)
                role = getattr(primary, "role", None)
            step.risk = self.classify(step.action, name, role)
            if step.risk == "irreversible":
                step.requires_human = True


def for_app(base_url: str, **overrides) -> Policy:
    """A policy scoped to one application, derived from where it lives.

    Hardcoding "/parabank/*" tied the allowlist to a single target. Deriving it
    from the base URL means pointing the system at another app does not quietly
    leave it ungated — the default is still deny-everything-else.
    """
    parsed = urlparse(base_url.rstrip("/"))
    prefix = parsed.path or ""
    paths = [f"{prefix}/*", prefix or "/"] if prefix else ["/*", "/"]
    return Policy(
        allowed_origins=[f"{parsed.scheme}://{parsed.netloc}"],
        allowed_paths=paths,
        **overrides,
    )


def load_policy(path: str | None = None, **overrides) -> Policy:
    if path:
        return Policy.model_validate_json(open(path).read()).model_copy(update=overrides)
    return Policy(**overrides)
