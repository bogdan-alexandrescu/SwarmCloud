"""The inputs the engine generates for every Terraform pass (DAYONE-ENGINE-3;
design §5.1, §5.4; plan R7, R8; lessons 5, 11, 28, 35; Q2)."""

from __future__ import annotations

import builtins
import inspect
import json
from pathlib import Path

import pytest

from swarm_install import terraform as tfmod
from swarm_install import tfvars
from swarm_install.api_client import ADMIN_ROLE, DELEGATION

from .test_state_store import FakeBucket
from .test_terraform_runner import PROJECT, FakeExec, guards, install_state, run

DIGEST = "sha256:" + "a" * 64


def _strings(value) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def test_generated_never_from_committed_tfvars(monkeypatch: pytest.MonkeyPatch) -> None:
    # Generation reads nothing from disk: not terraform.tfvars, not environments/.
    def no_files(*args, **kwargs):
        raise AssertionError(f"generating inputs opened {args[0] if args else kwargs}")

    monkeypatch.setattr(builtins, "open", no_files)
    monkeypatch.setattr(Path, "read_text", no_files)
    monkeypatch.setattr(Path, "read_bytes", no_files)
    state = install_state(image_digests={"swarm-api": DIGEST, "workspace-apply": DIGEST})
    boot = tfvars.bootstrap_inputs(state, pass_name="bootstrap-1", installer="admin@example.com")
    infra = tfvars.infra_inputs(state, pass_name="infra-2", installer="admin@example.com")
    monkeypatch.undo()

    assert boot["adopt_from_infra_states"] == [], "lesson 8: the first bootstrap adopts nothing"
    assert boot["ci_fix_service_account"] == "" and boot["infra_tenants_tfvars"] == ""
    assert boot["frontend_iap_members"] == ["user:admin@example.com"]
    assert boot["workspace_apply_image"] == (
        f"europe-west1-docker.pkg.dev/{PROJECT}/swarm/workspace-apply@{DIGEST}"
    ), "the workspace-apply image is the release's digest in the project's own registry"
    assert infra["tenants"] == {}, "design §5.6: installs declare no Terraform tenants"
    for value in _strings(boot) + _strings(infra):
        assert "saga" not in value, f"{value!r}: a generated input names the live deployment (lesson 5)"
    source = inspect.getsource(tfvars)
    assert "terraform.tfvars\"" not in source and "environments/" not in source.replace(
        "terraform/environments/`", "")


def test_no_dash_var_ever(tmp_path: Path) -> None:
    for arg in ("-var", "-var=project_id=x", "--var"):
        with pytest.raises(tfmod.TerraformRefused, match="lesson 11"):
            tfmod.refuse_var(["terraform", "plan", arg])
    exec_ = FakeExec()
    for pass_name in tfvars.PASSES:
        run(tmp_path / pass_name, pass_name, exec_, g=guards(deployer_exists=False))
    for argv, _ in exec_.calls:
        assert not any(a == "-var" or a.startswith(("-var=", "-var-file", "--var")) for a in argv), argv
    written = tmp_path / "infra-4" / "infra" / tfvars.INPUTS_FILE
    assert written.name.endswith(".auto.tfvars.json"), "Terraform loads the inputs itself; no flag names them"


def test_inputs_stored_in_state_bucket(tmp_path: Path) -> None:
    bucket = FakeBucket()
    inputs = tfvars.infra_inputs(install_state(), pass_name="infra-1", installer="admin@example.com")
    run(tmp_path, "infra-1", FakeExec(), bucket=bucket, g=guards(), inputs=inputs)
    stored = json.loads(bucket.objects["swarm-install/inputs/infra.auto.tfvars.json"][0])
    assert stored == inputs
    assert json.loads((tmp_path / "infra" / tfvars.INPUTS_FILE).read_text()) == inputs
    assert tfvars.stored(bucket, "infra") == inputs

    second = dict(inputs, workspace_gate=True, region="europe-west1")
    run(tmp_path, "infra-2", FakeExec(), bucket=bucket, g=guards(), inputs=second)
    assert tfvars.stored(bucket, "infra") == second, "the next apply's inputs replace the last, conditionally"


def test_pass2_sets_console_app_setup() -> None:
    state = install_state()
    first = tfvars.infra_inputs(state, pass_name="infra-1", installer="admin@example.com")
    assert first["enable_console_app_setup"] is False
    for pass_name in ("infra-2", "infra-3", "infra-4"):
        assert tfvars.infra_inputs(state, pass_name=pass_name, installer="a@example.com")[
            "enable_console_app_setup"] is True, pass_name


def test_groups_auth_admin_role_for_installs() -> None:
    def mode(**outputs) -> str:
        return tfvars.infra_inputs(install_state(**outputs), pass_name="infra-2", installer="a@example.com")[
            "groups_auth"]

    assert mode() == ADMIN_ROLE
    assert mode(groups_auth=ADMIN_ROLE) == ADMIN_ROLE
    assert mode(groups_auth="something-else") == ADMIN_ROLE
    assert mode(groups_auth=DELEGATION) == DELEGATION, "delegation only when the person chose it at stage 8"
    chosen = tfvars.infra_inputs(install_state(groups_auth=DELEGATION, groups_impersonate_user="it@example.com"),
                                 pass_name="infra-2", installer="a@example.com")
    assert chosen["groups_impersonate_user"] == "it@example.com"
    assert "groups_impersonate_user" not in tfvars.infra_inputs(
        install_state(groups_impersonate_user="it@example.com"), pass_name="infra-2", installer="a@example.com")


def test_tenant_job_vpc_egress_true() -> None:
    state = install_state()
    for pass_name in ("infra-2", "infra-3", "infra-4"):
        assert tfvars.infra_inputs(state, pass_name=pass_name, installer="a@example.com")[
            "tenant_job_vpc_egress"] is True, f"{pass_name}: lesson 35"


def test_traffic_latest_100(tmp_path: Path) -> None:
    for pass_name in ("registry", "infra-1", "infra-2", "infra-3", "infra-4"):
        inputs = tfvars.infra_inputs(install_state(), pass_name=pass_name, installer="a@example.com")
        assert not [k for k in inputs if "traffic" in k or "revision" in k], "no input pins a revision"
    with pytest.raises(ValueError, match="latest revision"):
        tfvars.write_local(tmp_path, {"swarm_api_traffic_revision": "swarm-api-00042"})

    latest = {"type": "google_cloud_run_v2_service", "address": "module.cloud_run.x",
              "change": {"actions": ["update"], "after": {"traffic": [
                  {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST", "percent": 100}]}}}
    pinned = {"type": "google_cloud_run_v2_service", "address": "module.cloud_run.y",
              "change": {"actions": ["update"], "after": {"traffic": [
                  {"type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION", "revision": "y-00042", "percent": 100}]}}}
    assert tfmod.traffic_problems(latest) == []
    assert tfmod.traffic_problems(pinned)
    exec_ = FakeExec(plan={"resource_changes": [pinned]})
    with pytest.raises(tfmod.TerraformRefused, match="latest revision"):
        run(tmp_path, "infra-2", exec_, g=guards())
    assert "apply" not in exec_.verbs(), "lesson 28: a pinned revision made a later deploy serve 0 %"
