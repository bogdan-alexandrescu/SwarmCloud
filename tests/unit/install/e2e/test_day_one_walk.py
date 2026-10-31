"""`swarm install` from stage 0 to READY, offline (lane DAYONE-E2E-CLI, part of #2688)."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_install import cli

from . import cluster_fake, console, deployment, effects, google, harness, manifest_met, services, swarm_api, terraform_fake
from .person import Person
from .world import World

PROJECT = "acme-swarm-1"
HOSTNAME = "swarm.acme.example"
RELEASE = "2026.11.1"


def walk(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys, assume: tuple[str, ...] = ()) -> int:
    harness.install(world, monkeypatch, tmp_path)
    for finding in assume:
        ASSUMED_FIXED[finding](monkeypatch)
    world.cloud = google.Cloud()
    world.cloud.publish(RELEASE)
    google.register(world, world.cloud)
    services.commands(world)
    services.renderers(world)
    services.unhandled_kube(world)
    services.swarm_cli(world)
    deployment.attach(world)
    services.release_tools(world)
    terraform_fake.attach(world)
    cluster_fake.attach(world)
    effects.attach(world, world.cloud)
    swarm_api.register(world, world.cloud)
    code = cli.main(["install", "--project", PROJECT, "--domain", HOSTNAME, "--region", "europe-west1"])
    out = capsys.readouterr()
    world.said.append(out.out + out.err)
    return code


def _f1_workspace_deployer(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1's proposed fix: bootstrap pass 2 renders enable_workspace_deployer
    (with the forge user slots its role needs), as the manifest says it does."""
    from swarm_install import tfvars

    real = tfvars.bootstrap_inputs

    def inputs(state, *, pass_name: str, installer: str):
        found = real(state, pass_name=pass_name, installer=installer)
        if tfvars.after(pass_name, "bootstrap-2"):
            found.setdefault("enable_workspace_deployer", True)
            found.setdefault("enable_forge_user_slots", True)
        return found

    monkeypatch.setattr(tfvars, "bootstrap_inputs", inputs)


def _f2_egress_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2's proposed fix: name the egress range's source in the shape
    render.py admits, run/<project>/<region>/swarm-api/<subnetwork>."""
    from swarm_install.steps import cluster

    def rendered(self, ctx):
        cidr = self.egress_cidr(ctx)
        source = f"run/{ctx.project}/{ctx.state.region}/swarm-api/swarm-subnet-{ctx.state.region}"
        return self.render(ctx, "swarm-system", f"--swarm-api-egress-cidr={cidr}",
                           f"--swarm-api-egress-source={source}")

    monkeypatch.setattr(cluster.SwarmSystemStep, "rendered", rendered)


#: An open finding's proposed fix, applied only where a test names it, so the
#: walk reaches the stages after it (e2e-findings.md). Never the product's code.
ASSUMED_FIXED = {"F1": _f1_workspace_deployer, "F2": _f2_egress_source}


def person(mode: str, members: int = 5) -> Person:
    """The person installing: their answers, nothing more (every value
    Google can tell the installer is read, not asked)."""
    return Person(answers=[
        (r"Type the project id \(acme-swarm-1\) to approve", PROJECT),
        (r"^Email address that receives", "oncall@acme.example"),
        (r"GitHub organisation", "acme"),
        (r"Billing account", "1"),
        (r"Create it under", "1"),
        (r"Who is this SwarmCloud for", "2" if mode == "team" else "1"),
        (r"How many people", str(members)),
        (r"Press Enter to accept", ""),
        (r"Workspace or Cloud Identity domain", "acme.example"),
        (r"Type the region again", "europe-west1"),
        (r"Desktop client.s id", "1234567890-e2e.apps.googleusercontent.com"),
        (r"Type ASSIGN to approve", "ASSIGN"),
        (r"Create .* through the Groups API", "y"),
    ], confirms=[], pages=[
        (r"toolbox\.googleapps\.com/apps/dig", set_a_record),
        (r"/admin/setup$", console.admin_setup),
    ])


def set_a_record(url: str, world: World) -> None:
    """At their own registrar, the person points the hostname at the address the page names."""
    reserved = [a["address"] for a in world.cloud.addresses.values()]
    world.addresses[HOSTNAME] = set(reserved[:1])


REPO = Path(__file__).resolve().parents[4]

#: What each open finding prints when the walk stops on it (e2e-findings.md).
STOPS = {
    "F1": "swarm-workspace-deployer@acme-swarm-1.iam.gserviceaccount.com has no uniqueId in IAM",
    "F2": "SWARM_API_EGRESS_SOURCE='install-state/",
}
MODES = [pytest.param("team", id="team-of-5"), pytest.param("individual", id="one-person")]


def walked(mode: str, monkeypatch, tmp_path, capsys, assume: tuple[str, ...] = ()) -> tuple[World, int]:
    world = World()
    world.person = person(mode)
    return world, walk(world, monkeypatch, tmp_path, capsys, assume)


@pytest.mark.parametrize("mode", MODES)
def test_the_walk_reaches_ready(mode, monkeypatch, tmp_path, capsys):
    """The product as it is, from stage 0: today it stops on F1 (and F2 after
    it), so this is an xfail that flips red, by design, once both are fixed."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys)
    said = world.said[-1]
    if code != 0:
        open_findings = [f for f, text in STOPS.items() if text in said]
        assert open_findings, f"the walk stopped on something no finding names:\n{said[-3000:]}"
        pytest.xfail(f"stops on {open_findings[0]} (e2e-findings.md): {STOPS[open_findings[0]]}")
    pytest.fail("READY without F1 and F2's patches: both are fixed, so drop them from ASSUMED_FIXED and "
                "STOPS and make this test assert READY (strict: a fixed finding turns its xfail red)")


