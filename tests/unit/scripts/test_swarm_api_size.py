"""swarm-api runs up to 8 instances of 2 vCPU and 2 GiB in dev (the owner's pick,
2026-11-06, part of #2688).

The load analysis of 2026-11-06 found swarm-api at 5 of its 5 instances for
359 of 360 minutes, at 91% CPU and 94% memory, on instances of 1 vCPU and
1 GiB. These hold the pick where a deployment reads it: the swarm-api service
in terraform/infra/main.tf, dev's service_max_instances, the Day One
installer's fixed monthly floor, and docs/scaling.md's ceilings row.

Text-level and offline; tests/terraform/cloud_run.tftest.hcl holds the module
that renders these to a plan.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MAIN_TF = ROOT / "terraform" / "infra" / "main.tf"
DEV_TFVARS = ROOT / "terraform" / "environments" / "dev" / "dev.tfvars.example"
SCALING = ROOT / "docs" / "scaling.md"


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"\s#.*$|^\s*#.*$", "", line) for line in text.splitlines())


def _api_service() -> str:
    """The body of swarm-api's entry in module.cloud_run_api's services map."""
    text = _strip_comments(MAIN_TF.read_text())
    call = re.search(r'(?ms)^module "cloud_run_api" \{\n(.*?)^\}', text)
    assert call, 'no module "cloud_run_api" block'
    entry = re.search(r'(?ms)^    "swarm-api" = \{\n(.*?)^    \}', call.group(1))
    assert entry, "no swarm-api entry in module.cloud_run_api"
    return entry.group(1)


def _attr(body: str, name: str) -> str:
    (value,) = re.findall(rf"(?m)^      {name}\s*=\s*(\S+)$", body)
    return value


def test_swarm_api_runs_2_vcpu_and_2_gib():
    body = _api_service()
    # MUTATION: put cpu back to "1" or memory to "1Gi" -> red.
    assert _attr(body, "cpu") == '"2"'
    assert _attr(body, "memory") == '"2Gi"'
    # The warm floor of two stays (owner decision Q6(a), #1457).
    assert _attr(body, "min_instances") == "2"
    # One uvicorn process an instance: concurrency stays 80 (main.tf says why).
    assert _attr(body, "concurrency") == "80"


def test_dev_runs_at_most_8_swarm_api_instances():
    tfvars = _strip_comments(DEV_TFVARS.read_text())
    block = re.search(r"(?ms)^service_max_instances = \{\n(.*?)^\}", tfvars)
    assert block, "no service_max_instances in dev.tfvars"
    # MUTATION: put dev's swarm-api max back to 5 -> red.
    assert re.findall(r'(?m)^\s*"swarm-api"\s*=\s*(\d+)$', block.group(1)) == ["8"]


def test_the_installer_floor_prices_two_instances_of_the_new_size():
    # Imported here: swarm_install pulls in pydantic, which collection of
    # tests/unit/scripts must not pay for (test_collection_stays_cheap.py).
    from swarm_install.steps import foundations

    (api,) = [(what, usd) for what, usd in foundations.MONTHLY_FLOOR_USD if what.startswith("swarm-api")]
    # 2 vCPU x $0.000018 + 2 GiB x $0.000002 a second, CPU always allocated,
    # 730 hours: ~$105 an instance, two of them.
    assert "2 vCPU / 2 GiB" in api[0]
    assert api[1] == 210


def test_the_scaling_doc_states_the_new_size():
    text = re.sub(r"\s+", " ", SCALING.read_text())
    assert "max 8 instances of 2 vCPU and 2 GiB" in text
