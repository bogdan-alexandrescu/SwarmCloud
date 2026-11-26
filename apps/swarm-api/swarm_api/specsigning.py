"""swarm-api signs every step spec it creates (contract request 34, archived issue 342).

A tenant's agents can write any task document of their tenant, so the worker
must not run a spec swarm-api did not sign. This module signs: for each task,
`spec_digest(canonical_step_spec(task.to_firestore(), task_id=task.id))` --
the ONE canonical form, `swarm_common.specsign` -- with Cloud KMS
`AsymmetricSign` on an `EC_SIGN_P256_SHA256` key only swarm-api's account
holds `signer` on. The three fields are set on the `Task`, so they are part of
the write that creates the document, never a second update.

WHERE IT IS CALLED, AND WHY THERE. `SubmissionService.submit_tasks` and
`submit_workflow`, after every write to the task and immediately before the
store call: for a workflow, after the loop that writes `metadata.input_from`
and after `record_expected_outputs`. Signing inside `_build_task` would sign a
spec missing both.

REFUSED BEFORE ANYTHING IS SIGNED. Every task of the submission is
canonicalised first; a value with no canonical form (an integer past
2^53 - 1, a non-finite number, a lone surrogate, a `timeout_seconds` or
`max_attempts` that is not an integer -- contract request 58 -- or lists and
maps nested deeper than `specsign.MAX_DEPTH` -- request 59) is 422
`invalid_input` and KMS is never called.

FAILS CLOSED. A KMS error, a digest KMS did not verify, a signature whose
CRC32C does not match, or an answer naming another version is 503 and nothing
is stored. One sign call per step, concurrently for a workflow.
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence

from swarm_common import specsign
from swarm_common.models import Task

from .errors import UpstreamUnavailable
from .validation import RUNTIME_METADATA_KEY, RUNTIME_SWAP_FIELDS, InvalidInput

log = logging.getLogger(__name__)

#: Workflow steps signed at once. Each is one KMS round trip, so a 30-step
#: workflow is a few round trips of latency rather than thirty.
MAX_CONCURRENT_SIGNS = 8


@dataclass(frozen=True)
class SpecSignature:
    """What a signer returns: the DER ECDSA signature and the version that made it."""

    signature: bytes
    #: The FULL resource name: projects/.../cryptoKeys/<k>/cryptoKeyVersions/<n>.
    key_version: str


class SpecSigner(Protocol):
    def sign(self, digest: bytes) -> SpecSignature:
        """Sign a SHA-256 digest. Raises on any failure; never returns a partial."""


class SpecSigningUnavailable(UpstreamUnavailable):
    """KMS could not sign, or its answer did not check out: 503, nothing stored."""

    code = "spec_signing_unavailable"


class NonCanonicalInput(InvalidInput):
    """A value in the step's spec has no canonical form (contract request 34)."""


def _crc32c(data: bytes) -> int:
    import google_crc32c

    return int(google_crc32c.value(data))


class KmsSpecSigner:
    """Cloud KMS `AsymmetricSign` on one key version, named in full.

    An asymmetric key has no primary version, so the version is configuration
    (`SPEC_SIGNING_KEY_VERSION`), and rotation is: publish N+1 to the workers,
    then point this at it (docs in contract request 34, "Rotation").
    """

    def __init__(self, key_version: str, *, client: Any | None = None) -> None:
        self._key_version = key_version
        self._client = client

    def _kms(self) -> Any:
        if self._client is None:
            from google.cloud import kms

            self._client = kms.KeyManagementServiceClient()
        return self._client

    def sign(self, digest: bytes) -> SpecSignature:
        request = {
            "name": self._key_version,
            "digest": {"sha256": digest},
            "digest_crc32c": _crc32c(digest),
        }
        try:
            response = self._kms().asymmetric_sign(request=request)
        except Exception as exc:  # any KMS failure is a 503, never an unsigned write
            raise SpecSigningUnavailable(
                "the step spec could not be signed (Cloud KMS); nothing was stored",
            ) from exc
        if not getattr(response, "verified_digest_crc32c", False):
            raise SpecSigningUnavailable("Cloud KMS did not verify the digest it was sent")
        if getattr(response, "name", None) != self._key_version:
            raise SpecSigningUnavailable("Cloud KMS answered for another key version")
        signature = bytes(getattr(response, "signature", b"") or b"")
        if not signature or _crc32c(signature) != int(getattr(response, "signature_crc32c", -1)):
            raise SpecSigningUnavailable("the signature Cloud KMS returned failed its CRC32C")
        return SpecSignature(signature=signature, key_version=self._key_version)


#: Environments that are never deployed: a swarm-api here may run unsigned.
UNDEPLOYED_ENVIRONMENTS = frozenset({"test", "local"})


