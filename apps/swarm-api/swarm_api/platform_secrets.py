"""Admin › Platform secrets: what is set, how old, and who writes it. STATUS ONLY.

docs/design/secrets-scopes.md §5, with BGD's Q7 answer of 2026-10-23 (§11):
the status comes from Secret Manager METADATA, read under a metadata-only
custom role, `swarmSecretStatusViewer` (terraform/bootstrap/
platform_secret_status.tf), which holds exactly `secretmanager.secrets.get`
and `secretmanager.versions.list`. Lane SSC-PLATFORM-STATUS, part of #2158.

WHAT IT NEVER DOES, each held by tests/unit/control_plane/test_platform_secrets.py:

* **It never reads a value.** The reader calls `get_secret` and
  `list_secret_versions` and nothing else: it never accesses a version, so
  this module is not in test_no_route_serves_credentials.py's
  SECRET_PAYLOAD_READERS. A row carries no value, no length and no last
  characters -- only whether an enabled version exists, how many, and the
  newest one's `create_time`. The role could not read a value if this code asked.
* **It never names a secret outside `swarm-`.** saga-agents-staging is shared
  (CLAUDE.md rule 2): the other team's secrets are agents-* and promptlab-*.
  Every name is checked by `_full_name` before a call is built, a broker
  account document whose recorded secret is not `swarm-account-*` is refused
  and counted, and nothing is LISTED -- the role carries no `secrets.list`,
  so the names come from the catalogue below and from the broker's own
  `accounts` documents, never from the project.
* **It writes nothing.** No route writes, rotates or mints a link for a
  platform secret (§5.3): each is written by the operator's
  `scripts/create-secrets.sh --stdin` or by the system that owns it, and the
  row says which.

A catalogue name Secret Manager does not have is `not_declared` (a deployment
without `enable_typesafe_forwarder`, say); a name the role may not read --
the bootstrap grant not yet applied -- is `unreadable` with
`permission_denied`, never a 500, so the page says what is missing rather
than failing whole.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

log = logging.getLogger(__name__)

#: Every name this module may put in a Secret Manager call starts with this.
#: The bootstrap grant's condition admits the same prefix and nothing else.
PREFIX = "swarm-"

#: The quota broker's per-account secrets, `swarm-account-<tenant>--<label>`
#: and its `-refresh` half (quota_broker.accounts.secret_name,
#: quota_broker.credentials.REFRESH_SUFFIX). Restated, not imported:
#: swarm-api's image does not carry the broker (workspaces.ACCOUNTS says the
#: same of the collection name).
ACCOUNT_PREFIX = "swarm-account-"
ACCOUNT_REFRESH_SUFFIX = "-refresh"

#: The broker's account documents, read for owner, label and recorded secret
#: only. workspaces.ACCOUNTS is the same collection.
ACCOUNTS_COLLECTION = "accounts"

#: Accounts read per request. Two metadata reads each; the pool is single
#: digits today. An account past the cap is reported by `accounts_truncated`,
#: never silently dropped.
ACCOUNT_READ_LIMIT = 200

#: A Secret Manager secret id under the prefix: letters, digits, `-`, `_`,
#: at most 255 characters in all (Secret Manager's own rule).
_SECRET_ID = re.compile(r"\Aswarm-[A-Za-z0-9_-]{1,249}\Z")

#: Row statuses.
SET = "set"                    # an enabled version exists
NOT_SET = "not_set"            # declared, no enabled version
NOT_DECLARED = "not_declared"  # Secret Manager has no such secret
UNREADABLE = "unreadable"      # the metadata read failed; `reason` says how

PERMISSION_DENIED = "permission_denied"
READ_FAILED = "read_failed"

TERRAFORM = "terraform"
SCRIPT = "scripts/create-secrets.sh"
BROKER = "quota-broker"


@dataclass(frozen=True)
class PlatformSecret:
    """One catalogue row: the secret id, who declares it, who writes its value."""

    secret_id: str
    managed_by: str
    written_by: str


#: Read 2026-10-24 from terraform/infra and scripts/create-secrets.sh
#: (design §5.1). Not `swarm-github-secrets-app-private-key`: §11 Q3 gave the
#: existing App the Actions permissions, so the second App is not built.
PLATFORM_SECRETS: tuple[PlatformSecret, ...] = (
    PlatformSecret("swarm-github-app-private-key", TERRAFORM,
                   "scripts/create-secrets.sh --github-app private-key --stdin"),
    PlatformSecret("swarm-github-app-client-secret", TERRAFORM,
                   "scripts/create-secrets.sh --github-app client-secret --stdin"),
    PlatformSecret("swarm-github-app-webhook-secret", TERRAFORM,
                   "scripts/create-secrets.sh --github-app-webhook-secret --stdin"),
    # Created by the script, not Terraform (create-secrets.sh, --search-api-key).
    PlatformSecret("swarm-search-api-key", SCRIPT,
                   "scripts/create-secrets.sh --search-api-key --stdin"),
    PlatformSecret("swarm-egress-credential", TERRAFORM,
                   "scripts/create-secrets.sh --egress-credential"),
    PlatformSecret("swarm-egress-key", TERRAFORM, "scripts/create-secrets.sh --egress-key"),
    PlatformSecret("swarm-live-key", TERRAFORM, "scripts/create-secrets.sh --live-key"),
    PlatformSecret("swarm-child-key", TERRAFORM, "scripts/create-secrets.sh --child-key"),
    PlatformSecret("swarm-graph-ticket-key", TERRAFORM,
                   "scripts/create-secrets.sh --graph-ticket-key"),
    PlatformSecret("swarm-settings-key", TERRAFORM,
                   "scripts/create-secrets.sh --settings-mac-key"),
    PlatformSecret("swarm-typesafe-fwd-key", TERRAFORM,
                   "scripts/create-secrets.sh --typesafe-fwd-key"),
    PlatformSecret("swarm-workspace-grant-key", TERRAFORM,
                   "scripts/create-secrets.sh --workspace-grant-key"),
    PlatformSecret("swarm-tailscale-oauth", TERRAFORM,
                   "scripts/create-secrets.sh --tailscale-oauth --stdin"),
)


class SecretNameRefused(ValueError):
    """A name outside `swarm-` reached the reader. Never sent to Secret Manager."""


def _full_name(project_id: str, secret_id: str) -> str:
    """`projects/<project>/secrets/<id>`, for a `swarm-` id ONLY.

    The one place a resource name is built, so the prefix rule has one gate.
    """
    if not _SECRET_ID.match(secret_id or ""):
        raise SecretNameRefused(f"not a swarm- secret id: {secret_id!r}")
    return f"projects/{project_id}/secrets/{secret_id}"


class NotDeclared(Exception):
    """Secret Manager has no secret by that name."""


class MetadataUnreadable(Exception):
    """The metadata read failed; `reason` is PERMISSION_DENIED or READ_FAILED."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SecretMetadata(Protocol):
    """The two metadata calls, and nothing that reads a value."""

    def enabled_versions(self, secret_id: str) -> list[datetime | None]:
        """The `create_time` of each ENABLED version of a declared secret.

        Raises NotDeclared when the secret does not exist, MetadataUnreadable
        when it cannot be read."""
        ...


