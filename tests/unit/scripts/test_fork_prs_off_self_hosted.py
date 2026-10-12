"""No job can run a fork pull request on a self-hosted runner.

OWNER DECISION, 2026-10-12. This repository is public, its fork-PR approval
policy is `first_time_contributors` (left as it is), and its jobs may target
the ephemeral GCP VMs that `runner-dispatcher` starts for the `bogdan-ci`
label (repo webhook `/hook/swarmcloud`, sagaxyz/ai-studio#336). A RETURNING
outside contributor's fork PR needs no approval, so a self-hosted job it
reaches would run their code on our VMs. The decision: fork PRs never reach
the VMs. Pushes and same-repository PRs may.

So every job whose `runs-on` can name a self-hosted runner must spell it as
the one approved expression, which picks a GitHub-hosted image whenever the
event is a pull request from a fork:

    runs-on: ${{ (github.event_name == 'pull_request' || github.event_name == 'pull_request_target') && github.event.pull_request.head.repo.full_name != github.repository && 'ubuntu-latest' || fromJSON('["self-hosted","linux","x64","bogdan-ci"]') }}

The guard FAILS CLOSED: any label that is not a GitHub-hosted image name
(`ubuntu-*`, `windows-*`, `macos-*`) is treated as self-hosted, and any
`runs-on` expression it cannot resolve is refused, so a new spelling has to
be added here deliberately rather than slipping past.

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
    r"\(github\.event_name == 'pull_request' \|\| github\.event_name == 'pull_request_target'\)"
    r" && github\.event\.pull_request\.head\.repo\.full_name != github\.repository"
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
        (APPROVED.replace("'ubuntu-latest'", "'bogdan-ci'"), "EXPOSED"),
        # The check inverted (== instead of !=) is not the approved spelling.
        (APPROVED.replace("!=", "=="), "EXPOSED"),
        # Only one of the two PR events checked.
        (APPROVED.replace(" || github.event_name == 'pull_request_target'", ""), "EXPOSED"),
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
