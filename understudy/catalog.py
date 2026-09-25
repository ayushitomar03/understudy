"""The door an AI agent reaches through.

Everything else in this system exists to produce capabilities. This is what
makes them callable — the layer that turns a directory of saved artifacts into
a set of typed tools an agent can discover by name, read the contract of, and
invoke with arguments.

Without it there is machinery and no way in: a capability could only be run by
a person typing a file path at a terminal, which is not what "the AI agents can
invoke on demand" means.

Two rules the catalog enforces, because an agent is not a careful caller:

  * A capability is only listed as callable if it is safe to run unattended —
    approved, no irreversible step, no step needing a human. Everything else is
    visible in the catalog but refuses to run, with the reason. An agent should
    be able to see that a payment capability exists and be told plainly that it
    needs a person.

  * Arguments are validated against the declared contract before a browser
    opens. A missing or malformed parameter comes back as a typed error in
    milliseconds rather than as a failed run.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, Field

from .artifact.schema import Capability
from .evidence import EventLog
from .policy import for_app
from .replay import Replayer
from .replay.result import ReplayResult
from .surface.web import WebSurface

CAPABILITIES = Path("capabilities")


class Listing(BaseModel):
    """What an agent sees when it asks what this system can do."""

    name: str
    description: str
    app: str
    callable: bool
    reason: str = Field(default="", description="why it cannot be called, when it cannot")
    input_schema: dict
    returns: list[str]
    version: int
    approval: str

    def as_tool(self) -> dict:
        """The capability as a tool definition, ready to hand to a model.

        Capabilities that cannot run unattended are still offered, with the
        reason in their description. Hiding them makes an agent answer "I
        cannot do that" when the truthful answer is "that needs a person" —
        and the second answer is the one that routes the request to a human
        instead of ending it.
        """
        note = "" if self.callable else f" REQUIRES A HUMAN: {self.reason}. Calling it will be refused."
        return {
            "name": self.name.replace(".", "__"),
            "description": (
                f"{self.description} (operates {self.app}; "
                f"returns {', '.join(self.returns) or 'nothing'}).{note}"
            ),
            "input_schema": self.input_schema,
        }


class Invocation(BaseModel):
    """The typed answer an agent gets back. Never prose, never a screenshot."""

    capability: str
    ok: bool
    outcome: str
    outputs: dict[str, str] = Field(default_factory=dict)
    business_outcome: str | None = None
    error: str | None = None
    evidence: str = ""
    seconds: float = 0.0

    def for_model(self) -> str:
        """How this reads when handed back to a calling model."""
        if self.ok:
            return "success: " + json.dumps(self.outputs)
        if self.business_outcome:
            return f"no result: {self.business_outcome} — {self.error}"
        return f"failed: {self.error}"


class Catalog:
    """Saved capabilities, discoverable and callable by name."""

    def __init__(self, directory: Path | str = CAPABILITIES, secrets: dict[str, str] | None = None):
        self.directory = Path(directory)
        self.secrets = secrets or {}
        self._capabilities: dict[str, Capability] = {}
        self.reload()

    def reload(self) -> None:
        self._capabilities.clear()
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                capability = Capability.load(path.read_text())
            except ValueError:
                continue  # a malformed artifact is not a reason to fail the catalog
            # Recompute risk rather than trusting what the artifact recorded.
            # Classification is deterministic, so a stale or missing `risk` —
            # an artifact written before annotation existed, or one edited by
            # hand — must not decide what an agent may call. This also means a
            # tightened policy takes effect on the whole catalog immediately.
            for_app(capability.app.base_url).annotate(capability)

            known = self._capabilities.get(capability.id)
            if known is None or capability.version > known.version:
                self._capabilities[capability.id] = capability

    # -- discovery ---------------------------------------------------------

    def list(self) -> list[Listing]:
        out: list[Listing] = []
        for capability in self._capabilities.values():
            callable_now, reason = self._callable(capability)
            out.append(Listing(
                name=capability.id,
                description=capability.goal,
                app=f"{capability.app.product} at {capability.app.base_url}",
                callable=callable_now,
                reason=reason,
                input_schema=capability.call_schema()["input_schema"],
                returns=[o.name for o in capability.outputs],
                version=capability.version,
                approval=capability.approval,
            ))
        return out

    def tools(self) -> list[dict]:
        """Every capability as a tool — the refusing ones included, so an agent
        can tell a caller that something needs a human rather than that it is
        impossible."""
        return [listing.as_tool() for listing in self.list()]

    def describe(self) -> str:
        """The catalog as text, for a human or for a prompt."""
        lines = []
        for listing in self.list():
            mark = "" if listing.callable else f"   [not callable: {listing.reason}]"
            params = ", ".join(listing.input_schema.get("properties", {}))
            lines.append(f"  {listing.name}  v{listing.version}{mark}")
            lines.append(f"     {listing.description}")
            lines.append(f"     takes: {params or '—'}   returns: {', '.join(listing.returns) or '—'}")
        return "\n".join(lines) or "  (no capabilities saved yet)"

    @staticmethod
    def _callable(capability: Capability) -> tuple[bool, str]:
        if capability.approval != "approved":
            return False, "still a draft — a person has not reviewed it"
        if any(step.requires_human for step in capability.steps):
            return False, "a step needs a human decision"
        if any(step.risk == "irreversible" for step in capability.steps):
            return False, "commits something irreversible; must be invoked with a human present"
        return True, ""

    # -- invocation --------------------------------------------------------

    def invoke(self, name: str, arguments: dict[str, str], *,
               allow_unattended_override: bool = False) -> Invocation:
        """Run a capability by name. This is the production execution path."""
        import time

        name = name.replace("__", ".")
        capability = self._capabilities.get(name)
        if capability is None:
            return Invocation(capability=name, ok=False, outcome="unknown_capability",
                              error=f"no capability named {name!r}. Known: "
                                    f"{sorted(self._capabilities)}")

        callable_now, reason = self._callable(capability)
        if not callable_now and not allow_unattended_override:
            return Invocation(capability=name, ok=False, outcome="refused", error=reason)

        started = time.monotonic()
        log = EventLog("invoke", capability.goal)
        surface = WebSurface()
        try:
            policy = for_app(capability.app.base_url,
                             mode="attended" if allow_unattended_override else "unattended",
                             allow_irreversible=allow_unattended_override)
            result: ReplayResult = Replayer(capability, surface, log, policy).run(
                arguments, secrets=self.secrets)
        except ValueError as exc:
            # A contract violation — a missing or malformed argument. Caught
            # before any of it mattered, and returned as a typed error.
            return Invocation(capability=name, ok=False, outcome="invalid_arguments",
                              error=str(exc), evidence=str(log.dir),
                              seconds=round(time.monotonic() - started, 2))
        finally:
            surface.close()

        return Invocation(
            capability=name,
            ok=result.outcome == "complete",
            outcome="refused" if result.subclass == "policy_refused" else result.outcome,
            outputs=result.outputs,
            # A refusal by our own policy is not the application reporting a
            # business outcome. Conflating them would tell a caller the bank
            # said no when in fact we declined to ask.
            business_outcome=(result.subclass
                              if result.outcome == "unreachable"
                              and result.subclass != "policy_refused" else None),
            error=None if result.outcome == "complete" else (result.observed or result.subclass),
            evidence=str(log.dir),
            seconds=round(time.monotonic() - started, 2),
        )


    async def ainvoke(self, name: str, arguments: dict[str, str], *,
                      allow_unattended_override: bool = False) -> Invocation:
        """Invoke from async code, which is where a calling agent lives.

        The surface is synchronous and Playwright refuses to start inside a
        running event loop, so the whole invocation goes to a worker thread.
        Without this, an agent calling a capability gets a Playwright API error
        rather than a result — the catalog would be unusable by exactly the
        caller it exists for.
        """
        return await asyncio.to_thread(
            self.invoke, name, arguments,
            allow_unattended_override=allow_unattended_override)


def publish(capability: Capability, directory: Path | str = CAPABILITIES,
            approve: bool = False) -> Path:
    """Put a discovered capability into the catalog.

    Approval is a separate, explicit act. A capability arrives as a draft and
    stays uncallable until a person says otherwise — that review is where ground
    truth enters the system, since everything downstream trusts that the
    recorded flow was actually correct when it was recorded.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if approve:
        capability = capability.model_copy(update={"approval": "approved"})
    path = directory / f"{capability.id}.json"
    path.write_text(capability.model_dump_json(indent=2))
    return path
