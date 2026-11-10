"""Every service that reads the managed plugins bucket is told its name.

docs/design/managed-plugins.md §3 and §9.3. Three readers of
SWARM_PLUGINS_BUCKET run as Cloud Run services: swarm-api (the catalogue),
swarm-scheduler (which passes it THROUGH to every worker by
`scheduler.dispatch.worker_env`, so the worker can fetch a pinned copy) and
swarm-reconciler (whose garbage collection deletes unused pinned copies). Each
gets the bucket Terraform makes, never a literal, in
terraform/infra/locals.tf's `service_env`.

These read the Terraform as text: no provider, no plan, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LOCALS_TF = ROOT / "terraform" / "infra" / "locals.tf"

SETTING = re.compile(r"^\s*SWARM_PLUGINS_BUCKET\s*=\s*(.+?)\s*$", re.M)
BUCKET = "google_storage_bucket.plugins.name"


def _code(text: str) -> str:
    """The text with every `#` comment removed."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _service_block(text: str, service: str) -> str:
    """The body of the `{ ... }` literal in `"<service>" = merge(..., { ... })`."""
    m = re.search(rf'^\s*"{re.escape(service)}"\s*=\s*merge\([^{{\n]*\{{', text, re.M)
    assert m, f"locals.tf has no service_env block for {service}"
    depth = 0
    for j in range(m.end() - 1, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[m.end() : j]
    raise AssertionError(f"unbalanced block for {service}")


@pytest.fixture(scope="module")
def locals_tf() -> str:
    return _code(LOCALS_TF.read_text())


@pytest.mark.parametrize("service", ["swarm-api", "swarm-scheduler", "swarm-reconciler"])
def test_the_service_gets_the_plugins_bucket_terraform_makes(locals_tf, service):
    values = SETTING.findall(_service_block(locals_tf, service))
    assert values == [BUCKET], (
        f"{service}'s environment must set SWARM_PLUGINS_BUCKET = {BUCKET} exactly once, "
        f"found {values!r}"
    )


@pytest.mark.parametrize("service", ["swarm-scheduler", "swarm-reconciler"])
def test_the_setting_says_what_reads_it(service):
    """A comment beside the value names its reader and what empty does."""
    raw = LOCALS_TF.read_text()
    m = re.search(rf'^\s*"{re.escape(service)}"\s*=\s*merge\(', raw, re.M)
    assert m
    tail = raw[m.end():]
    line = SETTING.search(tail)
    assert line, f"{service} has no SWARM_PLUGINS_BUCKET"
    comment = tail[: line.start()].rstrip().splitlines()[-1].strip()
    assert comment.startswith("#"), f"{service}'s SWARM_PLUGINS_BUCKET has no comment above it"
