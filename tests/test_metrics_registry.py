"""Schema contract for the metric registry (task S0.3).

These tests guard the promise the whole BI layer rests on: that a metric is
defined exactly once, completely, and unambiguously. A broken definition should
fail in CI, not on a dashboard.

The SQL these metrics compile to is executed against DuckDB in S3.6, once a
warehouse exists. Until then this file checks the contract, not the results.
"""

from __future__ import annotations

import re

import pytest

from semantic.registry import (
    REQUIRED_KEYS,
    VALID_DIRECTIONS,
    VALID_FORMATS,
    RegistryError,
    load_registry,
)

reg = load_registry()

METRICS = sorted(reg.metrics.values(), key=lambda m: m.name)
DIMENSIONS = sorted(reg.dimensions.values(), key=lambda d: d.name)
_ids = lambda items: [i.name for i in items]  # noqa: E731


# --------------------------------------------------------------- registry-wide
def test_registry_loads() -> None:
    assert reg.metrics, "no metrics defined"
    assert reg.dimensions, "no dimensions defined"


def test_north_star_and_guardrail_are_distinct_metrics() -> None:
    assert reg.north_star in reg.metrics
    assert reg.guardrail in reg.metrics
    assert reg.north_star != reg.guardrail, (
        "the guardrail must be a different metric from the north star, otherwise it guards nothing"
    )


def test_north_star_declares_its_guardrail() -> None:
    """GM-AWM can be gamed by starving stores of stock. Retention is what catches that."""
    assert reg.metric(reg.north_star).guarded_by == reg.guardrail


def test_every_family_is_populated() -> None:
    expected = {
        "executive",
        "wastage",
        "availability",
        "forecast",
        "pricing",
        "promotion",
        "merchandising",
        "customer",
        "data_quality",
    }
    assert expected <= set(reg.families), f"missing families: {expected - set(reg.families)}"


