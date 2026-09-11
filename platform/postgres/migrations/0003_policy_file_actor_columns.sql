-- Extends the control-plane actor trail (issue #160) to policy_file, the
-- compose entry-file store added by #179.
--
-- Separate from 0002 because the table did not exist when 0002 was written, and
-- a stack that deployed #179 already has policy_file without these columns. A
-- brand-new table needs no migration; one that shipped a release ago does.
--
-- Same conventions as 0002: no REFERENCES clause, NULL means "no human actor",
-- idempotent. Apply BEFORE deploying the code that reads the columns.

-- policy_file --------------------------------------------------------------
-- updated_at already exists, and NULL is the correct actor for every row
-- written before this, so no backfill.
ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS created_by_user_id varchar;
ALTER TABLE policy_file ADD COLUMN IF NOT EXISTS updated_by_user_id varchar;
