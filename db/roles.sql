-- db/roles.sql
-- Run once per PostgreSQL cluster as a superuser, before schema.sql.
-- One NOLOGIN role per engine. The application logs in as orch_app, which
-- inherits nothing: every engine transaction starts with SET LOCAL ROLE eN_role,
-- so the database itself refuses a write to another engine's table.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e1_role') THEN CREATE ROLE e1_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e2_role') THEN CREATE ROLE e2_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e3_role') THEN CREATE ROLE e3_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'e4_role') THEN CREATE ROLE e4_role NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orch_app') THEN
        CREATE ROLE orch_app LOGIN NOINHERIT PASSWORD 'orch';   -- change on the demo machine
    END IF;
END $$;
GRANT e1_role, e2_role, e3_role, e4_role TO orch_app;
