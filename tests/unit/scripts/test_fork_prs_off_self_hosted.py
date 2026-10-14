"""No job can run a fork pull request on a self-hosted runner, and every job
that is not on the self-hosted pool is there for a stated, checked reason.

OWNER DECISION, 2026-10-12. This repository is public, its fork-PR approval
policy is `first_time_contributors` (left as it is), and its jobs may target
the ephemeral GCP VMs that `runner-dispatcher` starts for the `bogdan-ci`
label (repo webhook `/hook/swarmcloud`, sagaxyz/ai-studio#336). A RETURNING
outside contributor's fork PR needs no approval, so a self-hosted job it
reaches would run their code on our VMs. The decision: fork PRs never reach
the VMs. Pushes and same-repository PRs may.

OWNER DECISION, 2026-10-14: "make sure we use CI runners for all jobs moving
forward if capacity allows". So EVERY job uses the one approved expression
unless it is in HOSTED_JOBS below, and each HOSTED_JOBS entry names a reason
this file checks against the job (it holds a cloud identity, the merge App's
key, or a forge token). A new job that silently uses a hosted image fails here,
and so does a job that keeps a hosted entry after its credential is gone.

The approved expression, with its capacity escape hatch: the repo variable
SELF_HOSTED_CI set to `off` sends every guarded job back to GitHub-hosted
ubuntu-24.04 (unset, or anything else, means the pool):

    runs-on: ${{ (vars.SELF_HOSTED_CI == 'off' || ((github.event_name == 'pull_request' || github.event_name == 'pull_request_target') && github.event.pull_request.head.repo.full_name != github.repository)) && 'ubuntu-24.04' || fromJSON('["self-hosted","linux","x64","bogdan-ci"]') }}

The guard FAILS CLOSED: any label that is not a GitHub-hosted image name
(`ubuntu-*`, `windows-*`, `macos-*`) is treated as self-hosted, and any
`runs-on` expression it cannot resolve is refused, so a new spelling has to
be added here deliberately rather than slipping past. The expression is also
EVALUATED (resolve() below) for a push, a same-repository PR and a fork PR,
with the hatch on and off, so the property is asserted, not only the text.

Matrix-driven `runs-on` (`${{ matrix.os }}`) is resolved against the job's
`strategy.matrix` values and `include` entries, each value judged on its own.

NOT COVERED by the expression, and so not by this guard: `workflow_run`
(the triggering PR's fork is `github.event.workflow_run.head_repository`, and
`github.event.pull_request` is absent there, so the expression resolves to
self-hosted). A `workflow_run` job runs the default branch's workflow file,
which is trusted; it runs fork code only if it checks out the triggering
head. Do not move such a job to self-hosted without that check.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"

HOSTED = re.compile(r"^(ubuntu|windows|macos)-[\w.-]+$")

#: The one approved spelling. Group 1 is the hosted fallback, group 2 the
#: JSON label list the job gets on a push or a same-repository PR.
FORK_GATED = re.compile(
    r"^\$\{\{\s*"
    r"\(vars\.SELF_HOSTED_CI == 'off'"
    r" \|\| \(\(github\.event_name == 'pull_request' \|\| github\.event_name == 'pull_request_target'\)"
    r" && github\.event\.pull_request\.head\.repo\.full_name != github\.repository\)\)"
    r" && '([^']+)' \|\| fromJSON\('(\[[^']*\])'\)"
    r"\s*\}\}$"
)
MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([\w-]+)\s*\}\}$")


def _labels(value: object) -> list[str]:
    """The labels a literal (non-expression) runs-on value names."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):  # {group: ..., labels: ...}
        out = []
        if "group" in value:
            out.append(f"group:{value['group']}")  # a runner group is never hosted
        return out + _labels(value.get("labels", []))
    raise TypeError(f"unrecognised runs-on: {value!r}")


def _self_hosted(labels: list[str]) -> bool:
    return not labels or any(not HOSTED.match(label) for label in labels)


def _matrix_values(job: dict, key: str) -> list[object]:
    matrix = ((job.get("strategy") or {}).get("matrix")) or {}
    if isinstance(matrix, str):
        return []  # fromJSON(...) matrix: unresolvable
    values = list(matrix.get(key, []) or [])
    values += [inc[key] for inc in matrix.get("include", []) or [] if key in inc]
    return values


