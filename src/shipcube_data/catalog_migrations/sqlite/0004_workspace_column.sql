-- 0004: every table carries workspace_id, like the shared layout (postgres_shared), so one set of
-- workspace-scoped statements serves every layout. A standalone catalog holds one workspace: the
-- value comes from its settings row (or the nil UUID for a catalog without a workspace).

ALTER TABLE settings ADD COLUMN workspace_id TEXT;
ALTER TABLE model_versions ADD COLUMN workspace_id TEXT;
ALTER TABLE migrations ADD COLUMN workspace_id TEXT;
ALTER TABLE cubes ADD COLUMN workspace_id TEXT;
ALTER TABLE members ADD COLUMN workspace_id TEXT;
ALTER TABLE member_aliases ADD COLUMN workspace_id TEXT;
ALTER TABLE joins ADD COLUMN workspace_id TEXT;
ALTER TABLE segments ADD COLUMN workspace_id TEXT;
ALTER TABLE partition_specs ADD COLUMN workspace_id TEXT;
ALTER TABLE schema_bindings ADD COLUMN workspace_id TEXT;
ALTER TABLE row_policies ADD COLUMN workspace_id TEXT;
ALTER TABLE saved_queries ADD COLUMN workspace_id TEXT;
ALTER TABLE datasources ADD COLUMN workspace_id TEXT;
ALTER TABLE data_role_assignments ADD COLUMN workspace_id TEXT;
ALTER TABLE data_attributes ADD COLUMN workspace_id TEXT;
UPDATE settings SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE model_versions SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE migrations SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE cubes SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE members SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE member_aliases SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE joins SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE segments SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE partition_specs SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE schema_bindings SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE row_policies SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE saved_queries SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE datasources SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE data_role_assignments SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE data_attributes SET workspace_id = COALESCE((SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
UPDATE audit_log SET workspace_id = COALESCE(workspace_id, (SELECT value FROM settings WHERE key = 'workspace_id'), '00000000-0000-0000-0000-000000000000');
