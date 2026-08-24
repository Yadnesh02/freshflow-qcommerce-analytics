# FreshFlow — Execution Plan
### From empty folder to deployed project. Commands, task order, acceptance gates.

This is the *build* document. [`PROJECT_1_PLAN.md`](PROJECT_1_PLAN.md) is the *what and why*; this is the *do it now*.

**Assumptions:** Windows 11, Python 3.12, Git 2.54, ~10–12 focused hours/week, target ship date **late Oct 2026** (Sprint 3 cut by ~mid-Sep).

**One deviation from the plan doc:** it mentions a `Makefile`. Windows has no `make`, so we use **`tasks.py`** (stdlib argparse, zero deps) — same commands, portable to CI. Everywhere the plan says `make x`, run `uv run python tasks.py x`.

---

## 1. Cost ledger — is this really free?

**Yes, ₹0.** Every component is free-tier or open-source. The honest caveats are in the right column.

| Component | Cost | Caveat you should know |
|---|---|---|
| Python, DuckDB, dbt-core, Dagster OSS, LightGBM, PuLP, FastAPI, Streamlit, pandas/numpy/scipy | **₹0** | All OSS (MIT/Apache/BSD). Permanent, no trial clock. |
| GitHub repo | **₹0** | Unlimited public *and* private repos on Free. |
| GitHub Actions (CI) | **₹0** | **Unlimited minutes on public repos.** Private repos get 2,000 min/month — enough, but public is simpler. |
| GitHub Pages (dbt docs) | **₹0** | Requires a **public** repo on the Free plan. |
| Streamlit Community Cloud | **₹0** | Requires a **public** GitHub repo. **1 GB RAM per app** — see the constraint below. |
| Render / Fly.io (FastAPI, optional) | **₹0** | Free instances sleep after ~15 min idle; first request cold-starts in ~30–60s. Fine for a portfolio demo. |
| Vercel / Netlify (React front-end, optional) | **₹0** | Generous hobby tier. Non-commercial use only — that's us. |
| MotherDuck (hosted DuckDB, optional) | **₹0** | Free tier exists but is the most likely to change. Treat as strictly optional; never make it load-bearing. |
| Screen recording | **₹0** | Loom Free caps at 5 min/video, 25 videos. **OBS Studio** is fully free and unlimited — use OBS. |
| Diagrams | **₹0** | Mermaid in markdown, or dbdiagram.io free tier for the ERD. |

**What it actually costs you:** ~5 GB disk, 8 GB RAM to be comfortable, your electricity, and roughly 60–70 hours of your time.

