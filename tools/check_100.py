"""Are all one hundred tasks actually answerable?

Expected values are imported from the application's fixtures, so a task can no
longer disagree with the app about what the answer *is*. What importing cannot
tell us is whether the answer is *reachable* — and that is the failure that cost
us twice: a notice set to fire every time never clears, so the record behind it is
unreachable, and a transfer drew on an account holding nothing. Both scored zero in
both arms and read exactly like hard tasks.

So each check here asks the question importing cannot: can this be done at all?

    .venv/bin/python tools/check_100.py
"""

from __future__ import annotations

import http.cookiejar
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from ab_twenty import _norm
from tasks100 import MEMBERS, ORDERS, STATEMENTS, TASKS, WORK, tier

APP = "http://localhost:8090"
SOURCE = Path("targets/meridian/app.py").read_text()

problems: list[str] = []


def fail(task, why: str) -> None:
    problems.append(f"  {task.n:>3} [{tier(task)}] {why}\n        {task.goal[:78]}")


# -- 1. a refusal the application cannot produce -----------------------------
# Every refusal we expect has to be a string the application actually emits. A
# typo here produces a task no arm can ever pass.
def check_refusals() -> None:
    for task in TASKS:
        if tier(task) != "T3R":
            continue
        expected = list(task.outputs.values())[0]
        # Several messages are built by concatenation — "NO STATEMENT FOR PERIOD " +
        # period — so the whole string is never a literal anywhere. Walk back from
        # the end until a prefix of it does appear, and only complain if even the
        # opening words do not.
        words = expected.split()
        for cut in range(len(words), 1, -1):
            if " ".join(words[:cut]).upper() in SOURCE.upper():
                break
        else:
            fail(task, f"the application never emits anything starting "
                       f"{' '.join(words[:2])!r}")


# -- 2. a transfer larger than the account holds -----------------------------
def check_transfers() -> None:
    for task in TASKS:
        goal, params = task.goal.lower(), task.params
        if "transfer" not in goal or "amount" not in params:
            continue
        member = params.get("member_no")
        if member not in MEMBERS:
            continue
        source = "savings" if "savings to checking" in goal else "checking"
        available = float(MEMBERS[member][source].replace(",", ""))
        wanted = float(params["amount"].replace(",", ""))
        expects_refusal = "INSUFFICIENT" in str(task.outputs.values()).upper()
        if wanted > available and not expects_refusal:
            fail(task, f"{wanted:,.2f} out of {source} which holds {available:,.2f}")
        if wanted <= available and expects_refusal:
            fail(task, f"expects INSUFFICIENT but {source} holds {available:,.2f}")


# -- 3. a reference or item that does not exist ------------------------------
def check_references() -> None:
    known_orders = {o["ref"] for orders in ORDERS.values() for o in orders}
    known_items = {w["id"] for w in WORK}
    for task in TASKS:
        ref = task.params.get("ref")
        expects_refusal = "NO SUCH" in str(task.outputs.values()).upper()
        if ref and (ref in known_orders) == expects_refusal:
            fail(task, f"order {ref!r} " +
                 ("exists but a refusal is expected" if expects_refusal
                  else "does not exist"))
        item = task.params.get("item_id")
        if item and item not in known_items:
            fail(task, f"work item {item!r} does not exist")
        period = task.params.get("period")
        member = task.params.get("member_no")
        if period and member:
            have = (member, period) in STATEMENTS
            wants_refusal = "NO STATEMENT" in str(task.outputs.values()).upper()
            if have == wants_refusal:
                fail(task, f"statement ({member}, {period}) " +
                     ("exists but a refusal is expected" if wants_refusal else "is missing"))


# -- 4. a hazard that makes the screen unreachable ---------------------------
def check_hazards() -> None:
    for task in TASKS:
        odds = task.hazards.get("interstitial")
        if odds is not None and odds >= 1.0:
            fail(task, "a notice at 100% never clears — acknowledging returns to the "
                       "same request, so the screen behind it is unreachable")


# -- 5. a workflow entered in the wrong state -------------------------------
def check_workflow() -> None:
    by_id = {w["id"]: w for w in WORK}
    for task in TASKS:
        item = task.params.get("item_id")
        if not item or item not in by_id:
            continue
        state = by_id[item]["state"]
        goal = task.goal.lower()
        if "claim" in goal and state != "OPEN":
            expects_refusal = tier(task) == "T3R"
            if not expects_refusal:
                fail(task, f"claiming {item} but it starts {state}, and only OPEN can be "
                           "claimed")
        if "without claiming" in goal and state != "OPEN":
            fail(task, f"{item} starts {state}, so the refusal under test would not fire")


# -- 6. the live application agrees, on the ones worth paying for -----------
def check_live() -> None:
    """A handful of end-to-end confirmations, because the static checks above all
    reason about the fixtures rather than about what the server does with them."""
    jar = http.cookiejar.CookieJar()
    o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def text(raw: bytes) -> str:
        return _norm(re.sub(r"<[^>]+>", " ", raw.decode()))

    urllib.request.urlopen(APP + "/admin/reset").read()
    o.open(APP + "/content").read()
    o.open(APP + "/signin",
           urllib.parse.urlencode({"uid": "tlr01", "pwd": "vault"}).encode()).read()

    # the three shapes that are computed rather than copied
    seen = text(o.open(APP + "/orders?no=40021").read())
    want = str(sum(1 for x in ORDERS["40021"] if x["status"] == "ACTIVE"))
    if f"Active orders: {want}" not in seen:
        problems.append(f"  live: standing orders does not report 'Active orders: {want}'")

    seen = text(o.open(APP + "/audit?no=40021&kind=INQUIRY").read())
    if "Entries shown: 2" not in seen:
        problems.append("  live: the audit filter does not narrow to 2 INQUIRY entries")

    seen = text(o.open(APP + "/worklist").read())
    want = str(sum(1 for w in WORK if w["state"] == "OPEN"))
    if f"Items in OPEN: {want}" not in seen:
        problems.append(f"  live: work queue does not report 'Items in OPEN: {want}'")

    urllib.request.urlopen(APP + "/admin/reset").read()


def main() -> int:
    for check in (check_refusals, check_transfers, check_references, check_hazards,
                  check_workflow, check_live):
        check()

    print(f"  {len(TASKS)} tasks checked")
    if problems:
        print(f"\n  {len(problems)} problem(s):\n")
        print("\n".join(problems))
        return 1
    print("  every expected answer is producible by the application")
    return 0


if __name__ == "__main__":
    sys.exit(main())
