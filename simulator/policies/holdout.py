"""One world, some stores treated, the rest a holdout (task S5.2).

S5.1 compares two *worlds*: every store on the baseline against every store on
Policy B, over common random numbers. It is the cleaner comparison and it is
also one no business could ever run - you cannot operate your whole estate two
ways at once. S5.2 runs the experiment the way it would actually be run: a
randomised subset of stores switches to Policy B on a fixed date, the rest carry
on, and the effect is estimated by difference-in-differences.

That design buys two things the between-world comparison cannot. It has a
pre-period in which both groups are on the baseline, so parallel trends can be
*checked* rather than assumed. And it differences out anything that hits both
groups on the same day - a festival, a monsoon week, a supplier failure - which
is exactly the confounding a before-and-after comparison cannot separate from
the policy.

**SUTVA is not a footnote here, it is a filter.** The stable unit treatment value
assumption says one store's treatment must not change another store's outcome.
Policy B moves stock between stores. A transfer out of a treated store into a
holdout store hands the holdout inventory it would never have had, so the
"control" is partly treated and the estimate is biased toward zero by an amount
nobody can measure afterwards. `transfers` therefore drops any arc whose ends
are in different groups. That is a real cost - it throws away transfers the
policy wanted - and it is the price of the control group meaning anything.

**The switch date is what makes it a difference-in-differences rather than a
comparison of groups.** Before it every store runs the baseline, including the
ones about to be treated, so any pre-existing difference between the groups is
observable instead of being attributed to the policy.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from simulator.policies.base import Policy, PolicyContext, ReplenishmentOrder
from simulator.policies.baseline import BaselinePolicy
from simulator.policies.optimized import OptimizedPolicy


def assign_treatment(n_stores: int, seed: int, share: float = 0.5) -> np.ndarray:
    """Randomly choose which store indices are treated.

    Drawn from its own generator seeded independently of the simulation, so the
    assignment does not move when the simulated world does. That matters for
    reading a set of seeds together: with a shared stream, changing the world
    would also reshuffle who was treated, and a difference between seeds could
    be either.

    Randomised rather than chosen, and the distinction is the whole point of a
    holdout. Picking the treated stores by hand - the busiest, the worst
    performers - makes the groups differ in ways that predict the outcome, and
    no amount of differencing repairs that.
    """
    rng = np.random.default_rng([seed, 20_260_502])
    treated = rng.choice(n_stores, size=max(1, int(round(n_stores * share))), replace=False)
    return np.sort(treated)


class HoldoutPolicy(Policy):
    """Baseline everywhere until the switch date, then Policy B in treated stores."""

    name = "holdout"

    def __init__(
        self,
        cfg,
        catalog,
        treated: np.ndarray,
        switch_date: dt.date,
    ) -> None:
        self.baseline = BaselinePolicy(cfg, catalog)
        self.optimized = OptimizedPolicy(cfg, catalog)
        self.treated = np.asarray(treated, dtype=int)
        self.switch_date = switch_date
        self._treated_set = set(self.treated.tolist())

    # ------------------------------------------------------------------ helpers
    def _is_post(self, ctx: PolicyContext) -> bool:
        return ctx.date >= self.switch_date

    def _treated_mask(self, ctx: PolicyContext) -> np.ndarray:
        """Which store rows are on Policy B today. All False before the switch."""
        mask = np.zeros(ctx.n_stores, dtype=bool)
        if self._is_post(ctx):
            mask[self.treated] = True
        return mask

    # ------------------------------------------------------------------ policy
    def replenish(self, ctx: PolicyContext) -> ReplenishmentOrder:
        """Each group's own order lines, kept only for its own stores.

        Both policies are asked for a full estate's worth and then filtered,
        rather than each being handed a subset. Handing a policy a slice would
        change what it sees - `OptimizedPolicy` reasons about the estate when it
        sets service levels - and the treated stores would then be running
        something subtly different from Policy B, which is what the experiment
        claims to be measuring.
        """
        mask = self._treated_mask(ctx)
        if not mask.any():
            return self.baseline.replenish(ctx)

        base = self.baseline.replenish(ctx)
        opt = self.optimized.replenish(ctx)
        keep_base = ~mask[base.store_idx]
        keep_opt = mask[opt.store_idx]
        return ReplenishmentOrder(
            store_idx=np.concatenate([base.store_idx[keep_base], opt.store_idx[keep_opt]]),
            sku_idx=np.concatenate([base.sku_idx[keep_base], opt.sku_idx[keep_opt]]),
            qty=np.concatenate([base.qty[keep_base], opt.qty[keep_opt]]),
        )

    def markdown(self, ctx: PolicyContext) -> np.ndarray:
        mask = self._treated_mask(ctx)
        if not mask.any():
            return self.baseline.markdown(ctx)
        return np.where(mask[:, None], self.optimized.markdown(ctx), self.baseline.markdown(ctx))

    def deal_slots(self, ctx: PolicyContext) -> dict[int, list[int]]:
        mask = self._treated_mask(ctx)
        if not mask.any():
            return self.baseline.deal_slots(ctx)
        base = self.baseline.deal_slots(ctx)
        opt = self.optimized.deal_slots(ctx)
        return {
            store: (opt.get(store, []) if mask[store] else base.get(store, []))
            for store in set(base) | set(opt)
        }

    def transfers(self, ctx: PolicyContext) -> list[tuple[int, int, int, int]]:
        """Only arcs with both ends in the same group. This is the SUTVA constraint.

        A transfer from a treated store to a holdout store would give the
        holdout stock it would not otherwise have had, making the control group
        partly treated. The bias runs toward zero, and - worse than its size -
        it is invisible in the output: the holdout simply performs better than a
        true control would, and the estimate reads as a smaller effect rather
        than as a broken design.

        Dropping them costs real value. The arcs the optimiser wanted are gone,
        so this design measures Policy B *as it could be run in a partial
        rollout*, not Policy B at full estate coverage. S5.1's between-world
        comparison measures the latter, and the two answering slightly different
        questions is a feature of having both rather than a discrepancy to
        reconcile.
        """
        if not self._is_post(ctx):
            return self.baseline.transfers(ctx)
        return [
            arc
            for arc in self.optimized.transfers(ctx)
            if (arc[0] in self._treated_set) == (arc[1] in self._treated_set)
        ]
