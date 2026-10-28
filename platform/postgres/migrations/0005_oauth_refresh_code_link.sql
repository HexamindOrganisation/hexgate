-- Links each refresh token to the authorization code its login started from,
-- so a second redemption of a spent code revokes the tokens that code issued
-- (RFC 6749 section 4.1.2).
--
-- oauth_refresh_token shipped in #327, so deployed stacks already have the
-- table without this column. NULL is correct for every row written before:
-- those logins predate the link, and a replayed code for one of them only
-- fails, as it did. Plain varchar with no REFERENCES clause, as the model
-- declares it, so a code row can be deleted while the refresh rows live on
-- for weeks.
--
-- Same conventions as 0002: guarded per table, idempotent. Apply BEFORE
-- deploying the code that writes the column: without it every code exchange
-- fails with UndefinedColumn.
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
