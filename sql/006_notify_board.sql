-- 006_notify_board.sql — a second notification switch: Board steps.
-- notify_flags  = flag activity (new flags, comments, proposals, decisions, closes, reopens)
-- notify_board  = Board steps (a flag sent to the Board, or recalled from it)
-- Board members can keep notify_board on and notify_flags off and hear only about their queue.
ALTER TABLE admin_users
  ADD COLUMN IF NOT EXISTS notify_board boolean NOT NULL DEFAULT true;
