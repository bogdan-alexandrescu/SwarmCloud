"""`scripts/register-tenant.sh` makes swarm-api the second reader of `-git`.

Owner decision 2026-10-02: the issue preview (swarm-api `forge.py` `preview`,
`routes/issues.py`, #511) reads each tenant's `swarm-tenant-<tenant>-git`, so
swarm-api's service account is that secret's second accessor beside the
tenant's worker. terraform/infra's `forge_readers` grants it only on a `-git`
secret Terraform manages, and none is: every tenant's `-git` is registered by
this script. Without the grant here the preview answers no_access on dev.

What has to hold:

  * the binding is made on `-git` by BOTH paths that grant it to the worker --
    `--add-provider git` and a full registration listing git -- as a binding on
    that one secret, never on the project;
  * a re-run on a tenant that already has it adds nothing, and one that lacks
    it gains it and nothing else;
  * `--dry-run` prints it and changes nothing;
  * it NEVER reaches `-git-merge` or `-git-review`: those App keys are
    retired (owner decision MS0-Q4, 2026-10-06) and the script refuses them
    before anything is read, so swarm-api is never bound to one;
  * swarm-api's address is read off terraform/modules/service_account_ids,
    not restated.

The harness is test_register_tenant_merge_step's: the real script, a fake
`gcloud` and `curl` first on PATH, nothing reaching a real project.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from .test_register_tenant_merge_step import (
    FULL,
    REGISTER,
    REPO,
    SA_IDS,
    WORKER,
    Fakes,
    _derive,
    _out,
    _sa,
    _tenant_document,
)

API = _sa("swarm-api")
API_MEMBER = f"serviceAccount:{API}"
GIT = "swarm-tenant-eng-git"
ACCESSOR = "roles/secretmanager.secretAccessor"


def _bound(fakes: Fakes, secret: str, *members: str) -> None:
    """Give SECRET a policy in which MEMBERS already hold secretAccessor."""
    policies = fakes.tmp / "secret-policies"
    policies.mkdir(exist_ok=True)
    (policies / f"{secret}.json").write_text(json.dumps({
        "version": 1,
        "etag": "BwX=",
        "bindings": [{"role": ACCESSOR, "members": list(members)}],
    }))
    fakes.env["FAKE_SECRET_POLICIES"] = str(policies)


def _secret_binding_calls(fakes: Fakes) -> list[list[str]]:
    return [
        c["argv"] for c in fakes.gcloud_changes()
        if "add-iam-policy-binding" in c["argv"] and API_MEMBER in c["argv"]
    ]


def _add_git(fakes: Fakes, *extra: str):
    return fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", "git", *extra])


# ---------------------------------------------------------------------------
# --add-provider git
# ---------------------------------------------------------------------------


def test_add_provider_git_binds_the_worker_and_swarm_api_on_that_one_secret(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = _add_git(fakes)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.secret_grants() == [
        (GIT, f"serviceAccount:{WORKER}"),
        (GIT, API_MEMBER),
    ], f"the worker first, then swarm-api, on {GIT} alone:\n{out}"
    # Per secret, never project-wide: a project-level accessor reads every
    # tenant's key (docs/multi-tenancy.md §4, Secrets).
    for argv in _secret_binding_calls(fakes):
        assert argv[:3] == ["secrets", "add-iam-policy-binding", GIT], argv
        assert argv[argv.index("--role") + 1] == ACCESSOR, argv
    assert not any(
        c["argv"][:2] == ["projects", "add-iam-policy-binding"] for c in fakes.gcloud_changes()
    ), out
    patches = fakes.patches()
    assert len(patches) == 1 and '"git"' in patches[0]["body"], out


def test_add_provider_git_rerun_with_the_binding_present_adds_nothing_for_swarm_api(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git"]))
    _bound(fakes, GIT, f"serviceAccount:{WORKER}", API_MEMBER)
    proc = _add_git(fakes)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert _secret_binding_calls(fakes) == [], f"a binding already present was made again:\n{out}"
    assert "swarm-api already reads" in out, out
    assert fakes.patches() == [], f"a tenant that already lists git was rewritten:\n{out}"


def test_add_provider_git_rerun_on_a_tenant_missing_it_adds_only_swarm_api(tmp_path) -> None:
    """The dev case: registered before this change, the worker bound, swarm-api not."""
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git"]))
    _bound(fakes, GIT, f"serviceAccount:{WORKER}")
    proc = _add_git(fakes)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert (GIT, API_MEMBER) in fakes.secret_grants(), out
    assert [s for s, m in fakes.secret_grants() if m == API_MEMBER] == [GIT], out
    assert fakes.patches() == [], out


def test_add_provider_git_dry_run_prints_the_binding_and_changes_nothing(tmp_path) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic"]))
    proc = _add_git(fakes, "--dry-run")
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.gcloud_changes() == [] and fakes.patches() == [], out
    would = [line for line in out.splitlines() if "would run: gcloud secrets add-iam-policy-binding" in line]
    assert any(GIT in line and API_MEMBER in line for line in would), would
    assert "would let swarm-api read " + GIT in out, out


# ---------------------------------------------------------------------------
# A full registration that lists git
# ---------------------------------------------------------------------------


def test_a_full_registration_with_git_binds_swarm_api_on_git_alone(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic,git"])
    out = _out(proc)
    assert proc.returncode == 0, out
    grants = fakes.secret_grants()
    assert (GIT, f"serviceAccount:{WORKER}") in grants, out
    assert [s for s, m in grants if m == API_MEMBER] == [GIT], (
        f"swarm-api must read {GIT} and nothing else:\n{grants}\n{out}"
    )


def test_a_full_registration_rerun_with_the_binding_present_adds_nothing_for_swarm_api(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    _bound(fakes, GIT, f"serviceAccount:{WORKER}", API_MEMBER)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic,git"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert _secret_binding_calls(fakes) == [], out
    assert "swarm-api already reads" in out, out


def test_a_full_registration_dry_run_prints_the_binding_and_changes_nothing(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic,git", "--dry-run"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert fakes.gcloud_changes() == [], out
    assert any(
        "would run: gcloud secrets add-iam-policy-binding" in line and GIT in line and API_MEMBER in line
        for line in out.splitlines()
    ), out


def test_a_full_registration_without_git_never_names_swarm_api(tmp_path) -> None:
    fakes = Fakes(tmp_path)
    proc = fakes.run([str(REGISTER), *FULL, "--providers", "anthropic"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert _secret_binding_calls(fakes) == [], out


# ---------------------------------------------------------------------------
# Never the App keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["git-merge", "git-review"])
def test_add_provider_of_an_app_key_never_binds_swarm_api(tmp_path, provider) -> None:
    fakes = Fakes(tmp_path, _tenant_document(["anthropic", "git"]))
    proc = fakes.run([str(REGISTER), "--tenant", "eng", "--add-provider", provider])
    out = _out(proc)
    assert proc.returncode != 0, out
    assert fakes.secret_grants() == [], out
    assert API not in out, f"swarm-api was named on the {provider} path:\n{out}"


# ---------------------------------------------------------------------------
# swarm-api's address comes from terraform/modules/service_account_ids
# ---------------------------------------------------------------------------


def test_swarm_apis_address_is_read_off_terraform(tmp_path) -> None:
    rows = _derive(tmp_path, SA_IDS, "platform_account_email swarm-api; echo")
    assert rows == [API], rows


def test_an_account_terraform_does_not_list_is_refused(tmp_path) -> None:
    rows = _derive(tmp_path, SA_IDS, "platform_account_email swarm-nobody || echo refused")
    assert rows == ["refused"], rows


def test_a_renamed_platform_account_in_terraform_moves_the_script_with_it(tmp_path) -> None:
    changed = tmp_path / "main.tf"
    text = SA_IDS.read_text()
    assert '"swarm-api" = {' in text
    changed.write_text(text.replace('"swarm-api" = {', '"swarm-front" = {'))
    rows = _derive(
        tmp_path, changed,
        "platform_account_email swarm-api || echo refused",
        "platform_account_email swarm-front; echo",
    )
    assert rows == ["refused", _sa("swarm-front")], rows


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_the_touched_scripts_are_shellcheck_clean() -> None:
    proc = subprocess.run(
        ["shellcheck", "-x", "scripts/register-tenant.sh", "scripts/lib/common.sh"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
