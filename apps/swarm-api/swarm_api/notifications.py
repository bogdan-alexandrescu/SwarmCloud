"""The notification feed: what is waiting on a person, and what finished (#1206, lane NOTIF-API).

docs/notifications.md says why the feed is shaped this way. In short:

DERIVED, NEVER STORED. Every item is computed on each read from a record some
other path already writes -- a workspace request, an approval, a parked task,
a GitHub connection, a finished workflow. Nothing here writes to those
records, and no path that writes them writes a notification. So a "needs you"
item cannot outlive what it is about: once the request is approved, the plan
decided or the hand-off taken, the next read does not derive it and it is gone.
A stored notification would need a second writer on every one of those paths,
and every writer someone forgot would be a bell that never clears.

THE ONE DOCUMENT IT OWNS is `notification_reads/<key>`, one per person: the ids
they marked read and when, pruned to `KEEP_READS`, and the moment of their last
"mark all read". Never a title, a detail or a link. `<key>` is a hash of the
lower-cased address (`forgeapp.user_hash`), so the document id names nobody.
Read marks key on ids, and ids are STABLE: the same underlying thing derives
the same id on every read (`admin:workspace:<workspace id>:<request id>`), and
a new occurrence of it -- a request made again, a plan revised -- derives a new
one, which is unread.

TENANT ISOLATION (invariant 9). Every tenant-scoped source reads under the
tenant the caller acts in (`deps.tenant_scope`, never a body or a query) and
keeps only the caller's own work: tasks and workflows they submitted, runs
they created, their own GitHub connection and tokens. Approvals are the
tenant's (any member may decide one by default), narrowed to those this person
may decide. The admin sources read across tenants and run for admins only.
The person's own workspace is theirs whichever tenant they act in.

AN AWAIT-INPUT STEP RINGS ITS `remind` LIST, IN ITS TENANT
(docs/design/human-steps.md §4.5, Q7, lane HW-NOTIFY). Any member may answer
a waiting step (Q2), but only the readers its author named are rung: every
member rung for every review is noise that teaches people to ignore the bell,
and the rest see the wait in the inbox (`approvals.project_input`) and on the
workflow. The step is read under the tenant the caller acts in, so an address
named from outside it rings nobody. The id carries the reminder period,
`task:input:<task>:<k>`, `k = floor((now - opened_at) / remind_after_seconds)`,
so each period derives a new, unread item and nothing is written to make it.

THE PERSON'S PREFERENCES ARE A FILTER AFTER DERIVATION (docs/design/
user-profile.md §4.6, Q3/Q4, lane PROFILE-BELL). Every item carries a `topic`
from `preferences.TOPICS`, a finer cut than its group. A muted topic is dropped
after the sources ran and counted in `muted`, so a muted approval is quiet,
never invisible; `admin_pending` is counted before the drop. A reminder is a
read mark that ages out (`REMINDER_PERIODS`): nothing is queued or stored, and
an update never re-rings. A waiting step's period is the person's when they
chose one and its author's `remind_after_seconds` at `as_set`.

A SOURCE THAT FAILS FAILS ALONE. Each source is a small function of a
`Reading`; `Notifications.feed` runs them one by one, logs a failure by the
source's name and the exception's type, and serves the rest with
`partial: true`.

RELEASE IAM HOLDS ARE NOT A SOURCE. A release that waits on an IAM grant is a
GitHub environment approval; the platform keeps no record of it in Firestore,
so there is nothing to derive one from. An issue run's IAM hard stop is a hold
on the run (`issueruns.STOP_IAM`), and it is here, as an approval item.

A FAILURE FINDING RINGS ITS AGENT'S SUBMITTER (docs/design/failure-review.md
§1, lane FR-4 FINDINGS). The `failure-review` schedule writes a finding into
the tenant whose own repository caused a failure; the source `failure_review`
derives a "needs you" item from it for the person who submitted the agent,
through the one tenant projection in `routes.failurefindings`, under the
existing topic `failed` (`preferences.TOPICS` is not this lane's to widen).

THE ADMIN PANEL'S TWO PLATFORM COUNTS ride on this read (CONFIG-ADMIN-POLISH;
part of #2157, proposal E S1b): `tenants_waiting`, the workspaces waiting to be
created (Admin › Tenants' lead), and `platform_secrets_unset`, the platform
secrets with no enabled version (Admin › Platform secrets' lead). The shell
already polls this feed, so a badge costs no page its own poll. Both are a
platform admin's only, and both are null -- never 0 -- when they were not read:
the admin source failed, a secret's metadata could not be read, or there is no
reader. The secret count comes from `platform_secrets.platform_secret_status`,
the page's own read, so the badge and the page count the same rows; it is
reused for `PLATFORM_SECRETS_TTL` per reader, because it is two Secret Manager
metadata calls per secret and the bell polls every 30 s.

A JOIN REQUEST RINGS ITS DECIDERS, AND ITS DECISION RINGS ITS REQUESTER
(docs/design/join-requests.md §4.6, Q3; lane JOIN-BELL, part of #2325).
`join_requests_to_decide` rings the open requests to join a group workspace
for that workspace's admins and for platform admins, `admin:join:<ref>`; a
workspace admin is rung only for a workspace in `Viewer.administers` (the
route's fresh `administered_workspaces` read, as People reads it) and only
while the request names the group that workspace has now, which is exactly
what `join_scope` lets them decide: a bell for a request they would be
refused is noise. `own_join_requests` rings the requester once per approval
or denial, `join:<ref>:<state>`, for `WINDOW`; never who decided, and never
a workspace id or group, which a working code does not reveal. Both read the
signed records through `joinrequests.JoinStore`, so a request or decision
that fails its MAC rings nobody, and both read nothing while no store is
given (`Notifications(joins=...)`).

NOTHING HERE CREATES DEMAND (invariants 1-3): it reads documents and writes one
document of its own; no task, lease or slot is touched.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import ParkReason, TaskState

from . import approvals, joinrequests, schedulefire
from . import platform_secrets as psec
from .agent_questions import blocking_unanswered, current_wait, parked_wait, unanswered
from . import workspaces as ws
from .forgeapp import CONNECTIONS, REFRESH_FAILED, connection_id_for, user_hash
from .gittokens import COLLECTION as GIT_TOKENS
from .issueruns import IssueRuns
from .people import LOAN_DECLINED
from .preferences import DEFAULT_REMINDER
from .preferences import TOPICS as TOPICS  # re-exported: the bell's topics are these
from .redaction import redact_detail

log = logging.getLogger(__name__)

#: The read marks, one document per person. Ids and times only.
COLLECTION = "notification_reads"

ADMIN = "admin"
APPROVAL = "approval"
TASK = "task"
ACCOUNT = "account"
FINISHED = "finished"
GROUPS = (ADMIN, APPROVAL, TASK, ACCOUNT, FINISHED)

#: The topics an item may carry (§4.6), the ones a person may mute (Q3).
#: `TOPICS` is `preferences.TOPICS`, so a saved mute always names one of these.
T_APPROVAL = "approval"
T_WAITING_STEP = "waiting_step"
T_QUESTION = "question"
T_FAILED = "failed"
T_FINISHED = "finished"
T_ACCOUNT = "account"
T_ADMIN = "admin"

#: How long a read mark keeps a "needs you" item read, by reminder (§4.6). A
#: reminder absent here never ages a mark: `as_set` is today's behaviour, and
#: `never` re-rings nothing. Both are at most `KEEP_READS`, so no mark a
#: reminder needs is pruned before it ages out.
REMINDER_PERIODS = {"every_4h": timedelta(hours=4), "daily": timedelta(days=1)}

#: How far back an update reaches, and how long a dated "needs you" item that
#: no later state resolves (a question asked, a workspace decided) stays.
WINDOW = timedelta(days=7)
#: At most this many updates in one feed (the bell's drop-down, not a history).
MAX_UPDATES = 50
#: A read mark older than this is dropped on the next write: by then the item
#: it marked has left the feed (`WINDOW`) or is a "needs you" item that has
#: waited a month, which may fairly be shown as unread again.
KEEP_READS = timedelta(days=30)
#: Bounds on the document: the most marks kept, and one request's ids.
MAX_MARKS = 2000
MAX_IDS = 500
MAX_ID_CHARS = 200
#: The most documents one source reads.
SCAN = 200
DETAIL_CHARS = 200

#: How long one platform-secret count is reused. A secret is written by an
#: operator's script, not by anyone in the console, so a badge five minutes
#: behind is honest enough; the page itself always reads live.
PLATFORM_SECRETS_TTL = timedelta(minutes=5)
#: What `platform_secrets_unset` counts. The page's catalogue, named here so a
#: test can narrow it.
PLATFORM_SECRET_CATALOGUE = psec.PLATFORM_SECRETS

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ENDED = {
    TaskState.SUCCEEDED: "succeeded",
    TaskState.FAILED: "failed",
    TaskState.CANCELLED: "was cancelled",
    TaskState.DEAD_LETTERED: "failed (dead-lettered)",
}
#: The end states the bell calls `failed`; every other end is `finished`.
_FAILED = frozenset({TaskState.FAILED, TaskState.CANCELLED, TaskState.DEAD_LETTERED})
_APPROVAL_TITLES = {
    approvals.RUN: "A scheduled run waits for approval",
    approvals.PLAN: "A plan waits for approval",
    approvals.MERGE: "A pull request waits for a merge approval",
    approvals.PROPOSAL: "A proposal waits for approval",
    approvals.SPEC: "A prompt waits for approval",
    approvals.HOLD: "A held run waits for approval",
}


@dataclass(frozen=True)
class Viewer:
    """Who is reading, in which tenant: from the verified identity only."""

    email: str
    tenant_id: str
    is_admin: bool = False
    is_owner: bool = False
    #: A personal workspace's member, who reads only their own tasks
    #: (`AuthContext.member_scope`): no tenant-wide source reaches past that.
    member_scope: bool = False
    #: The person's muted topics and reminder (`AuthContext.preferences`).
    muted: frozenset[str] = frozenset()
    reminder: str = DEFAULT_REMINDER
    #: Their preferences could not be read: the feed is served unfiltered, at
    #: `as_set`, with `partial: true` (§4.6). Only a request that named its
    #: workspace gets this far; one that did not is a 503 in authentication.
    preferences_unread: bool = False
    #: tenant_id -> group of each group workspace the person administers by
    #: their own role, read fresh (`routes.people.administered_workspaces`):
    #: whose join requests ring them. None when that read failed, which the
    #: join source reports as unread rather than as "administers nothing".
    administers: tuple[tuple[str, str], ...] | None = ()


@dataclass
class Reading:
    """One feed read: what every source is given. `own_tasks` is read once
    and shared by the sources that need it, and fails each of them alike."""

    db: Any
    store: Any
    viewer: Viewer
    now: datetime
    #: The join requests' store (`joinrequests.JoinStore`); None reads none.
    joins: Any | None = None
    _tasks: list[Any] | None = field(default=None, repr=False)

    @property
    def email(self) -> str:
        return self.viewer.email.strip().lower()

    def own_tasks(self) -> list[Any]:
        """The caller's newest tasks in their tenant (index
        tasks-tenant-submitter-created), newest first."""
        if self._tasks is None:
            page = self.store.list_tasks(self.viewer.tenant_id, limit=SCAN,
                                         submitted_by=self.viewer.email)
            self._tasks = [t for t in page.items if t.tenant_id == self.viewer.tenant_id]
        return self._tasks


Source = Callable[[Reading], list[dict[str, Any]]]


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _text(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return redact_detail(text, limit=DETAIL_CHARS) if text else None


def _item(id_: str, group: str, needs_you: bool, title: str, detail: Any, href: str,
          at: Any, *, topic: str) -> dict[str, Any]:
    return {"id": id_[:MAX_ID_CHARS], "group": group, "topic": topic, "needs_you": needs_you,
            "title": title, "detail": _text(detail), "href": href,
            "at": _aware(at) or _EPOCH}


def _recent(reading: Reading, at: Any) -> bool:
    moment = _aware(at)
    return moment is not None and moment >= reading.now - WINDOW


# --------------------------------------------------------------------------
# admin: requests waiting on any admin (admins only)
# --------------------------------------------------------------------------

def admin_requests(reading: Reading) -> list[dict[str, Any]]:
    """Pending workspace and Claude-account requests: what Admin › People
    shows first (`people.People.everyone`). Reads nothing for a non-admin."""
    if not reading.viewer.is_admin:
        return []
    items = []
    query = reading.db.collection(ws.WORKSPACES).where(
        filter=FieldFilter("state", "==", ws.REQUESTED)).limit(SCAN)
    for snap in query.stream():
        record = snap.to_dict() or {}
        if ws.state_of(record) != ws.REQUESTED or not record.get("workspace_id"):
            continue
        items.append(_item(
            f"admin:workspace:{record['workspace_id']}:{record.get('request_id') or ''}",
            ADMIN, True, f"{record.get('principal') or 'Someone'} asks for a workspace",
            f"Workspace {record['workspace_id']}", "/admin/people", record.get("requested_at"),
            topic=T_ADMIN))
    query = reading.db.collection(ws.LOAN_REQUESTS).where(
        filter=FieldFilter("state", "==", ws.REQUESTED)).limit(SCAN)
    for snap in query.stream():
        loan = snap.to_dict() or {}
        if loan.get("state") != ws.REQUESTED:
            continue
        items.append(_item(
            f"admin:loan:{loan.get('workspace_id') or snap.id}:{loan.get('request_id') or ''}",
            ADMIN, True, f"{loan.get('principal') or 'Someone'} asks for a Claude account",
            f"Workspace {loan.get('workspace_id')}" if loan.get("workspace_id") else None,
            "/admin/people", loan.get("requested_at"), topic=T_ADMIN))
    return items


#: What a requester is shown for a workspace with no display name: never its
#: id or its group (join-requests.md §4.2), as the join routes say it.
JOIN_UNNAMED = "a group workspace"


def _display_name(reading: Reading, tenant_id: str) -> str | None:
    """The workspace's display name, or None when it has none or cannot be
    read: a label only, never a reason to drop an item."""
    try:
        tenant = reading.store.get_tenant(tenant_id)
    except Exception:  # noqa: BLE001 - a label, never the item
        return None
    name = getattr(tenant, "display_name", None) if tenant is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def join_requests_to_decide(reading: Reading) -> list[dict[str, Any]]:
    """Open requests to join a group workspace, for those who decide them
    (join-requests.md §4.6, Q3): every one for a platform admin; for a
    workspace admin, those of the workspaces they administer whose request
    names the group the workspace has now. Reads nothing for anyone else.
    Approved, denied, withdrawn or expired: nothing is derived."""
    viewer, joins = reading.viewer, reading.joins
    if joins is None:
        return []
    if viewer.is_admin:
        views = joins.open_for(None)
    else:
        if viewer.administers is None:
            raise LookupError("the workspaces this person administers could not be read")
        groups = {tenant_id: str(group or "").strip().lower()
                  for tenant_id, group in viewer.administers
                  if tenant_id and not tenant_id.startswith("u-")}
        if not groups:
            return []
        views = [v for v in joins.open_for(groups)
                 if groups.get(v.request.tenant_id) == v.request.group]
    names: dict[str, str] = {}
    items = []
    for view in views:
        request = view.request
        if view.state != joinrequests.REQUESTED:
            continue
        if request.tenant_id not in names:
            names[request.tenant_id] = _display_name(reading, request.tenant_id) \
                or request.tenant_id
        items.append(_item(
            f"admin:join:{request.ref}", ADMIN, True,
            f"{request.requester} asks to join {names[request.tenant_id]}",
            request.message or None, "/admin/people", request.requested_at, topic=T_ADMIN))
    return items


# --------------------------------------------------------------------------
# approval: what blocks progress until a member decides
# --------------------------------------------------------------------------

def _may_decide(viewer: Viewer, approvers: Any, *, excluded: Iterable[str] = ()) -> bool:
    """A cheap reading of §4.6 (docs/schedules.md): who an item waits on. The
    decision itself is checked by `approvals` when it is made; this only keeps
    an item out of the bell of someone who could not decide it."""
    email = viewer.email.strip().lower()
    if approvers in (approvals.APPROVERS_OWNER_ONLY, "owner_only"):
        return viewer.is_owner
    if approvers == approvals.APPROVERS_SECOND_MEMBER:
        return email not in {str(e or "").strip().lower() for e in excluded}
    if isinstance(approvers, list):
        return email in {str(n).strip().lower() for n in approvers}
    return True


def _approval_href(approval_id: str) -> str:
    return "/approvals/" + quote(approval_id, safe="")


def approval_items(reading: Reading) -> list[dict[str, Any]]:
    """The tenant's pending approval records, its firings waiting for one, and
    its PLANNED issue runs: the inbox `approvals.inbox` serves, read without
    the inbox's writes (it materialises firings and closes stale merges)."""
    db, tenant_id, viewer = reading.db, reading.viewer.tenant_id, reading.viewer
    items: dict[str, dict[str, Any]] = {}
    for doc in approvals.pending(db, tenant_id, limit=SCAN):
        expires = _aware(doc.get("expires_at"))
        if expires is not None and expires <= reading.now:
            continue
        if doc.get("kind") == approvals.MERGE and not approvals._live_merge(db, doc):
            continue
        if not _may_decide(viewer, doc.get("approvers")):
            continue
        approval_id = str(doc.get("approval_id"))
        items[approval_id] = _item(
            f"approval:{approval_id}", APPROVAL, True,
            _APPROVAL_TITLES.get(str(doc.get("kind")), "An item waits for approval"),
            doc.get("summary"), _approval_href(approval_id), doc.get("requested_at"),
            topic=T_APPROVAL)

    # A firing the tick held for approval before anything made its record: the
    # id is the record's own (`approval_id` is deterministic), so the item keeps
    # its id, and its read mark, once the inbox makes the record.
    query = (db.collection(schedulefire.FIRINGS)
             .where(filter=FieldFilter("tenant_id", "==", tenant_id))
             .where(filter=FieldFilter("state", "==", schedulefire.AWAITING_APPROVAL))
             .limit(SCAN))
    schedules: dict[str, dict[str, Any] | None] = {}
    for snap in query.stream():
        firing = snap.to_dict() or {}
        if firing.get("tenant_id") != tenant_id or firing.get("approval_id") \
                or not firing.get("firing_id"):
            continue
        sid = str(firing.get("schedule_id") or "")
        if sid not in schedules:
            schedules[sid] = approvals.read_schedule(db, tenant_id, sid)
        schedule = schedules[sid]
        if schedule is None:
            continue
        if not _may_decide(viewer, (schedule.get("gate") or {}).get("approvers") or "members"):
            continue
        approval_id = approvals.approval_id(approvals.RUN, str(firing["firing_id"]))
        items.setdefault(approval_id, _item(
            f"approval:{approval_id}", APPROVAL, True, _APPROVAL_TITLES[approvals.RUN],
            f"{schedule.get('name')}: {schedule.get('type')} waits to run",
            "/approvals", firing.get("fired_at"), topic=T_APPROVAL))

    runs, _ = IssueRuns(db, now=lambda: reading.now).planned(tenant_id, limit=SCAN)
    for run in runs:
        sid = (run.schedule or {}).get("schedule_id")
        if sid and sid not in schedules:
            schedules[sid] = approvals.read_schedule(db, tenant_id, sid)
        projected = approvals.project_run(run, schedules.get(sid) if sid else None)
        expires = _aware(projected.get("expires_at"))
        if expires is not None and expires <= reading.now:
            continue
        if not _may_decide(viewer, projected.get("approvers"),
                           excluded=(run.created_by, run.plan_edited_by or "")):
            continue
        held = bool(run.approval_hold)
        items[projected["approval_id"]] = _item(
            f"approval:run:{run.id}:{run.plan_revision}", APPROVAL, True,
            f"{'A held plan' if held else 'The plan'} for {run.issue.short} waits for approval",
            (run.plan or {}).get("summary"), _approval_href(projected["approval_id"]),
            projected.get("requested_at"), topic=T_APPROVAL)
    return list(items.values())


