# PLCA HOA Backend & Admin Console

Two production services for Plantation Lakes Community Association (Waller & Grimes Counties, Texas), deployed from this single repo:

- **hoa-backend** — public Flask API behind the Carrd-embedded resident chatbot (`POST /ask`). Residents ask questions; GPT answers from the governing-document clause database with citations.
- **hoa-admin** — the admin console (hoa-admin.onrender.com) where the clause database is maintained, resident questions are reviewed, and the governing-document revision effort is run.

Developer documentation lives in [CLAUDE.md](CLAUDE.md). The improvement backlog and its history live in [ADMIN_IMPROVEMENTS.md](ADMIN_IMPROVEMENTS.md); the committee-facing backlog is [MEMBER_VIEW_REVIEW.md](MEMBER_VIEW_REVIEW.md). The committee member guide is [MEMBER_WORKFLOW.md](MEMBER_WORKFLOW.md).

---

## THE LAW — Role Mandate

**This section is the constitution of this tool. Every feature, permission, and change must serve it. Do not deviate from it, dilute it, or special-case around it. If a proposed change conflicts with this mandate, the change is wrong — not the mandate.** (Ratified by the owner, July 2026.)

The tool exists for exactly two missions:

1. **Revision** — support the Document Revision Committee in reviewing the current governing documents and building the new ones.
2. **Accuracy** — keep the clause database accurate, because it is the source of truth behind the front-end HOA search tool the community uses.

Three roles, three purposes:

### `member` — the Document Revision Committee
Community volunteers, plus a board member and an ARC member, revising the governing documents. Members **review** the current clauses (which constitute the actual governing documents), **flag and discuss** what should change, and **propose** revised language in the flag thread. Their work product is the new governing documents. **Members revise the documents, never the database** (amended by the owner, Sept 2026): they never see edit forms, Add/Delete Clause, My Submissions, pending or decided changes, change history, embeddings, source verification, or the Search Test — the database is updated to match the ratified documents afterward, by board-and-up. Members can never approve anything and never see accuracy tooling. Workflow: [MEMBER_WORKFLOW.md](MEMBER_WORKFLOW.md).

### `board` — the accuracy reviewer and the Board's decision seat
Records the Board's decision on every committee proposal — Board Approved (→ community vote), Board Rejected (reason required, shown to the committee), or Deferred — from the **Board Decisions** page (added Sept 2026). Also reviews the *tool itself* for accuracy: watches what residents actually ask (Resident Questions), flags and corrects inaccuracies in the clause database that powers the front-end search tool, closes revision flags, and reads the decision history. Includes everything `member` can do.

### `superuser` — final approval authority
Superusers are the final accuracy check on everything: **no clause change reaches residents without a superuser approving it**, and deletions require a *second* superuser. They also run user/role management, tag management, imports/exports, and the audit log. Self-approval of edits exists as a warned, audited, break-glass exception — it is never the norm.

### The Final Approval clause (amended by the owner, September 2026)

In **all member-facing language** — MEMBER_WORKFLOW.md, the in-console Guide, the Help & Reference panel as seen by non-superusers, My Submissions, and any future member surface — the approval step is described in **governance terms only**: the committee **proposes**, the **HOA Board approves or rejects** the submitted proposal, and a Board-approved proposal is **advanced to the community for the final vote**. A rejection always records its reason for the submitting member so the proposal can be revised and resubmitted. The canonical sequence is *Committee Review → Committee Proposal → Board Approval / Rejection → Community Vote* (the Guide's workflow diagram).

What member-facing language must **never** expose is the tool's internal mechanics: which console role applies a change, how many approvers there are, reviewer identities on decided items, or any superuser/board tooling. The governance terms are stable; the mechanics behind them are expected to change without member-facing language needing to. Before September 2026 the clause went further and allowed only the bare phrase "final approval"; the owner amended it so the Guide can name the Board and the community vote.

### Standing rules derived from the mandate

- The `admin_users.role` column is the single source of truth for permissions. Enforcement is per-request.
- Member-facing surfaces describe approval in governance terms only — Board approves/rejects, then community vote (see the Final Approval clause above) — and never reveal reviewer identities, approver counts, or superuser/board tooling.
- Every new feature must serve the Revision mission, the Accuracy mission, or administration of the tool itself — or it doesn't get built.
- No path may ever let a member change the clause database: every create/edit/delete/re-embed route and My Submissions is gated board-and-up (`db_change_required`), and every such surface in the templates is behind `can_edit_db`.
- No path may ever let a board-proposed change reach residents without superuser approval.
- Deliberation (flags, comments) is permanent and append-only — it is the committee's record.
- Every state-changing action is audit-logged.
- The Help & Reference panel in the console is core product: it ships updated with every user-visible change.

---

## Quick start (development)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py                 # public API, port 5000
python admin_app.py           # admin console against the .env Supabase, PORT env (local: 5051)
python -m pytest tests/ -q    # offline test suite
```

**Admin console sandbox** — a local Supabase stack (Docker) seeded with a copy of
production, for UI/UX work that must not touch production:

```bash
cd dev && ./dev.sh up && ./dev.sh seed && ./dev.sh run   # http://localhost:5052, password devpass123
```

See [dev/README.md](dev/README.md).

`main` auto-deploys **both** services on push — work on a branch, merge deliberately. See [CLAUDE.md](CLAUDE.md) for environment variables, schema, and conventions.
