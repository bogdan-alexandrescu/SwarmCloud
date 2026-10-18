"""The worker's half of a reused browser session: workflow carry and named logins (L4c).

docs/design/live-browser.md section 5.5.1 (LB-7). The gate reads and writes the
browser's session on its own pipe (livegate/session.py); this module decides
WHETHER a session moves, and moves it between the gate and Secret Manager:

* WORKFLOW CARRY, personal tenants only. When a browser step of a workflow in
  `u-<user>` ends with a session a hand-off produced, the worker asks the gate
  for the jar (`session_export`) and stores it as one secret,
  `swarm-tenant-u-<user>-browser-wf-<workflow>`, with a 24-hour TTL that
  swarm-api sets when it creates the secret. The next
  browser step of that workflow reads it and hands it to its gate
  (`session_import`) before the agent exists. swarm-api deletes the secret
  when the workflow ends (lane L9b); the TTL is the backstop.
* NAMED LOGINS, personal tenants only. A task opts in with
  `input.browser_login: "<name>"` -- a name, never cookies (invariant 10). The
  worker reads `swarm-tenant-u-<user>-browser-login-<name>` and imports it
  before the agent runs. An unknown, expired or revoked name FAILS THE TASK
  before the agent runs: the caller asked for a signed-in browser, and an agent
  started without one would spend its attempt on a sign-in page. A new sign-in
  during such a task refreshes the login (a new version, the same expiry).
  `save_login` stores one after a person returns control.

GROUP TENANTS NEVER STORE OR CARRY. Every worker of a group tenant runs as one
service account, so no IAM grant could keep one member's session from
another's agents. `plan_for` gives a group tenant no secret at all, the carrier
never asks its gate for the jar, and a group task naming a login is refused
with `browser_login_personal_only` -- swarm-api refuses it at submission
(L9b); this is the worker's own copy of that rule, because a task document is
not proof that the API ran.

IAM IS THE BOUNDARY; THIS MODULE IS NOT. The worker holds NO project-level grant
on any `-browser-` secret and never `secrets.create` (owner decision 2026-10-11,
lane L8c). Before it writes a session it asks swarm-api for the SLOT
(`SlotApi`, `POST /v1/tasks/<task>/browser-session/slot`, signed with the
attempt key and fenced on the generation): swarm-api creates the secret with
its TTL or expiry and labels, and binds `swarm-agent-worker-u-<user>` -- alone
-- to secretAccessor and secretVersionAdder on that one secret. The worker
then only adds a version. The names here come from the worker's admitted
tenant and the task document, never from the agent, and every name is checked
against the tenant's own prefix before it is used; swarm-api checks again.

A CARRY FAILS OPEN, A LOGIN FAILS CLOSED. A carry the gate cannot import costs
one sign-in prompt; it is recorded and the step goes on. A login the task named
and the gate cannot import fails the task before the agent runs.

NO VALUE IS EVER LOGGED, RECORDED, RAISED OR WRITTEN TO DISK. The jar travels
from the gate's ctl.sock reply to a Secret Manager payload and back, in memory.
Records and errors carry names and counts. Every cookie and storage value the
worker handles is registered with the logger's redaction set, so an agent that
prints a carried value it read from `document.cookie` is scrubbed from the logs
and outputs like any other secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote

from swarm_common.models import utcnow

from .children import PROOF_HEADER, TIMESTAMP_HEADER, ChildApiError
from .livegate.session import EXPORT_OP, IMPORT_OP, valid_jar

#: A personal tenant's id (`swarm_common.identity.tenant_id_for_user`).
PERSONAL_PREFIX = "u-"

#: The workflow carry's lifetime, past which Secret Manager deletes it even if
#: swarm-api's terminal-state delete never ran (section 5.5.1). swarm-api sets
#: it when it creates the secret (`swarm_api.browser_slots.CARRY_TTL_SECONDS`);
#: restated here only for the record of what a carry is.
CARRY_TTL_SECONDS = 24 * 3600

#: The signing purpose of swarm-api's browser-session worker routes,
#: `swarm_api.browser_logins.BROWSER_LOGIN_PURPOSE`, restated: this image does
#: not carry swarm_api. tests/unit/worker/test_browser_session.py holds the two
#: equal.
SLOT_PURPOSE = "swarm-browser-login/v1"
SLOT_PATH = "/v1/tasks/{task_id}/browser-session/slot"

#: A named login's expiry: 14 days unless the person picks another, never more
#: than 30 (section 5.5.1, the save dialog).
LOGIN_DEFAULT_DAYS = 14
LOGIN_MAX_DAYS = 30

ENVELOPE_VERSION = 1

# A login name is a short slug the person typed ("github-qa"). Lower case, so
# two spellings never name two secrets.
_LOGIN_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,47}[a-z0-9])?$")
# A secret id allows [A-Za-z0-9_-] up to 255 characters.
_SECRET_PART = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

Record = Callable[[str, Mapping[str, Any]], None]


class BrowserLoginRefused(RuntimeError):
    """The task names a browser login it cannot have. `cause` is the code; no value."""

    def __init__(self, cause: str, reason: str) -> None:
        super().__init__(f"{cause}: {reason}")
        self.cause = cause
        self.reason = reason


class GateUnavailable(RuntimeError):
    """The gate could not be reached on ctl.sock."""


class GateChannel(Protocol):
    """The worker's ctl.sock client (`livegate.ctl.CtlClient`)."""

    def call(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]: ...


