"""The git identity an agent commits under: the dispatcher's GitHub account, else the bot.

Owner decision 2026-10-06 (chunk-3 observer P37): every chunk-3 agent that
committed first failed `git commit` for want of an identity, then committed as
`swarm <swarm@localhost>`. The agent's environment now names the person.

NEVER AN EMAIL ADDRESS OF THEIR OWN (owner hard rule, 2026-11-07). Commits
used to carry the submitter's address; GitHub refused every push that would
publish a private one (GH007), and the owner ruled that no commit may carry an
address identifying a person or a company. So the record's email is used ONLY
when it is a GitHub no-reply address -- swarm-api writes `<login>
<id+login@users.noreply.github.com>` for a person whose GitHub account is
connected (`swarm_api.gitidentity`) -- and everything else commits as
`BOT_GIT_IDENTITY`. `submitted_by` is never a commit email.

FROM THE TASK DOCUMENT, NEVER FROM `input` (invariant 10). The record is
`metadata.dispatch.git_identity = {"name", "email"}`, inside the spec
signature (`dispatch` is a signed metadata key). Nothing a caller puts in
`input`, or in a metadata key of its own, reaches this.

The worker's OWN commits (its auto-commit, the publish fold, the integrator's
merges) are made as `Worker._publish_identity`: this identity, or
`WorkerConfig.git_author_*` for the bot, which `WorkerConfig` refuses unless
its email is a no-reply address too (`is_noreply`). They carry them as `-c`
keys in an environment of their own (`gitops._git_env`).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

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

#: What every address a commit may carry ends with: GitHub's no-reply domain
#: (`swarm_api.gitidentity.NOREPLY_SUFFIX`).
NOREPLY_SUFFIX = "@users.noreply.github.com"

#: `<id>+<login>@` (accounts since 2017) or `<login>@`, a login being letters,
#: digits and hyphens, not starting with a hyphen, at most 39 characters.
_NOREPLY = re.compile(
    r"(?:[0-9]{1,20}\+)?([A-Za-z0-9][A-Za-z0-9-]{0,38})" + re.escape(NOREPLY_SUFFIX), re.I
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


def is_noreply(email: Any) -> bool:
    """`email` is a GitHub no-reply address, and nothing else."""
    return isinstance(email, str) and _NOREPLY.fullmatch(email) is not None


def commit_identity(task: Mapping[str, Any] | None) -> tuple[str, str]:
    """(name, email) the agent's commits carry, for this task document."""
    task = task or {}
    metadata = task.get("metadata")
    block = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
    record = block.get(GIT_IDENTITY_FIELD) if isinstance(block, Mapping) else None
    if isinstance(record, Mapping) and isinstance(record.get("email"), str):
        email = record["email"].strip()
        found = _NOREPLY.fullmatch(email)
        if found is not None:
            return _clean_name(record.get("name")) or found.group(1), email
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
