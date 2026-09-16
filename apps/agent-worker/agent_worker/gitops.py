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
* credentials never appear in argv. A token goes into a 0600 credential file
  inside the workspace, because argv is world-readable through /proc.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse, quote

from .procman import run_child

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9._\-/]{1,255}$")
ALLOWED_SCHEMES = ("https", "ssh")


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
    if "\n" in url or "\r" in url:
        raise GitError("repository url must not contain newlines")
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
    return url


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _SAFE_REF.match(ref):
        raise GitError(f"repository ref {ref!r} contains unsupported characters")
    if ref.startswith("-") or ".." in ref:
        raise GitError(f"repository ref {ref!r} is not a valid git ref")
    return ref


def _write_credentials(url: str, token: str, tmp_dir: Path) -> Path:
    """Store `https://x-access-token:<token>@host` for git's `store` helper."""
    parsed = urlparse(url)
    cred_file = tmp_dir / ".git-credentials"
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
    workspace_tmp: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    token: str | None = None,
    git_binary: str = "git",
) -> CloneResult:
    """Clone `url` at `ref` into `destination`, shallow and single-branch."""
    url = validate_repository_url(url)
    ref = validate_ref(ref)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workspace_tmp),
        "GIT_TERMINAL_PROMPT": "0",            # never block waiting for a password
        "GIT_ASKPASS": "/bin/true",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        "LC_ALL": "C",
    }
    config_args: list[str] = ["-c", "protocol.version=2", "-c", "advice.detachedHead=false"]
    if token:
        cred_file = _write_credentials(url, token, workspace_tmp)
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
    for index, argv in enumerate(steps):
        result = run_child(
            argv,
            cwd=workspace_tmp,
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
            raise GitError(f"git step {index} failed with exit {result.exit_code}: {tail.strip()}")

    commit = _read_head(destination, workspace_tmp, logs_dir, logger, git_binary)
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
