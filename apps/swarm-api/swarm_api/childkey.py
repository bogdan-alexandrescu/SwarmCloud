"""The attempt key: what makes "only the worker may submit a child" true.

docs/design/child-tasks.md §3.2. An agent runs in its worker's container as
the worker's uid, so it can mint the same ID token its worker mints and read
the whole container environment. The ID token therefore proves the TENANT; what
proves the WORKER is a signature by an Ed25519 key the worker generated in its
non-dumpable heap and registered here before the agent existed.

Three HMAC-SHA256 derivations under `swarm-child-key` (held by swarm-scheduler
and swarm-api only), each domain-separated by its purpose string so one can
never be read as another:

  * the one-use registration NONCE the scheduler passes at dispatch;
  * the registration's DOCUMENT ID, which no tenant identity can compute;
  * the ATTESTATION over the tuple and the public key, which is the only thing
    that makes a registration trusted -- not the document's existence, which a
    tenant identity that found the id could rewrite.

The scheduler restates `nonce` (its image does not carry this package);
`tests/unit/control_plane/test_child_tasks_keys.py` holds the two equal.

Nothing here logs, and nothing derived here is ever served: not the nonce, the
registration id, the attestation or the key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass

NONCE_PURPOSE = "swarm-child-nonce/v1"
REGISTRATION_ID_PURPOSE = "swarm-child-reg-id/v1"
ATTESTATION_PURPOSE = "swarm-child-reg/v1"
REQUEST_PURPOSE = "swarm-child-req/v1"

#: Where registrations live. Not frozen; only swarm-api writes it.
CHILD_KEYS = "child_keys"

#: The tombstone's refusal (§5 F12): the worker could not protect its heap, so
#: it spent the nonce and registered no key.
WORKER_UNPROTECTED = "worker_unprotected"


@dataclass(frozen=True)
class AttemptTuple:
    """`(tenant_id, task_id, attempt_id, lease_id, generation)`: one attempt."""

    tenant_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    generation: int

    def encode(self) -> str:
        return "\n".join(
            (self.tenant_id, self.task_id, self.attempt_id, self.lease_id, str(self.generation))
        )

    def matches(self, doc: dict) -> bool:
        """Does a stored registration name exactly this tuple?"""
        return (
            doc.get("tenant_id") == self.tenant_id
            and doc.get("task_id") == self.task_id
            and doc.get("attempt_id") == self.attempt_id
            and doc.get("lease_id") == self.lease_id
            and doc.get("generation") == self.generation
            and not isinstance(doc.get("generation"), bool)
        )


def _mac(key: str, purpose: str, message: str) -> str:
    return hmac.new(
        key.encode("utf-8"), f"{purpose}\n{message}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def nonce(key: str, attempt: AttemptTuple) -> str:
    return _mac(key, NONCE_PURPOSE, attempt.encode())


def registration_id(key: str, attempt: AttemptTuple) -> str:
    return _mac(key, REGISTRATION_ID_PURPOSE, attempt.encode())


def registered_value(public_key: str | None, refused: str | None) -> str:
    """What the attestation covers beside the tuple: the key, or the tombstone."""
    return f"key:{public_key}" if public_key else f"tombstone:{refused or ''}"


def attestation(key: str, attempt: AttemptTuple, public_key: str | None, refused: str | None) -> str:
    return _mac(key, ATTESTATION_PURPOSE, f"{attempt.encode()}\n{registered_value(public_key, refused)}")


class ChildKeys:
    """The current and (during a rotation) previous `swarm-child-key` versions.

    §5 F13: a nonce or an attestation is accepted under either; registration
    ids are derived under both, current first. A registration is always WRITTEN
    under the current version.
    """

    def __init__(self, current: str, previous: str = "") -> None:
        self._versions = tuple(v for v in (current, previous) if v)

    @property
    def configured(self) -> bool:
        return bool(self._versions)

    @property
    def current(self) -> str:
        return self._versions[0]

    def verify_nonce(self, attempt: AttemptTuple, presented: str) -> bool:
        if not isinstance(presented, str) or not presented:
            return False
        ok = False
        for version in self._versions:
            # Every version compared, so the time taken says nothing about which.
            ok |= hmac.compare_digest(nonce(version, attempt), presented)
        return ok

    def registration_ids(self, attempt: AttemptTuple) -> list[str]:
        return [registration_id(version, attempt) for version in self._versions]

    def attest(self, attempt: AttemptTuple, public_key: str | None, refused: str | None) -> str:
        return attestation(self.current, attempt, public_key, refused)

    def attestation_holds(self, attempt: AttemptTuple, doc: dict) -> bool:
        stored = doc.get("attestation")
        if not isinstance(stored, str) or not stored:
            return False
        public_key = doc.get("public_key") if isinstance(doc.get("public_key"), str) else None
        refused = doc.get("refused") if isinstance(doc.get("refused"), str) else None
        ok = False
        for version in self._versions:
            ok |= hmac.compare_digest(attestation(version, attempt, public_key, refused), stored)
        return ok


# --------------------------------------------------------------------------
# The attempt proof (§3.2 step 5)
# --------------------------------------------------------------------------


def request_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs for one request. The worker restates this; a test
    signs with the worker's code and verifies with this one."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{REQUEST_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def valid_public_key(value: object) -> bool:
    """A base64url Ed25519 public key: exactly 32 raw bytes."""
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        raw = _b64url_decode(value)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 32


def verify_proof(public_key: str, signature: str, message: bytes) -> bool:
    """Ed25519 verification. False for anything malformed, never an exception."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(signature, str) or not signature or len(signature) > 128:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64url_decode(public_key))
        key.verify(_b64url_decode(signature), message)
    except (InvalidSignature, binascii.Error, ValueError, UnicodeEncodeError):
        return False
    return True
