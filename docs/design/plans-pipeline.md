# Plans: a Pipeline board for what automatic runs propose, and a plan conversation for deciding it

**Status: designed 2026-10-28 (lane PLANS-DESIGN, part of #2585). Nothing here
is built.** The owner's picks are BGD's of 2026-10-28 (§1); the build is the
lanes of §11, API lanes first. BGD answered owner questions Q2-Q10 on
2026-10-28, and Q1 (the placement on the page, picked C/C/C/B from the
hand-drawn PLANS-DIAG-A/B/C mock-ups) and Q11 (what the $25 counts) at about
18:30Z the same day; each answer is written into the section it decides (§12
lists them), and no owner question is open. Every file and symbol cited was read on 2026-10-28 at `12932fc`;
those §7.8 added (`store.PLATFORM_SETTINGS_DEFAULTS`, `AdminSettings.tsx`,
`WorkspaceSettings.tsx`, `ScheduleEdit.tsx`, `Attempt.model`) at `8b91b10`.
Lines move, so each citation names the symbol.

What this design settles, in one line each:

* **Where proposals land: Automate › Pipeline**, a board of five stages
  (proposal C, PL1). **What a plan looks like when you decide it: proposal D's
  decision page** at `/automate/approvals/<id>` (PL2-PL5), opened from a board
  card, with Back returning to the board and its filters. **Automate ›
  Approvals stays**, for the kinds that are not on the board.
* **New fields and routes, none in the frozen contract.** Each is a
  swarm-api document or read (§3), owned by `issueruns.py`, `approvals.py`,
  `routes/runs.py` or `routes/approvals.py`, plus one new settings module
  and three platform settings keys (§7.8).
* **Rationale and diagrams join the plan inside its digest**, as optional
  `PlanSpec` fields that a plan without them never serialises, so every stored
  `plan_digest` and `approved_digest` still verifies (§4).
* **Request changes is a planner re-plan**: the same `claude-code` planner, the
  same model class, its cost recorded on the revision it made, five per run by
  default, asked for by whoever may edit the plan, and an open Approve on the
  revision on screen stays valid while one runs and cancels it (§5).
* **Diagrams are Mermaid text, never an image**, drawn where the rule of §6.3
  says they help, inside the plan digest, on by default, switched at platform,
  workspace and schedule level (each overrides the one above, and a run
  records the setting it planned under); a plan that draws none says why in
  one line (§6). **They sit in the conversation** (BGD's PD1-PD2 pick, C):
  the planner's opening message names them, a "Pinned to the plan" strip at
  the top of the page shows one at a time on tabs, and a diagram asked for in
  the thread joins the strip through "Pin to the plan", which makes a
  revision (§2.5).
* **The plan review chat** is an agent that reads this run only, answers each
  question as one short `claude-code` agent streamed to the page, may draw a
  diagram or propose a revision, and never applies one; its transcript is
  every member's and kept with the run (§7). It is a right-hand column opened
  from the page head (PD3, C).
* **The plan-review settings** (§7.8): the chat's model is Sonnet by default,
  set at platform and workspace level and overridable on one plan when the
  plan's reading suggests a stronger one; the caps (5 re-plans, 20 diagram
  asks, 40 chat turns, $25 of review agents per run) are a platform admin's
  setting, and every cap refuses with its reason, never silently. The $25
  counts the chat's turns and the on-demand diagram agents only (Q11). A
  workspace's plan-review settings sit on a new **Automate › Settings** tab
  (PD4, B).

---

## 1. What was picked, in the owner's words

From #2585's comments (read 2026-10-28 10:20Z):

| question | pick | what it means here |
|---|---|---|
| PL1 Where proposals land | **C, Pipeline board** | Automate gains a Pipeline tab between Schedules and Approvals: found → planned/waiting → approved/running → PR and CI → merged/ended; a card that waits on a person is edged amber and sorts first. |
| PL2 One plan to decide | **D, Plan conversation** | A decision page: the plan as a document, a margin with the planner's reasons and the thread; the decision bar names the revision and digest it approves. |
| PL3 Editing | **D** | Comment on a step and Request changes: the planner answers with a new revision and its diff. Or edit by hand. Every revision keeps its digest. |
| PL4 After approval | **D** | The same thread carries on: steps to agents, the PR, CI rounds, the review, the merge. |
| PL5 Sources and gates | **D** | A sources-that-propose list drawing only the gates that exist. |

BGD, 2026-10-28 (10:08Z): *"a Pipeline card opens **D's full decision page**
at /automate/approvals/<id> (Back returns to the board with its filters), not
a drawer (question 59). **Automate › Approvals stays** for the kinds that are
not on the board: a firing waiting to run, a custom-prompt spec, an agent's
input wait, plus a count of what is on the board (question 60)."*

BGD, 2026-10-28 (~10:13Z), two additions:

> 1. "add the ability to generate flow diagrams and schematics to be included in plans for the majority of coding tasks, product design or research, issue fixing, error and failure fixes and more but also not everywhere where it doesn't make sense, like fixing a line in terraform or generally tasks that don't involve coming up with a diagram or a flow chart or schematics. This feature should be by default on but toggleable from the automation settings"
> 2. "provide the ability for the interactive conversational part of the plan
>    review according to the new D designs to also be able to provide on
>    demand schematics and diagrams during the plan review process"

BGD, 2026-10-28 (~10:15Z), the third addition: on-demand diagrams must work
*"even if the plan reviewer / approver asks this in chat with the agent that
takes part in the plan review conversation, interactively in a chat section on
the plan review page"*.

BGD, 2026-10-28, answered owner questions Q2-Q10 of the first round. They are
quoted in §12 and written into §4, §5, §6, §7 and §11; Q4 and Q5 asked for more
than a pick (both "configurable"), which §7.8 designs.

**Placement: BGD's answer to Q1, 2026-10-28 (~18:30Z, recorded on #2585 at
18:33Z).** Where the diagrams, the on-demand ask, the review chat and the
run's diagram setting sit was drawn as three competing mock-ups,
`docs/web-ui/mockups/plans-diagrams-a.html`, `-b.html` and `-c.html`
(PLANS-DIAG-A, B and C, PRs 2640, 2643 and 2642). They are **hand-drawn**:
static pages of example data, not the console, so a frame shows a placement
and never a built behaviour. BGD picked **C, C, C, B** from them on the
plan-diagrams picks page:

| question | pick | what it means here |
|---|---|---|
| PD1 Where a plan's diagrams sit | **C, diagrams in the conversation** | The planner's opening message in the thread lists the diagrams it drew, or one line saying why it drew none; a "Pinned to the plan" strip at the top of the decision page shows one diagram at a time on tabs, each anchored to the plan or a step. |
| PD2 Asking for a diagram during review | **C** | The reviewer asks in the thread, anchored to a step; the answer is a diagram message; "Pin to the plan" makes a new revision whose digest covers the diagram set, and puts it on the strip. |
| PD3 The review chat | **C** | A right-hand column opened from the page head; while it is open, the margin's reasons and thread fold under each part of the document, as on the phone. |
| PD4 The diagrams switch | **B** | A new Settings tab under Automate holds the workspace default (on); each schedule that plans can follow it or override it on or off. |

The picks take C's and B's placements only. The data, routes and checks stay
this design's (§3, §6, §7): where a mock-up's field list differs (C's
`plan-pin` and `diagrams_reason`, B's `automate_settings.plan_diagrams` and
`gate.plan_diagrams`), §6.2's `no_diagram`, §6.5's `plan:add-diagram`,
§6.4's `plans.diagrams` and the schedule's `plan_diagrams` serve the same
frame, and the mock-ups' example costs and caps are not this design's.

---

## 2. The pages and their states

Frames are cited by the id they carry in the two mock-ups: `C:P1a` is frame
P1a of `docs/web-ui/mockups/plans-c.html` (PR 2589), `D:P2a` is frame P2a of
`docs/web-ui/mockups/plans-d.html` (PR 2587). Both mock-ups obey PICKS.md:
nothing below 12px, no all-caps, a light / dark / system toggle, an unknown
figure is a dash with its reason (never 0), and state words are lower-case
words, never the API's enums.

### 2.1 Automate › Pipeline: the board (from C)

`/automate/pipeline`, a new tab of the `automate` entry of `SECTIONS`
(`apps/swarm-ui/src/App.tsx`), between `schedules` and `approvals`. The
section's `question` already ends "and what is waiting on me to approve?",
which the board answers too, so it does not change.

| frame | state | what it draws |
|---|---|---|
| `C:P1a` | every source, every state | Five columns: **found**, **planned / waiting**, **approved and running**, **pull request and CI**, **merged or ended**. Each column's head names its count and its oldest card; a column holding a card over a day is edged. Every card names its source in words (§3.1). A card that waits on a person is edged amber in whatever column it is, sorts first, oldest first, with its time to expiry. The lead strip names what waits on a person across the whole board. |
| `C:P1b` | filtered by source | `?source=failure_review`: the columns stay, other sources' cards leave, each column's count says how many it hides. |
| `C:P1c` | filtered by state | A state filter that leaves one column turns the board into that column's table, with room for each reason in full. |
| `C:P6a` | nothing waits on a person | The board, no amber; the lead says so and names the next firing. |
| `C:P6b` | a source paused | The paused source's cards keep their places; the filter chip and the lead say who paused it, when and why. |
| `C:P6c` | one read failed | `GET /v1/pipeline` answers with `partial` (§3.2): the columns fed by the failed read show the region error in place; the others draw. |
| `C:P6d` | not served | A deployment without `GET /v1/pipeline` (404): the whole-section state, with the nav usable and a link to Approvals, which still works. |
| `C:P7a` | phone at 390 | One stage at a time: a row of stage chips with counts, the waiting stage first. |

**What changed from C as drawn.** C opened a plan in a drawer over the board
(`C:P2a`-`C:P4b`). BGD's 10:08Z pick replaces the drawer: a card opens **D's
decision page** at `/automate/approvals/<approval_id>`, a full page, and its
Back control returns to `/automate/pipeline` with the filters the board had
(the address carries them, so Back is a link, not history). C's P2-P4 frames
are therefore not built; D's P2-P4 are. A card whose subject has no decision
page (a run in PLANNING, a run that ended without a plan) opens the run in
Work › Runs, as today.

