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
`tests/unit/control_plane/test_child_tasks_scheduler.py` holds the two equal
(`test_the_scheduler_mints_the_nonce_swarm_api_verifies`).

The state routes (docs/design/tenant-settings-integrity.md §4.1, §4.5) reuse
the key under their own request purpose, `swarm-state-req/v1`, and a v2
registration whose attestation also covers the purposes the key may sign for
and whether its heap was protected. A v1 registration is child-only.

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
#: The state routes' proof (docs/design/tenant-settings-integrity.md §4.1).
#: Domain-separated from REQUEST_PURPOSE, so a child-submission proof can never
#: be replayed as a state write, nor a state proof as a submission.
STATE_REQUEST_PURPOSE = "swarm-state-req/v1"
#: The v2 registration (§4.5): the attestation also covers what the key may
#: sign for and whether its heap was protected. A v1 attestation is read as
#: child-only, so a key registered before the state routes existed can never
#: sign a state write.
ATTESTATION_PURPOSE_V2 = "swarm-child-reg/v2"

#: What a registered key may sign for. A v1 registration is CHILD only.
PURPOSE_CHILD = "child"
PURPOSE_STATE = "state"
PURPOSES = (PURPOSE_CHILD, PURPOSE_STATE)

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


def registration_flags(purposes: tuple[str, ...] | list[str], unprotected: bool) -> str:
    """The v2 attestation's flags, one spelling: purposes in `PURPOSES` order."""
    listed = ",".join(p for p in PURPOSES if p in purposes)
    return f"purpose={listed};unprotected={1 if unprotected else 0}"


def attestation_v2(
    key: str, attempt: AttemptTuple, public_key: str, purposes: tuple[str, ...] | list[str],
    unprotected: bool,
) -> str:
    return _mac(
        key,
        ATTESTATION_PURPOSE_V2,
        f"{attempt.encode()}\n{registered_value(public_key, None)}\n"
        f"{registration_flags(purposes, unprotected)}",
    )


def stored_purposes(doc: dict) -> tuple[str, ...] | None:
    """The `purpose` list of a v2 registration, or None when it is not one.

    Unknown names, duplicates or a non-list make it None: a document a
    tenant identity rewrote is a denial, never a widened key.
    """
    raw = doc.get("purpose")
    if not isinstance(raw, list) or not raw:
        return None
    if any(p not in PURPOSES for p in raw) or len(set(raw)) != len(raw):
        return None
    return tuple(p for p in PURPOSES if p in raw)


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

    def attest_v2(
        self, attempt: AttemptTuple, public_key: str, purposes: tuple[str, ...], unprotected: bool
    ) -> str:
        return attestation_v2(self.current, attempt, public_key, purposes, unprotected)

    def purposes_of(self, attempt: AttemptTuple, doc: dict) -> frozenset[str]:
        """What a stored registration's key may sign for; empty when it is not trusted.

        A document carrying `purpose` is a v2 registration and is verified ONLY
        as one: adding the field to a v1 document breaks its attestation
        rather than widening it. A document without it is v1 and is CHILD
        only (§4.5). A tombstone may sign for nothing.
        """
        if not attempt.matches(doc):
            return frozenset()
        stored = doc.get("attestation")
        if not isinstance(stored, str) or not stored:
            return frozenset()
        if "purpose" not in doc and "version" not in doc:
            if self.attestation_holds(attempt, doc) and valid_public_key(doc.get("public_key")):
                return frozenset({PURPOSE_CHILD})
            return frozenset()
        purposes = stored_purposes(doc)
        public_key = doc.get("public_key")
        unprotected = doc.get("unprotected")
        if (
            purposes is None
            or doc.get("version") != 2
            or not valid_public_key(public_key)
            or not isinstance(unprotected, bool)
        ):
            return frozenset()
        ok = False
        for version in self._versions:
            ok |= hmac.compare_digest(
                attestation_v2(version, attempt, public_key, purposes, unprotected), stored
            )
        return frozenset(purposes) if ok else frozenset()


# --------------------------------------------------------------------------
# The attempt proof (§3.2 step 5)
# --------------------------------------------------------------------------


def request_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs for one request. The worker restates this; a test
    signs with the worker's code and verifies with this one."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{REQUEST_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def state_request_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs for one STATE request (§4.1): `request_message`'s
    shape under `swarm-state-req/v1`. The worker restates this; the tests sign
    with a restatement and verify with this one."""
    digest = hashlib.sha256(body).hexdigest()
    return (
        f"{STATE_REQUEST_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")
    )


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
