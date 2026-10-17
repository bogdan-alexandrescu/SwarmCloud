"""WORKSPACE_GATE is ON in dev and nowhere else (W10, WD8; docs/workspaces.md §5.5).

`swarm_api.workspaces.gate_from_env` reads WORKSPACE_GATE and ships OFF. Until
W10 nothing in Terraform set it, so the only way to turn it on was by hand on a
revision, which the next release's apply would put back. W10 renders it from
`var.workspace_gate` (terraform/infra/locals.tf) and sets it true in dev.tfvars
only, after the first workspace went end to end through the job (w-7f9285,
ready 2026-10-13 05:05Z; docs/multi-tenancy.md §7.5).

These read text, not a plan: the rendering is held by
tests/terraform/workspace_gate_env.tftest.hcl, and this file holds what the
environments SAY, which no `terraform test` run reads. It also holds the
rendered words to the reader, so a value Terraform writes that the gate would
refuse at start ("onn") cannot be rendered. Offline: source text and an import.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_api import workspaces as ws

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = ROOT / "terraform" / "environments"
LOCALS = ROOT / "terraform" / "infra" / "locals.tf"
VARIABLES = ROOT / "terraform" / "infra" / "variables.tf"

ASSIGNMENT = re.compile(r"^\s*workspace_gate\s*=\s*(?P<value>\S+)\s*(?:#.*)?$", re.MULTILINE)
RENDER = re.compile(
    r'^\s*WORKSPACE_GATE\s*=\s*var\.workspace_gate\s*\?\s*"(?P<on>[^"]*)"\s*:\s*"(?P<off>[^"]*)"\s*$',
    re.MULTILINE,
)


def _assignments(env: str) -> list[str]:
    return [m.group("value") for m in ASSIGNMENT.finditer((ENVIRONMENTS / env / f"{env}.tfvars").read_text())]


def test_dev_turns_the_gate_on_exactly_once():
    assert _assignments("dev") == ["true"], (
        "dev.tfvars must set `workspace_gate = true` once (W10: WD8's precondition, "
        "one real approval end to end, was met on 2026-10-13)"
    )


def test_dev_says_why_and_when_beside_the_value():
    text = (ENVIRONMENTS / "dev" / "dev.tfvars").read_text()
    head = text[: text.index("\nworkspace_gate")]
    comment = head[head.rindex("\n\n"):]
    for needle in ("WORKSPACE_GATE", "2026-10-13", "w-7f9285", "WD8"):
        assert needle in comment, f"the comment above dev's workspace_gate must name {needle!r}"


def test_every_other_environment_leaves_the_gate_off():
    for tfvars in sorted(ENVIRONMENTS.glob("*/*.tfvars")):
        env = tfvars.parent.name
        if env == "dev":
            continue
        assert "true" not in _assignments(env), f"{env} turns WORKSPACE_GATE on; W10 turns it on in dev only"


def test_the_variable_defaults_off():
    text = VARIABLES.read_text()
    block = text[text.index('variable "workspace_gate"'):]
    block = block[: block.index("\n}\n")]
    assert re.search(r"^\s*type\s*=\s*bool\s*$", block, re.M), "workspace_gate is a bool"
    assert re.search(r"^\s*default\s*=\s*false\s*$", block, re.M), (
        "workspace_gate defaults off, as gate_from_env does: on refuses every person without a ready record"
    )


def test_swarm_api_reads_what_terraform_renders():
    match = RENDER.search(LOCALS.read_text())
    assert match, "terraform/infra/locals.tf must render WORKSPACE_GATE from var.workspace_gate into swarm-api's env"
    assert ws.gate_from_env({"WORKSPACE_GATE": match.group("on")}) is True
    assert ws.gate_from_env({"WORKSPACE_GATE": match.group("off")}) is False
