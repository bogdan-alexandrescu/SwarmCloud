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


def walk(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> int:
    harness.install(world, monkeypatch, tmp_path)
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
        (r"Desktop client.s id", "123456789012-example.apps.googleusercontent.com"),
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

MODES = [pytest.param("team", id="team-of-5"), pytest.param("individual", id="one-person")]


def walked(mode: str, monkeypatch, tmp_path, capsys) -> tuple[World, int]:
    world = World()
    world.person = person(mode)
    return world, walk(world, monkeypatch, tmp_path, capsys)


@pytest.mark.parametrize("mode", MODES)
def test_the_walk_reaches_ready(mode, monkeypatch, tmp_path, capsys):
    """The product as it is, from stage 0, with no finding's fix assumed (F1
    and F2 are fixed on main): every stage walks, every core switch and API
    is held by the fakes, and the person is never sent to run anything
    outside `swarm install`."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys)
    said = world.said[-1]
    assert code == 0, said[-3000:]
    assert "READY: SwarmCloud answers at https://" + HOSTNAME in said
    assert world.unhandled == []
    assert world.person.unscripted == []
    assert world.person.asked_to_run_elsewhere() == []
    asked = " ".join(prompt for _, prompt in world.person.asked)
    # Audit 5b, M3: verify's own runs land in the first workspace (the first
    # team, or one person's own), where the console's runs, the pool account
    # and the grant are, never the personal default `swarm tenants` lists first.
    plugin_tenants = {t.tenant for t in world.deployment.tasks.values() if t.submitter == deployment.PERSON}
    assert plugin_tenants == {world.first_workspace}, plugin_tenants
    bootstrap = [inputs for root, inputs, _ in world.tf_applies if root == "bootstrap"][-1]
    members = set(bootstrap["frontend_iap_members"])
    if mode == "team":  # decision 1: the organisation's domain, the installer, the Desktop client
        assert "How many people" in asked
        assert {"domain:acme.example", f"user:{world.installer}"} <= members
    else:  # decision 7: one person, so IAP admits only the installer, and there is no team group
        assert "How many people" not in asked
        assert not any(m.startswith(("domain:", "group:")) for m in members), members
        assert f"user:{world.installer}" in members


@pytest.mark.parametrize("mode", MODES)
def test_every_core_switch_and_api_is_held_at_ready(mode, monkeypatch, tmp_path, capsys):
    """Met by state the fakes hold (the last apply's inputs, the enabled
    services), not only because the owning pass is done: F3 and F4's
    switches, once counted done with no pass rendering them, are held."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys)
    assert code == 0, world.said[-1][-3000:]
    assert manifest_met.unmet(world, REPO) == []


def test_every_wait_in_the_walk_says_why_it_is_still_waiting(monkeypatch, tmp_path, capsys):
    """Audit 5b M1: a wait's countdown carries its probe's detail, so the
    person reads why it is still waiting (here the certificate still
    provisioning behind the console), not only the wait's static reason."""
    world, code = walked("team", monkeypatch, tmp_path, capsys)
    lines = world.said[-1].splitlines()
    assert code == 0, "\n".join(lines[-60:])
    waits = [i for i, line in enumerate(lines) if line.startswith("  waiting on ")]
    assert waits, "the walk waits on something"
    steps = {lines[i].split(":", 1)[0].removeprefix("  waiting on ") for i in waits}
    for step in steps:
        first = next(i for i in waits if lines[i].startswith(f"  waiting on {step}:"))
        assert lines[first + 1].startswith("      now: "), (step, lines[first:first + 2])
    assert any("swarm-ui-cert is PROVISIONING" in line for line in lines if line.startswith("      now: "))


@pytest.mark.parametrize("mode", MODES)
def test_the_walk_applies_the_core_workloads_terraform_renders(mode, monkeypatch, tmp_path, capsys):
    """Epic 2806, comment 82: with enable_live_browser and graph_service.serve
    on and their images released, terraform/infra renders swarm_live_manifest
    and swarm_graph_manifest, so stage 10's WorkloadsStep applies both to
    swarm-system and waits for their rollouts; Voice it is off, so its output
    renders null and nothing of it is applied."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys)
    assert code == 0, world.said[-1][-3000:]
    infra = [inputs for root, inputs, targets in world.tf_applies if root == "infra" and not targets][-1]
    assert infra["enable_live_browser"] and infra["graph_service"]["serve"], infra
    outputs = world.tf_outputs["infra"]
    assert outputs.get("swarm_live_manifest") and outputs.get("swarm_graph_manifest"), sorted(outputs)
    assert outputs.get("swarm_voice_manifest") is None
    objects = world.kube_objects
    images = infra["image_refs"]
    for kind, name in (("statefulset", "swarm-live"), ("deployment", "swarm-graph")):
        held = objects.get((kind, "swarm-system", name))
        assert held is not None, f"{kind}/{name} was never applied to swarm-system"
        containers = held["spec"]["template"]["spec"]["containers"]
        assert images[name] in {c["image"] for c in containers}, containers
        assert held["status"]["readyReplicas"] >= 1
    assert ("statefulset", "swarm-system", "swarm-voice") not in objects
    labels = [k for doc in objects.values() for k in ((doc.get("metadata") or {}).get("labels") or {})]
    assert not any("__" in k for k in labels), "the engine fills the label prefix of what it applies"


@pytest.mark.parametrize("voice_it, rendered", [("off", False), ("account", False), ("on", True)])
def test_the_infra_effect_renders_voice_only_when_terraform_would(voice_it, rendered):
    """voice.tf renders swarm_voice_manifest only while voice_it is "on" on
    the Autopilot cluster with a swarm-voice image; live.tf and
    graph_service.tf likewise null their outputs while switched off."""
    images = {"swarm-live": "live@sha256:" + "1" * 64, "swarm-graph": "graph@sha256:" + "2" * 64,
              "swarm-voice": "voice@sha256:" + "3" * 64}
    on = effects.workload_manifests(PROJECT, "europe-west1", {
        "enable_live_browser": True, "enable_graph_service": True, "graph_service": {"serve": True, "enabled": True},
        "voice_it": voice_it, "image_refs": images})
    assert bool(on["swarm_voice_manifest"]) is rendered
    assert images["swarm-live"] in on["swarm_live_manifest"]
    assert images["swarm-graph"] in on["swarm_graph_manifest"]
    if rendered:
        assert images["swarm-voice"] in on["swarm_voice_manifest"]
    off = effects.workload_manifests(PROJECT, "europe-west1", {
        "enable_live_browser": True, "enable_graph_service": True, "graph_service": {"serve": True},
        "voice_it": "on", "enable_gke_autopilot": False, "image_refs": images})
    assert off == {"swarm_live_manifest": None, "swarm_graph_manifest": None, "swarm_voice_manifest": None}
    imageless = effects.workload_manifests(PROJECT, "europe-west1", {
        "enable_live_browser": True, "enable_graph_service": False, "graph_service": {"serve": True},
        "voice_it": "off", "image_refs": {}})
    assert imageless == {"swarm_live_manifest": None, "swarm_graph_manifest": None, "swarm_voice_manifest": None}


@pytest.mark.parametrize("mode", MODES)
def test_verify_connects_github_when_the_deployment_says_it_is_not_connected(mode, monkeypatch, tmp_path, capsys):
    """Epic 2806, comment 83: on a new deployment the person has not connected
    GitHub in the first workspace yet, so `sc access repos` is refused with
    swarm-api's github_not_connected; verify runs `swarm sc setup` once (the
    browser connection) and reads the repositories again."""
    world, code = walked(mode, monkeypatch, tmp_path, capsys)
    assert code == 0, world.said[-1][-3000:]
    sc = [" ".join(argv[1:4]) for argv in world.commands
          if Path(str(argv[0])).name == "swarm" and argv[1:2] == ["sc"]]
    assert sc == ["sc access repos", "sc setup --context", "sc access repos"], sc
    assert world.deployment.github_connected == {world.first_workspace}
    assert world.person.asked_to_run_elsewhere() == []
