"""Record the tables on every screen the map can reach. No model is called.

The survey recorded labelled values, which is what a form screen is made of. It
recorded nothing about tables, and a table is what the eleven tasks the system
cannot do are made of: how many posted items, the amount of the third one, how
many standing orders are on file. Asked for those, the planner declined —
correctly, because with only labelled values available it would have read the
cell after the column header and confidently returned row one.

So this walks the map's own routes to each screen and writes down the columns it
finds. Three things worth saying about how:

  * **It costs nothing.** The routes come from the map, the clicking is replay,
    and the columns are read off the page. There is no model in this file.

  * **It reads, never writes.** Only screens reached by navigation and clicks
    are visited, and a screen is skipped if its route needs an argument that is
    not a lookup value, so the walk cannot post a transfer to find out what the
    confirmation table looks like.

  * **Columns are taken from the page, not from a model's description of it.**
    A header is a row whose cells are all emboldened, which is how this
    application distinguishes its data tables from the tables it lays pages out
    with — a distinction that is in the markup rather than in anyone's opinion.

    .venv/bin/python tools/map_tables.py [--app http://localhost:8090]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from understudy.plan import _fill, _route                        # noqa: E402
from understudy.sitemap import SiteMap, Table, load, save        # noqa: E402
from understudy.surface.base import AfterText, RoleName          # noqa: E402
from understudy.surface.web import WebSurface                    # noqa: E402

CREDENTIALS = {"uid": "tlr01", "pwd": "vault"}
# An argument to look things up with. Any record will do: the columns of a table
# are a property of the screen, and the rows are what differ.
EXAMPLE = {"member_no": "40021", "item_id": "W-7701", "period": "SEP"}


def sign_on(surface: WebSurface, sitemap: SiteMap) -> str:
    """Start from the sign-on screen every time. Returns why it failed, or ''.

    Replaying the entry steps while already signed on types into fields that are
    not there, fails quietly, and leaves the walk on whichever screen the last
    one ended at — which is how every screen came back holding no tables.
    """
    surface.navigate(sitemap.app + "/content")       # what Sign Off points at
    for step in sitemap.entry:
        if step.action == "navigate":
            done = surface.navigate(sitemap.app + (step.url or "/"))
        elif step.action == "type" and step.target:
            secret = step.value.from_secret if step.value else None
            done = surface.type(step.target.target, CREDENTIALS.get(secret, ""))
        elif step.action == "click" and step.target:
            done = surface.click(step.target.target)
        else:
            continue
        if not done.ok:
            return f"signing on: {step.action} failed — {done.error[:60]}"
    return ""


def walk_to(surface: WebSurface, sitemap: SiteMap, screen_id: str) -> str:
    """Follow the map to a screen. Returns why it could not be reached, or ''."""
    landing = sitemap.entry_lands_on or (sitemap.screens[0].id if sitemap.screens else "")
    route = _route(sitemap, landing, screen_id)
    if route is None:
        return "no route on the map"
    for screen, control_name in route:
        for locator, name, action in _fill(screen, EXAMPLE, set()):
            value = EXAMPLE.get(name, "")
            done = (surface.select(locator.target, value) if action == "select"
                    else surface.type(locator.target, value))
            if not done.ok:
                return f"could not fill {name!r} on {screen.id!r}: {done.error[:60]}"
        control = screen.control(control_name)
        if control is None:
            return f"the map names a control {control_name!r} that {screen.id!r} has not got"
        # A click that silently fails leaves the walk on the menu reporting
        # success, and every screen then honestly records no tables.
        clicked = surface.click(control.locator.target)
        if not clicked.ok:
            return f"could not click {control_name!r} on {screen.id!r}: {clicked.error[:60]}"
        # Let the page arrive before asking what is on it. Without this the walk
        # looks for the next screen's links while the last click is still in
        # flight, and every screen beyond the second is reported unreachable.
        surface.observe()
    return ""


def stated_counts(surface: WebSurface) -> list[str]:
    """The labels of lines where this screen states a count of its own.

    `Items shown: 3 of 3` and `Active orders: 0` are the application doing the
    arithmetic, and it is better arithmetic than counting rows: it survives a
    truncated table, and `Active orders` counts what active means, which a table
    holding one cancelled order does not.
    """
    labels: list[str] = []
    for _, frame in surface._documents():
        try:
            text = " ".join((frame.locator("body").inner_text() or "").split())
        except Exception:
            continue
        # A label, not the last cell of the table above it: one capitalised word
        # and up to two lowercase ones. Matching capitals greedily read
        # "ACTIVE Active orders" off the row sitting above the line.
        for label in re.findall(r"([A-Z][a-z]+(?: [a-z]+){0,2})\s*:\s*\d+", text):
            if label.strip() and label.strip() not in labels:
                labels.append(label.strip())
    return labels


def main(app: str) -> int:
    sitemap = load(app)
    if sitemap is None:
        print(f"no map for {app}")
        return 1

    surface = WebSurface()
    recorded = skipped = 0
    try:
        for screen in sitemap.screens:
            why = sign_on(surface, sitemap) or walk_to(surface, sitemap, screen.id)
            if why:
                print(f"  {screen.id:<18} skipped — {why}")
                skipped += 1
                continue
            stated = stated_counts(surface)
            tables = [Table(columns=columns, rows_when_mapped=rows,
                            summary=stated.pop(0) if stated else "")
                      for columns, rows in surface.tables()]
            screen.tables = tables
            recorded += len(tables)
            shown = "; ".join(", ".join(t.columns) + (f"  [counted by {t.summary!r}]"
                                                      if t.summary else "")
                              for t in tables) or "no tables"
            print(f"  {screen.id:<18} {shown}")
    finally:
        surface.close()

    path = save(sitemap)
    print(f"\n  {recorded} tables on {sum(1 for s in sitemap.screens if s.tables)} screens, "
          f"{skipped} screens unreachable · written to {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="http://localhost:8090")
    raise SystemExit(main(parser.parse_args().app))
