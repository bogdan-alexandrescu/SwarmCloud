"""Optional shallow clone of the task's repository.

Shallow and single-branch, because an agent needs the working tree, not the
history, and a full clone of a large monorepo is minutes of wall clock and
gigabytes of an ephemeral disk that the resource class does not have.

The URL comes from the task document, which means it came from an authenticated
caller, which means it is not trusted here. Three things are enforced:

* the scheme is `https` or `ssh` -- never `file://`, never `ext::`, which git
  will happily use to execute an arbitrary command;
* the URL cannot begin with `-`, which would make git parse it as an option
  (`--upload-pack=...` is the classic remote-code-execution shape);
* credentials never appear in argv. A token goes into a 0600 credential file,
  because argv is world-readable through /proc.

The credential file lives in `workspace/private/`, not in the workspace `tmp/`
directory, and it is deleted as soon as the clone finishes. `tmp/` is the
directory the worker hands the agent as `TMPDIR`, so a credential left there is
one `cat $TMPDIR/.git-credentials` away from any prompt injection in the
repository that was just cloned. Same uid, so the mode bits are not a boundary
either way -- what changes is that the agent is never told the path and the file
is gone before the agent starts.

The token itself is the tenant's own, resolved from
`swarm-tenant-<tenant>-git`, never a platform-wide one: a single token that can
clone every tenant's repositories would make one malicious repository in one
tenant a credential compromise for all of them (invariant 9).
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse, urlunparse, quote

from .procman import run_child

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._\-/]{1,255}$")
ALLOWED_SCHEMES = ("https", "ssh")

#: A DNS hostname. Checked after parsing rather than trusted from it: the
#: scp-style rewrite below BUILDS a `ssh://` URL out of caller text, so a host
#: that came through that path has never been validated by anything else.
_SAFE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$")

#: Characters a repository path may contain. Deliberately narrower than RFC 3986
#: allows: no real repository path needs anything outside this, and the set is
#: what stops the scp rewrite from smuggling a shell fragment into the path
#: component of a URL that then looks structurally valid.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/-]*$")


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloneResult:
    path: Path
    url: str
    ref: str | None
    commit: str | None
    duration_seconds: float
    #: True when the clone succeeded and the repository had no commits, which
    #: is why `commit` is None. Established positively (no object at all), so
    #: a `rev-parse` that failed for any other reason is never read as "empty".
    empty: bool = False


def validate_repository_url(url: str) -> str:
    if not url or url.startswith("-"):
        raise GitError("repository url must not be empty or start with '-'")
    # Any whitespace or control character, not just a newline. The scp rewrite
    # below turns caller text into an `ssh://` URL by string concatenation, so
    # `git@ext::sh -c id` would otherwise become a structurally valid URL whose
    # path is a shell fragment. Nothing in a real repository URL is a space.
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise GitError("repository url must not contain whitespace or control characters")
    if url.startswith("git@") and ":" in url:
        # scp-style syntax; rewrite to ssh:// so it goes through one code path
        host, _, path = url[4:].partition(":")
        url = f"ssh://git@{host}/{path}"
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise GitError(
            f"repository scheme {parsed.scheme!r} is not allowed; "
            f"use one of {', '.join(ALLOWED_SCHEMES)}"
        )
    if not parsed.netloc:
        raise GitError("repository url has no host")
    try:
        hostname, port = parsed.hostname, parsed.port
    except ValueError as exc:                    # a non-numeric port
        raise GitError(f"repository url has an invalid port: {exc}") from exc
    if not hostname or not _SAFE_HOST.match(hostname):
        raise GitError(f"repository host {hostname!r} is not a valid hostname")
    if port is not None and not 1 <= port <= 65535:
        raise GitError(f"repository port {port} is out of range")
    if not _SAFE_PATH.match(parsed.path):
        raise GitError("repository path contains characters that are not allowed in a git path")
    return url


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _SAFE_REF.match(ref):
        raise GitError(f"repository ref {ref!r} contains unsupported characters")
    if ref.startswith("-") or ".." in ref:
        raise GitError(f"repository ref {ref!r} is not a valid git ref")
    return ref


def _write_credentials(url: str, token: str, private_dir: Path) -> Path:
    """Store `https://x-access-token:<token>@host` for git's `store` helper.

    `private_dir` is the worker's own scratch directory, never the one the agent
    is given as TMPDIR, and `shallow_clone` removes the file in a `finally`.
    """
    parsed = urlparse(url)
    private_dir.mkdir(parents=True, exist_ok=True)
    cred_file = private_dir / ".git-credentials"
    entry = urlunparse(
        (
            parsed.scheme,
            f"x-access-token:{quote(token, safe='')}@{parsed.netloc}",
            "",
            "",
            "",
            "",
        )
    )
    cred_file.write_text(entry + "\n")
    cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return cred_file


def shallow_clone(
    *,
    url: str,
    ref: str | None,
    destination: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    token: str | None = None,
    git_binary: str = "git",
) -> CloneResult:
    """Clone `url` at `ref` into `destination`, shallow and single-branch.

    `private_dir` is the worker's own scratch directory (`workspace/private/`).
    git's HOME and the credential file both live there rather than in the
    directory the agent is handed as TMPDIR, and the credential file is removed
    before this function returns, on every path including a failure.
    """
    url = validate_repository_url(url)
    ref = validate_ref(ref)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private_dir),
        "GIT_TERMINAL_PROMPT": "0",            # never block waiting for a password
        "GIT_ASKPASS": "/bin/true",
        "GIT_CONFIG_NOSYSTEM": "1",
        # /dev/null, not just HOME: `GIT_CONFIG_NOSYSTEM` disables /etc/gitconfig
        # but NOT `~/.gitconfig`, and pointing the global file at /dev/null is
        # what stops an inherited user config from carrying an `insteadOf`, a
        # proxy or a credential helper into a command that holds the token.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        "LC_ALL": "C",
    }
    config_args: list[str] = ["-c", "protocol.version=2", "-c", "advice.detachedHead=false"]
    cred_file: Path | None = None
    if token:
        cred_file = _write_credentials(url, token, private_dir)
        config_args += ["-c", f"credential.helper=store --file={cred_file}"]

    is_sha = bool(ref and _SHA_RE.match(ref))
    if is_sha:
        # A shallow clone cannot target a bare commit, so fetch it explicitly.
        steps = [
            [git_binary, *config_args, "init", "--quiet", str(destination)],
            [git_binary, *config_args, "-C", str(destination), "remote", "add", "origin", url],
            [
                git_binary, *config_args, "-C", str(destination),
                "fetch", "--depth", "1", "--no-tags", "origin", ref,
            ],
            [git_binary, *config_args, "-C", str(destination), "checkout", "--quiet", "FETCH_HEAD"],
        ]
    else:
        clone = [git_binary, *config_args, "clone", "--depth", "1", "--no-tags", "--single-branch"]
        if ref:
            clone += ["--branch", ref]
        clone += ["--", url, str(destination)]
        steps = [clone]

    total = 0.0
    try:
        for index, argv in enumerate(steps):
            result = run_child(
                argv,
                cwd=private_dir,
                env=env,
                stdout_path=logs_dir / f"git-{index}.out.log",
                stderr_path=logs_dir / f"git-{index}.err.log",
                timeout_seconds=timeout_seconds,
                grace_seconds=10,
                max_stdout_bytes=1 * 1024 * 1024,
                max_stderr_bytes=1 * 1024 * 1024,
                logger=logger,
            )
            total += result.duration_seconds
            if result.timed_out:
                raise GitError(f"git step {index} timed out after {timeout_seconds}s")
            if result.exit_code != 0:
                tail = (logs_dir / f"git-{index}.err.log").read_text(errors="replace")[-2000:]
                raise GitError(
                    f"git step {index} failed with exit {result.exit_code}: {tail.strip()}"
                )
    finally:
        # The clone is the only thing that ever needs this file. Leaving it on
        # disk for the length of the attempt is what turns a prompt injection in
        # the cloned repository into a stolen token.
        if cred_file is not None:
            try:
                cred_file.unlink(missing_ok=True)
            except OSError as exc:
                logger.error("could not remove the git credential file", error=str(exc))

    commit = _read_head(destination, private_dir, logs_dir, logger, git_binary)
    empty = commit is None and _holds_no_objects(
        destination, private_dir, logs_dir, logger, git_binary
    )
    logger.info(
        "repository cloned", url=url, ref=ref, commit=commit, empty=empty,
        seconds=round(total, 2),
    )
    return CloneResult(
        path=destination, url=url, ref=ref, commit=commit, duration_seconds=total, empty=empty
    )


def _read_head(
    destination: Path, tmp: Path, logs_dir: Path, logger: Any, git_binary: str
) -> str | None:
    result = run_child(
        [git_binary, "-C", str(destination), "rev-parse", "HEAD"],
        cwd=tmp,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp), "GIT_CONFIG_NOSYSTEM": "1"},
        stdout_path=logs_dir / "git-head.out.log",
        stderr_path=logs_dir / "git-head.err.log",
        timeout_seconds=30,
        grace_seconds=5,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    if result.exit_code != 0:
        return None
    text = (logs_dir / "git-head.out.log").read_text(errors="replace").strip()
    return text or None


def _holds_no_objects(
    destination: Path, tmp: Path, logs_dir: Path, logger: Any, git_binary: str
) -> bool:
    """True when the fresh clone holds no object at all: an empty repository.

    Asked only after `rev-parse HEAD` failed, and it must answer "yes" before
    the clone counts as empty. Reading a failed `rev-parse` as "empty" would
    let a transient failure turn a real repository into one whose whole tree
    the publish folds into a parentless commit. Objects rather than refs,
    because a clone of a pinned sha is a detached HEAD with no ref either
    (measured: `for-each-ref` is empty there too), while any clone that
    fetched something has a loose object or a pack.
    """
    result = run_child(
        [git_binary, "-C", str(destination), "count-objects", "-v"],
        cwd=tmp,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp), "GIT_CONFIG_NOSYSTEM": "1"},
        stdout_path=logs_dir / "git-objects.out.log",
        stderr_path=logs_dir / "git-objects.err.log",
        timeout_seconds=30,
        grace_seconds=5,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
        logger=logger,
    )
    if result.exit_code != 0:
        return False
    counts: dict[str, str] = {}
    for line in (logs_dir / "git-objects.out.log").read_text(errors="replace").splitlines():
        key, _, value = line.partition(":")
        counts[key.strip()] = value.strip()
    return counts.get("count") == "0" and counts.get("in-pack") == "0"


# ---------------------------------------------------------------------------
# Harvest -- getting the agent's work back OUT of the workspace
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS. Until this was written the loop was open at the far end. A
# worker cloned a repository into `work/repo`, the agent edited and committed
# into it, `checkpoint.py` archived the whole of `work/` to GCS every two
# minutes -- and then nothing ever read that archive except a retry of the same
# attempt. The commits were durable and unreachable at the same time. Asked
# "how does an agent's work get merged", the honest answer was "it does not".
#
# Two things are separated here on purpose, because they have very different
# blast radii:
#
#   HARVEST  is read-only. It asks git what changed and writes a patch into
#            `artifacts/`. It needs no credential at all and runs on EVERY exit
#            path, including a park and a crash.
#
#   PUBLISH  pushes a branch and opens a pull request. It needs a token with
#            write permission, runs only when the agent exited on its own, and
#            is refused outright unless the forge itself confirms the push bit.
#
# The split is what lets the capability ship complete while the permission
# stays a separate decision: with today's read-only token the harvest branch is
# the live path, not a placeholder for one.

#: Fields inside a `git log` record, and records inside the stream. Chosen
#: because neither byte can occur in a commit subject, an author name or a
#: path -- a newline can occur in all three, so line-splitting the output is
#: the bug this avoids.
_FS = "\x1f"
_RS = "\x1e"


@dataclass(frozen=True)
class CommitSummary:
    sha: str
    subject: str
    author: str
    committed_at: str
    files_changed: int
    insertions: int
    deletions: int
    binary_files: int


@dataclass(frozen=True)
class WorkSummary:
    """What the agent did to the repository, as facts rather than a guess."""

    base: str | None
    head: str | None
    commits: tuple[CommitSummary, ...]
    #: Paths git reports as changed-but-uncommitted, from `status --porcelain`.
    #: The common case by far: most agents edit and never commit.
    dirty: tuple[str, ...]
    dirty_truncated: bool
    patch_name: str | None
    patch_bytes: int
    #: True when a patch was produced but discarded for exceeding the cap. A
    #: TRUNCATED patch is never written: it would apply cleanly and silently
    #: drop the rest of the change, which is worse than having no patch.
    patch_omitted: bool
    insertions: int
    deletions: int

    @property
    def is_empty(self) -> bool:
        return not self.commits and not self.dirty


def _git_env(private_dir: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(private_dir),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "GIT_CONFIG_NOSYSTEM": "1",
        # See `shallow_clone`: NOSYSTEM leaves `~/.gitconfig` in play, so the
        # global file is pinned to /dev/null on every git the worker runs.
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        "LC_ALL": "C",
        # Every command that reaches this env runs in the clone AFTER the
        # agent has had the run of it, so its refs -- including `refs/replace/*`
        # -- are the agent's to write. `git replace` substitutes an object
        # wherever git reads one through the object database: `cat-file`,
        # `diff`, `show`, `log` all follow it (measured with git 2.40.1: a
        # `git replace <blob> <clean-blob>` made `git diff` print the clean
        # content for a blob that still held the real one). Replacement is
        # read-side only -- it never touches the tree a commit points at, so
        # `commit-tree`, pack-objects and the eventual push always send the
        # REAL object. Without this, the per-commit secret scan
        # (`lifecycle._first_leaking_commit`) could be shown a clean
        # replacement for a blob that holds a registered secret, pass the
        # commit as non-leaking, and then push the real, secret-holding blob
        # unfolded (#259 review, M1). `GIT_NO_REPLACE_OBJECTS=1` makes every
        # git command below read the real object instead.
        "GIT_NO_REPLACE_OBJECTS": "1",
        # The same for `.git/info/grafts`, which the agent can also write:
        # `rev-list --first-parent` and `merge-base` follow a graft even with
        # replacement off, so a grafted parent made the replay's list skip a
        # commit whose tree then shipped inside the next kept one (#259
        # re-review). An empty graft file means no grafts. The replay also
        # checks the chain itself against each commit's recorded parent
        # (`lifecycle._first_leaking_commit`), which covers `.git/shallow`.
        "GIT_GRAFT_FILE": "/dev/null",
        # And for a commit-graph (#259, third review): git takes a commit's
        # ROOT TREE from `objects/info/commit-graph` when one is present, so
        # a graph whose entry for a commit names a different tree made
        # `git diff` compare trees the push never sends. The scans run in a
        # repository the worker made (`prepare_publish_repo`), which has no
        # graph the agent wrote; this switches graphs off for every worker
        # git as well, so none is read even where one exists. Command-scope
        # configuration outranks any repository's own `core.commitGraph`.
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.commitGraph",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "fetch.writeCommitGraph",
        "GIT_CONFIG_VALUE_1": "false",
    }


#: Passed to every git invocation below. `core.hooksPath` is the one that
#: matters: hooks are not transferred by a clone, but the AGENT can write
#: `.git/hooks/pre-commit` in the workspace, and a harvest that ran it would
#: hand arbitrary code a worker-side execution point after the sandbox was
#: supposed to be finished with. Both belts are worn -- `--no-verify` on the
#: commands that accept it, and a hooks path that cannot contain anything.
#:
#: The same reasoning covers two programs that are hooks in all but name, both
#: read from the clone's `.git/config`, which the agent can write:
#:
#: * `core.fsmonitor` -- set to a path, git documents it as the fsmonitor
#:   hook command and runs it on every index refresh: measured at ten runs
#:   across the `add`, `diff --cached`, `commit`, `reset --soft` and `status`
#:   a publish makes, in a scratch repository with git 2.40.1.
#: * `commit.gpgSign` makes every worker commit run `gpg.program`, and what
#:   that program prints becomes a `gpgsig` header on the worker's own commit
#:   -- text the agent chose, in an object the worker pushes.
#:
#: NOT CLOSED HERE, and not closable by listing keys: a filter driver the
#: agent defines (`filter.<name>.clean`) and names in `.gitattributes` runs on
#: `git add`; diff drivers and `log.showSignature` are the same class. The fix
#: for that class is to run the worker's git in a repository whose
#: configuration the worker wrote, which is what the forge-token lane (#219)
#: does for the commands that carry a credential.
_NO_HOOKS = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgSign=false",
]


#: Prepended to every git invocation that carries the tenant token -- the push,
#: and the integrator's fetch-and-merge. It is meaningful ONLY because those
#: commands run in a worker-owned publish repository (`prepare_publish_repo`),
#: never in the repository the agent worked in.
#:
#: The reason the repository has to be worker-owned rather than the clone with
#: overrides bolted on: `url.<host>.insteadOf` / `pushInsteadOf` rewrites the
#: destination of a push -- including a URL passed explicitly on the command
#: line -- and there is NO `-c` that disables URL rewriting, nor a way to
#: enumerate every `url.*` key a repository config (or a config it `include`s)
#: might carry. So the credential's destination is safe only because the
#: repository the worker built has no such key in it. This is the filter-driver
#: class `_NO_HOOKS` names as "not closable by listing keys".
#:
#: The overrides below are belt to that suspenders: each neutralises a setting a
#: `-c` CAN override. The worker's own credential helper is appended by the
#: caller AFTER the empty `credential.helper` here, which discards any
#: accumulated helper list before the worker's is added (git treats an empty
#: value as a reset).
_TOKEN_SAFE = [
    *_NO_HOOKS,                                 # no repository hook, fsmonitor or gpg program runs
    "-c", "protocol.version=2",
    "-c", "credential.helper=",                 # reset: no inherited helper survives
    "-c", "http.sslVerify=true",                # only over verified TLS
    "-c", "http.proxy=",                        # no proxy may sit in front of the forge
    "-c", "http.extraHeader=",                  # no injected header rides with the request
    # No submodule recursion on a token-bearing fetch. git's default for
    # `fetch.recurseSubmodules` is `on-demand`, which fetches submodules named in
    # a contributor branch's `.gitmodules` -- an untrusted file -- and would
    # carry the credential to whatever hosts it lists. The integrator's fetch
    # wants the branch, never its submodules; `submodule.recurse=false` covers
    # the merge and any command that would otherwise recurse.
    "-c", "fetch.recurseSubmodules=false",
    "-c", "submodule.recurse=false",
]


def _worker_identity(name: str, email: str) -> list[str]:
    """The `-c` arguments that make the worker the author AND committer.

    `user.*` alone is not enough, and was all this module passed. git reads a
    commit's author from `author.name`/`author.email` BEFORE `user.*`, and its
    committer from `committer.*` before `user.*`; `-c user.name` overrides
    `user.name` and nothing else. The clone's `.git/config` is the agent's to
    write, so an agent that set `author.name=Claude` there made every commit
    the worker created -- its auto-commit, its fold, the integrator's merges --
    Claude's. A command-line `-c` of the SAME key outranks the repository's
    value, so all six are set. Measured with git 2.40.1.

    One case this does not reach: a `git commit` that concludes a cherry-pick
    takes its author from `CHERRY_PICK_HEAD` regardless of configuration.
    `--author` overrides that, so every worker `git commit` passes
    `_author_option` as well.
    """
    return [
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "-c", f"author.name={name}",
        "-c", f"author.email={email}",
        "-c", f"committer.name={name}",
        "-c", f"committer.email={email}",
    ]


def _author_option(name: str, email: str) -> str:
    """`--author` in the explicit `Name <email>` form. Without the angle
    brackets git treats the value as a pattern and searches history for an
    author to copy, which is the opposite of what is wanted here."""
    return f"--author={name} <{email}>"


def _git_text(
    argv: list[str],
    *,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[int, str]:
    """Run a git command and return its exit code with its stdout as text.

    A thin wrapper over `_git_text_full` for the many callers that only need
    the text, never whether the capture was truncated.
    """
    code, text, _truncated = _git_text_full(
        argv,
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug=slug,
        timeout_seconds=timeout_seconds,
        logger=logger,
        max_bytes=max_bytes,
    )
    return code, text


def _git_text_full(
    argv: list[str],
    *,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    max_bytes: int = 4 * 1024 * 1024,
) -> tuple[int, str, bool]:
    """Run a git command; return its exit code, stdout as text, and whether the
    capture was truncated.

    Output goes to a file rather than a pipe because `run_child` is the only
    thing in this worker that knows how to kill a process group on a timeout,
    and reusing it is what keeps a wedged git from outliving the attempt.

    TRUNCATION IS DECIDED FROM THE CAPTURE, NEVER FROM THE DECODED TEXT'S
    LENGTH (#259 review, M2). `StreamCapture` caps at `max_bytes` on the raw
    stream and sets `stdout_truncated` the moment it starts dropping bytes; a
    caller that instead re-measured `len(text.encode(...))` after `read_text`
    could be fooled, because reading in TEXT mode with the default `newline`
    applies universal-newline translation: every `\r\n` in the capture becomes
    one `\n`, which can decode-and-reencode SHORTER than the byte cap the
    capture actually hit, so a diff too big to scan whole could read as
    "under the cap" and pass as scanned.

    THE FILE IS READ WITH `newline=""` FOR THE SAME REASON `\r` MUST NOT
    DISAPPEAR (#259 review, M2). Universal-newline translation also turns a
    LONE `\r` inside a line (no `\n` after it) into a `\n`, which splits that
    one line into two. `lifecycle._adds_a_credential` reads only lines
    starting with `+`; a line the translation split in two loses its `+`
    prefix on the second half, so a credential that started after an embedded
    `\r` was read as un-prefixed context and never scanned. `newline=""`
    disables the translation: whatever bytes the stream carried are what this
    function hands back.
    """
    out = logs_dir / f"git-{slug}.out.log"
    result = run_child(
        argv,
        cwd=repo,
        env=_git_env(private_dir),
        stdout_path=out,
        stderr_path=logs_dir / f"git-{slug}.err.log",
        timeout_seconds=timeout_seconds,
        grace_seconds=5,
        max_stdout_bytes=max_bytes,
        max_stderr_bytes=256 * 1024,
        logger=logger,
    )
    if result.timed_out:
        raise GitError(f"git {slug} timed out after {timeout_seconds}s")
    try:
        raw_size = out.stat().st_size
    except OSError:
        raw_size = 0
    try:
        with out.open("r", newline="", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    truncated = result.stdout_truncated or raw_size >= max_bytes
    return result.exit_code, text, truncated


#: The most bytes `_git_stream` hands its consumer in one call.
GIT_STREAM_CHUNK_BYTES = 1024 * 1024


def _git_stream(
    argv: list[str],
    *,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    consume: Callable[[bytes], None],
) -> int:
    """Run a git command and hand ALL of its stdout to `consume`, in chunks.

    OWNER DECISION, 2026-09-29 (#259): the leak scans read every byte of a
    diff however big it is, and nothing of it is stored. A capture to a file
    has to be capped -- the workspace is memory-backed tmpfs -- and a cap is
    either a refusal of every big change or a hole in the scan. So stdout
    goes to a PIPE this process reads while git writes: `run_child` (still
    the one thing that kills a wedged git's process group on the deadline)
    is pointed at `/dev/fd/<write end>`, its pump reopens that as its sink,
    and a reader thread passes each read of at most `GIT_STREAM_CHUNK_BYTES`
    to `consume`. Memory is one chunk plus whatever `consume` keeps.

    The reader never stops draining, even once `consume` has seen enough or
    raised: a pipe nobody reads would block the pump, and git behind it,
    until the deadline. An exception from `consume` is re-raised here after
    the child has been reaped.
    """
    read_fd, write_fd = os.pipe()
    failure: list[BaseException] = []
    consumed = [0]

    def drain() -> None:
        with os.fdopen(read_fd, "rb", buffering=0) as source:
            while True:
                try:
                    chunk = source.read(GIT_STREAM_CHUNK_BYTES)
                except OSError as exc:
                    # FAIL CLOSED: a stream this side could not read is a
                    # stream the scan did not see.
                    failure.append(GitError(f"git {slug}: the output could not be read ({exc})"))
                    return
                if not chunk:
                    return
                consumed[0] += len(chunk)
                if failure:
                    continue
                try:
                    consume(chunk)
                except BaseException as exc:  # re-raised on the caller's thread
                    failure.append(exc)

    reader = threading.Thread(target=drain, name=f"git-{slug}-stream", daemon=True)
    reader.start()
    try:
        result = run_child(
            argv,
            cwd=repo,
            env=_git_env(private_dir),
            stdout_path=Path(f"/dev/fd/{write_fd}"),
            stderr_path=logs_dir / f"git-{slug}.err.log",
            timeout_seconds=timeout_seconds,
            grace_seconds=5,
            # Never reached: the pipe is not storage, and the scan must see
            # every byte. The cap exists only because the capture takes one.
            max_stdout_bytes=1 << 62,
            max_stderr_bytes=256 * 1024,
            logger=logger,
        )
    finally:
        # The pump reopened the pipe through /dev/fd and has closed its copy;
        # closing this one is what lets the reader see the end of the stream.
        os.close(write_fd)
        # With a deadline: a pump that never closed its end would otherwise
        # hold this call, and the publish, past the git command's own.
        reader.join(timeout=max(timeout_seconds, 1))
    if reader.is_alive():
        raise GitError(f"git {slug}: its output was still open after {timeout_seconds}s")
    if failure:
        raise failure[0]
    if result.timed_out:
        raise GitError(f"git {slug} timed out after {timeout_seconds}s")
    # FAIL CLOSED: every byte the pump wrote must have reached the consumer,
    # or the scan passed over output it never read.
    if consumed[0] != result.stdout_bytes:
        raise GitError(
            f"git {slug}: {result.stdout_bytes} bytes were written and "
            f"{consumed[0]} read; the stream was not scanned whole"
        )
    return result.exit_code if result.exit_code is not None else -1


def _parse_log(stream: str) -> tuple[list[CommitSummary], int, int]:
    """Parse one `git log --numstat` stream into commits plus totals."""
    commits: list[CommitSummary] = []
    total_add = 0
    total_del = 0
    for chunk in stream.split(_RS):
        if not chunk.strip():
            continue
        # Split on the FIELD separator, not on lines. The header looks like
        # one line and is not: `%an` is an author name, and a name can contain
        # a newline, which pushes `%aI` onto the next line and leaves the
        # header three fields short. Reading `lines[0]` therefore drops the
        # whole commit -- silently, because a short header is indistinguishable
        # from a malformed one. Found by the test, not by review.
        parts = chunk.split(_FS, 3)
        if len(parts) < 4:
            continue
        sha, subject, author, rest = parts
        when, _, numstat = rest.partition("\n")
        adds = dels = files = binary = 0
        for row in numstat.split("\n"):
            row = row.strip()
            if not row:
                continue
            cells = row.split("\t")
            if len(cells) < 3:
                continue
            files += 1
            # git prints "-" for both counts on a binary file. Counting those
            # as zero would be right; counting them as a file that changed
            # nothing would not, so they are reported separately.
            if cells[0] == "-" or cells[1] == "-":
                binary += 1
                continue
            try:
                adds += int(cells[0])
                dels += int(cells[1])
            except ValueError:
                continue
        commits.append(
            CommitSummary(
                sha=sha.strip(),
                subject=subject,
                author=author,
                committed_at=when,
                files_changed=files,
                insertions=adds,
                deletions=dels,
                binary_files=binary,
            )
        )
        total_add += adds
        total_del += dels
    return commits, total_add, total_del


def summarize_work(
    *,
    repo: Path,
    base: str | None,
    private_dir: Path,
    logs_dir: Path,
    patch_path: Path,
    max_patch_bytes: int,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    max_dirty_listed: int = 200,
    empty_base: bool = False,
) -> WorkSummary:
    """Describe what the agent did, and write one applicable patch.

    Read-only with respect to the repository's history: nothing is committed,
    nothing is pushed, no credential is needed or used. The one mutation is
    `add --intent-to-add`, explained at its call below.

    `base` is the commit the clone landed on. Without it there is nothing to
    diff against and only the dirty list can be reported -- which is still
    worth having, so this degrades rather than raising.

    `empty_base` says the missing base is KNOWN to be nothing: the repository
    was empty when it was cloned, so every commit reachable from HEAD is the
    agent's. Without it, an empty clone whose agent committed everything and
    left nothing dirty summarised as "changed nothing", and the publish
    returned before its commits were replayed (#259) -- the one case where a
    missing base must not mean "no commits to list".
    """
    repo = Path(repo)
    if not (repo / ".git").exists():
        raise GitError(f"{repo} is not a git repository")

    g = [git_binary, *_NO_HOOKS]

    def run(argv: list[str], slug: str, cap: int = 4 * 1024 * 1024) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
            max_bytes=cap,
        )

    code, head_text = run([*g, "rev-parse", "HEAD"], "harvest-head")
    head = head_text.strip() or None if code == 0 else None

    # `--intent-to-add` records untracked paths in the index WITHOUT staging
    # their content, which is the only way to make a single `git diff` cover
    # files the agent created. It touches the index and nothing else: no
    # commit, no working tree change, no reachable object. It runs after the
    # final checkpoint on every path that reaches here, so it cannot alter
    # what a resumed attempt restores.
    run([*g, "add", "--all", "--intent-to-add", "--", "."], "harvest-intent")

    status_code, status_text = run([*g, "status", "--porcelain=v1", "--"], "harvest-status")
    dirty_all: list[str] = []
    if status_code == 0:
        for line in status_text.splitlines():
            if len(line) > 3:
                dirty_all.append(line[3:].strip())
    dirty = tuple(dirty_all[:max_dirty_listed])

    commits: list[CommitSummary] = []
    adds = dels = 0
    span = f"{base}..HEAD" if base else ("HEAD" if empty_base else None)
    if span and head and base != head:
        log_code, log_text = run(
            [
                *g, "log", "--numstat", "--no-color", "--no-merges", "--no-ext-diff",
                "--no-textconv", f"--format={_RS}%H{_FS}%s{_FS}%an{_FS}%aI",
                span,
            ],
            "harvest-log",
        )
        if log_code == 0:
            commits, adds, dels = _parse_log(log_text)
        else:
            # A shallow clone whose base is not an ancestor of HEAD -- a rebase
            # inside the workspace, most likely. Not an error: the patch below
            # still describes the whole change, so say what is missing rather
            # than failing the harvest.
            logger.warning("could not list commits against the clone base", base=base)

    patch_name: str | None = None
    patch_bytes = 0
    omitted = False
    if base:
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        # `--binary` so the patch survives an image or a lockfile; diffing
        # against `base` rather than HEAD is deliberate, because it makes ONE
        # patch that carries committed and uncommitted work together. That is
        # what a reader wants: agents that edit without committing are the
        # common case, not the exception.
        # `--no-ext-diff --no-textconv`: the clone's `.git/config` and
        # `.gitattributes` are the agent's, and `git diff` runs the
        # `diff.external` program or `diff.<driver>.textconv` filter they
        # name -- agent code executing in the worker after the sandbox was
        # finished with, and a patch reading whatever that program printed
        # (#259, the hostile-config test).
        diff_code, _ = _git_text(
            [*g, "diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", base, "--"],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="harvest-diff",
            timeout_seconds=timeout_seconds,
            logger=logger,
            max_bytes=max_patch_bytes + 1,
        )
        produced = logs_dir / "git-harvest-diff.out.log"
        if diff_code == 0 and produced.exists():
            size = produced.stat().st_size
            if size == 0:
                pass
            elif size > max_patch_bytes:
                omitted = True
                patch_bytes = size
                logger.warning("patch exceeds the cap and was not kept", bytes=size)
            else:
                patch_path.write_bytes(produced.read_bytes())
                patch_name = patch_path.name
                patch_bytes = size

    return WorkSummary(
        base=base,
        head=head,
        commits=tuple(commits),
        dirty=dirty,
        dirty_truncated=len(dirty_all) > len(dirty),
        patch_name=patch_name,
        patch_bytes=patch_bytes,
        patch_omitted=omitted,
        insertions=adds,
        deletions=dels,
    )


def hide_from_git(
    *,
    repo: Path,
    name: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> bool:
    """Make the top-level `name` in the checkout invisible to git. True when it is.

    For the `./artifacts` link the worker makes in the checkout (#226): with a
    repository attached the agent starts in `work/repo`, so `./artifacts` is
    there, and a link git could see would be in the harvest's patch, the
    auto-commit and the pushed branch -- a symlink to this attempt's own
    directory, in the tenant's repository.

    `.git/info/exclude`, never `.gitignore`: the exclude file is local to this
    clone, is never committed and never pushed, so the repository the agent
    works on is left exactly as it arrived. `/name`, anchored, so a directory
    of the same name deeper in the tree is not hidden.

    Then ASKED, not assumed. A repository's own `.gitignore` outranks the
    exclude file, and a `!/artifacts` there un-hides the link; so the answer is
    git's (`check-ignore`), and False tells the caller to take the link away
    rather than let it into the diff. Idempotent: a resumed attempt restores
    the exclude file from its checkpoint, and the entry is not written twice.

    Refuses to write through a symlink at `.git/info` or at the exclude file:
    the agent of an earlier attempt could have put one there, and the checkpoint
    that restored it keeps only links that stay inside `work/`.
    """
    repo = Path(repo)
    git_dir = repo / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return False
    info = git_dir / "info"
    exclude = info / "exclude"
    if info.is_symlink() or exclude.is_symlink():
        return False
    pattern = f"/{name}"
    try:
        info.mkdir(exist_ok=True)
        existing = exclude.read_text(errors="replace") if exclude.exists() else ""
        if pattern not in existing.splitlines():
            separator = "" if not existing or existing.endswith("\n") else "\n"
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{separator}# SwarmCloud: the link to this attempt's artifacts "
                    f"directory, never part of the work\n{pattern}\n"
                )
    except OSError as exc:
        logger.warning("could not write the checkout's git exclude file", error=str(exc))
        return False
    try:
        code, _ = _git_text(
            [git_binary, *_NO_HOOKS, "check-ignore", "--quiet", "--", name],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="hide-artifacts-link",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
    except GitError as exc:
        logger.warning("could not ask git whether the link is hidden", error=str(exc))
        return False
    return code == 0


@dataclass(frozen=True)
class PushResult:
    branch: str
    head: str
    remote_url: str
    #: The commit the branch was pushed on top of, for the PR body.
    base: str | None
    auto_committed: bool
    forced: bool = False


def commit_dirty(
    *,
    repo: Path,
    message: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> str | None:
    """Commit whatever the agent left uncommitted. Returns the new sha, or None.

    WHY THIS IS NOT OPTIONAL ON THE PUBLISH PATH. A push transfers commits, so
    an agent that edited twenty files and never ran `git commit` would produce
    a branch identical to its base and a pull request with an empty diff. That
    is the most common agent behaviour, so treating it as the exception would
    make the feature useless most of the time.

    It is safe specifically because of where it writes: a branch named after
    the task, which only this attempt pushes, created fresh from the clone
    base. It never runs against a branch a human shares.

    `--no-verify` and a null hooks path: the agent can write `.git/hooks/*` in
    its own workspace, and a commit that ran them would be arbitrary code
    executing under the worker after the runner has already exited.
    """
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    # `add --all` after the harvest's intent-to-add is what turns those
    # placeholder index entries into real staged content.
    add_code, _ = run([*g, "add", "--all", "--", "."], "publish-add")
    if add_code != 0:
        raise GitError("could not stage the agent's changes")

    # Nothing staged means nothing to commit, and `git commit` exits 1 for
    # that. It is not a failure -- the agent may have committed everything
    # itself -- so it is distinguished here rather than raised.
    diff_code, _ = run([*g, "diff", "--cached", "--quiet"], "publish-staged")
    if diff_code == 0:
        return None

    # `--author` as well as the identity keys: this commit concludes a
    # cherry-pick the agent left in progress, if there is one, and git would
    # otherwise give it the picked commit's author (see `_worker_identity`).
    commit_code, _ = run(
        [
            *g, "commit", "--no-verify", _author_option(author_name, author_email),
            "--message", message,
        ],
        "publish-commit",
    )
    if commit_code != 0:
        raise GitError("could not commit the agent's uncommitted changes")

    code, text = run([git_binary, *_NO_HOOKS, "rev-parse", "HEAD"], "publish-head")
    return text.strip() if code == 0 else None


def fold_agent_commits(
    *,
    repo: Path,
    base: str | None,
    keep: str | None,
    message: str,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> int:
    """Replace every commit made since `base` with ONE commit the worker makes.

    Returns how many commits were replaced, or 0 when there was nothing to
    replace -- no commits at all, or only `keep`, the worker's own commit from
    `commit_dirty`.

    WHY THE WORKER WRITES EVERY COMMIT IT PUSHES. Nothing this platform puts on
    a forge may carry Claude attribution (the owner's rule, 2026-09-25). The
    worker's own commits never did; the AGENT'S could. The claude-code runner
    starts Claude Code with HOME set to the attempt's workspace and no settings
    of its own, so an agent that runs `git commit` follows Claude Code's
    default instruction to add a `Co-Authored-By: Claude` trailer and a
    "Generated with Claude Code" line -- and in a container with no git
    identity it may commit AS Claude. A settings file cannot fix this from the
    image: HOME is overridden, a setting governs only the tool's instruction
    and not the model's choice of identity, and it covers one runner. Folding
    here covers every runner, and is checkable against a real remote.

    The tree is not touched: `reset --soft` keeps the index and the working
    tree exactly as the agent left them, so the one commit carries all of its
    work, committed or not. What the agent committed is still recorded -- the
    harvest ran first, and `result_summary.git.commits` lists each commit's
    subject and author -- it is only not pushed as the agent wrote it.

    An unknown `base` REFUSES rather than degrading. Without it there is no way
    to tell the agent's commits from the repository's own, and pushing whatever
    HEAD holds is the unchecked push this exists to prevent.

    `EMPTY_CLONE_BASE` is not unknown. The repository had no commits when it
    was cloned, so every commit in it is the agent's, and they are replaced by
    one worker commit with no parent.

    `keep` is left alone only when it is the sole commit AND it reads back as
    the worker's. The sha alone does not prove that: a commit that concludes a
    cherry-pick the agent left in progress is authored as the picked commit
    was, whoever ran `git commit`. `commit_dirty` passes `--author` against
    exactly that, and this check is what folds the commit anyway if some other
    route lends it an author.
    """
    empty = base == EMPTY_CLONE_BASE
    if not empty and (not base or not _SHA_RE.match(base.strip())):
        raise GitError(
            "the clone base is unknown, so the worker cannot replace the agent's "
            "commits with its own; nothing was pushed rather than commits whose "
            "author and message the worker did not write"
        )
    base = (base or "").strip()
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if empty:
        code, _ = run([*g, "rev-parse", "--verify", "--quiet", "HEAD"], "publish-fold-born")
        if code != 0:
            # Still no commit at all: nothing was made, so nothing to replace.
            return 0
        span = "HEAD"
    else:
        span = f"{base}..HEAD"

    code, text = run([*g, "rev-list", "--count", span], "publish-fold-count")
    if code != 0:
        raise GitError("could not count the commits made since the clone base")
    try:
        count = int(text.strip() or "0")
    except ValueError as exc:
        raise GitError(f"could not read the commit count {text.strip()[:40]!r}") from exc
    if count == 0:
        return 0
    if count == 1 and keep:
        code, head = run([*g, "rev-parse", "HEAD"], "publish-fold-head")
        if code == 0 and head.strip() == keep:
            author, committer = _worker_idents(run, g)
            if not _foreign(_read_commit(run, g, keep), author, committer):
                # The only commit is the worker's own, from `commit_dirty`.
                return 0

    if empty:
        # `reset --soft` needs a commit to rewind to and there is none, so the
        # parentless commit is made from the index directly. The index is
        # everything the agent left, committed or not: `commit_dirty` staged
        # what was uncommitted, and the rest is what HEAD already holds.
        code, tree = run([*g, "write-tree"], "publish-fold-tree")
        if code != 0 or not tree.strip():
            raise GitError("could not write the agent's work as a tree")
        code, made = run([*g, "commit-tree", tree.strip(), "-m", message], "publish-fold-root")
        if code != 0 or not _SHA_RE.match(made.strip()):
            raise GitError("could not commit the agent's work as the worker")
        code, _ = run([*g, "reset", "--soft", made.strip()], "publish-fold-reset")
        if code != 0:
            raise GitError("could not move the branch to the worker's commit")
    else:
        code, _ = run([*g, "reset", "--soft", base], "publish-fold-reset")
        if code != 0:
            raise GitError("could not rewind to the clone base to replace the agent's commits")

        # Commits that add up to no change leave nothing staged; HEAD is the base.
        code, _ = run([*g, "diff", "--cached", "--quiet"], "publish-fold-staged")
        if code != 0:
            code, _ = run(
                [
                    *g, "commit", "--no-verify", _author_option(author_name, author_email),
                    "--message", message,
                ],
                "publish-fold-commit",
            )
            if code != 0:
                raise GitError("could not commit the agent's work as the worker")
    logger.info("folded the agent's commits into one worker commit", replaced=count)
    return count


#: The clone base recorded for a repository that had NO commits when it was
#: cloned. `git clone` of an empty repository succeeds and lands on nothing, so
#: there is no sha to record -- but that is a known state, not a lost one, and
#: treating it as unknown refused to publish a new project's first commit. Not
#: hexadecimal, so it can never be read as a sha.
EMPTY_CLONE_BASE = "empty"

#: Case-insensitive fragments only attribution produces, checked in every
#: commit message the worker is about to push. Not the bare word "claude": the
#: runner profile is `claude-code`, and a worker message may name it.
ATTRIBUTION_MARKERS = (
    "co-authored-by",
    "generated with",
    "anthropic.com",
    "claude.com/claude-code",
    "claude.ai/code",
)


@dataclass(frozen=True)
class _Commit:
    sha: str
    author: tuple[str, str]
    committer: tuple[str, str]
    signed: bool
    message: str


def _split_ident(value: str) -> tuple[str, str]:
    """`Name <email> 1700000000 +0000` -> ("Name", "email")."""
    lt = value.find("<")
    gt = value.find(">", lt + 1)
    if lt < 0 or gt < 0:
        return value.strip(), ""
    return value[:lt].strip(), value[lt + 1 : gt].strip()


def _parse_commit_object(sha: str, raw: str) -> _Commit:
    """Read a commit from `git cat-file commit` output: the object as stored,
    which is exactly what a push transfers. No pretty format, mailmap or
    `log.*` setting from the agent's config stands between it and the bytes."""
    header, _, message = raw.partition("\n\n")
    author = committer = ("", "")
    signed = False
    for line in header.split("\n"):
        if line.startswith("author "):
            author = _split_ident(line[len("author "):])
        elif line.startswith("committer "):
            committer = _split_ident(line[len("committer "):])
        elif line.startswith("gpgsig"):
            # `gpgsig` and `gpgsig-sha256`. Continuation lines start with a
            # space, so they never match a header name here.
            signed = True
    return _Commit(sha=sha, author=author, committer=committer, signed=signed, message=message)


def _read_commit(run: Any, g: list[str], sha: str) -> _Commit:
    code, raw = run([*g, "cat-file", "commit", sha], f"publish-read-{sha[:12]}")
    if code != 0:
        raise GitError(f"could not read commit {sha[:12]}")
    return _parse_commit_object(sha, raw)


def _worker_idents(run: Any, g: list[str]) -> tuple[tuple[str, str], tuple[str, str]]:
    """The author and committer git WILL write for the worker, asked of git.

    Asked rather than restated because git normalises an identity -- it trims
    surrounding punctuation and whitespace -- so comparing a commit against the
    configured string would refuse every push from a worker whose configured
    name ends in, say, a full stop. `g` carries `_worker_identity`, which
    outranks anything the agent put in the clone's config.
    """
    idents = []
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        code, text = run([*g, "var", var], f"publish-{var.lower().replace('_', '-')}")
        if code != 0:
            raise GitError(f"git could not state the worker's identity ({var})")
        idents.append(_split_ident(text.strip()))
    return idents[0], idents[1]


def _foreign(commit: _Commit, author: tuple[str, str], committer: tuple[str, str]) -> list[str]:
    """Every reason `commit` is not one the worker wrote. Empty when it is."""
    problems = []
    if commit.author != author:
        problems.append(f"authored by {commit.author[0]} <{commit.author[1]}>")
    if commit.committer != committer:
        problems.append(f"committed by {commit.committer[0]} <{commit.committer[1]}>")
    if commit.signed:
        # The worker never signs (`commit.gpgSign=false` on every call), so a
        # signature is text from a program the worker did not choose.
        problems.append("signed, and the worker never signs")
    lowered = commit.message.lower()
    found = [marker for marker in ATTRIBUTION_MARKERS if marker in lowered]
    if found:
        problems.append(f"its message carries attribution ({', '.join(found)})")
    return problems


def verify_worker_authorship(
    *,
    repo: Path,
    base: str | None,
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
) -> int:
    """Refuse a push that would add a commit the worker did not write.

    Returns how many commits were checked. Raises `GitError`, naming the
    commit and every reason, when one of them is not the worker's.

    THE FOLD MAKES THE PROPERTY; THIS CHECKS IT, where the work leaves. Every
    way an agent's identity or attribution reached a pushed commit so far was
    a route nobody had thought of -- a config key read before the one the
    worker set, a cherry-pick's author -- and each was closed where it was
    made. This does not depend on knowing the route: it reads the commits the
    push is about to add and refuses any that is not authored AND committed by
    the worker, is signed, or carries an attribution marker.

    WHICH COMMITS: the first-parent chain from HEAD down to the clone base.
    For `direct-pr` and a contributor that is everything the branch adds -- the
    fold or the auto-commit. For an integrator it is also every merge commit;
    the contributor commits those merges bring in are their second parents,
    and each was checked by its own worker when it pushed. A contributor branch
    pushed before this check existed is therefore not re-checked here.
    """
    empty = base == EMPTY_CLONE_BASE
    if not empty and (not base or not _SHA_RE.match(base.strip())):
        raise GitError(
            "the clone base is unknown, so the worker cannot tell which commits "
            "this push would add; nothing was pushed"
        )
    repo = Path(repo)
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if empty:
        code, _ = run([*g, "rev-parse", "--verify", "--quiet", "HEAD"], "publish-verify-born")
        if code != 0:
            return 0
        span = "HEAD"
    else:
        span = f"{(base or '').strip()}..HEAD"

    code, text = run([*g, "rev-list", "--first-parent", span], "publish-verify-list")
    if code != 0:
        raise GitError("could not list the commits this push would add")
    shas = text.split()
    if not shas:
        return 0

    author, committer = _worker_idents(run, g)
    for sha in shas:
        problems = _foreign(_read_commit(run, g, sha), author, committer)
        if problems:
            raise GitError(
                f"refusing to push commit {sha[:12]}: {'; '.join(problems)}. The "
                f"worker pushes only commits it wrote, as {author[0]} <{author[1]}>"
            )
    logger.info("every commit this push adds is the worker's", commits=len(shas))
    return len(shas)


#: A full sha-1 object name. The clean repository's fetch names each commit it
#: wants by sha, never by a ref the agent could point elsewhere, and a
#: refspec's source must be the whole name.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def prepare_publish_repo(
    *,
    source_repo: Path,
    work_head: str | None,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    base: str | None = None,
    git_binary: str = "git",
    local_branch: str = "swarm-publish",
) -> Path:
    """Build the worker-owned CLEAN repository the publish decides and pushes from.

    TWO GUARANTEES LIVE HERE, and both come from where the repository is, not
    from a list of settings to switch off.

    THE TOKEN (#219). The push and the integrator's merge authenticate with
    the tenant's token, and a token that reaches many repositories makes where
    it is allowed to go a tenant-isolation guarantee (invariant 9). Run inside
    the repository the agent edited, those commands honour its `.git/config`,
    which the agent can write: a `credential.helper` there is handed the
    token, and a `url.<host>.insteadOf` / `pushInsteadOf` redirects the
    authenticated push to a host of the agent's choosing. No `-c` disables
    `insteadOf`, so the token-bearing commands run in a repository whose
    configuration the worker wrote. The remote is never taken from here either;
    the caller passes the validated `repository_url` to `push_branch` /
    `merge_branches`.

    THE LEAK SCANS (#259, owner decision 2026-09-29). Three security reviews
    each found the per-commit and final-tree scans bypassable through state
    the agent writes under its clone's `.git`: `refs/replace/*`,
    `info/grafts`, `shallow`, and a forged `objects/info/commit-graph`, from
    which `git diff` takes a commit's root tree. Each was closed on its own
    and the next review found another. So the kept-commit list, both scans,
    the fold, the replay, the authorship check and the push all run HERE,
    and nothing the agent wrote under `.git` reaches them:

    * the repository is `git init`-ed fresh under an unpredictable name in
      the worker's private scratch (outside `work/`), so its config, hooks,
      refs and object store are the worker's;
    * NO ALTERNATES. The agent's objects are FETCHED in, with
      `transfer.fsckObjects=true`: every object arrives as bytes git checks
      and hashes, and the connectivity check proves each commit's real tree
      and parents are present. An object store borrowed through
      `objects/info/alternates` would bring the agent's
      `objects/info/commit-graph` with it;
    * the commits wanted are named by SHA -- the worker's own `commit_dirty`
      result or the clone's HEAD, and the clone base the worker recorded
      before the agent ran -- never by a ref the agent could repoint;
    * the fetch's upload side runs in the clone (git has no other way to read
      a repository it does not own) with grafts, replacement and the
      commit-graph switched off and its SHALLOW boundary taken from a file
      the worker writes here, holding the recorded base -- never from the
      agent's `.git/shallow`. `upload-pack` is the one git command git
      documents as safe to run in an untrusted repository: it runs no hook,
      and `uploadpack.packObjectsHook` is honoured only from configuration
      the agent cannot write. Whatever the agent's store or config does to
      the upload side can only make the fetch fail -- a missing or
      inconsistent object fails the check here, and nothing is published --
      never change what an object this side reads contains;
    * every git command here runs with `core.commitGraph=false`, no graft
      file and no replacement (`_git_env`), and the repository has no graph,
      grafts or replace refs of the agent's in it anyway.

    `work_head`, when given, is the commit to publish; when None it is the
    clone's HEAD, read by `rev-parse` (the agent's processes are reaped
    before this runs, so it cannot move). `base` is the clone base the
    worker recorded (`EMPTY_CLONE_BASE` for an empty repository); it is
    fetched too, so the fold and the final-tree scan have its tree, and
    anything else refuses: without it there is no telling the agent's
    commits from the repository's. The work is checked out on
    `local_branch`, so the index holds the final tree the fold commits and
    the integrator's merges have a working tree.
    """
    source_repo = Path(source_repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)

    empty = base == EMPTY_CLONE_BASE
    floor = "" if empty else (base or "").strip()
    if not empty and not _FULL_SHA_RE.match(floor):
        raise GitError(
            "the clone base is unknown, so the worker cannot tell the agent's "
            "commits from the repository's; nothing was pushed"
        )

    target = (work_head or "").strip()
    if not target:
        code, text = _git_text(
            [git_binary, *_NO_HOOKS, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            repo=source_repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="publish-source-head",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        target = text.strip() if code == 0 else ""
    if not _FULL_SHA_RE.match(target):
        raise GitError("could not resolve the commit to publish from the clone")

    # WHERE the repository lives is part of the guarantee, not an
    # implementation detail. `private_dir` shares a uid with the agent
    # (workspace.py: "not a permission boundary"), so during its run the agent
    # could create any path under it -- including a SYMLINK at a name it can
    # predict. A constant name was exactly that hole: `shutil.rmtree` refuses
    # to delete a symlink and, with `ignore_errors=True`, swallows the refusal;
    # `mkdir(exist_ok=True)` then no-ops on the surviving link; and `git init`
    # at that path re-initialises the AGENT's repository behind it. `mkdtemp`
    # creates the directory with an unpredictable name and O_EXCL semantics,
    # generated now -- after the agent has exited -- so the worker never lands
    # on, nor follows, anything the agent could have planted.
    publish_dir = Path(tempfile.mkdtemp(dir=str(private_dir), prefix="publish-"))

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=publish_dir,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    init_code, _ = _git_text(
        [git_binary, *_NO_HOOKS, "init", "--quiet", str(publish_dir)],
        repo=private_dir,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="publish-init",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    if init_code != 0:
        raise GitError("could not initialise the worker's publish repository")

    # The upload side's shallow boundary is the base the worker recorded, not
    # the agent's `.git/shallow`: a line the agent added there would make git
    # treat a commit mid-history as a root, and one it removed would send the
    # walk into history the clone never had. The first only drops objects
    # (the connectivity check then fails the fetch), the second only fails
    # the pack -- but neither is the agent's to decide.
    if empty:
        shallow_file = "/dev/null"
    else:
        boundary = publish_dir / ".git" / "swarm-source-shallow"
        boundary.write_text(f"{floor}\n")
        shallow_file = str(boundary)
    upload_pack = " ".join(
        [
            "env",
            "GIT_GRAFT_FILE=/dev/null",
            "GIT_NO_REPLACE_OBJECTS=1",
            f"GIT_SHALLOW_FILE={shlex.quote(shallow_file)}",
            "GIT_CONFIG_NOSYSTEM=1",
            "GIT_CONFIG_GLOBAL=/dev/null",
            shlex.quote(git_binary),
            "-c", "core.commitGraph=false",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            # The wants are shas, not advertised refs.
            "-c", "uploadpack.allowAnySHA1InWant=true",
            "upload-pack",
        ]
    )
    refspecs = [f"+{target}:refs/swarm/head"]
    if not empty:
        refspecs.append(f"+{floor}:refs/swarm/base")
    fetch_code, _ = run(
        [
            git_binary, *_NO_HOOKS,
            "-c", "transfer.fsckObjects=true",
            "-c", "fetch.fsckObjects=true",
            "-c", "fetch.recurseSubmodules=false",
            "-c", "submodule.recurse=false",
            "-c", "gc.auto=0",
            "fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--update-shallow",
            f"--upload-pack={upload_pack}",
            "--", str(source_repo), *refspecs,
        ],
        "publish-fetch",
    )
    if fetch_code != 0:
        raise GitError(
            "could not fetch the agent's work into the worker's clean repository "
            "(an object failed git's checks, or the clone's history is incomplete); "
            "nothing was pushed"
        )

    checkout_code, _ = run(
        [git_binary, *_NO_HOOKS, "checkout", "--quiet", "-f", "-B", local_branch, target],
        "publish-checkout",
    )
    if checkout_code != 0:
        raise GitError("could not check out the agent's work in the publish repository")

    logger.info("clean publish repository prepared", branch=local_branch)
    return publish_dir


def push_branch(
    *,
    repo: Path,
    url: str,
    branch: str,
    token: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
    protected: tuple[str, ...] = (),
) -> str:
    """Push HEAD to `refs/heads/<branch>` on `url`. Returns the pushed sha.

    `repo` MUST be a worker-owned publish repository (`prepare_publish_repo`),
    never the clone the agent worked in: this carries the tenant token, and a
    repository whose `.git/config` the agent can write could redirect it
    (`url.*.insteadOf`) or hand it to a helper the agent added. `_TOKEN_SAFE` is
    belt to that suspenders; the guarantee is the repository.

    THREE REFUSALS, none of which the agent can talk its way past:

    * the branch must carry `branch_prefix`. The worker derives the name from
      the task id; the agent never supplies it and this re-checks the derived
      value rather than trusting the caller;
    * the branch must not be one of `protected` -- in practice the repository's
      own default branch, read back from the forge rather than assumed to be
      `main`;
    * the push is never forced. A non-fast-forward means something other than
      this attempt moved the branch, and the correct response to that is to
      report it, not to win.
    """
    branch = validate_ref(branch) or ""
    if not branch:
        raise GitError("refusing to push without a branch name")
    if not branch.startswith(branch_prefix):
        raise GitError(f"refusing to push to {branch!r}: outside {branch_prefix!r}")
    if branch in protected:
        raise GitError(f"refusing to push to the protected branch {branch!r}")

    url = validate_repository_url(url)
    repo = Path(repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)

    cred_file = _write_credentials(url, token, private_dir)
    # `_TOKEN_SAFE` first (its trailing `credential.helper=` RESETS the helper
    # list), then the worker's own helper -- so the worker's is the only helper
    # git can consult for this token. This runs in a worker-owned publish repo.
    config_args = [
        *_TOKEN_SAFE,
        "-c", f"credential.helper=store --file={cred_file}",
    ]
    try:
        # `--no-follow-tags`: the refspec names one branch, but `push.followTags`
        # in the clone's config -- the agent's to set -- would push every
        # annotated tag reachable from it as well, with a tagger and a message
        # the agent chose. A swarm attempt has no tag to publish.
        code, _ = _git_text(
            [
                git_binary, *config_args, "push", "--no-verify", "--no-follow-tags",
                "--porcelain", "--", url, f"HEAD:refs/heads/{branch}",
            ],
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="publish-push",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        if code != 0:
            tail = ""
            err = logs_dir / "git-publish-push.err.log"
            if err.exists():
                tail = err.read_text(errors="replace")[-1200:].strip()
            raise GitError(f"push failed with exit {code}: {tail}")
    finally:
        # Same discipline as the clone: the credential exists for the length of
        # one git invocation and no longer. The runner has already exited by
        # the time this runs, but a park can bring another one back into the
        # same workspace, so "nobody is looking right now" is not a guarantee.
        try:
            cred_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("could not remove the git credential file", error=str(exc))

    code, text = _git_text(
        [git_binary, *_NO_HOOKS, "rev-parse", "HEAD"],
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug="publish-pushed-head",
        timeout_seconds=timeout_seconds,
        logger=logger,
    )
    head = text.strip() if code == 0 else ""
    logger.info("branch pushed", branch=branch, head=head or "unknown")
    return head


@dataclass(frozen=True)
class MergeOutcome:
    """What an integrator managed to bring together, and what it did not.

    `conflicted` and `missing` are NOT errors that abort the integration, and
    that is a deliberate choice rather than leniency. An integrator is the sink
    of a workflow whose other steps have already run, been billed and pushed
    their work; refusing the whole pull request because one of six contributors
    conflicts would throw away five successful attempts and leave the caller
    with nothing to look at. So the merge takes what merges and NAMES what it
    could not take -- in this object, in the run's result, and in the pull
    request body itself, where a human reviewing it cannot miss it.

    The failure this avoids is the opposite one: a pull request that claims to
    integrate a workflow while silently containing a subset of it.
    """

    merged: tuple[str, ...] = ()
    conflicted: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.conflicted and not self.missing


def merge_branches(
    *,
    repo: Path,
    url: str,
    branches: Sequence[str],
    token: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    author_name: str,
    author_email: str,
    git_binary: str = "git",
    branch_prefix: str = "swarm/",
) -> MergeOutcome:
    """Merge each contributor branch into the current HEAD, in the order given.

    `repo` MUST be a worker-owned publish repository (`prepare_publish_repo`):
    the contributor `fetch` below carries the tenant token, so it needs the same
    isolation as the push -- run in the clone, an `insteadOf` the agent wrote
    would redirect the authenticated fetch to another host.

    ORDER IS THE CALLER'S, AND IT MATTERS. swarm-api builds `integrates` from
    the topological prefix of the workflow, so branch N may depend on branch
    N-1 having landed. Merging in any other order turns a clean sequence into
    an artificial conflict.

    `--no-ff` on every merge, so the history shows which contributor each
    change came from even when the merge could have fast-forwarded. An
    integrator's whole purpose is attribution across steps; collapsing that is
    the one thing it must not do.

    A conflicted merge is aborted with `git merge --abort` before the next one
    is attempted, so a failed merge never leaks a half-applied index into the
    branch that follows it.
    """
    repo = Path(repo)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    url = validate_repository_url(url)

    merged: list[str] = []
    conflicted: list[str] = []
    missing: list[str] = []

    cred_file = _write_credentials(url, token, private_dir)
    # As in `push_branch`: `_TOKEN_SAFE` resets the credential-helper list before
    # the worker's own helper is added, and this runs in a worker-owned publish
    # repository -- the contributor fetch below carries the token.
    config_args = [
        *_TOKEN_SAFE,
        "-c", f"credential.helper=store --file={cred_file}",
        # The merge commits are the worker's, not the agent's. Without these
        # git refuses to commit at all in a container with no global config,
        # and the merge fails for a reason that reads like a conflict. All six
        # keys, not `user.*` alone: see `_worker_identity`.
        *_worker_identity(author_name, author_email),
    ]

    try:
        for raw in branches:
            branch = validate_ref(raw) or ""
            if not branch:
                missing.append(str(raw))
                continue
            # Re-checked here rather than trusted from the dispatch block: the
            # names arrive through task metadata, and the same prefix rule that
            # governs what this worker may PUSH governs what it may pull into a
            # branch it is about to push.
            if not branch.startswith(branch_prefix):
                logger.warning(
                    "refusing to merge a branch outside the prefix",
                    branch=branch,
                    prefix=branch_prefix,
                )
                missing.append(branch)
                continue

            slug = branch.replace("/", "-")
            code, _ = _git_text(
                [
                    git_binary, *config_args, "fetch", "--no-tags", "--depth=2147483647",
                    "--", url, f"refs/heads/{branch}",
                ],
                repo=repo,
                private_dir=private_dir,
                logs_dir=logs_dir,
                slug=f"integrate-fetch-{slug}",
                timeout_seconds=timeout_seconds,
                logger=logger,
            )
            if code != 0:
                # The contributor never pushed, or pushed under another name.
                # Absent, not conflicting -- the distinction is what tells a
                # reviewer whether to re-run a step or resolve a conflict.
                logger.warning("contributor branch not found on the remote", branch=branch)
                missing.append(branch)
                continue

            code, _ = _git_text(
                [
                    git_binary, *config_args, "merge", "--no-ff", "--no-edit",
                    "-m", f"swarm: integrate {branch}",
                    "FETCH_HEAD",
                ],
                repo=repo,
                private_dir=private_dir,
                logs_dir=logs_dir,
                slug=f"integrate-merge-{slug}",
                timeout_seconds=timeout_seconds,
                logger=logger,
            )
            if code != 0:
                abort_code, _ = _git_text(
                    [git_binary, *_NO_HOOKS, "merge", "--abort"],
                    repo=repo,
                    private_dir=private_dir,
                    logs_dir=logs_dir,
                    slug=f"integrate-abort-{slug}",
                    timeout_seconds=timeout_seconds,
                    logger=logger,
                )
                if abort_code != 0:
                    # An abort that fails leaves the tree in a state the next
                    # merge would silently build on. Stop rather than produce a
                    # pull request nobody can reason about.
                    raise GitError(
                        f"merging {branch} failed and `git merge --abort` then failed "
                        f"with exit {abort_code}; the working tree is mid-merge and "
                        "this integration cannot continue safely"
                    )
                logger.warning("contributor branch conflicts", branch=branch)
                conflicted.append(branch)
                continue

            merged.append(branch)
    finally:
        try:
            cred_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("could not remove the git credential file", error=str(exc))

    logger.info(
        "integration merge complete",
        merged=len(merged),
        conflicted=len(conflicted),
        missing=len(missing),
    )
    return MergeOutcome(
        merged=tuple(merged), conflicted=tuple(conflicted), missing=tuple(missing)
    )
