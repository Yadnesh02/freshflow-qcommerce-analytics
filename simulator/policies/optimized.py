"""The treatment arm: Sprint 4's decisions, applied day by day (task S4.6).

This closes the loop. The baseline docstring lists five specific ways it is
naive, and this policy attacks them one at a time so the Sprint 5 ablation can
attribute the gain to a named component rather than to "the new system":

  1. one assumed lead time      -> per-category lead time at its 90th percentile
  2. no anticipation            -> negative-binomial service level, not a flat mean
  3. no shelf-life cap on cover -> the newsvendor cap from S4.5
  4. a flat discount ladder     -> the elasticity-driven rule from S4.2
  5. one citywide deal SKU      -> the per-store allocator from S4.3
                                   (plus transfers, which the baseline has none of)

**It reads fitted parameters, never recommendations.** The rec_* tables were
computed on a warehouse built from the whole year. A policy on day 120 that
looked up its own row would be reading what happened on day 300, and Sprint 5
would end up measuring look-ahead. So `policy_bundle.json` carries coefficients
and knobs, and this class applies them to whatever `PolicyContext` exposes that
morning - the same information the baseline gets.

**It uses the estimated elasticity, not the simulator's.** `catalog["elasticity"]`
is the true coefficient the demand engine draws from. Reading it would produce
a wonderful result that means nothing, because no real business has that column.
What this reads is `mart_price_elasticity`, fitted from emitted events, wrong in
the ways S4.1 documented, and silent on nine of its twenty-three cells.

**Where the data said "do nothing", this does nothing.** S4.2's finding was that
at the fitted elasticities a markdown never pays: while stock is short of demand
the objective reduces to revenue, and revenue falls with price whenever the
elasticity is inside the unit interval, which all of them are. So this policy
marks down far *less* than the baseline, not more. That is the recommendation
the analysis produced, and wiring it faithfully is the point - a treatment arm
tuned until it wins would make the experiment worthless.

**Deal slots are greedy here, not the integer program.** S4.3 solves a proper
IP, but Sprint 5 runs 30 seeds x 90 days x 14 stores, which is 37,800 solver
calls and hours of CBC. The greedy takes the same objective and the same three
binding constraints - slot count, one per subcategory, private-label floor - and
picks in value order. `test_the_greedy_tracks_the_integer_program` measures the
gap on the as-of day rather than asserting it is zero, because it is not zero
and the honest claim is that it is small.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy import stats

from simulator.policies.base import Policy, PolicyContext, ReplenishmentOrder

BUNDLE_PATH = Path(__file__).resolve().parent.parent / "config" / "policy_bundle.json"

# a cell with no stock reports this as its days-to-expiry
NO_STOCK_DTE = 9999


class OptimizedPolicy(Policy):
    """Forecast-driven ordering, elasticity-driven markdown, per-store deal slots."""

    def __init__(self, cfg, catalog, bundle_path: Path | None = None) -> None:
        spec = cfg.policies["optimized"]
        self.name = spec["name"]

        path = Path(bundle_path or BUNDLE_PATH)
        if not path.exists():
            raise FileNotFoundError(
                f"no policy bundle at {path}. Run `python tasks.py policy-bundle` - "
                "the optimised arm is Sprint 4's output and cannot be configured by hand."
            )
        self.bundle = json.loads(path.read_text(encoding="utf-8"))

        self._catalog = catalog
        self._shelf_life = catalog["shelf_life_days"].to_numpy().astype(float)
        self._base_price = catalog["base_price"].to_numpy().astype(float)
        self._landed_cost = catalog["landed_cost"].to_numpy().astype(float)
        self._is_private_label = catalog["is_private_label"].to_numpy().astype(bool)
        self._deal_eligible = catalog["deal_eligible"].to_numpy().astype(bool)
        self._l1 = catalog["l1_category"].to_numpy()
        self._l2 = catalog["l2_subcategory"].to_numpy()

        demand = self.bundle["demand"]
        self._k = float(demand["dispersion_k"])

        nv = self.bundle["newsvendor"]
        self._review = int(nv["review_period_days"])
        self._cap_quantile = float(nv["shelf_life_cap_quantile"])
        self._retention_cost = float(nv["stockout_retention_cost"])

        # lead time per category, falling back to the pooled figure. The
        # baseline uses one number for everything; this is naivety #1.
        lead = self.bundle["lead_time_days"]
        pooled = float(lead["pooled_p90"])
        by_category = {c: float(v["p90"]) for c, v in lead["by_category"].items()}
        self._lead_days = np.array([by_category.get(c, pooled) for c in self._l1])

        # estimated elasticity, looked up by category and freshness band. Cells
        # S4.1 could not identify are left as NaN and never priced.
        self._elasticity_bands = [
            cell for cell in self.bundle["elasticity"] if cell["is_identified"]
        ]

        md = self.bundle["markdown"]
        self._depth_grid = np.asarray(md["depth_grid"], dtype=float)
        self._disposal_cost = float(md["disposal_cost_per_unit"])

        deal = self.bundle["deal_slot"]
        self._deal_price = float(deal["deal_price"])
        self._slots = int(deal["slots_per_store_day"])
        self._pl_floor = float(deal["private_label_floor"])
        self._uplift = float(deal["uplift_multiplier"])
        self._basket_margin = float(deal["incremental_basket_margin"])
        self._reactivation = float(deal["reactivation_rate"]) * float(deal["reactivation_value"])
        self._deal_min_on_hand = float(deal["min_on_hand_units"])
        self._deal_min_dte = float(deal["min_days_to_expiry"])

        self._cold_start = spec["replenishment"]["cold_start_units"]
        self._min_qty = spec["replenishment"]["min_order_qty"]
        self._multiple = spec["replenishment"]["order_multiple"]

    # ------------------------------------------------------------- helpers
    def _negbin_quantile(self, mean: np.ndarray, q: np.ndarray | float) -> np.ndarray:
        """Inverse CDF with Var = mu + mu^2/k, matching the fitted dispersion."""
        mean = np.maximum(mean, 1e-9)
        p = self._k / (self._k + mean)
        return stats.nbinom.ppf(q, self._k, p)

    def _elasticity_for(self, min_dte: np.ndarray) -> np.ndarray:
        """Estimated elasticity per store-SKU, NaN where nothing was identified.

        Bands are half-open, matching how S4.1 fitted them and S4.2 applied
        them. A cell whose interval covers zero is not a small effect - it is no
        measurement, and it stays NaN so the markdown rule declines to act.
        """
        out = np.full(min_dte.shape, np.nan, dtype=float)
        for cell in self._elasticity_bands:
            in_band = (min_dte >= cell["min_days"]) & (min_dte < cell["max_days"])
            in_category = self._l1 == cell["l1_category"]
            out = np.where(in_band & in_category[None, :], cell["elasticity_raw"], out)
        return out

    # ------------------------------------------------------------- replenish
    def replenish(self, ctx: PolicyContext) -> ReplenishmentOrder:
        """Perishable newsvendor: service level from the critical ratio, capped
        by what can actually sell before it expires.

        The baseline tops up to a fixed number of days of cover on one assumed
        lead time with no shelf-life cap. All three of those are replaced here.
        """
        lead = self._lead_days[None, :]
        protection = lead + self._review
        demand_protection = ctx.trailing_avg * protection
        demand_usable = ctx.trailing_avg * np.maximum(self._shelf_life, 1.0)[None, :]

        underage = (self._base_price - self._landed_cost)[None, :] + self._retention_cost

        # the most that can plausibly clear before expiry. Above this an order
        # is buying units that cannot sell in time, whatever the service level.
        cap = self._negbin_quantile(demand_usable, self._cap_quantile)

        # P(spoil) and the quantity are mutually determined; iterate as S4.5 does
        spoil = np.full(ctx.on_hand.shape, 0.5)
        order_up_to = np.zeros_like(ctx.on_hand, dtype=float)
        for _ in range(6):
            overage = self._landed_cost[None, :] * spoil
            critical_ratio = np.clip(underage / (underage + overage), 1e-6, 1.0 - 1e-9)
            order_up_to = np.minimum(self._negbin_quantile(demand_protection, critical_ratio), cap)
            mean = np.maximum(demand_usable, 1e-9)
            p = self._k / (self._k + mean)
            spoil = stats.nbinom.cdf(order_up_to - 1, self._k, p)

        never_sold = (ctx.trailing_avg <= 0) & (ctx.on_hand + ctx.on_order <= 0)
        order_up_to = np.where(never_sold, self._cold_start, order_up_to)

        position = ctx.on_hand + ctx.on_order
        gap = np.maximum(0.0, order_up_to - position)
        qty = np.ceil(gap / self._multiple) * self._multiple
        qty = np.where(qty > 0, np.maximum(qty, self._min_qty), 0)

        place = (qty > 0) & ctx.store_open[:, None]
        stores, skus = np.nonzero(place)
        if stores.size == 0:
            return ReplenishmentOrder.empty()
        return ReplenishmentOrder(stores, skus, qty[stores, skus].astype(np.int64))

    # ------------------------------------------------------------- markdown
    def markdown(self, ctx: PolicyContext) -> np.ndarray:
        """Cut where a cut pays, evaluating S4.2's objective over the depth grid.

        **This rule used to test `beta < -1` and stop, and that was a defect
        rather than a simplification.** The reasoning behind it was sound as far
        as it went: while stock is short of demand the margin is
        `sold x price - qty x cost`, the cost term does not move with price, so
        the decision reduces to revenue and `r ** (1 + beta)` is increasing in
        price for every coefficient S4.1 fitted. But that derivation silently
        assumes an unsold unit costs nothing to be rid of. S4.2's objective does
        not assume it - `analytics/optimization/markdown.py` evaluates
        `sold x (price - cost) - unsold x (cost + disposal_cost)` - and the
        bundle has carried `disposal_cost_per_unit` since S4.6.

        So the policy hardcoded the very parameter S5.3 exists to vary, and the
        consequence was not academic. The 180-day holdout found Policy B
        expiring 76% MORE units than the baseline by the final month and writing
        off 314% more value, in all thirty seeds, with the cost per expired unit
        doubling from Rs 44.49 to Rs 93.37. A policy that can never mark down
        has no price mechanism for clearing expensive at-risk stock, so the
        expensive stock is what accumulates and dies. The baseline's crude flat
        ladder gives away margin and does clear it.

        The objective is now evaluated over the whole depth grid, so at a zero
        disposal cost this still holds price nearly everywhere - the original
        finding survives, because it was a correct reading of that assumption -
        and above the breakeven it starts cutting.
        """
        out = np.zeros_like(ctx.on_hand, dtype=np.float64)
        beta = self._elasticity_for(ctx.min_dte)
        usable = (ctx.on_hand > 0) & np.isfinite(beta)
        if not usable.any():
            return out

        qty = ctx.on_hand.astype(np.float64)
        price = self._base_price[None, :]
        cost = self._landed_cost[None, :]
        # Demand over the remaining shelf life, which is the window the decision
        # is actually about: what does not sell before `min_dte` is written off.
        horizon = np.maximum(ctx.min_dte, 1.0)
        expected = np.maximum(ctx.trailing_avg * horizon, 0.0)
        # Never price through landed cost. A depth that does is not a cheaper
        # way to clear, it is a more expensive way to write off.
        floor_ratio = cost / np.maximum(price, 1e-9)

        best_depth = np.zeros_like(out)
        best_margin = np.full_like(out, -np.inf)
        for depth in self._depth_grid:
            ratio = 1.0 - float(depth)
            if ratio <= 0:
                continue
            demand = expected * ratio**beta
            sold = np.minimum(qty, demand)
            margin = sold * (price * ratio - cost) - (qty - sold) * (cost + self._disposal_cost)
            better = usable & (margin > best_margin) & (ratio >= floor_ratio)
            best_depth = np.where(better, float(depth), best_depth)
            best_margin = np.where(better, margin, best_margin)

        return np.where(usable, np.clip(best_depth, 0.0, 0.6), out)

    # ------------------------------------------------------------- deal rail
    def deal_slots(self, ctx: PolicyContext) -> dict[int, list[int]]:
        """Per store, by value, under the three constraints that bind.

        Greedy rather than the integer program: Sprint 5 runs tens of thousands
        of store-days and CBC would dominate the runtime. Same objective, same
        constraints, picked in value order.
        """
        eligible = (
            self._deal_eligible[None, :]
            & (ctx.on_hand >= self._deal_min_on_hand)
            & (ctx.min_dte >= self._deal_min_dte)
            & (ctx.min_dte < NO_STOCK_DTE)
            & (self._base_price[None, :] > self._deal_price)
        )

        uptake = np.minimum(ctx.trailing_avg * self._uplift, ctx.on_hand)
        # a write-off avoided: stock that will not clear before it expires
        residual = np.maximum(ctx.on_hand - ctx.trailing_avg * np.maximum(ctx.min_dte, 0.0), 0.0)
        clearance = np.minimum(uptake, residual) * self._landed_cost[None, :]
        basket = uptake * self._basket_margin
        reactivation = uptake * self._reactivation
        item_delta = uptake * (self._deal_price - self._landed_cost[None, :]) - (
            ctx.trailing_avg * (self._base_price - self._landed_cost)[None, :]
        )
        value = np.where(eligible, clearance + basket + reactivation + item_delta, -np.inf)

        required_pl = int(np.ceil(self._pl_floor * self._slots))
        chosen: dict[int, list[int]] = {}
        for store in range(ctx.n_stores):
            if not ctx.store_open[store]:
                chosen[store] = []
                continue
            picks: list[int] = []
            used_subcategories: set[str] = set()
            order = np.argsort(-value[store])
            # private label first, so the floor is met by the best PL available
            # rather than by whatever is left once the slots are gone
            for pass_pl in (True, False):
                for sku in order:
                    if len(picks) >= self._slots:
                        break
                    if not np.isfinite(value[store, sku]):
                        break
                    if pass_pl and not self._is_private_label[sku]:
                        continue
                    if pass_pl and sum(self._is_private_label[s] for s in picks) >= required_pl:
                        break
                    if sku in picks or self._l2[sku] in used_subcategories:
                        continue
                    picks.append(int(sku))
                    used_subcategories.add(self._l2[sku])
            chosen[store] = picks
        return chosen

    # ------------------------------------------------------------- transfers
    def transfers(self, ctx: PolicyContext) -> list[tuple[int, int, int, int]]:
        """Move stock that will not clear to a store that is short of it.

        Gated exactly as S4.4 gates it: the receiving store must be able to sell
        the units before they expire, transit included. Without a distance
        matrix in the policy context, transit is the bundle's handling time plus
        the network's median leg, which is conservative for the near pairs and
        about right for the median one.
        """
        cfg = self.bundle["transfers"]
        transit_days = (cfg["handling_hours"] + 12.84 / cfg["speed_kmh"]) / 24.0
        min_units = int(cfg["min_transfer_units"])
        # the economic gate S4.4 established, which the first draft of this
        # method dropped. Without it the policy moved 3,296 loads and Rs 11.2m
        # of stock in a single day, against the three transfers the offline
        # engine found worth making on the same network. A shelf-life gate
        # alone says a move is *possible*; it takes the trip cost to say it is
        # worth doing.
        trip_cost = float(cfg["fixed_trip_cost"]) + float(cfg["cost_per_km"]) * 12.84
        # A store dispatches vans, not parcels. Without this the rule proposed
        # 2,715 separate loads in one day - physically impossible, and the
        # reason the benefit test looked like it was working when it was not:
        # `qty x price > trip_cost` clears about five units of anything, so it
        # rejected almost nothing. The binding constraint on transfers is the
        # loading bay, and it belongs in the model.
        max_trips_per_store = int(cfg.get("max_outbound_trips_per_store_day", 2))

        dte = np.where(ctx.min_dte >= NO_STOCK_DTE, 0.0, ctx.min_dte)
        # Stock that will not clear where it stands - measured against the same
        # negative binomial the rest of the project uses, not against the mean.
        # Subtracting mean demand calls half the shelf "surplus" by construction,
        # which is how the first draft justified moving a quarter of the network
        # every day. A high quantile asks the sharper question: how much is left
        # even if demand comes in strong?
        will_clear = self._negbin_quantile(ctx.trailing_avg * dte, 0.90)
        surplus = np.maximum(ctx.on_hand - will_clear, 0.0)
        deficit = np.maximum(ctx.trailing_avg * (dte + 1.0) - (ctx.on_hand + ctx.on_order), 0.0)
        unit_benefit = self._landed_cost + np.maximum(self._base_price - self._landed_cost, 0.0)

        candidates: list[tuple[float, int, int, int, int]] = []
        movable = np.flatnonzero((surplus.sum(axis=0) >= min_units) & (deficit.sum(axis=0) > 0))
        for sku in movable:
            sellable = dte[:, sku] - transit_days
            senders = np.argsort(-surplus[:, sku])
            for to_store in np.flatnonzero(deficit[:, sku] >= min_units):
                if sellable[to_store] <= 0:
                    continue
                capacity = min(
                    deficit[to_store, sku],
                    ctx.trailing_avg[to_store, sku] * sellable[to_store],
                )
                for from_store in senders:
                    if from_store == to_store or capacity < min_units:
                        continue
                    qty = int(min(surplus[from_store, sku], capacity))
                    if qty < min_units:
                        continue
                    # One van per store pair per SKU, so the trip is paid in
                    # full rather than shared across a load. Conservative in the
                    # right direction: it declines marginal moves the batched
                    # offline engine would accept, never the reverse. The
                    # benefit is the write-off avoided on units that were
                    # genuinely not going to clear, plus the margin they earn
                    # where the demand actually is.
                    net = qty * unit_benefit[sku] - trip_cost
                    if net <= 0:
                        continue
                    candidates.append((net, int(from_store), int(to_store), int(sku), qty))
                    surplus[from_store, sku] -= qty
                    capacity -= qty

        # best first, then hand out the day's vans. Grouping by store pair means
        # several SKUs can ride together, which is how the offline engine shares
        # a trip - so the pair is the unit that consumes a slot, not the line.
        candidates.sort(reverse=True)
        dispatched: dict[int, set[tuple[int, int]]] = {}
        moves: list[tuple[int, int, int, int]] = []
        for _net, from_store, to_store, sku, qty in candidates:
            pairs = dispatched.setdefault(from_store, set())
            if (from_store, to_store) not in pairs and len(pairs) >= max_trips_per_store:
                continue
            pairs.add((from_store, to_store))
            moves.append((from_store, to_store, sku, qty))
        return moves
