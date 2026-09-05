"""Batch-level inventory: FEFO allocation, expiry write-offs, substitution (task S1.5).

This is the keystone module. Every sale is allocated to a specific physical
batch with a specific expiry date, and that `batch_id` travels onto the order
line. Without it there is no way to attribute a sale to an expiry date, and
the entire expiry-risk, freshness and markdown analysis becomes impossible.

Three things happen here.

**FEFO allocation.** First-expiring, first-out - not FIFO. A batch that arrived
later but expires sooner must go first, which happens routinely once inbound
freshness varies by supplier. Ties break on arrival order.

**Expiry write-offs.** Stock past its expiry date is written off at full landed
cost. Perishables have no salvage value; that is what makes the whole problem
expensive.

**Substitution.** A stockout does not simply vanish. Some demand moves to
another SKU in the same subcategory, some to private label, and the rest is
genuinely lost. That last share is what makes availability a revenue problem
rather than a service statistic, and the private-label share is a natural
experiment the P5 analysis can exploit later.

The ledger is an event log, not a stock snapshot. On-hand is always derivable
by summing movements, and a test asserts the two agree exactly - the same
reconciliation the dbt layer will run in Sprint 2.
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from simulator.config_loader import SimConfig

# movement event types, matching fct_inventory_movement in the ERD
INBOUND = "inbound"
# an opening balance is not a delivery: it is the stock already on the shelf
# when the window opens. It needs its own type so the ledger reconciles
# without pretending a year-one migration was a purchase order.
OPENING_BALANCE = "opening_balance"
SALE = "sale"
EXPIRY_WRITEOFF = "expiry_writeoff"
TRANSFER_IN = "transfer_in"
TRANSFER_OUT = "transfer_out"
DAMAGE = "damage"

# a customer notices a near-expiry item below this share of its shelf life
LOW_DTE_FRACTION = 0.25


@dataclass
class Allocation:
    """One sale, resolved against one batch."""

    __slots__ = ("batch_row", "qty", "dte_at_sale", "shelf_life_fraction")
    batch_row: int
    qty: int
    dte_at_sale: int
    shelf_life_fraction: float


@dataclass
class InventoryLedger:
    """Batch-level stock for every store x SKU cell, consumed first-expiring-first."""

    n_stores: int
    n_skus: int

    def __post_init__(self) -> None:
        # (store, sku) -> list of [expiry_ord, sequence, batch_row, remaining]
        # kept sorted, so index 0 is always the batch that expires soonest
        self._queues: dict[tuple[int, int], list[list[int]]] = {}
        self._on_hand = np.zeros((self.n_stores, self.n_skus), dtype=np.int64)
        self._seq = 0
        # A monotonic sequence on every movement. Without it an event log
        # cannot be replayed: 100+ sales can share a batch and a date, and
        # SUM(qty_delta) OVER (ORDER BY event_date) is then non-deterministic.
        self._move_seq = 0
        self.movements: list[tuple] = []
        # batch attributes, appended as batches are received
        self._batch_expiry: list[int] = []
        self._batch_shelf_life: list[int] = []
        self._batch_cell: list[tuple[int, int]] = []
        self._batch_cost: list[float] = []

    # ------------------------------------------------------------------ inbound
    def receive(
        self,
        store_idx: np.ndarray,
        sku_idx: np.ndarray,
        qty: np.ndarray,
        expiry_ord: np.ndarray,
        shelf_life: np.ndarray,
        unit_cost: np.ndarray,
        date_ord: int,
        event_type: str = INBOUND,
    ) -> np.ndarray:
        """Add batches to stock. Returns the ledger's row index for each batch."""
        start = len(self._batch_expiry)
        rows = np.arange(start, start + len(qty))

        # Every batch gets a row, including zero-quantity ones. Skipping them
        # would slide every later batch's row index by one and silently
        # mis-attribute movements to the wrong batch.
        for i in range(len(qty)):
            cell = (int(store_idx[i]), int(sku_idx[i]))
            self._batch_expiry.append(int(expiry_ord[i]))
            self._batch_shelf_life.append(int(shelf_life[i]))
            self._batch_cell.append(cell)
            self._batch_cost.append(float(unit_cost[i]))

            units = int(qty[i])
            if units <= 0:
                continue

            row = int(rows[i])
            self._seq += 1
            bisect.insort(
                self._queues.setdefault(cell, []), [int(expiry_ord[i]), self._seq, row, units]
            )
            self._on_hand[cell] += units
            self._move_seq += 1
            self.movements.append((event_type, row, units, date_ord, self._move_seq))

        return rows

    # ------------------------------------------------------------------ outbound
    def allocate(
        self, store_idx: int, sku_idx: int, qty: int, date_ord: int
    ) -> tuple[list[Allocation], int]:
        """Consume `qty` first-expiring-first. Returns what was taken and any shortfall."""
        q = self._queues.get((store_idx, sku_idx))
        if not q or qty <= 0:
            return [], max(qty, 0)

        out: list[Allocation] = []
        remaining = qty
        while remaining > 0 and q:
            entry = q[0]
            take = min(entry[3], remaining)
            entry[3] -= take
            remaining -= take

            row = entry[2]
            dte = self._batch_expiry[row] - date_ord
            shelf = self._batch_shelf_life[row]
            out.append(Allocation(row, take, dte, dte / shelf if shelf else 0.0))
            self._move_seq += 1
            self.movements.append((SALE, row, -take, date_ord, self._move_seq))

            if entry[3] == 0:
                q.pop(0)

        self._on_hand[store_idx, sku_idx] -= qty - remaining
        if not q:
            self._queues.pop((store_idx, sku_idx), None)
        return out, remaining

    def expire(self, date_ord: int) -> list[tuple[int, int, int]]:
        """Write off everything past its expiry date.

        Returns (batch_row, units, store_idx). The store is included because
        S5.2's difference-in-differences needs write-offs attributed to a store,
        and this loop already holds that - `cell` is (store_idx, sku_idx). Every
        other counter in the day is accumulated inside the per-store loop in
        `SimulationRun._step`; expiry happens before it, so without the store
        here it is the one outcome that could only be reported estate-wide.
        """
        written: list[tuple[int, int, int]] = []
        empty_cells: list[tuple[int, int]] = []

        for cell, q in self._queues.items():
            while q and q[0][0] < date_ord:
                entry = q.pop(0)
                units = entry[3]
                if units > 0:
                    self._on_hand[cell] -= units
                    self._move_seq += 1
                    self.movements.append(
                        (EXPIRY_WRITEOFF, entry[2], -units, date_ord, self._move_seq)
                    )
                    written.append((entry[2], units, cell[0]))
            if not q:
                empty_cells.append(cell)

        for cell in empty_cells:
            self._queues.pop(cell, None)
        return written

    # ------------------------------------------------------------------ state
    def on_hand(self, store_idx: int, sku_idx: int) -> int:
        return int(self._on_hand[store_idx, sku_idx])

    @property
    def on_hand_matrix(self) -> np.ndarray:
        return self._on_hand

    def batch_cost(self, batch_row: int) -> float:
        return self._batch_cost[batch_row]

    def drain_movements(self) -> list[tuple]:
        """Hand over the movement log and start a fresh one, to bound memory."""
        out = self.movements
        self.movements = []
        return out

    def min_dte_matrix(self, date_ord: int, empty: int = 9999) -> np.ndarray:
        """Days to expiry of the soonest-expiring batch in each cell.

        What a flat markdown ladder actually keys on: the store marks the whole
        facing down when anything behind it is close to going off.
        """
        out = np.full((self.n_stores, self.n_skus), empty, dtype=np.int64)
        for (si, ki), q in self._queues.items():
            if q:
                out[si, ki] = q[0][0] - date_ord
        return out

    def weighted_dte_matrix(self, date_ord: int, empty: int = 9999) -> np.ndarray:
        """Stock-weighted mean days to expiry per cell.

        Distinct from min_dte on purpose. A flat markdown ladder keys on the
        *soonest* expiry, because the store marks the whole facing down when
        anything behind it is going off. But customer freshness aversion has to
        key on the average, or a single near-expiry unit sitting behind fifty
        fresh ones would suppress demand for all fifty.
        """
        out = np.full((self.n_stores, self.n_skus), empty, dtype=np.float64)
        for (si, ki), q in self._queues.items():
            units = sum(e[3] for e in q)
            if units > 0:
                out[si, ki] = sum((e[0] - date_ord) * e[3] for e in q) / units
        return out

    def reconcile(self) -> np.ndarray:
        """On-hand rebuilt from the queues, for comparison against the counters."""
        rebuilt = np.zeros_like(self._on_hand)
        for (si, ki), q in self._queues.items():
            rebuilt[si, ki] = sum(e[3] for e in q)
        return rebuilt


