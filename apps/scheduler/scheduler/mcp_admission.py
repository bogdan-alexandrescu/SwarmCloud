"""May a task that names registered MCP servers be admitted? Asked before the lease.

docs/design/registered-mcp-servers.md §4.10 (lane MCP-SCHED, part of #1433):

  * a named server whose registration is disabled, or no longer allows the
    task's profile, PARKS `CREDENTIAL_MISSING` with code
    `mcp_server_unavailable` and the server's name; the credential sweep
    returns it to READY once the registration is enabled again (invariant 1:
    parked costs nothing);
  * a `dispatch` server whose credential row is `destroyed` or past
    `expires_at` FAILS `mcp_credential_expired`, with no lease and no retry.
    Nobody can hand a queued task a new value, so parking would wait for
    ever.

`credentials.credential_for` asks this module as the last half of its one
question, so admission (`loop.Scheduler._admit_one`) parks on it before the
capacity transaction (invariants 1, 2 and 3), and the credential sweep
(`loop.Scheduler._promote_credentials`) asks the same thing of every
CREDENTIAL_MISSING park. The worker's attach (`mcp:attach`, design §4.6 steps
5-6) repeats both checks, because state moves between admission and attach:
the attach is the authority, admission is the cheap early answer.

WHICH SERVERS A TASK NAMES is its `input.mcp_servers`, and only on a profile
that declares that input (contract request 88). The input is covered by the
spec signature (`canonical_step_spec`, format 3); the scheduler does not
verify it, and need not: a rewritten list can at worst park a task, or admit
one the attach then withholds -- never hand a worker a credential, which only
the attach serves.

THE DOCUMENTS ARE SWARM-API'S (lanes MCP-API and MCP-SUBMIT), read by id and
never written. Their collections and the id recipes are RESTATED below from
the design (§4.2, §4.4) because the scheduler's image does not carry swarm-api.
Neither document holds a credential value, so nothing read here can leak one.

TENANT-SCOPED (invariant 9). Both ids are derived from the tenant, and a
document is still taken only when its own `tenant_id` is the task's: one that
says otherwise is treated as absent, as `repositories.py` treats it.

A TASK THAT NAMES NO SERVER READS NOTHING, and while the feature is dark
(swarm-api refuses every `mcp_servers` with `mcp_unavailable` until
SWARM_MCP_SERVERS=on) no stored task names one, so no drain reads anything
extra and no admission decision changes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from swarm_common.models import MCP_SERVER_NAME, Task
from swarm_common.profiles import RunnerProfile

#: The profile-declared input that names the servers (contract request 88).
MCP_SERVERS_INPUT = "mcp_servers"

#: swarm-api's collections (design §4.2 and §4.4). Restated: see the module
#: docstring.
SERVERS_COLLECTION = "mcp_servers"
CREDENTIALS_COLLECTION = "mcp_dispatch_credentials"
#: A registration's `auth.mode` that takes a per-run credential the caller
#: sends, and the credential row's state while its secret exists.
AUTH_DISPATCH = "dispatch"
STATE_LIVE = "live"

#: The park's code for a server the task may not use now (design §4.10,
#: request M4: the park reason stays CREDENTIAL_MISSING).
CODE_SERVER_UNAVAILABLE = "mcp_server_unavailable"
#: The failure's code for a per-run credential that can no longer be read.
CODE_CREDENTIAL_EXPIRED = "mcp_credential_expired"


def server_id_for(tenant_id: str, name: str) -> str:
    """Design §4.2: `mcp_` + 16 hex of sha256(tenant_id + NUL + name)."""
    return "mcp_" + hashlib.sha256(f"{tenant_id}\0{name}".encode("utf-8")).hexdigest()[:16]


def credential_row_id(owner_id: str, server_id: str) -> str:
    """Design §4.4: `mcp_dispatch_credentials/{owner_id}~{server_id}`."""
    return f"{owner_id}~{server_id}"


def named_servers(profile: RunnerProfile, inputs: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The server names the task's input gives, in order, once each.

    Empty for a profile that does not declare `mcp_servers`, so a stray key on
    `indexer` or `mock` reads nothing (submission refuses it anyway). A name
    that is not a server name's spelling is kept: it can match no
    registration, so the task parks on it rather than running without it.
    """
    if MCP_SERVERS_INPUT not in (profile.inputs or {}) or not isinstance(inputs, Mapping):
        return ()
    value = inputs.get(MCP_SERVERS_INPUT)
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        name = str(item)
        if name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class Unavailable:
    """One named server the task may not use now. Names only, never a URL."""

    name: str
    #: A registration's `unregistered`, `disabled` or `profile_refused`; a
    #: credential's `missing`, `destroyed` or `expired`.
    reason: str

    def to_detail(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class McpVerdict:
    """What admission may do with a task that names MCP servers."""

    #: Servers whose registration says no now: the task parks, and is
    #: promoted once every one says yes.
    unavailable: tuple[Unavailable, ...] = ()
    #: `dispatch` servers whose credential is gone: the task fails.
    expired: tuple[Unavailable, ...] = ()

    @property
    def runnable(self) -> bool:
        return not self.unavailable and not self.expired

    @property
    def final(self) -> bool:
        """True when nothing can make the task runnable: it fails, it does not park."""
        return bool(self.expired)

    def code(self) -> str | None:
        if self.expired:
            return CODE_CREDENTIAL_EXPIRED
        if self.unavailable:
            return CODE_SERVER_UNAVAILABLE
        return None

    def detail(self) -> dict[str, Any]:
        """What a park or a failure records. Names and reasons, never a value."""
        out: dict[str, Any] = {}
        code = self.code()
        if code is not None:
            out["code"] = code
        if self.unavailable:
            out["mcp_servers"] = [u.to_detail() for u in self.unavailable]
        if self.expired:
            out["mcp_credentials"] = [u.to_detail() for u in self.expired]
        return out


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def judge(
    profile_name: str,
    names: tuple[str, ...],
    registrations: Mapping[str, Mapping[str, Any] | None],
    credentials: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
) -> McpVerdict:
    """THE RULE. Pure: no read, no clock.

    `registrations[name]` is the task's tenant's registration of `name`, or
    None when it has none; `credentials[name]` is the credential row the task
    may use for it, or None. A registration is usable when it is `enabled is
    True` and its `allowed_profiles` is empty or names the profile. A
    `dispatch` server's credential is usable while its row is `live` and
    `expires_at` is ahead of `now`; a row that is missing, destroyed, past its
    instant or carries an instant nobody can read is final.
    """
    unavailable: list[Unavailable] = []
    expired: list[Unavailable] = []
    for name in names:
        reg = registrations.get(name)
        if reg is None or not MCP_SERVER_NAME.fullmatch(name):
            unavailable.append(Unavailable(name, "unregistered"))
            continue
        auth = reg.get("auth") if isinstance(reg.get("auth"), Mapping) else {}
        if auth.get("mode") == AUTH_DISPATCH:
            row = credentials.get(name)
            if row is None:
                expired.append(Unavailable(name, "missing"))
                continue
            if row.get("state") != STATE_LIVE:
                expired.append(Unavailable(name, "destroyed"))
                continue
            expires_at = _as_utc(row.get("expires_at"))
            if expires_at is None or expires_at <= now:
                expired.append(Unavailable(name, "expired"))
                continue
        if reg.get("enabled") is not True:
            unavailable.append(Unavailable(name, "disabled"))
            continue
        allowed = reg.get("allowed_profiles") or []
        if allowed and profile_name not in allowed:
            unavailable.append(Unavailable(name, "profile_refused"))
    return McpVerdict(tuple(unavailable), tuple(expired))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class McpRegistrations:
    """A task's registrations and credential rows, read by id, at most once per drain each.

    Read lazily: a task that names no server reads nothing. `forget()` is
    called at the top of every run (through `credentials.ForgeConnections.
    forget`, which admission and the sweep already hand to `credential_for`),
    so a registration enabled between two drains is seen by the second.

    WHOSE CREDENTIAL ROW (design §4.3-§4.4). The row's owner is the task, or,
    for a workflow's map, the workflow, or, for a child, its parent (Q7: a
    child uses its parent's stored credential). Each is tried in that order
    and the first row of this tenant for this server is the task's.
    """

    def __init__(self, db: Any, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._db = db
        self._now = now
        self._docs: dict[tuple[str, str], dict[str, Any] | None] = {}

    def forget(self) -> None:
        self._docs = {}

    def _read(self, collection: str, doc_id: str, tenant_id: str) -> dict[str, Any] | None:
        key = (collection, doc_id)
        if key not in self._docs:
            snap = self._db.collection(collection).document(doc_id).get()
            data = snap.to_dict() if snap.exists else None
            self._docs[key] = data if data and data.get("tenant_id") == tenant_id else None
        return self._docs[key]

    def _credential(self, task: Task, server_id: str) -> dict[str, Any] | None:
        owners = [task.id, task.workflow_id, task.parent_task_id]
        for owner in owners:
            if not owner:
                continue
            row = self._read(
                CREDENTIALS_COLLECTION, credential_row_id(owner, server_id), task.tenant_id
            )
            if row is not None and row.get("server_id") in (None, server_id):
                return row
        return None

    def verdict(self, task: Task, profile: RunnerProfile) -> McpVerdict | None:
        """None, reading nothing, when the task names no server; else the rule's answer."""
        names = named_servers(profile, task.input)
        if not names:
            return None
        registrations: dict[str, dict[str, Any] | None] = {}
        credentials: dict[str, dict[str, Any] | None] = {}
        for name in names:
            if not MCP_SERVER_NAME.fullmatch(name):
                registrations[name] = None
                continue
            server_id = server_id_for(task.tenant_id, name)
            reg = self._read(SERVERS_COLLECTION, server_id, task.tenant_id)
            if reg is not None and reg.get("name") not in (None, name):
                reg = None
            registrations[name] = reg
            auth = reg.get("auth") if reg is not None and isinstance(reg.get("auth"), Mapping) else {}
            if auth.get("mode") == AUTH_DISPATCH:
                credentials[name] = self._credential(task, server_id)
        return judge(task.runner_profile, names, registrations, credentials, self._now())


def names_mcp_servers(profile: RunnerProfile | None, inputs: Any) -> bool:
    """Whether an attempt of `profile` with `inputs` names a registered MCP
    server, so needs an attempt key to sign its `mcp:attach` with (design
    §4.6, "The attempt key must exist"). Read from the input alone: the
    scheduler reads no registration to dispatch a task."""
    if profile is None or profile.worker_action is not None:
        return False
    return bool(named_servers(profile, inputs if isinstance(inputs, Mapping) else None))


__all__ = [
    "CODE_CREDENTIAL_EXPIRED",
    "CODE_SERVER_UNAVAILABLE",
    "McpRegistrations",
    "McpVerdict",
    "Unavailable",
    "credential_row_id",
    "judge",
    "named_servers",
    "names_mcp_servers",
    "server_id_for",
]
