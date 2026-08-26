{#
    Suppliers and their service profile.

    `inbound_freshness_pct` is a list, not a scalar: it is the distribution of
    remaining shelf life on arrival, and the mean alone hides the finding that
    matters - SUP-DAIRY-B lands 33% less usable life than SUP-DAIRY-A at a
    lower unit cost. Keeping the list intact is what lets the supplier
    scorecard compare distributions rather than averages.
#}

select
    supplier_id,
    supplier_name,
    categories,
    lead_time_mean_days,
    lead_time_sd_days,
    otif_rate,
    monsoon_otif_penalty,
    inbound_freshness_pct,
    cost_index,
    private_label_only,
    dt as arrival_date
from {{ source('ref', 'ref_suppliers') }}
