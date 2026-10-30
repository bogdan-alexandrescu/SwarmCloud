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
from .test_terraform_runner import DESKTOP_CLIENT_ID, PROJECT, FakeExec, guards, install_state, run

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
    assert boot["frontend_iap_members"] == ["domain:example.com", "user:admin@example.com",
                                            f"serviceAccount:qa-console@{PROJECT}.iam.gserviceaccount.com"]
    assert boot["workspace_apply_image"] == (
        f"europe-west1-docker.pkg.dev/{PROJECT}/swarm-images/workspace-apply@{DIGEST}"
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


# -- DAYONE-ENGINE-INPUTS: the inputs the design depends on ------------------------------

INFRA_PASSES = ("registry", "infra-1", "infra-2", "infra-3", "infra-4", "infra-close")
EVERY_VERSION = {name: True for name in (*tfvars.KEY_SWITCHES, *tfvars.APP_SECRETS, tfvars.DESKTOP_SECRET)}


def _infra(pass_name: str, versions: dict | None = None, **outputs) -> dict:
    return tfvars.infra_inputs(install_state(**outputs), pass_name=pass_name, installer="admin@example.com",
                               versions=versions)


def _boot(pass_name: str, **outputs) -> dict:
    return tfvars.bootstrap_inputs(install_state(**outputs), pass_name=pass_name, installer="admin@example.com")


def test_iap_admits_the_organisation_installer_and_qa_identity() -> None:
    # Decision 1: domain:<org_domain>, the installer's user:, the QA identity's account.
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        members = _boot(pass_name, org_domain="Acme.Example")["frontend_iap_members"]
        assert members[:2] == ["domain:acme.example", "user:admin@example.com"], pass_name
        assert f"serviceAccount:qa-console@{PROJECT}.iam.gserviceaccount.com" in members
        assert not [m for m in members if m.startswith("all")], "never allUsers or allAuthenticatedUsers"


def test_allowed_domains_is_the_org_domain() -> None:
    for pass_name in INFRA_PASSES:
        assert _infra(pass_name)["allowed_domains"] == ["example.com"], pass_name
    assert _infra("infra-2", org_domain="@Acme.Example ")["allowed_domains"] == ["acme.example"]
    for bad in ("gmail", "https://acme.example", "acme.example/x"):
        with pytest.raises(ValueError, match="not a domain"):
            _infra("infra-2", org_domain=bad)


def test_setup_github_org_from_stage_0() -> None:
    for pass_name in INFRA_PASSES:
        assert _infra(pass_name, github_org="acme-eng")["setup_github_org"] == "acme-eng", pass_name


def test_discovery_from_desktop_client_id() -> None:
    discovery = _infra("infra-1")["discovery"]
    # terraform/infra refuses an oauth_client_id without the other three (variables.tf, var.discovery).
    assert discovery == {"name": "SwarmCloud", "version": "2026.10.1", "oauth_client_id": DESKTOP_CLIENT_ID,
                         "plugin_min_version": tfvars.PLUGIN_MIN_VERSION}
    assert all(discovery.values())


def test_discovery_serves_secret_only_with_its_version() -> None:
    assert _infra("infra-1", EVERY_VERSION)["discovery_serves_client_secret"] is False, "pass 1: every switch off"
    assert _infra("infra-2", {})["discovery_serves_client_secret"] is False, "no version of the secret yet"
    for pass_name in ("infra-2", "infra-close", "infra-3", "infra-4"):
        assert _infra(pass_name, EVERY_VERSION)["discovery_serves_client_secret"] is True, pass_name


def test_iap_programmatic_client_and_backends_in_bootstrap_pass_2() -> None:
    first = _boot("bootstrap-1")
    assert first["frontend_iap_backends"] == [], "the backends do not exist before infra pass 1"
    assert first["frontend_iap_programmatic_clients"] == []
    second = _boot("bootstrap-2")
    assert second["frontend_iap_backends"] == ["swarm-ui-backend", "swarm-ui-ui-backend"]
    assert second["frontend_iap_programmatic_clients"] == [DESKTOP_CLIENT_ID]
    assert all(b.startswith("swarm") for b in second["frontend_iap_backends"]), "only the platform's own backends"


def test_qa_identity_and_hooks_dedup_role_on_in_bootstrap() -> None:
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        inputs = _boot(pass_name)
        assert inputs["enable_qa_identity"] is True, pass_name
        assert inputs["enable_github_hooks_dedup_role"] is True, f"{pass_name}: before infra-4's hooks"


def test_graph_service_with_its_key_and_image() -> None:
    assert "swarm-graph-ticket-key" in tfvars.KEY_SWITCHES, "stage 6 gives the ticket key its version"
    assert _infra("infra-1")["enable_graph_service"] is False, "the workload pool exists only after pass 1"
    assert _infra("infra-2")["enable_graph_service"] is True
    no_image = _infra("infra-2", EVERY_VERSION)
    assert no_image["graph_service"] == {"serve": False, "enabled": False}, "no swarm-graph digest"
    digests = {"image_digests": {"swarm-graph": DIGEST}}
    no_key = _infra("infra-2", dict(EVERY_VERSION, **{"swarm-graph-ticket-key": False}), **digests)
    assert no_key["graph_service"] == {"serve": False, "enabled": False}, "key before switch"
    on = _infra("infra-2", EVERY_VERSION, **digests)
    assert on["graph_service"] == {"serve": True, "enabled": True}


def test_scheduled_doctor_on_from_infra_pass_2() -> None:
    # DAYONE-TFVARS-2: BGD turned the hourly doctor on; pass 1 keeps every switch off.
    for pass_name in ("registry", "infra-1"):
        assert _infra(pass_name, EVERY_VERSION)["enable_scheduled_doctor"] is False, pass_name
    for pass_name in ("infra-2", "infra-3", "infra-4", "infra-close"):
        assert _infra(pass_name, {})["enable_scheduled_doctor"] is True, pass_name


def test_cluster_reach_until_the_closing_pass() -> None:
    reach = [{"cidr_block": "203.0.113.7/32", "display_name": "swarm-install"}]
    # Passes 3 and 4 keep it: stage 14's verify.reconciler-jobs runs kubectl after them.
    for pass_name in ("registry", "infra-1", "infra-2", "infra-3", "infra-4"):
        assert _infra(pass_name)["gke_master_authorized_cidrs"] == reach, pass_name
    assert _infra("infra-close")["gke_master_authorized_cidrs"] == []
    closing = _infra("infra-close", EVERY_VERSION)
    fourth = _infra("infra-4", EVERY_VERSION)
    assert {k: v for k, v in closing.items() if k != "gke_master_authorized_cidrs"} == {
        k: v for k, v in fourth.items() if k != "gke_master_authorized_cidrs"}, "the closing pass changes nothing else"
    for bad in ("203.0.113.7", "203.0.113.0/24", "2001:db8::1/128", "300.1.1.1/32"):
        with pytest.raises(ValueError, match="a.b.c.d/32"):
            _infra("infra-1", installer_cidr=bad)


@pytest.mark.parametrize(("key", "stage"), [("github_org", "stage 0"), ("org_domain", "stage 0"),
                                            ("installer_cidr", "stage 0"), ("desktop_client_id", "stage 2")])
def test_missing_key_names_the_stage_that_records_it(key: str, stage: str) -> None:
    with pytest.raises(LookupError, match=f"no {key} yet; {stage}"):
        _infra("infra-1", **{key: ""})
    if key in ("org_domain", "desktop_client_id"):
        with pytest.raises(LookupError, match=f"no {key} yet; {stage}"):
            _boot("bootstrap-2", **{key: ""})


def _declared(root: str) -> set[str]:
    import re

    tf = Path(__file__).resolve().parents[3] / "terraform" / root
    found: set[str] = set()
    for path in tf.glob("*.tf"):
        found.update(re.findall(r'^variable\s+"([A-Za-z0-9_]+)"', path.read_text(), flags=re.M))
    return found


def test_every_generated_input_is_a_declared_variable() -> None:
    # A name the root does not declare is a warning Terraform prints and an input it never applies.
    digests = {"image_digests": {"swarm-api": DIGEST, "workspace-apply": DIGEST, "swarm-graph": DIGEST},
               "admin_groups": ["swarm-admins@example.com"], "github_app": {"id": "42", "slug": "s"},
               "sizing": _sizing(), "addons": EVERY_ADDON_ON}
    for pass_name in tfvars.PASSES:
        root = tfvars.ROOT_OF[pass_name]
        if root == "bootstrap":
            inputs = _boot(pass_name, **digests)
        else:
            inputs = _infra(pass_name, EVERY_VERSION, **digests)
        assert set(inputs) <= _declared(root), (pass_name, sorted(set(inputs) - _declared(root)))


# -- DAYONE-TFVARS-2: sizing, one-person admission, add-ons --------------------------------

QA_MEMBER = f"serviceAccount:qa-console@{PROJECT}.iam.gserviceaccount.com"
EVERY_ADDON_ON = {
    "bright-data-egress": {"phase": "on"},
    "tailscale": {"phase": "on", "choices": {"tailscale_egress_target": "egress.tailnet.ts.net"}},
    "typesafe": {"phase": "on"},
    "brave-search": {"phase": "on"},
    "ci": {"phase": "on", "choices": {"github_repository": "acme/swarm", "sandbox_repository": "acme/sandbox",
                                      "runner_label": "swarm-ci"}},
}


def _sizing(mode: str = "team", members: int = 8, **pool_limits) -> dict:
    # The INTERFACE SHEET's shape, as SIZING-ASK writes it in stage 0.
    limits = {"global": 60, "default_tenant": 12, "provider_tenant": 6,
              "resource_classes": {"standard": 40}, "runner_profiles": {"claude-code": 30},
              "backends": {"cloud-run": 50}}
    limits.update(pool_limits)
    return {"mode": mode, "members": members, "pool_limits": limits,
            "service_max_instances": {"swarm-api": 8, "swarm-scheduler": 2, "swarm-quota-broker": 2,
                                      "swarm-reconciler": 1},
            "pool_accounts": 2, "budget_usd": 400}


def test_sizing_sets_pool_limits_on_every_infra_pass() -> None:
    sizing = _sizing()
    for pass_name in INFRA_PASSES:
        assert _infra(pass_name, sizing=sizing)["pool_limits"] == sizing["pool_limits"], pass_name


def test_sizing_sets_service_max_instances_keeping_swarm_ui() -> None:
    inputs = _infra("infra-2", sizing=_sizing())
    # infra refuses a map that does not name every control-plane service; sizing has no swarm-ui.
    assert inputs["service_max_instances"] == {"swarm-api": 8, "swarm-scheduler": 2, "swarm-quota-broker": 2,
                                               "swarm-reconciler": 1, "swarm-ui": 2}


def test_no_sizing_keeps_todays_inputs() -> None:
    # An install state from before SIZING-ASK: terraform's own defaults stand.
    for pass_name in INFRA_PASSES:
        inputs = _infra(pass_name)
        assert "pool_limits" not in inputs and "service_max_instances" not in inputs, pass_name


def test_sizing_breaking_provider_validation_fails_before_any_plan() -> None:
    # 8 people x provider_tenant 10 = 80 > the provider-wide pool, which is global (60) when unset.
    with pytest.raises(ValueError, match=r"provider_tenant .*8 .*60"):
        _infra("infra-1", sizing=_sizing(provider_tenant=10))
    with pytest.raises(ValueError, match="provider"):
        _infra("infra-1", sizing=_sizing(providers={"anthropic": 40}))
    # global is the pool of every provider `providers` does not name, so it is checked too.
    with pytest.raises(ValueError, match="global"):
        _infra("infra-1", sizing=_sizing(provider_tenant=10, providers={"anthropic": 80}))
    ok = _infra("infra-1", sizing=_sizing(provider_tenant=10, providers={"anthropic": 80}, **{"global": 80}))
    assert ok["pool_limits"]["providers"] == {"anthropic": 80}
    # One person is one tenant.
    assert _infra("infra-1", sizing=_sizing("individual", 1, provider_tenant=60))["pool_limits"]["global"] == 60


@pytest.mark.parametrize("bad", [{"global": 0}, {"global": -1}, {"provider_tenant": "ten"}, {"surplus": 3}])
def test_sizing_pool_limits_refused_when_infra_would_refuse_them(bad: dict) -> None:
    with pytest.raises(ValueError, match="sizing"):
        _infra("infra-1", sizing=_sizing(**bad))


def test_sizing_service_max_instances_must_be_positive() -> None:
    sizing = _sizing()
    sizing["service_max_instances"]["swarm-api"] = 0
    with pytest.raises(ValueError, match="sizing.*swarm-api"):
        _infra("infra-1", sizing=sizing)
    with pytest.raises(ValueError, match="sizing.*mode"):
        _infra("infra-1", sizing=dict(_sizing(), mode="company"))


def test_individual_iap_admits_only_the_installer() -> None:
    # Decision 7: one person; no domain: member and no team group. The QA identity stays:
    # terraform/bootstrap refuses enable_qa_identity without it (qa_identity.tf).
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        members = _boot(pass_name, sizing=_sizing("individual", 1))["frontend_iap_members"]
        assert members == ["user:admin@example.com", QA_MEMBER], pass_name
        assert not [m for m in members if m.startswith(("domain:", "group:"))]
    second = _boot("bootstrap-2", sizing=_sizing("individual", 1))
    assert second["frontend_iap_programmatic_clients"] == [DESKTOP_CLIENT_ID], "the Desktop client still signs in"


def test_individual_allowed_domains_stays_the_org_domain() -> None:
    for pass_name in INFRA_PASSES:
        assert _infra(pass_name, sizing=_sizing("individual", 1))["allowed_domains"] == ["example.com"], pass_name


def test_team_iap_keeps_decision_1() -> None:
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        members = _boot(pass_name, sizing=_sizing("team", 8))["frontend_iap_members"]
        assert members == ["domain:example.com", "user:admin@example.com", QA_MEMBER], pass_name


def test_addon_overlay_in_every_infra_pass() -> None:
    for pass_name in INFRA_PASSES:
        inputs = _infra(pass_name, EVERY_VERSION, addons=EVERY_ADDON_ON)
        assert inputs["enable_browser_egress"] is True, pass_name
        assert inputs["enable_tailscale_egress"] is True and inputs["search_tool_enabled"] is True, pass_name
        assert inputs["enable_typesafe_forwarder"] is True, pass_name
        assert inputs["typesafe_forwarder"]["serve"] is True, pass_name
        assert inputs["enable_acceptance_fixture"] is True, pass_name


def test_addon_overlay_in_every_bootstrap_pass() -> None:
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        inputs = _boot(pass_name, addons=EVERY_ADDON_ON)
        # Core sets enable_github_wif false; the ci add-on's recorded phase wins.
        assert inputs["enable_github_wif"] is True, pass_name
        assert inputs["github_repository"] == "acme/swarm", pass_name
        assert inputs["enable_typesafe_forwarder_read"] is True, pass_name


def test_no_addon_leaves_core_inputs_alone() -> None:
    assert _boot("bootstrap-1")["enable_github_wif"] is False
    assert _boot("bootstrap-1", addons={}) == _boot("bootstrap-1")
    assert _infra("infra-2", EVERY_VERSION, addons={}) == _infra("infra-2", EVERY_VERSION)
    off = _infra("infra-2", EVERY_VERSION, addons={"brave-search": {"phase": "off"}})
    assert off["search_tool_enabled"] is False


# -- DAYONE-TFVARS-3: the CI add-on's acceptance sandbox ---------------------------------


def test_ci_sandbox_repository_reaches_the_verify_job_on_every_infra_pass() -> None:
    # The swarm-verify job's SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL is the same URL the CI add-on
    # sets as the repository variable, so the acceptance never falls back to the live deployment's.
    for pass_name in INFRA_PASSES:
        inputs = _infra(pass_name, EVERY_VERSION, addons=EVERY_ADDON_ON)
        assert inputs["acceptance_sandbox_repository_url"] == "https://github.com/acme/sandbox.git", pass_name
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        assert "acceptance_sandbox_repository_url" not in _boot(pass_name, addons=EVERY_ADDON_ON), pass_name


def test_no_ci_sandbox_repository_leaves_the_input_unset() -> None:
    # Unset is Terraform's null default: no env on the job, no change planned.
    no_ci = {name: record for name, record in EVERY_ADDON_ON.items() if name != "ci"}
    no_sandbox = {"ci": {"phase": "on", "choices": {"github_repository": "acme/swarm"}}}
    for addons in ({}, no_ci, no_sandbox):
        for pass_name in INFRA_PASSES:
            inputs = _infra(pass_name, EVERY_VERSION, addons=addons)
            assert "acceptance_sandbox_repository_url" not in inputs, (pass_name, addons)
    assert "acceptance_sandbox_repository_url" not in _infra("infra-2", EVERY_VERSION)


# -- DAYONE-TF-NAMES: the registry repository and the state bucket's location -----------------


def _variable_default(root: str, name: str) -> str:
    import re

    tf = Path(__file__).resolve().parents[3] / "terraform" / root
    for path in tf.glob("*.tf"):
        found = re.search(rf'^variable\s+"{name}"\s*\{{(.*?)^\}}', path.read_text(), flags=re.M | re.S)
        if found:
            default = re.search(r'^\s*default\s*=\s*"([^"]*)"', found.group(1), flags=re.M)
            assert default, f"terraform/{root}'s {name} has no string default"
            return default.group(1)
    raise AssertionError(f"terraform/{root} declares no {name}")


def test_repository_is_infras_artifact_registry_repository_default() -> None:
    # Audit finding 4: stage 4 copies into REPOSITORY and infra validates image_refs against
    # var.artifact_registry_repository; a different name fails the copy and the plan.
    assert tfvars.REPOSITORY == _variable_default("infra", "artifact_registry_repository")


def test_every_infra_pass_names_the_repository_the_images_were_copied_into() -> None:
    digests = {"image_digests": {"swarm-api": DIGEST}}
    for pass_name in INFRA_PASSES:
        inputs = _infra(pass_name, EVERY_VERSION, **digests)
        assert inputs["artifact_registry_repository"] == tfvars.REPOSITORY, pass_name
        prefix = f"europe-west1-docker.pkg.dev/{PROJECT}/{inputs['artifact_registry_repository']}/"
        assert inputs["image_refs"]["swarm-api"].startswith(prefix), pass_name


def test_bootstrap_keeps_the_state_bucket_in_the_install_region() -> None:
    # Audit finding 5: stage 1 creates the bucket in the install's region; bootstrap imports it
    # with var.location (default US-CENTRAL1), and a location cannot change in place.
    for pass_name in ("bootstrap-1", "bootstrap-2"):
        assert _boot(pass_name)["location"] == "EUROPE-WEST1", pass_name
