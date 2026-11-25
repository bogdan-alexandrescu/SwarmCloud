"""The nightly coverage report (issue 64; owner decision 2026-11-23; docs/ci.md
"Coverage floors"): coverage is measured once a day, off the merge path.

Held here: scripts/coverage-report.sh, driven against a stand-in `gh`: a red
night opens ONE issue (found by its marker, never a second) naming the
packages and the commits since the last green run; a green night comments and
closes it; a night with no floors files nothing and prints the `fill` command.

THE TESTS THAT READ .github/ hold CI security decisions the owner kept on
2026-11-25: that coverage.yml's permissions are explicit and only its report
job writes. This file is on scripts/check_test_reads.py's SECURITY_GUARDS
list for those tests alone (issue 71).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
REPORT = ROOT / "scripts" / "coverage-report.sh"
MARKER = "<!-- swarm-coverage: nightly line coverage is under a floor -->"
PIN = re.compile(r"^([\w.-]+/[\w.-]+)@([0-9a-f]{40})$")


# --- the report ------------------------------------------------------------------

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
case "$1 $2" in
  "issue list")
    if [ -n "${FAKE_OPEN_ISSUE:-}" ]; then echo "${FAKE_OPEN_ISSUE}"; fi ;;
  "run list")
    if [ -n "${FAKE_LAST_GREEN:-}" ]; then echo "${FAKE_LAST_GREEN} https://example.invalid/runs/1"; fi ;;
  "issue create")
    for ((i = 1; i <= $#; i++)); do
      if [ "${!i}" = "--body-file" ]; then j=$((i + 1)); cp "${!j}" "${FAKE_GH_LOG}.body"; fi
    done
    echo "https://example.invalid/issues/7" ;;
  "issue comment")
    for ((i = 1; i <= $#; i++)); do
      if [ "${!i}" = "--body-file" ]; then j=$((i + 1)); cp "${!j}" "${FAKE_GH_LOG}.comment"; fi
    done ;;
esac
"""


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


@pytest.fixture
def night(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    shas = []
    for subject in ("baseline commit", "drop the reconciler tests", "add untested code"):
        _git(repo, "commit", "-q", "--allow-empty", "-m", subject)
        shas.append(_git(repo, "rev-parse", "HEAD"))
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "python.json").write_text(json.dumps({"suite": "python", "packages": {
        "apps/common": {"covered": 80, "lines": 100}, "apps/reconciler": {"covered": 50, "lines": 100}}}))
    floors = tmp_path / "floors.json"
    floors.write_text(json.dumps({"about": "a test", "packages": {
        "apps/common": {"suite": "python", "floor": 79}, "apps/reconciler": {"suite": "python", "floor": 70}}}))
    log = tmp_path / "gh.log"
    log.write_text("")

    def run(verdict: str, lines: str, **env: str) -> subprocess.CompletedProcess:
        (reports / "verdict.txt").write_text(lines)
        summary = tmp_path / "summary.md"
        summary.write_text("")
        proc = subprocess.run(["bash", str(REPORT)], capture_output=True, text=True, timeout=60, env={
            **os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "SWARM_NO_PROJECT": "1",
            "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "42", "GITHUB_STEP_SUMMARY": str(summary),
            "COVERAGE_VERDICT": verdict, "COVERAGE_REPORTS": str(reports), "COVERAGE_SHA": shas[-1],
            "COVERAGE_FLOORS": str(floors), "COVERAGE_GIT_DIR": str(repo), "FAKE_GH_LOG": str(log),
            "TMPDIR": str(tmp_path), **env})
        proc.summary = summary.read_text()  # type: ignore[attr-defined]
        return proc

    run.shas = shas  # type: ignore[attr-defined]
    run.log = log  # type: ignore[attr-defined]
    run.floors = floors  # type: ignore[attr-defined]
    return run


BELOW = ("BELOW  apps/reconciler: 50.00% (50/100 lines) is under its floor 70\n"
         "ok  apps/common: 80.00% (80/100 lines), floor 79\n")
GREEN = "ok  apps/common: 80.00% (80/100 lines), floor 79\nok  apps/reconciler: 71.00% (71/100 lines), floor 70\n"


def test_a_red_night_opens_one_issue_naming_the_package_and_the_commits(night):
    """MUTATION: no marker, a title that is not a fact, or no commit list."""
    proc = night("below", BELOW, FAKE_LAST_GREEN=night.shas[0])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = night.log.read_text()
    assert "issue create" in calls and "--label bug" in calls, calls
    assert "--title apps/reconciler line coverage is under its floor on main" in calls, calls
    body = Path(f"{night.log}.body").read_text()
    assert body.startswith(MARKER), body[:200]
    assert "BELOW  apps/reconciler" in body and "apps/common" not in body.split("### Identifiers")[1], body
    assert "drop the reconciler tests" in body and "add untested code" in body, body
    assert "baseline commit" not in body, body
    assert "| apps/reconciler | python | 50.00% (50/100) | 70 | BELOW |" in proc.summary, proc.summary


