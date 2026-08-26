{#
    Order headers, deduplicated (defect 1) and with the arrival lag that
    defect 2 creates made explicit.

    **Deduplication is on the business row hash, not on order_id.** A retried
    webhook redelivers the header byte for byte, so the hash identifies it. The
    id alone would not: it is the correct grain for this feed but it is also
    the wrong tool, because a rule that keeps "one row per order_id" happily
    discards a genuinely conflicting second version instead of failing on it.
    Hashing the content and then testing `order_id` for uniqueness separates
    the two: identical rows collapse, contradictory ones go red.

    **`arrival_date` is the earliest partition a row appeared in.** Defect 2
    moves an order into a partition up to 48h after it happened, and when a
    duplicated row is the one that moves, the same order legitimately exists in
    two partitions. First-seen is the honest reading of when the data became
    available, and taking min() rather than picking arbitrarily makes the
    model deterministic - re-running it cannot flip which copy survived.

    **`arrival_lag_days` is the column S2.6 is built on.** An incremental keyed
    on the partition drops every one of the 23k late orders. Publishing the lag
    turns that from a silent loss into a number the 48h lookback is sized
    against, and a test can assert it never exceeds the documented window.
#}

{%- set business_columns = [
    'order_id',
    'order_ts',
    'promised_ts',
    'delivered_ts',
    'is_late',
    'payment_mode',
    'n_units',
    'store_id',
    'customer_id',
] -%}

with source as (

    select * from {{ source('pos', 'pos_orders') }}

),

hashed as (

    select
        order_id,
        store_id,
        customer_id,
        payment_mode,
        n_units,
        is_late,

        -- the POS writes IST; the name says so from here on
        order_ts as order_ts_ist,
        promised_ts as promised_ts_ist,
        delivered_ts as delivered_ts_ist,

        dt as arrival_date,
        {{ row_hash(business_columns) }} as row_hash

    from source

),

deduplicated as (

    select *
    from hashed
    qualify row_number() over (partition by row_hash order by arrival_date) = 1

)

select
    order_id,
    store_id,
    customer_id,
    payment_mode,
    n_units,
    is_late,
    order_ts_ist,
    promised_ts_ist,
    delivered_ts_ist,
    cast(order_ts_ist as date) as order_date_ist,
    date_diff('minute', promised_ts_ist, delivered_ts_ist) as delivery_slack_minutes,

    -- defect 2: how far behind its partition the event actually sits
    arrival_date,
    date_diff('day', cast(order_ts_ist as date), arrival_date) as arrival_lag_days,

    row_hash
from deduplicated
