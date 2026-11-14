"""CI plans with the deployment's own tfvars, fetched, never the checkout's.

docs/design/generic-repository.md, sections 2.2-2.3: the real
`<env>.tfvars` are deployment values, kept under config/<env>/ in the state
bucket, and the tracked copies are on their way out of the checkout. Every job that plans -- release-apply (the
release and hotfix lanes) and terraform.yml's `plan` -- fetches them through
scripts/lib/resolve.sh (config_fetch), the read a laptop's `make tf-plan`
makes, into the runner's temporary directory outside the checkout, and plans
with that file.

The fetch step runs here for real, against a local configuration directory
standing in for the bucket (SWARM_DEPLOY_CONFIG_DIR, which config_fetch reads
in place of gs://), with the placeholder project.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
ACTION = REPO / ".github" / "actions" / "release-apply" / "action.yml"
TERRAFORM = REPO / ".github" / "workflows" / "terraform.yml"
FETCH = "fetch this environment's tfvars"

_spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(placeholders)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(where: str) -> list[dict]:
    if where == "release-apply":
        return _load(ACTION)["runs"]["steps"]
    return _load(TERRAFORM)["jobs"]["plan"]["steps"]


def _named(steps: list[dict], name: str) -> dict:
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _all_text() -> str:
    paths = sorted((REPO / ".github").rglob("*.yml"))
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def test_no_workflow_reads_tfvars_from_the_checkout():
    """MUTATION: put back `-var-file="../environments/${ENVIRONMENT}/..."` and
    this goes red: the file is not in a fresh checkout any more."""
    text = _all_text()
    assert "../environments/" not in text
    assert "environments/${ENVIRONMENT}/${ENVIRONMENT}.tfvars" not in text


@pytest.mark.parametrize("where", ["release-apply", "terraform.yml"])
def test_the_plan_uses_the_fetched_file_and_fetches_it_first(where: str):
    steps = _steps(where)
    names = [s.get("name") for s in steps]
    plan = next(s for s in steps if s.get("name") in ("plan", "terraform plan"))
    assert FETCH in names, names
    assert names.index(FETCH) < names.index(plan["name"])
    assert '-var-file="${SWARM_TFVARS_FILE}"' in plan["run"]
    fetch = _named(steps, FETCH)
    assert 'XDG_CACHE_HOME="${RUNNER_TEMP}" ./scripts/lib/resolve.sh tfvars "${ENVIRONMENT}"' in fetch["run"]


def test_every_job_that_plans_names_the_state_bucket_as_a_secret():
    """The configuration root is gs://<state bucket>/config, so resolve.sh
    needs the bucket; it is a deployment identifier, read as a secret. No
    zone is passed: common.sh does not require one and nothing reads it."""
    plan_env = _load(TERRAFORM)["jobs"]["plan"]["env"]
    assert plan_env["TF_STATE_BUCKET"] == "${{ secrets.TF_STATE_BUCKET }}"
    planners = 0
    for workflow in ("release.yml", "hotfix.yml"):
        for name, job in _load(REPO / ".github" / "workflows" / workflow)["jobs"].items():
            if any("release-apply" in str(s.get("uses", "")) for s in job.get("steps", [])):
                planners += 1
                assert job["env"]["TF_STATE_BUCKET"] == "${{ secrets.TF_STATE_BUCKET }}", f"{workflow}: {name}"
    assert planners >= 2, planners


@pytest.fixture()
def runner(tmp_path: Path) -> dict:
    """A runner's temporary directory, GITHUB_ENV and the deployment's
    configuration directory, holding a dev.tfvars no tracked file holds."""
    config = tmp_path / "config"
    (config / "dev").mkdir(parents=True, mode=0o700)
    config.chmod(0o700)
    marker = f'name_prefix = "swarm-{secrets.token_hex(4)}"\n'
    tfvars = config / "dev" / "dev.tfvars"
    tfvars.write_text(marker, encoding="utf-8")
    tfvars.chmod(0o600)
    temp = tmp_path / "runner-temp"
    temp.mkdir(mode=0o700)
    github_env = tmp_path / "github_env"
    github_env.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "PROJECT_ID": placeholders.PROJECT,
        "REGION": placeholders.REGION,
        # No ZONE: no job that plans sets one, and the fetch needs none.
        "ENVIRONMENT": "dev",
        "RUNNER_TEMP": str(temp),
        "GITHUB_ENV": str(github_env),
        "SWARM_DEPLOY_CONFIG_DIR": str(config),
        # No operator .env: a runner has none.
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
    }
    return {"env": env, "temp": temp, "github_env": github_env, "marker": marker, "tfvars": tfvars}


def _run(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env=env, cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.mark.parametrize("where", ["release-apply", "terraform.yml"])
def test_the_fetch_step_passes_a_file_outside_the_checkout(runner: dict, where: str):
    proc = _run(_named(_steps(where), FETCH)["run"], runner["env"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = runner["github_env"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].startswith("SWARM_TFVARS_FILE="), lines
    fetched = Path(lines[0].partition("=")[2])
    assert fetched.read_text(encoding="utf-8") == runner["marker"]
    assert fetched.is_relative_to(runner["temp"]), fetched
    assert not fetched.resolve().is_relative_to(REPO), "the fetched tfvars landed in the checkout"


@pytest.mark.parametrize("where", ["release-apply", "terraform.yml"])
def test_the_fetch_step_fails_when_the_environment_has_no_tfvars(runner: dict, where: str):
    """No object, no plan: never an empty GITHUB_ENV line, never a tracked
    fallback."""
    runner["tfvars"].unlink()
    proc = _run(_named(_steps(where), FETCH)["run"], runner["env"])
    assert proc.returncode != 0
    assert "dev.tfvars does not exist" in proc.stderr
    assert runner["github_env"].read_text(encoding="utf-8") == ""
