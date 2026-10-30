"""What an uninstall leaves behind, and why (docs/design/day-one-install.md §10;
docs/design/day-one-plan.md §9.2, lane DAYONE-UNINSTALL-1, part of #2688).

Some names outlive `swarm uninstall` whatever it does. Design §10 lists eight
and each is here, with the reason it survives and the exact step a person
takes if they want it gone:

| Kind | Why it survives |
|---|---|
| `oauth-brand` | the OAuth brand (consent screen) has no delete method; it goes with the project |
| `kms-key-ring` | key rings and keys cannot be deleted; their versions are destroyed |
| `custom-roles` | a deleted custom role's id is reserved (undeletable for 7 days) |
| `firestore-database` | a deleted database's id is reusable only after Google's delay |
| `desktop-client` | deleting the Desktop OAuth client is a console step; it is kept for reuse |
| `github-app` | only the organisation can delete its App |
| `dns-record` | the record is at the company's registrar, or in the project's Cloud DNS zone `edge.dns` wrote it to |
| `workspace-groups` | the groups, the Groups Reader role assignment or the delegation are org-admin objects |

The record also carries `left`: anything else the uninstall did not remove (a
resource in Terraform state that carries no `managed-by` label, so it is not
ours to delete), each with why.

The record is written to the local cache, `~/.config/swarmcloud/install/
<project>.remains.json`, and printed, before the state bucket is deleted. A
reinstall never depends on it: `adopt.py` re-derives every surviving name by
probing (D24) and uses the record's values only as defaults for what cannot be
probed (the Desktop client id, the GitHub App's slug).

**Retention windows.** This module does not settle them; it cites Google's
pages, and run 2b (P5) is the live proof. The engine never relies on either
window: it imports a ring if it exists and undeletes roles if it can.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .state import LOCAL_DIR, _write_local

KMS_DELETE_DOCS = "https://cloud.google.com/kms/docs/delete-kms-resources"
CUSTOM_ROLE_DOCS = "https://cloud.google.com/iam/docs/creating-custom-roles#deleting-custom-role"
FIRESTORE_DOCS = "https://cloud.google.com/firestore/docs/manage-databases#delete_a_database"

#: Design §10's eight rows, in its order.
KINDS = (
    "oauth-brand",
    "kms-key-ring",
    "custom-roles",
    "firestore-database",
    "desktop-client",
    "github-app",
    "dns-record",
    "workspace-groups",
)

#: infra's Firestore database when its inputs name none (terraform/infra/variables.tf).
DEFAULT_FIRESTORE_DATABASE = "swarm"


@dataclass(frozen=True)
class Remain:
    """One thing the uninstall did not remove: what it is, why, and what to do."""

    kind: str
    name: str
    why: str
    step: str

    def line(self) -> str:
        return f"{self.kind}: {self.name}\n    why: {self.why}\n    to remove it: {self.step}"


def _text(value: Any, missing: str = "(not recorded)") -> str:
    return str(value) if value not in (None, "", [], {}) else missing


def survivors(state: Any, found: Mapping[str, Any] | None = None) -> list[Remain]:
    """The eight survivors of design §10, named from the install state.

    `found` holds what the uninstall read from Terraform before destroying it:
    `custom_roles` (role ids) and `kms_key_ring` (the ring's id), and
    `firestore_database` (the database id infra's inputs named).
    """
    outputs: Mapping[str, Any] = state.outputs
    found = found or {}
    project = state.project
    region = _text(state.region, "<region>")
    ring = found.get("kms_key_ring") or (
        f"projects/{project}/locations/{region}/keyRings/swarm-{state.environment}-specs")
    roles = list(found.get("custom_roles") or ())
    database = found.get("firestore_database") or DEFAULT_FIRESTORE_DATABASE
    app = outputs.get("github_app") or {}
    org = outputs.get("github_org") or "<org>"
    slug = app.get("slug") or "<slug>"
    groups = list(outputs.get("admin_groups") or ())
    delegation = str(outputs.get("groups_auth") or "") == "delegation"
    host = _text(state.hostname, "the console hostname")
    zone = str(outputs.get("dns_zone") or "")  # edge.dns wrote the record to the project's Cloud DNS zone
    dns_record = (
        (f"edge.dns wrote it to the project's Cloud DNS zone {zone}, whose records the uninstall leaves",
         f"https://console.cloud.google.com/net-services/dns/zones/{zone}/details?project={project}: "
         f"delete the A record for {host}")
        if zone else
        ("the record is at the company's DNS registrar, which the installer cannot reach",
         f"at your registrar, delete the A record for {host}"))
    return [
        # No API reads a brand (the IAP OAuth Admin API was shut down on
        # 2026-03-19): it is named by its console page, never probed.
        Remain("oauth-brand", _text(outputs.get("oauth_brand"),
                                    f"https://console.cloud.google.com/auth/branding?project={project}"),
               "an OAuth brand (consent screen) cannot be deleted; it has no delete method",
               "nothing to do: it goes with the project (a reinstall reuses it)"),
        Remain("kms-key-ring", ring,
               f"KMS key rings and keys are not deleted here; their key versions were destroyed ({KMS_DELETE_DOCS})",
               "nothing to do: it holds no usable key version (a reinstall imports it and adds a new version)"),
        Remain("custom-roles", ", ".join(roles) or "(none recorded in bootstrap's state)",
               f"a deleted custom role's id stays reserved and can be undeleted for 7 days ({CUSTOM_ROLE_DOCS})",
               "nothing to do: a reinstall inside 7 days undeletes them, later it re-creates them"),
        Remain("firestore-database", f"projects/{project}/databases/{database}",
               f"a deleted Firestore database's id is reusable only after Google's delay ({FIRESTORE_DOCS})",
               "nothing to do: a reinstall waits for the id with a countdown"),
        Remain("desktop-client", _text(outputs.get("desktop_client_id")),
               "the Desktop OAuth client is deleted only in the console, and is kept for a reinstall to reuse",
               f"https://console.cloud.google.com/auth/clients?project={project}: delete the client with this id"),
        Remain("github-app", f"{org}/{slug}",
               "only the GitHub organisation can delete its App",
               f"https://github.com/organizations/{org}/settings/apps/{slug}/advanced: Delete GitHub App"),
        Remain("dns-record", f"{_text(state.hostname, '<hostname>')} A {_text(outputs.get('global_ip'))}",
               *dns_record),
        Remain("workspace-groups", ", ".join(groups) or "(none recorded)",
               "Google groups, the Groups Reader admin role assignment and domain-wide delegation are "
               "organisation-admin objects outside the project",
               "https://admin.google.com/ac/groups: delete the groups; "
               + ("https://admin.google.com/ac/owl/domainwidedelegation: remove swarm-api's client id"
                  if delegation else
                  "https://admin.google.com/ac/roles: remove the Groups Reader assignment of swarm-api")),
    ]


@dataclass
class Record:
    """The "what remains" record (manifest `other.install-state`, design §10)."""

    project: str
    environment: str
    written_at: float
    remains: list[Remain]
    left: list[Remain] = field(default_factory=list)

    def to_bytes(self) -> bytes:
        body = {
            "project": self.project,
            "environment": self.environment,
            "written_at": self.written_at,
            "remains": [asdict(r) for r in self.remains],
            "left": [asdict(r) for r in self.left],
        }
        return json.dumps(body, indent=1, sort_keys=True).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> Record:
        raw = json.loads(data)
        return cls(project=raw["project"], environment=raw["environment"], written_at=float(raw["written_at"]),
                   remains=[Remain(**r) for r in raw.get("remains", [])],
                   left=[Remain(**r) for r in raw.get("left", [])])

    def render(self) -> str:
        lines = [f"What remains of the SwarmCloud install in {self.project}:"]
        lines.extend(r.line() for r in self.remains)
        if self.left:
            lines.append("Also not removed:")
            lines.extend(r.line() for r in self.left)
        return "\n".join(lines)


def record_path(project: str, base: Path | None = None) -> Path:
    return (base if base is not None else LOCAL_DIR.expanduser()) / f"{project}.remains.json"


def write(record: Record, base: Path | None = None) -> Path:
    """Write the record to the local cache, atomically; its path."""
    path = record_path(record.project, base)
    _write_local(path, record.to_bytes())
    return path


def left_from(items: Sequence[Mapping[str, str]]) -> list[Remain]:
    """`left` entries as the install state keeps them (plain dicts)."""
    return [Remain(**dict(item)) for item in items]
