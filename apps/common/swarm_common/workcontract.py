"""The work contract's signed form: one definition for swarm-api and the worker.

Contract request 136 (AQ-WORK-CONTRACT, the agent quality program, part of
#2688), accepted by the owner. swarm-api compiles a versioned work contract
into a step and writes a reference to it -- its id, revision and digest --
inside the signed dispatch block (`metadata.dispatch.work_contract`). A
worker that enforces a contract fetches the stored body and checks it
against that reference before it acts on any of it. Both must compute the
same digest from the same projection, so the purpose, the schema version,
the canonical projection (`WorkContract.to_document`) and `contract_digest`
live here, moved from `swarm_api.workcontract` unchanged in bytes;
swarm-api imports them and keeps the schema validation and the amendment
protocol. Two copies would be two opinions about what is signed (the reason
`specsign.canonical_step_spec` is in this package too).

Pure: no Firestore, no network, no clock.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping

from .specsign import jcs

#: Domain separation: a contract's bytes can never be read as a step spec's
#: (`swarm.step-spec`) or a settings block's (`swarm.settings/v1`).
CONTRACT_PURPOSE = "swarm.work-contract/v1"
SCHEMA_VERSION = 1

#: The step limits a contract references, in the projection's order. Each
#: compiles into the step field of the same name, which the step-spec
#: signature already covers.
LIMIT_KEYS = ("runner_profile", "model_class", "budget", "max_turns", "timeout_seconds")


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    verification: tuple[str, ...]


@dataclass(frozen=True)
class WorkContract:
    """A decoded, canonical work contract. Immutable: an amendment is a new one."""

    contract_id: str
    tenant_id: str
    revision: int
    previous_digest: str | None
    strictness: str
    requirements: tuple[Requirement, ...]
    repositories: tuple[Mapping[str, Any], ...]
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    protected_paths: tuple[str, ...]
    protected_interfaces: tuple[str, ...]
    runtime_capabilities: tuple[str, ...]
    plugins: Mapping[str, Mapping[str, Any]] = field(hash=False)
    limits: Mapping[str, Any] = field(hash=False)
    artifacts: tuple[Mapping[str, Any], ...]
    owner: str
    depends_on: tuple[str, ...]
    withdrawn: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        """The canonical document: what `contract_digest` covers and what is stored."""
        return {
            "schema": SCHEMA_VERSION,
            "contract_id": self.contract_id,
            "tenant_id": self.tenant_id,
            "revision": self.revision,
            "previous_digest": self.previous_digest,
            "strictness": self.strictness,
            "requirements": [
                {"id": r.id, "text": r.text, "verification": list(r.verification)}
                for r in self.requirements
            ],
            "repositories": [dict(r) for r in self.repositories],
            "scope": {"read": list(self.read_scope), "write": list(self.write_scope)},
            "protected": {
                "paths": list(self.protected_paths),
                "interfaces": list(self.protected_interfaces),
            },
            "capabilities": {
                "runtime": list(self.runtime_capabilities),
                "plugins": {
                    name: {"mode": p["mode"], "skills": list(p["skills"])}
                    for name, p in sorted(self.plugins.items())
                },
            },
            "limits": {k: self.limits.get(k) for k in LIMIT_KEYS},
            "artifacts": [dict(a) for a in self.artifacts],
            "ownership": {
                "owner": self.owner,
                "depends_on": list(self.depends_on),
                "withdrawn": list(self.withdrawn),
            },
        }


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    """RFC 8785 (JCS) of the purpose and a canonical document
    (`WorkContract.to_document`, or a stored body), through `specsign.jcs`:
    one encoder for every signed thing. Raises `specsign.SpecNotCanonical`
    for a value JCS cannot encode."""
    return jcs({"purpose": CONTRACT_PURPOSE, "contract": document})


def contract_digest(document: Mapping[str, Any]) -> str:
    """SHA-256 of `canonical_bytes(document)`, hex: the `digest` of the signed
    reference. Names exactly one revision's content."""
    return hashlib.sha256(canonical_bytes(document)).hexdigest()
