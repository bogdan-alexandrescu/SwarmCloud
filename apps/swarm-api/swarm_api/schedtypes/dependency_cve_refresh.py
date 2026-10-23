"""`dependency-cve-refresh`: bump fixable vulnerable pins from the latest image scan (docs/schedules.md §3.8, lane S10d).

WHAT A FIRING DOES. As the schedule's owner, over the repositories in scope
now, in scope order, until ONE workflow has been submitted (§3.8: "one pull
request per firing at most"):

  1. FIND THE LATEST SCAN: the check run `trivy (published images)`, the
     `images` job of `security.yml`, which runs on its weekly schedule or a
     dispatch and is SKIPPED on every push and pull request. With `actions:
     read` the newest completed `security.yml` run on the default branch
     whose event is `schedule` or `workflow_dispatch` names the commit, and
     the check run is read there. Without it (lane S0: the onboarding App
     holds `checks: read` and NO `actions` permission) the default branch's
     last `MAX_COMMITS` commits are walked for the newest check run of that
     name that completed and was not skipped. Every read is the tenant's
     token through `forgewrite.GitHubWriter`, so the pinned host, the
     no-redirect rule and the constant error sentences are that module's.
  2. READ ITS PACKAGES. `success` is a clean scan: the job runs trivy with
     `--ignore-unfixed --exit-code 1`, so it passes only with nothing
     fixable. On `failure` the scan's own table rows are read from, in
     order, the check run's output, its annotations, and the tail of the
     job's log (`job_log_tail`, which needs `actions: read` and answers None
     without it). TODAY THE JOB WRITES ITS TABLE ONLY TO THE LOG: its check
     run has no output and no annotations. So without `actions: read` a
     failed scan is `unlisted`, and the outcome says that the log needs
     `actions: read` rather than reading as "nothing to fix".
  3. FILTER: fixable rows only (a valid fixed version), at or above
     `severity_at_least`, one entry per package, most severe first, at most
     `max_packages`. Names, versions and vulnerability ids must each match a
     strict pattern; a row that does not is dropped, never quoted. Titles
     are never read. So nothing an agent is told came from free text.
  4. SKIP while a pull request this schedule opened is still open (its body
     carries `marker(schedule_id)`): a second one would bump the same pins.
  5. SUBMIT ONE WORKFLOW through `firing.submit_workflow`: the review shape
     of an issue run (`issueruns.compile_plan`), with one implementer:
     `bump` -> `review` -> `fix` (gated on NOT_YET, the integrator), under
     `integrate`, which opens the one pull request.

THE MERGE GATE. The type's default and floor are `approve merge` (§3 table),
and swarm-api's merge approval in the inbox exists only for issue runs
(`approvals.request_merge`). So the workflow is submitted with `merge:
"off"` whatever the gate says: the pull request is opened and a person
merges it on GitHub. `off` is stricter than `approve` (`GATE_POINTS`), so
the floor holds, and no path here merges anything.

WHAT IS REPORTED. Each repository's outcome is logged and is the dry run's
answer:

    submitted       a workflow was submitted for its fixable packages
    room            fixable packages, and no room (`max_concurrent`)
    deferred        a pull request was already submitted by this firing
    pr_open         this schedule's earlier pull request is still open
    clean           the latest scan passed
    below_threshold fixable packages, none at `severity_at_least`
    unlisted        the scan failed and no package list could be read
    no_scan         no completed, unskipped scan was found
    failed: <code>  a GitHub read was refused or failed

A firing that submitted nothing, where every repository is `unlisted`,
`no_scan` or `failed`, is REFUSED with `cve_scan_unreadable`, naming each
repository's outcome and whether `actions: read` was held. A new refusal
ships report-only (`refusals.py`): off, the firing ends with nothing
submitted and the log names the refusal it would have made.

DRY RUN (§2.8): the same reads, and the workflow it would submit described;
nothing is submitted and nothing is written to GitHub.

INVARIANTS. One workflow of ordinary QUEUED steps, admitted like any other
(1-3); the profile is named here (10); every read is the schedule's tenant's
own registration with the tenant's own token (9).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from .. import refusals
from ..errors import ApiError, Conflict
from ..forgechecks import check_run_finished
from ..forgewrite import (
    CHECKS_READ,
    PULLS_WRITE,
    ChecksForbidden,
    ForgeWriteError,
    ForgeWriteForbidden,
    GitHubWriter,
)
from ..issueruns import FINDINGS_LOCATION_NOTE, FINDINGS_SHAPE, PATCH_FILE, VERDICT_FILE
from ..repositories import Repositories
from ..scheduletypes import BudgetCaps
from ..schemas import WorkflowCreate
from ..validation import MERGE_METADATA_KEY, IssueRef
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "dependency-cve-refresh"

#: The profile every step runs, named here and nowhere else (invariant 10).
PROFILE = "claude-code"

#: SD4: "the other types' figures are set in their lanes". One workflow per
#: firing and a 24 h minimum interval, so one live item; $10 a run is an
#: issue run's three-step shape on one small change; $20 a day leaves room
#: for one "run now" beside the daily firing. No measurement backs these:
#: they are caps, and an unknown cost counts at the per-run cap (§4.3).
BUDGET_CAPS = BudgetCaps(per_run_usd=10, per_day_usd=20, max_concurrent=1)

#: The scan this type reads (.github/workflows/security.yml, job `images`).
SCAN_WORKFLOW = "security.yml"
SCAN_CHECK = "trivy (published images)"
#: The events the `images` job runs on; on any other it is skipped.
SCAN_EVENTS = frozenset({"schedule", "workflow_dispatch"})
#: Default-branch commits walked for the scan without `actions: read`. The
#: scan is weekly; a branch that merged more than this many commits since
#: is `no_scan`, and the outcome says how far it looked.
MAX_COMMITS = 30
#: `security.yml` runs read with `actions: read`, newest first.
MAX_WORKFLOW_RUNS = 30
#: Open pull requests read for this schedule's marker.
MAX_OPEN_PULLS = 100
ACTIONS_READ = "actions: read"

BUMP_STEP = "bump"
REVIEW_STEP = "review"
FIX_STEP = "fix"

#: Lowest first: a severity's index is its rank. Trivy's UNKNOWN ranks below
#: every threshold a schedule can name, so it is never bumped.
SEVERITIES = ("unknown", "low", "medium", "high", "critical")
#: Trivy's `Status` column, present since 0.40. A row without it has the
#: installed and fixed versions straight after the severity.
STATUSES = frozenset({
    "fixed", "affected", "will_not_fix", "fix_deferred", "end_of_life",
    "under_investigation", "unknown", "not_affected",
})

_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_:@/-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~:_-]{0,99}$")
_VULN_ID = re.compile(r"^[A-Z][A-Z0-9]{1,15}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,4}$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$")
_CELLS = re.compile(r"[│┃|]")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: `security.yml` prints `==> <image>:dev` before each image's table.
_IMAGE_LINE = re.compile(r"==>\s+(\S+)\s*$")


class CveScanUnreadable(Conflict):
    code = "cve_scan_unreadable"


def marker(schedule_id: str) -> str:
    """The line this schedule's pull request body carries, so the next firing finds it open."""
    return f"<!-- swarm-schedule: {schedule_id} {TYPE} -->"


