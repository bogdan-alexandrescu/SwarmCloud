"""`swarm_check`: which SwarmCloud work of this project is stuck or stale.

Owner request 2026-10-14 (SC-WATCH): "add a feature to the sc plugin to have
a check every set interval (default 30 min) and see if there are any
workflows or tasks that are stuck or stale and to attach all workflows for a
given claude code session running at the time for that project". This module
is the check; `watch` is the interval, and `launch.for_all` the attach.

READ-ONLY, AND THROUGH THE API. Every read below is a GET the bridge
already makes somewhere else -- the workflow list and one workflow, the task
list, one task, a task's events, `/v1/capacity`, `/v1/tenants/mine` -- and
one more, outside it: a refused merge step's pull request state, from
GitHub's public REST API, unauthenticated (rule d). Nothing
is cancelled, retried, released or re-dispatched: the answer names the item,
why it was flagged, the evidence the API served for it, its console link and
the next command for a person to run.

SCOPED TO ONE REPOSITORY. A workflow belongs to the repository its step tasks
clone (`repository_url`; the frozen `Workflow` has no repository of its own),
and a single task to its own. The default scope is this checkout's `origin`
remote, so a check run in one project never reports another project's work;
`repository: "all"` drops the scope. Outside a checkout with no origin the
check is unscoped, and the answer says so rather than returning nothing.

THE RULES, each with the control that is NOT flagged:

(a) `no_heartbeat` (STALE): RUNNING or STARTING with no heartbeat AND no task
    event for more than `stale_minutes` (default 20). A beat or an event
    inside the window clears it, and so does a task younger than the window.
(b) `waiting_for_capacity` (STUCK): READY or QUEUED for more than
    `stale_minutes`, naming the pool that binds it: a READY task's live
    `waiting_for` (swarm_api.waiting -- admission's own analysis), else the
    scheduler's last `blocked_by`, else the binding pool of its profile from
    ONE `/v1/capacity` read (`render.binding_pool`, what `sc capacity`
    shows). A READY task is invariant 1 working -- it holds nothing -- and is
    flagged only for its age, never as a fault.
(c) `parked` (STUCK): PARKED with HUMAN_REQUIRED (only a person promotes it),
    CI_PENDING for longer than 90 minutes, or DEPENDENCY_INCOMPLETE whose
    dependency FAILED, was CANCELLED or DEAD_LETTERED. Any other park reason
    -- a provider quota window, a retry backoff -- is the platform waiting by
    design, and is not flagged.
    Both (b) and (c) carry `waiting_summary` in their evidence: the API's one
    line on why the task waits (#1782) -- the pull request and the checks
    still pending, the pool that binds -- left out when the API serves none.
(d) `merge_refused` (STUCK): a `merge` step FAILED with `head_moved`,
    `merge_conflict` or `checks_failed`, and its pull request is still
    OPEN. The API serves no pull request state, so it is read from
    GitHub's public REST API (`github_pull_state`: unauthenticated, no
    token is ever sent), once per PR per check. A PR that has since MERGED
    or CLOSED is not flagged (epic #1334: four merged PRs stayed flagged
    `head_moved` for good). When that read fails the flag stays, and its
    evidence says "PR state not read" and why.
(e) `state_disagrees` (STALE): a workflow whose stored state and derived
    state disagree about whether it is finished -- the stale RUNNING cache
    (stored RUNNING, every step done) or the reverse -- or one the API served
    without deriving at all. A stored QUEUED under a derived RUNNING is the
    cache lagging between two reads (`workflows.state_of`), and is not
    flagged.
(f) `awaiting_input`: PARKED with AWAITING_INPUT, a workflow step waiting for
    a member of its tenant to answer (docs/design/human-steps.md §4.5). It is
    INFORMATION, not a flag: listed under `awaiting_input` with its tenant,
    its window and how to answer it, never under `flagged`, because a person
    who has not answered yet is the step working as designed and it holds
    nothing (invariant 1). It is flagged STUCK only once `expires_at` has
    passed by more than `EXPIRY_TICKS` ticks of the expiry sweep: the
    schedule tick ends an overdue wait within one, so a wait still parked
    after two means the sweep is not running. HUMAN_REQUIRED stays STUCK
    under (c), as before.
(g) `owner_questions` (PLUGIN-OWNER-QUESTIONS, part of #1904): this
    session's tasks in this project, finished in the last
    `OWNER_QUESTIONS_DAYS`, whose agent asked the owner questions (its
    `questions.json`) with an index the task's `owner_answers` does not
    answer. INFORMATION, like (f), never a flag: a sweep on 2026-10-22 found
    36 such questions across two session tags that no session had raised,
    because the run that saw them end dropped them, or ended before a
    compaction. Each entry carries the open questions whole, read by
    `progress.owner_questions` (redacted, by name) -- once per task while
    the bridge lives, since a finished task's questions never change. A
    read that fails is under `unchecked` with its reason, and tried again
    on the next check. The tasks come from ONE server-side read,
    `GET /v1/tasks/open-questions` (#2835), whatever the session's volume;
    paging the session's finished tasks is only the fallback for a
    deployment that does not serve it, and says so under `unchecked`.

Every age is measured from evidence and says which: the event that entered
the state when the newest page of events holds it, else a LOWER BOUND from
the oldest event read ("at least"). `updated_at` is never used for an age:
the scheduler rewrites it on every pass that records `blocked_by`.
"""

from __future__ import annotations

import copy
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from swarm_common.states import TERMINAL_STATES

from . import awaitinput, checkout, progress, render, session, workflows
from .client import SwarmClient, SwarmError, task_id_of
from .follow import console_link, waiting_brief
from .sc import WORKFLOW_PAGE_SIZE

#: (a) and (b)'s window, in minutes: the owner's "N min (default 20)".
DEFAULT_STALE_MINUTES = 20
#: (c): a merge step's CI wait is flagged past this many minutes.
CI_PENDING_MINUTES = 90

#: How many of a task's newest events one read asks for: enough to hold the
#: event that entered its state, past a few heartbeats.
EVENT_PAGE = 20

#: Running workflows read in detail (one GET each). Beyond it the list names
#: them, says they were not checked, and costs nothing more -- the same
#: ceiling `sc.WORKFLOW_DETAIL_READS` gives `/sc attach --all`.
MAX_WORKFLOW_DETAILS = 30
#: The running-workflow list is read to its last page, through
#: `sc.walk_running_workflows`, asking the API for this session's work only
#: (CHECK-FILTER, #1749); its page size is `sc.WORKFLOW_PAGE_SIZE`, imported
#: above, and the single tasks are read to their last page the same way.
#: Task reads (one task, or one page of its events) per check. A check runs
#: every 30 minutes by default; a project with more candidates than this is
#: told which were not checked rather than made to wait.
MAX_TASK_READS = 80

#: GitHub's public REST API, read for a refused merge step's PR state. The
#: repository is public, so the read is unauthenticated: no token is sent.
GITHUB_API = "https://api.github.com"
#: Seconds one PR-state read may take. A check runs every 30 minutes; a slow
#: GitHub costs this much per PR, and the flag stays (unread) rather than wait.
PULL_STATE_TIMEOUT = 5

MERGE_PROFILE = "merge"
MERGE_CODES = ("head_moved", "merge_conflict", "checks_failed")