def test_a_red_night_with_an_open_issue_comments_and_never_opens_another(night):
    """MUTATION: create on every red night."""
    proc = night("below", BELOW, FAKE_OPEN_ISSUE="12", FAKE_LAST_GREEN=night.shas[0])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = night.log.read_text()
    assert "issue create" not in calls, calls
    assert "issue edit 12" in calls and "issue comment 12" in calls, calls
    assert MARKER in calls, "the open issue is found by its marker"
    assert "drop the reconciler tests" in Path(f"{night.log}.comment").read_text()


def test_a_green_night_comments_on_the_issue_and_closes_it(night):
    """MUTATION: leave it open, or close without saying why."""
    proc = night("green", GREEN, FAKE_OPEN_ISSUE="12")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = night.log.read_text()
    assert "issue comment 12" in calls and "issue close 12" in calls, calls
    assert "Green" in Path(f"{night.log}.comment").read_text()


def test_a_green_night_with_no_issue_files_nothing(night):
    proc = night("green", GREEN)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = night.log.read_text()
    assert "issue create" not in calls and "issue close" not in calls, calls


def test_a_night_with_no_floors_only_reports_and_prints_the_fill_command(night):
    """MUTATION: file an issue while the floors are unset, or no command."""
    night.floors.write_text(json.dumps({"about": "a test", "packages": {
        "apps/common": {"suite": "python", "floor": None, "note": "not yet measured"}}}))
    proc = night("unset", "UNMEASURED  apps/common: 80.00% (80/100 lines); set its floor to 79\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = night.log.read_text()
    assert "issue create" not in calls and "issue close" not in calls and "issue comment" not in calls, calls
    assert "gh run download 42 --repo example/repo -n coverage-reports" in proc.summary, proc.summary
    assert "coverage_floors.py fill --floors coverage-floors.json" in proc.summary, proc.summary


def test_an_incomplete_measurement_files_and_closes_nothing(night):
    """A failed shard is not fallen coverage. MUTATION: close the issue on it."""
    proc = night("", "coverage-floors.sh: 7 of 10 shards left coverage data\n", FAKE_OPEN_ISSUE="12")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = night.log.read_text()
    assert "issue" not in calls, calls
    assert "Not a measurement" in proc.summary, proc.summary


WORKFLOW = ROOT / ".github" / "workflows" / "coverage.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _on(doc: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    return doc.get("on", doc.get(True))


def _uses(doc: dict) -> list[str]:
    return [step["uses"] for job in doc["jobs"].values() for step in job.get("steps", []) if "uses" in step]


def test_it_runs_on_a_schedule_and_by_hand_only():
    """MUTATION: add pull_request, push, merge_group or workflow_run."""
    triggers = _on(_load(WORKFLOW))
    assert set(triggers) == {"schedule", "workflow_dispatch"}, sorted(triggers)
    assert "pull_request" not in triggers and "pull_request_target" not in triggers
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert len(crons) == 1, crons
    minute, hour, *rest = crons[0].split()
    assert rest == ["*", "*", "*"], crons  # once a day
    assert minute != "0", "the top of the hour is when GitHub's scheduler drops runs"


def test_every_job_runs_on_main_only():
    """A dispatch from another branch would close main's issue on its own
    green. MUTATION: drop plan's `if:`, or a job that does not need plan."""
    jobs = _load(WORKFLOW)["jobs"]
    assert jobs["plan"]["if"] == "github.ref == 'refs/heads/main'", jobs["plan"].get("if")

    def reaches_plan(name: str) -> bool:
        needs = jobs[name].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        return "plan" in needs or any(reaches_plan(n) for n in needs)

    for name in set(jobs) - {"plan"}:
        assert reaches_plan(name), name


def test_permissions_are_explicit_and_only_report_writes():
    """MUTATION: write-all, a missing top-level block, or issues: write on a
    job that runs the suites."""
    doc = _load(WORKFLOW)
    assert doc["permissions"] == {"contents": "read"}, doc.get("permissions")
    writers = {name for name, job in doc["jobs"].items()
               if any(v == "write" for v in (job.get("permissions") or {}).values())}
    assert writers == {"report"}, writers
    assert doc["jobs"]["report"]["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    for name, job in doc["jobs"].items():
        assert "id-token" not in (job.get("permissions") or {}), name


def test_every_action_is_pinned_to_the_commit_the_other_workflows_pin():
    """The SHAs application.yml pins; download-artifact, which application.yml
    does not use, at publish-release.yml's. MUTATION: a tag, or a SHA other
    than the one main already reviewed."""
    main_pins: dict[str, set[str]] = {}
    others = [p for p in sorted(WORKFLOW.parent.glob("*.yml")) if p != WORKFLOW]
    for uses in (u for path in others for u in _uses(_load(path))):
        match = PIN.match(uses.split("#")[0].strip())
        if match:
            main_pins.setdefault(match.group(1), set()).add(match.group(2))
    ours = _uses(_load(WORKFLOW))
    assert ours
    for uses in ours:
        match = PIN.match(uses.strip())
        assert match, f"{uses} is not pinned to a commit SHA"
        assert match.group(2) in main_pins.get(match.group(1), set()), (uses, main_pins.get(match.group(1)))
    for line in WORKFLOW.read_text().splitlines():
        if "uses:" in line:
            assert re.search(r"@[0-9a-f]{40} # v\d", line), line
