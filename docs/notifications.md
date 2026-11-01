# Notifications

What `GET /v1/notifications` serves, where each item comes from, and why the
feed is derived rather than stored. Issue #1206 (proposal A, picked by the
owner on 2026-10-12): every person gets a bell in the console's header, and
admins get a count on Admin. Lane NOTIF-API built the API
(`apps/swarm-api/swarm_api/notifications.py`, `routes/notifications.py`);
NOTIF-UI draws it.

## The API

```
GET  /v1/notifications            -> {items, unread_needs_you, unread_updates,
                                      admin_pending, tenants_waiting,
                                      platform_secrets_unset, muted, read_at, partial}
POST /v1/notifications/read       {"ids": [...]} -> {"ok": true}
POST /v1/notifications/read-all   -> {"ok": true}
```

Each item is `{id, group, topic, needs_you, title, detail, href, at, read}`. `href`
is a console path (`/admin/people`, `/agents/live/<task>/browser`). The feed
lists the needs-you items first, oldest first, and then the updates, newest
first: at most 50, all from the last 7 days.

`read_at` is when the person last pressed "mark all read". It is `null` if
they never have. `partial` is `true` when at least one source could not be
read: the feed still serves the other sources, and the failure is logged
under the source's name. It is also `true` when the person's preferences could
not be read (see below). `muted` is `{needs_you, updates}`, how many items the
person's muted topics took out of the list.

## Why derived, not stored

Every item is computed on each read from a record that another path already
writes. Nothing writes a notification. So **a "needs you" item cannot outlive
its cause**. Once the workspace request is approved, the plan decided or the
hand-off taken, the next read does not derive the item, and it is gone from
the feed and from the bell's count.

A stored notification would need a second writer on every path that resolves
one: approve, deny, lend, decide, take control, reconnect. Each writer someone
forgot would be a bell that never clears. Each writer added would be one more
thing on the admission, approval and hand-off paths that can fail. Deriving
the feed keeps those paths exactly as they were.

The cost is that a read runs every source, which is a handful of bounded
queries (each source reads at most 200 documents). The console reads the
feed when it draws its header, not in a tight loop.

## The one document it owns: read marks

`notification_reads/rd_<hash>` exists once per person. `<hash>` is
`forgeapp.user_hash` of their address, so the document id names nobody. The
document holds:

* `marks`: `[{id, at}]`, the ids the person marked read and when. Marks older
  than 30 days are pruned on the next write, and at most 2,000 are kept.
* `all_read_at`: the moment of their last "mark all read". Any item dated at
  or before it reads as read.

It never holds a title, a detail or a link. The feed is rebuilt from the
records each time, and the read marks only say which ids have been seen.

That is why **ids are stable**. The same underlying thing derives the same id
on every read, for example `admin:workspace:<workspace id>:<request id>` or
`task:handoff:<task>:<generation>:<request id>`. A new occurrence of the same
thing derives a new id, which starts unread: a request made again after a
denial, a plan revised, a second hand-off in the same attempt or a later one.

## The groups

| group | needs you | what derives it | where it links |
|---|---|---|---|
| `admin` | yes, admins only | `workspaces` in `requested`, `loan_requests` in `requested`: what Admin › People lists first | `/admin/people` |
| `approval` | yes | the tenant's pending `approvals` records (an expired one, or a merge whose run has moved on, is skipped); `schedule_firings` held for approval before the inbox made their record; `issue_runs` in `PLANNED` | `/automate/approvals/<id>` (a held firing with no record yet: `/automate/approvals`; the console redirects the old `/approvals` links already sent) |
| `task` | yes | "Agent needs you": RUNNING agents with a live-browser hand-off (`Task.human_wait`) the caller may take control of -- their own, and the tenant's while "Let any member take control" is on (docs/design/live-browser.md §4.1) -- and the caller's PARKED `HUMAN_REQUIRED` agents; the tenant's workflow steps PARKED `AWAITING_INPUT` whose `remind` names the caller (below); the caller's agents of the last 7 days whose agent wrote `questions.json` (`result_summary.questions`) | the agent's Browser tab; the workflow; its Artifacts tab |
| `account` | yes for GitHub, no for the workspace | the caller's `forge_connections` document in `refresh_failed` (only reconnecting renews it); their own expired user-scope `git_tokens`; their own workspace becoming `ready` or `denied` in the last 7 days; their own Claude-account loan request `declined` in the last 7 days (LOAN-DECLINE) | `/access`; `/setup#workspace`; `/setup#claude-account` |
| `finished` | no | the caller's own agents submitted alone, their workflows (stored state), and their issue runs whose pull request merged, all ended in the last 7 days | the agent, workflow or run |

Some notes on what each group deliberately leaves out:

