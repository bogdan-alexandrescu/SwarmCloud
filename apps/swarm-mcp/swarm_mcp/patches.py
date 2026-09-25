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

from swarm_common.states import CONCURRENCY_STATES, PENDING_STATES

from .client import SwarmClient, SwarmError, task_id_of
from .profiles import backend_of

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
    # ONE RETRY, on a 401 with a token the client had cached: the access token
    # is cached now (`SwarmClient.access_token`), and gcloud can hand back one
    # with less life left than the cache assumes. A fake client without the
    # method -- the tests' -- gets no retry, which is the old behaviour.
    forget = getattr(client, "forget_credentials", None)
    for attempt in (1, 2):
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {client.access_token()}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1 and callable(forget) and forget():
                continue
            body = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 403:
                body += (
                    " -- an artifact lives under the TENANT's prefix, so reading it "
                    "needs storage.objects.get on that bucket for your own account, "
                    "not for the worker's service account"
                )
            # THE STATUS AS DATA, so a caller can tell "not written yet" (404)
            # from "you may not read it" (403) without searching this sentence.
            # `swarm tail` read every failure here as "not published yet", which
            # hid a missing grant behind an agent that seemed to print nothing
            # (#88, SC-F12).
            raise SwarmError(f"could not read {uri}: {exc.code} {body}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise SwarmError(f"could not read {uri}: {exc.reason}") from exc
    raise SwarmError(f"could not read {uri}: refused twice")  # pragma: no cover - loop always returns or raises


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


#: States in which a task is still on its way and a summary is simply not due
#: yet. From the frozen contract, not written out; PARKED has its own sentence.
_UNFINISHED = frozenset(
    state.value for state in CONCURRENCY_STATES | PENDING_STATES
) - {"PARKED"}


def _no_summary(task: dict[str, Any]) -> str:
    """Why a task has no result summary, from its own state and history.

    THE PARKED SENTENCE IS FOR PARKED TASKS. It was printed for every task
    without a summary -- including, on 2026-09-25 (#88, SC-F10), a workflow
    step the scheduler cascade-cancelled before it was ever attempted, which
    has no parked attempt and no event detail to go looking in.
    """
    state = str(task.get("state") or "unknown")
    if state == "PARKED":
        return (
            f"{state}: no result summary was written. "
            "A parked attempt puts its summary in the event detail instead."
        )
    if state in _UNFINISHED:
        return f"{state}: no result summary yet -- one is written when the task finishes"
    if not task.get("started_at") and not task.get("attempt_count"):
        return (
            f"{state}: no result summary was written -- the task never started, "
            "so no agent produced anything"
        )
    return f"{state}: no result summary was written"


def explain_absence(task: dict[str, Any]) -> str:
    """Why there is no patch. Six causes, six different responses."""
    summary = task.get("result_summary") or {}
    git = summary.get("git")
    if summary == {} or summary is None:
        return _no_summary(task)
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


def describe_task(task: dict[str, Any]) -> dict[str, Any]:
    """What one task produced, in the shape every read tool hands back.

    IT LIVES HERE, not in `server.py`, because it is the composition of the two
    functions above and because it now has four callers: `swarm_result`,
    `swarm_wait`, `swarm_follow`'s per-task outcome, and the per-step rollup a
    workflow read produces. It was private to the MCP server while there was one
    consumer; a second copy for the workflow tools is exactly how a task's
    result and a workflow step's result would start disagreeing about what "no
    patch" means. (There WAS a second copy -- `server._describe`, byte-for-byte
    identical, left behind by the move and still being called by `swarm_follow`
    until 2026-09-24.)

    THE PROFILE AND THE BACKEND ARE HERE FOR THE FAILURE CASE. A result that
    says only `state: FAILED` and an error string sends the reader to the logs
    to find out what kind of thing failed. Which profile ran and which backend
    it landed on are the two facts that separate "this agent's prompt was wrong"
    from "GKE Autopilot could not place a browser pod" -- and they are the two
    the reader cannot derive, because the profile-to-backend mapping lives in
    the frozen catalogue rather than on the task. Both are cheap: the task
    document already carries the profile name, and the backend follows from it.

    `backend` is None when the catalogue does not hold the task's profile -- an
    old task naming a profile since renamed. NOT a default and not a guess: the
    three-marks rule this plugin is held to says an unknown is reported as
    unknown, and a reader who saw CLOUD_RUN_JOB there would go and read the
    wrong service's logs.
    """
    summary = task.get("result_summary") or {}
    git = summary.get("git") or {}
    out: dict[str, Any] = {
        # The API names it `id`. Read as `task_id` this was null for every task
        # on the platform, and a session reads a null id and a null state as
        # "it has not started yet".
        "task_id": task_id_of(task),
        "state": task.get("state"),
        "runner_profile": task.get("runner_profile"),
        "backend": backend_of(task.get("runner_profile")),
        "commits": git.get("commit_count", 0),
        "insertions": git.get("insertions", 0),
        "deletions": git.get("deletions", 0),
        "uncommitted_files": git.get("dirty_count", 0),
        "patch": patch_uri(task),
    }
    if not out["patch"]:
        out["no_patch_because"] = explain_absence(task)
    pr = git.get("pull_request")
    out["pull_request"] = pr["url"] if pr else None
    if not pr and git:
        out["no_pull_request_because"] = git.get("publish_reason")
    if task.get("last_error"):
        out["error"] = task["last_error"]
    return out


#: The states that mean "this went wrong", as the frozen vocabulary spells
#: them. `DEAD_LETTER` is not a member and is matched anyway, because
#: `client.TERMINAL` already carries both spellings defensively and a failure
#: explainer that silently declined to explain an unrecognised spelling would
#: be the worst possible place to be strict.
FAILED_STATES = frozenset({"FAILED", "DEAD_LETTERED", "DEAD_LETTER"})


def explain_failure(client: SwarmClient, task: dict[str, Any]) -> dict[str, Any] | None:
    """The per-attempt facts that make a dead agent actionable. None if it lived.

    "task failed" is not a report. What a reader needs is which attempt, on
    which backend, with what exit code, and what the worker said -- and none of
    those are on the task document. `result_summary` is written once at terminal
    state, so a task that failed twice and succeeded on the third try carries
    only the third attempt's numbers; the per-attempt record is the only place
    the others exist.

    ONE EXTRA ROUND TRIP, AND ONLY ON A FAILURE. Every successful result read
    would otherwise pay for a list it has nothing to say about. The condition is
    the task's state, so the cost lands exactly where the information is wanted.

    A FAILED READ IS REPORTED, NOT SWALLOWED. If the attempts route cannot be
    read, this says so in `attempts_unreadable` rather than returning None --
    None means "this task did not fail", and collapsing "it failed and I could
    not find out why" into that would be the exact substitution this repository
    keeps deleting.

    `exit_code: null` MEANS NOT RECORDED. It must never be rendered as 0, which
    is the one value that would read as a clean exit on a task that failed. The
    same rule as the em dash in `sc`, in the place where getting it wrong is
    most expensive.
    """
    if str(task.get("state") or "") not in FAILED_STATES:
        return None

    task_id = task_id_of(task)
    out: dict[str, Any] = {
        "state": task.get("state"),
        # From the task, so there is always something here even when the
        # attempts route cannot be read.
        "last_error": task.get("last_error"),
        "attempt_count": task.get("attempt_count"),
    }
    if not task_id:
        # NOT the same as "no attempts". Without an id the route cannot be
        # asked at all, so the attempts are UNREADABLE -- reporting that as an
        # empty list would say this task failed before any agent ran, which is
        # a claim nobody checked.
        out["attempts_unreadable"] = (
            "this task document carries no id, so its attempts cannot be read. "
            "The exit code and the backend that ran are UNKNOWN"
        )
        return out
    try:
        attempts = client.attempts(task_id)
    except SwarmError as exc:
        out["attempts_unreadable"] = (
            f"the per-attempt record could not be read: {exc}. The exit code and "
            "the backend that ran are UNKNOWN, not absent"
        )
        return out

    if not attempts:
        # A real measurement, and a meaningful one: a task can reach FAILED
        # without ever being attempted -- admission or dispatch failed -- and
        # that is a different investigation from an agent that ran and died.
        out["attempts"] = []
        out["note"] = (
            "this task has no attempt records, so it failed before any agent "
            "ran -- look at admission and dispatch, not at the agent"
        )
        return out

    # Newest first, per the route's contract, so the last attempt is the one
    # that decided the outcome.
    last = attempts[0]
    out["last_attempt"] = {
        "attempt_id": last.get("attempt_id"),
        "generation": last.get("generation"),
        # The backend that ACTUALLY ran, which is the ground truth. The
        # catalogue-derived `backend` on the result beside this is what the
        # profile says it should have been; the two differing is itself a
        # finding.
        "backend": last.get("backend"),
        "execution_name": last.get("execution_name"),
        "exit_code": last.get("exit_code"),
        "error": last.get("error"),
        # OOM is a common way an agent dies and is invisible in an exit code
        # alone. `oom_near_miss` says the run came close even when it survived,
        # which is the difference between "make the prompt smaller" and
        # "use a bigger resource class".
        "oom_near_miss": last.get("oom_near_miss"),
        "peak_rss_bytes": last.get("peak_rss_bytes"),
    }
    if last.get("exit_code") is None:
        out["last_attempt"]["exit_code_note"] = (
            "not recorded -- this is UNKNOWN, not 0. A missing exit code and a "
            "clean exit are different facts and only one of them means the "
            "agent finished"
        )
    if len(attempts) > 1:
        # Earlier attempts are the whole reason this route exists: their
        # numbers are the ones `result_summary` overwrote.
        out["earlier_attempts"] = [
            {
                "attempt_id": a.get("attempt_id"),
                "generation": a.get("generation"),
                "exit_code": a.get("exit_code"),
                "error": a.get("error"),
            }
            for a in attempts[1:]
        ]
    return out


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
