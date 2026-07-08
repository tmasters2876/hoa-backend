# PLCA HOA Backend & Admin Console

Two production services for Plantation Lakes Community Association (Waller & Grimes Counties, Texas), deployed from this single repo:

- **hoa-backend** — public Flask API behind the Carrd-embedded resident chatbot (`POST /ask`). Residents ask questions; GPT answers from the governing-document clause database with citations.
- **hoa-admin** — the admin console (hoa-admin.onrender.com) where the clause database is maintained, resident questions are reviewed, and the governing-document revision effort is run.

Developer documentation lives in [CLAUDE.md](CLAUDE.md). The improvement backlog and its history live in [ADMIN_IMPROVEMENTS.md](ADMIN_IMPROVEMENTS.md). The committee member guide is [MEMBER_WORKFLOW.md](MEMBER_WORKFLOW.md).

---

## THE LAW — Role Mandate

**This section is the constitution of this tool. Every feature, permission, and change must serve it. Do not deviate from it, dilute it, or special-case around it. If a proposed change conflicts with this mandate, the change is wrong — not the mandate.** (Ratified by the owner, July 2026.)

The tool exists for exactly two missions:

1. **Revision** — support the Document Revision Committee in reviewing the current governing documents and building the new ones.
2. **Accuracy** — keep the clause database accurate, because it is the source of truth behind the front-end HOA search tool the community uses.

Three roles, three purposes:

### `member` — the Document Revision Committee
Community volunteers, plus a board member and an ARC member, revising the governing documents. Members **review** the current clauses (which constitute the actual governing documents), **flag and discuss** what should change, **propose** changes, and **track** their submissions. Their work product feeds the drafting of the new governing documents. Members can never approve a change — their own or anyone's — and never see accuracy tooling. Workflow: [MEMBER_WORKFLOW.md](MEMBER_WORKFLOW.md).

### `board` — the accuracy reviewer
Reviews the *tool itself* for accuracy: watches what residents actually ask (Resident Questions), flags and corrects inaccuracies in the clause database that powers the front-end search tool, closes revision flags, and reads the decision history. Includes everything `member` can do.

### `superuser` — the final approvers (the HOA president and the developer)
The two superusers are the final accuracy check on everything: **no clause change reaches residents without a superuser approving it**, and deletions require the *second* superuser. They also run user/role management, tag management, imports/exports, and the audit log. Self-approval of edits exists as a warned, audited, break-glass exception — it is never the norm.

### Standing rules derived from the mandate

- The `admin_users.role` column is the single source of truth for permissions. Enforcement is per-request.
- Every new feature must serve the Revision mission, the Accuracy mission, or administration of the tool itself — or it doesn't get built.
- No path may ever let a member-proposed change reach residents without superuser approval.
- Deliberation (flags, comments) is permanent and append-only — it is the committee's record.
- Every state-changing action is audit-logged.
- The Help & Reference panel in the console is core product: it ships updated with every user-visible change.

---

## Quick start (development)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py                 # public API, port 5000
python admin_app.py           # admin console, PORT env (local: 5051)
python -m pytest tests/ -q    # offline test suite
```

`main` auto-deploys **both** services on push — work on a branch, merge deliberately. See [CLAUDE.md](CLAUDE.md) for environment variables, schema, and conventions.
