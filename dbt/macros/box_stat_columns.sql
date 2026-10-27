{# The per-player stat columns shared by ESPN box scores and the play-by-play rebuild. #}
{% macro box_stat_columns() %}
    {{ return(['minutes', 'pts', 'fgm', 'fga', 'fg3m', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb',
               'reb', 'ast', 'stl', 'blk', 'tov', 'pf', 'plus_minus']) }}
{% endmacro %}
