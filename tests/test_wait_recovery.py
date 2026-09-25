"""A recovery that waits, for conditions with nothing to press.

Every recovery the loop could express was a click, so a screen that offers no
control had no expressible fix. Both held-record tasks in the twenty-task run
scored 0/10 against a baseline that scored 3/3, and the reason was not that the
loop diagnosed it wrongly — it was that the fix could not be written down.

Four things had to be true and none of them were:

  * `Recovery` can say how long to wait.
  * The engine's wait lets time pass. It called wait_stable, which returns as
    soon as the page stops changing, and a screen reading RECORD IN USE never
    started — so four attempts burned inside a millisecond while the hold held.
  * `grounded()` does not demand a control on the page for a wait, since naming
    one is the thing a wait exists to avoid.
  * `apply()` can produce one. It could not: the guard rejects an empty action,
    and action="wait" was only reachable when the action was empty.
"""

from __future__ import annotations

from understudy.artifact.schema import (AppRef, Capability, ElementExists, Recovery,
                                        Step)
from understudy.propose import Diagnosis, Intervention, apply, grounded
from understudy.surface import RoleName


def _capability() -> Capability:
    return Capability(
        id="t.wait", app=AppRef(product="", base_url="http://localhost:8090"),
        goal="read a held record",
        steps=[Step(index=1, intent="open the record", action="navigate", url="/member")],
        success=ElementExists(target=RoleName(role="heading", name="MEMBER RECORD")))


def _held_screen() -> str:
    return ("heading MEMBER INQUIRY\n"
            "text RECORD IN USE BY ANOTHER TERMINAL\n"
            "text The record is held by terminal 11. Retry shortly.")


# -- the proposal ----------------------------------------------------------


def test_a_wait_is_recognised_however_it_is_phrased():
    for action in ("wait", "Wait", "wait for the hold to clear"):
        assert Intervention(kind="add_recovery", detects="RECORD IN USE",
                            action=action).is_wait
    assert not Intervention(kind="add_recovery", detects="NOTICE",
                            action="Acknowledge").is_wait


def test_a_wait_is_not_rejected_for_naming_no_control():
    """The check that keeps the proposer from inventing controls must not reject
    the one proposal whose whole point is that there is no control."""
    waiting = Intervention(kind="add_recovery", detects="RECORD IN USE",
                           action="wait", wait_seconds=3)
    assert grounded(waiting, _held_screen()) is None

    invented = Intervention(kind="add_recovery", detects="RECORD IN USE",
                            action="Release Record", role="button")
    assert "no control named" in (grounded(invented, _held_screen()) or "")


def test_a_detector_the_page_does_not_contain_is_still_rejected():
    """Loosening the control check must not loosen the detector check."""
    wrong = Intervention(kind="add_recovery", detects="QUEUE IS FULL", action="wait")
    assert "does not contain" in (grounded(wrong, _held_screen()) or "")


# -- applying it -----------------------------------------------------------


def test_applying_a_wait_produces_a_waiting_recovery():
    """This is the branch that was unreachable."""
    patched = apply(_capability(),
                    Intervention(kind="add_recovery", detects="RECORD IN USE",
                                 action="wait", wait_seconds=2.5,
                                 why="the hold clears on its own"),
                    Diagnosis(cause="resource_busy", statement="the record is held"))

    assert patched is not None
    rule = patched.recoveries[-1]
    assert rule.action == "wait"
    assert rule.target is None            # nothing to press, by design
    assert rule.seconds == 2.5
    assert patched.version == 2           # a repair is a version, not an edit


def test_a_click_recovery_still_carries_its_control():
    patched = apply(_capability(),
                    Intervention(kind="add_recovery", detects="Scheduled maintenance",
                                 action="Acknowledge", role="link", why="dismiss it"),
                    Diagnosis(cause="unexpected_screen", statement="a notice"))

    rule = patched.recoveries[-1]
    assert rule.action == "click"
    assert rule.target is not None
    assert getattr(rule.target, "name") == "Acknowledge"


def test_the_default_wait_is_used_when_no_duration_is_given():
    patched = apply(_capability(),
                    Intervention(kind="add_recovery", detects="RECORD IN USE",
                                 action="wait", why="it clears"),
                    Diagnosis(cause="resource_busy", statement="held"))
    assert patched.recoveries[-1].seconds == 2.0


def test_a_wait_reads_back_off_disk():
    """It has to survive the artifact being saved and loaded, or a repair that
    works in memory ships a capability that cannot wait."""
    patched = apply(_capability(),
                    Intervention(kind="add_recovery", detects="RECORD IN USE",
                                 action="wait", wait_seconds=4, why="held"),
                    Diagnosis(cause="resource_busy", statement="held"))
    reloaded = Capability.load(patched.model_dump_json())
    assert reloaded.recoveries[-1].action == "wait"
    assert reloaded.recoveries[-1].seconds == 4.0


def test_an_older_artifact_without_seconds_still_loads():
    """Artifacts recorded before waits existed must not fail validation."""
    old = Recovery(code="held", detect="RECORD IN USE", action="wait")
    assert old.seconds == 2.0
