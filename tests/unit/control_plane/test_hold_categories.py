"""The hold categories every issue run in a `platform: true` repository meets
(docs/design/failure-review.md §6, owner decision Q7; lane FR-1).

`hard_stop_hold` held IAM and, in a `platform: true` repository, the frozen
contract. Q7 adds four categories there, for every issue run in such a
repository whoever started it: `secrets`, `tenancy`, `shared` and `cost`.
Each holds NEEDS_OWNER (owner_only). In any other repository the stops are
today's -- IAM, the registration's protected paths, a security-class issue --
because these four name this platform's own files.

The checks are pure, so these tests call `hard_stop_hold` and `plan_hold`
directly: no app, no Firestore, no credentials, no network.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from swarm_api import issueruns

from .test_issue_runs import PLAN

REPO = Path(__file__).resolve().parents[3]


def _plan(*files: str, prompt: str = "Change what the issue asks.") -> dict:
    plan = copy.deepcopy(PLAN)
    plan["steps"][0]["files"] = list(files)
    plan["steps"][0]["prompt"] = prompt
    return plan


def _hold(paths, *, platform: bool = True, patches=None, prompt_text: str = ""):
    return issueruns.hard_stop_hold(
        paths, platform=platform, hard_stop_paths=[], patches=patches, prompt_text=prompt_text,
    )


def _patch(*added: str, removed: tuple[str, ...] = (), header: str = "") -> str:
    lines = ["--- a/x", "+++ b/x", f"@@ -1,3 +1,4 @@{(' ' + header) if header else ''}", " unchanged"]
    lines += [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


def _deny_list_from_common_sh() -> list[str]:
    text = (REPO / "scripts/lib/common.sh").read_text()
    block = re.search(r"^SHARED_DENY_LIST=\(\n(.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert block, "scripts/lib/common.sh no longer declares SHARED_DENY_LIST=( ... )"
    return re.findall(r'^\s*"([^"]+)"', block.group(1), re.MULTILINE)


# --------------------------------------------------------------------------
# secrets
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "scripts/create-secrets.sh",
    "terraform/modules/secret_manager/main.tf",
    "apps/redaction/swarm_redaction/rules.py",
    "apps/swarm-api/swarm_api/forge.py",
])
def test_a_pr_touching_a_secrets_path_is_held_secrets_for_the_owner(path):
    held = _hold([path])
    # MUTATION: drop `path` from SECRETS_PATTERNS and the hold is None.
    assert held is not None and held["reasons"] == ["secrets"]
    # MUTATION: return HOLD_NEEDS_SECOND_MEMBER for a platform hold and this is red.
    assert held["code"] == "NEEDS_OWNER" and held["approvers"] == "owner_only"
    assert held["matched"] == [path]


def test_a_secret_manager_resource_line_is_held_secrets_in_any_file():
    held = _hold(["terraform/infra/qa_targets.tf"], patches={
        "terraform/infra/qa_targets.tf": _patch('resource "google_secret_manager_secret" "qa" {'),
    })
    # MUTATION: stop scanning changed lines for `google_secret_manager_` and the hold is None.
    assert held is not None and held["reasons"] == ["secrets"]
    assert held["matched"] == ["terraform/infra/qa_targets.tf"]


# --------------------------------------------------------------------------
# tenancy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "apps/swarm-api/swarm_api/auth.py",
    "terraform/modules/tenancy/main.tf",
    "kubernetes/network-policies/default-deny.yaml",
])
def test_a_pr_touching_a_tenancy_path_is_held_tenancy(path):
    held = _hold([path])
    # MUTATION: drop `path` from TENANCY_PATTERNS and the hold is None.
    assert held is not None and held["reasons"] == ["tenancy"]
    assert held["code"] == "NEEDS_OWNER"


@pytest.mark.parametrize("path, function", [
    ("apps/swarm-api/swarm_api/deps.py", "tenant_scope"),
    ("apps/swarm-api/swarm_api/deps.py", "submission_scope"),
    ("apps/scheduler/scheduler/dispatch.py", "namespace_for"),
])
def test_a_change_inside_a_tenant_scope_function_is_held_tenancy(path, function):
    inside = _hold([path], patches={path: _patch("    x = 1", header=f"def {function}(")})
    # MUTATION: stop reading the hunk header for TENANCY_FUNCTIONS and this is None.
    assert inside is not None and inside["reasons"] == ["tenancy"]
    elsewhere = _hold([path], patches={path: _patch("    x = 1", header="def unrelated(")})
    # MUTATION: hold every change to the file and this is not None.
    assert elsewhere is None


def test_a_change_just_below_a_tenant_scope_def_is_held_tenancy():
    # Within git's three context lines of the def, the `@@` header names the
    # function before it, and the def is an unchanged context line.
    path = "apps/swarm-api/swarm_api/deps.py"
    patch = "\n".join([
        "--- a/apps/swarm-api/swarm_api/deps.py",
        "+++ b/apps/swarm-api/swarm_api/deps.py",
        "@@ -40,6 +40,6 @@ def current_auth(",
        "     return auth",
        "",
        " def tenant_scope(auth):",
        "-    return auth.tenant",
        '+    return request.headers["x-tenant"]',
        "",
    ]) + "\n"
    held = _hold([path], patches={path: patch})
    # MUTATION: stop scanning a hunk's context lines for a tenant-scope def before a changed line and this is None.
    assert held is not None and held["reasons"] == ["tenancy"]
    # MUTATION: count a tenant-scope def that follows every changed line and this is not None.
    assert _hold([path], patches={path: _patch_def_after()}) is None


def _patch_def_after() -> str:
    return "\n".join([
        "--- a/apps/swarm-api/swarm_api/deps.py",
        "+++ b/apps/swarm-api/swarm_api/deps.py",
        "@@ -40,6 +40,6 @@ def current_auth(",
        "-    return auth",
        "+    return auth or None",
        "",
        " def tenant_scope(auth):",
        "     return auth.tenant",
    ]) + "\n"


def test_a_prompt_naming_a_tenant_scope_function_is_held_at_plan():
    plan = _plan("apps/swarm-api/swarm_api/deps.py", prompt="Widen tenant_scope to accept a header.")
    held = issueruns.plan_hold(plan, platform=True, hard_stop_paths=[])
    # MUTATION: stop passing the prompts' text to the category scan in plan_hold.
    assert held is not None and "tenancy" in held["reasons"]


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------

def test_the_python_deny_list_is_the_one_in_common_sh():
    # MUTATION: drop or add one entry of issueruns.SHARED_DENY_LIST and this is red.
    assert list(issueruns.SHARED_DENY_LIST) == _deny_list_from_common_sh()


@pytest.mark.parametrize("name", [n for n in _deny_list_from_common_sh() if n != "default"])
def test_an_added_line_naming_a_deny_list_entry_is_held_shared(name):
    path = "terraform/infra/network.tf"
    held = _hold([path], patches={path: _patch(f'  network = "{name}"')})
    # MUTATION: skip one entry in the shared scan and its case is None.
    assert held is not None and held["reasons"] == ["shared"]
    assert held["code"] == "NEEDS_OWNER"


def test_a_removed_line_naming_a_deny_list_entry_is_not_held_shared():
    path = "terraform/infra/network.tf"
    # MUTATION: scan removed lines for the deny-list and this is held.
    assert _hold([path], patches={path: _patch("  x = 1", removed=('  network = "agents-staging-vpc"',))}) is None


def test_a_deny_list_name_is_matched_whole_not_inside_a_longer_name():
    path = "terraform/infra/network.tf"
    # "agents-staging" is a deny-list entry; "swarm-agents-staging-x" is not it.
    # MUTATION: drop the name-boundary lookarounds and this is held.
    assert _hold([path], patches={path: _patch('  name = "swarm-agents-staging-x"')}) is None


def test_default_is_held_only_as_a_quoted_name():
    path = "apps/swarm-api/swarm_api/settings.py"
    # MUTATION: match "default" unquoted and this ordinary line is held.
    assert _hold([path], patches={path: _patch("    timeout: int = 30  # the default")}) is None
    held = _hold([path], patches={path: _patch('    network = "default"')})
    assert held is not None and held["reasons"] == ["shared"]


def test_the_deny_list_itself_is_held_shared():
    held = _hold(["scripts/lib/common.sh"])
    # MUTATION: drop scripts/lib/common.sh from SHARED_PATTERNS and this is None.
    assert held is not None and held["reasons"] == ["shared"]


def test_a_prompt_naming_a_deny_list_entry_is_held_at_plan():
    plan = _plan("terraform/infra/network.tf", prompt="Peer our VPC with agents-staging-vpc.")
    held = issueruns.plan_hold(plan, platform=True, hard_stop_paths=[])
    # MUTATION: scan only the patches, never the prompt text, and this is None.
    assert held is not None and held["reasons"] == ["shared"]
    assert "agents-staging-vpc" in held["matched"]


# --------------------------------------------------------------------------
# cost
# --------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "        budget=BudgetCaps(per_run_usd=50, per_day_usd=400, max_concurrent=8),",
    "  max_instance_count = 20",
    "  replicas: 4",
    '    "resource_class": "large",',
])
def test_a_budget_cap_or_a_resource_class_line_is_held_cost(line):
    path = "apps/swarm-api/swarm_api/scheduletypes.py"
    held = _hold([path], patches={path: _patch(line)})
    # MUTATION: drop the line's term from _COST_LINE and its case is None.
    assert held is not None and held["reasons"] == ["cost"]
    assert held["code"] == "NEEDS_OWNER"


@pytest.mark.parametrize("path", [
    "terraform/environments/dev/dev.tfvars",
    "apps/swarm-api/swarm_api/runnersizes.py",
    "apps/scheduler/scheduler/sizes.py",
])
def test_a_pr_touching_a_cost_path_is_held_cost(path):
    held = _hold([path])
    # MUTATION: drop `path` from COST_PATTERNS and the hold is None.
    assert held is not None and held["reasons"] == ["cost"]


def test_a_prompt_naming_a_budget_cap_is_held_cost_at_plan():
    plan = _plan(prompt="Raise the BudgetCaps(per_run_usd=...) of the observer type.")
    held = issueruns.plan_hold(plan, platform=True, hard_stop_paths=[])
    # MUTATION: stop scanning the prompts for _COST_LINE and this is None.
    assert held is not None and held["reasons"] == ["cost"]


# --------------------------------------------------------------------------
# Not a platform repository: today's stops only. A clean change: no hold.
# --------------------------------------------------------------------------

CATEGORY_CHANGES = [
    (["scripts/create-secrets.sh"], None),
    (["apps/swarm-api/swarm_api/auth.py"], None),
    (["scripts/lib/common.sh"], None),
    (["terraform/environments/dev/dev.tfvars"], None),
    (["terraform/infra/network.tf"], {"terraform/infra/network.tf": _patch('  network = "agents-staging-vpc"')}),
    (["apps/x.py"], {"apps/x.py": _patch("budget=BudgetCaps(per_run_usd=1)")}),
    (["apps/x.tf"], {"apps/x.tf": _patch('resource "google_secret_manager_secret" "s" {')}),
]


@pytest.mark.parametrize("paths, patches", CATEGORY_CHANGES)
def test_outside_a_platform_repository_the_categories_hold_nothing(paths, patches):
    # MUTATION: apply the categories whatever `platform` says and each case is held.
    assert _hold(paths, platform=False, patches=patches) is None


def test_outside_a_platform_repository_iam_is_still_held_and_only_iam():
    paths = ["scripts/create-secrets.sh", "terraform/bootstrap/main.tf"]
    held = _hold(paths, platform=False)
    # MUTATION: drop the IAM stop for a non-platform repository and this is None.
    assert held is not None and held["reasons"] == ["iam"]
    assert held["code"] == "NEEDS_SECOND_MEMBER"


def test_in_a_platform_repository_the_categories_join_iam_and_the_contract():
    paths = ["terraform/bootstrap/main.tf", "apps/common/swarm_common/models.py",
             "scripts/create-secrets.sh", "apps/swarm-api/swarm_api/auth.py"]
    held = _hold(paths)
    # MUTATION: return after the first matching category and this is shorter.
    assert held["reasons"] == ["iam", "contract", "secrets", "tenancy"]


def test_a_clean_change_is_not_held():
    path = "apps/swarm-api/swarm_api/schedtypes/observer.py"
    patches = {path: _patch("    log.info('observer tick %s', tick)")}
    # MUTATION: make any category match on an empty pattern and this is held.
    assert _hold([path, "docs/schedules.md"], patches=patches) is None
    assert issueruns.plan_hold(_plan(path), platform=True, hard_stop_paths=[]) is None


def test_the_four_categories_follow_the_hold_switch_as_iam_does():
    """Q7 adds stops; it does not make them always enforced (only `security`
    and `workflows` are). The switch `hold_approver_required` decides."""
    held = _hold(["scripts/create-secrets.sh"])
    # MUTATION: add STOP_SECRETS to ENFORCED_STOPS and this is red.
    assert issueruns.hold_always_enforced(held) is False
