"""Discovery has to stop on its own: when it runs too long, when nothing it does
changes the page, and when it keeps coming back to the same screens. Each stop
hands the run to a person rather than letting the model spend its whole budget."""

from understudy.discovery.ledger import Attempt, Ledger
from understudy.discovery.runner import stopping_condition


def attempt(verdict="advanced", before="a", after="b", action="click"):
    return Attempt(turn=0, action=action, verdict=verdict,
                   digest_before=before, digest_after=after)


def check(ledger, since=0, elapsed_s=0.0, timeout_s=900.0):
    return stopping_condition(ledger, since=since, elapsed_s=elapsed_s, timeout_s=timeout_s)


def test_a_run_making_progress_carries_on():
    ledger = Ledger(attempts=[attempt(before=str(i), after=str(i + 1)) for i in range(10)])
    assert check(ledger) is None


def test_it_stops_when_time_runs_out():
    assert "limit 900s" in check(Ledger(), elapsed_s=901)


def test_it_stops_after_four_attempts_that_changed_nothing():
    ledger = Ledger(attempts=[attempt()] + [attempt("no_op", "b", "b")] * 3)
    assert check(ledger) is None
    ledger.attempts.append(attempt("unresolved", "b", "b"))
    assert "4 attempts in a row" in check(ledger)


def test_it_stops_when_the_page_keeps_returning_to_the_same_screens():
    bounce = [attempt(before="x", after="y"), attempt(before="y", after="x")] * 3
    assert "screens already seen" in check(Ledger(attempts=bounce))


def test_reading_many_values_off_one_screen_is_not_a_cycle():
    """A read leaves the page where it was. Six of them in a row is an agent
    collecting the answer, and stopping it there would fail a correct run."""
    reads = [attempt(action="read_value", before="s", after="s") for _ in range(8)]
    assert check(Ledger(attempts=reads)) is None


def test_attempts_before_a_human_handed_back_do_not_count():
    """After a handoff the page is whatever the person left it as, so the dead
    ends from before describe a screen that no longer exists."""
    ledger = Ledger(attempts=[attempt("no_op", "b", "b")] * 4)
    assert check(ledger) is not None
    assert check(ledger, since=4) is None