def _is_deployed(settings: Any) -> bool:
    if getattr(settings, "hardened", False):
        return True
    if not getattr(settings, "environment_declared", False):
        return False
    core = getattr(settings, "core", None)
    environment = str(getattr(core, "environment", "") or "").strip().lower()
    return environment not in UNDEPLOYED_ENVIRONMENTS


def signer_from_settings(settings: Any) -> SpecSigner | None:
    """The KMS signer, or None where no key is configured.

    None only in local development and tests. Every DEPLOYED environment
    refuses to start without the key, dev included: `hardened` is False for
    dev (settings.py), and dev is the environment that is actually deployed,
    so keying this on `hardened` alone let a dev swarm-api with no key start
    and write unsigned tasks that every worker in `enforce` mode refuses
    (archived pull request 353 security review, M2). "Deployed" is: hardened, or an ENVIRONMENT
    that was DECLARED as anything but test or local. An undeclared
    environment reads as the frozen default "dev" and is local development
    (docker-compose declares `local` for swarm-api for the same reason).
    """
    version = str(getattr(settings, "spec_signing_key_version", "") or "").strip()
    if version:
        return KmsSpecSigner(version)
    if _is_deployed(settings):
        raise ValueError(
            "SPEC_SIGNING_KEY_VERSION is required outside local development: without it "
            "swarm-api would write step specs no worker will run (contract request 34). "
            "Terraform sets it from the bootstrap key ring."
        )
    return None


def _runtime_alternate(task: Task) -> dict[str, Any] | None:
    """A `browser_if_ui` step's alternate profile fields (archived issue 2322), or None."""
    block = task.metadata.get(RUNTIME_METADATA_KEY) if isinstance(task.metadata, dict) else None
    alternate = block.get("alternate") if isinstance(block, dict) else None
    return alternate if isinstance(alternate, dict) else None


