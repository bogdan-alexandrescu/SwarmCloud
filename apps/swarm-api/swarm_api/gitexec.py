"""swarm-api's git execution: fetch → bundle and bundle → push, with the forge token.

docs/runner-git-tools.md §1.3 and §2.2 are the design; this is lane G4 of
#1225. It is a LIBRARY. It knows nothing about tasks, attempts, leases or
policy: lane G5 (`gittools.py`) authenticates the worker, checks the fence,
derives the branch from the signed spec, reads the slot and only then calls
here. What this module promises is narrower and is about the token alone.

WHY SWARM-API, AND WHY THE TOKEN NEVER LEAVES THIS PROCESS. A runner's agent
and its worker share a uid, so anything the worker does with a token while the
agent is alive the agent can reach (§0). The owner's Q4 answer (2026-10-13)
put the execution here instead, where the agent has no process. What crosses
the boundary is git objects, as bundles; the token does not.

HOW THE TOKEN IS HELD, PER CALL:

* **a scratch directory per call**, `mkdtemp` (0700) under `scratch_root`,
  removed in `finally` -- the bare repository, the bundle and the credential
  file all live in it, and nothing outlives the call;
* **a credential file**, created `O_CREAT|O_EXCL|O_NOFOLLOW` at mode 0600 from
  the first byte, naming `x-access-token` at the forge host only, handed to
  git's `store` helper after an empty `credential.helper=` resets any other
  helper, and unlinked in a `finally` as soon as the last remote command has
  run. The token is never in an argv (`/proc/<pid>/cmdline` is world-readable)
  and never in a child's environment;
* **a fixed, minimal environment** for every git: no inherited proxy, no
  global or system config, no askpass that could answer, no replace objects;
* **only https to the forge**: the URL must pass `forge.may_receive_forge_token`
  (#307), and `protocol.allow=never` with `protocol.https.allow=always` stops
  git from following a URL anywhere else. Redirects are not followed;
* **stderr is scrubbed** (`scrub`) before it is put in any error: the token
  bare and percent-encoded, URL userinfo of any kind, then the shared
  credential patterns (`swarm_api.redaction.redact`).

HOW A PUSH IS HELD:

* **never forced.** The argv is fixed: `push --no-verify --no-follow-tags
  --porcelain -- <url> <sha>:refs/heads/<branch>`. A non-fast-forward is the
  forge's refusal, relayed as `non_fast_forward` with both shas. The one
  exception is the owner's Q1 answer (A, 2026-10-13): when the CALLER passes
  `lease`, the tip swarm-api itself recorded for this task, the push adds
  `--force-with-lease=refs/heads/<branch>:<lease>`, which replaces the branch
  only if it is still exactly that sha. A lease is never read from the
  request; G5 owns where it comes from. A blind `--force` has no spelling here;
* **`swarm/` branches only** (Q5 = A), checked again here whatever G5 derived;
* **the bundle is bounded before git sees it**: its size against
  `max_bundle_bytes` (Q6: 256 MiB), then its header parsed here -- one ref,
  full-sha prerequisites, at most `MAX_PREREQUISITES` -- before anything is
  written or unpacked. The objects are unbundled in a step that carries no
  credential, then `git fsck`ed;
* **the fence is checked again** (`before_push`, §1.4, #453's rule) after the
  bundle is unpacked and immediately before `git push`.

FETCH → BUNDLE. The worker reports the shas its clone already has (`haves`).
Each is fetched at depth 1, so the fetch of `ref` negotiates against them and
the bundle (`refs/swarm/want ^<haves>`) carries only what the clone lacks. A
have the remote does not know is skipped, not an error: the bundle is then
merely larger.

Codes (`GitExecError.code`): `bad_remote`, `bad_ref`, `bad_bundle`,
`too_large`, `non_fast_forward`, `remote_rejected`, `timed_out`, `failed`.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence
from urllib.parse import quote, urlparse

from swarm_api.forge import may_receive_forge_token
from swarm_api.redaction import MASK, redact

#: Q6 = A (owner, 2026-10-13): a 256 MiB bundle, either way. Bounds the
#: memory and tmpfs one call can take in swarm-api.
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
#: How long one git command may run. A fetch of a large repository with no
#: have is the slow case; a request that needs longer is the wrong shape.
GIT_TIMEOUT_SECONDS = 300
#: Haves a fetch is negotiated against. A worker reports its branch tips; a
#: handful is the real number, and each is one depth-1 fetch.
MAX_HAVES = 16
#: Prerequisites a push bundle may name. `<remote tip>..HEAD` has one per
#: merged-in line of history; each costs a depth-1 fetch.
MAX_PREREQUISITES = 16
#: The most of a bundle's header read before its pack must have started.
BUNDLE_HEADER_MAX_BYTES = 64 * 1024
#: The most of git's stderr an error carries, after scrubbing.
STDERR_TAIL_CHARS = 2000

_SHA = re.compile(r"^[0-9a-f]{40}$")
#: A ref name the agent may ask for: what `gitops.validate_ref` accepts, minus
#: everything git gives meaning to (`..`, `@{`, `:`, `+`, a leading `-`).
_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_HELPER_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@'\"]*@")

#: Where a fetch lands in the scratch repository, and the ref the bundle names.
WANT_REF = "refs/swarm/want"
_PUSH_REF = "refs/swarm/push"
_TIP_REF = "refs/swarm/remote-tip"

_REFUSED_FF = ("non-fast-forward", "fetch first", "stale info")


class GitExecError(Exception):
    """A git call that did not do what was asked. `message` is already scrubbed."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class GitRun:
    code: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, cwd: Path, env: dict[str, str],
                 timeout: float) -> GitRun: ...


