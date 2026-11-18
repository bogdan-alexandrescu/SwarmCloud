"""The display name: the rules a name must pass, its claim, and the lookup.

docs/design/user-profile.md §4.4 (Q5, Q12). A display name is a LABEL a person
chooses on Profile and others see beside the person's email. It is never an
identity: every check that decides what a person may do keeps using the
verified email, and nothing looks a person up by name.

THE RULES (`check`, rules 1-5, then `DisplayNames.ensure_free` and the
transactional claim, rule 6):

  1. normalised first: NFC, surrounding spaces stripped, runs of spaces
     collapsed. The stored name is the normalised one;
  2. 1 to 64 characters, a stricter subset of `gitidentity.NAME_MAX_CHARS`, so
     a stored name is always a valid commit name (Q11);
  3. only letters, combining marks, spaces and `.'-`: no digit, no `@` (so no
     name embeds an email), no `<>` or brackets (no role tag), no format
     character (a bidi override or a zero-width joiner renders differently
     from its bytes);
  4. one script, by `unicodedata.name`'s leading word, which is how "Ada" and
     a Cyrillic-`а` "Аda" stop looking identical without a confusables table;
  5. no RESERVED word, after folding (`display_name_reserved`);
  6. not another person's folded name, and not containing one as a whole-word
     run WHEN THAT NAME HAS TWO OR MORE WORDS (Q12): "Ada Lovelace Admin" is
     refused beside "Ada Lovelace", "Bob Smith" is allowed beside "Bob"
     (`display_name_taken`). Platform-wide, because listing a group's members
     is the Cloud Identity call this project cannot make.

THE CLAIM. Each stored name holds `display_name_claims/dn_<32 hex>`, whose id
is HMAC-SHA256(swarm-child-key, "user-name:" + folded). A worker cannot compute
that id, so a claim is trusted only when its id IS the HMAC of its own `folded`
field: a document a tenant identity plants there is ignored by both halves of
rule 6. A claim whose owner's preferences no longer hold the name is free, which
is also how the claim of a document lost to a key rotation is released.

THE AUDIT. Every change of name, the first one and a clearing included, writes
one `admin_audit` entry in the preferences save's own transaction, so Admin ›
People shows every person's name history with no new route.

Nothing here logs a name.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Iterable

from swarm_common.admission import _snapshot

from .admins import AUDIT_COLLECTION
from .errors import DisplayNameInvalid, DisplayNameReserved, DisplayNameTaken

if TYPE_CHECKING:
    from .preferences import Preferences

log = logging.getLogger(__name__)

#: Where claims live. Only swarm-api writes it, inside the preferences save.
CLAIMS = "display_name_claims"
#: The claim id's message prefix. No other derivation's message begins with it.
CLAIM_PURPOSE = "user-name:"
CLAIM_PREFIX = "dn_"
#: Hex digits of the HMAC kept in an id: 128 bits, which no one guesses.
ID_HEX = 32

#: Rule 2. Long enough for any real name; short enough that `PersonName` can
#: always fit the email beside it. Below `gitidentity.NAME_MAX_CHARS` (100).
MAX_CHARS = 64

#: Rule 5: words that would make a person's name read as a platform role or as
#: `gitidentity.BOT_NAME`. Compared against FOLDED words.
RESERVED = frozenset(
    {"admin", "administrator", "owner", "swarmcloud", "system", "bot", "support"}
)

#: Rule 3's punctuation, and what folding turns into spaces.
PUNCTUATION = ".'-"

#: How long a looked-up name is served before it is read again, per instance.
NAME_CACHE_SECONDS = 60.0

#: What `display_name_changed` audit entries are called.
AUDIT_ACTION = "display_name_changed"

_SPACES = re.compile(r" +")
_OWNER = re.compile(r"^up_[0-9a-f]{32}$")


def hmac_id(key: str, message: str) -> str:
    """The first ID_HEX hex digits of HMAC-SHA256(key, message)."""
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()[
        :ID_HEX
    ]


def claim_id(key: str, folded: str) -> str:
    """`dn_` + 32 hex of HMAC(swarm-child-key, "user-name:" + folded)."""
    return CLAIM_PREFIX + hmac_id(key, CLAIM_PURPOSE + folded)


def normalise(raw: str) -> str:
    """Rule 1: NFC, surrounding spaces stripped, runs of spaces collapsed."""
    return _SPACES.sub(" ", unicodedata.normalize("NFC", raw).strip(" "))


def fold(name: str) -> str:
    """The form names are compared in: NFKD, combining marks dropped,
    case-folded, `.'-` turned into spaces, spaces collapsed.

    "ADA  lovelace", "Adá Lovelace" and "Ada Lovelace" all fold to
    "ada lovelace"."""
    decomposed = unicodedata.normalize("NFKD", name)
    bare = "".join(ch for ch in decomposed if not unicodedata.category(ch).startswith("M"))
    text = bare.casefold()
    for mark in PUNCTUATION:
        text = text.replace(mark, " ")
    return _SPACES.sub(" ", text).strip(" ")


def _script(ch: str) -> str:
    """A letter's script, as `unicodedata.name`'s leading word: LATIN, CYRILLIC..."""
    return unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0]


def check(raw: Any) -> str:
    """Rules 1-5. The normalised name, or the refusal of the first rule broken.

    Each message names the rule and nothing else. Rule 6 needs the other
    people's names and is `DisplayNames.ensure_free` and the save's
    transaction."""
    if not isinstance(raw, str):
        raise DisplayNameInvalid("A display name is text, or null to have none.")
    name = normalise(raw)
    if not name or len(name) > MAX_CHARS:
        raise DisplayNameInvalid(f"A display name is 1 to {MAX_CHARS} characters long.")
    scripts: set[str] = set()
    for ch in name:
        category = unicodedata.category(ch)
        if category.startswith("L"):
            scripts.add(_script(ch))
        elif category.startswith("M") or ch == " " or ch in PUNCTUATION:
            continue
        else:
            raise DisplayNameInvalid(
                "A display name may hold only letters, spaces and . ' - "
                "(no digits, no @, no other symbols or invisible characters)."
            )
    if not scripts:
        # Punctuation alone folds to nothing, and a name that folds to nothing
        # names nobody and could never hold a claim.
        raise DisplayNameInvalid("A display name needs at least one letter.")
    if len(scripts) > 1:
        raise DisplayNameInvalid(
            "A display name's letters are all from one script (Latin, Cyrillic, "
            "Greek...), so it cannot be made to look like another name."
        )
    if any(word in RESERVED for word in fold(name).split(" ")):
        raise DisplayNameReserved(
            "A display name may not use a word that reads as a platform role: "
            + ", ".join(sorted(RESERVED))
            + "."
        )
    return name


def collides(folded: str, other: str) -> bool:
    """Rule 6 between two folded names: equal, or `folded` holds `other` as a
    whole-word run and `other` has two or more words (Q12 (b))."""
    if not folded or not other:
        return False
    if folded == other:
        return True
    if " " not in other:
        return False
    return f" {other} " in f" {folded} "


def taken_message() -> str:
    """Never names the person collided with: the refusal is about the rule."""
    return (
        "That display name is taken: it matches another person's name, or "
        "contains one. Choose another."
    )


@dataclass
class ClaimState:
    """What the save's transaction read about claims, for `write_in`."""

    new_ref: Any = None
    deletes: list[Any] = field(default_factory=list)


