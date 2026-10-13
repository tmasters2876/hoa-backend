# Training scripts — Board and administration set (for review)

The **narration** is what the voice says and what the captions show, word for word. The *italic* lines are what the screen does meanwhile.

## 10-board-decisions — Board Decisions

*Audience: board · signed in as dev-board · about 3 minutes · 6 scenes*

**1. Board Decisions**  
Board accounts see one more item in the sidebar: Board Decisions. This is where the committee's proposals arrive once the committee has agreed on them, and where the Board records its answer.
*zoom: the Board Decisions item in the sidebar, then the page heading*

**2. Board Decisions**  
The queue tab lists everything waiting on the Board, oldest first. Each entry shows the clause, the committee's notes, the current language, and the proposed language side by side, so the decision can be made from this page without hunting for the clause.
*zoom: one queue entry, then the current and proposed language*

**3. Board Decisions**  
There are three answers. Approve means the proposal moves on to the community attorney for review and then to the owners for a vote. Reject asks for a reason, and that reason is shown to the committee so they can revise and send it again. Defer sets it aside without a decision, for when the Board wants to wait on something else first.
*zoom: Approve, Reject, Defer in turn*

**4. Board Decisions**  
Let's record a decision. The console asks you to confirm, and then the flag closes with the outcome written at the top, the committee is notified, and the decision joins the history.
*screen: click Approve on the sample flag, confirm · zoom: the confirmation, then the flag outcome*

**5. Board Decisions**  
The decided tab is the permanent record: every proposal the Board has acted on, with the decision, the date, and the reason where there was one. It's the same record the community can be shown if anyone asks how a change came about.
*screen: switch to the decided tab · zoom: the decided list*

**6. Board Decisions**  
One thing to know: a committee member can recall a flag from this queue until the Board decides, so an entry can disappear and come back later with a revised proposal. That's working as intended. Next: Resident Questions.
*zoom: slow pull back*

## 11-resident-questions — Resident Questions

*Audience: board · signed in as dev-board · about 2 minutes · 4 scenes*

**1. Resident Questions**  
Resident Questions is a log of what the community has asked the chatbot, and what it answered. No one is identified; it's the questions and the answers, newest first.
*zoom: the page heading, then the first row*

**2. Resident Questions**  
Each row shows the question, the answer, and the clauses the answer cited. The cited clauses link to their clause pages, so you can check an answer against the recorded text in one click.
*zoom: the Question & Answer column, then Cited Clauses*

**3. Resident Questions**  
This is the Board's best window into accuracy. If an answer is wrong, the usual cause is a clause whose summary or page is off, or a rule that was not captured. Open the cited clause, check it against the source, and fix it through the edit form, which the next video covers. If the documents themselves are the problem, raise a flag for the committee.
*zoom: a cited clause link*

**4. Resident Questions**  
The filters at the top narrow by date or keyword, which is handy when a resident mentions a question they asked. Next: keeping the clause database accurate.
*zoom: the filter row*

## 12-keeping-the-database-accurate — Keeping the clause database accurate

*Audience: board · signed in as dev-board · about 3 minutes · 8 scenes*

**1. Clauses**  
Board accounts have a second job in the console: keeping the clause database accurate, because it's what the community's chatbot answers from. On the Clauses page you'll notice extra tools that committee members don't see.
*zoom: the Add Clause panel, then the Search Test panel*

**2. Clause page**  
Open any clause page and there's an Edit This Clause panel. The fields are the ones residents depend on: the page, the citation, the Google Drive source link, the plain summary, the tags, and the recorded text.
*screen: open Edit This Clause · zoom: the edit fields*

**3. Clause page**  
Say a summary is misleading. Change the wording and click Submit for Approval. The live clause doesn't change yet. Your edit goes to the approval queue, where a superuser reviews it, and only then does it reach residents. Two sets of eyes on every change.
*screen: edit the plain summary, Submit for Approval · zoom: the summary field, then the Submit for Approval button, then the confirmation*

