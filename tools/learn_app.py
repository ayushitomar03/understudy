"""Give the loop a set of tasks on one application and let it work through them.

The claim being tested: that a loop which remembers what it learned about an
application gets cheaper at it. Measured on turns, because turns are model calls
and model calls are the whole cost of discovery.

Every task here is independent — none needs another's output — but all of them
share an application, and therefore share the way into it. Nothing tells the
loop that signing on is four steps; if it learns that, it learns it by noticing
that every task it has finished began the same way.

    .venv/bin/python tools/learn_app.py            # run the set
    .venv/bin/python tools/learn_app.py --forget   # start with no knowledge
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from understudy import knowledge as knowledge_store
from understudy.orchestrator import orchestrate

APP = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}

MONEY = r"[\d,]+\.\d{2}"

# Four tasks on the same application. They share an entry sequence and nothing
# else; the order is the order a person would commission them in.
TASKS = [
    {
        "id": "meridian.members.balances",
        "goal": "Sign on, then look up the given member and read their savings balance "
                "and checking balance",
        "params": {"member_no": "40021"},
        "outputs": {"savings_balance": MONEY, "checking_balance": MONEY},
        "expect": {"savings_balance": "4,210.55", "checking_balance": "812.03"},
    },
    {
        "id": "meridian.members.identity",
        "goal": "Sign on, then look up the given member and read their name and their "
                "account status",
        "params": {"member_no": "40055"},
        "outputs": {"member_name": r"[A-Z]\. [A-Z]+", "status": r"[A-Z]+"},
        "expect": {"member_name": "M. OYELARAN", "status": "ACTIVE"},
    },
    {
        "id": "meridian.members.branch",
        "goal": "Sign on, then look up the given member and read which branch holds "
                "their account",
        "params": {"member_no": "40204"},
        "outputs": {"branch": r"[A-Z]+"},
        "expect": {"branch": "MAIN"},
    },
    {
        "id": "meridian.members.savings_only",
        "goal": "Sign on, then look up the given member and read only their savings balance",
        "params": {"member_no": "40021"},
        "outputs": {"savings_balance": MONEY},
        "expect": {"savings_balance": "4,210.55"},
    },
]


async def pass_over(label: str, learn: bool) -> list[dict]:
    """Run the whole set once. With `learn` off, every task starts from nothing,
    which is the control — the same tasks at the same difficulty, differing only
    in whether the loop is allowed to remember."""
    store = knowledge_store.STORE / f"{knowledge_store._slug(APP)}.json"
    if not learn and store.exists():
        store.unlink()

    print(f"\n{'=' * 58}\n{label}\n{'=' * 58}")
    results = []
    for n, task in enumerate(TASKS, 1):
        known = knowledge_store.load(APP)
        print(f"[{n}/{len(TASKS)}] {task['id']}")
        print(f"        knows: {known.summary()}")
        if not learn:
            store.unlink(missing_ok=True)   # control: forget between tasks too

        state = await orchestrate(
            goal=task["goal"],
            capability_id=task["id"],
            base_url=APP,
            params=task["params"],
            credentials=CREDENTIALS,
            expect=task["expect"],
            outputs=list(task["outputs"]),
            output_patterns=task["outputs"],
            # Exactly one attempt per task. With more, attempt 2 resumes from
            # attempt 1's champion and its turn count collapses — which is
            # within-orchestration resumption, not cross-task memory, and
            # measuring one as the other would make the result meaningless.
            min_attempts=1, max_attempts=1, max_turns=25, learn=learn,
        )

        best = max(state.attempts, key=lambda a: a.rank) if state.attempts else None
        turns = best.turns if best else 0
        results.append({"task": task["id"], "turns": turns,
                        "steps": best.steps if best else None,
                        "score": best.score if best else 0.0,
                        "attempts": len(state.attempts)})
        print(f"        {turns} turns, {best.steps if best else '-'} steps, "
              f"score {best.score if best else 0:.2f}\n")

    return results


async def main(forget: bool = False) -> int:
    cold = await pass_over("WITHOUT MEMORY — every task starts from nothing", learn=False)
    warm = await pass_over("WITH MEMORY — what it learns carries forward", learn=True)

    after = knowledge_store.load(APP)
    print(f"\n{'=' * 64}")
    print(f"{'task':<32}{'cold':>8}{'warm':>8}{'saved':>9}")
    print("-" * 64)
    for c, w in zip(cold, warm):
        saved = c["turns"] - w["turns"]
        print(f"{c['task'][:30]:<32}{c['turns']:>8}{w['turns']:>8}"
              f"{(f'{saved:+d}' if saved else '0'):>9}")
    print("-" * 64)
    ct, wt = sum(c["turns"] for c in cold), sum(w["turns"] for w in warm)
    print(f"{'total turns':<32}{ct:>8}{wt:>8}{wt - ct:>+9}")
    print(f"\nlearned: {after.summary()}")
    if after.entry:
        print(f"entry prefix: {after.entry.describe()}")
        print(f"  confirmed by: {', '.join(after.entry.confirmed_by)}")

    Path("evidence/knowledge").mkdir(parents=True, exist_ok=True)
    Path("evidence/knowledge/ab.json").write_text(
        json.dumps({"cold": cold, "warm": warm}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--forget" in sys.argv)))