# --------------------------------------------------------------- per metric
@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_metric_has_all_required_keys(metric) -> None:
    for key in REQUIRED_KEYS:
        assert getattr(metric, key), f"{metric.name} has empty required key '{key}'"


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_metric_format_and_direction_are_known(metric) -> None:
    assert metric.format in VALID_FORMATS
    assert metric.direction in VALID_DIRECTIONS


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_metric_grain_resolves_to_dimensions(metric) -> None:
    for dim in metric.grain:
        assert dim in reg.dimensions, f"{metric.name} grains by undefined dimension '{dim}'"


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_ratio_metrics_never_average_a_ratio(metric) -> None:
    """Ratios must divide aggregates, not aggregate divisions.

    AVG(a / b) weights a store with three orders the same as one with three
    thousand. It is the most common silent error in a semantic layer.
    """
    if not metric.is_ratio:
        return
    for part in (metric.numerator, metric.denominator):
        assert part, f"{metric.name} is a ratio but has an empty numerator/denominator"
        assert not re.search(r"\bAVG\s*\([^)]*/", part, flags=re.I), (
            f"{metric.name} averages a per-row ratio in '{part}'"
        )


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_ratio_denominator_guards_division_by_zero(metric) -> None:
    """A denominator that can legitimately reach zero must be wrapped in NULLIF."""
    if not metric.is_ratio:
        return
    den = metric.denominator or ""
    # COUNT(*) over a grouped result is always >= 1, so it needs no guard
    if re.fullmatch(r"\s*COUNT\s*\(\s*\*\s*\)\s*", den, flags=re.I):
        return
    assert "NULLIF" in den.upper(), (
        f"{metric.name} has denominator '{den}' with no NULLIF guard - "
        "an empty slice would raise or return inf"
    )


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_metric_is_aggregated(metric) -> None:
    """Every expression must aggregate. A bare column would fan out on GROUP BY."""
    exprs = [metric.expression] if metric.expression else [metric.numerator, metric.denominator]
    agg = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX|QUANTILE_CONT|MEDIAN)\s*\(", re.I)
    for e in exprs:
        assert e and agg.search(e), f"{metric.name} has a non-aggregated expression: {e!r}"


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_description_is_a_sentence_not_a_restatement(metric) -> None:
    """A description that just repeats the label teaches the reader nothing."""
    assert len(metric.description) >= 40, f"{metric.name} description is too thin"
    normalise = lambda s: re.sub(r"[^a-z]", "", s.lower())  # noqa: E731
    assert normalise(metric.description) != normalise(metric.label)


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_percentage_metrics_are_bounded(metric) -> None:
    """Anything formatted as a percent should declare plausible bounds."""
    if metric.format != "percent_1dp":
        return
    between = [t["between"] for t in metric.tests if "between" in t]
    if not between:
        pytest.skip(f"{metric.name} has no declared bounds yet")
    lo, hi = between[0]
    assert lo >= -1.0 and hi <= 3.0, (
        f"{metric.name} bounds {between[0]} look like they are in percent units; "
        "ratios are stored as fractions and formatted on the way out"
    )


@pytest.mark.parametrize("metric", METRICS, ids=_ids(METRICS))
def test_incrementality_metrics_declare_an_experiment_dependency(metric) -> None:
    """Subsidy-per-incremental-order without a control arm is a made-up number."""
    if any(w in metric.name for w in ("incremental", "uplift")):
        assert metric.requires_experiment, (
            f"{metric.name} measures incrementality and must set requires_experiment: true"
        )


# --------------------------------------------------------------- per dimension
@pytest.mark.parametrize("dim", DIMENSIONS, ids=_ids(DIMENSIONS))
def test_dimension_join_is_well_formed(dim) -> None:
    if dim.needs_join:
        assert {"table", "join_on"} <= set(dim.join)
        assert dim.join["table"].startswith(("dim_", "mart_"))
    else:
        assert dim.key == dim.attribute, (
            f"{dim.name} has no join, so its key and attribute must be the same column"
        )


def test_every_dimension_is_used_by_at_least_one_metric_or_is_a_slicer() -> None:
    """Catch dimensions added to the registry and then forgotten."""
    used = {d for m in reg.metrics.values() for d in m.grain}
    # these exist to slice by, not to grain by - listed explicitly so the set stays deliberate
    slicers = {
        "locality",
        "store_tier",
        "subcategory",
        "brand",
        "is_private_label",
        "temp_zone",
        "month",
        "day_of_week",
        "is_festival",
        "is_monsoon",
        "is_salary_week",
        "dte_band",
        "rfm_segment",
        "promo_type",
        "funding_source",
        "supplier",
        "policy_arm",
    }
    orphans = set(reg.dimensions) - used - slicers
    assert not orphans, f"dimensions defined but never used or declared as slicers: {orphans}"


# --------------------------------------------------------------- negative tests
def test_validator_rejects_a_ratio_with_an_expression(tmp_path) -> None:
    bad = tmp_path / "metrics.yml"
    bad.write_text(
        "version: 1\nnorth_star: x\nguardrail: x\ndefaults: {owner: a, direction: up_is_good}\n"
        "metrics:\n  x:\n    label: X\n    description: 'a description long enough to pass'\n"
        "    family: executive\n    type: ratio\n    source: t\n    grain: [store]\n"
        "    format: inr\n    numerator: SUM(a)\n    denominator: SUM(b)\n    expression: SUM(c)\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="must not set 'expression'"):
        load_registry(metrics_path=bad)


def test_validator_rejects_unknown_grain(tmp_path) -> None:
    bad = tmp_path / "metrics.yml"
    bad.write_text(
        "version: 1\nnorth_star: x\nguardrail: x\ndefaults: {owner: a, direction: up_is_good}\n"
        "metrics:\n  x:\n    label: X\n    description: 'a description long enough to pass'\n"
        "    family: executive\n    type: measure\n    source: t\n    grain: [not_a_dimension]\n"
        "    format: inr\n    expression: SUM(a)\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="not present in dimensions.yml"):
        load_registry(metrics_path=bad)


# --------------------------------------------- the generated dictionary
def test_the_metric_dictionary_is_in_sync_with_the_registry() -> None:
    """docs/metrics.md is generated, and generated files drift silently.

    It is the artefact a reader meets first - linked from the README, published
    to Pages - so a stale copy is not a cosmetic problem: it is the registry
    saying one thing to CI and another to a human. Nothing forced regeneration
    until this test existed, and the file had already drifted once, still
    describing fill_rate as "meaningless without" a correction that had shipped.

    Regenerate with `python tasks.py docs`, or `python -m semantic.generate_docs`
    for just this file.
    """
    from pathlib import Path

    from semantic.generate_docs import render

    published = Path(__file__).resolve().parent.parent / "docs" / "metrics.md"
    assert published.exists(), "docs/metrics.md is missing - run `python tasks.py docs`"

    current = published.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected = render(reg).replace("\r\n", "\n")
    assert current == expected, (
        "docs/metrics.md no longer matches semantic/metrics.yml - "
        "run `python -m semantic.generate_docs` and commit the result"
    )
