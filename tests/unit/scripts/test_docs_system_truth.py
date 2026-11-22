"""Code- and Terraform-only invariants that used to be checked against doc prose
(lane DOCS-TRUTH, part of archived issue 1822, slice 1 of issue #71).

Each test reads the value it asserts from the code or the Terraform where one
exists there, with no doc read, so a later change to that value still turns
this red.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

MAIN_TF = REPO / "terraform" / "infra" / "main.tf"
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars.example"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tf_number(text: str, pattern: str) -> int:
    match = re.search(pattern, text, flags=re.M | re.S)
    assert match, f"no match for {pattern!r}"
    return int(match.group(1))


def _tf_block(text: str, header: str) -> str:
    """The body of a top-level HCL block, from its header to the closing brace in column 0."""
    start = text.index(header)
    end = re.search(r"^\}", text[start:], flags=re.M)
    assert end, header
    return text[start: start + end.end()]


# --------------------------------------------------------------------------
# archived issue 1818 (FS-BACKUPS): delete protection on, a daily backup kept 7 days
# --------------------------------------------------------------------------

INFRA_VARIABLES_TF = REPO / "terraform" / "infra" / "variables.tf"
FIRESTORE_VARIABLES_TF = REPO / "terraform" / "modules" / "firestore" / "variables.tf"


def _backup_retention_seconds() -> int:
    block = _tf_block(_text(FIRESTORE_VARIABLES_TF), 'variable "backup_retention" {')
    return _tf_number(block, r'default\s*=\s*"(\d+)s"')


def test_the_platform_database_takes_its_own_delete_protection_not_the_shared_switch():
    module = _tf_block(_text(MAIN_TF), 'module "firestore" {')
    # MUTATION: pass `delete_protection = var.deletion_protection` again -> red (dev turns that off).
    assert re.search(r"^\s*delete_protection\s*=\s*var\.firestore_delete_protection\s*$", module, flags=re.M), module
    variable = _tf_block(_text(INFRA_VARIABLES_TF), 'variable "firestore_delete_protection" {')
    # MUTATION: default firestore_delete_protection to false -> red.
    assert re.search(r"^\s*default\s*=\s*true\s*$", variable, flags=re.M), variable
    # MUTATION: set `firestore_delete_protection = false` in dev.tfvars -> red.
    assert not re.search(r"^\s*firestore_delete_protection\s*=\s*false", _text(DEV_TFVARS), flags=re.M)


def test_the_backup_retention_is_seven_days_like_point_in_time():
    # MUTATION: default backup_retention to "8467200s" -> red; point-in-time recovery is 7 days.
    assert _backup_retention_seconds() == 7 * 24 * 3600


def test_the_backup_schedule_type_is_exempt_from_the_managed_by_label():
    import json

    types = json.loads(_text(REPO / "scripts" / "lib" / "unlabelable-types.json"))["types"]
    # MUTATION: drop google_firestore_backup_schedule from the list -> red (the plan guard would
    # block the release on a resource that cannot carry managed-by).
    assert "google_firestore_backup_schedule" in types
    assert types == sorted(types)


def test_the_firestore_database_is_abandoned_not_destroyed_on_terraform_destroy():
    """main.tf sets deletion_policy=ABANDON on the platform database (destroy leaves it in
    place), and dev still turns the shared `deletion_protection` switch off -- the database no
    longer follows that switch, only its own `firestore_delete_protection` (previous test)."""
    main_tf = _text(MAIN_TF)
    # MUTATION: drop deletion_policy = "ABANDON" from the firestore module -> red.
    assert re.search(r'module "firestore" \{.*?deletion_policy\s*=\s*"ABANDON"', main_tf, flags=re.S)
    assert re.search(r"^deletion_protection\s*=\s*false", _text(DEV_TFVARS), flags=re.M)


# --------------------------------------------------------------------------
# docs/architecture.md and execution-backends.md: where each profile runs
# --------------------------------------------------------------------------


def _profiles_on(backend) -> list[str]:
    from swarm_common.profiles import RUNNER_PROFILES, resolve_backend

    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is backend)


def _fallback_profiles() -> list[str]:
    match = re.search(r"^\s*cloud_run_fallback_profiles\s*=\s*\[([^\]]*)\]", _text(LOCALS_TF), flags=re.M)
    assert match
    return re.findall(r'"([^"]+)"', match.group(1))


def test_cloud_run_fallback_profiles_in_locals_tf_are_gke_profiles():
    """locals.tf's cloud_run_fallback_profiles (the GKE profiles that get a Cloud Run Job
    fallback) names only profiles RUNNER_PROFILES resolves to the GKE Autopilot backend, and
    every non-control-plane profile resolves to exactly one of GKE Autopilot or Cloud Run Jobs."""
    from swarm_common.profiles import Backend, RUNNER_PROFILES

    gke = set(_profiles_on(Backend.GKE_AUTOPILOT))
    cloud_run = set(_profiles_on(Backend.CLOUD_RUN_JOB))
    assert "mock" in cloud_run and "claude-code" in gke
    # MUTATION: resolve one profile to both backends, or neither -> red.
    assert gke.isdisjoint(cloud_run)
    all_profiles = {n for n, p in RUNNER_PROFILES.items() if not p.control_plane}
    assert gke | cloud_run == all_profiles
    fallback = set(_fallback_profiles())
    # MUTATION: list a Cloud-Run-only profile as a GKE fallback in locals.tf -> red.
    assert fallback <= gke, fallback - gke