# --------------------------------------------------------------------------
# task: waiting on the person inside one of their tasks
# --------------------------------------------------------------------------

def _own_in_state(reading: Reading, state: TaskState) -> list[Any]:
    page = reading.store.list_tasks(reading.viewer.tenant_id, state=state, limit=SCAN,
                                    submitted_by=reading.viewer.email)
    return [t for t in page.items if t.tenant_id == reading.viewer.tenant_id and t.state == state]


#: The open hand-off's title (owner decision 2026-10-13, HANDOFF-POLICY).
HANDOFF_TITLE = "Agent needs you"


def _opened_to_members(reading: Reading, wait: Mapping[str, Any]) -> bool:
    """"Let any member take control" on the waiting attempt's live session
    (`live.LiveService.open_to_members`). Its tenant is checked again: a
    session of another tenant's task never opens anything here."""
    from .live import LIVE_SESSIONS, session_id_for

    attempt = wait.get("attempt_id")
    if not isinstance(attempt, str) or not attempt:
        return False
    snap = reading.db.collection(LIVE_SESSIONS).document(session_id_for(attempt)).get()
    session = snap.to_dict() if snap.exists else None
    return bool(session and session.get("tenant_id") == reading.viewer.tenant_id
                and session.get("open_to_members"))


def handoffs(reading: Reading) -> list[dict[str, Any]]:
    """Tasks whose agent waits for a person in the live browser: a RUNNING
    task holding a hand-off (`Task.human_wait`, docs/design/live-browser.md
    §4.1), and one of the caller's PARKED `HUMAN_REQUIRED` after its hold
    expired (§4.3), which only a person promotes. Both link to the Browser tab.

    An open hand-off is "Agent needs you" for everyone who may take control
    of it (`handoff.may_control`, §5.1): its submitter, and the tenant's
    members while the submitter has "Let any member take control" on (or the
    task has no human owner). That is the bell parking fast relies on: the
    hold lasts a minute unless someone opens the view. Each request is its
    own id, so a second hand-off in one attempt is unread again."""
    from .handoff import may_control

    items = []
    scope = reading.viewer.email if reading.viewer.member_scope else None
    page = reading.store.list_tasks(reading.viewer.tenant_id, state=TaskState.RUNNING,
                                    limit=SCAN, submitted_by=scope)
    for task in page.items:
        if task.tenant_id != reading.viewer.tenant_id or task.state != TaskState.RUNNING:
            continue
        wait = task.human_wait if isinstance(task.human_wait, dict) else None
        if not wait:
            continue
        own = (task.submitted_by or "").strip().lower() == reading.email
        if not own and not may_control(task, reading.email, reading.db,
                                       opened_to_members=_opened_to_members(reading, wait)):
            continue
        generation = wait.get("generation") or task.current_generation
        request = wait.get("request_id")
        suffix = f":{request}" if isinstance(request, str) and request else ""
        items.append(_item(
            f"task:handoff:{task.id}:{generation}{suffix}", TASK, True, HANDOFF_TITLE,
            wait.get("reason"), f"/agents/live/{task.id}/browser",
            wait.get("requested_at") or task.updated_at, topic=T_QUESTION))
    for task in _own_in_state(reading, TaskState.PARKED):
        if task.park_reason != ParkReason.HUMAN_REQUIRED:
            continue
        items.append(_item(
            f"task:handoff:{task.id}:{task.current_generation}", TASK, True,
            "Your agent parked until you take over its browser", f"Task {task.id}",
            f"/agents/waiting/{task.id}/browser", task.updated_at, topic=T_QUESTION))
    return items


