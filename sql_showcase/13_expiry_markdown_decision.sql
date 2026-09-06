-- question: Of the stock that will expire unsold, how much of it is worth
--           discounting today - and why does the optimiser recommend marking
--           down almost none of it?
-- technique: scoring joined to the decision it produced, so the gap between
--            "at risk" and "acted on" is visible instead of implied
-- needs_days: 30
-- needs_tables: marts.mart_expiry_risk, marts.rec_markdown
--
-- **The interesting output of this query is how small the last column is.**
-- Hundreds of batches carry real value at risk, and the markdown optimiser
-- recommends discounting a handful. That is not a bug and it is the finding
-- the whole elasticity strand produced: every fitted coefficient sits inside
-- the unit interval, so while stock is short of demand the margin is
-- `sold x price - qty x cost`, the cost term does not move with price, and
-- cutting price gives up more on the units already selling than it wins on the
-- ones the cut brings in.
--
-- A dashboard that showed only "value at risk" would imply an action for every
-- rupee of it. Showing the two columns side by side is what makes the
-- recommendation defensible: the stock is genuinely at risk, and discounting
-- is measured not to pay.
--
-- `at_risk` only. Batches already past expiry are a booked loss - P(unsold) is
-- 1 by definition - and no markdown recovers stock that has already gone.

with at_risk as (

    select
        risk.store_id,
        risk.sku_id,
        risk.l1_category,
        risk.batch_id,
        risk.days_to_expiry,
        risk.qty_remaining,
        risk.units_at_risk,
        risk.value_at_risk_inr,
        risk.expiry_risk_score
    from marts.mart_expiry_risk as risk
    where risk.risk_state = 'at_risk'

),

scored as (

    select
        batch_id,
        decision,
        -- the optimiser records why it declined, which is the column that turns
        -- "it recommended nothing" from a shrug into an explanation
        coalesce(decline_reason, 'price held after evaluating the depth grid') as reason
    from marts.rec_markdown

)

select
    at_risk.l1_category,
    count(*) as batches_at_risk,
    round(sum(at_risk.value_at_risk_inr), 0) as value_at_risk_inr,
    round(sum(at_risk.units_at_risk), 0) as units_at_risk,
    round(avg(at_risk.days_to_expiry), 1) as avg_days_to_expiry,
    round(avg(at_risk.expiry_risk_score), 3) as avg_risk_score,
    -- the decision, against the exposure
    count(*) filter (where scored.decision = 'markdown') as marked_down,
    count(*) filter (where scored.decision = 'hold_price') as held_price,
    count(*) filter (where scored.decision = 'no_recommendation') as no_coefficient,
    round(
        100.0 * count(*) filter (where scored.decision = 'markdown') / nullif(count(*), 0), 1
    ) as pct_of_at_risk_acted_on
from at_risk
left join scored on at_risk.batch_id = scored.batch_id
group by at_risk.l1_category
order by value_at_risk_inr desc;
