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
  * everything else is the same run: the full set of gcloud changes and
    Firestore writes differs from an unflagged run by those three objects and
    their bindings alone, and the namespace step still runs;
  * the closing lines send no one to a command that 409s or dies before the
    release, and claim no registration the release has not completed;
  * `--add-provider` and `--workspace` refuse the flag, as every full-only one;
  * the Terraform resources the run names are the ones Terraform declares.

The harness is test_register_tenant_qa_secret's: the real script, fake `gcloud`
and `curl` first on PATH, a stateful Secret Manager for the `-qa` secret.
"""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from pathlib import Path

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


def _sub(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


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
    # The default's own closing lines: the changed "Next" text, the old key and
    # add-provider steps, and the registered claim are what a tenant Terraform
    # never manages is told.
    assert "not as registered: this run made swarm-tenant-eng-qa, tenants/eng and" in out, out
    assert "store a provider key  scripts/create-secrets.sh --tenant eng --provider anthropic --stdin" in out, out
    assert "then add it           scripts/register-tenant.sh --tenant eng --add-provider anthropic" in out, out
    assert "resolves them to tenant 'eng'" in out, out
    assert "tenant eng registered" in out, out
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


def _normalised_changes(fakes: Fakes) -> Counter:
    """Every gcloud change, with the per-run condition file's path removed."""
    out: Counter = Counter()
    for c in fakes.gcloud_changes():
        argv = c["argv"]
        shown = tuple("<file>" if prev == "--condition-from-file" else a for prev, a in zip([""] + argv, argv))
        out[(shown, c["condition"])] += 1
    return out


def _normalised_patches(fakes: Fakes) -> Counter:
    """Every Firestore PATCH as its document path and update mask."""
    return Counter(re.sub(r"^.*/documents/", "", c["url"]) for c in fakes.patches())


def test_the_flag_skips_exactly_the_qa_secret_and_the_two_documents(tmp_path) -> None:
    flagged = QaFakes(_sub(tmp_path, "flagged"))
    plain = QaFakes(_sub(tmp_path, "plain"))
    argv = [str(REGISTER), *FULL, "--providers", "anthropic"]
    flagged_proc = flagged.run([*argv, "--terraform-tenant"])
    plain_proc = plain.run(argv)
    assert flagged_proc.returncode == 0, _out(flagged_proc)
    assert plain_proc.returncode == 0, _out(plain_proc)

    flagged_changes, plain_changes = _normalised_changes(flagged), _normalised_changes(plain)
    # MUTATION: skip one more step under the flag and the unflagged run has a change this one lacks.
    assert not (flagged_changes - plain_changes), "the flag makes a gcloud change the plain run does not"
    skipped = plain_changes - flagged_changes
    for (change, _condition), count in skipped.items():
        assert change[:2] in (("secrets", "create"), ("secrets", "add-iam-policy-binding")), change
        assert change[2] == QA, f"the flag skips a change that is not on {QA}: {change}"
    assert sum(n for (c, _), n in skipped.items() if c[1] == "create") == 1, skipped
    assert sum(n for (c, _), n in skipped.items() if c[1] == "add-iam-policy-binding") == 2, skipped

    flagged_patches, plain_patches = _normalised_patches(flagged), _normalised_patches(plain)
    assert not (flagged_patches - plain_patches), "the flag writes a document the plain run does not"
    assert sorted(re.sub(r"\?.*$", "", d) for d in (plain_patches - flagged_patches).elements()) == [
        "pools/tenant:eng",
        "tenants/eng",
    ], plain_patches - flagged_patches