# --------------------------------------------------------------------------
# The scan's table, parsed
# --------------------------------------------------------------------------


@dataclass
class Package:
    name: str
    installed: str
    fixed: list[str] = field(default_factory=list)
    severity: str = "unknown"
    vulnerabilities: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "installed": self.installed, "fixed": list(self.fixed),
            "severity": self.severity, "vulnerabilities": list(self.vulnerabilities),
            "images": list(self.images),
        }


def _versions(cell: str) -> list[str]:
    return [v for v in (part.strip() for part in cell.split(",")) if _VERSION.match(v)]


def _add(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def parse_scan(text: str) -> list[Package]:
    """Every FIXABLE row of trivy's table in `text`, one entry per package.

    A row is `│ library │ vulnerability │ severity │ [status │] installed │
    fixed │ title │`. Trivy merges a library's later rows into the first,
    leaving its library and installed cells empty, so those carry over from
    the row above. Anything before the library cell (a log line's timestamp)
    is ignored; a cell that does not match its pattern drops the row.
    """
    found: dict[str, Package] = {}
    image = ""
    library = installed = ""
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        image_match = _IMAGE_LINE.search(line)
        if image_match:
            image = image_match.group(1) if _IMAGE.match(image_match.group(1)) else ""
            library = installed = ""
            continue
        cells = [cell.strip() for cell in _CELLS.split(line)]
        at = next((i for i, cell in enumerate(cells) if _VULN_ID.match(cell)), None)
        if at is None or at < 1 or at + 3 >= len(cells):
            continue
        severity = cells[at + 1].lower()
        if severity not in SEVERITIES:
            continue
        rest = cells[at + 2:]
        status = ""
        if rest and rest[0].lower() in STATUSES:
            status, rest = rest[0].lower(), rest[1:]
        if len(rest) < 2:
            continue
        if cells[at - 1]:
            library = cells[at - 1]
            installed = rest[0]
        elif rest[0]:
            installed = rest[0]
        fixed = _versions(rest[1])
        if not (library and _PACKAGE.match(library) and _VERSION.match(installed or "")):
            continue
        if not fixed or status not in ("", "fixed"):
            continue
        entry = found.setdefault(library, Package(name=library, installed=installed))
        for version in fixed:
            _add(entry.fixed, version)
        _add(entry.vulnerabilities, cells[at])
        _add(entry.images, image)
        if SEVERITIES.index(severity) > SEVERITIES.index(entry.severity):
            entry.severity = severity
    return list(found.values())


def select(packages: Iterable[Package], *, severity_at_least: str, limit: int) -> tuple[list[Package], int]:
    """At or above the threshold, most severe first then by name; and how many were cut."""
    floor = SEVERITIES.index(severity_at_least)
    kept = [p for p in packages if SEVERITIES.index(p.severity) >= floor]
    kept.sort(key=lambda p: (-SEVERITIES.index(p.severity), p.name))
    return kept[:limit], max(0, len(kept) - limit)


# --------------------------------------------------------------------------
# GitHub reads, through the writer's request path
# --------------------------------------------------------------------------


@dataclass
class Scan:
    """What was found of the latest scan in one repository."""

    check_run: dict[str, Any] | None = None
    actions_read: bool | None = None
    #: How it was located: `workflow_run` (with actions: read) or `commit_walk`.
    located_by: str = ""
    commits_walked: int = 0


def _ref(registration: Mapping[str, Any]) -> IssueRef:
    # The writer's routes take an issue reference; `number` is not read by them.
    return IssueRef(owner=str(registration["owner"]), repo=str(registration["repo"]), number=1)


def _get(writer: GitHubWriter, ref: IssueRef, path: str, token: str, what: str, needs: str, *,
         forbidden: type[ForgeWriteForbidden] = ForgeWriteForbidden) -> Any:
    # The writer's own request path, as `mergewake.in_merge_queue` uses it:
    # the pinned host, the token in the Authorization header only, its codes.
    return writer._call("GET", writer._url(ref, path), token, what, needs=needs, forbidden=forbidden)


def _scan_at(writer: GitHubWriter, ref: IssueRef, sha: str, token: str) -> dict[str, Any] | None:
    """The newest `SCAN_CHECK` run at `sha` that completed and was not skipped or cancelled."""
    data = _get(
        writer, ref,
        f"commits/{quote(sha, safe='')}/check-runs?check_name={quote(SCAN_CHECK, safe='')}"
        "&filter=latest&per_page=10",
        token, f"the {SCAN_CHECK} check run at {ref.repository}@{sha[:12]}", CHECKS_READ,
        forbidden=ChecksForbidden,
    )
    runs = data.get("check_runs") if isinstance(data, dict) else None
    for run in runs if isinstance(runs, list) else []:
        # Finished as every merge judge reads it: status completed, OR a
        # conclusion with a completed_at (BGD 2026-10-23, CHECK-STUCK-2, part of #2031).
        if (isinstance(run, dict) and run.get("name") == SCAN_CHECK and check_run_finished(run)
                and run.get("conclusion") in ("success", "failure")):
            return run
    return None


def find_scan(writer: GitHubWriter, ref: IssueRef, branch: str, token: str) -> Scan:
    """Locate the latest scan: by workflow run with `actions: read`, else by walking commits."""
    scan = Scan()
    try:
        data = _get(
            writer, ref,
            f"actions/workflows/{SCAN_WORKFLOW}/runs?branch={quote(branch, safe='')}"
            f"&status=completed&per_page={MAX_WORKFLOW_RUNS}",
            token, f"the {SCAN_WORKFLOW} runs of {ref.repository}", ACTIONS_READ,
            forbidden=ChecksForbidden,
        )
        scan.actions_read = True
    except ForgeWriteForbidden:
        scan.actions_read = False
        data = None
    except ForgeWriteError as exc:
        # 404: no such workflow, or a token that cannot see Actions at all
        # (GitHub answers some refusals 404). The commit walk still answers.
        if exc.status_code != 404:
            raise
        data = None
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    for run in runs if isinstance(runs, list) else []:
        if isinstance(run, dict) and run.get("event") in SCAN_EVENTS and isinstance(run.get("head_sha"), str):
            scan.located_by = "workflow_run"
            scan.check_run = _scan_at(writer, ref, run["head_sha"], token)
            if scan.check_run is not None:
                return scan
    commits = _get(
        writer, ref, f"commits?sha={quote(branch, safe='')}&per_page={MAX_COMMITS}",
        token, f"the commits of {ref.repository}@{branch}", "contents: read",
    )
    scan.located_by = "commit_walk"
    for commit in commits if isinstance(commits, list) else []:
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str):
            continue
        scan.commits_walked += 1
        scan.check_run = _scan_at(writer, ref, sha, token)
        if scan.check_run is not None:
            return scan
    return scan


