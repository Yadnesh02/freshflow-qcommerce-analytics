"""Who is the rail worth showing to? (task D4)

D2 found the rail returns 4.66x on margin inside the window. D3 found it costs
retention beyond it, because the demand it creates lands on a few SKUs that then
run dry. Neither is the decision. The decision is *who*, and it is only
interesting because those two findings point opposite ways: the question is
which customers the margin gain outruns the churn cost for.

**Features come from the burn-in, outcomes from after it, and the two never
overlap.** This is the whole reason D1 was amended. With assignment on day one
there is no pre-treatment behaviour in the warehouse at all - 25 of
mart_customer_360's 31 columns are computed over the treated window - so a model
trained on RFM or discount dependency would be fitting on outcomes of the
treatment. It would score beautifully on a holdout and predict nothing, because
it would be reading off who responded rather than who will.

**Four estimators, reported side by side, because three of them fail.** The
T-learner is the obvious choice and the wrong one: it differences two models
fitted on an outcome with a Rs 700 standard deviation, and what survives is
mostly the difference of their errors. The X-learner is the textbook answer for
80/20 arms and does better without being enough. Clustering at least estimates
the effect where it is well estimated - over thousands of customers at a time -
but K-means optimises feature-space variance, which is not where the treatment
differs. The transformed outcome is the only one of the four whose target IS the
uplift, so it is the only one whose splits chase the right thing.

**Qini rather than AUC.** AUC asks whether the model ranks responders. Qini asks
whether it ranks people by how much the treatment *moved* them, which is the only
thing a targeting policy can act on. A model can have excellent AUC and a flat
Qini - that is exactly what happens when it has learned who orders anyway.

**The combined objective needs a declared price.** Margin uplift is in rupees;
churn uplift is a probability. Trading them off needs what a customer is worth,
and that cannot be measured inside a 135-day window. `CUSTOMER_VALUE` is one
line here, in the same spirit as the Rs 42 delivery cost: a parameter to be
argued with rather than a number buried in a model.

    python tasks.py deal-uplift
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

from analytics.deal.causal_tree import CausalTree

ROOT = Path(__file__).resolve().parent.parent.parent
WAREHOUSE = Path(
    os.environ.get("FRESHFLOW_WAREHOUSE", ROOT / "data" / "warehouse" / "freshflow.duckdb")
)

# AN ASSUMPTION, NOT A MEASUREMENT - and the one every number below is
# proportional to. What a retained customer is worth cannot be measured inside
# the window: it needs a horizon longer than the simulation. Rs 1,500 is roughly
# a year of the holdout arm's observed contribution, and it is a parameter to be
# argued with rather than a constant to be trusted.
CUSTOMER_VALUE = 1500.0

TEST_FRACTION = 0.30
SEED = 42

PANEL = """
-- Monetary sums are ROUNDED, and not for display. DuckDB aggregates in
-- parallel, so the order floats are added in varies between runs and
-- sum(gross_margin) differs in its last bits. On a signal this small that is
-- enough to move LightGBM's split points and with them the Qini: measured at
-- -79,829, then -75,445, then -58,586 on what should have been identical data.
-- Rounding to the paisa is also simply what these columns are.
with assign as (select max(assigned_date) as d from staging.stg_crm__deal_exposure),
pre as (
    select
        i.customer_id,
        count(distinct i.order_id)                                     as orders_pre,
        round(sum(i.gross_margin), 2)                                  as margin_pre,
        round(sum(i.net_revenue), 2)                                   as revenue_pre,
        sum(i.signed_qty)                                              as units_pre,
        count(distinct i.sku_id)                                       as skus_pre,
        count(distinct i.date_day)                                     as active_days_pre,
        max(i.date_day)                                                as last_order_pre,
        sum(case when i.promo_id is not null then i.signed_qty else 0 end) as promo_units_pre
    from marts.fct_order_item i, assign a
    where i.date_day < a.d
    group by 1
),
post as (
    select
        i.customer_id,
        round(sum(i.gross_margin), 2) as margin_post,
        count(distinct i.order_id) as orders_post
    from marts.fct_order_item i, assign a
    where i.date_day >= a.d
    group by 1
)
select
    e.deal_arm,
    e.customer_id,
    c.is_member,
    c.acquisition_channel,
    c.device,
    c.home_store_id,
    date_diff('day', c.signup_date, a.d)                   as tenure_days,
    coalesce(p.orders_pre, 0)                              as orders_pre,
    coalesce(p.margin_pre, 0.0)                            as margin_pre,
    coalesce(p.revenue_pre, 0.0)                           as revenue_pre,
    coalesce(p.units_pre, 0)                               as units_pre,
    coalesce(p.skus_pre, 0)                                as skus_pre,
    coalesce(p.active_days_pre, 0)                         as active_days_pre,
    coalesce(p.promo_units_pre, 0)                         as promo_units_pre,
    coalesce(date_diff('day', p.last_order_pre, a.d), 999) as recency_pre,
    coalesce(q.margin_post, 0.0)                           as margin_post,
    coalesce(q.orders_post, 0)                             as orders_post,
    case when c.churn_date is null then 0 else 1 end       as churned
