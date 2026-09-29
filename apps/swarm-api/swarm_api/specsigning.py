"""swarm-api signs every step spec it creates (contract request 34, #342).

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
2^53 - 1, a non-finite number, a lone surrogate) is 422 `invalid_input` and
KMS is never called.

FAILS CLOSED. A KMS error, a digest KMS did not verify, a signature whose
CRC32C does not match, or an answer naming another version is 503 and nothing
is stored. One sign call per step, concurrently for a workflow.
"""

from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from swarm_common import specsign
from swarm_common.models import Task

from .errors import UpstreamUnavailable
from .validation import InvalidInput

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


def signer_from_settings(settings: Any) -> SpecSigner | None:
    """The KMS signer, or None where no key is configured.

    None only outside a hardened environment (local development, tests):
    `build_context` refuses to start a hardened one without the key, the same
    way it refuses an unpinned token audience. A task written unsigned is
    refused by every worker in `enforce` mode, so a misconfiguration fails
    loudly at the worker rather than running anything unverified.
    """
    version = str(getattr(settings, "spec_signing_key_version", "") or "").strip()
    if version:
        return KmsSpecSigner(version)
    if getattr(settings, "hardened", False):
        raise ValueError(
            "SPEC_SIGNING_KEY_VERSION is required outside local development: without it "
            "swarm-api would write step specs no worker will run (contract request 34). "
            "Terraform sets it from the bootstrap key ring."
        )
    return None


def sign_task_specs(tasks: Sequence[Task], signer: SpecSigner | None) -> None:
    """Set `spec_signature`, `spec_key_version` and `spec_format` on every task.

    Canonicalises EVERY task before signing ANY, so a refusal calls KMS for
    none of them. Raises `NonCanonicalInput` (422) or `SpecSigningUnavailable`
    (503); on either, no task carries a signature.
    """
    digests: list[bytes] = []
    for task in tasks:
        try:
            canonical = specsign.canonical_step_spec(task.to_firestore(), task_id=task.id)
        except specsign.SpecNotCanonical as exc:
            raise NonCanonicalInput(
                "a value in this task has no canonical form and cannot be signed: an integer "
                "outside +/-(2**53 - 1), a number that is not finite, or a string holding a "
                "lone surrogate",
                detail={"where": str(exc).split(" ", 1)[0], "task_step": task.step_id},
            ) from None
        digests.append(specsign.spec_digest(canonical))
    if signer is None:
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
    for task, result in zip(tasks, signed):
        task.spec_signature = base64.b64encode(result.signature).decode("ascii")
        task.spec_key_version = result.key_version
        task.spec_format = specsign.SPEC_FORMAT