def scan_text(writer: GitHubWriter, ref: IssueRef, check_run: Mapping[str, Any], token: str) -> tuple[list[Package], list[str]]:
    """The packages of a failed scan, from the first source that lists any, and the sources tried."""
    tried: list[str] = []
    output = check_run.get("output") if isinstance(check_run.get("output"), dict) else {}
    text = "\n".join(str(output.get(k) or "") for k in ("title", "summary", "text"))
    tried.append("check_run_output")
    packages = parse_scan(text)
    run_id = check_run.get("id")
    if not packages and isinstance(run_id, int) and int(output.get("annotations_count") or 0):
        tried.append("annotations")
        notes = writer.check_annotations(ref, run_id, token)
        packages = parse_scan("\n".join(
            "\n".join(str(n.get(k) or "") for k in ("title", "message", "raw_details")) for n in notes
        ))
    if not packages and isinstance(run_id, int):
        # An Actions check run's id is its job's id. None without actions: read.
        tried.append("job_log_tail")
        tail = writer.job_log_tail(ref, run_id, token)
        if tail is None:
            tried[-1] = "job_log_tail (unread: needs actions: read)"
        else:
            packages = parse_scan(tail)
    return packages, tried


def open_pull(writer: GitHubWriter, ref: IssueRef, token: str, schedule_id: str) -> int | None:
    """The number of an open pull request carrying this schedule's marker, or None."""
    pulls = _get(writer, ref, f"pulls?state=open&per_page={MAX_OPEN_PULLS}", token,
                 f"the open pull requests of {ref.repository}", PULLS_WRITE)
    line = marker(schedule_id)
    for pull in pulls if isinstance(pulls, list) else []:
        if isinstance(pull, dict) and line in str(pull.get("body") or "") and isinstance(pull.get("number"), int):
            return int(pull["number"])
    return None


