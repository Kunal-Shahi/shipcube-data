-- 0003: pre-aggregations (see the sqlite/postgres 0005). `rollups` is versioned model; `rollup_partitions`
-- is build state: which months of a rollup are built in the warehouse, per tenant and model fingerprint.
-- Both under FORCE ROW LEVEL SECURITY like every catalog table; 0001's default privileges give the
-- service roles access to tables the owner creates here.

CREATE TABLE rollups (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, name, since_version)
);
CREATE UNIQUE INDEX rollups_current ON rollups (workspace_id, cube, name) WHERE until_version IS NULL;

CREATE TABLE rollup_partitions (
    workspace_id UUID NOT NULL DEFAULT app_workspace(),
    tenant       TEXT NOT NULL,
    cube         TEXT NOT NULL,
    rollup       TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    period       TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    row_count    BIGINT,
    built_at     TEXT NOT NULL,
    PRIMARY KEY (workspace_id, tenant, cube, rollup, fingerprint, period)
);

ALTER TABLE rollups ENABLE ROW LEVEL SECURITY;
ALTER TABLE rollups FORCE ROW LEVEL SECURITY;
CREATE POLICY rollups_workspace ON rollups
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE rollup_partitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE rollup_partitions FORCE ROW LEVEL SECURITY;
CREATE POLICY rollup_partitions_workspace ON rollup_partitions
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
