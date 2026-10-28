-- The ledger of applied catalog-schema migrations is written only by the migration role. 0001's grant
-- loop gave the service roles DML on every table in `catalog`, the ledger included; a compromised API
-- login could then forge or delete rows and make a missing migration look applied. They keep SELECT,
-- which the API's startup check (pending migrations) needs.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['shipcube_app', 'shipcube_worker'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON catalog.catalog_schema_migrations FROM %I', r);
    END IF;
  END LOOP;
END $$;