# --------------------------------------------------------------------------
# The workflow
# --------------------------------------------------------------------------


def packages_digest(packages: Sequence[Package]) -> str:
    return hashlib.sha256(json.dumps([p.as_dict() for p in packages], sort_keys=True).encode()).hexdigest()


def _package_lines(packages: Sequence[Package]) -> str:
    return "\n".join(
        f"- {p.name}: installed {p.installed}, fixed in {' or '.join(p.fixed)} "
        f"({p.severity}; {', '.join(p.vulnerabilities)})"
        + (f"; images: {', '.join(i for i in p.images if i)}" if any(p.images) else "")
        for p in packages
    )


def bump_prompt(packages: Sequence[Package], paths: Sequence[str], schedule_id: str, scan_sha: str) -> str:
    where = ", ".join(paths)
    return (
        f"The latest image vulnerability scan ({SCAN_CHECK}, at commit {scan_sha[:12]}) lists "
        "these packages as vulnerable with a fixed version available. Every line was parsed "
        "by SwarmCloud from the scan's table and checked against strict patterns:\n\n"
        f"{_package_lines(packages)}\n\n"
        f"Bump the package pins in the image definitions under {where} so that each listed "
        "package installs at least its fixed version. Follow the way this repository already "
        "pins packages in those files (the libtiff fix of 2026-10-08 is the model: "
        "`git log -S libtiff` finds it). Change only files under those paths, and only the "
        "pins: no refactoring, no other upgrades. A package that no file there pins (it "
        "comes from the base image) is left as it is and named in the pull request body "
        "as not bumped, with why. If nothing can be bumped, change nothing.\n\n"
        "Write $SWARM_ARTIFACTS_DIR/pr-title.txt: one line stating the bump as a fact. "
        "Write $SWARM_ARTIFACTS_DIR/pr-body.md: each package bumped, from and to, its "
        "vulnerabilities, and each not bumped with why; and as its last line, exactly:\n"
        f"{marker(schedule_id)}\n"
        "Do not write a closing keyword (Closes, Fixes or Resolves followed by an issue "
        "reference) anywhere."
    )


