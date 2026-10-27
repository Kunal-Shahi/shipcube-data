-- 0001: initial catalog schema. Model tables are SCD2-versioned (since_version/until_version).
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE model_versions (
    version        INTEGER PRIMARY KEY,
    migration_id   TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    applied_at     TIMESTAMPTZ NOT NULL,
    applied_by     TEXT,
    rolled_back_at TIMESTAMPTZ
);

CREATE TABLE migrations (
    id           TEXT PRIMARY KEY,
    description  TEXT NOT NULL DEFAULT '',
    checksum     TEXT NOT NULL,
    ops          JSONB NOT NULL,              -- JSON list of ops
    touches      JSONB NOT NULL DEFAULT '[]'::jsonb, -- JSON list of touch keys
    kind         TEXT NOT NULL,              -- additive | change | breaking
    status       TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'applied', 'rolled_back', 'rejected')),
    base_version INTEGER,
    version      INTEGER,                    -- model version it produced
    proposed_by  TEXT,
    approved_by  TEXT,
    impact       JSONB,                       -- JSON dry-run report
    created_at   TIMESTAMPTZ NOT NULL,
    applied_at   TIMESTAMPTZ
);

CREATE TABLE cubes (
    name          TEXT NOT NULL,
    table_name    TEXT,
    sql           TEXT,
    title         TEXT,
    description   TEXT,
    grants        TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (name, since_version)
);

CREATE TABLE members (
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('measure', 'dimension')),
    agg           TEXT,                      -- measures
    type          TEXT,                      -- dimensions
    sql           TEXT,
    deprecated    TEXT,
    grants        TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);

CREATE TABLE member_aliases (
    cube          TEXT NOT NULL,
    alias         TEXT NOT NULL,
    target        TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, alias, since_version)
);

CREATE TABLE joins (
    cube          TEXT NOT NULL,
    target        TEXT NOT NULL,
    relationship  TEXT NOT NULL,
    on_sql        TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, target, since_version)
);

CREATE TABLE segments (
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    sql           TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);

CREATE TABLE partition_specs (
    cube          TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    granularity   TEXT,
    table_pattern TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, since_version)
);

CREATE TABLE schema_bindings (
    cube          TEXT NOT NULL,             -- '*' = every cube
    tenant        TEXT NOT NULL,             -- '*' = every tenant
    schema_name   TEXT NOT NULL,             -- may contain {tenant}
    tenant_column TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, tenant, since_version)
);

CREATE TABLE row_policies (
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    filter        JSONB NOT NULL,
    roles         TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (cube, name, since_version)
);

CREATE TABLE saved_queries (
    name       TEXT PRIMARY KEY,
    app        TEXT,
    query      JSONB NOT NULL,
    tenant     TEXT,
    roles      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

-- Warehouse connections, created in the console. Credentials are referenced (security
-- schema's secret_refs), never stored.
CREATE TABLE datasources (
    name            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,           -- duckdb | postgres | bigquery | clickhouse
    secret_ref_id   TEXT,                    -- secret_refs.id
    secret_resource TEXT,                    -- projects/<p>/secrets/<name>
    secret_version  TEXT,                    -- pinned version
    options         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL
);

-- Who holds which data role. Principals follow the security schema's conventions
-- (access_grants.principal_type): a user email, a Google group email, a domain, or a
-- workspace membership role (owner/admin/developer/viewer). Not versioned: people come
-- and go far more often than the model changes. Changes are audited.
CREATE TABLE data_role_assignments (
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'domain', 'workspace_role')),
    principal      TEXT NOT NULL,            -- lower-cased email, group, domain or role
    role           TEXT NOT NULL,            -- e.g. regional_manager, finance
    granted_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (principal_type, principal, role)
);

-- Values row policies read as $ctx.<key>, e.g. region = 'South' for one user or a whole domain.
-- The most specific principal wins: user > group > domain > workspace_role.
CREATE TABLE data_attributes (
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'domain', 'workspace_role')),
    principal      TEXT NOT NULL,
    key            TEXT NOT NULL,
    value          TEXT NOT NULL,            -- JSON scalar or list
    set_by         TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (principal_type, principal, key)
);

-- Same shape as the security schema's audit_log, so production can write to that table directly.
CREATE TABLE audit_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id   TEXT,
    actor_user_id  TEXT,
    actor_token_id TEXT,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('user', 'token', 'system')),
    action         TEXT NOT NULL,            -- data.migration.apply, data.role.assign, ...
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    at             TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX cubes_current ON cubes (name) WHERE until_version IS NULL;
CREATE UNIQUE INDEX members_current ON members (cube, name) WHERE until_version IS NULL;
CREATE UNIQUE INDEX member_aliases_current ON member_aliases (cube, alias) WHERE until_version IS NULL;
CREATE UNIQUE INDEX joins_current ON joins (cube, target) WHERE until_version IS NULL;
CREATE UNIQUE INDEX segments_current ON segments (cube, name) WHERE until_version IS NULL;
CREATE UNIQUE INDEX partition_specs_current ON partition_specs (cube) WHERE until_version IS NULL;
CREATE UNIQUE INDEX schema_bindings_current ON schema_bindings (cube, tenant) WHERE until_version IS NULL;
CREATE UNIQUE INDEX row_policies_current ON row_policies (cube, name) WHERE until_version IS NULL;
