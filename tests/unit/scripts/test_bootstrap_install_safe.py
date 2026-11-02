"""terraform/bootstrap is safe to apply to a project being installed (DAYONE-BOOT-1, part of #2688).

Three things a fresh project needs from this root, each held to the HCL that
does it (docs/design/day-one-install.md §5.5 and §5.6, plan R1):

* THE STATE BUCKET IS IMPORTED, NEVER CREATED TWICE. The backend needs the
  bucket before `terraform init`, so it always exists before this root plans
  (scripts/bootstrap.sh makes it; the installer's stage 1 will). Without an
  `import` block the first apply on a new project tries to create it again and
  fails with a 409; with an unconditional one, Terraform adopts it.
* AN INSTALL MAY DECLARE NO TERRAFORM TENANTS. Installs pass
  `infra_tenants_tfvars = ""` (their workspaces are made by
  swarm-workspace-apply, not by `var.tenants`), so the precondition that the
  infra tfvars hold at least one tenant applies only when a file is named, and
  no file is read when none is.
* THE WORKSPACE JOB TRUSTS THE PROJECT'S OWN KEY. Its fixed command carries
  `--spec-signing-key=` rendered from the step-spec key this root reads the
  verify keys of, so the key and the public keys pinned beside it are one
  key's (images/workspace-apply/entry.py validates the argument).

No Terraform binary and no cloud: these read the HCL as text, block by block.
`tests/terraform/workspace_approval_keys.tftest.hcl` plans the job's command.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO / "terraform" / "bootstrap"


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "//")))


def _top_level_blocks(text: str, header: str) -> list[str]:
    """Every top-level block whose opening line starts with HEADER, to its
    closing `}` at column 0 (the files are `terraform fmt`ed)."""
    return re.findall(rf"(?ms)^{re.escape(header)}[^\n]*\{{\n.*?^\}}", _strip_comments(text))


def test_state_bucket_imported() -> None:
    text = (BOOTSTRAP / "state_bucket.tf").read_text()
    imports = _top_level_blocks(text, "import ")
    targets = [block for block in imports if re.search(r"(?m)^\s*to\s*=\s*google_storage_bucket\.state\s*$", block)]
    assert len(targets) == 1, "state_bucket.tf has no `import` block for google_storage_bucket.state"
    [block] = targets
    assert re.search(r'(?m)^\s*id\s*=\s*"\$\{var\.project_id\}/\$\{local\.state_bucket_name\}"\s*$', block), (
        "the import's id is not <project>/<the bucket the resource names>: " + block)
    # Unconditional: the bucket always exists before this root plans.
    assert "for_each" not in block, "the state bucket's import is conditional; it must always run"
    # The resource it adopts is still the one declared here, still undeletable.
    [resource] = _top_level_blocks(text, 'resource "google_storage_bucket" "state"')
    assert 'name     = local.state_bucket_name' in resource
    assert re.search(r"prevent_destroy\s*=\s*true", resource)


def _deployer_admin() -> tuple[str, str]:
    text = (BOOTSTRAP / "deployer_service_accounts.tf").read_text()
    [locals_block] = [b for b in _top_level_blocks(text, "locals ") if "infra_tenants_tfvars_text" in b]
    [grant] = _top_level_blocks(text, 'resource "google_service_account_iam_member" "deployer_admin"')
    return locals_block, grant


def test_empty_tenants_admitted_for_installs() -> None:
    locals_block, grant = _deployer_admin()
    # No file is read when none is named: file("") would fail the plan.
    match = re.search(r"(?m)^\s*infra_tenants_tfvars_text\s*=\s*(.+)$", locals_block)
    assert match, "infra_tenants_tfvars_text is gone from deployer_service_accounts.tf"
    assert re.fullmatch(r'var\.infra_tenants_tfvars == "" \? "" : file\(local\.infra_tenants_tfvars_path\)',
                        match.group(1).strip()), match.group(1)
    # The at-least-one-tenant precondition holds only when a file is named.
    conditions = re.findall(r"(?m)^\s*condition\s*=\s*(.+)$", grant)
    [tenants] = [c for c in conditions if "length(local.infra_tenants_block)" in c]
    assert tenants.strip() == (
        'var.infra_tenants_tfvars == "" || (length(local.infra_tenants_block) == 1 '
        "&& length(local.infra_tenant_ids) > 0)"), tenants
    # The other two still hold, unconditionally: a heredoc is refused and the
    # deployer is never admin over its own account or CI's.
    assert any('!strcontains(local.infra_tenants_body, "<<")' in c for c in conditions)
    assert any("setintersection" in c and "deployer_admin_excluded" in c for c in conditions)


def test_workspace_job_command_names_the_projects_own_signing_key() -> None:
    text = (BOOTSTRAP / "workspace_deployer.tf").read_text()
    [locals_block] = [b for b in _top_level_blocks(text, "locals ") if "workspace_apply_command" in b]
    command = re.search(r"(?ms)^\s*workspace_apply_command\s*=\s*\[\n(.*?)^\s*\]", locals_block)
    assert command, "workspace_apply_command is gone"
    elements = [e.strip() for e in command.group(1).replace("\n", " ").split(",") if e.strip()]
    assert elements == [
        '"python3"', '"-I"', '"/opt/swarm/entry.py"',
        '"--spec-verify-keys-sha256=${sha256(local.workspace_spec_verify_keys)}"',
        '"--spec-signing-key=${local.workspace_spec_signing_key}"',
    ], elements
    key = re.search(r"(?m)^\s*workspace_spec_signing_key\s*=\s*(.+)$", locals_block)
    assert key, "workspace_spec_signing_key is not rendered"
    # The key whose versions SPEC_VERIFY_KEYS holds: the module the verify keys
    # come from, so the trusted key and the pinned public keys are one key's.
    assert key.group(1).strip() == (
        'var.enable_workspace_deployer ? module.workspace_spec_keys[0].crypto_key_id : ""'), key.group(1)