QUIET_STATES = ("RUNNING", "STARTING")
WAITING_STATES = ("READY", "QUEUED")
_TERMINAL = frozenset(s.value for s in TERMINAL_STATES)
_FAILED_DEPENDENCY = frozenset({"FAILED", "CANCELLED", "DEAD_LETTERED"})

STALE = "STALE"
STUCK = "STUCK"
#: (f)'s kind: a wait for a person, reported apart from what is flagged.
AWAITING_INPUT = "awaiting_input"
#: How often the schedule tick runs its expiry sweep
#: (`terraform/modules/scheduler/variables.tf`, `schedule_tick_schedule`:
#: every minute), and how many of them an overdue wait may outlive before it
#: is STUCK. One tick can miss a wait that expired just after it read; two
#: cannot, unless the sweep is not running.
EXPIRY_TICK_SECONDS = 60
EXPIRY_TICKS = 2

#: The order flagged items are reported in: what needs a person first.
_RULE_ORDER = {"merge_refused": 0, "parked": 1, AWAITING_INPUT: 1, "no_heartbeat": 2, "state_disagrees": 3,
               "waiting_for_capacity": 4}

ALL = ("all", "*")


# --------------------------------------------------------------------------
# The repository scope
# --------------------------------------------------------------------------


def repo_key(url: Any) -> str | None:
    """`host/owner/repo`, lower-cased, for any spelling of one repository --
    https (with or without `.git`, with masked userinfo), ssh://, git@host:,
    or bare `owner/repo` (GitHub). None for anything that names none."""
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text and "@" not in text and ":" not in text:
        # `host/owner/repo` (`github.com/acme/app`, what this check's own
        # answer prints) already names its host; only a bare `owner/repo` is
        # GitHub's. A GitHub owner never holds a dot, a host always does.
        # Prefixing both read `github.com/acme/app` as owner `github.com`
        # (epic #1334).
        first = text.strip("/").split("/", 1)[0]
        text = f"https://{text}" if "." in first else f"https://github.com/{text}"
    try:
        parts = urlsplit(checkout.https_url(text))
    except SwarmError:
        return None
    segments = [s for s in parts.path.strip("/").split("/") if s]
    if not parts.hostname or len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{parts.hostname}/{owner}/{repo}".lower() if owner and repo else None


def resolve_scope(repository: Any) -> dict[str, Any]:
    """`{key, source, note?}`: the repository a check is scoped to.

    None or "origin": this checkout's origin remote, and when there is none
    the check is UNSCOPED with `note` saying why -- an empty answer would
    read as "nothing is stuck". "all": unscoped by request. Anything else must
    name a repository, or the call is refused: a typo is not a scope.
    """
    text = str(repository).strip() if repository is not None else ""
    if text.lower() in ALL:
        return {"key": None, "source": "all"}
    if text in ("", "origin"):
        try:
            got = checkout._git(checkout.directory(), "remote", "get-url", "origin")
            url = checkout._out(got) if got.returncode == 0 else ""
        except (OSError, SwarmError):
            url = ""
        key = repo_key(url)
        if key is None:
            return {"key": None, "source": "all",
                    "note": ("this session's directory has no `origin` remote naming a repository, "
                             "so the check is NOT scoped to a project: every repository of the "
                             "tenant is checked. Pass `repository` to scope it")}
        return {"key": key, "source": "origin"}
    key = repo_key(text)
    if key is None:
        raise SwarmError(
            f"swarm_check: `repository` {text!r} names no repository; give a URL, `owner/repo`, "
            "\"origin\" (this checkout's remote, the default) or \"all\". Nothing was read"
        )
    return {"key": key, "source": "given"}


def _in_scope(key: str | None, urls: list[Any]) -> bool:
    return key is None or any(repo_key(u) == key for u in urls)


def pull_request_link(repository_url: Any, number: Any) -> str | None:
    """The PR's page on its forge, from the repository the step cloned."""
    key = repo_key(repository_url)
    if key is None or not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    return f"https://{key}/pull/{number}"


