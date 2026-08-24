"""The status quo (task S1.6).

This is the control arm. It has to be a fair representation of how a dark-store
network actually runs, because a rigged control makes the whole Sprint 5 result
worthless - and "how do you know your baseline wasn't a strawman?" is the first
thing a sharp interviewer asks about a before/after number.

So it is competent where the industry is competent. A proper (s, S) reorder
point on a trailing mean is genuinely what most replenishment systems do; the
markdown ladder does fire before expiry rather than after; the deal rail runs
every single day.

It is naive in five specific, nameable ways, each of which the optimised policy
in Sprint 4 attacks separately so the ablation can attribute the gain:

1. **One assumed lead time for every supplier.** The real ones run 0.5 to 3.2
   days with very different variance, so safety stock is simultaneously too
   thin for the erratic supplier and too fat for the reliable one.
2. **No anticipation.** A trailing seven-day mean averages the weekly cycle away
   and cannot see Diwali coming. It under-orders into the run-up, then
   over-orders for a week afterwards on the inflated average.
3. **No shelf-life cap on cover.** Three-day milk gets the same days-of-cover
   rule as 540-day masala.
4. **A flat discount.** Fixed depth by days to expiry, identical everywhere,
   ignoring elasticity, how much is actually left, and whether it would have
   sold at full price anyway.
5. **One deal SKU for the whole city**, picked centrally without reference to
   what is about to expire in any particular store.
"""

from __future__ import annotations

import numpy as np

from simulator.policies.base import Policy, PolicyContext, ReplenishmentOrder

# a cell with no stock reports this as its days-to-expiry
NO_STOCK_DTE = 9999


class BaselinePolicy(Policy):
    """Static reorder point, flat markdown ladder, one citywide deal SKU."""

    def __init__(self, cfg, catalog) -> None:
        spec = cfg.policies["baseline"]
        self.name = spec["name"]

        rep = spec["replenishment"]
        self._review = rep["review_period_days"]
        self._lead = rep["assumed_lead_time_days"]
        self._safety = rep["safety_days"]
        self._min_qty = rep["min_order_qty"]
        self._multiple = rep["order_multiple"]
        self._max_cover = rep["max_days_of_cover"]
        self._cold_start = rep["cold_start_units"]

        md = spec["markdown"]
        self._md_shelf_max = md["applies_to_shelf_life_days_max"]
        # steepest step first, so the first match is the deepest applicable cut
        self._ladder = sorted(md["ladder"], key=lambda r: r["dte_max"])

        deal = spec["deal_slot"]
        self._slots = deal["slots_per_store"]
        self._citywide = deal["citywide"]
        self._rotate_days = deal["rotate_every_days"]

        self._shelf_life = catalog["shelf_life_days"].to_numpy()
        self._deal_eligible = np.flatnonzero(catalog["deal_eligible"].to_numpy())
        self._markdown_eligible = self._shelf_life <= self._md_shelf_max

    # ------------------------------------------------------------- replenish
    def replenish(self, ctx: PolicyContext) -> ReplenishmentOrder:
        """Classic (s, S): top up to a fixed number of days of cover.

        Cover is computed from a flat trailing mean and one assumed lead time,
        with no seasonal anticipation and no shelf-life cap.
        """
        cover_days = self._lead + self._review + self._safety
        target = ctx.trailing_avg * cover_days

        # a SKU that has never sold here still needs a first delivery, or the
        # store could never start selling it at all
        never_sold = (ctx.trailing_avg <= 0) & (ctx.on_hand + ctx.on_order <= 0)
        target = np.where(never_sold, self._cold_start, target)

        if self._max_cover is not None:
            target = np.minimum(target, ctx.trailing_avg * self._max_cover)

        reorder_point = ctx.trailing_avg * (self._lead + self._review)
        position = ctx.on_hand + ctx.on_order
        trigger = position <= reorder_point

        gap = np.maximum(0.0, target - position)
        qty = np.ceil(gap / self._multiple) * self._multiple
        qty = np.where(qty > 0, np.maximum(qty, self._min_qty), 0)

        place = trigger & (qty > 0) & ctx.store_open[:, None]
        stores, skus = np.nonzero(place)
        if stores.size == 0:
            return ReplenishmentOrder.empty()
        return ReplenishmentOrder(stores, skus, qty[stores, skus].astype(np.int64))

    # ------------------------------------------------------------- markdown
    def markdown(self, ctx: PolicyContext) -> np.ndarray:
        """Flat ladder on days to expiry. Same depth everywhere, for everything."""
        out = np.zeros_like(ctx.on_hand, dtype=np.float64)
        has_stock = ctx.on_hand > 0
        # deepest step is applied last so it wins where the bands overlap
        for step in reversed(self._ladder):
            hit = has_stock & (ctx.min_dte <= step["dte_max"]) & self._markdown_eligible[None, :]
            out = np.where(hit, step["discount"], out)
        return out

    # ------------------------------------------------------------- deal rail
    def deal_slots(self, ctx: PolicyContext) -> dict[int, list[int]]:
        """One SKU, chosen centrally, the same in every store, rotated weekly.

        Nothing here looks at stock or expiry. Whether the featured SKU happens
        to be sitting in a store at all is left to chance, which is the point.
        """
        period = ctx.date.toordinal() // self._rotate_days
        picker = np.random.default_rng([period, 555])
        chosen = picker.choice(self._deal_eligible, size=self._slots, replace=False).tolist()

        if self._citywide:
            return {si: chosen for si in range(ctx.n_stores) if ctx.store_open[si]}
        return {
            si: picker.choice(self._deal_eligible, size=self._slots, replace=False).tolist()
            for si in range(ctx.n_stores)
            if ctx.store_open[si]
        }

    # transfers() inherits the base no-op: the baseline moves nothing between
    # stores, which is why stranded stock stays stranded (problem P3).


if __name__ == "__main__":
    import datetime as dt

    from simulator.catalog import build_catalog
    from simulator.config_loader import load_sim_config
    from simulator.policies.base import PolicyContext

    cfg = load_sim_config()
    cat = build_catalog(cfg)
    pol = BaselinePolicy(cfg, cat)
    rng = np.random.default_rng(3)

    S, K = len(cfg.stores), len(cat)
    ctx = PolicyContext(
        date=dt.date(2026, 2, 17),
        on_hand=rng.integers(0, 40, (S, K)).astype(np.int64),
        on_order=np.zeros((S, K), dtype=np.int64),
        trailing_avg=rng.gamma(2.0, 1.5, (S, K)),
        min_dte=rng.integers(0, 12, (S, K)),
        store_open=np.ones(S, dtype=bool),
        catalog=cat,
        rng=rng,
    )

    orders = pol.replenish(ctx)
    md = pol.markdown(ctx)
    deals = pol.deal_slots(ctx)

    print(f"policy            : {pol.name}")
    print(f"order lines       : {len(orders):,} of {S * K:,} cells")
    print(f"units ordered     : {orders.qty.sum():,}")
    print(f"cells marked down : {(md > 0).sum():,}")
    print(f"  at 50%          : {(md == 0.50).sum():,}")
    print(f"  at 30%          : {(md == 0.30).sum():,}")
    deal_sku = next(iter(deals.values()))[0]
    print(f"deal SKU (citywide): {cat.iloc[deal_sku]['sku_name']}")
    print(f"  same in every store: {len({tuple(v) for v in deals.values()}) == 1}")
    print(f"  units of it on hand across the network: {ctx.on_hand[:, deal_sku].sum():,}")
    print(f"transfers         : {len(pol.transfers(ctx))}")
