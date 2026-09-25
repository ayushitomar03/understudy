"""Draw what happened across orchestration attempts.

Reads the orchestration records written by understudy.orchestrator and emits one
SVG. Two panels, because the interesting story is not in the score alone:

  * score per attempt, with promotions marked — whether the champion improved
  * turns and steps per attempt — whether the loop got cheaper and the
    capability got leaner

Written as an inline SVG with no dependencies so it can be dropped straight into
a write-up.

    .venv/bin/python tools/chart_orchestrations.py [out.svg]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RUNS = Path("evidence/orchestrations")

INK, MUTED, RULE = "#16232b", "#6a7f88", "#d3dee2"
SERIES = ["#1a6764", "#8a4b8f", "#b4610a"]
GOOD, BAD = "#2f6a4d", "#9c3a2b"

W, H = 900, 596
PAD_L, PAD_R, PAD_T = 64, 24, 68
PANEL_H = 168
GAP = 76


def load() -> list[dict]:
    """Newest record per capability id."""
    latest: dict[str, dict] = {}
    for path in sorted(RUNS.glob("*.json")):
        if "champion" in path.name:
            continue
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if record.get("attempts") and record["capability_id"].startswith("bench."):
            latest[record["capability_id"]] = record
    return list(latest.values())


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def panel(records: list[dict], top: int, key, label: str, y_max: float,
          fmt=lambda v: f"{v:g}") -> list[str]:
    """One plot: `key(attempt)` per attempt, a line per task."""
    out: list[str] = []
    base = top + PANEL_H
    width = W - PAD_L - PAD_R
    longest = max(len(r["attempts"]) for r in records)
    step = width / max(longest - 1, 1)

    out.append(f'<text x="{PAD_L}" y="{top - 14}" font-size="12.5" font-weight="600" '
               f'fill="{INK}">{esc(label)}</text>')

    for i in range(5):
        value = y_max * i / 4
        y = base - (value / y_max) * PANEL_H
        out.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                   f'stroke="{RULE}" stroke-dasharray="2 4"/>')
        out.append(f'<text x="{PAD_L - 8}" y="{y + 3:.1f}" font-size="9.5" text-anchor="end" '
                   f'fill="{MUTED}">{fmt(value)}</text>')

    for n in range(longest):
        x = PAD_L + n * step
        out.append(f'<text x="{x:.1f}" y="{base + 16}" font-size="9.5" text-anchor="middle" '
                   f'fill="{MUTED}">{n + 1}</text>')

    for index, record in enumerate(records):
        colour = SERIES[index % len(SERIES)]
        points = []
        for n, attempt in enumerate(record["attempts"]):
            value = key(attempt) or 0
            x = PAD_L + n * step
            y = base - min(value / y_max, 1.0) * PANEL_H
            points.append((x, y, attempt))
        out.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points) +
                   f'" fill="none" stroke="{colour}" stroke-width="2"/>')
        for x, y, attempt in points:
            promoted = attempt.get("promoted")
            cold = attempt.get("cold", True)
            if cold:
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="white" '
                           f'stroke="{colour}" stroke-width="1.8"/>')
            else:
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}"/>')
            if promoted:
                out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8.5" fill="none" '
                           f'stroke="{colour}" stroke-width="1.4" opacity="0.55"/>')
            if attempt.get("outputs_correct") is False:
                out.append(f'<text x="{x:.1f}" y="{y - 13:.1f}" font-size="9" '
                           f'text-anchor="middle" fill="{BAD}">wrong</text>')
    return out


def main(out_path: str = "evidence/orchestrations/iterations.svg") -> int:
    records = load()
    if not records:
        print("no orchestration records found")
        return 1

    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="IBM Plex Sans, system-ui, sans-serif">',
             f'<rect width="{W}" height="{H}" fill="white"/>',
             f'<text x="{PAD_L}" y="24" font-size="15" font-weight="600" fill="{INK}">'
             f'Five attempts per task, each graded by replaying what it produced</text>',
             f'<text x="{PAD_L}" y="40" font-size="10.5" fill="{MUTED}">'
             f'All 15 attempts returned the correct answer and scored 1.00, so score is not '
             f'plotted. What changes is cost and quality.</text>']

    # Score is deliberately not plotted: every one of these attempts scored
    # 1.00, so the panel would be a flat line and imply the loop has nothing
    # left to improve. What varies is what it costs to get there, and how good
    # the artifact is when you do.
    turn_max = max((a["turns"] for r in records for a in r["attempts"]), default=20)
    parts += panel(records, PAD_T, lambda a: a["turns"],
                   "turns the loop needed  —  filled = resumed from champion, hollow = cold start",
                   max(turn_max, 5), fmt=lambda v: f"{v:.0f}")
    step_max = max((a.get("steps") or 0 for r in records for a in r["attempts"]), default=12)
    parts += panel(records, PAD_T + PANEL_H + GAP, lambda a: a.get("steps") or 0,
                   "steps in the capability it produced  —  fewer is better",
                   max(step_max, 5), fmt=lambda v: f"{v:.0f}")

    legend_y = H - 34
    for index, record in enumerate(records):
        colour = SERIES[index % len(SERIES)]
        x = PAD_L + index * 276
        name = record["capability_id"].replace("bench.", "")
        best = max(_score(a) for a in record["attempts"])
        parts.append(f'<rect x="{x}" y="{legend_y - 9}" width="10" height="10" rx="2" fill="{colour}"/>')
        parts.append(f'<text x="{x + 16}" y="{legend_y}" font-size="10.5" fill="{INK}">'
                     f'{esc(name)}</text>')
        parts.append(f'<text x="{x + 16}" y="{legend_y + 14}" font-size="9.5" fill="{MUTED}">'
                     f'best {best:.2f} · {len(record["attempts"])} attempts</text>')

    parts.append(f'<text x="{PAD_L}" y="{H - 8}" font-size="9.5" fill="{MUTED}">'
                 f'ringed points were promoted to champion. hollow = started cold, '
                 f'filled = resumed from the champion. attempt number on the x axis.</text>')
    parts.append("</svg>")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("\n".join(parts))
    print(f"wrote {out_path}")
    for record in records:
        scores = [f"{_score(a):.2f}" for a in record["attempts"]]
        turns = [str(a["turns"]) for a in record["attempts"]]
        steps = [str(a.get("steps") or "-") for a in record["attempts"]]
        print(f"  {record['capability_id']}")
        print(f"     score {scores}\n     turns {turns}\n     steps {steps}")
    return 0


def _score(attempt: dict) -> float:
    if not attempt.get("produced"):
        return 0.0
    base = attempt.get("replay_score") or 0.0
    if attempt.get("replay_outcome") != "complete":
        return base * 0.5
    if attempt.get("outputs_correct") is False:
        return base * 0.6
    return base


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
