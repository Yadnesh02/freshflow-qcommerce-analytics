"""Compile a metric request into DuckDB SQL (task S3.6).

This is the file gate G3 rests on. G3 says no number on screen may be absent
from `metrics.yml`, and the only way to make that structurally true rather than
a promise is to remove every other route to a number: the app calls the API,
the API calls this, and this reads the registry. Nothing downstream writes
aggregate SQL of its own, so nothing downstream can invent a metric.

**Ratios are divided after aggregating, never averaged.** `SUM(a) / SUM(b)`
sliced by store is not the mean of per-store ratios - the two differ whenever
the denominators differ, which for anything divided by orders or units is
always. The registry stores the two halves separately precisely so this layer
cannot get it wrong, and `test_ratio_metrics_never_average_a_ratio` in the
registry suite stops a metric ever being declared as a pre-divided expression.

**A dimension is refused when the source cannot reach it.** `ddi_band` hangs off
`mart_customer_360` by `customer_id`; a metric sourced from a table with no
`customer_id` cannot be sliced by it. The wrong behaviour is to emit a cross
join and return a number - that number would look plausible and be meaningless.
`UnreachableDimension` is raised instead, and S3.7 turns it into a 400.

**Joins are narrowed to two columns and both are renamed.** A dimension table
is joined as `(select key as _k, attribute as _v from ...)`. Two reasons. The
metric's own SQL comes out of the registry as an unqualified fragment - literally
`SUM(units_sold)` - and cannot be qualified without parsing it, so any column
the join brings into scope with a name the fragment uses would make the
reference ambiguous and the query would fail, or worse, resolve to the wrong
table. Narrowing and renaming removes that whole class. Second, it makes the
generated SQL short enough to read, which matters because the API echoes it.

**Fan-out is a correctness question, not a performance one.** Every join here
is to a dimension keyed one row per key; if that ever stopped being true, a
`SUM` would silently double. `test_every_dimension_join_target_is_unique_on_its_key`
asserts it against the warehouse rather than trusting the modelling.

    from semantic.resolver import Resolver, MetricRequest
    resolver = Resolver(load_registry(), columns_from(con))
    compiled = resolver.compile(MetricRequest("wastage_rate", ("store", "week")))
    con.execute(compiled.sql).df()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from semantic.registry import Dimension, Metric, Registry

# Every mart lives here. The registry names bare tables because a schema is a
# deployment detail, not part of a metric's definition.
SCHEMA = "marts"

VALID_OPS = {"=", "!=", "<", "<=", ">", ">=", "in", "not in", "between"}


class ResolverError(ValueError):
    """A request cannot be compiled. The message says which part and why."""


class UnknownMetric(ResolverError):
    pass


class UnknownDimension(ResolverError):
    pass


class UnreachableDimension(ResolverError):
    """The metric's source has no column to join this dimension on."""


@dataclass(frozen=True)
class Filter:
    """One predicate, expressed against a dimension rather than a column.

    Filtering by dimension rather than by raw column is what stops a caller
    reaching a column the registry never published - the same closure that
    makes G3 hold for the select list has to hold for the where clause.
    """

    dimension: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.op not in VALID_OPS:
            raise ResolverError(f"unsupported operator '{self.op}'. Known: {sorted(VALID_OPS)}")
        if self.op in {"in", "not in"} and not isinstance(self.value, (list, tuple, set)):
            raise ResolverError(f"'{self.op}' needs a collection, got {type(self.value).__name__}")
        if self.op == "between" and (
            not isinstance(self.value, (list, tuple)) or len(self.value) != 2
        ):
            raise ResolverError("'between' needs exactly two values")


@dataclass(frozen=True)
class MetricRequest:
    metric: str
    dimensions: tuple[str, ...] = ()
    filters: tuple[Filter, ...] = ()
    limit: int | None = None


