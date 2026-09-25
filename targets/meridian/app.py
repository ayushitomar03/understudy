"""MERIDIAN CORE 4.2 — a deliberately hostile stand-in for legacy core banking.

Built because the two real applications we test against are both too polite.
ParaBank and phpLDAPadmin between them cover nested tables, image-only controls
and absent test IDs, but neither has the trait the brief names first:

    "a legacy web app (server-rendered, framesets, deeply nested tables,
     non-semantic markup, no test IDs)"

Our surface reads `page.locator("body")` — one document. A frameset application
would fail at step one and we would never have found out.

What this deliberately does, and why each one is real rather than gratuitous:

  * **Framesets.** Navigation and content live in separate documents. Anything
    that assumes one DOM sees an empty page.
  * **Labels as markup, not semantics.** `<td><b>Member No.</b></td>` beside an
    unlabelled input, three tables deep. No `for`, no `id`, no `aria-*`.
  * **Controls that are images.** The submit button is a picture with an `alt`.
  * **A session that expires on demand.** GET /admin/expire kills every session,
    so a flow can be interrupted mid-way and has to re-authenticate — the
    runtime condition our replay has no answer for. Deliberate rather than
    ambient: an app that expires sessions at random makes every measurement
    noisy instead of making one thing testable.
  * **Real error states.** Not found, validation, permission denied, and an
    interstitial that appears unpredictably.

Run:  python targets/meridian/app.py [port]
"""

from __future__ import annotations

import copy
import http.cookies
import json
import random
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8090

USERS = {"tlr01": "vault"}

# Member 40113 is deliberately restricted: a real back office has records a
# given teller may not open, and "permission denied" is one of the runtime
# conditions the brief lists.
MEMBERS = {
    "40021": {"name": "R. ACHTERBERG", "savings": "4,210.55", "checking": "812.03",
              "status": "ACTIVE", "branch": "MAIN", "restricted": False},
    "40055": {"name": "M. OYELARAN", "savings": "128.00", "checking": "0.00",
              "status": "ACTIVE", "branch": "NORTHGATE", "restricted": False},
    "40113": {"name": "D. SZABO", "savings": "91,004.12", "checking": "2,330.90",
              "status": "ACTIVE", "branch": "MAIN", "restricted": True},
    "40204": {"name": "K. NAKASHIMA", "savings": "0.00", "checking": "17.40",
              "status": "DORMANT", "branch": "MAIN", "restricted": False},
}

# Posted items per member. A list rather than a field, because a flow that
# extracts rows is a different shape of problem from one that reads a value —
# and the artifact has no way to express a repeating output, which is better
# found against a real screen than argued about in the abstract.
HISTORY = {
    "40021": [("14/09", "DEPOSIT", "1,200.00"), ("12/09", "ATM WDL", "80.00"),
              ("02/09", "TFR IN", "3,090.55")],
    "40055": [("11/09", "DEPOSIT", "128.00")],
    "40113": [("13/09", "TFR OUT", "8,000.00"), ("09/09", "DEPOSIT", "99,004.12")],
    "40204": [],
}

# Contact details, editable — a form carrying rules of its own.
CONTACT = {
    m: {"phone": "0114 496 0" + m[-3:], "email": "member" + m + "@firstvalley.test",
        "postcode": "S1 " + m[-3:]}
    for m in ("40021", "40055", "40113", "40204")
}

# Hazards, each off by default and switched on deliberately via
# /admin/hazard?name=X&on=1. A legacy application does all of these at once and
# a test suite cannot measure anything against all of them at once, so they are
# instruments rather than weather — the same reason session expiry is a switch.
#
#   lock        RECORD IN USE BY ANOTHER TERMINAL — clears itself after a wait,
#               so the right response is to wait, not to click something.
#   confirm     Two-phase commit: the first Post Transfer returns a confirmation
#               screen carrying the same button, so a flow recorded in one pass
#               submits once and silently posts nothing.
#   paging      Posted items arrives two rows at a time behind a Next key. The
#               artifact's Output is one locator and one value; there is no way
#               to say "every row", which is worth discovering against a screen.
#   truncate    The Post Code field silently keeps five characters. The flow is
#               correct and the echo check fails, which is a different thing
#               from the flow being broken.
#   stepup      A transfer over 1,000 needs a supervisor. Retrying cannot help
#               and a person has to decide — the opposite of an interstitial.
#   slow        Posted items takes 2.5s. Anything that waits 1.5s for the page
#               to settle concludes it has settled.
HAZARDS = {"lock": False, "confirm": False, "paging": False,
           "truncate": False, "stepup": False, "slow": False}

PAGE_SIZE = 2           # rows per screen when `paging` is on
LOCK_SECONDS = 2.0      # how long a record stays held by the other terminal
LOCK_ODDS = 0.5         # chance the other terminal grabs a free record
STEPUP_LIMIT = 1000.0   # above this a transfer needs a supervisor

# Which records are currently held, and until when. A held record is the most
# ordinary thing in a back office and has no equivalent on a modern web app.
LOCKS: dict[str, float] = {}

# Posting a transfer moves money and appends to the ledger, which means the
# application has state that outlives a run. That is correct — it is what makes a
# double submit observable — but it also means one measurement can decide the
# next one, so reset restores the opening position rather than only clearing
# sessions. The suite found this by failing: a transfer in one test left member
# 40021 thirty pounds short in another.
# Standing orders: a list you can add to and cancel from, which is a different
# shape of task from reading a field or posting a one-off transfer.
ORDERS = {
    "40021": [{"ref": "SO-4411", "payee": "NORTHERN GAS", "amount": "42.00",
               "day": "01", "status": "ACTIVE"},
              {"ref": "SO-4412", "payee": "CITY COUNCIL", "amount": "128.50",
               "day": "15", "status": "ACTIVE"}],
    "40055": [{"ref": "SO-5501", "payee": "AQUA UTILITIES", "amount": "18.00",
               "day": "08", "status": "CANCELLED"}],
    "40113": [], "40204": [],
}
NEXT_ORDER = [9000]        # a list so the counter survives being closed over

