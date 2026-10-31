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
