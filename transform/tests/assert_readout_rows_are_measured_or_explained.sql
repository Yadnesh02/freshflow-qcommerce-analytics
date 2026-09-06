{#
    Gate G5 says every published number is traceable to mart_experiment_readout.
    This asserts the property that makes the table safe to publish from: a row
    either carries a measurement or carries the reason it cannot.

    **The third state is the dangerous one.** A row with nulls and no reason
    renders as a blank cell, and a blank cell on an executive page reads as
    "no effect" rather than "this design cannot answer that". Two of the plan's
    six metrics are in exactly that position - 90-day retention is
    customer-level while the holdout randomises stores, and forecast WAPE has
    no Policy A column because Policy A does not forecast - and both are
    supposed to arrive with the reason attached.

    The inverse is asserted too: a row flagged `is_measured` with a null delta
    would be a metric that silently stopped being computed while the table kept
    its shape, which is the failure the readout's own docstring warns about.

    Written as a singular test rather than a generic one because the project
    carries dbt-expectations and not dbt-utils, and `expression_is_true` is not
    worth a second package - packages.yml says a dependency is a cost.
#}

select
    metric,
    is_measured,
    policy_a,
    delta,
    not_applicable_reason
from {{ ref('mart_experiment_readout') }}
where
    -- measured, but missing the numbers that make it a measurement
    (is_measured and (policy_a is null or policy_b is null or delta is null))
    -- not measured, and not saying why
    or (not is_measured and coalesce(trim(not_applicable_reason), '') = '')