* **Approvals are narrowed to what this person may decide.** An `owner_only`
  item reaches only the platform owner. A named list reaches only the people
  on it. A `second_member` hold does not reach the run's creator or its last
  plan editor. This is only a filter on what the feed shows. The decision is
  still checked when it is made (`approvals.check_gate_approver`,
  `check_hold`).
* **The feed reads the approval inbox without its writes.** `approvals.inbox`
  turns waiting firings into records and closes stale merge records as it
  reads. The feed does neither. A waiting firing gets the id its record will
  have, because `approval_id` is deterministic, so a read mark survives the
  record being made.
* **A question item goes once every question is answered.** An agent's
  `questions.json` is data for a person (`agent_worker/questions.py`). The
  person answers on the agent's "Questions for you" card
  (`POST /v1/tasks/{id}/answers`, TASK-ANSWERS, owner decision 2026-10-13), and
  the answer is stored on the agent as `metadata.owner_answers`. The item derives
  only while at least one counted question has no answer. Until then it stays
  for 7 days after its agent ended, and marking it read clears it from the bell's
  count.
* **A declined loan request does not count as needing you either.** Nothing
  the person does resolves it. It carries the admin's optional reason and
  never who declined. Asking again re-opens the request, and the item no
  longer derives.
* **A workspace becoming ready, or being denied, does not count as needing
  you.** Nothing the person does resolves it, so as a needs-you item it would
  sit in the bell until marked read. It is an update in the `account` group,
  with the denial's reason, which is shown to the person by design
  (docs/workspaces.md §1.3). It never says who decided.
* **The steps of a workflow are not reported one by one.** The workflow is
  reported once. A child agent is reported by its parent.
* **Release IAM holds are left out.** A release waiting on an IAM grant is a
  GitHub environment approval, and the platform keeps no record of it in its
  own stores, so there is nothing to derive an item from. An issue run's IAM
  hard stop is different: it is a hold on the run (`issueruns.STOP_IAM`), so
  it already appears as an `approval` item of kind `hold`.

## A workflow step waiting for a person

