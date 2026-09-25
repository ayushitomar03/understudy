"""An AI agent using the catalog — the thing the whole system exists to serve.

§1 frames this system as "the backend integration layer that gives those agents
hands". This is that claim tested: a model is handed the catalog as tools and a
question in plain language, and nothing else. It does not know what ParaBank is,
cannot see a browser, and has no way to drive a UI. All it can do is pick a
capability and call it.

Every tool call here runs a real capability against the real application, with
no model in the execution path — the agent decides *which* capability and with
what arguments; deterministic replay does the rest.

    .venv/bin/python tools/agent_invokes.py "what is the balance of account 13344?"
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from understudy.catalog import Catalog

CREDENTIALS = {"username": "john", "password": "demo",
               "dn": "cn=admin,dc=firstvalley,dc=test"}

SYSTEM = """You are an assistant for a bank's back-office staff.

You have no direct access to any system. What you have is a set of capabilities —
recorded, reviewed automations that operate the institution's applications on
your behalf. Call the one that fits, with the arguments it declares.

If no capability covers what is being asked, say so plainly rather than guessing.
Some capabilities are marked as requiring a human. If the request needs one of those,
do not call it — tell the user it needs a person to approve, and say which capability.
If a capability returns a business outcome such as "not found", report that as the
answer — it is a legitimate result, not a failure.

Answer in one or two sentences, using the values the capability returned."""


def build_server(catalog: Catalog, calls: list[dict]) -> Any:
    """Expose each callable capability as a tool the model can invoke."""
    tools = []
    for listing in catalog.list():

        def make(name: str):
            async def run(args: dict[str, Any]) -> dict[str, Any]:
                result = await catalog.ainvoke(name, {k: str(v) for k, v in args.items()})
                calls.append({"capability": name, "arguments": args,
                              "outcome": result.outcome, "outputs": result.outputs,
                              "seconds": result.seconds})
                print(f"    -> {name}({json.dumps(args)})", flush=True)
                print(f"       {result.outcome} in {result.seconds}s  {result.outputs}", flush=True)
                return {"content": [{"type": "text", "text": result.for_model()}]}
            return run

        spec = listing.as_tool()
        tools.append(tool(spec["name"], spec["description"], spec["input_schema"])(make(listing.name)))

    return create_sdk_mcp_server(name="capabilities", version="1.0.0", tools=tools)


async def ask(question: str) -> int:
    catalog = Catalog(secrets=CREDENTIALS)
    listings = catalog.list()
    if not listings:
        print("no capabilities published yet")
        return 1

    print(f"question: {question}\n")
    print("capabilities offered to the agent:")
    for listing in listings:
        params = ", ".join(listing.input_schema.get("properties", {}))
        mark = "" if listing.callable else "   [needs a human]"
        print(f"  {listing.name}({params}) -> {', '.join(listing.returns) or '—'}{mark}")
    print()

    calls: list[dict] = []
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM,
        mcp_servers={"capabilities": build_server(catalog, calls)},
        allowed_tools=[f"mcp__capabilities__{l.as_tool()['name']}" for l in listings],
        max_turns=6,
    )

    answer = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(question)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        answer = block.text.strip()

    print(f"\nanswer: {answer}")
    print(f"\ncapabilities invoked: {len(calls)}")
    for call in calls:
        print(f"  {call['capability']}{tuple(call['arguments'].values())} "
              f"-> {call['outputs']} ({call['seconds']}s, no model in the execution path)")
    return 0


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "What is the balance of account 13344?"
    sys.exit(asyncio.run(ask(question)))
