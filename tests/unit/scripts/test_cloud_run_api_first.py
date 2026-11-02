"""One release rolls swarm-api's new revision before the scheduler's (owner's
pick, 2026-11-02, #3041).

The scheduler's WORKER_IMAGE_REFS picks the image of every new dispatch, so a
release that adds a state route and its first worker call in one commit must
not hand workers that image before swarm-api serves the route. terraform/infra
therefore calls the cloud_run module twice:

  * module.cloud_run_api creates swarm-api and nothing else;
  * module.cloud_run creates every other service and `depends_on` it, so one
    apply of the one reviewed plan waits for swarm-api's revision to be Ready;
  * swarm-api's run.invoker grants stay in module.cloud_run
    (`invokers_elsewhere`), at the addresses they always had, and the service
    itself is carried over by one `moved` block, never destroyed.

Text-level and offline: `terraform validate` and tests/terraform/cloud_run.tftest.hcl
hold the same files to a plan.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "terraform" / "infra"


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"\s#.*$|^\s*#.*$", "", line) for line in text.splitlines())


def _block(text: str, header: str) -> str:
    """The body of the top-level block opened by `header`."""
    match = re.search(rf"(?m)^{re.escape(header)} \{{\n(.*?)^\}}", text, re.S)
    assert match, f"no top-level `{header}` block"
    return match.group(1)


def _services(body: str) -> list[str]:
    """The service names a module call's `services` map declares (4-space keys)."""
    return re.findall(r'(?m)^    "([a-z-]+)" = \{', body)


def _main() -> str:
    return _strip_comments((INFRA / "main.tf").read_text())


def test_swarm_api_has_a_call_of_its_own():
    body = _block(_main(), 'module "cloud_run_api"')
    assert 'source = "../modules/cloud_run"' in body
    assert _services(body) == ["swarm-api"]
    # Its grants are made by module.cloud_run, so its own map is empty.
    assert re.search(r"(?m)^      invokers = \{\}$", body)


def test_every_other_service_waits_for_swarm_api():
    body = _block(_main(), 'module "cloud_run"')
    assert "swarm-api" not in _services(body)
    assert "swarm-scheduler" in _services(body)
    (depends_on,) = re.findall(r"(?m)^  depends_on\s*=\s*(\[[^\]]*\])", body)
    assert "module.cloud_run_api" in re.sub(r"\s", "", depends_on).strip("[]").split(","), (
        "module.cloud_run must depend on module.cloud_run_api, or the scheduler can hand "
        "workers a new image before swarm-api's revision serves its routes (#3041)"
    )


def test_swarm_api_grants_stay_at_their_addresses():
    body = _block(_main(), 'module "cloud_run"')
    grants = re.search(r'(?ms)^  invokers_elsewhere = \{\n    "swarm-api" = merge\((.*?)^    \)', body)
    assert grants, "swarm-api's run.invoker grants must stay in module.cloud_run"
    for part in ("var.api_invokers", "gcp-sa-iap.iam.gserviceaccount.com", "local.child_route_invokers"):
        assert part in grants.group(1), part


def test_the_service_moves_rather_than_being_recreated():
    moves = [
        (m.group(1), m.group(2))
        for path in INFRA.glob("*.tf")
        for m in re.finditer(
            r"(?m)^moved \{\n\s*from\s*=\s*(\S+)\n\s*to\s*=\s*(\S+)\n\}", _strip_comments(path.read_text())
        )
    ]
    assert (
        'module.cloud_run.google_cloud_run_v2_service.this["swarm-api"]',
        'module.cloud_run_api.google_cloud_run_v2_service.this["swarm-api"]',
    ) in moves


def test_nothing_reads_swarm_api_from_the_other_call():
    for path in INFRA.glob("*.tf"):
        text = _strip_comments(path.read_text())
        assert 'module.cloud_run.service_urls["swarm-api"]' not in text, path.name
        assert 'module.cloud_run.service_ids : k if k == "swarm-api"' not in text, path.name
