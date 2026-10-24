"""Every job that reads the merge App's key or the acceptance sandbox's token
declares the main-only environment `repo-secrets`.

REPO-SECRETS-ENV (part of #2029; BGD's decision, 2026-10-23). A
repository-level Actions secret can be read by a workflow on ANY pushed branch,
before any review or merge hold: push a branch whose workflow echoes
`secrets.MERGE_APP_PRIVATE_KEY` and it runs. `MERGE_APP_PRIVATE_KEY` mints a
token that can merge pull requests; `SWARM_SANDBOX_GITHUB_TOKEN` writes the
acceptance sandbox. Both move into the environment `repo-secrets`, whose
deployment branches are limited to `main`, so only a job running on main's ref
can read them. That holds only while every job that reads either secret names
the environment: a job that does not reads the repository-level copy while it
exists, and reads an empty string once it is deleted (the merge App's presence
check, `HAS_MERGE_APP_KEY`, would then refuse every merge).

Every file in .github/workflows/ is scanned, not a list: a workflow added later
that reads either secret is held to the same rule. Comments are not reads --
the scan reads the parsed YAML, which carries none.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"

ENVIRONMENT = "repo-secrets"
GUARDED_SECRETS = ("MERGE_APP_PRIVATE_KEY", "SWARM_SANDBOX_GITHUB_TOKEN")

# The jobs that read either secret on 2026-10-23, found with `git grep`. The
# scan must find at least these: a scan that finds none passes vacuously.
KNOWN_READERS = {
    ("auto-merge.yml", "enable"),
    ("auto-merge.yml", "disable"),
    ("auto-merge.yml", "ready-on-green"),
    ("merge-train.yml", "tick"),
    ("runner-fallback.yml", "judge"),
    ("accept.yml", "sandbox"),
    ("accept.yml", "sandbox-results"),
}


def _reads_secret(text: str) -> bool:
    """`secrets.NAME` or `secrets['NAME']`, the two ways an expression reads one."""
    for name in GUARDED_SECRETS:
        if re.search(rf"\bsecrets\s*(\.\s*{name}\b|\[\s*['\"]{name}['\"]\s*\])", text):
            return True
    return False


def _environment_name(job: dict) -> str | None:
    env = job.get("environment")
    if isinstance(env, dict):
        return env.get("name")
    return env


def _load(text: str) -> dict:
    data = yaml.safe_load(text) or {}
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _readers(workflow: dict) -> dict[str, dict]:
    """Every job of `workflow` that reads a guarded secret, by id."""
    return {
        job_id: job
        for job_id, job in (workflow.get("jobs") or {}).items()
        if _reads_secret(yaml.safe_dump(job))
    }


def _undeclared(workflow: dict) -> list[str]:
    """The readers that do not declare the environment."""
    return sorted(job_id for job_id, job in _readers(workflow).items() if _environment_name(job) != ENVIRONMENT)


def _workflow_files() -> list[Path]:
    return sorted(p for p in WORKFLOWS.iterdir() if p.suffix in (".yml", ".yaml"))


def _all_readers() -> dict[tuple[str, str], dict]:
    found = {}
    for path in _workflow_files():
        for job_id, job in _readers(_load(path.read_text())).items():
            found[(path.name, job_id)] = job
    return found


def test_the_scan_finds_every_known_reader():
    # MUTATION: a regex that misses `secrets.NAME` (or a scan of no files)
    # finds nothing, and every check below would pass over an empty set: red.
    assert len(_workflow_files()) > 10
    found = set(_all_readers())
    assert KNOWN_READERS <= found, f"the scan missed {sorted(KNOWN_READERS - found)}"


def test_every_job_reading_a_guarded_secret_declares_the_environment():
    offenders = [
        f"{path.name}: {job_id}" for path in _workflow_files() for job_id in _undeclared(_load(path.read_text()))
    ]
    # MUTATION: delete `environment:` from any job that reads either secret
    # (auto-merge.yml `enable`, accept.yml `sandbox`, ...) and this is red.
    assert offenders == [], f"jobs read a repo-secrets secret without `environment: {ENVIRONMENT}`: {offenders}"


def test_no_workflow_reads_a_guarded_secret_outside_a_job():
    # A workflow-level `env:` evaluates the secret for every job in the file,
    # none of which can be held to the environment by it.
    for path in _workflow_files():
        workflow = _load(path.read_text())
        outside = {k: v for k, v in workflow.items() if k != "jobs"}
        # MUTATION: move a job's `env:` reading the key up to workflow level: red.
        assert not _reads_secret(yaml.safe_dump(outside)), f"{path.name} reads a guarded secret outside a job"


def test_the_environment_creates_no_deployment_record():
    # `deployment: false` (GitHub Actions, March 2026) gives the job the
    # environment's secrets without a deployment record, so no pull request's
    # timeline reads "deployed to repo-secrets" for a merge-bot run.
    for (name, job_id), job in sorted(_all_readers().items()):
        env = job.get("environment")
        # MUTATION: the plain `environment: repo-secrets` form is red here.
        assert isinstance(env, dict) and env.get("deployment") is False, f"{name}: {job_id} environment {env!r}"


PLANTED = """
on:
  workflow_dispatch:
