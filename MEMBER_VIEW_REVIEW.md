# Member view — owner review (Sept 2026)

**Status:** implemented on branch `member-view`, running in the local DEV sandbox. Not on `main`, not deployed.

**Update 2026-09-24 (owner chose Option B):** the Board review loop is built — proposal panel on every flag, *Submit to the Board* by any committee member, *Recall* until the Board decides, a **Board Decisions** page (queue + decided) for board-and-up with Approve / Reject (reason required) / Defer, *Reopen for revision* after a rejection or deferral, status labels renamed to Board terms, and a Flag button on the clause page. That covers proposed features 1, 2, 6 and 11 below. Requires `sql/003_board_review.sql` in production before merge.

**Premise (owner direction, 2026-09-24):** committee members are revising the *governing documents*, not this database. The database is the current documents laid out clause by clause so the committee can read, search, and discuss them. Members deliberate and propose through **revision flags**; the Board approves or rejects; the community votes; the database is updated afterward to match the ratified documents. Members therefore never see any database-maintenance activity.

---

## Part 1 — What committee members can no longer see

Everything below is hidden from the `member` role. Board and superuser see all of it unchanged. Each item is enforced in the template (`can_edit_db`) and, where it is a route, on the server (`db_change_required`, board-and-up) — a member who guesses a URL is redirected with "You do not have permission."

### Navigation
| Hidden | Where it was |
|---|---|
| **My Submissions** | sidebar |
| Resident Questions, Pending, Audit Log, Tags, Users management | already board/superuser-only; unchanged |

### Clauses page
| Hidden | Why it is database maintenance |
|---|---|
| **Add Clause** panel | creates database rows |
| **Edit / Actions** expander on every card (edit form, Submit for Approval, Self-approve, Regenerate Embedding, Delete Clause) | edits, re-embeds, deletes rows |
| **Search Test** panel (prefilter preview, legacy vector/keyword search) | accuracy tooling for the resident bot |
| **Stale embeddings** and **Pending approval** stat tiles; the "N stale embeddings" chip in the top bar | embedding and approval-queue state |
| "embedding ok / stale" pills, ⚠️ Stale badge, orange stale border, ⏳ Pending Approval pill, pending status dot | row-level database state |
| Import CSV (add / delete) | already superuser-only; unchanged |
| **Help & Reference** sections: Approval Workflow, Source Verification, Add Clause, Embeddings, Search Test, Import CSV, Tag Management, Audit Log, Roles & User Management | describe tooling members do not have |

What members get instead on the Clauses page:
- Tiles: **Clauses** (count across every governing document) and **Open Revision Flags**.
- Every card has **☰ Read clause**, which expands the plain-English summary and the verbatim text inline, plus **View Source** and **🏳 Flag**.
- Help & Reference rewritten for the member: Guide, Browse & Filters, Clause Pages, **Revision Workflow** (the governance sequence and where the proposal lives), Export CSV, Search, Revision Flags (with "post the proposed language in the thread"), Your Account.

### Clause page (`/admin/clauses/<ID>`)
| Hidden |
|---|
| **Edit This Clause** panel |
| **Pending & Decided Changes** table (field diffs, reviewer decisions) |
| **Change History** (audit log) |
| "approved / pending — not visible to residents" status pill and "stale embedding" pill |

Members see: the clause card (ID, precedence, citation, document, page, source link, tags, summary, full text) and **Revision Flags** that reference it. The two hidden queries are also skipped server-side for members.

### Routes now board-and-up
`POST /admin/clauses` (add), `POST /admin/clauses/<id>/update`, `…/update-json`, `…/delete`, `…/regenerate-embedding`, `GET /admin/my-submissions`.

### Guide (`/admin/guide`, MEMBER_WORKFLOW.md)
- Step 3 is now **"Propose the revised language"** — in the flag thread, not an edit form. States plainly: *you never edit a clause in this tool; the database is updated later to match the documents the community ratifies.*
- Step 4 is now **"Track the flag"** — the Revision Flags page and its statuses, not My Submissions.
- Diagram: the committee lane's last node reads "Propose the revised language **in the flag thread**"; the rejection node reads "Reason recorded on the **flag**". Board and Community lanes unchanged.
- "What you'll never see" table gains a row for edit forms / Add Clause / My Submissions.

### THE LAW (README.md, CLAUDE.md)
Member definition amended: *members revise the documents, never the database.* New derived rule: no path may let a member change the clause database.

