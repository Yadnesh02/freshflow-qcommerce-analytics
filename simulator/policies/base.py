"""The interface both policy arms implement (task S1.6).

The Sprint 5 experiment compares two decision policies over identical demand.
That comparison is only meaningful if both arms see exactly the same
information and are asked exactly the same questions - so the observable state
is defined once, here, and neither arm can reach past it.

`PolicyContext` is deliberately narrow. It contains what a store operator could
actually know on the morning of a given day: what is on the shelf, what is on
its way, what sold recently, and how close the nearest expiry is. It does not
contain the demand model, the customer segments, or anything else the simulator
knows - a policy that could read those would be cheating, and the experiment
would measure nothing.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PolicyContext:
    """Everything a policy is allowed to see when it decides, for one day."""

    date: dt.date
    on_hand: np.ndarray  # (n_stores, n_skus) units on the shelf
    on_order: np.ndarray  # (n_stores, n_skus) units already inbound
    trailing_avg: np.ndarray  # (n_stores, n_skus) mean daily units sold recently
    min_dte: np.ndarray  # (n_stores, n_skus) days to the soonest expiry
    store_open: np.ndarray  # (n_stores,) trading today
    catalog: pd.DataFrame
    rng: np.random.Generator

    @property
    def n_stores(self) -> int:
        return self.on_hand.shape[0]

    @property
    def n_skus(self) -> int:
        return self.on_hand.shape[1]


@dataclass(frozen=True)
class ReplenishmentOrder:
    """Order lines for one day, as parallel arrays."""

    store_idx: np.ndarray
    sku_idx: np.ndarray
    qty: np.ndarray

    def __len__(self) -> int:
        return len(self.qty)

    @classmethod
    def empty(cls) -> ReplenishmentOrder:
        z = np.array([], dtype=np.int64)
        return cls(z, z, z)


class Policy(ABC):
    """One decision-making regime. Two implementations, compared head to head."""

    name: str = "policy"

    @abstractmethod
    def replenish(self, ctx: PolicyContext) -> ReplenishmentOrder:
        """How much to order, per store and SKU."""

    @abstractmethod
    def markdown(self, ctx: PolicyContext) -> np.ndarray:
        """Discount fraction per store and SKU, between 0 and 1."""

    @abstractmethod
    def deal_slots(self, ctx: PolicyContext) -> dict[int, list[int]]:
        """SKUs on the deal rail today, per store index."""

    def transfers(self, ctx: PolicyContext) -> list[tuple[int, int, int, int]]:
        """Inter-store moves as (from_store, to_store, sku, qty). None by default."""
        return []
