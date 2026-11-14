"""The canonical form of a step's spec, signed by swarm-api and verified by the worker.

Contract request 34 (#342). A tenant's agents can write any task document of
their tenant (docs/multi-tenancy.md, "The Firestore row, in full"), so a
worker must not run a spec swarm-api did not sign. This module is the ONE
definition of which fields that signature covers and how they become bytes:
swarm-api signs `spec_digest(canonical_step_spec(task.to_firestore(),
task_id=task.id))`, and the worker verifies the same digest over the document
it fetched. Two copies of this projection would be two opinions about what
is signed. No cryptography lives here: signing is swarm-api's (Cloud KMS) and
verification is the worker's.

The encoding is RFC 8785 (JCS), implemented for exactly the values a
Firestore document written from JSON can hold, with no dependency: this
package is installed from a git tag by the plugin and declares none.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

#: The canonical form's version. A change to the covered fields or the encoding
#: is a NEW format, never an edit to this one: a signature is over a format.
#:
#: 2 is contract request 42, applied 2026-10-02 with request 14: format 1's
#: fields plus `parent_task_id` and `parent_attempt_id`, so a child whose
#: parent was rewritten fails its own worker's check. swarm-api signs at
#: SPEC_FORMAT; a worker verifies every format in SPEC_FORMATS, each under its
#: own projection, so a document signed at format 1 keeps verifying.
#:
#: 3 is contract request 54 (request E of docs/onboarding.md §3.3), accepted by
#: the owner 2026-10-07: format 2's fields plus `forge_credential` and
#: `forge_access`, so a task whose forge secret or access mode was rewritten
#: fails its worker's check.
#:
#: 4 is contract request 96 (MULTI-REPO request B,
#: docs/design/multi-repository-steps.md §5), accepted by the owner 2026-10-17
#: on PR 1461: format 3's fields plus `additional_repositories`, so a task
#: whose list was rewritten -- a commit repointed, a repository added that the
#: step was never given, a slot swapped -- fails its worker's check.
#:
#: 5 is contract request 104 (request E of docs/design/model-per-step.md §5,
#: #1435), accepted by the owner 2026-10-17 on PR 1509: format 4's fields plus
#: `model_class`, so a task whose model class was rewritten fails its worker's
#: check. The design named it format 4; request 96 had taken 4 first.
#:
#: 6 is contract request 120 (BUDGET-CAP, #2459), accepted by the owner
#: 2026-10-26: format 5's fields plus `budget`, so a task whose resolved
#: budget was raised or removed fails its worker's check.
#:
#: 7 is contract request 133 (CONTRACT-MAXTURNS, part of #2524), accepted by
#: the owner 2 November 2026: format 6's fields plus `max_turns`, so a task
#: whose resolved turn cap was raised or removed fails its worker's check.
#:
#: 8 is contract request 137 (part of #2688), accepted by the owner 4 November
#: 2026 ("Sign the key"): format 7's fields plus `metadata.work_contract`, the
#: stored work contract document whose digest the signed dispatch block names,
#: so a task whose contract was rewritten fails its worker's check.
SPEC_FORMAT = 8

#: Every format a verifier accepts, oldest first.
SPEC_FORMATS = (1, 2, 3, 4, 5, 6, 7, 8)

#: The fields format 2 adds to format 1's projection.
_FORMAT_2_FIELDS = ("parent_task_id", "parent_attempt_id")

#: The fields format 3 adds to format 2's projection. Formats 1 and 2 do not
#: cover them, so a document at either format that carries one is not
#: canonical (see `canonical_step_spec`): adding a forge credential to a task
#: signed without one must fail verification, not slip outside the bytes.
_FORMAT_3_FIELDS = ("forge_credential", "forge_access")

#: The fields format 4 adds to format 3's projection. Formats 1-3 do not cover
#: them, so a document at any of those that SETS one (non-None and non-empty)
#: is not canonical, exactly as a format-3 field below format 3.
_FORMAT_4_FIELDS = ("additional_repositories",)

#: The fields format 5 adds to format 4's projection (contract request 104).
#: Formats 1-4 do not cover them, so a document at any of those that sets one
#: (non-None) is not canonical: the worker acts on the class, so adding one to
#: a task signed without it must fail verification.
_FORMAT_5_FIELDS = ("model_class",)

#: The fields format 6 adds to format 5's projection (contract request 120).
#: Formats 1-5 do not cover them, so a document at any of those that sets one
#: (non-None) is not canonical: the worker enforces the budget, so adding one
#: to a task signed without it must fail verification.
_FORMAT_6_FIELDS = ("budget",)

#: The fields format 7 adds to format 6's projection (contract request 133).
#: Formats 1-6 do not cover them, so a document at any of those that sets one
#: (non-None) is not canonical: the worker passes the cap to the agent CLI, so
#: adding one to a task signed without it must fail verification.
_FORMAT_7_FIELDS = ("max_turns",)

#: The METADATA keys format 8 adds to the projection's `metadata` map
#: (contract request 137). Formats 1-7 do not cover them: their `metadata` map
#: keeps exactly the keys it had, so every stored signature still verifies,
#: and a document at any of those that sets one (non-None) is not canonical:
#: the worker reads the plugins it loads from the contract, so adding one to a
#: task signed without it must fail verification.
_FORMAT_8_METADATA_KEYS = ("work_contract",)

#: The top-level fields every writer stores as integers (`Task`'s types).
#: JCS gives 3600 and 3600.0 the same bytes, so without this a rewrite of one
#: to the other keeps the signature valid. Checked at every format: it removes
#: a canonical form from a value no signer writes, and changes the bytes of
#: none that has one, so it is not a new format (contract request 58).
_INTEGER_FIELDS = ("timeout_seconds", "max_attempts", "max_turns")


def signing_format(doc: Mapping[str, Any]) -> int:
    """The format a signer signs `doc` at: the oldest one that covers every field it sets.

    1 when it names no parent and no forge credential, 2 when it names a parent
    and no forge credential, 3 when it has a forge credential or access mode,
    and 4 when it reads additional repositories (a non-empty list). Format 2's projection of a task whose
    parent fields are both None differs from format 1's only by carrying two
    nulls, so signing such a task at format 1 covers exactly as much -- and
    keeps it verifiable by a worker built before format 2, which knows only
    format 1 and would refuse every task (SPEC_SIGNATURE_INVALID) during a
    rollout that updated swarm-api first. The same holds one format up: only a
    task swarm-api resolved a forge credential or access mode for is signed at
    format 3, and a task with neither keeps the bytes it had before format 3.

    Only a child is signed at format 2; a child needs a worker that has the
    child path anyway. A rewrite of a format-1 document that adds a parent
    STILL VERIFIES: the parent fields are then outside the signed bytes, so
    the signature vouches for no parent at all, and the readers that act on
    one (the cascade, the await) are platform components that do not consult
    the signature (deferred to #476). A rewrite that adds a forge field does
    NOT: the worker acts on those itself, so `canonical_step_spec` refuses
    them at a format that does not cover them.

    Format 4 follows the same rule (contract request 96): only a task that
    reads additional repositories is signed at it, so a worker built before
    format 4 keeps verifying every task that does not. Rollout order: workers
    first, then swarm-api, as for formats 2 and 3. An empty list is unset: it
    asks the worker for nothing, and an older format's bytes do not see it.

    Format 5 too (contract request 104): only a task that names a model class
    is signed at it, and swarm-api stores a class only when it differs from
    the profile's default, so a default step keeps the format and the bytes
    it had before format 5.

    Format 6 too (contract request 120): only a task with a budget is signed
    at it, and swarm-api stores one only when the step or its workspace sets
    one, so a workspace that sets nothing keeps the format and the bytes it
    had before format 6.

    Format 7 too (contract request 133): only a task with a turn cap is
    signed at it, and swarm-api stores one only when the step or its profile
    sets one, so a task without a cap keeps the format and the bytes it had
    before format 7.

    Format 8 too (contract request 137): only a task whose metadata carries a
    work contract is signed at it, so a task without one keeps the format and
    the bytes it had before format 8.
    """
    metadata = doc.get("metadata")
    if isinstance(metadata, Mapping) and any(
        metadata.get(key) is not None for key in _FORMAT_8_METADATA_KEYS
    ):
        return 8
    if any(doc.get(key) is not None for key in _FORMAT_7_FIELDS):
        return 7
    if any(doc.get(key) is not None for key in _FORMAT_6_FIELDS):
        return 6
    if any(doc.get(key) is not None for key in _FORMAT_5_FIELDS):
        return 5
    if any(_is_set(doc.get(key)) for key in _FORMAT_4_FIELDS):
        return 4
    if any(doc.get(key) is not None for key in _FORMAT_3_FIELDS):
        return 3
    if all(doc.get(key) is None for key in _FORMAT_2_FIELDS):
        return 1
    return 2


def _is_set(value: Any) -> bool:
    """A format-4 field is set when it is present, not None and not an empty list."""
    return value is not None and value != []


#: Domain separation. The bytes of a step spec cannot be read as another message.
SPEC_PURPOSE = "swarm.step-spec"

#: The keys of `Task.metadata` that swarm-api writes and the worker acts on.
#: Every other metadata key is the caller's label or another writer's record.
#: `work_contract` is contract request 137, covered from format 8 only
#: (`_FORMAT_8_METADATA_KEYS`).
SIGNED_METADATA_KEYS = ("dispatch", "input_from", "expected_outputs", "work_contract")

#: JCS numbers are IEEE doubles, so an integer is exact only within this bound.
MAX_SAFE_INTEGER = 2**53 - 1

#: How deep `_encode` follows lists and maps. Firestore refuses a document
#: nested deeper than 20 levels and the spec puts a stored field one level
#: down, so 32 refuses nothing Firestore can hold, and stays far inside
#: Python's recursion limit -- which `_encode` reached as `RecursionError`,
#: not `SpecNotCanonical`, before this bound (contract request 59).
MAX_DEPTH = 32


class SpecNotCanonical(ValueError):
    """A value in the spec has no canonical form (see `jcs`)."""


def canonical_step_spec(
    doc: Mapping[str, Any], *, task_id: str, spec_format: int = SPEC_FORMAT
) -> bytes:
    """The bytes the signature covers, from a task document as stored.

    `task_id` is the id the document was READ BY (the worker's TASK_ID, the
    id swarm-api minted), never the document's own `id` field. `spec_format`
    picks the projection: the signer leaves it at SPEC_FORMAT, a verifier
    passes the document's own `spec_format` (which is inside the signed bytes,
    so a rewrite to another format fails).

    A document that sets `forge_credential` or `forge_access` has no
    canonical form at formats 1 and 2, which do not cover them: absent (or
    None) they leave those formats' bytes exactly as they were, present they
    are refused rather than left outside the signature. The same holds for a
    set `additional_repositories` at formats 1-3; at format 4 the list is
    projected as stored, in submission order, and the order is in the bytes.
    And for a set `model_class` at formats 1-4 (contract request 104), and a
    set `budget` at formats 1-5 (contract request 120), and a set `max_turns`
    at formats 1-6 (contract request 133), and a set `metadata.work_contract`
    at formats 1-7 (contract request 137), whose `metadata` map holds only
    the keys it held before format 8.
    """
    if spec_format not in SPEC_FORMATS or isinstance(spec_format, bool):
        raise SpecNotCanonical(f"spec format {spec_format!r} is not one of {SPEC_FORMATS}")
    metadata = doc.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise SpecNotCanonical("metadata is not a map")
    if spec_format < 3:
        for key in _FORMAT_3_FIELDS:
            if doc.get(key) is not None:
                raise SpecNotCanonical(f"{key} is set, and format {spec_format} does not cover it")
    if spec_format < 4:
        for key in _FORMAT_4_FIELDS:
            if _is_set(doc.get(key)):
                raise SpecNotCanonical(f"{key} is set, and format {spec_format} does not cover it")
    if spec_format < 5:
        for key in _FORMAT_5_FIELDS:
            if doc.get(key) is not None:
                raise SpecNotCanonical(f"{key} is set, and format {spec_format} does not cover it")
    if spec_format < 6:
        for key in _FORMAT_6_FIELDS:
            if doc.get(key) is not None:
                raise SpecNotCanonical(f"{key} is set, and format {spec_format} does not cover it")
    if spec_format < 7:
        for key in _FORMAT_7_FIELDS:
            if doc.get(key) is not None:
                raise SpecNotCanonical(f"{key} is set, and format {spec_format} does not cover it")
    if spec_format < 8:
        for key in _FORMAT_8_METADATA_KEYS:
            if metadata.get(key) is not None:
                raise SpecNotCanonical(
                    f"metadata.{key} is set, and format {spec_format} does not cover it")
        metadata_keys = tuple(k for k in SIGNED_METADATA_KEYS if k not in _FORMAT_8_METADATA_KEYS)
    else:
        metadata_keys = SIGNED_METADATA_KEYS
    for key in _INTEGER_FIELDS:
        value = doc.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise SpecNotCanonical(f"{key} is a {type(value).__name__}, not an integer")
    spec = {
        "purpose": SPEC_PURPOSE,
        "format": spec_format,
        "task_id": task_id,
        "tenant_id": doc.get("tenant_id"),
        "workflow_id": doc.get("workflow_id"),
        "step_id": doc.get("step_id"),
        "submitted_by": doc.get("submitted_by"),
        "runner_profile": doc.get("runner_profile"),
        "resource_class": doc.get("resource_class"),
        "timeout_seconds": doc.get("timeout_seconds"),
        "max_attempts": doc.get("max_attempts"),
        "provider": doc.get("provider"),
        "model": doc.get("model"),
        "input": doc.get("input"),
        "depends_on": doc.get("depends_on"),
        "repository_url": doc.get("repository_url"),
        "repository_ref": doc.get("repository_ref"),
        "metadata": {key: metadata.get(key) for key in metadata_keys},
    }
    if spec_format >= 2:
        for key in _FORMAT_2_FIELDS:
            spec[key] = doc.get(key)
    if spec_format >= 3:
        for key in _FORMAT_3_FIELDS:
            spec[key] = doc.get(key)
    if spec_format >= 4:
        for key in _FORMAT_4_FIELDS:
            spec[key] = doc.get(key)
    if spec_format >= 5:
        for key in _FORMAT_5_FIELDS:
            spec[key] = doc.get(key)
    if spec_format >= 6:
        for key in _FORMAT_6_FIELDS:
            spec[key] = doc.get(key)
    if spec_format >= 7:
        for key in _FORMAT_7_FIELDS:
            spec[key] = doc.get(key)
    return jcs(spec)


#: Domain separation for a `workspace_settings` block (contract request 86).
#: Its bytes can never be read as a step spec's, or a spec's as a block's.
SETTINGS_PURPOSE = "swarm.settings/v1"

#: The fields of each `workspace_settings` block that its writer, swarm-api,
#: MACs and that the control plane acts on (contract request 86,
#: docs/design/tenant-settings-integrity.md §5 request B, #1457, accepted by the
#: owner 2026-10-17 on PR 1464). "top" is the document's own top level; "merge"
#: and "code_intelligence" are its maps of those names. A reader acts on a
#: field only when it is listed here AND the block's MAC verifies; a field
#: added to a block without being added here is outside the MAC, so a tenant
#: worker can forge it. A change to a list is a change to the bytes: every
#: stored MAC of that block must be re-signed in the same rollout.
#:
#: "budget" is contract request 119 (BUDGET-CAP, #2459), accepted by the owner
#: 2026-10-26: the workspace's per-agent budget default, `{unit, amount}`.
#:
#: "quality_gates" is contract request 128 (JEV-LANES, PR 2905), accepted by
#: the owner 2026-10-31: whether the workspace's claude-code lanes run
#: jev-belay and jev-enforce, `{jev}`.
#:
#: "plugins" is contract request 136 (AQ-WORK-CONTRACT, part of #2688),
#: accepted by the owner 2026-11-03: the admin-only list of catalogue plugins
#: the workspace's steps may name, `{allowed}` (docs/design/agent-plugins.md,
#: "A closed plugin catalogue"). A new block: no other block's bytes change,
#: and no workspace stores one yet, so nothing needs re-signing.
#:
#: "contract_review" is contract request 138 (JEV-SUBMISSION-REVIEW, part of
#: #2688), accepted by the owner 2026-11-04: the workspace's opt-in to
#: swarm-api's Jev review of its work contracts at submission, `{enabled}`
#: (docs/design/agent-quality/jev-contract.md section 4.1). swarm-api signed
#: these same bytes before the block was listed here, so every stored MAC
#: still verifies and nothing needs re-signing.
#:
#: "plugins" gains "defaults" and "configs_digest" by contract request 140
#: (managed plugins P1, docs/design/managed-plugins.md section 14), accepted
#: by the owner 2026-11-06: the install ids on for every step of the
#: workspace and the digest of its plugin settings documents are signed with
#: the Allowed list. An absent key projects as null, and a null key the block
#: gained after its MACs were stored is left out of the bytes
#: (_SETTINGS_ADDED_FIELDS), so every block stored before it still verifies.
SETTINGS_MAC_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "top": ("judgment_backend",),
        "merge": ("mode", "train_size", "max_train_age_minutes"),
        "code_intelligence": (
            "jev",
            "context",
            "context_ab",
            "graph_delivery",
            "judgment_backend",
        ),
        "budget": ("unit", "amount"),
        "quality_gates": ("jev",),
        "plugins": ("allowed", "defaults", "configs_digest"),
        "contract_review": ("enabled",),
    }
)

#: The keys of SETTINGS_MAC_FIELDS a block gained after MACs over it were
#: stored. Null or absent, such a key is left out of the bytes rather than
#: projected as null, so a block stored before the key existed keeps its
#: bytes and its MAC. Set, it is covered like any other key: planting one on a
#: block signed without it, or removing or nulling one that was signed,
#: changes the bytes. Null and absent stay one value, as for every key.
_SETTINGS_ADDED_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"plugins": ("defaults", "configs_digest")}  # contract request 140
)


def canonical_settings_block(
    *, tenant_id: str, block: str, fields: Mapping[str, Any]
) -> bytes:
    """JCS of {"purpose": "swarm.settings/v1", "tenant_id", "block", "fields"}.

    `block` is a key of SETTINGS_MAC_FIELDS. `fields` is the block as
    stored; only the keys SETTINGS_MAC_FIELDS lists for it are projected, a
    listed key that is absent is null, and any other key is not covered and
    must not be acted on. A null key in _SETTINGS_ADDED_FIELDS is left out of
    the bytes (contract request 140), so they are the bytes stored before it. The tenant and the block are inside the bytes, so a
    block copied onto another tenant's document, or into another block, does
    not verify.

    The MAC is HMAC-SHA256 over these bytes under the `swarm-settings-key`
    secret. It is not computed here, because its key is not the contract's.
    """
    if not isinstance(tenant_id, str) or not tenant_id:
        raise SpecNotCanonical("a settings block names no tenant")
    if block not in SETTINGS_MAC_FIELDS:
        raise SpecNotCanonical(f"settings block {block!r} is not one of {tuple(SETTINGS_MAC_FIELDS)}")
    if not isinstance(fields, Mapping):
        raise SpecNotCanonical(f"settings block {block!r} is not a map")
    return jcs(
        {
            "purpose": SETTINGS_PURPOSE,
            "tenant_id": tenant_id,
            "block": block,
            "fields": {
                key: fields.get(key)
                for key in SETTINGS_MAC_FIELDS[block]
                if fields.get(key) is not None or key not in _SETTINGS_ADDED_FIELDS.get(block, ())
            },
        }
    )


#: Domain separation for a managed plugin's install, version and intake
#: documents (contract request 141, managed plugins P2,
#: docs/design/managed-plugins.md sections 1.1 and 14, accepted by the owner
#: 2026-11-06). swarm-api MACs these bytes under `swarm-settings-key` and acts
#: on a document only when its MAC verifies: a tenant identity can write any
#: Firestore document, and a forged version could swap the digest every
#: workspace's steps are signed with.
PLUGIN_RECORD_PURPOSE = "swarm.plugin-record/v1"

#: `plugin_installs/{install_id}`, `plugin_installs/{install_id}/versions/
#: {version_id}`, `plugin_intake/{intake_id}` and `plugin_bundles/{bundle_id}`
#: (contract request 153, managed plugins P6, design sections 1.4.3 and 14,
#: accepted by the owner 2026-11-09): a bundle is platform scope only, because
#: its image is built once per deployment and serves every workspace.
PLUGIN_RECORD_KINDS: tuple[str, ...] = ("install", "version", "intake", "bundle")

#: A record's scope: a platform install, or one workspace's own.
PLUGIN_SCOPES: tuple[str, ...] = ("platform", "workspace")

#: The ids each kind's `fields` must carry, each a non-empty string. A
#: document's id is not one of its stored fields, so its writer and reader
#: put it into `fields`: a version copied under another install, or an
#: intake's record onto another intake, then does not verify. An intake for
#: a new install names no `install_id`; one for an update does, covered.
PLUGIN_RECORD_IDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "install": ("install_id",),
        "version": ("install_id", "version_id"),
        "intake": ("intake_id",),
        "bundle": ("bundle_id",),
    }
)

#: The keys a plugin record's MAC does not cover: the MAC itself, and who
#: wrote the document and when, which no reader acts on. EVERY other key is
#: covered, one no writer wrote included, so nothing stored is outside the MAC
#: and a reader may act on any key of a record that verifies. A covered value
#: must have a JSON form: a Firestore timestamp outside these keys is refused.
PLUGIN_RECORD_UNCOVERED: tuple[str, ...] = (
    "mac", "created_at", "created_by", "updated_at", "updated_by",
)


def canonical_plugin_record(kind: str, fields: Mapping[str, Any]) -> bytes:
    """JCS of {"purpose": "swarm.plugin-record/v1", "kind", "scope", "tenant_id", "fields"}.

    `kind` is one of PLUGIN_RECORD_KINDS and `fields` the document as stored
    plus its ids (PLUGIN_RECORD_IDS). `scope` is "platform" or "workspace";
    a workspace record names its tenant and a platform record names none
    (absent or null), so the scope and the tenant are inside the bytes: a
    record copied onto another workspace, or lifted to platform scope, does
    not verify. A bundle record is platform scope only. `fields` projects
    every other key except PLUGIN_RECORD_UNCOVERED.

    The MAC is HMAC-SHA256 over these bytes under the `swarm-settings-key`
    secret. It is not computed here, because its key is not the contract's.
    """
    if kind not in PLUGIN_RECORD_KINDS:
        raise SpecNotCanonical(f"plugin record kind {kind!r} is not one of {PLUGIN_RECORD_KINDS}")
    if not isinstance(fields, Mapping):
        raise SpecNotCanonical(f"plugin {kind} record is not a map")
    scope = fields.get("scope")
    tenant_id = fields.get("tenant_id")
    if scope not in PLUGIN_SCOPES:
        raise SpecNotCanonical(f"plugin {kind} record's scope is not one of {PLUGIN_SCOPES}")
    if scope == "workspace" and not (isinstance(tenant_id, str) and tenant_id):
        raise SpecNotCanonical(f"workspace plugin {kind} record names no tenant")
    if scope == "platform" and tenant_id is not None:
        raise SpecNotCanonical(f"platform plugin {kind} record names a tenant")
    if kind == "bundle" and scope != "platform":
        raise SpecNotCanonical("a plugin bundle record is platform scope only")
    for name in PLUGIN_RECORD_IDS[kind]:
        value = fields.get(name)
        if not (isinstance(value, str) and value):
            raise SpecNotCanonical(f"plugin {kind} record has no {name}")
    return jcs(
        {
            "purpose": PLUGIN_RECORD_PURPOSE,
            "kind": kind,
            "scope": scope,
            "tenant_id": tenant_id,
            "fields": {
                key: value
                for key, value in fields.items()
                if key not in PLUGIN_RECORD_UNCOVERED and key not in ("scope", "tenant_id")
            },
        }
    )


#: Domain separation for the platform switches document,
#: `platform_settings/switches` (contract request 144, managed plugins P5,
#: docs/design/managed-plugins.md sections 11 and 14, accepted by the owner
#: 2026-11-06). It names no tenant: there is one platform.
PLATFORM_SETTINGS_PURPOSE = "swarm.platform-settings/v1"

#: The switches swarm-api MACs and acts on: `work_contracts` (`on`/`off`) and
#: `contract_review` (`off`/`report`/`enforce`). `set_by`, `set_at` and
#: `mac` are not covered. A switch added to the document without being added
#: here is outside the MAC, so a tenant worker can forge it.
PLATFORM_SETTINGS_MAC_FIELDS: tuple[str, ...] = ("work_contracts", "contract_review")


def canonical_platform_settings(fields: Mapping[str, Any]) -> bytes:
    """JCS of {"purpose": "swarm.platform-settings/v1", "fields"}.

    `fields` is the document as stored; only PLATFORM_SETTINGS_MAC_FIELDS
    are projected, a listed key that is absent is null, and any other key is
    not covered and must not be acted on. Its purpose is not a workspace
    block's, so neither MAC verifies the other's document.

    The MAC is HMAC-SHA256 over these bytes under the `swarm-settings-key`
    secret. It is not computed here, because its key is not the contract's.
    """
    if not isinstance(fields, Mapping):
        raise SpecNotCanonical("platform settings are not a map")
    return jcs(
        {
            "purpose": PLATFORM_SETTINGS_PURPOSE,
            "fields": {key: fields.get(key) for key in PLATFORM_SETTINGS_MAC_FIELDS},
        }
    )


#: Per-person signed submissions (contract request 155, request E of
#: docs/design/workspace-secrets.md section 11.9, accepted by the owner
#: 2026-11-13). A person signs these bytes with their own key, so swarm-api,
#: the `sc` plugin and the console must all produce the same ones: an `sc`
#: keychain key (Ed25519) signs `"<purpose>\n" || spec_digest(bytes)` and a
#: console passkey takes `spec_digest(bytes)` as its WebAuthn challenge
#: (section 11.2). No key and no verification live here.
PERSON_SUBMISSION_PURPOSE = "swarm.person-submission/v1"
STANDING_GRANT_PURPOSE = "swarm.standing-grant/v1"
STANDING_GRANT_REVOKE_PURPOSE = "swarm.standing-grant-revoke/v1"

#: How the submitter reached the tenant (section 11.4, the owner's Q P10).
TENANT_VIA: tuple[str, ...] = ("member", "platform_admin")

#: The headless origins a standing grant may name (section 11.5).
STANDING_GRANT_ORIGIN_KINDS: tuple[str, ...] = ("schedule", "issue_run", "issue_sweep")

_STATEMENT_SPEC_KEYS = frozenset(("task_id", "spec_format", "spec_digest"))
_HEX = frozenset("0123456789abcdef")


def _required_text(value: Any, what: str) -> str:
    if not (isinstance(value, str) and value):
        raise SpecNotCanonical(f"{what} is not a non-empty string")
    return value


def _name_list(value: Any, what: str, *, user_slots: bool = False) -> list[str]:
    # A set of names: one canonical order, no duplicates, and never "all",
    # which none of a grant's lists accepts.
    if not isinstance(value, (list, tuple)):
        raise SpecNotCanonical(f"{what} is not a list")
    names = [_required_text(v, f"an entry of {what}") for v in value]
    if len(set(names)) != len(names):
        raise SpecNotCanonical(f"{what} names an entry twice")
    if "all" in names:
        raise SpecNotCanonical(f"{what} names 'all', which a grant never covers")
    if user_slots:
        for name in names:
            suffix = name[len("git-u-"):]
            if not (name.startswith("git-u-") and len(suffix) == 16 and set(suffix) <= _HEX):
                raise SpecNotCanonical(f"{what} entry {name!r} is not a user slot 'git-u-<16 hex>'")
    return sorted(names, key=lambda n: n.encode("utf-16-be"))


def canonical_submission_statement(
    *,
    prepared_id: str,
    tenant_id: str,
    submitted_by: str,
    tenant_via: str,
    issued_at: str,
    expires_at: str,
    specs: Any,
) -> bytes:
    """JCS of the statement a person signs to submit (design section 11.3).

    {"purpose": "swarm.person-submission/v1", "prepared_id", "tenant_id",
    "submitted_by", "tenant_via", "issued_at", "expires_at", "specs"}, each
    spec `{"task_id", "spec_format", "spec_digest"}`: the step's format and
    the lowercase hex of `spec_digest(canonical_step_spec(...))`. `specs` is
    sorted by `task_id`, then `spec_format`, then `spec_digest`, so a
    `browser_if_ui` step's runtime alternate (a second entry under the same
    task id) has one place. `tenant_via` is one of TENANT_VIA. The times are
    the strings swarm-api issued; every field is required.
    """
    head = {
        "purpose": PERSON_SUBMISSION_PURPOSE,
        "prepared_id": _required_text(prepared_id, "the statement's prepared_id"),
        "tenant_id": _required_text(tenant_id, "the statement's tenant_id"),
        "submitted_by": _required_text(submitted_by, "the statement's submitted_by"),
        "tenant_via": tenant_via,
        "issued_at": _required_text(issued_at, "the statement's issued_at"),
        "expires_at": _required_text(expires_at, "the statement's expires_at"),
    }
    if tenant_via not in TENANT_VIA:
        raise SpecNotCanonical(f"the statement's tenant_via is not one of {TENANT_VIA}")
    if not (isinstance(specs, (list, tuple)) and specs):
        raise SpecNotCanonical("the statement names no spec")
    entries = []
    for i, spec in enumerate(specs):
        if not (isinstance(spec, Mapping) and set(spec) == _STATEMENT_SPEC_KEYS):
            raise SpecNotCanonical(f"spec {i} is not exactly {sorted(_STATEMENT_SPEC_KEYS)}")
        spec_format = spec["spec_format"]
        if not isinstance(spec_format, int) or isinstance(spec_format, bool) or spec_format < 1:
            raise SpecNotCanonical(f"spec {i}'s spec_format is not a positive integer")
        digest = spec["spec_digest"]
        if not (isinstance(digest, str) and len(digest) == 64 and set(digest) <= _HEX):
            raise SpecNotCanonical(f"spec {i}'s spec_digest is not 64 lowercase hex digits")
        entries.append(
            {
                "task_id": _required_text(spec["task_id"], f"spec {i}'s task_id"),
                "spec_format": spec_format,
                "spec_digest": digest,
            }
        )
    entries.sort(key=lambda e: (e["task_id"].encode("utf-16-be"), e["spec_format"], e["spec_digest"]))
    if any(a == b for a, b in zip(entries, entries[1:])):
        raise SpecNotCanonical("the statement names one spec twice")
    return jcs({**head, "specs": entries})


def canonical_standing_grant(
    *,
    grant_id: str,
    grantor: str,
    tenant_id: str,
    origin: Mapping[str, Any],
    profiles: Any,
    secrets: Any,
    not_before: str,
    forge: Any = None,
    mcp_servers: Any = None,
    expires_at: str | None = None,
) -> bytes:
    """JCS of a headless job's standing grant (design section 11.5).

    {"purpose": "swarm.standing-grant/v1", "grant_id", "grantor",
    "tenant_id", "origin": {"kind", "id"}, "profiles", "secrets", "forge",
    "mcp_servers", "not_before", "expires_at"}. `origin.kind` is one of
    STANDING_GRANT_ORIGIN_KINDS. The lists are sets of names, sorted, never
    "all"; `profiles` names at least one profile, `secrets` may be empty, and
    `forge` holds only user slots (`git-u-<16 hex>`).

    `expires_at` is optional and absent means no expiry (the owner's Q P4),
    so an absent one is OMITTED, never written as null. `forge` and
    `mcp_servers` are optional and an absent or empty list covers nothing,
    so both are omitted too: a grant has one byte form whichever way its
    writer spelled "none".
    """
    if not (isinstance(origin, Mapping) and set(origin) == {"kind", "id"}):
        raise SpecNotCanonical("the grant's origin is not exactly {kind, id}")
    if origin["kind"] not in STANDING_GRANT_ORIGIN_KINDS:
        raise SpecNotCanonical(f"the grant's origin kind is not one of {STANDING_GRANT_ORIGIN_KINDS}")
    grant: dict[str, Any] = {
        "purpose": STANDING_GRANT_PURPOSE,
        "grant_id": _required_text(grant_id, "the grant's grant_id"),
        "grantor": _required_text(grantor, "the grant's grantor"),
        "tenant_id": _required_text(tenant_id, "the grant's tenant_id"),
        "origin": {"kind": origin["kind"], "id": _required_text(origin["id"], "the grant's origin id")},
        "profiles": _name_list(profiles, "the grant's profiles"),
        "secrets": _name_list(secrets, "the grant's secrets"),
        "not_before": _required_text(not_before, "the grant's not_before"),
    }
    if not grant["profiles"]:
        raise SpecNotCanonical("the grant names no profile")
    if forge:
        grant["forge"] = _name_list(forge, "the grant's forge slots", user_slots=True)
    elif forge is not None and not isinstance(forge, (list, tuple)):
        raise SpecNotCanonical("the grant's forge slots are not a list")
    if mcp_servers:
        grant["mcp_servers"] = _name_list(mcp_servers, "the grant's MCP servers")
    elif mcp_servers is not None and not isinstance(mcp_servers, (list, tuple)):
        raise SpecNotCanonical("the grant's MCP servers are not a list")
    if expires_at is not None:
        grant["expires_at"] = _required_text(expires_at, "the grant's expires_at")
    return jcs(grant)


def canonical_standing_grant_revoke(*, grant_id: str, grantor: str, revoked_at: str) -> bytes:
    """JCS of {"purpose": "swarm.standing-grant-revoke/v1", "grant_id", "grantor", "revoked_at"}.

    The grantor signs it to end one grant (section 11.5). With no expiry by
    default (Q P4), revocation is how a grant ends, so its bytes have this
    one definition too.
    """
    return jcs(
        {
            "purpose": STANDING_GRANT_REVOKE_PURPOSE,
            "grant_id": _required_text(grant_id, "the revoke's grant_id"),
            "grantor": _required_text(grantor, "the revoke's grantor"),
            "revoked_at": _required_text(revoked_at, "the revoke's revoked_at"),
        }
    )


def spec_digest(canonical: bytes) -> bytes:
    """SHA-256 of the canonical bytes: what the KMS key signs."""
    return hashlib.sha256(canonical).digest()


def jcs(value: Any) -> bytes:
    """RFC 8785 serialisation of `value`, as UTF-8 bytes."""
    return _encode(value, "$", 0).encode("utf-8")


def _encode(value: Any, path: str, depth: int) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise SpecNotCanonical(f"{path} is an integer outside +/-(2**53 - 1)")
        return str(value)
    if isinstance(value, float):
        return _number(value, path)
    if isinstance(value, str):
        return _string(value, path)
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            raise SpecNotCanonical(f"{path} is nested deeper than {MAX_DEPTH} levels")
        return "[" + ",".join(
            _encode(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(value)
        ) + "]"
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            raise SpecNotCanonical(f"{path} is nested deeper than {MAX_DEPTH} levels")
        items = []
        for key in value:
            if not isinstance(key, str):
                raise SpecNotCanonical(f"{path} has a key that is not a string")
            _string(key, path)  # refuses a lone surrogate before it is sorted
            items.append(key)
        # RFC 8785 3.2.3: members sorted by the UTF-16 code units of their RAW
        # names -- not their escaped form, which would put "\r" after "1".
        items.sort(key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(
            _string(k, path) + ":" + _encode(value[k], f"{path}.{k}", depth + 1)
            for k in items
        ) + "}"
    raise SpecNotCanonical(f"{path} is a {type(value).__name__}, which JSON has no form for")


def _string(text: str, path: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpecNotCanonical(f"{path} holds a lone surrogate") from exc
    # json.dumps with ensure_ascii=False escapes exactly what RFC 8785 3.2.2.2
    # does: '"', '\\', the five short forms, and \u00xx (lowercase) for the
    # rest of U+0000..U+001F. Everything else is written as itself.
    return json.dumps(text, ensure_ascii=False)


def _number(x: float, path: str) -> str:
    # RFC 8785 3.2.2.3: ECMAScript's Number.prototype.toString.
    if not math.isfinite(x):
        raise SpecNotCanonical(f"{path} is not a finite number")
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _number(-x, path)
    # repr() is the shortest round-tripping decimal, as ECMAScript requires.
    _, digits_t, exponent = Decimal(repr(x)).as_tuple()
    digits = "".join(map(str, digits_t))
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
