"""Who an agent's commits name: the dispatcher's GitHub account, else the bot.

THE MEASUREMENT (chunk-3 observer P37, owner decision 2026-10-06). All seven
chunk-3 agents that committed failed their first `git commit` for want of an
identity, then committed as `swarm <swarm@localhost>` -- a name nobody holds.
A commit names the PERSON WHO DISPATCHED the task or workflow.

NEVER AN EMAIL ADDRESS OF THEIR OWN (owner hard rule, 2026-11-07). Commits
used to carry the submitter's address; GitHub then refused every push that
would publish a private one (GH007), and the owner ruled that no commit may
carry an address identifying a person or a company, while agent commits still
use the person's GitHub username. So a person is named ONLY through the
GitHub account they connected in the workspace -- the `forge_connections`
record `forgeapp.ForgeApp` writes from GitHub's own `GET /user` at the OAuth
exchange, which the console shows as "Connected as @<login>":
`{"name": <login>, "email": "<id>+<login>@users.noreply.github.com"}`, the
no-reply address GitHub itself attributes to that account. Anyone else --
no connection, a revoked or malformed one, a service account -- commits as
`BOT_IDENTITY`.

RECORDED AT SUBMISSION, FROM THE AUTHENTICATED PRINCIPAL ONLY (invariant 10).
The record lives INSIDE the `metadata.dispatch` block, as
`dispatch.git_identity = {"name", "email"}`, for the reason `pr_role` and
`pr_author` do: the spec signature covers `dispatch` whole
(`swarm_common.specsign.SIGNED_METADATA_KEYS`), every metadata key the worker
reads must be signed (tests/unit/common/test_specsign_covers.py), and a
caller cannot write `dispatch` at all (`reject_reserved_metadata`). A caller's
own `metadata.git_identity`, or any other body field naming an identity, is
read by nothing: ignored. The connection is looked up by the VERIFIED
caller's address in the submission's own tenant (`connected_github`).

ABSENT FOR THE BOT. The worker reads a missing record, or one whose email is
not a GitHub no-reply address, as the bot, so the bot is never written.

WHO, WHEN NOBODY IS ON THE CALL.
  * A workflow's steps: the workflow's submitter, who is the caller.
  * A child task: its parent's record (`swarm_api.children`).
  * An issue run's steps and a merge step's CI-fix rounds are already submitted
    AS the person who created the run or the merge step (`run_owner_auth`,
    `mergewake`), so they record that person's connected account.
  * A ci-fix continuation submitted by a listed SERVICE ACCOUNT: the record on
    the task it continues, else `BOT_IDENTITY`.

The worker reads this record from its task document and sets GIT_AUTHOR_* and
GIT_COMMITTER_* in the agent's environment (`agent_worker.gitidentity`).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from swarm_common.identity import SERVICE_ACCOUNT_EMAIL

from .validation import DISPATCH_METADATA_KEY

log = logging.getLogger(__name__)

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


#: What every address a commit may carry ends with: GitHub's no-reply domain.
#: The worker spells it `agent_worker.gitidentity.NOREPLY_SUFFIX`.
NOREPLY_SUFFIX = "@users.noreply.github.com"

#: A GitHub login: letters, digits and hyphens, not starting with a hyphen, at
#: most 39 characters. Looser than GitHub's sign-up rule on hyphens, which
#: older accounts predate.
_LOGIN = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"

#: A no-reply address: `<id>+<login>@` (accounts since 2017) or `<login>@`.
_NOREPLY = re.compile(rf"(?:[0-9]{{1,20}}\+)?({_LOGIN})" + re.escape(NOREPLY_SUFFIX), re.I)


def is_noreply(email: Any) -> bool:
    """`email` is a GitHub no-reply address, and nothing else."""
    return isinstance(email, str) and _NOREPLY.fullmatch(email) is not None


def github_identity(login: Any, user_id: Any) -> dict[str, str] | None:
    """`<login> <id+login@users.noreply.github.com>`, or None when either is unusable."""
    if not isinstance(login, str) or re.fullmatch(_LOGIN, login) is None:
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None
    return {"name": login, "email": f"{user_id}+{login}{NOREPLY_SUFFIX}"}


def connected_github(db: Any, tenant_id: str, email: str) -> dict[str, str] | None:
    """The GitHub identity of the account `email` connected in `tenant_id`.

    Read from `forge_connections/<connection_id_for(tenant, email)>`: its
    `forge_login` and `forge_user_id` are GitHub's own answer to `GET /user`
    with the person's token. None for a service account, for no connection,
    a revoked one, one recorded for another tenant or person, or a read that
    fails -- the bot then commits, which exposes nobody.
    """
    # Lazy: forgeapp imports modules that import this one.
    from .forgeapp import CONNECTIONS, REVOKED, connection_id_for

    key = (email or "").strip().lower()
    if db is None or not tenant_id or "@" not in key or is_service_account(key):
        return None
    try:
        snap = db.collection(CONNECTIONS).document(connection_id_for(tenant_id, key)).get()
        doc = snap.to_dict() if snap.exists else None
    except Exception as exc:
        log.warning("github connection unread for the commit identity tenant=%s (%s); "
                    "the bot commits", tenant_id, type(exc).__name__)
        return None
    if not isinstance(doc, Mapping) or doc.get("state") == REVOKED:
        return None
    if doc.get("tenant_id") != tenant_id or str(doc.get("user") or "").lower() != key:
        return None
    return github_identity(doc.get("forge_login"), doc.get("forge_user_id"))


def _usable(raw: Any) -> dict[str, str] | None:
    """`raw` as a record, when its email is a GitHub no-reply address."""
    if not isinstance(raw, Mapping):
        return None
    email = raw.get("email")
    if not isinstance(email, str):
        return None
    email = email.strip()
    found = _NOREPLY.fullmatch(email)
    if found is None:
        return None
    return {"name": clean_name(raw.get("name")) or found.group(1), "email": email}


def recorded(metadata: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The record a stored task's dispatch block carries, when it is usable."""
    block = metadata.get(DISPATCH_METADATA_KEY) if isinstance(metadata, Mapping) else None
    return _usable(block.get(GIT_IDENTITY_FIELD) if isinstance(block, Mapping) else None)


