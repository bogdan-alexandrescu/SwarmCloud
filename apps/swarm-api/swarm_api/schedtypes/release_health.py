"""`release-health`: one issue per failing streak of a named workflow (docs/schedules.md §3.9, lane S10e).

AN API TYPE (SD8, §2.7). Nothing here runs an agent, submits a task or holds
capacity: each repository in scope is one `api_action` on the firing, ended
the moment it is recorded, and the firing record is the type's history.

WHAT A FIRING DOES, per repository in scope (`firing.repo_ids`), as the
schedule's tenant, with that tenant's forge credential
(`ctx.forge_tokens.token_for`, read once per firing and dropped at its end),
through the writer every issue run writes with (`ctx.forge_writer`, its
pinned host and its token-free errors):

  1. READ the latest completed runs of `workflow` (default `release.yml`) on
     `branch` (default: the registration's default branch, else GitHub's),
     newest first. `success` is green; `failure`, `timed_out` and
     `startup_failure` are red; any other conclusion (cancelled, skipped,
     neutral) says nothing about health and is passed over.
  2. RED (the newest classified run failed). The open issue of this
     workflow and branch is found by the marker its body opens with
     (`streak_marker`), among the open issues carrying `issue_label`.
       * none open: OPEN one -- the title states the failure as a fact, the
         body names the failing job and step and quotes the tail of that
         job's log, passed through `redact` with the token as a known
         literal; label `issue_label`.
       * one open: UPDATE it with one comment naming the newest failing run,
         unless the body or a comment already names that run (`run_marker`):
         a firing every 15 minutes reports a run once, not once per firing.
  3. GREEN after red: the newest classified runs are green and a red run
     precedes them. With the streak's issue open, it comments "green again
     at {sha}" naming the FIRST green run, once (its own marker). It NEVER
     closes the issue: a person does (§3.9).
  4. Anything else -- green with no red before it in the window, or no
     completed run -- writes nothing.

GitHub is the record: markers in the issue body and comments are what make a
retried firing, or the next firing, find what an earlier one wrote. A firing
that stopped after a write and before recording its `api_action` (§2.2) is
re-run, finds its own marker, and writes nothing twice.

THE PERMISSION S0 FOUND MISSING (§0.1 check 1). Reading workflow runs needs
`actions: read`, which the onboarding GitHub App does NOT hold. A 403 on that
read is not swallowed: the repository's action ends `failed` with code
`actions_read_missing`, GitHub's refusal sentence (which names the
permission) and `ACTIONS_READ_HINT`, which says who can add it. A 404 is
`workflow_not_found` and says it may be the same cause, since GitHub hides a
resource the token cannot see. A failed firing counts towards the schedule's
consecutive-failure pause (§4.4), so a schedule that can never read stops
itself with that reason in its history rather than succeeding empty.

A repository's GitHub failure is that repository's (`failed`, with the code):
retried every tick, an unreadable workflow would hold the firing claimed for
ever. No new refusal is raised here.

DRY RUN (§2.8): the same reads, and what each repository WOULD do. Nothing
is written to GitHub or Firestore, and no job log is fetched.

INVARIANTS. No task, lease or capacity (1-3). Every registration is read
under the schedule's tenant, and the token is that tenant's own (9). The
caller picks parameters the catalogue validates -- a workflow FILE NAME, a
branch, a label -- never a URL, a command or a credential (10).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from ..errors import ApiError
from ..forge import GITHUB_API_HOST
from ..forgewrite import ISSUES_WRITE, ForgeWriteError, ForgeWriteForbidden, ForgeWriteNotFound, GitHubWriter
from ..issuecomments import bounded, code, plain
from ..redaction import redact
from ..repositories import Repositories
from ..scheduletypes import BudgetCaps
from ..validation import IssueRef
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "release-health"

#: The caps (SD4: "the other types' figures are set in their lanes"). This
#: type runs no model and reports no cost, so the dollar caps only have to be
#: above zero (`schedules.resolve_budget` refuses zero); they bound nothing a
#: firing spends. `max_concurrent` is the repositories one firing reads: each
#: is one `api_action`, ended as it is recorded, so it never stays live.
BUDGET_CAPS = BudgetCaps(per_run_usd=0.01, per_day_usd=1.0, max_concurrent=10)

#: What reading a workflow's runs and jobs needs, named in every refusal.
ACTIONS_READ = "actions: read"
ACTIONS_READ_HINT = (
    "reading workflow runs needs `actions: read`. The onboarding GitHub App holds no "
    "Actions permission (docs/schedules.md §0.1, check 1), so a tenant connected through "
    "onboarding cannot use this type until the owner adds Actions: Read to the App and "
    "the installation accepts it; a stored tenant token needs Actions: Read on the repository"
)
METADATA_READ = "metadata: read"

#: Codes a repository's action ends `failed` with (never an `ApiError`: these
#: are GitHub's answers, recorded on the firing, not refusals of a request).
ACTIONS_READ_MISSING = "actions_read_missing"
WORKFLOW_NOT_FOUND = "workflow_not_found"
REPOSITORY_NOT_REGISTERED = "repository_not_registered"

RED = frozenset({"failure", "timed_out", "startup_failure"})
GREEN = frozenset({"success"})

#: Completed runs read per firing. At a 15-minute minimum interval a branch
#: completes far fewer runs than this between two firings, so the green run
#: that ended a streak is still in the window when the next firing looks.
RUNS_READ = 30
#: Open issues with `issue_label` read to find the streak's issue: 300.
ISSUE_PAGES = 3
PAGE_SIZE = 100
#: The log tail fetched, and the excerpt quoted from it.
LOG_TAIL_BYTES = 8 * 1024
EXCERPT_CHARS = 6_000
TITLE_CHARS = 200
NAME_CHARS = 100

_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_HTML_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+(?:/attempts/[0-9]+)?$")

OPEN_ISSUE = "open_issue"
UPDATE_ISSUE = "update_issue"
GREEN_AGAIN = "green_again"
NOTHING = "nothing"


# --------------------------------------------------------------------------
# Markers: how the next firing finds what this one wrote
# --------------------------------------------------------------------------


def streak_marker(workflow: str, branch: str) -> str:
    """Opens the streak issue's body. The branch is quoted, so no `-->` survives into it."""
    return f"<!-- swarm:release-health workflow={quote(workflow, safe='')} branch={quote(branch, safe='/')} -->"


