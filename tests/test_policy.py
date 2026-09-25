"""The gate every action passes through.

These exist because of a real incident rather than a hypothesis: development
discovery runs opened three accounts and sent a payment — $325 of irreversible
transactions — with every step recorded `risk: safe`, because nothing set it.
"""

from __future__ import annotations

import pytest

from understudy.policy import Policy


@pytest.fixture
def policy() -> Policy:
    return Policy(mode="discover")


# -- where it may go -------------------------------------------------------


def test_foreign_origin_is_refused(policy):
    assert not policy.check_navigation("http://evil.example/steal").allowed


def test_admin_page_is_denied_even_though_it_is_in_the_app(policy):
    """ParaBank's admin page can wipe the database and change the minimum
    deposit. In scope for the app, never in scope for a capability."""
    verdict = policy.check_navigation("http://localhost:8080/parabank/admin.htm")
    assert not verdict.allowed
    assert "denied" in verdict.why


def test_the_entry_point_is_allowed(policy):
    """Regression: /parabank does not match the glob /parabank/*, so the
    application's own front door was refused."""
    assert policy.check_navigation("http://localhost:8080/parabank").allowed
    assert policy.check_navigation("http://localhost:8080/parabank/overview.htm").allowed


# -- what it may commit ----------------------------------------------------


@pytest.mark.parametrize("name", ["Send Payment", "Transfer", "Open New Account", "Withdraw"])
def test_committing_buttons_are_irreversible(policy, name):
    verdict = policy.check_action("click", name, role="button")
    assert verdict.risk == "irreversible"
    assert not verdict.allowed
    assert verdict.requires_human


@pytest.mark.parametrize("name", ["Bill Pay", "Transfer Funds", "Accounts Overview"])
def test_navigation_links_are_not_commits(policy, name):
    """A link called 'Bill Pay' navigates; a button called 'Send Payment' moves
    money. Classifying on the name alone blocked the link — and an operator who
    sees a gate cry wolf turns the gate off."""
    verdict = policy.check_action("click", name, role="link")
    assert verdict.risk == "safe"
    assert verdict.allowed


def test_filling_a_field_commits_nothing(policy):
    assert policy.check_action("type", "Amount: $").risk == "safe"
    assert policy.check_action("select", "From account #:").risk == "safe"


def test_opt_in_permits_an_irreversible_action():
    permissive = Policy(mode="discover", allow_irreversible=True)
    assert permissive.check_action("click", "Send Payment", role="button").allowed


def test_unattended_refuses_irreversible_even_with_opt_in():
    """Unattended means nobody is there to decide, so the opt-in cannot apply."""
    p = Policy(mode="unattended", allow_irreversible=True)
    assert not p.check_action("click", "Send Payment", role="button").allowed


def test_disallowed_action_kind_is_refused():
    p = Policy(mode="discover", allowed_actions=["navigate", "read", "observe"])
    assert not p.check_action("click", "anything").allowed


# -- stamping the artifact -------------------------------------------------


def test_annotate_marks_the_committing_step(policy):
    from tests.test_artifact import capability, chain
    from understudy.artifact.schema import Step
    from understudy.surface.base import RoleName

    cap = capability(approval="approved")
    cap.steps.append(Step(index=3, intent="Send it", action="click",
                          target=chain(RoleName(role="button", name="Send Payment"))))
    assert cap.unattended_safe is True          # before: a payment claims to be safe

    policy.annotate(cap)

    assert cap.steps[-1].risk == "irreversible"
    assert cap.steps[-1].requires_human
    assert cap.unattended_safe is False


# -- a locator that encodes its own answer ---------------------------------


def test_self_referential_output_locator_is_detected():
    """Seen live: the model located the balance cell by searching for "$10.45",
    the value it was reading. That resolves for the discovery input and for
    nothing else — the capability silently works for exactly one account."""
    value, by_value, by_label = "$10.45", "$10.45", "Balance:"

    assert value in by_value        # caught
    assert value not in by_label    # the label anchor is stable across inputs


# -- output predicates -----------------------------------------------------


def test_output_predicate_rejects_the_wrong_kind_of_value():
    """The cheapest verification a commissioner can supply: not the answer,
    which differs per input, but the shape of one. This caught a capability
    that read the account number where a balance belonged."""
    from understudy.artifact.schema import Locator, Output
    from understudy.surface.base import AfterText

    balance = Output(name="balance", pattern=r"-?\$[\d,]+\.\d{2}",
                     locator=Locator(target=AfterText(anchor="Balance:", role="cell"),
                                     why="verified"))
    assert balance.check("-$2,300.00") is None
    assert balance.check("12345") is not None
    assert balance.check("") is not None


def test_output_without_a_predicate_accepts_anything():
    """Predicates are optional. A capability nobody has supplied one for must
    still run, and must not be silently held to an invented standard."""
    from understudy.artifact.schema import Locator, Output
    from understudy.surface.base import AfterText

    anything = Output(name="x", locator=Locator(target=AfterText(anchor="X", role="cell"),
                                                why="verified"))
    assert anything.check("whatever") is None


def test_read_predicate_check_needs_no_locator():
    """Regression: the discovery-time check was written to build an Output in
    order to call check(), using a variable that was not assigned until later.
    Every read raised, and the agent escalated with 'read_value is throwing an
    internal error on every call'. A predicate is a property of the value."""
    import re

    assert re.fullmatch(r"[\d,]+\.\d{2}", "4,210.55")
    assert not re.fullmatch(r"[\d,]+\.\d{2}", "R. ACHTERBERG")
