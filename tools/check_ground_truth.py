"""Does Meridian actually hold the answers tools/ab_twenty.py expects?

Worth its own file. A wrong expected value does not announce itself: it turns up
as a task both arms fail, which reads exactly like a hard task. Task 18 asked for
a transfer out of an account holding nothing, and the only reason it was caught
is that the agent escalated on insufficient funds and said so — a quieter task
would have scored 0/3 twice and been written up as a finding.

So every expected answer is confirmed against the running application before any
model is paid to look for it.

    .venv/bin/python tools/check_ground_truth.py
"""

from __future__ import annotations

import http.cookiejar
import re
import sys
import urllib.parse
import time
import urllib.request

from ab_twenty import TASKS, APP, configure

def _text(raw: bytes) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.decode())).strip()


class Terminal:
    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.get("/content")
        self.post("/signin", {"uid": "tlr01", "pwd": "vault"})

    def get(self, path: str) -> str:
        self.raw = self._o.open(APP + path).read().decode()
        return _text(self.raw.encode())

    def post(self, path: str, form: dict) -> str:
        return _text(self._o.open(APP + path,
                                  urllib.parse.urlencode(form).encode()).read())


# How to reach each task's answer by hand, and what the screen must contain.
# Written out per task rather than derived, because deriving it from the same
# assumptions that produced the expected value would confirm nothing.
def probe(task, t: Terminal) -> tuple[bool, str]:
    p = task.params
    member = p.get("member_no", "")
    want = list(task.outputs.values())[0]

    if task.n in (1, 2, 3, 4, 5, 6, 7, 8):
        for _ in range(8):
            screen = t.get(f"/member?no={member}")
            if "RECORD IN USE" in screen:
                time.sleep(2.2)               # the hold expires; waiting clears it
                continue
            if "NOTICE" in screen:            # acknowledging returns to the same request
                continue
            break
        return want in screen, screen[:110]

    if task.n in (9, 10, 11):
        # These values live in `value=` attributes, so they are invisible to
        # anything reading the screen as text. Our surface reads them with
        # input_value(); this check has to look at the same place.
        t.get(f"/contact?no={member}")
        return want in t.raw, t.raw[t.raw.find("Telephone"):][:110]

    if task.n in (12, 15):                     # how many items are posted
        screen = t.get(f"/history?no={member}")
        return f"of {want}" in screen or f"Items shown: {want} of {want}" in screen, screen[:110]

    if task.n == 13:                           # the newest item
        return want in t.get(f"/history?no={member}"), t.get(f"/history?no={member}")[:110]

    if task.n == 14:                           # the oldest item, behind Next
        first = t.get(f"/history?no={member}")
        last = t.get(f"/history?no={member}&from=2")
        return want in last and want not in first, f"page2: {last[:90]}"

    if task.n == 16:
        return want in t.get(f"/history?no={member}"), t.get(f"/history?no={member}")[:110]

    if task.n in (17, 18):
        screen = t.post("/dotransfer", {"memberno": member, "amount": p["amount"],
                                        "direction": "S2C"})
        if "AGAIN TO CONFIRM" in screen:       # two-phase commit
            screen = t.post("/dotransfer", {"memberno": member, "amount": p["amount"],
                                            "direction": "S2C"})
        if task.n == 17:
            return bool(re.search(r"8841-\d{4}", screen)), screen[:110]
        after = t.get(f"/member?no={member}")
        return want in after, f"posted: {screen[:60]} | record: {after[:70]}"

    if task.n == 19:
        return want in t.post("/docontact", {
            "memberno": member, "phone": p["phone"],
            "email": f"member{member}@firstvalley.test", "postcode": "S1 021"}), ""

    if task.n == 20:
        screen = t.post("/docontact", {
            "memberno": member, "phone": "0114 496 0055",
            "email": f"member{member}@firstvalley.test", "postcode": p["post_code"]})
        return f"Post Code {want}" in screen, screen[:110]

    return False, "no probe written"


def main() -> int:
    bad = []
    for task in TASKS:
        configure(task)                        # this task's hazards, opening data
        ok, seen = probe(task, Terminal())
        want = list(task.outputs.values())[0] or list(task.patterns.values())[0]
        print(f"  {task.n:>2}. {'ok  ' if ok else 'WRONG'}  want {want!r}")
        if not ok:
            bad.append(task.n)
            print(f"       screen: {seen}")
    urllib.request.urlopen(APP + "/admin/reset").read()

    print(f"\n{len(TASKS) - len(bad)}/{len(TASKS)} expected answers confirmed against the app")
    if bad:
        print(f"wrong: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
