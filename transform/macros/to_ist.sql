{#
    Convert a naive UTC timestamp to Indian Standard Time (defect 6).

    The clickstream collector writes UTC while the POS writes IST, and nothing
    in either feed says so. Joining the two on date misattributes every event
    before 05:30 IST to the previous day and moves the evening demand peak into
    the afternoon - the single most expensive silent bug in this dataset,
    because it corrupts the hour-of-day demand curve the forecast is built on.

    Goes through the tz database rather than adding a fixed `interval 5 hours
    30 minutes`. India has never observed DST so the two agree today, and the
    offset version would be faster. Naming the zone is still the right call: it
    states which zone the data is *in* rather than how far to shift it, and it
    is the version that survives this model being pointed at a second market.

    Every output column derived from this is suffixed `_ist`, because a bare
    `event_ts` is what caused the problem in the first place.
#}

{% macro to_ist(column) -%}
    timezone('Asia/Kolkata', timezone('UTC', {{ column }}))
{%- endmacro %}