### Deliberately **kept** for members (say the word if any should go)
- **Export CSV** — read-only download of the filtered clause set; useful for offline markup and meeting packets.
- **Search** — the resident-chatbot answer with cited clauses and Flag this Topic. It is the best way to see how the current documents handle a topic.
- Precedence pills and tags on cards — reading aids, not maintenance.
- Flag status changes to Open / In Review — members can move their own flags forward; closing remains board-and-up.

### Known follow-ups if you approve
- The presentation deck built last week (`PLCA Committee Console Walkthrough.pptx`) still shows Step 3 as the edit form and Step 4 as My Submissions. Slides 10 and 11 and their notes need redoing before the meeting. I can regenerate it from the new Guide.
- The flag statuses still say "Closed — Changed / No Change"; see feature 1 below for renaming them to Board terms.

---

## Part 2 — Features that would help the committee (for your review)

Grouped by size. Nothing here is built. ★ = my recommendation for the first round.

### Small (hours)
1. ★ **Board decision recorded on the flag.** Today a flag closes as "Changed / No Change / Deferred", which are database words. Rename for the committee: **Board Approved → advanced to community vote**, **Board Rejected** (reason required, shown prominently), **Deferred**. Same three statuses under the hood, different labels and a required reason on rejection — so the Guide's promise ("the reason is recorded on the flag") is literally true. *(Database: none, labels only. If you want new statuses, one small check-constraint migration.)*
2. ★ **Flag button on the clause page.** The reading page currently has no way to flag the clause you are reading; you have to go back to the list. One button.
3. **Flag counts on cards.** Show "2 flags · 5 comments" on a clause card and link straight to them, so members can see where discussion is already happening before opening a duplicate.
4. **"My flags" filter** on the Revision Flags page: flags I opened or commented on, with a "new comments since I last looked" marker.
5. **Sort flags by activity** (most recent comment first) — the current sort is creation date, which buries active threads.

### Medium (a day or two)
6. ★ **Proposal block on each flag.** A dedicated "Proposed language" panel per flag — *current text* beside *proposed text*, with who wrote it and when, editable until the flag goes In Review — separate from the comment thread. Today the proposal is just another comment and can get lost in a long discussion. This is the artifact the Board actually votes on.
7. ★ **Meeting packet export.** One click on the Revision Flags page produces a printable page or PDF of the selected flags: clause text, proposal, full thread, status. For agendas, minutes, and handing to the Board.
8. **Reading mode by document.** Browse a whole document in order — Article → Section → clause — instead of the flat paginated list. The committee is revising documents; reading them as documents (with a "next clause" link) is the natural workflow. Uses the existing page and citation fields.
9. **Coverage tracker.** Let a member mark a clause "Reviewed — no change needed" (a lightweight flag type or a checklist), and show a progress bar per document: 712 clauses, N reviewed, N flagged, N with proposals. Answers "how far along are we?" at every meeting.
10. **Topic index for flags.** Tag flags with a policy area (fencing, parking, rentals, sheds…) and list flags grouped by area; show "related flags" on each clause page. Topic flags from Search already capture the question; this generalises it.
11. **Board decision panel** (board role): when the Board decides, the board member records approved/rejected with reason and date on the flag from the console, and the member-facing status updates. Pairs with feature 1.

### Larger (a week or more)
12. **Draft document assembly.** Assemble the *new* governing document from approved proposals: for each document, the current clauses with approved proposals substituted in, exportable as a Word/PDF draft. This is the committee's actual deliverable, and today it would be built by hand from the flags.
13. **Resident-question insights for the committee.** Resident Questions is board-only accuracy tooling. A read-only, aggregated view for members ("topics residents ask about most", "questions the documents don't answer") would be very strong input for what to revise. This touches THE LAW (accuracy tooling stays hidden), so it is your call — it could be framed as revision input rather than tool accuracy.
14. **@mentions and notifications.** Mention a member in a thread; a daily email digest of new comments on flags you follow. Needs an email sender (none exists today) — Render + a free SMTP tier would do.
15. **Straw polls on a flag.** A simple agree / disagree / abstain per member on the proposal, visible to the committee, so consensus is measurable before it goes to the Board. Not a vote, a temperature check.

### Suggested first round
Features 1, 2, 6, 7 — they complete the "the flag is the proposal" story the Guide now tells, and none of them changes the database schema except optionally feature 1.
