"""The hazards Meridian can be switched into, pinned one at a time.

These are not tests of Understudy. They are tests of the instrument, and they
exist because every claim about what the loop learned rests on the application
having actually done the thing. A hazard that quietly stopped firing turns a
measured result into a measured nothing, and nothing about the run would say so.

Driven over plain HTTP rather than through a browser: what is being pinned is
what the server sends, and a browser would add twenty seconds to say the same.

Requires:  python targets/meridian/app.py
"""

from __future__ import annotations

import http.cookiejar
import re
import time
import urllib.parse
import urllib.request

import pytest

BASE = "http://localhost:8090"


def _text(html: bytes) -> str:
    """The screen as an operator reads it — markup stripped, spacing collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.decode())).strip()


class Terminal:
    """One signed-on session, driven the way the operator's browser would."""

    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        self.get("/content")
        self.post("/signin", {"uid": "tlr01", "pwd": "vault"})

    def get(self, path: str) -> str:
        self.raw = self._opener.open(BASE + path).read().decode()
        return _text(self.raw.encode())

    def post(self, path: str, form: dict) -> str:
        self.raw = self._opener.open(
            BASE + path, urllib.parse.urlencode(form).encode()).read().decode()
        return _text(self.raw.encode())


def _admin(path: str) -> str:
    return urllib.request.urlopen(BASE + path).read().decode()


@pytest.fixture
def clean():
    """Every hazard off, no sessions, no held records — before and after, so one
    test cannot leave the application in a state that decides the next one."""
    _admin("/admin/reset")
    _admin("/admin/interstitial?odds=0")
    yield
    _admin("/admin/reset")
    _admin("/admin/interstitial?odds=0")


@pytest.fixture
def hazard(clean):
    def switch(name: str) -> None:
        assert _admin(f"/admin/hazard?name={name}&on=1") == f"{name} = True"
    return switch


# -- off by default --------------------------------------------------------


def test_every_hazard_is_off_after_reset(clean):
    """The default application is the one the rest of the suite measures against,
    so a hazard left on by an earlier run must not be able to reach it."""
    assert '"lock": false' in _admin("/admin/hazards")
    assert "true" not in _admin("/admin/hazards")


def test_unknown_hazard_is_refused(clean):
    assert "no such hazard" in _admin("/admin/hazard?name=nonsense&on=1")


# -- a held record ---------------------------------------------------------


def test_a_held_record_offers_no_control_to_press(hazard):
    """The distinguishing property: there is nothing on the screen to click, so
    waiting is the only thing that can clear it."""
    hazard("lock")
    terminal = Terminal()

    for _ in range(12):                     # the hold is probabilistic
        screen = terminal.get("/member?no=40021")
        if "RECORD IN USE BY ANOTHER TERMINAL" in screen:
            assert "Acknowledge" not in screen
            assert "alt=\"" not in terminal.raw[terminal.raw.find("RECORD IN USE"):]
            break
    else:
        raise AssertionError("the hold never fired in twelve attempts")


def test_a_held_record_clears_by_waiting_and_can_be_taken_again(hazard):
    """Both halves matter. It has to clear, or no amount of waiting helps; and it
    has to be able to recur, or a flow that meets it once never meets it again —
    which is what made the fault invisible to the repair loop's own sampling."""
    hazard("lock")
    terminal = Terminal()

    freed = held_again = False
    for _ in range(30):
        screen = terminal.get("/member?no=40021")
        if "MEMBER RECORD" in screen:
            freed = True
        elif freed:
            held_again = True
            break
        time.sleep(0.3)

    assert freed, "waiting never cleared the hold"
    assert held_again, "the record could never be held a second time"


# -- two-phase commit ------------------------------------------------------