def run_git(argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: float) -> GitRun:
    """Run one git command with no stdin; a timeout is `timed_out`."""
    try:
        out = subprocess.run(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise GitExecError("timed_out", f"git ran longer than {timeout:g}s") from None
    return GitRun(out.returncode, out.stdout.decode(errors="replace"),
                  out.stderr.decode(errors="replace"))


@dataclass(frozen=True)
class FetchedBundle:
    """What a fetch resolved, and the bundle of what the worker lacks.

    `bundle` is None (and `size` 0) when the worker already has `sha`. The
    bundle's one ref is `ref` (`refs/swarm/want`); the worker applies it with
    `git fetch <bundle> refs/swarm/want:<where it belongs>`.
    """

    sha: str
    bundle: bytes | None
    size: int
    ref: str = WANT_REF


@dataclass(frozen=True)
class PushedBranch:
    branch: str
    old_sha: str | None
    new_sha: str
    #: True when the push replaced history under a lease (Q1 = A).
    rewrite: bool = False


@dataclass(frozen=True)
class _BundleHeader:
    prerequisites: tuple[str, ...]
    tip: str
    ref: str


def scrub(text: str, secrets: Iterable[str]) -> str:
    """`text` with each secret (bare and percent-encoded) and all URL userinfo masked.

    Userinfo is stripped whatever it holds: git prints the URL it was given or
    was redirected to, and a credential in one that this call never registered
    is still a credential.
    """
    literals: set[str] = set()
    for secret in secrets:
        if secret:
            literals |= {secret, quote(secret, safe="")}
    for literal in sorted(literals, key=len, reverse=True):
        text = text.replace(literal, MASK)
    text = _URL_USERINFO.sub(lambda m: f"{m.group(1)}{MASK}@", text)
    return redact(text, extra=tuple(literals)).text


def _valid_name(name: str) -> bool:
    return bool(
        _NAME.match(name)
        and not name.startswith(("-", "/"))
        and not name.endswith(("/", ".", ".lock"))
        and ".." not in name
        and "//" not in name
        and "/." not in name
    )


def _check_ref(ref: str) -> str:
    if not isinstance(ref, str) or not (_SHA.match(ref) or _valid_name(ref)):
        raise GitExecError("bad_ref", "ref must be a branch or tag name, or a 40-hex sha")
    return ref


def _check_branch(branch: str) -> str:
    if (not isinstance(branch, str) or not _valid_name(branch)
            or not branch.startswith("swarm/") or branch == "swarm/"):
        raise GitExecError("bad_ref", "only a swarm/ branch is pushed")
    return branch


def _parse_header(head: bytes) -> _BundleHeader:
    """The prerequisites and the one ref of a v2 bundle, or `bad_bundle`.

    Parsed here rather than by `git bundle list-heads` so that nothing the
    agent built reaches git before it has been bounded.
    """
    end = head.find(b"\n\n")
    if end < 0:
        raise GitExecError("bad_bundle", "not a git bundle, or its header is too long")
    try:
        lines = head[:end].decode("ascii").split("\n")
    except UnicodeDecodeError:
        raise GitExecError("bad_bundle", "the bundle header is not ASCII") from None
    if lines[0] != "# v2 git bundle":
        raise GitExecError("bad_bundle", "only a v2 git bundle is accepted")
    prerequisites: list[str] = []
    refs: list[tuple[str, str]] = []
    for line in lines[1:]:
        if line.startswith("-"):
            sha = line[1:].split(" ", 1)[0]
            if not _SHA.match(sha):
                raise GitExecError("bad_bundle", "a prerequisite is not a 40-hex sha")
            prerequisites.append(sha)
            continue
        sha, _, name = line.partition(" ")
        if not _SHA.match(sha) or not (name == "HEAD" or (
                name.startswith("refs/heads/") and _valid_name(name))):
            raise GitExecError("bad_bundle", "a bundle ref must be HEAD or refs/heads/<name>")
        refs.append((sha, name))
    if len(refs) != 1:
        raise GitExecError("bad_bundle", f"a bundle carries exactly one ref, not {len(refs)}")
    if len(prerequisites) > MAX_PREREQUISITES:
        raise GitExecError("bad_bundle", f"more than {MAX_PREREQUISITES} prerequisites")
    if head[end + 2:end + 6] != b"PACK":
        raise GitExecError("bad_bundle", "the bundle carries no pack")
    return _BundleHeader(tuple(prerequisites), refs[0][0], refs[0][1])


_BASE_CONFIG = (
    "-c", "core.hooksPath=/dev/null",      # no hook runs, here or in a repository git opens
    "-c", "core.fsmonitor=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "protocol.version=2",
    "-c", "protocol.allow=never",          # no transport but the ones named below
    "-c", "credential.helper=",            # reset: no helper but this call's own
    "-c", "http.sslVerify=true",
    "-c", "http.proxy=",
    "-c", "http.extraHeader=",
    "-c", "http.followRedirects=false",    # the token goes to the host it was written for
    "-c", "fetch.recurseSubmodules=false",
    "-c", "submodule.recurse=false",
)


@dataclass
class GitExec:
    """Run swarm-api's git with one tenant token, one call at a time.

    `local_remotes` is a TEST seam: it accepts `file://` URLs so a bare
    repository can stand in for github.com. Production never sets it, and then
    every URL must be one `may_receive_forge_token` accepts.
    """

    scratch_root: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "swarm-gitexec")
    git_binary: str = "git"
    timeout_seconds: float = GIT_TIMEOUT_SECONDS
    max_bundle_bytes: int = MAX_BUNDLE_BYTES
    runner: Runner = run_git
    local_remotes: bool = False

    # -- the request's shape, checked before any git ----------------------

    def _check_url(self, url: str) -> tuple[str, str]:
        """The URL and the host its credential entry names, or `bad_remote`."""
        if not isinstance(url, str) or url.startswith("-") or any(
                ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
            raise GitExecError("bad_remote", "the remote is not a forge URL")
        if may_receive_forge_token(url) and re.match(r"^[A-Za-z0-9._~/-]*$", urlparse(url).path):
            return url, urlparse(url).netloc
        if self.local_remotes and url.startswith("file:///"):
            # A file:// remote never consults a credential helper; the entry
            # names the forge so the file's lifecycle is the production one.
            return url, "github.com"
        raise GitExecError("bad_remote", "the forge token is sent only to github.com over https")

    # -- the scratch directory and the credential file --------------------

    @contextmanager
    def _scratch(self) -> Iterator[Path]:
        self.scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        scratch = Path(tempfile.mkdtemp(prefix="call-", dir=self.scratch_root))
        try:
            (scratch / "home").mkdir(mode=0o700)
            yield scratch
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @contextmanager
    def _credential(self, scratch: Path, host: str, token: str) -> Iterator[Path]:
        path = scratch / "credentials"
        # git runs the helper through a shell-like split of this one config
        # value, so a scratch path with a space or a metacharacter would
        # change what runs. mkdtemp's names are safe; `scratch_root` is ours.
        if not _HELPER_PATH.match(str(path)):
            raise GitExecError("failed", "the scratch directory's path is not one a credential "
                                         "helper can be given")
        entry = f"https://x-access-token:{quote(token, safe='')}@{host}\n".encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            code = errno.errorcode.get(exc.errno or -1, "no errno")
            raise GitExecError("failed", f"could not write the credential file ({code})") from None
        try:
            try:
                view = memoryview(entry)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            yield path
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _env(self, scratch: Path) -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(scratch / "home"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/true",
            "SSH_ASKPASS": "/bin/true",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
        }

    # -- running git -------------------------------------------------------

    def _git(self, scratch: Path, repo: Path, args: Sequence[str], *, token: str,
             credential: Path | None = None, extra: Sequence[str] = ()) -> GitRun:
        config = list(_BASE_CONFIG)
        config += ["-c", "protocol.https.allow=always"]
        if self.local_remotes:
            config += ["-c", "protocol.file.allow=always"]
        config += list(extra)
        if credential is not None:
            config += ["-c", f"credential.helper=store --file={credential}"]
        argv = [self.git_binary, *config, "--git-dir", str(repo), *args]
        run = self.runner(argv, cwd=scratch, env=self._env(scratch), timeout=self.timeout_seconds)
        return GitRun(run.code, scrub(run.stdout, (token,)), scrub(run.stderr, (token,)))

    def _must(self, run: GitRun, what: str, code: str = "failed") -> GitRun:
        if run.code != 0:
            raise GitExecError(code, f"{what} failed with exit {run.code}: "
                                     f"{run.stderr.strip()[-STDERR_TAIL_CHARS:]}")
        return run

    def _init(self, scratch: Path) -> Path:
        repo = scratch / "repo.git"
        self._must(self.runner([self.git_binary, *_BASE_CONFIG, "init", "--quiet", "--bare",
                                str(repo)], cwd=scratch, env=self._env(scratch),
                               timeout=self.timeout_seconds), "git init")
        return repo

    def _resolve(self, scratch: Path, repo: Path, ref: str, token: str) -> str | None:
        run = self._git(scratch, repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                        token=token)
        sha = run.stdout.strip()
        return sha if run.code == 0 and _SHA.match(sha) else None

    # -- fetch → bundle ----------------------------------------------------

    def fetch_bundle(self, *, url: str, token: str, ref: str,
                     haves: Sequence[str] = ()) -> FetchedBundle:
        """Fetch `ref` from `url`; bundle what is not reachable from `haves`."""
        url, host = self._check_url(url)
        ref = _check_ref(ref)
        haves = list(dict.fromkeys(haves))
        if len(haves) > MAX_HAVES or not all(isinstance(h, str) and _SHA.match(h) for h in haves):
            raise GitExecError("bad_ref", f"haves are at most {MAX_HAVES} 40-hex shas")
        with self._scratch() as scratch:
            repo = self._init(scratch)
            known: list[str] = []
            with self._credential(scratch, host, token) as cred:
                for have in haves:
                    run = self._git(scratch, repo, ["fetch", "--quiet", "--no-tags",
                                                    "--no-write-fetch-head", "--depth=1", "--",
                                                    url, have], token=token, credential=cred)
                    if run.code == 0:
                        known.append(have)
                self._must(self._git(scratch, repo, ["fetch", "--quiet", "--no-tags",
                                                     "--no-write-fetch-head", "--", url,
                                                     f"{ref}:{WANT_REF}"],
                                     token=token, credential=cred), f"fetch of {ref}")
            sha = self._resolve(scratch, repo, WANT_REF, token)
            if sha is None:
                raise GitExecError("bad_ref", f"{ref} does not name a commit")
            if sha in known:
                return FetchedBundle(sha, None, 0)
            out = scratch / "out.bundle"
            run = self._git(scratch, repo, ["bundle", "create", "--quiet", str(out), WANT_REF,
                                            *[f"^{h}" for h in known]], token=token)
            if run.code != 0 and "empty bundle" in run.stderr:
                return FetchedBundle(sha, None, 0)       # an ancestor of a have
            self._must(run, "bundle create")
            size = out.stat().st_size
            if size > self.max_bundle_bytes:
                raise GitExecError("too_large", f"the bundle is {size} bytes, over the "
                                                f"{self.max_bundle_bytes}-byte cap")
            return FetchedBundle(sha, out.read_bytes(), size)

    # -- bundle → push -----------------------------------------------------

    def push_bundle(self, *, url: str, token: str, bundle: bytes | Path, branch: str,
                    lease: str | None = None,
                    before_push: Callable[[], None] | None = None) -> PushedBranch:
        """Unbundle `bundle` and push its one ref to `refs/heads/<branch>`, never forced.

        `lease`, when given, is the tip swarm-api recorded for this task's
        branch (Q1 = A); the push may then replace exactly that sha.
        `before_push` re-checks the fence and raises to stop the push.
        """
        url, host = self._check_url(url)
        branch = _check_branch(branch)
        if lease is not None and not (isinstance(lease, str) and _SHA.match(lease)):
            raise GitExecError("bad_ref", "a lease is the 40-hex sha swarm-api recorded")
        head = self._bounded_head(bundle)
        header = _parse_header(head)
        with self._scratch() as scratch:
            if isinstance(bundle, Path):
                source = bundle
            else:
                source = scratch / "in.bundle"
                fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(bundle)
            repo = self._init(scratch)
            with self._credential(scratch, host, token) as cred:
                old = self._remote_tip(scratch, repo, url, branch, token, cred)
                for sha in header.prerequisites:
                    run = self._git(scratch, repo, ["fetch", "--quiet", "--no-tags",
                                                    "--no-write-fetch-head", "--depth=1", "--",
                                                    url, sha], token=token, credential=cred)
                    if run.code != 0:
                        raise GitExecError("bad_bundle", f"the bundle is built on {sha}, which "
                                                         "the remote does not have; fetch again")
                # The one step that reads what the agent built, so it carries
                # no credential and its objects are checked. Not through
                # `fetch.fsckObjects`: git 2.39 (bookworm's) does not apply it
                # to a fetch from a bundle -- measured, a commit with no
                # committer went through. `git fsck` over the scratch
                # repository instead, which holds only depth-1 tips from the
                # forge and the bundle's objects, so it is bounded by the cap.
                self._must(self._git(scratch, repo, ["fetch", "--quiet", "--no-tags",
                                                     "--no-write-fetch-head", "--", str(source),
                                                     f"{header.ref}:{_PUSH_REF}"],
                                     token=token, extra=("-c", "protocol.file.allow=always")),
                           "unbundle", code="bad_bundle")
                self._must(self._git(scratch, repo, ["fsck", "--no-dangling", "--no-progress"],
                                     token=token), "fsck of the bundle's objects",
                           code="bad_bundle")
                new = self._resolve(scratch, repo, _PUSH_REF, token)
                if new != header.tip:
                    raise GitExecError("bad_bundle", "the bundle's ref is not a commit")
                if old == new:
                    return PushedBranch(branch, old, new)
                if before_push is not None:
                    before_push()
                force = [f"--force-with-lease=refs/heads/{branch}:{lease}"] if lease else []
                run = self._git(scratch, repo, ["push", "--no-verify", "--no-follow-tags",
                                                "--porcelain", *force, "--", url,
                                                f"{new}:refs/heads/{branch}"],
                                token=token, credential=cred)
            return self._pushed(run, branch, old, new)

    def _bounded_head(self, bundle: bytes | Path) -> bytes:
        """The bundle's first bytes, after its size is held to the cap."""
        if isinstance(bundle, Path):
            try:
                info = bundle.lstat()
            except OSError:
                raise GitExecError("bad_bundle", "the bundle file cannot be read") from None
            if not stat.S_ISREG(info.st_mode):
                raise GitExecError("bad_bundle", "the bundle is not a regular file")
            size = info.st_size
        elif isinstance(bundle, (bytes, bytearray)):
            size = len(bundle)
        else:
            raise GitExecError("bad_bundle", "a bundle is bytes or a file")
        if size > self.max_bundle_bytes:
            raise GitExecError("too_large", f"the bundle is {size} bytes, over the "
                                            f"{self.max_bundle_bytes}-byte cap")
        if isinstance(bundle, Path):
            with bundle.open("rb") as handle:
                return handle.read(BUNDLE_HEADER_MAX_BYTES)
        return bytes(bundle[:BUNDLE_HEADER_MAX_BYTES])

    def _remote_tip(self, scratch: Path, repo: Path, url: str, branch: str, token: str,
                    cred: Path) -> str | None:
        """The branch's current tip on the remote, fetched at depth 1; None if it is new."""
        run = self._must(self._git(scratch, repo, ["ls-remote", "--heads", "--", url,
                                                   f"refs/heads/{branch}"],
                                   token=token, credential=cred), "ls-remote")
        tips = [line.split("\t", 1)[0] for line in run.stdout.splitlines()
                if line.endswith(f"\trefs/heads/{branch}")]
        if not tips:
            return None
        self._must(self._git(scratch, repo, ["fetch", "--quiet", "--no-tags",
                                             "--no-write-fetch-head", "--depth=1", "--", url,
                                             f"refs/heads/{branch}:{_TIP_REF}"],
                             token=token, credential=cred), "fetch of the branch tip")
        return self._resolve(scratch, repo, _TIP_REF, token) or tips[0]

    def _pushed(self, run: GitRun, branch: str, old: str | None, new: str) -> PushedBranch:
        """Read `--porcelain`: `<flag>\\t<from>:<to>\\t<summary>` for our one ref."""
        target = f":refs/heads/{branch}"
        line = next((ln for ln in run.stdout.splitlines()
                     if "\t" in ln and ln.split("\t")[1].endswith(target)), "")
        flag = line[:1]
        if run.code == 0 and flag in (" ", "*", "+", "="):
            return PushedBranch(branch, old, new, rewrite=flag == "+")
        summary = line.split("\t")[2] if line.count("\t") >= 2 else ""
        detail = {"branch": branch, "remote_sha": old, "new_sha": new}
        if flag == "!" and any(reason in summary for reason in _REFUSED_FF):
            raise GitExecError("non_fast_forward",
                               f"{branch} on the remote is at {old}, which {new} does not "
                               "descend from; fetch, rebase and push again", detail=detail)
        if flag == "!":
            raise GitExecError("remote_rejected", f"the forge refused the push: {summary}",
                               detail=detail)
        raise GitExecError("failed", f"push failed with exit {run.code}: "
                                     f"{run.stderr.strip()[-STDERR_TAIL_CHARS:]}", detail=detail)
