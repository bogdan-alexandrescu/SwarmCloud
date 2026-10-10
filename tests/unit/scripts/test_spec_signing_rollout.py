"""Contract request 34's rollout, as configured (#342, owner decisions 2026-09-29 on #353).

* dev runs the verifying worker in LEGACY first: an unsigned task created
  before the cutover is admitted and logged, because every task parked when
  the verifying worker ships is unsigned. A legacy worker with no cutover
  admits nothing (`agent_worker.specverify._legacy_admits`), so the cutover
  must be set beside the mode.
* The key ring cannot carry a label, so it is in unlabelable-types.json; the
  key's IAM members are exempt by SHAPE (destroy-guard.jq), as that file's own
  description says every `*_iam_member` is, and are not listed.
* The GKE copy of the public keys is applied by kubernetes/apply.sh, in the
  release job, from terraform/infra's output -- not by a Kubernetes provider.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"
UNLABELABLE = REPO / "scripts" / "lib" / "unlabelable-types.json"
RELEASE = REPO / ".github" / "workflows" / "release.yml"

#: The hard end of the legacy window, in #353's worker code (SPEC_LEGACY_UNTIL).
LEGACY_UNTIL = datetime.fromisoformat("2026-10-20T00:00:00+00:00")


def _tfvar(name: str) -> str | None:
    match = re.search(rf'(?m)^{name}\s*=\s*"([^"]*)"\s*$', DEV_TFVARS.read_text())
    return match.group(1) if match else None


def test_dev_enforces_signed_step_specs():
    # Runbook step 4 (#342, owner decision 2026-10-01): once no non-terminal
    # unsigned task is left, dev admits signed specs only.
    assert _tfvar("spec_signature_mode") == "enforce"


def test_dev_keeps_no_legacy_cutover_once_it_enforces():
    # A cutover is meaningful only in legacy mode; left behind under enforce it
    # reads as if an unsigned window were still open.
    assert _tfvar("spec_legacy_cutover") in (None, "")


def test_dev_names_the_signing_version():
    assert re.search(r"(?m)^spec_signing_key_version\s*=\s*[1-9][0-9]*\s*$", DEV_TFVARS.read_text())


def test_the_key_ring_is_unlabelable_and_the_iam_members_are_exempt_by_shape():
    types = json.loads(UNLABELABLE.read_text())["types"]
    assert "google_kms_key_ring" in types
    # The crypto key CAN carry a label and must.
    assert "google_kms_crypto_key" not in types
    assert not [t for t in types if re.search(r"_iam_(member|binding|policy)$", t)]
    assert types == sorted(types)


def _deploy_steps() -> list[dict]:
    # With the composite actions it runs flattened into their steps
    # (.github/actions/release-namespaces holds this one).
    from .test_release_reuses_ci_images import _workflow

    return _workflow(RELEASE.name)["jobs"]["deploy"]["steps"]


def _spec_keys_step() -> tuple[int, dict]:
    steps = _deploy_steps()
    matches = [
        (i, s) for i, s in enumerate(steps)
        if "kubernetes/apply.sh" in str(s.get("run", "")) and "--spec-verify-keys" in str(s.get("run", ""))
    ]
    assert len(matches) == 1, "exactly one release step applies the tenant namespaces with the keys"
    return matches[0]


def test_the_release_applies_the_configmap_from_terraforms_output_with_apply_sh():
    _, step = _spec_keys_step()
    run = step["run"]
    assert "output -json spec_verify_keys_configmap" in run
    assert "output -json tenant_namespaces" in run
    assert "--confirm" in run


def test_the_configmap_is_applied_before_anything_is_dispatched():
    index, _ = _spec_keys_step()
    names = [s.get("name", "") for s in _deploy_steps()]
    smoke = next(i for i, n in enumerate(names) if n.startswith("smoke test"))
    assert index < smoke