A workflow step on the `await-input` profile waits, `PARKED(AWAITING_INPUT)`,
for any member of its tenant to answer, for at most 24 hours
(docs/workflows.md, "Waiting for a person"; the design is
docs/design/human-steps.md §4.5). Two surfaces derive it, and neither stores
or sends anything (the owner's Q7):

* **The bell rings the step's `remind` list, not the whole tenant**
  (`apps/swarm-api/swarm_api/notifications.py::awaiting_input`). A reader in
  the step's tenant whose address is on `remind` (by default the submitter)
  gets one needs-you item in the `task` group, topic `waiting_step`, linked to
  the workflow. Its title says what is asked, by the step's form: "A workflow
  is waiting for a review" (`verdict`), "A workflow is waiting for answers to
  its questions" (`answers`) or "A workflow is waiting for a written answer"
  (`text`). Every member being rung for every review would teach people to
  ignore the bell, and the rest of the tenant sees the wait in the inbox. An
  address named on `remind` from outside the tenant rings nobody, because the
  step is read under the tenant the reader acts in. In a personal workspace
  the reader sees only the steps they submitted, as with hand-offs.
* **The id carries the reminder period.** It is `task:input:<task>:<k>`, with
  `k = floor((now - opened_at) / remind_after_seconds)`, or 0 when the author
  set no period. Each period therefore derives a new id, which is unread, and
  nothing is written to make the reminder. A step reminding every 4 hours
  over a full day derives at most six. The person's own reminder choice
  replaces the author's period (below).
* **The approvals inbox shows it to every member**, as kind `input` with id
  `input:<task_id>` (`apps/swarm-api/swarm_api/approvals.py::project_input`),
  because any member may answer. It is projected on each read from the agent
  and never made a record, and the inbox has no decide path for it: approve
  or reject on that id is a 404, so the step's own answer route stays its one
  writer.
* **Both stop at once when the wait closes.** The open steps are read by
  `apps/swarm-api/swarm_api/approvals.py::awaiting_input`: PARKED
  `AWAITING_INPUT` in the reader's tenant, with no recorded answer, no cancel
  requested and an `expires_at` still ahead. An answered, expired or
  cancelled step derives nothing on the next read.

## The person's preferences: topics, muting and reminders

Settings › Profile saves which topics a person mutes and how often a read
item rings again (docs/design/user-profile.md §4.6, lane PROFILE-BELL). The
feed is still derived: the preferences are a filter applied **after** every
source has run, and a reminder is a read mark that ages out. Nothing is
queued, stored or sent, so "derived, never stored" still holds.

**Topics.** A topic is a finer cut than a group. The groups stay as they are,
because the bell's layout uses them.

| topic | needs you | derived by |
|---|---|---|
| `approval` | yes | the `approval` group |
| `waiting_step` | yes | an await-input step whose `remind` names the person |
| `question` | yes | a hand-off, and an agent's unanswered questions |
| `failed` | no | an agent or workflow that ended failed, cancelled or dead-lettered |
| `finished` | no | everything else that ended: succeeded, a pull request merged |
| `account` | GitHub yes, others no | the `account` group |
| `admin` | yes, admins only | the `admin` group |

The list is `preferences.TOPICS`, the one a saved mute is checked against, so
the bell can never carry a topic Profile cannot mute.

**Every topic may be muted (owner, Q3), needs-you topics included.** A muted
item leaves the list and the badge (`unread_needs_you`), and is counted in
`muted`. The bell shows a quiet "N muted items need you" line when
`muted.needs_you` is above zero, so a muted approval is quiet, never
invisible. Updates are cut to 50 *after* muting, so a person who mutes
`finished` sees the failures the successes used to push out.
`admin_pending` drives the Admin pill, not the bell, and is counted before
muting: an admin who mutes `admin` still sees the pill.

**Two Admin panel counts ride on the feed** (CONFIG-ADMIN-POLISH, part of
#2157, proposal E S1b): `tenants_waiting`, the workspace requests among the
admin items (Admin › Tenants' "N waiting"), and `platform_secrets_unset`, the
platform secrets with no enabled version (Admin › Platform secrets). They are
here because the shell already polls this read every 30 s; a badge that
polled its own page's route would be a second poll per page, all day. Both are
a platform admin's only and both are `null`, never 0, when they were not read:
the admin source failed, a secret's metadata or the broker's account
documents could not be read, or the API has no project to read. The console
draws no badge for `null`. The secret count is the Platform secrets page's own
read (`platform_secrets.platform_secret_status`), so the two never count
different rows, and it is reused for five minutes per reader
(`PLATFORM_SECRETS_TTL`): it is two Secret Manager metadata calls per secret,
and a secret's value is written by the operator's script, not in the console.
Past those five minutes the reader's last count is still served while one
background read refreshes it (`_refresh_platform_secrets`), so no feed waits
for Secret Manager except the first an instance serves, and that one reads
it beside the other sources, not after them.

**"Mark all read" marks what was shown.** With nothing muted it still sets
`all_read_at`. With a mute in force it marks only the ids shown and leaves
`all_read_at` as it was, because a muted item was never seen: unmuted later,
it is unread, which is true.

**Reminders age a read mark out.** `as_set` (the default) is the behaviour
above: a read item stays read. At `every_4h` or `daily`, a read mark, or
`all_read_at`, keeps a **needs-you** item read only while it is younger than
4 hours or a day; after that the item is unread again, and keeps coming back
until whatever it is about resolves and it stops deriving. Updates never
re-ring. `never` re-rings nothing. Both periods are inside the 30 days marks
are kept, so no new retention is needed.

**A waiting step's reminder period is the person's when they chose one, and
its author's otherwise (owner, Q4).** At `every_4h` or `daily` its
`k = floor((now - opened_at) / period)` uses that period, even when the
author declared none. At `as_set` it uses the author's `remind_after_seconds`,
as before. At `never`, `k` is held at 0. The id still rotates per period, and
the step still waits in the approvals inbox and on the workflow for everyone,
whatever one person's bell does.

**The preferences come from authentication** (`AuthContext.preferences`), so
the bell never reads the document a second time. When they cannot be read:

* a request that names no workspace never reaches the bell. The bell's items
  are a workspace's, so serving it would mean guessing the workspace, which
  is the silent fallback the owner ruled out (Q9). Authentication answers 503
  `preferences_unavailable`, before any source runs; the route is not in
  `auth.PREFERENCE_FREE_ROUTES`;
* a request that names its workspace with `X-Swarm-Tenant` is served
  **unfiltered**, at `as_set`, with `partial: true`. Muting is a convenience,
  and telling too much is the safe failure once the workspace is the one the
  person chose.

## Tenant isolation (invariant 9)

The tenant is the one the caller acts in (`deps.tenant_scope`: the verified
identity and `X-Swarm-Tenant`, never a body). Every tenant-scoped source reads
under that tenant only, and keeps only the caller's own work: agents and
workflows they submitted, runs they created, and their own connection and
tokens. Approvals belong to the tenant, because any member may decide one by
default, and so do workflow steps waiting for a person, because any member
may answer one (the bell still rings only the step's `remind` list). The admin sources read across tenants and run only when `is_admin`
is true; for anyone else they read nothing, and `admin_pending` is 0. The
caller's own workspace is theirs whichever tenant they act in.

Continuation-scoped accounts, the rollup sweeper and the schedule tick cannot
call these routes, because they are not in `auth.CONTINUATION_ROUTES` or the
sweeper and tick allow-lists. They are not people.

## Invariants

The feed reads documents and writes one document of its own. It creates no
agent, lease or slot (invariants 1-3), and it takes no image, command or
resource spec (invariant 10).
