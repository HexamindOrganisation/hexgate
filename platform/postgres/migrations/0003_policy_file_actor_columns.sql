-- Extends the control-plane actor trail (issue #160) to policy_file, the
-- compose entry-file store added by #179.
--
-- Separate from 0002 because the table did not exist when 0002 was written:
-- #179 landed on main in parallel, and a stack that deployed it already has
-- policy_file created by create_all WITHOUT these columns. A brand-new table
-- needs no migration; one that shipped a release ago does.
--
-- Same conventions as 0002: no REFERENCES "user"(id) (create_all emits them on
-- a fresh database, but adding constraints to a live table is not worth the
-- lock), NULL means "no human actor", idempotent via IF NOT EXISTS.
--
-- ORDERING: apply BEFORE deploying the code that references the columns. The
-- old API never selects them; the new one 500s on every /policy-files read if
-- they are missing.

-- policy_file --------------------------------------------------------------
-- updated_at already exists (models.py:PolicyFile), so no backfill is needed:
-- only the two actor columns were missing, and NULL is their correct value for
-- every row written before this.
ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;
