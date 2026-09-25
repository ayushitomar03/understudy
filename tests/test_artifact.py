"""Artifact schema and its invariants. No browser needed.

The properties here are the ones that would be expensive to discover later: a
capability that references a parameter nobody supplies, a locator chain built
only from guesses, or — the one that actually happened — a credential reaching
the artifact through a field nobody thought to check.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from understudy.artifact.schema import (
    AppRef,
    Capability,
    ElementExists,
    Locator,
    Output,
    Overlay,
    Param,
    Step,
    TextMatches,
    Value,
)
from understudy.evidence import Redactor
from understudy.surface.base import AfterText, RoleName


def chain(target, verified: bool = True) -> Locator:
    return Locator(target=target, why="verified in discovery", verified=verified)


def capability(**overrides) -> Capability:
    base = dict(
        id="parabank.accounts.read_balance",
        app=AppRef(product="ParaBank", base_url="http://localhost:8080/parabank"),
        goal="Look up an account and read its balance",
        params=[Param(name="account_id", example="13344")],
        outputs=[Output(name="balance", type="money",
                        locator=chain(AfterText(anchor="Balance:", role="cell")))],
        steps=[
            Step(index=1, intent="Open the account", action="click",
                 target=chain(RoleName(role="link", name="{account_id}"))),
            Step(index=2, intent="Read the balance", action="read",
                 target=chain(AfterText(anchor="Balance:", role="cell"))),
        ],
        success=ElementExists(target=RoleName(role="heading", name="Account Details")),
    )
    base.update(overrides)
    return Capability(**base)


# -- the contract ----------------------------------------------------------


def test_round_trips_through_json():
    cap = capability()
    assert Capability.model_validate_json(cap.model_dump_json()) == cap


def test_call_schema_describes_the_parameters():
    schema = capability().call_schema()
    assert schema["name"] == "parabank.accounts.read_balance"
    assert schema["input_schema"]["required"] == ["account_id"]


def test_undeclared_param_reference_is_rejected():
    """Caught at build time: an artifact referencing a parameter nobody supplies
    is broken whether or not anyone runs it."""
    with pytest.raises(ValidationError, match="undeclared param"):
        capability(steps=[
            Step(index=1, intent="type it", action="type",
                 target=chain(AfterText(anchor="Account", role="textbox")),
                 value=Value(from_param="nonexistent")),
        ])


def test_value_needs_exactly_one_source():
    with pytest.raises(ValidationError):
        Value(literal="x", from_secret="password")
    with pytest.raises(ValidationError):
        Value()


# -- unattended safety -----------------------------------------------------


def test_draft_capabilities_are_not_unattended_safe():
    assert capability().unattended_safe is False


def test_approved_and_safe_is_unattended_safe():
    assert capability(approval="approved").unattended_safe is True


def test_irreversible_step_blocks_unattended_replay():
    cap = capability(approval="approved")
    cap.steps[0].risk = "irreversible"
    assert cap.unattended_safe is False


def test_step_needing_a_human_blocks_unattended_replay():
    cap = capability(approval="approved")
    cap.steps[0].requires_human = True
    assert cap.unattended_safe is False


# -- §3.4: secrets must not be expressible in an artifact ------------------


def test_credentials_are_referenced_not_stored():
    step = Step(index=1, intent="Enter the password", action="type",
                target=chain(AfterText(anchor="Password", role="textbox")),
                value=Value(from_secret="password"))
    blob = step.model_dump_json()
    assert "from_secret" in blob
    assert "demo" not in blob  # the value itself has nowhere to live


def test_redactor_catches_secrets_in_model_prose():
    """The real leak: the model wrote 'logged in as john/demo' into its own
    summary, which no field-specific rule would have anticipated."""
    r = Redactor()
    r.protect("demo")
    cleaned = r.scrub("Logged into ParaBank as john/demo, then opened the account")
    assert "demo" not in cleaned
    assert "<redacted>" in cleaned


def test_redactor_catches_undeclared_sensitive_shapes():
    r = Redactor()
    assert "123-45-6789" not in r.scrub("SSN 123-45-6789 on file")
    assert "4111111111111111" not in r.scrub("card 4111111111111111 ending")


# -- §3.7: one flow, many tenants -----------------------------------------


def test_overlay_specialises_a_target_without_rerecording():
    cap = capability()
    overlay = Overlay(
        capability_id=cap.id, capability_version=cap.version, tenant="second-valley",
        base_url="http://localhost:8081/parabank",
        step_targets={2: chain(AfterText(anchor="Current Balance:", role="cell"))},
        note="this tenant labels the field differently",
    )
    specialised = overlay.apply(cap)

    assert specialised.app.base_url.endswith(":8081/parabank")
    assert specialised.app.tenant == "second-valley"
    assert specialised.steps[1].target.target.anchor == "Current Balance:"
    # the original is untouched, so one recording serves both tenants
    assert cap.steps[1].target.target.anchor == "Balance:"


def test_overlay_refuses_a_different_capability():
    overlay = Overlay(capability_id="something.else", capability_version=1, tenant="t")
    with pytest.raises(ValueError, match="overlay is for"):
        overlay.apply(capability())


# -- regressions found by running the loop against the real app ------------


def test_string_zero_index_does_not_become_index_zero():
    """The model sends ordinals as strings. `int(raw or 1)` looks correct and
    is not — "0" is truthy, survives the fallback, and becomes XPath [0], which
    matches nothing. Observed live: two silent misses and the model abandoned
    accessibility targeting for CSS selectors."""
    from understudy.loop.tools import _index

    assert _index("0") == 1
    assert _index(0) == 1
    assert _index(None) == 1
    assert _index("") == 1
    assert _index("2") == 2
    assert _index(-5) == 1


def test_redactor_leaves_the_word_password_in_prose():
    """The candidate list the model reads to choose a target contains the word
    'Password'. A pattern matching the bare word redacted whatever followed it,
    corrupting the information the loop depends on."""
    r = Redactor()
    text = 'textbox after "Password" matched nothing — button "Log In"'
    assert r.scrub(text) == text


def test_redactor_still_catches_real_assignments():
    r = Redactor()
    assert "hunter2" not in r.scrub("password=hunter2")
    assert "abc123" not in r.scrub("api_key: abc123")


def test_boolean_answers_are_not_parameter_names():
    """`value_is_parameter` read like a flag, so the model answered it like one
    and created parameters called true and false."""
    from understudy.loop.runner import _param_name

    assert _param_name("true") == ""
    assert _param_name("False") == ""
    assert _param_name("") == ""
    assert _param_name("account_id") == "account_id"
