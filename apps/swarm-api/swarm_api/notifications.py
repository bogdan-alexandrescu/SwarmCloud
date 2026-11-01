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

A SOURCE THAT FAILS FAILS ALONE, AND THE SOURCES RUN AT ONCE. Each source is
a small function of a `Reading`; `Notifications.feed` runs them concurrently
(lane LOAD-SPEED-NOTIFICATIONS, part of #2688: run one by one they were 17-20
serial Firestore round trips, 6.2 s cold for an admin), logs a failure by the
source's name and the exception's type, and serves the rest with
`partial: true`. Their items are merged in `SOURCES` order whatever order
they finish in, so the first source to derive an id still keeps it. A read
two sources share (the caller's own tasks, the tenant's PARKED tasks) is made
once per request (`Reading._once`); every task read is one query, narrowed
to the person IN the query where it narrows at all, and projected to
`TASK_FIELDS`.

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
metadata calls per secret and the bell polls every 30 s. Once a count is
older than that it is still served, and ONE background read refreshes it
(`_refresh_platform_secrets`): no feed waits for Secret Manager except the
first an instance serves, and that one reads it beside the sources, not
after them.

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
from concurrent.futures import ThreadPoolExecutor
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
from .agent_questions import (
    OWNER_QUESTIONS_PARK_KEY,
    blocking_unanswered,
    current_wait,
    owner_questions_hold,
    parked_wait,
    unanswered,
)
from . import workspaces as ws
from .forgeapp import CONNECTIONS, REFRESH_FAILED, connection_id_for, user_hash
from .gittokens import COLLECTION as GIT_TOKENS
from .issueruns import IssueRuns
from .codec import owner_answers_by_index
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


#: The task fields the feed's items read: `Store.notification_tasks`
#: projects every task read here to these, so the prompt (`input`), the
#: checkpoint and the signed submission fields never cross the wire for a
#: bell. The decoder's required fields lead (`codec.task_from_dict`).
TASK_FIELDS = (
    "id", "tenant_id", "created_at", "updated_at", "state", "runner_profile",
    "resource_class", "submitted_by", "metadata", "park_reason", "blocked_by",
    "current_generation", "workflow_id", "step_id", "parent_task_id",
    "cancel_requested", "completed_at", "result_summary", "human_wait",
    "next_eligible_at",
)


