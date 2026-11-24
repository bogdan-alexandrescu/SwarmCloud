"""CI plans with the deployment's own tfvars, fetched, never the checkout's.

docs/design/generic-repository.md, sections 2.2-2.3: the real
`<env>.tfvars` are deployment values, kept under config/<env>/ in the state
bucket, and the tracked copies are on their way out of the checkout. Every job that plans -- release-apply (the
release and hotfix lanes) and terraform.yml's `plan` -- fetches them through
scripts/lib/resolve.sh (config_fetch), the read a laptop's `make tf-plan`
makes, into the runner's temporary directory outside the checkout, and plans
with that file.

The fetch step (its published run block, ci_run_blocks) runs here for real,
against a local configuration directory standing in for the bucket
(SWARM_DEPLOY_CONFIG_DIR, which config_fetch reads in place of gs://), with
the placeholder project.
"""

from __future__ import annotations

import importlib.util
import os
import secrets
import subprocess
from pathlib import Path

import pytest

from .ci_run_blocks import run_block

REPO = Path(__file__).resolve().parents[3]
FETCH = "fetch this environment's tfvars"

_spec = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(placeholders)


def _fetch_step(where: str) -> str:
    if where == "release-apply":
        return run_block(".github/actions/release-apply/action.yml", None, FETCH)
    return run_block("terraform.yml", "plan", FETCH)


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
    proc = _run(_fetch_step(where), runner["env"])
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
    proc = _run(_fetch_step(where), runner["env"])
    assert proc.returncode != 0
    assert "dev.tfvars does not exist" in proc.stderr
    assert runner["github_env"].read_text(encoding="utf-8") == ""