def test_a_terraform_tenant_still_applies_its_namespace(tmp_path) -> None:
    tree = tmp_path / "tree"
    shutil.copytree(REPO / "scripts", tree / "scripts")
    for entry in REPO.iterdir():
        if entry.name not in ("scripts", "kubernetes", ".git"):
            (tree / entry.name).symlink_to(entry)
    (tree / "kubernetes").mkdir()
    for entry in (REPO / "kubernetes").iterdir():
        if entry.name != "apply.sh":
            (tree / "kubernetes" / entry.name).symlink_to(entry)
    apply_sh = tree / "kubernetes" / "apply.sh"
    apply_sh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "printf '%s\\n' \"$@\" | jq -Rnc '{tool:\"apply.sh\", argv:[inputs]}' >>\"${FAKE_LOG}\"\n"
    )
    apply_sh.chmod(0o755)

    fakes = QaFakes(_sub(tmp_path, "fakes"))
    kubectl = fakes.tmp / "bin" / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "case \"$*\" in\n"
        "  \"version --client\"*) echo 'Client Version: v1.31.0' ;;\n"
        "  \"config current-context\") echo swarm-dev ;;\n"
        "  *readyz*) echo ok ;;\n"
        "esac\n"
    )
    kubectl.chmod(0o755)

    proc = fakes.run([
        str(tree / "scripts" / "register-tenant.sh"), "--group", "eng@example.com",
        "--providers", "anthropic", "--terraform-tenant",
    ])
    out = _out(proc)
    assert proc.returncode == 0, out
    applied = [c["argv"] for c in fakes.calls() if c["tool"] == "apply.sh"]
    # MUTATION: skip section 5 under the flag and kubernetes/apply.sh never runs.
    assert len(applied) == 1 and applied[0][:2] == ["--tenant", "eng"] and "--confirm" in applied[0], out
    assert fakes.qa_calls("create") == [] and _document_writes(fakes) == [], out


def _database_answering(fakes: Fakes, answer: str) -> dict[str, str]:
    """Environment putting a curl first on PATH that answers the Firestore
    database probe with `answer` and everything else as the harness's curl."""
    wrapper = fakes.tmp / "db-bin"
    wrapper.mkdir()
    curl = wrapper / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "for a in \"$@\"; do\n"
        "  case \"${a}\" in\n"
        "    https://firestore.googleapis.com/v1/projects/*/databases/*) printf '%s' \"${FAKE_DB_ANSWER}\"; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        f"exec \"{fakes.tmp / 'bin' / 'curl'}\" \"$@\"\n"
    )
    curl.chmod(0o755)
    return {"PATH": f"{wrapper}{os.pathsep}{fakes.env['PATH']}", "FAKE_DB_ANSWER": answer}


MISSING_DATABASE = '{"error":{"code":404,"message":"database not found"}}'
UNREADABLE_DATABASE = '{"error":{"code":403,"message":"permission denied"}}'


def test_the_flag_skips_the_database_check_it_has_no_write_for_but_not_the_preflight(tmp_path) -> None:
    # By design: the closing database check exists to say "the tenant is not yet
    # known to the scheduler" after the documents could not be written. With the
    # flag no document is written here, so a missing database fails nothing of
    # this run's; the release owns the database and the documents.
    flagged = QaFakes(_sub(tmp_path, "flagged"))
    argv = [str(REGISTER), *FULL, "--providers", "anthropic"]
    proc = flagged.run([*argv, "--terraform-tenant"], **_database_answering(flagged, MISSING_DATABASE))
    out = _out(proc)
    assert proc.returncode == 0, out
    assert "does not exist; run 'make infra'" not in out, out
    assert flagged.patches() == [], out

    plain = QaFakes(_sub(tmp_path, "plain"))
    proc = plain.run(argv, **_database_answering(plain, MISSING_DATABASE))
    assert proc.returncode == 1, _out(proc)
    assert "does not exist; run 'make infra'" in _out(proc), _out(proc)

    # A database that cannot be read still stops the flagged run before it creates anything.
    unreadable = QaFakes(_sub(tmp_path, "unreadable"))
    proc = unreadable.run([*argv, "--terraform-tenant"], **_database_answering(unreadable, UNREADABLE_DATABASE))
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "cannot confirm Firestore database" in out, out
    assert unreadable.gcloud_changes() == [], out


def test_the_flag_sends_nobody_to_a_command_that_409s_or_dies_before_the_release(tmp_path) -> None:
    fakes = QaFakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic", "--terraform-tenant"])
    out = _out(proc)
    assert proc.returncode == 0, out
    # The default's two steps, as the plain run prints them: before the release the first is
    # a 409 on swarm-tenant-eng-anthropic and the second dies on a missing tenants/eng.
    assert "store a provider key  scripts/create-secrets.sh" not in out, out
    assert "then add it           scripts/register-tenant.sh" not in out, out
    assert "resolves them" not in out, out
    assert "tenant eng registered" not in out, out
    # After the release: a key is added to the release's secret, and a provider only when absent.
    assert "after the release     store a provider key: scripts/create-secrets.sh --tenant eng --provider anthropic --stdin" in out, out
    assert "only if tenants/eng lacks the provider" in out, out
    assert "tenant record and pool come with the release" in out, out
    # Why nobody resolves yet: the group is not in TENANT_GROUPS, not an absent record.
    assert "TENANT_GROUPS" in out, out
