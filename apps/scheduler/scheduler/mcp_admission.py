"""May this task call the MCP servers its signed input names? Asked before the lease.

docs/design/registered-mcp-servers.md §4.10 (lane MCP-SCHED, part of #1433).
`credentials.credential_for` asks this module as the last half of its one
question, so admission (`loop.Scheduler._admit_one`) refuses before the capacity
transaction (invariants 1, 2 and 3), and the credential sweep
(`loop.Scheduler._promote_credentials`) returns a CREDENTIAL_MISSING park to
READY once every named server answers yes again. Two outcomes, and they are not
the same kind of no:

  * A SERVER THE TASK MAY NOT USE NOW: its registration is disabled, absent, or
    no longer lists the task's profile in `allowed_profiles`. That is an admin's
    to change, so the task parks CREDENTIAL_MISSING with code
    `mcp_server_unavailable` and waits, at no cost (contract request M4: no new
    park reason). Disabling is the kill switch, and a registration is never
    deleted, so an absent one is as good as disabled: a re-registration under
    the same name has the same id (`server_id_for`) and promotes it.
  * A CREDENTIAL NOBODY CAN REPLACE: a `dispatch` server whose credential row
    is `destroyed`, past `expires_at`, or not there at all. The caller supplied
    the value at submission and is not online to supply another, so waiting
    would wait for ever. The answer is TERMINAL (`McpAnswer.terminal`), code
    `mcp_credential_expired` (or `mcp_credential_missing`), and it never turns
    runnable, so the sweep never promotes it. §4.10 says FAILED, no lease, no
    retry; see `McpAnswer.terminal` for what admission does with it today.

THE ATTACH IS THE AUTHORITY, ADMISSION IS THE CHEAP EARLY ANSWER (§4.10). The
worker's attach route (§4.6 steps 5-6) re-reads both, because state moves
between the two. So nothing here reads Secret Manager, and nothing here proves a
row: a forged `live` row can at worst admit a task whose attach then withholds
the server and fails the attempt before the agent runs.

WHICH SERVERS A TASK NAMES is `names_mcp_servers`, and only there: the profile
declares the `mcp_servers` input (contract request 88) and the task's input
gives a non-empty list. `dispatch.worker_env` reads the same function to decide
the attempt key. The scheduler does not verify the spec signature, and need
not: a rewritten `mcp_servers` fails the worker's spec check before anything is
attached, and the attach reads the names from the VERIFIED input, never from
here. What a rewrite could do here is park a task, or admit one the worker then
refuses -- never hand a worker a server or a credential.

A TASK THAT NAMES NO SERVER READS NOTHING. With `SWARM_MCP_SERVERS` off,
swarm-api refuses every `mcp_servers` submission (`mcp_unavailable`), so no
task names one and nothing here is reached: the feature ships dark.

THE DOCUMENTS ARE swarm-api's (lane MCP-API), read by id and never written:

  * `mcp_servers/{server_id}` (§4.2), `server_id` = `mcp_` + the first 16 hex
    of sha256(tenant_id + "\\0" + name). A document counts only for the tenant
    and the name it stores (invariant 9: another tenant's id answers like an
    absent one).
  * `mcp_dispatch_credentials/{owner_id}~{server_id}` (§4.4), `owner_id` the
    task, or its workflow for a workflow's map, or -- for a child, which has no
    `mcp_credentials` of its own (Q7) -- its parent or its parent's workflow.
    The first of those rows that exists, and is the task's tenant's and this
    server's, is the task's credential row. It holds no value, and nothing
    here reads one.

The collection names and the id recipe are RESTATED, because the scheduler's
image does not carry swarm-api. MCP-API's lane holds them to its own module
with a test, as tests/unit/control_plane/test_secret_slot_admission.py holds
`secret_slots`' to swarm-api's.

Every read is by id, and only for a task that names a server: at most four
registrations, and for a `dispatch` server at most four credential rows plus
one parent task. Nothing is cached across tasks, so a registration enabled
between two drains is seen by the second.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from swarm_common.models import Task, utcnow
from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

from .codec import as_datetime

#: swarm-api's collections (§4.2, §4.4). RESTATED: see the module docstring.
SERVERS_COLLECTION = "mcp_servers"
CREDENTIALS_COLLECTION = "mcp_dispatch_credentials"
TASKS_COLLECTION = "tasks"

#: The profile-declared input that names the servers (contract request 88).
MCP_SERVERS_INPUT = "mcp_servers"

#: `auth.mode` of a registration whose credential the caller sends per run.
DISPATCH_MODE = "dispatch"
#: `state` of a credential row the sweep has not destroyed.
LIVE = "live"


class McpCode(str, Enum):
    """The code a CREDENTIAL_MISSING park records for a server."""

    #: Disabled, absent, or not allowed for the profile. Waits for an admin.
    SERVER_UNAVAILABLE = "mcp_server_unavailable"
    #: The per-run credential was destroyed or has expired. Terminal.
    CREDENTIAL_EXPIRED = "mcp_credential_expired"
    #: A `dispatch` server with no credential row for this task. Terminal.
    CREDENTIAL_MISSING = "mcp_credential_missing"


_TERMINAL = frozenset({McpCode.CREDENTIAL_EXPIRED, McpCode.CREDENTIAL_MISSING})


@dataclass(frozen=True)
class McpRefusal:
    """One named server that said no. Its name only: never a URL, a server id
    or a secret id, so a park event says what to fix and nothing else."""

    name: str
    code: McpCode
    #: `disabled`, `unregistered`, `profile_refused`, `expired`, `destroyed`
    #: or `missing`.
    why: str

    def to_detail(self) -> dict[str, str]:
        return {"name": self.name, "code": self.code.value, "why": self.why}


@dataclass(frozen=True)
class McpAnswer:
    """What every server a task names said, in the order the task names them."""

    refusals: tuple[McpRefusal, ...]

    @property
    def runnable(self) -> bool:
        return not self.refusals

    @property
    def terminal(self) -> bool:
        """THE TASK CAN NEVER RUN: a credential nobody can replace. §4.10 ends
        it FAILED `mcp_credential_expired` with no lease. The owner chose a
        FAILED edge over a dead-letter (2026-10-18), and contract request 98
        added QUEUED -> FAILED. But admission (`loop.py`) holds the task in
        READY and the credential sweep in PARKED, and the frozen state machine
        has neither READY -> FAILED nor PARKED -> FAILED
        (`swarm_common.states._ALLOWED`; request 99 asks for both). So such a
        task still parks CREDENTIAL_MISSING with this code and is never
        promoted: no lease and no cost (invariant 1), but no end either: a
        workflow step blocks its workflow and a parent waits on its child.
        tests/unit/scheduler/test_mcp_admission.py pins the park, and goes red
        the day either edge exists."""
        return any(r.code in _TERMINAL for r in self.refusals)

    @property
    def code(self) -> McpCode | None:
        """The code the park records: a terminal one when there is one, since
        enabling a server would not make that task runnable."""
        for refusal in self.refusals:
            if refusal.code in _TERMINAL:
                return refusal.code
        return self.refusals[0].code if self.refusals else None

    def to_detail(self) -> dict[str, Any]:
        code = self.code
        return {
            "code": code.value if code is not None else None,
            "mcp_servers": [r.to_detail() for r in self.refusals],
        }


def server_id_for(tenant_id: str, name: str) -> str:
    """§4.2's id: stable, and carrying no name."""
    return "mcp_" + hashlib.sha256(f"{tenant_id}\0{name}".encode("utf-8")).hexdigest()[:16]


