{#
    The calendar, and the India-specific flags that explain most of the
    variance in this dataset.

    Salary week, monsoon and the festival cycle are not decoration. A forecast
    that knows only day-of-week under-predicts the first week of every month by
    double digits and misses Diwali entirely, and the whole point of the
    demand model in Sprint 3 is that these are available to it as features.

    **Where the flags come from, and where they deliberately do not.** The
    analytics layer is not allowed to read the simulator's parameters - that is
    what makes any result it produces worth anything, and `test_import_boundary`
    enforces it for code. The same discipline applies to data, so the line
    drawn here is between public calendar facts and the generator's dials:

      - Festival names and dates come from a seed. A retailer knows when Diwali
        is; it is a published calendar, not a model parameter. What the seed
        does NOT carry is any demand multiplier - those live only in the
        simulator, and inferring them from the emitted data is Sprint 3's job.
      - Salary week, month end and monsoon are domain knowledge about Indian
        retail and Mumbai weather, encoded as plain date arithmetic.
      - `is_ipl_window` is the season, not the fixture list. Which nights
        actually had a match is a coin flip inside the simulator, and no amount
        of joining recovers it. Naming the column for what it is beats naming
        it `is_ipl_matchday` and quietly meaning something else - and recovering
        the true matchdays from the evening demand spike is a real inference
        worth doing later, not a column to fake now.

    **The range** covers the observed data with a month of history and a
    quarter of future either side, so the forecast horizons in Sprint 3 have
    calendar rows to join to rather than dropping their own predictions.
#}

with observed as (

    select
        min(snapshot_date) as first_day,
        max(snapshot_date) as last_day
    from {{ ref('stg_catalog__products') }}

),

bounds as (

    select
        first_day - 30 as spine_start,
        last_day + 90 as spine_end
    from observed

),

spine as (

    select cast(unnest(day_series) as date) as date_day
    from (
        select generate_series(spine_start, spine_end, interval 1 day) as day_series
        from bounds
    ) as series

),

events as (

    select
        event_name,
        event_type,
        cast(start_date as date) as start_date,
        cast(start_date as date) + cast(duration_days as integer) - 1 as end_date
    from {{ ref('calendar_events') }}

),

festivals as (

    select *
    from events
    where event_type = 'festival'

),

sports_seasons as (

    select *
    from events
    where event_type = 'sports_season'

),

-- a date can in principle fall inside two festivals; the earliest-starting one
-- wins so the column is deterministic rather than dependent on scan order
festival_by_day as (

    select
        spine.date_day,
        min_by(festivals.event_name, festivals.start_date) as festival_name,
        count(*) as festivals_in_progress
    from spine
    inner join festivals
        on spine.date_day between festivals.start_date and festivals.end_date
    group by spine.date_day

),

sports_season_by_day as (

    select distinct spine.date_day
    from spine
    inner join sports_seasons
        on spine.date_day between sports_seasons.start_date and sports_seasons.end_date

)

select
    spine.date_day,

    dayname(spine.date_day) as day_name,
    isodow(spine.date_day) as day_of_week,
    day(spine.date_day) as day_of_month,
    cast(date_trunc('week', spine.date_day) as date) as week_start_date,
    cast(date_trunc('month', spine.date_day) as date) as month_start_date,
    monthname(spine.date_day) as month_name,
    quarter(spine.date_day) as quarter_of_year,
    year(spine.date_day) as calendar_year,
    isodow(spine.date_day) >= 6 as is_weekend,

    -- salaries land at the start of the month and the last week is visibly
    -- thinner; both are large, real effects in Indian retail
    day(spine.date_day) between 1 and 7 as is_salary_week,
    day(spine.date_day) >= 25 as is_month_end,

    -- Mumbai monsoon: orders rise because nobody wants to go outside, while
    -- fresh supply is disrupted. Demand up and supply down at once, which is
    -- exactly when availability management gets hard.
    month(spine.date_day) between 6 and 9 as is_monsoon,

    -- the season, not the fixture list - see the header
    sports_season_by_day.date_day is not null as is_ipl_window,

    festival_by_day.festival_name is not null as is_festival,
    festival_by_day.festival_name,
    coalesce(festival_by_day.festivals_in_progress, 0) as festivals_in_progress
from spine
left join festival_by_day on spine.date_day = festival_by_day.date_day
left join sports_season_by_day on spine.date_day = sports_season_by_day.date_day