def run_marker(run_id: int) -> str:
    return f"<!-- swarm:release-health failed run={int(run_id)} -->"


def green_marker(run_id: int) -> str:
    return f"<!-- swarm:release-health green run={int(run_id)} -->"


# --------------------------------------------------------------------------
# What was read
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    id: int
    number: int | None
    sha: str
    conclusion: str
    url: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"run_id": self.id, "run_number": self.number, "sha": self.sha,
                "conclusion": self.conclusion, "url": self.url}


@dataclass(frozen=True)
class Failure:
    """The failing job and step of a red run; `job_id` is what the log is read by."""

    job: str | None
    step: str | None
    job_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"job": self.job, "step": self.step}


@dataclass
class Reading:
    """One repository's state and what the firing does about it."""

    repo_id: str
    ref: IssueRef | None
    workflow: str
    branch: str | None
    action: str = NOTHING
    reason: str = ""
    latest: Run | None = None
    #: Red: the newest failing run. Green after red: the first green run.
    subject: Run | None = None
    failure: Failure | None = None
    issue: int | None = None
    code: str | None = None
    message: str | None = None
    hint: str | None = None
    written: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.code is not None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "repo_id": self.repo_id,
            "repository": self.ref.repository if self.ref else None,
            "workflow": self.workflow,
            "branch": self.branch,
            "action": self.action,
            "reason": self.reason,
            "latest_run": self.latest.as_dict() if self.latest else None,
            "run": self.subject.as_dict() if self.subject else None,
            "failure": self.failure.as_dict() if self.failure else None,
            "issue": self.issue,
        }
        if self.failed:
            out.update({"code": self.code, "message": self.message})
            if self.hint:
                out["hint"] = self.hint
        if self.written:
            out["written"] = dict(self.written)
        return out


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _run(entry: Mapping[str, Any]) -> Run | None:
    run_id = _int(entry.get("id"))
    sha = entry.get("head_sha")
    if run_id is None or not isinstance(sha, str) or not _SHA.match(sha):
        return None
    url = entry.get("html_url")
    return Run(
        id=run_id, number=_int(entry.get("run_number")), sha=sha,
        conclusion=str(entry.get("conclusion") or ""),
        # Only GitHub's own run page is linked from the issue.
        url=url if isinstance(url, str) and _HTML_URL.match(url) else None,
    )


