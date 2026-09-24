"""What this platform has already published into each worker-facing secret.

WHY THIS EXISTS AT ALL
----------------------
The broker holds `secretmanager.versions.add` on a base secret and NOT
`secretmanager.versions.access` -- in saga-agents-staging that is the custom
role `swarmSecretLister` plus a `secretVersionAdder` binding, with the
`secretAccessor` binding pointing at the WORKER's service account instead. So
the one question the still-valid publish path needs answered -- "does that
secret already hold this access token?" -- is a question Secret Manager will
not answer for this caller.

A failed read is not a negative answer. Treating it as one wrote 1,741
identical versions of `swarm-account-u-bogdan-devops-main` and 1,814 of
`swarm-tenant-u-bogdan-anthropic` between 2026-09-18 and 2026-09-22, one every
five minutes. So when the read is refused, the ONLY admissible evidence is a
positive record of what this platform put there, and that is what this module
keeps.

WHY IT IS NOT A DICT ON THE REFRESHER
-------------------------------------
It was, for one day, and the leak came back at a slower rate. `swarm-quota-broker`
runs on Cloud Run with `minScale=0` and is redeployed several times a day; an
in-process note is empty in every new process, so the "have I already published
this?" question read as "no" once per process and wrote one more identical
version. Measured after the in-process fix went live in revision 00039 at
00:17:50 on 2026-09-22:

    rev 00039 created 00:17:50 -> base-only write 00:20:06
    rev 00040 created 02:57:13 -> base-only write 03:00:16
    rev 00041 created 03:25:44 -> base-only write 03:30:03
    rev 00042 created 03:32:16 -> base-only write 03:35:03

Four deploys, four writes, and no base-only write anywhere between them -- the
5-minute sweep keeps the instance warm, so a new process only ever came from a
new revision. The record has to outlive the process or it is not a record.

WHAT IT STORES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
A SHA-256 digest of the access token, never the token. This collection is read
by anything holding `roles/datastore.user` on the project, which is a much
wider circle than the secret's accessor binding; a durable map of live access
tokens would hand that circle every credential the platform has. Equality is
the only operation the caller needs, and a digest supports exactly that and
nothing else.

"COULD NOT READ THE RECORD" IS ITS OWN ANSWER
---------------------------------------------
`published_digest` returns None for "this platform has never published here"
and raises `LedgerUnavailable` for "I could not find out". Collapsing those two
would rebuild the original defect one layer down: an unreachable Firestore
would read as "never published", and every tick would publish again.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

log = logging.getLogger(__name__)


def fingerprint(access_token: str) -> str:
    """The digest form of an access token, defined in exactly one place.

    Two writers put records in this ledger -- the refresher and account
    registration -- and a second spelling of this function is a ledger whose
    entries never match, which reads as "never published" forever and restores
    the write-per-tick behaviour with extra steps.
    """
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


#: One document per base secret, keyed by the secret's own name. Secret names
#: contain no `/`, so they are valid Firestore document ids as they stand and
#: need no encoding that a reader would then have to reverse.
COLLECTION = "credential_publications"


class LedgerUnavailable(RuntimeError):
    """The record could not be read.

    NOT "there is no record". The caller must treat this as no evidence in
    either direction and decline to write, which is the whole reason the two
    are different types.
    """


class PublishLedger(Protocol):
    def published_digest(self, base: str) -> str | None:
        """The digest last published into `base`, or None if never.

        Raises `LedgerUnavailable` -- and only `LedgerUnavailable` -- when it
        cannot tell.
        """

    def record(self, base: str, digest: str) -> None:
        """Note that `digest` is now the payload of `base`'s latest version.

        Called only AFTER `add_version` has returned. A record of a write that
        did not happen suppresses the write the secret actually needs.
        """


class InMemoryPublishLedger:
    """The degenerate ledger: correct within one process, forgotten on exit.

    Kept because the unit tests must run with no cloud credentials and no
    emulator, and because a deployment with no Firestore handle should still
    get the within-process deduplication rather than the original leak. It is
    not what production runs on -- see the module docstring for what an
    in-process note costs across redeploys.
    """

    def __init__(self) -> None:
        self._digests: dict[str, str] = {}

    def published_digest(self, base: str) -> str | None:
        return self._digests.get(base)

    def record(self, base: str, digest: str) -> None:
        self._digests[base] = digest


class FirestorePublishLedger:
    """The real one, on the database the broker already owns.

    One document per base secret rather than one document holding a map: two
    credentials refreshed in the same tick would otherwise be a read-modify-
    write race on a single document, and the loser's record would be dropped --
    which shows up as one extra identical secret version, i.e. exactly the
    symptom this exists to remove.
    """

    def __init__(self, db: Any, *, now: Any = None) -> None:
        self._db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def published_digest(self, base: str) -> str | None:
        try:
            snap = self._db.collection(COLLECTION).document(base).get()
        except Exception as exc:  # noqa: BLE001 -- re-raised as a distinct type
            raise LedgerUnavailable(type(exc).__name__) from exc
        if not getattr(snap, "exists", False):
            return None
        digest = (snap.to_dict() or {}).get("digest")
        # An empty or absent field is "never published", not a digest that
        # happens to compare unequal to everything: the caller's `is None`
        # branch is the one that publishes, and it should.
        return digest or None

    def record(self, base: str, digest: str) -> None:
        self._db.collection(COLLECTION).document(base).set(
            {
                "digest": digest,
                # For a human reading the collection after an incident. Nothing
                # reads it back, so its format is a human's convenience only.
                "updated_at": self._now().isoformat(),
                "secret": base,
            }
        )


__all__ = [
    "COLLECTION",
    "fingerprint",
    "FirestorePublishLedger",
    "InMemoryPublishLedger",
    "LedgerUnavailable",
    "PublishLedger",
]
