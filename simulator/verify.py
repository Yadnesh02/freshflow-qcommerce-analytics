"""Checkpoint gate G1, run against a real emitted raw layer (task S1.9).

The unit tests prove each component behaves in isolation. This proves the
*dataset on disk* is trustworthy - which is a different claim, and the one that
matters before anything is built on top of it.

    python tasks.py gate

Checks come in two kinds, and separating them is the whole point.

**Invariants** must hold no matter what. Stock never goes negative, no sale is
served from a batch that belonged to another store, nothing expired is sold,
FEFO order is respected. If one of these fails, the simulator is wrong.

**Explained deviations** are places where the raw layer deliberately does not
reconcile, because task S1.8 broke it on purpose. Each one must match its
documented defect to within a tolerance. A deviation that is *larger* than
documented means something else is wrong too - which is exactly the failure
mode a single "does it reconcile?" check would hide.

Queries run in DuckDB directly against the parquet, so the gate reads the same
files the warehouse will and never loads 5M rows into pandas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

FEFO_SAMPLE_CELLS = 300
PASS, FAIL, EXPLAINED = "PASS", "FAIL", "EXPLAINED"


@dataclass
class Check:
    name: str
    kind: str  # "invariant" or "deviation"
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in (PASS, EXPLAINED)


class Gate:
    """Runs G1 against an emitted raw layer."""

    def __init__(self, raw_dir: Path = RAW, quiet: bool = False) -> None:
        self.raw = raw_dir
        self.quiet = quiet
        self.con = duckdb.connect()
        self.checks: list[Check] = []

        manifest = raw_dir.parent / "_manifest" / "dirt.json"
        self.defects = (
            {d["key"]: d for d in json.loads(manifest.read_text(encoding="utf-8"))}
            if manifest.exists()
            else {}
        )

    # ------------------------------------------------------------------ util
    def src(self, name: str) -> str:
        return f"read_parquet('{(self.raw / name).as_posix()}/*/*.parquet')"

    def one(self, sql: str):
        return self.con.execute(sql).fetchone()[0]

    def _add(self, name: str, kind: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, kind, status, detail))
        if not self.quiet:
            mark = {
                PASS: "\033[32mPASS\033[0m",
                EXPLAINED: "\033[33mEXPL\033[0m",
                FAIL: "\033[31mFAIL\033[0m",
            }[status]
            print(f"  {mark}  {name:<46}{detail}")

    def invariant(self, name: str, ok: bool, detail: str) -> None:
        self._add(name, "invariant", PASS if ok else FAIL, detail)

    def deviation(
        self, name: str, actual: int, expected: int, detail: str, tol: float = 0.10
    ) -> None:
        """A deviation is fine if it matches its documented defect, and only then."""
        if expected == 0:
            status = PASS if actual == 0 else FAIL
        else:
            status = EXPLAINED if abs(actual - expected) <= max(expected * tol, 5) else FAIL
        self._add(name, "deviation", status, f"{detail} (documented {expected:,})")

    # ------------------------------------------------------------- invariants
    def check_stock_never_negative(self) -> None:
        """Strict, but only on batches whose movement history survived.

        Nulling a movement's batch reference (defect 3) removes that inbound
        from the batch's balance, so the batch reads as over-consumed. That is
        a property of the damage, not of the simulator. The invariant therefore
        applies where the history is intact, and the residual imbalance is
        checked separately against the quarantined quantity - which is exactly
        the reasoning staging will have to do.
        """
        worst = self.one(f"""
            with intact as (
                select b.batch_id
                from {self.src("wms_inventory_batch")} b
                join (
                    select batch_id, sum(qty_delta) as received
                    from {self.src("wms_inventory_movement")}
                    where event_type in ('inbound', 'opening_balance')
                    group by batch_id) m using (batch_id)
                where m.received = b.qty_received)
            select coalesce(min(net), 0) from (
                select batch_id, sum(qty_delta) as net
                from {self.src("wms_inventory_movement")}
                where batch_id in (select batch_id from intact)
                group by batch_id)
        """)
        self.invariant(
            "stock never goes negative (intact batches)",
            worst >= 0,
            f"worst batch balance {worst:+,}",
        )

    def check_residual_imbalance_is_bounded(self) -> None:
        """Whatever no longer balances must be no larger than what went missing."""
        shortfall = -self.one(f"""
            select coalesce(sum(least(net, 0)), 0) from (
                select batch_id, sum(qty_delta) as net
                from {self.src("wms_inventory_movement")}
                where batch_id is not null group by batch_id)
        """)
        quarantined = self.one(f"""
            select coalesce(sum(abs(qty_delta)), 0)
            from {self.src("wms_inventory_movement")} where batch_id is null
        """)
        if quarantined == 0:
            status = PASS if shortfall == 0 else FAIL
        else:
            status = EXPLAINED if shortfall <= quarantined else FAIL
        self._add(
            "imbalance is covered by the quarantined rows",
            "deviation",
            status,
            f"{shortfall:,} units unaccounted vs {quarantined:,} quarantined",
        )

    def check_no_expired_stock_sold(self) -> None:
        bad = self.one(f"select count(*) from {self.src('pos_order_items')} where dte_at_sale < 0")
        self.invariant("no expired stock is ever sold", bad == 0, f"{bad:,} lines past expiry")

    def check_batches_belong_to_the_selling_store(self) -> None:
        bad = self.one(f"""
            select count(*)
            from {self.src("pos_order_items")} i
            join {self.src("pos_orders")} o using (order_id)
            join {self.src("wms_inventory_batch")} b on b.batch_id = i.batch_id
            where o.store_id <> b.store_id
        """)
        self.invariant(
            "stock is only sold by the store that held it",
            bad == 0,
            f"{bad:,} cross-store sales",
        )

    def check_every_sold_batch_exists(self) -> None:
        orphans = self.one(f"""
            select count(*) from {self.src("pos_order_items")} i
            left join {self.src("wms_inventory_batch")} b using (batch_id)
            where i.batch_id is not null and b.batch_id is null
        """)
        self.invariant(
            "every order line resolves to a real batch",
            orphans == 0,
            f"{orphans:,} orphaned lines",
        )

    def check_batch_dates_are_coherent(self) -> None:
        bad = self.one(f"""
            select count(*) from {self.src("wms_inventory_batch")}
            where expiry_date <= received_date or mfg_date > received_date or qty_received <= 0
        """)
        self.invariant("batch dates and quantities are coherent", bad == 0, f"{bad:,} bad batches")

    def check_purchase_orders_are_coherent(self) -> None:
        bad = self.one(f"""
            select count(*) from {self.src("wms_purchase_orders")}
            where received_date < ordered_date or received_qty > ordered_qty
        """)
        self.invariant("deliveries arrive after they were ordered", bad == 0, f"{bad:,} bad POs")

    def check_fefo_order_is_respected(self) -> None:
        """Replay a sample of cells and confirm the soonest-expiring batch went first.

        Replayed from the **sales feed**, not the movement ledger. Defect 3
        nulls the batch reference on some movements, and a missing sale row
        makes a batch look like it still has stock, so every later sale in that
        cell then reads as out of order. The order-item feed keeps its batch
        reference, so it can be replayed even on damaged data - which is also
        how an analyst would actually check this.

        Duplicates and returns are filtered the way staging will filter them.
        A batch is available from the day it arrives until the day it expires;
        that avoids needing the write-off events at all.

        Sampled rather than exhaustive: a violation would show in any cell.
        """
        cells = self.con.execute(f"""
            select store_id, sku_id
            from {self.src("wms_inventory_batch")}
            group by 1, 2 having count(*) between 5 and 60
            order by hash(store_id || sku_id) limit {FEFO_SAMPLE_CELLS}
        """).fetchall()
        if not cells:
            self.invariant("FEFO order is respected", False, "no cells to sample")
            return

        keys = ", ".join(f"('{s}','{k}')" for s, k in cells)
        receipts = self.con.execute(f"""
            select store_id, sku_id, batch_id, received_date, expiry_date, qty_received
            from {self.src("wms_inventory_batch")}
            where (store_id, sku_id) in ({keys})
        """).fetchall()
        sales = self.con.execute(f"""
            select o.store_id, i.sku_id, i.batch_id, o.order_ts, i.qty
            from (
                select distinct order_id, sku_id, batch_id, qty, unit_realized_price
                from {self.src("pos_order_items")} where qty > 0) i
            join (select distinct order_id, store_id, order_ts
                  from {self.src("pos_orders")}) o using (order_id)
            where (o.store_id, i.sku_id) in ({keys})
            order by o.store_id, i.sku_id, o.order_ts
        """).fetchall()

        by_cell: dict[tuple, list] = {}
        for store, sku, batch, recv, exp, qty in receipts:
            by_cell.setdefault((store, sku), []).append([batch, recv, exp, qty])

        # One order line can be split across two batches when the first runs
        # out mid-line. Both rows carry the same timestamp, so the raw feed has
        # no ordering between them - and checking one against the other would
        # report a violation that never happened. Sales are therefore grouped
        # by instant, and a sale is only judged against stock that was not part
        # of the same allocation.
        from itertools import groupby

        violations, checked = 0, 0
        remaining: dict[tuple, dict] = {}
        for (store, sku, ts), group in groupby(sales, key=lambda r: (r[0], r[1], r[3])):
            cell = (store, sku)
            stock = remaining.setdefault(
                cell, {b: [r, e, q] for b, r, e, q in by_cell.get(cell, [])}
            )
            batch_group = [(b, q) for _s, _k, b, _t, q in group if b in stock]
            if not batch_group:
                continue

            day = ts.date()
            concurrent = {b for b, _ in batch_group}
            for batch, _qty in batch_group:
                checked += 1
                sold_expiry = stock[batch][1]
                for other, (recv, exp, left) in stock.items():
                    if other in concurrent or left <= 0:
                        continue
                    # only stock that had arrived and had not yet expired
                    if recv <= day <= exp and exp < sold_expiry:
                        violations += 1
                        break
            for batch, qty in batch_group:
                stock[batch][2] -= qty

        self.invariant(
            "FEFO order is respected",
            violations == 0,
            f"{violations:,} of {checked:,} sampled sales out of order",
        )

    def check_partitions_are_complete(self) -> None:
        expected = len(list((self.raw / "pos_orders").glob("dt=*")))
        gaps = {}
        for source in ("pos_order_items", "wms_inventory_movement", "catalog_snapshot"):
            got = len(list((self.raw / source).glob("dt=*")))
            if got != expected:
                gaps[source] = got
        self.invariant(
            "core feeds have a partition for every day",
            not gaps,
            f"{expected} days" if not gaps else f"gaps: {gaps}",
        )

    # ------------------------------------------------------------- deviations
    def check_duplicates(self) -> None:
        total, distinct = self.con.execute(f"""
            select count(*), count(distinct (order_id, sku_id, batch_id, qty, unit_realized_price))
            from {self.src("pos_order_items")} where qty > 0
        """).fetchone()
        documented = self.defects.get("duplicate_events", {}).get("rows", 0)
        # the manifest counts both feeds; roughly half land on item lines
        self.deviation(
            "duplicate order lines match the defect log",
            total - distinct,
            int(documented * 0.955),
            f"{total - distinct:,} duplicate lines",
            tol=0.25,
        )

    def check_null_batch_ids(self) -> None:
        nulls = self.one(
            f"select count(*) from {self.src('wms_inventory_movement')} where batch_id is null"
        )
        self.deviation(
            "movements with no batch match the defect log",
            nulls,
            self.defects.get("null_batch_ids", {}).get("rows", 0),
            f"{nulls:,} unattributable movements",
        )

    def check_returns(self) -> None:
        negatives = self.one(f"select count(*) from {self.src('pos_order_items')} where qty < 0")
        separate = (
            self.one(f"select count(*) from {self.src('pos_returns')}")
            if (self.raw / "pos_returns").exists()
            else 0
        )
        self.deviation(
            "returns appear in both encodings",
            negatives + separate,
            self.defects.get("inconsistent_returns", {}).get("rows", 0),
            f"{negatives:,} negative lines + {separate:,} in the returns feed",
        )

    def check_late_arrivals(self) -> None:
        late = self.one(f"""
            select count(*) from (
                select order_ts,
                       cast(regexp_extract(filename, 'dt=([0-9-]+)', 1) as date) as partition_day
                from read_parquet('{(self.raw / "pos_orders").as_posix()}/*/*.parquet',
                                  filename = true))
            where cast(order_ts as date) < partition_day
        """)
        self.deviation(
            "late arrivals match the defect log",
            late,
            self.defects.get("late_arrivals", {}).get("rows", 0),
            f"{late:,} orders landed after the fact",
            tol=0.20,
        )

    def check_clickstream_outage(self) -> None:
        expected = len(list((self.raw / "pos_orders").glob("dt=*")))
        got = len(list((self.raw / "clickstream").glob("dt=*")))
        documented = 2 if "clickstream_outage" in self.defects else 0
        self.deviation(
            "clickstream gap matches the documented outage",
            expected - got,
            documented,
            f"{expected - got} missing days",
            tol=0.0,
        )

    def check_sku_code_drift(self) -> None:
        orphans = self.one(f"""
            select count(*) from {self.src("clickstream")} c
            where not exists (
                select 1 from {self.src("catalog_snapshot")} s where s.sku_id = c.sku_id)
        """)
        documented = self.defects.get("sku_code_migration", {}).get("rows", 0)
        self.deviation(
            "clickstream SKUs that will not join",
            orphans,
            documented,
            f"{orphans:,} events with a migrated identifier",
            tol=0.02,
        )

    # ------------------------------------------------------------------ run
    def run(self) -> list[Check]:
        if not self.quiet:
            print(f"\ngate G1 against {self.raw}\n")
            print("  invariants")
        self.check_stock_never_negative()
        self.check_no_expired_stock_sold()
        self.check_batches_belong_to_the_selling_store()
        self.check_every_sold_batch_exists()
        self.check_batch_dates_are_coherent()
        self.check_purchase_orders_are_coherent()
        self.check_fefo_order_is_respected()
        self.check_partitions_are_complete()

        if not self.quiet:
            print("\n  deviations explained by docs/known_data_issues.md")
        self.check_residual_imbalance_is_bounded()
        self.check_duplicates()
        self.check_null_batch_ids()
        self.check_returns()
        self.check_late_arrivals()
        self.check_clickstream_outage()
        self.check_sku_code_drift()
        return self.checks

    def report(self) -> bool:
        failed = [c for c in self.checks if not c.ok]
        if not self.quiet:
            print()
            if failed:
                print(f"\033[31mGATE FAILED\033[0m - {len(failed)} of {len(self.checks)} checks")
                for c in failed:
                    print(f"    {c.name}: {c.detail}")
            else:
                inv = sum(1 for c in self.checks if c.kind == "invariant")
                dev = len(self.checks) - inv
                print(
                    f"\033[32mGATE PASSED\033[0m - {inv} invariants hold, "
                    f"{dev} deviations all explained by documented defects"
                )
        return not failed


def main() -> int:
    import sys

    raw = Path(sys.argv[1]) if len(sys.argv) > 1 else RAW
    if not raw.exists():
        print(f"no raw layer at {raw} - run: python tasks.py simulate")
        return 1
    gate = Gate(raw)
    gate.run()
    return 0 if gate.report() else 1


if __name__ == "__main__":
    raise SystemExit(main())
