"""Where CI-FOLD (#1799) put each sub-minute check: the job it is folded
into, per workflow, and the old job ids kept as name-keepers.

A module of its own, not test_ci_fold.py's, because tests that import
test_ci_gate.py read it too (test_ci_fold.py imports that chain), and
because every test that reads a folded check's steps -- the actionlint file
list, the lock check -- reads them through `check_steps`, so
the layout is stated once. test_ci_fold.py holds the workflows to it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
PATCH_NOTE = ("-- the workflow change of CI-FOLD (#1799) is not applied: it ships as "
              "workflow-lines.patch, which the operator pushes by hand")

#: Per workflow: the job the sub-minute checks are folded into, and each old
#: job id kept as a name-keeper with the check name it reports.
FOLDED: dict[str, tuple[str, dict[str, str]]] = {
    "application.yml": ("quick-checks", {
        "workflows": "release workflow wiring (actionlint)",
        "ruff": "ruff check",
        "manifests": "kubernetes manifests",
        "python-checks": "lock / contract / format / bridge install",
    }),
    # `trivy (repo)` (`filesystem`) is not folded: it uploads SARIF, so it is
    # the one job that holds security-events: write, and the folded job runs
    # repository scripts (owner decision 2026-10-21 on PR 1813).
    "security.yml": ("checks", {
        "secrets": "secret scan",
        "policy": "platform policy assertions",
    }),
    "terraform.yml": ("offline", {
        "validate": "fmt / validate / tflint",
        "test": "terraform test",
    }),
}
#: The folded job of each workflow, by file name.
FOLDED_HOSTS = {name: host for name, (host, _) in FOLDED.items()}


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def host_job(name: str) -> dict:
    host = FOLDED_HOSTS[name]
    jobs = _jobs(name)
    assert host in jobs, f"{name} has no `{host}` job {PATCH_NOTE}"
    return jobs[host]


def check_steps(name: str, job_id: str) -> list[dict]:
    """The steps that run old job `job_id`'s check, in its folded job.

    Other tests that read a folded check's steps (the actionlint file list,
    the lock check) read them through this, so the layout is
    stated once."""
    steps = [s for s in host_job(name).get("steps") or [] if str(s.get("id", "")).startswith(f"{job_id}-")]
    assert steps, f"{name}: no `{job_id}-*` step in `{FOLDED_HOSTS[name]}` {PATCH_NOTE}"
    return steps
