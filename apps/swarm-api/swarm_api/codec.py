"""Firestore document <-> frozen dataclass conversion.

The dataclasses in `swarm_common.models` are the contract. This module is the
only place that knows how they are spelled as Firestore documents, so a field
rename in a query never silently reads `None`.

Firestore hands datetimes back as `DatetimeWithNanoseconds`, a `datetime`
subclass, so no conversion is needed on the read path; the ISO-string branch
exists for documents written by tooling (seed scripts, the emulator REST API)
rather than by this service.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

from swarm_common.models import (
    Attempt,
    EndCause,
    FORGE_ACCESS,
    FORGE_CREDENTIAL,
    Lease,
    QuotaState,
    SlotPool,
    Task,
    TaskEvent,
    Tenant,
    Workflow,
    WorkflowStep,
    check_additional_repositories,
    check_sized_live_split,
)
from swarm_common.profiles import RUNNER_PROFILES, step_model_class
from swarm_common.states import BlockedReason, EventType, ParkReason, TaskState
from swarm_common.models import ProviderState, utcnow

from .prgate import wait_for_api
from . import sessiontag
from .task_input import TaskMasking, masking_for
from .waiting_detail import build_waiting, mask_waiting, waiting_row
from .validation import (
    DEFAULT_CARRIER,
    DEFAULT_STRATEGY,
    DISPATCH_METADATA_KEY,
    FORGE_SOURCE_METADATA_KEY,
    OWNER_ANSWERS_METADATA_KEY,
    START_PROBLEM_METADATA_KEY,
)

#: A masker over no document: the rules, and no literals. What an attempt is
#: masked with when its caller had no task to hand (`attempt_to_api`). It only
#: masks with `remember=False`, so sharing it across requests keeps nothing.
_RULES_ONLY = TaskMasking(None, None)


# --------------------------------------------------------------------------
# Console links
# --------------------------------------------------------------------------
#
# THE API IS THE ONE SOURCE OF A CONSOLE LINK (owner decision 2026-10-01). The
# plugin, the sc CLI, the MCP answers and the web UI print the `links.console`
# these functions put on a task or a workflow, and none of them rebuilds the
# host: a host spelled in five places is five places to drift, and the first
# one wrong is a link that opens somebody else's console or nothing at all.
#
# The origin is `ApiSettings.console_url` (SWARM_CONSOLE_URL, which terraform
# renders as `https://<frontend_hostname>`). EMPTY MEANS NO LINK: the field is
# null, never a guess at a run.app URL -- swarm-ui behind IAP is not reachable
# at its run.app address, so a guessed link is a link that does not open.
#
# The PATHS are the console's own History-API routes, apps/swarm-ui/src/paths.ts:
# one agent is `/agents/<tab>/<id>`, and a pasted link uses `live`, the tab
# `addressToPath` defaults to; one workflow is `/workflows/<id>`, its id
# URI-encoded exactly as `addressToPath` encodes it.
# tests/unit/control_plane/test_console_links.py reads paths.ts, so a route
# renamed in the UI fails that test instead of silently breaking every link.

#: The agent list a pasted link opens in (paths.ts `addressToPath`'s default).
CONSOLE_AGENT_TAB = "live"

#: What JavaScript's encodeURIComponent leaves unescaped besides quote's own
#: `_.-~`, so a workflow id is spelled exactly as paths.ts spells it.
_URI_COMPONENT_SAFE = "!*'()"


def console_origin(origin: str | None) -> str | None:
    """The configured console origin without a trailing slash, or None when unset."""
    text = (origin or "").strip().rstrip("/")
    return text or None


def agent_console_url(origin: str | None, task_id: str | None) -> str | None:
    """`<origin>/agents/live/<task_id>`, or None with no origin or no task."""
    base = console_origin(origin)
    if base is None or not task_id:
        return None
    return f"{base}/agents/{CONSOLE_AGENT_TAB}/{task_id}"


def workflow_console_url(origin: str | None, workflow_id: str | None) -> str | None:
    """`<origin>/workflows/<workflow_id>`, or None with no origin or no workflow."""
    base = console_origin(origin)
    if base is None or not workflow_id:
        return None
    return f"{base}/workflows/{quote(workflow_id, safe=_URI_COMPONENT_SAFE)}"


def run_console_url(origin: str | None, run_id: str | None) -> str | None:
    """`<origin>/runs/<run_id>` (an issue run, #454), or None with no origin or no run.

    paths.ts spells one run as `/runs/${encodeURIComponent(run)}`.
    """
    base = console_origin(origin)
    if base is None or not run_id:
        return None
    return f"{base}/runs/{quote(run_id, safe=_URI_COMPONENT_SAFE)}"


def as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"cannot interpret {value!r} as a datetime")


def _required_datetime(value: Any) -> datetime:
    parsed = as_datetime(value)
    if parsed is None:
        raise ValueError("document is missing a required timestamp")
    return parsed


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------

def _end_cause(value: Any) -> EndCause | None:
    """A stored end cause, or None -- for an old document, and for a value this
    image does not know. A newer writer's cause must not make a task unreadable
    here; the outcome ledger reads the raw string and counts it as `other`."""
    if value is None:
        return None
    try:
        return EndCause(value)
    except ValueError:
        return None


def served_model_class(runner_profile: str, stored: str | None) -> dict[str, Any]:
    """`model_class` as served: the stored class, or the profile's default
    when none is stored (`model_class_defaulted: true`), by the frozen
    `profiles.step_model_class` the scheduler and the worker resolve with.

    Never raises: a document written past the API with a class its profile
    does not accept, or naming a profile this image does not know, is served
    as stored, not defaulted, so a reader sees what the scheduler will refuse.
    """
    profile = RUNNER_PROFILES.get(runner_profile)
    if profile is None:
        return {"model_class": stored, "model_class_defaulted": False}
    try:
        resolved = step_model_class(profile, stored)
    except ValueError:
        return {"model_class": stored, "model_class_defaulted": False}
    return {"model_class": resolved, "model_class_defaulted": stored is None and resolved is not None}


def task_to_firestore(task: Task) -> dict[str, Any]:
    return task.to_firestore()


def _timeout_asked(value: Any) -> int | None:
    """A stored `timeout_seconds_asked`: an int above 0, anything else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def task_from_dict(data: dict[str, Any]) -> Task:
    park = data.get("park_reason")
    return Task(
        id=data["id"],
        tenant_id=data["tenant_id"],
        created_at=_required_datetime(data.get("created_at")),
        updated_at=_required_datetime(data.get("updated_at")),
        state=TaskState(data["state"]),
        runner_profile=data["runner_profile"],
        resource_class=data["resource_class"],
        input=dict(data.get("input") or {}),
        submitted_by=data.get("submitted_by", ""),
        provider=data.get("provider"),
        model=data.get("model"),
        priority=int(data.get("priority", 0)),
        metadata=dict(data.get("metadata") or {}),
        repository_url=data.get("repository_url"),
        repository_ref=data.get("repository_ref"),
        timeout_seconds=int(data.get("timeout_seconds", 3600)),
        max_attempts=int(data.get("max_attempts", 3)),
        attempt_count=int(data.get("attempt_count", 0)),
        # Contract request 123: absent on every older document, which is 0.
        platform_refusals=int(data.get("platform_refusals") or 0),
        # Contract request 131: absent on every older document and on a task
        # not lowered. A record only, never a limit (invariant 10).
        timeout_seconds_asked=_timeout_asked(data.get("timeout_seconds_asked")),
        next_eligible_at=as_datetime(data.get("next_eligible_at")),
        park_reason=ParkReason(park) if park else None,
        blocked_by=list(data.get("blocked_by") or []),
        current_lease_id=data.get("current_lease_id"),
        current_generation=int(data.get("current_generation", 0)),
        workflow_id=data.get("workflow_id"),
        step_id=data.get("step_id"),
        depends_on=list(data.get("depends_on") or []),
        cancel_requested=bool(data.get("cancel_requested", False)),
        started_at=as_datetime(data.get("started_at")),
        completed_at=as_datetime(data.get("completed_at")),
        last_error=data.get("last_error"),
        result_summary=data.get("result_summary"),
        latest_checkpoint=data.get("latest_checkpoint"),
        end_cause=_end_cause(data.get("end_cause")),
        # Contract request 34. Read back so a task this service decodes and
        # writes again keeps the signature swarm-api put on it at submission.
        spec_signature=data.get("spec_signature") or None,
        spec_key_version=data.get("spec_key_version") or None,
        spec_format=_spec_format(data.get("spec_format")),
        # Contract request 14. Absent on every task that is not a child.
        parent_task_id=data.get("parent_task_id") or None,
        parent_attempt_id=data.get("parent_attempt_id") or None,
        # Contract request 54. Read back so a task this service decodes and
        # writes again keeps the signed fields it was submitted with.
        forge_credential=_forge_credential(data.get("forge_credential")),
        forge_access=_forge_access(data.get("forge_access")),
        # Contract request 70 (LB-C). Read back so a task this service decodes
        # and writes again never clears a live hand-off; anything but a dict
        # is no wait.
        human_wait=_human_wait(data.get("human_wait")),
        # Contract request 95 (MR-CONTRACT). Read back so a task this service
        # decodes and writes again keeps the signed list it was submitted with.
        additional_repositories=_additional_repositories(data.get("additional_repositories")),
        # Contract request 104 (#1435). Read back so a task this service
        # decodes and writes again keeps the signed class it was submitted
        # with. None: the profile's default class.
        model_class=data.get("model_class") or None,
        # Contract request 120 (#2459). Read back so a task this service
        # decodes and writes again keeps the signed budget it was submitted
        # with. Anything but a map is no budget.
        budget=_budget_map(data.get("budget")),
    )


def _budget_map(value: Any) -> dict[str, Any] | None:
    """`Task.budget` / `WorkflowStep.budget` as stored, or None when it is not a map."""
    return dict(value) if isinstance(value, dict) else None


#: The units of a budget (`swarm_api.budget.UNITS`, not imported: that module
#: reads Firestore and the role store, and this one is imported by both).
_BUDGET_UNITS = ("usd", "tokens")


def _budget_limits(value: Any) -> dict[str, Any] | None:
    """`{usd?, tokens?}` from a stored budget, only its positive numbers;
    None when it holds neither."""
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {}
    for unit in _BUDGET_UNITS:
        amount = value.get(unit)
        if (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                and math.isfinite(amount) and amount > 0):
            out[unit] = amount
    return out or None


def served_budget(stored: Any, metadata: Any) -> dict[str, Any] | None:
    """A task's `budget` as the API serves it: the typed `Task.budget`, or
    for a task stored before contract request 120 the limits it pinned in
    `metadata.dispatch.budget`, read for one release. None: no budget."""
    if stored is not None:
        return _budget_limits(stored)
    dispatch = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
    return _budget_limits(dispatch.get("budget") if isinstance(dispatch, Mapping) else None)


def _human_wait(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


#: What of `Task.human_wait` a caller is served (docs/design/live-browser.md
#: §4.1, contract request 70): the five fields the design names, for the
#: console's hand-off banner (lane LB-L12). The document also carries the
#: worker's `request_id`, `attempt_id` and `generation`, which only the worker
#: routes compare; they are left out so the public shape is the design's, not
#: whatever the writer happens to store.
HUMAN_WAIT_PUBLIC_KEYS = ("reason", "expect", "requested_at", "deadline", "controller")


def human_wait_to_api(value: Any) -> dict[str, Any] | None:
    """The served hand-off, or None while nobody is being waited for."""
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in HUMAN_WAIT_PUBLIC_KEYS}


def _forge_credential(value: Any) -> str | None:
    """A stored forge credential, or None for an old document and for a value
    of the wrong shape. A tenant's agent can write its task documents, and one
    malformed field must not make a task unreadable to every listing here.
    Nothing here acts on it: the worker verifies the signed document itself,
    and a None written back over a signed value fails that check."""
    if isinstance(value, str) and FORGE_CREDENTIAL.fullmatch(value):
        return value
    return None


def _forge_access(value: Any) -> str | None:
    """A stored forge access mode, or None -- as `_forge_credential`."""
    return value if isinstance(value, str) and value in FORGE_ACCESS else None


def _additional_repositories(value: Any) -> list[dict[str, Any]] | None:
    """A stored additional-repository list, or None -- as `_forge_credential`:
    a malformed one must not make the task unreadable here, and the worker
    verifies the signed document itself."""
    try:
        check_additional_repositories(value)
    except ValueError:
        return None
    return value


#: Where the agent finds an additional repository, relative to its own
#: checkout `work/repo`: the worker links `work/repos/<name>` (#1434).
ADDITIONAL_REPOSITORIES_PATH = "../repos"


def additional_repositories_to_api(value: Any) -> list[dict[str, Any]] | None:
    """What a caller is served of `Task.additional_repositories`
    (docs/design/multi-repository-steps.md §4.1): each entry's name,
    repository, registration, ref as asked, pinned commit, where the agent
    finds it, and whose credential clones it, by kind. Never the URL
    (derivable, and the primary's is masked for a reason) and never the slot
    suffix (the primary's `forge_credential` is not public either). None for
    a task that reads none, and for a stored list of the wrong shape."""
    entries = _additional_repositories(value)
    if not entries:
        return None
    return [{
        "name": entry["name"],
        "repository": entry["repository"],
        "repo_id": entry["repo_id"],
        "ref": entry["ref"],
        "commit": entry["commit"],
        "path": f"{ADDITIONAL_REPOSITORIES_PATH}/{entry['name']}",
        "forge_credential_source": entry["forge_credential_source"],
    } for entry in entries]


#: What `task_to_api` says a task's `forge_credential` is, by its prefix.
#: `git` is only ever a service submission's (`SubmissionService._resolve_forge`);
#: so, in a personal tenant with no `-git` slot, is its owner's `git-u-` slot,
#: which `metadata.forge_credential_source` = `owner` tells apart (INDEX-CRED).
#: `app_installation` is a service run the broker mints a GitHub App
#: installation token for (SERVICE-APP-TOKEN): whatever slot it names, the
#: installation's token is what runs (DAYONE-CODEC-LABEL).
_FORGE_SOURCES = (
    ("git-u-", "the submitter's GitHub credential"),
    ("git-r-", "the repository's token"),
)


def forge_credential_source(value: str | None, source: Any = None) -> str | None:
    """The words for a task's forge credential, or None when it names none
    (no repository, not GitHub, or submitted before #780). `source` is the
    task's `metadata.forge_credential_source`: `owner` on a user slot is the
    owner's connection run on the tenant's behalf, not the submitter's own;
    `app_installation` is a run whose token the broker mints from the GitHub
    App installation."""
    if value is None:
        return None
    if source == "app_installation":
        return "GitHub App installation token, minted for the run"
    if value == "git":
        return "tenant token, service submission"
    if source == "owner" and value.startswith("git-u-"):
        return "owner's GitHub connection, service submission"
    for prefix, words in _FORGE_SOURCES:
        if value.startswith(prefix):
            return words
    return None


def _spec_format(value: Any) -> int | None:
    # `bool` is an `int`; a stored True is not format 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def dispatch_of(task: Task) -> dict[str, Any]:
    """The EFFECTIVE dispatch options for a task, always complete.

    `task.metadata["dispatch"]` is where they are stored, and it is absent on
    every task submitted before this feature existed. Absent means today's
    behaviour -- harvest the patch, push nothing -- so it reads back as
    `collect`/`checkpoints` rather than as null. A caller reading this key never
    has to know that the encoding lives in metadata, or that a task may predate
    it.
    """
    raw = task.metadata.get(DISPATCH_METADATA_KEY)
    block = raw if isinstance(raw, dict) else {}
    out = {
        "strategy": block.get("strategy") or DEFAULT_STRATEGY,
        "carrier": block.get("carrier") or DEFAULT_CARRIER,
        # None on everything but an `integrate` workflow's steps.
        "role": block.get("role"),
        "integrates": list(block.get("integrates") or ()),
    }
    # A step's base and verdict gate (#264), only on a step that has them, so
    # every other task reads back exactly as it did before they existed.
    if block.get("builds_on"):
        out["builds_on"] = block["builds_on"]
    if isinstance(block.get("verdict_gate"), dict):
        out["verdict_gate"] = dict(block["verdict_gate"])
    # The step's empty-diff permission (2026-10-05), only when it was given.
    if block.get("allow_empty_diff") is True:
        out["allow_empty_diff"] = True
    # The attempt's resolved budget (#2459), only on a budgeted task: the
    # limits the worker enforces, `usd` and/or `tokens`, and where each came
    # from. The limits are the typed `Task.budget` (contract request 120) and
    # the sources `budget_from`, both platform-written and signed; a task
    # stored before the field carries both in the block's `budget`, read for
    # one release. Served in one shape either way.
    limits = served_budget(task.budget, task.metadata)
    if limits is not None:
        legacy = block.get("budget")
        sources = block.get("budget_from") if task.budget is not None else (
            legacy.get("from") if isinstance(legacy, dict) else None)
        out["budget"] = {**limits, "from": dict(sources) if isinstance(sources, dict) else {}}
    return out


def claude_session_of(task: Task, masking: TaskMasking) -> str | None:
    """The Claude Code session the task was tagged with (SC-SESSION-SCOPE,
    `sessiontag`), or None: `metadata.claude_session`, lifted out as
    `dispatch` is so a list in the summary view -- which drops `metadata` --
    still says whose the task is. Caller-written, so masked by the task's
    masker like every other caller string."""
    raw = sessiontag.of(task.metadata)
    if raw is None:
        return None
    masked, count = masking.text(raw)
    return masked if count == 0 else None


def workflow_claude_session(step_tasks: Mapping[str, Task]) -> dict[str, Any]:
    """`{claude_session}` for a workflow, off its step tasks (the frozen
    `Workflow` has no metadata; every step task carries the workflow's): the
    key only when a step task was read, null when none is tagged, so a reader
    tells "untagged" from "not read"."""
    if not step_tasks:
        return {}
    for task in step_tasks.values():
        found = claude_session_of(task, masking_for(task, warm=False))
        if found:
            return {sessiontag.KEY: found}
    return {sessiontag.KEY: None}


def start_problem_to_api(task: Task, masking: TaskMasking) -> dict[str, Any] | None:
    """Why the task's pod is not starting, as the reconciler last saw it, or None (F1-POD).

    Stored as `metadata.start_problem` by the reconciler
    (`reconciler.startproblem`), because the frozen `Task` has no field for
    it; lifted here so a caller reads one key without knowing the encoding,
    as `dispatch` is. `message` is kubelet's text about a tenant's pod, so
    every string is masked by the task's masker, as `last_error` is. A record
    that is not a map serves None: a document can hold anything.
    """
    raw = (task.metadata or {}).get(START_PROBLEM_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    masked, _ = masking.leaves(
        {
            "container": raw.get("container"),
            "reason": raw.get("reason"),
            "restarts": raw.get("restarts") if isinstance(raw.get("restarts"), int) else 0,
            "message": raw.get("message"),
            "pod_started": raw.get("pod_started") is True,
            "attempt_id": raw.get("attempt_id"),
            "seen_at": raw.get("seen_at"),
        }
    )
    return masked


def owner_answers_by_index(metadata: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    """The owner's answers stored on a task (`metadata.owner_answers`), by question index.

    Written only by `agent_questions.answer_question` (TASK-ANSWERS). A
    document can hold anything, so an entry that is not a map or has no
    non-negative integer `question_index` is skipped, never guessed at.
    """
    raw = (metadata or {}).get(OWNER_ANSWERS_METADATA_KEY)
    out: dict[int, dict[str, Any]] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("question_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        out[index] = entry
    return out


def owner_answer_to_api(entry: dict[str, Any], masking: TaskMasking) -> dict[str, Any]:
    """One stored answer as it is served, every string masked by the task's masker.

    The free text was redacted before it was stored; it is masked again here
    as every collected text is (`task_input.COLLECTED_METADATA_KEYS`). `via`
    and `session` say where it was answered (ANSWER-SYNC), null on an answer
    stored before they were.
    """

    def text(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    def one(e: dict[str, Any]) -> dict[str, Any]:
        at = e.get("at")
        return {
            "choice": text(e.get("choice")),
            "text": text(e.get("text")),
            "by": text(e.get("by")),
            "at": at.isoformat() if isinstance(at, datetime) else text(at),
            "via": text(e.get("via")),
            "session": text(e.get("session")),
        }

    history = entry.get("history") if isinstance(entry.get("history"), list) else []
    masked, _ = masking.leaves(
        {
            "question_index": entry.get("question_index"),
            "question": text(entry.get("question")),
            **one(entry),
            "history": [one(h) for h in history if isinstance(h, dict)],
        }
    )
    return masked


def owner_answers_to_api(task: Task, masking: TaskMasking) -> list[dict[str, Any]]:
    """The owner's answers to the agent's questions, in question order; `[]` for none."""
    return [
        owner_answer_to_api(entry, masking)
        for _, entry in sorted(owner_answers_by_index(task.metadata).items())
    ]


def workflow_dispatch(tasks: Any) -> dict[str, Any]:
    """A workflow's dispatch options, read back from the tasks that carry them.

    The frozen `Workflow` dataclass has no metadata field, so there is nowhere
    on the workflow document to store them; every task of a workflow is written
    with the same strategy and carrier, so any one of them answers. The per-step
    ROLE is not reported here because it differs by step -- the integrator is
    named instead, which is the part a caller wants from the workflow level.
    """
    strategy, carrier = DEFAULT_STRATEGY, DEFAULT_CARRIER
    integrator_task_id: str | None = None
    for task in tasks:
        options = dispatch_of(task)
        strategy, carrier = options["strategy"], options["carrier"]
        if options["role"] == "integrator":
            integrator_task_id = task.id
            break
    return {
        "strategy": strategy,
        "carrier": carrier,
        "integrator_task_id": integrator_task_id,
    }


def task_to_api(
    task: Task,
    waiting_for: dict[str, Any] | None = None,
    *,
    account: dict[str, Any] | None = None,
    console_url: str | None = None,
    heartbeat: dict[str, Any] | None = None,
    summary: bool = False,
    waiting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Public JSON shape. Contains no credential material and no backend spec.

    `summary` is `GET /v1/tasks?view=summary` (#168): the row WITHOUT `input`
    and `result_summary` and their masking counts -- the keys ABSENT, not
    null, so a reader cannot mistake a dropped field for an empty one -- and
    without the masking work for them, which is skipped rather than computed
    and discarded. Its `metadata` is cut to `SUMMARY_METADATA_KEYS`, the
    title alone (`summary_metadata`). Everything else, `last_error` and
    `repository_url` included, is masked exactly as in the full row.

    `heartbeat` is `swarm_api.heartbeats`' reading of the task's current
    lease (#179): `heartbeat_at`, `heartbeat_grace_seconds` and `heartbeat`
    (what the reading is). All three null on routes that read no lease and for
    a task that holds none.

    `console_url` is the deployment's console origin (`ApiSettings.console_url`);
    `links.console` is this agent's page on it, the same in every state, and
    null when the deployment has no console (see `agent_console_url`).

    THE INPUT AND THE METADATA ARE SERVED MASKED (owner decision, 2026-09-26,
    on #184: "the API never serves a credential-shaped string back, even to
    the submitter"). Both come from `task_input.TaskMasking` -- one masker over
    the input and the caller's metadata, the one `/input` uses -- with
    `input_redaction_count` and `metadata_redaction_count` beside them. The
    platform's own metadata keys are `task_input.PLATFORM_METADATA_KEYS`,
    which IS `validation.RESERVED_METADATA_KEYS` (submission refuses every one
    of them from every caller; read the tuple for the list, `startup_refunds`
    and the child and merge-wait keys included). They are not walked by the
    masker and are served as stored -- EXCEPT the two in
    `task_input.NAME_METADATA_KEYS`, `input_from` and `expected_outputs`, whose
    values are filenames copied from the caller's workflow spec and are masked
    name by name by `TaskMasking.name` (#227). Every route that serves a task
    goes through this function, so no route serves the raw input.

    AND WHAT THE TASK COLLECTED ABOUT ITSELF (the PR #229 review). The same
    masker masks `last_error` (the agent's stderr tail), every string in
    `result_summary` (the agent's own summary among them) and the userinfo of
    `repository_url`, each with a count beside it -- see `TaskMasking.text`
    and `.leaves` for why string by string. Inside `result_summary` the
    lookup keys' values (an artifact's `name`/`uri`, a staged input's
    `filename`/`path`, ids: `task_input.LOOKUP_KEYS`) are masked by
    `TaskMasking.name`, the function that masks `input_from` and
    `expected_outputs`, so a clean name is served as written and a masked one
    still equals its declared copy (#296). The masker comes from
    `task_input.masking_for`, which keeps it per task: a list page of large
    inputs is masked once, not on every refresh.
    """
    masking = masking_for(task, warm=not summary)
    masked_input: Any = None
    masked_metadata: Any = None
    result_summary: Any = None
    input_count = metadata_count = result_summary_count = 0
    if not summary:
        masked_input, input_count = masking.input_value()
        masked_metadata, metadata_count = masking.metadata_value()
        result_summary, result_summary_count = masking.leaves(task.result_summary)
    else:
        masked_metadata, metadata_count = summary_metadata(task, masking)
    last_error, last_error_count = masking.text(task.last_error)
    repository_url, repository_url_count = masking.repository_url(task.repository_url)
    beat = heartbeat or {}
    row = {
        "id": task.id,
        "tenant_id": task.tenant_id,
        "state": task.state.value,
        "runner_profile": task.runner_profile,
        "resource_class": task.resource_class,
        "provider": task.provider,
        "model": task.model,
        # Contract request 104 (#1435, model-per-step §4.4): the class this
        # task runs, RESOLVED -- the stored one, or its profile's default when
        # none is stored -- and whether it was defaulted. Null on a profile
        # that runs no Claude model.
        **served_model_class(task.runner_profile, task.model_class),
        "priority": task.priority,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "submitted_by": task.submitted_by,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        # Contract request 123: consecutive dispatches the platform refused
        # (a PLATFORM_REFUSED park counts one; a dispatch that succeeds
        # resets it). Past PLATFORM_REFUSED_MAX_PARKS the task is trouble.
        "platform_refusals": task.platform_refusals,
        "timeout_seconds": task.timeout_seconds,
        # Contract request 131: what the spec asked when swarm-api stored a
        # lower `timeout_seconds`, else null. A record only: the limit is
        # `timeout_seconds` (invariant 10).
        "timeout_seconds_asked": task.timeout_seconds_asked,
        # Contract request 120 (#2459): the attempt's resolved budget, or
        # null for none. A task stored before the field serves what its
        # dispatch block pinned (`served_budget`).
        "budget": served_budget(task.budget, task.metadata),
        "next_eligible_at": task.next_eligible_at,
        "park_reason": task.park_reason.value if task.park_reason else None,
        "blocked_by": task.blocked_by,
        # THE FENCING PAIR, which this serialiser never emitted.
        #
        # CONTRACT invariant 5 -- a stale worker exits without running the
        # agent -- turns on `current_generation`, and `task_from_dict` above has
        # always read both fields back. Neither ever reached a caller, so the
        # platform's core safety mechanism was invisible through the API and no
        # screen could show it. The live case: task_b5dc2568713a40158851 sat
        # DISPATCHED for twenty minutes at generation 2 while its
        # `current_lease_id` named an UNRELEASED generation-1 lease whose
        # attempt never started, so the slot stayed held for work that could
        # never run. The one number that says so was not served.
        #
        # THAT TASK'S DOCUMENT STILL PROVES THE POINT after the reconciler
        # reclaimed it (`release_reason: reconciler:missing_execution`) and the
        # retry succeeded: it is SUCCEEDED with `current_generation` 3 and
        # `attempt_count` 2. Generation above attempt count is the permanent
        # record that a stale worker was fenced, and it was unreadable through
        # every API a person or a screen could call.
        #
        # Neither is withheld material. The docstring above promises no
        # credential material and no backend spec: a generation is a small
        # integer and the lease id is already public -- `lease_to_api` serves
        # `lease_id` itself, and that lease's own generation, to the same
        # callers. Nothing in this function's history ever removed them; they
        # were absent from the first commit that wrote it.
        #
        # ALWAYS EMITTED, INCLUDING AS 0 AND None. Zero is an answer -- the
        # task has never been admitted -- and it is what makes
        # `current_generation > attempt_count` ("a stale worker was fenced")
        # readable. A `or None` here would turn that answer back into "not
        # reported", the conflation this codec has spent days removing.
        "current_generation": task.current_generation,
        "current_lease_id": task.current_lease_id,
        "workflow_id": task.workflow_id,
        "step_id": task.step_id,
        "depends_on": task.depends_on,
        # Contract request 14 (docs/design/child-tasks.md §6.3): the parent a
        # child was submitted by, set by swarm-api, null for everything else.
        "parent_task_id": task.parent_task_id,
        "parent_attempt_id": task.parent_attempt_id,
        # #780 OB7: which GitHub credential the task runs with, by NAME (a
        # slot suffix, never a value), and whether it may push. The owner's
        # D4 for automation (2026-10-07): "the task says so".
        "forge_credential": task.forge_credential,
        "forge_access": task.forge_access,
        "forge_credential_source": forge_credential_source(
            task.forge_credential, (task.metadata or {}).get(FORGE_SOURCE_METADATA_KEY)),
        "cancel_requested": task.cancel_requested,
        "metadata": masked_metadata,
        "metadata_redaction_count": metadata_count,
        # Also inside `metadata`, which is where it is STORED. It is lifted out
        # here so a caller reads the effective values -- including on a task
        # that predates the feature and has no block -- without knowing the
        # encoding.
        "dispatch": dispatch_of(task),
        # SC-SESSION-SCOPE: the Claude Code session that submitted it, lifted
        # out of `metadata` (and kept in the summary view, which drops it).
        "claude_session": claude_session_of(task, masking),
        # F1-POD: why the pod is not starting, in the summary view too -- the
        # list is where a stuck "dispatched" row is first seen. Null when the
        # reconciler recorded nothing. Also inside `metadata`, masked there too.
        "start_problem": start_problem_to_api(task, masking),
        # TASK-ANSWERS (part of #1206): the owner's answers to the agent's
        # questions, lifted out of `metadata.owner_answers` as `start_problem`
        # is, so whoever launched the work reads them from the task it already
        # reads. `[]` when none is answered. Also inside `metadata`, masked.
        "owner_answers": owner_answers_to_api(task, masking),
        "repository_url": repository_url,
        "repository_url_redaction_count": repository_url_count,
        "repository_ref": task.repository_ref,
        # #1434 (MR-API): the repositories it reads beside its own, each at
        # the commit swarm-api pinned at submission; no URL and no slot.
        "additional_repositories": additional_repositories_to_api(
            task.additional_repositories),
        "input": masked_input,
        "input_redaction_count": input_count,
        "last_error": last_error,
        "last_error_redaction_count": last_error_count,
        "result_summary": result_summary,
        "result_summary_redaction_count": result_summary_count,
        "latest_checkpoint": task.latest_checkpoint,
        # BROWSER-USE-FLAG (part of #2322): whether a browser runtime was
        # used, lifted out of `result_summary` where the worker records it,
        # so the list row (which drops the summary) carries it too. Numbers,
        # booleans and the worker's fixed reason words only; null on a task
        # that is not a browser runtime, has not ended, or predates it.
        **browser_use_to_api(task.result_summary),
        # Contract request 70 (LB-C): the live-browser hand-off a RUNNING task
        # is holding for, so the console can raise its banner (§4.1, §6).
        # In the summary view too: the list and the Overview read that view,
        # and they are where "needs you" is drawn. Null when nobody is waited
        # for, including on a PARKED(HUMAN_REQUIRED) task, whose wait the
        # park cleared.
        "human_wait": human_wait_to_api(task.human_wait),
        # Contract request 23 (#217), ACCEPTED 2026-09-25. Written by every
        # terminal writer beside `completed_at` and read here first by the
        # outcome ledger (`swarm_api.outcomes`); this serialiser never sent it,
        # so nothing outside this process could read the same classification --
        # `swarm_mcp.progress.outcome` (the bridge's per-task outcome, read by
        # `swarm_follow`, `swarm_wait` and `swarm_result`) had to fall back to
        # sorting free-text `last_error` itself. SUCCEEDED, and a task that
        # ended before this field existed, both carry null.
        "end_cause": task.end_cause.value if isinstance(task.end_cause, EndCause) else task.end_cause,
        # #362: which pool refuses a READY task, read live on the GET by
        # `swarm_api.waiting` (never by this serialiser, which reads nothing).
        # Null for every other state, and on routes that do not compute it.
        # `blocked_by` above is the scheduler's record from its last pass.
        "waiting_for": waiting_for,
        # #1782 (WAIT-API): why a PARKED, READY or QUEUED task waits, what
        # resumes it and when it is next checked, built by
        # `swarm_api.waiting_detail` from what the platform recorded (never
        # by this serialiser, which reads nothing) and masked here by the
        # task's own masker, string by string. The GET serves it in full,
        # a listing row `{reason, summary}`; null on a route that built none.
        "waiting": mask_waiting(waiting, masking.text),
        # #379: the subscription account the LATEST attempt runs on, derived
        # by `swarm_api.task_accounts` from this task's own account events
        # (never by this serialiser, which reads nothing). Null on routes that
        # do not read it; `status` says why there is no account otherwise.
        "account": account,
        # #179: the worker's last beat, from the task's current LEASE (the
        # worker never writes it to the task), and the grace the reconciler
        # acts on. Read by `swarm_api.heartbeats`, never by this serialiser.
        # `heartbeat_at` null with `heartbeat: "read"` is a lease that has
        # never beaten; with `"not read"` it is a failed read, not silence.
        "heartbeat_at": beat.get("heartbeat_at"),
        "heartbeat_grace_seconds": beat.get("heartbeat_grace_seconds"),
        "heartbeat": beat.get("heartbeat"),
        # The console page for this agent, in every state from QUEUED to
        # terminal -- the one link every surface prints (owner decision
        # 2026-10-01). Null when no console is configured.
        "links": {"console": agent_console_url(console_url, task.id)},
    }
    if summary:
        for key in SUMMARY_DROPPED_KEYS:
            del row[key]
    return row


#: The counts `browser_use_to_api` serves from `result_summary.browser_use`,
#: each a non-negative integer or null.
BROWSER_USE_COUNTS: tuple[str, ...] = ("tool_calls", "mcp_calls", "command_calls", "images")
#: The worker's words for an unknown verdict (`lifecycle.browser_use_record`).
BROWSER_USE_UNKNOWN_BECAUSE = frozenset({"not_reported", "stream_unread", "stream_cut"})


def browser_use_to_api(result_summary: Any) -> dict[str, Any]:
    """`browser_used` (true, false or null) and `browser_use` (its counts and
    seconds, or null), read from the worker's `result_summary` keys
    (BROWSER-USE-FLAG). Only whitelisted keys of the expected types are
    served: the summary is the worker's, and nothing in it is passed through
    unread."""
    summary = result_summary if isinstance(result_summary, dict) else {}
    used = summary.get("browser_used")
    detail = summary.get("browser_use")
    served: dict[str, Any] | None = None
    if isinstance(detail, dict):
        served = {}
        for key in BROWSER_USE_COUNTS:
            value = detail.get(key)
            served[key] = (
                value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
            )
        seconds = detail.get("seconds")
        served["seconds"] = (
            float(seconds)
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0
            else None
        )
        served["complete"] = detail.get("complete") is True
        because = detail.get("unknown_because")
        served["unknown_because"] = because if because in BROWSER_USE_UNKNOWN_BECAUSE else None
    return {"browser_used": used if isinstance(used, bool) else None, "browser_use": served}


#: What `view=summary` leaves out of a task row (#168): the two fields a
#: list never shows, and the masking count beside each. `metadata` was the
#: third until SUMMARY-TITLE; it is cut to `SUMMARY_METADATA_KEYS` instead.
SUMMARY_DROPPED_KEYS: tuple[str, ...] = (
    "input",
    "input_redaction_count",
    "result_summary",
    "result_summary_redaction_count",
)

#: The `metadata` keys `view=summary` serves (SUMMARY-TITLE, part of #2391):
#: the title alone. BGD's decision of 2026-10-26 (VQ-009) is that the console
#: names a lone agent by its title in the list, with no read per row. Every
#: other key stays out: the summary exists because the list moved megabytes of
#: what it never draws (#168).
SUMMARY_METADATA_KEYS: tuple[str, ...] = ("title",)


def summary_metadata(task: Task, masking: TaskMasking) -> tuple[dict[str, Any], int]:
    """The summary row's `metadata`: its string `title`, masked, and the masks it took.

    `{}` when the task has no title or a title that is not a string. Masked
    by the task's own masker as one string (`TaskMasking.text`), so a title
    reads here as it reads in the full row's `metadata`, and the full
    metadata's masking (`metadata_value`) is still never run for a summary.
    """
    stored = task.metadata or {}
    out: dict[str, Any] = {}
    count = 0
    for key in SUMMARY_METADATA_KEYS:
        value = stored.get(key)
        if isinstance(value, str):
            out[key], found = masking.text(value)
            count += found
    return out, count


# --------------------------------------------------------------------------
# Events / attempts / leases
# --------------------------------------------------------------------------

def event_to_firestore(event: TaskEvent) -> dict[str, Any]:
    d = asdict(event)
    d["type"] = event.type.value
    return d


def stored_event_type(data: dict[str, Any]) -> EventType:
    """The type a stored event RECORDS, which for one legacy shape is not its field.

    Until 2026-09-24 `Store.request_cancel` wrote a flag-only cancel -- a task
    that still held capacity, so nothing was cancelled -- as `type: cancelled`
    with `detail.phase: "cancel_requested"`. Contract request 17 gave that its
    own type, CANCEL_REQUESTED, and the API writes it now. The events already
    stored keep the old shape: nothing rewrites them, because a migration is a
    write to every task's history for a fact this one line can read correctly.

    So the old shape is read HERE, the one decoder every event route goes
    through, and every API reader -- the console, `swarm_follow`, `swarm tail`
    -- gets one vocabulary whenever the event was written. Only that exact
    shape: a `cancelled` with `phase: "cancelled"` (the immediate path) or with
    no phase (the scheduler's cascade, the worker, the reconciler) was a real
    cancel and stays one. The detail is served as stored, so the served legacy
    event is the same shape as a new one, `phase` included.
    """
    kind = EventType(data["type"])
    detail = data.get("detail")
    if (
        kind is EventType.CANCELLED
        and isinstance(detail, dict)
        and detail.get("phase") == EventType.CANCEL_REQUESTED.value
    ):
        return EventType.CANCEL_REQUESTED
    # The same for the budget's 80% warning (#2459): until contract request
    # 121 gave it a type the worker wrote it as `running` with `detail.cause:
    # "budget_warning"`. Read as BUDGET_WARNING for one release, so a reader
    # of the type sees one vocabulary whichever worker wrote it.
    if (
        kind is EventType.RUNNING
        and isinstance(detail, dict)
        and detail.get("cause") == EventType.BUDGET_WARNING.value
    ):
        return EventType.BUDGET_WARNING
    return kind


def event_from_dict(data: dict[str, Any]) -> TaskEvent:
    return TaskEvent(
        event_id=data["event_id"],
        task_id=data["task_id"],
        tenant_id=data["tenant_id"],
        type=stored_event_type(data),
        at=_required_datetime(data.get("at")),
        attempt_id=data.get("attempt_id"),
        lease_id=data.get("lease_id"),
        generation=data.get("generation"),
        detail=dict(data.get("detail") or {}),
    )


def _checkpoint_digests(value: Any) -> dict[str, str]:
    """`Attempt.checkpoint_sha256` from a stored value: its `str -> str` entries only."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _sized_live_split(value: Any) -> dict[str, dict[str, float]] | None:
    """`Attempt.sized_live_split` from a stored value, or None when malformed.

    `Attempt` refuses a malformed split at construction (contract request
    111), so handing it the stored value raw would make one bad document
    unreadable, attempt list and all. A split that fails the check is read as
    "not recorded" instead.
    """
    try:
        check_sized_live_split(value)
    except (ValueError, TypeError, KeyError):
        return None
    return value


def attempt_from_dict(data: dict[str, Any]) -> Attempt:
    return Attempt(
        attempt_id=data["attempt_id"],
        task_id=data["task_id"],
        tenant_id=data["tenant_id"],
        generation=int(data.get("generation", 0)),
        lease_id=data.get("lease_id", ""),
        backend=data.get("backend", ""),
        created_at=_required_datetime(data.get("created_at")),
        execution_name=data.get("execution_name"),
        started_at=as_datetime(data.get("started_at")),
        completed_at=as_datetime(data.get("completed_at")),
        exit_code=data.get("exit_code"),
        error=data.get("error"),
        peak_rss_bytes=data.get("peak_rss_bytes"),
        peak_disk_bytes=data.get("peak_disk_bytes"),
        oom_near_miss=bool(data.get("oom_near_miss", False)),
        checkpoints=list(data.get("checkpoints") or []),
        # CONTRACT REQUEST 51 (accepted by the owner 2026-10-09): the archive
        # digest of each checkpoint, which the worker's `record_checkpoint`
        # writes and a retry binds its restore to (#347). This decoder used to
        # drop it, as it once dropped the spend fields below. Only `str -> str`
        # entries are kept; a missing or malformed map is the empty default,
        # "no digest recorded". `attempt_to_api` does not serve it.
        checkpoint_sha256=_checkpoint_digests(data.get("checkpoint_sha256")),
        # THE FIVE SPEND FIELDS, which this decoder used to drop.
        #
        # `control.record_spend` merge-sets all five into the attempt document
        # and they arrive intact -- but they were never read back here, so the
        # dataclass defaults applied and `attempt_to_api` faithfully served
        # None for every attempt that ever ran. Every cost and token figure in
        # the product was unreachable, and the serialiser's own comment blamed
        # a worker fix that had already shipped.
        #
        # `peak_rss_bytes` above is the control: same document, same decoder,
        # and it round-tripped throughout. Exactly these five were missing.
        #
        # NOTE the `is None` checks rather than `or`: 0 tokens and $0.00 are
        # measurements, and `data.get(k) or None` would turn a real zero back
        # into "not measured" -- the same conflation this codebase has spent
        # three days removing, reintroduced in the line that fixes it.
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        cache_read_input_tokens=data.get("cache_read_input_tokens"),
        cache_creation_input_tokens=data.get("cache_creation_input_tokens"),
        cost_usd=data.get("cost_usd"),
        # CONTRACT REQUEST #15 (accepted on #184, 2026-09-25): the attempt's
        # CPU, written by `control.record_cpu_usage`. `is None`-safe for the
        # reason the spend fields are: an idle agent's 0.0 cores is a
        # measurement, and `or None` would erase it.
        cpu_seconds=data.get("cpu_seconds"),
        peak_cpu_cores=data.get("peak_cpu_cores"),
        mean_cpu_cores=data.get("mean_cpu_cores"),
        cpu_limit_cores=data.get("cpu_limit_cores"),
        # CONTRACT REQUEST #26 (accepted on #184, 2026-09-26): when the four
        # were written and where the limit came from. None on every attempt
        # from before it, which is read as `not recorded`, never as a guess.
        cpu_measured_at=as_datetime(data.get("cpu_measured_at")),
        cpu_limit_source=data.get("cpu_limit_source"),
        # Contract request 103 (#1435): the model ID the worker started this
        # attempt's agent with. Read explicitly, as the spend fields above
        # were not. None: no model, or an attempt from before the field.
        model=data.get("model") or None,
        # Contract request 111 (RS-4): the size the scheduler rendered the
        # attempt at, which SIZES-SCHED-2's `create_attempt` writes. Read
        # explicitly, as the spend fields above once were not. None on every
        # attempt from before it: not recorded.
        size_version=data.get("size_version"),
        sized_cpu_request=data.get("sized_cpu_request"),
        sized_cpu_limit=data.get("sized_cpu_limit"),
        sized_memory_gib=data.get("sized_memory_gib"),
        sized_disk_gib=data.get("sized_disk_gib"),
        sized_live_split=_sized_live_split(data.get("sized_live_split")),
    )


def lease_from_dict(data: dict[str, Any]) -> Lease:
    return Lease(
        lease_id=data["lease_id"],
        task_id=data["task_id"],
        attempt_id=data.get("attempt_id", ""),
        tenant_id=data["tenant_id"],
        generation=int(data.get("generation", 0)),
        pools=list(data.get("pools") or []),
        units=int(data.get("units", 1)),
        state=TaskState(data.get("state", TaskState.LEASED.value)),
        created_at=_required_datetime(data.get("created_at")),
        dispatch_deadline=_required_datetime(data.get("dispatch_deadline")),
        expires_at=_required_datetime(data.get("expires_at")),
        heartbeat_at=as_datetime(data.get("heartbeat_at")),
        released_at=as_datetime(data.get("released_at")),
        release_reason=data.get("release_reason"),
        # Absent on a lease reserved before contract request 129 (#2950):
        # None, and `Lease.held_units` then reads the old rule.
        pool_units=_pool_units(data.get("pool_units")),
    )


def _pool_units(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {str(name): int(units) for name, units in value.items()}


# --------------------------------------------------------------------------
# Pools / quota / tenants
# --------------------------------------------------------------------------

def hard_limit_known(pool: SlotPool) -> bool:
    """False for a pool whose document carried no `hard_limit` (contract request 38)."""
    return pool.hard_limit is not None


def pool_from_dict(name: str, data: dict[str, Any]) -> SlotPool:
    """A pool document as a `SlotPool`.

    A missing (or null) `hard_limit` is None -- no ceiling set, UNKNOWN, never
    0 -- exactly as the frozen admission transaction reads it (request 38, #374).
    """
    raw_limit = data.get("hard_limit")
    pool = SlotPool(
        name=name,
        hard_limit=int(raw_limit) if raw_limit is not None else None,
        adaptive_target=data.get("adaptive_target"),
        quota_derived_limit=data.get("quota_derived_limit"),
        active=int(data.get("active", 0)),
        enabled=bool(data.get("enabled", True)),
        updated_at=as_datetime(data.get("updated_at")) or datetime.now(timezone.utc),
    )
    # Who last changed the pool through an admin route (`Store.upsert_pool`
    # writes both). Not a `SlotPool` field -- that dataclass is the frozen
    # contract -- so it rides on the instance, outside the dataclass's fields
    # (equality, `asdict` and the admission transaction never see it), and is
    # read back only through `pool_attribution`.
    setattr(
        pool,
        _POOL_ATTRIBUTION,
        {
            "admin_changed_by": data.get("admin_changed_by"),
            "admin_changed_at": as_datetime(data.get("admin_changed_at")),
            "admin_change": _admin_change(data.get("admin_change")),
        },
    )
    return pool


#: The fields `Store.upsert_pool` records in `admin_change`, and their types.
_ADMIN_CHANGE_FIELDS: dict[str, type] = {"hard_limit": int, "enabled": bool}


def _admin_change(raw: Any) -> dict[str, dict[str, Any]] | None:
    """`admin_change` as stored, narrowed to the fields an admin route writes (#133).

    Each entry is `{"from": ..., "to": ...}`. `from` is null when the pool did
    not exist, or carried no value, before the write. Anything else in the map
    -- a field this codec does not know, a value of the wrong type -- is left
    out rather than served as if it were a ceiling. A pool with nothing left
    is null: no admin change on record.
    """
    if not isinstance(raw, dict):
        return None

    def typed(v: Any, kind: type) -> Any:
        if v is None:
            return None
        # `bool` is an `int` in Python: a ceiling of `True` is not a ceiling.
        if kind is int and (isinstance(v, bool) or not isinstance(v, int)):
            return _INVALID
        if kind is bool and not isinstance(v, bool):
            return _INVALID
        return v

    out: dict[str, dict[str, Any]] = {}
    for field, kind in _ADMIN_CHANGE_FIELDS.items():
        entry = raw.get(field)
        if not isinstance(entry, dict):
            continue
        before, after = typed(entry.get("from"), kind), typed(entry.get("to"), kind)
        if before is _INVALID or after is _INVALID or after is None:
            continue
        out[field] = {"from": before, "to": after}
    return out or None


_INVALID = object()


_POOL_ATTRIBUTION = "_swarm_admin_attribution"


def pool_attribution(pool: SlotPool) -> dict[str, Any]:
    """`admin_changed_by` / `admin_changed_at` / `admin_change` of a pool read by `pool_from_dict` (#133).

    FOR THE ADMIN POOL READS ONLY, which is why it is not in `pool_to_api`:
    `/v1/capacity` serves pools to every tenant member, and an admin's email is
    not tenant data. Both null for a pool no admin route ever changed (the
    store writes neither for internal writers), and for a pool this codec did
    not decode -- never a guess, and never the pool's `updated_at`, which every
    admission rewrites.
    """
    found = getattr(pool, _POOL_ATTRIBUTION, None)
    if not isinstance(found, dict):
        return {"admin_changed_by": None, "admin_changed_at": None, "admin_change": None}
    return dict(found)


#: The task states that prove a lease's dispatch landed: the worker itself
#: writes them, so a container ran. LEASED and DISPATCHED are written by the
#: scheduler before any container exists, and prove nothing.
_WORKER_STARTED = frozenset({TaskState.STARTING, TaskState.RUNNING})


def lease_task_is_current(lease: Lease, task: Task) -> bool:
    """`task` is on the attempt `lease` was taken for -- not a later one.

    By `current_lease_id` when the task names one, by generation otherwise
    (a document written before the field existed). `routes/leases.py` and
    `lease_dispatch_overdue` judge "this lease's task" by the same rule.
    """
    if task.current_lease_id is not None:
        return task.current_lease_id == lease.lease_id
    return task.current_generation == lease.generation


def lease_dispatch_overdue(
    lease: Lease, task: Task | None = None, now: datetime | None = None
) -> bool:
    """Past the dispatch deadline with NO EVIDENCE that the dispatch landed.

    NOT `Lease.dispatch_overdue()` alone (QA, Capacity > Holders, 2026-10-11
    ~12:30Z: 9 of 13 healthy, heartbeating leases flagged). The frozen
    predicate is "the deadline has passed", and a lease's deadline passes on
    every attempt that lives longer than `dispatch_timeout_seconds` -- the
    lease's own state never says otherwise, because it stops at DISPATCHED:
    the worker advances the TASK through STARTING and RUNNING and touches the
    lease only to heartbeat.

    So the flag is computed READ-SIDE from what the worker does write, and
    nothing is added to the heartbeat path or to admission. Either of these is
    proof a container ran, and clears it:

      * the lease has a `heartbeat_at` -- the reconciler's own split
        (`detect_stale_leases`): before the first beat the dispatch deadline
        is the only clock; after it, `silent` and `expired` are, and they
        still catch a worker that beat and then went quiet;
      * the lease's task, on THIS lease's attempt, is STARTING or RUNNING.

    A lease whose task never started and never beat stays overdue, whatever
    the lease's state says -- `mark_dispatched` writes DISPATCHED the moment
    the backend accepts the create call (contract change request 9).
    """
    if not lease.dispatch_overdue(now):
        return False
    if lease.heartbeat_at is not None:
        return False
    if task is not None and task.state in _WORKER_STARTED and lease_task_is_current(lease, task):
        return False
    return True


def lease_to_api(
    lease: Lease, *, task: Task | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Public JSON shape for a lease. No credential material, no backend spec.

    `dispatch_overdue` and `expired` are COMPUTED here rather than left to the
    caller. Both are exactly the kind of thing a UI gets subtly wrong, and
    computing them server-side means every caller agrees with the reconciler.
    `dispatch_overdue` is `lease_dispatch_overdue`: pass the lease's `task`
    when the route has read it, so a STARTING or RUNNING task clears the flag
    even before its first beat lands on the lease.
    """
    return {
        "lease_id": lease.lease_id,
        "task_id": lease.task_id,
        "attempt_id": lease.attempt_id,
        "tenant_id": lease.tenant_id,
        "generation": lease.generation,
        "pools": lease.pools,
        "units": lease.units,
        # What the lease holds in each pool, the provider pools at 1 since
        # contract request 129 (#2950): the per-pool figure a drift check
        # sums against `pool.active`, never `units` times the pools.
        "pool_units": lease.held_units(),
        # Only ever LEASED or DISPATCHED. The worker advances the TASK through
        # STARTING and RUNNING and never touches this field, so a UI must not
        # label this column "state" -- see docs/web-ui/02, trap B.
        "dispatch_state": lease.state.value,
        "created_at": lease.created_at,
        "dispatch_deadline": lease.dispatch_deadline,
        "expires_at": lease.expires_at,
        "heartbeat_at": lease.heartbeat_at,
        "released_at": lease.released_at,
        "release_reason": lease.release_reason,
        # `is_released` is a @property while `is_expired` and
        # `dispatch_overdue` are methods. Mixed, on a frozen model, so it
        # cannot be tidied -- calling the property returns a bool and then
        # tries to call it, which fails at runtime rather than at import.
        #
        # `dispatch_overdue` carries no `state is LEASED` guard: DISPATCHED is
        # written as soon as the backend accepts the create call, so that guard
        # hid every lease whose dispatch was in flight. Nor is it the bare
        # deadline, which flagged every healthy agent older than the dispatch
        # window -- see `lease_dispatch_overdue`.
        "released": lease.is_released,
        "expired": lease.is_expired(now),
        "dispatch_overdue": lease_dispatch_overdue(lease, task, now),
    }


def attempt_to_api(
    attempt: Attempt,
    *,
    masking: TaskMasking | None = None,
    read_at: datetime | None = None,
) -> dict[str, Any]:
    """Public JSON shape for one attempt.

    This is the per-attempt record that `result_summary` cannot give you:
    result_summary is written once, at terminal state, so a task that failed
    twice and succeeded on the third try carries only the third attempt's
    numbers. The first two live here.

    `read_at` is the route's own clock, and `cpu_reading_age_seconds` is the
    CPU reading's age against it (contract request #26): aged on the server,
    as #188's heartbeat reading was, because an age off a browser's clock is a
    guess drawn as a measurement (the #187 review). None when the attempt
    records no time for its figures, or the caller gave no clock.

    `masking` is the attempt's TASK's masker (`task_input.masking_for`):
    `error` is the stderr tail the task's `last_error` is, and is masked by the
    same rules and literals, with `error_redaction_count` (the PR #229 review).
    Every route passes one. Without one the rules still apply -- a masker over
    nothing, which has no literals -- so no caller gets the error unmasked.
    """
    error, error_count = (masking or _RULES_ONLY).text(attempt.error)
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "tenant_id": attempt.tenant_id,
        "generation": attempt.generation,
        "lease_id": attempt.lease_id,
        "backend": attempt.backend,
        "execution_name": attempt.execution_name,
        "created_at": attempt.created_at,
        "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
        "exit_code": attempt.exit_code,
        "error": error,
        "error_redaction_count": error_count,
        "peak_rss_bytes": attempt.peak_rss_bytes,
        "peak_disk_bytes": attempt.peak_disk_bytes,
        "oom_near_miss": attempt.oom_near_miss,
        "checkpoints": attempt.checkpoints,
        # NULL IS NOT ZERO: a caller must render an em dash, never $0.00, or a
        # run with no measurement reads as a free one.
        #
        # This comment used to say "null until the worker fix ships in an
        # agent-runtime-base image". That was false by the time anyone read it:
        # the worker records all five, and `attempt_from_dict` was silently
        # dropping them on the way back out. A comment naming the wrong cause
        # is worse than none -- it was read, believed, and cited as the reason
        # AgentDetail.tsx omits the cost columns, so a working feature stayed
        # hidden behind an explanation that had stopped being true.
        "input_tokens": attempt.input_tokens,
        "output_tokens": attempt.output_tokens,
        "cache_read_input_tokens": attempt.cache_read_input_tokens,
        "cache_creation_input_tokens": attempt.cache_creation_input_tokens,
        "cost_usd": attempt.cost_usd,
        # The attempt's CPU (request #15), on EVERY row: serving it costs no
        # read the route was not already making. It replaced #188's opt-in
        # `include=usage`, which read the task's events per request. Null is
        # not measured, never zero.
        "cpu_seconds": attempt.cpu_seconds,
        "peak_cpu_cores": attempt.peak_cpu_cores,
        "mean_cpu_cores": attempt.mean_cpu_cores,
        "cpu_limit_cores": attempt.cpu_limit_cores,
        # Request #26: when the four were written, where the limit came from,
        # and how old the reading is by this API's clock. Null is not recorded.
        "cpu_measured_at": attempt.cpu_measured_at,
        "cpu_limit_source": attempt.cpu_limit_source,
        "cpu_reading_age_seconds": _age_seconds(attempt.cpu_measured_at, read_at),
        # Contract request 103: the model ID the agent was started with.
        # Null is not recorded.
        "model": attempt.model,
        # Contract request 111 (RS-4, part of #1689): the size the scheduler
        # RENDERED the attempt at. Details reads "4 cores guaranteed, bursts
        # to 8" off these (AgentDetail.tsx `sizedCpuWords`), never off the
        # class, which after a resize no longer says what an older attempt
        # ran at. Null is not recorded -- every attempt from before RS-4 --
        # and is served as null, never filled from the class catalogue.
        "size_version": attempt.size_version,
        "sized_cpu_request": attempt.sized_cpu_request,
        "sized_cpu_limit": attempt.sized_cpu_limit,
        "sized_memory_gib": attempt.sized_memory_gib,
        "sized_disk_gib": attempt.sized_disk_gib,
        "sized_live_split": attempt.sized_live_split,
    }


def _age_seconds(at: datetime | None, now: datetime | None) -> float | None:
    """Seconds from `at` to `now`, never negative; None when either is unknown.

    Clamped at zero: a worker's clock a little ahead of the API's is skew, not
    a reading from the future.
    """
    if at is None or now is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - at).total_seconds()), 1)


def pool_to_api(pool: SlotPool) -> dict[str, Any]:
    # A ceiling nobody set is served as null, and so is everything computed
    # from it (#374): the contract's `SlotPool` already says None for all
    # three (request 38), so nothing here may substitute a 0.
    return {
        "name": pool.name,
        "hard_limit": pool.hard_limit,
        "adaptive_target": pool.adaptive_target,
        "quota_derived_limit": pool.quota_derived_limit,
        "effective_limit": pool.effective_limit,
        "active": pool.active,
        "available": pool.available,
        "enabled": pool.enabled,
        "updated_at": pool.updated_at,
    }


def quota_from_dict(data: dict[str, Any]) -> QuotaState:
    return QuotaState(
        provider=data["provider"],
        tenant_id=data["tenant_id"],
        state=ProviderState(data.get("state", ProviderState.UNKNOWN.value)),
        updated_at=as_datetime(data.get("updated_at")) or datetime.now(timezone.utc),
        configured_hard_max=int(data.get("configured_hard_max", 50)),
        adaptive_target=data.get("adaptive_target"),
        quota_derived_limit=data.get("quota_derived_limit"),
        requests_remaining=data.get("requests_remaining"),
        tokens_remaining=data.get("tokens_remaining"),
        reset_at=as_datetime(data.get("reset_at")),
        cooldown_until=as_datetime(data.get("cooldown_until")),
        last_429_at=as_datetime(data.get("last_429_at")),
        retry_after_seconds=data.get("retry_after_seconds"),
        success_count=int(data.get("success_count", 0)),
        rate_limit_count=int(data.get("rate_limit_count", 0)),
    )


def quota_to_firestore(state: QuotaState) -> dict[str, Any]:
    d = asdict(state)
    d["state"] = state.state.value
    return d


def quota_to_api(state: QuotaState) -> dict[str, Any]:
    payload = quota_to_firestore(state)
    payload["effective_limit"] = state.effective_limit
    return payload


def tenant_from_dict(data: dict[str, Any]) -> Tenant:
    return Tenant(
        tenant_id=data["tenant_id"],
        kind=data.get("kind", "user"),
        principal=data.get("principal", ""),
        # None when the document holds none (F10): the read time here was a
        # fabricated creation date that moved on every read. The frozen
        # `Tenant.created_at` is typed `datetime`; nothing reads it but the
        # API's tenant view, which serves the None as null.
        created_at=as_datetime(data.get("created_at")),  # type: ignore[arg-type]
        display_name=data.get("display_name"),
        max_active=int(data.get("max_active", 20)),
        capacity_units=int(data.get("capacity_units", 40)),
        monthly_budget_usd=data.get("monthly_budget_usd"),
        enabled=bool(data.get("enabled", True)),
        credentials=list(data.get("credentials") or []),
        service_account=data.get("service_account"),
        gcs_prefix=data.get("gcs_prefix"),
        namespace=data.get("namespace"),
    )


def tenant_to_firestore(tenant: Tenant) -> dict[str, Any]:
    return asdict(tenant)


def tenant_to_api(tenant: Tenant) -> dict[str, Any]:
    """Tenant view for callers.

    `credentials` is a list of PROVIDER NAMES, never key material: registering a
    key is write-only by design and no read path in this service can surface it.
    """
    return {
        "tenant_id": tenant.tenant_id,
        "kind": tenant.kind,
        "principal": tenant.principal,
        "display_name": tenant.display_name,
        "created_at": tenant.created_at,
        "max_active": tenant.max_active,
        "capacity_units": tenant.capacity_units,
        "monthly_budget_usd": tenant.monthly_budget_usd,
        "enabled": tenant.enabled,
        "credentials": sorted(tenant.credentials),
        "service_account": tenant.service_account,
        "gcs_prefix": tenant.gcs_prefix,
        "namespace": tenant.namespace,
    }


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------

def workflow_to_firestore(workflow: Workflow) -> dict[str, Any]:
    d = asdict(workflow)
    d["state"] = workflow.state.value
    return d


def workflow_from_dict(data: dict[str, Any]) -> Workflow:
    return Workflow(
        workflow_id=data["workflow_id"],
        tenant_id=data["tenant_id"],
        created_at=_required_datetime(data.get("created_at")),
        updated_at=_required_datetime(data.get("updated_at")),
        state=TaskState(data["state"]),
        submitted_by=data.get("submitted_by", ""),
        steps=[
            WorkflowStep(
                step_id=s["step_id"],
                runner_profile=s["runner_profile"],
                input=dict(s.get("input") or {}),
                depends_on=list(s.get("depends_on") or []),
                resource_class=s.get("resource_class"),
                input_from=dict(s.get("input_from") or {}),
                timeout_seconds=s.get("timeout_seconds"),
                task_id=s.get("task_id"),
                model_class=s.get("model_class") or None,
                budget=_budget_map(s.get("budget")),
            )
            for s in (data.get("steps") or [])
        ],
        on_step_failure=data.get("on_step_failure", "fail_workflow"),
        priority=int(data.get("priority", 0)),
        cancel_requested=bool(data.get("cancel_requested", False)),
    )


def workflow_to_api(
    workflow: Workflow,
    rollup: dict[str, Any] | None = None,
    *,
    step_tasks: Mapping[str, Task] | None = None,
    console_url: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Public JSON shape for a workflow.

    `summary` (`GET /v1/workflows?view=summary`, live QA CH-21) serves each
    step without `input`, `input_redaction_count` and `input_masked_by` -- the
    keys absent, not null, as `GET /v1/tasks?view=summary` drops its rows' --
    and the step input's masking is never run. A list draws no step's input;
    `GET /v1/workflows/{id}` and the step's own task still serve it.

    `links.console` is the workflow's console page and each step's
    `links.console` its agent's, both from `console_url`
    (`ApiSettings.console_url`); null without one, and null on a step that
    has no task yet.

    `step_tasks` maps a step's `task_id` to its task, as far as the route read
    them, and each step's `input` is masked by ITS TASK's masker (the PR #229
    review): the frozen `Workflow` has no metadata, but every step task carries
    the workflow's (`{**spec.metadata, "workflow_step": id}`), so a literal the
    metadata names is masked in `steps[i].input` exactly as in the task's own
    `input`. A route that did not read a step's task passes a sibling's --
    every step task carries the same workflow metadata -- and one that read
    none serves the step's input as null, with `input_masked_by:
    "not_read"`, rather than masked by a masker that never saw the metadata.

    `state` SERVES THE DERIVED VALUE when a rollup is supplied, and the value
    read out of Firestore is served beside it as `stored_state`. That is a
    deliberate change of meaning for an existing field and it is the point of the
    change: every consumer already asks "what state is this workflow in" by
    reading `.state`, and until now the answer was QUEUED forever because nothing
    advanced the stored field. Leaving `.state` faithful to the document would
    have preserved the defect for the sake of a fidelity nobody asked for.

    `rollup` is None only on a path that did not read the steps. The field then
    reports the stored value and says so, rather than implying it was confirmed.
    """
    served = dict(rollup or {})
    state = served.pop("state", None) or workflow.state.value
    # SPEED-D3: the pull requests the workflow waits for, off its step tasks
    # (the frozen `Workflow` has no metadata); the key only on a workflow
    # submitted with `after`, so every other workflow's shape is unchanged.
    after = wait_for_api(step_tasks or {})
    return {
        "workflow_id": workflow.workflow_id,
        "tenant_id": workflow.tenant_id,
        "state": state,
        #: Always the value in Firestore. Kept so the cache can be audited, and
        #: so a consumer that specifically wants the document gets the document.
        "stored_state": workflow.state.value,
        "state_source": "derived" if rollup else "stored",
        **served,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
        "submitted_by": workflow.submitted_by,
        "priority": workflow.priority,
        "on_step_failure": workflow.on_step_failure,
        "cancel_requested": workflow.cancel_requested,
        **({"after_pull_requests": after} if after is not None else {}),
        # SC-SESSION-SCOPE: the Claude Code session that submitted it.
        **workflow_claude_session(step_tasks or {}),
        "steps": [
            _step_to_api(
                s,
                _step_masking(workflow, s, step_tasks or {}),
                console_url,
                summary=summary,
                waiting=_step_waiting(s, step_tasks or {}),
            )
            for s in workflow.steps
        ],
        "links": {"console": workflow_console_url(console_url, workflow.workflow_id)},
    }


def _step_waiting(step: WorkflowStep, step_tasks: Mapping[str, Task]) -> dict[str, Any] | None:
    """A waiting step's one line (#1782): `{reason, summary}` off its task, masked.

    Built from the task document alone -- no event, pool or related read --
    so a workflow view costs nothing more for it. Null for a step with no task
    read, and for one whose task is not waiting.
    """
    task = step_tasks.get(step.task_id) if step.task_id else None
    if task is None:
        return None
    row = waiting_row(build_waiting(task, as_of=utcnow()))
    return mask_waiting(row, masking_for(task, warm=False).text)


def _step_masking(
    workflow: Workflow, step: WorkflowStep, step_tasks: Mapping[str, Task]
) -> tuple[TaskMasking, str] | None:
    """The masker for one step's input, and whose it is; None when no step task was read."""
    own = step_tasks.get(step.task_id) if step.task_id else None
    if own is not None:
        return masking_for(own), "task"
    for sibling in workflow.steps:
        task = step_tasks.get(sibling.task_id) if sibling.task_id else None
        if task is not None:
            # The workflow's metadata, off a sibling: the one key that differs
            # by step, `workflow_step`, is a label and names no credential.
            return TaskMasking(step.input, task.metadata), "workflow"
    return None


def _step_to_api(
    step: WorkflowStep,
    masking: tuple[TaskMasking, str] | None,
    console_url: str | None = None,
    *,
    summary: bool = False,
    waiting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One workflow step. Its `input` is served MASKED, with its count.

    A step's input is the input its task was created with, so it is masked by
    the task's own masker (see `workflow_to_api`). `input_masked_by` says whose:
    `task` (the step's task), `workflow` (a sibling step task's copy of the
    workflow metadata), or `not_read` (no step task was read, so the input is
    not served at all -- read it on the task).

    `input_from`'s VALUES are filenames out of the caller's workflow spec, the
    same as `metadata.expected_outputs` (#227), and this route served them as
    stored: `GET /v1/workflows/{id}` drew the step map straight off the
    document, next to the masked copy `metadata_value` puts in the task's own
    served metadata, so the same secret filename arrived once masked and once
    in clear (owner decision 2026-09-28). Each value is masked here by
    `TaskMasking.name` -- the step's own masker when one was read, `_RULES_ONLY`
    (the rules alone, no literal to check) when it was not, so the map is never
    served in clear merely because this read stopped short of the step's task.
    """
    masker = masking[0] if masking is not None else _RULES_ONLY
    base = {
        "step_id": step.step_id,
        "runner_profile": step.runner_profile,
        "resource_class": step.resource_class,
        "depends_on": step.depends_on,
        "input_from": {key: masker.name(value)[0] for key, value in step.input_from.items()},
        "timeout_seconds": step.timeout_seconds,
        "task_id": step.task_id,
        # Contract request 102: the class the step runs, resolved as its
        # task's is (`served_model_class`).
        **served_model_class(step.runner_profile, step.model_class),
        # Contract request 120: what the step asked for; its task's `budget`
        # is what that resolved to. Null: it asked for none.
        "budget": _budget_limits(step.budget),
        "links": {"console": agent_console_url(console_url, step.task_id)},
        # #1782: why a waiting step waits, in one line (`_step_waiting`).
        "waiting": waiting,
    }
    if summary:
        return base
    if masking is None:
        return {**base, "input": None, "input_redaction_count": None, "input_masked_by": "not_read"}
    _, whose = masking
    masked_input, input_count = masker.step_input_value(step.input)
    return {
        **base,
        "input": masked_input,
        "input_redaction_count": input_count,
        "input_masked_by": whose,
    }


def blocked_reason_values() -> list[str]:
    return [r.value for r in BlockedReason]