# --------------------------------------------------------------------------
# Reads, through the writer's request path
# --------------------------------------------------------------------------


def _repo_ref(registration: Mapping[str, Any]) -> IssueRef:
    # `number` is unused by the repository-level routes `_url` builds.
    return IssueRef(owner=str(registration["owner"]), repo=str(registration["repo"]), number=0)


def _branch(writer: GitHubWriter, ref: IssueRef, registration: Mapping[str, Any], params: Any,
            token: str) -> str:
    if params.branch:
        return str(params.branch)
    stored = registration.get("default_branch")
    if isinstance(stored, str) and stored:
        return stored
    what = f"the repository {ref.repository}"
    data = writer._call(
        "GET", f"https://{GITHUB_API_HOST}/repos/{quote(ref.owner, safe='')}/{quote(ref.repo, safe='')}",
        token, what, needs=METADATA_READ,
    )
    branch = data.get("default_branch") if isinstance(data, Mapping) else None
    if not isinstance(branch, str) or not branch:
        raise ForgeWriteError(f"GitHub's answer for {what} names no default branch")
    return branch


def completed_runs(writer: GitHubWriter, ref: IssueRef, workflow: str, branch: str,
                   token: str) -> list[Run]:
    """The newest `RUNS_READ` completed runs of `workflow` on `branch`, newest first."""
    what = f"the runs of {workflow} on {ref.repository}"
    data = writer._call(
        "GET",
        writer._url(ref, f"actions/workflows/{quote(workflow, safe='')}/runs"
                         f"?branch={quote(branch, safe='')}&status=completed"
                         f"&exclude_pull_requests=true&per_page={RUNS_READ}"),
        token, what, needs=ACTIONS_READ,
    )
    entries = data.get("workflow_runs") if isinstance(data, Mapping) else None
    if not isinstance(entries, list):
        raise ForgeWriteError(f"GitHub's answer for {what} is not a list of runs")
    runs = []
    for entry in entries:
        # The filter again: only this branch's runs say anything about it.
        if isinstance(entry, Mapping) and entry.get("head_branch") == branch:
            run = _run(entry)
            if run is not None:
                runs.append((str(entry.get("created_at") or ""), run))
    # GitHub answers newest first; sorted again so a reordered page cannot
    # make an old run read as the latest.
    runs.sort(key=lambda pair: (pair[0], pair[1].id), reverse=True)
    return [run for _created, run in runs]


def failing_job(writer: GitHubWriter, ref: IssueRef, run: Run, token: str) -> Failure:
    """The first failed job of `run`, and its first failed step."""
    what = f"the jobs of run {run.id} on {ref.repository}"
    data = writer._call(
        "GET", writer._url(ref, f"actions/runs/{run.id}/jobs?filter=latest&per_page={PAGE_SIZE}"),
        token, what, needs=ACTIONS_READ,
    )
    jobs = data.get("jobs") if isinstance(data, Mapping) else None
    if not isinstance(jobs, list):
        raise ForgeWriteError(f"GitHub's answer for {what} is not a list of jobs")
    for job in jobs:
        if not isinstance(job, Mapping) or job.get("conclusion") not in RED:
            continue
        step_name = None
        for step in job.get("steps") or []:
            if isinstance(step, Mapping) and step.get("conclusion") in RED:
                step_name = step.get("name")
                break
        return Failure(
            job=str(job.get("name")) if job.get("name") else None,
            step=str(step_name) if step_name else None,
            job_id=_int(job.get("id")),
        )
    return Failure(job=None, step=None, job_id=None)


