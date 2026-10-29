-- Links each refresh token to the authorization code its login started from,
-- and marks a code presented again after it was spent, so a second redemption
-- of a spent code revokes the tokens that code issued, and refuses the ones it
-- has not finished issuing (RFC 6749 section 4.1.2).
--
-- Both tables shipped in #327, so deployed stacks already have them without
-- these columns. NULL is correct for every row written before:
-- those logins predate the link, and a replayed code for one of them only
-- fails, as it did. Plain varchar with no REFERENCES clause, as the model
-- declares it, so a code row can be deleted while the refresh rows live on
-- for weeks.
--
-- Same conventions as 0002: guarded per table, idempotent. Apply BEFORE
-- deploying the code that reads them: without them every consent and code
-- exchange fails with UndefinedColumn.
DO $$
BEGIN
  IF to_regclass('oauth_refresh_token') IS NULL THEN
    RAISE NOTICE 'oauth_refresh_token absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  ALTER TABLE oauth_refresh_token ADD COLUMN IF NOT EXISTS authorization_code_id varchar;
  CREATE INDEX IF NOT EXISTS ix_oauth_refresh_token_authorization_code_id
    ON oauth_refresh_token (authorization_code_id);
END $$;

DO $$
BEGIN
  IF to_regclass('oauth_authorization_code') IS NULL THEN
    RAISE NOTICE 'oauth_authorization_code absent -- create_all builds it complete; skipping';
    RETURN;
  END IF;
  ALTER TABLE oauth_authorization_code ADD COLUMN IF NOT EXISTS replayed_at timestamptz;
END $$;