def _as_datetime(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    to_dt = getattr(raw, "ToDatetime", None)  # a protobuf Timestamp
    if callable(to_dt):
        return to_dt().replace(tzinfo=timezone.utc)
    return None


def _enabled(state: Any) -> bool:
    # A proto-plus enum (`.name`) or, from a fake, the plain string.
    return str(getattr(state, "name", state)).upper() == "ENABLED"


class SecretManagerMetadata:
    """`secrets.get` and `versions.list` against Secret Manager, nothing else.

    Builds no client until the first call, so `create_app()` needs no
    credentials. There is no method here that accesses a version, and the
    role it runs under (`swarmSecretStatusViewer`) could not if there were.
    """

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._project_id = project_id
        self._client = client
        # The rows are read concurrently (`platform_secret_status`): one client.
        self._client_lock = threading.Lock()

    def _secret_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                from google.cloud import secretmanager

                self._client = secretmanager.SecretManagerServiceClient()
            return self._client

    def enabled_versions(self, secret_id: str) -> list[datetime | None]:
        from google.api_core import exceptions as gexc
        from google.auth import exceptions as gauth

        name = _full_name(self._project_id, secret_id)
        try:
            # The client is built inside the guard: with no Google credentials
            # (CI's offline app, or a deployment that cannot reach them)
            # building it raises google.auth's error, which is not an API call
            # error. Outside the guard it was a 500 for the whole page; here
            # it is one unreadable row (PLATFORM-SECRETS-500).
            client = self._secret_client()
            # secrets.get first: a missing secret is NotFound here, which
            # tells "not declared" from "declared with no version".
            client.get_secret(request={"name": name})
            versions = client.list_secret_versions(
                request={"parent": name, "filter": "state:ENABLED"})
            return [_as_datetime(getattr(v, "create_time", None))
                    for v in versions if _enabled(getattr(v, "state", ""))]
        except gexc.NotFound:
            raise NotDeclared(secret_id) from None
        except gexc.PermissionDenied:
            raise MetadataUnreadable(PERMISSION_DENIED) from None
        except (gexc.GoogleAPICallError, gauth.GoogleAuthError):
            raise MetadataUnreadable(READ_FAILED) from None


def _status_row(meta: SecretMetadata, secret_id: str, *, managed_by: str,
                written_by: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "secret_id": secret_id,
        "managed_by": managed_by,
        "written_by": written_by,
        "status": UNREADABLE,
        "reason": None,
        "enabled_versions": None,
        "newest_enabled_at": None,
        "rotation_in_grace": False,
    }
    try:
        times = meta.enabled_versions(secret_id)
    except NotDeclared:
        row["status"] = NOT_DECLARED
        return row
    except MetadataUnreadable as exc:
        row["reason"] = exc.reason
        log.warning("platform secrets: %s unreadable (%s)", secret_id, exc.reason)
        return row
    known = [t for t in times if t is not None]
    row["status"] = SET if times else NOT_SET
    row["enabled_versions"] = len(times)
    row["newest_enabled_at"] = max(known).isoformat() if known else None
    # Two enabled versions: a rotation whose old version is still in its grace.
    row["rotation_in_grace"] = len(times) > 1
    return row


def _account_secrets(db: Any) -> tuple[list[dict[str, Any]], int, bool]:
    """(accounts with their recorded secret, refused count, truncated).

    Owner, label and the recorded `secret` only; the derived name when none is
    recorded is the broker's two-dash rule. A recorded name outside
    `swarm-account-` is refused, logged by account id, and counted -- it is
    never sent to Secret Manager.
    """
    snaps = list(db.collection(ACCOUNTS_COLLECTION).limit(ACCOUNT_READ_LIMIT + 1).stream())
    truncated = len(snaps) > ACCOUNT_READ_LIMIT
    accounts: list[dict[str, Any]] = []
    refused = 0
    for snap in snaps[:ACCOUNT_READ_LIMIT]:
        data = snap.to_dict() or {}
        owner = str(data.get("owner_tenant") or "")
        label = str(data.get("label") or "")
        base = str(data.get("secret") or "") or f"{ACCOUNT_PREFIX}{owner}--{label}"
        if not (owner and label and base.startswith(ACCOUNT_PREFIX) and _SECRET_ID.match(base)
                and _SECRET_ID.match(base + ACCOUNT_REFRESH_SUFFIX)):
            refused += 1
            log.warning("platform secrets: account %s names no swarm-account- secret; not read",
                        getattr(snap, "id", "?"))
            continue
        accounts.append({
            "account_id": str(data.get("account_id") or getattr(snap, "id", "")),
            "owner_tenant": owner,
            "label": label,
            "secret": base,
        })
    accounts.sort(key=lambda a: a["secret"])
    return accounts, refused, truncated


def platform_secret_status(
    meta: SecretMetadata,
    db: Any,
    *,
    catalogue: Iterable[PlatformSecret] = PLATFORM_SECRETS,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """The page's payload: one row per catalogue secret, then two per account.

    `accounts_unread` is true when the broker's documents could not be read;
    the catalogue rows are still served.

    The rows are read CONCURRENTLY, at most `ROW_READERS` at once, and
    served in this order: each is two metadata calls, and one after another
    they were the admin feed's slowest part (lane LOAD-SPEED-NOTIFICATIONS).
    The account documents are read beside the catalogue's rows.
    """
    with ThreadPoolExecutor(max_workers=ROW_READERS,
                            thread_name_prefix="platform-secrets") as pool:
        return _status(meta, db, list(catalogue), now, pool)


#: The most Secret Manager metadata rows read at once.
ROW_READERS = 8


def _status(meta: SecretMetadata, db: Any, catalogue: list[PlatformSecret],
            now: Callable[[], datetime], pool: ThreadPoolExecutor) -> dict[str, Any]:
    platform = [pool.submit(_status_row, meta, entry.secret_id, managed_by=entry.managed_by,
                            written_by=entry.written_by) for entry in catalogue]
    accounts_unread = False
    refused = 0
    truncated = False
    try:
        accounts, refused, truncated = _account_secrets(db)
    except Exception as exc:  # noqa: BLE001 - the catalogue is served without them
        log.warning("platform secrets: account documents unread (%s)", type(exc).__name__)
        accounts, accounts_unread = [], True
    halves = [
        (account, half, pool.submit(_status_row, meta, secret_id, managed_by=BROKER,
                                    written_by="the quota broker (Capacity › Accounts)"))
        for account in accounts
        for secret_id, half in ((account["secret"], "access"),
                                (account["secret"] + ACCOUNT_REFRESH_SUFFIX, "refresh"))
    ]
    rows = [future.result() | {"family": "platform"} for future in platform]
    for account, half, future in halves:
        rows.append(future.result() | {
            "family": "account", "account_id": account["account_id"],
            "owner_tenant": account["owner_tenant"], "label": account["label"], "half": half})
    return {
        "secrets": rows,
        "counts": summary(rows),
        "accounts_unread": accounts_unread,
        "accounts_refused": refused,
        "accounts_truncated": truncated,
        "read_at": now().isoformat(),
    }


def summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Counts by status, for the page's header line."""
    out: dict[str, int] = {}
    for row in rows:
        out[str(row["status"])] = out.get(str(row["status"]), 0) + 1
    return out
