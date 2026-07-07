-- Priority Phase item 1 (#11): resident question log.
-- Run in the Supabase SQL editor BEFORE deploying code that writes to it.
-- Additive only — touches nothing existing.
--
-- Owner decisions (July 2026): no IP column (not stored, not hashed);
-- whimsy answers are logged with the flag below; retention is 24 months,
-- enforced manually each quarter:
--   DELETE FROM resident_questions WHERE created_at < now() - interval '24 months';

CREATE TABLE resident_questions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  question text NOT NULL,
  answer text,                            -- final HTML answer sent to the resident
  cited_clause_ids text[],                -- e.g. '{"DECL_27_08","BG_WALLS_01"}'
  prefilter_used boolean,                 -- false = fell back to full corpus
  prefilter_clause_count int,             -- size of the filtered set sent to GPT
  whimsy boolean NOT NULL DEFAULT false,  -- canned whimsy answer, no GPT call
  mode text,
  output_format text
);

CREATE INDEX resident_questions_created_at_idx
  ON resident_questions (created_at DESC);
