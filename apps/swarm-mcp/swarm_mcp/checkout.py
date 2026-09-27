"""Which repository and ref a dispatch clones, when the caller did not say.

WHY THIS EXISTS (owner decision, 2026-09-26). A Claude Code workflow step can
now run in SwarmCloud instead of locally (`sc:remote`, `/sc:run`). A local step
works on the checkout in front of it without being told which one; a remote
step that had to be handed a repository URL and a branch by every caller would
make "run this step remotely" a different, fussier act than "run this step".
So `swarm_dispatch` and `swarm_workflow` infer both from the git checkout the
bridge runs in -- but only when the caller passes `infer: true`. That opt-in
was added the SAME day this module was: `sc:remote` and `/sc:run` pass it on
every call; a plain `swarm_dispatch` or `swarm_workflow` call goes back to
what it did before this module existed -- a repository only when `repo` is
named -- unless it opts in too. What travels for an inferred repository is the
commit the branch is PINNED at when the dispatch is made, not the branch name:
a remote task can sit QUEUED for a long time, and a caller who pushes again to
the same branch in the meantime must not silently move what an already-sent
dispatch will clone.

WHY IT REFUSES RATHER THAN GUESSES. A remote agent gets a fresh
`--depth 1 --single-branch` clone of what is on the remote
(`agent_worker/gitops.py`). A branch that exists only here, or a branch whose
last commits were never pushed, is a branch the agent cannot see -- and it does
not fail when it cannot see them: it works on the older tip, succeeds, and hands
back a patch against the wrong base. That is the failure the delegate skill's
clean-checkout rule describes, and the inference is exactly where it would
otherwise be reintroduced silently. So:

  * a detached HEAD, a branch that is not on its remote under its OWN name,
    or a branch AHEAD of that remote branch is refused, and the refusal names
    the command that fixes it -- always `git push -u <remote> <branch>`;
  * uncommitted changes are not refused -- they are often unrelated -- but the
    reply says, with a count, that the remote agent will not see them;
  * a branch BEHIND its remote branch is not refused either: the agent sees
    more than this checkout, not less, and the reply says so.

WHAT "PUSHED" MEANS: `<remote>/<branch>`, the branch's SAME-NAMED remote
branch -- never its upstream. The two differ in this repository's own lane
recipe: `git checkout -b <lane> origin/main` sets the lane's upstream to
`origin/main` (`branch.autoSetupMerge` defaults to true), so measuring against
the upstream counted main's distance, refused a lane that WAS pushed, and told
the reader to run `git push origin <lane>:main` -- a push of unreviewed commits
onto the default branch. The remote agent clones by name, so the name is what
is checked; the remote is the one `git push` would use (`pushRemote`, then
`remote.pushDefault`, then the branch's own remote, then `origin`). `@{push}`
is not used: under the default `push.default=simple` it does not resolve at all
for a branch whose upstream has another name (measured 2026-09-25: "cannot
resolve 'simple' push to a single destination").

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
    infer: bool = False,
    where: Path | None = None,
) -> Repository:
    """The repository and ref to send, from the caller, from the checkout, or nothing.

    Owner decision, 2026-09-26: inference is OPT-IN. `swarm_dispatch` and
    `swarm_workflow` go back to their pre-PR behaviour -- a repository only
    when `repo` is given -- unless the caller passes `infer: true`. `sc:remote`
    and `/sc:run` are the two callers that pass it on every call; anything
    else naming neither `repo` nor `infer` gets `source: "none"`, exactly as
    before this PR, and git is never even consulted.

    * `repo` given -- sent as given, with `ref` as given; git is not consulted,
      whatever `infer` says.
    * `infer` false (the default) and no `repo` -- nothing is cloned.
    * `infer` true, only `ref` given -- the URL is inferred, the ref is the
      caller's.
    * `infer` true, neither given -- both are inferred: the URL from the
      checkout's remote, and the ref the COMMIT the pushed branch is at, not
      the branch name -- a moving branch pointer is not what a pinned remote
      clone should chase. The branch must still be pushed under its own name
      (see the module docstring for why that is a refusal and not a warning).
    """
    repo_text = str(repo).strip() if isinstance(repo, str) else ""
    ref_text = str(ref).strip() if isinstance(ref, str) else ""
    if repo_text:
        return Repository(url=repo_text, ref=ref_text or None, source="given")
    if not infer:
        return Repository(
            url=None, ref=None, source="none",
            notes=["no repository was named and `infer` was not requested: this task "
                   "clones nothing, so it can produce no patch and no pull request. Pass "
                   "`repo`, or `infer: true` to clone this checkout's pushed branch"],
        )

    here = where if where is not None else directory()
    try:
        inside = _git(here, "rev-parse", "--is-inside-work-tree")
    except _NoGit as exc:
        return _nothing(here, str(exc), ref_text)
    if inside.returncode != 0 or _out(inside) != "true":
        return _nothing(here, f"{here} is not inside a git checkout", ref_text)

    top = Path(_out(_git(here, "rev-parse", "--show-toplevel")) or here)
    branch = _out(_git(here, "symbolic-ref", "--quiet", "--short", "HEAD"))
    remote = _push_remote(here, branch)
    if ref_text:
        # The caller chose the ref, so whether THIS branch is pushed is not the
        # question; only the URL is inferred, from the remote `git push` would
        # use.
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
    if remote is None:
        raise SwarmError(
            f"the checkout at {top} has no remote to push {branch!r} to: none is called "
            "origin and the branch names none, so its repository cannot be inferred. Add "
            f"one and push it -- `git remote add origin <url>` then `git push -u origin "
            f"{branch}` -- or pass `repo` and `ref` explicitly"
        )
    url = _remote_url(here, remote)
    # THE BRANCH'S OWN NAME ON THE REMOTE, never its upstream: see the module
    # docstring for the lane recipe that makes the two differ, and the push to
    # main that reading the upstream used to recommend.
    on_remote = f"{remote}/{branch}"
    tracking = f"refs/remotes/{on_remote}"
    push = f"git push -u {remote} {branch}"
    upstream = _upstream(here)
    elsewhere = upstream if upstream and upstream != on_remote else None
    if _git(here, "rev-parse", "--verify", "--quiet", tracking).returncode != 0:
        message = (
            f"branch {branch!r} in {top} is not on {remote} under its own name: this "
            f"checkout has no {on_remote}, so it was never pushed (or not since the last "
            f"fetch), and a remote agent, which clones by name, cannot see it. Push it "
            f"first -- `{push}` -- or pass `repo` and `ref` explicitly"
        )
        if elsewhere:
            message += (
                f". Its upstream is {elsewhere}, the branch it was started from; that is "
                "never sent in its place"
            )
        raise SwarmError(message)
    ahead = _count(here, f"{tracking}..HEAD")
    if ahead:
        raise SwarmError(
            f"branch {branch!r} has {ahead} commit(s) that are not on {on_remote}; a remote "
            f"agent clones {on_remote} and would work without them, on an older base. "
            f"Push first -- `{push}` -- or pass `ref` explicitly to use what is already "
            "pushed"
        )
    commit = _out(_git(here, "rev-parse", "HEAD")) or None
    if not commit:
        raise SwarmError(
            f"`git rev-parse HEAD` in {top} could not be read, so the commit to pin cannot "
            "be named. Pass `repo` and `ref` explicitly"
        )
    notes = [
        f"inferred from the checkout at {top}: {on_remote}, pushed at {commit[:12]} "
        f"(checked against this checkout's remote-tracking ref; nothing was fetched); the "
        f"commit is what is sent, not the branch name, so a later push to {branch!r} does "
        "not move what this dispatch clones"
    ]
    if elsewhere:
        notes.append(
            f"the branch's upstream is {elsewhere}; what is cloned is the branch itself as "
            f"pushed, {on_remote}, never its upstream"
        )
    behind = _count(here, f"HEAD..{tracking}")
    if behind:
        notes.append(
            f"{on_remote} has {behind} commit(s) this checkout does not; the remote agent "
            "will see them"
        )
    dirty = [line for line in (_git(here, "status", "--porcelain").stdout or "").splitlines() if line.strip()]
    if dirty:
        notes.append(
            f"{len(dirty)} uncommitted change(s) in this checkout are NOT visible to the "
            "remote agent: it clones only what is pushed"
        )
    # THE COMMIT, NOT THE BRANCH NAME (owner decision, 2026-09-26): a remote
    # dispatch is pinned at what was pushed when it was inferred, not at
    # whatever `branch` points to by the time the task actually runs.
    return Repository(url=url, ref=commit, source="checkout", commit=commit, notes=notes)


def _push_remote(here: Path, branch: str) -> str | None:
    """The remote `git push` would send `branch` to, or None when there is none.

    Git's own order: `branch.<name>.pushRemote`, `remote.pushDefault`, the
    branch's `branch.<name>.remote` -- unless that is `.`, an upstream that is
    another LOCAL branch -- then `origin`, or the one remote when there is only
    one. A remote that is named but does not exist is refused by `_remote_url`,
    by name.
    """
    keys = ["remote.pushDefault"]
    if branch:
        keys = [f"branch.{branch}.pushRemote", "remote.pushDefault", f"branch.{branch}.remote"]
    for key in keys:
        value = _out(_git(here, "config", key))
        if value and value != ".":
            return value
    names = [name.strip() for name in _out(_git(here, "remote")).splitlines() if name.strip()]
    if "origin" in names:
        return "origin"
    if len(names) == 1:
        return names[0]
    return None


def _upstream(here: Path) -> str | None:
    """HEAD's upstream as `<remote>/<branch>`, or None. Reported, never cloned."""
    got = _git(here, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if got.returncode != 0:
        return None
    return _out(got) or None


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
