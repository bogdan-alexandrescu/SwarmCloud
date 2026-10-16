"""The workspace-secret grant attestation (SEC-GRANT-ATTEST, part of #1347).

Design: docs/design/workspace-secrets.md §2.2 and "What SEC-GRANT-ATTEST
built"; owner decision 2026-10-15, answering SEC-RELEASE's "Attest grants".

A grant (`workspace_secret_grants/{secret_id}~{consumer}`) is a document a
tenant's agent can write (docs/multi-tenancy.md, "The Firestore row, in
full"). swarm-api therefore MACs every grant it writes, and the release drops
every grant whose MAC does not verify, so an agent that writes Firestore
cannot widen what is released to it.

THE ONLY PLACE THE CANONICAL FORM IS STATED. `canonical()` below; a test
greps that no other module builds it. The form is a JSON array, fixed order,
no whitespace, ASCII only:

    ["v1", secret_id, tenant_id, consumer, slot or null, enabled]

Every field the release decides on is in it: the secret, the workspace the
grant is queried by, the consumer it is matched against, the slot it is
delivered into, and the checkbox. `"v1"` leads so the form can change: a new
form gets a new version and its own branch in `_FORMS`, and an attestation
names the version it was made under (`v1.<base64url MAC>`).

THE KEY is `swarm-workspace-grant-key` (SWARM_WORKSPACE_GRANT_KEY), held by
swarm-api alone, read as a ring with the previous version during a rotation
(SWARM_WORKSPACE_GRANT_KEY_PREVIOUS), as `swarm-child-key` and
`swarm-live-key` are: signing uses the current key, verifying accepts either,
so a rotation invalidates no grant mid-life. Nothing here logs, raises or
returns a key or a MAC, other than the one `attestation` string `sign`
returns for the grant document.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: The grant document's field this module writes and reads.
ATTESTATION_FIELD = "attestation"
#: The canonical form's current version. Also the attestation's prefix.
FORM_VERSION = "v1"
#: Log codes. A grant dropped for its attestation is logged with the first; an
#: unattested grant released while `grant_attestation_required` is off, with
#: the second. Neither line carries a MAC or a key.
INVALID = "grant_attestation_invalid"
MISSING = "grant_attestation_missing"

#: Outcomes of `GrantKeys.check`.
VALID = "valid"

#: A MAC is SHA-256: 32 bytes, 43 base64url characters unpadded.
_MAC_BYTES = hashlib.sha256().digest_size


class NotCanonical(ValueError):
    """A grant whose fields cannot be put in the canonical form (a wrong type
    or a missing field). It is never attested, and never verifies."""


def canonical(secret_id: Any, tenant_id: Any, consumer: Any, slot: Any, enabled: Any) -> bytes:
    """The bytes a grant's attestation is a MAC of. THE ONLY STATEMENT OF THE FORM."""
    for value in (secret_id, tenant_id, consumer):
        if not isinstance(value, str) or not value:
            raise NotCanonical("a grant names its secret, workspace and consumer as text")
    if slot is not None and (not isinstance(slot, str) or not slot):
        raise NotCanonical("a grant's slot is text or absent")
    if not isinstance(enabled, bool):
        raise NotCanonical("a grant's enabled is a boolean")
    form = [FORM_VERSION, secret_id, tenant_id, consumer, slot, enabled]
    return json.dumps(form, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_of(grant: Mapping[str, Any]) -> bytes:
    """`canonical` over a grant document's own fields."""
    return canonical(grant.get("secret_id"), grant.get("tenant_id"), grant.get("consumer"),
                     grant.get("slot"), grant.get("enabled"))


_FORMS = {FORM_VERSION: canonical_of}


def _mac(key: str, message: bytes) -> bytes:
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class GrantKeys:
    """The ring: the current key, which signs, and the previous one, which
    only verifies. Excluded from the repr, so a logged object carries neither."""

    current: str = field(default="", repr=False)
    previous: str = field(default="", repr=False)

    @classmethod
    def from_settings(cls, settings: Any) -> "GrantKeys":
        return cls(current=str(getattr(settings, "workspace_grant_key", "") or ""),
                   previous=str(getattr(settings, "workspace_grant_key_previous", "") or ""))

    @property
    def configured(self) -> bool:
        return bool(self.current)

    def sign(self, grant: Mapping[str, Any]) -> str | None:
        """The `attestation` for `grant`, or None when this deployment has no
        key (the grant is then written unattested; see `GrantPolicy`)."""
        if not self.current:
            return None
        return f"{FORM_VERSION}.{_b64(_mac(self.current, canonical_of(grant)))}"

    def check(self, grant: Mapping[str, Any]) -> str:
        """VALID, MISSING (no attestation at all) or INVALID (present and
        malformed, made for other fields, or by neither ring key)."""
        presented = grant.get(ATTESTATION_FIELD)
        if presented is None:
            return MISSING
        if not isinstance(presented, str) or presented.count(".") != 1:
            return INVALID
        version, encoded = presented.split(".")
        form = _FORMS.get(version)
        if form is None or len(encoded) != 43:
            return INVALID
        try:
            mac = base64.urlsafe_b64decode(encoded + "=")
            message = form(grant)
        except (binascii.Error, ValueError):
            return INVALID
        if len(mac) != _MAC_BYTES:
            return INVALID
        # Every ring key is compared, in constant time each, whatever the first said.
        verified = [hmac.compare_digest(_mac(key, message), mac)
                    for key in (self.current, self.previous) if key]
        return VALID if any(verified) else INVALID


@dataclass(frozen=True)
class GrantPolicy:
    """What the release does with a grant's attestation.

    Present and wrong: ALWAYS dropped, whatever `required` says; the only way
    to make one is to alter a grant swarm-api signed. Missing: dropped when
    `required` (ApiSettings.grant_attestation_required), released with a
    MISSING warning while it is off, which is only the migration window before
    the attest route has re-signed the grants written before this lane.
    A grant's fields are tenant-written text, so they are logged with %r: a
    newline in one cannot forge a log line."""

    keys: GrantKeys
    required: bool

    @classmethod
    def from_settings(cls, settings: Any) -> "GrantPolicy":
        return cls(keys=GrantKeys.from_settings(settings),
                   required=bool(getattr(settings, "grant_attestation_required", False)))

    def admits(self, grant: Mapping[str, Any], grant_id: str) -> bool:
        outcome = self.keys.check(grant)
        if outcome == VALID:
            return True
        if outcome == MISSING and not self.required:
            log.warning("%s grant=%r tenant=%r secret=%r: released unattested while "
                        "grant_attestation_required is off", MISSING, grant_id,
                        grant.get("tenant_id"), grant.get("secret_id"))
            return True
        log.warning("%s grant=%r tenant=%r secret=%r: dropped (%s)", INVALID, grant_id,
                    grant.get("tenant_id"), grant.get("secret_id"),
                    "no attestation" if outcome == MISSING else "attestation did not verify")
        return False
