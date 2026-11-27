"""Contract request 34's rollout, as configured (archived issue 342, owner decisions 2026-09-29 on archived pull request 353).

* dev admits signed step specs only, with no legacy cutover left behind, and
  names the signing key version.
* The key ring cannot carry a label, so it is in unlabelable-types.json; the
  key's IAM members are exempt by SHAPE (destroy-guard.jq), as that file's own
  description says every `*_iam_member` is, and are not listed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars.example"
UNLABELABLE = REPO / "scripts" / "lib" / "unlabelable-types.json"


def _tfvar(name: str) -> str | None:
    match = re.search(rf'(?m)^{name}\s*=\s*"([^"]*)"\s*$', DEV_TFVARS.read_text())
    return match.group(1) if match else None


def test_dev_enforces_signed_step_specs():
    # Runbook step 4 (archived issue 342, owner decision 2026-10-01): once no non-terminal
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