@dataclass
class Reading:
    """One feed read: what every source is given. The sources run
    concurrently (`Notifications._derive`), so a read two of them share --
    the caller's own tasks, the tenant's PARKED tasks -- is made once, by
    whichever asks first, under a lock; the others wait for it and share its
    answer, or its failure, which fails each of them alike."""

    db: Any
    store: Any
    viewer: Viewer
    now: datetime
    #: The join requests' store (`joinrequests.JoinStore`); None reads none.
    joins: Any | None = None
    _shared: dict[str, tuple[bool, Any]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def email(self) -> str:
        return self.viewer.email.strip().lower()

    def _once(self, key: str, read: Callable[[], list[Any]]) -> list[Any]:
        with self._lock:
            if key not in self._shared:
                try:
                    self._shared[key] = (True, read())
                except Exception as exc:  # noqa: BLE001 - raised again to every source sharing it
                    self._shared[key] = (False, exc)
            ok, answer = self._shared[key]
        if not ok:
            raise answer
        return answer

    def tasks(self, *, state: TaskState | None = None,
              submitted_by: str | None = None) -> list[Any]:
        """One task read of the feed: the tenant's newest `SCAN`, narrowed in
        the query to `state` and to `submitted_by`, projected to
        `TASK_FIELDS`, newest first."""
        return self.store.notification_tasks(self.viewer.tenant_id, state=state,
                                             submitted_by=submitted_by, limit=SCAN,
                                             fields=TASK_FIELDS)

    def own_tasks(self) -> list[Any]:
        """The caller's newest tasks in their tenant (index
        tasks-tenant-submitter-created), newest first. Read once."""
        return self._once("own", lambda: self.tasks(submitted_by=self.viewer.email))

    def parked(self) -> list[Any]:
        """The tenant's newest PARKED tasks (tasks-tenant-state-created), or
        a personal workspace member's own (tasks-tenant-submitter-state-
        created): read ONCE, shared by `handoffs` and `awaiting_input`."""
        scope = self.viewer.email if self.viewer.member_scope else None
        return self._once("parked", lambda: self.tasks(state=TaskState.PARKED,
                                                       submitted_by=scope))


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


# Approvals is Automate's second page (APPROVALS-MOVE, PR 2486). The console
# still redirects `/approvals` and `/approvals/<id>`, but only for links that
# were already sent: every link built here names the page where it lives.
APPROVALS_HREF = "/automate/approvals"


def _approval_href(approval_id: str) -> str:
    return f"{APPROVALS_HREF}/" + quote(approval_id, safe="")


def approval_items(reading: Reading) -> list[dict[str, Any]]:
    """The tenant's pending approval records, its firings waiting for one, and
    its PLANNED issue runs: the inbox `approvals.inbox` serves, read without
    the inbox's writes (it materialises firings and closes stale merges)."""
    db, tenant_id, viewer = reading.db, reading.viewer.tenant_id, reading.viewer
    items: dict[str, dict[str, Any]] = {}
    docs = approvals.pending(db, tenant_id, limit=SCAN)
    live = approvals.live_merges(db, docs)
    for doc in docs:
        expires = _aware(doc.get("expires_at"))
        if expires is not None and expires <= reading.now:
            continue
        if doc.get("kind") == approvals.MERGE and str(doc.get("approval_id")) not in live:
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
            APPROVALS_HREF, firing.get("fired_at"), topic=T_APPROVAL))

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
    A PARKED merge step held by owner questions is no hand-off: its item, if
    it has one, is `_held_merge_item`'s, linked to Details (#2313).

    An open hand-off is "Agent needs you" for everyone who may take control
    of it (`handoff.may_control`, §5.1): its submitter, and the tenant's
    members while the submitter has "Let any member take control" on (or the
    task has no human owner). That is the bell parking fast relies on: the
    hold lasts a minute unless someone opens the view. Each request is its
    own id, so a second hand-off in one attempt is unread again."""
    from .handoff import may_control

    items = []
    scope = reading.viewer.email if reading.viewer.member_scope else None
    for task in reading.tasks(state=TaskState.RUNNING, submitted_by=scope):
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
    asked: dict[str, set[int]] | None = None
    for task in reading.parked():
        # The caller's own, out of the shared read: the same rows the
        # tenant's PARKED page held for them before it was shared.
        if task.submitted_by != reading.viewer.email or task.state != TaskState.PARKED \
                or task.park_reason != ParkReason.HUMAN_REQUIRED:
            continue
        hold = _owner_questions_hold(task)
        if hold is not None:
            if asked is None:
                asked = _asked_indices(reading)
            item = _held_merge_item(reading, task, hold, asked)
            if item is not None:
                items.append(item)
            continue
        items.append(_item(
            f"task:handoff:{task.id}:{task.current_generation}", TASK, True,
            "Your agent parked until you take over its browser", f"Task {task.id}",
            f"/agents/waiting/{task.id}/browser", task.updated_at, topic=T_QUESTION))
    return items


#: The merge step's owner-question hold, as its park records it in
#: `blocked_by` (`agent_worker.lifecycle._park_for_owner_questions`); the
#: same spelling `waiting_detail.OWNER_QUESTIONS_PHASE` reads.
OWNER_QUESTIONS_PHASE = "owner_questions"


def _owner_questions_hold(task: Any) -> Mapping[str, Any] | None:
    """The owner-question hold this HUMAN_REQUIRED park records, else None (a hand-off)."""
    for entry in task.blocked_by or ():
        if (isinstance(entry, Mapping) and entry.get("reason") == ParkReason.HUMAN_REQUIRED.value
                and entry.get("park_phase") == OWNER_QUESTIONS_PHASE):
            hold = entry.get(OWNER_QUESTIONS_PARK_KEY)
            return hold if isinstance(hold, Mapping) else {}
    return None


def _asked_indices(reading: Reading) -> dict[str, set[int]]:
    """Which questions already ring from the task that asked them: each
    task `agent_questions` lists, with the indices its item stands for."""
    asked: dict[str, set[int]] = {}
    for task in reading.own_tasks():
        found = _asked_item(reading, task)
        if found is not None:
            asked[task.id] = found[1]
    return asked


def _lane_label(task: Any) -> str:
    """What the owner calls a workflow: its label, which `submit` stores on
    every step as `metadata.unit` (`swarm_mcp.workflows.stored_names`), then
    its title, then the step's own name."""
    metadata = task.metadata if isinstance(task.metadata, Mapping) else {}
    for key in ("unit", "title"):
        value = " ".join(str(metadata.get(key) or "").split())
        if value:
            return value
    return task.step_id or task.runner_profile


