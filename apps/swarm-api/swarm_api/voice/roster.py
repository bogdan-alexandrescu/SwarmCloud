"""Voice it's agent roster: what a project set going, with each item's state
(docs/design/voice-it.md §3.12.6).

    GET /v1/voice/projects/{project_id}/roster   (routes/voice_roster.py)

WHAT IT LISTS. Everything the project started, from its `voice_starts` rows
(`kind`, `target_id`: an agent, a workflow or a run), and the follow-ups the
platform started on its behalf: tasks tagged with the project,
`metadata.claude_session == voice:<project>`, the query the ledger makes
(ledger.py). Each row is its kind, id, title, state and progress:

    {kind, id, title, state, done, total}

`state` is one of `workfeed.ROSTER_STATES` (queued, running, waiting on the
owner, done, failed, cancelled), mapped from the platform's own state by
`row_state`; `done`/`total` are a workflow's steps succeeded of its steps
(None for an agent and a run). The planner's delegations are not rows here:
they live on the session's stream as `work.delegation` events, which a
reconnect replays from its last id.

THE READS. The project's starts (one query), the tag query (one query), one
batched read of every started id (`get_all` over the tasks, workflows and
runs they name) and, when a workflow was started, one batched read of its
step tasks, for its progress and its title. No read per row.

PER-TENANT ISOLATION (invariant 9). The project must be the caller's own in
the caller's workspace (`VoiceStore.starts_of`: another person's project,
another tenant's and a missing one are the same 404). The starts are read by
`tenant_id ==` and `project_id ==` and kept only when they are the person's;
the tag query is `tenant_id ==`; every document the batched reads return is
dropped unless its `tenant_id` is the caller's. A title is redacted before it
is served.

NOTHING HERE ADMITS, RESERVES OR RUNS WORK: it reads documents and answers.

swarm-voice's follower (swarm_voice/follow.py) maps the served states with
the same table; tests/unit/voice/test_follow.py holds the two equal.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

from ..redaction import redact
from . import store as vstore
from . import workfeed as vwork

TASKS = "tasks"
WORKFLOWS = "workflows"
RUNS = "issue_runs"
#: The collection each started kind is read from.
COLLECTIONS: Mapping[str, str] = {"agent": TASKS, "workflow": WORKFLOWS, "run": RUNS}

#: The project's tag on everything the platform starts for it (ledger.py).
TAG_PREFIX = "voice:"
#: The longest title served; the page shows one line.
TITLE_MAX = 200

QUEUED, RUNNING, WAITING, DONE, FAILED, CANCELLED = vwork.ROSTER_STATES

#: The platform's states, as the roster names them. A task's and a
#: workflow's are TaskState's values (a workflow's derived state may also be
#: UNKNOWN: not ended, so running); a run's are issueruns.RunState's. A run
#: whose plan waits for approval (PLANNED) or whose issue was judged not ready
#: (NOT_READY) waits on the owner.
STATES: Mapping[str, str] = {
    "SUBMITTED": QUEUED, "QUEUED": QUEUED, "PARKED": QUEUED, "READY": QUEUED,
    "LEASED": RUNNING, "DISPATCHED": RUNNING, "STARTING": RUNNING, "RUNNING": RUNNING,
    "SUCCEEDED": DONE, "FAILED": FAILED, "DEAD_LETTERED": FAILED, "CANCELLED": CANCELLED,
    "UNKNOWN": RUNNING,
    "PLANNING": RUNNING, "APPROVED": RUNNING, "CHECKING": RUNNING, "FIXING": RUNNING,
    "PLANNED": WAITING, "NOT_READY": WAITING,
    "DONE": DONE, "REJECTED": CANCELLED,
}


def row_state(state: Any) -> str | None:
    """The roster's state for a platform state, or None for one it does not know."""
    return STATES.get(str(state or "").upper())


def tag_of(project_id: str) -> str:
    return TAG_PREFIX + project_id