def review_prompt(packages: Sequence[Package], paths: Sequence[str]) -> str:
    return (
        "Review a pull request that bumps package pins in image definitions to fix these "
        "vulnerable packages:\n\n"
        f"{_package_lines(packages)}\n\n"
        f"The diff is staged as {PATCH_FILE}. Check that every changed file is under "
        f"{', '.join(paths)}; that each bumped package reaches at least its fixed version; "
        "that nothing else changed (no unrelated upgrades, no edits to build logic); and "
        "that a package not bumped is explained in pr-body.md. The release builds and "
        "deploys these images, so a wrong pin reaches the platform. Do not edit files. "
        f"Write $SWARM_ARTIFACTS_DIR/{VERDICT_FILE}: "
        '{"verdict": "MERGE" or "NOT_YET", ' + FINDINGS_SHAPE + "}. " + FINDINGS_LOCATION_NOTE
    )


def workflow_spec(registration: Mapping[str, Any], packages: Sequence[Package], paths: Sequence[str],
                  schedule_id: str, scan_sha: str) -> WorkflowCreate:
    """bump -> review -> fix (gated on NOT_YET, the integrator), `integrate`, never merged."""
    return WorkflowCreate.model_validate({
        "strategy": "integrate",
        "repository_url": str(registration["repository_url"]),
        "steps": [
            {
                "step_id": BUMP_STEP,
                "runner_profile": PROFILE,
                "allow_empty_diff": True,
                "input": {"prompt": bump_prompt(packages, paths, schedule_id, scan_sha)},
            },
            {
                "step_id": REVIEW_STEP,
                "runner_profile": PROFILE,
                "depends_on": [BUMP_STEP],
                "builds_on": BUMP_STEP,
                "input_from": {BUMP_STEP: PATCH_FILE},
                "input": {"prompt": review_prompt(packages, paths)},
            },
            {
                "step_id": FIX_STEP,
                "runner_profile": PROFILE,
                "depends_on": [REVIEW_STEP],
                "builds_on": BUMP_STEP,
                "input_from": {REVIEW_STEP: VERDICT_FILE},
                "when": {"step": REVIEW_STEP, "verdict_in": ["NOT_YET"]},
                "input": {"prompt": (
                    f"Fix every finding in {VERDICT_FILE} about this package-pin bump. Change "
                    f"only files under {', '.join(paths)}, and nothing the findings do not name. "
                    "Do not write a closing keyword anywhere."
                )},
            },
        ],
        "metadata": {
            # Stated, so no repository merge_policy or platform default can
            # append a merge: a person merges (module docstring).
            MERGE_METADATA_KEY: "off",
            "unit": "Bump fixable vulnerable package pins in the image definitions",
            "cve_refresh": {
                "schedule_id": schedule_id,
                "scan_sha": scan_sha,
                "packages": [p.name for p in packages],
                "packages_digest": packages_digest(packages),
            },
        },
    })


