"""The Cloud Build GitHub connection's grants are gone from Terraform (W11).

WHY THIS EXISTS. terraform/bootstrap/cloudbuild_connection.tf gave Google's
Cloud Build service agent two custom roles: an unconditioned
secretmanager.secrets.create, and setIamPolicy/getIamPolicy on secrets named
`swarm-github-github-oauthtoken-*`. They existed only so the `swarm-github`
second-generation connection could store its OAuth token, for the Cloud Build
trigger that ran workspace setup. WD2's re-decision (2026-10-10) replaced that
trigger with the Cloud Run job `swarm-workspace-apply`, and on 2026-10-13 the
job took w-7f9285 through A1 to A9 to ready. From then on the grants admit
Google's agent to a power nothing uses, in a project that holds another
team's secrets (docs/workspaces.md §10, row W11 and the removal note).

These tests hold the deletion: no file, no resource, no role id, and no
`removed` block with `destroy = false` that would leave the grants live while
looking retired. A control proves the scan read the bootstrap root at all, so
an empty glob cannot pass as a clean one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TERRAFORM = REPO / "terraform"
BOOTSTRAP = TERRAFORM / "bootstrap"
WORKSPACES = REPO / "docs" / "workspaces.md"

# Every name the retired file declared: its resources and locals, and the two
# role ids, which a re-created role under another resource name would reuse.
RETIRED = (
    "cloudbuild_connection",
    "swarmCloudBuildConnectionSecretCreate",
    "swarmCloudBuildConnectionSecretPolicy",
    "cloudbuild_agent_member",
)

RESOURCE = re.compile(r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)


def _tf_files() -> list[Path]:
    return sorted(TERRAFORM.rglob("*.tf"))


def test_the_scan_reads_the_bootstrap_root() -> None:
    """Control: the glob finds files, and a resource known to exist is seen."""
    files = _tf_files()
    assert len(files) > 50, f"only {len(files)} .tf files under terraform/"
    declared = {
        (m.group(2), m.group(3))
        for path in files
        if path.is_relative_to(BOOTSTRAP)
        for m in RESOURCE.finditer(path.read_text(encoding="utf-8"))
    }
    assert ("google_storage_bucket", "state") in declared


def test_the_connection_file_is_deleted() -> None:
    assert not (BOOTSTRAP / "cloudbuild_connection.tf").exists()


def test_no_resource_named_cloudbuild_connection_remains() -> None:
    offenders = [
        f"{path.relative_to(REPO)}: {m.group(1)} {m.group(2)}.{m.group(3)}"
        for path in _tf_files()
        for m in RESOURCE.finditer(path.read_text(encoding="utf-8"))
        if "cloudbuild_connection" in m.group(3)
    ]
    assert offenders == []


def test_nothing_in_terraform_names_the_retired_grants() -> None:
    """Not a `moved`, not a `removed` (destroy = false would keep them live),
    not a role id, not a comment pointing a reader at a file that is gone."""
    offenders = [
        f"{path.relative_to(REPO)}:{number}: {name}"
        for path in _tf_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for name in RETIRED
        if name in line
    ]
    assert offenders == []


def test_w11_records_the_terraform_half_done_and_the_owner_steps() -> None:
    text = WORKSPACES.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| W11 |"))
    assert "Terraform half done" in row
    runbook = text[text.index("### W11: retiring the Cloud Build connection"):]
    runbook = runbook[: runbook.index("\n## ")]
    for step in (
        "gcloud builds connections delete swarm-github --region=us-central1",
        "swarm-github-github-oauthtoken-",
        "gcloud secrets delete",
        "Google Cloud Build",
    ):
        assert step in runbook, step