def sign_task_specs(tasks: Sequence[Task], signer: SpecSigner | None) -> None:
    """Set `spec_signature`, `spec_key_version` and `spec_format` on every task.

    Canonicalises EVERY task before signing ANY, so a refusal calls KMS for
    none of them. Raises `NonCanonicalInput` (422) or `SpecSigningUnavailable`
    (503); on either, no task carries a signature.

    A `browser_if_ui` step (archived issue 2322) is signed TWICE, under the same task id:
    as stored, and with its `metadata.runtime.alternate` profile fields in
    place, whose signature goes into that alternate. The scheduler, which
    holds no key, swaps a step to its other profile only with the signature
    made here, so the worker still runs only a spec swarm-api signed.
    """
    subjects: list[Task] = []
    for task in tasks:
        subjects.append(task)
        alternate = _runtime_alternate(task)
        if alternate is not None:
            subjects.append(replace(
                task, **{name: alternate.get(name) for name in RUNTIME_SWAP_FIELDS}))
    digests: list[bytes] = []
    formats: list[int] = []
    for task in subjects:
        stored = task.to_firestore()
        # Format 1 for a task that names no parent, so a worker built before
        # contract request 42 keeps verifying every non-child task.
        spec_format = specsign.signing_format(stored)
        formats.append(spec_format)
        try:
            canonical = specsign.canonical_step_spec(
                stored, task_id=task.id, spec_format=spec_format
            )
        except specsign.SpecNotCanonical as exc:
            raise NonCanonicalInput(
                "a value in this task has no canonical form and cannot be signed: an integer "
                "outside +/-(2**53 - 1), a number that is not finite, a string holding a "
                "lone surrogate, a timeout_seconds or max_attempts that is not an integer, "
                f"or lists and maps nested deeper than {specsign.MAX_DEPTH} levels",
                detail={"where": str(exc).split(" ", 1)[0], "task_step": task.step_id},
            ) from None
        digests.append(specsign.spec_digest(canonical))
    if signer is None:
        for task in tasks:
            alternate = _runtime_alternate(task)
            if alternate is not None:
                alternate.update(spec_signature=None, spec_key_version=None, spec_format=None)
        if tasks:
            log.warning(
                "no SPEC_SIGNING_KEY_VERSION: %d task(s) written UNSIGNED; every worker in "
                "enforce mode will refuse them", len(tasks),
            )
        return
    try:
        if len(digests) == 1:
            signed = [signer.sign(digests[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_SIGNS, len(digests))) as pool:
                signed = list(pool.map(signer.sign, digests))
    except UpstreamUnavailable:
        raise
    except Exception as exc:
        raise SpecSigningUnavailable(
            "the step spec could not be signed; nothing was stored"
        ) from exc
    results = iter(zip(signed, formats))
    for task in tasks:
        result, spec_format = next(results)
        task.spec_signature = base64.b64encode(result.signature).decode("ascii")
        task.spec_key_version = result.key_version
        task.spec_format = spec_format
        alternate = _runtime_alternate(task)
        if alternate is not None:
            result, spec_format = next(results)
            alternate.update(
                spec_signature=base64.b64encode(result.signature).decode("ascii"),
                spec_key_version=result.key_version,
                spec_format=spec_format,
            )


def stored_refusal(doc: Mapping[str, Any], *, task_id: str, signer: SpecSigner | None,
                   settings: Any) -> str | None:
    """Why a STORED task must not be signed again, or None when it may.

    The one verify-before-resign rule (issue 207): every path that signs a
    new spec from a stored task document -- the review gate's class
    (`resign_stored`), a fix loop's round (`fixloop._next`), a step re-run's
    clone (`steprerun.build`) -- asks this first, because a tenant's agents
    can write any task document of their tenant. The check itself is
    `verdictpublish.stored_spec_refusal`: the document, and its runtime
    alternate, verify against the deployment's keys.

    None with no signer: a deployment with no key (local development and
    tests only, `signer_from_settings`) writes every spec unsigned and
    verifies none, as the review gate does there. Nothing it writes carries
    swarm-api's signature, so nothing is vouched for.
    """
    if signer is None:
        return None
    from .verdictpublish import stored_spec_refusal  # verdictpublish imports this module

    return stored_spec_refusal(settings, doc, task_id)


def resign_stored(doc: Mapping[str, Any], *, task_id: str, changes: Mapping[str, Any],
                  alternate_changes: Mapping[str, Any] | None,
                  signer: SpecSigner | None, settings: Any) -> dict[str, Any] | None:
    """The fields to write so a STORED task runs with `changes`, signed again.

    For the review gate's class (`stepclass`, the owner 2026-11-23): the step
    was signed at submission and is still held, so swarm-api -- the only
    holder of the key -- signs it again as it will be stored, before the
    scheduler may promote it. `changes` are top-level fields
    (`model_class`, `model`); `alternate_changes` the same fields of a
    `browser_if_ui` step's `metadata.runtime.alternate`, which is signed
    again too, so the scheduler's swap still carries swarm-api's signature.

    None when it cannot be signed: no signer for a task that was signed (an
    unsigned spec would be refused by every worker), a value with no
    canonical form, or a KMS failure. The caller then leaves the task as it
    was. With no signer and an unsigned task, the fields are written
    unsigned, exactly as `sign_task_specs` writes a task with no key.

    None, too, when the document as stored does not verify against the
    deployment's keys (`settings`), or its runtime alternate does not
    (`verdictpublish.stored_spec_refusal`): a tenant's agents can write any
    task document of their tenant, and signing `{**doc, **changes}` over a
    field one of them rewrote would make swarm-api vouch for it. Checked
    here, not only by the caller, so no later caller can sign a tampered
    spec again (`stored_refusal`).
    """
    refusal = stored_refusal(doc, task_id=task_id, signer=signer, settings=settings)
    if refusal is not None:
        log.warning("task %s is not signed again: %s", task_id, refusal)
        return None
    updated = {**doc, **changes}
    metadata = dict(doc.get("metadata") or {}) if isinstance(doc.get("metadata"), Mapping) else {}
    raw_block = metadata.get(RUNTIME_METADATA_KEY)
    block: Mapping[str, Any] = raw_block if isinstance(raw_block, Mapping) else {}
    alternate = (dict(block["alternate"]) if isinstance(block.get("alternate"), Mapping)
                 else None)
    if alternate is not None and alternate_changes:
        alternate.update(alternate_changes)
    subjects = [updated]
    if alternate is not None:
        subjects.append({**updated, **{name: alternate.get(name) for name in RUNTIME_SWAP_FIELDS}})
    fields: dict[str, Any] = dict(changes)
    if signer is None:
        if doc.get("spec_signature"):
            return None
        signed: list[tuple[str | None, str | None, int | None]] = [(None, None, None)] * len(subjects)
    else:
        try:
            digests, formats = [], []
            for subject in subjects:
                spec_format = specsign.signing_format(subject)
                formats.append(spec_format)
                digests.append(specsign.spec_digest(specsign.canonical_step_spec(
                    subject, task_id=task_id, spec_format=spec_format)))
            signed = []
            for digest, spec_format in zip(digests, formats):
                result = signer.sign(digest)
                signed.append((base64.b64encode(result.signature).decode("ascii"),
                               result.key_version, spec_format))
        except Exception:  # the task keeps the class it was signed with
            log.exception("task %s could not be signed again; it keeps its class", task_id)
            return None
    fields.update(zip(("spec_signature", "spec_key_version", "spec_format"), signed[0]))
    if alternate is not None:
        alternate.update(zip(("spec_signature", "spec_key_version", "spec_format"), signed[1]))
        metadata[RUNTIME_METADATA_KEY] = {**block, "alternate": alternate}
        fields["metadata"] = metadata
    return fields