jobs:
  declared:
    runs-on: ubuntu-24.04
    environment:
      name: repo-secrets
      deployment: false
    steps:
      - run: echo
        env:
          KEY: ${{ secrets.MERGE_APP_PRIVATE_KEY }}
  plain-form:
    runs-on: ubuntu-24.04
    environment: repo-secrets
    steps:
      - run: echo
        env:
          KEY: ${{ secrets.SWARM_SANDBOX_GITHUB_TOKEN }}
  undeclared:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/create-github-app-token@v2
        with:
          private-key: ${{ secrets.MERGE_APP_PRIVATE_KEY }}
  presence-check:
    runs-on: ubuntu-24.04
    steps:
      - run: echo
        env:
          HAS: ${{ secrets.MERGE_APP_PRIVATE_KEY != '' }}
  bracket-form:
    runs-on: ubuntu-24.04
    steps:
      - run: echo
        env:
          T: ${{ secrets['SWARM_SANDBOX_GITHUB_TOKEN'] }}
  wrong-environment:
    runs-on: ubuntu-24.04
    environment: production
    steps:
      - run: echo
        env:
          T: ${{ secrets.SWARM_SANDBOX_GITHUB_TOKEN }}
  other-secret:
    runs-on: ubuntu-24.04
    steps:
      # secrets.MERGE_APP_PRIVATE_KEY in a comment is not a read.
      - run: echo
        env:
          T: ${{ secrets.SOME_OTHER_SECRET }}
"""


def test_a_planted_undeclared_reader_is_caught():
    workflow = _load(PLANTED)
    # MUTATION: a check that accepts any `environment:` (or none) lets
    # `undeclared`, `presence-check` and `wrong-environment` through: red.
    assert _undeclared(workflow) == ["bracket-form", "presence-check", "undeclared", "wrong-environment"]
    # MUTATION: a scan that counts every `secrets.` read, or a comment, flags
    # `other-secret`: red.
    assert set(_readers(workflow)) == {
        "declared",
        "plain-form",
        "undeclared",
        "presence-check",
        "bracket-form",
        "wrong-environment",
    }


@pytest.mark.parametrize("name", GUARDED_SECRETS)
def test_both_secrets_are_still_read_somewhere(name):
    # MUTATION: renaming a secret in the workflows (so this test guards a name
    # nothing reads any more) without updating GUARDED_SECRETS is red.
    texts = [yaml.safe_dump(job) for job in _all_readers().values()]
    assert any(f"secrets.{name}" in t for t in texts)