def _held_merge_item(reading: Reading, task: Any, hold: Mapping[str, Any],
                     asked: dict[str, set[int]]) -> dict[str, Any] | None:
    """A merge step held by owner questions (#2070), not a browser hand-off
    (MERGE-PARK-BELL, part of #2313; BGD 2026-10-25).

    ONE ITEM PER OPEN QUESTION, NEVER TWO AND NEVER NONE. A question the
    hold names rings already when the step that asked it is listed by
    `agent_questions` for this reader with that index open (`asked`): the
    merge leaves it to that item. A question still open that rings nowhere
    -- its step ended past `WINDOW`, reported fewer questions, is someone
    else's or lies outside the merge's workflow -- is counted here, and the
    merge's own item, "<lane> needs your answers to N questions to merge",
    opens the merge's Details, where the held-questions card lists every
    one with its answer control. Not `/browser`: a merge step has none.

    A hold every open question of which rings elsewhere is no item. So is
    one whose every question is answered: the answer that clears the last
    one returns the merge to READY in its own transaction
    (`agent_questions._hold_cleared`). A hold that cannot be read whole
    (`owner_questions_hold` is None: an `unreadable` count, a malformed
    entry) is always an item, because no answer wakes it and only a
    person's re-run does; its entries that cannot be read count as open.
    """
    named = owner_questions_hold({"state": TaskState.PARKED.value,
                                  "park_reason": ParkReason.HUMAN_REQUIRED.value,
                                  "blocked_by": [{"reason": ParkReason.HUMAN_REQUIRED.value,
                                                  OWNER_QUESTIONS_PARK_KEY: dict(hold)}]})
    readable = named is not None
    unreadable = 0
    if named is None:
        named = []
        raw = hold.get("questions")
        for q in raw if isinstance(raw, list) else ():
            asker = q.get("task_id") if isinstance(q, Mapping) else None
            index = q.get("question_index") if isinstance(q, Mapping) else None
            if (isinstance(asker, str) and asker and not isinstance(index, bool)
                    and isinstance(index, int) and index >= 0):
                named.append((asker, index))
            else:
                unreadable += 1
        recorded = hold.get("unreadable")
        if not isinstance(recorded, bool) and isinstance(recorded, int) and recorded > 0:
            unreadable += recorded
    askers = reading.store.tasks_by_id(reading.viewer.tenant_id, {t for t, _ in named})
    rings_nowhere = 0
    for asker_id, index in dict.fromkeys(named):
        asker = askers.get(asker_id)
        if (asker is not None and asker.workflow_id == task.workflow_id
                and index in owner_answers_by_index(asker.metadata)):
            continue
        if index not in asked.get(asker_id, ()):
            rings_nowhere += 1
    count = rings_nowhere + unreadable
    if readable and count == 0:
        return None
    count = max(count, 1)
    return _item(
        f"task:handoff:{task.id}:{task.current_generation}", TASK, True,
        f"{_lane_label(task)} needs your answers to {count} "
        f"question{'s' if count != 1 else ''} to merge",
        f"{task.step_id or task.runner_profile} · Task {task.id}",
        f"/agents/waiting/{task.id}/details", task.updated_at, topic=T_QUESTION)


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
    items = []
    for task in approvals.open_input_steps(reading.parked(), reading.viewer.tenant_id,
                                           reading.now):
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
        found = _asked_item(reading, task)
        if found is not None:
            items.append(found[0])
    return items


def _asked_item(reading: Reading, task: Any) -> tuple[dict[str, Any], set[int]] | None:
    """`task`'s questions item, with the question indices it stands for, else
    None: the one rule `agent_questions` lists by and `handoffs` counts a
    held merge's questions by, so the two never disagree about which
    question already rings."""
    wait = parked_wait(task)
    if wait is not None:
        _, adopted = current_wait(task)
        open_ = blocking_unanswered(wait, task.metadata if adopted else None)
        if not open_:
            return None
        return _item(
            f"task:questions:{task.id}", TASK, True,
            f"Your agent is waiting for your answer to {len(open_)} "
            f"question{'s' if len(open_) != 1 else ''}",
            f"{task.step_id or task.runner_profile} · Task {task.id}",
            f"/agents/recent/{task.id}#questions", task.updated_at, topic=T_QUESTION), set(open_)
    count = _question_count(task)
    if count <= 0 or task.state not in _ENDED:
        return None
    if unanswered(count, task.metadata) == 0:
        return None
    ended = task.completed_at or task.updated_at
    if not _recent(reading, ended):
        return None
    answered = owner_answers_by_index(task.metadata)
    return _item(
        f"task:questions:{task.id}", TASK, True,
        f"Your agent asked {count} question{'s' if count != 1 else ''}",
        f"{task.step_id or task.runner_profile} · Task {task.id}",
        f"/agents/recent/{task.id}#questions", ended,
        topic=T_QUESTION), {i for i in range(count) if i not in answered}


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


