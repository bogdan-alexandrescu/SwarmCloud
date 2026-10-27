"""Per-run MCP credentials at rest, and their destruction (MCP-API, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.4, and owner decision Q2 of
2026-10-17: a per-run credential dies when its task ends or at its
`expires_in`, whichever is first; the worker's copy dies with each attempt.

  * WHERE. One Secret Manager secret per (submission, server),
    `swarm-tenant-<tenant>-mcpc-<16 hex>`, the hex random. `mcpc-` is a marker
    no other suffix uses, so MCP-IAM's conditioned read grant can find it, as
    `wsec-` does. It is created with Secret Manager's own `ttl` (`expires_in`
    plus `TTL_MARGIN`), so a sweep that never runs still cannot leave it
    behind, and labelled `managed-by=swarm-api`, `swarm-kind=mcp-credential`.
  * THE ROW. `mcp_dispatch_credentials/{owner_id}~{server_id}`, where
    `owner_id` is the task id, or the workflow id for a workflow's map. It
    holds the secret's NAME and version, never the value, its length, a
    prefix or a hash.
  * THE ROW IS ATTESTED. A tenant's agent can write Firestore documents, so
    a row it wrote could point its own task at a colleague's per-run secret,
    or point the sweep at another tenant's. Every row carries an HMAC
    (`RowKeys`) over the fields the attach and the sweep decide on, under
    `swarm-child-key` with this module's own purpose: the key the attempt
    path already needs (no child key, no attempt key, no attach), held by
    swarm-api alone. A row whose attestation does not verify is no row: the
    attach withholds the server and the sweep deletes nothing for it.
  * WHO WRITES. `McpCredentials.store`, called by MCP-SUBMIT's submission
    path after every other check has passed; the value is a local of that
    call and goes to exactly one Secret Manager `add_version`.
  * WHO READS. Only the attach route (`routes/attempts_mcp.py`), through the
    one workspace-secret reader (`attempts_secrets.secret_reader`), after the
    attempt's signature, fence and signed spec. `credential_for` finds the
    row; `version_to_read` rebuilds the version name from a row it has
    checked, so a rewritten row can point only at its own tenant's `-mcpc-`.
  * RE-STORED BY THE CALLER (BGD 2026-10-26, Product Oracle's runs).
    `PUT /v1/workflows/{id}/mcp-credentials` (`restore_workflow`) replaces
    a workflow's credential for each server its body names, through the
    same `McpCredentials.store`: a new secret, a new live row with the new
    `expires_at`. A step that waited past its credential's expiry parks
    CREDENTIAL_MISSING with `credential: "mcp"` (the scheduler's
    `mcp_admission`), and the re-store returns it to READY in the same
    request, guarded on that park (`promote_restored`).
  * DESTROYED WHEN. `McpCredentials.sweep` (`POST /v1/admin/mcp-credentials/
    sweep`, every 10 minutes as the rollup sweeper): a row whose owner is
    terminal, or whose `expires_at` has passed, has its secret deleted and is
    marked `destroyed`. Not at the end of each attempt: a retry is a new
    attempt of the same task and needs it (Q2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets as _random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES

from swarm_common.models import TaskEvent, new_id
from swarm_common.states import EventType, ParkReason, TaskState, assert_transition

from .errors import Conflict, UpstreamUnavailable, ValidationFailed
from .mcp_servers import McpUnavailable, read_registration
from .validation import (
    MCP_DISPATCH_MODE,
    McpCredentialUnexpected,
    McpServerUnregistered,
    mcp_server_names,
)

log = logging.getLogger(__name__)

#: One row per (owner, server). Never a value.
COLLECTION = "mcp_dispatch_credentials"

#: The `Tenant.secret_name` suffix marker of a per-run credential.
MARKER = "mcpc-"

LIVE = "live"
DESTROYED = "destroyed"

OWNER_TASK = "task"
OWNER_WORKFLOW = "workflow"

#: §4.3: 8 B to 8 KiB, printable ASCII, one line, so the relay can put it in
#: a header as it is (`typesafe_shim` holds a key to one printable line too).
MIN_VALUE_BYTES = 8
MAX_VALUE_BYTES = 8 * 1024
#: §4.3: a credential lives 1 minute to 24 hours.
MIN_EXPIRES_IN = 60
MAX_EXPIRES_IN = 86400
#: Secret Manager's own TTL is `expires_in` plus this, so the sweep (which
#: marks the row) normally gets there first, and the TTL is the backstop.
TTL_MARGIN = timedelta(minutes=5)

#: Why a row did not yield a credential, as the attach's `withheld` says it.
CREDENTIAL_MISSING = "credential_missing"
CREDENTIAL_EXPIRED = "credential_expired"

_HEX = re.compile(r"[0-9a-f]{16}")
_VERSION_TAIL = re.compile(r"[0-9]+")
_PRINTABLE = re.compile(r"[\x20-\x7e]+")


class McpCredentialShape(ValidationFailed):
    code = "mcp_credential_shape"


def check_credential(value: Any, expires_in: Any) -> tuple[str, int]:
    """The value and its lifetime, or a refusal that names the rule and never
    the value, its length or any part of it."""
    if (not isinstance(value, str) or not _PRINTABLE.fullmatch(value)
            or not MIN_VALUE_BYTES <= len(value) <= MAX_VALUE_BYTES):
        raise McpCredentialShape(
            f"an MCP credential is {MIN_VALUE_BYTES} B to {MAX_VALUE_BYTES // 1024} KiB of "
            "printable ASCII on one line")
    if (isinstance(expires_in, bool) or not isinstance(expires_in, int)
            or not MIN_EXPIRES_IN <= expires_in <= MAX_EXPIRES_IN):
        raise McpCredentialShape(
            f"an MCP credential's expires_in is {MIN_EXPIRES_IN} to {MAX_EXPIRES_IN} seconds")
    return value, expires_in


def secret_prefix(tenant_id: str) -> str:
    """`swarm-tenant-<tenant>-mcpc-`: every per-run credential of `tenant_id` starts so."""
    return f"swarm-tenant-{tenant_id}-{MARKER}"


def row_id(owner_id: str, server_id: str) -> str:
    return f"{owner_id}~{server_id}"


# ---------------------------------------------------------------------------
# Secret Manager: create with a TTL, add one version, delete. No read path.
# ---------------------------------------------------------------------------


class McpCredentialWriter(Protocol):
    def create_secret(self, tenant_id: str, secret_id: str, ttl: timedelta) -> None: ...

    def add_version(self, secret_id: str, payload: bytes) -> str: ...

    def delete_secret(self, secret_id: str) -> None: ...


class SecretManagerMcpCredentials:
    """The per-run credential's Secret Manager writes, and nothing else.

    NO READ PATH and NO IAM WRITE: the tenant's worker account is never bound
    to a `-mcpc-` secret (§4.4; the agent can mint that account's token).
    swarm-api's existing writer reach over `swarm-tenant-*` creates, adds and
    deletes; MCP-IAM confirms it. Builds no client until the first call, so
    `create_app()` needs no credentials."""

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self._project_id}"

    def create_secret(self, tenant_id: str, secret_id: str, ttl: timedelta) -> None:
        from google.api_core import exceptions as gexc

        try:
            self._secret_client().create_secret(request={
                "parent": self.parent,
                "secret_id": secret_id,
                "secret": {
                    "replication": {"automatic": {}},
                    "ttl": ttl,
                    "labels": {"managed-by": "swarm-api", "swarm-kind": "mcp-credential",
                               "tenant": tenant_id},
                },
            })
        except gexc.AlreadyExists:
            # 64 random bits: a collision is a retry of this very create.
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(
                f"could not create the MCP credential's secret ({type(exc).__name__})") from None

    def add_version(self, secret_id: str, payload: bytes) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        name = f"{self.parent}/secrets/{secret_id}"
        try:
            version = self._secret_client().add_secret_version(request={
                "parent": name, "payload": secretmanager.SecretPayload(data=payload),
            })
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(
                f"could not store the MCP credential ({type(exc).__name__})") from None
        return str(getattr(version, "name", "") or f"{name}/versions/1")

    def delete_secret(self, secret_id: str) -> None:
        from google.api_core import exceptions as gexc

        try:
            self._secret_client().delete_secret(
                request={"name": f"{self.parent}/secrets/{secret_id}"})
        except gexc.NotFound:
            # The TTL got there first: the state the sweep wants.
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(
                f"could not delete an MCP credential's secret ({type(exc).__name__})") from None


# ---------------------------------------------------------------------------
# The row's attestation
# ---------------------------------------------------------------------------

#: The HMAC's purpose: no other MAC made with `swarm-child-key` (a nonce, a
#: registration id, an attestation of an attempt key) is over this prefix.
ROW_PURPOSE = "swarm-mcp-credential-row/v1"
ATTESTATION_FIELD = "attestation"


def row_canonical(row: Mapping[str, Any]) -> bytes | None:
    """The bytes a row's attestation is a MAC of, or None for a row that has
    no canonical form. THE ONLY STATEMENT OF THE FORM: every field the attach
    and the sweep decide on, `expires_at` as whole epoch seconds."""
    fields = [row.get(k) for k in ("owner_id", "owner_kind", "tenant_id", "server_id",
                                   "secret_id", "version")]
    expires_at = row.get("expires_at")
    if not all(isinstance(f, str) and f for f in fields) or not isinstance(expires_at, datetime):
        return None
    form = [ROW_PURPOSE, *fields, int(expires_at.timestamp())]
    return json.dumps(form, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class RowKeys:
    """`swarm-child-key`'s current and (during a rotation) previous versions,
    as `childkey.ChildKeys` holds them: sign with the current, accept either."""

    def __init__(self, current: str, previous: str = "") -> None:
        self._versions = tuple(v for v in (current, previous) if v)

    @classmethod
    def from_settings(cls, settings: Any) -> "RowKeys":
        return cls(str(getattr(settings, "child_key", "") or ""),
                   str(getattr(settings, "child_key_previous", "") or ""))

    @property
    def configured(self) -> bool:
        return bool(self._versions)

    @staticmethod
    def _mac(key: str, message: bytes) -> str:
        return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def sign(self, row: Mapping[str, Any]) -> str:
        message = row_canonical(row)
        if not self._versions or message is None:
            raise ValueError("a row is attested only with a key and a canonical form")
        return self._mac(self._versions[0], message)

    def holds(self, row: Mapping[str, Any]) -> bool:
        stored, message = row.get(ATTESTATION_FIELD), row_canonical(row)
        if not isinstance(stored, str) or not stored or message is None:
            return False
        ok = False
        for version in self._versions:
            # Every version compared, so the time taken says nothing about which.
            ok |= hmac.compare_digest(self._mac(version, message), stored)
        return ok


# ---------------------------------------------------------------------------
# The row, read back by the attach
# ---------------------------------------------------------------------------


def _version_number(row: Mapping[str, Any]) -> str | None:
    secret_id, version = row.get("secret_id"), row.get("version")
    if not isinstance(secret_id, str) or not isinstance(version, str):
        return None
    head, sep, number = version.rpartition("/versions/")
    parts = head.split("/")
    if (not sep or len(parts) != 4 or parts[0] != "projects" or not parts[1]
            or parts[2] != "secrets" or parts[3] != secret_id):
        return None
    return number if _VERSION_TAIL.fullmatch(number) else None


def owned_by(row: Mapping[str, Any], tenant_id: str) -> bool:
    """Is `row`'s secret `tenant_id`'s own `-mcpc-` secret, by its NAME? A row
    is a Firestore document a tenant's agent may be able to write, so its
    `secret_id` is checked as a name and its version as a version of it."""
    secret_id = row.get("secret_id")
    prefix = secret_prefix(tenant_id)
    if row.get("tenant_id") != tenant_id or not isinstance(secret_id, str) \
            or not secret_id.startswith(prefix):
        return False
    return bool(_HEX.fullmatch(secret_id[len(prefix):])) and _version_number(row) is not None


def version_to_read(row: Mapping[str, Any], project_id: str) -> str:
    """The version the attach reads, rebuilt under THIS project from a row
    `owned_by` accepted, never the stored string itself."""
    number = _version_number(row)
    if number is None or not project_id:
        raise ValueError("not an MCP credential row's version")
    return f"projects/{project_id}/secrets/{row['secret_id']}/versions/{number}"


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def credential_for(db: Any, *, tenant_id: str, server_id: str, owner_ids: Iterable[str],
                   now: datetime, keys: RowKeys) -> tuple[dict[str, Any] | None, str | None]:
    """`(row, None)` for the first owner with a live, unexpired row for
    `server_id`, or `(None, reason)`.

    `owner_ids` is the task's own id, then its workflow's, then its parent's
    (Q7: a child uses only its parent's servers, with the parent's stored
    credential), each from the SIGNED task the caller verified. A row of
    another tenant, pointing at another tenant's secret, or whose attestation
    `keys` does not verify, is no row."""
    expired = False
    for owner_id in owner_ids:
        if not owner_id:
            continue
        snap = db.collection(COLLECTION).document(row_id(owner_id, server_id)).get()
        row = (snap.to_dict() or {}) if snap.exists else {}
        if (row.get("owner_id") != owner_id or row.get("server_id") != server_id
                or not owned_by(row, tenant_id)):
            continue
        if not keys.holds(row):
            log.warning("mcp credential row=%s dropped: its attestation does not verify",
                        snap.id)
            continue
        expires_at = _as_datetime(row.get("expires_at"))
        if row.get("state") != LIVE or expires_at is None or expires_at <= now:
            expired = True
            continue
        return row, None
    return None, CREDENTIAL_EXPIRED if expired else CREDENTIAL_MISSING


# ---------------------------------------------------------------------------
# The store and the sweep
# ---------------------------------------------------------------------------


@dataclass
class SweepReport:
    examined: int = 0
    destroyed: int = 0
    failed: int = 0
    kept: int = 0

    def to_api(self) -> dict[str, int]:
        return {"examined": self.examined, "destroyed": self.destroyed,
                "failed": self.failed, "kept": self.kept}


class McpCredentials:
    """`mcp_dispatch_credentials/` and the secrets its rows name."""

    def __init__(self, db: Any, writer: McpCredentialWriter, *,
                 now: Callable[[], datetime], keys: RowKeys) -> None:
        self._db = db
        self._writer = writer
        self._now = now
        self._keys = keys

    def store(self, *, tenant_id: str, owner_id: str, owner_kind: str,
              registration: Mapping[str, Any], value: str, expires_in: int,
              by: str) -> dict[str, Any]:
        """Store one per-run credential for `registration`, owned by a task
        or a workflow; answer `{"server", "expires_at"}` and nothing about the
        value. Every other check of the submission has passed before this is
        called (MCP-SUBMIT): nothing here refuses after the value is stored,
        except Secret Manager itself, whose failure leaves a secret the TTL
        removes and no row."""
        if owner_kind not in (OWNER_TASK, OWNER_WORKFLOW):
            raise ValueError("owner_kind is task or workflow")
        if registration.get("tenant_id") != tenant_id:
            raise ValueError("the registration is not this tenant's")
        if not self._keys.configured:
            raise McpUnavailable(
                "this deployment has no swarm-child-key, so it can neither attest a "
                "per-run MCP credential nor attach one; nothing was stored")
        value, expires_in = check_credential(value, expires_in)
        server = str(registration.get("server_id"))
        name = str(registration.get("name"))
        now = self._now()
        # Whole seconds: the attestation covers `expires_at` as epoch seconds.
        expires_at = (now + timedelta(seconds=expires_in)).replace(microsecond=0)
        # `Tenant.secret_name(f"mcpc-{hex}")`'s spelling (§5 M4), from the id alone.
        secret_id = f"{secret_prefix(tenant_id)}{_random.token_hex(8)}"
        self._writer.create_secret(tenant_id, secret_id, timedelta(seconds=expires_in) + TTL_MARGIN)
        version = self._writer.add_version(secret_id, value.encode("ascii"))
        row = {
            "owner_id": owner_id, "owner_kind": owner_kind, "tenant_id": tenant_id,
            "server_id": server, "name": name, "secret_id": secret_id, "version": version,
            "expires_at": expires_at, "state": LIVE, "created_at": now, "created_by": by,
            "destroyed_at": None, "destroyed_reason": None,
        }
        row[ATTESTATION_FIELD] = self._keys.sign(row)
        self._db.collection(COLLECTION).document(row_id(owner_id, server)).set(row)
        log.info("mcp credential stored tenant=%s owner=%s server=%s secret=%s expires_at=%s",
                 tenant_id, owner_id, server, secret_id, expires_at.isoformat())
        return {"server": name, "expires_at": expires_at.isoformat()}

    # -- the sweep -------------------------------------------------------------

    def _task_done(self, task_id: str) -> bool:
        """A task is done when it is terminal and no child of it is still
        live: a child uses its parent's row (Q7). A task not written yet is
        NOT done: the submission stores its credential before it writes the
        task, and a sweep between the two must not destroy it. A row whose
        task never appears dies at its own `expires_at`."""
        snap = self._db.collection("tasks").document(task_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else None
        if doc is None or not _terminal(doc.get("state")):
            return False
        children = self._db.collection("tasks") \
            .where(filter=FieldFilter("parent_task_id", "==", task_id))
        return all(_terminal((c.to_dict() or {}).get("state")) for c in children.stream())

    def _owner_done(self, row: Mapping[str, Any]) -> bool:
        owner_id = str(row.get("owner_id") or "")
        if row.get("owner_kind") == OWNER_WORKFLOW:
            tasks = [s for s in self._db.collection("tasks")
                     .where(filter=FieldFilter("workflow_id", "==", owner_id)).stream()
                     if (s.to_dict() or {}).get("tenant_id") == row.get("tenant_id")]
            # No task yet is a workflow not written yet, as above.
            return bool(tasks) and all(self._task_done(s.id) for s in tasks)
        return self._task_done(owner_id)

    def sweep(self) -> dict[str, int]:
        """Destroy every live row whose owner is terminal or whose
        `expires_at` has passed: the secret first, then the row marked, so a
        failed delete leaves the row live for the next sweep to try again.

        A row whose attestation does not verify was not written by swarm-api:
        its secret is NOT deleted (it may name someone else's), and the row is
        marked `destroyed` with reason `unattested` so it is not examined
        again. Without a key nothing verifies, so nothing is deleted at all
        and each secret's own TTL is what removes it."""
        now = self._now()
        report = SweepReport()
        query = self._db.collection(COLLECTION).where(filter=FieldFilter("state", "==", LIVE))
        for snap in query.stream():
            row = snap.to_dict() or {}
            report.examined += 1
            if not self._keys.configured:
                report.kept += 1
                continue
            if not self._keys.holds(row):
                self._mark(snap.id, "unattested", now)
                report.failed += 1
                log.warning("mcp credential sweep: row=%s is not attested; its secret was "
                            "not deleted", snap.id)
                continue
            expires_at = _as_datetime(row.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                reason = "expired"
            elif self._owner_done(row):
                reason = "owner_terminal"
            else:
                report.kept += 1
                continue
            tenant_id = str(row.get("tenant_id") or "")
            secret_id = row.get("secret_id")
            # Only a secret this row may name: a rewritten row never makes the
            # sweep delete another tenant's (or any other) secret.
            if isinstance(secret_id, str) and secret_id.startswith(secret_prefix(tenant_id)) \
                    and _HEX.fullmatch(secret_id[len(secret_prefix(tenant_id)):]):
                try:
                    self._writer.delete_secret(secret_id)
                except UpstreamUnavailable:
                    report.failed += 1
                    log.error("mcp credential sweep could not delete row=%s secret=%s",
                              snap.id, secret_id)
                    continue
            self._mark(snap.id, reason, now)
            report.destroyed += 1
            log.info("mcp credential destroyed row=%s tenant=%s secret=%s reason=%s",
                     snap.id, tenant_id, secret_id, reason)
        return report.to_api()

    def _mark(self, doc_id: str, reason: str, now: datetime) -> None:
        ref = self._db.collection(COLLECTION).document(doc_id)

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            row = (snap.to_dict() or {}) if snap.exists else {}
            if row.get("state") != LIVE:
                return
            txn.set(ref, {**row, "state": DESTROYED, "destroyed_at": now,
                          "destroyed_reason": reason})

        _write(self._db.transaction())


def _terminal(state: Any) -> bool:
    return any(state == s.value for s in TERMINAL_STATES)


# ---------------------------------------------------------------------------
# The caller's re-store (PUT /v1/workflows/{id}/mcp-credentials)
# ---------------------------------------------------------------------------

#: The scheduler's `mcp_admission.MCP_CREDENTIAL`, restated (this image does
#: not carry the scheduler): what a park waiting for a re-store names as its
#: `blocked_by[].credential`. tests/unit/control_plane/test_mcp_restore.py
#: holds the two equal.
PARK_CREDENTIAL = "mcp"

#: The `reason` of the READY event a re-store writes on a park it returns.
RESTORED = "mcp_credential_restored"


class WorkflowFinished(Conflict):
    """The workflow has ended: no step of it will ever use a credential."""

    code = "workflow_finished"


def workflow_server_names(step_tasks: Iterable[Mapping[str, Any]]) -> set[str]:
    """Every server a step task of the workflow names in `input.mcp_servers`."""
    names: set[str] = set()
    for doc in step_tasks:
        names.update(mcp_server_names(doc.get("input") if isinstance(doc.get("input"), Mapping)
                                      else None))
    return names


def check_restore(db: Any, tenant_id: str, credentials: Mapping[str, Any],
                  step_tasks: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every check a re-store makes, BEFORE anything is stored: each key a
    server a step of the workflow names, an enabled `dispatch` registration
    of this tenant, and each value the shape the create body's is
    (`check_credential`). Answers each server's registration by name.
    Every refusal names the server, never a value."""
    if not credentials:
        raise McpCredentialShape("mcp_credentials names no server; nothing was stored")
    named = workflow_server_names(step_tasks)
    unnamed = sorted(k for k in credentials if k not in named)
    if unnamed:
        raise McpCredentialUnexpected(
            "mcp_credentials names a server no step of this workflow names in "
            "input.mcp_servers; nothing was stored",
            detail={"field": "mcp_credentials", "servers": unnamed})
    registrations: dict[str, dict[str, Any]] = {}
    for name in sorted(credentials):
        registration = read_registration(db, tenant_id, name)
        if registration is None or registration.get("enabled") is not True:
            raise McpServerUnregistered(
                "mcp_credentials names a server that is not an enabled MCP server "
                "registered in this workspace; nothing was stored",
                detail={"field": "mcp_credentials", "server": name})
        if (registration.get("auth") or {}).get("mode") != MCP_DISPATCH_MODE:
            raise McpCredentialUnexpected(
                "mcp_credentials names a server whose auth mode is not `dispatch` "
                "(its key is the workspace's, or it takes none); nothing was stored",
                detail={"field": "mcp_credentials", "servers": [name]})
        entry = credentials[name]
        check_credential(getattr(entry, "value", None), getattr(entry, "expires_in", None))
        registrations[name] = registration
    return registrations


def restore_workflow(store: "McpCredentials", db: Any, writer: McpCredentialWriter, *,
                     tenant_id: str, workflow_id: str,
                     registrations: Mapping[str, Mapping[str, Any]],
                     credentials: Mapping[str, Any], by: str) -> list[dict[str, Any]]:
    """Replace the workflow's credential for each checked server: a new
    secret and a new live row (`McpCredentials.store`, which logs `mcp
    credential stored ... owner=wf_...`), then the replaced row's own secret
    deleted, best effort -- its Secret Manager TTL removes it otherwise.
    Servers the body does not name are untouched. Answers
    `{"server", "expires_at"}` per server, nothing about a value."""
    stored: list[dict[str, Any]] = []
    for name in sorted(registrations):
        registration = registrations[name]
        server = str(registration.get("server_id"))
        snap = db.collection(COLLECTION).document(row_id(workflow_id, server)).get()
        old = (snap.to_dict() or {}) if snap.exists else {}
        entry = credentials[name]
        stored.append(store.store(
            tenant_id=tenant_id, owner_id=workflow_id, owner_kind=OWNER_WORKFLOW,
            registration=registration, value=entry.value, expires_in=entry.expires_in, by=by))
        secret_id = old.get("secret_id")
        if old.get("state") == LIVE and isinstance(secret_id, str) \
                and secret_id.startswith(secret_prefix(tenant_id)) \
                and _HEX.fullmatch(secret_id[len(secret_prefix(tenant_id)):]):
            try:
                writer.delete_secret(secret_id)
            except UpstreamUnavailable:
                log.warning("mcp credential re-store: the replaced secret=%s was not deleted; "
                            "its TTL removes it", secret_id)
    return stored


def _waits_on(doc: Mapping[str, Any], servers: set[str]) -> list[str]:
    """The re-stored servers a CREDENTIAL_MISSING park waits on, by name."""
    named: list[str] = []
    for block in doc.get("blocked_by") or []:
        if isinstance(block, Mapping) and block.get("credential") == PARK_CREDENTIAL \
                and block.get("server") in servers and block["server"] not in named:
            named.append(block["server"])
    return named


def step_task_docs(db: Any, tenant_id: str, workflow_id: str) -> dict[str, dict[str, Any]]:
    """The workflow's step tasks in this tenant, by id: one query."""
    query = db.collection("tasks").where(filter=FieldFilter("workflow_id", "==", workflow_id))
    return {snap.id: doc for snap in query.stream()
            if (doc := snap.to_dict() or {}).get("tenant_id") == tenant_id}


def promote_restored(db: Any, *, tenant_id: str, workflow_id: str,
                     task_ids: Iterable[str], servers: Iterable[str],
                     now: datetime) -> int:
    """Return to READY every step of the workflow PARKED CREDENTIAL_MISSING
    on one of the re-stored `servers` (its `blocked_by[]` entry with
    `credential: "mcp"`), each in its own transaction guarded on that park,
    with a READY event naming the servers. READY holds nothing (invariant
    1): admission, later, asks the whole credential question again before
    any lease, so a park that also waits on something else is parked again
    there. Answers how many it returned. A step parked meanwhile on another
    server, or a child task the query does not reach, is the scheduler's
    credential sweep's, on the drain this request wakes."""
    wanted = set(servers)
    promoted = 0
    for task_id in task_ids:
        ref = db.collection("tasks").document(task_id)

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            current = _snapshot(txn.get(ref))
            doc = (current.to_dict() or {}) if current.exists else {}
            if (doc.get("tenant_id") != tenant_id or doc.get("workflow_id") != workflow_id
                    or doc.get("state") != TaskState.PARKED.value
                    or doc.get("park_reason") != ParkReason.CREDENTIAL_MISSING.value):
                return False
            named = _waits_on(doc, wanted)
            if not named:
                return False
            assert_transition(TaskState.PARKED, TaskState.READY)
            txn.update(ref, {"state": TaskState.READY.value, "park_reason": None,
                             "next_eligible_at": None, "blocked_by": [], "updated_at": now})
            from .codec import event_to_firestore
            from .store import EVENTS

            event = TaskEvent(
                event_id=new_id("ev"), task_id=task_id, tenant_id=tenant_id,
                type=EventType.READY, at=now, attempt_id=None,
                detail={"reason": RESTORED,
                        "park_reason": ParkReason.CREDENTIAL_MISSING.value,
                        "servers": named})
            txn.set(ref.collection(EVENTS).document(event.event_id), event_to_firestore(event))
            return True

        if _apply(db.transaction()):
            promoted += 1
    return promoted