def streak_issue(writer: GitHubWriter, ref: IssueRef, label: str, marker: str,
                 token: str) -> tuple[int, str] | None:
    """The OLDEST open issue labelled `label` whose body opens with `marker`: `(number, body)`."""
    what = f"the open issues of {ref.repository}"
    found: tuple[int, str] | None = None
    for page in range(1, ISSUE_PAGES + 1):
        entries = writer._call(
            "GET",
            writer._url(ref, f"issues?state=open&labels={quote(label, safe='')}"
                             f"&sort=created&direction=asc&per_page={PAGE_SIZE}&page={page}"),
            token, what, needs=ISSUES_WRITE,
        )
        if not isinstance(entries, list):
            raise ForgeWriteError(f"GitHub's answer for {what} is not a list")
        for entry in entries:
            if not isinstance(entry, Mapping) or "pull_request" in entry:
                continue
            body, number = entry.get("body"), _int(entry.get("number"))
            if number is not None and isinstance(body, str) and body.startswith(marker):
                if found is None or number < found[0]:
                    found = (number, body)
        if found is not None or len(entries) < PAGE_SIZE:
            return found
    return found


def _said(writer: GitHubWriter, ref: IssueRef, number: int, body: str, marker: str, token: str) -> bool:
    """Whether the issue's body or one of its comments already carries `marker`."""
    if marker in body:
        return True
    return writer.find_comment(_issue(ref, number), marker, token) is not None


def _issue(ref: IssueRef, number: int) -> IssueRef:
    return IssueRef(owner=ref.owner, repo=ref.repo, number=int(number))


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


def read(writer: GitHubWriter, reading: Reading, params: Any, token: str,
         registration: Mapping[str, Any]) -> Reading:
    """Fill `reading` from GitHub: its runs, its open issue, and the action due. Reads only."""
    ref = reading.ref
    assert ref is not None
    reading.branch = _branch(writer, ref, registration, params, token)
    try:
        runs = completed_runs(writer, ref, reading.workflow, reading.branch, token)
    except ForgeWriteNotFound:
        # GitHub hides what a token cannot see behind the same 404 as what
        # does not exist, so the hint names both.
        reading.code = WORKFLOW_NOT_FOUND
        reading.hint = (f"no workflow {reading.workflow} on this repository, or the credential "
                        f"cannot see it; {ACTIONS_READ_HINT}")
        raise
    classified = [run for run in runs if run.conclusion in RED or run.conclusion in GREEN]
    if not classified:
        reading.reason = "no completed run that passed or failed"
        return reading
    reading.latest = latest = classified[0]
    marker = streak_marker(reading.workflow, reading.branch)
    if latest.conclusion in RED:
        reading.subject = latest
        reading.failure = failing_job(writer, ref, latest, token)
        found = streak_issue(writer, ref, params.issue_label, marker, token)
        if found is None:
            reading.action, reading.reason = OPEN_ISSUE, "red, and no open issue for this streak"
            return reading
        reading.issue = found[0]
        if _said(writer, ref, found[0], found[1], run_marker(latest.id), token):
            reading.reason = "red, and the open issue already names this run"
        else:
            reading.action, reading.reason = UPDATE_ISSUE, "red again since the issue was last updated"
        return reading
    # Green: the oldest green run before the newest red is the one that ended the streak.
    first_green = latest
    for run in classified[1:]:
        if run.conclusion in RED:
            break
        first_green = run
    else:
        reading.reason = "green, with no failed run in the window"
        return reading
    reading.subject = first_green
    found = streak_issue(writer, ref, params.issue_label, marker, token)
    if found is None:
        reading.reason = "green after a failure, and no open issue to tell"
        return reading
    reading.issue = found[0]
    if _said(writer, ref, found[0], found[1], green_marker(first_green.id), token):
        reading.reason = "green, and the open issue already says so"
    else:
        reading.action, reading.reason = GREEN_AGAIN, "the first success after a failure"
    return reading


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _name(text: str | None, token: str) -> str:
    """A job, step or branch name for the title, which GitHub shows as text (#2391)."""
    return plain(text or "unknown", NAME_CHARS, literals=(token,), one_line=True)


