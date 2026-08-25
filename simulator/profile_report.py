"""Sprint 1 data profile — does the generated year hold up? (task S1.9)

The validation gate in `verify.py` answers "is this dataset internally
consistent?". This answers the other half: "does it behave like a real
q-commerce network, and is there a real problem in it worth solving?"

    python tasks.py profile

Everything is queried in DuckDB over the raw parquet and rendered to a single
self-contained page. Two feeds are cleaned first - duplicates deduped, returns
excluded - exactly the way the staging layer will in Sprint 2, because leaving
the injected defects in would overstate revenue by half a percent and make
every figure on the page subtly wrong.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "docs" / "data_profile.html"


# ==========================================================  data
def collect(raw: Path = RAW) -> dict:
    con = duckdb.connect()

    def src(name: str) -> str:
        return f"read_parquet('{(raw / name).as_posix()}/*/*.parquet')"

    # de-duplicate and drop returns, as staging will
    con.execute(f"""create view items as
        select distinct order_id, sku_id, batch_id, qty, unit_base_price,
               unit_realized_price, unit_cogs, dte_at_sale, promo_id, is_substitution
        from {src("pos_order_items")} where qty > 0""")
    con.execute(f"create view orders as select distinct * from {src('pos_orders')}")
    con.execute(f"create view batches as select * from {src('wms_inventory_batch')}")
    con.execute(f"create view moves as select * from {src('wms_inventory_movement')}")
    last = con.execute(f"select max(snapshot_date) from {src('catalog_snapshot')}").fetchone()[0]
    con.execute(
        f"create view cat as select * from {src('catalog_snapshot')} where snapshot_date = '{last}'"
    )
    con.execute(f"create view outs as select * from {src('wms_stockout_interval')}")

    one = lambda s: con.execute(s).fetchone()  # noqa: E731
    rows = lambda s: con.execute(s).fetchall()  # noqa: E731

    d: dict = {}

    revenue, cogs, units, lines = one("""
        select sum(qty * unit_realized_price), sum(qty * unit_cogs), sum(qty), count(*)
        from items""")
    wastage, wasted_units = one("""
        select sum(-m.qty_delta * b.unit_landed_cost), sum(-m.qty_delta)
        from moves m join batches b using (batch_id) where m.event_type = 'expiry_writeoff'""")
    n_orders = one("select count(*) from orders")[0]
    subbed = one("select sum(qty) from items where is_substitution")[0]

    d["headline"] = {
        "revenue": revenue,
        "cogs": cogs,
        "gross_margin": revenue - cogs,
        "wastage": wastage,
        "wasted_units": int(wasted_units),
        "units": int(units),
        "lines": lines,
        "orders": n_orders,
        "substituted": int(subbed),
        "gm_pct": (revenue - cogs) / revenue,
        "gm_awm_pct": (revenue - cogs - wastage) / revenue,
        "wastage_pct_rev": wastage / revenue,
    }

    # daily demanded vs sold: sold from the order feed, lost from the stockout feed
    d["daily"] = [
        {"d": str(day), "sold": int(s)}
        for day, s in rows("""
            select cast(o.order_ts as date) d, sum(i.qty)
            from items i join orders o using (order_id) group by 1 order by 1""")
    ]
    d["stockout_days"] = [
        {"d": str(day), "cells": int(n)}
        for day, n in rows("select event_date, count(*) from outs group by 1 order by 1")
    ]

    d["waste_by_category"] = [
        {"k": k, "v": float(v)}
        for k, v in rows("""
            select c.l1_category, sum(-m.qty_delta * b.unit_landed_cost) v
            from moves m join batches b using (batch_id) join cat c on c.sku_id = b.sku_id
            where m.event_type = 'expiry_writeoff' group by 1 having v > 0 order by v desc""")
    ]

    d["suppliers"] = [
        {"k": k, "days": float(u), "cost": float(c), "batches": int(n)}
        for k, n, u, c in rows("""
            select b.supplier_id, count(*), avg(b.usable_days), avg(b.unit_landed_cost)
            from batches b join cat c on c.sku_id = b.sku_id
            where c.l1_category = 'Dairy & Eggs' and c.shelf_life_days <= 14
              and b.supplier_id <> 'SUP-OPENING'
            group by 1 order by 3 desc""")
    ]

    d["promo"] = [
        {"k": k, "units": int(u), "margin": float(m)}
        for k, u, m in rows("""
            select case when promo_id is null then 'Full price'
                        when promo_id = 'PROMO-DEAL11' then 'Rs 11 deal rail'
                        when promo_id = 'PROMO-MD-30' then 'Markdown 30%'
                        else 'Markdown 50%' end,
                   sum(qty), sum(qty * (unit_realized_price - unit_cogs))
            from items group by 1 order by 2 desc""")
    ]

    d["velocity"] = [
        {"decile": int(x), "stockout_rate": float(r), "share": float(s)}
        for x, r, s in rows("""
            with v as (
                select i.sku_id, sum(i.qty) units
                from items i group by 1),
            ranked as (
                select sku_id, units, ntile(10) over (order by units) decile,
                       units / sum(units) over () vol_share
                from v),
            so as (select sku_id, count(*) n from outs group by 1)
            select r.decile, avg(coalesce(so.n, 0)) / (365.0 * 14), sum(r.vol_share)
            from ranked r left join so using (sku_id) group by 1 order by 1""")
    ]

    d["dte"] = [
        {"k": str(k), "v": int(v)}
        for k, v in rows("""
            select case when i.dte_at_sale = 0 then 'expires today'
                        when i.dte_at_sale = 1 then '1 day left'
                        when i.dte_at_sale <= 3 then '2-3 days'
                        when i.dte_at_sale <= 7 then '4-7 days'
                        else '8+ days' end k, sum(i.qty)
            from items i join cat c using (sku_id)
            where c.shelf_life_days <= 14 group by 1
            order by case k when 'expires today' then 0 when '1 day left' then 1
                            when '2-3 days' then 2 when '4-7 days' then 3 else 4 end""")
    ]

    d["counts"] = {
        "days": len(d["daily"]),
        "stores": one("select count(distinct store_id) from orders")[0],
        "skus": one("select count(*) from cat")[0],
        "batches": one("select count(*) from batches")[0],
        "movements": one("select count(*) from moves")[0],
    }

    manifest = raw.parent / "_manifest" / "dirt.json"
    d["defects"] = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
    return d


# ==========================================================  render helpers
def scale(v, lo, hi, a, b):
    return a + (v - lo) / (hi - lo) * (b - a) if hi > lo else a


def path_of(points):
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in points)


def inr(v: float) -> str:
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:.1f} L"
    return f"₹{v:,.0f}"


def hbars(data, key, val, fmt, width=520, row_h=26, pad_left=170, colour="s1", zero=False):
    """Horizontal bars. `zero` centres on zero for values that can go negative."""
    height = len(data) * row_h + 16
    inner = width - pad_left - 66
    vals = [d[val] for d in data]
    lo = min(0, min(vals)) if zero else 0
    hi = max(vals) if max(vals) > 0 else 1
    x0 = pad_left + (scale(0, lo, hi, 0, inner) if zero else 0)

    out = []
    for i, d in enumerate(data):
        y = 8 + i * row_h
        v = d[val]
        x = pad_left + scale(min(v, 0), lo, hi, 0, inner)
        w = max(abs(scale(v, lo, hi, 0, inner) - scale(0, lo, hi, 0, inner)), 1.5)
        cls = "neg" if v < 0 else colour
        out.append(
            f'<rect class="bar {cls}" x="{x:.1f}" y="{y}" width="{w:.1f}" height="{row_h - 9}" '
            f'rx="3"><title>{d[key]}: {fmt(v)}</title></rect>'
        )
        out.append(f'<text class="rowlab" x="{pad_left - 12}" y="{y + row_h - 14}">{d[key]}</text>')
        out.append(f'<text class="rowval" x="{x + w + 7:.1f}" y="{y + row_h - 14}">{fmt(v)}</text>')
    if zero:
        out.append(f'<line class="axis" x1="{x0:.1f}" y1="4" x2="{x0:.1f}" y2="{height - 8}"/>')
    return f'<svg viewBox="0 0 {width} {height}" class="chart">{"".join(out)}</svg>'


def render(d: dict) -> str:
    h, c = d["headline"], d["counts"]

    # ---- the year: units sold per day, with stockout pressure beneath
    W, H = 1000, 260
    PL, PR, PT, PB = 56, 16, 20, 30
    sold = [r["sold"] for r in d["daily"]]
    hi = max(sold) * 1.10
    pts = [
        (scale(i, 0, len(sold) - 1, PL, W - PR), scale(v, 0, hi, H - PB, PT))
        for i, v in enumerate(sold)
    ]
    grid = "".join(
        f'<line class="grid" x1="{PL}" y1="{scale(v, 0, hi, H - PB, PT):.1f}" '
        f'x2="{W - PR}" y2="{scale(v, 0, hi, H - PB, PT):.1f}"/>'
        f'<text class="tick ay" x="{PL - 8}" y="{scale(v, 0, hi, H - PB, PT) + 3.5:.1f}">'
        f"{v // 1000}k</text>"
        for v in range(5000, int(hi), 5000)
    )
    months = "".join(
        f'<text class="tick" x="{scale(i, 0, len(sold) - 1, PL, W - PR):.1f}" y="{H - PB + 16}">'
        f"{dt.date.fromisoformat(r['d']):%b}</text>"
        for i, r in enumerate(d["daily"])
        if dt.date.fromisoformat(r["d"]).day == 1
    )
    year_chart = f"""<svg viewBox="0 0 {W} {H}" class="chart" role="img"
      aria-label="Units sold per day across the simulated year">
      {grid}
      <path class="area s1" d="{path_of(pts)} L{W - PR} {H - PB} L{PL} {H - PB} Z"/>
      <path class="line s1" d="{path_of(pts)}"/>
      <line class="axis" x1="{PL}" y1="{H - PB}" x2="{W - PR}" y2="{H - PB}"/>
      {months}
    </svg>"""

    # ---- margin waterfall
    steps = [
        ("Revenue", h["revenue"], "s1"),
        ("less COGS", -h["cogs"], "neg"),
        ("Gross margin", h["gross_margin"], "s3"),
        ("less wastage", -h["wastage"], "neg"),
        ("GM after wastage", h["gross_margin"] - h["wastage"], "s3"),
    ]
    WW, WH, wpad = 1000, 210, 40
    top = h["revenue"] * 1.08
    slot = (WW - 80) / len(steps)
    bars, running = [], 0.0
    for i, (label, v, cls) in enumerate(steps):
        is_total = cls != "neg" and i in (0, 2, 4)
        base = 0 if is_total else running
        val = v if is_total else v
        y0 = scale(max(base, base + val), 0, top, WH - wpad, 12)
        y1 = scale(min(base, base + val), 0, top, WH - wpad, 12)
        x = 40 + i * slot + slot * 0.18
        bars.append(
            f'<rect class="bar {cls}" x="{x:.1f}" y="{y0:.1f}" width="{slot * 0.64:.1f}" '
            f'height="{max(y1 - y0, 2):.1f}" rx="3"><title>{label}: {inr(abs(v))}</title></rect>'
        )
        bars.append(
            f'<text class="wlab" x="{x + slot * 0.32:.1f}" y="{WH - wpad + 15}">{label}</text>'
        )
        bars.append(
            f'<text class="wval" x="{x + slot * 0.32:.1f}" y="{y0 - 6:.1f}">{inr(abs(v))}</text>'
        )
        running = base + val if not is_total else v
    waterfall = (
        f'<svg viewBox="0 0 {WW} {WH}" class="chart" role="img" '
        f'aria-label="From revenue to gross margin after wastage">{"".join(bars)}'
        f'<line class="axis" x1="40" y1="{WH - wpad}" x2="{WW - 20}" y2="{WH - wpad}"/></svg>'
    )

    defect_rows = "".join(
        f"<tr><td>{i}</td><td>{x['title']}</td><td class='num'>{x['rows']:,}</td></tr>"
        for i, x in enumerate(d["defects"], 1)
    )
    promo_rows = "".join(
        f"<tr><td>{p['k']}</td><td class='num'>{p['units']:,}</td>"
        f"<td class='num {'bad' if p['margin'] < 0 else ''}'>{inr(p['margin'])}</td></tr>"
        for p in d["promo"]
    )
    dte_rows = "".join(
        f"<tr><td>{x['k']}</td><td class='num'>{x['v']:,}</td>"
        f"<td class='num'>{x['v'] / sum(y['v'] for y in d['dte']):.1%}</td></tr>"
        for x in d["dte"]
    )

    # the meaningful comparison is between the two branded dairy suppliers.
    # Private label is sourced differently, so holding it up as the benchmark
    # would overstate the gap and invite the obvious objection.
    branded = [s for s in d["suppliers"] if s["k"].startswith("SUP-DAIRY")]
    worst = min(branded, key=lambda s: s["days"])
    best = max(branded, key=lambda s: s["days"])
    md50 = next((p for p in d["promo"] if "50" in p["k"]), None)
    top_decile = d["velocity"][-1]
    # decile 1 is barely-ranged tail stock, so the honest comparison starts above it
    best_served = min(d["velocity"][1:], key=lambda v: v["stockout_rate"])

    return f"""<title>FreshFlow Data Profile</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  --ground:#f4f6f4; --card:#fdfdfc; --ink:#0d1211; --ink-2:#55605c; --muted:#8a938f;
  --grid:#e3e7e3; --axis:#c5cdc8; --rule:rgba(13,18,17,.10); --accent:#0f8a5f;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --neg:#e34948;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#0e1110; --card:#181c1b; --ink:#f1f4f2; --ink-2:#b6c0bb; --muted:#8a938f;
    --grid:#262c2a; --axis:#39413e; --rule:rgba(255,255,255,.10); --accent:#22b988;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --neg:#e66767;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#0e1110; --card:#181c1b; --ink:#f1f4f2; --ink-2:#b6c0bb; --muted:#8a938f;
  --grid:#262c2a; --axis:#39413e; --rule:rgba(255,255,255,.10); --accent:#22b988;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --neg:#e66767;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--ground); color:var(--ink); font-size:15px; line-height:1.55;
  font-family:Archivo,system-ui,-apple-system,"Segoe UI",sans-serif; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1120px; margin:0 auto; padding:44px 24px 72px; }}