# Statement periods, so a read can be parameterised by something other than a
# member number and the answer has to be computed rather than copied.
STATEMENTS = {
    ("40021", "SEP"): {"opening": "3,010.55", "credits": "4,290.55", "debits": "80.00",
                       "closing": "4,210.55", "items": "3"},
    ("40021", "AUG"): {"opening": "2,800.00", "credits": "310.55", "debits": "100.00",
                       "closing": "3,010.55", "items": "2"},
    ("40055", "SEP"): {"opening": "0.00", "credits": "128.00", "debits": "0.00",
                       "closing": "128.00", "items": "1"},
    ("40204", "SEP"): {"opening": "17.40", "credits": "0.00", "debits": "0.00",
                       "closing": "17.40", "items": "0"},
}
PERIODS = ["SEP", "AUG", "JUL"]

# A worklist. Claim an item, action it, complete it — three screens and an order
# that matters, which no single-screen task can test.
WORK = [
    {"id": "W-7701", "kind": "ADDRESS VERIFY", "member": "40021", "state": "OPEN",
     "by": ""},
    {"id": "W-7702", "kind": "DORMANCY REVIEW", "member": "40204", "state": "OPEN",
     "by": ""},
    {"id": "W-7703", "kind": "FEE WAIVER", "member": "40055", "state": "OPEN", "by": ""},
    {"id": "W-7704", "kind": "ADDRESS VERIFY", "member": "40113", "state": "CLOSED",
     "by": "tlr02"},
]

# Who touched what. A list you filter rather than read whole.
AUDIT = {
    "40021": [("22/09 09:14", "tlr01", "INQUIRY"), ("21/09 16:02", "tlr02", "AMEND"),
              ("21/09 11:48", "tlr01", "INQUIRY"), ("18/09 14:20", "sup09", "OVERRIDE")],
    "40055": [("22/09 10:01", "tlr01", "INQUIRY")],
    "40113": [("20/09 08:30", "sup09", "RESTRICT")],
    "40204": [],
}

# A supervisor code, so the step-up path can actually be completed rather than
# only refused. Not a secret: a fixture on a fake bank.
OVERRIDE_CODE = "7731"
OVERRIDES: set[str] = set()          # members with an override in force

PRISTINE = copy.deepcopy((MEMBERS, HISTORY, CONTACT, ORDERS, STATEMENTS, WORK, AUDIT))

SESSIONS: dict[str, dict] = {}
# Session expiry is an instrument, not weather. Ambient expiry made the test
# suite flaky at random points, which is a different thing from being able to
# expire a session deliberately and watch what the system does about it.
# Raise it here or call /admin/expire to trigger one on demand.
MAX_REQUESTS = 400
INTERSTITIAL_ODDS = 0.0    # raised by /admin to make a known interstitial appear

CHROME = """<html><head><title>MERIDIAN CORE 4.2</title></head>
<body bgcolor="#d4d0c8" link="#000080" vlink="#000080">
<table border="0" cellpadding="0" cellspacing="0" width="100%%"><tr>
<td bgcolor="#000080"><table border="0" cellpadding="3" cellspacing="0"><tr>
<td><font face="MS Sans Serif" size="2" color="#ffffff"><b>%s</b></font></td>
</tr></table></td></tr></table>
<table border="0" cellpadding="8" cellspacing="0" width="100%%"><tr><td>
%s
</td></tr></table></body></html>"""


def page(title: str, body: str) -> bytes:
    return (CHROME % (title, body)).encode()


def field(label: str, name: str, value: str = "", kind: str = "text") -> str:
    """A labelled input the legacy way: the label is bold text in a cell beside
    the control, with nothing tying the two together."""
    return (f'<tr><td align="right" width="150"><font face="MS Sans Serif" size="2">'
            f'<b>{label}</b></font></td>'
            f'<td><input type="{kind}" name="{name}" value="{value}" size="24"></td></tr>')