def _code_name(text: str | None, token: str) -> str:
    """The same name as a code span in a body: shown as typed, never as HTML."""
    return code(text or "unknown", NAME_CHARS, literals=(token,))


def title(workflow: str, branch: str, run: Run, failure: Failure, token: str) -> str:
    where = f"job {_name(failure.job, token)}"
    if failure.step:
        where += f", step {_name(failure.step, token)}"
    text = f"{workflow} on {_name(branch, token)} failed at {run.sha[:12]} in {where}"
    return text[:TITLE_CHARS]


def excerpt(log_tail: str | None, token: str) -> str:
    """The log's tail, redacted with the token as a known literal, in a fence it cannot close."""
    if not log_tail:
        return "_The job's log could not be read: the run page above has it._"
    masked = redact(log_tail, extra=(token,)).text.replace("\x00", "")
    if len(masked) > EXCERPT_CHARS:
        masked = masked[-EXCERPT_CHARS:]
        masked = masked.split("\n", 1)[1] if "\n" in masked else masked
    longest = max((len(m) for m in re.findall(r"~+", masked)), default=0)
    fence = "~" * max(4, longest + 1)
    return f"Tail of the job's log, redacted:\n\n{fence}text\n{masked.rstrip()}\n{fence}"


def _run_line(run: Run) -> str:
    label = f"run #{run.number}" if run.number is not None else f"run {run.id}"
    return f"[{label}]({run.url})" if run.url else label


def failure_text(reading: Reading, log_tail: str | None, token: str) -> str:
    run, failure = reading.subject, reading.failure or Failure(None, None, None)
    assert run is not None and reading.branch is not None
    lines = [
        f"**{reading.workflow}** failed on {_code_name(reading.branch, token)} at `{run.sha}` "
        f"({_run_line(run)}, `{run.conclusion}`).",
        "",
        f"* Job: {_code_name(failure.job, token)}",
        f"* Step: {_code_name(failure.step, token)}",
        "",
        excerpt(log_tail, token),
    ]
    return "\n".join(lines)


def issue_body(reading: Reading, log_tail: str | None, schedule_id: str, token: str) -> str:
    assert reading.subject is not None and reading.branch is not None
    head = [
        streak_marker(reading.workflow, reading.branch),
        run_marker(reading.subject.id),
        "",
        failure_text(reading, log_tail, token),
        "",
        f"Opened by SwarmCloud schedule `{schedule_id}` ({TYPE}). It comments here when the "
        "workflow fails again and when it is green again. It never closes this issue: a person does.",
    ]
    return bounded("\n".join(head))


def _log_tail(writer: GitHubWriter, reading: Reading, token: str) -> str | None:
    failure = reading.failure
    if failure is None or failure.job_id is None or reading.ref is None:
        return None
    return writer.job_log_tail(reading.ref, failure.job_id, token, limit=LOG_TAIL_BYTES)


def write(writer: GitHubWriter, reading: Reading, params: Any, schedule_id: str, token: str) -> None:
    """Do `reading.action` on GitHub. Each write is found again by its marker."""
    ref, run = reading.ref, reading.subject
    if reading.action == NOTHING or ref is None or run is None or reading.branch is None:
        return
    if reading.action == OPEN_ISSUE:
        what = f"a new issue on {ref.repository}"
        data = writer._call(
            "POST", writer._url(ref, "issues"), token, what, needs=ISSUES_WRITE, ok=(200, 201),
            payload={
                "title": title(reading.workflow, reading.branch, run,
                               reading.failure or Failure(None, None, None), token),
                "body": issue_body(reading, _log_tail(writer, reading, token), schedule_id, token),
                "labels": [params.issue_label],
            },
        )
        number = _int(data.get("number")) if isinstance(data, Mapping) else None
        if number is None:
            raise ForgeWriteError(f"GitHub's answer for {what} names no issue number")
        reading.issue = number
        reading.written = {"issue_opened": number}
        return
    assert reading.issue is not None
    if reading.action == UPDATE_ISSUE:
        body = bounded("\n".join([
            run_marker(run.id), "", "Failed again.", "", failure_text(reading, _log_tail(writer, reading, token), token),
        ]))
    else:
        body = "\n".join([
            green_marker(run.id), "",
            f"Green again at `{run.sha}` ({_run_line(run)}). This issue stays open until a person closes it.",
        ])
    comment = writer.create_comment(_issue(ref, reading.issue), body, token)
    reading.written = {"comment_id": comment.id}


