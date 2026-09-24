-- Board review of committee proposals (Sept 2026, owner direction).
-- Run in the Supabase SQL editor BEFORE deploying the code that uses it.
-- Additive only.
--
-- A revision flag now carries the committee's PROPOSAL (current text beside
-- proposed text + where the language comes from) and moves through:
--   open → in_review (committee discussing) → awaiting_board (submitted by
--   any committee member; recallable until the Board decides) →
--   closed_changed (Board Approved → community vote) |
--   closed_no_change (Board Rejected, reason required; member may reopen) |
--   closed_deferred (Board Deferred).
-- The three closed_* values are unchanged so existing rows stay valid; the
-- console relabels them as Board decisions.

ALTER TABLE clause_flags DROP CONSTRAINT clause_flags_status_check;
ALTER TABLE clause_flags ADD CONSTRAINT clause_flags_status_check
  CHECK (status IN ('open', 'in_review', 'awaiting_board',
                    'closed_no_change', 'closed_changed', 'closed_deferred'));

ALTER TABLE clause_flags
  ADD COLUMN proposal_current    text,          -- the language being replaced (verbatim)
  ADD COLUMN proposal_text       text,          -- the committee's proposed language
  ADD COLUMN proposal_source     text,          -- where it comes from, or "new drafting"
  ADD COLUMN proposal_updated_by text,
  ADD COLUMN proposal_updated_at timestamptz,
  ADD COLUMN submitted_by        text,          -- who sent it to the Board
  ADD COLUMN submitted_at        timestamptz,
  ADD COLUMN decided_at          timestamptz;   -- when the Board decided (closed_by + resolution_notes hold who/why)

CREATE INDEX clause_flags_status_idx ON clause_flags (status);

-- VERIFY:
SELECT status, count(*) FROM clause_flags GROUP BY status;
