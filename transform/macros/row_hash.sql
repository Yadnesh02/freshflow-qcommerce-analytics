{#
    A fingerprint of a row's business content, used to remove the duplicate
    events documented as defect 1 in docs/known_data_issues.md.

    Two decisions here are load-bearing.

    **The caller passes the column list, and it excludes the partition column.**
    A retried webhook delivers a byte-identical row, so hashing the business
    columns identifies it. But `dt` is not a business column - it records which
    partition the row landed in - and the late-arrival pass (defect 2) can move
    one copy of a duplicated pair into a later partition. Hash `dt` along with
    everything else and those pairs stop matching: on the 365-day run that
    leaves 179 orders surviving deduplication twice, which is exactly the kind
    of near-miss that reconciles to "almost".

    **Nulls and separators are explicit.** concat_ws() drops nulls, so
    ('a', null, 'b') and ('a', 'b', null) would collide. Every value is
    coalesced to a sentinel and joined with the ASCII unit separator, chr(31) -
    a character no price, id or product name in this catalogue contains, so the
    join is unambiguous in a way '|' would not be.
#}

{#
    Built by joining the parts in Jinja rather than emitting them across
    several lines. The multi-line form rendered SQL whose indentation sqlfluff
    could not follow, and since CI lints macros as well as models, that failed
    every build for three commits without anything local noticing.
#}
{%- macro row_hash(columns) -%}
    {%- set parts = [] -%}
    {%- for column in columns -%}
        {%- do parts.append("coalesce(cast(" ~ column ~ " as varchar), '\\N')") -%}
    {%- endfor -%}
    md5({{ parts | join(" || chr(31) || ") }})
{%- endmacro -%}