# --------------------------------------------------------------------------
# One firing
# --------------------------------------------------------------------------


@dataclass
class Plan:
    """Per repository, what was found and what a firing does with it."""

    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: (repo_id, registration, packages, scan sha) of the one repository to submit for.
    chosen: tuple[str, Mapping[str, Any], list[Package], str] | None = None


FAILED_OUTCOMES = ("unlisted", "no_scan", "failed")


def _read_repository(writer: GitHubWriter, registration: Mapping[str, Any], token: str,
                     params: Any, schedule_id: str) -> tuple[dict[str, Any], list[Package], str]:
    ref = _ref(registration)
    branch = str(registration.get("default_branch") or "main")
    scan = find_scan(writer, ref, branch, token)
    row: dict[str, Any] = {
        "repository": ref.repository,
        "actions_read": scan.actions_read,
        "located_by": scan.located_by,
    }
    if scan.located_by == "commit_walk":
        row["commits_walked"] = scan.commits_walked
    run = scan.check_run
    if run is None:
        row["outcome"] = "no_scan"
        row["why"] = (f"no completed {SCAN_CHECK} check run on {branch}"
                      + (f" in its last {scan.commits_walked} commits" if scan.located_by == "commit_walk" else ""))
        return row, [], ""
    sha = str(run.get("head_sha") or "")
    row["scan"] = {"sha": sha, "conclusion": run.get("conclusion"), "completed_at": run.get("completed_at")}
    if run.get("conclusion") == "success":
        row["outcome"] = "clean"
        return row, [], sha
    found, tried = scan_text(writer, ref, run, token)
    row["sources"] = tried
    if not found:
        row["outcome"] = "unlisted"
        row["why"] = ("the scan failed and lists no package in its check run; its table is in the "
                      "job log, which needs actions: read"
                      + (", and the token does not hold it" if not scan.actions_read else ""))
        return row, [], sha
    chosen, cut = select(found, severity_at_least=params.severity_at_least, limit=params.max_packages)
    row["fixable"] = len(found)
    if not chosen:
        row["outcome"] = "below_threshold"
        return row, [], sha
    row["packages"] = [p.as_dict() for p in chosen]
    if cut:
        row["packages_cut"] = cut
    number = open_pull(writer, ref, token, schedule_id)
    if number is not None:
        row["outcome"] = "pr_open"
        row["pull_request"] = number
        return row, [], sha
    row["outcome"] = "fixable"
    return row, chosen, sha