def from_task(metadata: Mapping[str, Any] | None, submitted_by: str = "") -> dict[str, str] | None:
    """The GitHub identity a stored task names, or None for the bot.

    The record alone decides: it is inside the signed dispatch block, and it is
    only ever a no-reply address. `submitted_by` is never a commit email; it is
    taken so every caller reads a task the same way.
    """
    del submitted_by
    return recorded(metadata)


def for_caller(email: str, github: Mapping[str, str] | None = None) -> dict[str, str]:
    """The authenticated caller's record: their connected GitHub account, else the bot."""
    if is_service_account(email):
        return dict(BOT_IDENTITY)
    return _usable(github) or dict(BOT_IDENTITY)


def for_continuation(
    caller_email: str,
    continued: Any | None,
    github: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Who a continuation's commits name.

    A PERSON continuing a task is the submitter, like any caller. A SERVICE
    ACCOUNT (the ci-fix account, contract request 30) is nobody: its commits
    name the GitHub account the continued task records, else the bot.
    """
    if not is_service_account(caller_email):
        return for_caller(caller_email, github)
    if continued is not None:
        found = from_task(getattr(continued, "metadata", None))
        if found is not None:
            return found
    return dict(BOT_IDENTITY)


def record_on(dispatch_block: dict[str, Any], identity: Mapping[str, str]) -> None:
    """Write the record into the dispatch block, unless it is the bot.

    The worker reads no record as the bot, so the block is unchanged for every
    task that names no GitHub account. Only a no-reply address is written.
    """
    wanted = _usable(identity)
    if wanted is not None and wanted != BOT_IDENTITY:
        dispatch_block[GIT_IDENTITY_FIELD] = wanted