def named_servers(profile_name: str, inputs: Any) -> tuple[str, ...]:
    """The servers a task on `profile_name` with `inputs` names, in order,
    each once. Empty unless the profile DECLARES the input: a stray key on a
    profile without it names nothing here, and is the spec check's to refuse."""
    profile = RUNNER_PROFILES.get(profile_name)
    if profile is None or MCP_SERVERS_INPUT not in (profile.inputs or {}):
        return ()
    value = inputs.get(MCP_SERVERS_INPUT) if isinstance(inputs, Mapping) else None
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for name in value:
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return tuple(out)


def names_mcp_servers(profile_name: str, inputs: Any) -> bool:
    """Whether an attempt on this profile with these inputs attaches an MCP
    server, so needs an attempt key to sign the attach with (§4.6)."""
    return bool(named_servers(profile_name, inputs))


class McpRegistrations:
    """Reads the registrations and credential rows a task's servers need."""

    def __init__(self, db: Any) -> None:
        self._db = db

    def _read(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(collection).document(doc_id).get()
        return (snap.to_dict() or None) if snap.exists else None

    def answer(
        self, task: Task, profile: RunnerProfile, *, now: datetime | None = None
    ) -> McpAnswer | None:
        """None, reading nothing, when the task names no server; else every
        refusal, empty when every server may be used."""
        names = named_servers(profile.name, task.input)
        if not names:
            return None
        moment = now or utcnow()
        owners: list[str] | None = None
        refusals: list[McpRefusal] = []
        for name in names:
            server_id = server_id_for(task.tenant_id, name)
            doc = self._read(SERVERS_COLLECTION, server_id)
            if doc is None or doc.get("tenant_id") != task.tenant_id or doc.get("name") != name:
                refusals.append(McpRefusal(name, McpCode.SERVER_UNAVAILABLE, "unregistered"))
                continue
            if doc.get("enabled") is not True:
                refusals.append(McpRefusal(name, McpCode.SERVER_UNAVAILABLE, "disabled"))
            allowed = doc.get("allowed_profiles") or []
            if allowed and profile.name not in allowed:
                refusals.append(McpRefusal(name, McpCode.SERVER_UNAVAILABLE, "profile_refused"))
            auth = doc.get("auth") if isinstance(doc.get("auth"), Mapping) else {}
            if auth.get("mode") != DISPATCH_MODE:
                continue
            if owners is None:
                owners = self._owners(task)
            why = self._credential_refusal(task, server_id, owners, moment)
            if why is not None:
                code = McpCode.CREDENTIAL_MISSING if why == "missing" else McpCode.CREDENTIAL_EXPIRED
                refusals.append(McpRefusal(name, code, why))
        return McpAnswer(tuple(refusals))

    def _owners(self, task: Task) -> list[str]:
        """Whose credential rows serve this task, in the order they are tried."""
        owners = [task.id]
        if task.workflow_id:
            owners.append(task.workflow_id)
        if task.parent_task_id:
            owners.append(task.parent_task_id)
            parent = self._read(TASKS_COLLECTION, task.parent_task_id)
            if parent is not None and parent.get("tenant_id") == task.tenant_id:
                workflow = parent.get("workflow_id")
                if isinstance(workflow, str) and workflow:
                    owners.append(workflow)
        return list(dict.fromkeys(owners))

    def _credential_refusal(
        self, task: Task, server_id: str, owners: list[str], now: datetime
    ) -> str | None:
        """None when the task's credential row for `server_id` is live and
        unexpired; else why not."""
        for owner in owners:
            row = self._read(CREDENTIALS_COLLECTION, f"{owner}~{server_id}")
            if row is None or row.get("tenant_id") != task.tenant_id \
                    or row.get("server_id") != server_id:
                continue
            if row.get("state") != LIVE:
                return "destroyed"
            try:
                expires_at = as_datetime(row.get("expires_at"))
            except (TypeError, ValueError):
                expires_at = None
            # A row with no readable expiry is not evidence of a live value.
            if expires_at is None or expires_at <= now:
                return "expired"
            return None
        return "missing"


__all__ = [
    "McpAnswer",
    "McpCode",
    "McpRefusal",
    "McpRegistrations",
    "named_servers",
    "names_mcp_servers",
    "server_id_for",
]
