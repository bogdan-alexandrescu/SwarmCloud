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

import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
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
    logger.info("repository cloned", url=url, ref=ref, commit=commit, seconds=round(total, 2))
    return CloneResult(path=destination, url=url, ref=ref, commit=commit, duration_seconds=total)


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
    }


#: Passed to every git invocation below. `core.hooksPath` is the one that
#: matters: hooks are not transferred by a clone, but the AGENT can write
#: `.git/hooks/pre-commit` in the workspace, and a harvest that ran it would
#: hand arbitrary code a worker-side execution point after the sandbox was
#: supposed to be finished with. Both belts are worn -- `--no-verify` on the
#: commands that accept it, and a hooks path that cannot contain anything.
_NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


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
#: repository the worker built has no such key in it.
#:
#: The overrides below are belt to that suspenders: each neutralises a setting a
#: `-c` CAN override, so that even a change that later reintroduced inherited
#: configuration could not move the credential. The worker's own credential
#: helper is appended by the caller AFTER the empty `credential.helper` here,
#: which discards any accumulated helper list before the worker's is added
#: (git treats an empty value as a reset).
_TOKEN_SAFE = [
    *_NO_HOOKS,                                 # no repository hook runs
    "-c", "protocol.version=2",
    "-c", "credential.helper=",                 # reset: no inherited helper survives
    "-c", "http.sslVerify=true",                # only over verified TLS
    "-c", "http.proxy=",                        # no proxy may sit in front of the forge
    "-c", "http.extraHeader=",                  # no injected header rides with the request
    "-c", "core.fsmonitor=",                    # no filesystem-monitor program is run
]


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

    Output goes to a file rather than a pipe because `run_child` is the only
    thing in this worker that knows how to kill a process group on a timeout,
    and reusing it is what keeps a wedged git from outliving the attempt.
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
        text = out.read_text(errors="replace")
    except OSError:
        text = ""
    return result.exit_code, text


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
) -> WorkSummary:
    """Describe what the agent did, and write one applicable patch.

    Read-only with respect to the repository's history: nothing is committed,
    nothing is pushed, no credential is needed or used. The one mutation is
    `add --intent-to-add`, explained at its call below.

    `base` is the commit the clone landed on. Without it there is nothing to
    diff against and only the dirty list can be reported -- which is still
    worth having, so this degrades rather than raising.
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
    if base and head and base != head:
        log_code, log_text = run(
            [
                *g, "log", "--numstat", "--no-color", "--no-merges",
                f"--format={_RS}%H{_FS}%s{_FS}%an{_FS}%aI",
                f"{base}..HEAD",
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
        diff_code, _ = _git_text(
            [*g, "diff", "--binary", "--no-color", base, "--"],
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
    ident = [
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
    ]
    g = [git_binary, *_NO_HOOKS, *ident]

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

    commit_code, _ = run(
        [*g, "commit", "--no-verify", "--message", message], "publish-commit"
    )
    if commit_code != 0:
        raise GitError("could not commit the agent's uncommitted changes")

    code, text = run([git_binary, *_NO_HOOKS, "rev-parse", "HEAD"], "publish-head")
    return text.strip() if code == 0 else None


def prepare_publish_repo(
    *,
    source_repo: Path,
    work_head: str | None,
    publish_dir: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    local_branch: str = "swarm-publish",
) -> Path:
    """Build a fresh, worker-owned repository holding the agent's committed work.

    WHY THIS EXISTS. The push and the integrator's merge authenticate with the
    tenant's token, and a token that reaches many repositories makes where it is
    allowed to go a tenant-isolation guarantee (invariant 9). Run inside the
    repository the agent just edited, those commands honour that repository's
    `.git/config` -- which the agent can write. A `credential.helper` there is
    handed the token when the request succeeds; a `url.<host>.insteadOf` /
    `pushInsteadOf` there redirects the authenticated push, and the credential
    with it, to a host of the agent's choosing. No `-c` override disables
    `insteadOf`, so the only robust defence is to run the token-bearing commands
    in a repository whose configuration the worker wrote.

    This builds that repository. It is created empty with `git init`, so its
    config is the worker's; the agent's committed work is transferred in by a
    LOCAL fetch of the source's current HEAD, carrying NO token -- so nothing the
    source repository's configuration could do can move a credential that is not
    present. The remote is never taken from here; the caller passes the validated
    `repository_url` to `push_branch` / `merge_branches`.

    `source_repo` is the agent's clone, whose HEAD the worker has just set (the
    harvest's `commit_dirty` runs first, so HEAD already carries the auto-commit
    when there was one). `work_head`, when known, is only checked against what
    was transferred; it is never trusted as a ref name from the agent.
    """
    source_repo = Path(source_repo)
    publish_dir = Path(publish_dir)
    private_dir = Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    if publish_dir.exists():
        shutil.rmtree(publish_dir, ignore_errors=True)
    publish_dir.mkdir(parents=True, exist_ok=True)

    def step(argv: list[str], slug: str) -> tuple[int, str]:
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

    # Transfer by LOCAL PATH and by the source's own HEAD ref. No token is in
    # the environment or in any config for this step, so even if the source
    # repository's configuration were consulted it could not move a credential.
    # `--no-tags`/`--no-recurse-submodules` keep it to exactly the commit line.
    fetch_code, _ = step(
        [
            git_binary, *_NO_HOOKS, "fetch", "--no-tags", "--no-recurse-submodules",
            "--", str(source_repo), "HEAD",
        ],
        "publish-transfer",
    )
    if fetch_code != 0:
        raise GitError("could not transfer the agent's work into the publish repository")

    checkout_code, _ = step(
        [git_binary, *_NO_HOOKS, "checkout", "--quiet", "-B", local_branch, "FETCH_HEAD"],
        "publish-checkout",
    )
    if checkout_code != 0:
        raise GitError("could not check out the transferred work in the publish repository")

    if work_head:
        head_code, head_text = step(
            [git_binary, *_NO_HOOKS, "rev-parse", "HEAD"], "publish-verify-head"
        )
        got = head_text.strip() if head_code == 0 else ""
        if got and got != work_head:
            # The source's HEAD moved between the worker computing `work_head`
            # and this transfer. In a single-threaded attempt that cannot happen
            # from the worker itself, so it means something else wrote the clone;
            # refuse rather than push a commit the worker did not vouch for.
            raise GitError(
                "the transferred HEAD does not match the work the worker committed"
            )

    logger.info("publish repository prepared", branch=local_branch)
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
    never the clone the agent worked in: this command carries the tenant token,
    and a repository whose `.git/config` the agent can write could redirect it
    (`url.*.insteadOf`) or hand it to a helper the agent added. `_TOKEN_SAFE`
    below is belt to that suspenders -- it resets the credential-helper list to
    the worker's own and pins the transport -- but the guarantee is the repository.

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
    # `_TOKEN_SAFE` first (which ends by RESETTING the credential-helper list),
    # then the worker's own helper -- so the worker's is the only helper git can
    # consult for this token.
    config_args = [
        *_TOKEN_SAFE,
        "-c", f"credential.helper=store --file={cred_file}",
    ]
    try:
        code, _ = _git_text(
            [
                git_binary, *config_args, "push", "--no-verify", "--porcelain",
                "--", url, f"HEAD:refs/heads/{branch}",
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
    # repository. The contributor fetch below carries the token, so it must be as
    # isolated as the push.
    config_args = [
        *_TOKEN_SAFE,
        "-c", f"credential.helper=store --file={cred_file}",
        # The merge commits are the worker's, not the agent's. Without these
        # git refuses to commit at all in a container with no global config,
        # and the merge fails for a reason that reads like a conflict.
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
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
