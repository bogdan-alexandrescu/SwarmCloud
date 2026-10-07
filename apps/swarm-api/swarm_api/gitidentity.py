"""Who an agent's commits name: the person who dispatched the work.

THE MEASUREMENT (chunk-3 observer P37, owner decision 2026-10-06). All seven
chunk-3 agents that committed failed their first `git commit` for want of an
identity, then committed as `swarm <swarm@localhost>` -- a name nobody holds.
A commit names the PERSON WHO DISPATCHED the task or workflow.

RECORDED AT SUBMISSION, FROM THE AUTHENTICATED PRINCIPAL ONLY (invariant 10).
The record lives INSIDE the `metadata.dispatch` block, as
`dispatch.git_identity = {"name", "email"}`, for the reason `pr_role` and
`pr_author` do: the spec signature covers `dispatch` whole
(`swarm_common.specsign.SIGNED_METADATA_KEYS`), every metadata key the worker
reads must be signed (tests/unit/common/test_specsign_covers.py), and a
caller cannot write `dispatch` at all (`reject_reserved_metadata`). A caller's
own `metadata.git_identity`, or any other body field naming an identity, is
read by nothing: ignored.

ABSENT WHEN THE SIGNED `submitted_by` ALREADY SAYS IT. The worker derives a
person's identity from `submitted_by` -- their address, named by its local
part -- so the record is written only when it says something more: a token's
`name` claim, or the person behind a service account's continuation. A task
submitted through IAP (whose assertion carries no name) stores exactly the
dispatch block it stored before.

THE NAME is the token's `name` claim when the token carries one (a Google ID
token minted with the `profile` scope does; an IAP assertion does not), else
the email's local part. No directory lookup is made for it: the owner ruled out
a new external call on the submission path.

WHO, WHEN NOBODY IS ON THE CALL.
  * A workflow's steps: the workflow's submitter, who is the caller.
  * A child task: its parent's record (`swarm_api.children`).
  * An issue run's steps and a merge step's CI-fix rounds are already submitted
    AS the person who created the run or the merge step (`run_owner_auth`,
    `mergewake`), so they record that person.
  * A ci-fix continuation submitted by a listed SERVICE ACCOUNT: the person
    recorded on the task it continues, else -- when that task records nobody a
    commit could name -- `BOT_IDENTITY`.

The worker reads this record from its task document and sets GIT_AUTHOR_* and
GIT_COMMITTER_* in the agent's environment (`agent_worker.lifecycle`).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from swarm_common.identity import SERVICE_ACCOUNT_EMAIL

from .validation import DISPATCH_METADATA_KEY

#: The field of the `metadata.dispatch` block the record lives under. The
#: worker spells it `agent_worker.gitidentity.GIT_IDENTITY_FIELD`.
GIT_IDENTITY_FIELD = "git_identity"

#: The identity a commit carries when no person can be named: a continuation a
#: service account submitted for a task that itself records no person. A
#: GitHub no-reply address because it is deliverable to nobody and GitHub
#: attributes it to no account, so a bot commit is never mistaken for -- or
#: linked to -- a real user's profile. Kept equal to the worker's copy
#: (`agent_worker.lifecycle.BOT_GIT_IDENTITY`) by a unit test.
BOT_NAME = "SwarmCloud"
BOT_EMAIL = "swarmcloud@users.noreply.github.com"
BOT_IDENTITY: dict[str, str] = {"name": BOT_NAME, "email": BOT_EMAIL}

#: Longer than any real display name, short enough that a hostile one cannot
#: bloat every task document it is copied onto.
NAME_MAX_CHARS = 100

#: git rejects `<`, `>` and newlines in an identity and strips surrounding
#: punctuation; control characters would reach a terminal through `git log`.
_UNSAFE = re.compile(r"[<>\x00-\x1f\x7f]")


def clean_name(raw: Any) -> str:
    """`raw` as a commit author name, or "" when nothing usable is left."""
    if not isinstance(raw, str):
        return ""
    text = " ".join(_UNSAFE.sub(" ", raw).split())
    return text[:NAME_MAX_CHARS].strip()


def is_service_account(email: str) -> bool:
    """A Google service account: never a person a commit should name."""
    email = (email or "").strip().lower()
    return bool(SERVICE_ACCOUNT_EMAIL.match(email)) or email.endswith(".gserviceaccount.com")


def identity_for(email: str, name: Any = "") -> dict[str, str] | None:
    """The record for `email`, named `name` or the email's local part.

    None for an address that is not a person's -- empty, malformed, or a
    service account -- so the caller decides the fallback.
    """
    email = (email or "").strip().lower()
    if "@" not in email or _UNSAFE.search(email) or is_service_account(email):
        return None
    return {"name": clean_name(name) or email.split("@", 1)[0], "email": email}


def recorded(metadata: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The record a stored task's dispatch block carries, when it is usable."""
    block = metadata.get(DISPATCH_METADATA_KEY) if isinstance(metadata, Mapping) else None
    raw = block.get(GIT_IDENTITY_FIELD) if isinstance(block, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    return identity_for(str(raw.get("email") or ""), raw.get("name"))


def from_task(metadata: Mapping[str, Any] | None, submitted_by: str) -> dict[str, str] | None:
    """The person a stored task names: its record, else its human submitter.

    The record is taken when it names the task's own submitter, or when that
    submitter is a service account -- a continuation's record names the person
    behind the task it continued, which is the point of carrying it on.
    """
    submitter = (submitted_by or "").strip().lower()
    found = recorded(metadata)
    if found is not None and (found["email"] == submitter or is_service_account(submitter)):
        return found
    return identity_for(submitted_by)


def for_caller(email: str, display_name: str = "") -> dict[str, str]:
    """The authenticated caller's record, or the bot's for a service account."""
    return identity_for(email, display_name) or dict(BOT_IDENTITY)


def for_continuation(
    caller_email: str,
    display_name: str,
    continued: Any | None,
) -> dict[str, str]:
    """Who a continuation's commits name.

    A PERSON continuing a task is the submitter, like any caller. A SERVICE
    ACCOUNT (the ci-fix account, contract request 30) is nobody: its commits
    name the person the continued task names, else the bot.
    """
    own = identity_for(caller_email, display_name)
    if own is not None:
        return own
    if continued is not None:
        found = from_task(getattr(continued, "metadata", None), getattr(continued, "submitted_by", ""))
        if found is not None:
            return found
    return dict(BOT_IDENTITY)


def record_on(
    dispatch_block: dict[str, Any], identity: Mapping[str, str], submitted_by: str
) -> None:
    """Write the record into the dispatch block, unless `submitted_by` says it.

    What the worker derives with no record is `identity_for(submitted_by)`, or
    the bot for a service account; a record equal to that is left out, so the
    block is unchanged for every task whose submitter's address is all there
    is to say.
    """
    derived = identity_for(submitted_by) or BOT_IDENTITY
    wanted = {"name": identity["name"], "email": identity["email"]}
    if wanted != derived:
        dispatch_block[GIT_IDENTITY_FIELD] = wanted
