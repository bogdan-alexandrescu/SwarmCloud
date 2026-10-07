"""The git identity an agent commits under: the person who dispatched the task.

Owner decision 2026-10-06 (chunk-3 observer P37): every chunk-3 agent that
committed first failed `git commit` for want of an identity, then committed as
`swarm <swarm@localhost>`. The agent's environment now names the person.

FROM THE TASK DOCUMENT, NEVER FROM `input` (invariant 10). `submitted_by` is
the verified email of whoever submitted, and swarm-api records
`metadata.dispatch.git_identity = {"name", "email"}` at submission, from the
authenticated principal, when there is more to say than that address
(`swarm_api.gitidentity`). Both are inside the spec signature: `dispatch` is a
signed metadata key, which is why the record lives there. The record's EMAIL is used
only when it IS `submitted_by`, or when `submitted_by` is a service account (a
ci-fix continuation, whose record names the person behind the task it
continues). Otherwise the bare submitter is named, by the local part of their
address. Nothing a caller puts in `input` reaches this.

When no person can be named -- a service account's task with no usable record,
or a document with no submitter -- the commits name `BOT_GIT_IDENTITY`.

The worker's OWN commits (its auto-commit, the publish fold, the integrator's
merges) are unchanged: they are made under `WorkerConfig.git_author_*` with
`-c` keys and an environment of their own (`gitops._git_env`), which never
sees these variables.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from swarm_common.identity import SERVICE_ACCOUNT_EMAIL

#: The field of `metadata.dispatch` swarm-api writes
#: (`swarm_api.gitidentity.GIT_IDENTITY_FIELD`).
GIT_IDENTITY_FIELD = "git_identity"

#: Who commits when no person can be named. A GitHub no-reply address because
#: it reaches nobody and GitHub links it to no account, so a bot commit is
#: never attributed to a real user's profile. Equal to swarm-api's
#: `BOT_IDENTITY`, held so by tests/unit/worker/test_git_identity_env.py.
BOT_GIT_IDENTITY: tuple[str, str] = ("SwarmCloud", "swarmcloud@users.noreply.github.com")

#: The four variables git reads before any configuration, so an agent's
#: `git commit` succeeds with no `user.*` set and names the person.
GIT_IDENTITY_ENV: tuple[str, ...] = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)

NAME_MAX_CHARS = 100

#: git refuses `<`, `>` and newlines in an identity; control characters would
#: reach a terminal through `git log`. Re-checked here rather than trusted:
#: a document written before signing, inside the rollout window, is unsigned.
_UNSAFE = re.compile(r"[<>\x00-\x1f\x7f]")


def _clean_name(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return " ".join(_UNSAFE.sub(" ", raw).split())[:NAME_MAX_CHARS].strip()


def _is_service_account(email: str) -> bool:
    return bool(SERVICE_ACCOUNT_EMAIL.match(email)) or email.endswith(".gserviceaccount.com")


def _person(email: Any) -> str:
    """`email` lowered when it is a person's address, else ""."""
    if not isinstance(email, str):
        return ""
    email = email.strip().lower()
    if "@" not in email or _UNSAFE.search(email) or " " in email or _is_service_account(email):
        return ""
    return email


def commit_identity(task: Mapping[str, Any] | None) -> tuple[str, str]:
    """(name, email) the agent's commits carry, for this task document."""
    task = task or {}
    submitter = str(task.get("submitted_by") or "").strip().lower()
    metadata = task.get("metadata")
    block = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
    record = block.get(GIT_IDENTITY_FIELD) if isinstance(block, Mapping) else None
    if isinstance(record, Mapping):
        email = _person(record.get("email"))
        if email and (email == submitter or _is_service_account(submitter)):
            return _clean_name(record.get("name")) or email.split("@", 1)[0], email
    email = _person(submitter)
    if email:
        return email.split("@", 1)[0], email
    return BOT_GIT_IDENTITY


def git_identity_env(task: Mapping[str, Any] | None) -> dict[str, str]:
    """GIT_AUTHOR_* and GIT_COMMITTER_* for the agent's environment."""
    name, email = commit_identity(task)
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
