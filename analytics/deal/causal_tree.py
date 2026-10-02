"""An honest causal tree, which splits on the effect rather than on the outcome.

Four estimators in `uplift.py` lose to random targeting on this data, and the
diagnosis in D4 located the gap precisely: 84% of customers are identifiable
from pre-period behaviour, the oracle on the latent segment clears a Qini of
+96,764, and segment-level uplift runs from +Rs 58.5 to -Rs 97.1. The signal is
there and the features reach the customers. What no estimator tried converts is
identification into *ranking*, because each of them optimises something that is
not the treatment effect:

    T-learner / X-learner   model the outcome, then subtract. On an outcome with
                            a Rs 700 standard deviation and an effect of a few
                            rupees, the subtraction is mostly error.
    K-means two-stage       feature-space variance. Clusters that are far apart
                            in feature space need not differ in response.
    transformed outcome     the right target, but at p=0.80 a control customer
                            carries -5Y and the variance eats the objective.

A causal tree (Athey & Imbens, 2016) attacks it head on: **every split is chosen
to maximise the difference in treatment effect between the two children.** It
never asks who orders. It asks only where the treatment does something
different, which is the single question a targeting policy needs answered.

**Honesty is not optional here, it is the whole safeguard.** The criterion below
rewards splits that make effects differ, and on noise it will happily find some
- an ordinary tree would carve out leaves whose apparent effects are artefacts
of the same rows that chose the split. So the sample is halved: one half decides
the structure, the other estimates the effects in the leaves it produced. A leaf
whose effect was noise gets a near-zero estimate from data that had no say in
creating it. That is what makes a positive result here mean something, and it is
why this is the one method worth adding after four failures.

**Both arms must survive every leaf.** A leaf with no control customers has no
counterfactual and therefore no effect - only a treated mean, which is not the
same thing and is exactly the number that would make this look like it worked.
`MIN_LEAF_PER_ARM` is the guard, and it binds much earlier than depth does.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MIN_LEAF_PER_ARM = 150
MAX_DEPTH = 4
CANDIDATE_SPLITS = 24


@dataclass
class Node:
    """A leaf carries an effect; an internal node carries a question."""

    effect: float = 0.0
    n_treated: int = 0
    n_control: int = 0
    feature: str | None = None
    threshold: float = 0.0
    left: Node | None = None
    right: Node | None = None

    @property
    def is_leaf(self) -> bool:
        return self.feature is None


@dataclass
class CausalTree:
    """Honest causal tree. Structure from one half, effects from the other."""

    features: list[str]
    max_depth: int = MAX_DEPTH
    min_leaf_per_arm: int = MIN_LEAF_PER_ARM
    seed: int = 42
    root: Node | None = field(default=None, init=False)

    # ------------------------------------------------------------------ fit
    def fit(self, frame: pd.DataFrame, outcome: str) -> CausalTree:
        rng = np.random.default_rng(self.seed)
        structure = rng.random(len(frame)) < 0.5
        build = frame[structure]
        estimate = frame[~structure]
        if build.empty or estimate.empty:
            raise ValueError("not enough rows to split into structure and estimation halves")

        self.root = self._grow(build, outcome, depth=0)
        self._estimate(self.root, estimate, outcome, fallback=_effect(estimate, outcome))
        return self

    def _grow(self, frame: pd.DataFrame, outcome: str, depth: int) -> Node:
        node = Node(
            effect=_effect(frame, outcome),
            n_treated=int((frame["treated"] == 1).sum()),
            n_control=int((frame["treated"] == 0).sum()),
        )
        if depth >= self.max_depth:
            return node

        best = self._best_split(frame, outcome)
        if best is None:
            return node

        feature, threshold = best
        left = frame[frame[feature] <= threshold]
        right = frame[frame[feature] > threshold]
        node.feature, node.threshold = feature, threshold
        node.left = self._grow(left, outcome, depth + 1)
        node.right = self._grow(right, outcome, depth + 1)
        return node

    def _best_split(self, frame: pd.DataFrame, outcome: str) -> tuple[str, float] | None:
        """The split that most increases the spread of treatment effects.

        Scored as `n_L * tau_L^2 + n_R * tau_R^2`, which is the weighted
        between-leaf variance of the effect once the parent's effect is held
        fixed. Maximising it is the same as making the two children's effects as
        different from each other as the data allows.
        """
        best_score = _effect_score(frame, outcome)
        best: tuple[str, float] | None = None

        for feature in self.features:
            values = frame[feature].to_numpy()
            unique = np.unique(values)
            if len(unique) < 2:
                continue
            # quantile candidates rather than every value: the gain surface is
            # flat between close thresholds and this keeps the fit seconds, not
            # minutes
            qs = np.linspace(0.05, 0.95, min(CANDIDATE_SPLITS, len(unique) - 1))
            for threshold in np.unique(np.quantile(values, qs)):
                left = frame[values <= threshold]
                right = frame[values > threshold]
                if not _usable(left, self.min_leaf_per_arm) or not _usable(
                    right, self.min_leaf_per_arm
                ):
                    continue
                score = _effect_score(left, outcome) + _effect_score(right, outcome)
                if score > best_score:
                    best_score, best = score, (feature, float(threshold))
        return best

    def _estimate(self, node: Node, frame: pd.DataFrame, outcome: str, fallback: float) -> None:
        """Re-estimate every leaf on data that had no say in the structure."""
        if node.is_leaf:
            node.n_treated = int((frame["treated"] == 1).sum())
            node.n_control = int((frame["treated"] == 0).sum())
            # A leaf the estimation half cannot populate on both arms gets the
            # parent's effect rather than an invented one.
            node.effect = (
                _effect(frame, outcome) if node.n_treated >= 1 and node.n_control >= 1 else fallback
            )
            return
        here = _effect(frame, outcome) if _usable(frame, 1) else fallback
        mask = frame[node.feature] <= node.threshold
        self._estimate(node.left, frame[mask], outcome, here)
        self._estimate(node.right, frame[~mask], outcome, here)

    # -------------------------------------------------------------- predict
    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if self.root is None:
            raise ValueError("fit the tree first")
        return np.array([self._route(self.root, row) for _, row in frame.iterrows()])

    def _route(self, node: Node, row: pd.Series) -> float:
        while not node.is_leaf:
            node = node.left if row[node.feature] <= node.threshold else node.right
        return node.effect

    def leaves(self) -> list[Node]:
        out: list[Node] = []

        def walk(node: Node) -> None:
            if node.is_leaf:
                out.append(node)
            else:
                walk(node.left)
                walk(node.right)

        if self.root is not None:
            walk(self.root)
        return out


def _effect(frame: pd.DataFrame, outcome: str) -> float:
    treated = frame.loc[frame["treated"] == 1, outcome]
    control = frame.loc[frame["treated"] == 0, outcome]
    if treated.empty or control.empty:
        return 0.0
    return float(treated.mean() - control.mean())


def _effect_score(frame: pd.DataFrame, outcome: str) -> float:
    return len(frame) * _effect(frame, outcome) ** 2


def _usable(frame: pd.DataFrame, minimum: int) -> bool:
    return bool(
        (frame["treated"] == 1).sum() >= minimum and (frame["treated"] == 0).sum() >= minimum
    )