def test_the_first_submit_posts_nothing_and_returns_the_same_control(hazard):
    """Why this is worse than an error: the screen it returns is the form, with
    the same button on it, so a flow recorded in one pass ends somewhere that
    looks like where it started and reports no failure."""
    hazard("confirm")
    terminal = Terminal()
    transfer = {"memberno": "40055", "amount": "5.00", "direction": "S2C"}

    first = terminal.post("/dotransfer", transfer)
    assert "PRESS POST TRANSFER AGAIN TO CONFIRM" in first
    assert "TRANSFER POSTED" not in first
    # The control's only name is an attribute, so it is invisible to anything
    # reading the screen as text — which is how a flow stops here without
    # noticing it is looking at the form it just submitted.
    assert 'alt="Post Transfer"' in terminal.raw

    assert "TRANSFER POSTED" in terminal.post("/dotransfer", transfer)


# -- the answer is longer than the screen ----------------------------------


def test_posted_items_arrives_two_rows_at_a_time(hazard):
    """A flow that reads the screen once reads part of the answer, and nothing
    about the value it read says it is partial."""
    hazard("paging")
    terminal = Terminal()

    first = terminal.get("/history?no=40021")
    assert "Items shown: 2 of 3" in first
    assert "MORE ITEMS TO FOLLOW" in first

    second = terminal.get("/history?no=40021&from=2")
    assert "Items shown: 1 of 3" in second
    assert "MORE ITEMS TO FOLLOW" not in second


# -- the field keeps part of what it was given -----------------------------


def test_the_post_code_field_silently_keeps_five_characters(hazard):
    """The flow typed the right value and the screen reports a different one.
    That is not the same failure as a broken flow, and a loop that cannot tell
    them apart repairs a flow that was correct."""
    hazard("truncate")
    terminal = Terminal()

    amended = terminal.post("/docontact", {
        "memberno": "40021", "phone": "0114 4960021",
        "email": "member@firstvalley.test", "postcode": "S1 4XYZ99"})

    assert "DETAILS AMENDED" in amended     # it accepted the value
    assert "Post Code S1 4X" in amended     # and kept five characters of it
    assert "S1 4XYZ99" not in amended       # without saying so


# -- a person has to authorise it ------------------------------------------


def test_a_large_transfer_needs_a_supervisor_and_retrying_cannot_help(hazard):
    """The opposite of an interstitial: no control, no wait, and the same answer
    every time. The loop's correct move here is to stop and ask."""
    hazard("stepup")
    terminal = Terminal()
    large = {"memberno": "40021", "amount": "2000.00", "direction": "S2C"}

    for _ in range(3):                       # retrying changes nothing
        screen = terminal.post("/dotransfer", large)
        assert "SUPERVISOR AUTHORISATION REQUIRED" in screen
        assert "TRANSFER POSTED" not in screen

    small = {"memberno": "40021", "amount": "10.00", "direction": "S2C"}
    assert "TRANSFER POSTED" in terminal.post("/dotransfer", small)


# -- the screen takes its time --------------------------------------------


def test_the_posted_items_screen_takes_longer_than_a_default_wait(hazard):
    """1.5s is what our surface waits for a page to settle. A screen slower than
    that settles after we have decided it did."""
    hazard("slow")
    terminal = Terminal()

    started = time.monotonic()
    assert "POSTED ITEMS" in terminal.get("/history?no=40055")
    assert time.monotonic() - started > 1.5


# -- posting is recorded ---------------------------------------------------


def test_posting_twice_leaves_two_rows_in_the_ledger(clean):
    """The reason preconditions matter at all. With no ledger a double submit was
    something to reason about; with one it is something the application records,
    and a run that reports success while leaving two rows can be caught."""
    terminal = Terminal()
    transfer = {"memberno": "40204", "amount": "1.00", "direction": "C2S"}

    assert "NO ITEMS POSTED IN PERIOD" in terminal.get("/history?no=40204")

    terminal.post("/dotransfer", transfer)
    terminal.post("/dotransfer", transfer)

    assert terminal.get("/history?no=40204").count("TFR IN") == 2
