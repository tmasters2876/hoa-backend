-- Priority Phase item 2 (Roles Rebuild): three-tier role column.
-- Run in the Supabase SQL editor BEFORE deploying the roles code —
-- and VERIFY with the final SELECT, or you risk locking superusers out.
-- Additive only: is_approver is kept (ignored by new code) as a rollback aid;
-- drop it in a later cleanup once the rebuild has been live and stable.

ALTER TABLE admin_users
  ADD COLUMN role text NOT NULL DEFAULT 'member'
  CHECK (role IN ('superuser', 'board', 'member'));

-- Backfill: hardcoded SUPERUSERS set -> superuser; approvers -> board (1:1, owner confirmed).
UPDATE admin_users SET role = 'superuser'
  WHERE username IN ('tmasters', 'cmasters', 'admin');

UPDATE admin_users SET role = 'board'
  WHERE is_approver = true AND role = 'member';

-- VERIFY BEFORE DEPLOYING CODE: every expected superuser must show role='superuser'.
SELECT username, role, is_approver, is_active
FROM admin_users
ORDER BY role, username;
