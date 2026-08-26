{#
    Structural invariant of a type 2 dimension: consecutive versions of a key
    meet exactly. Version n ends on the day version n+1 begins - no gap, no
    overlap - and only the newest version is left open.

    The sale-coverage test proves this holds where it has been exercised. This
    one proves it holds everywhere, including for the 1,179 SKUs whose price
    never moved and for any date nobody happened to buy on. Cheap to run and it
    fails on the two mistakes that are easy to make here: closing a version at
    its own last day rather than at its successor's first, and leaving more
    than one version open per key.
#}

with versions as (

    select * from {{ ref('dim_product_snapshot') }}

),

adjacent as (

    select
        current_version.sku_id,
        current_version.version_no,
        current_version.valid_to_date,
        next_version.valid_from_date as next_valid_from_date
    from versions as current_version
    inner join versions as next_version
        on
            current_version.sku_id = next_version.sku_id
            and current_version.version_no + 1 = next_version.version_no

),

broken_chain as (

    select
        sku_id,
        version_no,
        valid_to_date,
        next_valid_from_date,
        'version does not end where its successor begins' as problem
    from adjacent
    where valid_to_date is distinct from next_valid_from_date

),

multiple_open as (

    select
        sku_id,
        max(version_no) as version_no,
        cast(null as date) as valid_to_date,
        cast(null as date) as next_valid_from_date,
        'more than one version left open' as problem
    from versions
    where is_current
    group by sku_id
    having count(*) > 1

)

select * from broken_chain

union all by name

select * from multiple_open
