{#
    dbt's default prefixes custom schemas with the target schema, producing
    `main_staging`, `main_marts` and so on. Overriding it gives clean schema
    names, which matters here because the metrics API addresses tables by name
    and those names end up in the SQL echoed back to the dashboard.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