def _title(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(redact(value).text.split())
    return text[:TITLE_MAX]


def _task_title(task: Mapping[str, Any]) -> str | None:
    metadata = task.get("metadata")
    return _title(metadata.get("title")) if isinstance(metadata, Mapping) else None


def _run_title(run: Mapping[str, Any]) -> str | None:
    issue = run.get("issue")
    if not isinstance(issue, Mapping):
        return None
    if issue.get("title"):
        return _title(issue.get("title"))
    repo, number = issue.get("repo"), issue.get("number")
    return _title(f"{repo}#{number}") if repo and number is not None else None


def _step_task_ids(workflow: Mapping[str, Any]) -> list[str]:
    steps = workflow.get("steps") or []
    return [str(s["task_id"]) for s in steps if isinstance(s, Mapping) and s.get("task_id")]


class Roster:
    """One person's project's roster, read from Firestore in their tenant."""

    def __init__(self, db: Any, store: vstore.VoiceStore) -> None:
        self._db = db
        self._store = store

    def _batched(self, collection_ids: Iterable[tuple[str, str]],
                 tenant_id: str) -> dict[tuple[str, str], dict[str, Any]]:
        """One `get_all` over (collection, id) pairs; only this tenant's documents."""
        wanted = {f"{c}/{i}": (c, i) for c, i in collection_ids}
        if not wanted:
            return {}
        refs = [self._db.collection(c).document(i) for c, i in wanted.values()]
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for snap in self._db.get_all(refs):
            data = snap.to_dict() if snap.exists else None
            key = wanted.get(snap.reference.path)
            if key is not None and data and data.get("tenant_id") == tenant_id:
                out[key] = data
        return out

    def _tagged(self, tenant_id: str, project_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = (self._db.collection(TASKS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("metadata.claude_session", "==", tag_of(project_id)))
                .stream())
        return [(snap.id, data) for snap in rows
                if (data := snap.to_dict() or {}).get("tenant_id") == tenant_id]

    def rows(self, *, tenant_id: str, person: str, project_id: str) -> list[dict[str, Any]]:
        """The roster, starts first in the order they were made, then the
        follow-ups no start covers. The same 404 for a project that is not
        this person's in this tenant (`VoiceStore.starts_of`)."""
        starts = self._store.starts_of(tenant_id=tenant_id, person=person, project_id=project_id)
        starts.sort(key=lambda s: str(s.get("at") or ""))
        started = [(str(s["kind"]), str(s["target_id"])) for s in starts
                   if s.get("kind") in COLLECTIONS and s.get("target_id")]
        docs = self._batched(((COLLECTIONS[k], i) for k, i in started), tenant_id)
        step_ids = [t for k, i in started if k == "workflow" and (COLLECTIONS[k], i) in docs
                    for t in _step_task_ids(docs[(WORKFLOWS, i)])]
        steps = self._batched(((TASKS, t) for t in step_ids), tenant_id)

        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        covered: set[str] = set(step_ids)
        for kind, target_id in started:
            if (kind, target_id) in seen:
                continue
            seen.add((kind, target_id))
            row = self._row(kind, target_id, docs.get((COLLECTIONS[kind], target_id)), steps)
            if row is not None:
                out.append(row)
                if kind == "agent":
                    covered.add(target_id)
                elif kind == "run":
                    run = docs.get((RUNS, target_id)) or {}
                    if run.get("planner_task_id"):
                        covered.add(str(run["planner_task_id"]))
        for task_id, task in self._tagged(tenant_id, project_id):
            if task_id in covered:
                continue
            state = row_state(task.get("state"))
            if state is not None and vwork.clean_id(task_id) is not None:
                out.append({"kind": "agent", "id": task_id, "title": _task_title(task),
                            "state": state, "done": None, "total": None})
        return out

    @staticmethod
    def _row(kind: str, target_id: str, doc: Mapping[str, Any] | None,
             steps: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[str, Any] | None:
        """One started item's row; None when it is not readable in this tenant."""
        if doc is None or vwork.clean_id(target_id) is None:
            return None
        state = row_state(doc.get("state"))
        if state is None:
            return None
        title: str | None = None
        done = total = None
        if kind == "agent":
            title = _task_title(doc)
        elif kind == "workflow":
            ids = _step_task_ids(doc)
            total = len(doc.get("steps") or [])
            done = sum(1 for i in ids if str((steps.get((TASKS, i)) or {}).get("state")) == "SUCCEEDED")
            title = next((t for i in ids if (t := _task_title(steps.get((TASKS, i)) or {}))), None)
        else:
            title = _run_title(doc)
        return {"kind": kind, "id": target_id, "title": title, "state": state,
                "done": done, "total": total}
