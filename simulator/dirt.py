"""Make the raw layer realistically broken (task S1.8).

The simulator emits clean data. Real source feeds are not clean, and a project
whose Bronze layer arrives perfect skips the part of the job that actually
takes the time - so this pass deliberately breaks it in eight specific ways,
each one a defect that genuinely happens and each one requiring a different
fix in the staging layer.

Every defect is applied after the run rather than during it, which keeps the
simulator's own invariants provable and means the clean and dirty versions can
be diffed. Everything is seeded, so the same seed produces the same damage.

    python tasks.py simulate            # dirty, the default
    python -m simulator.run --clean     # skip this pass

The point is not that dirty data is virtuous. It is that "how do you handle
duplicate events in a stream?" is a top-ten interview question, and the honest
answer is a dbt test and a quarantine table you actually wrote.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

IST_OFFSET = dt.timedelta(hours=5, minutes=30)

# --- how much of each defect ------------------------------------------------
DUPLICATE_RATE = 0.004  # retried webhooks
LATE_ARRIVAL_RATE = 0.015  # events landing in a later partition than they belong to
LATE_MAX_DAYS = 2
NULL_BATCH_RATE = 0.010  # movements with no batch reference
UNIT_DRIFT_SKUS = 14  # SKUs whose pack size flips between g and kg
RETURN_RATE = 0.0025  # returns, encoded two different ways
OUTAGE_DAYS = 2  # consecutive days of missing clickstream
SKU_CODE_MIGRATION = dt.date(2026, 3, 1)  # clickstream switches identifier format


@dataclass
class Defect:
    key: str
    title: str
    feeds: tuple[str, ...]
    rows: int
    symptom: str
    fix: str


@dataclass
class DirtInjector:
    """Applies the documented defects to an emitted raw layer, in place."""

    raw_dir: Path
    seed: int = 42
    quiet: bool = False
    # overridable so the tests can reach both window-dependent defects without
    # simulating six months
    migration_date: dt.date = SKU_CODE_MIGRATION
    min_partitions_for_outage: int = 60

    defects: list[Defect] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng([self.seed, 31337])

    # ------------------------------------------------------------------ io
    def _partitions(self, source: str) -> list[Path]:
        return sorted((self.raw_dir / source).glob("dt=*/*.parquet"))

    @staticmethod
    def _day_of(path: Path) -> dt.date:
        return dt.date.fromisoformat(path.parent.name.removeprefix("dt="))

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    def _record(self, **kw) -> None:
        self.defects.append(Defect(**kw))
        d = self.defects[-1]
        self._log(f"  {d.title:<44}{d.rows:>10,} rows")

    # ------------------------------------------------------------------ 1
    def duplicate_events(self) -> None:
        """A retried webhook delivers the same order twice, byte for byte."""
        total = 0
        for source in ("pos_orders", "pos_order_items"):
            for path in self._partitions(source):
                frame = pd.read_parquet(path)
                n = int(len(frame) * DUPLICATE_RATE)
                if n == 0:
                    continue
                dupes = frame.iloc[self.rng.choice(len(frame), n, replace=False)]
                pd.concat([frame, dupes], ignore_index=True).to_parquet(path, index=False)
                total += n
        self._record(
            key="duplicate_events",
            title="Duplicate order events (retried webhook)",
            feeds=("pos_orders", "pos_order_items"),
            rows=total,
            symptom="Exact duplicate rows. Revenue and units overstated by ~0.4%.",
            fix="Deduplicate on the full row hash in staging. Do not dedupe on "
            "order_id alone - a genuine order has many item lines.",
        )

    # ------------------------------------------------------------------ 2
    def late_arrivals(self) -> None:
        """Some orders land in a later partition than the day they happened."""
        paths = self._partitions("pos_orders")
        by_day = {self._day_of(p): p for p in paths}
        moved: dict[dt.date, list[pd.DataFrame]] = {}
        total = 0

        for path in paths:
            frame = pd.read_parquet(path)
            n = int(len(frame) * LATE_ARRIVAL_RATE)
            if n == 0:
                continue
            pick = self.rng.choice(len(frame), n, replace=False)
            late = frame.iloc[pick]
            delay = self.rng.integers(1, LATE_MAX_DAYS + 1, n)

            relocated = np.zeros(n, dtype=bool)
            for offset in range(1, LATE_MAX_DAYS + 1):
                at_offset = delay == offset
                target = self._day_of(path) + dt.timedelta(days=offset)
                if not at_offset.any() or target not in by_day:
                    continue
                moved.setdefault(target, []).append(late[at_offset])
                relocated |= at_offset
                total += int(at_offset.sum())

            # Only rows that actually landed somewhere are removed from here.
            # Near the end of the run the target partition does not exist, and
            # dropping those anyway - which an unguarded drop does - deletes
            # the orders outright and orphans their item lines. That is not a
            # documented defect, it is data loss, and it is invisible until
            # something tries to join lines back to their header.
            frame.drop(frame.index[pick[relocated]]).to_parquet(path, index=False)

        for day, chunks in moved.items():
            path = by_day[day]
            pd.concat([pd.read_parquet(path), *chunks], ignore_index=True).to_parquet(
                path, index=False
            )

        self._record(
            key="late_arrivals",
            title="Late-arriving orders (up to 48h)",
            feeds=("pos_orders",),
            rows=total,
            symptom="An order's timestamp is up to two days before the partition "
            "it arrived in. A daily incremental keyed on the partition silently "
            "drops them.",
            fix="Incremental models must key on the event timestamp and reprocess "
            "a 48-hour lookback window, not just the newest partition.",
        )

    # ------------------------------------------------------------------ 3
    def null_batch_ids(self) -> None:
        """The WMS occasionally emits a movement with no batch reference."""
        total = 0
        for path in self._partitions("wms_inventory_movement"):
            frame = pd.read_parquet(path)
            n = int(len(frame) * NULL_BATCH_RATE)
            if n == 0:
                continue
            idx = frame.index[self.rng.choice(len(frame), n, replace=False)]
            frame.loc[idx, "batch_id"] = None
            frame.to_parquet(path, index=False)
            total += n
        self._record(
            key="null_batch_ids",
            title="Inventory movements with no batch reference",
            feeds=("wms_inventory_movement",),
            rows=total,
            symptom="batch_id is null, so the movement cannot be attributed to an "
            "expiry date and the stock reconciliation will not balance.",
            fix="Route to a quarantine table with the reason recorded. Never drop "
            "silently - the reconciliation test is what surfaces the gap.",
        )

    # ------------------------------------------------------------------ 4
    def unit_drift(self) -> None:
        """Pack sizes for a few SKUs arrive in kilograms while the unit still says grams."""
        paths = self._partitions("catalog_snapshot")
        if not paths:
            return
        skus = pd.read_parquet(paths[0])
        candidates = skus.loc[skus["uom"] == "g", "sku_id"].to_numpy()
        drifted = self.rng.choice(candidates, min(UNIT_DRIFT_SKUS, len(candidates)), replace=False)

        total = 0
        for path in paths:
            frame = pd.read_parquet(path)
            hit = frame["sku_id"].isin(drifted)
            frame.loc[hit, "pack_qty"] = frame.loc[hit, "pack_qty"] / 1000.0
            frame.to_parquet(path, index=False)
            total += int(hit.sum())

        self._record(
            key="unit_drift",
            title=f"Pack size in kg while uom says g ({len(drifted)} SKUs)",
            feeds=("catalog_snapshot",),
            rows=total,
            symptom="A 500 g pack reports pack_qty 0.5. Any per-kilo price or "
            "weight rollup is wrong by three orders of magnitude for those SKUs.",
            fix="Range-check pack_qty by uom in staging and rescale. A "
            "dbt-expectations bound on pack_qty per uom catches it.",
        )

    # ------------------------------------------------------------------ 5
    def inconsistent_returns(self) -> None:
        """Returns exist in two encodings, because two systems wrote them."""
        paths = self._partitions("pos_order_items")
        as_negative = 0
        as_events: list[pd.DataFrame] = []

        for path in paths:
            frame = pd.read_parquet(path)
            n = int(len(frame) * RETURN_RATE)
            if n < 2:
                continue
            pick = self.rng.choice(len(frame), n, replace=False)
            half = n // 2

            # encoding A: a negative quantity appended to the sales feed
            neg = frame.iloc[pick[:half]].copy()
            neg["qty"] = -neg["qty"]
            pd.concat([frame, neg], ignore_index=True).to_parquet(path, index=False)
            as_negative += len(neg)

            # encoding B: a separate returns feed, positive quantities
            ret = frame.iloc[pick[half:]][["order_id", "sku_id", "batch_id", "qty"]].copy()
            ret["return_date"] = self._day_of(path)
            ret["reason"] = self.rng.choice(["damaged", "wrong_item", "quality", "late"], len(ret))
            as_events.append((path, ret))

        for path, ret in as_events:
            out = self.raw_dir / "pos_returns" / path.parent.name
            out.mkdir(parents=True, exist_ok=True)
            ret.to_parquet(out / "part-0.parquet", index=False)

        self._record(
            key="inconsistent_returns",
            title="Returns encoded two different ways",
            feeds=("pos_order_items", "pos_returns"),
            rows=as_negative + sum(len(r) for _, r in as_events),
            symptom="Some returns are negative quantities inside the sales feed; "
            "others are positive rows in a separate feed. Counting either alone "
            "gets net units wrong, and counting both naively double-counts.",
            fix="Normalise both into one signed movement in staging, and assert "
            "that gross sales minus returns reconciles to the inventory ledger.",
        )

    # ------------------------------------------------------------------ 6
    def timezone_mix(self) -> None:
        """Clickstream is UTC while the POS is IST, and nobody wrote it down."""
        total = 0
        for path in self._partitions("clickstream"):
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            ist = pd.to_datetime(frame["event_date"].astype(str)) + pd.to_timedelta(
                frame["hour"], unit="h"
            )
            utc = ist - IST_OFFSET
            frame["event_ts_utc"] = utc
            # the date column now disagrees with the partition for anything
            # before 05:30 IST, which is where this bug is usually noticed
            frame["event_date"] = utc.dt.date
            frame = frame.drop(columns=["hour"])
            frame.to_parquet(path, index=False)
            total += len(frame)

        self._record(
            key="timezone_mix",
            title="Clickstream in UTC, POS in IST",
            feeds=("clickstream",),
            rows=total,
            symptom="Clickstream timestamps are 5h30m behind the orders they "
            "relate to. Joining on date misattributes every event before "
            "05:30 IST to the previous day, and the evening demand peak lands "
            "in the afternoon.",
            fix="Conform everything to IST in staging and say so in the column "
            "name. A test that the hourly demand curve peaks in the evening "
            "catches this immediately.",
        )

    # ------------------------------------------------------------------ 7
    def sku_code_migration(self) -> None:
        """A catalogue migration changes the identifier format mid-year."""
        total = 0
        for path in self._partitions("clickstream"):
            if self._day_of(path) < self.migration_date:
                continue
            frame = pd.read_parquet(path)
            if frame.empty:
                continue
            frame["sku_id"] = frame["sku_id"].str.replace(r"^SKU-0*(\d+)$", r"SKU_\1", regex=True)
            frame.to_parquet(path, index=False)
            total += len(frame)

        self._record(
            key="sku_code_migration",
            title=f"SKU identifier format changes on {self.migration_date}",
            feeds=("clickstream",),
            rows=total,
            symptom="SKU-00042 becomes SKU_42 partway through the year, so an "
            "inner join to the product dimension silently loses every "
            "clickstream event after that date.",
            fix="Normalise the identifier in staging and assert referential "
            "integrity against dim_product. A relationships test fails loudly "
            "where a silent inner join would not.",
        )

    # ------------------------------------------------------------------ 8
    def clickstream_outage(self) -> None:
        """The event collector fell over for two days."""
        paths = self._partitions("clickstream")
        if len(paths) < self.min_partitions_for_outage:
            return

        # Collectors fall over under load, not at random. Picking the outage
        # from the busiest days is both more realistic and far more awkward:
        # the censored-demand signal goes missing exactly on the days when
        # stockouts were worst and the analysis needed it most.
        #
        # Ranked by row count, not file size: earlier defects in this pass
        # rewrite these same files and change their size on disk.
        counts = [
            (pq.ParquetFile(path).metadata.num_rows, i)
            for i, path in enumerate(paths[3:-OUTAGE_DAYS], start=3)
        ]
        busiest = [i for _, i in sorted(counts, reverse=True)[: max(3, len(counts) // 5)]]
        start = int(self.rng.choice(busiest))
        lost = 0
        days: list[str] = []
        for path in paths[start : start + OUTAGE_DAYS]:
            lost += len(pd.read_parquet(path))
            days.append(self._day_of(path).isoformat())
            shutil.rmtree(path.parent)

        self._record(
            key="clickstream_outage",
            title=f"Clickstream outage ({', '.join(days)})",
            feeds=("clickstream",),
            rows=lost,
            symptom="Two partitions are missing entirely, and because collectors "
            "fail under load they are two of the busiest days of the year. Any "
            "metric averaging over that window is biased downward, and the "
            "censored-demand signal is absent exactly where stockouts were worst.",
            fix="A freshness and row-count check per source per day. Missing "
            "partitions must fail a check, not average to zero.",
        )

    # ------------------------------------------------------------------ run
    def apply(self) -> list[Defect]:
        self._log("injecting data defects")
        self.duplicate_events()
        self.late_arrivals()
        self.null_batch_ids()
        self.unit_drift()
        self.inconsistent_returns()
        self.timezone_mix()
        self.sku_code_migration()
        self.clickstream_outage()

        manifest = self.raw_dir.parent / "_manifest"
        manifest.mkdir(parents=True, exist_ok=True)
        (manifest / "dirt.json").write_text(
            json.dumps([d.__dict__ for d in self.defects], indent=2, default=str),
            encoding="utf-8",
        )
        return self.defects


def write_docs(defects: list[Defect], path: Path) -> None:
    """Generate docs/known_data_issues.md - the doc a real data team would keep."""
    lines = [
        "# Known Data Issues",
        "",
        "> **Generated file — do not edit.** Produced by `simulator/dirt.py`.",
        "",
        "The raw layer is deliberately imperfect. Real source feeds arrive with",
        "duplicates, late events, unit drift and outages, and a project whose",
        "Bronze layer is pristine has skipped the part of the job that takes the",
        "time. Each defect below is injected on purpose, is reproducible from the",
        "run seed, and needs a different fix in staging.",
        "",
        "This is the document a data team actually keeps: what is wrong with the",
        "feeds, how you notice, and what the pipeline does about it.",
        "",
        "| # | Defect | Feeds affected |",
        "|---|---|---|",
    ]
    for i, d in enumerate(defects, 1):
        lines.append(f"| {i} | {d.title} | {', '.join(f'`{f}`' for f in d.feeds)} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, d in enumerate(defects, 1):
        lines += [
            f"## {i}. {d.title}",
            "",
            # a bullet list rather than a two-space markdown hard break: the
            # pre-commit whitespace hook strips those, and the generated file
            # would differ from the committed one on every run
            f"- **Feeds** — {', '.join(f'`{f}`' for f in d.feeds)}",
            f"- **Rows affected** — {d.rows:,}",
            "",
            f"**Symptom.** {d.symptom}",
            "",
            f"**Fix.** {d.fix}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent
    injector = DirtInjector(root / "data" / "raw")
    defects = injector.apply()
    write_docs(defects, root / "docs" / "known_data_issues.md")
    print(f"\n{len(defects)} defects injected, docs/known_data_issues.md regenerated")
    sys.exit(0)
