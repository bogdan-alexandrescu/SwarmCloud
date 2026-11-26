"""dev runs swarm-api with two uvicorn workers; every other environment keeps one.

The owner's choice of 2026-11-10 (measured offline on 2 CPUs: 82 -> 172
requests per second at 8 in flight, p95 206 -> 96 ms), made only for dev and
only once the per-process caps hold per instance (swarm_api.workers). infra's
`api_workers` defaults to 1 (tests/terraform/api_workers_env.tftest.hcl holds
the rendering), so an environment that sets nothing -- prod, and a fresh
install, whose tfvars the installer renders without it -- runs one.
Offline: source text only.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = ROOT / "terraform" / "environments"
SETTING = re.compile(r"^\s*api_workers\s*=\s*(\d+)\s*$", re.MULTILINE)


def _setting(env: str) -> list[str]:
    return SETTING.findall((ENVIRONMENTS / env / f"{env}.tfvars.example").read_text())


def test_dev_sets_two_workers():
    assert _setting("dev") == ["2"]


def test_prod_sets_nothing_and_keeps_the_default_of_one():
    assert _setting("prod") == []


def test_the_infra_default_is_one():
    text = (ROOT / "terraform" / "infra" / "variables.tf").read_text()
    block = re.search(r'variable "api_workers" \{(?P<body>.*?)\n\}', text, re.DOTALL)
    assert block is not None, "terraform/infra/variables.tf declares api_workers"
    assert re.search(r"^\s*default\s*=\s*1\s*$", block.group("body"), re.MULTILINE)


# That the installer renders no worker count -- a fresh deployment keeps infra's
# default of one -- is the lint rule `installer-renders-no-api-workers`
# (scripts/source_text_rules/control_plane_am.py).
