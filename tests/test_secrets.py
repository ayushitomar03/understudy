"""No credential the surveyor was given may survive into a map on disk.

This is a regression test with a specific incident behind it. The survey records
what each field held when the map was built; on the sign-on screen those fields
held the operator id and the pass code, so a map went into a public repository
with the password in it. It was caught by grepping the repository before
publishing, which is not a control worth relying on twice.
"""

from __future__ import annotations

from understudy.artifact.schema import Locator
from understudy.sitemap import Screen, SiteMap, Table, Value, save, scrub
from understudy.surface.base import AfterText

SECRETS = {"uid": "tlr01", "pwd": "vault"}


def _sign_on_screen() -> Screen:
    def value(label: str, example: str) -> Value:
        return Value(label=label,
                     locator=Locator(target=AfterText(anchor=label, role="textbox", index=1),
                                     why="test", verified=True),
                     example=example)
    return Screen(id="teller_sign_on", identifies_by="TELLER SIGN ON",
                  values=[value("Operator ID", "tlr01"), value("Pass Code", "vault"),
                          value("Savings Bal.", "4,210.55")])


def test_a_credential_recorded_as_a_field_example_is_removed():
    sitemap = SiteMap(app="http://localhost:8090", screens=[_sign_on_screen()])
    removed = scrub(sitemap, SECRETS)

    examples = {v.label: v.example for v in sitemap.screens[0].values}
    assert examples["Operator ID"] == ""
    assert examples["Pass Code"] == ""
    assert set(removed) == {"teller_sign_on.Operator ID", "teller_sign_on.Pass Code"}


def test_an_ordinary_value_is_left_alone():
    """Scrubbing that blanks real data is its own kind of damage."""
    sitemap = SiteMap(app="http://localhost:8090", screens=[_sign_on_screen()])
    scrub(sitemap, SECRETS)
    assert sitemap.screens[0].values[2].example == "4,210.55"


def test_a_value_merely_containing_a_secret_is_left_alone():
    """Fields are compared whole: a balance holding a short pass code is not a leak."""
    sitemap = SiteMap(app="http://localhost:8090", screens=[_sign_on_screen()])
    sitemap.screens[0].values[2].example = "vaulted 4,210.55"
    scrub(sitemap, SECRETS)
    assert sitemap.screens[0].values[2].example == "vaulted 4,210.55"


def test_nothing_secret_reaches_the_file(tmp_path):
    sitemap = SiteMap(app="http://localhost:8090", screens=[_sign_on_screen()])
    scrub(sitemap, SECRETS)
    written = save(sitemap, root=tmp_path).read_text()
    for secret in SECRETS.values():
        assert secret not in written


def test_the_map_this_system_ships_with_holds_no_credential():
    from understudy.sitemap import STORE
    for path in STORE.glob("*.json"):
        text = path.read_text()
        for secret in SECRETS.values():
            assert secret not in text, f"{path} contains a credential"