# =============================================================== substitution
@dataclass
class Fulfiller:
    """Turns wanted units into sold units, routing stockouts through substitution."""

    cfg: SimConfig
    catalog: pd.DataFrame

    substitutions: dict[str, np.ndarray] = field(init=False)

    def __post_init__(self) -> None:
        cats = [c.l1 for c in self.cfg.categories]
        cat_index = {n: i for i, n in enumerate(cats)}
        self._sku_cat = self.catalog["l1_category"].map(cat_index).to_numpy()
        self._is_private = self.catalog["is_private_label"].to_numpy()
        self._popularity = self.catalog["popularity_weight"].to_numpy()

        sub = self.cfg.raw["catalog"]["substitution"]
        keys = ("within_subcategory", "to_private_label", "lost")
        base = np.array([sub[k] for k in keys])
        self._probs = np.tile(base, (len(cats), 1))
        for name, override in (sub.get("overrides") or {}).items():
            self._probs[cat_index[name]] = [override[k] for k in keys]

        # candidate substitutes, precomputed per SKU
        groups = self.catalog.groupby("l2_subcategory").indices
        self._peers: list[np.ndarray] = []
        self._pl_peers: list[np.ndarray] = []
        for i, subcat in enumerate(self.catalog["l2_subcategory"]):
            members = groups[subcat]
            peers = members[members != i]
            self._peers.append(peers)
            self._pl_peers.append(peers[self._is_private[peers]])

    def _pick(self, candidates: np.ndarray, rng: np.random.Generator) -> int | None:
        if candidates.size == 0:
            return None
        w = self._popularity[candidates]
        total = w.sum()
        if total <= 0:
            return int(rng.choice(candidates))
        return int(rng.choice(candidates, p=w / total))

    def fulfil_line(
        self,
        ledger: InventoryLedger,
        store_idx: int,
        sku_idx: int,
        qty: int,
        date_ord: int,
        rng: np.random.Generator,
    ) -> tuple[list[tuple[int, Allocation]], int, bool]:
        """Fulfil one order line, substituting where stock has run out.

        Returns the allocations as (sku_idx, Allocation) pairs, the units
        genuinely lost, and whether the customer hit a stockout at all.
        """
        allocs, short = ledger.allocate(store_idx, sku_idx, qty, date_ord)
        result = [(sku_idx, a) for a in allocs]
        if short == 0:
            return result, 0, False

        lost = 0
        probs = self._probs[self._sku_cat[sku_idx]]
        # each unfulfilled unit independently switches, downgrades, or walks away
        outcomes = rng.choice(3, size=short, p=probs)

        for outcome in outcomes:
            if outcome == 2:  # lost
                lost += 1
                continue
            candidates = self._peers[sku_idx] if outcome == 0 else self._pl_peers[sku_idx]
            alt = self._pick(candidates, rng)
            if alt is None:
                lost += 1
                continue
            alt_allocs, alt_short = ledger.allocate(store_idx, alt, 1, date_ord)
            if alt_short:
                lost += 1  # the substitute was out too
            else:
                result.extend((alt, a) for a in alt_allocs)

        return result, lost, True


