"""Surface tests against MERIDIAN CORE 4.2, the frameset app.

Integration tests by choice. Every property worth protecting here — that a
frameset is perceivable at all, that a label four tables deep inside a <b>
resolves, that anything we describe to the model is reachable by what we
described — is a property of this code meeting a hostile surface. A mocked page
would assert that our mock behaves the way our code expects, which is worth
nothing.

Meridian rather than a polite third-party demo because it has the traits the
brief names first and the polite ones do not: separate documents, labels that
are markup rather than semantics, controls that are images, and a session that
expires mid-flow.

Requires:  python targets/meridian/app.py
"""

from __future__ import annotations

import pytest

from understudy.surface import (AfterText, Css, Observation, Ordinal, RoleName,
                                WebSurface)

BASE = "http://localhost:8090"
OPERATOR, PASSCODE = "tlr01", "vault"
MEMBER, RESTRICTED = "40021", "40113"

# The same page with and without a per-request token in the tree. Kept as text
# so the digest can be tested without a second browser — Playwright's sync API
# allows one instance per thread.
COLD_TREE = '''- link "Main Menu":
  - /url: /menu;jsessionid=E0AED80611E3B7F2A1
- textbox
- button "Sign On"
'''

WARM_TREE = '''- link "Main Menu":
  - /url: /menu
- textbox
- button "Sign On"
'''


@pytest.fixture(scope="module", autouse=True)
def deterministic_app():
    """Switch off the interstitial for the duration of the suite.

    It is an instrument for experiments, not weather: these tests assert
    properties that must hold every time, and an app that shows a NOTICE screen
    on six lookups in ten makes them fail at random. The experiments that want
    flakiness turn it on deliberately.
    """
    import urllib.request

    urllib.request.urlopen(f"{BASE}/admin/interstitial?odds=0").read()
    yield


@pytest.fixture(scope="module")
def surface():
    s = WebSurface()
    yield s
    s.close()


@pytest.fixture(scope="module")
def signed_on(surface):
    surface.navigate(f"{BASE}/")
    surface.type(AfterText(anchor="Operator ID", role="textbox"), OPERATOR)
    surface.type(AfterText(anchor="Pass Code", role="textbox"), PASSCODE)
    surface.click(RoleName(role="button", name="Sign On"))
    return surface


@pytest.fixture
def at_member(signed_on):
    signed_on.navigate(f"{BASE}/member?no={MEMBER}")
    return signed_on


# -- frames: the condition this app exists to test -------------------------


def test_a_frameset_is_perceivable_at_all(surface):
    """A frameset document has no <body>. Reading one document saw nothing and
    timed out after five seconds — the surface could not perceive the page."""
    surface.navigate(f"{BASE}/")
    observation = surface.observe()

    assert observation.tree.strip(), "the page must not be empty"
    assert observation.candidates, "the controls live in the child documents"


def test_controls_from_both_documents_are_seen(surface):
    surface.navigate(f"{BASE}/")
    frames = {c.frame for c in surface.observe().candidates}

    assert "navFrame" in frames
    assert "workFrame" in frames


def test_a_control_names_the_document_it_lives_in(surface):
    """Two frames can hold controls of the same name, so the frame is part of
    a control's address rather than decoration."""
    surface.navigate(f"{BASE}/")
    signon = next(c for c in surface.observe().candidates if c.name == "Sign On")

    assert signon.frame == "workFrame"
    assert "in frame workFrame" in signon.describe()


def test_acting_across_frames(surface):
    """Type in one document, click a link in another, see the first update."""
    surface.navigate(f"{BASE}/")
    assert surface.type(AfterText(anchor="Operator ID", role="textbox"), OPERATOR).ok
    assert surface.type(AfterText(anchor="Pass Code", role="textbox"), PASSCODE).ok
    assert surface.click(RoleName(role="button", name="Sign On")).ok
    assert surface.click(RoleName(role="link", name="Member Inquiry")).ok

    assert surface.resolve(AfterText(anchor="Member No.", role="textbox")).found


# -- legacy markup ---------------------------------------------------------


def test_a_label_four_tables_deep_resolves(surface):
    """Meridian labels a field with <td><b>Operator ID</b></td> beside an
    unlabelled input, four tables down. No for, no id, no aria."""
    surface.navigate(f"{BASE}/")
    assert surface.resolve(AfterText(anchor="Operator ID", role="textbox")).found


def test_inputs_have_no_accessible_name(surface):
    """If this ever starts passing, the app gained real labels and the anchor
    strategy can be demoted from primary back to fallback."""
    surface.navigate(f"{BASE}/")
    assert surface.resolve(RoleName(role="textbox", name="Operator ID")).found is False


def test_an_image_control_is_named_by_its_alt(surface):
    """Controls here are pictures. Perception and targeting must agree on the
    name — they once used different algorithms, so a control was advertised by
    its title and resolved by its alt, and could never be reached."""
    surface.navigate(f"{BASE}/")
    assert surface.resolve(RoleName(role="button", name="Sign On")).found
    assert surface.resolve(RoleName(role="link", name="Member Inquiry")).found


