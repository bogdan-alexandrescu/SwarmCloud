"""A release whose full apply fails still ships its Cloud Run image updates.

Owner, 2026-11-09: one failing resource must not block a release. That day a
dev release's held plan failed to create one unrelated machine (no capacity in
its zone), the one apply failed, and no service update shipped for about ten
hours. `scripts/lib/release-apply.sh` applies the saved plan as before and,
when that fails, re-plans with `-target` on the Cloud Run services and jobs the
failed plan updated, judges that plan, applies it only when it changes nothing
else, and reports both outcomes with the release still red.

Terraform here is a fake on PATH: it fails or succeeds the full apply, shows
the plans each case writes, and records every call, so these run with no cloud
credentials and no Terraform.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "release-apply.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

FAILED_MACHINE = 'google_compute_instance.speech["gpu"]'
API = 'module.cloud_run["swarm-api"].google_cloud_run_v2_service.this'
WORKER = 'module.cloud_run_jobs.google_cloud_run_v2_job.worker["swarm-worker"]'

# Terraform's error block, as `apply -no-color` writes it to a CI log.
APPLY_ERROR = f"""google_compute_instance.speech["gpu"]: Creating...
╷
│ Error: Error waiting for instance to create: The zone does not have enough resources available to fulfill the request.
│
│   with {FAILED_MACHINE},
│   on speech.tf line 12, in resource "google_compute_instance" "speech":
│   12: resource "google_compute_instance" "speech" {{
│
╵
"""

FAKE_TERRAFORM = r'''#!/usr/bin/env python3
import json, os, shutil, sys
state = os.environ["FAKE_TF_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
cmd = [a for a in args if not a.startswith("-chdir=")][0]
last = args[-1]
if cmd == "apply":
    if last.endswith("services.tfplan"):
        sys.exit(int(open(os.path.join(state, "services_apply_rc")).read()))
    sys.stdout.write(open(os.path.join(state, "apply.log")).read())
    sys.exit(int(open(os.path.join(state, "full_apply_rc")).read()))
if cmd == "show":
    name = "services.json" if last.endswith("services.tfplan") else "full.json"
    sys.stdout.write(open(os.path.join(state, name)).read())
    sys.exit(0)
if cmd == "plan":
    for a in args:
        if a.startswith("-var-file="):
            shutil.copy(a.split("=", 1)[1], os.path.join(state, "vars-seen.json"))
            mode = os.stat(a.split("=", 1)[1]).st_mode & 0o777
            open(os.path.join(state, "vars-mode"), "w").write(oct(mode))
        if a.startswith("-out="):
            open(a.split("=", 1)[1], "w").write("saved plan")
    sys.exit(0)
sys.exit(f"fake terraform: unexpected {args}")
'''


def _project() -> str:
    return os.environ["PROJECT_ID"]


def _change(address: str, rtype: str, actions: list[str], image: str = "sha256:new") -> dict:
    project = _project()
    before = {"name": address, "project": project, "labels": {"managed-by": "swarm-terraform"},
              "template": [{"containers": [{"image": "img@sha256:old"}]}]}
    after = copy.deepcopy(before)
    after["template"][0]["containers"][0]["image"] = f"img@{image}"
    if actions == ["create"]:
        before = None
    return {"address": address, "mode": "managed", "type": rtype, "name": "this",
            "provider_name": "registry.terraform.io/hashicorp/google",
            "change": {"actions": actions, "before": before, "after": after, "after_unknown": {}}}


def _plan(*changes: dict) -> dict:
    return {"format_version": "1.2", "terraform_version": "1.16.2",
            "variables": {"environment": {"value": "dev"},
                          "image_refs": {"value": {"swarm-api": "img@sha256:new"}}},
            "resource_changes": list(changes)}


def _full_plan() -> dict:
    return _plan(
        _change(API, "google_cloud_run_v2_service", ["update"]),
        _change(WORKER, "google_cloud_run_v2_job", ["update"]),
        _change(FAILED_MACHINE, "google_compute_instance", ["create"]),
        _change("google_service_account_iam_member.reader", "google_service_account_iam_member", ["create"]),
    )


def _services_plan() -> dict:
    return _plan(
        _change(API, "google_cloud_run_v2_service", ["update"]),
        _change(WORKER, "google_cloud_run_v2_job", ["update"]),
    )


class Release:
    def __init__(self, tmp_path: Path, *, full_rc: int = 1, full: dict | None = None,
                 services: dict | None = None, services_rc: int = 0) -> None:
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.root = tmp_path / "infra"
        self.root.mkdir()
        (self.root / "plan.tfplan").write_text("held plan")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "terraform"
        fake.write_text(FAKE_TERRAFORM)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        (self.state / "full_apply_rc").write_text(str(full_rc))
        (self.state / "services_apply_rc").write_text(str(services_rc))
        (self.state / "apply.log").write_text(APPLY_ERROR if full_rc else "Apply complete!\n")
        (self.state / "full.json").write_text(json.dumps(full or _full_plan()))
        (self.state / "services.json").write_text(json.dumps(services or _services_plan()))
        self.outputs = tmp_path / "github_output"
        self.summary = tmp_path / "summary.md"
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_TF_STATE": str(self.state), "TMPDIR": str(tmp_path)}

    def run(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(SCRIPT), "--chdir", str(self.root), "--plan", "plan.tfplan", "--lock-timeout", "180s",
             "--outputs", str(self.outputs), "--summary", str(self.summary)],
            capture_output=True, text=True, timeout=120, env=self.env)

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def applied(self) -> list[str]:
        return [c[-1] for c in self.calls() if "apply" in c]

    def outputs_map(self) -> dict[str, str]:
        text = self.outputs.read_text() if self.outputs.exists() else ""
        return dict(line.split("=", 1) for line in text.splitlines())


def test_a_full_apply_that_succeeds_applies_the_saved_plan_once_and_nothing_else(tmp_path: Path) -> None:
    release = Release(tmp_path, full_rc=0)
    proc = release.run()
    assert proc.returncode == 0, proc.stderr
    assert release.applied() == ["plan.tfplan"]
    assert not [c for c in release.calls() if "plan" in c]
    assert release.outputs_map() == {"outcome": "applied"}


def test_a_failed_full_apply_is_followed_by_a_targeted_apply_of_the_services(tmp_path: Path) -> None:
    release = Release(tmp_path)
    proc = release.run()
    # The release stays red.
    assert proc.returncode == 1, proc.stderr
    plans = [c for c in release.calls() if "plan" in c]
    assert len(plans) == 1
    targets = sorted(a.split("=", 1)[1] for a in plans[0] if a.startswith("-target="))
    assert targets == sorted([API, WORKER])
    applied = release.applied()
    assert applied[0] == "plan.tfplan"
    assert len(applied) == 2 and applied[1].endswith("services.tfplan")
    outputs = release.outputs_map()
    assert outputs["outcome"] == "services"
    assert outputs["failed_resources"] == FAILED_MACHINE
    report = proc.stderr + release.summary.read_text()
    assert FAILED_MACHINE in report and API in report and WORKER in report


def test_the_targeted_plan_uses_the_failed_plans_own_variables_in_a_private_file(tmp_path: Path) -> None:
    release = Release(tmp_path)
    release.run()
    assert json.loads((release.state / "vars-seen.json").read_text()) == {
        "environment": "dev", "image_refs": {"swarm-api": "img@sha256:new"}}
    assert (release.state / "vars-mode").read_text() == "0o600"
    # Its private directory is gone once it exits.
    assert not list(tmp_path.glob("release-apply.*"))


def test_a_targeted_plan_that_touches_anything_else_is_refused(tmp_path: Path) -> None:
    services = _services_plan()
    services["resource_changes"].append(_change(FAILED_MACHINE, "google_compute_instance", ["create"]))
    release = Release(tmp_path, services=services)
    proc = release.run()
    assert proc.returncode == 1
    assert release.applied() == ["plan.tfplan"], "the refused targeted plan must not be applied"
    assert release.outputs_map()["outcome"] == "failed"
    assert FAILED_MACHINE in proc.stderr and "not a Cloud Run service or job" in proc.stderr


@pytest.mark.parametrize("rtype", ["google_cloud_run_v2_service_iam_member", "google_project_iam_member"])
def test_a_targeted_plan_with_iam_in_it_is_refused(tmp_path: Path, rtype: str) -> None:
    services = _services_plan()
    services["resource_changes"].append(_change("x.iam", rtype, ["create"]))
    release = Release(tmp_path, services=services)
    proc = release.run()
    assert proc.returncode == 1
    assert release.applied() == ["plan.tfplan"]
    assert release.outputs_map()["outcome"] == "failed"


def test_a_service_replaced_rather_than_updated_is_refused(tmp_path: Path) -> None:
    services = _plan(_change(API, "google_cloud_run_v2_service", ["delete", "create"]))
    release = Release(tmp_path, services=services)
    proc = release.run()
    assert proc.returncode == 1
    assert release.applied() == ["plan.tfplan"]
    assert "not an in-place update" in proc.stderr


def test_a_service_value_the_failed_plan_did_not_set_is_refused(tmp_path: Path) -> None:
    services = _plan(_change(API, "google_cloud_run_v2_service", ["update"], image="sha256:other"))
    release = Release(tmp_path, services=services)
    proc = release.run()
    assert proc.returncode == 1
    assert release.applied() == ["plan.tfplan"]
    assert "values the failed plan did not" in proc.stderr


def test_a_service_the_failed_plan_did_not_update_is_refused(tmp_path: Path) -> None:
    other = 'module.cloud_run["swarm-ui"].google_cloud_run_v2_service.this'
    services = _plan(_change(other, "google_cloud_run_v2_service", ["update"]))
    release = Release(tmp_path, services=services)
    proc = release.run()
    assert proc.returncode == 1
    assert release.applied() == ["plan.tfplan"]
    assert "the failed plan did not update it" in proc.stderr


def test_a_failed_plan_with_no_service_updates_retries_nothing(tmp_path: Path) -> None:
    full = _plan(_change(FAILED_MACHINE, "google_compute_instance", ["create"]))
    release = Release(tmp_path, full=full)
    proc = release.run()
    assert proc.returncode == 1
    assert not [c for c in release.calls() if "plan" in c]
    assert release.applied() == ["plan.tfplan"]
    assert release.outputs_map()["outcome"] == "failed"


def test_services_already_updated_by_the_failed_apply_are_reported_not_reapplied(tmp_path: Path) -> None:
    services = _services_plan()
    for change in services["resource_changes"]:
        change["change"]["actions"] = ["no-op"]
    release = Release(tmp_path, services=services)
    proc = release.run()
    assert proc.returncode == 1
    assert release.applied() == ["plan.tfplan"]
    assert release.outputs_map()["outcome"] == "services"


def test_a_failed_targeted_apply_is_reported_as_failed(tmp_path: Path) -> None:
    release = Release(tmp_path, services_rc=1)
    proc = release.run()
    assert proc.returncode == 1
    assert len(release.applied()) == 2
    assert release.outputs_map()["outcome"] == "failed"
