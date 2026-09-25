"""One hundred tasks on MERIDIAN CORE 4.2, across seven structural difficulty tiers.

Expected answers are derived from the application's own data rather than typed out
here. That is deliberate: hand-written expectations were the single largest source
of false results in the twenty-task run — two tasks were impossible as written and
one drew on an empty account, and each looked exactly like a hard task rather than a
broken one. Importing the fixtures means a task cannot disagree with the app about
what the answer is; tools/check_100.py then confirms each answer is actually
*reachable*, which importing cannot tell us.

Difficulty is structural, so "hard" is something you can point at:

    T1  one screen after sign-on, one field read
    T2  two screens to cross before the value is visible
    T3  the answer must be selected or counted from a list
    T4  parameterised by something other than a member number
    T5  the flow changes data and then reads back what it changed
    T6  an ordered multi-screen workflow where the order is enforced
    T7  any of the above with the application misbehaving

Refusals — restricted records, absent members, invalid input, insufficient funds —
are their own tier (T3R) because the correct outcome is a message rather than a
value, and averaging "did it succeed" with "did it correctly decline" makes both
numbers meaningless.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from bench import Task

# -- the application's own fixtures ---------------------------------------


def _fixtures():
    spec = importlib.util.spec_from_file_location(
        "meridian_app", Path("targets/meridian/app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["meridian_app"] = module
    spec.loader.exec_module(module)
    return module


M = _fixtures()
MEMBERS, CONTACT, HISTORY = M.MEMBERS, M.CONTACT, M.HISTORY
ORDERS, STATEMENTS, WORK, AUDIT = M.ORDERS, M.STATEMENTS, M.WORK, M.AUDIT

OPEN = [m for m, d in MEMBERS.items() if not d["restricted"]]      # 40021 40055 40204
RESTRICTED = [m for m, d in MEMBERS.items() if d["restricted"]]    # 40113

TASKS: list[Task] = []
_n = 0


def add(goal, outputs, params=None, hazards=None, patterns=None,
        mutates=False, tier="T1", note="") -> None:
    global _n
    _n += 1
    task = Task(_n, goal, outputs, params or {}, hazards or {}, patterns or {},
                mutates, note=f"[{tier}] {note}".strip())
    TASKS.append(task)


# =========================================================== T1  one screen ==
# The member record carries six fields. Reading one of them off it is the
# simplest thing this application can be asked, and it is the control group.
for member, (label, key) in [("40021", ("savings balance", "savings")),
                             ("40021", ("account status", "status")),
                             ("40055", ("checking balance", "checking")),
                             ("40204", ("branch holding the record", "branch"))]:
    add(f"Look up member {{member_no}} and report the {label}",
        {key: MEMBERS[member][key]}, {"member_no": member}, tier="T1")

# =========================================================== T2  two screens ==
# Contact details and standing orders sit one screen beyond the record, so the
# flow has to cross a screen that does not hold the answer.
for member in OPEN:
    for label, key in [("telephone number", "phone"), ("post code", "postcode"),
                       ("e-mail address", "email")]:
        add(f"Report the {label} on file for member {{member_no}}",
            {key: CONTACT[member][key]}, {"member_no": member}, tier="T2")

# ============================================= T3  counted or selected lists ==
for member in OPEN:
    items = HISTORY[member]
    if items:
        add("Report how many items are posted for member {member_no}",
            {"item_count": str(len(items))}, {"member_no": member}, tier="T3",
            note="the count exists only inside 'Items shown: n of n'")
        add("Report the amount of the most recent posted item for member {member_no}",
            {"latest_amount": items[0][2]}, {"member_no": member}, tier="T3")
        add("Report the date of the oldest posted item for member {member_no}",
            {"oldest_date": items[-1][0]}, {"member_no": member}, tier="T3")
    else:
        add("Confirm whether member {member_no} has any posted items and report what "
            "the screen says", {"items_message": "NO ITEMS POSTED IN PERIOD."},
            {"member_no": member}, tier="T3",
            note="an empty result that is a real answer, not a failure")

for member in OPEN:
    orders = ORDERS[member]
    if orders:
        add("Report how many standing orders are active for member {member_no}",
            {"active_orders": str(sum(1 for o in orders if o["status"] == "ACTIVE"))},
            {"member_no": member}, tier="T3",
            note="a count that excludes the cancelled rows")
        add("Report the payee of the standing order with the largest amount for "
            "member {member_no}",
            {"largest_payee": max(orders, key=lambda o: float(o["amount"].replace(",", "")))
             ["payee"]}, {"member_no": member}, tier="T3",
            note="a row selected by comparing a column")
        add("Report the reference of the standing order paid on day {day} for "
            "member {member_no}",
            {"order_ref": next(o["ref"] for o in orders if o["day"] == orders[0]["day"])},
            {"member_no": member, "day": orders[0]["day"]}, tier="T3",
            note="a row selected by matching a column")
    else:
        add("Confirm whether member {member_no} has standing orders and report what the "
            "screen says", {"orders_message": "NO STANDING ORDERS ON FILE."},
            {"member_no": member}, tier="T3")

for member in OPEN:
    entries = AUDIT[member]
    if entries:
        add("Report how many audit entries exist for member {member_no}",
            {"audit_count": str(len(entries))}, {"member_no": member}, tier="T3")
        add("Report the operator who made the most recent audit entry for "
            "member {member_no}", {"latest_operator": entries[0][1]},
            {"member_no": member}, tier="T3")
        inquiries = [e for e in entries if e[2] == "INQUIRY"]
        if inquiries:
            add("Report how many INQUIRY entries are in the audit trail for "
                "member {member_no}", {"inquiry_count": str(len(inquiries))},
                {"member_no": member}, tier="T3",
                note="a list filtered before it is counted")
    else:
        add("Confirm whether member {member_no} has any audit entries and report what "
            "the screen says", {"audit_message": "NO AUDIT ENTRIES MATCH."},
            {"member_no": member}, tier="T3")

# ==================================== T4  parameterised by something else ====
# A statement is selected by period from a dropdown, and its figures are computed
# rather than copied off a record.
for (member, period), data in STATEMENTS.items():
    for label, key in [("closing balance", "closing"), ("total credits", "credits")]:
        add(f"Produce the {{period}} statement for member {{member_no}} and report the {label}",
            {key: data[key]}, {"member_no": member, "period": period}, tier="T4",
            note="the period is chosen from a dropdown")
add("Produce the {period} statement for member {member_no} and report the opening balance",
    {"opening": STATEMENTS[("40021", "AUG")]["opening"]},
    {"member_no": "40021", "period": "AUG"}, tier="T4")
add("Produce the {period} statement for member {member_no} and report the number of items",
    {"items": STATEMENTS[("40021", "SEP")]["items"]},
    {"member_no": "40021", "period": "SEP"}, tier="T4")
add("Produce the {period} statement for member {member_no} and report the total debits",
    {"debits": STATEMENTS[("40021", "SEP")]["debits"]},
    {"member_no": "40021", "period": "SEP"}, tier="T4")

# The work queue is reached from the nav panel rather than from a member, and is
# filtered by state rather than keyed by anything.
add("Open the work queue and report how many items are OPEN",
    {"open_items": str(sum(1 for w in WORK if w["state"] == "OPEN"))}, tier="T4",
    note="not reached through a member at all")
add("Open the work queue and report the member number on work item {item_id}",
    {"item_member": next(w["member"] for w in WORK if w["id"] == "W-7702")},
    {"item_id": "W-7702"}, tier="T4")
add("Open the work queue and report the type of work item {item_id}",
    {"item_kind": next(w["kind"] for w in WORK if w["id"] == "W-7703")},
    {"item_id": "W-7703"}, tier="T4")
add("Open the work queue and report which operator holds work item {item_id}",
    {"held_by": next(w["by"] for w in WORK if w["id"] == "W-7704")},
    {"item_id": "W-7704"}, tier="T4",
    note="an item already closed by someone else")

# ============================================ T5  change it, then read back ==
add("Transfer {amount} from savings to checking for member {member_no} and report the "
    "transaction reference", {"reference": ""}, {"member_no": "40021", "amount": "10.00"},
    patterns={"reference": r"8841-\d{4}"}, mutates=True, tier="T5",
    note="the reference differs every run, so it is graded by shape")
add("Transfer {amount} from savings to checking for member {member_no} and report the new "
    "savings balance", {"new_savings": "4,110.55"},
    {"member_no": "40021", "amount": "100.00"}, mutates=True, tier="T5")
add("Transfer {amount} from checking to savings for member {member_no} and report the new "
    "checking balance", {"new_checking": "12.40"},
    {"member_no": "40204", "amount": "5.00"}, mutates=True, tier="T5")
add("Change the telephone number for member {member_no} to {phone} and report the "
    "telephone number the confirmation screen shows", {"saved_phone": "0114 496 9999"},
    {"member_no": "40021", "phone": "0114 496 9999"}, mutates=True, tier="T5")
add("Change the e-mail address for member {member_no} to {email} and report the address "
    "the confirmation screen shows", {"saved_email": "new.member@firstvalley.test"},
    {"member_no": "40055", "email": "new.member@firstvalley.test"}, mutates=True, tier="T5")
add("Create a standing order for member {member_no} paying {payee} {amount} and report the "
    "reference it was given", {"order_ref": ""},
    {"member_no": "40055", "payee": "THAMES WATER", "amount": "31.50"},
    patterns={"order_ref": r"SO-\d{4}"}, mutates=True, tier="T5")
add("Cancel standing order {ref} for member {member_no} and report the status the "
    "confirmation screen shows", {"cancelled": "CANCELLED"},
    {"member_no": "40021", "ref": "SO-4411"}, mutates=True, tier="T5")
add("Create a standing order for member {member_no} paying {payee} {amount}, then report "
    "how many of their orders are active",
    {"active_orders": str(sum(1 for o in ORDERS["40021"] if o["status"] == "ACTIVE") + 1)},
    {"member_no": "40021", "payee": "NORTHERN POWER", "amount": "12.00"},
    mutates=True, tier="T5", note="the answer depends on the change just made")

add("Transfer {amount} from savings to checking for member {member_no}, then report the "
    "amount of the most recent posted item", {"latest_amount": "250.00"},
    {"member_no": "40021", "amount": "250.00"}, mutates=True, tier="T5",
    note="the transfer writes the ledger row the read then looks for")
add("Cancel standing order {ref} for member {member_no}, then report how many of their "
    "orders are still active",
    {"active_orders": str(sum(1 for o in ORDERS["40021"] if o["status"] == "ACTIVE") - 1)},
    {"member_no": "40021", "ref": "SO-4412"}, mutates=True, tier="T5",
    note="a count that only changes because of the cancellation")
add("Change the post code for member {member_no} to {post_code}, then read the post code "
    "back off the contact screen", {"post_code_read": "LS28 4HG"},
    {"member_no": "40204", "post_code": "LS28 4HG"}, mutates=True, tier="T5",
    note="read back from the form rather than from the confirmation")

# ================================== T6  an ordered multi-screen workflow =====
add("Claim work item {item_id} and report which operator now holds it",
    {"held_by": "tlr01"}, {"item_id": "W-7701"}, mutates=True, tier="T6",
    note="the screen only offers Claim while the item is OPEN")
add("Claim work item {item_id}, complete it with outcome {outcome}, and report what the "
    "confirmation screen says the outcome was", {"outcome_shown": "VERIFIED"},
    {"item_id": "W-7702", "outcome": "VERIFIED"}, mutates=True, tier="T6",
    note="completing before claiming is refused, so the order is enforced")
add("Claim work item {item_id}, complete it, then report how many items remain OPEN in "
    "the queue",
    {"open_items": str(sum(1 for w in WORK if w["state"] == "OPEN") - 1)},
    {"item_id": "W-7703"}, mutates=True, tier="T6")
add("Claim work item {item_id}, complete it with outcome {outcome}, then report how many "
    "items are CLOSED in the queue",
    {"closed_items": str(sum(1 for w in WORK if w["state"] == "CLOSED") + 1)},
    {"item_id": "W-7701", "outcome": "REFERRED"}, mutates=True, tier="T6",
    note="three screens, then a count that proves the third one took effect")
add("Report the outcome dropdown options available on work item {item_id} after claiming "
    "it", {"first_option": "VERIFIED"}, {"item_id": "W-7702"}, mutates=True, tier="T6",
    note="a control that does not exist until the item is claimed")
add("Authorise a supervisor override for member {member_no} with code {code}, transfer "
    "{amount} from savings to checking, then attempt the same transfer again and report "
    "exactly what the application says the second time",
    {"refusal": "SUPERVISOR AUTHORISATION REQUIRED"},
    {"member_no": "40021", "code": "7731", "amount": "1200.00"},
    hazards={"stepup": True}, mutates=True, tier="T6",
    note="the override authorises one transfer and then expires, so the second is "
         "refused — and 40021 can afford both, so the refusal is about authority")
add("Authorise a supervisor override for member {member_no} with code {code}, then "
    "transfer {amount} from savings to checking and report the transaction reference",
    {"reference": ""}, {"member_no": "40021", "code": "7731", "amount": "1500.00"},
    patterns={"reference": r"8841-\d{4}"}, hazards={"stepup": True}, mutates=True,
    tier="T6", note="the transfer is refused until the override is in force")

# ============================================= T7  the application misbehaves =
_HAZARDS = [
    ({"interstitial": 0.7}, "a notice stands in front of the record and can recur"),
    ({"lock": True}, "the record is held by another terminal; nothing to press"),
    ({"slow": True}, "a screen slower than the settle wait"),
]
for hazard, note in _HAZARDS:
    for member in OPEN:
        add("Look up member {member_no} and report the savings balance",
            {"savings": MEMBERS[member]["savings"]}, {"member_no": member},
            hazards=hazard, tier="T7", note=note)
    add("Report the telephone number on file for member {member_no}",
        {"phone": CONTACT["40021"]["phone"]}, {"member_no": "40021"},
        hazards=hazard, tier="T7", note=note)
    add("Report how many items are posted for member {member_no}",
        {"item_count": str(len(HISTORY["40021"]))}, {"member_no": "40021"},
        hazards=hazard, tier="T7", note=note)

add("Report the date of the oldest posted item for member {member_no}",
    {"oldest_date": HISTORY["40021"][-1][0]}, {"member_no": "40021"},
    hazards={"paging": True}, tier="T7",
    note="the answer is on the second screen, behind a Next key")
add("Report the amount of the second posted item for member {member_no}",
    {"second_amount": HISTORY["40021"][1][2]}, {"member_no": "40021"},
    hazards={"paging": True}, tier="T7", note="still on the first page, but paginated")
add("Report how many items are posted in total for member {member_no}",
    {"item_count": str(len(HISTORY["40021"]))}, {"member_no": "40021"},
    hazards={"paging": True}, tier="T7",
    note="the total is on the screen; the rows are not all on it")
add("Transfer {amount} from savings to checking for member {member_no} and report the new "
    "savings balance", {"new_savings": "123.00"},
    {"member_no": "40055", "amount": "5.00"}, hazards={"confirm": True}, mutates=True,
    tier="T7", note="the first press posts nothing and returns the same button")
add("Transfer {amount} from savings to checking for member {member_no} and report the "
    "transaction reference", {"reference": ""}, {"member_no": "40021", "amount": "20.00"},
    patterns={"reference": r"8841-\d{4}"}, hazards={"confirm": True}, mutates=True,
    tier="T7", note="two-phase commit on a reference read")
add("Change the post code for member {member_no} to {post_code} and report the post code "
    "the confirmation screen shows", {"saved_post_code": "S2 7Q"},
    {"member_no": "40055", "post_code": "S2 7QQ"}, hazards={"truncate": True},
    mutates=True, tier="T7", note="the field keeps five characters and says nothing")
add("Change the post code for member {member_no} to {post_code} and report the post code "
    "the confirmation screen shows", {"saved_post_code": "LS11 "},
    {"member_no": "40021", "post_code": "LS11 9AA"}, hazards={"truncate": True},
    mutates=True, tier="T7", note="truncation that lands on a space")
add("Claim work item {item_id} and report which operator now holds it",
    {"held_by": "tlr01"}, {"item_id": "W-7701"}, hazards={"slow": True}, mutates=True,
    tier="T7", note="an ordered workflow on a slow application")

# ============================== T3R  the correct answer is a refusal ==========
add("Attempt to look up member {member_no} and report exactly what the application says",
    {"refusal": "NOT AUTHORISED"}, {"member_no": RESTRICTED[0]}, tier="T3R",
    note="a record this operator may not open; retrying is how accounts get locked")
add("Attempt to look up member {member_no} and report exactly what the application says",
    {"refusal": "NO MEMBER ON FILE FOR 40999"}, {"member_no": "40999"}, tier="T3R",
    note="the application answered correctly; nothing is broken")
add("Enter {member_no} as the member number and report exactly what the application says",
    {"refusal": "MEMBER NO. MUST BE NUMERIC"}, {"member_no": "ABC"}, tier="T3R",
    note="the caller's input is wrong, not the flow")
add("Attempt to transfer {amount} from checking to savings for member {member_no} and "
    "report exactly what the application says",
    {"refusal": "INSUFFICIENT AVAILABLE BALANCE"},
    {"member_no": "40055", "amount": "500.00"}, mutates=True, tier="T3R",
    note="a business outcome, not a defect")
add("Attempt to transfer {amount} from savings to checking for member {member_no} and "
    "report exactly what the application says",
    {"refusal": "SUPERVISOR AUTHORISATION REQUIRED"},
    {"member_no": "40021", "amount": "2000.00"}, hazards={"stepup": True}, mutates=True,
    tier="T3R", note="above the terminal limit but within the balance, so it tests the "
                     "authorisation rather than whichever check runs first")
add("Attempt to cancel standing order {ref} for member {member_no} and report exactly "
    "what the application says", {"refusal": "NO SUCH ORDER REFERENCE SO-9999"},
    {"member_no": "40021", "ref": "SO-9999"}, mutates=True, tier="T3R")
add("Attempt to complete work item {item_id} without claiming it and report exactly what "
    "the application says",
    {"refusal": "ITEM MUST BE CLAIMED BEFORE IT CAN BE COMPLETED"},
    {"item_id": "W-7701"}, mutates=True, tier="T3R",
    note="the workflow's order, enforced")
add("Attempt to change the telephone number for member {member_no} to {phone} and report "
    "exactly what the application says",
    {"refusal": "TELEPHONE MUST BE AT LEAST 10 DIGITS"},
    {"member_no": "40021", "phone": "0114"}, mutates=True, tier="T3R")
add("Produce the {period} statement for member {member_no} and report exactly what the "
    "application says", {"refusal": "NO STATEMENT FOR PERIOD JUL"},
    {"member_no": "40204", "period": "JUL"}, tier="T3R",
    note="a period with no data is an answer")
add("Attempt to authorise a supervisor override for member {member_no} with code {code} "
    "and report exactly what the application says",
    {"refusal": "OVERRIDE CODE NOT RECOGNISED"},
    {"member_no": "40021", "code": "0000"}, mutates=True, tier="T3R")


def tier(task: Task) -> str:
    return task.note.split("]")[0].lstrip("[") if task.note.startswith("[") else "?"


# Exactly one hundred, and it should stay that way: a set that quietly grows or
# shrinks makes two runs incomparable.
assert len(TASKS) == 100, f"expected 100 tasks, built {len(TASKS)}"


if __name__ == "__main__":
    from collections import Counter
    counts = Counter(tier(t) for t in TASKS)
    print(f"{len(TASKS)} tasks")
    for name in sorted(counts):
        hazarded = sum(1 for t in TASKS if tier(t) == name and t.hazards)
        mutating = sum(1 for t in TASKS if tier(t) == name and t.mutates)
        print(f"  {name:<5} {counts[name]:>3}   {hazarded:>2} hazarded  {mutating:>2} mutating")
    print(f"\n  hazarded: {sum(1 for t in TASKS if t.hazards)}"
          f"   mutating: {sum(1 for t in TASKS if t.mutates)}")