**4. Clause page**  
When you submit, the console also checks the clause text against the recorded PDF on Google Drive and shows a match score. It's advisory, a quick sanity check that the text really is on the page the clause cites.
*zoom: the verification badge*

**5. Clause page**  
Below the edit panel, Pending and Decided Changes shows what's been proposed on this clause and what happened to it, with a field-by-field before and after. Change History is the full audit trail for the clause.
*zoom: Pending & Decided Changes, then Change History*

**6. My Submissions**  
My Submissions, in the sidebar, lists every change you've submitted and its status. If one is declined, the reason is written there so you can adjust and resubmit.
*screen: open My Submissions · zoom: the table*

**7. Clauses**  
Add Clause is for a rule that exists in a recorded document but is missing here. Citation, page, and the Drive link are required, and it follows the same approval path. Search Test lets you ask a question and see exactly which clauses the chatbot would use to answer it, which is the fastest way to find a gap.
*screen: open Search Test, run a question · zoom: Add Clause, then the Search Test results*

**8. Clauses**  
Everything here keeps the database honest without anyone editing the governing documents themselves; those change only through the committee, the Board, the attorney, and the owners. Next: the Pending queue.
*zoom: slow pull back*

## 13-pending-queue — The Pending queue

*Audience: board · signed in as dev-board · about 1 minute · 4 scenes*

**1. Pending**  
Pending is the approval queue for database changes: every edit, addition, or deletion that has been submitted and is waiting for a superuser's review. Board accounts can see it; superusers act on it.
*zoom: the Pending item in the sidebar, then the queue*

**2. Pending**  
Each item shows who submitted it, when, and a field-by-field before and after, so a reviewer can see exactly what would change. The source check score sits beside it.
*zoom: one item's before and after*

**3. Pending**  
The History tab is the record of every decided change: approved or declined, by whom, when, and the reason for any decline. It's the database's own governance record, separate from the committee's flags.
*screen: switch to History · zoom: the history list*

**4. Pending**  
A badge on the Pending item in the sidebar shows how many are waiting, so a reviewer can see at a glance when there's something to look at. Next, for superusers: users, tags, and the audit log.
*zoom: the sidebar badge*

## 14-administration — Administration for superusers

*Audience: superuser · signed in as dev-superuser · about 3 minutes · 6 scenes*

**1. Pending**  
Superusers are the final check on every database change. On the Pending page, each item has Approve and Reject. Approve applies the change; Reject asks for a reason the submitter will see. A deletion always needs a second superuser, and self-approval of your own edit is possible but flagged in the audit log as the exception it is.
*zoom: Approve and Reject on one item*

**2. Account**  
The Account page is also the user management page for superusers. Add a user with a username, a temporary password, and a role: member, board, or superuser. Each role includes everything below it. The person is asked to set their own password the first time they sign in.
*screen: show the Add User panel · zoom: the username, password, and role fields*

**3. Account**  
The Admin Users table lets you set an email on anyone's account, switch their notification emails on or off, change a role, deactivate an account, or reset a password. A role change takes effect on the person's next page load, with no sign-out needed.
*zoom: the table, then the email and flag-emails controls on one row*

**4. Tags**  
Tags lists every tag in use across the clauses, with counts. Tags help both the filters and the chatbot's relevance matching, so keeping them consistent, and upper case, keeps answers sharp. Rename from this page and every clause follows.
*zoom: the tag list*

**5. Audit Log**  
The Audit Log is the record of everything that has happened to the database and the flags: every edit, approval, flag step, email setting, and sign-in. Filter by person, action, or date. It's append-only; nothing here is ever removed.
*screen: filter by a user · zoom: the filters, then a few rows*

**6. Audit Log**  
That completes the Board and administration set. Together with the member videos, every page in the console has been covered. When a page changes, its video is rebuilt from the script, so what you see here stays current.
*zoom: slow pull back*

