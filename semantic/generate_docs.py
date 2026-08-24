"""Generate docs/metrics.md from the metric registry.

The dictionary is generated, never hand-written, so it cannot drift from the
definitions the API and dashboard actually serve. Run via:

    python tasks.py docs

CI regenerates it and fails if the committed copy is stale, which keeps the
two in lockstep without anyone having to remember.
"""

from __future__ import annotations

import sys
from pathlib import Path

from semantic.registry import Metric, Registry, load_registry

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "metrics.md"

FAMILY_ORDER = [
    "executive",
    "wastage",
    "availability",
    "forecast",
    "pricing",
    "promotion",
    "merchandising",
    "customer",
    "data_quality",
]

FAMILY_BLURB = {
    "executive": "The P&L spine. Every other family rolls up into these.",
    "wastage": "Problem P1 - perishables written off at 100% of landed cost.",
    "availability": "Problems P3 and P4 - stock in the wrong place, or not there at all.",
    "forecast": "Problem P4 - you cannot mark down your way out of a bad purchase order.",
    "pricing": "Problem P2 - flat discount ladders destroy margin and still waste stock.",
    "promotion": "Problem P6 - the deal slot as a system, not a gimmick.",
    "merchandising": "Problem P5 - private label mix, net of cannibalisation.",
    "customer": "Problem P7 - whether deals build habit or train discount-seekers.",
    "data_quality": "Problem P8 - one bad number kills adoption of everything above.",
}

ARROW = {
    "up_is_good": "higher is better",
    "down_is_good": "lower is better",
    "neutral": "context-dependent",
}


def _formula(m: Metric) -> str:
    if m.is_ratio:
        return f"`{m.numerator}`<br>&nbsp;&nbsp;÷&nbsp;`{m.denominator}`"
    return f"`{m.expression}`"


def _bounds(m: Metric) -> str:
    for t in m.tests:
        if "between" in t:
            lo, hi = t["between"]
            return f"{lo} to {hi}"
    return "—"


def render(reg: Registry) -> str:
    lines: list[str] = [
        "# Metric Dictionary",
        "",
        "> **Generated file — do not edit.**",
        "> Source of truth is [`semantic/metrics.yml`](../semantic/metrics.yml).",
        "> Regenerate with `python tasks.py docs`.",
        "",
        f"**{len(reg.metrics)} metrics** across **{len(reg.families)} families**, "
        f"sliceable by **{len(reg.dimensions)} dimensions**.",
        "",
        f"- **North star:** `{reg.north_star}` — {reg.metric(reg.north_star).label}",
        f"- **Guardrail:** `{reg.guardrail}` — {reg.metric(reg.guardrail).label}",
        "",
        "The guardrail exists because the north star can be gamed: margin bought by "
        "starving stores of stock shows up as churn, not as success.",
        "",
        "---",
        "",
    ]

    families = FAMILY_ORDER + [f for f in reg.families if f not in FAMILY_ORDER]
    for family in families:
        metrics = sorted(reg.metrics_in_family(family), key=lambda m: m.name)
        if not metrics:
            continue
        lines += [f"## {family.replace('_', ' ').title()}", ""]
        if family in FAMILY_BLURB:
            lines += [f"*{FAMILY_BLURB[family]}*", ""]

        for m in metrics:
            badge = " · **north star**" if m.name == reg.north_star else ""
            badge += " · **guardrail**" if m.name == reg.guardrail else ""
            lines += [
                f"### `{m.name}` — {m.label}{badge}",
                "",
                m.description,
                "",
                "| | |",
                "|---|---|",
                f"| **Formula** | {_formula(m)} |",
                f"| **Source** | `{m.source}` |",
                f"| **Grain** | {', '.join(f'`{g}`' for g in m.grain)} |",
                f"| **Format** | `{m.format}` |",
                f"| **Direction** | {ARROW.get(m.direction, m.direction)} |",
                f"| **Expected range** | {_bounds(m)} |",
                f"| **Owner** | {m.owner} |",
            ]
            if m.guarded_by:
                lines.append(f"| **Guarded by** | `{m.guarded_by}` |")
            if m.requires_experiment:
                lines.append("| **Requires** | a control arm — meaningless without a holdout |")
            lines.append("")
            if m.notes:
                lines += [f"> ⚠ {m.notes}", ""]

        lines.append("---")
        lines.append("")

    lines += [
        "## Dimensions",
        "",
        "| Dimension | Label | Key | Resolves via |",
        "|---|---|---|---|",
    ]
    for d in sorted(reg.dimensions.values(), key=lambda d: d.name):
        via = f"`{d.join['table']}`" if d.needs_join else "already on the mart"
        lines.append(f"| `{d.name}` | {d.label} | `{d.key}` | {via} |")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    reg = load_registry()
    content = render(reg)
    check = "--check" in sys.argv

    if check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != content:
            print(f"{OUT_PATH.relative_to(ROOT)} is stale - run: python tasks.py docs")
            return 1
        print(f"{OUT_PATH.relative_to(ROOT)} is up to date")
        return 0

    OUT_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(ROOT)}  ({len(reg.metrics)} metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
