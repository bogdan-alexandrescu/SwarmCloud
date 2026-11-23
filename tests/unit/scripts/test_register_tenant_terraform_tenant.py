"""`scripts/register-tenant.sh --terraform-tenant`: the pre-release registration of a Terraform tenant (#141).

docs/ci.md ("A new account exists before the release that adds it") has the
operator register a workspace BEFORE the release that adds it to
`var.tenants`, so the worker account exists for bootstrap's per-account grant.
A full registration also made three objects that release then creates with no
import, so it answered 409 "already exists" and kept failing:

  * `swarm-tenant-<t>-qa` -- terraform/infra/qa_targets.tf,
    google_secret_manager_secret.qa_target;
  * `tenants/<t>` and `pools/tenant:<t>` -- terraform/modules/firestore/
    bootstrap.tf, google_firestore_document.tenant and .pool.

What has to hold:

  * with `--terraform-tenant` the run creates none of the three (not even under
    --dry-run's "would"), names each as Terraform's, and still makes every
    boundary the release adopts or adds to: the worker account, its secret and
    bucket grants;
  * with no flag a registration makes all three, exactly as before -- the job
    path and every tenant Terraform never manages depend on it;
  * `--add-provider` and `--workspace` refuse the flag, as every full-only one;
  * the Terraform resources the run names are the ones Terraform declares.

The harness is test_register_tenant_qa_secret's: the real script, fake `gcloud`
and `curl` first on PATH, a stateful Secret Manager for the `-qa` secret.
"""

from __future__ import annotations

import re

from .test_register_tenant_merge_step import (
    FULL,
    REGISTER,
    REPO,
    WORKER,
    Fakes,
    _out,
    _tenant_document,
)
from .test_register_tenant_qa_secret import QA, QaFakes

QA_TF = REPO / "terraform" / "infra" / "qa_targets.tf"
FIRESTORE_TF = REPO / "terraform" / "modules" / "firestore" / "bootstrap.tf"


def _document_writes(fakes: Fakes) -> list[str]:
    """The document path of every Firestore PATCH the run made."""
    return [re.sub(r"^.*/documents/", "", c["url"]).split("?")[0] for c in fakes.patches()]


def test_a_terraform_tenant_registration_creates_nothing_terraform_owns(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic", "--terraform-tenant"])
    out = _out(proc)
    assert proc.returncode == 0, out
    # MUTATION: drop the --terraform-tenant guard on section 4b and the secret is created.
    assert fakes.qa_calls("create") == [], f"{QA} is qa_targets.tf's; the release 409s on it:\n{out}"
    assert fakes.qa_calls("add-iam-policy-binding") == [], out
    # MUTATION: drop the guard on section 6 and both documents are written.
    writes = _document_writes(fakes)
    assert "tenants/eng" not in writes, f"tenants/eng is bootstrap.tf's:\n{out}"
    assert "pools/tenant:eng" not in writes, f"pools/tenant:eng is bootstrap.tf's:\n{out}"
    # The run says whose they are, so the operator does not make them by hand.
    for name in (
        "google_secret_manager_secret.qa_target",
        "google_firestore_document.tenant",
        "google_firestore_document.pool",
    ):
        assert name in out, f"the run does not say {name} is the release's:\n{out}"


def test_a_terraform_tenant_still_gets_every_other_boundary(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = fakes.run(
        [str(REGISTER), *FULL, "--providers", "anthropic", "--terraform-tenant"],
        FAKE_MISSING_ACCOUNTS="swarm-agent-worker-eng@swarm-test-project.iam.gserviceaccount.com",
    )
    out = _out(proc)
    assert proc.returncode == 0, out
    # The account bootstrap's grant needs, which the release adopts.
    assert any(
        c["argv"][:3] == ["iam", "service-accounts", "create"] for c in fakes.gcloud_changes()
    ), f"the worker account must still be created before the release:\n{out}"
    assert ("swarm-tenant-eng-anthropic", f"serviceAccount:{WORKER}") in fakes.secret_grants(), out
    assert fakes.bucket_changes(), out


def test_a_terraform_tenant_dry_run_would_not_write_them_either(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic", "--terraform-tenant", "--dry-run"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out
    assert "would write tenants/eng" not in out, out
    assert "would write pools/tenant:eng" not in out, out
    assert f"would run: gcloud secrets create {QA}" not in out, out


def test_without_the_flag_a_registration_makes_all_three_as_before(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert [argv[2] for argv in fakes.qa_calls("create")] == [QA], out
    writes = _document_writes(fakes)
    assert "tenants/eng" in writes, out
    assert "pools/tenant:eng" in writes, out


def test_add_provider_refuses_the_flag(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "openai", "--terraform-tenant"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "--terraform-tenant" in out, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out


def test_workspace_refuses_the_flag(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), "--workspace", "w-3f9a2c", "--terraform-tenant"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "takes its every value from the approved record" in out, out
    assert fakes.calls() == [], out


def test_the_resources_named_are_the_ones_terraform_declares() -> None:
    # If Terraform stops creating one of these, the flag must stop skipping it.
    assert re.search(r'resource "google_secret_manager_secret" "qa_target" \{\s*for_each = toset\(keys\(var\.tenants\)\)',
                     QA_TF.read_text()), "qa_targets.tf no longer makes a QA secret per var.tenants key"
    text = FIRESTORE_TF.read_text()
    assert 'resource "google_firestore_document" "tenant"' in text
    assert 'resource "google_firestore_document" "pool"' in text
