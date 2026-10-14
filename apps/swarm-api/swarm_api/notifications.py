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

A SOURCE THAT FAILS FAILS ALONE. Each source is a small function of a
`Reading`; `Notifications.feed` runs them one by one, logs a failure by the
source's name and the exception's type, and serves the rest with
`partial: true`.

RELEASE IAM HOLDS ARE NOT A SOURCE. A release that waits on an IAM grant is a
GitHub environment approval; the platform keeps no record of it in Firestore,
so there is nothing to derive one from. An issue run's IAM hard stop is a hold
on the run (`issueruns.STOP_IAM`), and it is here, as an approval item.

NOTHING HERE CREATES DEMAND (invariants 1-3): it reads documents and writes one
document of its own; no task, lease or slot is touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import ParkReason, TaskState

from . import approvals, schedulefire
from .agent_questions import unanswered
from . import workspaces as ws
from .forgeapp import CONNECTIONS, REFRESH_FAILED, connection_id_for, user_hash
from .gittokens import COLLECTION as GIT_TOKENS
from .issueruns import IssueRuns, RunState
from .people import LOAN_DECLINED
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

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ENDED = {
    TaskState.SUCCEEDED: "succeeded",
    TaskState.FAILED: "failed",
    TaskState.CANCELLED: "was cancelled",
    TaskState.DEAD_LETTERED: "failed (dead-lettered)",
}
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


@dataclass
class Reading:
    """One feed read: what every source is given. `own_tasks` is read once
    and shared by the sources that need it, and fails each of them alike."""

    db: Any
    store: Any
    viewer: Viewer
    now: datetime
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
          at: Any) -> dict[str, Any]:
    return {"id": id_[:MAX_ID_CHARS], "group": group, "needs_you": needs_you,
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
            f"Workspace {record['workspace_id']}", "/admin/people", record.get("requested_at")))
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
            "/admin/people", loan.get("requested_at")))
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
            doc.get("summary"), _approval_href(approval_id), doc.get("requested_at"))

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
            "/approvals", firing.get("fired_at")))

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
            projected.get("requested_at"))
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
            wait.get("requested_at") or task.updated_at))
    for task in _own_in_state(reading, TaskState.PARKED):
        if task.park_reason != ParkReason.HUMAN_REQUIRED:
            continue
        items.append(_item(
            f"task:handoff:{task.id}:{task.current_generation}", TASK, True,
            "Your agent parked until you take over its browser", f"Task {task.id}",
            f"/agents/waiting/{task.id}/browser", task.updated_at))
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
    2026-10-13)."""
    items = []
    for task in reading.own_tasks():
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
            f"Task {task.id}", f"/agents/recent/{task.id}#questions", ended))
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
            "/access", failed))
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
            f"GitHub account {login}" if login else None, "/access", expired))
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
                      "/setup#workspace", record.get("ready_at"))]
    if state == ws.DENIED and _recent(reading, decision.get("at")):
        return [_item(f"account:workspace:{workspace_id}:denied:{record.get('request_id') or ''}",
                      ACCOUNT, False, "Your workspace request was denied", decision.get("reason"),
                      "/setup#workspace", decision.get("at"))]
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
                  "/setup#claude-account", decision.get("at"))]


# --------------------------------------------------------------------------
# finished: updates, never "needs you"
# --------------------------------------------------------------------------

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
            f"/agents/recent/{task.id}", ended))
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
            f"/workflows/{workflow.workflow_id}", workflow.updated_at))
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
            (run.plan or {}).get("summary"), f"/runs/{run.id}", merged))
    return items


#: Every source, by the name a failure is logged under.
SOURCES: tuple[tuple[str, Source], ...] = (
    ("admin", admin_requests),
    ("approvals", approval_items),
    ("handoffs", handoffs),
    ("questions", agent_questions),
    ("github", github_items),
    ("workspace", workspace_items),
    ("loan", loan_items),
    ("finished", finished_items),
)


# --------------------------------------------------------------------------
# the feed and the read marks
# --------------------------------------------------------------------------

def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


class Notifications:
    def __init__(self, db: Any, store: Any, *, now: Callable[[], datetime],
                 sources: Sequence[tuple[str, Source]] = SOURCES) -> None:
        self._db = db
        self._store = store
        self._now = now
        self._sources = tuple(sources)

    def _ref(self, viewer: Viewer) -> Any:
        return self._db.collection(COLLECTION).document("rd_" + user_hash(viewer.email))

    def _derive(self, viewer: Viewer) -> tuple[list[dict[str, Any]], bool]:
        reading = Reading(db=self._db, store=self._store, viewer=viewer, now=_aware(self._now()))
        items: dict[str, dict[str, Any]] = {}
        partial = False
        for name, source in self._sources:
            try:
                found = source(reading)
            except Exception as exc:  # noqa: BLE001 - one source, never the feed
                partial = True
                log.warning("notifications: source %s unread (%s)", name, type(exc).__name__)
                continue
            for item in found:
                items.setdefault(item["id"], item)
        needs = sorted((i for i in items.values() if i["needs_you"]),
                       key=lambda i: (i["at"], i["id"]))
        updates = sorted((i for i in items.values()
                          if not i["needs_you"] and i["at"] >= reading.now - WINDOW),
                         key=lambda i: (i["at"], i["id"]), reverse=True)[:MAX_UPDATES]
        return needs + updates, partial

    def _marks(self, viewer: Viewer) -> tuple[set[str], datetime | None]:
        snap = self._ref(viewer).get()
        doc = snap.to_dict() if snap.exists else None
        if not doc:
            return set(), None
        ids = {str(m.get("id")) for m in doc.get("marks") or [] if isinstance(m, Mapping)}
        return ids, _aware(doc.get("all_read_at"))

    def feed(self, viewer: Viewer) -> dict[str, Any]:
        items, partial = self._derive(viewer)
        try:
            read_ids, all_read_at = self._marks(viewer)
        except Exception as exc:  # noqa: BLE001 - unread marks show everything unread
            partial = True
            log.warning("notifications: read marks unread (%s)", type(exc).__name__)
            read_ids, all_read_at = set(), None
        out = []
        for item in items:
            read = item["id"] in read_ids or (all_read_at is not None and item["at"] <= all_read_at)
            out.append({**item, "at": _iso(item["at"]), "read": read})
        return {
            "items": out,
            "unread_needs_you": sum(1 for i in out if i["needs_you"] and not i["read"]),
            "unread_updates": sum(1 for i in out if not i["needs_you"] and not i["read"]),
            "admin_pending": sum(1 for i in out if i["group"] == ADMIN) if viewer.is_admin else 0,
            "read_at": _iso(all_read_at),
            "partial": partial,
        }

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
        """Every item the feed derives now, and anything dated before now."""
        items, _ = self._derive(viewer)
        self._write(viewer, [i["id"] for i in items], all_read=True)
