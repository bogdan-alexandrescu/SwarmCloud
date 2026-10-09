"""dev runs the personal-workspace gate the owner decided (W10 of #847, WD8).

`WORKSPACE_GATE` refuses a person's submission into their own tenant until
their workspace is `ready` and has a Claude account (docs/workspaces.md §5).
It ships off: on, it refuses every personal tenant without a `ready` record,
which until the first real approval end to end is every personal tenant there
is (§5.5). It is set in ONE place per environment, `workspace_gate` in that
environment's tfvars, rendered by terraform/infra/locals.tf into swarm-api's
environment (tests/terraform/workspace_gate_env.tftest.hcl holds the
rendering; a terraform test cannot load an environment's tfvars, so this file
holds dev's value).

Offline: reads the tfvars, variables.tf and locals.tf as text, and the gate's
own parser.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_api.workspaces import gate_from_env

REPO = Path(__file__).resolve().parents[3]
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"
VARIABLES = REPO / "terraform" / "infra" / "variables.tf"
LOCALS = REPO / "terraform" / "infra" / "locals.tf"

#: What dev runs. "on" from the commit that turned it on (2026-10-09), which
#: must not merge before the first real approval end to end on SwarmCloud
#: (WD8). It changed this and dev.tfvars together.
DEV_VALUE = "on"

_ASSIGNMENT = re.compile(r'^workspace_gate\s*=\s*"([^"]*)"\s*$', re.M)


def _dev_values() -> list[str]:
    return _ASSIGNMENT.findall(DEV_TFVARS.read_text(encoding="utf-8"))


def test_dev_sets_the_gate_once():
    assert len(_dev_values()) == 1, "dev.tfvars sets workspace_gate exactly once: one place to read, one to change"


def test_dev_runs_the_decided_value():
    assert _dev_values() == [DEV_VALUE]


def test_the_value_dev_sets_is_one_the_gate_reads_as_meant():
    (value,) = _dev_values()
    assert gate_from_env({"WORKSPACE_GATE": value}) is (DEV_VALUE == "on")


def test_dev_says_why_next_to_the_value():
    text = DEV_TFVARS.read_text(encoding="utf-8")
    comment = text[: _ASSIGNMENT.search(text).start()].rstrip().splitlines()[-8:]
    block = " ".join(line.lstrip("# ").strip() for line in comment)
    assert "WD8" in block and re.search(r"20\d\d-\d\d-\d\d", block), (
        "the value carries a dated comment saying why, next to it"
    )


def test_every_other_environment_keeps_the_default_off():
    default = re.search(r'variable "workspace_gate" \{.*?^\s*default\s*=\s*"([^"]*)"', VARIABLES.read_text(encoding="utf-8"),
                        flags=re.S | re.M)
    assert default and default.group(1) == "off", "the variable defaults to off (WD8)"


def test_locals_renders_the_variable_into_swarm_apis_environment():
    text = LOCALS.read_text(encoding="utf-8")
    assert re.search(r"^\s*WORKSPACE_GATE\s*=\s*var\.workspace_gate\s*$", text, flags=re.M)
