"""Reconnaissance: what does ParaBank's accessibility tree actually give us?

Phase 1 exists to answer one question before anything is built on top of it:
is role+accessible-name targeting viable on a legacy JSP app, or is the tree
too bare to identify controls with?

Finding so far: buttons and links carry accessible names, but form inputs do
not — ParaBank labels fields with a sibling <p><b>Username</b></p> rather than
a <label for>. So "the textbox following the text X" is the primary targeting
strategy here, not a fallback. This probe uses it deliberately to prove it
works across every page of the flow.

Run:  .venv/bin/python tools/probe_ax.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = "http://localhost:8080/parabank"
USER, PASSWORD = "john", "demo"
OUT = Path(__file__).resolve().parent.parent / "evidence" / "probe"

# Roles we can target, mapped to the HTML that realises them. The point of the
# mapping is that callers name a role, never a tag — the same vocabulary a
# desktop accessibility API would use.
ROLE_TAGS = {
    "textbox": "input[@type='text' or @type='password' or not(@type)]",
    "combobox": "select",
    "button": "input[@type='submit' or @type='button'] | button",
}


def after_text(page: Page, anchor: str, role: str):
    """The first control of `role` that follows the text `anchor` in reading order.

    This is the tier ParaBank forces on us. It is expressed here as XPath, but
    the *intent* — an anchor string plus a role — is platform-neutral, which is
    what lets the same artifact step resolve on a different surface later.
    """
    tag = ROLE_TAGS[role]
    return page.locator(
        f"xpath=//*[normalize-space(text())='{anchor}']/following::{tag}[1]"
    )


def dump(page: Page, label: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    snapshot = page.locator("body").aria_snapshot()
    (OUT / f"{label}.ax.yaml").write_text(snapshot)

    stats = {
        "url": page.url.split("/")[-1][:48],
        "frames": len(page.frames),
        "testids": page.locator("[data-testid]").count(),
        "inputs": page.locator("input").count(),
        "unnamed inputs": page.locator("input:not([id]):not([aria-label])").count(),
        "tables": page.locator("table").count(),
        "ax lines": len(snapshot.splitlines()),
    }
    print(f"\n=== {label} ===")
    for k, v in stats.items():
        print(f"  {k:<15}: {v}")
    return stats


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        page.goto(f"{BASE}/index.htm", wait_until="domcontentloaded")
        dump(page, "01-landing")

        # log in through the real form, using the anchor strategy
        after_text(page, "Username", "textbox").fill(USER)
        after_text(page, "Password", "textbox").fill(PASSWORD)
        page.get_by_role("button", name="Log In").click()
        page.wait_for_load_state("networkidle")
        dump(page, "02-overview")

        accounts = page.locator("#accountTable a")
        ids = [accounts.nth(i).inner_text().strip() for i in range(accounts.count())]
        print(f"  accounts        : {ids}")

        accounts.first.click()
        page.wait_for_load_state("networkidle")
        dump(page, "03-account-detail")
        print("  --- account detail, as the model would see it ---")
        for line in page.locator("#showOverview, #accountDetails").aria_snapshot().splitlines()[:18]:
            print(f"    {line}")

        # the multi-step flow: open a new account
        page.get_by_role("link", name="Open New Account").click()
        page.wait_for_load_state("networkidle")
        dump(page, "04-open-account-form")
        print("  --- open account form ---")
        for line in page.locator("#openAccountForm").aria_snapshot().splitlines()[:20]:
            print(f"    {line}")

        # a real error state: look up an account that does not exist
        page.goto(f"{BASE}/activity.htm?id=99999", wait_until="domcontentloaded")
        dump(page, "05-not-found")
        print("  --- error page text ---")
        print(f"    {page.locator('body').inner_text()[:240]!r}")

        browser.close()

    print(f"\nSnapshots in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