def verdict(job: dict) -> tuple[str, str]:
    """('hosted' | 'gated' | 'EXPOSED', why) for one job's runs-on."""
    if "uses" in job and "runs-on" not in job:
        return "hosted", "calls a reusable workflow (its own jobs are scanned)"
    runs_on = job.get("runs-on")
    if runs_on is None:
        return "EXPOSED", "no runs-on"
    if isinstance(runs_on, str) and "${{" in runs_on:
        gated = FORK_GATED.match(runs_on.strip())
        if gated:
            fallback, labels = gated.group(1), json.loads(gated.group(2))
            if _self_hosted([fallback]):
                return "EXPOSED", f"fork fallback {fallback!r} is not a hosted image"
            return "gated", f"self-hosted {labels} behind the fork check"
        ref = MATRIX_REF.match(runs_on.strip())
        if ref:
            values = _matrix_values(job, ref.group(1))
            if not values:
                return "EXPOSED", f"matrix.{ref.group(1)} has no static values to resolve"
            for v in values:
                if isinstance(v, str) and "${{" in v:
                    sub = verdict({"runs-on": v})
                    if sub[0] == "EXPOSED":
                        return sub
                elif _self_hosted(_labels(v)):
                    return "EXPOSED", f"matrix value {v!r} is self-hosted with no fork check"
            return "hosted", f"matrix.{ref.group(1)} resolves to hosted images only"
        return "EXPOSED", f"unrecognised runs-on expression: {runs_on!r}"
    labels = _labels(runs_on)
    if _self_hosted(labels):
        return "EXPOSED", f"bare self-hosted labels {labels} reach fork PRs"
    return "hosted", f"{labels}"


def _jobs():
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files, f"no workflows found under {WORKFLOWS}"  # the scan saw something
    for path in files:
        doc = yaml.safe_load(path.read_text())
        for name, job in (doc.get("jobs") or {}).items():
            yield f"{path.name}:{name}", job


def test_no_job_runs_a_fork_pr_on_a_self_hosted_runner() -> None:
    jobs = list(_jobs())
    assert jobs, "no jobs parsed: the guard would assert over nothing"
    verdicts = {name: verdict(job) for name, job in jobs}
    gated = sorted(n for n, (v, _) in verdicts.items() if v == "gated")
    print(f"scanned {len(jobs)} job(s); {len(gated)} self-hosted behind the fork check: {gated}")
    exposed = {n: why for n, (v, why) in verdicts.items() if v == "EXPOSED"}
    assert not exposed, "jobs that could run a fork PR on a self-hosted runner:\n" + "\n".join(
        f"  {n}: {why}" for n, why in sorted(exposed.items())
    )


APPROVED = (
    "${{ (vars.SELF_HOSTED_CI == 'off' || ((github.event_name == 'pull_request'"
    " || github.event_name == 'pull_request_target')"
    " && github.event.pull_request.head.repo.full_name != github.repository))"
    " && 'ubuntu-24.04' || fromJSON('[\"self-hosted\",\"linux\",\"x64\",\"bogdan-ci\"]') }}"
)
#: The spelling PR 1174 approved, before the escape hatch. Fork-safe, but no
#: longer the one spelling: a job still using it ignores SELF_HOSTED_CI=off.
PRE_HATCH = (
    "${{ (github.event_name == 'pull_request' || github.event_name == 'pull_request_target')"
    " && github.event.pull_request.head.repo.full_name != github.repository"
    " && 'ubuntu-latest' || fromJSON('[\"self-hosted\",\"linux\",\"x64\",\"bogdan-ci\"]') }}"
)


