"""The bench the hundred-task benchmark runs on: a task, a grader, and an app each.

Everything here was extracted from the twenty-task harness this project started
with, which grew arms of its own and a dependency on a repair loop that is no
longer part of the system. What is left is the part tools/arms.py actually uses,
and nothing else: what a task is, how an answer is graded against what Meridian
actually holds, and how to stand up one application instance per worker.

The grader is carried over unchanged, deliberately. Every number in REPORT.md was
produced by it, and rewriting it would quietly make those numbers incomparable.
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field

from understudy.policy import for_app

APP = "http://localhost:8090"
CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}
BASE_PORT = 8100


@dataclass
class Task:
    n: int
    goal: str
    outputs: dict[str, str]                      # name -> expected value
    params: dict[str, str] = field(default_factory=dict)
    hazards: dict[str, float | bool] = field(default_factory=dict)
    patterns: dict[str, str] = field(default_factory=dict)
    mutates: bool = False
    note: str = ""

    def policy(self, mode: str):
        """The gate this task runs under.

        A flow that posts a transfer is refused by default and asks for a person,
        which is the property worth having and which the first run of this harness
        demonstrated by escalating three times. Measuring those flows at all
        therefore needs an opt-in, so the opt-in is explicit, scoped to the four
        tasks that move money, and recorded in the result: fake money, on a fake
        bank, on localhost, for a run the operator asked for.

        Note what this does *not* do: it does not touch `classify`, so the action
        is still recognised as irreversible and still recorded as such. The opt-in
        changes who may authorise it, not what it is.
        """
        return for_app(APP, mode=mode, allow_irreversible=self.mutates)

    @property
    def id(self) -> str:
        return f"ab{self.n:02d}"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def grade(task: Task, got: dict[str, str]) -> tuple[bool, str]:
    """Is this the answer Meridian holds? Separate from whether the run finished.

    The answer counts if it appears in what was read as a value in its own right,
    rather than only when the read isolated it exactly. This is not leniency for
    its own sake: on a screen like

        POSTED ITEMS  Member 40204  NO ITEMS POSTED IN PERIOD.

    there is no cell containing only the message, and on the amended-details screen
    the telephone number only exists inside a sentence. Demanding an exact match
    marked a dozen correct answers wrong, which is a measurement of the grader.

    The bound that keeps it from being meaningless: the expected value must not be
    flanked by digits, letters or decimal punctuation, so "0.00" does not match
    inside "10.00" and "1" does not match inside "128.00".
    """
    for name, expected in task.outputs.items():
        actual = _norm(got.get(name, ""))
        if not actual:
            return False, f"{name} empty"
        if (pattern := task.patterns.get(name)):
            if not re.search(pattern, actual):
                return False, f"{name}={actual!r} does not match {pattern}"
            continue
        want = _norm(expected)
        if want == actual:
            continue
        if not re.search(r"(?<![\w.,])" + re.escape(want) + r"(?![\w.,])", actual):
            return False, f"{name}={actual!r} does not contain {want!r}"
    return True, "correct"

def start_apps(n: int) -> list[str]:
    """One Meridian per worker. Independent memory means independent hazards."""
    procs, urls = [], []
    for i in range(n):
        port = BASE_PORT + i
        subprocess.Popen([".venv/bin/python", "targets/meridian/app.py", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        urls.append(f"http://localhost:{port}")
    for url in urls:                          # wait for each to answer
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/admin/hazards", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise SystemExit(f"{url} never came up")
    print(f"  {n} independent Meridian instances on {BASE_PORT}-{BASE_PORT + n - 1}\n")
    return urls


def configure(task: Task, app: str) -> None:
    urllib.request.urlopen(app + "/admin/reset").read()
    for name, value in task.hazards.items():
        if name == "interstitial":
            urllib.request.urlopen(f"{app}/admin/interstitial?odds={value}").read()
        else:
            urllib.request.urlopen(f"{app}/admin/hazard?name={name}&on=1").read()


def gate(task: Task, app: str, mode: str):
    return for_app(app, mode=mode, allow_irreversible=task.mutates)


def gate(task: Task, app: str, mode: str):
    return for_app(app, mode=mode, allow_irreversible=task.mutates)
