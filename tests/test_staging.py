"""Contract for the staging layer (task S2.1).

The dbt tests prove the staging models are internally consistent. This file
proves something narrower and more useful: that each of the eight defects in
`docs/known_data_issues.md` was actually repaired, and repaired by the amount
the defect log says was injected.

The distinction matters. A staging layer that deduplicates 3,000 rows out of a
feed with 22,878 duplicates passes every uniqueness test it has - the survivors
are unique - and is still wrong. So every assertion here is anchored to
`data/_manifest/dirt.json`, the record of what S1.8 broke, rather than to a
number this test happens to observe.

Where a count cannot be exact it says why in the assertion, because "roughly
right" is the failure mode this whole file exists to rule out.

Needs a built warehouse:

    python tasks.py simulate --days 365 --seed 42
    python tasks.py build
    python -m pytest tests/test_staging.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
# CI builds the 30-day slice into a different file; the assertions are anchored
# to the defect manifest, so they hold for either window
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)
# same env var tasks.py hands dbt, so a probe run can point tests at a slice
RAW = Path(os.environ.get("FRESHFLOW_RAW_DIR", ROOT / "data" / "raw"))
MANIFEST = Path(os.environ.get("FRESHFLOW_MANIFEST", ROOT / "data" / "_manifest" / "dirt.json"))

pytestmark = pytest.mark.needs_warehouse

IST_OFFSET_MINUTES = 330


@pytest.fixture(scope="module")
def con():
    if not WAREHOUSE.exists():
        pytest.skip(f"no warehouse at {WAREHOUSE} - run `python tasks.py build`")
    connection = duckdb.connect(str(WAREHOUSE), read_only=True)
    yield connection
    connection.close()


@pytest.fixture(scope="module")
def defects() -> dict:
    if not MANIFEST.exists():
        pytest.skip("no defect manifest - run `python tasks.py simulate`")
    return {d["key"]: d for d in json.loads(MANIFEST.read_text(encoding="utf-8"))}


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


def raw(source: str) -> str:
    """The same parquet the sources read, addressed directly."""
    return f"read_parquet('{(RAW / source).as_posix()}/**/*.parquet', hive_partitioning = true)"


# ====================================================== 1. duplicate events
def test_deduplication_removes_exactly_the_injected_duplicates(con, defects) -> None:
    """The count has to match the defect log, not merely be greater than zero.

    Deduplicating too little leaves inflated revenue behind; deduplicating too
    much silently deletes real orders. Both pass a uniqueness test.
    """
    orders_removed = one(con, f"select count(*) from {raw('pos_orders')}") - one(
        con, "select count(*) from staging.stg_pos__orders"
    )
    items_removed = one(con, f"select count(*) from {raw('pos_order_items')}") - one(
        con, "select count(*) from staging.stg_pos__order_items"
    )
    assert orders_removed + items_removed == defects["duplicate_events"]["rows"]


def test_the_row_hash_ignores_the_partition_column(con) -> None:
    """The near-miss that makes this whole approach subtle.

    Defect 1 duplicates a row; defect 2 then moves one copy of the pair into a
    later partition. Hash `dt` along with the business columns and those pairs
    no longer match, so they survive deduplication and `order_id` stops being
    unique - by a couple of hundred rows out of 1.5M, which reconciles to
    "almost" and is the hardest kind of wrong to notice.
    """
    duplicated_ids = one(
        con,
        """
        select count(*) from (
            select order_id from staging.stg_pos__orders
            group by order_id having count(*) > 1)
        """,
    )
    assert duplicated_ids == 0, f"{duplicated_ids:,} order_ids survived deduplication twice"


# ======================================================== 2. late arrivals
def test_late_arrivals_are_kept_and_dated_by_the_event_not_the_partition(con, defects) -> None:
    """The rows an incremental keyed on the partition would drop are still here.

    Counted with tolerance on purpose: when a duplicated row is the copy that
    moved, deduplication keeps the earliest partition it appeared in and the
    lag legitimately collapses to zero. The bound is what is asserted - a lag
    beyond 48h would mean the S2.6 lookback is undersized.
    """
    late = one(con, "select count(*) from staging.stg_pos__orders where arrival_lag_days > 0")
    documented = defects["late_arrivals"]["rows"]
    assert late == pytest.approx(documented, rel=0.02)

    worst = one(con, "select max(arrival_lag_days) from staging.stg_pos__orders")
    assert 0 <= worst <= 2, f"an order arrived {worst} days after the fact"


# ================================================ 3. movements with no batch
def test_every_unattributable_movement_is_quarantined_not_dropped(con, defects) -> None:
    """The S2.1 acceptance criterion, stated exactly.

    Not 'about 49k rows were excluded' - the quarantine count and the defect
    log are the same number, or the staging layer lost something it never
    admitted to.
    """
    quarantined = one(
        con,
        """
        select count(*) from staging.stg_quarantine
        where reason_code = 'missing_batch_reference'
        """,
    )
    assert quarantined == defects["null_batch_ids"]["rows"]


def test_the_movement_ledger_adds_back_up_to_the_raw_feed(con) -> None:
    """Kept plus quarantined equals raw. The one-line summary of the contract."""
    kept = one(con, "select count(*) from staging.stg_wms__inventory_movements")
    quarantined = one(
        con,
        """
        select count(*) from staging.stg_quarantine
        where source_name = 'wms_inventory_movement'
        """,
    )
    assert kept + quarantined == one(con, f"select count(*) from {raw('wms_inventory_movement')}")


def test_the_quarantine_records_how_much_stock_it_is_holding(con) -> None:
    """impact_units is what makes the S2.7 reconciliation quantitative.

    The stock ledger is allowed not to balance, but only by the quantity sitting
    in quarantine. Without this column the reconciliation can only say 'off by
    some amount', which is indistinguishable from a second, unknown bug.
    """
    held = one(
        con,
        """
        select sum(impact_units) from staging.stg_quarantine
        where reason_code = 'missing_batch_reference'
        """,
    )
    expected = one(
        con,
        f"select sum(abs(qty_delta)) from {raw('wms_inventory_movement')} where batch_id is null",
    )
    assert held == expected


# ============================================================ 4. unit drift
def test_the_unit_repair_fires_on_exactly_the_drifted_rows(con, defects) -> None:
    repaired = one(
        con, "select count(*) from staging.stg_catalog__products where pack_qty_was_repaired"
    )
    assert repaired == defects["unit_drift"]["rows"]


def test_no_repaired_pack_size_is_still_physically_impossible(con) -> None:
    """A 500 g pack reading 0.5 makes every per-kilo price 1000x wrong."""
    implausible = one(
        con,
        """
        select count(*) from staging.stg_catalog__products
        where pack_qty <= 0 or (uom in ('g', 'ml') and pack_qty < 1)
        """,
    )
    assert implausible == 0


def test_the_repair_restored_the_original_pack_sizes(con) -> None:
    """Rescaling by 1000 has to land back on the catalogue's real pack sizes.

    A repair that merely moves values into a plausible range would pass the
    bound check above while inventing sizes nobody sells. The drifted SKUs must
    end up on sizes that undrifted SKUs also use.
    """
    unknown = one(
        con,
        """
        with sizes as (
            select distinct pack_qty
            from staging.stg_catalog__products
            where uom = 'g' and not pack_qty_was_repaired)
        select count(distinct sku_id)
        from staging.stg_catalog__products
        where pack_qty_was_repaired and pack_qty not in (select pack_qty from sizes)
        """,
    )
    assert unknown == 0, f"{unknown} repaired SKUs landed on a pack size the catalogue never sells"


# =============================================================== 5. returns
def test_both_return_encodings_survive_into_one_signed_model(con, defects) -> None:
    normalised = one(
        con, "select count(*) from staging.stg_pos__order_lines where line_type = 'return'"
    )
    quarantined = one(
        con,
        """
        select count(*) from staging.stg_quarantine
        where reason_code = 'return_without_matching_sale'
        """,
    )
    assert normalised + quarantined == defects["inconsistent_returns"]["rows"]


def test_returns_are_negative_and_sales_are_positive(con) -> None:
    """The convention every downstream sum depends on. Worth one assertion."""
    wrong_sign = one(
        con,
        """
        select count(*) from staging.stg_pos__order_lines
        where (line_type = 'sale' and signed_qty <= 0)
           or (line_type = 'return' and signed_qty >= 0)
        """,
    )
    assert wrong_sign == 0


def test_counting_returns_naively_double_counts_one_encoding(con) -> None:
    """Proof the normalisation is load-bearing rather than decorative.

    `sum(qty)` over the raw sales feed is already net of encoding A, because
    those returns are negative rows sitting inside it. Read that total as gross
    sales and then subtract every return, and encoding A comes off twice. It is
    an easy mistake to make and an impossible one to spot in the output: the
    number is smaller, plausible, and wrong by 0.1%.

    Everything is measured on the deduplicated feed. Taking the raw total here
    mixes defect 1 into the arithmetic and the identity stops holding by the
    weight of the duplicate rows - which is the same mistake one layer up.
    """
    feed_total = one(con, "select sum(qty) from staging.stg_pos__order_items")
    encoding_a = one(con, "select sum(abs(qty)) from staging.stg_pos__order_items where qty < 0")
    # net of any return that could not be matched to a sale, since those are
    # quarantined rather than carried into the normalised model
    encoding_b = one(con, "select sum(returned_qty) from staging.stg_pos__returns") - one(
        con,
        """
        select coalesce(sum(impact_units), 0) from staging.stg_quarantine
        where reason_code = 'return_without_matching_sale'
        """,
    )

    correct = one(con, "select sum(signed_qty) from staging.stg_pos__order_lines")
    naive = feed_total - encoding_a - encoding_b

    assert encoding_a > 0, "encoding A is absent - the trap this test describes cannot occur"
    assert correct == feed_total - encoding_b
    assert naive == correct - encoding_a


# ============================================================ 6. timezone
def test_clickstream_is_conformed_to_ist(con) -> None:
    drift = one(
        con,
        """
        select max(abs(date_diff('minute', event_ts_utc, event_ts_ist)))
        from staging.stg_web__clickstream
        """,
    )
    assert drift == IST_OFFSET_MINUTES


def test_the_conformed_demand_curve_peaks_in_the_evening(con) -> None:
    """The test docs/known_data_issues.md asks for by name.

    Left in UTC and read as local, the evening window lands on IST 22:30-03:30
    and the small hours fill with the morning grocery peak, so the inequality
    does not narrow - it inverts.
    """
    evening, small_hours = con.execute(
        """
        select
            sum(case when event_hour_ist between 17 and 22 then 1 else 0 end),
            sum(case when event_hour_ist between 0 and 4 then 1 else 0 end)
        from staging.stg_web__clickstream
        """
    ).fetchone()
    assert evening > small_hours


def test_conforming_the_timezone_realigns_events_with_their_partition(con) -> None:
    """Defect 6 is what pushed pre-05:30 events into the previous day's date.

    Once conformed, an event's IST date is its partition again - which is also
    why the outage below shows up as exactly two missing dates rather than
    three partially empty ones.
    """
    misfiled = one(
        con,
        """
        select count(*) from staging.stg_web__clickstream
        where event_date_ist <> arrival_date
        """,
    )
    assert misfiled == 0


# ==================================================== 7. sku code migration
def test_every_clickstream_event_now_joins_to_the_catalogue(con) -> None:
    orphans = one(
        con,
        """
        select count(*) from staging.stg_web__clickstream as c
        where not exists (
            select 1 from staging.stg_catalog__products as p where p.sku_id = c.sku_id)
        """,
    )
    assert orphans == 0


def test_the_conform_fires_on_exactly_the_migrated_events(con, defects) -> None:
    conformed = one(
        con, "select count(*) from staging.stg_web__clickstream where sku_id_was_conformed"
    )
    assert conformed == defects["sku_code_migration"]["rows"]


def test_without_the_conform_the_join_would_have_lost_them_silently(con, defects) -> None:
    """Why a relationships test, not an inner join.

    An inner join on the raw identifier still returns rows - just 1.69M fewer -
    and nothing in the pipeline would have said so.
    """
    would_have_been_lost = one(
        con,
        f"""
        select count(*) from {raw("clickstream")} as c
        where not exists (
            select 1 from {raw("catalog_snapshot")} as s where s.sku_id = c.sku_id)
        """,
    )
    assert would_have_been_lost == pytest.approx(defects["sku_code_migration"]["rows"], rel=0.02)


# ============================================================== 8. outage
def test_the_outage_is_left_visible_rather_than_filled(con, defects) -> None:
    """Whatever the collector lost, staging must not paper over.

    Interpolating would be worse than useless: the collector fell over under
    load, so the missing days are two of the busiest of the year, and any fill
    biases the censored-demand signal downward exactly where stockouts were
    worst. The gap is a fact the S2.7 row-count check has to be able to fail on.

    The expected width comes from the defect log rather than from the constant
    2, because the outage only fires on a run long enough to have days worth
    losing - the injector skips it below 60 partitions. CI builds a 30-day
    slice, where the honest expectation is a gap of zero, and a test hardcoded
    to 2 would fail there for the wrong reason entirely.
    """
    expected_missing_days = 2 if "clickstream_outage" in defects else 0

    clickstream_days = one(
        con, "select count(distinct event_date_ist) from staging.stg_web__clickstream"
    )
    order_days = one(con, "select count(distinct order_date_ist) from staging.stg_pos__orders")

    assert order_days - clickstream_days == expected_missing_days, (
        f"expected {expected_missing_days} missing clickstream day(s) but found "
        f"{order_days - clickstream_days} - either staging filled the gap, or the "
        "defect changed"
    )
    if expected_missing_days:
        assert defects["clickstream_outage"]["rows"] > 0


# ======================================================= layer-wide contract
def test_no_staging_model_is_empty(con) -> None:
    """Cheap, and catches a source path that silently resolved to nothing."""
    models = [
        row[0]
        for row in con.execute(
            "select table_name from information_schema.tables where table_schema = 'staging'"
        ).fetchall()
    ]
    assert len(models) >= 15, f"only {len(models)} staging models built"
    empty = [m for m in models if one(con, f"select count(*) from staging.{m}") == 0]
    assert not empty, f"empty staging models: {empty}"
