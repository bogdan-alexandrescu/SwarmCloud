# source-text: forbidden-pattern - asserts the wrong-project predicate is not restated in destroy-guard.jq or planguard.py
"""The install engine refuses a plan that names another project (issue 344, gap 1).

The rule is the operator scripts' own: `scripts/lib/wrong-project.jq`, which
`scripts/lib/destroy-guard.jq` (and so `scripts/lib/plan-guard.sh`) includes
and the engine runs with jq over every plan it makes, before any apply. One
rule file, two readers: these tests hold the engine to it, hold the shell
guard's verdict equal to the engine's on the same plan, and hold the wheel
mapping that ships the file.

No credentials, no emulator, no terraform binary: terraform is a fake; jq is
the local one (the engine's pinned jq in an install).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from swarm_install import planguard
from swarm_install import uninstall as un
from swarm_install import upgrade as up
from swarm_install.terraform import TerraformRefused

from .test_terraform_runner import PROJECT, FakeExec, run
from .test_uninstall import DEPLOYER, FakeWorld as UninstallWorld
from .test_uninstall import PROJECT as UNINSTALL_PROJECT
from .test_upgrade import NEW, FakeWorld as UpgradeWorld
from .test_upgrade import PROJECT as UPGRADE_PROJECT
from .test_upgrade import make as make_upgrade

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib"
RULE = LIB / "wrong-project.jq"
DESTROY_GUARD = LIB / "destroy-guard.jq"
PYPROJECT = REPO / "apps" / "swarm-install" / "pyproject.toml"

#: Another team's project: generic, never a real one.
FOREIGN = "peer-team-prod"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed here")


def bucket_change(project: str, actions: list[str], *, half: str = "after") -> dict:
    body = {"name": "swarm-artifacts", "project": project, "labels": {"managed-by": "swarm-terraform"}}
    other = "before" if half == "after" else "after"
    return {"address": "google_storage_bucket.artifacts", "type": "google_storage_bucket",
            "change": {"actions": actions, half: body, other: None}}


def shell_verdict(tmp_path: Path, plan: dict) -> list[dict]:
    """The shell guard's `wrong_project` for `plan`, with every change a deletion."""
    plan = json.loads(json.dumps(plan))
    for rc in plan.get("resource_changes", []):
        rc["change"]["actions"] = ["delete"]
        rc["change"].setdefault("before", rc["change"].get("after"))
    path = tmp_path / "shell-plan.json"
    path.write_text(json.dumps(plan))
    done = subprocess.run(
        ["jq", "-c", "-f", str(DESTROY_GUARD), "--argjson", "deny", "[]", "--argjson", "allow_types", "[]",
         "--arg", "project", PROJECT, "--arg", "prefix", "swarm-", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(done.stdout)["wrong_project"]


def verbs(exec_: FakeExec) -> list[str]:
    return exec_.verbs()


# -- run_pass: install and every pass that goes through it ----------------------


def test_a_plan_naming_another_project_is_refused_before_apply(tmp_path: Path) -> None:
    exec_ = FakeExec(plan={"resource_changes": [bucket_change(FOREIGN, ["create"])]})
    with pytest.raises(TerraformRefused) as refused:
        run(tmp_path, "infra-1", exec_)
    message = str(refused.value)
    assert "google_storage_bucket.artifacts" in message
    assert FOREIGN in message
    assert PROJECT in message
    assert "apply" not in verbs(exec_)


def test_a_plan_for_the_installs_own_project_passes(tmp_path: Path) -> None:
    exec_ = FakeExec(plan={"resource_changes": [bucket_change(PROJECT, ["create"])]})
    run(tmp_path, "infra-1", exec_)
    assert "apply" in verbs(exec_)


def test_a_project_not_set_in_the_plan_and_not_unknown_reads_as_the_installs_own(tmp_path: Path) -> None:
    change = bucket_change(PROJECT, ["create"])
    change["change"]["after"].pop("project")
    exec_ = FakeExec(plan={"resource_changes": [change]})
    run(tmp_path, "infra-1", exec_)
    assert "apply" in verbs(exec_)


def test_a_project_unknown_until_apply_is_refused(tmp_path: Path) -> None:
    change = bucket_change(PROJECT, ["create"])
    change["change"]["after"].pop("project")
    change["change"]["after_unknown"] = {"project": True}
    exec_ = FakeExec(plan={"resource_changes": [change]})
    with pytest.raises(TerraformRefused, match="unknown at plan time"):
        run(tmp_path, "infra-1", exec_)
    assert "apply" not in verbs(exec_)


def other_change(after: dict, rtype: str = "google_thing", *, unknown: dict | None = None) -> dict:
    change = {"actions": ["create"], "before": None, "after": after}
    if unknown is not None:
        change["after_unknown"] = unknown
    return {"address": f"{rtype}.x", "type": rtype, "change": change}


NAMING_ANOTHER = [
    pytest.param(other_change({"service_account_id": f"projects/{FOREIGN}/serviceAccounts/a@{FOREIGN}.iam.gserviceaccount.com"}),
                 id="projects-path"),
    pytest.param(other_change({"crypto_key_id": f"projects/{FOREIGN}/locations/x/keyRings/k/cryptoKeys/c"}), id="key-id"),
    pytest.param(other_change({"host_project": FOREIGN}, "google_compute_shared_vpc_service_project"), id="host_project"),
    pytest.param(other_change({"nested": [{"subnetwork_project": FOREIGN}]}), id="nested-subnetwork_project"),
    pytest.param(other_change({}, "google_organization_iam_member"), id="organization-level"),
    pytest.param(other_change({}, "google_folder_iam_member"), id="folder-level"),
    pytest.param(other_change({}, "google_billing_account_iam_member"), id="billing-level"),
    pytest.param(other_change({"name": "x"}, unknown={"host_project": True}), id="unknown-host_project"),
]


@pytest.mark.parametrize("change", NAMING_ANOTHER)
def test_every_way_of_naming_another_project_is_refused(tmp_path: Path, change: dict) -> None:
    exec_ = FakeExec(plan={"resource_changes": [change]})
    with pytest.raises(TerraformRefused):
        run(tmp_path, "infra-1", exec_)
    assert "apply" not in verbs(exec_)
    plan = {"resource_changes": [change]}
    assert planguard.wrong_project(plan, PROJECT)
    assert shell_verdict(tmp_path, plan)


def test_the_installs_own_paths_and_numbers_pass(tmp_path: Path) -> None:
    plan = {"prior_state": {"values": {"root_module": {"resources": [
                {"type": "google_project", "values": {"number": 123456789012}}]}}},
            "resource_changes": [other_change({
                "member": f"projects/{PROJECT}/serviceAccounts/a", "path": "projects/123456789012/secrets/s",
                "image": "projects/deeplearning-platform-release/global/images/family/x",
                "host_project": PROJECT})]}
    assert planguard.wrong_project(plan, PROJECT) == []


def test_a_provider_naming_another_project_is_refused(tmp_path: Path) -> None:
    plan = {"resource_changes": [], "configuration": {"provider_config": {
        "google.peer": {"expressions": {"project": {"constant_value": FOREIGN}}},
        "google": {"expressions": {"project": {"references": ["var.project_id"]}}}}}}
    assert [v["project"] for v in planguard.wrong_project(plan, PROJECT)] == [FOREIGN]
    assert [v["project"] for v in shell_verdict(tmp_path, plan)] == [FOREIGN]


def test_the_before_half_is_read_too(tmp_path: Path) -> None:
    exec_ = FakeExec(plan={"resource_changes": [bucket_change(FOREIGN, ["update"], half="before")]})
    with pytest.raises(TerraformRefused, match=FOREIGN):
        run(tmp_path, "infra-1", exec_)
    assert "apply" not in verbs(exec_)


# -- fail closed --------------------------------------------------------------------


def test_no_jq_refuses_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planguard, "jq_binary", lambda: str(tmp_path / "no-such-jq"))
    exec_ = FakeExec(plan={"resource_changes": [bucket_change(PROJECT, ["create"])]})
    with pytest.raises(TerraformRefused, match="could not be checked"):
        run(tmp_path, "infra-1", exec_)
    assert "apply" not in verbs(exec_)


@pytest.mark.parametrize("script", ["#!/bin/sh\nexit 3\n", "#!/bin/sh\ncat >/dev/null\n"])
def test_a_jq_that_fails_or_says_nothing_refuses_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         script: str) -> None:
    fake = tmp_path / "jq"
    fake.write_text(script)
    fake.chmod(0o755)
    monkeypatch.setattr(planguard, "jq_binary", lambda: str(fake))
    assert planguard.refusal({"resource_changes": []}, PROJECT, "infra-1") is not None