@pytest.mark.parametrize(
    ("runs_on", "expected"),
    [
        # Positive controls: what must pass.
        ("ubuntu-24.04", "hosted"),
        (["ubuntu-latest"], "hosted"),
        (APPROVED, "gated"),
        # What must fail.
        (["self-hosted", "bogdan-ci"], "EXPOSED"),
        ("bogdan-ci", "EXPOSED"),
        ("self-hosted", "EXPOSED"),
        ({"group": "vms", "labels": ["bogdan-ci"]}, "EXPOSED"),
        ("${{ fromJSON('[\"self-hosted\",\"bogdan-ci\"]') }}", "EXPOSED"),
        # The fork check with its fallback swapped to a self-hosted label.
        (APPROVED.replace("'ubuntu-24.04'", "'bogdan-ci'"), "EXPOSED"),
        # The check inverted (== instead of !=) is not the approved spelling.
        (APPROVED.replace("!=", "=="), "EXPOSED"),
        # Only one of the two PR events checked.
        (APPROVED.replace(" || github.event_name == 'pull_request_target'", ""), "EXPOSED"),
        # The escape hatch dropped, or the pre-hatch spelling: not the one spelling.
        (APPROVED.replace("vars.SELF_HOSTED_CI == 'off' || ", ""), "EXPOSED"),
        (PRE_HATCH, "EXPOSED"),
    ],
)
def test_verdict(runs_on: object, expected: str) -> None:
    assert verdict({"runs-on": runs_on})[0] == expected


