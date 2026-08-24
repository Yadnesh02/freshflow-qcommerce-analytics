"""Contract for the catalogue expansion and demand engine (task S1.2).

Two things are being defended here.

First, determinism. The policy A/B backtest in Sprint 5 compares two decision
policies over *the same* demand realisations - common random numbers. If the
catalogue or the base demand matrix shifted between runs, the comparison would
be measuring noise and the headline result would be meaningless.

Second, realism. Several properties have to hold or the analysis built on top
is dishonest: demand must be over-dispersed, popularity must be concentrated,
festivals must shift category mix rather than just scale everything up, and a
new store must not open at steady state.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from simulator.catalog import GROUND_TRUTH_COLUMNS, build_catalog, to_bronze
from simulator.config_loader import load_sim_config
from simulator.demand import DemandModel, freshness_multiplier, price_multiplier

cfg = load_sim_config()
catalog = build_catalog(cfg, seed=42)
model = DemandModel(cfg, catalog, seed=42)

ORDINARY_DAY = dt.date(2026, 2, 17)  # a Tuesday, mid-month, no festival, no monsoon


# =============================================================== catalogue
def test_catalog_has_the_configured_shape() -> None:
    assert len(catalog) == cfg.total_sku_count
    assert catalog["sku_id"].is_unique
    assert catalog["sku_id"].notna().all()


def test_catalog_is_deterministic() -> None:
    """Same seed, same shelves - the A/B backtest depends on this."""
    again = build_catalog(cfg, seed=42)
    assert catalog.equals(again)


def test_a_different_seed_gives_a_different_catalog() -> None:
    other = build_catalog(cfg, seed=43)
    assert not catalog["base_price"].equals(other["base_price"])


def test_popularity_is_a_probability_distribution() -> None:
    assert catalog["popularity_weight"].sum() == pytest.approx(1.0, abs=1e-9)
    assert (catalog["popularity_weight"] > 0).all()


def test_popularity_is_concentrated_like_grocery_retail() -> None:
    cum = catalog["popularity_weight"].sort_values(ascending=False).cumsum()
    top_decile = cum.iloc[len(catalog) // 10 - 1]
    assert 0.35 < top_decile < 0.70, f"top 10% of SKUs carry {top_decile:.1%} of demand"


def test_category_demand_shares_match_the_config() -> None:
    """Catalogue expansion must not distort the configured category mix."""
    actual = catalog.groupby("l1_category")["popularity_weight"].sum()
    for cat in cfg.categories:
        assert actual[cat.l1] == pytest.approx(cat.demand_weight, abs=1e-6)


def test_prices_are_economically_coherent() -> None:
    assert (catalog["landed_cost"] < catalog["base_price"]).all(), "selling below cost"
    assert (catalog["mrp"] >= catalog["base_price"]).all(), "base price above MRP"
    assert (catalog["base_price"] > 0).all()


def test_cheaper_skus_tend_to_be_more_popular() -> None:
    """Deliberate confounding: price correlates with demand for reasons other
    than elasticity, which is exactly the identification problem the markdown
    work has to reckon with rather than assume away."""
    corr = catalog["base_price"].corr(catalog["popularity_weight"], method="spearman")
    assert corr < -0.15, f"price/popularity correlation is {corr:.2f}, expected clearly negative"


def test_private_label_is_cheaper_and_higher_margin() -> None:
    pl = catalog[catalog["is_private_label"]]
    brand = catalog[~catalog["is_private_label"]]
    assert 100 <= len(pl) <= 160

    pl_margin = 1 - (pl["landed_cost"] / pl["base_price"])
    brand_margin = 1 - (brand["landed_cost"] / brand["base_price"])
    assert pl_margin.mean() > brand_margin.mean() + 0.05, (
        "private label must carry a real margin advantage - it is the whole "
        "commercial reason problem P5 exists"
    )


def test_private_label_is_spread_across_the_popularity_curve() -> None:
    """A PL range that only contains unpopular SKUs could never take share."""
    ranks = catalog["popularity_weight"].rank(pct=True)
    pl_ranks = ranks[catalog["is_private_label"]]
    assert pl_ranks.max() > 0.80, "no private label SKU is in the popular half"


def test_shelf_life_spans_genuinely_different_businesses() -> None:
    assert catalog["shelf_life_days"].min() <= 3, "nothing is truly perishable"
    assert catalog["shelf_life_days"].max() >= 365
    perishable = (catalog["shelf_life_days"] <= 14).sum()
    assert 280 <= perishable <= 420


def test_bronze_export_strips_generator_ground_truth() -> None:
    """Emitting elasticity or popularity as source data would hand the analytics
    layer the answer key."""
    bronze = to_bronze(catalog)
    for col in GROUND_TRUTH_COLUMNS:
        assert col not in bronze.columns
    assert "base_price" in bronze.columns and len(bronze) == len(catalog)


# =============================================================== demand: shape
def test_daily_lambda_has_the_store_by_sku_shape() -> None:
    lam = model.daily_lambda(ORDINARY_DAY)
    assert lam.shape == (len(cfg.stores), len(catalog))
    assert (lam >= 0).all()


def test_model_is_deterministic() -> None:
    again = DemandModel(cfg, build_catalog(cfg, seed=42), seed=42)
    np.testing.assert_allclose(again.daily_lambda(ORDINARY_DAY), model.daily_lambda(ORDINARY_DAY))


def test_annual_volume_lands_near_the_configured_anchor() -> None:
    """Calendar factors average well above 1.0; without calibration the model
    would overshoot its own scale anchor by roughly a fifth."""
    baseline = cfg.raw["catalog"]["demand"]["network_daily_units_baseline"]
    annual = model.expected_annual_units()
    assert 0.90 < annual / (baseline * 365) < 1.10, f"annual units {annual:,.0f}"


def test_target_dataset_size_stays_laptop_sized() -> None:
    """~5.5M order lines: big enough to be real work, small enough for DuckDB
    on a laptop and for the sub-80MB Streamlit demo slice."""
    assert 4.5e6 < model.expected_annual_units() < 7.0e6


# =============================================================== demand: calendar
def test_the_busiest_days_of_the_year_are_festival_days() -> None:
    """Not "Diwali is the single peak day" - that turns out to be false, and
    correctly so. The observed peak is a salary-week Saturday in the middle of
    Ganesh Chaturthi, because festival, payday and weekend compound. What must
    hold is that the top of the distribution is festival-driven."""
    totals = {d: model.daily_lambda(d).sum() for d in model.factors}
    top_ten = sorted(totals, key=totals.get, reverse=True)[:10]
    festival_days = sum(1 for d in top_ten if model.factors[d].is_festival)
    assert festival_days >= 8, f"only {festival_days} of the 10 busiest days are festival days"


def test_diwali_is_the_strongest_festival_and_lifts_its_whole_window() -> None:
    festivals = cfg.calendar["festivals"]
    strongest = max(festivals, key=lambda f: f["overall_factor"])
    assert strongest["name"] == "Diwali"

    diwali = next(f for f in festivals if f["name"] == "Diwali")
    window = [
        diwali["date"] + dt.timedelta(days=o)
        for o in range(-diwali["lead_days"], diwali["duration_days"])
    ]
    window_mean = np.mean([model.daily_lambda(d).sum() for d in window])
    annual_mean = model.expected_annual_units() / 365
    assert window_mean > annual_mean * 1.25, (
        f"Diwali window averages {window_mean / annual_mean:.2f}x the annual mean"
    )


def test_weekends_outsell_midweek() -> None:
    saturday = model.daily_lambda(dt.date(2026, 2, 14)).sum()
    tuesday = model.daily_lambda(dt.date(2026, 2, 17)).sum()
    assert saturday > tuesday * 1.15


def test_month_end_demand_collapses() -> None:
    """Salary-cycle effect. Large, real in India, and the reason a naive
    day-of-week forecast underperforms."""
    salary_week = model.daily_lambda(dt.date(2026, 2, 3)).sum()
    month_end = model.daily_lambda(dt.date(2026, 2, 26)).sum()
    assert salary_week > month_end * 1.20


def test_navratri_suppresses_meat_while_lifting_fasting_foods() -> None:
    """A festival model that only multiplies upward is not modelling behaviour."""
    navratri = dt.date(2025, 9, 25)
    meat = model.category_names.index("Meat, Fish & Seafood")
    staples = model.category_names.index("Staples & Packaged Food")

    fest = model.factors[navratri].by_category
    plain = model.factors[ORDINARY_DAY].by_category
    assert fest[meat] < plain[meat] * 0.6
    assert fest[staples] > plain[staples] * 1.5


def test_monsoon_lifts_orders_and_suppresses_produce() -> None:
    monsoon_day = dt.date(2026, 7, 14)
    dry_day = dt.date(2026, 2, 17)
    produce = model.category_names.index("Fruits & Vegetables")

    assert model.factors[monsoon_day].is_monsoon
    assert not model.factors[dry_day].is_monsoon
    assert model.factors[monsoon_day].by_category[produce] < 1.0


def test_ipl_nights_exist_and_lift_snacks() -> None:
    ipl_days = [d for d, f in model.factors.items() if f.is_ipl_matchday]
    assert 40 < len(ipl_days) < 60, f"{len(ipl_days)} IPL match nights in the window"

    snacks = model.category_names.index("Snacks & Namkeen")
    assert model.factors[ipl_days[0]].by_category[snacks] > 1.2


# =============================================================== demand: stores
def test_a_store_sells_nothing_before_it_opens() -> None:
    goregaon = next(s for s in cfg.stores if s["store_id"] == "FF-GOR-01")
    idx = model.store_ids.index("FF-GOR-01")
    day_before = goregaon["opened_date"] - dt.timedelta(days=1)

    assert model.daily_lambda(day_before)[idx].sum() == 0
    assert model.daily_lambda(goregaon["opened_date"])[idx].sum() > 0


def test_a_new_store_ramps_rather_than_opening_at_steady_state() -> None:
    goregaon = next(s for s in cfg.stores if s["store_id"] == "FF-GOR-01")
    idx = model.store_ids.index("FF-GOR-01")
    opening = model.store_ramp(goregaon["opened_date"])[idx]
    mature = model.store_ramp(goregaon["opened_date"] + dt.timedelta(days=120))[idx]

    assert opening < 0.6
    assert mature == pytest.approx(1.0)


def test_store_category_affinity_differentiates_the_network() -> None:
    """If every store were a scaled copy of the others, the transfer engine
    would have nothing to solve."""
    lam = model.daily_lambda(ORDINARY_DAY)
    rte = model.category_names.index("Ready to Eat & Frozen")
    mask = model.sku_category_idx == rte

    powai = model.store_ids.index("FF-POW-01")
    borivali = model.store_ids.index("FF-BOR-01")

    powai_share = lam[powai, mask].sum() / lam[powai].sum()
    borivali_share = lam[borivali, mask].sum() / lam[borivali].sum()
    assert powai_share > borivali_share * 1.5


# =============================================================== demand: sampling
def test_realised_demand_is_over_dispersed() -> None:
    """Poisson would give variance/mean of 1.0. Real retail is lumpier, and a
    Poisson generator would flatter the Sprint 3 forecast."""
    rng = np.random.default_rng(7)
    lam = model.daily_lambda(ORDINARY_DAY)
    drawn = model.sample(lam, rng)
    ratio = drawn.var() / drawn.mean()
    assert ratio > 1.5, f"variance/mean is {ratio:.2f} - demand is not over-dispersed"


def test_sampling_is_unbiased() -> None:
    """Over many draws the mean must track lambda, or the whole scale is wrong."""
    rng = np.random.default_rng(11)
    lam = model.daily_lambda(ORDINARY_DAY)
    totals = [model.sample(lam, rng).sum() for _ in range(12)]
    assert np.mean(totals) == pytest.approx(lam.sum(), rel=0.03)


def test_sampled_demand_is_non_negative_integers() -> None:
    rng = np.random.default_rng(3)
    drawn = model.sample(model.daily_lambda(ORDINARY_DAY), rng)
    assert drawn.dtype.kind == "i"
    assert (drawn >= 0).all()


def test_closed_store_never_generates_demand_even_when_sampled() -> None:
    rng = np.random.default_rng(5)
    idx = model.store_ids.index("FF-GOR-01")
    before = dt.date(2025, 12, 1)
    assert model.sample(model.daily_lambda(before), rng)[idx].sum() == 0


# =============================================================== multipliers
def test_hour_weights_are_valid_distributions() -> None:
    hw = model.hour_weights()
    assert hw.shape == (len(catalog), 24)
    np.testing.assert_allclose(hw.sum(axis=1), 1.0, atol=1e-3)


def test_dairy_peaks_in_the_morning_and_snacks_at_night() -> None:
    hw = model.hour_weights()
    milk = catalog.index[catalog["l2_subcategory"] == "Milk"][0]
    chips = catalog.index[catalog["l2_subcategory"] == "Chips & Crisps"][0]
    assert hw[milk].argmax() < 12
    assert hw[chips].argmax() >= 18


def test_discount_raises_demand_more_for_elastic_categories() -> None:
    staples = cfg.category("Staples & Packaged Food").elasticity
    snacks = cfg.category("Snacks & Namkeen").elasticity
    ratio = 0.75  # a 25% markdown

    assert price_multiplier(ratio, snacks) > price_multiplier(ratio, staples) > 1.0


def test_price_multiplier_is_neutral_at_the_base_price() -> None:
    assert price_multiplier(1.0, -1.4) == pytest.approx(1.0)


def test_freshness_acceptance_falls_as_expiry_approaches() -> None:
    """Why a flat markdown ladder fails: the customer is weighing more than price."""
    fresh = freshness_multiplier(1.0, cfg)
    half = freshness_multiplier(0.4, cfg)
    nearly_expired = freshness_multiplier(0.05, cfg)

    assert fresh > half > nearly_expired
    assert fresh == pytest.approx(1.0)
    assert nearly_expired < 0.45


def test_freshness_multiplier_is_vectorised_and_clipped() -> None:
    out = freshness_multiplier(np.array([-0.5, 0.0, 0.5, 1.0, 2.0]), cfg)
    assert out.shape == (5,)
    assert out[0] == out[1]  # clipped at zero
    assert out[3] == out[4]  # clipped at one
