"""The typed action tools the model is given, and nothing else.

Two properties this file exists to guarantee:

  * The model can only do what a Target can express. There is no "run this
    code" escape hatch, so every action is checkable against policy before it
    happens and maps one-to-one onto a step in the artifact. That is what makes
    an allowlist enforcement rather than an instruction.

  * Every result reports richly. A failed action returns what *is* on the page,
    a successful one returns what changed. The model owns strategy, so the only
    way it can choose well is if the runner describes the world honestly —
    a bare "not found" silently forces it into guessing.
"""

from __future__ import annotations

import difflib
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..surface.base import AfterText, Css, Ordinal, RoleName, Target, summarise

# One schema for "which control", shared by every tool that needs one. Flat on
# purpose: the model fills a few named fields rather than composing a nested
# object, which it gets right far more often.
TARGET_FIELDS = {
    "strategy": {
        "type": "string",
        "enum": ["role_name", "after_text", "ordinal", "css"],
        "description": (
            "How to find the control. 'role_name' when it has a visible accessible name. "
            "'after_text' for the control following a piece of visible text — needed when a "
            "field is unlabelled. 'ordinal' for the nth control of a role. 'css' only as a "
            "last resort; it will not port to another surface."
        ),
    },
    "role": {
        "type": "string",
        "enum": ["textbox", "combobox", "button", "link", "checkbox", "radio",
                 "heading", "cell", "row", "option"],
        "description": "What kind of control it is.",
    },
    "name": {"type": "string", "description": "For role_name: the visible accessible name."},
    "anchor": {"type": "string", "description": "For after_text: the visible text just before it."},
    "index": {"type": "integer", "description": "For after_text/ordinal: which match, from 1."},
    "selector": {"type": "string", "description": "For css only."},
}

# Shorter than this identifies nothing on a real page.
MIN_ANCHOR = 3

REASON_FIELD = {
    "type": "string",
    "description": "Why you are doing this, in one sentence. Recorded in the run evidence.",
}