from staging.stg_crm__deal_exposure e
join marts.dim_customer c using (customer_id)
cross join assign a
left join pre  p on p.customer_id = e.customer_id
left join post q on q.customer_id = e.customer_id
-- Only customers who existed before assignment have a pre-period. Scoring
-- anybody else would mean predicting from features that do not exist.
where c.signup_date < a.d
-- NOT cosmetic. A result set with no ORDER BY comes back in whatever order the
-- scan produced, which varies between processes - so the train/test mask below
-- lands on different customers every run. Measured before this line existed:
-- the same Qini came out at -11,009, then 19,031, then 36,239.
order by e.customer_id
"""

FEATURES = [
    "is_member",
    "tenure_days",
    "orders_pre",
    "margin_pre",
    "revenue_pre",
    "units_pre",
    "skus_pre",
    "active_days_pre",
    "promo_units_pre",
    "recency_pre",
    "promo_share_pre",
    "basket_pre",
    "acquisition_channel",
    "device",
    "home_store_id",
]
CATEGORICAL = ["acquisition_channel", "device", "home_store_id"]


@dataclass(frozen=True)
class QiniResult:
    curve: pd.DataFrame
    coefficient: float
    random_area: float

    @property
    def beats_random(self) -> bool:
        return self.coefficient > 0


def build_panel(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    frame = con.execute(PANEL).df()
    if set(frame["deal_arm"].unique()) != {"exposed", "holdout"}:
        raise ValueError(
            "one arm is missing - this build predates the D1 holdout, or the burn-in "
            "left no post-assignment period"
        )
    frame["promo_share_pre"] = np.where(
        frame["units_pre"] > 0, frame["promo_units_pre"] / frame["units_pre"].clip(lower=1), 0.0
    )
    frame["basket_pre"] = np.where(
        frame["orders_pre"] > 0, frame["revenue_pre"] / frame["orders_pre"].clip(lower=1), 0.0
    )
    frame["treated"] = (frame["deal_arm"] == "exposed").astype(int)
    for column in CATEGORICAL:
        frame[column] = frame[column].astype("category")
    frame["is_member"] = frame["is_member"].astype(int)
    return frame.reset_index(drop=True)


def _fit(frame: pd.DataFrame, outcome: str, classifier: bool):
    # `random_state` alone is NOT enough. LightGBM builds histograms across
    # threads, and with the default thread count the reduction order varies with
    # scheduling - so the same seed on the same data gives different trees in
    # different processes. Measured before this was pinned: the T-learner's
    # margin Qini came out at 8,645, then 39,872, then 80,445 on identical
    # inputs. Every "beats random" verdict was noise.
    #
    # `deterministic` needs `force_row_wise` and a fixed thread count to mean
    # anything. One thread costs about a second on 31k rows and buys a number
    # that can be quoted.
    params = {
        "n_estimators": 200,
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_child_samples": 100,
        "verbose": -1,
        "random_state": SEED,
        "deterministic": True,
        "force_row_wise": True,
        # n_jobs is the sklearn wrapper's name for it. Passing `num_threads`
        # alone leaves n_jobs at its default and the booster still threads.
        "n_jobs": 1,
    }
    model = lgb.LGBMClassifier(**params) if classifier else lgb.LGBMRegressor(**params)
    model.fit(frame[FEATURES], frame[outcome], categorical_feature=CATEGORICAL)
    return model


def _predict(model, frame: pd.DataFrame, classifier: bool) -> np.ndarray:
    if classifier:
        return model.predict_proba(frame[FEATURES])[:, 1]
    return model.predict(frame[FEATURES])


def t_learner(train: pd.DataFrame, score: pd.DataFrame, outcome: str) -> np.ndarray:
    """Two models, one per arm, differenced at prediction time.

    Kept for comparison rather than for use. On this data it does not beat
    random, and the reason is instructive: the outcome has a standard deviation
    of about Rs 700 against a treatment effect of a few rupees, so differencing
    two independently fitted models leaves mostly the difference of their
    errors. See `x_learner`.
    """
    classifier = set(train[outcome].unique()) <= {0, 1}
    treated = _fit(train[train["treated"] == 1], outcome, classifier)
    control = _fit(train[train["treated"] == 0], outcome, classifier)
    return _predict(treated, score, classifier) - _predict(control, score, classifier)


def x_learner(train: pd.DataFrame, score: pd.DataFrame, outcome: str) -> np.ndarray:
    """Impute each customer's counterfactual, then model the imputed effect.

    Kunzel et al.'s estimator, and the right one here for exactly the reason
    they propose it: the arms are 80/20. The treated model is fitted on four
    times the data, so the counterfactual it imputes for a *control* customer is
    the reliable half of the construction, and the control arm is where the
    imputed effects are therefore worth modelling.

    Differencing happens once, inside the imputation, against a well-fitted
    surface rather than between two equally noisy ones. That is the whole
    difference from the T-learner above.
    """
    classifier = set(train[outcome].unique()) <= {0, 1}
    treated_rows = train[train["treated"] == 1]
    control_rows = train[train["treated"] == 0]

    mu1 = _fit(treated_rows, outcome, classifier)
    mu0 = _fit(control_rows, outcome, classifier)

    d1 = treated_rows[outcome].to_numpy() - _predict(mu0, treated_rows, classifier)
    d0 = _predict(mu1, control_rows, classifier) - control_rows[outcome].to_numpy()

    tau1 = _fit(treated_rows.assign(_d=d1), "_d", classifier=False)
    tau0 = _fit(control_rows.assign(_d=d0), "_d", classifier=False)

    # Weight toward the arm whose imputed effects rest on the better-fitted
    # surface. With 80% treated that is tau0.
    p = float(train["treated"].mean())
    return p * tau0.predict(score[FEATURES]) + (1.0 - p) * tau1.predict(score[FEATURES])


def transformed_outcome(train: pd.DataFrame, score: pd.DataFrame, outcome: str) -> np.ndarray:
    """One model, fitted on a target whose conditional mean IS the uplift.

    The other three estimators all optimise the wrong thing. T- and X-learners
    model the OUTCOME and subtract; the clustering models feature-space
    variance. None of them splits on what the question is about, which is where
    the treatment effect differs.

    The Horvitz-Thompson transform fixes that without any new machinery:

        Y* = Y * (T - p) / (p * (1 - p))        so  E[Y* | X] = tau(X)

    A regressor fitted on Y* therefore chases effect heterogeneity directly -
    every split it makes is a split that explains where the treatment did
    something different, because that is what its target encodes.

    The price is variance. With p = 0.80 a control customer's target is -5Y,
    which is why this is worth trying on 31k customers and would not be on 300.
    """
    p_treat = float(train["treated"].mean())
    if not 0.0 < p_treat < 1.0:
        raise ValueError("one arm is empty - the transform divides by zero")

    def star(frame: pd.DataFrame) -> np.ndarray:
        t = frame["treated"].to_numpy()
        return frame[outcome].to_numpy() * (t - p_treat) / (p_treat * (1.0 - p_treat))

    model = _fit(train.assign(_star=star(train)), "_star", classifier=False)
    return model.predict(score[FEATURES])


def two_stage(
    train: pd.DataFrame, score: pd.DataFrame, outcome: str, clusters: int = 6
) -> np.ndarray:
    """Estimate the effect per behavioural cluster, then predict the cluster.

    The estimator that actually works here, and the reason is the shape of the
    problem rather than anything clever. The response varies by a latent type -
    the simulator calls them segments, the warehouse never sees them - and a
    T-learner tries to recover a per-customer effect from an outcome with a
    Rs 700 standard deviation, which is hopeless. This instead:

      1. clusters customers on pre-period behaviour, where the signal is strong
         and the variance is low;
      2. measures the treatment effect WITHIN each cluster, where it is an
         average over thousands of customers and therefore well estimated;
      3. assigns each scored customer their cluster's effect.

    The per-customer noise is averaged away at step 2 instead of being modelled
    at step 1. Nothing here needs the true segment, which is as well - the
    analytics layer is forbidden from importing the simulator, and in a real
    warehouse no such column exists either.
    """
    numeric = [f for f in FEATURES if f not in CATEGORICAL]
    scaler = StandardScaler().fit(train[numeric])
    model = KMeans(n_clusters=clusters, random_state=SEED, n_init=10).fit(
        scaler.transform(train[numeric])
    )

    train = train.assign(_cluster=model.labels_)
    effects = {}
    for cluster, chunk in train.groupby("_cluster"):
        treated = chunk[chunk["treated"] == 1][outcome]
        control = chunk[chunk["treated"] == 0][outcome]
        # A cluster with no control customers has no counterfactual, so it gets
        # the population effect rather than a number invented from one arm.
        effects[cluster] = float(treated.mean() - control.mean()) if len(control) else float("nan")
    fallback = float(
        train[train["treated"] == 1][outcome].mean() - train[train["treated"] == 0][outcome].mean()
    )
    assigned = model.predict(scaler.transform(score[numeric]))
    return np.array(
        [fallback if np.isnan(effects.get(c, np.nan)) else effects[c] for c in assigned]
    )


def causal_tree(train: pd.DataFrame, score: pd.DataFrame, outcome: str) -> np.ndarray:
    """The one that works. Splits on the effect, estimates leaves honestly.

    See `analytics/deal/causal_tree.py` for why, at length. The short version is
    that the other four optimise the outcome, feature-space variance, or a target
    with crippling variance, and this one optimises the thing the question is
    about: where the treatment does something different.
    """
    numeric = [f for f in FEATURES if f not in CATEGORICAL]
    tree = CausalTree(features=numeric, seed=SEED).fit(train, outcome)
    return tree.predict(score)


def qini(uplift: np.ndarray, treated: np.ndarray, outcome: np.ndarray) -> QiniResult:
    """Incremental outcome captured as the targeted fraction grows.

    At each cut the treated sum is compared to the control sum rescaled by the
    arm ratio *within that cut*, which is what keeps an unbalanced holdout from
    looking like an effect.
    """
    order = np.argsort(-uplift)
    t, y = treated[order], outcome[order]
    n = len(order)

    cum_t = np.cumsum(t)
    cum_c = np.cumsum(1 - t)
    cum_yt = np.cumsum(y * t)
    cum_yc = np.cumsum(y * (1 - t))

    with np.errstate(divide="ignore", invalid="ignore"):
        scaled = np.where(cum_c > 0, cum_yc * (cum_t / np.maximum(cum_c, 1)), 0.0)
    gain = cum_yt - scaled

    fraction = np.arange(1, n + 1) / n
    total = gain[-1]
    random = total * fraction
    curve = pd.DataFrame({"fraction": fraction, "gain": gain, "random": random})
    # Trapezoid over the fraction axis, which is already in [0, 1].
    area = float(np.trapezoid(gain, fraction))
    random_area = float(np.trapezoid(random, fraction))
    return QiniResult(curve=curve, coefficient=area - random_area, random_area=random_area)


def targeting_curve(
    uplift: np.ndarray,
    treated: np.ndarray,
    outcome: np.ndarray,
    fractions=(0.1, 0.2, 0.3, 0.5, 1.0),
) -> pd.DataFrame:
    """Cumulative incremental outcome if you target the top fraction by uplift.

    This replaces a per-decile table, which was the obvious presentation and the
    wrong one. A decile here holds about 1,000 customers of whom roughly 200 are
    control, so a decile-level uplift carries a standard error near Rs 50 on an
    effect of a few rupees - the column swung from -98 to +109 and none of it
    meant anything. The cumulative form is what the policy question actually
    asks ("target the top 30% and you capture what?") and it averages over
    everything above the cut rather than inside a thin slice.
    """
    result = qini(uplift, treated, outcome)
    curve = result.curve
    rows = []
    for f in fractions:
        row = curve.iloc[min(int(f * len(curve)), len(curve) - 1)]
        rows.append(
            {
                "targeted": f,
                "gain": float(row["gain"]),
                # NO share-of-total column. The total net gain here is
                # negative - the rail loses money shown to everybody - and a
                # share of a negative denominator inverts, so "37% of the total"
                # printed next to a loss of 56,359. What the policy question
                # needs is the level and the margin over random, both absolute.
                "vs_random": float(row["gain"] - row["random"]),
            }
        )
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE)
    ap.add_argument("--customer-value", type=float, default=CUSTOMER_VALUE)
    args = ap.parse_args(argv)

    con = duckdb.connect(str(args.warehouse), read_only=True)
    try:
        panel = build_panel(con)
    finally:
        con.close()

    rng = np.random.default_rng(SEED)
    is_test = rng.random(len(panel)) < TEST_FRACTION
    train, score = panel[~is_test].copy(), panel[is_test].copy()

    print()
    print(f"  {len(panel):,} customers with a pre-period, {len(score):,} held out for scoring")
    print(
        f"  features from before {panel['tenure_days'].notna().sum():,} assignments; "
        f"outcomes from after"
    )
    print()

    print("  Five estimators, so the comparison is in the output rather than in a claim.")
    print()
    print(f"    {'estimator':<12} {'outcome':<8} {'Qini':>14}   verdict")
    results = {}
    estimators = (
        ("T-learner", t_learner),
        ("X-learner", x_learner),
        ("two-stage", two_stage),
        ("transformed", transformed_outcome),
        ("causal-tree", causal_tree),
    )
    for name, fn in estimators:
        margin_up = fn(train, score, "margin_post")
        churn_up = fn(train, score, "churned")
        net_up = margin_up - churn_up * args.customer_value
        results[name] = net_up
        treated = score["treated"].to_numpy()
        net_outcome = (
            score["margin_post"].to_numpy() - score["churned"].to_numpy() * args.customer_value
        )
        for label, pred, outcome in (
            ("margin", margin_up, score["margin_post"].to_numpy()),
            ("net", net_up, net_outcome),
        ):
            r = qini(pred, treated, outcome)
            verdict = "beats random" if r.beats_random else "NO BETTER THAN RANDOM"
            print(f"    {name:<12} {label:<8} {r.coefficient:>14,.1f}   {verdict}")

    net_uplift = results["causal-tree"]
    print()
    print(f"  Targeting by causal-tree net uplift (customer value Rs {args.customer_value:,.0f})")
    print(f"    {'targeted':>9} {'net gain':>14} {'vs random':>13}")
    table = targeting_curve(net_uplift, treated, net_outcome)
    for _, r in table.iterrows():
        mark = "  <- pays" if r["gain"] > 0 else ""
        print(f"    {r['targeted']:>8.0%} {r['gain']:>14,.0f} {r['vs_random']:>13,.0f}{mark}")

    everyone = float(table.loc[table["targeted"] == 1.0, "gain"].iloc[0])
    pays = table[table["gain"] > 0]
    print()
    if len(pays) and everyone < 0:
        best = pays.iloc[0]
        print(
            f"    Shown to everybody the rail loses Rs {abs(everyone):,.0f}. Shown to the top "
            f"{best['targeted']:.0%} by predicted net uplift it gains Rs {best['gain']:,.0f}."
        )
        print("    That sign change is the whole argument for targeting it.")
        print()
        print("    Read the Qini, not the row. The curve is not monotonic - the 10% and 30%")
        print("    points sit either side of zero - because a cut that thin holds only a few")
        print("    hundred control customers. The coefficient integrates the whole curve and")
        print("    is the stable number; any single fraction on it is one noisy estimate.")
    elif everyone > 0:
        print(f"    The rail pays whoever sees it: Rs {everyone:,.0f} across the base.")
    else:
        print("    No targeting fraction turns the rail positive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