# -- the invariant ---------------------------------------------------------


@pytest.mark.parametrize("where", ["/", "/lookup", "/member?no=" + MEMBER])
def test_every_advertised_control_is_reachable(signed_on, where):
    """Anything described to the model must be targetable by what we described.

    This is the invariant, not a detail: the failure message that teaches the
    model what to try next was once capable of suggesting controls that did not
    exist.
    """
    signed_on.navigate(f"{BASE}{where}")
    observation = signed_on.observe()
    assert observation.candidates

    unreachable = []
    for c in observation.candidates:
        if c.name:
            target = RoleName(role=c.role, name=c.name, frame=c.frame)
        elif c.after_text:
            target = AfterText(role=c.role, anchor=c.after_text, frame=c.frame)
        else:
            target = Ordinal(role=c.role, index=c.ordinal, frame=c.frame)
        if not signed_on.resolve(target).found:
            unreachable.append(c.describe())

    assert not unreachable, f"advertised but unreachable: {unreachable}"


# -- observation stability -------------------------------------------------


def test_same_page_gives_same_digest(signed_on):
    """The no-op verdict is digest equality, so an unstable digest silently
    turns every action into an apparent success."""
    digests = set()
    for _ in range(4):
        signed_on.navigate(f"{BASE}/lookup")
        observation = signed_on.observe()
        assert observation.stable
        digests.add(observation.digest)
    assert len(digests) == 1, f"observation is flaky: {digests}"


def test_different_pages_give_different_digests(signed_on):
    signed_on.navigate(f"{BASE}/lookup")
    a = signed_on.observe().digest
    signed_on.navigate(f"{BASE}/member?no={MEMBER}")
    assert a != signed_on.observe().digest


def test_digest_survives_session_tokens_in_the_tree():
    """A page's first load can carry a token its later loads do not. Anything
    that perturbs the digest turns every action into an apparent success, and
    legacy apps emit per-request tokens constantly."""
    cold = Observation(url="/", tree=COLD_TREE)
    warm = Observation(url="/", tree=WARM_TREE)

    assert "jsessionid" in cold.tree and "jsessionid" not in warm.tree
    assert cold.digest == warm.digest, "a session token leaked into the digest"


def test_digest_still_notices_real_changes():
    a = Observation(url="/", tree=WARM_TREE)
    b = Observation(url="/", tree=WARM_TREE.replace("Main Menu", "Member Inquiry"))
    assert a.digest != b.digest


# -- the learning mechanism ------------------------------------------------


def test_a_failed_target_names_one_that_works(surface):
    """A failure has to teach. This is what lets the model recover from a dead
    end during discovery."""
    surface.navigate(f"{BASE}/")
    result = surface.type(RoleName(role="textbox", name="Operator ID"), OPERATOR)

    assert result.ok is False
    assert 'textbox after "Operator ID"' in result.error


def test_a_field_reports_what_it_holds(surface):
    """Where the label is useless, the value is the only way to tell one box
    from another. Adding this took a run from 30 turns to 13."""
    surface.navigate(f"{BASE}/")
    surface.type(AfterText(anchor="Operator ID", role="textbox"), OPERATOR)
    filled = [c for c in surface.observe().candidates if c.value]

    assert any(OPERATOR in (c.value or "") for c in filled)
    assert any("holding" in c.describe() for c in filled)


# -- reading data out of nested tables -------------------------------------


@pytest.mark.parametrize("label,expected",
                         [("Name", "R. ACHTERBERG"), ("Savings Bal.", "4,210.55"),
                          ("Status", "ACTIVE")])
def test_reads_label_value_cell_pairs(at_member, label, expected):
    result = at_member.read(AfterText(anchor=label, role="cell"))
    assert result.ok
    assert result.value == expected


# -- the runtime conditions §3.3 asks about --------------------------------


def test_a_member_not_on_file_says_so(signed_on):
    signed_on.navigate(f"{BASE}/lookup")
    signed_on.type(AfterText(anchor="Member No.", role="textbox"), "40999")
    signed_on.click(RoleName(role="button", name="Inquire"))

    assert "NO MEMBER ON FILE" in signed_on.observe().tree


def test_a_non_numeric_member_is_a_validation_error(signed_on):
    signed_on.navigate(f"{BASE}/lookup")
    signed_on.type(AfterText(anchor="Member No.", role="textbox"), "ABC")
    signed_on.click(RoleName(role="button", name="Inquire"))

    assert "MUST BE NUMERIC" in signed_on.observe().tree


def test_a_restricted_member_is_refused(signed_on):
    """Permission denial is a distinct condition from not-found, and a caller
    has to be able to tell them apart."""
    signed_on.navigate(f"{BASE}/member?no={RESTRICTED}")
    assert "NOT AUTHORISED" in signed_on.observe().tree


def test_ambiguity_is_reported_not_hidden(surface):
    surface.navigate(f"{BASE}/")
    assert surface.resolve(Css(selector="input")).ambiguous > 1
