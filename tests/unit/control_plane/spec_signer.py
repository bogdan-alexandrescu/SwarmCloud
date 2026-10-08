"""A local P-256 signer behind swarm-api's `SpecSigner` interface.

Contract request 34, section 9: offline, no KMS, no credentials. It signs the
way Cloud KMS's `AsymmetricSign` does for `EC_SIGN_P256_SHA256` -- ECDSA over
a SHA-256 digest the caller computed, DER-encoded -- and names a full key
version, so `swarm_api.specsigning` cannot tell it from the real one. It also
records every digest it was asked to sign, so a test can prove KMS was never
called on a refusal.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from swarm_api.specsigning import SpecSignature
from swarm_common import specsign

KEY_VERSION = (
    "projects/swarm-test/locations/us-central1/keyRings/swarm-test-specs/"
    "cryptoKeys/step-spec/cryptoKeyVersions/1"
)


class LocalSpecSigner:
    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.fail_with = fail_with
        self.signed: list[bytes] = []

    def sign(self, digest: bytes) -> SpecSignature:
        if self.fail_with is not None:
            raise self.fail_with
        self.signed.append(digest)
        der = self._key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return SpecSignature(signature=der, key_version=KEY_VERSION)

    def public_pem(self) -> str:
        """The PEM a SPEC_VERIFY_KEYS entry carries for this key."""
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")

    def verifies(self, doc: Mapping[str, Any], task_id: str) -> bool:
        """Does `doc`, as stored, carry a signature of its own spec by this key?"""
        if (
            doc.get("spec_key_version") != KEY_VERSION
            or doc.get("spec_format") not in specsign.SPEC_FORMATS
        ):
            return False
        digest = specsign.spec_digest(
            specsign.canonical_step_spec(doc, task_id=task_id, spec_format=doc["spec_format"])
        )
        try:
            self._key.public_key().verify(
                base64.b64decode(doc["spec_signature"]),
                digest,
                ec.ECDSA(utils.Prehashed(hashes.SHA256())),
            )
        except (InvalidSignature, KeyError, ValueError):
            return False
        return True

    def sign_document(self, doc: dict[str, Any], task_id: str) -> dict[str, Any]:
        """Sign a stored document in place, as submission would have."""
        spec_format = specsign.signing_format(doc)
        digest = specsign.spec_digest(
            specsign.canonical_step_spec(doc, task_id=task_id, spec_format=spec_format)
        )
        signed = self.sign(digest)
        doc["spec_signature"] = base64.b64encode(signed.signature).decode("ascii")
        doc["spec_key_version"] = signed.key_version
        doc["spec_format"] = spec_format
        return doc
