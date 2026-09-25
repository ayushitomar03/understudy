"""Do a task the cheapest way that works, and never a way that is worse.

Three prices, tried cheapest first:

    a recording of this shape ──▶ replay it ──▶ clean? ──▶ done, nothing was spent
            │                          │
        none yet                     failed, and the recording is dropped
            └────────────┬─────────────┘
                         ▼
            the model does it, holding the map
                         │
                         ▼
               recorded, and free from then on

The map is what makes the model attempt cheap; the recording is what makes the
*next* one free. They are different savings and are reported separately.

A plan synthesised from the map without ever running is available as a fourth
price (`use_planner`), and is off by default. It refuses most tasks and its
failures are silent — a route that looks right and is wrong — so it earns its
place only if measured, not by being wired in.

So the success rate is whatever the model achieves, and the map only decides how
much of it was free. That is the honest claim, and it is the reason a planner that
declines is harmless rather than a regression.

One thing this deliberately does not do: grade the answer. Production has no
answer to grade against — that is the whole reason it is asking. Verification here
is what a replay can establish on its own: every step ran, the success condition
holds, the outputs are non-empty, and any pattern the commissioner supplied
matches. Whether "clean" and "correct" actually agree is a separate question, and
one worth measuring rather than assuming — so `verified_clean` is reported
alongside the answer rather than folded into it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from . import cache
from .artifact.schema import Capability
from .evidence import EventLog
from .plan import _recoveries as _map_recoveries, from_map
from .policy import Policy, for_app
from .replay import Replayer
from .replay.result import ReplayResult
from .sitemap import SiteMap
from .surface.web import WebSurface


@dataclass
class Solution:
    """How a task was done, and what it cost."""

    capability: Capability | None
    how: str                      # "cached" | "planned" | "model" | "failed"
    outputs: dict[str, str] = field(default_factory=dict)
    shape: str = ""               # the cache key this task was filed under
    inferences: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    plan_refused: str = ""        # why the map could not cover it, if it could not
    plan_failed: str = ""         # why the plan did not survive replay, if it did not
    note: str = ""

    @property
    def free(self) -> bool:
        return self.how in ("cached", "planned") and self.inferences == 0


def _replay(capability: Capability, params: dict[str, str], secrets: dict[str, str],
            policy: Policy, log: EventLog) -> ReplayResult:
    surface = WebSurface()
    try:
        return Replayer(capability, surface, log, policy).run(params, secrets=secrets)
    finally:
        surface.close()


async def solve(sitemap: SiteMap, goal: str, params: dict[str, str],
                outputs: list[str], *, capability_id: str,
                credentials: dict[str, str] | None = None,
                patterns: dict[str, str] | None = None,
                policy: Policy | None = None,
                model_policy: Policy | None = None,
                max_inferences: int = 40,
                use_cache: bool = True,
                use_map: bool = True,
                use_planner: bool = False,
                model: bool = True,
                log: EventLog | None = None) -> Solution:
    """Replay what is known, else ask the model. Return what happened."""
    secrets = credentials or {}
    app = sitemap.app
    log = log or EventLog("solve", capability_id)
    gate = policy or for_app(app, mode="unattended")

    # -- free: a recording of this shape ------------------------------------
    # Keyed on which application this is, not on where it is answering today: a
    # recording made against one instance is the same recording on the next.
    key = cache.shape(goal, params, sitemap.product or app)
    known = cache.load(key) if use_cache else None
    if known is not None and known.app.base_url != app:
        # The recording is of an application, not of the address it answered on
        # when it was made. Replaying it against a different instance otherwise
        # navigates off the app the policy is scoped to, and every step is
        # refused before it runs.
        known = known.model_copy(deep=True)
        known.app.base_url = app
    if known is not None:
        log.emit("cache_hit", shape=key, version=known.version)
        result = await asyncio.to_thread(_replay, known, params, secrets, gate, log)
        missing = [o.name for o in known.outputs
                   if not (result.outputs or {}).get(o.name)]
        broken = [o.name for o in known.outputs
                  if (result.outputs or {}).get(o.name)
                  and o.check((result.outputs or {})[o.name])]
        if result.outcome == "complete" and not missing and not broken:
            log.emit("solved_from_cache", outputs=result.outputs)
            return Solution(capability=known, how="cached",
                            outputs=result.outputs or {}, shape=key,
                            note="replayed a recording of this task shape")
        # Forget it only when something can record a better one. With no model
        # behind this, dropping a recording destroys the only way the task can be
        # done at all — and the failure may be the application misbehaving rather
        # than the recording being wrong.
        if model:
            cache.forget(key)
        log.emit("cache_kept" if not model else "cache_dropped", shape=key,
                 why=f"{result.outcome}/{result.subclass or '-'}"
                     + (f"; empty {missing}" if missing else "")
                     + (f"; pattern {broken}" if broken else ""))
    elif use_cache:
        log.emit("cache_miss", shape=key)

    # -- free: a plan synthesised from the map, off by default ---------------
    planned, why = (from_map(sitemap, goal, params, outputs, capability_id)
                    if use_planner else (None, "planner off"))
    if planned is None:
        if use_planner:
            log.emit("plan_refused", why=why, goal=goal)
    else:
        log.emit("planned", steps=len(planned.steps), outputs=outputs,
                 recoveries=[r.detect for r in planned.recoveries])
        result = await asyncio.to_thread(_replay, planned, params, secrets, gate, log)

        missing = [o.name for o in planned.outputs
                   if not (result.outputs or {}).get(o.name)]
        broken = [o.name for o in planned.outputs
                  if (result.outputs or {}).get(o.name)
                  and o.check((result.outputs or {})[o.name])]

        if result.outcome == "complete" and not missing and not broken:
            log.emit("solved_without_a_model", outputs=result.outputs)
            return Solution(capability=planned, how="planned",
                            outputs=result.outputs or {}, shape=key, note=why)

        failed = (f"{result.outcome}/{result.subclass or '-'}"
                  + (f"; empty {missing}" if missing else "")
                  + (f"; pattern {broken}" if broken else ""))
        log.emit("plan_did_not_survive_replay", why=failed)
        why = failed

    # -- nothing free worked, and the model is not allowed ------------------
    if not model:
        log.emit("no_model_available", shape=key, why=why)
        return Solution(capability=None, how="failed", shape=key,
                        plan_refused="" if planned is not None else why,
                        plan_failed=why if planned is not None else "",
                        note=f"nothing the system already knows covers this: {why}"[:200])

    # -- the model, holding the map ---------------------------------------
    from .loop.runner import discover           # imported here to avoid a cycle

    capability, model_log = await discover(
        goal=goal, base_url=app, capability_id=capability_id,
        parameters=params, outputs=outputs, output_patterns=patterns or {},
        credentials=secrets, knowledge=sitemap if use_map else None,
        policy=model_policy or for_app(app, mode="discover"),
        max_inferences=max_inferences)

    if capability is not None and use_cache:
        # Only what will still read the right thing for the next set of arguments.
        # A recording anchored to this run's answer costs its replay *and* the
        # model run it was meant to save, which is worse than never caching it.
        fitted, note = cache.fit(_with_recoveries(capability, sitemap), sitemap)
        if fitted is not None:
            cache.save(fitted, key)
            log.emit("cached", shape=key, version=capability.version, note=note)
        else:
            log.emit("not_cached", shape=key, why=note)

    cost = _cost_of(model_log)
    solution = Solution(
        capability=capability,
        how="model" if capability is not None else "failed",
        outputs=_read_values(model_log),
        inferences=cost.get("inferences", 0),
        cost_usd=cost.get("cost_usd") or 0.0,
        shape=key,
        plan_refused="" if planned is not None else why,
        plan_failed=why if planned is not None else "",
        note=f"nothing recorded for this shape yet ({why})"[:200])
    log.emit("solved_with_a_model", inferences=solution.inferences,
             cost_usd=solution.cost_usd, recorded=capability is not None)
    return solution


def _with_recoveries(capability: Capability, sitemap: SiteMap) -> Capability:
    """Attach what the map knows this application interrupts with.

    The model recorded a flow on a quiet run, so the recording knows nothing
    about a notice that only appears seven times in ten. The map does — it
    classified those messages when the application was surveyed — and an
    interruption is a property of the application rather than of this flow, so
    every recording inherits them. Without this a cached recording is dropped
    the first time the app misbehaves, and the shape is re-learned at full
    price on every hazarded run.
    """
    known = {r.detect.strip().lower() for r in capability.recoveries}
    extra = [r for r in _map_recoveries(sitemap)
             if r.detect.strip().lower() not in known]
    if not extra:
        return capability
    carried = capability.model_copy(deep=True)
    carried.recoveries = list(carried.recoveries) + extra
    return carried


def _events(log: EventLog) -> list[dict]:
    import json
    out = []
    for line in (log.dir / "events.jsonl").read_text().splitlines():
        if line.strip().startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _cost_of(log: EventLog) -> dict:
    for event in _events(log):
        if event.get("event") == "model_cost":
            return event
    return {}


def _read_values(log: EventLog) -> dict[str, str]:
    values: dict[str, str] = {}
    for event in _events(log):
        if event.get("event") == "output_declared":
            values[event["name"]] = event.get("value") or ""
    return values