def to_movement_frame(
    movements: list[tuple], batch_ids: list[str], epoch: dt.date | None = None
) -> pd.DataFrame:
    """Materialise a drained movement log as the fct_inventory_movement feed."""
    if not movements:
        return pd.DataFrame(
            {
                c: pd.Series(dtype=t)
                for c, t in {
                    "batch_id": "object",
                    "event_type": "object",
                    "qty_delta": "int64",
                    "event_date": "object",
                }.items()
            }
        )
    kinds, rows, deltas, ords, seqs = zip(*movements, strict=True)
    return pd.DataFrame(
        {
            "movement_seq": list(seqs),
            "batch_id": [batch_ids[r] for r in rows],
            "event_type": list(kinds),
            "qty_delta": list(deltas),
            "event_date": [dt.date.fromordinal(o) for o in ords],
        }
    )


if __name__ == "__main__":
    from simulator.catalog import build_catalog
    from simulator.config_loader import load_sim_config

    cfg = load_sim_config()
    cat = build_catalog(cfg)
    rng = np.random.default_rng(11)

    ledger = InventoryLedger(len(cfg.stores), len(cat))
    ful = Fulfiller(cfg, cat)
    today = dt.date(2026, 2, 17).toordinal()

    # two batches of the same SKU: the one that ARRIVED SECOND expires FIRST,
    # which is exactly the case FIFO gets wrong and FEFO gets right
    ledger.receive(
        np.array([0, 0]),
        np.array([5, 5]),
        np.array([40, 25]),
        np.array([today + 9, today + 3]),
        np.array([10, 10]),
        np.array([20.0, 20.0]),
        today,
    )
    allocs, short = ledger.allocate(0, 5, 30, today)
    print("FEFO allocation of 30 units from two batches:")
    for a in allocs:
        print(f"  batch row {a.batch_row}  qty {a.qty:>3}  expires in {a.dte_at_sale} days")
    print(f"  shortfall {short}, on hand now {ledger.on_hand(0, 5)}\n")

    # a stockout, routed through substitution
    ledger2 = InventoryLedger(len(cfg.stores), len(cat))
    ful2 = Fulfiller(cfg, cat)
    milk = cat.index[cat["l2_subcategory"] == "Milk"].to_numpy()
    ledger2.receive(
        np.zeros(len(milk) - 1, dtype=int),
        milk[1:],
        np.full(len(milk) - 1, 50),
        np.full(len(milk) - 1, today + 3),
        np.full(len(milk) - 1, 3),
        np.full(len(milk) - 1, 30.0),
        today,
    )
    got, lost, hit = ful2.fulfil_line(ledger2, 0, int(milk[0]), 100, today, rng)
    substituted = sum(a.qty for s, a in got if s != milk[0])
    print("100 units wanted of an out-of-stock milk SKU:")
    print(f"  substituted {substituted}, lost {lost}, stockout experienced: {hit}")

    counted = ledger2.reconcile()
    print(
        f"\nledger reconciles to its movement counters: "
        f"{np.array_equal(counted, ledger2.on_hand_matrix)}"
    )
