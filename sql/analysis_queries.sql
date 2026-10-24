-- Queries used by analysis.py. Each block starts with "-- name: <key>".
-- They can also be run one by one in psql to explore the data.

-- name: completeness
-- Did the database capture every pack the checkweigher counted?
SELECT equipment_pack_count, rows_collected, equipment_reject_count, rejects_collected,
       equipment_pack_count - rows_collected AS missing_packs
FROM collection_runs ORDER BY run_id DESC LIMIT 1;

-- name: subgroups
-- SPC subgroups: the first 5 packs of every running minute.
WITH ranked AS (
    SELECT ts, weight_g, date_trunc('minute', ts) AS minute,
           row_number() OVER (PARTITION BY date_trunc('minute', ts) ORDER BY ts) AS rn
    FROM packs
)
SELECT minute,
       avg(weight_g)::float                AS xbar,
       (max(weight_g) - min(weight_g))::float AS r
FROM ranked
WHERE rn <= %(n)s
GROUP BY minute
HAVING count(*) = %(n)s
ORDER BY minute;

-- name: per_minute
-- Packs, rejects and average weight for every minute the line produced.
SELECT date_trunc('minute', ts) AS minute,
       count(*)                                  AS packs,
       count(*) FILTER (WHERE rejected)          AS rejects,
       avg(weight_g)::float                      AS avg_weight
FROM packs GROUP BY 1 ORDER BY 1;

-- name: stops
-- Every stop with its duration, from consecutive state changes.
WITH ev AS (
    SELECT ts, state, reason, planned, lead(ts) OVER (ORDER BY ts) AS next_ts
    FROM line_events
)
SELECT ts AS start_ts, next_ts AS end_ts, reason, planned,
       extract(epoch FROM next_ts - ts)::float / 60 AS minutes
FROM ev
WHERE state = 'STOPPED' AND next_ts IS NOT NULL
ORDER BY ts;

-- name: shift_window
SELECT min(ts) FILTER (WHERE state = 'RUNNING') AS shift_start,
       max(ts) FILTER (WHERE state = 'SHIFT_END') AS shift_end
FROM line_events;

-- name: totals
-- Output, rejects and giveaway (weight above the 500 g label on packs shipped).
SELECT count(*)                                       AS total_packs,
       count(*) FILTER (WHERE rejected)               AS rejects,
       avg(weight_g)::float                           AS mean_weight,
       stddev_samp(weight_g)::float                   AS sd_weight,
       sum(weight_g - %(nominal)s) FILTER (WHERE NOT rejected)::float / 1000 AS giveaway_kg
FROM packs;

-- name: manual_vs_checkweigher
-- Join QA's manual reference-scale readings to the same packs on the checkweigher.
SELECT m.ts, m.manual_weight_g::float AS manual_g, p.weight_g::float AS checkweigher_g,
       (p.weight_g - m.manual_weight_g)::float AS diff_g
FROM manual_checks m
JOIN packs p ON p.ts = m.ts
ORDER BY m.ts;

-- name: manual_unmatched
SELECT count(*) AS unmatched FROM manual_checks m
WHERE NOT EXISTS (SELECT 1 FROM packs p WHERE p.ts = m.ts);