def plan(firing: Any) -> Plan:
    """Read every repository in scope until one has packages to bump. Reads only."""
    ctx = firing.ctx
    params = issue_sweep.params_of(firing)
    tenant_id = str(firing.schedule["tenant_id"])
    schedule_id = str(firing.schedule["schedule_id"])
    tenant = ctx.store.get_tenant(tenant_id)
    token = ctx.forge_tokens.token_for(tenant)
    writer = ctx.forge_writer or GitHubWriter()
    registrations = Repositories(ctx.db, now=ctx.now)
    out = Plan()
    for repo_id in firing.repo_ids:
        if out.chosen is not None:
            out.outcomes[repo_id] = {"outcome": "deferred", "why": "one pull request per firing"}
            continue
        registration = registrations.find(tenant_id, repo_id)
        if registration is None:
            continue
        try:
            row, packages, sha = _read_repository(writer, registration, token, params, schedule_id)
        except ApiError as exc:
            out.outcomes[repo_id] = {"repository": f"{registration.get('owner')}/{registration.get('repo')}",
                                     "outcome": f"failed: {exc.code}", "why": exc.message}
            continue
        out.outcomes[repo_id] = row
        if packages:
            out.chosen = (repo_id, registration, packages, sha)
    return out


def _unreadable(outcomes: Mapping[str, Mapping[str, Any]]) -> bool:
    return bool(outcomes) and all(
        str(row.get("outcome")).split(":", 1)[0] in FAILED_OUTCOMES for row in outcomes.values()
    )


def _summary(outcomes: Mapping[str, Mapping[str, Any]]) -> str:
    return "; ".join(
        f"{row.get('repository') or repo_id}: {row.get('outcome')}"
        + (" (no actions: read)" if row.get("actions_read") is False else "")
        for repo_id, row in outcomes.items()
    )[:1500]


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    schedule_id = str(firing.schedule["schedule_id"])
    found = plan(firing)
    work: list[dict[str, Any]] = []
    if found.chosen is not None:
        repo_id, registration, packages, sha = found.chosen
        if firing.room < 1:
            found.outcomes[repo_id]["outcome"] = "room"
        else:
            work.append(firing.submit_workflow(
                workflow_spec(registration, packages, params.paths, schedule_id, sha), repo_id=repo_id,
            ))
            found.outcomes[repo_id]["outcome"] = "submitted"
    log.info("schedule %s firing %s: %s", schedule_id, firing.firing["firing_id"],
             json.dumps(found.outcomes, sort_keys=True, default=str))
    if not work and _unreadable(found.outcomes):
        # Report-only until switched on (refusals.py): off, the firing ends
        # with nothing submitted and this log line names what it would refuse.
        refusals.refuse(CveScanUnreadable(
            "no repository's image scan could be read for fixable packages: " + _summary(found.outcomes)
            + ". The scan's table is in its job log, which needs the GitHub App's Actions: Read",
            detail={"repositories": found.outcomes},
        ))
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    schedule_id = str(firing.schedule["schedule_id"])
    try:
        found = plan(firing)
    except ApiError as exc:
        if exc.status_code >= 500:
            raise
        return {"would_submit": None, "error": {"code": exc.code, "message": exc.message}}
    would: dict[str, Any] | None = None
    if found.chosen is not None:
        repo_id, registration, packages, sha = found.chosen
        spec = workflow_spec(registration, packages, params.paths, schedule_id, sha)
        would = {
            "repo_id": repo_id,
            "room": firing.room,
            "steps": [step.step_id for step in spec.steps],
            "profile": PROFILE,
            "merge": spec.metadata[MERGE_METADATA_KEY],
            "packages": [p.name for p in packages],
        }
    return {
        "would_submit": would,
        "repositories": found.outcomes,
        "unreadable": _unreadable(found.outcomes),
        "severity_at_least": params.severity_at_least,
        "paths": list(params.paths),
    }
