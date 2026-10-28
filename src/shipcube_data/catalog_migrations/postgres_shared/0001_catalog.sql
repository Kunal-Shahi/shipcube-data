-- shipcube-data catalog in ShipCube's own database (security's design, Oct 29):
-- one schema `catalog`, every table keyed by workspace_id with FORCE ROW LEVEL SECURITY on
-- workspace_id = app_workspace(), the same mechanism as the API's tenant tables. Requests run in the
-- API's db.tx(caller) as shipcube_app; this DDL runs as the migration role (sc-migrator, sessions
-- start as shipcube_owner), after the API's own migrations (it needs app_workspace()).
-- A missing app.workspace_id makes app_workspace() NULL, so every query returns zero rows: fail closed.
-- shipcube_app/shipcube_worker get DML on these tables from the API's owner_defaults.sql.

CREATE TABLE settings (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    key TEXT,
    value TEXT NOT NULL,
    PRIMARY KEY (workspace_id, key)
);

CREATE TABLE model_versions (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    version INTEGER,
    migration_id   TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,
    applied_at     TIMESTAMPTZ NOT NULL,
    applied_by     TEXT,
    rolled_back_at TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, version)
);

CREATE TABLE migrations (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    id TEXT,
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
    applied_at   TIMESTAMPTZ,
    proposed_by_user TEXT,
    approved_by_user TEXT,
    drafted_by   TEXT,
    decision_note TEXT,
    decided_at   TIMESTAMPTZ,
    diff         JSONB,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE cubes (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    name          TEXT NOT NULL,
    table_name    TEXT,
    sql           TEXT,
    title         TEXT,
    description   TEXT,
    grants        TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, name, since_version)
);

CREATE TABLE members (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
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
    PRIMARY KEY (workspace_id, cube, name, since_version)
);

CREATE TABLE member_aliases (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,
    alias         TEXT NOT NULL,
    target        TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, alias, since_version)
);

CREATE TABLE joins (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,
    target        TEXT NOT NULL,
    relationship  TEXT NOT NULL,
    on_sql        TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, target, since_version)
);

CREATE TABLE segments (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    sql           TEXT NOT NULL,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, name, since_version)
);

CREATE TABLE partition_specs (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,
    column_name   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    granularity   TEXT,
    table_pattern TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, since_version)
);

CREATE TABLE schema_bindings (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,             -- '*' = every cube
    tenant        TEXT NOT NULL,             -- '*' = every tenant
    schema_name   TEXT NOT NULL,             -- may contain {tenant}
    tenant_column TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, tenant, since_version)
);

CREATE TABLE row_policies (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    cube          TEXT NOT NULL,
    name          TEXT NOT NULL,
    filter        JSONB NOT NULL,
    roles         TEXT,
    spec          JSONB NOT NULL,
    since_version INTEGER NOT NULL,
    until_version INTEGER,
    PRIMARY KEY (workspace_id, cube, name, since_version)
);

CREATE TABLE saved_queries (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    name TEXT,
    app        TEXT,
    query      JSONB NOT NULL,
    tenant     TEXT,
    roles      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, name)
);

CREATE TABLE datasources (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    name TEXT,
    kind            TEXT NOT NULL,           -- duckdb | postgres | bigquery | clickhouse
    secret_ref_id   TEXT,                    -- secret_refs.id
    secret_resource TEXT,                    -- projects/<p>/secrets/<name>
    secret_version  TEXT,                    -- pinned version
    options         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, name)
);

CREATE TABLE data_role_assignments (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'domain', 'workspace_role')),
    principal      TEXT NOT NULL,            -- lower-cased email, group, domain or role
    role           TEXT NOT NULL,            -- e.g. regional_manager, finance
    granted_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, principal_type, principal, role)
);

CREATE TABLE data_attributes (
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'domain', 'workspace_role')),
    principal      TEXT NOT NULL,
    key            TEXT NOT NULL,
    value          TEXT NOT NULL,            -- JSON scalar or list
    set_by         TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, principal_type, principal, key)
);