def test_matrix_runs_on_is_resolved() -> None:
    job = {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": {"os": ["ubuntu-24.04"]}}}
    assert verdict(job)[0] == "hosted"
    job["strategy"]["matrix"]["include"] = [{"os": ["self-hosted", "bogdan-ci"]}]
    assert verdict(job)[0] == "EXPOSED"
    job["strategy"]["matrix"]["include"] = [{"os": APPROVED}]
    assert verdict(job)[0] == "hosted"
    assert verdict({"runs-on": "${{ matrix.os }}"})[0] == "EXPOSED"  # unresolvable


# --- The expression, evaluated ----------------------------------------------

POOL = ["self-hosted", "linux", "x64", "bogdan-ci"]
REPOSITORY = "bogdan-alexandrescu/SwarmCloud"
#: The only context paths the approved expression reads. Anything else in an
#: expression handed to resolve() is refused rather than guessed at.
_CONTEXT_PATH = re.compile(r"\b(?:vars|github)(?:\.[A-Za-z_][\w-]*)+")


def resolve(runs_on: str, *, event: str, fork: bool, hatch: str = "") -> object:
    """What GitHub would put in `runs-on` for this event: evaluates the
    expression's &&/|| (which, as in Python's and/or, return an operand), with
    `github.event.pull_request` null on a push as it is on GitHub. `hatch` is
    vars.SELF_HOSTED_CI; GitHub gives an unset variable as ''."""
    body = runs_on.strip()
    assert body.startswith("${{") and body.endswith("}}"), body
    body = body[3:-2]
    is_pr = event in ("pull_request", "pull_request_target")
    values = {
        "vars.SELF_HOSTED_CI": hatch,
        "github.event_name": event,
        "github.repository": REPOSITORY,
        "github.event.pull_request.head.repo.full_name": (
            ("someone/SwarmCloud" if fork else REPOSITORY) if is_pr else None
        ),
    }
    names: dict[str, object] = {}

    def bind(match: re.Match) -> str:
        path = match.group(0)
        assert path in values, f"resolve() does not model {path!r}"
        name = f"_v{len(names)}"
        names[name] = values[path]
        return name

    # Contexts first, so a quoted '&&' or 'fromJSON' can never be rewritten
    # inside a path; the approved expression has neither inside a string.
    py = _CONTEXT_PATH.sub(bind, body)
    py = py.replace("&&", " and ").replace("||", " or ").replace("fromJSON(", "_fromjson(")
    return eval(py, {"__builtins__": {}}, {**names, "_fromjson": json.loads})  # noqa: S307


@pytest.mark.parametrize(
    ("event", "fork", "hatch", "expected"),
    [
        ("push", False, "", POOL),
        ("push", False, "on", POOL),
        ("merge_group", False, "", POOL),
        ("workflow_dispatch", False, "", POOL),
        ("pull_request", False, "", POOL),
        ("pull_request_target", False, "", POOL),
        # Fork PRs never reach the pool, whatever the hatch says.
        ("pull_request", True, "", "ubuntu-24.04"),
        ("pull_request", True, "on", "ubuntu-24.04"),
        ("pull_request_target", True, "", "ubuntu-24.04"),
        # The capacity escape hatch sends everything to GitHub-hosted.
        ("push", False, "off", "ubuntu-24.04"),
        ("pull_request", False, "off", "ubuntu-24.04"),
        ("pull_request", True, "off", "ubuntu-24.04"),
    ],
)
def test_the_approved_expression_resolves(event: str, fork: bool, hatch: str, expected: object) -> None:
    assert resolve(APPROVED, event=event, fork=fork, hatch=hatch) == expected


def test_resolve_can_come_out_the_other_way() -> None:
    """The control: the inverted fork check sends a fork PR to the pool, and
    the pre-hatch spelling ignores the hatch. If resolve() could not tell
    these apart, its passes above would mean nothing."""
    inverted = APPROVED.replace("!=", "==")
    assert resolve(inverted, event="pull_request", fork=True) == POOL
    assert resolve(PRE_HATCH, event="push", fork=False, hatch="off") == POOL


def test_every_guarded_job_in_the_workflows_resolves_safely() -> None:
    """The spellings actually in the files, not only the constant above."""
    seen = 0
    for name, job in _jobs():
        runs_on = job.get("runs-on")
        if not (isinstance(runs_on, str) and FORK_GATED.match(runs_on.strip())):
            continue
        seen += 1
        for event in ("pull_request", "pull_request_target"):
            assert resolve(runs_on, event=event, fork=True) == "ubuntu-24.04", name
        assert resolve(runs_on, event="push", fork=False, hatch="off") == "ubuntu-24.04", name
        assert resolve(runs_on, event="push", fork=False) == POOL, name
    assert seen, "no guarded job found: the loop asserted over nothing"


# --- Every job is on the pool, or hosted for a checked reason ---------------

CLOUD = "cloud"
APP_KEY = "app-key"
FORGE = "forge-token"

REASONS = {
    CLOUD: (
        "holds a Google identity through workload identity (id-token: write and "
        "google-github-actions/auth): deploy and cloud credentials stay on "
        "GitHub-hosted, the rule PR 1184 recorded for release.yml"
    ),
    APP_KEY: (
        "holds the merge App's private key (secrets.MERGE_APP_PRIVATE_KEY). Kept "
        "hosted, decided 2026-10-14: on a bogdan-ci VM the key sits in that VM's "
        "memory and disk for the job's life, readable by whoever holds IAP SSH "
        "(the VMs' one ingress rule) or disk-snapshot rights in the runner "
        "project. That audience is not shown to be no wider than GitHub's, so "
        "the default the brief set (keep it hosted) stands"
    ),
    FORGE: (
        "holds a forge token (secrets.SWARM_SANDBOX_GITHUB_TOKEN, a broad PAT): "
        "kept hosted for the same reason as the merge App's key"
    ),
}

#: Every job NOT on the guarded expression. Keyed `file:job`.
HOSTED_JOBS: dict[str, str] = {
    "accept.yml:target": CLOUD,
    "accept.yml:quiesce": CLOUD,
    "accept.yml:smoke": CLOUD,
    "accept.yml:sandbox": FORGE,
    "accept.yml:acceptance": CLOUD,
    "accept.yml:acceptance-2": CLOUD,
    "accept.yml:sandbox-results": FORGE,
    "accept.yml:report": CLOUD,
    "application.yml:build": CLOUD,
    "auto-merge.yml:enable": APP_KEY,
    "auto-merge.yml:disable": APP_KEY,
    "ci-fix.yml:fix": CLOUD,
    "hotfix.yml:images": CLOUD,
    "hotfix.yml:promote": CLOUD,
    "hotfix.yml:infrastructure": CLOUD,
    "hotfix.yml:deploy": CLOUD,
    "iam-refusal-probe.yml:probe": CLOUD,
    "release.yml:build": CLOUD,
    "release.yml:promote": CLOUD,
    "release.yml:infrastructure": CLOUD,
    "release.yml:infrastructure-iam": CLOUD,
    "release.yml:deploy": CLOUD,
    "security.yml:images": CLOUD,
    "terraform.yml:plan": CLOUD,
}

_SECRET = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


def _workflow_jobs():
    """(file:job, job, the workflow's own top-level permissions)."""
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    for path in files:
        doc = yaml.safe_load(path.read_text())
        for name, job in (doc.get("jobs") or {}).items():
            yield f"{path.name}:{name}", job, doc.get("permissions")


def _id_token(job: dict, workflow_permissions: object) -> bool:
    """A job's own `permissions` replaces the workflow's entirely."""
    perms = job["permissions"] if "permissions" in job else workflow_permissions
    if perms == "write-all":
        return True
    return isinstance(perms, dict) and perms.get("id-token") == "write"


def _secrets(job: dict) -> set[str]:
    return set(_SECRET.findall(yaml.safe_dump(job))) - {"GITHUB_TOKEN"}


def _uses(job: dict) -> list[str]:
    return [str(s.get("uses", "")) for s in job.get("steps") or []]


def _reason_holds(reason: str, job: dict, workflow_permissions: object) -> bool:
    if reason == CLOUD:
        return _id_token(job, workflow_permissions) and any(
            u.startswith("google-github-actions/auth@") for u in _uses(job)
        )
    if reason == APP_KEY:
        return "MERGE_APP_PRIVATE_KEY" in _secrets(job)
    if reason == FORGE:
        return "SWARM_SANDBOX_GITHUB_TOKEN" in _secrets(job)
    raise AssertionError(f"unknown reason {reason!r}")


def test_every_job_is_on_the_pool_or_hosted_for_a_checked_reason() -> None:
    jobs = list(_workflow_jobs())
    assert jobs, "no jobs parsed"
    problems = []
    names = set()
    for name, job, wf_perms in jobs:
        names.add(name)
        if "uses" in job and "runs-on" not in job:
            continue  # a reusable workflow call: its own jobs are scanned
        runs_on = job.get("runs-on")
        gated = isinstance(runs_on, str) and bool(FORK_GATED.match(runs_on.strip()))
        if name in HOSTED_JOBS:
            reason = HOSTED_JOBS[name]
            if gated:
                problems.append(f"{name}: listed as hosted ({reason}) but on the guarded expression")
            elif verdict(job)[0] != "hosted":
                problems.append(f"{name}: listed as hosted but runs-on {runs_on!r} is not a hosted image")
            elif not _reason_holds(reason, job, wf_perms):
                problems.append(f"{name}: kept hosted because it {REASONS[reason]}, which is no longer true")
        elif not gated:
            problems.append(
                f"{name}: runs-on {runs_on!r} is neither the guarded expression nor in HOSTED_JOBS"
            )
    stale = sorted(set(HOSTED_JOBS) - names)
    problems += [f"{n}: in HOSTED_JOBS but no such job" for n in stale]
    print(f"checked {len(jobs)} job(s), {len(HOSTED_JOBS)} kept hosted")
    assert not problems, "\n".join(problems)


def test_no_guarded_job_holds_a_cloud_identity_or_a_repository_secret() -> None:
    """The other direction: a credential never rides onto the pool by a job
    being moved, or by a credential being added to a job already there."""
    held = []
    for name, job, wf_perms in _workflow_jobs():
        runs_on = job.get("runs-on")
        if not (isinstance(runs_on, str) and FORK_GATED.match(runs_on.strip())):
            continue
        if _id_token(job, wf_perms):
            held.append(f"{name}: id-token: write")
        if _secrets(job):
            held.append(f"{name}: secrets {sorted(_secrets(job))}")
    assert not held, "credentials on self-hosted jobs:\n" + "\n".join(held)


def test_the_reason_checks_can_fail() -> None:
    """Controls: each reason is false for a job without its credential."""
    bare = {"runs-on": "ubuntu-24.04", "steps": [{"run": "true"}]}
    for reason in REASONS:
        assert not _reason_holds(reason, bare, {"contents": "read"}), reason
    cloud = {"permissions": {"id-token": "write"}, "steps": [{"uses": "google-github-actions/auth@v3"}]}
    assert _reason_holds(CLOUD, cloud, None)
    # A job's own permissions replace the workflow's.
    assert not _id_token({"permissions": {"contents": "read"}}, {"id-token": "write"})
    assert _id_token({}, {"id-token": "write"})