class Vault(Protocol):
    """Secret Manager, as far as a session needs it: read a version, add one.
    Never create: swarm-api creates the secret (`Slots`)."""

    def read(self, name: str) -> str | None: ...

    def write(self, name: str, payload: str) -> None: ...


class SlotRefused(RuntimeError):
    """swarm-api would not provision the secret. `code` is its refusal; no value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Slots(Protocol):
    """swarm-api's slot route (`SlotApi`): create `name` and grant this worker on it."""

    def provision(self, name: str) -> None: ...


def slot_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the attempt key signs for the slot route: swarm-api's
    `browser_login_message`, restated."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{SLOT_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


class SlotApi:
    """`Slots` over swarm-api, signed with the attempt key.

    `api` is the worker's `children.ChildApi` (it mints the ID token and
    never logs it); `sign` is the attempt key's Ed25519 signer, raw message in,
    raw signature out. Nothing this sends or raises carries a value: the body
    is the attempt tuple and a secret id.
    """

    def __init__(
        self,
        api: Any,
        *,
        tenant_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        generation: int,
        sign: Callable[[bytes], bytes],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self._fields = {"tenant_id": tenant_id, "attempt_id": attempt_id,
                        "lease_id": lease_id, "generation": int(generation)}
        self._path = SLOT_PATH.format(task_id=quote(task_id, safe=""))
        self._sign = sign
        self._clock = clock

    def provision(self, name: str) -> None:
        body = json.dumps({**self._fields, "secret_id": name}, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(self._clock()))
        signature = self._sign(slot_message("POST", self._path, body, timestamp))
        headers = {
            PROOF_HEADER: base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
            TIMESTAMP_HEADER: timestamp,
        }
        try:
            _, payload = self._api.call("POST", self._path, body=body, headers=headers)
        except ChildApiError as exc:
            raise SlotRefused(exc.code) from None
        if payload.get("secret_id") != name:
            raise SlotRefused("slot_mismatch")


def is_personal(tenant_id: str) -> bool:
    return isinstance(tenant_id, str) and tenant_id.startswith(PERSONAL_PREFIX) and len(tenant_id) > len(PERSONAL_PREFIX)


def _prefix(tenant_id: str) -> str:
    if not is_personal(tenant_id) or not _SECRET_PART.match(tenant_id):
        raise ValueError("only a personal tenant stores a browser session")
    return f"swarm-tenant-{tenant_id}-browser-"


def carry_secret_name(tenant_id: str, workflow_id: str) -> str:
    if not isinstance(workflow_id, str) or not _SECRET_PART.match(workflow_id):
        raise ValueError("the workflow id cannot form a secret id")
    return f"{_prefix(tenant_id)}wf-{workflow_id}"


def park_secret_name(tenant_id: str, task_id: str) -> str:
    """Where a park keeps its browser's session for the same task's resumed
    attempt (§5.5.2, issue #1362): one per task, a personal tenant's only."""
    if not isinstance(task_id, str) or not _SECRET_PART.match(task_id):
        raise ValueError("the task id cannot form a secret id")
    return f"{_prefix(tenant_id)}park-{task_id}"


def login_secret_name(tenant_id: str, name: str) -> str:
    if not isinstance(name, str) or not _LOGIN_NAME.match(name):
        raise ValueError("not a browser login name")
    return f"{_prefix(tenant_id)}login-{name}"


@dataclass(frozen=True)
class SessionPlan:
    """What this attempt's browser session may come from and go to. Names only."""

    tenant_id: str
    workflow_id: str | None
    carry_secret: str | None
    login_name: str | None
    login_secret: str | None


def requested_login(task_input: Any) -> str | None:
    """`input.browser_login`, validated as a name. A value of any other shape is refused."""
    if not isinstance(task_input, Mapping) or "browser_login" not in task_input:
        return None
    name = task_input.get("browser_login")
    if name is None:
        return None
    if not isinstance(name, str) or not _LOGIN_NAME.match(name):
        raise BrowserLoginRefused("browser_login_invalid", "input.browser_login must name a saved browser login")
    return name


def plan_for(tenant_id: str, task: Mapping[str, Any]) -> SessionPlan:
    """The plan for one attempt. Raises `BrowserLoginRefused` for a login it cannot have.

    `tenant_id` is the worker's admitted tenant (`WorkerConfig.tenant_id`), never
    a field of the task.
    """
    login = requested_login(task.get("input"))
    personal = is_personal(tenant_id)
    if login is not None and not personal:
        raise BrowserLoginRefused(
            "browser_login_personal_only", "a browser login is used only in the person's own workspace"
        )
    workflow_id = task.get("workflow_id") if isinstance(task.get("workflow_id"), str) else None
    carry = None
    if personal and workflow_id:
        try:
            carry = carry_secret_name(tenant_id, workflow_id)
        except ValueError:
            carry = None
    return SessionPlan(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        carry_secret=carry,
        login_name=login,
        login_secret=login_secret_name(tenant_id, login) if login is not None else None,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _envelope(payload: str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    try:
        envelope = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or envelope.get("v") != ENVELOPE_VERSION or not valid_jar(envelope.get("jar")):
        return None
    return envelope


def _values(jar: Mapping[str, Any]) -> list[str]:
    out = [c["value"] for c in jar.get("cookies") or [] if isinstance(c.get("value"), str)]
    for entry in jar.get("origins") or []:
        out += [item[1] for item in entry.get("localStorage") or []]
    return out


class SessionCarrier:
    """Moves one attempt's session between its gate and Secret Manager."""

    def __init__(
        self,
        vault: Vault,
        gate: GateChannel,
        *,
        slots: Slots,
        record: Record,
        register_secret: Callable[[str], None] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._vault = vault
        self._slots = slots
        self._gate = gate
        self._record = record
        self._register = register_secret
        self._now = now
        self._login_expires: str | None = None

    # ------------------------------------------------------------ helpers

    def _gate_call(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._gate.call(op, args)
        except Exception as exc:  # CtlError / CtlRefused, or the channel's own failure
            refused = type(exc).__name__ == "CtlRefused"
            raise GateUnavailable("gate_refused" if refused else "gate_unavailable") from None

    def _protect(self, jar: Mapping[str, Any]) -> None:
        if self._register is None:
            return
        for value in _values(jar):
            self._register(value)

    def _write(self, plan: SessionPlan, name: str, envelope: Mapping[str, Any]) -> None:
        """Ask swarm-api for the slot, then add the version. Raises `SlotRefused`.

        The slot first, every time: it is what creates the secret (with its
        TTL or expiry and labels) and what grants this worker on it, and it is
        idempotent for a secret that exists. A worker never creates one."""
        # Belt and braces under IAM: never a name outside this tenant's own prefix.
        if not name.startswith(_prefix(plan.tenant_id)):
            raise ValueError("refusing a browser-session secret outside the tenant's prefix")
        self._slots.provision(name)
        self._vault.write(name, json.dumps(envelope, separators=(",", ":"), ensure_ascii=True))

    def _export(self) -> dict[str, Any] | None:
        jar = self._gate_call(EXPORT_OP, {}).get("jar")
        if jar is None:
            return None
        if not valid_jar(jar):
            self._record("browser_session_carry_failed", {"cause": "malformed_export"})
            return None
        self._protect(jar)
        return jar

    # ------------------------------------------------------- before agent

    def before_agent(self, plan: SessionPlan) -> str:
        """Import this attempt's session into the gate. Call before the agent is spawned.

        Returns "login", "carry" or "none". Raises `BrowserLoginRefused` when the
        task named a login it cannot have: the task fails before the agent runs.
        """
        if plan.login_secret is not None:
            self._import_login(plan)
            return "login"
        if plan.carry_secret is None:
            return "none"
        envelope = _envelope(self._vault.read(plan.carry_secret))
        if envelope is None:
            return "none"
        self._protect(envelope["jar"])
        try:
            self._gate_call(IMPORT_OP, {"jar": envelope["jar"]})
        except GateUnavailable as exc:
            self._record("browser_session_carry_failed", {"cause": str(exc)})
            return "none"
        self._record("browser_session_carried", {"workflow_id": plan.workflow_id or ""})
        return "carry"

    def _import_login(self, plan: SessionPlan) -> None:
        assert plan.login_secret is not None and plan.login_name is not None
        name = plan.login_name
        try:
            payload = self._vault.read(plan.login_secret)
        except Exception:
            payload = None
        envelope = _envelope(payload)
        if envelope is None:
            # Never saved, revoked (every version destroyed), or deleted by its TTL.
            raise BrowserLoginRefused("browser_login_unknown", f"no browser login named {name} in this workspace")
        expires = _parse_time(envelope.get("expires_at"))
        if expires is None or expires <= self._now():
            raise BrowserLoginRefused("browser_login_expired", f"the browser login {name} has expired")
        self._protect(envelope["jar"])
        try:
            self._gate_call(IMPORT_OP, {"jar": envelope["jar"]})
        except GateUnavailable:
            raise BrowserLoginRefused(
                "browser_login_unavailable", f"the browser login {name} could not be loaded into this attempt's browser"
            ) from None
        self._login_expires = envelope["expires_at"]
        self._record("browser_login_used", {"name": name})

    # -------------------------------------------------------- after agent

    def after_agent(self, plan: SessionPlan) -> list[str]:
        """Store the session a hand-off produced, before the gate's `wipe`.

        Returns the secret names written. A group tenant, a step outside a
        workflow without a login, and a step with no hand-off write nothing.
        """
        if plan.carry_secret is None and plan.login_secret is None:
            return []
        try:
            jar = self._export()
        except GateUnavailable as exc:
            self._record("browser_session_carry_failed", {"cause": str(exc)})
            return []
        if jar is None:
            return []
        written: list[str] = []
        saved_at = _iso(self._now())
        if plan.carry_secret is not None:
            envelope = {"v": ENVELOPE_VERSION, "kind": "carry", "saved_at": saved_at, "jar": jar}
            try:
                self._write(plan, plan.carry_secret, envelope)
            except SlotRefused as exc:
                # A carry fails open: the next step signs in again.
                self._record("browser_session_carry_failed", {"cause": exc.code})
            else:
                written.append(plan.carry_secret)
                self._record("browser_session_stored", {"workflow_id": plan.workflow_id or ""})
        if plan.login_secret is not None and self._login_expires is not None:
            # The login's site asked for a sign-in anyway and the person signed
            # in again: a new version, the same expiry.
            envelope = {
                "v": ENVELOPE_VERSION,
                "kind": "login",
                "saved_at": saved_at,
                "expires_at": self._login_expires,
                "jar": jar,
            }
            try:
                self._write(plan, plan.login_secret, envelope)
            except SlotRefused as exc:
                # The stored login stays as it was; it expires or prompts again.
                self._record("browser_login_refresh_failed", {"name": plan.login_name or "", "cause": exc.code})
            else:
                written.append(plan.login_secret)
                self._record("browser_login_refreshed", {"name": plan.login_name or ""})
        return written

    # ----------------------------------------------------------- save

    def save_login(
        self, plan: SessionPlan, name: str, *, expires_days: int = LOGIN_DEFAULT_DAYS, site: str | None = None
    ) -> str:
        """Store the session a person just returned as a named login of their workspace."""
        if not is_personal(plan.tenant_id):
            raise BrowserLoginRefused(
                "browser_login_personal_only", "a browser login is saved only in the person's own workspace"
            )
        if not isinstance(name, str) or not _LOGIN_NAME.match(name):
            raise BrowserLoginRefused("browser_login_invalid", "a browser login name is a short lower-case slug")
        if isinstance(expires_days, bool) or not isinstance(expires_days, int) or not 1 <= expires_days <= LOGIN_MAX_DAYS:
            raise BrowserLoginRefused("browser_login_invalid", f"a browser login expires within 1 to {LOGIN_MAX_DAYS} days")
        try:
            jar = self._export()
        except GateUnavailable:
            jar = None
        if jar is None:
            raise BrowserLoginRefused("browser_login_no_session", "there is no signed-in session to save")
        now = self._now()
        expires = now + timedelta(days=expires_days)
        secret = login_secret_name(plan.tenant_id, name)
        envelope = {
            "v": ENVELOPE_VERSION,
            "kind": "login",
            "saved_at": _iso(now),
            "expires_at": _iso(expires),
            "site": site if isinstance(site, str) else None,
            "jar": jar,
        }
        try:
            self._write(plan, secret, envelope)
        except SlotRefused as exc:
            raise BrowserLoginRefused("browser_login_slot_refused", exc.code) from None
        self._record("browser_login_saved", {"name": name})
        return secret


class SecretManagerVault:
    """`Vault` over Secret Manager. Built lazily: importing this module needs no credentials."""

    def __init__(self, project_id: str, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _get(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager  # lazy import

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _secret(self, name: str) -> str:
        return f"projects/{self._project_id}/secrets/{name}"

    def read(self, name: str) -> str | None:
        """The latest version, or None when the secret or its version is gone.

        NotFound is a secret never saved or deleted by its TTL; FailedPrecondition
        is a version destroyed by Revoke. Both read as "no such login".
        """
        from google.api_core import exceptions  # lazy import

        try:
            response = self._get().access_secret_version(request={"name": f"{self._secret(name)}/versions/latest"})
        except (exceptions.NotFound, exceptions.FailedPrecondition):
            return None
        return response.payload.data.decode("utf-8")

    def write(self, name: str, payload: str) -> None:
        """A new version of a secret swarm-api provisioned. No create: this
        worker holds no `secrets.create`, only secretVersionAdder on the one
        secret swarm-api granted it (lane L8c)."""
        self._get().add_secret_version(
            request={"parent": self._secret(name), "payload": {"data": payload.encode("utf-8")}}
        )
