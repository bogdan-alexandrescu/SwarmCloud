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

BOUNDED IN THE KERNEL, NOT ONLY BY THE CAP (security review, 2026-10-13).
Every git runs under RLIMIT_AS (`max_git_memory_bytes`) and RLIMIT_FSIZE (the
bundle cap), set by `_LIMIT_SHIM` before it becomes git; a call has one
deadline across all its commands; at most `MAX_CONCURRENT_CALLS` run in a
process, and a timeout kills git's whole process group. A write or an
allocation over a limit is `too_large`.

Codes (`GitExecError.code`): `bad_remote`, `bad_ref`, `bad_bundle`,
`too_large`, `non_fast_forward`, `remote_rejected`, `busy`, `timed_out`,
`failed`.
"""

from __future__ import annotations

import contextvars
import errno
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
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
#: The address space one git process may map (RLIMIT_AS). The security review
#: (2026-10-13) measured a 408 KB bundle of a 400 MiB zero blob peaking at
#: 403 MB RSS in the unbundle and again in fsck: the byte cap bounds what
#: arrives, not what it inflates to, and a delta declares its result size up
#: front. swarm-api is a shared 1 GiB service, so the bound is per process,
#: in the kernel. `core.bigFileThreshold` keeps an honest large blob streamed
#: under it; `pack.threads=1` and MALLOC_ARENA_MAX keep the address space a
#: function of the data rather than of the core count.
MAX_GIT_MEMORY_BYTES = 512 * 1024 * 1024
#: A whole call, every git command in it. Up to MAX_HAVES or
#: MAX_PREREQUISITES depth-1 fetches each had GIT_TIMEOUT_SECONDS of their
#: own, which held one request for over an hour.
CALL_DEADLINE_SECONDS = 600
#: Calls running at once in one swarm-api process. Each may hold a bundle at
#: the cap in tmpfs (memory, on Cloud Run) and a git under MAX_GIT_MEMORY_BYTES;
#: a call over the limit is refused `busy` at once rather than queued in a
#: request thread.
MAX_CONCURRENT_CALLS = 2
_CALL_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)
_DEADLINE: contextvars.ContextVar[float] = contextvars.ContextVar("gitexec_deadline")

#: Run as `python -I -c _LIMIT_SHIM <as> <fsize> git ...`: set the two limits,
#: then become git. Not `preexec_fn`, which is unsafe in a threaded server, and
#: not `sh -c ulimit`, whose units differ by shell. Python leaves SIGXFSZ
#: ignored across the exec, so a write over RLIMIT_FSIZE fails EFBIG and git
#: says "File too large" (measured, git 2.39.5) rather than dying silently.
_LIMIT_SHIM = (
    "import os, resource, sys\n"
    "a, f = int(sys.argv[1]), int(sys.argv[2])\n"
    "resource.setrlimit(resource.RLIMIT_AS, (a, a))\n"
    "resource.setrlimit(resource.RLIMIT_FSIZE, (f, f))\n"
    "os.execvp(sys.argv[3], sys.argv[3:])\n"
)
_OVER_LIMIT = re.compile(r"File too large|[Oo]ut of memory|Cannot allocate memory")

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
    """Run one git command with no stdin; a timeout is `timed_out`.

    In its own session, so a timeout kills the whole group: git's
    `git-remote-https`, `index-pack` and `credential-store` children would
    otherwise outlive it and keep writing into a scratch directory being
    removed.
    """
    proc = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=max(timeout, 0.001))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise GitExecError("timed_out", f"git ran longer than {timeout:g}s") from None
    return GitRun(proc.returncode, out.decode(errors="replace"), err.decode(errors="replace"))


@dataclass(frozen=True)
class FetchedBundle:
    """What a fetch resolved, and the bundle of what the worker lacks.

    `bundle` is None (and `size` 0) when the worker already has `sha`, and
    None when the caller passed `deliver` and took the file instead. The
    bundle's one ref is `ref` (`refs/swarm/want`); the worker applies it with
    `git fetch <bundle> refs/swarm/want:<where it belongs>`.
    """

    sha: str
    bundle: bytes | None
    size: int
    ref: str = WANT_REF
    #: What `deliver` returned, when the caller took the bundle as a file.
    delivered: Any = None


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
    # Bounded by MAX_GIT_MEMORY_BYTES and the file cap (review, 2026-10-13):
    # what arrives stays one pack the file limit covers, never loose objects;
    # a large blob is streamed, not held; one thread, small pack windows.
    "-c", "fetch.unpackLimit=1",
    "-c", "transfer.unpackLimit=1",
    "-c", "core.bigFileThreshold=1m",
    "-c", "pack.threads=1",
    "-c", "core.packedGitWindowSize=32m",
    "-c", "core.packedGitLimit=128m",
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
    max_git_memory_bytes: int = MAX_GIT_MEMORY_BYTES
    deadline_seconds: float = CALL_DEADLINE_SECONDS
    #: Shared by every GitExec in the process unless a test passes its own.
    slots: Any = _CALL_SLOTS

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
            "MALLOC_ARENA_MAX": "2",
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
        return self._run(scratch, [*config, "--git-dir", str(repo), *args], token=token)

    def _run(self, scratch: Path, args: Sequence[str], *, token: str) -> GitRun:
        """git under the per-process limits, within what is left of the call's deadline."""
        left = _DEADLINE.get() - time.monotonic()
        if left <= 0:
            raise GitExecError("timed_out", f"the call ran longer than {self.deadline_seconds:g}s")
        argv = [sys.executable, "-I", "-c", _LIMIT_SHIM, str(self.max_git_memory_bytes),
                str(self.max_bundle_bytes), self.git_binary, *args]
        run = self.runner(argv, cwd=scratch, env=self._env(scratch),
                          timeout=min(self.timeout_seconds, left))
        return GitRun(run.code, scrub(run.stdout, (token,)), scrub(run.stderr, (token,)))

    @contextmanager
    def _call(self) -> Iterator[Path]:
        """A slot, a deadline and a scratch directory for one call."""
        if not self.slots.acquire(blocking=False):
            raise GitExecError("busy", f"{MAX_CONCURRENT_CALLS} git calls are already running; "
                                       "try again shortly")
        reset = _DEADLINE.set(time.monotonic() + self.deadline_seconds)
        try:
            with self._scratch() as scratch:
                yield scratch
        finally:
            _DEADLINE.reset(reset)
            self.slots.release()

    def _must(self, run: GitRun, what: str, code: str = "failed") -> GitRun:
        if run.code != 0 and _OVER_LIMIT.search(run.stderr):
            raise GitExecError("too_large", f"{what} went over the {self.max_bundle_bytes}-byte "
                                            f"file cap or the {self.max_git_memory_bytes}-byte "
                                            "memory bound")
        if run.code != 0:
            raise GitExecError(code, f"{what} failed with exit {run.code}: "
                                     f"{run.stderr.strip()[-STDERR_TAIL_CHARS:]}")
        return run

    def _init(self, scratch: Path) -> Path:
        repo = scratch / "repo.git"
        self._must(self._run(scratch, [*_BASE_CONFIG, "init", "--quiet", "--bare", str(repo)],
                             token=""), "git init")
        return repo

    def _resolve(self, scratch: Path, repo: Path, ref: str, token: str) -> str | None:
        run = self._git(scratch, repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                        token=token)
        sha = run.stdout.strip()
        return sha if run.code == 0 and _SHA.match(sha) else None

    # -- fetch → bundle ----------------------------------------------------

    def fetch_bundle(self, *, url: str, token: str, ref: str, haves: Sequence[str] = (),
                     deliver: Callable[[Path, int], Any] | None = None) -> FetchedBundle:
        """Fetch `ref` from `url`; bundle what is not reachable from `haves`.

        `deliver(path, size)`, when given, is handed the bundle file while it
        exists -- to stream it to the tenant's GCS prefix, say -- instead of a
        copy of up to the cap in memory; what it returns is `delivered`.
        """
        url, host = self._check_url(url)
        ref = _check_ref(ref)
        haves = list(dict.fromkeys(haves))
        if len(haves) > MAX_HAVES or not all(isinstance(h, str) and _SHA.match(h) for h in haves):
            raise GitExecError("bad_ref", f"haves are at most {MAX_HAVES} 40-hex shas")
        with self._call() as scratch:
            repo = self._init(scratch)
            with self._credential(scratch, host, token) as cred:
                known = [have for have in haves
                         if self._fetch_tip(scratch, repo, url, have, token, cred)]
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
            if deliver is not None:
                return FetchedBundle(sha, None, size, delivered=deliver(out, size))
            return FetchedBundle(sha, out.read_bytes(), size)

    def _fetch_tip(self, scratch: Path, repo: Path, url: str, sha: str, token: str,
                   cred: Path) -> bool:
        """`sha`, fetched from the remote at depth 1: True when it landed."""
        run = self._git(scratch, repo, ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                                        "--depth=1", "--", url, sha], token=token, credential=cred)
        return run.code == 0

    # -- which of these does the remote have (#2413) -------------------------

    def confirm_shas(self, *, url: str, token: str, shas: Sequence[str]) -> list[str]:
        """The `shas` the remote has, as commits, in the order asked.

        Each is fetched at depth 1, exactly as `fetch_bundle` learns its haves,
        and kept only if it then resolves to itself as a commit (a tag's sha
        peels to another). A sha the remote does not have is left out, not an
        error. Nothing is bundled: the answer is the names. The worker's push
        asks this before it refuses `foreign_author` over a commit somebody
        else wrote, so origin's own history is never the task's to answer for,
        and a ref the agent wrote in its clone never has to be believed.
        """
        url, host = self._check_url(url)
        shas = list(dict.fromkeys(shas))
        if len(shas) > MAX_HAVES or not all(isinstance(s, str) and _SHA.match(s) for s in shas):
            raise GitExecError("bad_ref", f"confirm names at most {MAX_HAVES} 40-hex shas")
        with self._call() as scratch:
            repo = self._init(scratch)
            with self._credential(scratch, host, token) as cred:
                landed = [sha for sha in shas
                          if self._fetch_tip(scratch, repo, url, sha, token, cred)]
            return [sha for sha in landed if self._resolve(scratch, repo, sha, token) == sha]

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
        with self._call() as scratch:
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
            # A file swarm-api itself wrote (G5 downloads a large bundle from
            # the tenant's GCS prefix); never a path a request names. git
            # reads it again by name, so the file must not change underneath.
            try:
                fd = os.open(bundle, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                raise GitExecError("bad_bundle", "the bundle file cannot be read") from None
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise GitExecError("bad_bundle", "the bundle is not a regular file")
                if info.st_size > self.max_bundle_bytes:
                    raise GitExecError("too_large", f"the bundle is {info.st_size} bytes, over "
                                                    f"the {self.max_bundle_bytes}-byte cap")
                return handle.read(BUNDLE_HEADER_MAX_BYTES)
        if not isinstance(bundle, (bytes, bytearray)):
            raise GitExecError("bad_bundle", "a bundle is bytes or a file")
        if len(bundle) > self.max_bundle_bytes:
            raise GitExecError("too_large", f"the bundle is {len(bundle)} bytes, over the "
                                            f"{self.max_bundle_bytes}-byte cap")
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
