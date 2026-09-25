"""Understudy command line.

    python cli.py discover "look up account 12345 and read its balance"
    python cli.py dashboard
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from understudy.dashboard import serve
from understudy.policy import Policy, for_app
from understudy.artifact.schema import Capability, Overlay
from understudy.evidence import EventLog
from understudy.loop.runner import discover
from understudy.catalog import Catalog, publish
from understudy.judge import judge
from understudy.repair import repair
from understudy.orchestrator import orchestrate
from understudy.replay import Replayer
from understudy.surface.web import WebSurface

# MERIDIAN CORE 4.2 — the frameset app in targets/meridian. It is the primary
# target because it is the only one with the traits the brief names first:
# frames, labels that are markup rather than semantics, image-only controls,
# and a session that expires mid-flow.
BASE_URL = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}


def main() -> int:
    parser = argparse.ArgumentParser(prog="understudy")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="run one autonomous discovery")
    d.add_argument("goal")
    d.add_argument("--id", default="parabank.discovered", help="capability id to save as")
    d.add_argument("--base-url", default=BASE_URL)
    d.add_argument("--model", default="claude-sonnet-5")
    d.add_argument("--max-turns", type=int, default=40)
    d.add_argument("--headed", action="store_true", help="show the browser")
    d.add_argument("--param", action="append", default=[], metavar="NAME=VALUE",
                   help="an input the capability takes; the value is the example for this run")
    d.add_argument("--output", action="append", default=[], metavar="NAME[=REGEX]",
                   help="an output the capability must return; add =REGEX to declare its shape")
    d.add_argument("--credential", action="append", default=[], metavar="NAME=VALUE",
                   help="a login credential; never written to the artifact or the evidence")
    d.add_argument("--allow-irreversible", action="store_true",
                   help="permit actions that commit something (payments, transfers, new accounts)")

    r = sub.add_parser("replay", help="run a saved capability deterministically")
    r.add_argument("capability", help="path to capability.json")
    r.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--allow-irreversible", action="store_true")
    r.add_argument("--mode", default="unattended", choices=["unattended", "attended"])
    r.add_argument("--credential", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--judge", action="store_true", help="score the run against the rubric")
    r.add_argument("--tenant", metavar="NAME=BASE_URL",
                   help="run this capability against a different instance of the same product")

    o = sub.add_parser("orchestrate", help="keep attempting a goal until it replays perfectly")
    o.add_argument("goal")
    o.add_argument("--id", required=True)
    o.add_argument("--base-url", default=BASE_URL)
    o.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    o.add_argument("--credential", action="append", default=[], metavar="NAME=VALUE")
    o.add_argument("--expect", action="append", default=[], metavar="OUTPUT=VALUE",
                   help="the correct value for an output; makes wrong answers score lower")
    o.add_argument("--min-attempts", type=int, default=5)
    o.add_argument("--max-attempts", type=int, default=8)
    o.add_argument("--max-turns", type=int, default=30)
    o.add_argument("--samples", type=int, default=1,
                   help="replays per attempt; >1 measures reliability on a flaky app")

    pub = sub.add_parser("publish", help="put a discovered capability into the catalog")
    pub.add_argument("capability")
    pub.add_argument("--approve", action="store_true",
                     help="mark it reviewed, so an agent may call it unattended")

    sub.add_parser("catalog", help="what an agent can call")

    rep = sub.add_parser("repair", help="run it; if it fails, diagnose, change, and retry")
    rep.add_argument("capability")
    rep.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    rep.add_argument("--credential", action="append", default=[], metavar="NAME=VALUE")
    rep.add_argument("--samples", type=int, default=5, help="replays per measurement")
    rep.add_argument("--rounds", type=int, default=4)

    inv = sub.add_parser("invoke", help="call a capability by name, as an agent would")
    inv.add_argument("name")
    inv.add_argument("--arg", action="append", default=[], metavar="NAME=VALUE")
    inv.add_argument("--credential", action="append", default=[], metavar="NAME=VALUE")
    inv.add_argument("--with-human", action="store_true",
                     help="a person is present, so irreversible capabilities may run")

    s = sub.add_parser("dashboard", help="watch a run live")
    s.add_argument("run_id", nargs="?")
    s.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()

    if args.command == "dashboard":
        serve(args.run_id, port=args.port)
        return 0

    if args.command == "orchestrate":
        def show(state, record):
            right = "" if record.outputs_correct is None else (
                " outputs ok" if record.outputs_correct else " WRONG OUTPUT")
            mark = "PROMOTED" if record.promoted else record.note
            rate = f"{(record.replay_score or 0)*100:.0f}% reliable"
            print(f"  attempt {record.n}: {record.turns:>2} turns, {rate}, "
                  f"{record.steps or '-'} steps,{right} score {record.score:.2f}  [{mark}]",
                  flush=True)

        print(f"goal: {args.goal}\nrunning at least {args.min_attempts} attempts\n", flush=True)
        state = asyncio.run(orchestrate(
            goal=args.goal, capability_id=args.id, base_url=args.base_url,
            params=dict(p.split("=", 1) for p in args.param),
            credentials=dict(c.split("=", 1) for c in args.credential) or CREDENTIALS,
            expect=dict(e.split("=", 1) for e in args.expect),
            min_attempts=args.min_attempts, max_attempts=args.max_attempts,
            max_turns=args.max_turns, samples=args.samples, on_update=show))
        print(f"\n{state.table()}\n\nchampion: {state.champion_path} "
              f"(score {state.champion_score:.2f})")
        return 0 if state.champion_score > 0 else 1

    if args.command == "publish":
        cap = Capability.load(Path(args.capability).read_text())
        path = publish(cap, approve=args.approve)
        state = "approved — agents may call it" if args.approve else "draft — not callable yet"
        print(f"published {cap.id} v{cap.version} -> {path}  ({state})")
        return 0

    if args.command == "repair":
        cap = Capability.load(Path(args.capability).read_text())
        fixed, record = asyncio.run(repair(
            cap,
            dict(p.split("=", 1) for p in args.param),
            dict(c.split("=", 1) for c in args.credential) or CREDENTIALS,
            samples=args.samples, rounds=args.rounds,
            on_round=lambda r, after: print(
                f"\n  ROUND {r.n}  ·  v{r.version} measured {r.passed}/{r.of} = {r.reliability:.0%}"
                f"\n    diagnosis : {r.cause} — {r.statement}"
                f"\n    change    : {r.change}"
                f"\n    result    : {r.reliability:.0%} -> {after:.0%}  "
                f"{'KEPT' if r.kept else 'rolled back'}", flush=True)))
        print(f"\n{record.table()}")
        out = Path(args.capability)
        if fixed.version > cap.version:
            out = out.with_name(f"{fixed.id}.v{fixed.version}.json")
            out.write_text(fixed.model_dump_json(indent=2))
            print(f"\nrepaired to v{fixed.version} -> {out}")
        if record.escalated:
            print(f"\nneeds a person: {record.escalated}")
        return 0 if record.final_reliability >= 0.9 else 1

    if args.command == "catalog":
        print(Catalog().describe())
        return 0

    if args.command == "invoke":
        catalog = Catalog(secrets=dict(c.split("=", 1) for c in args.credential) or CREDENTIALS)
        result = catalog.invoke(args.name, dict(a.split("=", 1) for a in args.arg),
                                allow_unattended_override=args.with_human)
        print(json.dumps(result.model_dump(), indent=2))
        return 0 if result.ok else 1

    if args.command == "replay":
        cap = Capability.load(Path(args.capability).read_text())
        if args.tenant:
            name, base = args.tenant.split("=", 1)
            cap = Overlay(capability_id=cap.id, capability_version=cap.version,
                          tenant=name, base_url=base).apply(cap)
            print(f"applying {name} overlay: {base}")
        params = dict(p.split("=", 1) for p in args.param)
        log = EventLog("replay", cap.goal)
        surface = WebSurface(headless=not args.headed)
        policy = for_app(cap.app.base_url, mode=args.mode,
                         allow_irreversible=args.allow_irreversible)
        try:
            secrets = dict(c.split("=", 1) for c in args.credential) or CREDENTIALS
            result = Replayer(cap, surface, log, policy).run(params, secrets=secrets)
        finally:
            surface.close()
        print(f"\n{result.summary()}\n\nevidence: {log.dir}")
        if args.judge:
            verdict = asyncio.run(judge(cap, result, params, log=log))
            print(f"\njudged by {verdict.judged_by}\n{verdict.table()}")
        return 0 if result.ok else 1

    parameters = dict(p.split("=", 1) for p in args.param)
    credentials = dict(c.split("=", 1) for c in args.credential) or CREDENTIALS

    cap, log = asyncio.run(
        discover(
            goal=args.goal,
            parameters=parameters,
            outputs=[o.split("=", 1)[0] for o in args.output] or None,
            output_patterns=dict(o.split("=", 1) for o in args.output if "=" in o),
            base_url=args.base_url,
            capability_id=args.id,
            credentials=credentials,
            policy=for_app(args.base_url, mode="discover",
                           allow_irreversible=args.allow_irreversible),
            headless=not args.headed,
            model=args.model,
            max_turns=args.max_turns,
        )
    )

    print(f"\nevidence: {log.dir}")
    if cap:
        print(f"capability: {log.dir / 'capability.json'}")
        print(f"  steps   : {len(cap.steps)}")
        print(f"  params  : {[p.name for p in cap.params] or '—'}")
        print(f"  outputs : {[o.name for o in cap.outputs] or '—'}")
        return 0

    print("no capability produced — see the evidence for why")
    return 1


if __name__ == "__main__":
    sys.exit(main())