.eyebrow {{ font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:11px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--accent); font-weight:500; }}
h1 {{ font-size:clamp(28px,4vw,40px); line-height:1.08; margin:10px 0 12px; font-weight:700;
  letter-spacing:-.02em; text-wrap:balance; }}
.lede {{ color:var(--ink-2); max-width:66ch; font-size:16.5px; margin:0; }}
.note {{ margin-top:18px; padding:12px 16px; border-left:3px solid var(--accent);
  background:var(--card); color:var(--ink-2); font-size:14px; max-width:76ch; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px;
  background:var(--rule); border:1px solid var(--rule); margin:32px 0 8px; }}
.stat {{ background:var(--card); padding:16px 18px; }}
.stat .k {{ font-family:"IBM Plex Mono",monospace; font-size:10.5px; letter-spacing:.1em;
  text-transform:uppercase; color:var(--muted); }}
.stat .v {{ font-size:25px; font-weight:600; letter-spacing:-.02em; margin-top:5px;
  font-variant-numeric:tabular-nums; }}
.stat .s {{ font-size:12.5px; color:var(--ink-2); margin-top:2px; }}
section {{ margin-top:46px; }}
h2 {{ font-size:19px; font-weight:600; margin:0 0 4px; letter-spacing:-.01em; }}
.sub {{ color:var(--ink-2); font-size:14px; margin:0 0 16px; max-width:78ch; }}
.card {{ background:var(--card); border:1px solid var(--rule); padding:18px 20px 14px; }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(420px,1fr)); gap:20px; }}
.scroll {{ overflow-x:auto; }}
.chart {{ display:block; width:100%; height:auto; min-width:340px; }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.axis {{ stroke:var(--axis); stroke-width:1; }}
.tick {{ font-family:"IBM Plex Mono",monospace; font-size:10px; fill:var(--muted); text-anchor:middle; }}
.ay {{ text-anchor:end; }}
.line {{ fill:none; stroke-width:2; stroke-linejoin:round; }}
.area {{ stroke:none; opacity:.13; }}
.s1 {{ stroke:var(--s1); }} .area.s1 {{ fill:var(--s1); }}
.bar.s1 {{ fill:var(--s1); }} .bar.s2 {{ fill:var(--s2); }} .bar.s3 {{ fill:var(--s3); }}
.bar.neg {{ fill:var(--neg); }}
.rowlab {{ font-size:12.5px; fill:var(--ink-2); text-anchor:end; }}
.rowval {{ font-family:"IBM Plex Mono",monospace; font-size:11px; fill:var(--muted); }}
.wlab {{ font-size:11.5px; fill:var(--ink-2); text-anchor:middle; }}
.wval {{ font-family:"IBM Plex Mono",monospace; font-size:11px; fill:var(--ink); text-anchor:middle; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; margin-top:4px; }}
th,td {{ text-align:left; padding:7px 12px 7px 0; border-bottom:1px solid var(--rule);
  font-variant-numeric:tabular-nums; }}
th {{ color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:.08em; }}
td.num {{ text-align:right; }} td.bad {{ color:var(--neg); font-weight:600; }}
.finding {{ margin-top:12px; padding:11px 15px; background:var(--card);
  border-left:3px solid var(--s2); font-size:13.5px; color:var(--ink-2); }}
.finding b {{ color:var(--ink); }}
footer {{ margin-top:56px; padding-top:20px; border-top:1px solid var(--rule);
  color:var(--muted); font-size:12.5px; }}
</style>

<div class="wrap">
  <div class="eyebrow">FreshFlow &nbsp;·&nbsp; Sprint 1 complete &nbsp;·&nbsp; Gate G1 passed</div>
  <h1>A year of the network, and the problem inside it</h1>
  <p class="lede">The simulator now produces {c["days"]} days across {c["stores"]} Mumbai dark
  stores and {c["skus"]:,} SKUs — {h["orders"]:,} orders, {h["lines"]:,} order lines, every one
  allocated to a physical batch with a real expiry date. This is what came out, and what it says
  about how much is being left on the table.</p>

  <div class="note"><b>Figures are cleaned, not raw.</b> Duplicate events are deduplicated and
  returns excluded, exactly as the staging layer will do in Sprint 2. Leaving the injected defects
  in would overstate revenue by about half a percent and make every number here subtly wrong.</div>

  <div class="stats">
    <div class="stat"><div class="k">Revenue</div><div class="v">{inr(h["revenue"])}</div>
      <div class="s">{h["units"]:,} units</div></div>
    <div class="stat"><div class="k">Gross margin</div><div class="v">{h["gm_pct"]:.1%}</div>
      <div class="s">{inr(h["gross_margin"])}</div></div>
    <div class="stat"><div class="k">GM after wastage</div><div class="v">{
        h["gm_awm_pct"]:.1%}</div>
      <div class="s">the north star</div></div>
    <div class="stat"><div class="k">Wastage</div><div class="v">{inr(h["wastage"])}</div>
      <div class="s">{h["wasted_units"]:,} units, {h["wastage_pct_rev"]:.1%} of revenue</div></div>
    <div class="stat"><div class="k">Substituted</div><div class="v">{h["substituted"]:,}</div>
      <div class="s">units bought as second choice</div></div>
    <div class="stat"><div class="k">Batches</div><div class="v">{c["batches"]:,}</div>
      <div class="s">{c["movements"]:,} movements</div></div>
  </div>

  <section>
    <h2>The year, in units sold</h2>
    <p class="sub">Daily units across the network. Festival peaks, the monsoon lift and the
    month-end trough are all visible — but so is the ceiling the baseline policy keeps hitting.</p>
    <div class="card"><div class="scroll">{year_chart}</div></div>
  </section>

  <section>
    <h2>Where the money goes</h2>
    <p class="sub">Revenue through to gross margin after wastage — the project's north star.
    The gap between the two green bars is what the optimiser in Sprint 4 is aiming at.</p>
    <div class="card"><div class="scroll">{waterfall}</div></div>
    <div class="finding"><b>{inr(h["wastage"])} written off</b> — {h["wastage_pct_rev"]:.1%} of
    revenue, or {h["wastage"] / h["gross_margin"]:.0%} of the gross margin the network earned.</div>
  </section>

  <section>
    <div class="grid2">
      <div>
        <h2>Wastage is concentrated</h2>
        <p class="sub">Write-off value by category, over the year.</p>
        <div class="card">{hbars(d["waste_by_category"], "k", "v", inr, colour="s2")}</div>
      </div>
      <div>
        <h2>The markdown ladder loses money</h2>
        <p class="sub">Units and realised margin by price tier.</p>
        <div class="card">
          <table><thead><tr><th>Tier</th><th class="num">Units</th><th class="num">Margin</th></tr></thead>
          <tbody>{promo_rows}</tbody></table>
          <div class="finding">The flat <b>50% ladder moved {md50["units"]:,} units at
          {inr(md50["margin"])}</b> — it sells below landed cost. Clearing stock at any price is
          not the same as clearing it profitably, and that is problem P2 in one line.</div>
        </div>
      </div>
    </div>
  </section>

  <section>
    <h2>The supplier nobody is looking at</h2>
    <p class="sub">Short-life dairy only, by supplier: average usable shelf life on arrival, and
    what it cost.</p>
    <div class="card">
      {
        hbars(
            d["suppliers"],
            "k",
            "days",
            lambda v: f"{v:.2f} days",
            colour="s3",
            width=1000,
            pad_left=210,
        )
    }
      <div class="finding"><b>{worst["k"]} delivers {worst["days"]:.2f} usable days against
      {best["k"]}'s {best["days"]:.2f}</b> — {1 - worst["days"] / best["days"]:.0%} less selling
      window on the same products, across {worst["batches"]:,} batches. It is also the cheaper
      supplier, which is why nobody has dropped it: procurement is measured on unit cost and the
      write-off lands on operations. That trade-off is invisible without batch-level inventory.</div>
    </div>
  </section>

  <section>
    <div class="grid2">
      <div>
        <h2>The fastest movers run out most</h2>
        <p class="sub">Share of store-SKU-days out of stock, by sales-volume decile.</p>
        <div class="card">
          {
        hbars(
            [{"k": f"decile {v['decile']}", "v": v["stockout_rate"]} for v in d["velocity"]],
            "k",
            "v",
            lambda x: f"{x:.1%}",
            colour="s1",
        )
    }
          <div class="finding">Above the long tail the relationship runs the wrong way:
          from <b>{best_served["stockout_rate"]:.1%} in decile {best_served["decile"]}</b> up to
          <b>{top_decile["stockout_rate"]:.1%} in decile {top_decile["decile"]}</b>, which carries
          {top_decile["share"]:.0%} of all units. The SKUs whose demand is most predictable are the
          ones running out, because a flat safety-days rule cannot cope with demand this
          over-dispersed. (Decile 1 is a separate story — barely-ranged tail stock that is out more
          often than it is in.)</div>
        </div>
      </div>
      <div>
        <h2>How fresh it was when it sold</h2>
        <p class="sub">Perishable units by days to expiry at the moment of sale.</p>
        <div class="card">
          <table><thead><tr><th>Remaining life</th><th class="num">Units</th><th class="num">Share</th></tr></thead>
          <tbody>{dte_rows}</tbody></table>
          <div class="finding">Freshness is a customer-experience metric, not only a cost one.
          Every unit sold on its expiry day is a review waiting to happen — and in this model it
          also raises that customer's churn hazard.</div>
        </div>
      </div>
    </div>
  </section>

  <section>
    <h2>The raw layer is deliberately broken</h2>
    <p class="sub">Eight defects injected on purpose, each needing a different fix in staging.
    Gate G1 confirms every one reconciles against the defect log — and that nothing else does not.</p>
    <div class="card">
      <table><thead><tr><th>#</th><th>Defect</th><th class="num">Rows</th></tr></thead>
      <tbody>{defect_rows}</tbody></table>
    </div>
  </section>

  <footer>
    Generated by <code>simulator/profile_report.py</code> from the seed-42 run. All data is
    synthetic, produced by a purpose-built model; no real company data is used and the brand names
    are fictional. Next: the dbt warehouse, where these eight defects get cleaned for real.
  </footer>
</div>
"""


def main() -> int:
    if not RAW.exists():
        print(f"no raw layer at {RAW} - run: python tasks.py simulate")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(collect()), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
