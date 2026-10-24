-- Tables the collector writes into. Timestamps are the equipment's own source timestamps.

DROP TABLE IF EXISTS packs, line_events, manual_checks, collection_runs;

-- One row per pack weighed by the checkweigher
CREATE TABLE packs (
    id          bigserial PRIMARY KEY,
    ts          timestamp    NOT NULL UNIQUE,   -- when the pack crossed the checkweigher
    weight_g    numeric(6,1) NOT NULL,          -- measured weight
    rejected    boolean      NOT NULL,          -- below the lower reject limit
    received_at timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX packs_minute_idx ON packs (date_trunc('minute', ts));

-- Every change of line state (running / stopped and why)
CREATE TABLE line_events (
    id       bigserial PRIMARY KEY,
    ts       timestamp NOT NULL,
    state    text      NOT NULL,     -- RUNNING | STOPPED | SHIFT_END
    reason   text,
    planned  boolean   NOT NULL DEFAULT false
);

-- Manual weight checks done by QA on a reference scale (today: paper log, typed in)
CREATE TABLE manual_checks (
    ts              timestamp    PRIMARY KEY,   -- pack time printed on the pack label
    manual_weight_g numeric(6,1) NOT NULL,
    technician      text
);

-- Bookkeeping: did we capture every pack the equipment says it made?
CREATE TABLE collection_runs (
    run_id              bigserial PRIMARY KEY,
    started_at          timestamptz NOT NULL,
    finished_at         timestamptz,
    equipment_pack_count   integer,
    equipment_reject_count integer,
    rows_collected      integer,
    rejects_collected   integer
);