def button(alt: str, name: str = "go") -> str:
    """The submit control is a picture. Its only name is the alt text."""
    return (f'<input type="image" name="{name}" alt="{alt}" '
            f'src="/img/btn.gif" border="0" width="78" height="22">')


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- session ---------------------------------------------------------

    def _session(self) -> dict | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = http.cookies.SimpleCookie(raw)
        if "MCSESS" not in cookie:
            return None
        session = SESSIONS.get(cookie["MCSESS"].value)
        if session is None:
            return None
        session["requests"] += 1
        if session["requests"] > MAX_REQUESTS or session.get("killed"):
            SESSIONS.pop(cookie["MCSESS"].value, None)
            return None
        return session

    def _require(self) -> dict | None:
        """Every content page calls this. An expired session drops the operator
        back to a login screen *inside the content frame* — which is what makes
        it hard to notice: the chrome still looks logged in."""
        session = self._session()
        if session is None:
            self._html(self._login_form("SESSION EXPIRED — sign in again"))
            return None
        return session

    # -- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        global INTERSTITIAL_ODDS
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path in ("/", "/index.htm"):
            return self._frameset()
        if path == "/nav":
            return self._nav()
        if path == "/img/btn.gif":
            return self._gif()
        if path == "/content":
            return self._html(self._login_form())
        if path == "/menu":
            return self._menu()
        if path == "/lookup":
            return self._lookup_form()
        if path == "/member":
            return self._member(query.get("no", [""])[0])
        if path == "/transfer":
            return self._transfer_form(query.get("no", [""])[0])
        if path == "/history":
            return self._history(query.get("no", [""])[0],
                                 int(query.get("from", ["0"])[0]))
        if path == "/contact":
            return self._contact_form(query.get("no", [""])[0])
        if path == "/orders":
            return self._orders(query.get("no", [""])[0])
        if path == "/neworder":
            return self._new_order_form(query.get("no", [""])[0])
        if path == "/statement":
            return self._statement_form(query.get("no", [""])[0])
        if path == "/worklist":
            return self._worklist(query.get("state", ["OPEN"])[0])
        if path == "/workitem":
            return self._work_item(query.get("id", [""])[0])
        if path == "/audit":
            return self._audit(query.get("no", [""])[0], query.get("kind", [""])[0])
        if path == "/override":
            return self._override_form(query.get("no", [""])[0])
        if path == "/admin/expire":
            for s in SESSIONS.values():
                s["killed"] = True
            return self._text("sessions expired")
        if path == "/admin/interstitial":
            INTERSTITIAL_ODDS = float(query.get("odds", ["0"])[0])
            return self._text(f"interstitial odds now {INTERSTITIAL_ODDS}")
        if path == "/admin/hazard":
            name = query.get("name", [""])[0]
            if name not in HAZARDS:
                return self._text(f"no such hazard: {name!r}; have {sorted(HAZARDS)}")
            HAZARDS[name] = query.get("on", ["1"])[0] not in ("0", "false", "")
            return self._text(f"{name} = {HAZARDS[name]}")
        if path == "/admin/hazards":
            return self._text(json.dumps(HAZARDS))
        if path == "/admin/reset":
            INTERSTITIAL_ODDS = 0.0
            SESSIONS.clear()
            LOCKS.clear()
            OVERRIDES.clear()
            NEXT_ORDER[0] = 9000
            for live, opening in zip((MEMBERS, HISTORY, CONTACT, ORDERS, STATEMENTS,
                                      WORK, AUDIT), PRISTINE):
                fresh = copy.deepcopy(opening)
                if isinstance(live, list):
                    live[:] = fresh
                else:
                    live.clear()
                    live.update(fresh)
            for name in HAZARDS:
                HAZARDS[name] = False
            return self._text("reset")
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        path = urllib.parse.urlparse(self.path).path
        get = lambda k: (form.get(k) or [""])[0].strip()  # noqa: E731

        if path == "/signin":
            return self._signin(get("uid"), get("pwd"))
        if path == "/find":
            return self._find(get("memberno"))
        if path == "/dotransfer":
            return self._do_transfer(get("memberno"), get("amount"), get("direction"))
        if path == "/doorder":
            return self._add_order(get("memberno"), get("payee"), get("amount"), get("day"))
        if path == "/docancel":
            return self._cancel_order(get("memberno"), get("ref"))
        if path == "/dostatement":
            return self._statement(get("memberno"), get("period"))
        if path == "/doclaim":
            return self._claim(get("id"))
        if path == "/docomplete":
            return self._complete(get("id"), get("outcome"))
        if path == "/dooverride":
            return self._do_override(get("memberno"), get("code"))
        if path == "/docontact":
            return self._save_contact(get("memberno"), get("phone"), get("email"),
                                      get("postcode"))
        self.send_error(404)

    # -- hazards ---------------------------------------------------------

    @staticmethod
    def _held(memberno: str) -> bool:
        """Is this record held right now? A hold expires on its own, so waiting is
        what clears it — and the other terminal can take it again afterwards.

        The first version held the record once per reset and then left it free
        forever. A real back office holds a record whenever someone else opens
        it, not once, and a hazard that fires only once is invisible to every
        replay after the first.
        """
        now = time.monotonic()
        if LOCKS.get(memberno, 0.0) > now:
            return True                   # still held; waiting is the only answer
        if random.random() < LOCK_ODDS:
            LOCKS[memberno] = now + LOCK_SECONDS
            return True                   # the other terminal has just taken it
        return False

    # -- screens ---------------------------------------------------------

    def _frameset(self) -> None:
        """The whole application is two documents. Nothing that reads one
        document sees the other."""
        body = """<html><head><title>MERIDIAN CORE 4.2</title></head>
<frameset cols="168,*" border="1" frameborder="1">
  <frame name="navFrame" src="/nav" scrolling="no" marginwidth="0" marginheight="0">
  <frame name="workFrame" src="/content" marginwidth="0" marginheight="0">
  <noframes><body>This terminal requires frames.</body></noframes>
</frameset></html>"""
        self._send(200, "text/html", body.encode())

    def _nav(self) -> None:
        links = "".join(
            f'<tr><td><a href="{href}" target="workFrame">'
            f'<img src="/img/btn.gif" alt="{alt}" border="0" width="140" height="20"></a></td></tr>'
            for href, alt in [("/menu", "Main Menu"), ("/lookup", "Member Inquiry"),
                              ("/content", "Sign Off")])
        body = f"""<html><body bgcolor="#000080">
<table border="0" cellpadding="4" cellspacing="0"><tr><td>
<font face="MS Sans Serif" size="1" color="#ffffff"><b>MERIDIAN</b></font></td></tr>
{links}</table></body></html>"""
        self._send(200, "text/html", body.encode())

    def _login_form(self, message: str = "") -> str:
        note = (f'<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                f'<b>{message}</b></font></td></tr>') if message else ""
        return f"""<table border="1" cellpadding="0" cellspacing="0" bgcolor="#d4d0c8"><tr><td>
<table border="0" cellpadding="6" cellspacing="0"><tr><td>
<font face="MS Sans Serif" size="2"><b>TELLER SIGN ON</b></font>
<form action="/signin" method="post">
<table border="0" cellpadding="3" cellspacing="0">
{note}
{field("Operator ID", "uid")}
{field("Pass Code", "pwd", kind="password")}
<tr><td></td><td>{button("Sign On")}</td></tr>
</table></form></td></tr></table></td></tr></table>"""

    def _signin(self, uid: str, pwd: str) -> None:
        if USERS.get(uid) != pwd:
            return self._html(self._login_form("INVALID OPERATOR ID OR PASS CODE"))
        token = f"S{random.randint(10**9, 10**10 - 1)}"
        SESSIONS[token] = {"uid": uid, "requests": 0}
        self._html(self._menu_body(), cookie=token)

    def _menu(self) -> None:
        if self._require() is None:
            return
        self._html(self._menu_body())

    @staticmethod
    def _menu_body() -> str:
        return """<font face="MS Sans Serif" size="2"><b>MAIN MENU</b><br><br>
Select a function from the panel at left.<br><br>
<table border="1" cellpadding="0" cellspacing="0"><tr><td>
<table border="0" cellpadding="6" cellspacing="0"><tr><td>
<font face="MS Sans Serif" size="2">
Operator signed on.<br>Branch: MAIN<br>Terminal: 04
</font></td></tr></table></td></tr></table></font>"""

    def _lookup_form(self, message: str = "") -> None:
        if self._require() is None:
            return
        note = (f'<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                f'<b>{message}</b></font></td></tr>') if message else ""
        # Three tables deep, label in a <b> in a <td>, input with no id.
        self._html(f"""<font face="MS Sans Serif" size="2"><b>MEMBER INQUIRY</b></font><br><br>
<table border="1" cellpadding="0" cellspacing="0"><tr><td>
<table border="0" cellpadding="8" cellspacing="0"><tr><td>
<form action="/find" method="post">
<table border="0" cellpadding="3" cellspacing="0">
{note}
{field("Member No.", "memberno")}
<tr><td></td><td>{button("Inquire")}</td></tr>
</table></form></td></tr></table></td></tr></table>""")

    def _find(self, memberno: str) -> None:
        if self._require() is None:
            return
        if not memberno.isdigit():
            return self._lookup_form("MEMBER NO. MUST BE NUMERIC")
        if memberno not in MEMBERS:
            return self._lookup_form(f"NO MEMBER ON FILE FOR {memberno}")
        self._member(memberno)

    def _member(self, memberno: str) -> None:
        if self._require() is None:
            return
        member = MEMBERS.get(memberno)
        if member is None:
            return self._lookup_form(f"NO MEMBER ON FILE FOR {memberno}")
        if member["restricted"]:
            return self._html("""<font face="MS Sans Serif" size="2" color="#800000">
<b>NOT AUTHORISED</b><br><br>This record is restricted. Refer to a supervisor.
</font>""")

        if HAZARDS["lock"] and self._held(memberno):
            # No control to press. The record frees itself, and the only correct
            # response is to wait and ask again.
            return self._html("""<font face="MS Sans Serif" size="2" color="#800000">
<b>RECORD IN USE BY ANOTHER TERMINAL</b><br><br>
The record is held by terminal 11. Retry shortly.</font>""")

        if random.random() < INTERSTITIAL_ODDS:
            # A known interstitial: the operator has to acknowledge it and the
            # real screen is one click further on.
            return self._html(f"""<font face="MS Sans Serif" size="2">
<b>NOTICE</b><br><br>Scheduled maintenance 02:00-04:00.<br><br>
<a href="/member?no={memberno}"><img src="/img/btn.gif" alt="Acknowledge"
 border="0" width="78" height="22"></a></font>""")

        rows = "".join(
            f'<tr><td align="right" bgcolor="#c0c0c0"><font face="MS Sans Serif" size="2">'
            f'<b>{label}</b></font></td>'
            f'<td><font face="MS Sans Serif" size="2">{value}</font></td></tr>'
            for label, value in [
                ("Member No.", memberno), ("Name", member["name"]),
                ("Status", member["status"]), ("Branch", member["branch"]),
                ("Savings Bal.", member["savings"]), ("Checking Bal.", member["checking"]),
            ])
        self._html(f"""<font face="MS Sans Serif" size="2"><b>MEMBER RECORD</b></font><br><br>
<table border="1" cellpadding="0" cellspacing="0"><tr><td>
<table border="0" cellpadding="6" cellspacing="0"><tr><td>
<table border="0" cellpadding="3" cellspacing="1">{rows}</table>
</td></tr></table></td></tr></table><br>
<a href="/transfer?no={memberno}"><img src="/img/btn.gif" alt="Transfer Funds"
 border="0" width="78" height="22"></a>
<a href="/history?no={memberno}"><img src="/img/btn.gif" alt="Posted Items"
 border="0" width="78" height="22"></a>
<a href="/contact?no={memberno}"><img src="/img/btn.gif" alt="Contact Details"
 border="0" width="78" height="22"></a>
<a href="/orders?no={memberno}"><img src="/img/btn.gif" alt="Standing Orders"
 border="0" width="78" height="22"></a>
<a href="/statement?no={memberno}"><img src="/img/btn.gif" alt="Statement Enquiry"
 border="0" width="78" height="22"></a>
<a href="/audit?no={memberno}"><img src="/img/btn.gif" alt="Audit Trail"
 border="0" width="78" height="22"></a>""")

    def _history(self, memberno: str, start: int = 0) -> None:
        """Posted items — a table of rows rather than a single field."""
        if self._require() is None:
            return
        if HAZARDS["slow"]:
            # A mainframe screen that takes its time. Anything that waits a
            # fixed 1.5s for the page to settle decides it already has.
            time.sleep(2.5)
        member = MEMBERS.get(memberno)
        if member is None:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        if member["restricted"]:
            return self._html('<font face="MS Sans Serif" size="2" color="#800000">'
                              "<b>NOT AUTHORISED</b><br><br>This record is restricted."
                              "</font>")

        items = HISTORY.get(memberno, [])
        if not items:
            return self._html('<font face="MS Sans Serif" size="2"><b>POSTED ITEMS</b>'
                              "<br><br>Member " + memberno +
                              "<br><br>NO ITEMS POSTED IN PERIOD.</font>")

        shown = items[start:start + PAGE_SIZE] if HAZARDS["paging"] else items
        rows = "".join(
            '<tr><td><font face="MS Sans Serif" size="2">' + d + "</font></td>"
            '<td><font face="MS Sans Serif" size="2">' + k + "</font></td>"
            '<td align="right"><font face="MS Sans Serif" size="2">' + a + "</font></td></tr>"
            for d, k, a in shown)

        more = ""
        if HAZARDS["paging"] and start + PAGE_SIZE < len(items):
            # The rest of the answer is behind a key. A flow that reads the screen
            # once reads part of the answer and has no way to know it.
            more = ('<br><font face="MS Sans Serif" size="2" color="#800000">'
                    "<b>MORE ITEMS TO FOLLOW</b></font><br>"
                    '<a href="/history?no=' + memberno + "&from=" +
                    str(start + PAGE_SIZE) + '"><img src="/img/btn.gif" alt="Next Items" '
                    'border="0" width="78" height="22"></a>')
        self._html(
            '<font face="MS Sans Serif" size="2"><b>POSTED ITEMS</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="6" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="3" cellspacing="1">'
            '<tr bgcolor="#c0c0c0">'
            '<td><font face="MS Sans Serif" size="2"><b>Date</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Type</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Amount</b></font></td></tr>'
            + rows + "</table><br>"
            '<font face="MS Sans Serif" size="2">Items shown: ' + str(len(shown)) +
            " of " + str(len(items)) + "</font>" + more +
            "</td></tr></table></td></tr></table>")

    def _contact_form(self, memberno: str, message: str = "") -> None:
        """A form with rules of its own: a phone length, an address shape."""
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        current = CONTACT[memberno]
        note = ('<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                "<b>" + message + "</b></font></td></tr>") if message else ""
        self._html(
            '<font face="MS Sans Serif" size="2"><b>CONTACT DETAILS</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="8" cellspacing="0"><tr><td>'
            '<form action="/docontact" method="post">'
            '<input type="hidden" name="memberno" value="' + memberno + '">'
            '<table border="0" cellpadding="3" cellspacing="0">' + note +
            '<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Member No.</b>'
            '</font></td><td><font face="MS Sans Serif" size="2">' + memberno +
            "</font></td></tr>" +
            field("Telephone", "phone", current["phone"]) +
            field("E-mail", "email", current["email"]) +
            field("Post Code", "postcode", current["postcode"]) +
            "<tr><td></td><td>" + button("Save Details") + "</td></tr>"
            "</table></form></td></tr></table></td></tr></table>")

    def _save_contact(self, memberno: str, phone: str, email: str, postcode: str) -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        digits = phone.replace(" ", "")
        if not digits.isdigit() or len(digits) < 10:
            return self._contact_form(memberno, "TELEPHONE MUST BE AT LEAST 10 DIGITS")
        if "@" not in email or "." not in email.split("@")[-1]:
            return self._contact_form(memberno, "E-MAIL ADDRESS NOT VALID")
        if len(postcode.strip()) < 5:
            return self._contact_form(memberno, "POST CODE NOT RECOGNISED")

        if HAZARDS["truncate"]:
            # The field keeps five characters and says nothing about it. The flow
            # typed the right value, the screen reports a different one, and
            # "what I sent is not what came back" is not the same failure as
            # "the flow is broken".
            postcode = postcode[:5]

        CONTACT[memberno] = {"phone": phone, "email": email, "postcode": postcode}
        self._html(
            '<font face="MS Sans Serif" size="2"><b>DETAILS AMENDED</b><br><br>'
            '<table border="1" cellpadding="6" cellspacing="0"><tr><td>'
            '<font face="MS Sans Serif" size="2">Member ' + memberno +
            " contact details updated.<br>Telephone " + phone + " &nbsp; Post Code " +
            postcode + "</font></td></tr></table></font>")

    # -- standing orders: a list you add to and cancel from ----------------

    def _orders(self, memberno: str, message: str = "") -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        orders = ORDERS.get(memberno, [])
        note = ('<font face="MS Sans Serif" size="2" color="#800000"><b>' + message +
                "</b></font><br><br>") if message else ""

        if not orders:
            rows = ('<tr><td colspan="5"><font face="MS Sans Serif" size="2">'
                    "NO STANDING ORDERS ON FILE.</font></td></tr>")
        else:
            rows = "".join(
                "<tr>"
                '<td><font face="MS Sans Serif" size="2">' + o["ref"] + "</font></td>"
                '<td><font face="MS Sans Serif" size="2">' + o["payee"] + "</font></td>"
                '<td align="right"><font face="MS Sans Serif" size="2">' + o["amount"] +
                "</font></td>"
                '<td align="right"><font face="MS Sans Serif" size="2">' + o["day"] +
                "</font></td>"
                '<td><font face="MS Sans Serif" size="2">' + o["status"] + "</font></td>"
                "</tr>" for o in orders)

        active = sum(1 for o in orders if o["status"] == "ACTIVE")
        self._html(
            note + '<font face="MS Sans Serif" size="2"><b>STANDING ORDERS</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="6" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="3" cellspacing="1"><tr bgcolor="#c0c0c0">'
            '<td><font face="MS Sans Serif" size="2"><b>Reference</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Payee</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Amount</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Day</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Status</b></font></td></tr>'
            + rows + "</table><br>"
            '<font face="MS Sans Serif" size="2">Active orders: ' + str(active) +
            "</font></td></tr></table></td></tr></table><br>"
            '<a href="/neworder?no=' + memberno + '"><img src="/img/btn.gif" '
            'alt="New Order" border="0" width="78" height="22"></a>'
            '<form action="/docancel" method="post">'
            '<input type="hidden" name="memberno" value="' + memberno + '">'
            '<table border="0" cellpadding="3" cellspacing="0">'
            + field("Cancel Ref.", "ref") +
            "<tr><td></td><td>" + button("Cancel Order") + "</td></tr></table></form>")

    def _new_order_form(self, memberno: str, message: str = "") -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        note = ('<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                "<b>" + message + "</b></font></td></tr>") if message else ""
        self._html(
            '<font face="MS Sans Serif" size="2"><b>NEW STANDING ORDER</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="8" cellspacing="0"><tr><td>'
            '<form action="/doorder" method="post">'
            '<input type="hidden" name="memberno" value="' + memberno + '">'
            '<table border="0" cellpadding="3" cellspacing="0">' + note +
            '<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Member No.</b>'
            '</font></td><td><font face="MS Sans Serif" size="2">' + memberno +
            "</font></td></tr>" +
            field("Payee", "payee") + field("Amount", "amount") +
            '<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Day of Month</b>'
            '</font></td><td><select name="day">' +
            "".join('<option value="%02d">%02d</option>' % (d, d) for d in (1, 8, 15, 22)) +
            "</select></td></tr>"
            "<tr><td></td><td>" + button("Create Order") + "</td></tr>"
            "</table></form></td></tr></table></td></tr></table>")

    def _add_order(self, memberno: str, payee: str, amount: str, day: str) -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        if len(payee.strip()) < 3:
            return self._new_order_form(memberno, "PAYEE NAME TOO SHORT")
        try:
            value = float(amount.replace(",", ""))
            if value <= 0:
                raise ValueError
        except ValueError:
            return self._new_order_form(memberno, "AMOUNT NOT VALID")

        NEXT_ORDER[0] += 1
        ref = "SO-" + str(NEXT_ORDER[0])
        ORDERS.setdefault(memberno, []).append(
            {"ref": ref, "payee": payee.upper(), "amount": "{:,.2f}".format(value),
             "day": day or "01", "status": "ACTIVE"})
        self._html(
            '<font face="MS Sans Serif" size="2"><b>ORDER CREATED</b><br><br>'
            '<table border="1" cellpadding="6" cellspacing="0"><tr><td>'
            '<font face="MS Sans Serif" size="2">Reference ' + ref +
            "<br>Payee " + payee.upper() + " &nbsp; Amount {:,.2f}".format(value) +
            "<br>Member " + memberno + "</font></td></tr></table></font>")

    def _cancel_order(self, memberno: str, ref: str) -> None:
        if self._require() is None:
            return
        orders = ORDERS.get(memberno, [])
        found = next((o for o in orders if o["ref"].upper() == ref.strip().upper()), None)
        if found is None:
            return self._orders(memberno, "NO SUCH ORDER REFERENCE " + ref.upper())
        if found["status"] == "CANCELLED":
            return self._orders(memberno, "ORDER ALREADY CANCELLED")
        found["status"] = "CANCELLED"
        self._html(
            '<font face="MS Sans Serif" size="2"><b>ORDER CANCELLED</b><br><br>'
            '<table border="1" cellpadding="6" cellspacing="0"><tr><td>'
            '<font face="MS Sans Serif" size="2">Reference ' + found["ref"] +
            " is now CANCELLED.<br>Payee " + found["payee"] +
            "</font></td></tr></table></font>")

    # -- statements: a read parameterised by something other than a member ---

    def _statement_form(self, memberno: str, message: str = "") -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        note = ('<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                "<b>" + message + "</b></font></td></tr>") if message else ""
        self._html(
            '<font face="MS Sans Serif" size="2"><b>STATEMENT ENQUIRY</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="8" cellspacing="0"><tr><td>'
            '<form action="/dostatement" method="post">'
            '<input type="hidden" name="memberno" value="' + memberno + '">'
            '<table border="0" cellpadding="3" cellspacing="0">' + note +
            '<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Member No.</b>'
            '</font></td><td><font face="MS Sans Serif" size="2">' + memberno +
            "</font></td></tr>"
            '<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Period</b>'
            '</font></td><td><select name="period">' +
            "".join('<option value="' + p + '">' + p + "</option>" for p in PERIODS) +
            "</select></td></tr>"
            "<tr><td></td><td>" + button("Produce Statement") + "</td></tr>"
            "</table></form></td></tr></table></td></tr></table>")

    def _statement(self, memberno: str, period: str) -> None:
        if self._require() is None:
            return
        data = STATEMENTS.get((memberno, period.upper()))
        if data is None:
            return self._statement_form(
                memberno, "NO STATEMENT FOR PERIOD " + period.upper())
        rows = "".join(
            '<tr><td align="right" bgcolor="#c0c0c0"><font face="MS Sans Serif" size="2">'
            "<b>" + label + "</b></font></td>"
            '<td><font face="MS Sans Serif" size="2">' + data[key] + "</font></td></tr>"
            for label, key in [("Opening Bal.", "opening"), ("Total Credits", "credits"),
                               ("Total Debits", "debits"), ("Closing Bal.", "closing"),
                               ("Items", "items")])
        self._html(
            '<font face="MS Sans Serif" size="2"><b>STATEMENT ' + period.upper() +
            "</b></font><br><br>"
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="6" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="3" cellspacing="1">' + rows +
            "</table></td></tr></table></td></tr></table>")

    # -- a worklist: claim, action, complete -------------------------------

    def _worklist(self, state: str) -> None:
        if self._require() is None:
            return
        state = (state or "OPEN").upper()
        items = [w for w in WORK if w["state"] == state]
        if not items:
            return self._html('<font face="MS Sans Serif" size="2"><b>WORK QUEUE</b>'
                              "<br><br>NO ITEMS IN STATE " + state + ".</font>")
        rows = "".join(
            "<tr>"
            '<td><a href="/workitem?id=' + w["id"] + '"><font face="MS Sans Serif" '
            'size="2">' + w["id"] + "</font></a></td>"
            '<td><font face="MS Sans Serif" size="2">' + w["kind"] + "</font></td>"
            '<td><font face="MS Sans Serif" size="2">' + w["member"] + "</font></td>"
            '<td><font face="MS Sans Serif" size="2">' + w["state"] + "</font></td>"
            '<td><font face="MS Sans Serif" size="2">' + (w["by"] or "-") + "</font></td>"
            "</tr>" for w in items)
        self._html(
            '<font face="MS Sans Serif" size="2"><b>WORK QUEUE</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="6" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="3" cellspacing="1"><tr bgcolor="#c0c0c0">'
            '<td><font face="MS Sans Serif" size="2"><b>Item</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Type</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Member</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>State</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Held By</b></font></td></tr>'
            + rows + "</table><br>"
            '<font face="MS Sans Serif" size="2">Items in ' + state + ": " +
            str(len(items)) + "</font></td></tr></table></td></tr></table>")

    def _work_item(self, item_id: str, message: str = "") -> None:
        if self._require() is None:
            return
        item = next((w for w in WORK if w["id"].upper() == item_id.strip().upper()), None)
        if item is None:
            return self._html('<font face="MS Sans Serif" size="2" color="#800000">'
                              "<b>NO SUCH WORK ITEM</b><br><br>" + item_id + "</font>")
        note = ('<font face="MS Sans Serif" size="2" color="#800000"><b>' + message +
                "</b></font><br><br>") if message else ""
        rows = "".join(
            '<tr><td align="right" bgcolor="#c0c0c0"><font face="MS Sans Serif" size="2">'
            "<b>" + label + "</b></font></td>"
            '<td><font face="MS Sans Serif" size="2">' + value + "</font></td></tr>"
            for label, value in [("Item", item["id"]), ("Type", item["kind"]),
                                 ("Member No.", item["member"]), ("State", item["state"]),
                                 ("Held By", item["by"] or "-")])

        # A claimed item shows the completion form; an unclaimed one does not. The
        # order is the point: you cannot complete what you have not claimed.
        if item["state"] == "CLAIMED":
            action = ('<form action="/docomplete" method="post">'
                      '<input type="hidden" name="id" value="' + item["id"] + '">'
                      '<table border="0" cellpadding="3" cellspacing="0">'
                      '<tr><td align="right"><font face="MS Sans Serif" size="2">'
                      '<b>Outcome</b></font></td><td><select name="outcome">'
                      '<option value="VERIFIED">VERIFIED</option>'
                      '<option value="REFERRED">REFERRED</option></select></td></tr>'
                      "<tr><td></td><td>" + button("Complete Item") +
                      "</td></tr></table></form>")
        elif item["state"] == "OPEN":
            action = ('<form action="/doclaim" method="post">'
                      '<input type="hidden" name="id" value="' + item["id"] + '">'
                      + button("Claim Item") + "</form>")
        else:
            action = ('<font face="MS Sans Serif" size="2">This item is '
                      + item["state"] + " and needs no action.</font>")

        self._html(
            note + '<font face="MS Sans Serif" size="2"><b>WORK ITEM</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="6" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="3" cellspacing="1">' + rows +
            "</table></td></tr></table></td></tr></table><br>" + action)

    def _claim(self, item_id: str) -> None:
        session = self._require()
        if session is None:
            return
        item = next((w for w in WORK if w["id"].upper() == item_id.strip().upper()), None)
        if item is None:
            return self._work_item(item_id)
        if item["state"] != "OPEN":
            return self._work_item(item["id"], "ITEM IS NOT OPEN AND CANNOT BE CLAIMED")
        item["state"] = "CLAIMED"
        item["by"] = session["uid"]
        self._work_item(item["id"], "ITEM CLAIMED")

    def _complete(self, item_id: str, outcome: str) -> None:
        session = self._require()
        if session is None:
            return
        item = next((w for w in WORK if w["id"].upper() == item_id.strip().upper()), None)
        if item is None:
            return self._work_item(item_id)
        if item["state"] != "CLAIMED":
            return self._work_item(item["id"],
                                   "ITEM MUST BE CLAIMED BEFORE IT CAN BE COMPLETED")
        item["state"] = "CLOSED"
        self._html(
            '<font face="MS Sans Serif" size="2"><b>ITEM COMPLETED</b><br><br>'
            '<table border="1" cellpadding="6" cellspacing="0"><tr><td>'
            '<font face="MS Sans Serif" size="2">Item ' + item["id"] +
            " closed with outcome " + (outcome or "VERIFIED") +
            "<br>Member " + item["member"] + "</font></td></tr></table></font>")

    # -- audit trail: a list you filter ------------------------------------

    def _audit(self, memberno: str, kind: str) -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        entries = AUDIT.get(memberno, [])
        if kind:
            entries = [e for e in entries if e[2].upper() == kind.upper()]
        if not entries:
            return self._html('<font face="MS Sans Serif" size="2"><b>AUDIT TRAIL</b>'
                              "<br><br>Member " + memberno +
                              "<br><br>NO AUDIT ENTRIES MATCH.</font>")
        rows = "".join(
            "<tr>"
            '<td><font face="MS Sans Serif" size="2">' + when + "</font></td>"
            '<td><font face="MS Sans Serif" size="2">' + who + "</font></td>"
            '<td><font face="MS Sans Serif" size="2">' + what + "</font></td>"
            "</tr>" for when, who, what in entries)
        self._html(
            '<font face="MS Sans Serif" size="2"><b>AUDIT TRAIL</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="6" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="3" cellspacing="1"><tr bgcolor="#c0c0c0">'
            '<td><font face="MS Sans Serif" size="2"><b>When</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Operator</b></font></td>'
            '<td><font face="MS Sans Serif" size="2"><b>Action</b></font></td></tr>'
            + rows + "</table><br>"
            '<font face="MS Sans Serif" size="2">Entries shown: ' + str(len(entries)) +
            "</font></td></tr></table></td></tr></table>")

    # -- supervisor override: the step-up path, completable -----------------

    def _override_form(self, memberno: str, message: str = "") -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form("NO MEMBER ON FILE FOR " + memberno)
        note = ('<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                "<b>" + message + "</b></font></td></tr>") if message else ""
        self._html(
            '<font face="MS Sans Serif" size="2"><b>SUPERVISOR OVERRIDE</b></font><br><br>'
            '<table border="1" cellpadding="0" cellspacing="0"><tr><td>'
            '<table border="0" cellpadding="8" cellspacing="0"><tr><td>'
            '<form action="/dooverride" method="post">'
            '<input type="hidden" name="memberno" value="' + memberno + '">'
            '<table border="0" cellpadding="3" cellspacing="0">' + note +
            '<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Member No.</b>'
            '</font></td><td><font face="MS Sans Serif" size="2">' + memberno +
            "</font></td></tr>" +
            field("Override Code", "code", kind="password") +
            "<tr><td></td><td>" + button("Authorise") + "</td></tr>"
            "</table></form></td></tr></table></td></tr></table>")

    def _do_override(self, memberno: str, code: str) -> None:
        if self._require() is None:
            return
        if code.strip() != OVERRIDE_CODE:
            return self._override_form(memberno, "OVERRIDE CODE NOT RECOGNISED")
        OVERRIDES.add(memberno)
        self._html(
            '<font face="MS Sans Serif" size="2"><b>OVERRIDE IN FORCE</b><br><br>'
            '<table border="1" cellpadding="6" cellspacing="0"><tr><td>'
            '<font face="MS Sans Serif" size="2">Member ' + memberno +
            " is authorised for one transfer above the terminal limit."
            "</font></td></tr></table></font>")

    def _transfer_form(self, memberno: str, message: str = "",
                       amount: str = "") -> None:
        if self._require() is None:
            return
        if memberno not in MEMBERS:
            return self._lookup_form(f"NO MEMBER ON FILE FOR {memberno}")
        note = (f'<tr><td colspan="2"><font face="MS Sans Serif" size="2" color="#800000">'
                f'<b>{message}</b></font></td></tr>') if message else ""
        self._html(f"""<font face="MS Sans Serif" size="2"><b>FUNDS TRANSFER</b></font><br><br>
<table border="1" cellpadding="0" cellspacing="0"><tr><td>
<table border="0" cellpadding="8" cellspacing="0"><tr><td>
<form action="/dotransfer" method="post">
<input type="hidden" name="memberno" value="{memberno}">
<table border="0" cellpadding="3" cellspacing="0">
{note}
<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Member No.</b></font></td>
<td><font face="MS Sans Serif" size="2">{memberno}</font></td></tr>
{field("Amount", "amount", amount)}
<tr><td align="right"><font face="MS Sans Serif" size="2"><b>Direction</b></font></td>
<td><select name="direction">
<option value="S2C">Savings to Checking</option>
<option value="C2S">Checking to Savings</option></select></td></tr>
<tr><td></td><td>{button("Post Transfer")}</td></tr>
</table></form></td></tr></table></td></tr></table>""")

    def _do_transfer(self, memberno: str, amount: str, direction: str) -> None:
        if self._require() is None:
            return
        try:
            value = float(amount.replace(",", ""))
            if value <= 0:
                raise ValueError
        except ValueError:
            return self._transfer_form(memberno, "AMOUNT NOT VALID")

        member = MEMBERS.get(memberno)
        if member is None:
            return self._lookup_form(f"NO MEMBER ON FILE FOR {memberno}")

        if HAZARDS["stepup"] and value > STEPUP_LIMIT and memberno in OVERRIDES:
            OVERRIDES.discard(memberno)      # one transfer per authorisation
        elif HAZARDS["stepup"] and value > STEPUP_LIMIT:
            # No amount of retrying clears this and there is nothing to click.
            # A person has to authorise it, which is the one condition where the
            # right move is to stop and ask.
            return self._html("""<font face="MS Sans Serif" size="2" color="#800000">
<b>SUPERVISOR AUTHORISATION REQUIRED</b><br><br>
Transfers above 1,000.00 require a supervisor at this terminal.</font>""")

        if HAZARDS["confirm"]:
            session = self._session() or {}
            pending = session.get("pending")
            if pending != (memberno, amount, direction):
                # First press does not post. The screen it returns carries the
                # same control, so a flow recorded in one pass stops here
                # looking like it succeeded.
                session["pending"] = (memberno, amount, direction)
                return self._transfer_form(
                    memberno, "PRESS POST TRANSFER AGAIN TO CONFIRM", amount=amount)
            session.pop("pending", None)

        source = "savings" if direction == "S2C" else "checking"
        target = "checking" if direction == "S2C" else "savings"
        available = float(member[source].replace(",", ""))
        if value > available:
            return self._transfer_form(memberno, "INSUFFICIENT AVAILABLE BALANCE")

        member[source] = f"{available - value:,.2f}"
        member[target] = f"{float(member[target].replace(',', '')) + value:,.2f}"
        # Posting appends to the ledger, so a flow that submits twice leaves two
        # rows. Until now a double submit was something we reasoned about; now it
        # is something the application records.
        HISTORY.setdefault(memberno, []).insert(
            0, ("22/09", "TFR OUT" if direction == "S2C" else "TFR IN", f"{value:,.2f}"))
        self._html(f"""<font face="MS Sans Serif" size="2"><b>TRANSFER POSTED</b><br><br>
<table border="1" cellpadding="6" cellspacing="0"><tr><td>
<font face="MS Sans Serif" size="2">
Reference 8841-{random.randint(1000, 9999)}<br>
Member {memberno} &nbsp; Amount {value:,.2f}<br>
New Savings Bal. {member['savings']} &nbsp; New Checking Bal. {member['checking']}
</font></td></tr></table></font>""")

    # -- plumbing --------------------------------------------------------

    def _html(self, body: str, cookie: str | None = None) -> None:
        self._send(200, "text/html", page("MERIDIAN CORE 4.2", body), cookie)

    def _text(self, message: str) -> None:
        self._send(200, "text/plain", message.encode())

    def _gif(self) -> None:
        """A 1x1 grey GIF. Controls are images, and an image needs bytes."""
        data = bytes.fromhex("47494638396101000100800000c0c0c000000021f9040100000"
                             "02c00000000010001000002024401003b")
        self._send(200, "image/gif", data)

    def _send(self, code: int, ctype: str, body: bytes, cookie: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", f"MCSESS={cookie}; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"MERIDIAN CORE 4.2 on http://localhost:{port}   (teller tlr01 / vault)")
    print(f"  members: {', '.join(MEMBERS)}   40113 is restricted")
    print(f"  GET /admin/expire to expire every session mid-flow")
    print("  GET /admin/hazard?name=X&on=1  hazards: " + ", ".join(sorted(HAZARDS)))
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