@dataclass(frozen=True)
class CompiledMetric:
    sql: str
    params: tuple[Any, ...]
    metric: Metric
    dimensions: tuple[Dimension, ...]
    # The metric the registry says must be read alongside this one. The north
    # star can be moved by starving stores of stock, which shows up as churn and
    # not as failure, so the pairing is carried through to the caller rather
    # than left for a dashboard author to remember.
    guardrail: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def columns_from(con) -> dict[str, set[str]]:
    """Column names per table, for reachability checks.

    Taken from the warehouse rather than declared in YAML: a hand-maintained
    copy of the schema is a second source of truth that drifts, and the failure
    it produces - a dimension silently refused, or worse silently allowed - is
    the kind nobody notices until a chart is wrong.
    """
    rows = con.execute(
        """
        select table_name, column_name
        from information_schema.columns
        """
    ).fetchall()
    columns: dict[str, set[str]] = {}
    for table, column in rows:
        columns.setdefault(table, set()).add(column)
    return columns


class Resolver:
    def __init__(self, registry: Registry, columns: dict[str, set[str]] | None = None) -> None:
        self.registry = registry
        self.columns = columns or {}

    # ------------------------------------------------------------------ public
    def compile(self, request: MetricRequest) -> CompiledMetric:
        metric = self._metric(request.metric)
        dimensions = tuple(self._dimension(name) for name in request.dimensions)

        for dimension in dimensions:
            self._check_reachable(metric, dimension)
        for predicate in request.filters:
            self._check_reachable(metric, self._dimension(predicate.dimension))

        select_parts = [
            f"    {self._attribute_ref(dim)} as {name}"
            for name, dim in zip(request.dimensions, dimensions, strict=True)
        ]
        select_parts.append(f"    {self._measure(metric)} as {metric.name}")

        # A filter needs its join emitted as much as a select does. Joining only
        # the selected dimensions leaves the where clause referencing an alias
        # that was never introduced, and that fails at execution rather than at
        # compile - inside the API, not inside a test. De-duplicated by name so
        # a dimension used in both places is joined once.
        joined: dict[str, Dimension] = {}
        for dim in (*dimensions, *(self._dimension(f.dimension) for f in request.filters)):
            if dim.needs_join:
                joined.setdefault(dim.name, dim)

        lines = ["select", ",\n".join(select_parts), f"from {SCHEMA}.{metric.source} as base"]
        lines.extend(self._join(dim) for dim in joined.values())

        where, params = self._where(request.filters)
        if where:
            lines.append(where)

        if dimensions:
            grouped = ",\n".join(f"    {self._attribute_ref(dim)}" for dim in dimensions)
            lines.append("group by\n" + grouped)
            # order by the metric so the first page of any sliced request is the
            # part worth looking at, in the direction the registry declares
            descending = metric.direction != "down_is_good"
            lines.append(f"order by {metric.name} {'desc' if descending else 'asc'} nulls last")

        if request.limit is not None:
            if request.limit <= 0:
                raise ResolverError(f"limit must be positive, got {request.limit}")
            lines.append(f"limit {request.limit}")

        return CompiledMetric(
            sql="\n".join(lines),
            params=params,
            metric=metric,
            dimensions=dimensions,
            guardrail=metric.guarded_by,
            warnings=self._warnings(metric, dimensions),
        )

    # ------------------------------------------------------------------ pieces
    def _metric(self, name: str) -> Metric:
        try:
            return self.registry.metric(name)
        except KeyError as exc:
            raise UnknownMetric(str(exc)) from None

    def _dimension(self, name: str) -> Dimension:
        try:
            return self.registry.dimension(name)
        except KeyError as exc:
            raise UnknownDimension(str(exc)) from None

    def _check_reachable(self, metric: Metric, dimension: Dimension) -> None:
        """The source must carry the join key, or the slice is meaningless.

        Skipped when no schema was supplied, so the resolver stays usable for
        rendering SQL without a warehouse - but the API always supplies one,
        because returning a wrong number is worse than returning an error.
        """
        if not self.columns:
            return
        source_columns = self.columns.get(metric.source)
        if source_columns is None:
            raise ResolverError(
                f"metric '{metric.name}' is sourced from '{metric.source}', which does not "
                f"exist in the warehouse"
            )
        if dimension.key not in source_columns:
            raise UnreachableDimension(
                f"'{metric.name}' cannot be sliced by '{dimension.name}': its source "
                f"'{metric.source}' has no '{dimension.key}' to join on"
            )

    @staticmethod
    def _measure(metric: Metric) -> str:
        if metric.is_ratio:
            # aggregate, then divide. The denominator arrives already guarded by
            # NULLIF in the registry, which test_ratio_denominator_guards_
            # division_by_zero enforces.
            return f"{metric.numerator} / {metric.denominator}"
        return metric.expression

    @staticmethod
    def _alias(dimension: Dimension) -> str:
        return f"d_{dimension.name}"

    def _attribute_ref(self, dimension: Dimension) -> str:
        if not dimension.needs_join:
            return f"base.{dimension.attribute}"
        return f"{self._alias(dimension)}._v"

    def _join(self, dimension: Dimension) -> str:
        """A two-column join, both columns renamed.

        `_k` and `_v` rather than the real names because the metric fragment
        from the registry is unqualified: if a joined table brought a column
        into scope that the fragment mentions, the reference would become
        ambiguous or bind to the wrong table, and neither failure announces
        itself in the number.
        """
        alias = self._alias(dimension)
        table = dimension.join["table"]
        join_on = dimension.join["join_on"]
        return (
            f"left join (\n"
            f"    select distinct {join_on} as _k, {dimension.attribute} as _v\n"
            f"    from {SCHEMA}.{table}\n"
            f") as {alias} on base.{dimension.key} = {alias}._k"
        )

    def _where(self, filters: tuple[Filter, ...]) -> tuple[str, tuple[Any, ...]]:
        """Predicates as bound parameters, never interpolated.

        The values reach here from an HTTP query string in S3.7. Building the
        literal into the string would put a caller inside the SQL.
        """
        if not filters:
            return "", ()

        clauses: list[str] = []
        params: list[Any] = []
        for predicate in filters:
            dimension = self._dimension(predicate.dimension)
            column = self._attribute_ref(dimension)
            if predicate.op in {"in", "not in"}:
                values = tuple(predicate.value)
                if not values:
                    raise ResolverError(f"'{predicate.dimension} {predicate.op} ()' is empty")
                placeholders = ", ".join("?" for _ in values)
                clauses.append(f"{column} {predicate.op} ({placeholders})")
                params.extend(values)
            elif predicate.op == "between":
                clauses.append(f"{column} between ? and ?")
                params.extend(predicate.value)
            else:
                clauses.append(f"{column} {predicate.op} ?")
                params.append(predicate.value)
        return "where " + "\n  and ".join(clauses), tuple(params)

    @staticmethod
    def _warnings(metric: Metric, dimensions: tuple[Dimension, ...]) -> tuple[str, ...]:
        """Things the caller should know that are not errors.

        Slicing finer than a metric's declared grain does not fail - the SQL is
        valid and the numbers add up - but it answers a subtly different
        question, and a reader looking at a chart cannot tell. Saying so is
        cheaper than a footnote nobody writes.
        """
        notes: list[str] = []
        requested = {dim.name for dim in dimensions}
        beyond = requested - set(metric.grain)
        if beyond:
            notes.append(
                f"sliced by {', '.join(sorted(beyond))}, which is outside "
                f"{metric.name}'s declared grain ({', '.join(metric.grain)})"
            )
        if metric.requires_experiment:
            notes.append(
                f"{metric.name} is only interpretable against a holdout; without one it is "
                f"a description, not an effect"
            )
        if metric.notes:
            notes.append(metric.notes.strip())
        return tuple(notes)