class DisplayNames:
    """The claim reads and writes of a save, and the name lookup.

    `preferences` derives the preference ids and reads the documents; this
    object never derives a preference id itself, so there is one derivation."""

    def __init__(
        self,
        db: Any,
        preferences: "Preferences",
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = NAME_CACHE_SECONDS,
    ) -> None:
        self._db = db
        self._preferences = preferences
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str | None]] = {}

    @property
    def _keys(self) -> tuple[str, ...]:
        return self._preferences.keys

    def _claims(self) -> Any:
        return self._db.collection(CLAIMS)

    def genuine(self, snap_id: str, doc: dict[str, Any]) -> bool:
        """Is this claim's id the HMAC of its own `folded`, under any version?

        Every version compared, so the time taken says nothing about which."""
        folded = doc.get("folded")
        if not isinstance(folded, str) or not folded or not isinstance(snap_id, str):
            return False
        ok = False
        for key in self._keys:
            ok |= hmac.compare_digest(claim_id(key, folded), snap_id)
        return ok and isinstance(doc.get("owner"), str) and bool(_OWNER.match(doc["owner"]))

    # -- rule 6, before the transaction -----------------------------------

    def ensure_free(self, folded: str, caller_ids: Iterable[str]) -> None:
        """Refuse `folded` when a genuine claim of another person's equals it
        or is contained in it (`collides`), and that person still holds it.

        Streams the claims (a few hundred), so it runs before the save's
        transaction; the transaction re-checks the exact match. Two saves
        racing to contain each other in the same second is a race the design
        accepts: the audit names both and the email is beside both."""
        mine = set(caller_ids)
        candidates: list[tuple[str, str]] = []
        for snap in self._claims().stream():
            doc = snap.to_dict() or {}
            if not self.genuine(snap.id, doc) or doc["owner"] in mine:
                continue
            if collides(folded, doc["folded"]):
                candidates.append((doc["owner"], doc["folded"]))
        if not candidates:
            return
        refs = [self._preferences.ref(owner) for owner, _ in candidates]
        held = {
            snap.id: (snap.to_dict() or {}).get("display_name_folded")
            for snap in self._db.get_all(refs)
            if snap.exists
        }
        if any(held.get(owner) == other for owner, other in candidates):
            raise DisplayNameTaken(taken_message())

    # -- inside the save's transaction -------------------------------------

    def read_in(
        self,
        txn: Any,
        *,
        caller_ids: list[str],
        old_folded: str | None,
        new_folded: str | None,
    ) -> ClaimState:
        """Every claim read the save needs. Reads only, as a transaction
        requires before its first write.

        Refuses a new name whose claim (under any key version) is another
        person's who still holds it; takes over one whose owner does not."""
        mine = set(caller_ids)
        state = ClaimState()
        if new_folded:
            state.new_ref = self._claims().document(claim_id(self._keys[0], new_folded))
            for key in self._keys:
                ref = self._claims().document(claim_id(key, new_folded))
                snap = _snapshot(txn.get(ref))
                if not snap.exists:
                    continue
                doc = snap.to_dict() or {}
                owner = doc.get("owner")
                if (
                    new_folded != old_folded
                    and doc.get("folded") == new_folded
                    and isinstance(owner, str)
                    and _OWNER.match(owner)
                    and owner not in mine
                ):
                    held = _snapshot(txn.get(self._preferences.ref(owner)))
                    if held.exists and (held.to_dict() or {}).get(
                        "display_name_folded"
                    ) == new_folded:
                        raise DisplayNameTaken(taken_message())
                if ref.path != state.new_ref.path:
                    state.deletes.append(ref)
        if old_folded and old_folded != new_folded:
            for key in self._keys:
                ref = self._claims().document(claim_id(key, old_folded))
                snap = _snapshot(txn.get(ref))
                if snap.exists and (snap.to_dict() or {}).get("owner") in mine:
                    state.deletes.append(ref)
        return state

    def write_in(
        self,
        txn: Any,
        state: ClaimState,
        *,
        owner: str,
        new_folded: str | None,
        by: str,
        old_name: str | None,
        new_name: str | None,
        at: datetime,
    ) -> None:
        """The claim writes, and the audit entry when the name changed."""
        if state.new_ref is not None and new_folded:
            txn.set(state.new_ref, {"owner": owner, "folded": new_folded, "at": at})
        for ref in state.deletes:
            txn.delete(ref)
        if old_name != new_name:
            txn.set(
                self._db.collection(AUDIT_COLLECTION).document(),
                {
                    "action": AUDIT_ACTION,
                    "target_workspace_id": None,
                    "by": by,
                    "at": at,
                    "detail": {"from": old_name, "to": new_name},
                },
            )

    # -- the lookup ----------------------------------------------------------

    def names_for(self, emails: Iterable[str]) -> dict[str, str | None]:
        """Each email's `display_name`, or None. Nothing else from the
        documents is served.

        One `get_all` for the emails not cached in the last 60 s. A failed read
        serves every name as None, so a page shows emails as today and never
        fails because a name could not be read."""
        wanted = list(dict.fromkeys(e for e in emails if isinstance(e, str) and e))
        now = self._clock()
        out: dict[str, str | None] = {}
        missing: list[str] = []
        with self._lock:
            for email in wanted:
                hit = self._cache.get(email.lower())
                if hit is not None and now - hit[0] < self._ttl:
                    out[email] = hit[1]
                else:
                    missing.append(email)
        if not missing:
            return out
        try:
            read = self._preferences.read_many(missing)
        except Exception:  # noqa: BLE001 - any failure: names are null, the page is served
            log.warning("display name lookup failed count=%d", len(missing))
            return {email: None for email in wanted}
        with self._lock:
            for email in missing:
                stored = read.get(email)
                name = stored.display_name if stored is not None else None
                out[email] = name
                self._cache[email.lower()] = (now, name)
        return out

    def forget(self, email: str) -> None:
        """Drop one email's cached name, after its owner saved."""
        with self._lock:
            self._cache.pop(email.lower(), None)
