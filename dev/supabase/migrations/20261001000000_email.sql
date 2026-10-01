-- 005_email.sql — email on accounts, password reset by email, notification log.
-- Run in the Supabase SQL editor (prod) after the email-notifications branch merges.
-- Applied in the DEV stack via dev/supabase/migrations/20261001000000_email.sql.

ALTER TABLE admin_users
  ADD COLUMN IF NOT EXISTS email         text,
  ADD COLUMN IF NOT EXISTS notify_flags  boolean NOT NULL DEFAULT true;   -- flag activity emails on/off

CREATE UNIQUE INDEX IF NOT EXISTS admin_users_email_key
  ON admin_users (lower(email)) WHERE email IS NOT NULL;

-- One-time password reset links. Only the SHA-256 of the token is stored.
CREATE TABLE IF NOT EXISTS password_resets (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
  token_hash   text NOT NULL UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz,
  requested_ip text
);
CREATE INDEX IF NOT EXISTS password_resets_user_idx ON password_resets (user_id);

-- Every email the console sends (or skips), for the audit trail.
CREATE TABLE IF NOT EXISTS email_log (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at  timestamptz NOT NULL DEFAULT now(),
  to_address  text NOT NULL,
  subject     text NOT NULL,
  kind        text,            -- password_reset | flag_created | flag_comment | ...
  related_id  text,            -- flag id / user id
  status      text NOT NULL,   -- sent | failed | skipped
  error       text
);

ALTER TABLE password_resets ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_log       ENABLE ROW LEVEL SECURITY;
