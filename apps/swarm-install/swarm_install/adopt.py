"""Adoption: what an earlier install (or a half-finished one) left behind, found
by probing and taken over, never recreated (design §10, D24; review finding 18).

Some Google resources cannot be deleted, or keep their name long after they
are: a KMS key ring and its keys live for ever; a deleted custom role keeps its
id for 37 days and can be undeleted only in the first 7; a project has one
OAuth brand, which no API can read any more (Google shut the IAP OAuth Admin
API down on 2026-03-19), so it is never probed: the Desktop client the console
made under it is its proof (`steps/edge.py`); a deleted Firestore database's id is unavailable for a while; a
GitHub App's name is global. Each function here probes the live resource
**every time it is asked**: what remains is re-derived, never read from a cache
or from the install state (D24), because the state of a failed install is
exactly what cannot be trusted.

`InstallGcp` adds the REST calls stages 3 to 15 make to stage 0's `Gcp`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .events import Events
from .steps.preflight import IAM, ApiError, Gcp
from .waits import Clock, WaitSpec, wait_until

KMS = "https://cloudkms.googleapis.com/v1"
FIRESTORE = "https://firestore.googleapis.com/v1"
MONITORING = "https://monitoring.googleapis.com/v3"

#: A deleted custom role can be undeleted for 7 days (then its id is held, unusable, for 30 more).
ROLE_UNDELETE_DAYS = 7

#: After a database is deleted Firestore keeps its id for a few minutes.
FIRESTORE_ID = WaitSpec(
    reason="Firestore releasing the id of a database deleted moments ago",
    expected="up to 5 min",
    expected_s=5 * 60,
    bound_s=15 * 60,
    look_at="gcloud firestore databases list: a database in DELETING keeps its id until it is gone",
    interval_s=15,
)


class InstallGcp(Gcp):
    """Stage 0's REST client plus the calls stages 3 to 15 make."""

    def key_ring(self, project: str, location: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{KMS}/projects/{project}/locations/{location}/keyRings/{name}", missing_ok=True)

    def crypto_key(self, ring_id: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{KMS}/{ring_id}/cryptoKeys/{name}", missing_ok=True)

    def custom_roles(self, project: str) -> list[dict[str, Any]]:
        return self._pages(f"{IAM}/projects/{project}/roles", "roles", {"showDeleted": "true", "view": "BASIC"})

    def undelete_role(self, name: str) -> None:
        self._call("POST", f"{IAM}/{name}:undelete", body={})

    def firestore_database(self, project: str, database: str) -> dict[str, Any] | None:
        return self._call("GET", f"{FIRESTORE}/projects/{project}/databases/{database}", missing_ok=True)

    def sa_permissions(self, email: str, permissions: Sequence[str]) -> set[str] | None:
        """The caller's permissions on a service account; `None` if it does not exist."""
        found = self._call("POST", f"{IAM}/projects/-/serviceAccounts/{email}:testIamPermissions",
                           body={"permissions": list(permissions)}, missing_ok=True)
        return None if found is None else set(found.get("permissions", []))

    def lint_condition(self, resource: str, expression: str) -> list[str]:
        found = self._call("POST", f"{IAM}/iamPolicies:lintPolicy",
                           body={"fullResourceName": resource, "condition": {"expression": expression,
                                                                             "title": "swarm-install lint"}})
        return [str(r.get("debugMessage") or r.get("validationUnitName"))
                for r in (found or {}).get("lintResults", []) if r.get("severity") in ("ERROR", "NOTICE_ERROR")]

    def metric_descriptors(self, project: str, prefix: str) -> list[str]:
        found = self._pages(f"{MONITORING}/projects/{project}/metricDescriptors", "metricDescriptors",
                            {"filter": f'metric.type = starts_with("{prefix}")'})
        return [str(d.get("type")) for d in found]

    def alert_policies(self, project: str) -> list[dict[str, Any]]:
        return self._pages(f"{MONITORING}/projects/{project}/alertPolicies", "alertPolicies")

    def notification_channels(self, project: str) -> list[dict[str, Any]]:
        return self._pages(f"{MONITORING}/projects/{project}/notificationChannels", "notificationChannels")

    def http_status(self, url: str) -> int:
        """The status an anonymous GET of `url` answers, redirects not followed."""
        import requests

        return int(requests.get(url, timeout=30, allow_redirects=False).status_code)


# -- KMS: imported, never recreated ----------------------------------------------


def kms_imports(gcp: Any, project: str, region: str, environment: str, in_state: Iterable[str]) -> list[tuple[str, str]]:
    """(address, id) for every step-spec ring and key that exists in the
    project and not in the bootstrap state: `terraform import` them, because a
    plan that creates them fails on the name and they can never be deleted."""
    ring_name = f"swarm-{environment}-specs"
    ring_id = f"projects/{project}/locations/{region}/keyRings/{ring_name}"
    ring_address = f'google_kms_key_ring.step_spec["{environment}"]'
    key_address = f'google_kms_crypto_key.step_spec["{environment}"]'
    known = set(in_state)
    found: list[tuple[str, str]] = []
    if gcp.key_ring(project, region, ring_name) is None:
        return found
    if ring_address not in known:
        found.append((ring_address, ring_id))
    if key_address not in known and gcp.crypto_key(ring_id, "step-spec") is not None:
        found.append((key_address, f"{ring_id}/cryptoKeys/step-spec"))
    return found


# -- custom roles: undeleted inside 7 days ------------------------------------------


@dataclass(frozen=True)
class RoleFinding:
    undeleted: tuple[str, ...]
    blocked: tuple[str, ...]


def undelete_roles(gcp: Any, project: str, wanted: Iterable[str]) -> RoleFinding:
    """Undelete every wanted custom role that is deleted. IAM reports no
    deletion time, so the undelete itself is the test: inside the 7 days it
    succeeds; past them IAM refuses it (FAILED_PRECONDITION) and the role is
    named (its id is held for 37 days: a decision for the person, since a role
    cannot be recreated under it)."""
    wanted_ids = set(wanted)
    undeleted, blocked = [], []
    for role in gcp.custom_roles(project):
        role_id = str(role.get("name", "")).rsplit("/", 1)[-1]
        if role_id not in wanted_ids or not role.get("deleted"):
            continue
        try:
            gcp.undelete_role(str(role["name"]))
        except ApiError as exc:
            if exc.status not in (400, 409):
                raise
            blocked.append(role_id)
            continue
        undeleted.append(role_id)
    return RoleFinding(tuple(sorted(undeleted)), tuple(sorted(blocked)))


# -- the Desktop client (and, through it, the OAuth brand) -------------------------

#: A Desktop OAuth client's id, as `steps/edge.py` checks it.
DESKTOP_CLIENT_ID = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")


def desktop_client(recorded: Mapping[str, Any] | None) -> str | None:
    """The Desktop client id an earlier install recorded (the remains record's
    `desktop_client_id`), adopted as the default; None when there is none.

    No API lists OAuth clients or brands any more (the IAP OAuth Admin API was
    shut down on 2026-03-19), so the record is the only source; the client's
    existence is shown by verify signing in with it. A recorded client also
    proves the project's OAuth brand exists: the console refuses to create a
    client before Branding and Audience are set."""
    client = str((recorded or {}).get("desktop_client_id") or "").strip()
    return client if DESKTOP_CLIENT_ID.match(client) else None


# -- the GitHub App's name ---------------------------------------------------------


def github_app_name(base: str, recorded: Mapping[str, Any] | None, exists: Callable[[str], bool]) -> tuple[str, bool]:
    """(name, reuse): the App the console recorded is reused; else the first of
    `base`, `base-2`, `base-3`... that no App holds (App names are global)."""
    if recorded and recorded.get("slug") and exists(str(recorded["slug"])):
        return str(recorded["slug"]), True
    for n in range(1, 50):
        name = base if n == 1 else f"{base}-{n}"
        if not exists(name):
            return name, False
    raise LookupError(f"every name from {base} to {base}-49 is taken; choose another base name")


# -- Firestore: the id waited for, with a countdown --------------------------------


def wait_firestore_id(gcp: Any, project: str, database: str, *, clock: Clock, events: Events) -> None:
    """Wait (bounded, with a countdown) while a deleted database still holds the id."""

    def status() -> str:
        found = gcp.firestore_database(project, database)
        if found is None or found.get("deleteTime") is None and found.get("state") != "DELETING":
            return "met"
        return "waiting"

    wait_until(f"firestore {database}", FIRESTORE_ID, status, clock=clock, events=events)


# -- what remains, re-derived ---------------------------------------------------------


def remains(probes: Mapping[str, Callable[[], bool]]) -> list[str]:
    """Every resource whose probe says it is there now. Each probe runs on every
    call: nothing is cached, and nothing is read from the install state (D24)."""
    return sorted(name for name, probe in probes.items() if probe())
