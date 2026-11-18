"""The plugin intake job reaches the bucket through the API, with get and create only.

Live QA finding, 10 November 2026: every plugin install stayed "Installing..."
because apps/plugin-intake read `quarantine/<intake_id>` from a mount path
(SWARM_INTAKE_MOUNT, /plugins) that the deployed job never had: the job
definition in terraform/infra/plugins.tf mounts nothing. The job now reads and
writes the bucket through the Cloud Storage JSON API with PLUGINS_BUCKET
(apps/plugin-intake/plugin_intake/bucket.py), over Private Google Access, and
these hold the job to what docs/design/managed-plugins.md §2.2-§2.3 says:

* the program reads the bucket name the job definition sets, and no mount;
* the job's account holds exactly two bucket grants: get under quarantine/
  (objectViewer, whose list a resource.name condition on an object prefix can
  never satisfy: a list request names the bucket) and create under staging/
  (objectCreator: no delete, so no overwrite); no role anywhere that deletes;
* the job mounts nothing, and leaves only through its NAT-less network, whose
  subnetwork has Private Google Access and whose firewall admits HTTPS alone.

These read the Terraform and the code as text: no provider, no plan, no
credentials. tests/terraform/plugins_infra.tftest.hcl plans the same job.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGINS_TF = ROOT / "terraform" / "infra" / "plugins.tf"
INFRA = ROOT / "terraform" / "infra"
INTAKE = ROOT / "apps" / "plugin-intake" / "plugin_intake"

DELETING_ROLES = {"roles/storage.objectAdmin", "roles/storage.objectUser", "roles/storage.admin", "roles/storage.legacyBucketWriter", "roles/owner", "roles/editor"}


def _code(text: str) -> str:
    """The text with every `#` comment removed."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _block_at(text: str, start: int) -> str:
    """The body of the `{ ... }` block whose `{` is the first at or after `start`."""
    depth = 0
    for j in range(text.index("{", start), len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[text.index("{", start) + 1 : j]
    raise AssertionError("unbalanced block")


def _resources(text: str) -> dict[str, str]:
    """`type.name` -> body, for every resource in the text."""
    return {f"{m.group(1)}.{m.group(2)}": _block_at(text, m.end() - 1) for m in re.finditer(r'^resource\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{', text, re.M)}


def _attr(body: str, name: str) -> str:
    m = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*$", body, re.M)
    assert m, f"no {name}"
    return m.group(1)


@pytest.fixture(scope="module")
def plugins() -> str:
    return _code(PLUGINS_TF.read_text())


@pytest.fixture(scope="module")
def job(plugins: str) -> str:
    return _resources(plugins)["google_cloud_run_v2_job.plugin_intake"]


def _env_reads(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


def test_the_program_reads_the_bucket_the_job_is_given_and_no_mount(job):
    reads = _env_reads(INTAKE / "main.py")
    assert re.search(r'env\s*\{\s*name\s*=\s*"PLUGINS_BUCKET"\s*value\s*=\s*google_storage_bucket\.plugins\.name\s*\}', job)
    assert "PLUGINS_BUCKET" in reads
    assert "SWARM_INTAKE_MOUNT" not in reads


def test_the_job_mounts_nothing(job):
    assert not re.search(r"^\s*(volumes|volume_mounts|dynamic\s+\"volumes\"|dynamic\s+\"volume_mounts\")\b", job, re.M)


def test_the_intake_account_holds_get_under_quarantine_and_create_under_staging_only():
    grants = {}
    for path in sorted(INFRA.glob("*.tf")):
        for address, body in _resources(_code(path.read_text())).items():
            if re.search(r"local\.plugin_intake_(member|email)|swarm-plugin-intake@", body) and not address.startswith(("google_cloud_run_v2_job.", "google_service_account_iam_member.")):
                grants[address] = body
    assert set(grants) == {"google_storage_bucket_iam_member.plugins_intake_read", "google_storage_bucket_iam_member.plugins_intake_stage"}, sorted(grants)
    read = grants["google_storage_bucket_iam_member.plugins_intake_read"]
    stage = grants["google_storage_bucket_iam_member.plugins_intake_stage"]
    assert _attr(read, "role") == '"roles/storage.objectViewer"' and _attr(read, "expression") == "local.plugins_prefix.quarantine"
    assert _attr(stage, "role") == '"roles/storage.objectCreator"' and _attr(stage, "expression") == "local.plugins_prefix.staging"
    for body in grants.values():
        assert _attr(body, "role").strip('"') not in DELETING_ROLES


def test_the_conditions_name_objects_so_no_list_can_match(plugins):
    # A list request's resource is the bucket; these conditions need an object.
    assert re.search(r'plugins_objects\s*=\s*"projects/_/buckets/\$\{local\.plugins_bucket\}/objects"', plugins)
    for prefix in ("quarantine", "staging"):
        assert re.search(rf'{prefix}\s*=\s*"resource\.name\.startsWith\(\\"\$\{{local\.plugins_objects\}}/{prefix}/\\"\)"', plugins), prefix


def test_the_job_reaches_cloud_storage_only_through_private_google_access(plugins, job):
    resources = _resources(plugins)
    assert re.search(r'egress\s*=\s*"ALL_TRAFFIC"', job)
    assert re.search(r"subnetwork\s*=\s*google_compute_subnetwork\.plugin_intake\.id", job)
    assert _attr(resources["google_compute_subnetwork.plugin_intake"], "private_ip_google_access") == "true"
    allow = resources["google_compute_firewall.plugin_intake_allow_google_apis"]
    assert re.search(r'protocol\s*=\s*"tcp"\s*ports\s*=\s*\["443"\]', allow)
    assert not any(address.startswith(("google_compute_router.", "google_compute_router_nat.")) for address in resources)