#: The bell's title for a waiting step, by its declared form (§4.1).
_INPUT_TITLES = {
    "verdict": "A workflow is waiting for a review",
    "answers": "A workflow is waiting for answers to its questions",
    "text": "A workflow is waiting for a written answer",
}


def _reminder(task: Any, block: Mapping[str, Any], now: datetime,
              reminder: str = DEFAULT_REMINDER) -> tuple[int, datetime | None]:
    """`k`, the whole reminder periods since the wait opened (0 with none),
    and the moment period `k` began: the item's `at`.

    WHOSE PERIOD (Q4): the reader's, when they chose `every_4h` or `daily`;
    none at `never`, which holds `k` at 0; and the author's
    `remind_after_seconds` at `as_set`. The id still rotates per period, so
    each reminder is a new, unread item and nothing is written to make it."""
    opened = approvals.input_opened_at(task)
    if reminder == "never":
        return 0, opened
    chosen = REMINDER_PERIODS.get(reminder)
    period = int(chosen.total_seconds()) if chosen else block.get("remind_after_seconds")
    if opened is None or isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        return 0, opened
    k = max(0, int((now - opened).total_seconds() // period))
    return k, opened + timedelta(seconds=k * period)


def awaiting_input(reading: Reading) -> list[dict[str, Any]]:
    """Workflow steps of the caller's tenant PARKED `AWAITING_INPUT` whose
    `remind` list names the caller (§4.5): one item per step and reminder
    period, linked to the workflow, where the step's card takes the answer.
    A personal workspace's member reads only the steps they submitted, as
    `handoffs` does. Answered, expired or cancelled: nothing is derived."""
    scope = reading.viewer.email if reading.viewer.member_scope else None
    items = []
    for task in approvals.awaiting_input(reading.store, reading.viewer.tenant_id, reading.now,
                                         limit=SCAN, submitted_by=scope):
        block = approvals.await_input_declaration(task)
        remind = block.get("remind", [task.submitted_by])
        if not isinstance(remind, list) \
                or reading.email not in {str(a or "").strip().lower() for a in remind}:
            continue
        k, at = _reminder(task, block, reading.now, reading.viewer.reminder)
        workflow = str(task.workflow_id or "")
        items.append(_item(
            f"task:input:{task.id}:{k}", TASK, True,
            _INPUT_TITLES.get(str(block.get("form")), "A workflow is waiting for an answer"),
            f"Step {task.step_id or task.id} · Workflow {workflow}",
            f"/workflows/{quote(workflow, safe='')}" if workflow else "/workflows",
            at or task.updated_at, topic=T_WAITING_STEP))
    return items


def _question_count(task: Any) -> int:
    count = (task.result_summary or {}).get("questions")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def agent_questions(reading: Reading) -> list[dict[str, Any]]:
    """The caller's tasks of the last `WINDOW` whose agent asked the owner
    questions (`questions.json`, counted by the worker in
    `result_summary.questions`) and has not answered them all. An answer is
    stored on the task (`metadata.owner_answers`, `POST /v1/tasks/{id}/answers`,
    TASK-ANSWERS); once every question has one the item is gone, and until
    then it stays for `WINDOW` after the task ended, reading it clearing it
    from the bell's count. It opens the task's Details at `#questions`, where
    the "Questions for you" card shows them (`GET /v1/tasks/{id}/questions`);
    it opened Artifacts, which showed a file, not the questions (owner report
    2026-10-13).

    THE ITEM NAMES ITS TASK BY WHAT IT WAS DOING (QA14-UI, owner QA
    2026-10-13): a bare task id said nothing about which agent asked. The
    detail leads with the step's name, or the runner profile for a task that
    is no workflow step; the question's own text is the console's to read
    (`GET /v1/tasks/{id}/questions`), because it lives in an artifact this
    feed does not open on every poll.

    AN AGENT PARKED ON A BLOCKING QUESTION IS LISTED WHILE IT WAITS (lane
    AU-7, docs/design/agent-autonomy.md §3.9): it has ended nothing, and
    the wait is bounded at 72 hours itself, so no window applies; the item
    says the agent is
    waiting and how many blocking questions are open. Its id is the same
    `task:questions:<id>`, so the console's card marks it read as before;
    the last blocking answer resumes the agent and the item goes."""
    items = []
    for task in reading.own_tasks():
        wait = parked_wait(task)
        if wait is not None:
            _, adopted = current_wait(task)
            open_ = blocking_unanswered(wait, task.metadata if adopted else None)
            if not open_:
                continue
            items.append(_item(
                f"task:questions:{task.id}", TASK, True,
                f"Your agent is waiting for your answer to {len(open_)} "
                f"question{'s' if len(open_) != 1 else ''}",
                f"{task.step_id or task.runner_profile} · Task {task.id}",
                f"/agents/recent/{task.id}#questions", task.updated_at, topic=T_QUESTION))
            continue
        count = _question_count(task)
        if count <= 0 or task.state not in _ENDED:
            continue
        if unanswered(count, task.metadata) == 0:
            continue
        ended = task.completed_at or task.updated_at
        if not _recent(reading, ended):
            continue
        items.append(_item(
            f"task:questions:{task.id}", TASK, True,
            f"Your agent asked {count} question{'s' if count != 1 else ''}",
            f"{task.step_id or task.runner_profile} · Task {task.id}",
            f"/agents/recent/{task.id}#questions", ended, topic=T_QUESTION))
    return items


# --------------------------------------------------------------------------
# account: the person's own access
# --------------------------------------------------------------------------

def github_items(reading: Reading) -> list[dict[str, Any]]:
    """The caller's GitHub connection in this tenant whose refresh failed (the
    refresh sweep, `forgeapp`; only reconnecting renews it), and every token
    they registered for themselves here that has expired. A connection's own
    token is the connection's item, not a second one."""
    items = []
    tenant_id, email = reading.viewer.tenant_id, reading.email
    conn_id = connection_id_for(tenant_id, email)
    snap = reading.db.collection(CONNECTIONS).document(conn_id).get()
    conn = snap.to_dict() if snap.exists else None
    if conn and conn.get("tenant_id") == tenant_id and conn.get("state") == REFRESH_FAILED:
        failed = conn.get("failed_at")
        items.append(_item(
            f"account:github:{conn_id}:{(_aware(failed) or _EPOCH).isoformat()}", ACCOUNT, True,
            "GitHub needs you to reconnect", (conn.get("failure") or {}).get("recovery"),
            "/access", failed, topic=T_ACCOUNT))
    query = (reading.db.collection(GIT_TOKENS)
             .where(filter=FieldFilter("tenant_id", "==", tenant_id))
             .where(filter=FieldFilter("user", "==", email))
             .limit(SCAN))
    for token in query.stream():
        doc = token.to_dict() or {}
        if doc.get("tenant_id") != tenant_id or str(doc.get("user") or "").lower() != email \
                or doc.get("scope") != "user" or doc.get("state") != "expired" \
                or doc.get("kind") == "app_user":
            continue
        expired = _aware(doc.get("expires_at")) or _aware(doc.get("verified_at")) \
            or _aware(doc.get("registered_at"))
        login = doc.get("forge_login")
        items.append(_item(
            f"account:token:{doc.get('token_id')}:{(expired or _EPOCH).isoformat()}", ACCOUNT, True,
            "A GitHub token you registered has expired",
            f"GitHub account {login}" if login else None, "/access", expired, topic=T_ACCOUNT))
    return items


def workspace_items(reading: Reading) -> list[dict[str, Any]]:
    """The person's own workspace (`workspaces/<their personal tenant>`) became
    ready, or was denied, within `WINDOW`. The person's whichever tenant they
    act in. A denial shows its reason (shown to the person by design, §1.3);
    never who decided."""
    tenant_id = ws.personal_tenant_id(reading.email)
    snap = reading.db.collection(ws.WORKSPACES).document(tenant_id).get()
    record = snap.to_dict() if snap.exists else None
    if not record or str(record.get("principal") or "").lower() != reading.email:
        return []
    state, workspace_id = ws.state_of(record), record.get("workspace_id")
    decision = record.get("decision") or {}
    if state == ws.READY and _recent(reading, record.get("ready_at")):
        return [_item(f"account:workspace:{workspace_id}:ready", ACCOUNT, False,
                      "Your workspace is ready", f"Workspace {workspace_id}",
                      "/setup#workspace", record.get("ready_at"), topic=T_ACCOUNT)]
    if state == ws.DENIED and _recent(reading, decision.get("at")):
        return [_item(f"account:workspace:{workspace_id}:denied:{record.get('request_id') or ''}",
                      ACCOUNT, False, "Your workspace request was denied", decision.get("reason"),
                      "/setup#workspace", decision.get("at"), topic=T_ACCOUNT)]
    return []


def loan_items(reading: Reading) -> list[dict[str, Any]]:
    """The person's own Claude-account loan request was declined within
    `WINDOW` (LOAN-DECLINE). The reason is theirs to read; who declined is
    not. An open request is the admins' item, and a lent one shows as the
    account itself, so neither is an item here. Asking again re-opens the
    request, and the item goes with the declined state."""
    snap = reading.db.collection(ws.LOAN_REQUESTS).document(
        ws.personal_tenant_id(reading.email)).get()
    loan = snap.to_dict() if snap.exists else None
    if not loan or str(loan.get("principal") or "").lower() != reading.email:
        return []
    decision = loan.get("decision") or {}
    if loan.get("state") != LOAN_DECLINED or not _recent(reading, decision.get("at")):
        return []
    return [_item(f"account:loan:{loan.get('workspace_id') or snap.id}:declined:"
                  f"{loan.get('request_id') or ''}", ACCOUNT, False,
                  "Your request to borrow a Claude account was declined", decision.get("reason"),
                  "/setup#claude-account", decision.get("at"), topic=T_ACCOUNT)]


def own_join_requests(reading: Reading) -> list[dict[str, Any]]:
    """The person's own requests to join a group workspace that were approved
    or denied within `WINDOW` (join-requests.md §4.6): one item per decision,
    `join:<ref>:<state>`, linked to their request list. A denial shows its
    reason, which is the person's to read; who decided is not. An open request
    is its deciders' item, and a withdrawal is the person's own act."""
    if reading.joins is None:
        return []
    items = []
    for view in reading.joins.mine(reading.email):
        decision = view.decision
        if decision is None or view.request.requester != reading.email \
                or view.state not in (joinrequests.APPROVED, joinrequests.DENIED) \
                or not _recent(reading, decision.at):
            continue
        name = _display_name(reading, view.request.tenant_id) or JOIN_UNNAMED
        denied = view.state == joinrequests.DENIED
        items.append(_item(
            f"join:{view.request.ref}:{view.state}", ACCOUNT, False,
            f"Your request to join {name} was {'denied' if denied else 'approved'}",
            decision.reason if denied else None, "/join", decision.at, topic=T_ACCOUNT))
    return items


# --------------------------------------------------------------------------
# finished: updates, never "needs you"
# --------------------------------------------------------------------------

def _end_topic(state: TaskState) -> str:
    """`failed` for a failed, cancelled or dead-lettered end, else `finished`:
    the cut a person mutes the successes by and keeps the failures (§4.6)."""
    return T_FAILED if state in _FAILED else T_FINISHED


def finished_items(reading: Reading) -> list[dict[str, Any]]:
    """The caller's own work that ended within `WINDOW`: tasks submitted alone
    (a workflow's steps and an agent's children are told by their workflow or
    parent), workflows by their STORED state, which the rollup sweeper
    converges, and issue runs whose pull request merged."""
    items = []
    for task in reading.own_tasks():
        if task.state not in _ENDED or task.workflow_id or task.parent_task_id:
            continue
        ended = task.completed_at or task.updated_at
        if not _recent(reading, ended):
            continue
        items.append(_item(
            f"finished:task:{task.id}:{task.state.value}", FINISHED, False,
            f"Task {task.id} {_ENDED[task.state]}", task.runner_profile,
            f"/agents/recent/{task.id}", ended, topic=_end_topic(task.state)))
    tenant_id = reading.viewer.tenant_id
    page = reading.store.list_workflows(tenant_id, limit=SCAN, submitted_by=reading.viewer.email)
    for workflow in page.items:
        if workflow.tenant_id != tenant_id or workflow.state not in _ENDED \
                or not _recent(reading, workflow.updated_at):
            continue
        items.append(_item(
            f"finished:workflow:{workflow.workflow_id}:{workflow.state.value}", FINISHED, False,
            f"Workflow {workflow.workflow_id} {_ENDED[workflow.state]}",
            f"{len(workflow.steps)} step{'s' if len(workflow.steps) != 1 else ''}",
            f"/workflows/{workflow.workflow_id}", workflow.updated_at,
            topic=_end_topic(workflow.state)))
    runs, _ = IssueRuns(reading.db, now=lambda: reading.now).list(tenant_id, limit=SCAN)
    for run in runs:
        pull = run.pull_request or {}
        if run.tenant_id != tenant_id or run.created_by.strip().lower() != reading.email \
                or not pull.get("merged"):
            continue
        merged = _aware(pull.get("merged_at")) or _aware(run.updated_at)
        if not _recent(reading, merged):
            continue
        items.append(_item(
            f"finished:pr:{run.id}:{pull.get('number')}", FINISHED, False,
            f"Pull request #{pull.get('number')} for {run.issue.short} merged",
            (run.plan or {}).get("summary"), f"/runs/{run.id}", merged, topic=T_FINISHED))
    return items


# --------------------------------------------------------------------------
# failure_review: a cause the failure review found in the tenant's own code
# --------------------------------------------------------------------------

#: The bell's title for a finding; the detail is the cause sentence.
FINDING_TITLE = "Your agent failed for a cause in your own repository"


def failure_findings(reading: Reading) -> list[dict[str, Any]]:
    """The caller's tenant's open failure findings about agents the caller
    submitted (docs/design/failure-review.md §1; lane FR-4 FINDINGS).

    The `failure-review` schedule found the cause of this agent's failure in
    the tenant's OWN repository, which this platform does not fix, so it is
    the submitter's to act on: a "needs you" item naming the agent -- its
    step, or its runner profile, and its id -- with the cause sentence as its
    detail, linked to the agent's Details where the finding's card is. Read
    through `routes.failurefindings`, the one tenant projection, so the
    platform tenant's fields (`fingerprint`, `firing_id`, the proposed fix)
    never reach this item. The topic is `failed`: muting failures mutes these.
    A finding no later state resolves stays for `WINDOW`, as a question does."""
    from .routes.failurefindings import tenant_findings, tenant_view

    tenant_id = reading.viewer.tenant_id
    docs = [d for d in tenant_findings(reading.db, tenant_id, limit=SCAN)
            if d.get("state") == "open" and d.get("task_id") and _recent(reading, d.get("created_at"))]
    tasks = reading.store.tasks_by_id(tenant_id, [str(d["task_id"]) for d in docs])
    items = []
    for doc in docs:
        task = tasks.get(str(doc["task_id"]))
        if task is None or task.tenant_id != tenant_id \
                or (task.submitted_by or "").strip().lower() != reading.email:
            continue
        view = tenant_view(doc)
        items.append(_item(
            f"failure_review:{view['finding_id']}", TASK, True,
            f"{FINDING_TITLE}: {task.step_id or task.runner_profile} · Task {task.id}",
            view["cause"], f"/agents/recent/{task.id}#failure-findings",
            doc.get("created_at"), topic=T_FAILED))
    return items


#: Every source, by the name a failure is logged under.
SOURCES: tuple[tuple[str, Source], ...] = (
    ("admin", admin_requests),
    ("join_requests", join_requests_to_decide),
    ("approvals", approval_items),
    ("handoffs", handoffs),
    ("awaiting_input", awaiting_input),
    ("questions", agent_questions),
    ("github", github_items),
    ("workspace", workspace_items),
    ("loan", loan_items),
    ("own_join_requests", own_join_requests),
    ("finished", finished_items),
    ("failure_review", failure_findings),
)


# --------------------------------------------------------------------------
# the feed and the read marks
# --------------------------------------------------------------------------

def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


@dataclass(frozen=True)
class _Derived:
    """One derivation: the items shown, what the mutes dropped, the Admin
    pill's count (before the mutes) and whether anything went unread."""

    items: list[dict[str, Any]]
    muted: dict[str, int]
    admin_pending: int
    partial: bool
    #: Workspaces waiting to be created; None for a non-admin or an unread admin source.
    tenants_waiting: int | None = None


#: Each reader's last count and when it was read (`PLATFORM_SECRETS_TTL`).
_SECRET_COUNTS: "weakref.WeakKeyDictionary[Any, tuple[datetime, int | None]]" = weakref.WeakKeyDictionary()
_SECRET_COUNTS_LOCK = threading.Lock()


def platform_secrets_unset(meta: Any, db: Any, now: datetime) -> int | None:
    """How many platform secrets have no enabled version (`not_set` or
    `not_declared`), as Admin › Platform secrets counts them. None when any
    row, or the broker's account documents, could not be read: a lower bound
    drawn as a count would be the one claim nobody measured."""
    with _SECRET_COUNTS_LOCK:
        kept = _SECRET_COUNTS.get(meta)
    if kept is not None and now - kept[0] < PLATFORM_SECRETS_TTL:
        return kept[1]
    try:
        status = psec.platform_secret_status(meta, db, catalogue=PLATFORM_SECRET_CATALOGUE,
                                             now=lambda: now)
        rows = status["secrets"]
        count: int | None = None if status["accounts_unread"] or any(
            r["status"] == psec.UNREADABLE for r in rows) else sum(
            1 for r in rows if r["status"] in (psec.NOT_SET, psec.NOT_DECLARED))
    except Exception as exc:  # noqa: BLE001 - one count, never the feed
        log.warning("notifications: platform secrets unread (%s)", type(exc).__name__)
        count = None
    with _SECRET_COUNTS_LOCK:
        _SECRET_COUNTS[meta] = (now, count)
    return count


class Notifications:
    def __init__(self, db: Any, store: Any, *, now: Callable[[], datetime],
                 sources: Sequence[tuple[str, Source]] = SOURCES,
                 secret_metadata: Any | None = None, joins: Any | None = None) -> None:
        self._db = db
        self._store = store
        self._now = now
        self._sources = tuple(sources)
        #: The platform secrets' metadata reader, the app's one (routes/notifications.py
        #: hands it over); None counts nothing.
        self._secret_metadata = secret_metadata
        #: The join requests' store (`joinrequests.JoinStore`); None rings no join item.
        self._joins = joins

    def _ref(self, viewer: Viewer) -> Any:
        return self._db.collection(COLLECTION).document("rd_" + user_hash(viewer.email))

    def _derive(self, viewer: Viewer) -> "_Derived":
        """Every source, then the person's mutes. Updates are cut to
        `MAX_UPDATES` after the mutes, so muting the successes leaves room
        for the failures; `admin_pending` is counted before them, because it
        drives the Admin pill, not the bell."""
        reading = Reading(db=self._db, store=self._store, viewer=viewer, now=_aware(self._now()),
                          joins=self._joins)
        items: dict[str, dict[str, Any]] = {}
        partial = viewer.preferences_unread
        unread: set[str] = set()
        for name, source in self._sources:
            try:
                found = source(reading)
            except Exception as exc:  # noqa: BLE001 - one source, never the feed
                partial = True
                unread.add(name)
                log.warning("notifications: source %s unread (%s)", name, type(exc).__name__)
                continue
            for item in found:
                items.setdefault(item["id"], item)
        admin_pending = sum(1 for i in items.values() if i["group"] == ADMIN) \
            if viewer.is_admin else 0
        # Tenants' "N waiting": the workspace requests among the admin items,
        # not the Claude-account ones (People and roles counts both).
        tenants_waiting = sum(1 for i in items.values()
                              if i["group"] == ADMIN and i["id"].startswith("admin:workspace:")) \
            if viewer.is_admin and "admin" not in unread else None
        muted = {"needs_you": 0, "updates": 0}
        needs, updates = [], []
        for item in items.values():
            if not item["needs_you"] and item["at"] < reading.now - WINDOW:
                continue
            if item["topic"] in viewer.muted:
                muted["needs_you" if item["needs_you"] else "updates"] += 1
                continue
            (needs if item["needs_you"] else updates).append(item)
        needs.sort(key=lambda i: (i["at"], i["id"]))
        updates.sort(key=lambda i: (i["at"], i["id"]), reverse=True)
        return _Derived(needs + updates[:MAX_UPDATES], muted, admin_pending, partial,
                        tenants_waiting)

    def _marks(self, viewer: Viewer) -> tuple[dict[str, datetime], datetime | None]:
        snap = self._ref(viewer).get()
        doc = snap.to_dict() if snap.exists else None
        if not doc:
            return {}, None
        marks: dict[str, datetime] = {}
        for mark in doc.get("marks") or []:
            if isinstance(mark, Mapping) and mark.get("id"):
                marks[str(mark["id"])] = _aware(mark.get("at")) or _EPOCH
        return marks, _aware(doc.get("all_read_at"))

    def feed(self, viewer: Viewer) -> dict[str, Any]:
        derived = self._derive(viewer)
        partial = derived.partial
        try:
            marks, all_read_at = self._marks(viewer)
        except Exception as exc:  # noqa: BLE001 - unread marks show everything unread
            partial = True
            log.warning("notifications: read marks unread (%s)", type(exc).__name__)
            marks, all_read_at = {}, None
        # A reminder ages a needs-you item's read mark out (§4.6); an update's never.
        period = REMINDER_PERIODS.get(viewer.reminder)
        fresh_after = _aware(self._now()) - period if period else None
        out = []
        for item in derived.items:
            marked = marks.get(item["id"])
            all_read = all_read_at is not None and item["at"] <= all_read_at
            if item["needs_you"] and fresh_after is not None:
                marked = marked if marked is not None and marked >= fresh_after else None
                all_read = all_read and all_read_at >= fresh_after
            read = marked is not None or all_read
            out.append({**item, "at": _iso(item["at"]), "read": read})
        return {
            "items": out,
            "unread_needs_you": sum(1 for i in out if i["needs_you"] and not i["read"]),
            "unread_updates": sum(1 for i in out if not i["needs_you"] and not i["read"]),
            "admin_pending": derived.admin_pending,
            "tenants_waiting": derived.tenants_waiting,
            "platform_secrets_unset": self._platform_secrets_unset(viewer),
            "muted": derived.muted,
            "read_at": _iso(all_read_at),
            "partial": partial,
        }

    def _platform_secrets_unset(self, viewer: Viewer) -> int | None:
        if not viewer.is_admin:
            return None
        meta = self._secret_metadata
        if meta is None:
            return None
        return platform_secrets_unset(meta, self._db, _aware(self._now()))

    def _write(self, viewer: Viewer, ids: Iterable[str], *, all_read: bool) -> None:
        now = _aware(self._now())
        fresh = [str(i)[:MAX_ID_CHARS] for i in ids]
        ref = self._ref(viewer)

        @firestore.transactional
        def _apply(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() if snap.exists else None) or {}
            kept: dict[str, datetime] = {}
            for mark in doc.get("marks") or []:
                at = _aware(mark.get("at")) if isinstance(mark, Mapping) else None
                if at is not None and at >= now - KEEP_READS and mark.get("id"):
                    kept[str(mark["id"])] = at
            for id_ in fresh:
                kept[id_] = now
            newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:MAX_MARKS]
            all_read_at = now if all_read else _aware(doc.get("all_read_at"))
            if all_read_at is not None and all_read_at < now - KEEP_READS:
                all_read_at = None
            txn.set(ref, {
                "user_hash": user_hash(viewer.email),
                "marks": [{"id": id_, "at": at} for id_, at in newest],
                "all_read_at": all_read_at,
                "updated_at": now,
            })

        _apply(self._db.transaction())

    def mark_read(self, viewer: Viewer, ids: Sequence[str]) -> None:
        self._write(viewer, ids, all_read=False)

    def mark_all_read(self, viewer: Viewer) -> None:
        """Every item the feed shows now, and -- when nothing was muted out of
        it -- anything dated before now. With a mute in force only the shown
        ids are marked, so a muted item unmuted later is unread, which is
        true: it was never seen (§4.6)."""
        derived = self._derive(viewer)
        hidden = derived.muted["needs_you"] + derived.muted["updates"]
        self._write(viewer, [i["id"] for i in derived.items], all_read=hidden == 0)