# --------------------------------------------------------------------------
# One firing
# --------------------------------------------------------------------------


def _failed(reading: Reading, exc: Exception) -> Reading:
    if reading.code is not None:
        pass  # `read` named it where it knew more (WORKFLOW_NOT_FOUND)
    elif isinstance(exc, ForgeWriteForbidden) and ACTIONS_READ in exc.message:
        reading.code, reading.hint = ACTIONS_READ_MISSING, ACTIONS_READ_HINT
    else:
        reading.code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
    # ApiError messages are constant, token-free sentences (errors.py).
    reading.message = exc.message if isinstance(exc, ApiError) else None
    reading.action = NOTHING
    return reading


def _readings(firing: Any, repo_ids: list[str], params: Any, *, writes: bool) -> Iterator[Reading]:
    """Each repository's reading, one at a time, so the caller records it before the next."""
    ctx = firing.ctx
    tenant_id = str(firing.schedule["tenant_id"])
    schedule_id = str(firing.schedule["schedule_id"])
    writer: GitHubWriter = ctx.forge_writer
    registrations = Repositories(ctx.db, now=ctx.now)
    token = ""
    token_error: Exception | None = None
    try:
        for repo_id in repo_ids:
            reading = Reading(repo_id=repo_id, ref=None, workflow=params.workflow, branch=params.branch)
            registration = registrations.find(tenant_id, repo_id)
            if registration is None:
                reading.code = REPOSITORY_NOT_REGISTERED
                reading.message = f"repository {repo_id!r} is not registered in this tenant"
                yield reading
                continue
            reading.ref = _repo_ref(registration)
            if not token and token_error is None:
                try:
                    token = ctx.forge_tokens.token_for(ctx.store.get_tenant(tenant_id))
                except ApiError as exc:
                    token_error = exc
            if token_error is not None:
                yield _failed(reading, token_error)
                continue
            try:
                read(writer, reading, params, token, registration)
                if writes:
                    write(writer, reading, params, schedule_id, token)
            except ApiError as exc:
                # GitHub's answer for this repository (writer errors are
                # ApiErrors, 502 included): the repository's failure, not
                # the firing's, so one unreadable workflow cannot hold it.
                _failed(reading, exc)
            yield reading
    finally:
        token = ""


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    schedule_id = str(firing.schedule["schedule_id"])
    # One action per repository: past the room, a repository waits for the next firing.
    room = max(0, int(firing.room))
    held = firing.repo_ids[room:]
    if held:
        log.info("schedule %s firing %s: %d repositories past the room wait for the next firing",
                 schedule_id, firing.firing["firing_id"], len(held))
    work: list[dict[str, Any]] = []
    for reading in _readings(firing, firing.repo_ids[:room], params, writes=True):
        # Recorded as soon as it is done (§2.2): a firing that stops at the
        # next repository is finished by adopting this one, not redoing it.
        work.append(firing.api_action(
            f"{TYPE}:{reading.repo_id}",
            outcome="failed" if reading.failed else "succeeded",
            detail=reading.as_dict(), repo_id=reading.repo_id,
        ))
        log.info("schedule %s firing %s: %s %s (%s)", schedule_id, firing.firing["firing_id"],
                 reading.repo_id, reading.code or reading.action, reading.reason)
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    room = max(0, int(firing.room))
    rows = [reading.as_dict() for reading in _readings(firing, firing.repo_ids[:room], params, writes=False)]
    return {
        "workflow": params.workflow,
        "branch": params.branch,
        "issue_label": params.issue_label,
        "room": room,
        "held_for_room": list(firing.repo_ids[room:]),
        "repositories": rows,
    }
