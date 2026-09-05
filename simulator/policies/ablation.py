"""Policy B with one component switched off (task S5.4).

The experiment says Policy B raises margin about 6% and writes off roughly twice
the value of the baseline. Neither number says *which part* of it is responsible,
and the four components pull in different directions: the newsvendor decides how
much stock exists, markdown and the deal rail decide how it clears, transfers
decide where it sits.

An ablation answers that by removing one component at a time and handing its job
back to the baseline. What the estate loses is that component's contribution.

**Why substitution rather than deletion.** Removing replenishment entirely would
not be a policy without a newsvendor, it would be a store that never orders, and
the comparison would measure the absence of a shop. Each ablation therefore runs
the baseline's version of that one decision and Policy B's version of everything
else, so the difference is attributable to the component rather than to a hole.

**The components do not decompose cleanly, and the readout has to say so.** They
interact: a newsvendor that orders less leaves less for markdown to clear, so
switching either off changes what the other would have done. The sum of the four
individual contributions will therefore not equal the total, and the gap is the
interaction - which is a finding about the policy rather than an error in the
arithmetic. S5.4's gate asks that the parts sum to approximately the whole, and
"approximately" is doing real work: a large gap means the components are not
separable and no per-component attribution should be quoted.
"""

from __future__ import annotations

import numpy as np

from simulator.policies.base import Policy, PolicyContext, ReplenishmentOrder
from simulator.policies.baseline import BaselinePolicy
from simulator.policies.optimized import OptimizedPolicy

# The four decisions Sprint 4 built, each ablatable on its own.
COMPONENTS = ("replenish", "markdown", "deal_slots", "transfers")


class AblatedPolicy(Policy):
    """Policy B everywhere except one decision, which the baseline makes instead."""

    def __init__(self, cfg, catalog, component: str, bundle_path=None) -> None:
        if component not in COMPONENTS:
            raise ValueError(f"unknown component {component!r} - expected one of {COMPONENTS}")
        self.component = component
        self.name = f"optimized_without_{component}"
        self.optimized = OptimizedPolicy(cfg, catalog, bundle_path=bundle_path)
        self.baseline = BaselinePolicy(cfg, catalog)

    def _source(self, component: str) -> Policy:
        """Whichever policy owns this decision under this ablation."""
        return self.baseline if component == self.component else self.optimized

    def replenish(self, ctx: PolicyContext) -> ReplenishmentOrder:
        return self._source("replenish").replenish(ctx)

    def markdown(self, ctx: PolicyContext) -> np.ndarray:
        return self._source("markdown").markdown(ctx)

    def deal_slots(self, ctx: PolicyContext) -> dict[int, list[int]]:
        return self._source("deal_slots").deal_slots(ctx)

    def transfers(self, ctx: PolicyContext) -> list[tuple[int, int, int, int]]:
        return self._source("transfers").transfers(ctx)
