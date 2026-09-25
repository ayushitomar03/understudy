"""Does Meridian actually hold the answers the held-out set expects?

Worth its own file, for the reason tools/check_ground_truth.py gives about the
trained set: a wrong expected value does not announce itself. It reads exactly
like a hard task, and it is worse here, because the held-out score is the number
the whole claim rests on — a generated set that quietly asks for three
unreachable answers understates the system by three.

Two have already been caught this way, both in tasks written by hand: the audit
count for the restricted member, whose audit trail is reached through a member
record that refuses, and that member's telephone number, for the same reason.

So every expected value is fetched from the screen the task must reach, over an
ordinary signed-on session, and compared the way the grader compares it. No
model, no browser, about a second.

    .venv/bin/python tools/check_holdout.py
"""

from __future__ import annotations

import http.cookiejar
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from ab_twenty import grade                                  # noqa: E402
from tasks_holdout import TASKS, tier                        # noqa: E402

APP = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}

# Which screen answers which output. A task is probed on the screen its answer
# lives on, because reaching it is the thing being confirmed.
ON_SCREEN = {
    "member": ("savings", "checking", "status", "branch", "member_name", "member_no_shown"),
    "contact": ("phone", "email", "post_code", "postcode"),
    "statement": ("opening", "credits", "debits", "closing", "items"),
    "history": ("item_count", "first_amount", "first_date", "first_kind",
                "second_amount", "second_date", "second_kind",
                "third_amount", "third_date", "third_kind"),
    "orders": ("order_count",),
    "audit": ("entry_count",),
    "workitem": ("state", "kind"),
    "message": ("refusal",),
}


class Terminal:
    """An ordinary signed-on session, driven the way the browser drives it."""

    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._open = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)).open
        self.get("/content")
        self.post("/signin", CREDENTIALS)

    def get(self, path: str) -> str:
        return self._read(self._open(APP + path))

    def post(self, path: str, form: dict) -> str:
        return self._read(self._open(APP + path, urllib.parse.urlencode(form).encode()))

    @staticmethod
    def _read(response) -> str:
        raw = response.read().decode()
        # Field values live in attributes, not in text: the contact screen renders
        # every value in an <input>, and a tag-stripped page shows none of them.
        values = " ".join(re.findall(r'value="([^"]*)"', raw))
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw) + " " + values).strip()


def screen_for(task) -> str:
    for screen, outputs in ON_SCREEN.items():
        if any(name in outputs for name in task.outputs):
            return screen
    return "?"


def fetch(terminal: Terminal, task, screen: str) -> str:
    member = task.params.get("member_no", "")
    if screen in ("member", "message"):
        # Through the lookup form, not straight at the record: validation lives on
        # the submit path, so /member?no=ABC answers "no member on file" where the
        # form answers "must be numeric" — and the form is the route a flow takes.
        return terminal.post("/find", {"memberno": member})
    if screen == "contact":
        return terminal.get(f"/contact?no={member}")
    if screen == "history":
        return terminal.get(f"/history?no={member}")
    if screen == "orders":
        return terminal.get(f"/orders?no={member}")
    if screen == "audit":
        return terminal.get(f"/audit?no={member}")
    if screen == "workitem":
        return terminal.get(f"/workitem?id={task.params.get('item_id', '')}")
    if screen == "statement":
        return terminal.post("/dostatement", {"memberno": member,
                                              "period": task.params.get("period", "")})
    return ""


def _states_an_absence(task, page: str) -> bool:
    """Whether the screen says there is nothing, where the task expects zero."""
    return (all(value == "0" for value in task.outputs.values())
            and re.search(r"\bNO [A-Z ]+(POSTED|ENTRIES|ORDERS|MATCH)", page) is not None)


def main() -> int:
    terminal = Terminal()
    unreachable, implied = [], []
    for task in TASKS:
        screen = screen_for(task)
        page = fetch(terminal, task, screen)
        # A count is stated by the application, not rendered as a row, so it is
        # compared the same way the grader compares any other answer.
        ok, why = grade(task, {name: page for name in task.outputs})
        if not ok and _states_an_absence(task, page):
            # "NO ITEMS POSTED IN PERIOD." is the application answering zero. It
            # is a real answer a person acts on, so the task stands — but the
            # count is implied rather than printed, and that is worth saying out
            # loud rather than scoring quietly.
            implied.append(task)
            continue
        if not ok:
            unreachable.append((task, screen, why))

    for task, screen, why in unreachable:
        print(f"  {task.n:>3} [{tier(task)}] on /{screen:<9} {task.goal.format(**task.params)[:52]}")
        print(f"        expected {task.outputs} — {why[:80]}")

    print(f"\n  {len(TASKS) - len(unreachable)}/{len(TASKS)} held-out answers are on the "
          f"screen the task must reach")
    if implied:
        print(f"  {len(implied)} of them as an absence the application states rather than a "
              f"number it prints: {[t.n for t in implied]}")
    if unreachable:
        print(f"  {len(unreachable)} are not, and would be scored as failures the system "
              "did not cause")
    return 1 if unreachable else 0


if __name__ == "__main__":
    raise SystemExit(main())
