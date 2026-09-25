"""Reading tables and messages: what the planner may promise, and what it may not.

Every case here is a task that was answered wrongly before the rule it tests
existed. They are kept as tests rather than as notes because each wrong answer
replayed cleanly and reported success — the failure mode this whole layer is
built to prevent.
"""

from __future__ import annotations

import pytest

from understudy.plan import _headline, _selects, _unexplained, from_map
from understudy.sitemap import load
from understudy.surface.base import AnyOf, InTable, RowCount


@pytest.fixture
def sitemap():
    m = load("http://localhost:8090")
    if m is None or not any(s.tables for s in m.screens):
        pytest.skip("no map with tables on disk (run tools/map_tables.py)")
    return m


def plan(sitemap, goal, outputs, params=None):
    return from_map(sitemap, goal, params or {"member_no": "40021"}, outputs, "t")


# -- what the goal is asking for -------------------------------------------


def test_a_position_is_read_as_a_row():
    assert _selects("report the amount of the third posted item").row == 3
    assert _selects("report the last posted item").row == -1


def test_a_count_is_read_as_a_count():
    assert _selects("report how many items are posted").count is True
    assert _selects("report the number of standing orders").count is True


def test_a_plain_field_read_is_neither():
    assert _selects("look up member 40021 and report the savings balance") is None


# -- rows -------------------------------------------------------------------


def test_a_positional_row_is_planned(sitemap):
    capability, why = plan(sitemap, "Report the amount of the third posted item for "
                                    "member 40021", ["third_amount"])
    assert capability is not None, why
    assert isinstance(capability.outputs[0].locator.target, InTable)
    assert capability.outputs[0].locator.target.row == 3


def test_an_order_the_map_does_not_know_is_refused(sitemap):
    """The map records where a table is, not how it is sorted."""
    capability, why = plan(sitemap, "Report the date of the oldest posted item for "
                                    "member 40021", ["oldest_date"])
    assert capability is None
    assert "how the table is sorted" in why


# -- counts -----------------------------------------------------------------


def test_a_count_uses_the_application_s_own_line(sitemap):
    capability, why = plan(sitemap, "Report how many items are posted for member 40021",
                           ["item_count"])
    assert capability is not None, why
    target = capability.outputs[0].locator.target
    assert isinstance(target, RowCount) and target.summary == "Items shown"


def test_a_count_the_application_does_not_separate_is_refused(sitemap):
    """`Entries shown: 4` is not the number of INQUIRY entries, and answering
    it with 4 was wrong by two."""
    capability, why = plan(sitemap, "Report how many INQUIRY entries are in the audit "
                                    "trail for member 40021", ["inquiry_count"])
    assert capability is None
    assert "inquiry" in why


def test_what_a_count_does_not_explain_is_what_was_added(sitemap):
    screen = next(s for s in sitemap.screens if s.id == "audit_trail")
    left = _unexplained("how many INQUIRY entries are in the audit trail for member 40021",
                        screen, "Entries shown", {"member_no": "40021"})
    assert left == {"inquiry"}


# -- messages ---------------------------------------------------------------


def test_an_answer_that_is_a_message_is_planned(sitemap):
    capability, why = plan(sitemap, "Attempt to look up member 40999 and report exactly "
                                    "what the application says", ["refusal"],
                           {"member_no": "40999"})
    assert capability is not None, why
    assert isinstance(capability.outputs[0].locator.target, AnyOf)


def test_a_message_is_matched_by_the_words_the_screen_leads_with():
    assert _headline("NOT AUTHORISED — This record is restricted.") == "NOT AUTHORISED"
    assert _headline("NO MEMBER ON FILE") == "NO MEMBER ON FILE"
