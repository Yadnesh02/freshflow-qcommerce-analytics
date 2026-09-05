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
    """One synthetic setting. Both arms, because a 2x2 needs a control group."""
    days = list(range(first, last + 1))
    rows = []
    for treated in (False, True):
        for store in range(4):
            for day in days:
                post = day >= 0
                # a small treatment effect so the estimator has something to find
                expired = 10.0 - (2.0 if (post and treated) else 0.0)
                rows.append(
                    {
                        "param": param,
                        "value": float(value),
                        "seed": seed,
                        "store_id": f"{'T' if treated else 'C'}{store:02d}",
                        "day_rel": day,
                        "post": post,
                        "treated": treated,
                        "revenue": 100.0,
                        "cogs": 50.0,
                        "units_expired": expired,
                        "units_lost": 5.0,
                        "writeoff_value": expired * 4.0,
                    }
                )
    return pd.DataFrame(rows)


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


def test_a_single_setting_reports_not_swept_rather_than_a_failure(tmp_path, capsys) -> None:
    """One value is not evidence against a finding, and must not print as one.

    A parameter given a single setting produces a table identical in shape to a
    swept one. Printing NO against it says the conclusion failed, when in fact
    nothing was varied - which is how a gap in the experiment gets filed as a
    result. The first real sweep did this for dispersion_scale and
    lead_time_scale, both of which carry one setting.
    """
    from analytics.experiment.sensitivity import report

    frames = []
    for value in (0.0, 25.0):
        for seed in (1, 2, 3):
            frames.append(_run("disposal_cost", value, seed, -5, 5))
    for seed in (1, 2, 3):
        frames.append(_run("lead_time_scale", 1.3, seed, -5, 5))
    frame = pd.concat(frames, ignore_index=True)
    frame["margin"] = frame["revenue"] - frame["cogs"]

    report(frame)
    out = capsys.readouterr().out
    swept, single = out.split("--- lead_time_scale ---")
    assert "not swept" not in swept, "a genuinely swept parameter was called not swept"
    assert "not swept" in single, "a single-setting parameter was reported as a failed finding"