# -- the uninstall's destroy and the upgrade's plan review -------------------------


def test_the_uninstall_destroy_refuses_a_plan_naming_another_project(tmp_path: Path) -> None:
    world = UninstallWorld(tmp_path)
    tf = world.terraform("infra")
    world.tf["infra"].resources = {"google_storage_bucket.b": (
        "google_storage_bucket", {"name": "swarm-b", "project": FOREIGN,
                                  "labels": {"managed-by": "swarm-terraform"}})}
    with pytest.raises(un.UninstallRefused) as refused:
        un.destroy_root(tf, project=UNINSTALL_PROJECT, who=DEPLOYER, inputs={"project_id": UNINSTALL_PROJECT},
                        deny_list=[], approved=UNINSTALL_PROJECT)
    assert "google_storage_bucket.b" in str(refused.value) and FOREIGN in str(refused.value)
    assert "apply" not in [e[2] for e in world.log if e[0] == "terraform"]


def test_the_upgrade_plan_review_refuses_a_plan_naming_another_project(tmp_path: Path) -> None:
    world = UpgradeWorld()
    world.plans["infra"] = [{"resource_changes": [bucket_change(FOREIGN, ["create"])]}]
    flow, _, _ = make_upgrade(tmp_path, world, answer=UPGRADE_PROJECT)
    with pytest.raises(up.UpgradeRefused) as refused:
        flow.run(NEW)
    assert FOREIGN in str(refused.value) and "google_storage_bucket.artifacts" in str(refused.value)
    assert not [c for c in world.calls if c[0] == "apply_root"]


