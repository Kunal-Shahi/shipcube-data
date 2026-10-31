-- 0005: pre-aggregations. `rollups` is part of the versioned model (SCD2, like segments);
-- `rollup_partitions` records which months of a rollup are built in the warehouse, per tenant and
-- model fingerprint. It's build state, not model, so it isn't versioned.

CREATE TABLE rollups (
    workspace_id  TEXT,
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);
CREATE UNIQUE INDEX rollups_current ON rollups (cube, name) WHERE until_version IS NULL;

CREATE TABLE rollup_partitions (
    workspace_id TEXT NOT NULL,
    tenant       TEXT NOT NULL,
    cube         TEXT NOT NULL,
    rollup       TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    period       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    row_count    INTEGER,
    built_at     TEXT NOT NULL,
    PRIMARY KEY (workspace_id, tenant, cube, rollup, fingerprint, period)
);
