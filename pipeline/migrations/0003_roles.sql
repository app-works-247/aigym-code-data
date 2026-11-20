-- 0003_roles.sql: the two non-owner roles and their rights (docs/database.md section 4).
-- Roles are created without LOGIN and without a password, so nothing secret lives in this
-- public repo. Passwords are set once per database by the owner, outside git:
--   ALTER ROLE pipeline_rw LOGIN PASSWORD '...';  (see docs/database.md section 4)

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_rw') THEN
    CREATE ROLE pipeline_rw NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'readonly') THEN
    CREATE ROLE readonly NOLOGIN;
  END IF;
END
$$;

-- Nobody but the owner may create objects in the schema.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pipeline_rw, readonly;

-- pipeline_rw: read and write rows, never change the structure.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pipeline_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pipeline_rw;  -- pipeline_runs.id
-- The migration log belongs to the owner alone.
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM pipeline_rw;

-- readonly: look, never touch.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly;

-- Tables that later migrations create get the same rights automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pipeline_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO pipeline_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly;
