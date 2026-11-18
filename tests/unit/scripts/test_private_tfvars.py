"""The plans that deploy pass the deployment's private values: the never-touch list and the alert addresses.

THE DEFECT. terraform/infra renders swarm-api's SWARM_SHARED_DENY_LIST and
SWARM_SHARED_DENY_LIST_SET from `shared_deny_list`, and the alert policies'
notification channels from `alert_emails`. The release (and main's plan) read
only the tracked <env>.tfvars and the image refs, and the tracked file can hold
neither (this repository names no real resource and no person). So the list was
null on every release: the deployed marker read "0" and swarm-api refused every
issue run touching infrastructure with DENY_LIST_UNSET, while the
shared-project guard in the same job read the reviewed
config/<env>/deny-list.json without trouble; and the alerts notified nobody.

WHAT IS UNDER TEST is the shipped script and the shipped plan steps:

  * scripts/lib/private-tfvars.sh writes the reviewed list's entries, unchanged,
    as shared_deny_list, adds alert_emails from config/<env>/private.tfvars.json
    when it exists, and prints no value of either;
  * the private file may set only the allowed keys, so it can never quietly
    override a tracked setting;
  * it stops and writes nothing when the list cannot be read, so a plan never
    goes out with no list (that would turn the marker back to "0");
  * the release's plan (release-apply, which release.yml and hotfix.yml both
    run) and main's plan (terraform.yml) each write that file before planning
    and pass it as a -var-file.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "private-tfvars.sh"
EXAMPLE = REPO / "scripts" / "lib" / "deny-list.example.json"
RELEASE_APPLY = REPO / ".github" / "actions" / "release-apply" / "action.yml"
TERRAFORM_WORKFLOW = REPO / ".github" / "workflows" / "terraform.yml"
ENTRIES = json.loads(EXAMPLE.read_text())["entries"]
#: Invented addresses on a reserved domain: never a person's.
ADDRESSES = ["alerts-a@example.com", "alerts-b@example.com"]


def _config(tmp_path: Path, *, deny_list: bool = True, private: object | None = None) -> dict[str, str]:
    """A child env whose configuration directory is the test's own (the placeholder
    deployment leaves a directory a test names alone), with the example list
    written for this process's project and, when given, a private.tfvars.json."""
    config = tmp_path / "config"
    (config / "dev").mkdir(parents=True)
    config.chmod(0o700)
    (config / "dev").chmod(0o700)
    if deny_list:
        body = json.loads(EXAMPLE.read_text())
        body["project"] = os.environ["PROJECT_ID"]
        path = config / "dev" / "deny-list.json"
        path.write_text(json.dumps(body))
        path.chmod(0o600)
    if private is not None:
        path = config / "dev" / "private.tfvars.json"
        path.write_text(json.dumps(private))
        path.chmod(0o600)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    env = os.environ.copy()
    env.update({"SWARM_DEPLOY_CONFIG_DIR": str(config), "XDG_CACHE_HOME": str(cache), "ENVIRONMENT": "dev"})
    return env


def _run(out: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--out", str(out)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _printed_values(result: subprocess.CompletedProcess[str]) -> list[str]:
    printed = result.stdout + result.stderr
    return [v for v in ENTRIES + ADDRESSES if v in printed]


def test_it_writes_the_reviewed_list_unchanged_and_no_alert_without_a_private_file(tmp_path: Path) -> None:
    out = tmp_path / "build" / "private-dev.tfvars.json"

    result = _run(out, _config(tmp_path))

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == {"shared_deny_list": ENTRIES}
    assert not _printed_values(result)
    assert f"{len(ENTRIES)} never-touch entries" in result.stderr


def test_it_adds_the_private_alert_addresses_and_prints_none(tmp_path: Path) -> None:
    out = tmp_path / "private-dev.tfvars.json"

    result = _run(out, _config(tmp_path, private={"alert_emails": ADDRESSES}))

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == {"shared_deny_list": ENTRIES, "alert_emails": ADDRESSES}
    assert not _printed_values(result)
    assert "2 alert address(es)" in result.stderr


@pytest.mark.parametrize(
    "private",
    [
        {"alert_emails": ADDRESSES, "create_alerts": False},
        {"shared_deny_list": []},
        {"alert_emails": "alerts-a@example.com"},
        ["alert_emails"],
    ],
    ids=["a tracked key", "the never-touch list", "addresses not a list", "not an object"],
)
def test_a_private_file_setting_anything_else_refuses_and_writes_nothing(tmp_path: Path, private: object) -> None:
    out = tmp_path / "private-dev.tfvars.json"

    result = _run(out, _config(tmp_path, private=private))

    assert result.returncode != 0
    assert "private.tfvars.json refused" in result.stderr
    assert not out.exists()
    assert not _printed_values(result)


def test_it_stops_and_writes_nothing_when_the_list_cannot_be_read(tmp_path: Path) -> None:
    out = tmp_path / "private-dev.tfvars.json"

    result = _run(out, _config(tmp_path, deny_list=False, private={"alert_emails": ADDRESSES}))

    assert result.returncode != 0
    assert "never-touch list" in result.stderr
    assert not out.exists()
    assert not list(tmp_path.glob("private-dev.tfvars.json.part.*"))


def _plan_runs(text: str) -> list[str]:
    """Every shell block that runs `terraform ... plan` with var files."""
    blocks: list[str] = []
    current: list[str] | None = None
    indent = 0
    for line in text.splitlines():
        stripped = line.strip()
        if current is not None:
            if stripped and (len(line) - len(line.lstrip())) < indent:
                blocks.append("\n".join(current))
                current = None
            else:
                current.append(line)
                continue
        if stripped.startswith("run: |"):
            current = []
            indent = len(line) - len(line.lstrip()) + 1
    if current is not None:
        blocks.append("\n".join(current))
    return [b for b in blocks if "terraform -chdir" in b and " plan " in b and "-var-file=" in b]


@pytest.mark.parametrize("path", [RELEASE_APPLY, TERRAFORM_WORKFLOW], ids=["release-apply", "terraform.yml"])
def test_every_deploying_plan_passes_the_private_values_it_just_wrote(path: Path) -> None:
    plans = _plan_runs(path.read_text())
    assert plans, f"{path.relative_to(REPO)} has no terraform plan step"
    for block in plans:
        lines = [ln.strip() for ln in block.splitlines()]
        writes = [i for i, ln in enumerate(lines) if ln.startswith("./scripts/lib/private-tfvars.sh --out ")]
        plan_at = next(i for i, ln in enumerate(lines) if ln.startswith("terraform -chdir") and " plan " in ln)
        assert writes and writes[0] < plan_at, f"{path.relative_to(REPO)}: the private values are not written before the plan"
        written = lines[writes[0]].split("--out ", 1)[1].strip().strip('"')
        passed = [ln.rstrip(" \\") for ln in lines[plan_at:] if ln.startswith("-var-file=")]
        assert f'-var-file="{written}"' in passed, f"{path.relative_to(REPO)}: the plan does not pass {written}"
        assert passed[-1] == f'-var-file="{written}"', (
            f"{path.relative_to(REPO)}: the private values must be the last var file, so a tracked value never "
            "overrides them"
        )
