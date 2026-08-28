"""A small TTL cache in front of the warehouse (task S3.7).

Streamlit Cloud gives the app one container with 1 GB of RAM, and every tile on
every page is an HTTP call. Without a cache, switching between two pages
re-runs the same aggregate over the same immovable data, and the warehouse this
serves is a file that changes only when someone rebuilds it.

**Keyed on the SQL and its parameters, not on the request.** Two different
requests that compile to the same query are the same question, and the resolver
is deterministic, so the compiled form is the honest cache key. Keying on the
URL instead would miss that `?dimensions=store,week` and `?dimensions=week,store`
differ only in presentation.

**Entries expire rather than being invalidated.** The API has no way to know a
rebuild happened - it holds a read-only handle to a file that may be replaced
underneath it - so a TTL is the only correctness story available. It is short
enough that a rebuild becomes visible within a minute and long enough that a
page of twelve tiles costs one round of queries rather than twelve.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

DEFAULT_TTL_SECONDS = 60.0
# Enough for every tile on every page at a few slices each. The values are small
# result frames, not the warehouse, so the ceiling exists to bound a pathological
# caller rather than to manage memory pressure.
DEFAULT_MAX_ENTRIES = 256


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class TTLCache:
    """Least-recently-used, with a time bound. Not thread-safe on its own.

    The API serialises access behind the same lock it uses for the DuckDB
    cursor, so a second lock here would be two locks protecting one critical
    section - and the second one would eventually be forgotten.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock=time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl must be positive, got {ttl_seconds}")
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        # monotonic, not wall clock: a clock adjustment must not make an entry
        # immortal or expire the whole cache at once
        self._clock = clock
        self._entries: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self.stats = CacheStats()

    def get(self, key: tuple) -> tuple[bool, Any]:
        """Returns (hit, value). A miss and a cached None are different answers."""
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return False, None

        stored_at, value = entry
        if self._clock() - stored_at > self.ttl:
            del self._entries[key]
            self.stats.expirations += 1
            self.stats.misses += 1
            return False, None

        self._entries.move_to_end(key)
        self.stats.hits += 1
        return True, value

    def put(self, key: tuple, value: Any) -> None:
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = (self._clock(), value)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