**Nothing on the board drags** (C's own risk note): a card moves only when its
run or approval moves, and a decision is made only on the decision page,
against a digest.

**Which card sits where** is the server's (§3.2), from the states C's `P1a`
note names:

| stage | from |
|---|---|
| found | a pending `proposal` approval (an observer or flake proposal); an issue run in `PLANNING`; a failure-review cause filed with no run yet (`failure_causes`, read by fingerprint) |
| planned / waiting | an issue run in `PLANNED` (projected as `plan`, or `hold` when held) |
| approved and running | `APPROVED`, `RUNNING` (with `approved_by`, `auto-approval` for a gate's automatic approval) |
| pull request and CI | `CHECKING`, `FIXING`; a pending `merge` approval |
| merged or ended | `DONE`, `FAILED`, `CANCELLED`, `REJECTED` (`rejected_by` and its reason), `NOT_READY`; a `proposal` approved (filed), rejected, expired or superseded |

`waits_on_person` is true exactly for a pending `plan`, `hold`, `merge` or
`proposal` item: the inbox's own pending projection (`approvals.inbox`), never
re-derived in the browser.

### 2.2 Automate › Approvals: what stays (BGD 10:08Z)

`/automate/approvals` keeps the kinds that are not on the board: `run` (a
firing waiting to run), `spec` (a `custom-prompt` digest) and `input` (an
agent's await-input step), drawn as `Approvals.tsx` draws them today. Above
them, one line: *"N waiting on the Pipeline board"*, linking to
`/automate/pipeline?waits=person`. The count is `by_stage` of §3.2 summed over
`waits_on_person`, read from the same `GET /v1/pipeline`, so the two pages
cannot disagree on it.

`/automate/approvals/<id>` stays the one address of every decision page,
whatever the kind: a `plan` or `hold` item opens D's page (§2.3); a `merge` or
`proposal` item opens its decision page as `ApprovalDecision.tsx` draws it
today, with Back to the board when it was opened from there. So a link sent
before this change still opens what it named.

### 2.3 The decision page (from D)

`/automate/approvals/run:<run_id>` for a projected plan or hold. Work › Runs
links to it from a `PLANNED` run (`D`'s "Runs links to it"); the plan view in
`Runs.tsx` becomes a summary with that link.

| frame | state | what it draws |
|---|---|---|
| `D:P2a` | waiting, seen by someone who may decide | **The document** on the left, in the order a decision is made: why it exists (the source, §3.1, and the issue), the summary, requirements (folded past the first), overlaps (folded past the first), the steps (id, files, tests, estimate, prompt), risk per step (§3.7), estimate and **expected cost** (§3.8). Above it, the **Pinned to the plan** strip of the revision's diagrams (§2.5, §6). **The margin** on the right: against each part, the planner's reason (§3.6) and that part's thread entries (§3.4), opening with the planner's message naming the diagrams it drew (§2.5). The page head carries the button that opens the review chat's column (§2.5, §7). **The decision bar** names the revision and digest it approves: Approve, Edit, Request changes, Reject with a reason. |
| `D:P2b` | seen by a member who may not decide | No Approve, Edit or Reject; the bar says who can (`may_decide.deciders`, §3.9); Comment and the chat (§7) stay, because "a comment never changes the plan, only a revision does". |
| `D:P2c` | superseded while open | A poll brought revision N+1 while N is on screen: the page keeps N, says N+1 arrived and what changed, takes Approve away from N (the server would answer 409 `plan_changed` anyway) and offers one button, "Read revision N+1". The rule `Runs.tsx` records today: a poll never swaps the plan under an open Approve. |
| `D:P3a` | Request changes | Every open comment goes to the planner with the digest it was written on; the thread shows "re-planning" with the cost so far; the planner's answer arrives as revision N+1, with its diff, each comment marked answered by it, and its cost on its row (§5). |
| `D:P3b` | edit by hand | Today's `PlanEditor` (summary, estimate, requirements; per step title, prompt, files, tests, estimate) plus **Remove step** (disabled on the last step, because `PlanSpec.steps` keeps at least one) and, with §6, remove or reorder a diagram. The change list beside it is the diff being built. Save carries the digest it replaces. |
| `D:P3c` | revision saved | Revision N+1, its digest and author; Approve offered for it alone. |
| `D:P4a` | running | Approval does not close the page. The thread carries on (§3.4's events): approved revision N by whom, each step's agent, the pull request, each CI round and fix round. |
| `D:P4b` | merged, every requirement confirmed | The review's verdict against each requirement, the merge, "the pull request says Closes #N". |
| `D:P4c` | merged, a requirement unmet | "part of #N", naming the unmet requirement (`requirements_unmet`, `requirements_note`), as docs/issue-runs.md requires. |
| `D:P6d` | the server predates threads | The run read carries no `plan_revisions` or `plan_thread`: the plan is drawn without revisions or thread, and Approve, Edit and Reject work as today. |
| `D:P7b` | phone at 390 | One column: each part, then its reason and thread under it; the decision bar sticky at the foot. The strip draws each diagram in a tall layout of the same source, and the review chat opens as a sheet over the plan (`DC:D7`, §2.5). |

D's `P1a`/`P1b` (Approvals as a list of threads) are **not** built: PL1 is C's
board. Their one idea that survives is the "last word" of a thread, which a
board card shows as its second line when the thread has one (§3.2,
`last_word`).

**The decision page's address keeps the board's filters for Back.** A card
links to `/automate/approvals/run:<id>?from=pipeline&source=…&state=…`; Back
reads `from` and the filters and links to the board with them. `keptKeys` in
`App.tsx` gains `from`, `source`, `state` and `waits` for the approvals tab and
`source`, `state`, `waits` for the pipeline tab.

### 2.4 The sources that propose (from D, P5)

`D:P5a`: Automate › Schedules, narrowed to the schedules that propose
(`/automate/schedules?proposes=1`), and linked from the board's lead as
"Sources". Each row: what it proposes, its gate **as stored** (`gate.run`,
`gate.plan`, `gate.merge`, `gate.approvers`, `gate.approval_ttl_hours`), its
spend today against its day budget, and what it proposed and what became of it
in seven days (`proposals_7d`, §3.10). A row opens the schedule's sheet. The
merge column's switch is the audited one: `merge: automatic` is reachable only
through `POST /v1/schedules/{id}:merge-mode` (recorded `gate_merge_auto`;
`PATCH` refuses it 422 `use_merge_switch`), as C's `P5b` draws it. **Only gates
that exist are drawn**: nothing here invents a gate the API does not serve.

A schedule "proposes" when its type is `issue-sweep`, `failure-review`,
`observer` or a flake type: the schedule types whose firings create issue runs
or `proposal` approvals. `scheduletypes` gains a `proposes: bool` on each
entry, served on `GET /v1/schedule-types`, so the list is the server's.

### 2.5 Diagrams and the review chat on the page

**Placed as BGD picked on 2026-10-28 (Q1: C, C, C, B; §1).** Frames are cited
by the id they carry in the two picked mock-ups: `DC:D1a` is frame D1a of
`docs/web-ui/mockups/plans-diagrams-c.html` (PR 2642), `DB:D5a` is frame D5a
of `docs/web-ui/mockups/plans-diagrams-b.html` (PR 2643). Both are hand-drawn
pages of example data, so their costs, caps and counts are examples; the
values are §6-§7's. Each part below is fed by a read of §6-§7:

| frame | part | what it draws |
|---|---|---|
| `DC:D1a`, `DC:D1b` | **the Pinned to the plan strip** (PD1) | One card above the document, the revision's `diagrams[]` (§6.2) and nothing else: one diagram drawn at a time, the others named on tabs, each tab with its title and what it explains (the plan, or a step; a requirement or overlap when §6.2's `explains` names one). The card says "N diagrams in revision R · inside its digest" and offers Open large. Every diagram is drawn once, here: the messages that name it link to its tab. A revision with no diagram has no strip. |
| `DC:D1a` | **the planner's opening message** (PD1) | The head of the margin's thread is the `planned` event (§3.4), drawn as the planner's message: it names each diagram of revision 1 with its `why` and a link to its strip tab. A `revised` event names the diagrams its revision added or removed. Both are derived on read from the revisions, never stored twice. |
| `DC:D2a` | **the "no diagram, because …" line** (PD1) | A plan that drew none (`no_diagram`, §6.2) has no strip; its opening message carries the one line, and Ask for a diagram stays offered beside it. |
| `DC:D3a`-`DC:D3c` | **asking in the thread** (PD2) | Ask for a diagram sits beside Comment in each part's margin composer, anchored to that part (a step, or the whole plan). The waiting entry shows elapsed time and its cost so far; the answer is a `diagram` entry (§3.4) with the diagram drawn in it, which is part of the conversation, not of the plan, so Approve does not include it. **Pin to the plan** (§6.5's `plan:add-diagram`) makes revision N+1 whose digest covers the new diagram set, puts the diagram on the strip, and the entry then names it and links to its tab. The revision chip says "diagrams only" when a revision only added one. |
| `DC:D8a`-`DC:D8c` | **the review chat** (PD3) | A right-hand column, opened from a button in the page head and closed the same way. Its head names the agent, its model and the next turn's model with its level (§7.8); the meter under the composer is the run's review spend against its $25 (§7.8). A chat diagram offers Pin to the plan, which makes a revision exactly as from the thread, and Pin to the thread (§7.5). **While the column is open**, the margin folds: each part's reason and thread are drawn under that part of the document, as on the phone (`D:P7b`), and the strip folds to its tabs so the document keeps its width. |
| `DC:D7a`-`DC:D7c` | **the phone** | The strip draws each diagram in a tall layout of the same source; a sequence diagram scrolls inside its own box. The chat opens as a sheet over the plan. |
| `DC:D6c`, `DB:D5c` | **this run's diagram setting** | When the setting this run planned under (`run.plan_settings`, §6.4) is off: no strip, no Ask for a diagram, no draw in the chat, and one line in the strip's place naming the level that turned it off ("off for this schedule") with a link to that setting for someone who may change it. When it is on, the strip's card says nothing more: on is the default. A run planned before the field says "planned before diagrams". |

**The answer in the thread is "the planner's" by side, not by agent.** PD2
says the planner answers. On the page the answer sits on the planner's side
of the thread, as a diagram message; the agent that draws it is §6.5's
diagram agent at the chat's model (§7.2's builder), and the entry's `by` says
`review-agent` with its model and cost. It is not a re-plan: a diagram
changes no step, so an opus planner run would buy nothing, and Q11 counts it
in the $25 as a diagram agent.

**The settings are edited off this page.** The workspace's are on the new
**Automate › Settings** tab (PD4, B; `DB:D5a`, `DB:D5b`): the workspace
default for diagrams in plans (on unless someone turns it off) and, under it,
each schedule that plans with its choice to follow the workspace or override
it on or off. The rest of the workspace's Plan review card (the chat's model
class and the caps' read-only sentence, §7.8) moves there with the switch,
rather than staying under Settings › Workspace as the design first placed it:
all three are one `plan-settings` read and PUT, and one card per route keeps
a workspace admin from finding half of it on another page. That move is this
design's, not B's, and is asked in `questions.json`. The platform's stay on Admin › Settings (Q2), and a schedule's
override is also on its edit sheet (§6.4, §7.8, lanes U10-U12).

---

## 3. New fields and routes

Both proposals list what they need beyond today's reads in their "New fields
this proposal needs" tables. Every row of both tables is here, each with its
exact shape, the module that owns it, its tenant-isolation rule and its
who-may rule. Two pairs name one thing twice, and each pair becomes one field:
C's `plan.steps[].risk` and D's `risk_paths` (§3.7); C's `plan.expected_usd`
and D's `expected_cost_usd` (§3.8). C's `run.may_decide` and D's
`can_decide`/`deciders` are the `may_decide` the approvals read already serves
(§3.9). C's `run.trace` is not a field of its own: PL4 is D's, whose
after-approval thread is §3.4's derived events, and they carry what `trace`
would have.

**Route spelling.** The brief writes the new run routes as
`POST /v1/runs/{id}:plan-comment`. The run routes that exist are
`POST /v1/runs/{run_id}/plan:approve`, `plan:edit` and `plan:reject`
(`routes/runs.py`), so the new ones are their siblings: `plan:comment`,
`plan:revise`, `plan:diagram`, `plan:add-diagram`, and the chat's `plan-chat`
collection. Same handlers, same `tenant_scope`; one spelling per resource.

**Tenant isolation, once for all of them (invariant 9).** Every route below
takes its tenant from `tenant_scope`, never from the body. Every read of a run
goes through `IssueRuns.get(tenant_id, run_id)`, which answers a stored
`tenant_id` that is not the caller's with the same 404 as a missing run, so
no route here is an oracle for another tenant's run ids. Every new document is
a **subcollection of its run** (`issue_runs/{run_id}/…`) and carries the run's
`tenant_id`; it is read only after its run's read passed, and a subcollection
document whose `tenant_id` differs from the run's is treated as absent and
logged. A platform admin decides another tenant's item by no route here,
exactly as `approvals.py` says today.

**Who may, once.** Four levels, each an existing check, never a new one:

| level | who | the check |
|---|---|---|
| read | any member of the run's tenant, any role | `tenant_scope` |
| comment / chat | a member past the role floor (`member`, not `viewer`) | `approvals.require_decider` (workspace-roles.md §4.8), the floor `plan:edit` uses today |
| edit / request changes / ask for a diagram / add a diagram to the plan / override the chat's model on this plan | the same as `plan:edit` today (BGD Q6, 2026-10-28: "whoever may edit the plan today" asks for a re-plan or a diagram) | `require_decider`; the `second_member` hold's last-editor rule already counts every person who made a revision (§3.3) |
| approve / reject | who may approve, and only they reject | `plan:approve`'s who-checks, and `reject_rule` for reject (BGD 2026-10-26); a platform-scope schedule's plan is a platform admin's (`platform_only`) |

### 3.1 `run.source` (C)

What produced the run, named, so a card and "Why it exists" say it in words.

```
"source": {
  "kind": "sweep" | "failure_review" | "plan_only" | "hand",
  "schedule_type": "issue-sweep" | "failure-review" | "issue-plan-only" | null,
  "schedule_id": "sch_…" | null,
  "firing_id": "…" | null,
  "fingerprint": "fr-…" | null,        // failure_review only
  "proposal_id": "apr_…" | null        // the observer/flake proposal the issue was filed from, when known
}
```

**Owner: `issueruns.py`** (the `IssueRun` field and `to_api`), written at
creation: `routes/runs.py` `POST /v1/runs` writes `hand`; the issue-sweep
executor (`schedtypes/issue_sweep.py` `start`) writes `sweep`, or
`failure_review` with the cause's fingerprint when failure-review starts the run
(`schedtypes/failure_review.py`, beside `causes.record_run`). A run stored
before the field is read with `source` **derived** from its stored
`schedule.type` (`hand` when it has none) and `fingerprint: null`, so an old
card still names its source; the derivation is `IssueRun.from_firestore`'s,
not the browser's. Not signed: it decides nothing anyone may do. Observer and
flake proposals are approvals, not runs: their card's source is their
schedule's type, which `GET /v1/pipeline` reads from the approval's
`subject.schedule_id`.

### 3.2 `GET /v1/pipeline` (C)

One read of everything the board draws, each card already in its stage.

```
GET /v1/pipeline?source=<kind>&state=<waiting|running|decided|ended>&waits=person
200 {
  "cards": [{
    "card_id": "run:<run_id>" | "apr_…",
    "stage": "found" | "planned" | "running" | "pr_ci" | "ended",
    "source": {…§3.1…},
    "subject": {"run_id"?, "issue": "owner/repo#N"?, "title": "…", "approval_id"?},
    "state": "<the run's or approval's own state, lower-case word>",
    "waits_on_person": true | false,
    "approval_kind": "plan" | "hold" | "merge" | "proposal" | null,
    "expires_at": "<iso>" | null,
    "since": "<iso>",                  // when it entered this stage
    "last_word": {"by", "at", "text"} | null,   // §3.4's newest entry, 200 chars
    "revision": 3 | null, "plan_digest": "sha256:…" | null,
    "pull_request": {"number", "url", "checks"} | null
  }],
  "by_stage": {"found": {"count", "hidden", "oldest_at"}, …},
  "waits_on_person": 4,
  "partial": [{"read": "approvals" | "runs" | "schedules" | "causes", "status": 503}],
  "window_days": 14
}
```

**Owner: `approvals.py`** (`pipeline(ctx, tenant_id, …)`, beside `inbox` and
`history`, whose projections it reuses: `project_run`, `pending`,
`decided_records`, `project_decided_run`) and **`routes/approvals.py`** (the
route, beside `GET /v1/approvals`). Ended cards are the last
`PIPELINE_WINDOW_DAYS = 14` days, bounded per source like `history`
(`HISTORY_LIMIT` per read), and `by_stage.*.hidden` counts what a filter or the
bound left out, so a column's count is the API's, never 0 for "not read".
`partial` names a read that failed; the others still answer (`C:P6c`). A card
is placed once: a run with a pending `merge` approval is one card in `pr_ci`,
the approval folded into it. Read only, writes nothing, holds nothing
(invariant 1). Who: any member of the tenant (`tenant_scope`), as
`GET /v1/approvals`.

### 3.3 `plan_revisions[]` (D)

Every revision's whole plan, kept, so the page can draw any revision and a
diff between two.

Stored as `issue_runs/{run_id}/plan_revisions/{revision}`:

```
{
  "tenant_id": "…", "run_id": "…",
  "revision": 2,
  "plan": {…PlanSpec…},
  "digest": "sha256:…",              // plan_digest(plan)
  "replaces": "sha256:…" | null,     // the digest it replaced; null for revision 1
  "author": {"kind": "planner" | "person" | "chat_proposal_applied", "by": "<email>" | "planner"},
  "made_by": "plan" | "edit" | "revise" | "add_diagram",
  "answers": ["<thread entry id>", …], // the comments a revise answered
  "changed": [{"path": "steps.1.prompt", "op": "add" | "remove" | "replace"}],  // ≤ 100, a summary; the page diffs the two plans itself
  "cost": {"usd": 1.80 | null, "task_id": "task_…", "model_class": "opus", "why_none": "…" | null} | null,
  "at": "<iso>"
}
```

Served on `GET /v1/runs/{run_id}` as `plan_revisions`: every revision **without
its plan** (number, digest, replaces, author, made_by, answers, changed, cost,
at), newest first, and `GET /v1/runs/{run_id}/plan-revisions/{revision}` for
one whole plan. The run read stays small: a plan is up to `MAX_PLAN_BYTES`
(64 KiB), and a run document holding every revision would pass Firestore's
1 MiB.

**Why a subcollection and not a field.** The run document carries the current
plan, its digest, its revision and its signature (`owner_signature` over
`plan_digest`, `plan_revision`, `plan_edited_by`, RUN-SIG-2). Revisions are a
record of what was shown; they decide nothing. The plan that is approved is
still the run's `plan`, checked against its signed `plan_digest`
(`owner_refusal`), and `approved_digest` names which revision that was. So the
subcollection needs no signature: a forged revision document could mislead a
diff, never change what runs, and the page says "revision N's digest does not
match its plan" when `plan_digest(plan) != digest` rather than drawing it.

**Written** in the same transaction that moves the run's `plan`: the transition
to `PLANNED` from the planner (revision 1), `plan:edit`, `plan:revise`'s
answer, `plan:add-diagram`. **Owner: `issueruns.py`** (`IssueRuns`, which
already owns every write of the run). **Old runs**: no subcollection; the read
synthesises one entry from the run (`revision: plan_revision or 1`, `digest:
plan_digest`, `author` from `plan_edited_by` or the planner) with
`history_complete: false`, and the page says earlier revisions were not kept.

**The `second_member` hold.** `hold_excluded` names the run's creator and its
last plan editor. A revision made by `plan:revise` is the planner's, but it was
asked for by a person; the person who asked is recorded as `plan_edited_by`,
so the rule that keeps an editor from approving their own change keeps the
requester from approving the change they asked for. Unchanged rule, one more
writer.

### 3.4 `plan_thread[]` and `POST /v1/runs/{run_id}/plan:comment` (D)

Comments anchored to a part of the plan, and, after approval, what the plan
became.

Stored as `issue_runs/{run_id}/plan_thread/{entry_id}`:

```
{
  "tenant_id", "run_id",
  "entry_id": "pth_<hex>",
  "kind": "comment" | "answer" | "diagram" | "chat_pin",
  "anchor": {"part": "plan" | "step" | "requirement" | "overlap" | "diagram",
             "ref": "<step_id>" | "<requirement index>" | "<owner/repo#N>" | "<diagram_id>" | null},
  "by": "<email>" | "planner" | "review-agent",
  "text": "…",                        // ≤ 4,000 chars, masked like every stored text
  "on_digest": "sha256:…",            // the revision it was written on
  "on_revision": 2,
  "answered_by_revision": 3 | null,   // set by the revise that answered it
  "resolved": false,
  "diagram": {…§6.2…} | null,         // kind "diagram"
  "cost": {…as §3.3…} | null,         // an agent's entry
  "at": "<iso>"
}
```

```
POST /v1/runs/{run_id}/plan:comment
{"plan_digest": "sha256:…", "anchor": {"part", "ref"}, "text": "…"}
→ 201 {"entry": {…}}
POST /v1/runs/{run_id}/plan:comment {"entry_id", "resolved": true}   // fold a resolved comment
```

`plan_digest` is the revision the comment was written on; a digest that is not
one of the run's revisions is 409 `plan_changed`, a digest of an older revision
is accepted (a comment on revision 1 read after revision 2 arrived is still a
comment on revision 1, and says so). An anchor whose `ref` is not in that
revision is 422 `anchor_unknown`. **A comment never changes the plan, its
digest or its revision.** Who: comment level (§3, `require_decider`'s floor);
resolving: its author, or anyone at the edit level. Allowed while the run is
`PLANNED`, and after it, through `DONE`, so a reviewer can remark on what the
plan became; never on `REJECTED` or `NOT_READY`.

**Served** on `GET /v1/runs/{run_id}` as `plan_thread`: the stored entries
**plus derived events**, merged by time, each event `{"kind": "event",
"event": …, "at", "ref"}`, computed on read from the run, never stored twice:
`planned` (revision 1), `revised` (each revision), `approved` (`approved_by`,
`approved_digest`, its revision), `rejected`, `step_task` (each step's task id
and state, from the compiled workflow, read as `RunSteps.tsx`'s
`useRunWorkflows` reads it today), `pull_request` (opened), `ci_round` (each
`ci_fix_workflows` entry and its result), `review` (`requirements_met`,
`requirements_unmet`), `merged` (`pull_request.merged_at`), `keyword`
(`Closes #N` or `part of #N`). That is D's P4 (`D:P4a`-`D:P4c`) and the
"trace" C wanted as `run.trace`, from the run's own fields. Bounded: the newest
`PLAN_THREAD_LIMIT = 300` entries, `thread_truncated: true` past it.

**Owner: `issueruns.py`** (documents, events), **`routes/runs.py`** (route).

### 3.5 `POST /v1/runs/{run_id}/plan:revise` (D)

Request changes: the open comments and the shown digest go to the planner,
which returns a whole plan. §5 is the round trip; the route is:

```
POST /v1/runs/{run_id}/plan:revise
{"plan_digest": "sha256:…", "entries": ["pth_…", …] | null}   // null: every open comment on this revision
→ 202 {"replan": {"replan_id", "task_id", "from_digest", "entries", "requested_by", "at"}}
```

Refused: 409 `plan_changed` on a digest that is not the current one (a
re-plan of a revision nobody is reading would answer the wrong plan); 409
`replan_running` while one runs (one at a time per run); 409 with the run's
state when it is not `PLANNED`; 422 `nothing_to_revise` with no open comment;
429 `replan_cap` past the per-run cap (§5.4). Who: the edit level. **Owner:
`issueruns.py`** (the prompt, the answer's validation, the revision) and
**`routes/runs.py`** (the route, the submission through `submit_tasks`).

### 3.6 `rationale` on `PlanSpec` and `PlanStep` (D)

The planner's reason for the plan and for each step: the margin's reasons.

```
PlanSpec.rationale: str | None   (1..2,000 chars)
PlanStep.rationale: str | None   (1..1,000 chars)
```

Optional, inside the digest, written by the planner, editable by hand like any
field (the editor gains one text area per step). Why inside the digest: it is
on the page beside the step when the decision is made, so it is part of what
was approved; a reason that could change after approval without changing the
digest would let the margin say something the approver never read. Why
optional: plans stored before it must keep their digests (§4). **Owner:
`issueruns.py`** (`PlanSpec`, `PlanStep`, `_PLAN_SHAPE`, `planner_prompt`).
When a step's prompt or files change by hand and its `rationale` does not, the
margin marks the reason "written for revision N" (the revision whose
`changed` last touched `rationale`), so a stale reason is not read as current.

### 3.7 Per-step `risk_paths` (D; C's `plan.steps[].risk`)

For each step, which hold categories its files and its prompt's paths touch,
with the paths that matched.

```
GET /v1/runs/{run_id} → "plan_risk": {
  "digest": "sha256:…",                       // the plan it was computed for
  "steps": {"<step_id>": [{"stop": "iam" | "contract" | "protected_path" | "security" | "workflows"
                                     | "secrets" | "tenancy" | "shared" | "cost",
                           "matched": ["<path>", …]}]},     // ≤ MAX_HOLD_MATCHED per step
  "run_hold": {…approval_hold as served…} | null
}
```

**Computed on read, not stored, and not in the plan.** It is
`issueruns.hard_stop_hold` run per step over that step's `files` and the paths
its prompt names (`prompt_paths` narrowed to one step), with the run's
registration (`platform`, `hard_stop_paths`, `workflow_files`) exactly as
`plan_hold` reads them. Outside the digest because the planner does not write
it: it is the platform's reading of the plan, and the patterns change with the
code; a plan approved under yesterday's patterns keeps its digest. It never
clears a hold: `approval_hold` stays the run's, widened as today, and the page
draws the run's hold above the per-step rows. Restating the patterns in the
browser is how the shell copies drifted (C's note), so the browser only draws
this. **Owner: `issueruns.py`** (`step_holds(plan, …)` beside `plan_hold`),
served by `routes/runs.py` `get_run`.

### 3.8 `expected_cost_usd` (D; C's `plan.expected_usd`)

```
GET /v1/runs/{run_id} → "expected_cost_usd": {
  "low": 3.10, "high": 7.80,
  "basis": "2 steps × estimate 1-2 agent-hours at opus list price, plus review and fix",
  "model_classes": {"opus": 2, "sonnet": 1},
  "prices_at": "<iso>"
} | {"low": null, "high": null, "why_none": "the plan's estimates are not in agent-hours"}
```

**Computed by the platform, on read, outside the digest.** The planner writes
`estimate` text; the platform turns it into a range from each step's estimate
(parsed only when it reads as hours or agent-hours), the step's model class,
the review and fix steps the plan compiles to (`compile_plan`'s shape) and the
platform's price table (`modelprices`), with the hourly figure taken from the
tenant's recorded `cost_usd` per agent-hour for that class over the last 30
days (`attempt_totals`), and the platform default when there is no history.
An estimate that does not parse makes the whole figure a dash with
`why_none`, never 0. Outside the digest because prices move; the approver
sees the figure as read at `prices_at`. **Owner: `issueruns.py`**
(`expected_cost(plan, rates)`), served by `routes/runs.py`.

### 3.9 `can_decide` / `deciders` (D; C's `run.may_decide`)

Served today on every approval read as `may_decide: {approve, reject, why,
deciders}` (`routes/approvals.py` `may_decide`, AUTO-API-APPROVALS). D's page
is an approval page, so it reads that. Two additions:

```
"may_decide": {"approve", "reject", "why", "deciders",
               "edit": true | false,        // the edit level, §3
               "comment": true | false}     // the comment level, §3
```

and the same object on `GET /v1/runs/{run_id}` as `may_decide`, computed by
the same function, so Work › Runs' summary and the decision page cannot
disagree. Computed without moving or logging anything, as today; the routes
still decide. **Owner: `routes/approvals.py`** (`may_decide`), reused by
`routes/runs.py`.

### 3.10 `proposals_7d` on a schedule (D)

```
GET /v1/schedules (each row) → "proposals_7d": {
  "proposed": 5, "approved": 3, "auto_approved": 1, "rejected": 1, "expired": 0,
  "running": 1, "merged": 2, "window_days": 7, "complete": true
}
```

Counted by the server from the approvals and runs whose `schedule_id` is the
row's, over seven days, with one bounded read per source like
`approvals.history`; `complete: false` when the bound was reached, and the
column then says "at least". **Owner: `approvals.py`** (`proposals_by_schedule`),
served by the schedules list route. Who: whoever may read the schedule.

### 3.11 What this adds for diagrams and the chat

Set out in §6 and §7, listed here so the table is whole:

| field or route | owner | §|
|---|---|---|
| `PlanSpec.diagrams[]`, `PlanSpec.no_diagram` | `issueruns.py` | 6.2 |
| `run.plan_settings` (the diagram switch it planned under) | `issueruns.py` | 6.4 |
| platform settings `plan_diagrams_default`, `plan_chat_model_class`, `plan_review_caps` | `store.py` (`PLATFORM_SETTINGS_DEFAULTS`), `schemas.py` (`PlatformSettingsRequest`) | 6.4, 7.8 |
| `GET/PUT /v1/workspaces/{tenant_id}/plan-settings` (`diagrams`, `chat_model_class`; `caps` read-only); schedule `plan_diagrams` | new `plansettings.py`, `routes/plan_settings.py`; `schedules.py` | 6.4, 7.8 |
| `run.plan_chat_model`, `chat_model_suggestion`, `plan_review_usage`; `POST /v1/runs/{run_id}/plan:chat-model` | `planchat.py`, `routes/runs.py` | 7.8 |
| `POST /v1/runs/{run_id}/plan:diagram` | `issueruns.py`, `routes/runs.py` | 6.5 |
| `POST /v1/runs/{run_id}/plan:add-diagram` | `issueruns.py`, `routes/runs.py` | 6.5 |
| `issue_runs/{run_id}/plan_chat/{turn_id}`; `POST/GET /v1/runs/{run_id}/plan-chat`; `GET …/plan-chat/{turn_id}/stream`; `POST …/plan-chat/{turn_id}:pin` | new `planchat.py`, `routes/runs.py` | 7 |

**Nothing in this section touches `apps/common/swarm_common/`.** The run, its
plan, approvals and schedules are swarm-api documents (`issueruns.py` says so:
"its shape is not the frozen contract's and must not leak into it"). Every
agent this design submits is an ordinary `TaskCreate` on the existing
`claude-code` profile with `model_class`, which the frozen catalogue already
carries (`MODEL_CLASSES`). §9 checks it field by field.

---

## 4. The plan digest stays honest

`plan_digest(plan)` is `sha256:` over the plan's canonical JSON
(`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`), and
the plan it hashes is `parse_plan`'s output, `PlanSpec.model_dump(
exclude_unset=True, exclude_none=True)`: only what the plan **set**. That one
property is what lets the plan grow without breaking a single stored digest,
and this design keeps to it:

1. **Every new plan field is optional and never defaulted into the dump.**
   `rationale` (plan and step), `diagrams` and `no_diagram` are `None` by
   default; a plan that does not set them dumps exactly the bytes it dumped
   before they existed. So every stored `plan_digest` and `approved_digest`
   still equals `plan_digest(parse_plan(stored_plan, stored=True))`. This is
   the rule `PlanOverlap.action`, `depends_on` and `model_class` already
   follow, and the red-first test of lane P2 (§11) pins it on stored fixtures:
   a plan from before this change, read back, must hash to the digest it was
   approved under.
2. **What the planner or a person writes is inside; what the platform reads
   off it is outside.** Inside: `rationale`, `diagrams`, `no_diagram`. They are
   on the page when the decision is made, so approving a digest approves them;
   a diagram or a reason that could change under an open Approve without
   changing the digest is the exact hole D3 closed. Outside, computed on read:
   `plan_risk` (§3.7), `expected_cost_usd` (§3.8), `may_decide` (§3.9),
   `diagram_expected` (§6.3). They are the platform's readings, and they
   change when the patterns, prices or roles do; a plan approved yesterday
   must not lose its digest because a price moved.
3. **A comment, a chat turn and an on-demand diagram are outside.** They are
   about a revision (each records `on_digest`), never part of it. Adding a
   diagram to the plan (§6.5) is an edit, which makes a new revision with a new
   digest through the same transaction `plan:edit` uses.
4. **The owner signature does not change shape.** RUN-SIG-2's v2 covers
   `plan_digest`, `plan_revision`, `plan_edited_by` (`RESIGNED_FIELDS`); every
   new revision is a move of those fields, re-signed in the same transaction
   (`owner_resigner`), as `plan:edit` does today. No new field joins
   `OWNER_FIELDS`, so no v3 purpose and no re-signing of old runs.
   `plan_settings` (§6.4) is not signed: it changes only what the planner was
   asked to draw, never who may decide or what runs.
5. **`MAX_PLAN_BYTES` (64 KiB) still bounds the whole plan,** diagrams and
   reasons included. §6.2's caps keep four diagrams well inside it; a plan over
   the bound is refused as today, naming the bound.
6. **The comment on the issue** (`issuesync`) carries the digest and the
   revision as today, and the diagrams' Mermaid source in fenced `mermaid`
   blocks, which GitHub renders. The issue comment is a copy; the run is the
   source of truth.

**Diagrams are inside the digest: BGD's answer to Q8, 2026-10-28** (answered;
an approved plan's diagrams are the ones approved, and old runs' digests still
verify by rule 1 above, which lane P2's stored-fixture test pins). Outside would mean an approver approves a plan whose
pictures can be swapped after the click; the pictures are exactly what a
reviewer reads to judge a flow, so they are part of what is approved. The cost
is that adding a diagram to the plan makes a new revision, and Approve moves
to it, which D's page already draws (`D:P3c`).

---

## 5. The planner re-plan round trip

### 5.1 What runs

**Who may ask for one: whoever may edit the plan today** (BGD's answer to
Q6, 2026-10-28; the edit level of §3, `plan:edit`'s check). A re-plan is an
edit the planner makes, so it is asked for at the level a person's own edit
needs; a member who may only comment writes a comment, which the next re-plan
carries.

`POST plan:revise` (§3.5) submits **one planner agent**, exactly the planner's
shape: `PLANNER_PROFILE` (`claude-code`), the run's repository, `metadata:
{issue_run: <run_id>, replan: <replan_id>}`, the run's tenant, submitted as the
run's owner (`run_owner_auth`, as an approval's workflow is) so it uses the
run's forge slot and counts against the run's tenant's budget. **Model:** the
run's own planner class (`RunCreate.model_class`, default the profile's
`opus`): a re-plan is the planner answering about its own plan, and a cheaper
class answering for a stronger one would make revision 2 worse than revision
1 for a reason nobody asked for.

The prompt is `planner_prompt`'s, with the same REPO INDEX, REPO GRAPH, IN
FLIGHT and open-work sections (`plancontext`, re-read now, so the re-plan sees
work that landed since revision 1), plus two sections between delimiter lines
carrying the run id and a fresh nonce, as data: **THE CURRENT PLAN** (the
revision named by `from_digest`) and **THE REVIEWERS' COMMENTS** (each open
entry: its anchor, its text, its author's local part). The instruction: answer
every comment, return a whole plan in `plan.json` (the same `_PLAN_SHAPE`), and
for each comment a one-line answer in `answers` keyed by entry id. Comments are
text a person wrote; they never select a profile, an image, a command or a
resource (invariant 10) because the plan schema has no field for one.

### 5.2 What comes back

When the agent ends, `advance_run` (the same read-or-tick that moves a
`PLANNING` run) reads `plan.json`:

* **A valid plan** (`parse_plan`, then `territory_refusal`, `workflow_paths`
  and `hard_stops_at_plan`, exactly `plan:edit`'s checks) becomes revision
  N+1: one transaction moves `plan`, `plan_digest`, `plan_revision`,
  `plan_edited_by: <the requester>`, widens `approval_hold`, writes the
  revision document and marks each answered entry `answered_by_revision`; and
  an `answer` thread entry per comment with the planner's one line. The
  transaction re-checks that the run is still `PLANNED` **and its `plan_digest`
  is still `from_digest`**; if not (§5.3), the answer is kept as a thread entry
  "the planner's answer to revision N, not applied: the plan moved", with its
  plan readable, and no revision is made.
* **A plan the schema refuses**, or an agent that failed or was cancelled: no
  revision; one `answer` entry saying why (the refusal's text, redacted), the
  comments stay open, the run stays `PLANNED` on revision N.
* **A NOT_READY verdict:** not a transition. A re-plan is a conversation about
  a plan someone is reviewing; it does not end the run. The verdict becomes an
  `answer` entry ("the planner now finds the issue not ready: …"), and a person
  decides whether to reject.

The plan comment on the issue is rewritten for the new revision (`sync_issue`),
as after an edit.

### 5.3 What happens to an open Approve while a re-plan runs

**Approve stays valid for the revision on screen, and cancels the re-plan**
(BGD's answer to Q7, 2026-10-28; answered). `plan:approve` checks the
digest it was sent against the run's current `plan_digest`; a re-plan in
flight does not change it, so an approver reading revision N may approve
revision N. Approving cancels the re-plan agent (`executioncancel`, recorded
`cancelled: approved`), and its answer, if it lands anyway, is the "not
applied" entry above. The decision bar says so before the click: "a re-plan is
running for 2 comments; approving revision 3 cancels it". Why: a request for
changes is not a veto; holding Approve for a re-plan would let any member with
the comment level block a decision, and the approval is exactly of the text on
screen (D3).

**Reject** also cancels it. **Edit by hand** while a re-plan runs is refused
409 `replan_running`, naming it: two writers producing revision N+1 from N is
the overwrite D3 exists to stop. The page offers "Cancel the re-plan" (`POST
plan:revise {"cancel": "<replan_id>"}`, the edit level), after which the edit
goes ahead. **A poll** that brings the re-plan's revision while the reader is
on revision N is `D:P2c`: revision N stays on screen, Approve leaves it, "Read
revision N+1" is offered.

**Approval expiry** (§4.7 of docs/schedules.md) counts from the newest
revision, not from revision 1: `_planned_at` reads the last transition into
`PLANNED`, and a revision is written as a `PLANNED → PLANNED` history entry,
which `_planned_at` skips today. It changes to take the newest revision's `at`.
A new revision is a new question; the reviewer gets the schedule's
`approval_ttl_hours` to answer it.

### 5.4 Its cost, and the cap

* **Recorded on the revision** (`cost` in §3.3): the re-plan agent's
  `cost_usd_total` from `attempt_totals`, its task id and model class, written
  when the agent ends. An agent whose CLI reported no cost and whose spend
  estimate returned none records `usd: null, why_none: "cost not reported"`,
  never 0. A failed re-plan's cost is recorded on its `answer` entry instead.
* **Shown** on the revision's row and in a run total, `plan_review_cost_usd`
  on `GET /v1/runs/{run_id}`: the planner, every re-plan, every on-demand
  diagram and every chat turn, summed, with `complete: false` while any is
  unreported.
* **Capped** at **5 re-plans per run by default** (BGD's answer to Q5,
  2026-10-28), read from the platform setting `plan_review_caps.replans`
  (§7.8), which a platform admin sets; a 6th is 429 `replan_cap` with its
  reason (§7.8's refusal shape). The cap counts re-plans submitted, failed
  ones included, because a failed opus planner run was still paid for. A
  re-plan's dollars count against the run's day budget, not the $25 of
  review agents (BGD's answer to Q11, 2026-10-28; §7.8 says why).
* **Budget**: the agent counts against the tenant's day budget like any agent
  (`budget.py`); a re-plan refused by budget is 429 with the budget's refusal,
  and the thread says so.

**Invariant 1.** A re-plan is demand only while its agent runs, exactly as the
planner is; the `PLANNED` run holds nothing before, during (the run document
itself holds no lease) or after.

---

## 6. Diagrams in plans

### 6.1 What was asked

BGD's first two additions of 2026-10-28 (§1): diagrams **in plans**
"for the majority of coding tasks, product design or research, issue fixing, error and failure fixes and more but also not everywhere where it doesn't make sense, like fixing a line in terraform",
**on by default, toggleable from the
automation settings**; and **on demand** during the plan review, in the thread
and (the third addition) in the review chat.

### 6.2 The plan's diagram field

```
PlanSpec.diagrams: list[PlanDiagram] | None     (0..MAX_PLAN_DIAGRAMS = 4)
PlanSpec.no_diagram: str | None                 (1..200 chars, one line)

PlanDiagram {
  "diagram_id": "d-<lowercase-id>",             // unique in the plan; what a comment anchors to
  "kind": "flow" | "sequence" | "architecture" | "state" | "data" | "schematic",
  "title": "<one line, ≤ 120 chars>",
  "language": "mermaid",                        // the one language, named so a second can be added
  "source": "<Mermaid text, ≤ 6,000 chars, ≤ 150 lines>",
  "why": "<why this diagram is here, ≤ 300 chars>",
  "explains": {"part": "plan" | "step" | "requirement" | "overlap",
               "ref": "<step_id>" | "<requirement index>" | "<owner/repo#N>" | null}
}
```

The page shows `no_diagram` as one line where the diagrams would be: *"no
diagram: a one-line change to terraform/environments/dev/dev.tfvars"*.

**The rule the schema enforces** (on a plan written now, never on one read back
with `stored=True`): when the run's diagram setting is on (§6.4), a plan
carries **exactly one** of a non-empty `diagrams` or a `no_diagram`; when it is
off, **neither**, and a plan carrying one is refused naming the field, as
every extra key is ("an extra key is refused, naming it, rather than
dropped"). A diagram whose `explains.ref` is not a step, requirement or
overlap of the same plan is refused naming it. The source passes §6.6's
server-side check or is refused with its reason, so the planner learns why in
its own refusal and a plan that cannot render never reaches a reviewer.

**Never an image the agent uploads.** The diagram is text the console renders
(§6.6). An uploaded image is opaque to review, to diffs and to the screen
reader, cannot be themed and is an unbounded artifact; text is none of those.

**Mermaid kinds.** Each `kind` names what it explains; each maps to the
Mermaid diagram types it may use, and the source's first statement must be one
of them:

| kind | Mermaid | for |
|---|---|---|
| `flow` | `flowchart` (`graph`) | control flow, a request's path, a decision |
| `sequence` | `sequenceDiagram` | who calls whom, in order: a route round trip, a worker and swarm-api |
| `architecture` | `flowchart` with `subgraph`s | components and where the change sits |
| `state` | `stateDiagram-v2` | a state machine: `RunState`, an agent's states |
| `data` | `erDiagram` | a document shape, collections and their keys |
| `schematic` | `flowchart` with `subgraph`s | a layout or a wiring the change makes |

The `-beta` types (`architecture-beta`, `block-beta`) are left out: their
syntax still moves between Mermaid releases, and a plan approved today must
render in a year. A kind can gain a type when it leaves beta.

**Why Mermaid, evaluated first as the brief asks.** Four text languages were
weighed:

| language | for | against |
|---|---|---|
| **Mermaid** (BGD's pick, Q9) | Models write it fluently; it has flow, sequence, state and ER in one language; `mermaid.parse` validates without drawing; `securityLevel: "strict"` turns off click handlers and HTML; **GitHub renders a ```` ```mermaid ```` block**, so the plan comment on the issue shows the same diagram the console does. | A large browser bundle (loaded lazily, on the decision page only); its directives (`%%{init}`) can change its config, which §6.6 refuses. |
| Graphviz DOT (`@viz-js/viz`, wasm) | Mature layout. | No sequence diagrams; HTML-like labels and `URL`/`href` attributes need their own stripping; GitHub does not render it. |
| D2 | Clean output. | A Go toolchain or a wasm build in the console; models know it less; GitHub does not render it. |
| PlantUML | Every kind. | A Java renderer: either a server we run or sending plan text to a public one, which publishes it. |

**The language is Mermaid: BGD's answer to Q9, 2026-10-28** (answered), with
the sandboxing of §6.6 unchanged: checked where stored, parsed and rendered
strictly, sanitised, and shown as an `<img>`. Nothing in §11 builds a second
language; `language` stays a field so one could be added by a later decision.


### 6.3 When a diagram helps: the planner's rule

The rule is a pure function, `issueruns.diagram_expected(plan, source) ->
{"expected": bool, "rule": "<id>", "why": "<sentence>"}`, unit-tested over the
examples below. It is **advisory**: the planner decides, and the page draws the
rule's reading beside the planner's (`diagram_expected` on the run read,
outside the digest), so a reviewer sees "no diagram: … · the rule expected one:
3 steps across apps/swarm-api and apps/swarm-ui". The schema does not refuse a
plan the rule disagrees with; the reviewer can ask for one (§6.5). Rules, the
first that applies wins:

| id | when | expected |
|---|---|---|
| N1 | every file of every step is a value or wording file: `terraform/environments/**`, `*.tfvars`, `.env*`, a lock file, `docs/**` or `*.md` whose step is a wording fix — and the plan has one step | no: "a value or wording change" |
| N2 | one step, one file, estimate under one agent-hour, and no requirement names a flow, state or sequence word (E4's list) | no: "a one-file change with no flow" |
| E1 | two or more steps | yes: "N steps; how they hand over" |
| E2 | the files span two or more components (top-level `apps/<x>`, `terraform/`, `kubernetes/`, `scripts/`) | yes: "crosses <a> and <b>" |
| E3 | `source.kind` is `failure_review`, or the issue is labelled `bug`, and the files span two or more modules | yes: "the failure's path" |
| E4 | the summary or a requirement names a flow, state, sequence, route, round trip, lifecycle, pipeline, protocol, schema or document shape, or the issue is labelled `design`, `research` or `epic` | yes: "the plan changes a <word>" |
| N0 | none of the above | no: "nothing the rule recognises as a flow" |

**Examples, both ways** (each is a test case of lane P6):

* *Yes, E1+E2:* a plan of three steps — a route in `apps/swarm-api`, its
  console page in `apps/swarm-ui`, a doc — draws a `sequence` of the
  console's request and the route's answer, and an `architecture` of where the
  three pieces sit.
* *Yes, E4:* "an issue run moves to FIXING after a red CI read" draws a
  `state` diagram of `RunState` with the new edge highlighted in its title.
* *Yes, E3:* a failure-review fix for a lease that a reconciler reclaimed
  early draws the `flow` from the failure to the fix, anchored to the step
  that changes the reconciler.
* *Yes, E4:* a design or research issue ("how should the merge train batch?")
  draws a `flow` of the proposal and a `data` diagram of the document it adds.
* *No, N1:* "raise `max_replicas` in `terraform/environments/dev/dev.tfvars`"
  — `no_diagram: "a one-line value change in dev.tfvars"`.
* *No, N1:* "fix three typos in docs/issue-runs.md" — `no_diagram: "wording
  only"`.
* *No, N2:* "rename a variable in `apps/swarm-api/swarm_api/budget.py`" —
  `no_diagram: "a rename in one file; no flow changes"`.

**The planner-prompt wording**, a section `_DIAGRAMS` of `planner_prompt`,
present only when the run's setting is on (so a run with diagrams off submits
exactly today's prompt), after `_PLAN_SHAPE`:

> DIAGRAMS. Draw a diagram when it lets a reviewer judge the plan faster than
> the text does: when the plan has more than one step, crosses components,
> changes a flow, a state machine, a sequence of calls, a route's round trip,
> a document's shape or a failure's path, or the issue is design or research.
> Do not draw one for a value change, a wording fix or a one-file change with
> no flow; then set `no_diagram` to one line saying why ("a one-line change to
> terraform/environments/dev/dev.tfvars"). Never both, never neither.
> Each diagram: `diagram_id` (`d-` and a short id), `kind` (flow, sequence,
> architecture, state, data, schematic), a `title`, `language: "mermaid"`, the
> Mermaid `source`, `why` it is there, and what it `explains` (the whole plan,
> or one step, requirement or overlap by its id). At most 4 diagrams, each at
> most 60 nodes. Mermaid only: `flowchart`, `sequenceDiagram`,
> `stateDiagram-v2` or `erDiagram`. No `%%{init}` directives, no `click`, no
> links, no HTML in labels, no `style`, `classDef` or `linkStyle` lines: the
> console themes the diagram itself. A diagram that does not parse is refused
> with its reason.

### 6.4 The switch: on by default, from the automation settings

**Platform, workspace and schedule: BGD's answer to Q2, 2026-10-28**
(answered). Each level overrides the one above it, so the narrowest that is
set wins, and a run records the setting it planned under:

| level | where it is stored | who sets it, and where |
|---|---|---|
| platform | the platform setting `plan_diagrams_default` in `store.PLATFORM_SETTINGS_DEFAULTS` (the `control/settings` document `GET/PUT /v1/admin/settings` serves), `true`, with its reason beside the value ("BGD 2026-10-28: by default on") | a platform admin, on Admin › Settings (`AdminSettings.tsx`), the page that already sets `merge_by_default` and the agent budget ceiling; recorded as that route's other settings are (`updated_by`, `updated_at`) |
| workspace | `workspace_settings/{tenant_id}.plans.diagrams`: `on` \| `off` \| absent (the platform's) | the workspace's admin (a workspace admin, or the owner of a personal workspace, or a platform admin), exactly `mergesettings.py`'s rule and its switched refusal; on the new Automate › Settings tab (PD4, §2.5, §7.8) |
| schedule | the schedule document's `plan_diagrams`: `inherit` \| `on` \| `off`, default `inherit` | whoever may edit the schedule (its PATCH), audited in `schedaudit` as any field; on Automate › Settings' list of the schedules that plan (follow the workspace, on or off), and on the schedule's edit sheet (`ScheduleEdit.tsx`), both through the schedule's `PATCH`; shown only for a type that `proposes` (§2.4) |

The platform level is a setting a platform admin changes at run time, not an
environment variable: a value in tfvars changes only with a deploy, and BGD's
"configurable in platform" for the caps (Q5) puts every plan-review default in
one place (§7.8).

`workspace_settings` is the per-workspace store `mergesettings.py`,
`judgment.py` and `qasettings.py` already use; the new module `plansettings.py`
follows them: `GET/PUT /v1/workspaces/{tenant_id}/plan-settings`, the settings
MAC every writer there stores (tenant-settings-integrity.md §3.4, so a
tenant's worker cannot forge the value), and one `admin_audit` entry,
`workspace_plan_settings_set`, in the same transaction as the change; a PUT
that changes nothing writes nothing. A run made by hand has no schedule and
reads the workspace's.

**"From the automation settings": a new Automate › Settings tab, BGD's PD4
pick (B), 2026-10-28.** There was no page by that name; B drew one, and BGD
picked it. Automate gains a Settings tab after Approvals (`DB:D5a`). It holds
the workspace's default for diagrams in plans, on unless a workspace admin
turns it off, with BGD's examples of where the planner draws and where it does
not beside it so the switch's meaning is on the screen; and, under it, every
schedule whose type `proposes` (§2.4) with its choice: follow the workspace,
on, or off; a type that does not propose has no plan to draw in and no row.
An issue run submitted by hand has no schedule and follows the workspace, and
the tab says so. Each level is still edited in one way: the workspace's through
`PUT /v1/workspaces/{tenant_id}/plan-settings`, a schedule's through its
`PATCH` (the same field the schedule's edit sheet sets, so the two controls
are one value and one audit trail, never two copies), the platform's on
Admin › Settings (lanes U10, U11, U12). The decision page only **shows** this
run's setting, as §2.5 draws it.

**How a run records what it planned under.** Each planner, re-plan, on-demand
diagram and chat turn reads the effective setting at the moment it is
submitted and records it on what it made:

```
"plan_settings": {"diagrams": true | false,
                  "from": "platform" | "workspace" | "schedule",
                  "read_at": "<iso>"}
```

on the run (the first plan), on each revision document, and on each
on-demand thread entry and chat turn. The record is written once, when the
thing is made, and never rewritten: a workspace that turns diagrams off on
Tuesday does not change what Monday's run says it planned under. A schedule
level that is `inherit` records the level it inherited from, never
`schedule`. A run stored before the field reads as
`null`, and the page says "planned before diagrams". The chat turn's record
also carries the model and the caps it ran under (§7.8). **Off means no diagrams
and no on-demand drawing:** the planner's prompt has no `_DIAGRAMS` section and
its schema context refuses the fields; `plan:diagram` is 409 `diagrams_off`
naming the level that turned them off; the chat agent is told diagrams are off
and a diagram in its answer is dropped with a line saying so. A plan that
already carries diagrams keeps them when the switch goes off later: they are
inside its digest, and dropping them would change what was approved.

### 6.5 On demand: in the thread and in the chat

**Asked in the thread** (D's anchored thread; PD2, `DC:D3a`: Ask for a diagram beside Comment in a part's margin composer, anchored to that part):

```
POST /v1/runs/{run_id}/plan:diagram
{"plan_digest": "sha256:…",                       // the revision on screen
 "anchor": {"part": "plan" | "step" | "requirement" | "overlap", "ref": …},
 "ask": "<what to draw, ≤ 1,000 chars>",
 "kind": "flow" | … | null}                        // a hint; the agent may choose
→ 202 {"entry": {"entry_id", "kind": "diagram", "state": "drawing", "task_id", …}}
```

It submits one review agent (§7.2's builder, asked for a diagram), and
its answer fills the pending `diagram` thread entry: the diagram (§6.2's shape,
checked by §6.6), a one-line note, its cost. **A diagram-only answer does not
change the plan, its digest or its revision.** A digest that is not one of the
run's revisions is 409 `plan_changed`; an older revision's is accepted and the
entry says which revision it was drawn for.

**Asked in the chat** (§7, the right-hand column of PD3): a chat turn may
answer with diagrams; they live in the chat transcript, same shape, same
checks, same cap, and Pin to the plan adds one exactly as from the thread.

**Added to the plan: the page's "Pin to the plan"** (PD2, `DC:D3c`, `DC:D8c`):

```
POST /v1/runs/{run_id}/plan:add-diagram
{"plan_digest": "sha256:…",                        // the CURRENT revision
 "from": {"entry_id": "pth_…"} | {"chat_turn_id": "pct_…", "diagram_id": "d-…"},
 "explains": {"part", "ref"} | null}                // default: the ask's anchor
→ 200 {"run": {…revision N+1…}}
```

The edit level (§3), because it makes a revision exactly as `plan:edit` does:
the same transaction, the digest it replaces, `plan_revision + 1`,
`plan_edited_by`, re-signed; `no_diagram` is cleared when the first diagram
joins. Refused 409 `plan_changed` on a stale digest, 409 `replan_running`
(§5.3), 422 `diagrams_cap` past `MAX_PLAN_DIAGRAMS`, 409 `diagrams_off`.
No agent runs, so pinning costs nothing and its author is the person who
pinned. The new revision's digest covers the whole diagram set, so Approve
names the diagrams it approves, and the revision before keeps its own set;
the diagram moves onto the "Pinned to the plan" strip (§2.5) and the message
it came from names it and links there.

**Who may ask: whoever may edit the plan today** (BGD's answer to Q6,
2026-10-28; the edit level of §3). A member who may only comment asks in a
comment, and an editor turns it into an ask. **In the chat** the same rule
holds: a chat turn asked by a member below the edit level is told it may not
draw, and a diagram in its answer is dropped with the line "a diagram is asked
for by someone who may edit this plan", so the cap cannot be spent around the
rule. **Cost:** recorded on the thread entry or chat turn like a re-plan's
(§5.4), counted in `plan_review_cost_usd` and in the run's $25 of review
agents (§7.8). **Cap:** **20 diagram asks per run by default** (BGD's answer to
Q5), the platform setting `plan_review_caps.diagram_asks` (§7.8), thread and
chat together (the 21st is 429 `diagram_cap`, with §7.8's reason); one ask in
flight per run. **States:** `PLANNED` only;
after the decision the transcript and the thread's diagrams stay readable.

### 6.6 Rendering and safety

Diagram source is **untrusted agent output**. It is checked twice, once where
it is stored and once where it is drawn, and the drawing cannot run anything.

**Stored (swarm-api, `issueruns.check_diagram_source`, no Mermaid needed):**
the size caps (6,000 chars, 150 lines); the first statement is a type its
`kind` allows; refused, naming the line: any `%%{` directive (it can rewrite
Mermaid's configuration), `click`, `call`, `href`, `callback`, `javascript:`,
`data:`, `url(`, `<` followed by a letter, `/` or `!` (an HTML tag or comment
in a label), `style`, `classDef`, `class `, `linkStyle` and `@{` lines; and an
edge or node count over the caps by a lexical count (60 nodes, 100 edges),
which overestimates rather than under.

**Drawn (the console, `DiagramView.tsx`):**

1. **Parse first.** `mermaid.parse(source)`. A failure shows **the source and
   the parser's reason**, never a blank: "this diagram did not render: line 7,
   expecting '-->'", then the source in a code block. The same for a source
   over the caps after parsing (counted nodes and edges).
2. **Render strictly.** `mermaid.initialize({startOnLoad: false,
   securityLevel: "strict", htmlLabels: false, flowchart: {htmlLabels: false},
   maxTextSize: 6000, maxEdges: 100, secure: [every config key]})`: no HTML
   labels, no click handlers, no links, and no directive may change a key.
3. **Sanitise the SVG** with DOMPurify's SVG profile and an allowlist: no
   `<script>`, `<foreignObject>`, `<a>`, `<use>` with an external `href`,
   `<image>`, no `on*` attribute, no `href`/`xlink:href`, no `url(` outside a
   `#`-local marker.
4. **Show it as an image, not as DOM.** The sanitised SVG goes into an
   `<img src="data:image/svg+xml;…">`. An SVG loaded through `<img>` cannot
   run script, load a resource or receive an event, whatever the three steps
   above missed. Mermaid itself runs in the page but only parses and lays out
   text it was given; it fetches nothing.
5. **Lazy.** Mermaid is a separate chunk, loaded by the decision page only,
   with its version pinned in `apps/swarm-ui/package.json`.

**Themes.** Rendered with Mermaid's `base` theme and `themeVariables` from the
console's tokens, once for light and once for dark, keyed by the source's
digest; switching the theme swaps the image without re-asking anyone.

**A text alternative.** The image's accessible name is the diagram's `title`
and `why`; a "Read as text" disclosure under each diagram gives a generated
outline from the parse (for a flow: every edge as "A → B, label"; a sequence:
each message in order; a state diagram: each transition; an ER diagram: each
entity and relation), and the source. A screen reader reads the outline, not
the picture.

**Export.** Copy the Mermaid source; download the sanitised SVG; download a
PNG drawn from the image on a canvas. The issue comment carries the source in a
` ```mermaid ` block, so GitHub draws it there with its own renderer.

---

## 7. The plan review chat

BGD's third addition: a chat section on D's decision page where the reviewer
talks live with an agent that takes part in the plan review. It is a
right-hand column opened from the page head (BGD's PD3 pick, C; §2.5), and
the margin folds under the document while it is open. It reads the
plan, its revisions, the issue, the repository index and the thread; it
answers questions, draws diagrams on request, and can **propose** a revision,
which the reviewer applies through Request changes or an edit, **never on its
own**.

### 7.1 How the chat agent runs

**One agent per question (a turn): BGD's answer to Q3, 2026-10-28**
(answered). Each question submits one
ordinary `claude-code` agent, the **review agent**, whose prompt carries
everything it reads (§7.3) and the transcript so far; it writes its answer and
ends. The next question is the next agent, with the longer transcript. Three
shapes were weighed, and BGD picked the first:

| shape | latency | contract and worker | cost while idle |
|---|---|---|---|
| **per question** (BGD's pick, Q3) | an agent's start (a pod's scheduling and clone) before the first word, then streamed | none: an ordinary agent on the existing profile | nothing: no agent exists between questions (invariant 1) |
| per open review (a session) | the first question pays the start; later ones answer at once | a worker change (a runner that waits for the next question through a worker-only route) and likely a declared runner input on the frozen profile: a contract change request | a live lease for as long as the session idles, which invariant 4 forbids past a short wait: it must checkpoint and exit, and the next question starts it again |
| pod-less (swarm-api calls the model) | the fastest | a provider key in swarm-api and a model call outside the worker: no repository checkout to read | nothing |

Per question is the only shape that builds with no contract change and no
worker change, keeps a waiting review at zero demand, and lets the agent read
the repository itself (the agent clones the run's repository as the planner's
does). Its weakness is the start time before the first word; lane P7 records
`first_token_ms` on every turn, served, so BGD can judge it on real numbers and
revisit the shape if it is too slow. The session shape is written out as
contract change request R1 (§9) so that a later choice does not start from
nothing; nothing in §11 builds it. BGD declined a pod-less path for the re-review on 2026-10-25
(docs/design/no-pod-rereview.md); the pod-less row is there for completeness,
not recommended.

**Runner and model.** `claude-code`, at **Sonnet by default** (BGD's answer to
Q4, 2026-10-28): a conversational answer about a plan already written does not
need the planner's class, and a turn should be cheap enough to ask freely; the
QA review step moved to Sonnet on the same reasoning (#2601). BGD added "make
this configurable as well workspace lvl and platform lvl and as a plan setting
when it makes sense": §7.8 designs the three levels. A reviewer still cannot
pick the model per question: the class is resolved from the settings when the
turn is submitted, and recorded on the turn.

**Streaming.** The worker already publishes the agent's stream-json live
(`logs/live/agent_stdout.tail.log`, docs `agent_streams.py`, #184). swarm-api
serves `GET /v1/runs/{run_id}/plan-chat/{turn_id}/stream` as server-sent
events: it reads that live tail for the turn's agent, keeps only the
assistant's text deltas (never tool calls, tool results, file contents or
system lines), passes each delta through `redact`, and ends with the turn's
final answer once `chat-answer.json` is read. The console shows the deltas as
they come and swaps in the final answer, whose text, diagrams and proposal are
the only things kept. Before the agent starts the stream sends `{"state":
"starting", "position": …}` so the page says "starting the review agent" and
not nothing.

**Idle timeout and one turn's limits.** A turn's agent has a 10-minute timeout
(`PLAN_CHAT_TURN_TIMEOUT_SECONDS = 600`, beside its reason: an answer about a
plan that takes longer is a re-plan, and should be asked as one). There is no
session to idle out in the chosen shape; the console's stream closes
after 60 seconds without a byte and reconnects by the turn's id. One turn in
flight per run: a second question while one runs is 409 `chat_busy`, and the
page queues nothing silently.

**Cost of a turn, where it is shown, and its cap.** Recorded on the turn when
its agent ends (`cost: {usd, task_id, model_class, why_none}`, as §5.4), shown
on the turn, summed in the chat's header and in `plan_review_cost_usd` (§5.4).
Capped per run at **40 chat turns and $25 of review agents** by default
(BGD's answer to Q5, 2026-10-28), the platform settings
`plan_review_caps.chat_turns` and `plan_review_caps.review_usd` (§7.8),
whichever comes first (429 `chat_cap`, naming which, with §7.8's reason). The
dollars count every review agent of the run, chat turns and on-demand diagrams
together, because they are one builder (§7.2), and nothing else: a re-plan
is not in them (BGD's answer to Q11, §7.8); an unreported cost counts at
the median of the run's reported review agents (or, with none, at $0.50), and
the figure says "at least". The tenant's day budget applies on top, as to any
agent.

### 7.2 How the review agent is submitted

```
TaskCreate(
  runner_profile="claude-code",
  model_class=<the class §7.8 resolves: plan, else workspace, else platform>,
  repository_url=<the run's repository>,
  input={"prompt": review_agent_prompt(run, revision, turn), "issue": <N>},
  metadata={"issue_run": <run_id>, "plan_chat": <turn_id>},
)
```

Submitted in the run's tenant **as the person who asked** (their identity, so
the run's forge slot is not borrowed for a member's question, and the cost is
attributed to them), through `submit_tasks` like every agent. `metadata.issue_run`
keeps it out of the run's own state machine: `advance_run` reads a chat agent
only to fill its turn. It changes nothing in the repository: its prompt says
so, and its result is read from one file, `chat-answer.json`, never from a
diff; an agent that pushes is not published, because no integrate step exists.

### 7.3 What it reads, and isolation

Its prompt carries, each section between delimiter lines carrying the run id
and a fresh nonce, as data: the revision on screen (whole), the list of
revisions with their digests and `changed`, the issue as read
(`issue_read`), the REPO INDEX and REPO GRAPH sections (`plancontext`, the
planner's own, re-read), the thread (newest 100 entries), the chat transcript
(newest 20 turns, then a one-line summary of older ones), the diagram setting
and the anchor the reviewer chose. It also has the run's repository checked
out, which it may read.

**It reads only this run's tenant and repository** (invariant 9): the agent is
submitted in the run's tenant, through that tenant's service account,
namespace and secrets, with the run's repository as its one repository. Its
prompt is built by swarm-api from this run's documents after the run's tenant
check; nothing in a question selects another run, repository or tenant,
because a question is text and the agent's shape is fixed here. A question is
data inside delimiters, never a profile, image, command or resource
(invariant 10).

### 7.4 What it answers

`chat-answer.json`, checked before anything is kept:

```
{"text": "<markdown, ≤ 16,000 chars>",
 "diagrams": [PlanDiagram, …],                 // ≤ 2 per turn, §6.2's shape and §6.6's checks
 "proposal": {"plan": {…PlanSpec…}, "why": "…"} | null}
```

* `text` is stored masked (`redact`) and rendered as the console renders
  other agent text: Markdown with raw HTML off and links shown as text.
* each diagram is checked as a plan's is; one that fails is dropped and the
  turn says which and why; with diagrams off they are all dropped, saying so.
* a **proposal** is a whole plan, checked by `parse_plan` and the edit's
  checks. Valid, it is kept on the turn with its diff against the revision on
  screen and two buttons: **Request changes with this** (posts it as a
  comment with the proposal attached, and a re-plan, §5, may adopt it) and
  **Edit with this** (opens the editor pre-filled, so a person saves it as
  their own revision, with the digest it replaces). Invalid, it is dropped
  and the turn says why. **The agent never applies a revision**: no route
  takes a chat turn's proposal and writes a plan without a person's request.

### 7.5 The chat and D's anchored thread

They are two places with one shape of content:

* **The thread** is the plan's record: anchored, kept with the revision it was
  about, carried on after approval. **The chat** is a conversation with the
  agent: free questions, quick answers, not every line worth keeping.
* **Pin to the thread.** `POST /v1/runs/{run_id}/plan-chat/{turn_id}:pin
  {"anchor": {"part", "ref"}}` copies the turn's answer (its text, and its
  diagrams) into the thread as a `chat_pin` entry, by the person pinning,
  quoting the agent, at the comment level. A pinned answer is then a comment
  like any other, and Request changes sends it to the planner.
* **Add to the plan.** A chat diagram goes in through `plan:add-diagram`
  (§6.5); a proposal through Request changes or an edit (§7.4).
* **Asked in the thread or asked in the chat** are the same machinery (§7.2);
  the answer lands where it was asked.

### 7.6 Who may chat

Members past the role floor ask (the comment level, §3); a turn asked below
the edit level draws no diagram (§6.5, BGD's Q6); every member of the
tenant reads the transcript (BGD's Q10); a viewer reads it and cannot ask. **Only
approvers decide**: nothing in the chat approves, rejects or makes a revision.
A platform-scope schedule's plan: members of the platform tenant may ask; its
decision stays a platform admin's (`platform_only`).

### 7.7 The transcript, kept with the run

`issue_runs/{run_id}/plan_chat/{turn_id}`:

```
{"tenant_id", "run_id", "turn_id": "pct_<hex>",
 "asked_by": "<email>", "ask": "<≤ 2,000 chars>",
 "anchor": {"part", "ref"} | null,
 "on_digest": "sha256:…", "on_revision": 3,
 "state": "starting" | "answering" | "answered" | "failed" | "cancelled",
 "task_id": "task_…",
 "answer": {"text", "diagrams", "proposal", "dropped": ["…why…"]} | null,
 "error": "<redacted>" | null,
 "cost": {…}, "first_token_ms": 41000 | null,
 "plan_settings": {…§6.4…},
 "model": {…§7.8…},                     // the class it was submitted at, from where, and the model ID it ran
 "caps": {…§7.8…},                      // the caps and the usage it was admitted under
 "pinned_as": "pth_…" | null,
 "at", "answered_at"}
```

`GET /v1/runs/{run_id}/plan-chat` serves the turns, oldest first, bounded at
the cap. **Visible to every member of the workspace and kept with the run:
BGD's answer to Q10, 2026-10-28** (answered). Every member, viewers included,
sees the whole transcript, as they see the thread, because it is part of how a
plan was judged; nothing is private to the person who asked. Kept as long as
the run document is. No route deletes issue runs today (`purge.py` only
refuses to purge an agent a live run still references, `_issue_run_guard`), so
whichever lane first deletes a run must delete its three subcollections
(`plan_revisions`, `plan_thread`, `plan_chat`) in the same pass; lane P3's
module docstring says so. Never posted to GitHub: the issue may be public, and
a chat is a working conversation.

**The same diagram safety rules** (§6.6) apply to every diagram in a turn.
**States:** asking is open while the run is `PLANNED`; after the decision the
transcript is read-only.

### 7.8 The plan-review settings: the chat's model and the caps

BGD, 2026-10-28, on Q4 (Sonnet): *"make this configurable as well workspace lvl
and platform lvl and as a plan setting when it makes sense"*; on Q5 (5 / 20 /
40 / $25): *"configurable in platform"*. This section designs both, beside the
diagram switch of §6.4, so that every plan-review setting is read, recorded
and refused one way.

**Where each setting lives, and who sets it.**

| setting | platform (default) | workspace | schedule | this plan |
|---|---|---|---|---|
| diagrams on or off | `plan_diagrams_default`, `true` | `plans.diagrams` | `plan_diagrams` | (none: the plan's diagrams are in its digest) |
| the chat's model class | `plan_chat_model_class`, `"sonnet"` | `plans.chat_model_class` | (none: see "Why no schedule level" below) | `plan_chat_model` on the run, only when §"When it makes sense" allows |
| the caps | `plan_review_caps`, `{replans: 5, diagram_asks: 20, chat_turns: 40, review_usd: 25.00}` | read-only: shown, never set | (none) | (none) |
| who sets it | a platform admin, Admin › Settings, `PUT /v1/admin/settings` | the workspace's admin (§6.4's rule), the Plan review card on Automate › Settings (PD4, §6.4), `PUT /v1/workspaces/{tid}/plan-settings` | whoever may edit the schedule, its row on Automate › Settings or its edit sheet, its `PATCH` | whoever may edit the plan (§3's edit level), on the decision page, `POST /v1/runs/{run_id}/plan:chat-model` |

The platform keys join `store.PLATFORM_SETTINGS_DEFAULTS`, each with its
reason beside the value as the keys there carry today ("BGD 2026-10-28: Sonnet
by default"; "BGD 2026-10-28: 5 / 20 / 40 / $25"), and
`schemas.PlatformSettingsRequest` gains them. The accepted ranges join
`PLATFORM_SETTINGS_RANGES`'s rule, so an admin's typo written straight to
Firestore reads as the default rather than lifting a limit:
`plan_chat_model_class` is a `MODEL_CLASSES` name that `claude-code` carries
(`model_classes_carried`); `replans` 0 to 20, `diagram_asks` 0 to 100,
`chat_turns` 0 to 200, `review_usd` 0 to 500.00. A cap of 0 is accepted and
means that kind of ask is refused on every run, with the reason below: it is
how a platform admin turns a costly feature off without a deploy.

**Why no schedule level for the model or the caps.** BGD named platform,
workspace and plan for the model, and platform for the caps. A schedule
already has its own spend control (its day budget), and a fourth place a
model can be set is a fourth place to look when a turn ran at the wrong one.

**The chat's model, resolved.** When a turn (or an on-demand diagram, which is
the same builder, §7.2) is submitted, the class is the run's `plan_chat_model`
if set, else the workspace's `plans.chat_model_class` if set, else the
platform's `plan_chat_model_class`. A class the profile does not carry at that
moment (an admin removed it) falls through to the next level, and the turn
records that it fell through and why. The re-plan is not resolved here: it
stays at the run's planner class (§5.1), because it is the planner answering
for its own plan.

**When it makes sense: the per-plan override.** BGD's "as a plan setting when
it makes sense" is read as: a plan may ask for a **stronger** model than its
workspace's when the plan's own reading says the questions about it are
harder, and may always ask for a **cheaper** one.

* *Stronger* and *cheaper* are measured, not listed: a class is stronger than
  another when its list output price per million tokens in the platform's
  `model_prices` (`modelprices`) is higher. A class with no price is treated
  as stronger, so it needs a reason.
* The platform computes **`chat_model_suggestion`** on the run read, outside
  the digest like `plan_risk` (§4 rule 2): `{"suggested": "<class>" | null,
  "why": "<sentence>" | null}`. It suggests the run's planner class when the
  planner class is stronger than the resolved one **and** any of: a step of
  `plan_risk` (§3.7) carries a stop (`iam`, `contract`, `security`,
  `tenancy`, `secrets`, `shared`, `workflows`, `protected_path` or `cost`):
  *"step 2 touches iam and workflows"*; the run's `approval_hold` is set; or
  the plan's diagram rule (§6.3) answers E1 with four or more steps or E2
  across three or more components: *"5 steps across apps/swarm-api,
  apps/agent-worker and terraform"*. Otherwise it suggests nothing:
  *"a one-step plan with no risk paths"*.
* `POST /v1/runs/{run_id}/plan:chat-model {"model_class": "<class>" | null}`,
  at the edit level, while the run is `PLANNED`. `null` clears it. A
  **cheaper** class than the resolved one is always accepted. A **stronger**
  class is accepted only when it is the one `chat_model_suggestion` suggests
  at that moment, and refused otherwise, 422 `chat_model_not_needed`, with
  the suggestion's own sentence: *"this plan's reading suggests no stronger
  model: a one-step plan with no risk paths"*. A class the profile does not
  carry is 422 `unknown_model_class`, listing the ones it does. Recorded on
  the run as `plan_chat_model: {"class", "set_by", "set_at", "why"}` (the
  suggestion's sentence, or "cheaper than the workspace's") and as a
  `plan_thread` event "the chat now answers with opus: step 2 touches iam",
  so the transcript says why the next turn costs more. Not signed and not in
  the digest: it changes what the chat costs, never what was approved.
* Where it shows: in the chat column's header, the model the next turn will
  use and from which level ("sonnet, the workspace's"), and, only when a
  suggestion exists, one button, "Answer with opus: step 2 touches iam". The
  header is the head of the chat's right-hand column (PD3, §2.5).

**How a run records the model each turn used.** Each turn (and each
on-demand diagram entry) records, when it is submitted and when its agent
ends:

```
"model": {"class": "sonnet",                        // the class it was submitted at
          "from": "platform" | "workspace" | "plan",
          "fell_through": "<why>" | null,           // a level's class the profile no longer carried
          "model_id": "<the ID the attempt ran>" | null,
          "why_none": "<why>" | null}
```

`model_id` is the frozen `Attempt.model` (contract request 103) of the turn's
agent's last attempt, which the worker writes at attempt start: what actually
ran, not what was asked for. `null` with `why_none` when the attempt recorded
none, never a guess. The transcript draws it beside each turn's cost, so
"why did this turn cost $1.90" is answered on the line itself.

**The caps: how a workspace sees them.** `GET /v1/workspaces/{tid}/plan-settings`
serves them read-only, each with where it comes from:

```
"caps": {"replans": 5, "diagram_asks": 20, "chat_turns": 40, "review_usd": 25.00,
         "from": "platform", "set_by": "<admin's email>" | null, "set_at": "<iso>" | null}
```

and the Plan review card on Automate › Settings draws them as a sentence
with no input: "Each run may have 5 re-plans, 20 diagram asks, 40 chat turns
and $25 of review agents. Set by a platform admin." Any member reads it;
nobody below a platform admin can change it.

**The caps: what each run has used.** `GET /v1/runs/{run_id}` serves
`plan_review_usage`, computed on read from the run's revisions, thread and
transcript:

```
"plan_review_usage": {
  "replans":      {"used": 2, "cap": 5},
  "diagram_asks": {"used": 7, "cap": 20},
  "chat_turns":   {"used": 12, "cap": 40},
  "review_usd":   {"spent": 6.40, "cap": 25.00, "at_least": true | false},
  "caps_read_at": "<iso>"}
```

The caps are read when the ask is made, not frozen on the run: an admin who
lowers a cap mid-review has lowered it for the next ask on every run, which is
what a cap moved to stop a runaway is for. Each ask records the caps and usage
it was admitted under (`caps` on the turn, the entry and the re-plan), so a
run that went past today's cap under yesterday's says so rather than looking
like a breach.

**At each cap: refused with its reason, never silently.** Every refusal is a
429 with one shape, and the console disables the control before the click
with the same sentence the server would answer:

```
429 {"code": "replan_cap" | "diagram_cap" | "chat_cap",
     "cap": "replans" | "diagram_asks" | "chat_turns" | "review_usd",
     "used": 5, "limit": 5,
     "detail": "This run has had its 5 re-plans. The cap is a platform setting
                (Admin › Settings); reject the plan and re-file it, or edit it by hand."}
```

* **Re-plans** (5): Request changes is refused; comments, the chat and a
  hand edit still work, and the sentence says so.
* **Diagram asks** (20): `plan:diagram` is refused; a chat turn is still
  accepted, told it may not draw, and a diagram in its answer is dropped with
  the line "this run has had its 20 diagram asks". Adding an already drawn
  diagram to the plan is an edit, not an ask, and stays open.
* **Chat turns** (40) and **review dollars** ($25): the ask is refused,
  naming which cap. A turn already running is never cut off when it crosses
  $25: its answer is kept, the spend shows above the cap, and the next ask
  is refused. Cutting it off would throw away an answer already paid for.
* **The tenant's day budget** (`budget.py`) and the per-agent budget (#2459)
  apply on top of all four and refuse with their own reasons, unchanged.
* Nothing is queued, truncated or retried at a cap, and no cap is enforced
  only in the console: each route checks its cap in the same transaction that
  records the ask, so two asks racing for the last turn cannot both pass.

**The $25 counts the review chat's turns and the on-demand diagram agents
only: BGD's answer to Q11, 2026-10-28 (~18:30Z)** (answered). Re-plans are
bounded by their own cap of 5 and by the tenant's day budget, never by the
$25. The on-demand diagram agents are the chat's builder at the chat's model
(§7.2), so they share the chat's dollars, whether asked in the thread or in
the chat. A re-plan is an opus planner run; counting it in the $25 would let
two re-plans spend most of it before anyone asked the chat. So
`plan_review_usage.review_usd` sums the cost of every chat turn and every
`diagram` thread entry of the run and nothing else, and a re-plan's cost is
recorded on its revision (§5.4) and counted in `plan_review_cost_usd` only.

**Where spend against the $25 shows.** In three places, all from
`plan_review_usage.review_usd`: the chat column's header ("$6.40 of $25 ·
at least" when a cost is unreported), amber from 80% ($20) and with the
refusal's sentence at 100%; the decision bar's cost line beside the plan's
expected cost (§3.8), "review so far $6.40 of $25"; and the run's row in Work ›
Runs, which already shows cost, as a second figure. `plan_review_cost_usd`
(§5.4) stays the whole review's total, the planner and re-plans included, so
the two figures are never confused: one is what the review cost, the other is
what counts against the cap.

**Audit.** A platform setting change is recorded as `/v1/admin/settings`
records today; a workspace change is one `admin_audit` entry,
`workspace_plan_settings_set`, naming the keys and both values; a per-plan
override is a `plan_thread` event and the run's `plan_chat_model`.

---

## 8. Who may do what, in one table

Every row is decided by the server; the console reads `may_decide` (§3.9) and
never widens it. "Comment level" and "edit level" are §3's.

| action | route | who | a non-member |
|---|---|---|---|
| read the board | `GET /v1/pipeline` | any member | never reached: `tenant_scope` serves only the caller's own tenant's cards |
| read a plan, revisions, thread, transcript | `GET /v1/runs/{id}`, `…/plan-revisions/{n}`, `…/plan-chat` | any member | 404 |
| comment, resolve own | `plan:comment` | comment level | 404 |
| ask for a diagram | `plan:diagram` | comment level | 404 |
| ask the chat, pin a turn | `plan-chat`, `…:pin` | comment level | 404 |
| edit by hand | `plan:edit` | edit level (today's) | 404 |
| request changes, cancel a re-plan | `plan:revise` | edit level | 404 |
| add a diagram to the plan | `plan:add-diagram` | edit level | 404 |
| approve | `plan:approve`, `/v1/approvals/{id}:approve` | today's who-checks: the gate's approvers, the hold, `second_member` (a revision's requester counts as its editor, §3.3), platform scope | 404 |
| reject | `plan:reject`, `/v1/approvals/{id}:reject` | **only someone who may approve** (`reject_rule`, BGD 2026-10-26) | 404 |
| switch diagrams, set the chat's model, for a workspace | `PUT /v1/workspaces/{tid}/plan-settings` | the workspace's admin, as merge settings | 404 |
| read a workspace's caps | `GET /v1/workspaces/{tid}/plan-settings` | any member | 404 |
| switch diagrams for a schedule | the schedule's `PATCH` | whoever may edit the schedule | 404 |
| set the platform's diagram default, chat model and caps | `PUT /v1/admin/settings` | a platform admin | 403, as today |
| override the chat's model on one plan | `plan:chat-model` | edit level, within §7.8's rule | 404 |

The approve and reject rows are unchanged; this design adds no path to a
decision.

---

## 9. The frozen contract: untouched

Checked field by field against `apps/common/swarm_common/`:

| what this design uses | where it lives | frozen? |
|---|---|---|
| the run, its plan, `PlanSpec`, `PlanStep`, revisions, thread, chat | `issueruns.py` and its subcollections | no: "its shape is not the frozen contract's" |
| approvals, `may_decide`, `GET /v1/pipeline` | `approvals.py`, `routes/approvals.py` | no |
| `workspace_settings/{tid}.plans` | `plansettings.py` (new) | no: `Tenant` is frozen and gains nothing, as for merge settings |
| the platform's plan-review keys | `store.PLATFORM_SETTINGS_DEFAULTS` (`control/settings`) | no: a swarm-api document |
| the model a turn ran | `Attempt.model` (contract request 103) | frozen, **read as it is** |
| schedule `plan_diagrams`, type `proposes` | `schedules.py`, `scheduletypes.py` | no |
| the re-plan, diagram and chat agents | `TaskCreate` on profile `claude-code` with `model_class` and `metadata` | the types are frozen and **used as they are**: `claude-code` declares the `issue` input these send; `model_class` is accepted (`model_classes_carried`); `metadata` is free-form |
| the live stream | `logs/live/agent_stdout.tail.log` (#184) | read as it is published |

So the build needs **no contract change**. One is written down now so a later
choice does not start from nothing:

* **R1 (not built: Q3 picked one agent per question on 2026-10-28; kept for a
  later change of mind).** A review-agent session needs a
  runner that waits for the next question between turns, which means a
  declared runner input on the frozen catalogue (contract request 25's rule:
  "an input means something only to the runner that reads it") and a
  worker-only route for the next question. That is an entry in
  `docs/contract-change-requests.md` and an owner decision, not a lane of
  this plan. The per-question shape BGD picked (Q3) needs neither.

---

## 10. Invariants

1. **Only `LEASED`…`RUNNING` create demand.** A `PLANNED` run, a pending
   approval, a comment, a revision document, a transcript and a board card
   are documents. The re-plan, diagram and chat agents are demand only while
   they run, like the planner; none waits for a person while holding a lease.
2. **All-or-nothing reservation** and 3. **concurrency from `LEASED`:** every
   agent here is admitted by the ordinary path; nothing reserves capacity on
   its own.
4. **No sleeping through a long wait.** The chosen chat shape (Q3) has no
   wait: one agent per question. The session shape is refused here for exactly
   this reason unless it parks.
5. **Fencing** is untouched: every agent is an ordinary attempt.
6. **Spot** is untouched. 7. **Requests and limits** come from the profile.
8. **Checkpointing** is the runner's own, unchanged.
9. **Per-tenant isolation:** §3's rule, §7.3's.
10. **Callers pick a profile by name and nothing else.** A comment, an ask, a
    chat question and a diagram are text; the plan schema has no field for an
    image, a command, a resource or a backend, and §6.2 adds none. The model
    class of every agent here is resolved by the platform from its settings
    (§7.8). The per-plan override names a class **by name**, from the ones
    the frozen profile already carries, as `RunCreate.model_class` does
    today; it never names a model ID, an image or a resource, and a stronger
    class is accepted only on the platform's own suggestion.

---

## 11. Build plan

API lanes first, each with red-first tests (pushed alone to the PR branch,
failing on main, then the change). UI lanes touch at most about three console
files each, tests aside. Lane names are proposals; the orchestrator dispatches
a lane only after its dependencies merged.

### API

**P1 PLANS-SOURCE-PIPELINE.** `run.source` (§3.1) and `GET /v1/pipeline`
(§3.2). Territory: `issueruns.py` (field, `to_api`, derivation),
`schedtypes/issue_sweep.py` and `schedtypes/failure_review.py` (write it at
start), `routes/runs.py` (`hand`), `approvals.py` (`pipeline`),
`routes/approvals.py` (route). Red first: a sweep run, a failure-review run
with a fingerprint and a hand run each serve their `source`; an old run with
`schedule.type: issue-sweep` derives `sweep`; one fixture of every state of
§2.1's table lands in its one stage, never two; a pending merge folds into its
run's card; another tenant's runs and approvals never appear; a failed approvals
read answers `partial` with the runs still served; `by_stage.hidden` counts a
filter's hidden cards. Tests: `tests/unit/control_plane/test_plans_pipeline.py`.
Depends on nothing.

**P2 PLANS-REVISIONS.** `plan_revisions` (§3.3), `rationale` (§3.6), the
digest rules of §4. Territory: `issueruns.py`, `routes/runs.py`. Red first:
every stored plan fixture in the repository's tests hashes to its stored
digest after the change (old runs' digests verify); a plan with `rationale`
digests differently from the same plan without; the planner's plan writes
revision 1, `plan:edit` writes revision N+1 with `replaces`, in the same
transaction (a failed transaction writes neither); a revision document whose
plan does not hash to its digest is served flagged, never as valid; an old run
serves one synthesised revision with `history_complete: false`;
`GET …/plan-revisions/{n}` is 404 across tenants. Tests:
`test_plan_revisions.py`, plus the planner-prompt bytes test updated for
`rationale` in `_PLAN_SHAPE`. Depends on nothing.

**P3 PLANS-THREAD.** `plan_thread` and `plan:comment` (§3.4), derived events.
Territory: `issueruns.py`, `routes/runs.py`. Red first: a viewer's comment is
refused with the role floor's refusal; a member's is stored with `on_digest`;
an unknown digest is 409 `plan_changed`, an older revision's is accepted; an
anchor not in that revision is 422; a comment changes neither `plan_digest`
nor `plan_revision`; the served thread merges events in time order (approved,
step agents, pull request, CI rounds, review, merged, keyword) from a DONE
fixture; another tenant's thread is 404. Tests: `test_plan_thread.py`. Depends
on P2 (revisions to anchor to).

**P4 PLANS-REVISE.** `plan:revise` and the round trip (§5). Territory:
`issueruns.py` (prompt sections, answer reading, the revision write),
`routes/runs.py` (route, submission, cancel), `approvals.py` (`_planned_at`
counts from the newest revision). Red first: the re-plan agent is `claude-code`
at the run's planner class with `metadata.replan`; comments reach the prompt
between nonce delimiters; a valid answer makes revision N+1 with
`plan_edited_by` the requester and `answered_by_revision` on each comment; the
requester is excluded by a `second_member` hold; an answer to a moved plan
makes no revision and one "not applied" entry; a refused plan, a failed agent
and a NOT_READY answer each leave the run `PLANNED` with one answer entry;
approve during a re-plan approves the shown digest and cancels the agent (Q7);
edit during one is 409 `replan_running`; a member who may comment but not edit
is refused (Q6); with `plan_review_caps.replans` at its default the 6th
re-plan is 429 `replan_cap` in §7.8's shape, naming the cap and where it is
set, and with the setting at 2 the 3rd is; the revision's `cost` is the
agent's `cost_usd_total`, null with `why_none` when unreported; `expires_at`
moves to the newest revision. Tests: `test_plan_revise.py`. Depends on P2, P3,
P9.

**P5 PLANS-DECIDE-READS.** `plan_risk` (§3.7), `expected_cost_usd` (§3.8),
`may_decide` with `edit`/`comment` on the run read (§3.9), `proposals_7d`
(§3.10), `proposes` on schedule types (§2.4). Territory: `issueruns.py`
(`step_holds`, `expected_cost`), `routes/approvals.py` (`may_decide`),
`routes/runs.py` (serve them), `approvals.py` (`proposals_by_schedule`),
`scheduletypes.py`. Red first: a step touching `.github/workflows/x.yml` gets
`workflows` and its path, and its sibling step gets none; the per-step stops
match `plan_hold` over the same files (the union of the steps' rows equals
the run's hold for a fresh plan); an estimate "2 agent-hours" yields a range
and one in words yields `why_none`, never 0; `may_decide` on the run read
equals the approval read's for the same reader; `proposals_7d.complete` is
false past the bound. Tests: `test_plan_decide_reads.py`. Depends on nothing.

**P6 PLANS-DIAGRAMS-API.** §6: `PlanDiagram`, `no_diagram`, the schema rule,
`check_diagram_source`, `diagram_expected`, `_DIAGRAMS` in the prompt,
`plan_settings` on the run (read through P9's resolution), `plan:diagram`,
`plan:add-diagram`. Territory: `issueruns.py`, `routes/runs.py`. Red first: an old plan without either
field keeps its digest; with diagrams on, a plan with neither is refused and
one with both is refused; with diagrams off, the prompt is byte for byte
today's and a plan carrying `diagrams` is refused naming it; every refused
token of §6.6 is refused naming its line, and each example of §6.3 gets its
rule's answer; a run records the setting it planned under and keeps it when
the workspace's setting changes later (Q2); `plan:diagram` is 409
`diagrams_off` naming the level; a member who may comment but not edit is
refused (Q6); a diagram answer changes no digest; `plan:add-diagram` makes
revision N+1 and clears `no_diagram`; with the default caps the 21st ask is
429 `diagram_cap` in §7.8's shape. Tests: `test_plan_diagrams.py`. Depends on
P2, P3, P9, and on
P7's agent builder for `plan:diagram`'s agent — so P6 lands the field, the
checks, the settings and `add-diagram` first (P6a) and `plan:diagram` after
P7 (P6b).

**P7 PLANS-CHAT-API.** §7: `planchat.py` (turns, the review-agent
builder and prompt, answer reading and checks, cost and caps), routes in
`routes/runs.py` (ask, list, stream, pin). Territory: new `planchat.py`,
`routes/runs.py`. Red first: the agent is `claude-code` at `sonnet` with no
setting anywhere (Q4's default), at the workspace's class when the workspace
sets one, and at the platform's when only the platform does; in the run's
tenant, as the asker, with `metadata.plan_chat`; its prompt carries the
plan, revisions, issue, index sections, thread and transcript between nonce
delimiters and nothing of another run; a second ask in flight is 409
`chat_busy`; with the default caps the 41st turn is 429 `chat_cap` naming
`chat_turns`, and a run whose chat turns and diagram agents together reported
$25.00 is refused naming `review_usd`, and one whose re-plans alone cost
$25.00 is not (Q11); a turn running when the spend crosses
$25 keeps its answer; a turn records `model.model_id` from its attempt's
`Attempt.model`, and `null` with `why_none` when the attempt has none; a turn
asked below the edit level drops its diagrams with §6.5's line; an
answer's invalid diagram is dropped with its reason and a valid proposal is
kept with no revision made; the stream emits only text deltas, redacted (a
fixture tail with a tool result and a fake token built at runtime yields
neither); a viewer's ask is refused and a viewer reads the whole transcript
(Q10); pin makes a `chat_pin` thread entry. Tests: `test_plan_chat.py`.
Depends on P3, P6a, P9.

**P9 PLANS-SETTINGS-API.** §6.4 and §7.8's settings, the one place every
other lane reads them from. The platform keys `plan_diagrams_default`,
`plan_chat_model_class` and `plan_review_caps` in
`store.PLATFORM_SETTINGS_DEFAULTS` with their ranges and reasons, and in
`schemas.PlatformSettingsRequest`; `plansettings.py` (the workspace's
`plans.diagrams` and `plans.chat_model_class`, the caps served read-only, and
`effective_plan_settings(tenant_id, schedule)`, which resolves each setting to
its value and the level it came from); its routes; schedule `plan_diagrams`.
Territory: `store.py`, `schemas.py`, new `plansettings.py`, new
`routes/plan_settings.py`, `schedules.py`, `main.py` (router). Red first: with
nothing set, diagrams are on from `platform`, the chat model is `sonnet` from
`platform` and the caps are 5 / 20 / 40 / 25.00; a platform admin's PUT
changes each and a workspace admin's is 403; a stored cap outside its range or
of the wrong type reads as the default; a class `claude-code` does not carry
is refused on save at both levels; the workspace PUT is a workspace admin's,
MAC'd and audited, and a no-op writes nothing; the workspace PUT refuses a
`caps` key, naming it as the platform's; the schedule override wins over the
workspace and the workspace over the platform, and `inherit` reports the
level it inherited from; any member reads the workspace's caps. Tests:
`test_plan_settings.py`. Depends on nothing.

**P10 PLANS-CHAT-MODEL-API.** §7.8's per-plan override and the run's usage:
`chat_model_suggestion` and `plan_review_usage` on the run read, `POST
plan:chat-model`, the `plan_chat_model` thread event. Territory: `planchat.py`,
`routes/runs.py`. Red first: a plan with a step on `iam` suggests the
planner's class with the step named, and a one-step plan with no risk paths
suggests nothing, with its sentence; a cheaper class is accepted on any plan;
a stronger class without the suggestion is 422 `chat_model_not_needed` with
the suggestion's sentence; a class the profile does not carry is 422; a
member who may not edit is refused; the next turn is submitted at the
override and records `from: "plan"`; clearing it returns to the workspace's;
`plan_review_usage` counts re-plans, asks, turns and review dollars against
the caps read now, with `at_least` when a cost is unreported, and
`review_usd` sums chat turns and `diagram` entries only, a re-plan's cost
never (Q11); a stronger
class is treated as such by its `model_prices` output price, and an unpriced
class needs the suggestion. Tests: `test_plan_chat_model.py`. Depends on P5
(`plan_risk`), P7, P9.

**P8 PLANS-DOCS.** docs/issue-runs.md (revisions, thread, Request changes,
the chat, diagrams), docs/schedules.md §6.1 (the Pipeline tab),
docs/web-ui (the board), `.github/ISSUE_TEMPLATE/bug_report.yml` and
`feature_request.yml` "Where" lists gain "Web UI · Automate › Pipeline
(/automate/pipeline)" — `tests/unit/scripts/test_issue_forms.py` fails the
moment U1 adds the tab without them, so P8's form change lands **with U1**, in
U1's pull request (stated in U1); the same holds for Automate › Settings,
whose form line lands with U11. docs/issue-runs.md also gains the
plan-review settings (§7.8: the three levels, the caps, the refusals), and
docs/web-ui the Automate › Settings tab and the decision page's strip and
chat column (§2.5). Depends on P1-P10 for the prose.

### Console

**U1 PIPELINE-BOARD.** The board (§2.1, `C:P1a`-`C:P1c`, `C:P6a`-`C:P6d`,
`C:P7a`). Territory: new `apps/swarm-ui/src/Pipeline.tsx`, new
`styles/pipeline.css`, `App.tsx` (the tab in `SECTIONS`, `keptKeys`), plus
`api.ts`'s `loadPipeline`, and the two issue forms' "Where" line (P8). Red
first: `pipeline.board.test.tsx` — each stage draws its served cards; a
waiting card is amber and first; a source filter keeps columns and shows
hidden counts; a `partial` read shows the region error in its column only; a
404 shows the not-served state with Approvals linked; the address carries the
filters. Depends on P1.

**U2 APPROVALS-KEEPS.** Approvals keeps `run`, `spec`, `input` and the "N on
the board" line (§2.2). Territory: `Approvals.tsx`. Red first: a `plan`,
`hold`, `merge` or `proposal` item is not listed; the count equals the
pipeline read's `waits_on_person`; a link to `/automate/approvals/<id>` of any
kind still opens its page. Depends on P1, U1.

**U3 DECISION-DOCUMENT.** D's page: the document, the margin's reasons, the
risk rows, expected cost, the decision bar with revision and digest, `D:P2a`,
`D:P2b`, `D:P2c`, `D:P6d`, `D:P7b`, Back to the board with its filters.
Territory: new `PlanDocument.tsx`, `ApprovalDecision.tsx` (mount it for
`plan`/`hold`), new `styles/plan-document.css`. Red first:
`plan.document.test.tsx` — the bar names revision and digest; a member who may
not decide sees no Approve, Edit or Reject and the deciders sentence; a poll
with a new digest keeps the shown revision and removes its Approve; a run
read without `plan_revisions` draws the plan and today's buttons; Back keeps
`source`/`state`. Depends on P2, P5.

**U4 PLAN-THREAD-UI.** The margin's thread, Comment, Request changes, the
revision picker and diff (`D:P3a`, `D:P4a`-`D:P4c`). Territory: new
`PlanThread.tsx`, new `PlanRevisions.tsx`, `PlanDocument.tsx` (mount). Red
first: a comment posts with the shown digest and its anchor; Request changes
shows "re-planning" and then the new revision's diff and cost; approve during
a re-plan says it cancels it; events after approval draw in order; resolved
comments fold. Depends on P3, P4, U3.

**U5 PLAN-EDITOR-MOVE.** Today's `PlanEditor` moves out of `Runs.tsx` into
`PlanEditor.tsx`, gains Remove step, per-step rationale and diagram remove,
and is used by the decision page (`D:P3b`, `D:P3c`); Work › Runs' plan view
becomes a summary linking to the decision page, and a planned run's row shows
its review spend against the cap ("review $6.40 of $25", §7.8) beside its cost. Territory: `Runs.tsx`, new
`PlanEditor.tsx`, `PlanDocument.tsx` (mount). Red first: Remove is disabled on
the last step with its reason; an edit preserves fields the editor does not
show (the `...plan` spread, held by a test with `diagrams` and `rationale`);
Save sends the digest it replaces; Runs links a PLANNED run to its page; a run
read without `plan_review_usage` shows no review figure rather than $0.
Depends on U3.

**U6 PLAN-SOURCES.** Schedules narrowed to the schedules that propose, with
gates as stored, spend, `proposals_7d` (`D:P5a`; the merge switch as today).
Territory: `Schedules.tsx` (the filter and the columns),
`styles/schedules.css`. Red first: `?proposes=1` lists only `proposes` types;
an unknown count is a dash with its reason; no gate is drawn that the read
does not serve. Depends on P5.

**U7 DIAGRAM-VIEW.** `DiagramView.tsx` (§6.6): parse, strict render, sanitise,
`<img>`, themes, text outline, export, the failure state; `package.json`
(pinned `mermaid`, `dompurify`). Territory: new `DiagramView.tsx`, new
`styles/diagram.css`, `package.json`. Red first: `diagram.view.test.tsx` —
a source with `click`, a `%%{init}` directive or an HTML label is shown as
source with its reason and never rendered; a parse failure shows the source
and the reason, never a blank; the rendered output is an `<img>` whose SVG has
no `script`, `foreignObject`, `a` or `on*`; the outline lists every edge of a
fixture flowchart; switching the theme changes the image. Depends on nothing
(it renders a source), so it can run beside the API lanes.

**U8 DIAGRAMS-ON-PAGE.** §2.5 as BGD picked it (PD1, PD2: C): the
"Pinned to the plan" strip above the document (one diagram at a time, tabs
naming each one's anchor, Open large; `DC:D1a`, `DC:D1b`), the planner's
opening message naming the diagrams with links to their tabs or carrying the
"no diagram, because …" line (`DC:D2a`), Ask for a diagram beside Comment in
each margin composer (offered only when `may_decide.edit`, Q6, with the cap's
sentence when it is reached), the diagram message and its Pin to the plan
(`DC:D3a`-`DC:D3c`), the "diagrams only" revision chip, the phone's tall
layout (`DC:D7a`, `DC:D7b`), and this run's setting when off, naming its level
(`DC:D6c`). The settings are edited by U10-U12, not here. Territory:
`PlanDocument.tsx`, `PlanThread.tsx`, new `PinnedDiagrams.tsx`. Red first:
`plan.diagrams-strip.test.tsx` — a revision with two diagrams draws one and
names both on tabs; a revision with none has no strip and the opening message
carries `no_diagram`; a diagram entry is not on the strip until Pin to the
plan returns revision N+1, then it is, and the entry links to its tab; a
member below the edit level sees no Ask for a diagram; a run planned with
diagrams off shows the off line naming its level and no ask. Depends on P6,
U4, U7.

**U9 PLAN-CHAT-UI.** The chat as a right-hand column (PD3, C;
`DC:D8a`-`DC:D8c`, `DC:D7c`): ask, stream, each turn's cost and the
model it ran (`model.model_id`), the header's "$6.40 of $25" from
`plan_review_usage` (amber from 80%, the refusal's sentence at a cap, the ask
disabled with it), pin, a proposal's diff with Request changes / Edit with
this. Territory: new
`PlanChat.tsx`, new `styles/plan-chat.css`, and its mount point. The component
is built against P7 with its own tests now, so the lane splits: U9a (the
component, testable alone) and U9b (its mount on the decision page: the
button in the page head that opens and closes the column, the margin folded
under each part and the strip folded to its tabs while it is open, the sheet
over the plan on the phone). U9b's territory adds `PlanDocument.tsx` and
`styles/plan-document.css`; red first, `plan.chat-column.test.tsx` — the
column is closed on load; opening it folds each part's reason and thread
under that part and the strip to its tabs, and closing it restores the
margin; at 390 it opens as a sheet. Depends on P7, U7; U9b on U3, U4, U8 and
U9a.

**U10 PLATFORM-PLAN-SETTINGS.** Admin › Settings gains a Plan review group:
the diagram default (a switch), the chat's model class (a choice of the
classes `claude-code` carries) and the four caps (whole numbers, and dollars
to the cent), each with its range and its reason, saved through `PUT
/v1/admin/settings` as the page's other settings are. Territory:
`AdminSettings.tsx`, `api.ts` (`PlatformSettings`'s save), `types.ts`
(`PlatformSettings`). Red first: `admin.plan-settings.test.tsx` — the defaults
read 5 / 20 / 40 / $25, sonnet, on; a cap outside its range is refused
before the save with the range; a save sends only the keys changed; a
non-admin never sees the group. Depends on P9.

**U11 AUTOMATE-SETTINGS.** The new Automate › Settings tab (PD4, B;
`DB:D5a`, `DB:D5b`), after Approvals, at `/automate/settings`. Its Plan
review card: the diagram switch (on / off / the platform's) with BGD's
examples of where the planner draws and where it does not, the chat's model
class (a class / the platform's), each saying what it inherits, and the caps
as a read-only sentence naming who sets them. Under it, the schedules whose
type `proposes`, each with "follow the workspace / on / off", saved through
the schedule's `PATCH` (the same field as U12's), and the line that an issue
run submitted by hand follows the workspace. Territory: new
`AutomateSettings.tsx`, new `PlanSettingsCard.tsx`, `App.tsx` (the tab in
`SECTIONS`), `api.ts` (`loadPlanSettings`, `savePlanSettings`), and the two
issue forms' "Where" line "Web UI · Automate › Settings (/automate/settings)",
in this lane's pull request because `tests/unit/scripts/test_issue_forms.py`
fails the moment the tab is added without it. Red first:
`automate.settings.test.tsx` — an admin changes each and a member sees them
read-only; "the platform's" shows the platform's value; the caps have no
input for anyone; a refused save shows the server's reason; a schedule row's
override sends the schedule's `PATCH` with `plan_diagrams` only; a type that
does not propose has no row. Depends on P9, P5 (`proposes`) and U12
(`plan_diagrams` on the schedule in `types.ts`, which U12 adds).

**U12 SCHEDULE-PLAN-DIAGRAMS.** The schedule's edit sheet gains "Diagrams in
plans: inherit / on / off", shown only for a type that `proposes`, naming
what `inherit` resolves to now. Territory: `ScheduleEdit.tsx`, `types.ts`
(`plan_diagrams` on the schedule). Red first: `schedule.plan-diagrams.test.tsx`
— a sweep schedule shows it and a type that does not propose does not; the
PATCH sends `plan_diagrams` only when changed. Depends on P9 and P5
(`proposes`).

**U13 PLAN-CHAT-MODEL.** The per-plan model override inside the chat's
header: the model the next turn uses and from where, the "Answer with opus:
step 2 touches iam" button when `chat_model_suggestion` suggests one, a
cheaper choice, and clearing it; and the decision bar's "review so far $6.40
of $25". Territory: new `ChatModelControl.tsx`, `PlanChat.tsx` (its header),
`PlanDocument.tsx` (the bar's line). Red first: `plan.chat-model.test.tsx` —
no suggestion, no stronger choice is offered; a suggestion offers exactly its
class with its sentence; a member who may not edit sees the model but no
control; a 422 shows its sentence. Because it lives inside the chat
component, it sits in the head of the chat's column (PD3). Depends
on P10, U9a, U3.

**Order.** P1, P2, P5, P9 and U7 start at once; P3 after P2; P4 after P3 and
P9; P6a after P3 and P9; P7 after P6a; P6b and P10 after P7; U1 after P1
(with P8's form line); U2 after U1; U3 after P2 and P5; U4 after U3 and P4;
U5 after U3; U6 after P5; U10 after P9; U12 after P9 and P5; U11 after U12; U8
after P6, U4 and U7; U9a after P7 and U7, U9b after U3, U4, U8 and U9a; U13
after P10, U9a and U3; P8 last. No lane waits on an owner question.

---

## 12. Owner questions

BGD answered Q2-Q10 on 2026-10-28, and Q1 and Q11 the same day at about
18:30Z (Q1 recorded on #2585 at 18:33Z); each answer is written into the
section named beside it, and its lanes in §11 are built to it. No owner
question is open.

1. **Q1 Where diagrams, the on-demand ask, the chat and the diagram switch's
   state sit on D's page.** **Answered 2026-10-28 (~18:30Z): C, C, C, B,**
   picked from the hand-drawn mock-ups `plans-diagrams-a.html`, `-b.html` and
   `-c.html` (PLANS-DIAG-A, B, C; PRs 2640, 2643, 2642). PD1, C: the planner's
   opening message lists the diagrams it drew (or one line saying why it drew
   none), and a "Pinned to the plan" strip at the top of the decision page
   shows one at a time on tabs, each anchored to the plan or a step. PD2, C:
   the reviewer asks in the thread anchored to a step, the answer is a diagram
   message, and Pin to the plan makes a revision whose digest covers the
   diagram set and puts it on the strip. PD3, C: the chat is a right-hand
   column opened from the page head; while it is open the margin's reasons
   and thread fold under each part, as on the phone. PD4, B: a new Settings
   tab under Automate holds the workspace default (on), and each schedule
   that plans follows it or overrides it on or off. Written into §1, §2.3,
   §2.5, §6.4, §6.5, §7 and §7.8; lanes U8, U9 (U9b no longer waits), U11
   (now Automate › Settings), U13 and P8.
2. **Q2 Where the diagram switch lives.** **Answered 2026-10-28: platform,
   workspace and schedule; each level overrides the one above, and a run
   records the setting it planned under.** Written into §6.4 (the platform
   level became a platform admin's setting) and lanes P9, U10, U11, U12.
3. **Q3 How the review chat runs.** **Answered 2026-10-28: one agent per
   question.** Written into §7.1; R1 (§9) stays a record, not a lane.
4. **Q4 The chat agent's model.** **Answered 2026-10-28: Sonnet by default,**
   and *"make this configurable as well workspace lvl and platform lvl and as
   a plan setting when it makes sense"*. Written into §7.1 and designed in
   §7.8: platform and workspace levels, a per-plan override by an editor
   (cheaper always; stronger only on the platform's `chat_model_suggestion`,
   for a plan touching risk paths, held, or large), and each turn records the
   class, its level and the model ID it ran. Lanes P7, P9, P10, U10, U11, U13.
5. **Q5 The caps per run.** **Answered 2026-10-28: 5 re-plans, 20 diagram
   asks, 40 chat turns and $25,** and *"configurable in platform"*. Designed
   in §7.8: a platform admin's setting with these defaults, read-only to a
   workspace, every cap refused with its reason and never silently, the spend
   shown in the chat header, the decision bar and the run's row. Written into
   §5.4, §6.5, §7.1. Lanes P4, P6, P7, P9, P10, U5, U9, U10, U11, U13.
6. **Q6 Who may ask for a re-plan or a diagram.** **Answered 2026-10-28:
   whoever may edit the plan today.** Written into §3's levels, §5.1, §6.5,
   §7.6 and §8; a diagram ask moved from the comment level to the edit level,
   and a chat turn asked below it draws nothing. Lanes P4, P6, P7, U8.
7. **Q7 An open Approve while a re-plan runs.** **Answered 2026-10-28:
   Approve stays valid for the revision on screen and cancels the re-plan.**
   Written into §5.3; lanes P4, U4 unchanged.
8. **Q8 Diagrams inside the plan digest.** **Answered 2026-10-28: inside;**
   an approved plan's diagrams are the ones approved, and old runs' digests
   still verify. Written into §4; lanes P2, P6 unchanged.
9. **Q9 The diagram language.** **Answered 2026-10-28: Mermaid,** with §6.6's
   sandboxing. Written into §6.2; lanes P6, U7 unchanged.
10. **Q10 Who sees the chat transcript.** **Answered 2026-10-28: every member
    of the workspace, kept with the run.** Written into §7.6 and §7.7; lane P7.
11. **Q11 What the $25 counts.** **Answered 2026-10-28 (~18:30Z): the
    review chat's turns and the on-demand diagram agents only.** Re-plans are
    bounded by their own cap of 5 and the day budget. Written into §5.4,
    §7.1 and §7.8; lanes P7 and P10 (`review_usd` never counts a re-plan).
