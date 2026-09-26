"""Which repository and ref a dispatch clones, when the caller did not say.

WHY THIS EXISTS (owner decision, 2026-09-26). A Claude Code workflow step can
now run in SwarmCloud instead of locally (`sc:remote`, `/sc:run`). A local step
works on the checkout in front of it without being told which one; a remote
step that had to be handed a repository URL and a branch by every caller would
make "run this step remotely" a different, fussier act than "run this step".
So `swarm_dispatch` and `swarm_workflow` infer both from the git checkout the
bridge runs in, when the caller names neither.

WHY IT REFUSES RATHER THAN GUESSES. A remote agent gets a fresh
`--depth 1 --single-branch` clone of what is on the remote
(`agent_worker/gitops.py`). A branch that exists only here, or a branch whose
last commits were never pushed, is a branch the agent cannot see -- and it does
not fail when it cannot see them: it works on the older tip, succeeds, and hands
back a patch against the wrong base. That is the failure the delegate skill's
clean-checkout rule describes, and the inference is exactly where it would
otherwise be reintroduced silently. So:

  * a detached HEAD, a branch with no upstream, or a branch AHEAD of its
    upstream is refused, and the refusal names the command that fixes it;
  * uncommitted changes are not refused -- they are often unrelated -- but the
    reply says, with a count, that the remote agent will not see them;
  * a branch BEHIND its upstream is not refused either: the agent sees more
    than this checkout, not less, and the reply says so.

"Pushed" is judged against this checkout's REMOTE-TRACKING ref, which is as
fresh as the last fetch or push. Nothing here fetches: a network call that can
prompt for a password inside an MCP server would hang the tool, and a push
updates the tracking ref anyway, which is the case that matters.

THE URL IS REWRITTEN TO HTTPS AND STRIPPED OF CREDENTIALS. The worker
authenticates a clone with the tenant's forge token as an HTTPS credential
(`gitops._write_credentials`) and runs ssh with `BatchMode=yes` and no key, so
an `git@host:owner/name` remote clones nothing. And a remote URL can carry a
token (`https://x-access-token:<token>@github.com/...`); it is removed before
the URL goes anywhere, because the dispatch reply echoes it and the task
document stores it.

NEVER A NETWORK CALL, NEVER A WRITE. Every command below reads local git state.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .client import SwarmError

#: Overrides the directory the inference reads. The default is the directory
#: the MCP server was started in, which Claude Code sets to the session's
#: working directory; a session that later moved into a worktree can point the
#: bridge at it with this.
CHECKOUT_DIR_ENV = "SWARM_CHECKOUT_DIR"

#: How long one local git command may take. They are all reads of local state;
#: anything slower than this is a hung filesystem, not a slow repository.
_GIT_TIMEOUT = 20

#: `git@host:owner/name(.git)` -- scp-like syntax, which has no scheme.
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def directory() -> Path:
    """The checkout this bridge infers from: $SWARM_CHECKOUT_DIR, else its cwd."""
    override = os.environ.get(CHECKOUT_DIR_ENV, "").strip()
    return Path(override) if override else Path.cwd()


@dataclass
class Repository:
    """What a dispatch will clone, and how that was decided.

    `url` None means NOTHING IS CLONED, and `source` plus `notes` say why --
    the caller asked for none, or the bridge is not in a checkout. A reply that
    carried a null URL without the reason would read as a bridge that forgot.
    """

    url: str | None
    ref: str | None
    #: "given" -- the caller named it; "checkout" -- inferred here;
    #: "none" -- nothing is cloned.
    source: str
    commit: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "url": self.url,
            "ref": self.ref,
            "source": self.source,
        }
        if self.commit:
            out["commit"] = self.commit
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def _git(where: Path, *args: str) -> subprocess.CompletedProcess:
    """One read-only git command in `where`. Never prompts, never raises on exit."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        return subprocess.run(
            ["git", "-C", str(where), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise _NoGit("git is not installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise SwarmError(
            f"`git {' '.join(args)}` in {where} did not answer within {_GIT_TIMEOUT}s; "
            "the bridge cannot tell which branch this checkout is on. Pass `repo` and "
            "`ref` explicitly"
        ) from exc


class _NoGit(Exception):
    """git itself is missing: there is no checkout to infer from."""


def _out(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or "").strip()


def https_url(raw: str) -> str:
    """A remote URL as the worker can clone it: https, with no credentials.

    Refuses what no rewrite makes clonable from a container -- a local path,
    `file://`, plain `http://` (the API accepts only https, ssh and git@).
    """
    text = (raw or "").strip()
    if not text:
        raise SwarmError("the checkout's remote has no URL")
    scp = None if "://" in text else _SCP_LIKE.match(text)
    if scp and not text.startswith(("/", ".", "~")):
        host, path = scp.group("host"), scp.group("path").lstrip("/")
        return f"https://{host}/{path}"
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme in ("ssh", "git+ssh", "https"):
        host = parts.hostname or ""
        if not host:
            raise SwarmError(f"the checkout's remote URL names no host: {_redact(text)}")
        # The port is dropped with the scheme: an ssh port says nothing about
        # where the same repository is served over https.
        netloc = host if scheme != "https" or not parts.port else f"{host}:{parts.port}"
        return urlunsplit(("https", netloc, parts.path, "", ""))
    if scheme == "http":
        raise SwarmError(
            f"the checkout's remote is plain http ({_redact(text)}); the API accepts "
            "only https, ssh:// and git@ URLs. Pass `repo` explicitly"
        )
    raise SwarmError(
        f"the checkout's remote is not a network URL ({_redact(text)}): a remote agent "
        "cannot clone a path on this machine. Pass `repo` explicitly"
    )


def _redact(url: str) -> str:
    """A URL safe to print: no user or password component."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def resolve(
    *,
    repo: Any = None,
    ref: Any = None,
    no_repository: bool = False,
    where: Path | None = None,
) -> Repository:
    """The repository and ref to send, from the caller or from the checkout.

    * `no_repository` -- nothing is cloned, whatever else was passed.
    * `repo` given -- sent as given, with `ref` as given; git is not consulted.
    * only `ref` given -- the URL is inferred, the ref is the caller's.
    * neither -- both are inferred, and the branch must be pushed (see the
      module docstring for why that is a refusal and not a warning).
    """
    repo_text = str(repo).strip() if isinstance(repo, str) else ""
    ref_text = str(ref).strip() if isinstance(ref, str) else ""
    if no_repository:
        if repo_text or ref_text:
            raise SwarmError(
                "`no_repository` says clone nothing, and `repo`/`ref` name something to "
                "clone; pass one or the other"
            )
        return Repository(
            url=None, ref=None, source="none",
            notes=["no_repository was set: this task clones nothing, so it can "
                   "produce no patch and no pull request"],
        )
    if repo_text:
        return Repository(url=repo_text, ref=ref_text or None, source="given")

    here = where if where is not None else directory()
    try:
        inside = _git(here, "rev-parse", "--is-inside-work-tree")
    except _NoGit as exc:
        return _nothing(here, str(exc), ref_text)
    if inside.returncode != 0 or _out(inside) != "true":
        return _nothing(here, f"{here} is not inside a git checkout", ref_text)

    top = Path(_out(_git(here, "rev-parse", "--show-toplevel")) or here)
    branch = _out(_git(here, "symbolic-ref", "--quiet", "--short", "HEAD"))
    remote = _out(_git(here, "config", f"branch.{branch}.remote")) if branch else ""
    if ref_text:
        # The caller chose the ref, so whether THIS branch is pushed is not the
        # question; only the URL is inferred. The remote is the branch's own
        # when it has one, else `origin`.
        url = _remote_url(here, remote or "origin")
        return Repository(
            url=url, ref=ref_text, source="checkout",
            notes=[f"repository inferred from the checkout at {top}; ref {ref_text!r} "
                   "was given and is sent as given"],
        )

    if not branch:
        raise SwarmError(
            f"the checkout at {top} has a detached HEAD, so there is no branch a remote "
            "agent could clone. Check out a branch and push it (`git switch -c <name>` "
            "then `git push -u origin <name>`), or pass `repo` and `ref` explicitly"
        )
    merge = _out(_git(here, "config", f"branch.{branch}.merge"))
    if not remote or not merge:
        raise SwarmError(
            f"branch {branch!r} in {top} has no upstream: it has never been pushed, so a "
            f"remote agent cannot clone it. Push it first -- `git push -u origin {branch}` "
            "-- or pass `repo` and `ref` explicitly"
        )
    if remote == ".":
        raise SwarmError(
            f"branch {branch!r} tracks another LOCAL branch ({merge}), which a remote agent "
            f"cannot see. Push it -- `git push -u origin {branch}` -- or pass `repo` and "
            "`ref` explicitly"
        )
    remote_branch = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") else merge
    upstream = f"{remote}/{remote_branch}"
    tracking = _git(here, "rev-parse", "--verify", "--quiet", "@{upstream}")
    if tracking.returncode != 0:
        raise SwarmError(
            f"branch {branch!r} names {upstream} as its upstream, but this checkout has no "
            f"remote-tracking ref for it, so it was never pushed or fetched. Push it -- "
            f"`git push -u {remote} {branch}` -- or pass `repo` and `ref` explicitly"
        )
    ahead = _count(here, "@{upstream}..HEAD")
    if ahead:
        raise SwarmError(
            f"branch {branch!r} has {ahead} commit(s) that are not on {upstream}; a remote "
            f"agent clones {upstream} and would work without them, on an older base. "
            f"Push first -- `git push {remote} {branch}:{remote_branch}` -- or pass `ref` "
            "explicitly to use what is already pushed"
        )
    url = _remote_url(here, remote)
    commit = _out(_git(here, "rev-parse", "HEAD")) or None
    notes = [
        f"inferred from the checkout at {top}: {upstream}, pushed at {commit[:12] if commit else 'an unread commit'} "
        f"(checked against this checkout's remote-tracking ref; nothing was fetched)"
    ]
    behind = _count(here, "HEAD..@{upstream}")
    if behind:
        notes.append(
            f"{upstream} has {behind} commit(s) this checkout does not; the remote agent "
            "will see them"
        )
    dirty = [line for line in (_git(here, "status", "--porcelain").stdout or "").splitlines() if line.strip()]
    if dirty:
        notes.append(
            f"{len(dirty)} uncommitted change(s) in this checkout are NOT visible to the "
            "remote agent: it clones only what is pushed"
        )
    return Repository(url=url, ref=remote_branch, source="checkout", commit=commit, notes=notes)


def _nothing(here: Path, why: str, ref_text: str) -> Repository:
    if ref_text:
        raise SwarmError(
            f"`ref` {ref_text!r} was given without `repo`, and the repository cannot be "
            f"inferred: {why}. Pass `repo` as well"
        )
    return Repository(
        url=None, ref=None, source="none",
        notes=[f"{why}, so no repository was inferred and this task clones nothing -- "
               "it can produce no patch and no pull request. Pass `repo` to clone one"],
    )


def _remote_url(here: Path, remote: str) -> str:
    got = _git(here, "remote", "get-url", remote)
    if got.returncode != 0 or not _out(got):
        raise SwarmError(
            f"the checkout has no remote called {remote!r}, so its repository URL cannot be "
            "inferred. Pass `repo` explicitly"
        )
    return https_url(_out(got))


def _count(here: Path, spec: str) -> int:
    """How many commits `spec` names. RAISES when git could not say.

    Not 0 on a failed read: `ahead == 0` is what lets an inferred dispatch
    through, so a count that quietly became zero would dispatch a branch whose
    commits were never pushed -- the one thing this module exists to refuse.
    """
    got = _git(here, "rev-list", "--count", spec)
    try:
        if got.returncode != 0:
            raise ValueError(got.stderr)
        return int(_out(got))
    except ValueError as exc:
        raise SwarmError(
            f"`git rev-list --count {spec}` in {here} could not be read "
            f"({(got.stderr or '').strip() or 'no output'}), so whether this branch is "
            "pushed is unknown. Pass `repo` and `ref` explicitly"
        ) from exc