@pytest.mark.parametrize("mode", MODES)
def test_the_walk_reaches_ready_with_the_open_findings_fixed(mode, monkeypatch, tmp_path, capsys):
    """F1 and F2's proposed fixes applied in the test only: every later stage
    still walks, every core switch and API is held by the fakes, and the
    person is never sent to run anything outside `swarm install`."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys, ("F1", "F2"))
    said = world.said[-1]
    assert code == 0, said[-3000:]
    assert "READY: SwarmCloud answers at https://" + HOSTNAME in said
    assert world.unhandled == []
    assert world.person.unscripted == []
    assert world.person.asked_to_run_elsewhere() == []
    asked = " ".join(prompt for _, prompt in world.person.asked)
    bootstrap = [inputs for root, inputs, _ in world.tf_applies if root == "bootstrap"][-1]
    members = set(bootstrap["frontend_iap_members"])
    if mode == "team":  # decision 1: the organisation's domain, the installer, the Desktop client
        assert "How many people" in asked
        assert {"domain:acme.example", f"user:{world.installer}"} <= members
    else:  # decision 7: one person, so IAP admits only the installer, and there is no team group
        assert "How many people" not in asked
        assert not any(m.startswith(("domain:", "group:")) for m in members), members
        assert f"user:{world.installer}" in members


#: Core switches the manifest counts done that no pass renders on (A1's class),
#: each under the finding that records it (e2e-findings.md).
UNRENDERED = {
    "F3": {"switch.enable_browser_logins", "switch.enable_mcp_credentials", "switch.enable_platform_secret_status",
           "switch.enable_provider_keys", "switch.enable_workspace_kinds", "switch.enable_workspace_secrets"},
    "F4": {"switch.forge_broker", "switch.mcp_servers_enabled", "switch.platform_admin_reach",
           "switch.workspace_apply_publish"},
}


@pytest.mark.parametrize("mode", MODES)
def test_every_core_switch_and_api_is_held_at_ready(mode, monkeypatch, tmp_path, capsys):
    """Met by state the fakes hold (the last apply's inputs, the enabled
    services), not only because the owning pass is done."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys, ("F1", "F2"))
    assert code == 0, world.said[-1][-3000:]
    unmet = manifest_met.unmet(world, REPO)
    known = set().union(*UNRENDERED.values())
    assert [line for line in unmet if line.split(":", 1)[0] not in known] == []
    open_findings = sorted(f for f, ids in UNRENDERED.items() if any(line.split(":", 1)[0] in ids for line in unmet))
    if open_findings:
        pytest.xfail(f"{', '.join(open_findings)} (e2e-findings.md): " + "; ".join(unmet))
    pytest.fail("every core switch is held: F3 and F4 are fixed, so drop UNRENDERED and make this an assertion "
                "(strict: a fixed finding turns its xfail red)")
