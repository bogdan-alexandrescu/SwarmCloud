"""`scripts/register-tenant.sh --worker-firestore-access`, #1457's switch, inert by default.

A tenant's worker holding swarmTenantWorkerFirestore can read and write any
document whose id it can guess -- another tenant's `workspace_settings` among
them -- and no IAM condition can scope Firestore by tenant. Design B
(docs/design/tenant-settings-integrity.md) moves every worker onto swarm-api's
attempt-signed routes and then removes the grant. terraform/modules/tenancy's
`worker_firestore_access` is that removal for a Terraform-managed tenant; this
flag is the same switch for a tenant this script registers, because the two
paths must grant identical authority.

What has to hold:

  * with no flag, the registration grants the Firestore role exactly as before;
  * `--worker-firestore-access false` grants no Firestore role, makes no
    project-level Firestore call at all, and still makes the tenant's other
    grants;
  * a value other than true or false is refused before anything is called;
  * `--add-provider` and `--workspace` refuse it, as every full-only flag.

The harness is test_register_tenant_merge_step's: the real script, a fake
`gcloud` and `curl` first on PATH, nothing reaching a real project.
"""

from __future__ import annotations

from .test_register_tenant_merge_step import (
    FULL,
    PROJECT,
    REGISTER,
    WORKER,
    Fakes,
    _out,
    _tenant_document,
)

FIRESTORE_ROLE = f"projects/{PROJECT}/roles/swarmTenantWorkerFirestore"


def _firestore_calls(fakes: Fakes) -> list[list[str]]:
    """Every gcloud call, read or write, that names the Firestore role."""
    return [
        c["argv"] for c in fakes.calls()
        if c["tool"] == "gcloud" and any("swarmTenantWorkerFirestore" in a for a in c["argv"])
    ]


def _firestore_grants(fakes: Fakes) -> list[list[str]]:
    return [
        argv for argv in (c["argv"] for c in fakes.gcloud_changes())
        if argv[:2] == ["projects", "add-iam-policy-binding"]
        and argv[argv.index("--role") + 1] == FIRESTORE_ROLE
    ]


def test_by_default_the_worker_gets_the_firestore_role_as_before(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic"])
    out = _out(proc)
    assert proc.returncode == 0, out
    grants = _firestore_grants(fakes)
    assert len(grants) == 1, f"the default must grant the Firestore role once, as before:\n{out}"
    argv = grants[0]
    assert argv[argv.index("--member") + 1] == f"serviceAccount:{WORKER}", argv
    assert argv[argv.index("--condition") + 1] == "None", argv


def test_true_is_the_default_spelled_out(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic", "--worker-firestore-access", "true"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert len(_firestore_grants(fakes)) == 1, out


def test_false_grants_no_firestore_role_and_keeps_the_other_grants(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic", "--worker-firestore-access", "false"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert _firestore_grants(fakes) == [], f"--worker-firestore-access false still granted the role:\n{out}"
    # Not even a describe of the role: once the owner removes it from
    # terraform/bootstrap, a registration that looked for it would stop.
    assert _firestore_calls(fakes) == [], f"the role was still read:\n{out}"
    # Only the Firestore grant goes: the worker still needs its objects and
    # its provider key.
    assert fakes.bucket_changes(), f"the object grants went with the Firestore one:\n{out}"
    assert any(m == f"serviceAccount:{WORKER}" for _, m in fakes.secret_grants()), out
    assert "no Firestore role" in out, out


def test_false_with_dry_run_changes_nothing_and_would_grant_no_firestore_role(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([
        str(REGISTER), *FULL, "--providers", "anthropic", "--worker-firestore-access", "false", "--dry-run",
    ])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.gcloud_changes() == [], out
    assert not any(
        "would run:" in line and "swarmTenantWorkerFirestore" in line and "add-iam-policy-binding" in line
        for line in out.splitlines()
    ), out


def test_a_misspelt_value_is_refused_before_anything_is_called(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--worker-firestore-access", "no"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "takes true or false" in out, out
    assert fakes.calls() == [], f"a refused value still reached gcloud:\n{out}"


def test_add_provider_refuses_the_switch(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = fakes.run([
        str(REGISTER), "--tenant", "eng", "--add-provider", "openai", "--worker-firestore-access", "false",
    ])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "--worker-firestore-access" in out, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out


def test_workspace_refuses_the_switch(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), "--workspace", "w-3f9a2c", "--worker-firestore-access", "false"])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "takes its every value from the approved record" in out, out
    assert fakes.calls() == [], out
