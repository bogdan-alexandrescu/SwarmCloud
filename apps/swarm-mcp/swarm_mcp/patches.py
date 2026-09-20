"""Getting an agent's code out of GCS and into a working tree.

This is the half of the loop that did not exist. A worker now harvests one
`git apply`-able patch per attempt into the tenant's artifact prefix
(`lifecycle.py::_harvest_git`); this is what reads it back.

TWO DESIGN POINTS WORTH KNOWING BEFORE CHANGING ANYTHING HERE
-------------------------------------------------------------

**`--3way`, always.** A plain `git apply` fails atomically on the first hunk it
cannot place, leaving nothing and saying little. `--3way` falls back to a merge
using the blobs the patch names, which means it either applies, or it leaves
ordinary conflict markers in the file. Conflict markers are the point: they are
what lets the integration be finished by whoever is reading, rather than being
handed back as "patch 3 of 5 did not apply".

**Patches are applied in sequence onto one branch, never merged pairwise.**
Agents never see each other's trees -- per-tenant isolation is not negotiable
and cross-attempt visibility would breach it. So the only place several agents'
work can meet is a working tree that already has all of them. Sequential apply
puts that meeting somewhere a human can watch it happen.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import SwarmClient, SwarmError

_GCS = "https://storage.googleapis.com/storage/v1/b"


def parse_gs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise SwarmError(f"not a gs:// uri: {uri}")
    rest = uri[len("gs://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise SwarmError(f"incomplete gs:// uri: {uri}")
    return bucket, key


def download(client: SwarmClient, uri: str, *, timeout: int = 120) -> bytes:
    """Read one object, with the READER's own credentials.

    The API deliberately mints no signed download URL -- it passes artifacts by
    reference so the tenant boundary stays in one place, enforced by IAM rather
    than by whoever holds a link. The cost is that this needs an access token
    of its own, which is why `SwarmClient` exposes one separately from the ID
    token IAP wants.
    """
    bucket, key = parse_gs_uri(uri)
    url = f"{_GCS}/{urllib.parse.quote(bucket, safe='')}/o/{urllib.parse.quote(key, safe='')}?alt=media"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {client.access_token()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 403:
            body += (
                " -- an artifact lives under the TENANT's prefix, so reading it "
                "needs storage.objects.get on that bucket for your own account, "
                "not for the worker's service account"
            )
        raise SwarmError(f"could not read {uri}: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise SwarmError(f"could not read {uri}: {exc.reason}") from exc


def patch_uri(task: dict[str, Any]) -> str | None:
    """The GCS uri of a task's patch, or None with the reason available above.

    The patch is recorded twice on purpose: `git.patch` holds its NAME and
    `artifacts[]` holds the uri. Matching them here rather than minting a
    second uri means an artifact that failed to upload has no entry, and this
    returns None instead of a link to nothing.
    """
    summary = task.get("result_summary") or {}
    git = summary.get("git") or {}
    name = git.get("patch")
    if not name:
        return None
    for artifact in summary.get("artifacts") or []:
        if artifact.get("name") == name:
            return artifact.get("uri")
    return None


def explain_absence(task: dict[str, Any]) -> str:
    """Why there is no patch. Six causes, six different responses."""
    summary = task.get("result_summary") or {}
    git = summary.get("git")
    if summary == {} or summary is None:
        return (
            f"{task.get('state', 'unknown')}: no result summary was written. "
            "A parked attempt puts its summary in the event detail instead."
        )
    if not git:
        return "this task cloned no repository, so there is no code to apply"
    if git.get("error"):
        return f"the change could not be read from the workspace: {git['error']}"
    if git.get("patch_omitted"):
        return (
            f"the diff was {git.get('patch_bytes')} bytes, over the cap, and was "
            "discarded rather than truncated"
        )
    if not git.get("commits") and not git.get("dirty"):
        return "the agent changed nothing in the repository"
    return git.get("publish_reason") or "no patch was recorded"


@dataclass
class ApplyResult:
    task_id: str
    applied: bool
    conflicted: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def clean(self) -> bool:
        return self.applied and not self.conflicted


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if check and done.returncode != 0:
        raise SwarmError(f"git {args[0]} failed: {done.stderr.strip()[:400]}")
    return done


def apply_patch(patch: bytes, repo: Path, *, task_id: str = "") -> ApplyResult:
    """Apply one patch with a three-way fallback. Returns rather than raises.

    A conflict is an OUTCOME, not an error: the whole design of the integrate
    path is that conflicts come back as markers in files for someone with the
    whole repository in front of them, rather than as an exception that throws
    away the four patches that did apply.
    """
    tmp = repo / ".git" / "swarm-incoming.patch"
    tmp.write_bytes(patch)
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "apply", "--3way", "--whitespace=nowarn", str(tmp)],
            capture_output=True,
            text=True,
        )
    finally:
        tmp.unlink(missing_ok=True)

    stderr = done.stderr.strip()
    if done.returncode == 0:
        return ApplyResult(task_id=task_id, applied=True, detail="applied cleanly")

    # `git apply --3way` exits non-zero when it leaves conflict markers, which
    # is a SUCCESS for our purposes -- the content is in the tree. The files it
    # could not resolve are the ones `diff --name-only --diff-filter=U` lists.
    unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout
    conflicted = [line for line in unmerged.splitlines() if line.strip()]
    if conflicted:
        return ApplyResult(
            task_id=task_id,
            applied=True,
            conflicted=conflicted,
            detail="applied with conflicts; markers are in the files listed",
        )
    return ApplyResult(task_id=task_id, applied=False, detail=stderr[:600] or "apply failed")


@dataclass
class IntegrateResult:
    branch: str
    results: list[ApplyResult] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def conflicts(self) -> list[str]:
        seen: list[str] = []
        for result in self.results:
            for path in result.conflicted:
                if path not in seen:
                    seen.append(path)
        return seen

    def render(self) -> str:
        lines = [f"branch {self.branch}"]
        for result in self.results:
            mark = "ok" if result.clean else ("CONFLICT" if result.conflicted else "FAILED")
            lines.append(f"  apply {result.task_id}  {mark}")
            for path in result.conflicted:
                lines.append(f"      {path}")
            if not result.applied:
                lines.append(f"      {result.detail}")
        for task_id, why in self.skipped:
            lines.append(f"  skip  {task_id}  {why}")
        if self.conflicts:
            lines.append("")
            lines.append(
                f"{len(self.conflicts)} file(s) carry conflict markers. Resolve them "
                "in place; nothing was committed."
            )
        return "\n".join(lines)


def integrate(
    client: SwarmClient,
    task_ids: list[str],
    repo: Path,
    *,
    branch: str,
    base: str | None = None,
) -> IntegrateResult:
    """Put several agents' work on one branch, in the order given.

    ORDER IS THE CALLER'S, and it matters: patch N is applied to a tree that
    already contains patches 1..N-1, so a later task's conflict is reported
    against the accumulated state rather than against the pristine base. That
    is the state a reviewer actually has to resolve, so it is the one reported.

    The tree must be clean before this starts. Applying on top of uncommitted
    local edits would mix the operator's work into the agents' and leave no way
    to tell which conflict came from where.
    """
    status = _git(repo, "status", "--porcelain").stdout.strip()
    if status:
        raise SwarmError(
            "the working tree has uncommitted changes. Integrating on top of them "
            "would mix your edits with the agents' and leave no way to tell which "
            "conflict came from where. Commit or stash first."
        )

    if base:
        _git(repo, "checkout", "-B", branch, base)
    else:
        _git(repo, "checkout", "-B", branch)

    out = IntegrateResult(branch=branch)
    for task_id in task_ids:
        task = client.task(task_id)
        uri = patch_uri(task)
        if uri is None:
            out.skipped.append((task_id, explain_absence(task)))
            continue
        out.results.append(apply_patch(download(client, uri), repo, task_id=task_id))
    return out