CREATE TABLE audit_log (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id  UUID NOT NULL DEFAULT app_workspace(),
    actor_user_id  TEXT,
    actor_token_id TEXT,
    actor_type     TEXT NOT NULL CHECK (actor_type IN ('user', 'token', 'system')),
    action         TEXT NOT NULL,            -- data.migration.apply, data.role.assign, ...
    target_type    TEXT NOT NULL,
    target_id      TEXT NOT NULL,
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    at             TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX cubes_current ON cubes (workspace_id, name) WHERE until_version IS NULL;
CREATE UNIQUE INDEX members_current ON members (workspace_id, cube, name) WHERE until_version IS NULL;
CREATE UNIQUE INDEX member_aliases_current ON member_aliases (workspace_id, cube, alias) WHERE until_version IS NULL;
CREATE UNIQUE INDEX joins_current ON joins (workspace_id, cube, target) WHERE until_version IS NULL;
CREATE UNIQUE INDEX segments_current ON segments (workspace_id, cube, name) WHERE until_version IS NULL;
CREATE UNIQUE INDEX partition_specs_current ON partition_specs (workspace_id, cube) WHERE until_version IS NULL;
CREATE UNIQUE INDEX schema_bindings_current ON schema_bindings (workspace_id, cube, tenant) WHERE until_version IS NULL;
CREATE UNIQUE INDEX row_policies_current ON row_policies (workspace_id, cube, name) WHERE until_version IS NULL;

ALTER TABLE settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE settings FORCE ROW LEVEL SECURITY;
CREATE POLICY settings_workspace ON settings
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE model_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY model_versions_workspace ON model_versions
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY migrations_workspace ON migrations
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE cubes ENABLE ROW LEVEL SECURITY;
ALTER TABLE cubes FORCE ROW LEVEL SECURITY;
CREATE POLICY cubes_workspace ON cubes
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE members ENABLE ROW LEVEL SECURITY;
ALTER TABLE members FORCE ROW LEVEL SECURITY;
CREATE POLICY members_workspace ON members
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE member_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE member_aliases FORCE ROW LEVEL SECURITY;
CREATE POLICY member_aliases_workspace ON member_aliases
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE joins ENABLE ROW LEVEL SECURITY;
ALTER TABLE joins FORCE ROW LEVEL SECURITY;
CREATE POLICY joins_workspace ON joins
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE segments FORCE ROW LEVEL SECURITY;
CREATE POLICY segments_workspace ON segments
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE partition_specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE partition_specs FORCE ROW LEVEL SECURITY;
CREATE POLICY partition_specs_workspace ON partition_specs
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE schema_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY schema_bindings_workspace ON schema_bindings
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE row_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE row_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY row_policies_workspace ON row_policies
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE saved_queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE saved_queries FORCE ROW LEVEL SECURITY;
CREATE POLICY saved_queries_workspace ON saved_queries
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE datasources ENABLE ROW LEVEL SECURITY;
ALTER TABLE datasources FORCE ROW LEVEL SECURITY;
CREATE POLICY datasources_workspace ON datasources
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE data_role_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_role_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY data_role_assignments_workspace ON data_role_assignments
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE data_attributes ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_attributes FORCE ROW LEVEL SECURITY;
CREATE POLICY data_attributes_workspace ON data_attributes
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_log_workspace ON audit_log
    USING (workspace_id = app_workspace()) WITH CHECK (workspace_id = app_workspace());

-- The API's owner_defaults.sql only covers schema public. Give ShipCube's service roles the same access
-- to `catalog`, now and for tables later migrations add. Guarded, so other databases (tests, standalone)
-- without these roles skip it.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['shipcube_app', 'shipcube_worker'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT USAGE ON SCHEMA catalog TO %I', r);
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA catalog TO %I', r);
      EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA catalog TO %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA catalog GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA catalog GRANT USAGE, SELECT ON SEQUENCES TO %I', r);
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shipcube_readonly') THEN
    GRANT USAGE ON SCHEMA catalog TO shipcube_readonly;
    GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO shipcube_readonly;
    ALTER DEFAULT PRIVILEGES IN SCHEMA catalog GRANT SELECT ON TABLES TO shipcube_readonly;
  END IF;
END $$;
