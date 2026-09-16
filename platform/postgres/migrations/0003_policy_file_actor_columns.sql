-- Extends the control-plane actor trail (issue #160) to policy_file, the
-- compose entry-file store added by #179.
--
-- Separate from 0002 because the table did not exist when 0002 was written, and
-- a stack that deployed #179 already has policy_file without these columns. A
-- brand-new table needs no migration; one that shipped a release ago does.
--
-- Same conventions as 0002: no REFERENCES clause, NULL means "no human actor",
-- idempotent. Apply BEFORE deploying the code that reads the columns.
--
-- GUARDED, and this is the file that proved why. On prod -- a stack predating
-- #179 -- the bare ALTER below failed with `relation "policy_file" does not
-- exist`, and because platform-migrate stopped there, every ClickHouse
-- migration was skipped with it. `ADD COLUMN IF NOT EXISTS` guards the COLUMN,
-- not the table. Every migration guards each table it touches; see
-- 0002_actor_columns.sql for the convention in full.

-- policy_file --------------------------------------------------------------
-- updated_at already exists, and NULL is the correct actor for every row
-- written before this, so no backfill.
DO $$
BEGIN
  IF to_regclass('policy_file') IS NULL THEN
    RAISE NOTICE 'policy_file absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
  ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;
END $$;
