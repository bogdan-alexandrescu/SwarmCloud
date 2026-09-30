"""The release's `acceptance (dev)` job, and the suite's own wiring.

Owner decision, 2026-09-29: after every dev deploy, scripts/acceptance/run.sh
dispatches real tasks and asserts what they produced. These pin the parts a
reviewer cannot see running:

* the job runs after `deploy` (which runs smoke) and only when it succeeded,
  on a push and a dev dispatch, and NEVER on prod -- it spends subscription
  quota and opens real pull requests, which test_release_prod_gate.py's
  DEV_ONLY_JOBS exemption relies on;
* it runs every group through verify-remote.sh, the path smoke's credentials
  take, and every group it names has a wrapper that verify-remote.sh can
  address (`scripts/<target>.sh`) and that run.sh knows;
* a partial re-run of prod that restarts it still goes red (stale-approval
  needs it);
* every acceptance script is a script this repository accepts: `set -euo
  pipefail` first, executable, and shellcheck-clean where shellcheck exists.
  CI's shell job checks scripts/acceptance/ as well since #358; this holds
  the entry scripts to it in the unit job too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_release_reuses_ci_images import _code, _evaluate, _needs, _workflow

ROOT = Path(__file__).resolve().parents[3]
ACCEPTANCE = ROOT / "scripts" / "acceptance"
GROUPS = ("mock", "generic", "claude-code", "workflow", "browser")
#: What is run rather than sourced. lib.sh and the group files are sourced by
#: run.sh and checked through it (-x); on their own they would report every
#: variable common.sh and lib.sh set.
ENTRY_SCRIPTS = ("run.sh", *(f"{g}.sh" for g in GROUPS), "github-cleanup.sh", "parsers.sh")


def _job() -> dict:
    jobs = _workflow("release.yml")["jobs"]
    assert "acceptance" in jobs, "release.yml has no `acceptance` job"
    return jobs["acceptance"]


def _starts(environment, deploy: str = "success") -> bool:
    condition = str(_job()["if"])
    # The status function is modelled by test_release_prod_gate.py; here a
    # run that was not cancelled is the case that matters.
    assert "!cancelled()" in condition, "the acceptance job must stop on a cancel"
    condition = condition.replace("!cancelled() &&", "")
    return bool(
        _evaluate(
            condition,
            **{"needs.deploy.result": deploy, "github.event.inputs.environment": environment},
        )
    )


def test_the_job_is_named_for_dev_and_follows_deploy_and_smoke():
    job = _job()
    assert job["name"] == "acceptance (dev)"
    assert _needs(job) == ["deploy"]


@pytest.mark.parametrize("environment", [None, "dev"], ids=["push", "dev-dispatch"])
def test_it_runs_on_dev_only_after_deploy_succeeded(environment):
    assert _starts(environment, "success")
    for other in ("failure", "skipped", "cancelled"):
        assert not _starts(environment, other), f"it starts after deploy ended {other}"


def test_it_never_starts_on_prod():
    assert not _starts("prod", "success")


def test_it_runs_every_group_through_verify_remote():
    runs = [_code(step.get("run", "")) for step in _job()["steps"]]
    suite = [r for r in runs if "verify-remote.sh" in r]
    assert len(suite) == 1, "exactly one step runs the suite, through verify-remote.sh"
    targets = re.findall(r"acceptance/([\w-]+)", suite[0])
    assert tuple(targets) == GROUPS, f"the release runs {targets}, the suite has {GROUPS}"
    for target in targets:
        wrapper = ACCEPTANCE / f"{target}.sh"
        assert wrapper.is_file(), f"verify-remote.sh would run scripts/acceptance/{target}.sh, which does not exist"
        assert f"--only {target}" in wrapper.read_text()


def test_the_sweep_runs_after_the_suite_even_when_it_failed():
    steps = _job()["steps"]
    names = [s.get("name", "") for s in steps]
    sweep = next(i for i, s in enumerate(steps) if "github-cleanup.sh" in _code(s.get("run", "")))
    suite = next(i for i, s in enumerate(steps) if "verify-remote.sh" in _code(s.get("run", "")))
    assert sweep > suite, names
    assert "!cancelled()" in str(steps[sweep].get("if", "")), "the sweep must run after a failed suite"
    assert "github.token" in str(steps[sweep].get("env", {}).get("GITHUB_TOKEN", ""))


def test_a_partial_rerun_that_restarts_acceptance_still_reaches_the_loud_job():
    jobs = _workflow("release.yml")["jobs"]
    assert "acceptance" in _needs(jobs["stale-approval"])


def test_list_names_every_group_and_its_checks_without_touching_anything():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("API_", "SWARM_"))}
    env["NO_COLOR"] = "1"
    result = subprocess.run(
        ["bash", str(ACCEPTANCE / "run.sh"), "--list"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    listed = [line for line in result.stdout.splitlines() if line and not line.startswith(" ")]
    assert tuple(listed) == GROUPS
    checks = [line.strip() for line in result.stdout.splitlines() if line.startswith("  ")]
    for group in GROUPS:
        assert any(c.startswith(f"{group}: ") for c in checks), f"{group} lists no checks"
    assert len(checks) == len(set(checks)), "two checks share a name"


def test_an_unknown_group_is_refused():
    result = subprocess.run(
        ["bash", str(ACCEPTANCE / "run.sh"), "--only", "nonsense", "--list"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "unknown group" in result.stderr


def _scripts() -> list[Path]:
    return sorted(ACCEPTANCE.glob("*.sh")) + sorted((ACCEPTANCE / "groups").glob("*.sh"))


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_acceptance_script_starts_with_set_euo_pipefail(script):
    first = next(
        line.strip()
        for line in script.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert first == "set -euo pipefail", f"{script}: first effective line is {first!r}"


@pytest.mark.parametrize("script", [ACCEPTANCE / n for n in ENTRY_SCRIPTS], ids=lambda p: p.name)
def test_every_entry_script_is_executable(script):
    assert os.access(script, os.X_OK), f"{script} is not executable; verify-remote.sh runs it directly"


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_script_defaults_a_jq_argument_to_the_escaped_brace_literal(script):
    """`"${1:-{\\}}"` is what PR #358's first live run hit: inside double
    quotes, bash 3.2 (macOS, the version these scripts must support) does not
    strip the backslash, so the "default empty object" is the literal,
    invalid JSON text `{\\}` rather than `{}`. jq then refuses it with
    "invalid JSON text passed to --argjson", the caller's argument silently
    turns into nothing, and the API 422s with "Field required". Every
    default must instead come from a variable (`local empty='{}'` then
    `"${1:-$empty}"`) or an explicit `[ -n "$1" ] || set -- '{}'`.
    """
    text = script.read_text()
    assert ":-{\\" not in text, f"{script}: bash 3.2 will not strip the backslash in \"${{1:-{{\\}}}}\" -- use a variable default instead"


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_acceptance_script_is_syntactically_valid_bash(script):
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"{script}: bash -n failed:\n{result.stderr}"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed here")
def test_the_acceptance_scripts_are_shellcheck_clean():
    result = subprocess.run(
        # The ENTRY scripts, with -x: they source lib.sh, parsers.sh and every
        # group, so all of it is checked in the context it runs in. A group
        # file checked on its own would report every variable lib.sh sets.
        ["shellcheck", "-x", *[str(ACCEPTANCE / name) for name in ENTRY_SCRIPTS]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
