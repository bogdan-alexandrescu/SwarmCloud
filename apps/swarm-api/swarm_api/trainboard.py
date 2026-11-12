"""Board on time: start the repository merge train once its gathering deadline passes.

THE LIVE FINDING (12 November 2026, the owner's pick "Fix: board on time").
A lane's pull request was green and labelled `ready` at 02:56Z; the merge
train's gather ("6 ready or 20 minutes", scripts/merge-train.sh
`gather_waits`) said it boards at the latest at 03:15:35Z. But the train
runs only when .github/workflows/merge-train.yml is triggered -- a */5
schedule, the `workflow_run` of application, security and auto-merge, or
`workflow_dispatch` -- and from 03:10Z to 07:57Z GitHub ran almost none of
the scheduled ticks and no other workflow finished. Nothing boarded it until
07:59Z. A lone ready pull request must never depend on GitHub's schedule.

WHAT THIS DOES, on swarm-api's per-tenant merge wake tick (every minute,
`mergewake.wake_tenant`). For the tenant's merge steps parked on their
repository's GitHub merge train -- CI_PENDING with `merge_train_waiting`,
in a step whose signed target is not a workspace train (TRAIN-WS), the
repository the task's own `repository_url` (invariant 9) -- it asks, per
repository:

  1. Is the train on (`vars.MERGE_TRAIN`, exactly `on`, the tick's
     `_train_on`)? Off, a dispatch would start a DRY RUN train
     (merge-train.yml's "on demand"), so nothing is dispatched.
  2. Has the gathering deadline passed? The window is
     `vars.MERGE_TRAIN_MAX_WAIT_MINUTES` when the repository sets it, as
     merge-train.yml hands it to the script, else the default both read
     from ONE file, merge_train_gather.json beside this module
     (`gather_defaults`). "Ready since" is what the script reads: the last
     time `ready` was added, from the pull request's issue events; one whose
     `ready` was taken off after is not ready. The deadline has passed when
     the oldest ready pull request of the parks has waited the window.
  3. Is a merge-train run queued or in progress? Then it boards on that
     run, and nothing is dispatched.
  4. Then ONE `workflow_dispatch` of merge-train.yml on the repository's
     default branch (the only ref its `repo-secrets` environment admits),
     with no inputs: a tick exactly like a scheduled one.

AT MOST ONE DISPATCH PER REPOSITORY PER GATHERING WINDOW. Before it asks
GitHub, the tick claims the repository's document in `merge_train_boards`
in a Firestore transaction: refused when another dispatch was claimed less
than a window ago (never less than DISPATCH_GAP_MIN_SECONDS), so two ticks,
or two replicas, never both dispatch. The gathering rule itself is
unchanged: the dispatched run reads every pull request afresh and boards or
gathers by `gather_waits` as any tick does.

A REFUSED DISPATCH writes one line naming the reason and backs off:

  * refused for the credential or the route -- no `actions: write` (403),
    the workflow missing or not dispatchable (404, 422) -- the claim
    stands, recorded with the refusal, and the repository is not asked
    again for a window;
  * rate-limited -- the tick's own hold (`forge_limits.sweep_key`), as
    every other read of the tick, until the reset GitHub named; the claim
    is released, so the first tick after the reset may dispatch;
  * unreachable (5xx, a timeout) -- the claim is released and the next
    tick asks again.

It never fails the tick for any other park: every read here fails soft to a
log line, and only a rate limit stops the tenant's tick, as everywhere else.

QUOTA. Every request acts as the tenant's `-git` credential, the token the
tick already holds for a re-run of failed jobs, which needs the same
`actions: write` a dispatch does (docs/multi-tenancy.md, "What the forge
credential must be allowed"). A credential under the ledger's threshold,
or held, is not asked: the repository waits for the next tick.

INVARIANTS. Nothing here admits, reserves or runs an agent: a park stays
PARKED, holding no lease, no pool count and no execution (1, 3); the only
writes are the board document and one GitHub dispatch. No caller selects
the repository, the workflow, the ref or the token (10).
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from google.cloud import firestore

from swarm_common.admission import _snapshot

from . import forge_limits
from .errors import ApiError
from .forgewrite import (
    ACTIONS_WRITE, ISSUES_READ, ForgeRateLimited, ForgeWriteError, GitHubWriter,
    is_transient,
)
from .validation import IssueRef

log = logging.getLogger(__name__)

#: THE GATHER's defaults, stated once for scripts/merge-train.sh and this
#: module (the owner's pick on 11 November 2026, "6 ready or 20 minutes").
GATHER_SOURCE = Path(__file__).with_name("merge_train_gather.json")
#: The repository variable merge-train.yml hands the script as
#: MERGE_TRAIN_MAX_WAIT_MINUTES; unset or empty, the default above.
WAIT_VARIABLE = "MERGE_TRAIN_MAX_WAIT_MINUTES"
#: The workflow dispatched, and the label it boards by (`merge-train.sh select`).
TRAIN_WORKFLOW = "merge-train.yml"
READY_LABEL = "ready"
#: One document per (tenant, repository): the last dispatch claimed, and its
#: refusal. Written here only.
BOARDS_COLLECTION = "merge_train_boards"
#: The shortest gap between two dispatches of one repository, whatever the
#: window: with the gather off (window 0) a waiting pull request would
#: otherwise be dispatched for every minute no run shows yet. Five minutes is
#: the schedule's own period, the pace the train was built for.
DISPATCH_GAP_MIN_SECONDS = 300
#: Issue-event pages read per pull request (100 each), oldest first as
#: GitHub lists them; `merge-train.sh ready_since` reads them all.
MAX_EVENT_PAGES = 10
#: The newest merge-train runs read for one queued or in progress. One
#: concurrency group keeps at most one running and one pending.
RUNS_READ = 20
_MINUTES = re.compile(r"^(0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class Gather:
    min_prs: int
    max_wait_minutes: int


@functools.lru_cache(maxsize=1)
def gather_defaults() -> Gather:
    """merge_train_gather.json, the gather's one statement. A file that does
    not hold two whole numbers raises: never a second copy to fall back on."""
    data = json.loads(GATHER_SOURCE.read_text(encoding="utf-8"))
    values = [data.get("min_prs"), data.get("max_wait_minutes")]
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
        raise ValueError(f"{GATHER_SOURCE.name} does not hold the gather's two numbers")
    return Gather(min_prs=values[0], max_wait_minutes=values[1])


def board_id_for(tenant_id: str, repository: str) -> str:
    key = f"{tenant_id}\n{repository.lower()}"
    return "mtb_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


@dataclass
class BoardReport:
    """What one tick did per repository: dispatched, refused, or still gathering."""

    dispatched: int = 0
    refused: int = 0
    gathering: int = 0

    def to_api(self) -> dict[str, int]:
        return {"dispatched": self.dispatched, "refused": self.refused,
                "gathering": self.gathering}

    def __bool__(self) -> bool:
        return bool(self.dispatched or self.refused or self.gathering)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def window_minutes(writer: GitHubWriter, ref: IssueRef, token: str) -> int:
    """The gathering window of `ref`'s repository, in minutes, as the script
    gets it: the repository variable when set to a whole number, else the
    shared default. A variable the credential cannot read, or that is not a
    number (the script would stop on it), is the default."""
    default = gather_defaults().max_wait_minutes
    try:
        value = writer.repository_variable(ref, WAIT_VARIABLE, token)
    except ForgeRateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable is the default
        code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
        log.info("merge train board %s: %s not read (%s); %d minutes", ref.repository,
                 WAIT_VARIABLE, code, default)
        return default
    if value is None or not value.strip():
        return default
    if not _MINUTES.fullmatch(value.strip()):
        log.info("merge train board %s: %s is not a number of minutes; %d minutes",
                 ref.repository, WAIT_VARIABLE, default)
        return default
    return int(value.strip())


def ready_since(writer: GitHubWriter, ref: IssueRef, number: int, token: str) -> datetime | None:
    """When `ready` was last added to pull request `number`, from its issue
    events, as `merge-train.sh ready_since` reads it; None when it was never
    added or was taken off after (the train does not board it)."""
    what = f"the events of {ref.repository}#{int(number)}"
    added: datetime | None = None
    removed: datetime | None = None
    for page in range(1, MAX_EVENT_PAGES + 1):
        data = writer._call(
            "GET", writer._url(ref, f"issues/{int(number)}/events?per_page=100&page={page}"),
            token, what, needs=ISSUES_READ)
        if not isinstance(data, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        for event in data:
            if not isinstance(event, Mapping):
                continue
            label = event.get("label")
            if not isinstance(label, Mapping) or label.get("name") != READY_LABEL:
                continue
            at = _iso(event.get("created_at"))
            if at is None:
                continue
            if event.get("event") == "labeled" and (added is None or at > added):
                added = at
            elif event.get("event") == "unlabeled" and (removed is None or at > removed):
                removed = at
        if len(data) < 100:
            break
    if added is None or (removed is not None and removed >= added):
        return None
    return added


def train_in_flight(writer: GitHubWriter, ref: IssueRef, token: str) -> bool:
    """Whether a merge-train run is queued or in progress (any not `completed`)."""
    runs = writer.workflow_runs(ref, TRAIN_WORKFLOW, token, limit=RUNS_READ)
    return any(str(run.get("status") or "") != "completed" for run in runs)


def dispatch(writer: GitHubWriter, ref: IssueRef, branch: str, token: str) -> None:
    """One `workflow_dispatch` of merge-train.yml on `branch`, no inputs.

    Through the writer's own request path: the pinned host, the token in the
    Authorization header only, its status mapping (403 names `actions:
    write`). GitHub answers 204."""
    writer._call(
        "POST", writer._url(ref, f"actions/workflows/{quote(TRAIN_WORKFLOW, safe='')}/dispatches"),
        token, f"a run of {TRAIN_WORKFLOW} in {ref.repository}", needs=ACTIONS_WRITE,
        payload={"ref": branch}, ok=(200, 201, 204))


def _gap(window: int) -> timedelta:
    return timedelta(seconds=max(window * 60, DISPATCH_GAP_MIN_SECONDS))


def _recent(data: Mapping[str, Any], now: datetime, window: int) -> bool:
    last = _aware(data.get("dispatched_at"))
    return last is not None and last <= now and now - last < _gap(window)


def _claim(db: Any, tenant_id: str, ref: IssueRef, *, now: datetime, window: int,
           since: datetime, number: int) -> bool:
    """Claim this window's one dispatch of `ref`'s repository; True only if
    claimed. In one transaction on the repository's board document, so two
    ticks or two replicas never both dispatch."""
    doc_ref = db.collection(BOARDS_COLLECTION).document(board_id_for(tenant_id, ref.repository))
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(doc_ref))
        data = (snap.to_dict() or {}) if snap.exists else {}
        if data and data.get("tenant_id") != tenant_id:
            return False
        if _recent(data, now, window):
            return False
        txn.set(doc_ref, {
            "tenant_id": tenant_id,
            "repository": ref.repository,
            "dispatched_at": now,
            "ready_since": since,
            "pull_request": int(number),
            "window_minutes": int(window),
            "refused": None,
            "refused_at": None,
        })
        return True

    return bool(_apply(transaction))


def _settle(db: Any, tenant_id: str, ref: IssueRef, now: datetime, *,
            refused: str | None, release: bool) -> None:
    """After a dispatch that did not go out: record its refusal (the claim
    stands, one window's back-off), or release the claim (`release`), only
    while it is still the one this tick made."""
    doc_ref = db.collection(BOARDS_COLLECTION).document(board_id_for(tenant_id, ref.repository))
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> None:
        snap = _snapshot(txn.get(doc_ref))
        data = dict((snap.to_dict() or {}) if snap.exists else {})
        if data.get("tenant_id") != tenant_id or _aware(data.get("dispatched_at")) != now:
            return
        if release:
            data["dispatched_at"] = None
        data.update(refused=refused, refused_at=now)
        txn.set(doc_ref, data)

    _apply(transaction)


def _board_document(db: Any, tenant_id: str, ref: IssueRef) -> Mapping[str, Any]:
    snap = db.collection(BOARDS_COLLECTION).document(
        board_id_for(tenant_id, ref.repository)).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    return data if data.get("tenant_id") == tenant_id else {}


def board_on_time(ctx: Any, tenant: Any, tenant_id: str,
                  parks: Sequence[tuple[IssueRef, Any]], *,
                  train_on: Callable[[IssueRef], bool],
                  token_for: Callable[[], str],
                  sweep: str,
                  halted: datetime | None) -> tuple[BoardReport, datetime | None]:
    """One tick's dispatches over `parks`, each (the pull request, when it
    parked); see the module docstring. `sweep` is the tick's hold key
    (`forge_limits.sweep_key`). Returns what it did and the tenant's hold,
    if one began."""
    report = BoardReport()
    if not parks or halted is not None:
        return report, halted
    db = ctx.store.db
    now = ctx.now()
    writer: GitHubWriter = ctx.forge_writer
    credential = forge_limits.tenant_credential(tenant_id)
    percent = forge_limits.defer_percent(ctx)
    repositories: dict[str, list[tuple[IssueRef, Any]]] = {}
    for ref, parked_at in parks:
        repositories.setdefault(ref.repository.lower(), []).append((ref, parked_at))
    for entries in repositories.values():
        ref = entries[0][0]
        # Oldest park first: its pull request is the likeliest to be past
        # the deadline, and the first one past it is enough.
        entries.sort(key=lambda e: _aware(e[1]) or now)
        try:
            board = _board_document(db, tenant_id, ref)
            last_window = board.get("window_minutes")
            if _recent(board, now, last_window if isinstance(last_window, int)
                       else gather_defaults().max_wait_minutes):
                # Dispatched (or refused) inside its window: nothing is read.
                continue
        except Exception as exc:  # noqa: BLE001 - the claim reads it again
            log.info("merge train board %s tenant=%s: board not read (%s)", ref.repository,
                     tenant_id, type(exc).__name__)
        if forge_limits.QUOTAS.defer(credential, percent, now=now) is not None:
            continue
        try:
            with forge_limits.acting_as(credential):
                token = token_for()
                if not train_on(ref):
                    continue
                window = window_minutes(writer, ref, token)
                oldest: tuple[datetime, int] | None = None
                for park_ref, _ in entries:
                    since = ready_since(writer, park_ref, park_ref.number, token)
                    if since is None:
                        continue
                    if oldest is None or since < oldest[0]:
                        oldest = (since, park_ref.number)
                    if now - since >= timedelta(minutes=window):
                        break
                if oldest is None:
                    continue
                since, number = oldest
                if now - since < timedelta(minutes=window):
                    report.gathering += 1
                    continue
                if train_in_flight(writer, ref, token):
                    continue
                if not _claim(db, tenant_id, ref, now=now, window=window, since=since,
                              number=number):
                    continue
                try:
                    dispatch(writer, ref, writer.default_branch(ref, token), token)
                except Exception as exc:
                    code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
                    transient = is_transient(exc) or not isinstance(exc, ApiError)
                    report.refused += 1
                    log.warning("merge train board %s tenant=%s: %s not dispatched for #%d, "
                                "ready since %s (%s: %s); %s", ref.repository, tenant_id,
                                TRAIN_WORKFLOW, number, since.isoformat(), code,
                                str(exc)[:300] if isinstance(exc, ApiError) else "",
                                "asked again next tick" if transient
                                else f"not asked again for {max(window, 5)} minutes")
                    _settle(db, tenant_id, ref, now, refused=code, release=transient)
                    if isinstance(exc, ForgeRateLimited):
                        raise
                    continue
                report.dispatched += 1
                log.info("merge train board %s tenant=%s: %s dispatched: #%d ready since %s, "
                         "past its %d-minute gather with no train run", ref.repository,
                         tenant_id, TRAIN_WORKFLOW, number, since.isoformat(), window)
        except ForgeRateLimited as exc:
            halted = exc.reset or now + forge_limits.DEFAULT_HOLD
            forge_limits.QUOTAS.hold(sweep, halted)
            log.warning("github quota: merge train board tenant=%s rate-limited; held until %s",
                        tenant_id, halted.isoformat())
            break
        except Exception as exc:  # noqa: BLE001 - never what fails the tick
            code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
            log.warning("merge train board %s tenant=%s: not read (%s)", ref.repository,
                        tenant_id, code)
    return report, halted