#: Each reader's background refresh while one runs: at most one per reader.
_SECRET_REFRESHES: "weakref.WeakKeyDictionary[Any, threading.Thread]" = weakref.WeakKeyDictionary()


def platform_secrets_unset(meta: Any, db: Any, now: datetime) -> int | None:
    """How many platform secrets have no enabled version (`not_set` or
    `not_declared`), as Admin › Platform secrets counts them. None when any
    row, or the broker's account documents, could not be read: a lower bound
    drawn as a count would be the one claim nobody measured.

    STALE WHILE IT REFRESHES. A count read within `PLATFORM_SECRETS_TTL` is
    served as it is. An older one is served too, and one background thread
    reads the next (never two for one reader): the badge is at most one read
    behind, and no admin's feed waits on Secret Manager for it. Only a reader
    with no count yet is read inline."""
    with _SECRET_COUNTS_LOCK:
        kept = _SECRET_COUNTS.get(meta)
        if kept is not None and now - kept[0] >= PLATFORM_SECRETS_TTL:
            running = _SECRET_REFRESHES.get(meta)
            if running is None or not running.is_alive():
                thread = threading.Thread(target=_read_platform_secrets, args=(meta, db, now),
                                          name="notifications-platform-secrets", daemon=True)
                _SECRET_REFRESHES[meta] = thread
                thread.start()
    if kept is not None:
        return kept[1]
    return _read_platform_secrets(meta, db, now)


def wait_platform_secrets_refresh(meta: Any, *, timeout: float) -> bool:
    """Wait for `meta`'s background refresh, if one runs; True once none does.
    For a caller that must see the refreshed count (a test, a shutdown)."""
    with _SECRET_COUNTS_LOCK:
        running = _SECRET_REFRESHES.get(meta)
    if running is not None:
        running.join(timeout)
        return not running.is_alive()
    return True


def _read_platform_secrets(meta: Any, db: Any, now: datetime) -> int | None:
    """One count from Secret Manager, kept for `platform_secrets_unset`."""
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
        for name, found in self._run_sources(reading):
            if isinstance(found, Exception):
                partial = True
                unread.add(name)
                log.warning("notifications: source %s unread (%s)", name, type(found).__name__)
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

    def _run_sources(self, reading: Reading) -> list[tuple[str, Any]]:
        """Every source at once, each on its own thread of a pool that lives
        for this request: (name, its items or the exception it raised), in
        `SOURCES` order. A source's failure is caught on its own thread, so
        it never cancels or delays another's."""

        def run(source: Source) -> Any:
            try:
                return source(reading)
            except Exception as exc:  # noqa: BLE001 - one source, never the feed
                return exc

        if len(self._sources) <= 1:
            return [(name, run(source)) for name, source in self._sources]
        with ThreadPoolExecutor(max_workers=len(self._sources),
                                thread_name_prefix="notifications") as pool:
            futures = [(name, pool.submit(run, source)) for name, source in self._sources]
            return [(name, future.result()) for name, future in futures]

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
        # The read marks and the admin's platform count are read beside the
        # sources, not after them: neither depends on what they derive.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="notifications") as pool:
            marks_read = pool.submit(self._marks, viewer)
            secrets_read = pool.submit(self._platform_secrets_unset, viewer)
            derived = self._derive(viewer)
            partial = derived.partial
            try:
                marks, all_read_at = marks_read.result()
            except Exception as exc:  # noqa: BLE001 - unread marks show everything unread
                partial = True
                log.warning("notifications: read marks unread (%s)", type(exc).__name__)
                marks, all_read_at = {}, None
            platform_secrets = secrets_read.result()
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
            "platform_secrets_unset": platform_secrets,
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
