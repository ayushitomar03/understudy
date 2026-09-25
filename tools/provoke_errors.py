"""Provoke every error state ParaBank will actually produce, and record it.

The taxonomy is only worth anything if its detectors match strings the app
really emits. Writing them from imagination produces a schema that looks
thorough and never fires. So: trigger each state deliberately, capture what
comes back, and let the taxonomy be written from this output.

Run:  .venv/bin/python tools/provoke_errors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from understudy.surface import AfterText, Ordinal, RoleName, WebSurface

BASE = "http://localhost:8080/parabank"
USER, PASSWORD = "john", "demo"
OUT = Path("evidence/error-states")

# Accounts from the live demo dataset.
OVERDRAWN = "12345"   # -$2300.00
FUNDED = "13344"      # $1231.10
SMALL = "12456"       # $10.45


def signals(surface: WebSurface) -> dict:
    """What a detector could actually key on."""
    obs = surface.observe()
    lines = obs.tree.splitlines()
    return {
        "url": obs.url.split("/")[-1][:60],
        "error_heading": any('heading "Error!"' in l for l in lines),
        "headings": [l.strip().split('heading ')[1] for l in lines if "heading " in l][:4],
        "messages": [
            l.strip()[2:].strip()
            for l in lines
            if l.strip().startswith("- paragraph:") and len(l.strip()) > 20
        ][:6],
        "error_spans": [l.strip() for l in lines if "rror" in l or "nvalid" in l or "not " in l.lower()][:6],
    }


def login(s: WebSurface, user: str = USER, password: str = PASSWORD) -> None:
    s.navigate(f"{BASE}/index.htm")
    s.type(AfterText(anchor="Username", role="textbox"), user)
    s.type(AfterText(anchor="Password", role="textbox"), password)
    s.click(RoleName(role="button", name="Log In"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict] = {}
    s = WebSurface()

    try:
        # ---- 1. bad credentials --------------------------------------------
        login(s, "john", "wrong-password")
        found["bad_login"] = signals(s)

        # ---- 2. unauthenticated access to a protected page -----------------
        s.navigate(f"{BASE}/overview.htm")
        found["not_authenticated"] = signals(s)

        login(s)

        # ---- 3. account that does not exist --------------------------------
        s.navigate(f"{BASE}/activity.htm?id=99999")
        found["account_not_found"] = signals(s)

        # ---- 4. transfer more than the balance -----------------------------
        s.navigate(f"{BASE}/transfer.htm")
        s.type(AfterText(anchor="$", role="textbox"), "999999")
        s.select(Ordinal(role="combobox", index=1), SMALL)
        s.select(Ordinal(role="combobox", index=2), FUNDED)
        s.click(RoleName(role="button", name="Transfer"))
        found["transfer_over_balance"] = signals(s)

        # ---- 5. transfer with a non-numeric amount -------------------------
        s.navigate(f"{BASE}/transfer.htm")
        s.type(AfterText(anchor="$", role="textbox"), "abc")
        s.click(RoleName(role="button", name="Transfer"))
        found["transfer_bad_amount"] = signals(s)

        # ---- 6. transfer with the amount left empty ------------------------
        s.navigate(f"{BASE}/transfer.htm")
        s.click(RoleName(role="button", name="Transfer"))
        found["transfer_empty_amount"] = signals(s)

        # ---- 7. bill pay with nothing filled in ----------------------------
        s.navigate(f"{BASE}/billpay.htm")
        s.click(RoleName(role="button", name="Send Payment"))
        found["billpay_empty_form"] = signals(s)

        # ---- 8. bill pay where the two account fields disagree -------------
        s.navigate(f"{BASE}/billpay.htm")
        for anchor, value in [
            ("Payee Name:", "Acme Utilities"), ("Address:", "1 Main St"),
            ("City:", "Springfield"), ("State:", "IL"), ("Zip Code:", "62701"),
            ("Phone #:", "5551234567"), ("Account #:", "54321"),
            ("Verify Account #:", "99999"), ("Amount: $", "10"),
        ]:
            s.type(AfterText(anchor=anchor, role="textbox"), value)
        s.click(RoleName(role="button", name="Send Payment"))
        found["billpay_account_mismatch"] = signals(s)

        # ---- 9. a search that legitimately matches nothing -----------------
        s.navigate(f"{BASE}/findtrans.htm")
        s.select(Ordinal(role="combobox", index=1), FUNDED)
        boxes = [c for c in s.observe().candidates if c.role == "textbox"]
        if boxes:
            s.type(Ordinal(role="textbox", index=len(boxes)), "999999.99")
        for btn in range(1, 6):
            r = s.click(Ordinal(role="button", index=btn))
            if r.ok:
                break
        found["search_no_results"] = signals(s)

        # ---- 10. loan far beyond means -------------------------------------
        s.navigate(f"{BASE}/requestloan.htm")
        s.type(Ordinal(role="textbox", index=1), "100000000")
        s.type(Ordinal(role="textbox", index=2), "0")
        s.click(RoleName(role="button", name="Apply Now"))
        found["loan_denied"] = signals(s)

        # ---- 11. session ended, then use a protected page ------------------
        s.navigate(f"{BASE}/logout.htm")
        s.navigate(f"{BASE}/activity.htm?id={FUNDED}")
        found["session_ended"] = signals(s)

    finally:
        s.close()

    (OUT / "states.json").write_text(json.dumps(found, indent=2))

    for name, sig in found.items():
        msg = (sig["messages"] or sig["error_spans"] or ["—"])[0]
        flag = "ERROR!" if sig["error_heading"] else "      "
        print(f"{name:<26} {flag}  {str(msg)[:88]}")
    print(f"\nfull capture: {OUT / 'states.json'}")


if __name__ == "__main__":
    main()
