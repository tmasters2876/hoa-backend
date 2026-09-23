-- hoa-admin local DEV schema — a faithful copy of the production Supabase
-- project "HOA Clause Search" (public schema) as of 2026-09-23.
--
-- Extracted from production via information_schema / pg_constraint /
-- pg_indexes / pg_get_functiondef. Column types, defaults, constraints,
-- indexes and the match_clauses RPC match production exactly. RLS is
-- enabled with no policies, exactly like production — the console talks
-- to the database with the service-role key, which bypasses RLS.
--
-- Applied automatically by `supabase start` / `supabase db reset` from
-- dev/. Never run this against production.

create extension if not exists vector with schema public;

-- ── clauses ──────────────────────────────────────────────────────────────────
create table public.clauses (
  id               uuid primary key default gen_random_uuid(),
  clause_id        text,
  document         text,
  page             integer,
  citation         text,
  clause_text      text,
  plain_summary    text,
  link             text,
  embedding        public.vector(1536),
  match_source     text,
  reviewer_id      text,
  tags             text[],
  created_at       timestamp without time zone default now(),
  precedence_level integer,
  status           text default 'approved'::text,
  constraint unique_clause_id unique (clause_id)
);

-- ── admin_users ──────────────────────────────────────────────────────────────
create table public.admin_users (
  id                   uuid primary key default gen_random_uuid(),
  username             text not null unique,
  password_hash        text not null,
  is_active            boolean default true,
  created_at           timestamp without time zone default now(),
  must_change_password boolean default false,
  is_approver          boolean not null default false,
  role                 text not null default 'member'::text,
  constraint admin_users_role_check check (role = any (array['superuser'::text, 'board'::text, 'member'::text]))
);

-- ── clause_audit_log (append-only) ───────────────────────────────────────────
create table public.clause_audit_log (
  id            uuid primary key default gen_random_uuid(),
  clause_id     text,
  record_id     uuid,
  changed_by    text,
  changed_at    timestamp with time zone default now(),
  action        text not null,
  field_changed text,
  old_value     text,
  new_value     text,
  notes         text
);
create index clause_audit_log_action_idx     on public.clause_audit_log using btree (action);
create index clause_audit_log_changed_at_idx on public.clause_audit_log using btree (changed_at desc);
create index clause_audit_log_changed_by_idx on public.clause_audit_log using btree (changed_by);

-- ── pending_changes ──────────────────────────────────────────────────────────
create table public.pending_changes (
  id               uuid primary key default gen_random_uuid(),
  clause_id        uuid,
  submitted_by     text not null,
  submitted_at     timestamp with time zone default now(),
  action           text not null,
  proposed_changes jsonb,
  original_values  jsonb,
  status           text default 'pending'::text,
  reviewed_by      text,
  reviewed_at      timestamp with time zone,
  review_notes     text
);
create index pending_changes_clause_id_idx    on public.pending_changes using btree (clause_id);
create index pending_changes_status_idx       on public.pending_changes using btree (status);
create index pending_changes_submitted_at_idx on public.pending_changes using btree (submitted_at desc);

-- ── user_activity_log ────────────────────────────────────────────────────────
create table public.user_activity_log (
  id          uuid primary key default gen_random_uuid(),
  username    text,
  action      text,
  ip_address  text,
  user_agent  text,
  occurred_at timestamp with time zone default now()
);
create index user_activity_log_occurred_at_idx on public.user_activity_log using btree (occurred_at desc);
create index user_activity_log_username_idx    on public.user_activity_log using btree (username);

-- ── clause_flags ─────────────────────────────────────────────────────────────
create table public.clause_flags (
  id               uuid primary key default gen_random_uuid(),
  flag_type        text not null default 'clause'::text,
  clause_id        text references public.clauses(clause_id) on delete set null,
  flagged_by       text not null,
  flag_notes       text,
  status           text not null default 'open'::text,
  resolution_notes text,
  closed_by        text,
  question_text    text,
  answer_snapshot  text,
  cited_clause_ids text[],
  created_at       timestamp with time zone not null default now(),
  updated_at       timestamp with time zone not null default now(),
  constraint clause_flags_flag_type_check check (flag_type = any (array['clause'::text, 'topic'::text])),
  constraint clause_flags_status_check check (status = any (array['open'::text, 'in_review'::text, 'closed_no_change'::text, 'closed_changed'::text, 'closed_deferred'::text]))
);

-- ── clause_flag_comments (append-only) ───────────────────────────────────────
create table public.clause_flag_comments (
  id         uuid primary key default gen_random_uuid(),
  flag_id    uuid not null references public.clause_flags(id) on delete cascade,
  author     text not null,
  comment    text not null,
  created_at timestamp with time zone not null default now()
);

-- ── resident_questions (sql/001_resident_questions.sql) ──────────────────────
create table public.resident_questions (
  id                     uuid primary key default gen_random_uuid(),
  created_at             timestamp with time zone not null default now(),
  question               text not null,
  answer                 text,
  cited_clause_ids       text[],
  prefilter_used         boolean,
  prefilter_clause_count integer,
  whimsy                 boolean not null default false,
  mode                   text,
  output_format          text
);
create index resident_questions_created_at_idx on public.resident_questions using btree (created_at desc);

-- ── RLS: enabled, no policies (matches production) ───────────────────────────
alter table public.clauses              enable row level security;
alter table public.admin_users          enable row level security;
alter table public.clause_audit_log     enable row level security;
alter table public.pending_changes      enable row level security;
alter table public.user_activity_log    enable row level security;
alter table public.clause_flags         enable row level security;
alter table public.clause_flag_comments enable row level security;
alter table public.resident_questions   enable row level security;

-- ── RPC: match_clauses (legacy admin vector search) ──────────────────────────
create or replace function public.match_clauses(query_embedding public.vector, match_threshold double precision, match_count integer)
returns table(clause_id text, plain_summary text, clause_text text, citation text, document text, link text, tags text[], similarity double precision)
language sql
as $function$
  SELECT
    clause_id,
    plain_summary,
    clause_text,
    citation,
    document,
    link,
    tags,
    1 - (embedding <=> query_embedding) AS similarity
  FROM clauses
  WHERE 1 - (embedding <=> query_embedding) > match_threshold
  ORDER BY similarity DESC
  LIMIT match_count;
$function$;