def github_pull_state(key: str, number: int) -> str:
    """`OPEN`, `MERGED` or `CLOSED`: pull request `number` of repository
    `key` (`github.com/owner/repo`), from GitHub's public REST API.

    UNAUTHENTICATED, ALWAYS. The request carries no Authorization header and
    no credential of any kind is read to build it: the repositories this
    platform merges into are public, and a token sent here would be a token
    sent somewhere the platform never needed it. Raises SwarmError when the
    state could not be read -- another forge, a rate limit, a timeout."""
    host, _, path = str(key).partition("/")
    owner, _, repo = path.partition("/")
    if host != "github.com" or not owner or not repo:
        raise SwarmError(f"{key} is not a GitHub repository, and GitHub is the only forge read")
    url = f"{GITHUB_API}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/pulls/{int(number)}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "swarm-mcp-check"})
    try:
        with urllib.request.urlopen(request, timeout=PULL_STATE_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SwarmError(f"GitHub answered HTTP {exc.code} for {url}") from None
    except (OSError, ValueError) as exc:
        raise SwarmError(f"GitHub could not be read for {url}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise SwarmError(f"GitHub served no pull request at {url}")
    if data.get("merged") is True or data.get("merged_at"):
        return "MERGED"
    state = str(data.get("state") or "").lower()
    if state in ("open", "closed"):
        return state.upper()
    raise SwarmError(f"GitHub served pull request state {data.get('state')!r} at {url}")


class _PullStates:
    """The PR states one check reads: each `(repository, number)` at most
    once, whatever its outcome, however many steps or tenants name it.
    Thread-safe."""

    def __init__(self, reader: Callable[[str, int], str] | None = None) -> None:
        self.reader = reader
        self.lock = threading.Lock()
        self.seen: dict[tuple[str, int], tuple[str | None, str | None]] = {}

    def read(self, key: str, number: int) -> tuple[str | None, str | None]:
        """`(state, None)`, or `(None, why it was not read)`."""
        with self.lock:
            if (key, number) not in self.seen:
                # Looked up at call time, so the module's reader is the one a
                # test (or a caller) put there.
                reader = self.reader or github_pull_state
                try:
                    self.seen[(key, number)] = (reader(key, number), None)
                except SwarmError as exc:
                    self.seen[(key, number)] = (None, str(exc))
            return self.seen[(key, number)]


# --------------------------------------------------------------------------
# Reading, under one budget
# --------------------------------------------------------------------------


class _Reads:
    """The task reads one check makes: counted, and refused past the budget
    (None, and the item lands in `unchecked`). Thread-safe."""

    def __init__(self, client: SwarmClient, budget: int) -> None:
        self.client = client
        self.left = budget
        self.lock = threading.Lock()
        self._capacity: dict[str, Any] | None = None
        self._capacity_error: str | None = None
        self._capacity_read = False

    def _take(self) -> bool:
        with self.lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True

    def task(self, task_id: str) -> dict[str, Any] | None:
        if not self._take():
            return None
        data = self.client.request("GET", f"/v1/tasks/{task_id}")
        task = data.get("task") if isinstance(data, dict) and isinstance(data.get("task"), dict) else data
        return task if isinstance(task, dict) else None

    def events(self, task_id: str) -> list[dict[str, Any]] | None:
        """The task's newest EVENT_PAGE events, newest first."""
        if not self._take():
            return None
        query = urllib.parse.urlencode({"order": "desc", "limit": EVENT_PAGE})
        data = self.client.request("GET", f"/v1/tasks/{task_id}/events?{query}")
        events = data.get("events") if isinstance(data, dict) else None
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

    def capacity(self) -> tuple[dict[str, Any] | None, str | None]:
        """`/v1/capacity`, read at most once per check."""
        with self.lock:
            if not self._capacity_read:
                self._capacity_read = True
                try:
                    got = self.client.request("GET", "/v1/capacity")
                    self._capacity = got if isinstance(got, dict) else None
                except SwarmError as exc:
                    self._capacity_error = str(exc)
            return self._capacity, self._capacity_error


class _Budget(Exception):
    """The read budget ran out before this item could be checked."""


def _need(value: Any) -> Any:
    if value is None:
        raise _Budget()
    return value


# --------------------------------------------------------------------------
# Ages, from evidence
# --------------------------------------------------------------------------


def _minutes(delta_seconds: float) -> int:
    return max(0, int(delta_seconds // 60))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _entered(events: list[dict[str, Any]], state: str, created: datetime | None) -> dict[str, Any]:
    """When the task entered `state`, from its newest events (newest first).

    `{since, exact, source}`: exact when the page holds the event that entered
    the state; else a LOWER bound -- the oldest event read when the page was
    full (older ones exist), or the task's creation when it was not (every
    event was read and none entered the state again since)."""
    wanted = state.lower()
    for event in events:
        if str(event.get("type") or "").lower() == wanted:
            at = render.parse_time(event.get("at"))
            if at is not None:
                return {"since": at, "exact": True, "source": f"its `{wanted}` event"}
    if len(events) >= EVENT_PAGE:
        oldest = render.parse_time(events[-1].get("at"))
        if oldest is not None:
            return {"since": oldest, "exact": False,
                    "source": f"the oldest of its newest {EVENT_PAGE} events (the `{wanted}` event is older)"}
    return {"since": created, "exact": False, "source": "its creation (no later event entered the state)"}


def _age_evidence(entered: dict[str, Any], now: datetime) -> dict[str, Any]:
    since = entered["since"]
    if since is None:
        return {"in_state_since": None, "age_minutes": None, "age_source": "no timestamp was served"}
    return {
        "in_state_since": _iso(since),
        "age_minutes": _minutes((now - since).total_seconds()),
        "age_is": "exact" if entered["exact"] else "at least",
        "age_source": entered["source"],
    }


# --------------------------------------------------------------------------
# The rules over one task
# --------------------------------------------------------------------------


def _item(kind: str, rule: str, reason: str, evidence: dict[str, Any], task: dict[str, Any],
          where: dict[str, Any]) -> dict[str, Any]:
    task_id = task_id_of(task)
    item: dict[str, Any] = {
        "kind": kind,
        "rule": rule,
        "reason": reason,
        "workspace": where.get("workspace"),
        "workflow_id": where.get("workflow_id"),
        "step_id": where.get("step_id"),
        "task_id": task_id,
        "state": task.get("state"),
        "evidence": evidence,
    }
    link = console_link(task)
    if link:
        item["console"] = link
    item["next"] = f"/sc status {where.get('workflow_id') or task_id}"
    return item


def _quiet(task: dict[str, Any], reads: _Reads, now: datetime, window: int) -> dict[str, Any] | None:
    """(a): RUNNING/STARTING with neither a beat nor an event inside the window."""
    created = render.parse_time(task.get("created_at"))
    if created is not None and (now - created).total_seconds() <= window * 60:
        return None
    task_id = str(task_id_of(task))
    if task.get("heartbeat") is None:
        # A workflow read serves its step tasks without the lease's beat
        # (`heartbeat` null: not read); the task route reads it
        # (`swarm_api.heartbeats`).
        task = {**task, **_need(reads.task(task_id))}
    beat = render.parse_time(task.get("heartbeat_at"))
    if beat is not None and (now - beat).total_seconds() <= window * 60:
        return None
    events = _need(reads.events(task_id))
    newest = next((e for e in events if render.parse_time(e.get("at")) is not None), None)
    event_at = render.parse_time(newest.get("at")) if newest else None
    last = max([t for t in (beat, event_at) if t is not None], default=None)
    basis = "its last heartbeat or event"
    if last is None:
        last = render.parse_time(task.get("started_at")) or created
        basis = "its start (no heartbeat and no event was served)"
    if last is None or (now - last).total_seconds() <= window * 60:
        return None
    quiet = _minutes((now - last).total_seconds())
    return {
        "kind": STALE,
        "rule": "no_heartbeat",
        "reason": f"{task.get('state')} with no heartbeat and no event for {quiet} min (window {window} min)",
        "evidence": {
            "heartbeat_at": task.get("heartbeat_at"),
            "heartbeat": task.get("heartbeat"),
            "heartbeat_grace_seconds": task.get("heartbeat_grace_seconds"),
            "last_event": {"type": newest.get("type"), "at": newest.get("at")} if newest else None,
            "quiet_since": _iso(last),
            "quiet_since_is": basis,
            "quiet_minutes": quiet,
            "window_minutes": window,
        },
    }


def _binding(task: dict[str, Any], reads: _Reads) -> dict[str, Any]:
    """The pool that binds a waiting task, and where that was read."""
    waiting = task.get("waiting_for")
    if isinstance(waiting, dict):
        lead = waiting.get("lead")
        if isinstance(lead, dict) and lead.get("pool"):
            return {"pool": lead.get("pool"), "pool_state": lead.get("state"), "active": lead.get("active"),
                    "limit": lead.get("limit"), "units": lead.get("units"),
                    "source": "the task's live waiting_for (admission's analysis, read now)"}
        if waiting.get("admissible_now") is True:
            return {"pool": None, "source": "the task's live waiting_for",
                    "note": "every required pool is open now: the next scheduler pass may admit it"}
    for blocker in task.get("blocked_by") or []:
        if isinstance(blocker, dict) and blocker.get("pool"):
            return {"pool": blocker.get("pool"), "reason": blocker.get("reason"),
                    "active": blocker.get("active"), "limit": blocker.get("limit"),
                    "source": "the scheduler's last pass (blocked_by)"}
    capacity, error = reads.capacity()
    profile = task.get("runner_profile")
    if capacity is None:
        return {"pool": None, "source": "/v1/capacity", "note": f"capacity could not be read: {error}"}
    pools = {p["name"]: p for p in capacity.get("pools") or [] if isinstance(p, dict) and p.get("name")}
    spec = (capacity.get("runner_profiles") or {}).get(profile)
    if not isinstance(spec, dict):
        return {"pool": None, "source": "/v1/capacity", "note": f"the capacity read names no profile {profile!r}"}
    units = render._int_or_none(spec.get("units")) or 1
    bound = render.binding_pool(list(spec.get("pools") or []), pools, units)
    out = {"pool": bound.pool, "active": bound.active, "limit": bound.limit, "room": bound.room,
           "source": f"/v1/capacity: the pool that binds profile {profile!r} now"}
    if bound.paused:
        out["pool_state"] = "paused"
    elif bound.unknown:
        out["pool_state"] = "unknown"
    elif bound.unconfigured:
        out["note"] = "no required pool is configured: nothing caps this profile"
    return out


def _with_summary(evidence: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """`evidence`, with the API's one line on why the task waits (#1782).

    Left out, never null, when the API served none: an API older than
    WAIT-API keeps exactly the evidence it had.
    """
    brief = waiting_brief(task)
    if brief is not None:
        evidence["waiting_summary"] = brief["summary"]
    return evidence


def _waiting(task: dict[str, Any], reads: _Reads, now: datetime, window: int) -> dict[str, Any] | None:
    """(b): READY or QUEUED longer than the window, with its binding pool."""
    created = render.parse_time(task.get("created_at"))
    if created is not None and (now - created).total_seconds() <= window * 60:
        return None
    task_id = str(task_id_of(task))
    entered = _entered(_need(reads.events(task_id)), str(task.get("state")), created)
    if entered["since"] is None or (now - entered["since"]).total_seconds() <= window * 60:
        return None
    if task.get("state") == "READY" and not isinstance(task.get("waiting_for"), dict):
        # A workflow read serves no `waiting_for`; the task route computes it live.
        task = {**task, **_need(reads.task(task_id))}
    evidence = _age_evidence(entered, now)
    evidence["window_minutes"] = window
    evidence["binding"] = _binding(task, reads)
    _with_summary(evidence, task)
    pool = evidence["binding"].get("pool")
    held = f", bound by pool {pool}" if pool else ""
    return {
        "kind": STUCK,
        "rule": "waiting_for_capacity",
        "reason": (f"{task.get('state')} for {evidence['age_is']} {evidence['age_minutes']} min"
                   f"{held} (window {window} min); it holds no capacity while it waits"),
        "evidence": evidence,
    }


def _parked(task: dict[str, Any], reads: _Reads, now: datetime,
            dependencies: dict[str, Any] | None) -> dict[str, Any] | None:
    """(c): HUMAN_REQUIRED, CI_PENDING past 90 min, or a dependency that failed.
    (f): AWAITING_INPUT, as information, or STUCK once its sweep is overdue."""
    reason = task.get("park_reason")
    if reason == "AWAITING_INPUT":
        return _awaiting(task, now)
    if reason == "DEPENDENCY_INCOMPLETE":
        failed = {k: v for k, v in (dependencies or {}).items() if v in _FAILED_DEPENDENCY}
        if not failed:
            return None
        names = ", ".join(f"{k} ({v})" for k, v in sorted(failed.items()))
        return {
            "kind": STUCK, "rule": "parked",
            "reason": f"PARKED on DEPENDENCY_INCOMPLETE, and its dependency will not complete: {names}",
            "evidence": _with_summary({"park_reason": reason, "dependencies": dependencies}, task),
        }
    if reason not in ("HUMAN_REQUIRED", "CI_PENDING"):
        return None
    created = render.parse_time(task.get("created_at"))
    entered = _entered(_need(reads.events(str(task_id_of(task)))), "PARKED", created)
    evidence = _age_evidence(entered, now)
    evidence["park_reason"] = reason
    age = evidence["age_minutes"]
    if reason == "CI_PENDING":
        if age is None or age <= CI_PENDING_MINUTES:
            return None
        evidence["window_minutes"] = CI_PENDING_MINUTES
        text = (f"PARKED on CI_PENDING for {evidence['age_is']} {age} min (over {CI_PENDING_MINUTES}): "
                "its pull request's checks have not finished")
    else:
        # IN THE API'S OWN WORDS when it served them (#2313): a merge step held
        # by the owner's questions parks HUMAN_REQUIRED too, and "take control"
        # sent the owner to a browser it does not have. An older API keeps the
        # hand-off's sentence.
        brief = waiting_brief(task)
        why = (brief["summary"] if brief is not None
               else "only a person promotes it -- open its console and take control")
        text = f"PARKED on HUMAN_REQUIRED for {evidence['age_is']} {age} min: {why}"
    return {"kind": STUCK, "rule": "parked", "reason": text, "evidence": _with_summary(evidence, task)}


def _clock(value: datetime | None) -> str:
    return value.strftime("%H:%M UTC") if value else "an unrecorded time"


def _span(seconds: float) -> str:
    minutes = _minutes(abs(seconds))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} m" if hours else f"{minutes} m"


def _awaiting(task: dict[str, Any], now: datetime) -> dict[str, Any]:
    """(f): a wait for a person -- information, or STUCK when the sweep is overdue."""
    held = awaitinput.window(task)
    block = awaitinput.declaration(task)
    opened = render.parse_time(held.get("opened_at"))
    expires = render.parse_time(held.get("expires_at"))
    evidence = {
        "park_reason": "AWAITING_INPUT",
        "tenant": task.get("tenant_id"),
        "opened_at": _iso(opened),
        "expires_at": _iso(expires),
        "form": block.get("form"),
        "on_expire": block.get("on_expire"),
    }
    grace = EXPIRY_TICKS * EXPIRY_TICK_SECONDS
    if expires is not None and (now - expires).total_seconds() > grace:
        evidence["overdue_minutes"] = _minutes((now - expires).total_seconds())
        return {
            "kind": STUCK, "rule": AWAITING_INPUT,
            "reason": (f"waiting for a member since {_clock(opened)}, but it expired "
                       f"{_span((now - expires).total_seconds())} ago and is still parked: the "
                       f"expiry sweep (every {EXPIRY_TICK_SECONDS}s) has not ended it in "
                       f"{EXPIRY_TICKS} ticks, so it is not running"),
            "evidence": evidence,
        }
    if expires is None:
        until = "no expiry was served"
    elif expires >= now:
        until = f"expires in {_span((expires - now).total_seconds())}"
    else:
        until = f"expired {_span((now - expires).total_seconds())} ago; the next sweep ends it"
    return {
        "kind": AWAITING_INPUT, "rule": AWAITING_INPUT,
        "reason": f"waits for a member of {task.get('tenant_id')} since {_clock(opened)}, {until}",
        "evidence": evidence,
    }


def _refusal_code(task: dict[str, Any]) -> tuple[str | None, str | None]:
    summary = task.get("result_summary") if isinstance(task.get("result_summary"), dict) else {}
    refusal = summary.get("refusal") if isinstance(summary.get("refusal"), dict) else {}
    code = refusal.get("code")
    message = refusal.get("message")
    if not code:
        error = str(task.get("last_error") or "")
        head = error.split(":", 1)[0].strip()
        if head in MERGE_CODES:
            code, message = head, error.split(":", 1)[1].strip() if ":" in error else None
    return (str(code) if code else None), (str(message) if message else None)


def _merge(task: dict[str, Any], step: dict[str, Any],
           pulls: _PullStates | None = None) -> dict[str, Any] | None:
    """(d): a merge step FAILED on head_moved, merge_conflict or checks_failed,
    and its PR is not known to have MERGED or CLOSED since."""
    if task.get("state") != "FAILED":
        return None
    code, message = _refusal_code(task)
    if code not in MERGE_CODES:
        return None
    summary = task.get("result_summary") if isinstance(task.get("result_summary"), dict) else {}
    number = summary.get("pull_request")
    if not isinstance(number, int):
        given = (step.get("input") or {}).get("pull_request") if isinstance(step.get("input"), dict) else None
        number = given if isinstance(given, int) else None
    link = pull_request_link(task.get("repository_url"), number)
    state: str | None = None
    if link is None:
        why = "the step names no pull request it could be read for"
    else:
        state, why = (pulls or _PullStates()).read(str(repo_key(task.get("repository_url"))), number)
    if state in ("MERGED", "CLOSED"):
        # The refusal is history: the PR it refused has since been settled.
        return None
    evidence = {
        "refusal": code,
        "message": message or task.get("last_error"),
        "pull_request": number,
        "pull_request_url": link,
        "pull_request_state": state,
    }
    if state is None:
        evidence["pull_request_state_unread_because"] = (
            f"PR state not read: {why}. Whether the PR is still open is unknown: open the link to see"
        )
    return {
        "kind": STUCK,
        "rule": "merge_refused",
        "reason": f"the merge step FAILED with {code}" + (f" on PR #{number}" if number else ""),
        "evidence": evidence,
        "pull_request": link,
    }


def classify_task(task: dict[str, Any], reads: _Reads, *, now: datetime, window: int,
                  dependencies: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The one flag rules (a)-(c) raise on `task`, or None (not flagged)."""
    state = task.get("state")
    if state in QUIET_STATES:
        return _quiet(task, reads, now, window)
    if state in WAITING_STATES:
        return _waiting(task, reads, now, window)
    if state == "PARKED":
        return _parked(task, reads, now, dependencies)
    return None


def disagreement(workflow: dict[str, Any]) -> dict[str, Any] | None:
    """(e): stored and derived state disagree on whether it is finished."""
    stored = workflow.get("stored_state")
    derived = workflow.get("state")
    drift = workflow.get("drift") if isinstance(workflow.get("drift"), dict) else {}
    evidence = {"stored_state": stored, "derived_state": derived,
                "state_source": workflow.get("state_source")}
    if drift:
        evidence["drift"] = drift
    if workflow.get("state_source") not in (None, workflows.DERIVED):
        return {"kind": STALE, "rule": "state_disagrees",
                "reason": (f"the API served this workflow's stored state ({stored}) without deriving "
                           "it from its steps, so its state is not known"),
                "evidence": evidence}
    if not stored or not derived or derived == workflows.UNKNOWN:
        return None
    if (stored in _TERMINAL) == (derived in _TERMINAL):
        return None
    return {"kind": STALE, "rule": "state_disagrees",
            "reason": (f"stored state {stored} but its steps derive {derived}"
                       + (": the stale RUNNING cache -- every step is done" if derived in _TERMINAL else
                          ": the stored state says finished while a step still runs")),
            "evidence": evidence}


# --------------------------------------------------------------------------
# One tenant
# --------------------------------------------------------------------------


def _page(client: SwarmClient, params: list[tuple[str, str]]) -> dict[str, Any]:
    data = client.request("GET", f"/v1/workflows?{urllib.parse.urlencode(params)}")
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), list):
        raise SwarmError("GET /v1/workflows answered without a `workflows` list")
    return data


def server_scope(key: str | None, sid: str | None, record: session.Record) -> dict[str, str]:
    """What `GET /v1/workflows` is asked to filter by for this session's
    check or attach-all (CHECK-FILTER, owner decision 2026-10-20, #1749).

    THE SESSION whenever it is known: only work tagged with it can be `own`
    (`session.ownership` decides the tag first), so the API dropping the rest
    loses nothing -- and a session's own work is a page or two however many
    workflows the tenant runs. With no session, nothing is asked: there is no
    candidate, and the whole tenant is read to count what was left out.

    THE REPOSITORY only beside the session and only while the project's
    record is EMPTY. Work in the record is this project's whatever it clones
    (SC-TITLES), so a repository filter would drop recorded work in another
    repository; with nothing recorded the repository is the whole rule, and
    the API applies it exactly as `_in_scope` does -- any step's repository."""
    if not sid:
        return {}
    params = {"claude_session": sid}
    if key and not record.ids and not record.submitted:
        params["repository"] = key
    return params


def _workflow_lists(
    client: SwarmClient, scoped: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """`(running, recent, notes)`: the unfinished workflows, every page of
    them (`sc.walk_running_workflows`, filtered by the API to `scoped`), and
    the newest page of ALL of them -- the only place a finished workflow with
    a stale RUNNING cache, or a failed merge step, can be seen."""
    from .sc import walk_running_workflows

    walk = walk_running_workflows(client, **(scoped or {}))
    notes: dict[str, Any] = {"tenant_id": walk["tenant_id"]}
    if "stalled_workflows" in walk:
        notes["reconciler_stalled_workflows"] = walk["stalled_workflows"]
    if "incomplete_because" in walk:
        notes["running_incomplete_because"] = walk["incomplete_because"]
    if "filtered_by_api" in walk:
        notes["filtered_by_api"] = walk["filtered_by_api"]
    recent_page = _page(client, [("limit", str(WORKFLOW_PAGE_SIZE))])
    recent = [w for w in recent_page["workflows"] if isinstance(w, dict)]
    notes["tenant_id"] = notes["tenant_id"] or recent_page.get("tenant_id")
    return walk["workflows"], recent, notes


def _single_tasks(
    client: SwarmClient, sid: str | None = None
) -> tuple[list[dict[str, Any]], str | None, dict[str, int]]:
    """`(tasks, gap, left_out_by_api)`: the caller's unfinished tasks in no
    workflow, as the task route serves them (with `heartbeat_at` and a READY
    task's live `waiting_for`), every page of each state.

    THIS SESSION'S ONLY, ON THE SERVER, whenever the session is known
    (CHECK-FILTER-TASKS, #1749): only work tagged with it can be `own`, so
    the API dropping the rest loses nothing (`server_scope` says the same
    for workflows). `left_out_by_api` is what the route left out of single
    tasks, by kind, summed over every state's walk; empty from a deployment
    that ignored the parameter, whose rows the caller's ownership test then
    counts itself."""
    from .sc import _callers_tasks_in, _live_states

    found: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    by_api: dict[str, int] = {}
    for state in _live_states():
        tasks, _, gap, filtered = _callers_tasks_in(client, state, claude_session=sid)
        if gap:
            gaps.append(gap)
        for kind, count in ((filtered or {}).get("left_out_single") or {}).items():
            by_api[kind] = by_api.get(kind, 0) + count
        for task in tasks:
            task_id = task_id_of(task)
            if (not task_id or task.get("workflow_id") or task.get("parent_task_id")
                    or task.get("state") in _TERMINAL):
                continue
            found.setdefault(str(task_id), task)
    gap = f"{'; '.join(gaps)}; older ones are not checked" if gaps else None
    return list(found.values()), gap, by_api


#: (g)'s window: a finished task's questions are raised again for this long.
#: A week covers a lane that ended over a weekend; older work is the
#: console's to show.
OWNER_QUESTIONS_DAYS = 7

#: The FALLBACK walk only (`_finished_tasks`, for a deployment without
#: `OPEN_QUESTIONS_ROUTE`): one page of finished tasks per request, and at
#: most this many pages per terminal state, newest first.
OWNER_QUESTIONS_PAGE = 50
OWNER_QUESTIONS_MAX_PAGES = 10

#: A finished task's questions, by task id, as `progress.owner_questions`
#: read them (all of them, never only the open ones: an answer given since
#: must still take one off). Kept while the bridge lives -- the check runs
#: every 30 minutes in the same process -- bounded, oldest dropped first.
_ASKED: dict[str, list[dict[str, Any]]] = {}
_ASKED_CAP = 1024
_ASKED_LOCK = threading.Lock()


def _questions_of(client: SwarmClient, task: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """`(questions, None)`, from the cache or one read; `(None, why)` when the
    read failed, which is NOT cached."""
    tid = str(task_id_of(task) or "")
    with _ASKED_LOCK:
        kept = _ASKED.get(tid)
    if kept is not None:
        return kept, None
    try:
        read = progress.owner_questions(client, task)
    except SwarmError as exc:
        return None, f"its questions could not be read: {exc}"
    if read.get("questions_unavailable_because"):
        return None, str(read["questions_unavailable_because"])
    questions = [q for q in read.get("questions") or [] if isinstance(q, dict)]
    with _ASKED_LOCK:
        if tid not in _ASKED and len(_ASKED) >= _ASKED_CAP:
            _ASKED.pop(next(iter(_ASKED)))
        _ASKED[tid] = questions
    return questions, None


def _finished_tasks(client: SwarmClient, sid: str, since: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """`(tasks, failures)`: the caller's tasks of this session in every
    terminal state, newest first, back to the first page that reaches a task
    created before `since` (one finished inside the window was created
    before it only if it ran or waited for longer than the window, and is
    then the console's). `failures` says which reads failed or stopped."""
    from .sc import _tasks_page

    found: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for state in sorted(_TERMINAL):
        token: str | None = None
        seen: set[str] = set()
        for page in range(1, OWNER_QUESTIONS_MAX_PAGES + 1):
            params = [("state", state), ("submitted_by", "me"), ("claude_session", sid),
                      ("limit", str(OWNER_QUESTIONS_PAGE))]
            if token:
                params.append(("page_token", token))
            try:
                data = _tasks_page(client, params)
            except SwarmError as exc:
                failures.append(f"reading page {page} of this session's {state} tasks failed ({exc}); "
                                "their questions for the owner are not listed")
                break
            older = False
            for task in data["tasks"]:
                tid = task_id_of(task) if isinstance(task, dict) else None
                if not tid:
                    continue
                found.setdefault(str(tid), task)
                created = render.parse_time(task.get("created_at"))
                older = older or (created is not None and created < since)
            token = data.get("next_page_token")
            if older or not token or token in seen:
                break
            seen.add(token)
        else:
            failures.append(f"more than {OWNER_QUESTIONS_MAX_PAGES} pages of this session's {state} tasks "
                            f"in {OWNER_QUESTIONS_DAYS} days; older ones' questions are not listed")
    return list(found.values()), failures


#: The one read (g) makes (#2835): this session's finished tasks with an
#: owner question that has no answer, filtered on the server.
OPEN_QUESTIONS_ROUTE = "/v1/tasks/open-questions"

#: (g)'s `unchecked` line when the API does not serve `OPEN_QUESTIONS_ROUTE`
#: and the questions were found by `_finished_tasks`' bounded walk instead.
OPEN_QUESTIONS_FALLBACK = (f"this deployment does not serve GET {OPEN_QUESTIONS_ROUTE}, so the owner's "
                           f"questions were found by paging this session's finished tasks, at most "
                           f"{OWNER_QUESTIONS_MAX_PAGES} pages per state; an older task's may not be listed")


def _asked_tasks(client: SwarmClient, sid: str, since: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """`(tasks, failures)`: this session's tasks that ended since `since` with
    an owner question no answer covers, from ONE server-side query (#2835).

    It used to page every finished task of the session, newest first, and
    stopped after `OWNER_QUESTIONS_MAX_PAGES`: a busy session passed that in
    hours, and an older task's questions were never listed. That walk is
    kept only for a deployment that does not serve the route (a 404 from the
    API itself, not from the edge), and `failures` names it."""
    params = urllib.parse.urlencode([("claude_session", sid), ("since", _iso(since))])
    try:
        data = client.request("GET", f"{OPEN_QUESTIONS_ROUTE}?{params}")
    except SwarmError as exc:
        if exc.status == 404 and not exc.edge:
            tasks, failures = _finished_tasks(client, sid, since)
            return tasks, [OPEN_QUESTIONS_FALLBACK, *failures]
        return [], [f"reading this session's tasks with open questions failed ({exc}); "
                    "their questions for the owner are not listed"]
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return [], [f"GET {OPEN_QUESTIONS_ROUTE} answered without a `tasks` list; "
                    "this session's questions for the owner are not listed"]
    failures = []
    if data.get("complete") is False:
        failures.append(f"GET {OPEN_QUESTIONS_ROUTE} reached its read bound; "
                        "some of this session's questions for the owner may not be listed")
    return [t for t in data["tasks"] if isinstance(t, dict)], failures


def _owner_questions(client: SwarmClient, scope: str | None, sid: str | None, record: session.Record,
                     now: datetime, names: dict[str, str], tenant: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(entries, unchecked)`: (g), this session's finished tasks in this
    project whose questions for the owner have an index with no answer."""
    if not sid:
        # No session, no candidate (`session.UNKNOWN_NOTE`): nothing is read.
        return [], []
    since = now - timedelta(days=OWNER_QUESTIONS_DAYS)
    tasks, failures = _asked_tasks(client, sid, since)
    unchecked: list[dict[str, Any]] = [{"task_id": None, "workflow_id": None, "section": "owner_questions",
                                        "why": why} for why in failures]
    wanted: list[tuple[dict[str, Any], set[int]]] = []
    for task in tasks:
        count = progress.questions_count(task)
        ended = render.parse_time(task.get("completed_at"))
        if count == 0 or ended is None or ended < since:
            continue
        ident = task.get("workflow_id") or task_id_of(task)
        if session.ownership(session.owner_of(task), sid, record=record, ident=ident,
                             in_repository=_in_scope(scope, [task.get("repository_url")])) != "own":
            continue
        answered = {a.get("question_index") for a in progress.owner_answers(task)}
        if all(index in answered for index in range(count)):
            continue  # every counted question has an answer: no read
        wanted.append((task, {i for i in answered if isinstance(i, int)}))

    def read(job: tuple[dict[str, Any], set[int]]) -> tuple[dict[str, Any], set[int], Any, str | None]:
        task, answered = job
        questions, why = _questions_of(client, task)
        return task, answered, questions, why

    entries: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(wanted)))) as pool:
        for task, answered, questions, why in pool.map(read, wanted):
            where = {"task_id": task_id_of(task), "workflow_id": task.get("workflow_id")}
            if questions is None:
                unchecked.append({**where, "section": "owner_questions", "why": why})
                continue
            open_ = [{"index": i, **q} for i, q in enumerate(questions) if i not in answered]
            if not open_:
                continue
            metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            name = metadata.get("title") or metadata.get("unit")
            entry: dict[str, Any] = {"workspace": workflows.workspace_of(task, names, fallback=tenant), **where}
            if task.get("workflow_id"):
                entry["workflow"] = name or task.get("workflow_id")
                entry["step_id"] = task.get("step_id")
            else:
                entry["title"] = name or task_id_of(task)
            link = console_link(task)
            if link:
                entry["console"] = link
            entry.update({
                "state": task.get("state"),
                "finished_at": _iso(render.parse_time(task.get("completed_at"))),
                "open": len(open_),
                "questions": open_,
                "answer_with": (f"AskUserQuestion, then swarm_task_answer {{task_id: {task_id_of(task)!r}, "
                                "question_index: <its index>, choice: <the option the person picked>}"),
            })
            entries.append(entry)
    entries.sort(key=lambda e: str(e.get("finished_at") or ""), reverse=True)
    return entries, unchecked


def _check_tenant(client: SwarmClient, scope: str | None, window: int, now: datetime,
                  names: dict[str, str], sid: str | None = None,
                  pulls: _PullStates | None = None) -> dict[str, Any]:
    reads = _Reads(client, MAX_TASK_READS)
    pulls = pulls or _PullStates()
    record = session.project_record()
    running, recent, notes = _workflow_lists(client, server_scope(scope, sid, record))
    # THIS SESSION'S OWN (SC-SESSION-SCOPE): a list row that already names
    # another session, or names none on a deployment that serves the tag, is
    # never read step by step; the rest are decided off their step tasks.
    left_out = {"workflows": session.counts(), "single_tasks": session.counts()}
    out_of_scope = {"workflows": 0, "single_tasks": 0}
    # What the API left out for us (CHECK-FILTER): counted as if this check
    # had left it out itself, so the report reads the same either way.
    by_api = (notes.get("filtered_by_api") or {}).get("left_out") or {}
    for kind in ("other_session", "untagged"):
        left_out["workflows"][kind] += int(by_api.get(kind) or 0)
    out_of_scope["workflows"] += int(by_api.get("other_repository") or 0)

    def foreign(row: dict[str, Any]) -> str | None:
        if session.METADATA_KEY not in row:
            return None
        kind = session.ownership(session.owner_of(row), sid)
        return None if kind == "own" else kind

    kept_running = []
    for row in running:
        kind = foreign(row)
        if kind:
            left_out["workflows"][kind] += 1
        else:
            kept_running.append(row)
    running = kept_running
    recent = [w for w in recent if not foreign(w)]
    tenant = notes.get("tenant_id")
    running_ids = {str(w.get("workflow_id")) for w in running}

    # What needs its step tasks read: every running workflow (for the
    # repository and rules a-d), and a recent one that disagrees with itself
    # or failed with a merge step.
    wanted: dict[str, dict[str, Any]] = {str(w["workflow_id"]): w for w in running if w.get("workflow_id")}
    for workflow in recent:
        wid = str(workflow.get("workflow_id") or "")
        if not wid or wid in wanted:
            continue
        has_merge = any(isinstance(s, dict) and s.get("runner_profile") == MERGE_PROFILE
                        for s in workflow.get("steps") or [])
        if disagreement(workflow) or (has_merge and workflow.get("state") not in ("SUCCEEDED", "CANCELLED")):
            wanted[wid] = workflow
    read_ids = list(wanted)[:MAX_WORKFLOW_DETAILS]
    unchecked: list[dict[str, Any]] = [
        {"workflow_id": wid, "why": f"only {MAX_WORKFLOW_DETAILS} workflows are read step by step per check"}
        for wid in list(wanted)[MAX_WORKFLOW_DETAILS:]
    ]

    def detail(wid: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            return wid, workflows.fetch(client, wid), None
        except SwarmError as exc:
            return wid, None, str(exc)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(read_ids)))) as pool:
        envelopes = list(pool.map(detail, read_ids))

    flagged: list[dict[str, Any]] = []
    in_scope_running: list[dict[str, Any]] = []
    jobs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []

    for wid, envelope, error in envelopes:
        if envelope is None:
            unchecked.append({"workflow_id": wid, "why": f"could not be read: {error}"})
            continue
        listed = wanted[wid]
        doc = envelope["workflow"]
        tasks = {str(t.get("id")): t for t in envelope.get("tasks") or [] if isinstance(t, dict) and t.get("id")}
        kind = session.ownership(
            session.owner_of(listed) or session.owner_of(doc) or session.owner_of_tasks(tasks.values()), sid,
            record=record, ident=wid, name=session.stored_name(envelope, wid),
            in_repository=_in_scope(scope, [t.get("repository_url") for t in tasks.values()]))
        if kind == "not_this_project":
            if wid in running_ids:
                out_of_scope["workflows"] += 1
            continue
        if kind != "own":
            if wid in running_ids:
                left_out["workflows"][kind] += 1
            continue
        workspace = workflows.workspace_of(doc, names, fallback=envelope.get("tenant_id") or tenant)
        where = {"workspace": workspace, "workflow_id": wid}
        # (e), from the LIST row: the list read is the one that saw the cache
        # before deriving repaired it (`drift`).
        flag = disagreement(listed) or disagreement(doc)
        if flag:
            item = {**flag, **where, "step_id": None, "task_id": None, "state": listed.get("state"),
                    "next": f"/sc status {wid}"}
            link = console_link(doc) or console_link(listed)
            if link:
                item["console"] = link
            flagged.append(item)
        steps = [s for s in doc.get("steps") or [] if isinstance(s, dict)]
        state_of_step = {s.get("step_id"): (tasks.get(str(s.get("task_id"))) or {}).get("state") for s in steps}
        for step in steps:
            task = tasks.get(str(step.get("task_id")))
            if task is None:
                continue
            step_where = {**where, "step_id": step.get("step_id")}
            if step.get("runner_profile") == MERGE_PROFILE and doc.get("state") != "SUCCEEDED":
                merged = _merge(task, step, pulls)
                if merged:
                    extra = merged.pop("pull_request", None)
                    item = _item(merged["kind"], merged["rule"], merged["reason"], merged["evidence"], task, step_where)
                    if extra:
                        item["pull_request"] = extra
                        item["next"] = f"open {extra}, then /sc status {wid}"
                    flagged.append(item)
            if wid in running_ids:
                deps = {d: state_of_step.get(d) for d in step.get("depends_on") or []}
                jobs.append((task, step_where, deps))
        if wid in running_ids:
            entry = {"workflow_id": wid, "workspace": workspace, "state": doc.get("state"),
                     "title": workflows.stored_names(envelope).get("title"),
                     "repository": next((repo_key(t.get("repository_url")) for t in tasks.values()
                                         if repo_key(t.get("repository_url"))), None)}
            link = console_link(doc)
            if link:
                entry["console"] = link
            in_scope_running.append(entry)

    singles, singles_gap, singles_by_api = _single_tasks(client, sid)
    # What the API left out of this session's single tasks (CHECK-FILTER-
    # TASKS): counted as if this check had left it out itself.
    for kind in ("other_session", "untagged"):
        left_out["single_tasks"][kind] += int(singles_by_api.get(kind) or 0)
    in_scope_singles: list[dict[str, Any]] = []
    for task in singles:
        kind = session.ownership(session.owner_of(task), sid, record=record, ident=task_id_of(task),
                                 in_repository=_in_scope(scope, [task.get("repository_url")]))
        if kind == "not_this_project":
            out_of_scope["single_tasks"] += 1
            continue
        if kind != "own":
            left_out["single_tasks"][kind] += 1
            continue
        workspace = workflows.workspace_of(task, names, fallback=tenant)
        entry = {"task_id": task_id_of(task), "workspace": workspace, "state": task.get("state"),
                 "label": (task.get("metadata") or {}).get("unit") if isinstance(task.get("metadata"), dict) else None,
                 "repository": repo_key(task.get("repository_url"))}
        link = console_link(task)
        if link:
            entry["console"] = link
        in_scope_singles.append(entry)
        jobs.append((task, {"workspace": workspace}, None))

    def run(job: tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]) -> Any:
        task, where, deps = job
        try:
            flag = classify_task(task, reads, now=now, window=window, dependencies=deps)
        except _Budget:
            return ("unchecked", {"task_id": task_id_of(task), "workflow_id": where.get("workflow_id"),
                                  "why": f"the check's read budget ({MAX_TASK_READS} task reads) was spent"})
        except SwarmError as exc:
            return ("unchecked", {"task_id": task_id_of(task), "workflow_id": where.get("workflow_id"),
                                  "why": f"could not be read: {exc}"})
        if flag is None:
            return None
        item = _item(flag["kind"], flag["rule"], flag["reason"], flag["evidence"], task, where)
        if flag["kind"] == AWAITING_INPUT:
            item["next"] = (f"swarm_workflow_input {{workflow_id: {where.get('workflow_id')!r}, "
                            f"step_id: {where.get('step_id')!r}}}, or its console link")
            return (AWAITING_INPUT, item)
        return ("flagged", item)

    asked, asked_unchecked = _owner_questions(client, scope, sid, record, now, names, tenant)
    unchecked.extend(asked_unchecked)

    awaiting: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as pool:
        for outcome in pool.map(run, jobs):
            if outcome is None:
                continue
            bucket = {"flagged": flagged, AWAITING_INPUT: awaiting}.get(outcome[0], unchecked)
            bucket.append(outcome[1])

    out: dict[str, Any] = {
        "tenant_id": tenant,
        "flagged": flagged,
        "awaiting_input": awaiting,
        "owner_questions": asked,
        "running": {"workflows": in_scope_running, "single_tasks": in_scope_singles},
        "out_of_scope": out_of_scope,
        "other_sessions": left_out,
        "unchecked": unchecked,
    }
    for key in ("reconciler_stalled_workflows", "running_incomplete_because"):
        if key in notes:
            out[key] = notes[key]
    if singles_gap:
        out["single_tasks_incomplete_because"] = singles_gap
    return out


def _readers(client: SwarmClient, tenants: Any) -> list[SwarmClient]:
    """The session's tenant, or with `tenants: "all"` every tenant the caller
    may choose (`GET /v1/tenants/mine`), each through a COPY of the client --
    never by setting `client.tenant`, which other calls share (see
    `workflows.fetch`)."""
    if tenants in (None, "", "current"):
        return [client]
    if tenants not in ALL:
        raise SwarmError(f"swarm_check: `tenants` is \"current\" (the default) or \"all\", not {tenants!r}. "
                         "Nothing was read")
    listing = client.request("GET", "/v1/tenants/mine")
    ids = [str(e.get("tenant_id")) for e in listing if isinstance(e, dict) and e.get("tenant_id")] \
        if isinstance(listing, list) else []
    readers = [client]
    for tenant in ids:
        if tenant == getattr(client, "tenant", None):
            continue
        reader = copy.copy(client)
        reader.tenant = tenant
        readers.append(reader)
    return readers


def run(
    client: SwarmClient,
    *,
    repository: Any = None,
    stale_minutes: Any = None,
    tenants: Any = None,
    now: datetime | None = None,
    names: Callable[[SwarmClient], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The whole check: flagged items first, then what runs in scope."""
    window = DEFAULT_STALE_MINUTES if stale_minutes is None else stale_minutes
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise SwarmError(f"swarm_check: `stale_minutes` must be a whole number of minutes, 1 or more, "
                         f"not {stale_minutes!r}. Nothing was read")
    now = now or datetime.now(timezone.utc)
    scope = resolve_scope(repository)
    sid = session.current()
    shown = (names or workflows.workspace_names)(client)
    seen_tenants: list[Any] = []
    per_tenant: list[dict[str, Any]] = []
    pulls = _PullStates()  # one cache for the whole check, across tenants
    for reader in _readers(client, tenants):
        result = _check_tenant(reader, scope["key"], window, now, shown, sid, pulls)
        if result["tenant_id"] in seen_tenants:
            continue
        seen_tenants.append(result["tenant_id"])
        per_tenant.append(result)

    flagged = [item for r in per_tenant for item in r["flagged"]]
    flagged.sort(key=lambda item: _RULE_ORDER.get(item["rule"], 9))
    out: dict[str, Any] = {
        "checked_at": _iso(now),
        "read_only": True,
        "repository": scope["key"],
        "repository_source": scope["source"],
        "stale_minutes": window,
        "tenants": seen_tenants,
        "count": {"flagged": len(flagged),
                  "stuck": sum(1 for i in flagged if i["kind"] == STUCK),
                  "stale": sum(1 for i in flagged if i["kind"] == STALE)},
        "flagged": flagged,
        # (f): steps waiting for a person, as information -- never counted
        # as stuck or stale, and never a reason for the watch to alarm.
        "awaiting_input": [i for r in per_tenant for i in r["awaiting_input"]],
        # (g): questions a lane's agent asked the owner and nobody answered,
        # whole -- information like (f), never counted as stuck or stale.
        # The session puts each to the person (skill: `check`).
        "owner_questions": [i for r in per_tenant for i in r["owner_questions"]],
        "running": {
            "workflows": [w for r in per_tenant for w in r["running"]["workflows"]],
            "single_tasks": [t for r in per_tenant for t in r["running"]["single_tasks"]],
        },
        "out_of_scope": {
            "workflows": sum(r["out_of_scope"]["workflows"] for r in per_tenant),
            "single_tasks": sum(r["out_of_scope"]["single_tasks"] for r in per_tenant),
        },
        # What was left out for being another session's or untagged
        # (SC-SESSION-SCOPE): counted, never checked, never attached.
        "session": {
            **session.describe(sid),
            "left_out": {
                group: {kind: sum(r["other_sessions"][group][kind] for r in per_tenant)
                        for kind in ("other_session", "untagged")}
                for group in ("workflows", "single_tasks")
            },
        },
        "unchecked": [u for r in per_tenant for u in r["unchecked"]],
        "attach_with": (
            "swarm_workflow_launch {attach: \"all\", repository: <this answer's repository, or "
            "\"all\" when it is null>, new_only: true} -- rows only for what has none in this session"
        ),
    }
    if scope.get("note"):
        out["repository_note"] = scope["note"]
    if not out["running"]["workflows"] and not out["running"]["single_tasks"]:
        # Said, not acted on (SC-AUTO-WATCH, owner 2026-10-14): an armed watch
        # with nothing to watch costs one read per interval, so the check
        # leaves it armed for the next workflow rather than cancelling it.
        where = f"in {scope['key']}" if scope["key"] else "in any repository"
        out["nothing_running"] = (
            f"nothing this session started is running in SwarmCloud {where}; the watch stays armed "
            "for the next workflow (`/sc watch off` cancels it)"
        )
    for r in per_tenant:
        for key in ("reconciler_stalled_workflows", "running_incomplete_because",
                    "single_tasks_incomplete_because"):
            if key in r:
                out.setdefault(key, {})[str(r["tenant_id"])] = r[key]
    return out
