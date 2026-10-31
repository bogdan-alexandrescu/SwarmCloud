"""`swarm addon enable|disable <name>` (DAYONE-ADDON-1, part of #2688): the five
add-ons, each opt-in after READY, on fakes.

What has to hold (docs/design/day-one-install.md §12; docs/design/day-one-plan.md
lane ADDON-1):

  * core passes with every add-on off: no install step satisfies an `addon:*`
    entry, and the install's own inputs set no add-on switch;
  * disabling keeps the secret containers; deleting them is a second, typed
    step, and a plan that would delete one is refused before it is applied;
  * a secret goes in only through the console's one-time link or the hidden
    prompt: no flag takes a value and nothing reads stdin;
  * lesson 6: the CI add-on's builds run as their own account, never a
    default one; the CI add-on turns the acceptance fixture on;
  * lesson 45: every `with:` key of an `actions/create-github-app-token` step
    is an input of the pinned action, or a GitHub permission key.

No credentials, no emulator, no cluster: Google, swarm-api, Terraform, the
cluster, gh and the clock are fakes.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import secrets
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from swarm_install import addons, tfvars
from swarm_install import manifest as manifest_mod
from swarm_install import steps as steps_mod
from swarm_install.events import Events
from swarm_install.exitcodes import MET
from swarm_install.state import InstallState
from swarm_install.verbs import discover

ROOT = Path(__file__).resolve().parents[3]
PROJECT = "acme-swarm-prod"
INSTALLER = "alice@example.com"
DIGEST = "sha256:" + "0" * 64


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeWorld:
    """Google, swarm-api, Terraform, the cluster and gh, recording every call."""

    #: Core's infra passes create these unconditionally (egress.tf, tailscale.tf).
    CORE_CONTAINERS = ("swarm-egress-credential", "swarm-egress-key", "swarm-tailscale-oauth")

    def __init__(self, *, containers: Sequence[str] = CORE_CONTAINERS, versions: Sequence[str] = (),
                 plan_deletes: Sequence[tuple[str, str]] = (), versions_after_polls: int = 2) -> None:
        self.project = PROJECT
        self.containers = set(containers)
        self.versions = set(versions)
        self.calls: list[tuple[str, Any]] = []
        self.applied: list[tuple[str, dict[str, Any]]] = []
        self.plan_deletes = list(plan_deletes)
        self.links: list[tuple[str, str]] = []
        self.polls_left = versions_after_polls
        self.services: set[str] = set()
        self.accounts: dict[str, dict[str, Any]] = {}
        self.variables: dict[str, str] = {}
        self.workloads: set[str] = set()
        self.added: list[str] = []
        #: swarm-api reads swarm-search-api-key once the owner's IAM grant is in.
        self.readers = {f"serviceAccount:swarm-api@{PROJECT}.iam.gserviceaccount.com"}
        #: The repository's environments and their protection rule counts.
        self.environments: dict[str, int] = {"dev": 0, "prod": 1}
        self.created_environments: list[tuple[str, str | None]] = []

    # Secret Manager
    def secret_exists(self, name: str) -> bool:
        return name in self.containers

    def has_version(self, name: str) -> bool:
        if name in self.versions:
            return True
        if any(name == secret for _, secret in self.links):
            self.polls_left -= 1
            if self.polls_left <= 0:
                self.versions.add(name)  # the person redeemed the link in the console
                return True
        return False

    def create_secret(self, name: str, labels: Mapping[str, str]) -> None:
        self.calls.append(("create_secret", name))
        self.containers.add(name)

    def add_version(self, name: str, data: bytes) -> int:
        assert len(data) >= 32
        self.added.append(name)
        self.versions.add(name)
        return 1

    def delete_secret(self, name: str) -> None:
        self.calls.append(("delete_secret", name))
        self.containers.discard(name)

    def secret_readers(self, name: str) -> set[str]:
        return set(self.readers) if name == "swarm-search-api-key" else set()

    def enabled_services(self) -> set[str]:
        return set(self.services)

    def enable_services(self, names: Sequence[str]) -> None:
        self.services.update(names)

    def service_account(self, email: str) -> Mapping[str, Any] | None:
        return self.accounts.get(email)

    def create_service_account(self, account_id: str, display_name: str) -> None:
        self.calls.append(("create_service_account", account_id))
        self.accounts[f"{account_id}@{PROJECT}.iam.gserviceaccount.com"] = {"displayName": display_name}

    # swarm-api
    def mint_link(self, addon: str, owner: str) -> Mapping[str, Any]:
        self.links.append((owner, addons.ADDONS[addon].secrets[-1].name))
        return {"kind": "addon-secret", "label": addon, "owner": owner,
                "url": f"https://console.example.com/admin/setup/link/{secrets.token_hex(8)}",
                "expires_at": "2026-10-31T00:00:00+00:00"}

    # Terraform: a root's containers appear with the switch that creates them.
    def apply(self, root: str, overlay: Mapping[str, Any], *,
              guard: Callable[[Mapping[str, Any]], None]) -> Mapping[str, Any]:
        plan = {"resource_changes": [
            {"address": address, "type": kind, "change": {"actions": ["delete"]}}
            for address, kind in self.plan_deletes]}
        guard(plan)
        self.applied.append((root, dict(overlay)))
        outputs: dict[str, Any] = {}
        if root == "infra":
            if overlay.get("enable_typesafe_forwarder"):
                self.containers.add("swarm-typesafe-fwd-key")
            # Terraform renders the repository's own manifests (egress.tf, tailscale.tf, typesafe_fwd.tf).
            if overlay.get("enable_browser_egress"):
                outputs["swarm_egress_manifest"] = workload_manifest("swarm-egress.yaml")
            if overlay.get("enable_tailscale_egress"):
                outputs["tailscale_egress_manifest"] = workload_manifest("tailscale-egress.yaml")
            if (overlay.get("typesafe_forwarder") or {}).get("serve"):
                outputs["swarm_typesafe_fwd_manifest"] = workload_manifest("swarm-typesafe-fwd.yaml")
        return outputs

    def kube(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(("kube", args))
        if args[0] == "apply":
            self.workloads.update(f"{k.lower()}/{n}" for k, n in WORKLOAD.findall(str(stdin)))
            return 0, "applied", ""
        if args[0] == "get":
            # Only the workload the manifest declares is there, by kind and name.
            return (0, "1/1", "") if args[1] in self.workloads else (1, "", f"{args[1]} not found")
        if args[0] == "delete":
            self.workloads.discard(args[1])
        return 0, "", ""

    def sync_tailscale_secret(self) -> None:
        self.calls.append(("sync_tailscale_secret", None))

    def github(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        self.calls.append(("github", args))
        if args[:2] == ("api", "user"):
            return 0, "alice-gh\t4242\n", ""
        if args[:3] == ("api", "--method", "PUT"):
            name = args[3].rsplit("/", 1)[1]
            self.created_environments.append((name, stdin))
            self.environments[name] = len(json.loads(stdin or "{}").get("reviewers") or ())
            return 0, "{}", ""
        if args[:1] == ("api",) and args[1].endswith("/environments"):
            return 0, "".join(f"{n}\t{r}\n" for n, r in self.environments.items()), ""
        if args[:1] == ("api",):
            return 0, "true\n", ""
        if args[:2] == ("variable", "set"):
            self.variables[args[2]] = args[args.index("--body") + 1]
            return 0, "", ""
        if args[:2] == ("variable", "get"):
            return (0, self.variables[args[2]] + "\n", "") if args[2] in self.variables else (1, "", "not found")
        return 1, "", "unexpected"


def ready_state(**outputs: Any) -> InstallState:
    state = InstallState(project=PROJECT, environment="prod", region="us-central1", hostname="swarm.example.com",
                         release="2026.10.1", ready_at=1.0)
    state.outputs.update({"installer": INSTALLER, "image_digests": {"swarm-egress": DIGEST,
                                                                    "swarm-typesafe-fwd": DIGEST}})
    # The stage 0 and stage 2 records a ready install has, which every generated input needs (DAYONE-ENGINE-INPUTS).
    state.outputs.update({"alert_email": "alerts@example.com", "github_org": "example-org",
                          "org_domain": "example.com", "installer_cidr": "203.0.113.7/32",
                          "desktop_client_id": "123456789012-abc123def456.apps.googleusercontent.com"})
    state.outputs.update(outputs)
    return state


def flow(world: FakeWorld, state: InstallState | None = None, *, answers: Mapping[str, str] | None = None,
         typed: str = "", hidden: Callable[[str, str], int] | None = None) -> tuple[addons.Flow, io.StringIO]:
    out = io.StringIO()
    holder: dict[str, InstallState] = {"state": state or ready_state()}
    asked: list[str] = []

    def save(change: Callable[[InstallState], None]) -> None:
        change(holder["state"])
        the_flow.state = holder["state"]

    def ask(question: str, default: str | None) -> str:
        asked.append(question)
        for key, value in (answers or {}).items():
            if key in question:
                return value
        return default or ""

    clock = FakeClock()
    the_flow = addons.Flow(world=world, state=holder["state"], save=save,
                           events=Events(clock.now, out=io.StringIO()), clock=clock, out=out, ask=ask,
                           confirm=lambda prompt: typed, hidden_entry=hidden)
    return the_flow, out


CI_CHOICES = {"github_repository": "acme/swarm", "sandbox_repository": "acme/swarm-sandbox"}
GIVEN: dict[str, dict[str, str]] = {
    "tailscale": {"tailscale_egress_target": "decision.acme-tailnet.ts.net"},
    "ci": CI_CHOICES,
    "bright-data-egress": {"tenants": "acme"},
    "typesafe": {"tenants": "acme"},
}
#: What each add-on's check says once it is on.
ON_MESSAGE = {"bright-data-egress": "bright-data-egress is on for acme", "typesafe": "typesafe is on for acme"}

#: An apps/v1 workload's kind and name in a manifest.
WORKLOAD = re.compile(r"^kind: (Deployment|StatefulSet)\nmetadata:\n  name: ([a-z0-9-]+)$", re.MULTILINE)


def workload_manifest(file: str) -> str:
    """The workload document of kubernetes/swarm-system/<file>, as Terraform renders it."""
    text = (ROOT / "kubernetes" / "swarm-system" / file).read_text()
    found = WORKLOAD.findall(text)
    assert len(found) == 1, f"{file} declares {found}"
    return "kind: {}\nmetadata:\n  name: {}\n".format(*found[0])


def _variables(root: str) -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "terraform" / root).glob("*.tf"):
        found.update(re.findall(r'^variable\s+"([a-z0-9_]+)"', path.read_text(), re.MULTILINE))
    return found


# ---------------------------------------------------------------------------
# Core never depends on an add-on
# ---------------------------------------------------------------------------


def test_core_passes_with_every_addon_off() -> None:
    manifest = manifest_mod.load()
    by_id = manifest.by_id()
    # No install step satisfies an add-on's entry (stage 14 is reached with every add-on off).
    owned: list[str] = []
    pending = list(steps_mod.discover())
    while pending:
        step = pending.pop()
        pending.extend(steps_mod.triggered_of(step))
        owned.extend(e for e in step.satisfies if e in by_id and by_id[e].scope != "core")
    assert owned == []
    # The install's own inputs turn every add-on switch off, for every pass.
    state = ready_state(admin_groups=["admins@example.com"])
    switches: dict[str, set[str]] = {"infra": set(), "bootstrap": set()}
    for addon in addons.ADDONS.values():
        for phase in (addons.PREPARED, addons.ON):
            for root, overlay in addon.overlay(phase, GIVEN.get(addon.name, {})):
                switches[root].update(k for k, v in overlay.items() if v is True or isinstance(v, Mapping))
    for pass_name in tfvars.PASSES:
        if tfvars.ROOT_OF[pass_name] == "bootstrap":
            inputs = tfvars.bootstrap_inputs(state, pass_name=pass_name, installer=INSTALLER)
        else:
            inputs = tfvars.infra_inputs(state, pass_name=pass_name, installer=INSTALLER,
                                         versions=dict.fromkeys(tfvars.KEY_SWITCHES, True))
        on = [k for k in switches[tfvars.ROOT_OF[pass_name]] if inputs.get(k) not in (None, False, {})]
        assert on == [], f"{pass_name} turns add-on switches on: {on}"
    # An install that never enabled an add-on carries no add-on input at all.
    assert addons.inputs_for({}, "infra") == {} and addons.inputs_for({}, "bootstrap") == {}
    # The verb is its own module: install's steps never import it.
    assert "addon" in discover() and not any(s.id.startswith("addon.") for s in steps_mod.discover())


def test_every_switch_is_a_variable_of_its_root() -> None:
    known = {root: _variables(root) for root in ("infra", "bootstrap")}
    for addon in addons.ADDONS.values():
        for phase in (addons.PREPARED, addons.ON, addons.OFF):
            for root, overlay in addon.overlay(phase, GIVEN.get(addon.name, {})):
                missing = set(overlay) - known[root]
                assert not missing, f"{addon.name} {phase}: {missing} are not terraform/{root} variables"


def test_every_addon_names_its_own_manifest_entries() -> None:
    manifest = manifest_mod.load()
    by_id = manifest.by_id()
    assert set(addons.NAMES) == set(addons.ADDONS)
    for addon in addons.ADDONS.values():
        assert addon.satisfies, addon.name
        for entry in addon.satisfies:
            assert entry in by_id, f"{addon.name}: {entry} is in no manifest file"
            assert by_id[entry].scope == addon.scope, f"{entry} is {by_id[entry].scope}, not {addon.scope}"
    fragment = manifest_mod.parse_file(ROOT / "install" / "manifest.d" / "addon-1.yaml")
    for entry in fragment.entries:
        assert entry.step, f"{entry.id} names no step"
        name = entry.scope.removeprefix("addon:")
        assert entry.id in addons.ADDONS[name].satisfies


@pytest.mark.parametrize("name", addons.NAMES)
def test_each_addon_enables_end_to_end_on_fakes(name: str) -> None:
    world = FakeWorld()
    the_flow, out = flow(world)
    message = the_flow.enable(name, given=GIVEN.get(name, {}))
    assert message == ON_MESSAGE.get(name, f"{name} is on")
    record = addons.recorded(the_flow.state.outputs)[name]
    assert record["phase"] == addons.ON
    for spec in addons.ADDONS[name].secrets:
        assert spec.name in world.versions
    if addons.ADDONS[name].workload is not None:
        assert world.workloads, "the workload's manifest was applied"
    assert the_flow.check(addons.ADDONS[name]) == (MET, ON_MESSAGE.get(name, f"{name} is on"))


def test_enabling_one_addon_keeps_another_on() -> None:
    world = FakeWorld()
    the_flow, _ = flow(world)
    the_flow.enable("brave-search")
    the_flow.enable("bright-data-egress")
    root, last = world.applied[-1]
    assert root == "infra"
    assert last["search_tool_enabled"] is True and last["enable_browser_egress"] is True


def test_preflight_refuses_before_ready_and_on_a_bad_target() -> None:
    state = ready_state()
    state.ready_at = None
    the_flow, _ = flow(FakeWorld(), state)
    with pytest.raises(addons.AddonError, match="not READY"):
        the_flow.enable("brave-search")
    the_flow, _ = flow(FakeWorld())
    with pytest.raises(addons.AddonError, match="tailscale_egress_target"):
        the_flow.enable("tailscale", given={"tailscale_egress_target": "decision.example.com"})


# ---------------------------------------------------------------------------
# Disabling keeps the containers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", addons.NAMES)
def test_disable_keeps_secret_containers(name: str) -> None:
    world = FakeWorld()
    the_flow, out = flow(world)
    the_flow.enable(name, given=GIVEN.get(name, {}))
    the_flow.disable(name)
    assert ("delete_secret", addons.ADDONS[name].secrets[0].name if addons.ADDONS[name].secrets else "") \
        not in world.calls
    assert not [c for c in world.calls if c[0] == "delete_secret"]
    for spec in addons.ADDONS[name].secrets:
        assert spec.name in world.containers
    assert addons.recorded(the_flow.state.outputs)[name]["phase"] == addons.OFF
    # No off overlay turns off a switch that gates a container (typesafe's
    # enable_typesafe_forwarder creates swarm-typesafe-fwd-key).
    for root, overlay in addons.ADDONS[name].overlay(addons.OFF, GIVEN.get(name, {})):
        assert overlay.get("enable_typesafe_forwarder") is not False
    if addons.ADDONS[name].secrets:
        assert "--delete-secrets" in out.getvalue()


def test_disable_refuses_a_plan_that_deletes_a_container_or_the_deployer() -> None:
    for address, kind in (("google_secret_manager_secret.typesafe_fwd_key[0]", "google_secret_manager_secret"),
                          ("google_service_account.deployer[0]", "google_service_account")):
        world = FakeWorld(plan_deletes=[(address, kind)])
        the_flow, _ = flow(world)
        with pytest.raises(addons.AddonRefused, match="never deletes"):
            the_flow.disable("typesafe")
        assert world.applied == []


def test_deleting_containers_is_a_second_typed_step() -> None:
    world = FakeWorld()
    the_flow, _ = flow(world, typed="no")
    the_flow.enable("brave-search")
    with pytest.raises(addons.AddonRefused):
        the_flow.disable("brave-search", delete_secrets=True)
    assert "swarm-search-api-key" in world.containers
    the_flow.confirm = lambda prompt: PROJECT
    the_flow.disable("brave-search", delete_secrets=True)
    assert ("delete_secret", "swarm-search-api-key") in world.calls


# ---------------------------------------------------------------------------
# A secret's value never passes through the terminal
# ---------------------------------------------------------------------------


def _swarm_api_addon_secrets() -> dict[str, str]:
    source = (ROOT / "apps" / "swarm-api" / "swarm_api" / "setup_links.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "ADDON_SECRETS":
            return ast.literal_eval(node.value)
    raise AssertionError("swarm_api.setup_links has no ADDON_SECRETS")


def test_secret_only_through_link_or_hidden_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoStdin(io.StringIO):
        def read(self, *a: Any) -> str:
            raise AssertionError("an add-on read stdin")

        readline = read

    monkeypatch.setattr("sys.stdin", NoStdin())
    served = _swarm_api_addon_secrets()
    for addon in addons.ADDONS.values():
        for spec in addon.secrets:
            assert spec.how in (addons.LINK, addons.GENERATED)
            if spec.how == addons.LINK:
                # The console's link writes exactly this secret for this add-on.
                assert served.get(addon.name) == spec.name, (addon.name, spec.name)
    # No flag of the verb takes a value.
    parser = argparse.ArgumentParser()
    discover()["addon"].add_arguments(parser)
    text = " ".join(a.dest for sub in parser._subparsers._group_actions for p in sub.choices.values()
                    for a in p._actions)
    assert not re.search(r"value|token|key\b|password|credential|secret(?!s)", text), text
    # The link: minted for the installer, printed, polled; no value in the output.
    world = FakeWorld()
    the_flow, out = flow(world)
    the_flow.enable("brave-search")
    assert world.links == [(INSTALLER, "swarm-search-api-key")]
    assert "/admin/setup/link/" in out.getvalue() and "never typed here" in out.getvalue()
    # The hidden prompt, when asked for, instead of the link.
    world = FakeWorld()
    entered: list[str] = []
    def hidden(secret_id: str, prompt: str) -> int:
        entered.append(secret_id)
        world.versions.add(secret_id)  # secret_entry.enter_secret's addVersion
        return 1

    the_flow, _ = flow(world, hidden=hidden)
    the_flow.enable("brave-search", in_terminal=True)
    assert entered == ["swarm-search-api-key"] and world.links == []
    # A generated key is generated, never asked for.
    world = FakeWorld()
    the_flow, _ = flow(world)
    the_flow.enable("typesafe")
    assert world.added == ["swarm-typesafe-fwd-key"] and world.links == []


# ---------------------------------------------------------------------------
# The CI add-on
# ---------------------------------------------------------------------------


def test_ci_addon_makes_cloud_build_sa_not_default_sa() -> None:
    world = FakeWorld()
    the_flow, _ = flow(world)
    the_flow.enable("ci", given=CI_CHOICES)
    assert ("create_service_account", addons.CI_BUILD_ACCOUNT) in world.calls
    assert "cloudbuild.googleapis.com" in world.services
    own = addons.build_account(PROJECT)
    assert addons.build_account_problem(own) is None
    for default in ("123456789-compute@developer.gserviceaccount.com", "123456789@cloudbuild.gserviceaccount.com"):
        assert addons.build_account_problem(default), default
    assert addons.build_account_problem("") is not None


def test_ci_addon_turns_on_acceptance_fixture() -> None:
    assert "enable_acceptance_fixture" in _variables("infra")
    world = FakeWorld()
    the_flow, _ = flow(world)
    the_flow.enable("ci", given=CI_CHOICES)
    assert addons.inputs_for(the_flow.state.outputs, "infra")["enable_acceptance_fixture"] is True
    assert addons.inputs_for(the_flow.state.outputs, "bootstrap") == {"enable_github_wif": True,
                                                                      "github_repository": "acme/swarm"}
    assert world.variables == {"SWARM_CI_RUNNER_LABEL": "swarm-ci",
                               "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL": "https://github.com/acme/swarm-sandbox.git"}
    the_flow.disable("ci")
    assert addons.inputs_for(the_flow.state.outputs, "infra")["enable_acceptance_fixture"] is False
    # No other add-on touches it.
    for name in addons.NAMES:
        if name != "ci":
            for _, overlay in addons.ADDONS[name].overlay(addons.ON, GIVEN.get(name, {})):
                assert "enable_acceptance_fixture" not in overlay


def test_ci_addon_refuses_the_deploying_repository_as_sandbox_and_open_prod() -> None:
    the_flow, _ = flow(FakeWorld())
    with pytest.raises(addons.AddonError, match="#628"):
        the_flow.enable("ci", given={"github_repository": "acme/swarm", "sandbox_repository": "acme/swarm"})

    class OpenProd(FakeWorld):
        def github(self, *args: str) -> tuple[int, str, str]:
            if args[:1] == ("api",) and args[1].endswith("/environments"):
                return 0, "dev\t0\nprod\t0\n", ""
            return super().github(*args)

    the_flow, _ = flow(OpenProd())
    with pytest.raises(addons.AddonError, match="lesson 25"):
        the_flow.enable("ci", given=CI_CHOICES)


#: actions/create-github-app-token@v2's inputs (its action.yml, read
#: 2026-10-30), without the `permission-*` ones: those are checked against
#: GitHub's own permission keys below, which is what lesson 45 was about.
APP_TOKEN_INPUTS = {"v2": {"app-id", "private-key", "owner", "repositories", "skip-token-revoke",
                           "github-api-url"}}
#: GitHub App permission keys (the REST API's app-permissions object, plus
#: `actions_variables`, the key #2220 established on 2026-10-27).
GITHUB_PERMISSIONS = {
    "actions", "actions_variables", "administration", "checks", "codespaces", "contents", "dependabot_secrets",
    "deployments", "email_addresses", "environments", "followers", "git_ssh_keys", "gpg_keys",
    "interaction_limits", "issues", "members", "metadata", "organization_administration",
    "organization_announcement_banners", "organization_copilot_seat_management", "organization_custom_org_roles",
    "organization_custom_properties", "organization_custom_roles", "organization_events", "organization_hooks",
    "organization_packages", "organization_personal_access_token_requests", "organization_personal_access_tokens",
    "organization_plan", "organization_projects", "organization_secrets", "organization_self_hosted_runners",
    "organization_user_blocking", "packages", "pages", "profile", "pull_requests",
    "repository_custom_properties", "repository_hooks", "repository_projects", "secret_scanning_alerts",
    "secrets", "security_events", "single_file", "starring", "statuses", "team_discussions",
    "vulnerability_alerts", "workflows",
}


def app_token_problems(documents: Mapping[str, Any]) -> list[str]:
    """Every `with:` key of an app-token step that the pinned action does not read."""
    problems: list[str] = []
    for where, doc in documents.items():
        jobs = (doc or {}).get("jobs") or {}
        step_lists = [job.get("steps") or [] for job in jobs.values() if isinstance(job, Mapping)]
        step_lists.append(((doc or {}).get("runs") or {}).get("steps") or [])
        for steps in step_lists:
            for step in steps:
                uses = str((step or {}).get("uses") or "")
                if not uses.startswith("actions/create-github-app-token@"):
                    continue
                pin = uses.split("@", 1)[1]
                if pin not in APP_TOKEN_INPUTS:
                    problems.append(f"{where}: {uses} is pinned to a version whose inputs this test does not hold")
                    continue
                for key in (step.get("with") or {}):
                    if key.startswith("permission-"):
                        if key[len("permission-"):].replace("-", "_") not in GITHUB_PERMISSIONS:
                            problems.append(f"{where}: {key} is not a GitHub permission key (lesson 45)")
                    elif key not in APP_TOKEN_INPUTS[pin]:
                        problems.append(f"{where}: {key} is not an input of {uses}")
    return problems


def test_ci_addon_app_token_inputs_declared_by_action() -> None:
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted(
        (ROOT / ".github" / "actions").glob("*/action.yml"))
    documents = {str(p.relative_to(ROOT)): yaml.safe_load(p.read_text()) for p in files}
    steps = [s for d in documents.values() for j in ((d or {}).get("jobs") or {}).values()
             for s in (j.get("steps") or []) if "create-github-app-token" in str((s or {}).get("uses"))]
    assert steps, "no app-token step found: the test read nothing"
    assert app_token_problems(documents) == []
    # Lesson 45's own defect is caught.
    bad = {"w.yml": {"jobs": {"j": {"steps": [{"uses": "actions/create-github-app-token@v2",
                                               "with": {"app-id": "1", "permission-variables": "write"}}]}}}}
    assert app_token_problems(bad) == ["w.yml: permission-variables is not a GitHub permission key (lesson 45)"]


# ---------------------------------------------------------------------------
# The acceptance sandbox is the CI add-on's choice
# ---------------------------------------------------------------------------


def test_acceptance_sandbox_comes_from_the_ci_addon_variable() -> None:
    config = ROOT / "scripts" / "acceptance" / "config.sh"
    script = (f"source {shlex.quote(str(config))}\n"
              'printf "%s\\n%s\\n" "${ACC_REPOSITORY_URL}" "${ACC_GITHUB_REPO}"\n')
    env = {"PATH": "/usr/bin:/bin",
           "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL": "https://github.com/acme/swarm-sandbox.git"}
    done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["https://github.com/acme/swarm-sandbox.git", "acme/swarm-sandbox"]


# ---------------------------------------------------------------------------
# Finding 16: an add-on's kubectl after the closing pass
# ---------------------------------------------------------------------------


class ClosingWorld(FakeWorld):
    """A world whose `kube` reopened the control plane: the flow closes it at the end."""

    def __init__(self, *, left: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.closed = 0
        self.left = left

    def close_reach(self) -> str | None:
        self.closed += 1
        return self.left


def test_enable_and_disable_close_the_reach_when_they_finish() -> None:
    world = ClosingWorld()
    the_flow, _ = flow(world)
    the_flow.enable("brave-search")
    assert world.closed == 1
    the_flow.disable("brave-search")
    assert world.closed == 2


def test_a_failed_enable_still_closes_the_reach_and_says_what_is_left() -> None:
    world = ClosingWorld(left="swarm addon: the closing pass did not apply")
    state = ready_state()
    state.ready_at = None
    the_flow, out = flow(world, state)
    with pytest.raises(addons.AddonError, match="not READY"):
        the_flow.enable("brave-search")
    assert world.closed == 1
    assert "the closing pass did not apply" in out.getvalue()


def _real_world(state: InstallState) -> addons.RealWorld:
    from types import SimpleNamespace

    clock = FakeClock()
    ctx = SimpleNamespace(project=PROJECT, state=state, outputs=state.outputs, holder="admin@example.com",
                          environment=state.environment, events=Events(clock.now, out=io.StringIO()),
                          bucket=object(), dry_run=False)
    return addons.RealWorld(project=PROJECT, ctx=ctx, gcp=object(), deps=object())


def test_real_world_kube_reopens_through_an_engine_shaped_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm_install.steps import cluster, infra

    seen: list[Any] = []

    class Kube:
        def credentials(self) -> None:
            pass

        def __call__(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
            return 0, "", ""

    def kube_for(ctx: Any) -> Kube:
        seen.append(ctx)
        return Kube()

    monkeypatch.setattr(cluster, "kube_for", kube_for)
    world = _real_world(ready_state())
    assert world.close_reach() is None, "no kubectl ran: nothing was reopened"
    world.kube("get", "namespaces")
    assert isinstance(seen[0], infra.VerbContext) and seen[0].engine.store.bucket is world.ctx.bucket
    closed: list[Any] = []
    monkeypatch.setattr(infra.REACH, "close_after", lambda ctx, verb: closed.append((ctx, verb)))
    world.close_reach()
    assert closed == [(seen[0], "swarm addon")]


def test_real_world_apply_keeps_the_reopened_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm_install.steps import bootstrap, infra

    state = ready_state()
    state.outputs.update({bootstrap.applied_key("infra-4"): "x", bootstrap.applied_key("infra-close"): "y",
                          infra.REOPENED: "203.0.113.7/32"})
    generated: list[str] = []

    class Stop(Exception):
        pass

    def generate(ctx: Any, pass_name: str, gcp: Any) -> dict:
        generated.append(pass_name)
        raise Stop

    monkeypatch.setattr(bootstrap, "generate", generate)
    world = _real_world(state)
    with pytest.raises(Stop):
        world.apply("infra", {}, guard=lambda plan: None)
    assert generated == ["infra-4"], "the close's inputs would remove the address kubectl still needs"
    state.outputs[infra.REOPENED] = None
    with pytest.raises(Stop):
        world.apply("infra", {}, guard=lambda plan: None)
    assert generated[-1] == "infra-close"


# ---------------------------------------------------------------------------
# DAYONE-ADDON-2 (audit 4e F8, F10, F11): every add-on finishes, says on only
# when the feature works, and keeps the person in the terminal
# ---------------------------------------------------------------------------

#: The manifest each workload add-on applies (Terraform renders these files).
WORKLOAD_FILES = {"bright-data-egress": "swarm-egress.yaml", "tailscale": "tailscale-egress.yaml",
                  "typesafe": "swarm-typesafe-fwd.yaml"}


@pytest.mark.parametrize("name", sorted(WORKLOAD_FILES))
def test_the_workload_waited_on_is_the_one_the_manifest_declares(name: str) -> None:
    """F8: bright-data-egress waited on deployment/swarm-egress (a StatefulSet) and
    tailscale on deployment/tailscale-egress (decision-api-egress) for ever."""
    kind, workload = WORKLOAD.findall(workload_manifest(WORKLOAD_FILES[name]))[0]
    assert addons.ADDONS[name].workload is not None
    assert addons.ADDONS[name].workload[0] == f"{kind.lower()}/{workload}"
    world = FakeWorld()
    the_flow, _ = flow(world)
    the_flow.enable(name, given=GIVEN.get(name, {}))
    assert f"{kind.lower()}/{workload}" in world.workloads
    # Disabling deletes the real workload, not a name that was never there.
    the_flow.disable(name)
    assert f"{kind.lower()}/{workload}" not in world.workloads


def test_brave_search_is_not_on_while_swarm_api_cannot_read_its_key() -> None:
    """F10: the check looked only for a version, so it said on while every search
    answered search_unconfigured."""
    world = FakeWorld()
    world.readers = set()
    the_flow, _ = flow(world)
    with pytest.raises(addons.AddonError, match="swarm-api .* cannot read swarm-search-api-key"):
        the_flow.enable("brave-search")
    status, why = the_flow.check(addons.ADDONS["brave-search"])
    assert status != MET and "search_unconfigured" in why
    world.readers = {f"serviceAccount:swarm-api@{PROJECT}.iam.gserviceaccount.com"}
    assert the_flow.check(addons.ADDONS["brave-search"]) == (MET, "brave-search is on")


def test_tailscale_turns_the_decision_api_url_on_and_names_the_right_scope() -> None:
    """F10: without internal_decision_api_enabled swarm-api never gets INTERNAL_DECISION_API_URL."""
    world = FakeWorld()
    the_flow, out = flow(world)
    the_flow.enable("tailscale", given=GIVEN["tailscale"])
    on = addons.inputs_for(the_flow.state.outputs, "infra")
    assert on["internal_decision_api_enabled"] is True and on["enable_tailscale_egress"] is True
    the_flow.disable("tailscale")
    off = addons.inputs_for(the_flow.state.outputs, "infra")
    assert off["internal_decision_api_enabled"] is False
    # docs/runbooks/tailscale-egress.md: Auth Keys: write, and the tag has an owner.
    page = addons.ADDONS["tailscale"].page
    assert page is not None
    assert "Auth Keys: write" in page.what and "tagOwners" in page.what and "Devices" not in page.what


@pytest.mark.parametrize(("name", "setting"), [("bright-data-egress", "browser_egress"),
                                               ("typesafe", "typesafe_forwarder")])
def test_routing_addons_render_the_tenants_they_serve(name: str, setting: str) -> None:
    """F10: the overlays set only the service switch, so no tenant was ever routed."""
    world = FakeWorld()
    the_flow, _ = flow(world, answers={"tenants": " acme, beta "})
    message = the_flow.enable(name)
    inputs = addons.inputs_for(the_flow.state.outputs, "infra")
    assert inputs[setting]["enabled"] is True and inputs[setting]["tenants"] == ["acme", "beta"]
    assert message == f"{name} is on for acme, beta"
    # The tenants are an optional choice: none is a valid answer, and the check says so.
    world = FakeWorld()
    the_flow, _ = flow(world)
    message = the_flow.enable(name)
    inputs = addons.inputs_for(the_flow.state.outputs, "infra")
    assert inputs[setting]["enabled"] is False and inputs[setting]["tenants"] == []
    assert "routes no tenant" in message and "--tenants" in message
    # A tenant id is swarm_common's shape.
    the_flow, _ = flow(FakeWorld())
    with pytest.raises(addons.AddonError, match="tenants"):
        the_flow.enable(name, given={"tenants": "Not A Tenant"})
    # The verb takes them as a flag.
    parser = argparse.ArgumentParser()
    discover()["addon"].add_arguments(parser)
    args = parser.parse_args(["enable", name, "--project", PROJECT, "--tenants", "acme,beta"])
    from swarm_install.verbs import addon as addon_verb
    assert addon_verb._given(args) == {"tenants": "acme,beta"}


def test_a_vendor_page_is_opened_in_the_browser() -> None:
    world = FakeWorld()
    the_flow, _ = flow(world)
    opened: list[tuple[str, str]] = []
    the_flow.open_page = lambda url, why: opened.append((url, why))
    the_flow.enable("brave-search")
    assert opened and opened[0][0] == "https://api-dashboard.search.brave.com/app/keys"
    assert "API key" in opened[0][1]


def test_ci_creates_the_release_environments_after_a_typed_approval() -> None:
    """F11: the person was sent to github.com/<repo>/settings/environments by hand."""
    world = FakeWorld()
    world.environments = {}
    the_flow, _ = flow(world, typed="no")
    with pytest.raises(addons.AddonRefused, match="not created"):
        the_flow.enable("ci", given=CI_CHOICES)
    assert world.created_environments == []
    world = FakeWorld()
    world.environments = {"dev": 0, "prod": 0}
    the_flow, _ = flow(world, typed="acme/swarm")
    assert the_flow.enable("ci", given=CI_CHOICES) == "ci is on"
    assert world.created_environments == [("prod", '{"reviewers": [{"type": "User", "id": 4242}]}')]
    world = FakeWorld()
    world.environments = {}
    the_flow, _ = flow(world, typed="acme/swarm")
    the_flow.enable("ci", given=CI_CHOICES)
    assert [n for n, _ in world.created_environments] == ["dev", "prod"]
    assert world.environments == {"dev": 0, "prod": 1}


def test_tailscale_secret_sync_finds_the_installers_own_gcloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """F11: shutil.which missed ~/.cache/swarmcloud/tools/bin, on PATH only inside swarm install."""
    from swarm_install.steps import preflight

    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(preflight, "find_gcloud", lambda: "/home/alice/.cache/swarmcloud/tools/bin/gcloud")
    ran: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        out_file = next(a for a in argv if a.startswith("--out-file=")).split("=", 1)[1]
        Path(out_file).write_bytes(b"tskey-client-" + secrets.token_hex(16).encode())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(addons.subprocess, "run", run)
    world = _real_world(ready_state())
    kubed: list[tuple[str, ...]] = []
    world._kube = lambda *args, stdin=None: (kubed.append(args), (0, "rendered", ""))[1]
    world.sync_tailscale_secret()
    assert ran[0][0] == "/home/alice/.cache/swarmcloud/tools/bin/gcloud"
    assert kubed[-1][:1] == ("apply",)


def test_github_uses_the_pinned_gh_and_signs_in_through_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """F11: with no gh on PATH the person was told to install it and run gh auth login."""
    from swarm_install import tools

    monkeypatch.setenv("PATH", "/nonexistent")
    ensured: list[str] = []
    monkeypatch.setattr(tools, "ensure", lambda name, **kw: (ensured.append(name), Path("/cache/gh-2/gh"))[1])
    answers = [subprocess.CompletedProcess([], 4, "", "gh: To get started with GitHub CLI, please run: gh auth login"),
               subprocess.CompletedProcess([], 0, "true\n", "")]
    runs: list[tuple[list[str], str | None]] = []
    logins: list[list[str]] = []
    world = _real_world(ready_state())
    monkeypatch.setattr(world, "_run", lambda argv, stdin: (runs.append((argv, stdin)), answers.pop(0))[1])
    monkeypatch.setattr(world, "_login", lambda argv: (logins.append(argv), 0)[1])
    assert world.github("api", "repos/acme/swarm-sandbox", "--jq", ".private") == (0, "true\n", "")
    assert ensured == ["gh"]
    assert [argv[0] for argv, _ in runs] == ["/cache/gh-2/gh", "/cache/gh-2/gh"]
    assert logins == [["/cache/gh-2/gh", "auth", "login", "--web", "--hostname", "github.com",
                       "--git-protocol", "https"]]
    # A second "not signed in" does not loop through the browser again.
    answers.append(subprocess.CompletedProcess([], 4, "", "auth"))
    assert world.github("api", "user")[0] == 4 and len(logins) == 1


def test_gh_is_pinned_for_every_platform() -> None:
    from swarm_install import tools

    for system in (("linux", "amd64"), ("linux", "arm64"), ("darwin", "amd64"), ("darwin", "arm64")):
        pin = tools.pin_for("gh", system)
        assert pin.version == tools.GH_VERSION and len(pin.sha256) == 64
        assert pin.url.startswith(f"https://github.com/cli/cli/releases/download/v{tools.GH_VERSION}/")
        assert pin.member is not None and pin.member.endswith("/bin/gh") and pin.member.split("/")[0] in pin.url
