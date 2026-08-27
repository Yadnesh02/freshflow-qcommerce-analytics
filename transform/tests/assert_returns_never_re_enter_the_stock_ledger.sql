{#
    A documented gap, pinned so it cannot widen unnoticed.

    The POS posts 10,566 returned units. The WMS movement ledger has four event
    types - inbound, opening_balance, sale and expiry_writeoff - and none of
    them puts a unit back. So returned stock leaves the POS's books and never
    arrives on the WMS's: `closing_units` in agg_store_sku_day does not include
    it, and net sales will always sit below units removed from stock by exactly
    the returned quantity.

    For perishables that is arguably right - a returned punnet of berries is
    binned, not reshelved - but the ledger records no write-off for it either,
    so the units are not disposed of so much as unaccounted for. Either way it
    is a property of the source feeds, not something staging can repair, and
    the reconciliation test above has to know about it or it would read the
    returned quantity as a balance failure.

    This test fails if a restocking event type ever appears, which would mean
    the assumption above quietly stopped holding and the reconciliation needs
    rewriting rather than adjusting.
#}

select
    event_type,
    count(*) as movements,
    sum(qty_delta) as net_units
from {{ ref('fct_inventory_movement') }}
where event_type not in ('inbound', 'opening_balance', 'sale', 'expiry_writeoff')
group by event_type
