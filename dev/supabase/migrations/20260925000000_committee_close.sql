-- Mirror of sql/004_committee_close.sql for the local DEV stack.
-- Committee close (Sept 2026, owner direction).
-- Run in the Supabase SQL editor BEFORE deploying the code that uses it.
-- Additive only.
--
-- The committee can end a flag itself — "Closed by committee, no change
-- needed" — without sending it to the Board. Any member may close (reason
-- required, recorded in the thread) and any member may reopen. Board
-- decisions keep their own closed_* values; this one never appears in the
-- Board's queue or history.

ALTER TABLE clause_flags DROP CONSTRAINT clause_flags_status_check;
ALTER TABLE clause_flags ADD CONSTRAINT clause_flags_status_check
  CHECK (status IN ('open', 'in_review', 'awaiting_board',
                    'closed_no_change', 'closed_changed', 'closed_deferred',
                    'closed_committee'));


