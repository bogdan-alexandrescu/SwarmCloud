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
                                      admin_pending, read_at, partial}
POST /v1/notifications/read       {"ids": [...]} -> {"ok": true}
POST /v1/notifications/read-all   -> {"ok": true}
```

Each item is `{id, group, needs_you, title, detail, href, at, read}`. `href`
is a console path (`/admin/people`, `/agents/live/<task>/browser`). The feed
lists the needs-you items first, oldest first, and then the updates, newest
first: at most 50, all from the last 7 days.

`read_at` is when the person last pressed "mark all read". It is `null` if
they never have. `partial` is `true` when at least one source could not be
read: the feed still serves the other sources, and the failure is logged
under the source's name.

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
`task:handoff:<task>:<generation>`. A new occurrence of the same thing derives
a new id, which starts unread: a request made again after a denial, a plan
revised, a second hand-off in a later attempt.

## The groups

| group | needs you | what derives it | where it links |
|---|---|---|---|
| `admin` | yes, admins only | `workspaces` in `requested`, `loan_requests` in `requested`: what Admin › People lists first | `/admin/people` |
| `approval` | yes | the tenant's pending `approvals` records (an expired one, or a merge whose run has moved on, is skipped); `schedule_firings` held for approval before the inbox made their record; `issue_runs` in `PLANNED` | `/approvals/<id>` |
| `task` | yes | the caller's RUNNING tasks with a live-browser hand-off (`Task.human_wait`) and their PARKED `HUMAN_REQUIRED` tasks; the caller's tasks of the last 7 days whose agent wrote `questions.json` (`result_summary.questions`) | the task's Browser tab; its Artifacts tab |
| `account` | yes for GitHub, no for the workspace | the caller's `forge_connections` document in `refresh_failed` (only reconnecting renews it); their own expired user-scope `git_tokens`; their own workspace becoming `ready` or `denied` in the last 7 days; their own Claude-account loan request `declined` in the last 7 days (LOAN-DECLINE) | `/access`; `/setup#workspace`; `/setup#claude-account` |
| `finished` | no | the caller's own tasks submitted alone, their workflows (stored state), and their issue runs whose pull request merged, all ended in the last 7 days | the agent, workflow or run |

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
  person answers on the task's "Questions for you" card
  (`POST /v1/tasks/{id}/answers`, TASK-ANSWERS, owner decision 2026-10-13), and
  the answer is stored on the task as `metadata.owner_answers`. The item derives
  only while at least one counted question has no answer. Until then it stays
  for 7 days after its task ended, and marking it read clears it from the bell's
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
  reported once. A child task is reported by its parent.
* **Release IAM holds are left out.** A release waiting on an IAM grant is a
  GitHub environment approval, and the platform keeps no record of it in its
  own stores, so there is nothing to derive an item from. An issue run's IAM
  hard stop is different: it is a hold on the run (`issueruns.STOP_IAM`), so
  it already appears as an `approval` item of kind `hold`.

## Tenant isolation (invariant 9)

The tenant is the one the caller acts in (`deps.tenant_scope`: the verified
identity and `X-Swarm-Tenant`, never a body). Every tenant-scoped source reads
under that tenant only, and keeps only the caller's own work: tasks and
workflows they submitted, runs they created, and their own connection and
tokens. Approvals belong to the tenant, because any member may decide one by
default. The admin sources read across tenants and run only when `is_admin`
is true; for anyone else they read nothing, and `admin_pending` is 0. The
caller's own workspace is theirs whichever tenant they act in.

Continuation-scoped accounts, the rollup sweeper and the schedule tick cannot
call these routes, because they are not in `auth.CONTINUATION_ROUTES` or the
sweeper and tick allow-lists. They are not people.

## Invariants

The feed reads documents and writes one document of its own. It creates no
task, lease or slot (invariants 1-3), and it takes no image, command or
resource spec (invariant 10).
