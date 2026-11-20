-- The policy assistant's own database and login, kept apart from the main
-- `hexgate` database. Run as the `hexgate` superuser on every deploy of a
-- stack (the `assistant-db-init` service in docker-compose.deploy.yml, and
-- `make assistant-db-init` locally), so every statement is safe to repeat.
--
-- Not an init script: the deployed stages already have a Postgres volume, and
-- /docker-entrypoint-initdb.d runs only on an empty one, so the database would
-- never be created there.
--
-- The password comes from the environment (psql's \getenv), not from the
-- command line, where `ps` would show it. Run with `-v ON_ERROR_STOP=1`.

\getenv assistant_password HEXGATE_ASSISTANT_POSTGRES_PASSWORD
\if :{?assistant_password}
  SELECT :'assistant_password' <> '' AS has_password \gset
\else
  \set has_password false
\endif
-- Postgres would store an empty password as none, and the login would fail
-- later with a less useful error. With ON_ERROR_STOP this exits nonzero.
\if :has_password
\else
  DO $$ BEGIN RAISE EXCEPTION 'HEXGATE_ASSISTANT_POSTGRES_PASSWORD is not set'; END $$;
\endif

-- The login: created once, its password set on every run, so a new value in
-- the secret takes effect on the next start. No superuser, no CREATEDB, no
-- CREATEROLE (the CREATE ROLE defaults).
SELECT 'CREATE ROLE hexgate_assistant LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hexgate_assistant')
\gexec
ALTER ROLE hexgate_assistant WITH LOGIN PASSWORD :'assistant_password';

-- CREATE DATABASE cannot run in a transaction or a DO block, hence \gexec.
-- Owned by the login, which then creates its own tables at startup.
SELECT 'CREATE DATABASE hexgate_assistant OWNER hexgate_assistant'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hexgate_assistant')
\gexec

-- PUBLIC may connect to every database by default; after this, the
-- assistant's login cannot open `hexgate`, nor another login the assistant's.
-- The owners keep CONNECT (`hexgate` is also a superuser).
REVOKE CONNECT ON DATABASE hexgate FROM PUBLIC;
REVOKE CONNECT ON DATABASE hexgate_assistant FROM PUBLIC;
