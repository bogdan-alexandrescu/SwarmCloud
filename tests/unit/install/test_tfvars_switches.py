"""Every switch the manifest counts as done is a switch the install turned on
(DAYONE-WORKSPACE-SWITCHES, audit 3 findings A1 and A10).

The engine counts a manifest `switch.*` entry met once the step its `step:`
names is done (engine.py `_owning_step_done`). So when a Terraform pass owns
a switch, that pass's generated inputs must set it true, or the manifest
reports as done a switch a fresh install leaves at its default, false: the
workspace switches were exactly that (no swarm-workspace-deployer, so
`cluster.policies` stopped at stage 10). These render the real tfvars, in
team mode and in one-person mode, for every switch entry whose owning step is
a Terraform pass, read the value the pass applies (its rendered input, else
the variable's own default) and hold it true from that pass on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_install import manifest as manifest_mod
from swarm_install import sizing as sizes
from swarm_install import tfvars
from swarm_install.steps import discover
from swarm_install.steps.bootstrap import Deps
from swarm_install.steps.github_switches import Pass3Step, Pass4Step

from .test_terraform_runner import install_state
from .test_tfvars import DIGEST, EVERY_ADDON_ON, EVERY_VERSION

REPO = Path(__file__).resolve().parents[3]
INSTALLER = "admin@example.com"

#: Switches an owning pass leaves off on purpose, each because turning it on
#: is the owner's decision (asked in this lane's questions.json). The guard
#: holds them off too, so neither a silent flip nor a silent drop passes.
OWNER_CHOICES = {
    # Widens who may enter a tenant (invariant 9); dev turned it on as a
    # dev-then-prod rollout, never by default.
    "switch.platform_admin_reach": "platform_admin_reach",
    # The forge broker is the owner's rollout step (#1041); dev keeps it off.
    "switch.forge_broker": "forge_broker",
    # Off until the owner's O1 step 3 proves IAP evaluates the attribute;
    # bounds K1 only, which a fresh install does not grant.
    "switch.front_door_admit_role_bound": "front_door_admit_role_bound",
}
#: Not a bool: a map of refusal codes, every one shipped off (report-only).
NOT_A_BOOL = {"switch.api_refusals"}
#: Owned by bootstrap pass 1 in the BOOT-2 fragment, but its grant names
#: swarm-quota-broker by a string in platform_roles.tf, so the renderer turns
#: it on from bootstrap pass 2, once the account exists. The pass it does
#: turn on in is held here.
TURNS_ON_LATER = {"switch.grant_broker_secret_lister": "bootstrap-2"}


def _pass_of_step() -> dict[str, str]:
    """The Terraform pass each step applies, from the steps themselves."""
    found = {s.id: s.pass_name for s in discover() if getattr(s, "pass_name", "")}
    for cls in (Pass3Step, Pass4Step):
        step = cls(Deps(gcp=lambda: None))
        found[step.id] = step.pass_name
    return found


def _owned_switches() -> list[tuple[str, str, str]]:
    """(entry id, owning pass, scope) for every switch entry a Terraform pass owns."""
    passes = _pass_of_step()
    return [(e.id, passes[e.step], e.scope) for e in manifest_mod.load().entries
            if e.id.startswith("switch.") and e.step in passes and e.id not in NOT_A_BOOL]


def _variable(entry_id: str) -> str:
    # switch.<variable> or switch.<variable>-<field> (an object's field).
    return entry_id.removeprefix("switch.").replace("-", ".", 1)


def _default(root: str, name: str) -> bool:
    for path in (REPO / "terraform" / root).glob("*.tf"):
        found = re.search(rf'^variable\s+"{name}"\s*\{{(.*?)^\}}', path.read_text(), flags=re.M | re.S)
        if found:
            default = re.search(r"^\s*default\s*=\s*(true|false)\s*$", found.group(1), flags=re.M)
            return bool(default) and default.group(1) == "true"
    raise AssertionError(f"terraform/{root} declares no {name}")


def _applied(inputs: dict, root: str, variable: str) -> bool:
    """The value the pass applies: its rendered input, else the variable's default."""
    name, _, field = variable.partition(".")
    if name not in inputs:
        return _default(root, name) if not field else False
    value = inputs[name]
    if field:
        return isinstance(value, dict) and value.get(field) is True
    return value is True


def _state(mode: str, members: int, *, addons: dict | None = None):
    # A state as the install holds it when each pass runs for real: every
    # release image copied (stage 4), every key with a version (stage 6), the
    # console's GitHub App recorded (stage 9).
    digests = {n: DIGEST for n in ("swarm-api", "workspace-apply", "swarm-graph", "swarm-live")}
    outputs = {"image_digests": digests, "github_app": {"id": "42", "slug": "s", "client_id": "c"},
               "sizing": dict(sizes.propose(mode, members))}
    if addons:
        outputs["addons"] = addons
    return install_state(**outputs)


def _render(state, pass_name: str) -> dict:
    if tfvars.ROOT_OF[pass_name] == "bootstrap":
        return tfvars.bootstrap_inputs(state, pass_name=pass_name, installer=INSTALLER)
    return tfvars.infra_inputs(state, pass_name=pass_name, installer=INSTALLER, versions=EVERY_VERSION)