# -- one rule, two readers -----------------------------------------------------------


def test_the_rule_is_written_once_and_the_shell_guard_includes_it() -> None:
    assert RULE.is_file()
    guard = DESTROY_GUARD.read_text()
    assert 'include "wrong-project"' in guard
    assert ".change.before.project //" not in guard, "destroy-guard.jq restates the wrong-project predicate"
    engine = (REPO / "apps" / "swarm-install" / "swarm_install" / "planguard.py").read_text()
    assert "// $project" not in engine, "planguard.py restates the wrong-project predicate"


def test_the_shell_guard_and_the_engine_agree(tmp_path: Path) -> None:
    plan = {"resource_changes": [
        bucket_change(FOREIGN, ["delete"], half="before"),
        {"address": "google_pubsub_topic.t", "type": "google_pubsub_topic",
         "change": {"actions": ["delete"], "before": {"name": "swarm-t", "project": PROJECT}, "after": None}},
    ]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    done = subprocess.run(
        ["jq", "-c", "-f", str(DESTROY_GUARD), "--argjson", "deny", "[]", "--argjson", "allow_types", "[]",
         "--arg", "project", PROJECT, "--arg", "prefix", "swarm-", str(path)],
        capture_output=True, text=True, check=True)
    shell = json.loads(done.stdout)["wrong_project"]
    assert shell == [{"address": "google_storage_bucket.artifacts", "type": "google_storage_bucket",
                      "project": FOREIGN}]
    assert planguard.wrong_project(plan, PROJECT) == shell


def test_the_wheel_ships_the_rule_file() -> None:
    config = tomllib.loads(PYPROJECT.read_text())
    mapped = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert mapped.get("../../scripts/lib/wrong-project.jq") == "swarm_install/_guard/wrong-project.jq"
    assert planguard.rule_dir().joinpath("wrong-project.jq").is_file()
