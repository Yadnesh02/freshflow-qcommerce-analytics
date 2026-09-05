"""The sensitivity table refuses to compare things that are not comparable (task S5.3).

Two ways this report can be confidently wrong, and both are quiet:

  1. A run left over from an earlier configuration joins the directory and
     shifts one column. A shifted column reads as a parameter response, which is
     the only conclusion the table exists to draw.
  2. `survives` says yes on evidence that does not support it, turning "we did
     not measure this well enough" into "this finding is robust".
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from analytics.experiment.sensitivity import load, survives


@dataclass
class FakeEstimate:
    effect: float
    p_value: float


def _run(param: str, value: float, seed: int, first: int, last: int) -> pd.DataFrame:
    days = range(first, last + 1)
    return pd.DataFrame(
        {
            "param": param,
            "value": float(value),
            "seed": seed,
            "store_id": "S00",
            "day_rel": list(days),
            "post": [d >= 0 for d in days],
            "revenue": 1.0,
            "cogs": 0.5,
            "units_expired": 1.0,
            "treated": True,
        }
    )


def test_runs_of_different_lengths_are_refused(tmp_path) -> None:
    """A stale file from another horizon must stop the table, not skew it.

    Found by leaving a 60-day smoke run beside a set of 90-day ones: the report
    printed a three-column table in which one column had a different post-period
    and one seed, and nothing in the output said so.
    """
    _run("disposal_cost", 0, 1, -30, 59).to_parquet(tmp_path / "a.parquet", index=False)
    _run("disposal_cost", 25, 1, -20, 39).to_parquet(tmp_path / "b.parquet", index=False)

    with pytest.raises(SystemExit) as excinfo:
        load(tmp_path)
    assert "different lengths" in str(excinfo.value)


def test_matching_runs_load_fine(tmp_path) -> None:
    """The guard must not refuse the case it exists to permit."""
    _run("disposal_cost", 0, 1, -30, 59).to_parquet(tmp_path / "a.parquet", index=False)
    _run("disposal_cost", 25, 1, -30, 59).to_parquet(tmp_path / "b.parquet", index=False)
    frame = load(tmp_path)
    assert set(frame["value"]) == {0.0, 25.0}
    assert "margin" in frame.columns


def test_duplicate_files_do_not_double_count(tmp_path) -> None:
    """Downloading the per-setting artifacts next to the combined panel.

    Double-counting changes no estimate and halves the standard error, so it
    shows up as more confidence rather than a wrong number - the most flattering
    way this could break.
    """
    run = _run("disposal_cost", 0, 1, -30, 59)
    run.to_parquet(tmp_path / "a.parquet", index=False)
    run.to_parquet(tmp_path / "a_copy.parquet", index=False)
    assert len(load(tmp_path)) == len(run)


# ==================================================== the survival rule
def test_a_sign_flip_does_not_survive() -> None:
    """The case the whole table is looking for."""
    assert not survives([FakeEstimate(-5.0, 1e-6), FakeEstimate(3.0, 1e-6)])


def test_losing_significance_does_not_survive() -> None:
    """Significant at one end and not the other is a dependence, not a finding."""
    assert not survives([FakeEstimate(-5.0, 1e-6), FakeEstimate(-4.0, 0.42)])


def test_a_stable_significant_effect_survives() -> None:
    assert survives([FakeEstimate(-5.0, 1e-6), FakeEstimate(-4.0, 1e-4)])


def test_a_single_setting_never_survives() -> None:
    """One point is not a range, and must not be reported as robustness.

    A parameter swept at one value produces a table that looks identical to a
    swept one, and would claim survival on evidence that contains no variation
    at all.
    """
    assert not survives([FakeEstimate(-5.0, 1e-9)])
