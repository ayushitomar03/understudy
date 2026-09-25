"""The mapper must not be able to name things that do not exist.

Two runs of the mapper were lost to this, and neither failure looked like a bug
from the outside — the model reported a thorough, articulate investigation and
concluded the tool was broken, which it was. First `summarise(target)` raised
because summarise takes candidates, so every unresolved target crashed instead of
reporting and the model tried thirty-four strategies against a useless error.
Then `s.type_text(...)`, which WebSurface spells `type`, so every *successful*
resolution crashed while failures returned cleanly.

Both are one-line mistakes that cost a full model run each to discover. They are
also both checkable without a browser, which is what this file does.
"""

from __future__ import annotations

import re
from pathlib import Path

from understudy.loop import mapper
from understudy.surface import AfterText, Css, Ordinal, RoleName
from understudy.surface.web import WebSurface


def test_every_surface_method_the_mapper_calls_exists():
    """The mapper reaches the surface through lambdas, so a wrong name is not an
    import error — it is a crash at the one moment the tool is asked to act."""
    source = Path(mapper.__file__).read_text()
    called = set(re.findall(r"lambda s: s\.([a-z_]+)\(", source))
    assert called, "no surface calls found; this test has stopped testing anything"
    missing = sorted(name for name in called if not hasattr(WebSurface, name))
    assert not missing, f"the mapper calls {missing}, which WebSurface does not have"


def test_a_target_can_always_be_described_for_a_failure_message():
    """A refusal message is the only thing the model has to correct itself with, so
    describing the target must never be the thing that raises."""
    for target in (RoleName(role="textbox", name="Operator ID"),
                   AfterText(anchor="Member No.", role="textbox"),
                   Css(selector="input[name=uid]"),
                   Ordinal(role="textbox", index=2)):
        described = mapper._describe(target)
        assert described and "object" not in described


def test_the_tools_the_agent_is_allowed_are_the_tools_that_exist():
    """A name in allowed_tools that no @tool defines is silently unavailable."""
    source = Path(mapper.__file__).read_text()
    defined = set(re.findall(r'@tool\(\s*"([a-z_]+)"', source))
    block = re.search(r"allowed_tools=\[(.*?)\],", source, re.S).group(1)
    allowed = set(re.findall(r'"([a-z_]+)"', block)) - {"mcp__sitemap__"}
    allowed = {n for n in allowed if not n.startswith("mcp")}
    assert allowed <= defined, f"allowed but not defined: {sorted(allowed - defined)}"
    assert defined <= allowed, f"defined but not allowed: {sorted(defined - allowed)}"