> ### ⚠ The one constraint that will bite you: Streamlit Cloud gives the app **1 GB RAM**
> Your full warehouse is ~1.5 GB. You cannot ship it. Design for this from day one:
> - The deployed app queries **pre-aggregated marts only** — never `fct_order_item`.
> - Build a `tasks.py demo-slice` target that emits a **demo warehouse**: 5 stores × 90 days × marts only, target **< 80 MB** (GitHub's per-file hard limit is 100 MB, and Git LFS free tier is only 1 GB bandwidth/month — so stay under 100 MB and commit it plainly).
> - Full 14-store / 365-day warehouse stays local and gitignored; it's what you run the experiment on.
>
> Getting this wrong means discovering at Sprint 3 that your app won't deploy. Build the demo slice in Sprint 2.

---

## 2. How we'll work

Each session: pick the next task ID, I write the code, you run it, we check the acceptance gate, commit. Small commits, one task each.

- **Branch per sprint**: `sprint-0-foundations`, `sprint-1-simulator`, … merge to `main` at each sprint DoD.
- **Commit style**: `feat(sim): negative binomial demand engine` / `test(dbt): reconciliation of movements to on-hand`.
- **Never commit**: `data/`, `*.duckdb` (except the demo slice), `.env`, `target/`, `logs/`.
- **Timebox rule**: if a task blows past 2× its estimate, stop and we simplify it. Written into each task below as the fallback.

---

## 3. One-time machine setup (~30 min)

```bash
pip install uv
```

```bash
mkdir -p D:/Yadnesh_Personal/Project1/freshflow && cd D:/Yadnesh_Personal/Project1/freshflow && git init -b main
```

```bash
uv init --python 3.12 --no-workspace
```

Then the dependency groups (run from the repo root):

```bash
uv add duckdb dbt-core dbt-duckdb pandas numpy scipy pyyaml faker python-dateutil
```

```bash
uv add scikit-learn lightgbm statsmodels pulp networkx
```

```bash
uv add fastapi "uvicorn[standard]" pydantic httpx streamlit plotly
```

```bash
uv add dagster dagster-webserver dagster-dbt
```

```bash
uv add --dev pytest pytest-cov ruff sqlfluff sqlfluff-templater-dbt pre-commit
```

**Verify before going further:**

```bash
uv run python -c "import duckdb,lightgbm,dagster,streamlit,fastapi,pulp; print('all imports OK')"
```

If `lightgbm` fails to install, that's the only likely Windows hiccup — fall back to `uv add xgboost` or `scikit-learn`'s `HistGradientBoostingRegressor`, which is bundled and needs no compiler. The forecast quality difference is negligible for this project.

---

## 4. Sprint 0 — Foundations (2–3 sessions, ~6h)

Goal: an empty-but-correct skeleton where the metric contract already exists.

| ID | Task | Est | Acceptance gate |
|---|---|---|---|
| **S0.1** | Repo scaffold: create the full directory tree from plan §8, plus `README.md`, `.gitignore`, `LICENSE` (MIT) | 30m | `git status` clean; tree matches §8 |
| **S0.2** | `tasks.py` runner with stubs: `setup`, `simulate`, `build`, `test`, `forecast`, `app`, `docs`, `demo-slice`, `all` | 45m | `uv run python tasks.py --help` lists all targets |
| **S0.3** | **`semantic/metrics.yml`** — all ~22 metrics from plan §2, each with label, description, numerator, denominator, grain, format, owner | 2h | YAML parses; every metric has all 7 required keys (assert in a test) |
| **S0.4** | ERD: author `docs/img/erd.md` as Mermaid `erDiagram` from plan §5; render check on GitHub | 1h | Renders in the GitHub preview |
| **S0.5** | dbt project init against DuckDB + one trivial model; `profiles.yml` pointing at `data/warehouse/freshflow.duckdb` | 45m | `uv run dbt build` succeeds |
| **S0.6** | Pre-commit (ruff + sqlfluff) + `.github/workflows/ci.yml` running lint → pytest → dbt build | 1h | Green CI badge on the first push |

**Sprint 0 DoD:** `uv run python tasks.py build` runs a dbt model end to end, CI is green, and `metrics.yml` exists before any pipeline code does.

> **Why metrics.yml first?** Because it's the contract everything downstream conforms to. Writing it after the pipeline means the pipeline dictates the metrics, which is backwards — and it's the exact habit interviewers probe when they ask "how did you decide what to measure?"

---

## 5. Sprint 1 — Simulator & Bronze (4–5 sessions, ~12h)

Goal: 365 days of realistic, dirty event data in `data/raw/`.

| ID | Task | Est | Acceptance gate |
|---|---|---|---|
| **S1.1** | Config YAMLs: `stores.yaml` (14 Mumbai stores, tier, capacity), `catalog.yaml` (1,500 SKUs generated from category templates), `calendar.yaml` (2025–26 Indian festivals, monsoon window, IPL dates), `segments.yaml` (5 customer segments), `suppliers.yaml` | 2h | Configs load; SKU popularity follows a Pareto shape (plot it) |
| **S1.2** | `demand.py` — the multiplicative λ model from plan §6 with negative binomial draws | 3h | Hourly demand shows twin peaks; weekend > weekday; Navratri spike visible in a sanity plot |
| **S1.3** | `supply.py` — POs, Gamma lead times, OTIF misses, **inbound freshness variance**, batch creation | 2h | Batches carry varying `mfg_date`; ~10% arrive with <70% shelf life |
| **S1.4** | `customers.py` — segment assignment, basket construction, **churn hazard** driven by stockouts / low-DTE receipts / late delivery | 2.5h | Retention curve decays plausibly; churn responds to injected stockouts |
| **S1.5** | `fefo.py` — FEFO batch allocation on every sale, stockout + substitution handling | 2h | **Invariant test: on-hand never negative; oldest batch always consumed first** |
| **S1.6** | `policies/baseline.py` — static reorder point + flat markdown ladder + one citywide ₹11 SKU | 1.5h | Runs 365 days without intervention |
| **S1.7** | `run.py` — orchestrate the full year, emit Hive-partitioned parquet to `data/raw/<source>/dt=.../` | 1.5h | ~5.5M order items; total < 2 GB; runs in < 10 min |
| **S1.8** | `dirt.py` — inject all 8 defect types from plan §6, seeded; write `docs/known_data_issues.md` | 1.5h | Each defect type is countable in raw data |
| **S1.9** | `tests/test_simulator.py` — 6 invariants | 1h | All pass in CI |

**Sprint 1 DoD:** `data/raw/` populated, invariants green, and a scratch notebook showing DOW/hour/festival patterns that look like real retail.

> **Timebox fallback:** if S1.2/S1.4 run long, cut to 3 customer segments and drop the IPL factor. Do **not** cut the churn hazard — plan §P7 depends on it, and without it your retention finding is unfalsifiable.

---

## 6. Sprint 2 — Warehouse & marts (4–5 sessions, ~12h)

| ID | Task | Est | Acceptance gate |
|---|---|---|---|
| **S2.1** | dbt sources + staging: dedupe, cast, **IST conform**, unit repair, SKU-code mapping, quarantine table for bad rows | 3h | Quarantine row count matches injected defect count |
| **S2.2** | `snapshots/dim_product_snapshot.sql` — SCD2 on `landed_cost` / `base_price` | 1h | Price change mid-year produces 2 versions |
| **S2.3** | Dimensions: `dim_store`, `dim_product`, `dim_customer`, `dim_date`, `dim_supplier`, `dim_promotion` | 2h | All PKs unique + not null |
| **S2.4** | Facts: `fct_inventory_batch`, `fct_inventory_movement`, `fct_order`, **`fct_order_item` with FEFO `batch_id`**, `fct_clickstream`, `fct_price_history` | 3h | `fct_order_item` row count ties to raw exactly |
| **S2.5** | `fct_availability_hour` — hourly on-hand via running balance window function; stockout intervals | 2h | Time-weighted in-stock % computes; differs from a midnight snapshot |
| **S2.6** | `agg_store_sku_day` — incremental with **48h lookback** for late arrivals | 2h | Re-running after injecting a late event updates the affected day |
| **S2.7** | ~60 dbt tests + `dbt-expectations`; **the reconciliation test**: `Σ movements = on_hand` per store-sku-day | 2h | `dbt build` green; reconciliation exact to the rupee |
| **S2.8** | `tasks.py demo-slice` — 5 stores × 90 days, marts only, **< 80 MB** DuckDB for deployment | 1.5h | File size verified; app can open it |
| **S2.9** | Publish dbt docs to GitHub Pages | 45m | Live URL, lineage graph renders |

**Sprint 2 DoD:** `dbt build` green, lineage live, `agg_store_sku_day` ties to raw order totals to the rupee, demo slice under 80 MB.

---

## 7. Sprint 3 — Forecast, expiry risk, live app ← **the resume-ready cut** (5–6 sessions, ~14h)

This is the sprint that must land. Everything after it is upside.

| ID | Task | Est | Acceptance gate |
|---|---|---|---|
| **S3.1** | **Censored-demand imputation** — detect stockout cells, scale by fitted intra-day arrival curve, emit `demand_imputed` | 2.5h | Imputed > observed only on censored days; documented |
| **S3.2** | Forecast baseline: seasonal naive + 7d MA, rolling-origin backtest harness | 1.5h | WAPE reported by ABC-XYZ class |
| **S3.3** | LightGBM model: lags 1/7/14/28, rolling means, DOW, price ratio, promo, festival, monsoon, salary week | 3h | **FVA vs naive is positive on A/X SKUs** — and if it isn't on C/Z, say so and use the naive rule there |
| **S3.4** | `mart_expiry_risk` — residual demand vs on-hand per batch, risk buckets, value-at-risk ₹ | 2h | Every open batch scored; at-risk ₹ sums sensibly |
| **S3.5** | `mart_customer_360` — RFM, cohorts, DDI | 2h | Cohort triangle renders; retention decays monotonically |
| **S3.6** | **`semantic/resolver.py`** — compile a metric request into DuckDB SQL | 2.5h | `pytest` renders *and executes* every metric in `metrics.yml` |
| **S3.7** | **FastAPI**: `/metrics/{name}`, `/actions/expiry`, `/health/freshness`; response echoes generated SQL | 2h | `/docs` OpenAPI page loads; SQL echo visible |
| **S3.8** | Streamlit pages 1–3, reading **only** from the API | 3h | No direct DuckDB call anywhere in `serving/web/` |
| **S3.9** | Deploy to Streamlit Community Cloud; README with architecture, ERD, screenshots, honest data note | 1.5h | **Live public URL** |

**Sprint 3 DoD:** a live URL you can paste into your resume today. Stop and update the resume here — don't wait for Sprint 5.

---

## 8. Sprint 4 — Decision engine (4–5 sessions, ~12h)

| ID | Task | Est | Acceptance gate |
|---|---|---|---|
| **S4.1** | Elasticity: log-log with store+SKU fixed effects, clustered SEs, partial pooling for thin SKUs | 3h | Signs are negative; premium categories more elastic than staples |
| **S4.2** | Markdown optimizer: grid over discount depth, expected-margin objective, cost floor, daily budget | 3h | Deeper discounts chosen for lower DTE and higher on-hand — verify the monotonicity |
| **S4.3** | Deal-slot allocator (PuLP IP) with all 5 constraints | 2.5h | Solver returns feasible; PL floor respected |
| **S4.4** | Transfer optimizer (min-cost flow) with shelf-life feasibility gate | 2.5h | No transfer recommended that can't survive transit |
| **S4.5** | Newsvendor replenishment with perishable critical ratio | 1.5h | Order-up-to level capped by shelf life |
| **S4.6** | Wire `rec_*` tables into `policies/optimized.py` — **close the loop** | 2h | One simulated day under Policy B produces a sensible action list |
| **S4.7** | Streamlit pages 4–5 | 2h | Elasticity curves + action queue render |

**Sprint 4 DoD:** running Policy B for one simulated day yields concrete, defensible actions with rupee values.

---

## 9. Sprint 5 — Proof & polish (4–5 sessions, ~12h)

| ID | Task | Est | Acceptance gate |
|---|---|---|---|
| **S5.1** | Experiment harness: 30 seeds × 90 days × 2 policies, **common random numbers** | 3h | Same seed reproduces identical demand under both policies — assert it |
| **S5.2** | Store-level randomized holdout + DiD with parallel-trends check; restrict transfers within arms (SUTVA) | 2.5h | Pre-period trends parallel; DiD estimate with CI |
| **S5.3** | Sensitivity: elasticity ±30%, forecast error ×1.5, shelf life −1 day | 1.5h | Table of which findings survive |
| **S5.4** | Ablation: B minus each component, attribute the gain | 1.5h | Per-component contribution sums ≈ total |
| **S5.5** | `mart_experiment_readout` + Executive page readout | 1.5h | Table from plan §10 filled with **real** numbers |
| **S5.6** | Dagster asset graph, daily schedule, asset checks | 2.5h | `dagster dev` shows the full DAG; screenshot for README |
| **S5.7** | Streamlit page 6 (Data Quality) + Soda freshness checks | 1.5h | Injected staleness triggers an alert |
| **S5.8** | `sql_showcase/` — 15 documented queries | 2.5h | Each runs against the warehouse; each has the business question in a comment |
| **S5.9** | `docs/business_case.pdf` (2 pages), OBS walkthrough video, ADRs, final README | 2.5h | §14 checklist complete |

**Sprint 5 DoD:** the full Definition of Done in plan §14.

---

## 10. Sprint 6 — Custom React front-end *(optional, only if Project 2 is already underway)*

Vite + React + ECharts against the existing API; deploy front-end on Vercel, API on Render. **Cut this without guilt** — tiers 1–2 of the BI layer already earn the resume claim.

---

## 11. Checkpoint gates — do not pass until true

| Gate | When | Test |
|---|---|---|
| **G1** | End Sprint 1 | On-hand never negative; FEFO strictly oldest-first; every injected defect countable |
| **G2** | End Sprint 2 | `agg_store_sku_day` revenue ties to raw order totals **exactly**; demo slice < 80 MB |
| **G3** | End Sprint 3 | Live public URL; **no number on screen that isn't in `metrics.yml`** |
| **G4** | End Sprint 4 | Markdown depth increases monotonically as DTE falls, holding demand constant |
| **G5** | End Sprint 5 | Experiment reproducible from a seed; every resume number traceable to `mart_experiment_readout` |

G3 and G5 are the two that matter most. G3 makes you employable-with-a-link. G5 makes every number on your resume defensible.

---

## 12. The rule that protects the whole project

> **`analytics/` may never import from `simulator/`.**

Add this as a pytest that walks the import graph and fails on violation. It is a three-line test that turns "I made the data" from a fatal objection into a design decision you can defend. Write it in Sprint 1, not later.

---

## 13. Timeline against your Nov 2026 switch

| Window | Milestone |
|---|---|
| Late Aug 2026 | Sprint 0 + Sprint 1 |
| Early Sep | Sprint 2 |
| **Mid Sep** | **Sprint 3 — resume updated, live link, start applying** |
| Late Sep | Sprint 4 (+ start Project 2 in parallel) |
| Early Oct | Sprint 5 — full proof |
| Mid/Late Oct | Project 2 finish, interview prep, mock rounds |
| Nov | Switch |

Applying from mid-September with a live link beats applying in November with a perfect one.
