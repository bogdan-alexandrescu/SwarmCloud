"""A local stand-in for swarm-api's Cloud KMS signer, for the worker's tests.

Contract request 34, section 9: "the signer in tests is a local P-256 key from
`cryptography` behind the same interface as the KMS signer, and the worker's
`SPEC_VERIFY_KEYS` holds its public key". The key is made once per test
process; nothing here reaches KMS, a credential or the network.

`sign_document` does what `swarm_api.specsigning.sign_task_specs` does to a
task at submission: project the stored document with the ONE canonical form
(`swarm_common.specsign`), sign its SHA-256 with ECDSA P-256 over the digest,
and write the three fields beside it.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from swarm_common import specsign

#: The crypto key the worker trusts versions of (`SPEC_SIGNING_KEY`).
SIGNING_KEY = (
    "projects/swarm-test/locations/us-central1/keyRings/swarm-test-specs/cryptoKeys/step-spec"
)
#: The one enabled version, by its full resource name.
KEY_VERSION = f"{SIGNING_KEY}/cryptoKeyVersions/1"

_PRIVATE = ec.generate_private_key(ec.SECP256R1())
#: A second key the platform never published: what a forger holds.
_FORGER = ec.generate_private_key(ec.SECP256R1())


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )


#: `SPEC_VERIFY_KEYS` as terraform renders it: {full version name: PEM}.
VERIFY_KEYS: dict[str, str] = {KEY_VERSION: _pem(_PRIVATE)}


def sign_digest(digest: bytes, *, forged: bool = False) -> bytes:
    key = _FORGER if forged else _PRIVATE
    return key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))


def signature_for(
    doc: Mapping[str, Any],
    task_id: str,
    *,
    forged: bool = False,
    spec_format: int = specsign.SPEC_FORMAT,
) -> str:
    canonical = specsign.canonical_step_spec(doc, task_id=task_id, spec_format=spec_format)
    return base64.b64encode(sign_digest(specsign.spec_digest(canonical), forged=forged)).decode()


def sign_document(
    doc: dict[str, Any],
    task_id: str,
    *,
    key_version: str = KEY_VERSION,
    forged: bool = False,
    spec_format: int = specsign.SPEC_FORMAT,
) -> dict[str, Any]:
    """Sign `doc` in place, as swarm-api would have at submission, and return it."""
    doc["spec_signature"] = signature_for(doc, task_id, forged=forged, spec_format=spec_format)
    doc["spec_key_version"] = key_version
    doc["spec_format"] = spec_format
    return doc
