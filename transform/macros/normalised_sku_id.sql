{#
    Conform both generations of SKU identifier onto the catalogue's format
    (defect 7).

    A catalogue migration on 2026-03-01 changed the clickstream's identifier
    from the zero-padded `SKU-00042` to the bare `SKU_42`. Nothing downstream
    was told. An inner join to the product dimension then silently drops every
    event after that date - 1.69M of them, over 40% of the year's browsing -
    and the loss is invisible because a join that returns fewer rows still
    returns rows.

    Mapping the new format back onto the old one rather than the reverse keeps
    the catalogue as the system of record, so `dim_product` never has to know
    the migration happened. Anything matching neither format is passed through
    untouched and left to fail the relationships test loudly, which is the
    whole point: a conform step that quietly coalesces unknown codes to null
    reintroduces the silent loss it was written to remove.
#}

{% macro normalised_sku_id(column) -%}
    case
        when regexp_matches({{ column }}, '^SKU_[0-9]+$')
            then 'SKU-' || lpad(regexp_extract({{ column }}, '^SKU_([0-9]+)$', 1), 5, '0')
        else {{ column }}
    end
{%- endmacro %}
