"""Walk the application once and write down what is verifiably there.

    .venv/bin/python tools/map_app.py [--app http://localhost:8090]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from understudy.loop.mapper import build_map
from understudy.plan import self_check, single_use_values
from understudy.sitemap import save, scrub


async def main(app: str, product: str) -> int:
    sitemap, log = await build_map(
        base_url=app, product=product,
        credentials={"uid": "tlr01", "pwd": "vault"})

    # Nothing the surveyor was given to sign on with may survive into the map.
    if (removed := scrub(sitemap, {"uid": "tlr01", "pwd": "vault"})):
        print(f"  scrubbed credentials the survey recorded as field examples: "
              f"{', '.join(removed)}")
    path = save(sitemap)
    print(f"\n{sitemap.summary()}\n")
    for screen in sitemap.screens:
        verified = sum(1 for c in screen.controls if c.verified)
        print(f"  {screen.id:<22} {screen.identifies_by[:38]!r:<42}"
              f" {verified}/{len(screen.controls)} controls, {len(screen.values)} values")
    print()
    for m in sorted(sitemap.messages, key=lambda m: m.means):
        how = m.control or (f"{m.seconds:g}s" if m.clears_by == "wait" else "")
        print(f"  {m.means:<13} {m.clears_by:<15} {m.text[:44]!r} {how}")
    print(f"\n  entry: {len(sitemap.entry)} steps -> {sitemap.entry_lands_on or '?'}")

    # Ask the map to do the thing it exists for, before anything relies on it.
    # Three maps in a row were saved looking complete and were not, and each time
    # the first symptom was a task failing for an unrelated-looking reason two
    # steps downstream.
    can, cannot = self_check(sitemap)
    single = single_use_values(sitemap)
    print(f"\n  self-check: {len(can)}/{len(can) + len(cannot)} readable screens can be "
          f"planned to")
    for line in cannot:
        print(f"    cannot: {line}")
    if single:
        print(f"    values recorded by their own contents, so usable for one record "
              f"only: {single}")

    print(f"\n  written to {path}   evidence in {log.dir}")
    # A map that cannot be planned from is not a map, and saying so here is cheaper
    # than discovering it from a task failure.
    return 0 if len(can) > len(cannot) and not single else 2


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--app", default="http://localhost:8090")
    p.add_argument("--product", default="MERIDIAN CORE 4.2")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.app, a.product)))
