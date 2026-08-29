"""One simulated day under either arm, as an action list (task S4.6's gate).

Sprint 4's definition of done is that running Policy B for one simulated day
yields concrete, defensible actions with rupee values. This produces that list,
and it produces the same list for the baseline so the two can be read side by
side - a treatment arm's output only means something next to what it replaced.

The day is genuinely simulated rather than reconstructed: the simulation runs
forward from its own opening stock under the chosen policy, and the actions
printed are the ones that policy took on the final day, against the state its
own earlier decisions produced. Warming up matters. Asking a replenishment
policy what it would do on day one tells you about the opening-stock routine,
not about the policy.

    python tasks.py actions --policy optimized --days 21
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np

from simulator.config_loader import load_sim_config
from simulator.policies.base import PolicyContext
from simulator.run import SimulationRun, build_policy

NO_STOCK_DTE = 9999


def context_after(run: SimulationRun, day: dt.date) -> PolicyContext:
    """The state a policy would see on the morning after the run's last day."""
    return PolicyContext(
        date=day,
        on_hand=run.ledger.on_hand_matrix.copy(),
        on_order=run.on_order,
        trailing_avg=run._trailing_demand(),
        min_dte=run.ledger.min_dte_matrix(day.toordinal()),
        store_open=np.ones(run.S, dtype=bool),
        catalog=run.catalog,
        rng=np.random.default_rng([run.seed, 4242]),
    )


def action_list(run: SimulationRun, policy, ctx: PolicyContext) -> dict:
    """Every decision the policy makes for one day, priced."""
    base_price = run.base_price
    landed_cost = run.landed_cost

    order = policy.replenish(ctx)
    order_value = float((order.qty * landed_cost[order.sku_idx]).sum()) if len(order) else 0.0

    discount = policy.markdown(ctx)
    marked = discount > 0
    markdown_units = float(np.where(marked, ctx.on_hand, 0.0).sum())
    revenue_forgone = float((discount * ctx.on_hand * base_price[None, :]).sum())

    deals = policy.deal_slots(ctx)
    deal_lines = sum(len(v) for v in deals.values())
    deal_subsidy = 0.0
    for store, skus in deals.items():
        for sku in skus:
            uptake = min(ctx.trailing_avg[store, sku] * 3.57, ctx.on_hand[store, sku])
            deal_subsidy += uptake * (base_price[sku] - 11.0)

    moves = policy.transfers(ctx)
    transfer_units = sum(qty for *_, qty in moves)
    transfer_value = sum(qty * landed_cost[sku] for _, _, sku, qty in moves)

    at_risk = (ctx.min_dte <= 2) & (ctx.min_dte < NO_STOCK_DTE) & (ctx.on_hand > 0)
    value_at_risk = float((ctx.on_hand * landed_cost[None, :])[at_risk].sum())

    return {
        "policy": policy.name,
        "date": ctx.date,
        "order_lines": len(order),
        "order_units": int(order.qty.sum()) if len(order) else 0,
        "order_value": order_value,
        "markdown_cells": int(marked.sum()),
        "markdown_units": markdown_units,
        "revenue_forgone": revenue_forgone,
        "deal_lines": deal_lines,
        "deal_subsidy": deal_subsidy,
        "transfer_moves": len(moves),
        "transfer_units": transfer_units,
        "transfer_value": transfer_value,
        "value_at_risk": value_at_risk,
        "_deals": deals,
        "_moves": moves,
    }


def render(actions: dict, run: SimulationRun) -> None:
    print(f"\n  {actions['policy'].upper()} - actions for {actions['date']}\n")
    rows = [
        (
            "replenishment orders",
            actions["order_lines"],
            actions["order_units"],
            actions["order_value"],
            "landed cost of what is being bought",
        ),
        (
            "markdowns",
            actions["markdown_cells"],
            actions["markdown_units"],
            -actions["revenue_forgone"],
            "revenue given up at current shelf quantity",
        ),
        (
            "deal slots",
            actions["deal_lines"],
            0,
            -actions["deal_subsidy"],
            "subsidy at the expected uptake",
        ),
        (
            "transfers",
            actions["transfer_moves"],
            actions["transfer_units"],
            actions["transfer_value"],
            "landed cost of stock moved, not spent",
        ),
    ]
    print(f"    {'action':<24}{'lines':>7}{'units':>10}{'rupees':>14}   note")
    for label, lines, units, rupees, note in rows:
        print(f"    {label:<24}{lines:>7,}{units:>10,.0f}{rupees:>14,.0f}   {note}")
    print(
        f"\n    {'stock at risk (<=2d)':<24}{'':>7}{'':>10}{actions['value_at_risk']:>14,.0f}"
        "   landed cost exposed to expiry"
    )

    deals = actions["_deals"]
    shown = [(s, k) for s, ks in sorted(deals.items()) for k in ks][:6]
    if shown:
        print("\n    deal rail, first few stores:")
        for store, sku in shown:
            print(
                f"      {run.store_ids[store]:<12}{run.sku_ids[sku]:<12}"
                f"{run.catalog['sku_name'].iloc[sku][:38]:<40}"
                f"Rs {run.base_price[sku]:>6.0f} -> 11"
            )

    moves = actions["_moves"][:5]
    if moves:
        print("\n    transfers:")
        for frm, to, sku, qty in moves:
            print(
                f"      {run.store_ids[frm]:<12} -> {run.store_ids[to]:<12}"
                f"{run.sku_ids[sku]:<12}{qty:>5} units"
            )


def compare(baseline: dict, optimized: dict) -> None:
    print("\n  the two arms on the same day, from the same shelf:\n")
    fields = [
        ("order lines", "order_lines", "{:,.0f}"),
        ("order units", "order_units", "{:,.0f}"),
        ("markdown cells", "markdown_cells", "{:,.0f}"),
        ("revenue forgone to markdown", "revenue_forgone", "Rs {:,.0f}"),
        ("deal slots", "deal_lines", "{:,.0f}"),
        ("deal subsidy", "deal_subsidy", "Rs {:,.0f}"),
        ("transfers", "transfer_moves", "{:,.0f}"),
    ]
    print(f"    {'':<30}{'baseline':>16}{'optimized':>16}")
    for label, key, fmt in fields:
        print(f"    {label:<30}{fmt.format(baseline[key]):>16}{fmt.format(optimized[key]):>16}")


def main() -> int:
    parser = argparse.ArgumentParser(description="One simulated day's action list.")
    parser.add_argument("--days", type=int, default=21, help="warm-up days before the readout")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--policy", default="both", choices=["baseline", "optimized", "both"])
    args = parser.parse_args()

    cfg = load_sim_config()
    arms = ["baseline", "optimized"] if args.policy == "both" else [args.policy]

    results = {}
    for arm in arms:
        print(f"  simulating {args.days} days under {arm}...", flush=True)
        run = SimulationRun(cfg, seed=args.seed, days=args.days, policy_name=arm, quiet=True)
        run.run()
        day = run.summary[-1]["date"] + dt.timedelta(days=1)
        ctx = context_after(run, day)
        policy = build_policy(arm, cfg, run.catalog)
        actions = action_list(run, policy, ctx)
        render(actions, run)
        results[arm] = actions

    if len(results) == 2:
        compare(results["baseline"], results["optimized"])

    total = sum(
        a["order_lines"] + a["markdown_cells"] + a["deal_lines"] + a["transfer_moves"]
        for a in results.values()
    )
    print(
        f"\n  S4.6 gate - a simulated day produces a concrete action list: "
        f"{'PASS' if total > 0 else 'FAIL'}\n"
    )
    return 0 if total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
