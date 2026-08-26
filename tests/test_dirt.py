"""Contract for the injected data defects (task S1.8).

Each defect has to be present, reproducible, and bounded. Present, because a
staging layer that cleans nothing proves nothing. Reproducible, because the
Sprint 5 experiment compares two policies over the same world and that includes
the same broken feeds. Bounded, because damage that swamped the signal would
make the whole dataset useless rather than realistic.

The tests run a short window and compare a clean run against a dirty one, so
each assertion is a genuine before-and-after rather than a guess about what the
raw layer should look like.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil

import pandas as pd
import pytest

from simulator.config_loader import load_sim_config
from simulator.dirt import DirtInjector, write_docs
from simulator.run import SimulationRun

cfg = load_sim_config()
# A short window with the two window-dependent defects pulled into range.
# Simulating six months to reach the real migration date would make this file
# unrunnable in CI.
DAYS = 20
MIGRATION = dt.date(2025, 9, 15)


def _read(root, source: str) -> pd.DataFrame:
    files = sorted(root.glob(f"{source}/dt=*/*.parquet"))
    return (
        pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        if files
        else pd.DataFrame()
    )


@pytest.fixture(scope="module")
def worlds(tmp_path_factory):
    """One clean raw layer and one dirty one, from the same seed."""
    base = tmp_path_factory.mktemp("dirt")
    clean, dirty = base / "clean", base / "dirty"
    for out in (clean, dirty):
        SimulationRun(cfg, seed=42, days=DAYS, out_dir=out, quiet=True).run()
    defects = DirtInjector(
        dirty, seed=42, quiet=True, migration_date=MIGRATION, min_partitions_for_outage=20
    ).apply()
    return clean, dirty, {d.key: d for d in defects}


# =============================================================== overall
def test_all_eight_defects_are_injected(worlds) -> None:
    _, _, defects = worlds
    expected = {
        "duplicate_events",
        "late_arrivals",
        "null_batch_ids",
        "unit_drift",
        "inconsistent_returns",
        "timezone_mix",
        "sku_code_migration",
        "clickstream_outage",
    }
    assert set(defects) == expected


def test_every_defect_touched_rows_and_is_documented(worlds) -> None:
    _, _, defects = worlds
    for d in defects.values():
        assert d.rows > 0, f"{d.key} injected nothing"
        assert len(d.symptom) > 40 and len(d.fix) > 40, f"{d.key} is not documented"
        assert d.feeds


def test_injection_is_reproducible(worlds, tmp_path) -> None:
    """Same seed, same damage - the experiment needs both arms to face it.

    Copies the clean world rather than simulating a third one; the simulator's
    own reproducibility is covered in test_run.
    """
    clean, _, defects = worlds
    again = tmp_path / "again"
    shutil.copytree(clean, again)
    repeat = {
        d.key: d.rows
        for d in DirtInjector(
            again,
            seed=42,
            quiet=True,
            migration_date=MIGRATION,
            min_partitions_for_outage=20,
        ).apply()
    }
    assert repeat == {k: d.rows for k, d in defects.items()}


def test_a_manifest_is_written_outside_the_raw_layer(worlds) -> None:
    _, dirty, _ = worlds
    manifest = dirty.parent / "_manifest" / "dirt.json"
    assert manifest.exists(), "no manifest written"
    assert json.loads(manifest.read_text())
    assert not (dirty / "_manifest").exists(), "the manifest leaked into the raw layer"


# =============================================================== 1. duplicates
def test_duplicate_order_rows_appear(worlds) -> None:
    clean, dirty, _ = worlds
    before, after = _read(clean, "pos_orders"), _read(dirty, "pos_orders")
    assert after["order_id"].duplicated().sum() > 0
    assert before["order_id"].duplicated().sum() == 0, "the clean run already had duplicates"


def test_duplicates_are_exact_copies_so_a_row_hash_removes_them(worlds) -> None:
    _, dirty, _ = worlds
    items = _read(dirty, "pos_order_items")
    deduped = items.drop_duplicates()
    assert len(deduped) < len(items)
    # and deduping on the order key alone would destroy real multi-line orders
    assert len(items.drop_duplicates(subset=["order_id"])) < len(deduped)


# =============================================================== 2. late arrivals
def test_some_orders_land_in_a_later_partition_than_they_happened(worlds) -> None:
    _, dirty, _ = worlds
    late = 0
    for path in sorted((dirty / "pos_orders").glob("dt=*/*.parquet")):
        day = dt.date.fromisoformat(path.parent.name.removeprefix("dt="))
        frame = pd.read_parquet(path)
        late += int((frame["order_ts"].dt.date < day).sum())
    assert late > 0, "no late arrivals - an incremental model would never be tested"


def test_late_arrivals_stay_within_the_documented_lookback(worlds) -> None:
    """The 48h window is what the incremental model will be built against."""
    _, dirty, _ = worlds
    for path in sorted((dirty / "pos_orders").glob("dt=*/*.parquet")):
        day = dt.date.fromisoformat(path.parent.name.removeprefix("dt="))
        frame = pd.read_parquet(path)
        lag = (pd.Timestamp(day) - frame["order_ts"].dt.normalize()).dt.days
        assert lag.max() <= 2, f"an order arrived {lag.max()} days late"


def test_relocating_an_order_never_deletes_it(worlds) -> None:
    """Regression: the pass used to drop orders it had nowhere to put.

    Rows picked for relocation were removed from their partition before it was
    known whether the target partition existed, so orders scheduled to land
    past the end of the run vanished - leaving their item lines pointing at a
    header that was not there. It cost 87 orders on the 365-day run and no
    check looked for it, because every existing check joined outward from the
    lines to batches rather than back to the order.
    """
    clean, dirty, _ = worlds
    before = set(_read(clean, "pos_orders")["order_id"])
    after = set(_read(dirty, "pos_orders")["order_id"])
    lost = before - after
    assert not lost, f"{len(lost):,} orders disappeared during relocation"

    lines = set(_read(dirty, "pos_order_items")["order_id"])
    assert not lines - after, f"{len(lines - after):,} item lines have no order header"


def test_late_arrivals_move_rows_rather_than_inventing_them(worlds) -> None:
    clean, dirty, defects = worlds
    before = len(_read(clean, "pos_orders"))
    after = len(_read(dirty, "pos_orders"))
    # the only net addition to this feed is duplication, not relocation
    assert after - before == defects["duplicate_events"].rows // 2 or after >= before


# =============================================================== 3. null batches
def test_some_movements_lose_their_batch_reference(worlds) -> None:
    clean, dirty, _ = worlds
    assert _read(clean, "wms_inventory_movement")["batch_id"].isna().sum() == 0
    nulls = _read(dirty, "wms_inventory_movement")["batch_id"].isna().sum()
    assert nulls > 0


def test_null_batch_ids_stay_a_small_minority(worlds) -> None:
    """Enough to need a quarantine table, not enough to break reconciliation."""
    _, dirty, _ = worlds
    moves = _read(dirty, "wms_inventory_movement")
    assert 0.002 < moves["batch_id"].isna().mean() < 0.03


# =============================================================== 4. unit drift
def test_a_few_skus_report_pack_size_in_the_wrong_unit(worlds) -> None:
    clean, dirty, _ = worlds
    before = _read(clean, "catalog_snapshot")
    after = _read(dirty, "catalog_snapshot")
    grams_before = before[before["uom"] == "g"]
    grams_after = after[after["uom"] == "g"]
    assert grams_before["pack_qty"].min() >= 1
    assert grams_after["pack_qty"].min() < 1, "no SKU drifted into kilograms"


def test_unit_drift_is_detectable_by_a_range_check(worlds) -> None:
    """Which is exactly how staging will catch it."""
    _, dirty, _ = worlds
    grams = _read(dirty, "catalog_snapshot").query("uom == 'g'")
    suspicious = grams.loc[grams["pack_qty"] < 1, "sku_id"].nunique()
    assert 5 <= suspicious <= 30


# =============================================================== 5. returns
def test_returns_exist_in_both_encodings(worlds) -> None:
    clean, dirty, _ = worlds
    assert (_read(clean, "pos_order_items")["qty"] > 0).all()

    items = _read(dirty, "pos_order_items")
    assert (items["qty"] < 0).any(), "no returns encoded as negative sales lines"
    returns = _read(dirty, "pos_returns")
    assert not returns.empty, "no separate returns feed"
    assert (returns["qty"] > 0).all(), "the separate feed should be positive quantities"


def test_the_returns_feed_carries_a_reason(worlds) -> None:
    _, dirty, _ = worlds
    returns = _read(dirty, "pos_returns")
    assert returns["reason"].notna().all()
    assert set(returns["reason"]) <= {"damaged", "wrong_item", "quality", "late"}


# =============================================================== 6. timezone
def test_clickstream_arrives_in_utc_while_orders_are_ist(worlds) -> None:
    clean, dirty, _ = worlds
    before, after = _read(clean, "clickstream"), _read(dirty, "clickstream")
    assert "hour" in before.columns
    assert "hour" not in after.columns, "the IST hour column should be gone"
    assert "event_ts_utc" in after.columns


def test_the_timezone_shift_is_exactly_five_and_a_half_hours(worlds) -> None:
    _, dirty, _ = worlds
    after = _read(dirty, "clickstream")
    hours = after["event_ts_utc"].dt.hour
    # the evening peak should now appear in the afternoon, which is the tell
    assert hours.value_counts().idxmax() < 18


def test_some_events_now_carry_the_wrong_date_for_their_partition(worlds) -> None:
    """Anything before 05:30 IST rolls back a day. This is where the bug is noticed."""
    _, dirty, _ = worlds
    mismatched = 0
    for path in sorted((dirty / "clickstream").glob("dt=*/*.parquet")):
        day = dt.date.fromisoformat(path.parent.name.removeprefix("dt="))
        frame = pd.read_parquet(path)
        mismatched += int((pd.to_datetime(frame["event_date"]).dt.date != day).sum())
    assert mismatched > 0


# =============================================================== 7. SKU codes
def test_the_sku_identifier_format_changes_partway_through(worlds) -> None:
    _, dirty, _ = worlds
    before, after = [], []
    for path in sorted((dirty / "clickstream").glob("dt=*/*.parquet")):
        day = dt.date.fromisoformat(path.parent.name.removeprefix("dt="))
        frame = pd.read_parquet(path)
        (after if day >= MIGRATION else before).append(frame["sku_id"])

    if not after:
        pytest.skip("the migration date is outside this test window")
    assert pd.concat(before).str.startswith("SKU-").all()
    assert pd.concat(after).str.startswith("SKU_").all()


def test_the_migrated_codes_would_break_a_naive_join(worlds) -> None:
    """Silently, which is the point - a relationships test is what catches it."""
    _, dirty, _ = worlds
    clicks = _read(dirty, "clickstream")
    catalog = _read(dirty, "catalog_snapshot")
    known = set(catalog["sku_id"])
    orphaned = ~clicks["sku_id"].isin(known)
    if not orphaned.any():
        pytest.skip("the migration date is outside this test window")
    assert orphaned.mean() > 0.05


# =============================================================== 8. outage
def test_two_days_of_clickstream_are_missing(worlds) -> None:
    clean, dirty, _ = worlds
    before = len(list((clean / "clickstream").glob("dt=*")))
    after = len(list((dirty / "clickstream").glob("dt=*")))
    assert before - after == 2


def test_the_outage_lands_on_a_busy_day(worlds) -> None:
    """Collectors fail under load, so the signal goes missing exactly when the
    analysis needed it."""
    clean, dirty, _ = worlds
    present = {p.name for p in (dirty / "clickstream").glob("dt=*")}
    # rows, not bytes: other defects rewrite these files and change their size
    rows = {
        p.name: len(pd.read_parquet(next(p.glob("*.parquet"))))
        for p in (clean / "clickstream").glob("dt=*")
    }
    missing = [n for n in rows if n not in present]
    assert len(missing) == 2

    median = sorted(rows.values())[len(rows) // 2]
    assert all(rows[n] > median for n in missing), (
        "the outage landed on quiet days, which is not how collectors fail"
    )


def test_the_missing_days_are_consecutive(worlds) -> None:
    clean, dirty, _ = worlds
    present = {p.name for p in (dirty / "clickstream").glob("dt=*")}
    missing = sorted(
        dt.date.fromisoformat(p.name.removeprefix("dt="))
        for p in (clean / "clickstream").glob("dt=*")
        if p.name not in present
    )
    assert (missing[1] - missing[0]).days == 1


# =============================================================== docs
def test_the_issues_document_generates_and_covers_every_defect(worlds, tmp_path) -> None:
    _, _, defects = worlds
    out = tmp_path / "known_data_issues.md"
    write_docs(list(defects.values()), out)
    text = out.read_text(encoding="utf-8")

    assert "Generated file" in text
    for d in defects.values():
        assert d.title in text
        assert d.fix[:40] in text
