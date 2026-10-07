"""The #295 merge, post-verdict and review identities are retired (owner decision MS0-Q4, 2026-10-06).

Part of #352 box 4 and #295. The merge step runs as the tenant's worker
account on its `-git` token (contract request 47), and the review writes its
verdict file as an ordinary artifact the merge stages (docs/merge-step.md,
"Revised 2026-10-06"). So nothing runs as `swarm-<tenant>-merge`,
`-post-verdict` or `-review`, nothing reads a `-git-merge` or `-git-review`
App key, and nothing renders a tenant's `forge` record. This holds that the
pieces which fed them are gone from Terraform and from
`scripts/register-tenant.sh`, and that a tenant can no longer register an App
provider at all (#453 box 118).

What is NOT retired, and is held here so the removal cannot take it too:

  * the frozen catalogue's `post-verdict` and `claude-code-review` entries
    (apps/common/swarm_common/profiles.py; contract request 50 is the
    owner's call). terraform/infra mirrors them, and creates no Job for
    either (`profiles_without_a_job`): a Job would run one as the tenant's
    worker account;
  * the worker's two bucket bindings, `worker_objects_read` and
    `worker_objects_write`. Collapsing them back into one `worker_objects`
    is a create, and this change creates nothing.

FAILS WITHOUT THE CHANGE: every module named below still declared the
accounts, their grants, the forge record and the git-review/git-merge
registration path, and terraform/infra accepted a tenant listing either
provider.

Static reads only: `terraform test` (tests/terraform/merge_step_iam.tftest.hcl)
plans the same modules in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES

REPO = Path(__file__).resolve().parents[3]
TF = REPO / "terraform"
SA_IDS = TF / "modules" / "service_account_ids"
TENANCY = TF / "modules" / "tenancy"
INFRA = TF / "infra"
REGISTER = REPO / "scripts" / "register-tenant.sh"

RETIRED_PROFILES = ("post-verdict", "claude-code-review")
APP_PROVIDERS = ("git-merge", "git-review")


def _code(path: Path) -> str:
    """The file with `#` comments stripped, so a sentence about a removed
    name does not read as the name still being declared."""
    out = []
    for line in path.read_text().splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def _tf(directory: Path) -> str:
    return "\n".join(_code(p) for p in sorted(directory.glob("*.tf")))


# ---------------------------------------------------------------------------
# modules/service_account_ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["action_accounts", "action_account_prefix", "action_ids", "tenant_providers"])
def test_service_account_ids_no_longer_derives_an_action_account(name):
    text = _tf(SA_IDS)
    assert not re.search(rf"\b{name}\b", text), f"modules/service_account_ids still declares {name}"


def test_infra_managed_is_the_platform_and_worker_accounts_alone():
    text = " ".join(_tf(SA_IDS).split())
    m = re.search(r"infra_managed = sort\(concat\((.*?)\)\)", text)
    assert m, "modules/service_account_ids no longer builds infra_managed the way this test reads it"
    parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
    assert parts == [
        "keys(local.platform)",
        "[local.tick_id",
        "local.verify_id",
        "local.rollup_sweeper_id]",
        "values(local.worker_ids)",
    ], parts


# ---------------------------------------------------------------------------
# modules/tenancy
# ---------------------------------------------------------------------------


def test_tenancy_declares_no_action_or_review_resource():
    declared = re.findall(r'^resource "([^"]+)" "([^"]+)"', _tf(TENANCY), re.M)
    retired = [f"{t}.{n}" for t, n in declared if n.startswith(("action", "review_"))]
    assert retired == [], retired
    # The control: the worker's own resources are still read.
    names = {n for _, n in declared}
    assert {"worker", "worker_firestore", "worker_objects_read", "worker_objects_write", "act_as"} <= names, names


@pytest.mark.parametrize(
    "name",
    ["action_act_as_members", "action_act_as_grants", "secret_readers", "action_profiles",
     "action_providers", "action_members", "action_service_accounts", "verdicts_expression"],
)
def test_tenancy_no_longer_feeds_the_action_accounts(name):
    assert not re.search(rf"\b{name}\b", _tf(TENANCY)), f"modules/tenancy still declares {name}"


def test_no_tenants_variable_carries_a_forge_record():
    for root in (TENANCY, INFRA):
        assert not re.search(r"^\s*forge\s*=\s*optional\(", _tf(root), re.M), f"{root} still declares tenants.*.forge"
    assert not re.search(r'^output "forge"', _tf(TENANCY), re.M)


def test_the_worker_bucket_split_is_kept_unchanged():
    """Collapsing it into one binding is a create; this change creates nothing."""
    text = _tf(TENANCY)
    assert 'resource "google_storage_bucket_iam_member" "worker_objects_read"' in text
    assert 'resource "google_storage_bucket_iam_member" "worker_objects_write"' in text
    assert 'write_expression    = { for t, _ in var.tenants : t => "${local.object_prefix[t]} && !${local.verdicts_prefix[t]}" }' in text


# ---------------------------------------------------------------------------
# terraform/infra
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["action_profiles", "forge_env_names", "forge_env", "action_key", "action_act_as_members",
     "accessor_overrides", "action_providers", "action_members", "action_service_accounts"],
)
def test_infra_no_longer_renders_an_action_job_or_grant(name):
    assert not re.search(rf"\b{name}\b", _tf(INFRA)), f"terraform/infra still reads {name}"


def test_the_retired_profiles_get_no_job_and_are_still_disabled():
    text = " ".join(_tf(INFRA).split())
    m = re.search(r"profiles_without_a_job = \[([^\]]*)\]", text)
    assert m, "terraform/infra declares no profiles_without_a_job list"
    listed = sorted(re.findall(r'"([a-z0-9-]+)"', m.group(1)))
    assert listed == sorted(RETIRED_PROFILES), listed
    assert "!contains(local.profiles_without_a_job, profile_name)" in text
    # The premise: the frozen catalogue still holds both, disabled.
    for name in RETIRED_PROFILES:
        assert name in RUNNER_PROFILES and RUNNER_PROFILES[name].available is False, name


def test_infra_refuses_a_tenant_registering_an_app_provider():
    """#453 box 118: the providers no account reads any more."""
    text = " ".join(_tf(INFRA).split())
    blocks = re.findall(r"validation \{ condition = (.*?) error_message = \"([^\"]*)\"", text)
    refusing = [c for c, _ in blocks if all(f'"{p}"' in c for p in APP_PROVIDERS) and "var.tenants" in c]
    assert refusing, "terraform/infra has no validation refusing a tenant provider git-merge or git-review"


# ---------------------------------------------------------------------------
# scripts/register-tenant.sh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["app_key_readers", "require_action_account", "forge_tfvars", "resolve_review_bot_id",
     "REVIEW_BOT_ID", "tf_action_accounts", "tenant_action_account_id", "TFVARS_FILE", "--tfvars"],
)
def test_register_tenant_has_no_app_key_registration_path(name):
    assert name not in _code(REGISTER), f"scripts/register-tenant.sh still carries {name}"
