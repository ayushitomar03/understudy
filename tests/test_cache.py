"""The cache keeps what generalises and declines what does not."""

from __future__ import annotations

import pytest

from understudy import cache
from understudy.artifact.schema import (AppRef, Capability, ElementExists, Locator,
                                        Output, Param, Step)
from understudy.surface.base import AfterText, RoleName


def _capability(locator: Locator, name: str = "savings") -> Capability:
    return Capability(
        id="t", app=AppRef(product="", base_url="http://localhost:8090"),
        goal="look up member 40021 and report the savings balance",
        params=[Param(name="member_no", example="40021")],
        steps=[Step(index=1, intent="read it", action="read", target=locator)],
        outputs=[Output(name=name, locator=locator)],
        success=ElementExists(target=RoleName(role="heading", name="")))


def _label(anchor: str) -> Locator:
    return Locator(target=AfterText(anchor=anchor, role="cell", index=1), why="test", verified=True)


def _content(name: str) -> Locator:
    return Locator(target=RoleName(role="row", name=name), why="test", verified=True)


# -- the key ---------------------------------------------------------------


def test_the_same_task_with_different_arguments_is_one_shape():
    a = cache.shape("look up member 40021 and report the savings balance",
                    {"member_no": "40021"})
    b = cache.shape("look up member 40055 and report the savings balance",
                    {"member_no": "40055"})
    assert a == b


def test_a_different_task_is_a_different_shape():
    a = cache.shape("look up member 40021 and report the savings balance",
                    {"member_no": "40021"})
    b = cache.shape("look up member 40021 and report the checking balance",
                    {"member_no": "40021"})
    assert a != b


def test_a_short_argument_does_not_mask_inside_a_longer_one():
    key = cache.shape("transfer 4.00 for member 40021", {"amount": "4", "member_no": "40021"})
    assert "{member_no}" in key.replace("-", "") or "member" in key
    assert "0021" not in key


# -- what may be kept ------------------------------------------------------


def test_a_label_anchored_read_is_kept():
    fitted, why = cache.fit(_capability(_label("Savings Bal.")))
    assert fitted is not None, why


def test_a_read_anchored_to_this_run_s_answer_is_declined():
    fitted, why = cache.fit(_capability(_content("Date Type Amount 11/09 DEPOSIT 128.00")))
    assert fitted is None
    assert "anchored to data" in why


def test_a_read_anchored_to_an_answer_the_app_gives_is_declined(sitemap):
    fitted, why = cache.fit(_capability(_content("NO MEMBER ON FILE FOR {member_no}"),
                                        name="refusal"), sitemap)
    assert fitted is None
    assert "anchored to an answer" in why


def test_the_map_repairs_a_read_it_has_a_label_for(sitemap):
    fitted, why = cache.fit(_capability(_content("4,210.55 SAVINGS BALANCE SHOWN")), sitemap)
    assert fitted is not None
    assert cache._text(fitted.outputs[0].locator) == "Savings Bal."
    assert cache._text(fitted.steps[0].target) == "Savings Bal."


# -- the store -------------------------------------------------------------


def test_a_recording_round_trips(tmp_path):
    capability = _capability(_label("Savings Bal."))
    cache.save(capability, "k", root=tmp_path)
    assert cache.load("k", root=tmp_path).id == capability.id
    assert cache.forget("k", root=tmp_path)
    assert cache.load("k", root=tmp_path) is None


@pytest.fixture
def sitemap():
    from understudy.sitemap import load
    m = load("http://localhost:8090")
    if m is None:
        pytest.skip("no map on disk")
    return m


# -- a column header is not a field label ----------------------------------


def test_a_column_header_is_not_a_field_label(sitemap):
    """`Amount` names the transfer form's textbox and the posted-items column.

    Reading the cell after the column header returns row one, which is how a
    replay reported 14/09 as the *oldest* posted item and called it a success.
    """
    header = Locator(target=AfterText(anchor="Amount", role="cell", index=1),
                     why="test", verified=True)
    fitted, why = cache.fit(_capability(header, name="oldest_date"), sitemap)
    assert fitted is None
    assert "never recorded as a field label" in why


def test_the_same_label_as_the_survey_recorded_it_is_kept(sitemap):
    field = Locator(target=AfterText(anchor="Telephone", role="textbox", index=1),
                    why="test", verified=True)
    fitted, why = cache.fit(_capability(field, name="phone"), sitemap)
    assert fitted is not None, why


def test_a_value_that_satisfies_its_pattern_is_not_called_broken():
    """`Output.check` returns the reason it FAILED, or None when it holds.

    Read the other way round, every correct value looks broken — which is how a
    replay that read '4,210.55' for the savings balance was thrown away and the
    no-model arm scored 0/100.
    """
    from understudy.artifact.schema import Output
    money = Output(name="savings", locator=_label("Savings Bal."), pattern=r"^[\d,.]+$")
    assert money.check("4,210.55") is None
    assert money.check("DORMANT") is not None