def _index(raw: Any) -> int:
    """Ordinals are 1-based, and the model sends them as strings.

    `int(raw or 1)` looks right and is not: the string "0" is truthy, so it
    survives the fallback and becomes index 0, which matches nothing in XPath.
    Observed live — after two silent misses the model abandoned accessibility
    targeting and fell back to CSS selectors, which is the one strategy the
    design exists to avoid.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


# What the model reaches for when it half-remembers the enum. Seen live: it
# sent strategy="index" and lost two turns to a validation error.
STRATEGY_ALIASES = {"index": "ordinal", "position": "ordinal", "nth": "ordinal",
                    "name": "role_name", "role": "role_name", "text": "after_text",
                    "anchor": "after_text", "label": "after_text", "selector": "css"}


def build_target(args: dict[str, Any]) -> Target:
    """Turn the flat tool arguments into a Target, or explain what is missing."""
    strategy = STRATEGY_ALIASES.get(args.get("strategy"), args.get("strategy"))
    role = args.get("role")
    index = _index(args.get("index"))

    if strategy == "role_name":
        if not role or not args.get("name"):
            raise ValueError("role_name needs both 'role' and 'name'")
        return RoleName(role=role, name=args["name"])
    if strategy == "after_text":
        if not role or not args.get("anchor"):
            raise ValueError("after_text needs both 'role' and 'anchor'")
        anchor = args["anchor"]
        if len(anchor.strip()) < MIN_ANCHOR:
            # Seen live: the model anchored on "o" — a one-letter attribute
            # name — and read the wrong cell five times without noticing,
            # because a degenerate anchor matches something, just not the
            # intended thing. Refusing is more useful than resolving.
            raise ValueError(
                f"anchor {anchor!r} is too short to identify anything reliably. "
                "Use the full visible label next to the control, or target it by "
                "role and name, or by ordinal."
            )
        return AfterText(role=role, anchor=anchor, index=index)
    if strategy == "ordinal":
        if not role:
            raise ValueError("ordinal needs 'role'")
        return Ordinal(role=role, index=index)
    if strategy == "css":
        if not args.get("selector"):
            raise ValueError("css needs 'selector'")
        return Css(selector=args["selector"])
    raise ValueError(f"unknown strategy {strategy!r}")


def describe_change(before: str, after: str, limit: int = 10) -> str:
    """What changed between two accessibility trees, as text for the model.

    Reports the change without naming its meaning: added and removed lines, not
    "an error appeared". Deciding that an added line is an error message is
    interpretation, and interpretation belongs to the model.
    """
    if before == after:
        return "the page did not change"

    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0))
    added = [l[1:].strip() for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:].strip() for l in diff if l.startswith("-") and not l.startswith("---")]

    parts = []
    if added:
        parts.append("appeared: " + " | ".join(added[:limit]))
    if removed:
        parts.append("gone: " + " | ".join(removed[:limit]))
    extra = (len(added) - limit) + (len(removed) - limit)
    if extra > 0:
        parts.append(f"(+{extra} more lines)")
    return "; ".join(parts) or "the page changed"


def make_tools(ctx) -> Any:
    """Build the tool server bound to one run's context.

    `ctx` is the RunContext from runner.py: it owns the surface, the ledger, the
    evidence log and the draft capability. Tools are thin — they express intent,
    and the context decides what that costs.
    """

    @tool(
        "observe",
        "Look at the current page: its accessibility tree and every control you can target.",
        {"reason": REASON_FIELD},
    )
    async def observe(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_observe(args.get("reason", ""))

    @tool(
        "navigate",
        "Go to a URL. Must be inside the allowlist.",
        {"url": {"type": "string"}, "reason": REASON_FIELD},
    )
    async def navigate(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_navigate(args["url"], args.get("reason", ""))

    @tool("click", "Click a control.", {**TARGET_FIELDS, "reason": REASON_FIELD})
    async def click(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_click(args)

    @tool(
        "type_text",
        "Type into a textbox. Use value_is_parameter when the value should become an input of the capability.",
        {
            **TARGET_FIELDS,
            "text": {"type": "string", "description": "The value to type."},
            "parameter_name": {
                "type": "string",
                "description": (
                    "Leave empty unless this value is an input a caller varies per invocation. "
                    "If it is, put a snake_case NAME here, such as account_id — not true/false."
                ),
            },
            "reason": REASON_FIELD,
        },
    )
    async def type_text(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_type(args)

    @tool(
        "select_option",
        "Choose an option in a dropdown.",
        {**TARGET_FIELDS, "option": {"type": "string"},
         "parameter_name": {"type": "string",
                            "description": "snake_case NAME if a caller varies this; empty otherwise"},
         "reason": REASON_FIELD},
    )
    async def select_option(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_select(args)

    @tool(
        "read_value",
        "Read the text of something. Use output_name when this is a value the capability should return.",
        {
            **TARGET_FIELDS,
            "output_name": {
                "type": "string",
                "description": "snake_case name if this is a value the capability returns; empty otherwise.",
            },
            "reason": REASON_FIELD,
        },
    )
    async def read_value(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_read(args)

    @tool(
        "declare_parameter",
        "Say that a literal value you are using is really an input a caller would vary. "
        "Call this as soon as you know — every later use of that value is recorded as the "
        "parameter instead of the literal.",
        {
            "name": {"type": "string", "description": "snake_case name, e.g. account_id"},
            "value": {"type": "string", "description": "the actual value you are using in this run"},
            "description": {"type": "string", "description": "what a caller should pass here"},
        },
    )
    async def declare_parameter(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_declare_param(args)

    @tool(
        "note",
        "Record something you have established, so you do not re-derive it later.",
        {"claim": {"type": "string"}},
    )
    async def note(args: dict[str, Any]) -> dict[str, Any]:
        ctx.ledger.prove(args["claim"])
        ctx.log.emit("model_note", claim=args["claim"])
        return _text(f"noted: {args['claim']}")

    @tool(
        "finish",
        "Call this once the goal is met. Describe what proves it, so replay can check the same thing.",
        {
            "summary": {"type": "string", "description": "What you did, in one or two sentences."},
            "success_text": {
                "type": "string",
                "description": "Text visible on the final page that proves the goal was reached.",
            },
            "reason": REASON_FIELD,
        },
    )
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_finish(args)

    @tool(
        "escalate",
        "Call this when you cannot proceed safely: you are stuck, blocked, or the next step needs a human.",
        {"why": {"type": "string"}, "needed": {"type": "string", "description": "What a human should do."}},
    )
    async def escalate(args: dict[str, Any]) -> dict[str, Any]:
        return await ctx.do_escalate(args.get("why", ""), args.get("needed", ""))

    return create_sdk_mcp_server(
        name="surface",
        version="1.0.0",
        tools=[observe, navigate, click, type_text, select_option, read_value,
               declare_parameter, note, finish, escalate],
    )


def _text(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}]}


def render_result(
    *,
    verdict: str,
    url: str,
    change: str = "",
    value: str | None = None,
    error: str | None = None,
    candidates: list | None = None,
    extra: str = "",
) -> dict[str, Any]:
    """The shape every action reports back in."""
    lines = [f"verdict: {verdict}", f"url: {url}"]
    if error:
        lines.append(f"error: {error}")
    if value is not None:
        lines.append(f"value: {value!r}")
    if change:
        lines.append(f"change: {change}")
    if candidates:
        lines.append(f"controls on this page: {summarise(candidates, limit=25)}")
    if extra:
        lines.append(extra)
    return _text("\n".join(lines))
