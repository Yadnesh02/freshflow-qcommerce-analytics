"""The committed bundle says which build fitted it, and that build holds the anchors.

**This file exists because its absence cost five sprints.** `policy_bundle.json`
is what Policy B reads - it is the only thing crossing from the analytics half
of the project into the simulator - and the committed copy had been exported
from the development laptop's warehouse, a build that fails five of the seven
anchors. All twenty-three elasticity cells differed from what a clean runner
fits, two of them with the sign reversed, and the identified-cell count was 14
against a clean build's 13.

Nothing caught it. `warehouse.yml` regenerates the bundle on every run with a
comment saying it does so "so the committed copy cannot drift from the
parameters this build actually fitted" - and then nothing compared the two. The
old provenance block recorded how many cells there were and never which build
they came from, so the experiment reproduced perfectly from a clone and would
never have reproduced from a rebuild. That is the exact shape of failure gate G5
exists to catch, and it went undetected because the check was a comment.

The test below needs no warehouse. It compares two committed things - the
bundle's declared source and the anchors this project pins - which is what makes
it run everywhere and fail immediately rather than only on a full-year build.
"""

from __future__ import annotations

import json

import pytest

from analytics.anchors import ANCHORS
from analytics.optimization.policy_bundle import BUNDLE_PATH

# The anchors carry a money tolerance for float summation across a rebuild;
# reuse the same idea rather than demanding bit equality on a sum of millions.
MONEY_TOLERANCE_INR = 1.0


@pytest.fixture(scope="module")
def bundle() -> dict:
    assert BUNDLE_PATH.exists(), f"no policy bundle at {BUNDLE_PATH}"
    return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))


def _anchor(name: str) -> float:
    return next(a.expected for a in ANCHORS if a.name == name)


def test_the_bundle_records_the_build_it_was_fitted_from(bundle: dict) -> None:
    """Counts are not provenance. Which warehouse produced them is."""
    source = bundle["provenance"].get("fitted_from")
    assert source is not None, (
        "policy_bundle.json has no `fitted_from` block, so nothing says which warehouse "
        "produced the parameters Policy B reads - which is how a bundle fitted on the "
        "laptop survived five sprints"
    )
    for key in ("agg_rows", "net_revenue", "workflow_run"):
        assert key in source, f"`fitted_from` is missing {key!r}"


def test_the_bundle_was_fitted_on_a_build_that_holds_the_anchors(bundle: dict) -> None:
    """The guard itself: a bundle from a build the anchors reject is the wrong bundle.

    `analytics/anchors.py` pins what a correct build produces. A bundle whose
    source warehouse disagrees with those figures was fitted somewhere else, and
    every parameter in it describes a different dataset from the one every
    published number comes from.
    """
    source = bundle["provenance"]["fitted_from"]

    assert int(source["agg_rows"]) == int(_anchor("agg_rows")), (
        f"the bundle was fitted on a warehouse with {int(source['agg_rows']):,} rows in "
        f"agg_store_sku_day; the anchors pin {int(_anchor('agg_rows')):,}. Policy B is "
        "reading parameters from a different dataset than every published figure."
    )
    assert abs(float(source["net_revenue"]) - _anchor("net_revenue")) <= MONEY_TOLERANCE_INR, (
        f"the bundle's source warehouse reports net revenue {source['net_revenue']:,.2f} "
        f"against the anchored {_anchor('net_revenue'):,.2f} - so it is not the build the "
        "anchors describe, whatever else agrees"
    )


def test_the_identified_cell_count_matches_the_cells(bundle: dict) -> None:
    """The count that first exposed the drift, asserted against the data beside it.

    The laptop bundle claimed 14 identified cells and a clean build fits 13. The
    count is a summary of the `elasticity` array in the same file, so the two
    can be checked against each other without any warehouse at all - and a
    summary that has drifted from what it summarises is worth catching on its
    own.
    """
    prov = bundle["provenance"]
    cells = bundle["elasticity"]
    identified = sum(1 for cell in cells if cell["is_identified"])

    assert prov["elasticity_cells"] == len(cells)
    assert prov["elasticity_identified"] == identified, (
        f"provenance claims {prov['elasticity_identified']} identified cells and the "
        f"array carries {identified}"
    )


def test_no_identified_cell_reaches_unit_elasticity(bundle: dict) -> None:
    """The finding the markdown engine rests on, pinned where Policy B reads it.

    Every fitted coefficient sits inside the unit interval, which is why the
    optimiser recommends almost no markdown: below |beta| = 1, cutting price
    gives up more on the units already selling than it wins on the ones the cut
    brings in. If a rebuild ever fits a cell past -1 this stops being true, and
    the markdown result needs re-reading rather than re-quoting.
    """
    usable = [c["elasticity_raw"] for c in bundle["elasticity"] if c["is_identified"]]
    assert usable, "no identified elasticity cells at all"
    assert min(usable) > -1.0, (
        f"an identified cell now reaches {min(usable):.4f}, past unit elasticity - the "
        "markdown engine's 'discounting does not pay' finding no longer follows"
    )
