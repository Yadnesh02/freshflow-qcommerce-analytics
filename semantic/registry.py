"""Loader and validator for the metric and dimension registries.

Shared by the schema test (S0.3), the SQL resolver (S3.6), the metrics API
(S3.7) and the docs generator. Nothing else should read the YAML directly -
one parser means one set of rules about what a valid metric is.

    from semantic.registry import load_registry

    reg = load_registry()
    reg.metric("wastage_rate_value").numerator
    reg.metrics_in_family("customer")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEMANTIC_DIR = Path(__file__).parent
METRICS_PATH = SEMANTIC_DIR / "metrics.yml"
DIMENSIONS_PATH = SEMANTIC_DIR / "dimensions.yml"

VALID_TYPES = {"ratio", "measure"}
VALID_FORMATS = {"percent_1dp", "inr", "number_0dp", "number_1dp", "days_1dp"}
VALID_DIRECTIONS = {"up_is_good", "down_is_good", "neutral"}
VALID_TEST_KEYS = {"between", "not_null"}

# every metric must declare these - the contract the dashboard relies on
REQUIRED_KEYS = ("label", "description", "family", "type", "source", "grain", "format", "owner")


class RegistryError(ValueError):
    """A registry file violates the schema contract."""


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    description: str
    family: str
    type: str
    source: str
    grain: list[str]
    format: str
    owner: str
    direction: str
    numerator: str | None = None
    denominator: str | None = None
    expression: str | None = None
    guarded_by: str | None = None
    requires_experiment: bool = False
    notes: str | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_ratio(self) -> bool:
        return self.type == "ratio"


@dataclass(frozen=True)
class Dimension:
    name: str
    label: str
    key: str
    attribute: str
    join: dict[str, str] | None = None
    description: str | None = None
    values: list[Any] | None = None

    @property
    def needs_join(self) -> bool:
        return self.join is not None


@dataclass(frozen=True)
class Registry:
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    north_star: str
    guardrail: str

    def metric(self, name: str) -> Metric:
        try:
            return self.metrics[name]
        except KeyError:
            raise KeyError(
                f"unknown metric '{name}'. Known: {', '.join(sorted(self.metrics))}"
            ) from None

    def dimension(self, name: str) -> Dimension:
        try:
            return self.dimensions[name]
        except KeyError:
            raise KeyError(
                f"unknown dimension '{name}'. Known: {', '.join(sorted(self.dimensions))}"
            ) from None

    def metrics_in_family(self, family: str) -> list[Metric]:
        return [m for m in self.metrics.values() if m.family == family]

    @property
    def families(self) -> list[str]:
        return sorted({m.family for m in self.metrics.values()})


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RegistryError(f"registry file missing: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RegistryError(f"{path.name} must parse to a mapping, got {type(data).__name__}")
    return data


def _validate_metric(name: str, spec: dict[str, Any]) -> None:
    missing = [k for k in REQUIRED_KEYS if spec.get(k) in (None, "", [])]
    if missing:
        raise RegistryError(f"metric '{name}' is missing required keys: {', '.join(missing)}")

    if spec["type"] not in VALID_TYPES:
        raise RegistryError(
            f"metric '{name}' has type '{spec['type']}', expected one of {VALID_TYPES}"
        )
    if spec["format"] not in VALID_FORMATS:
        raise RegistryError(
            f"metric '{name}' has format '{spec['format']}', expected one of {VALID_FORMATS}"
        )
    if spec["direction"] not in VALID_DIRECTIONS:
        raise RegistryError(
            f"metric '{name}' has direction '{spec['direction']}', expected one of {VALID_DIRECTIONS}"
        )

    if spec["type"] == "ratio":
        if not spec.get("numerator") or not spec.get("denominator"):
            raise RegistryError(f"ratio metric '{name}' needs both numerator and denominator")
        if spec.get("expression"):
            raise RegistryError(
                f"ratio metric '{name}' must not set 'expression' - use numerator/denominator so "
                "the ratio is computed once over aggregates, not averaged per row"
            )
    elif not spec.get("expression"):
        raise RegistryError(f"measure metric '{name}' needs an 'expression'")
    elif spec.get("numerator") or spec.get("denominator"):
        raise RegistryError(f"measure metric '{name}' must not set numerator/denominator")

    for t in spec.get("tests") or []:
        if not isinstance(t, dict) or len(t) != 1:
            raise RegistryError(f"metric '{name}' has a malformed test entry: {t!r}")
        key = next(iter(t))
        if key not in VALID_TEST_KEYS:
            raise RegistryError(
                f"metric '{name}' has unknown test '{key}', expected {VALID_TEST_KEYS}"
            )
        if key == "between":
            bounds = t[key]
            if not (isinstance(bounds, list) and len(bounds) == 2 and bounds[0] < bounds[1]):
                raise RegistryError(f"metric '{name}' has invalid 'between' bounds: {bounds!r}")


def _validate_dimension(name: str, spec: dict[str, Any]) -> None:
    for key in ("label", "key", "attribute"):
        if not spec.get(key):
            raise RegistryError(f"dimension '{name}' is missing required key '{key}'")
    join = spec.get("join")
    if join is not None and (not isinstance(join, dict) or not {"table", "join_on"} <= set(join)):
        raise RegistryError(f"dimension '{name}' has a malformed join: {join!r}")


def load_registry(
    metrics_path: Path = METRICS_PATH,
    dimensions_path: Path = DIMENSIONS_PATH,
) -> Registry:
    """Parse, validate and return both registries. Raises RegistryError on any violation."""
    mdoc = _read_yaml(metrics_path)
    ddoc = _read_yaml(dimensions_path)

    defaults: dict[str, Any] = mdoc.get("defaults") or {}
    raw_metrics: dict[str, Any] = mdoc.get("metrics") or {}
    raw_dims: dict[str, Any] = ddoc.get("dimensions") or {}

    if not raw_metrics:
        raise RegistryError("metrics.yml defines no metrics")
    if not raw_dims:
        raise RegistryError("dimensions.yml defines no dimensions")

    dimensions: dict[str, Dimension] = {}
    for name, spec in raw_dims.items():
        _validate_dimension(name, spec)
        dimensions[name] = Dimension(
            name=name,
            label=spec["label"],
            key=spec["key"],
            attribute=spec["attribute"],
            join=spec.get("join"),
            description=spec.get("description"),
            values=spec.get("values"),
        )

    metrics: dict[str, Metric] = {}
    for name, spec in raw_metrics.items():
        merged = {**defaults, **spec}
        _validate_metric(name, merged)

        unknown_grain = [g for g in merged["grain"] if g not in dimensions]
        if unknown_grain:
            raise RegistryError(
                f"metric '{name}' declares grain {unknown_grain} not present in dimensions.yml"
            )

        metrics[name] = Metric(
            name=name,
            label=merged["label"],
            description=" ".join(merged["description"].split()),
            family=merged["family"],
            type=merged["type"],
            source=merged["source"],
            grain=list(merged["grain"]),
            format=merged["format"],
            owner=merged["owner"],
            direction=merged["direction"],
            numerator=merged.get("numerator"),
            denominator=merged.get("denominator"),
            expression=merged.get("expression"),
            guarded_by=merged.get("guarded_by"),
            requires_experiment=bool(merged.get("requires_experiment", False)),
            notes=" ".join(merged["notes"].split()) if merged.get("notes") else None,
            tests=list(merged.get("tests") or []),
        )

    north_star = mdoc.get("north_star")
    guardrail = mdoc.get("guardrail")
    for label, ref in (("north_star", north_star), ("guardrail", guardrail)):
        if not ref:
            raise RegistryError(f"metrics.yml must declare a '{label}'")
        if ref not in metrics:
            raise RegistryError(f"{label} '{ref}' is not a defined metric")

    for m in metrics.values():
        if m.guarded_by and m.guarded_by not in metrics:
            raise RegistryError(f"metric '{m.name}' is guarded_by unknown metric '{m.guarded_by}'")

    return Registry(
        metrics=metrics, dimensions=dimensions, north_star=north_star, guardrail=guardrail
    )


if __name__ == "__main__":
    reg = load_registry()
    print(f"{len(reg.metrics)} metrics across {len(reg.families)} families")
    print(f"{len(reg.dimensions)} dimensions")
    print(f"north star : {reg.north_star}")
    print(f"guardrail  : {reg.guardrail}\n")
    for fam in reg.families:
        names = sorted(m.name for m in reg.metrics_in_family(fam))
        print(f"  {fam:<13} {', '.join(names)}")