MODES = ((sizes.TEAM, 5), (sizes.INDIVIDUAL, 1))


def test_the_guard_reads_the_workspace_switches() -> None:
    # The guard is only as good as what it visits: the switches audit 3 named are among them.
    owned = {entry_id: pass_name for entry_id, pass_name, _ in _owned_switches()}
    for entry_id in ("switch.enable_workspace_deployer", "switch.enable_forge_user_slots",
                     "switch.enable_workspace_kinds", "switch.enable_provider_keys",
                     "switch.enable_workspace_secrets", "switch.enable_browser_logins",
                     "switch.enable_mcp_credentials", "switch.enable_platform_secret_status",
                     "switch.workspace_apply_publish", *OWNER_CHOICES):
        assert entry_id in owned, f"{entry_id} is not owned by a Terraform pass"
    assert len(owned) >= 30, len(owned)


@pytest.mark.parametrize("mode,members", MODES, ids=[m for m, _ in MODES])
def test_every_pass_owned_switch_is_on_from_its_pass(mode: str, members: int) -> None:
    off: list[str] = []
    for entry_id, owner, scope in _owned_switches():
        state = _state(mode, members, addons=EVERY_ADDON_ON if scope.startswith("addon:") else None)
        root = tfvars.ROOT_OF[owner]
        first = TURNS_ON_LATER.get(entry_id, owner)
        for pass_name in tfvars.PASSES:
            if tfvars.ROOT_OF[pass_name] != root or not tfvars.after(pass_name, first):
                continue
            applied = _applied(_render(state, pass_name), root, _variable(entry_id))
            if entry_id in OWNER_CHOICES:
                if applied:
                    off.append(f"{entry_id} is on in {pass_name}, but turning it on is the owner's decision")
            elif not applied:
                off.append(f"{entry_id} (owned by {owner}) is off in {pass_name}")
    assert not off, "\n".join(off)


@pytest.mark.parametrize("mode,members", MODES, ids=[m for m, _ in MODES])
def test_workspace_switches_wait_for_what_they_need(mode: str, members: int) -> None:
    state = _state(mode, members)
    # Bootstrap pass 1: swarm-api and the image's job are made in this same
    # apply or later, and the grants name swarm-api by a string.
    first = _render(state, "bootstrap-1")
    for switch in ("enable_workspace_deployer", "enable_forge_user_slots", "enable_workspace_kinds",
                   "enable_provider_keys", "enable_workspace_secrets", "enable_browser_logins",
                   "enable_mcp_credentials", "enable_platform_secret_status"):
        assert first.get(switch) is not True, f"{switch} is on in bootstrap-1"
    second = _render(state, "bootstrap-2")
    assert second["enable_workspace_deployer"] is True
    assert second["workspace_apply_image"].endswith(f"/workspace-apply@{DIGEST}")
    # Publishing waits for the job bootstrap pass 2 makes: off in the infra passes before it.
    for pass_name in ("infra-1", "infra-2"):
        assert _render(state, pass_name).get("workspace_apply_publish") is not True, pass_name
    for pass_name in ("infra-3", "infra-4", "infra-close"):
        assert _render(state, pass_name)["workspace_apply_publish"] is True, pass_name


def test_no_workspace_apply_image_leaves_the_deployer_off() -> None:
    # workspace_deployer.tf refuses enable_workspace_deployer without the image;
    # a state with no digest for it renders the deployer, its kinds and publishing off.
    state = install_state(github_app={"id": "42", "slug": "s"},
                          image_digests={"swarm-api": DIGEST})
    second = _render(state, "bootstrap-2")
    assert second["enable_workspace_deployer"] is False and second["enable_workspace_kinds"] is False
    assert "workspace_apply_image" not in second
    assert _render(state, "infra-3")["workspace_apply_publish"] is False


# -- A10: the first bootstrap apply never grants on an account before creating it --------------


def _block(text: str, header: str) -> str:
    # To the block's closing brace at the start of a line: an error message may hold a lone `{`.
    found = re.search(re.escape(header) + r"\s*\{.*?^\}", text, flags=re.M | re.S)
    assert found, f"no block: {header}"
    return found.group(0)


@pytest.mark.parametrize("path,header", [
    # Grants serviceAccountAdmin on each platform account by a literal resource name.
    ("deployer_service_accounts.tf", 'resource "google_service_account_iam_member" "deployer_admin"'),
    # Names serviceAccount:swarm-api@... by a string in the account's policy.
    ("qa_identity.tf", 'resource "google_service_account_iam_policy" "qa_identity"'),
])
def test_grants_naming_platform_accounts_wait_for_them(path: str, header: str) -> None:
    block = _block((REPO / "terraform" / "bootstrap" / path).read_text(), header)
    deps = re.search(r"depends_on\s*=\s*\[([^\]]*)\]", block)
    assert deps, f"{header} declares no depends_on, so a first apply can grant before the account exists"
    assert "google_service_account.platform" in deps.group(1), header
