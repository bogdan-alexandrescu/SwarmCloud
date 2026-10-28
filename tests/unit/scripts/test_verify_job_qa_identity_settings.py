"""The swarm-verify job carries the QA identity case's two settings (lane
QA-IDENTITY-WIRE, part of #1649; BGD's decision of 2026-10-27).

`run_live_browser_qa` (scripts/acceptance/groups/live-browser.sh) SKIPs every
check unless SWARM_ACCEPTANCE_QA_TENANT and SWARM_ACCEPTANCE_CONSOLE_URL are
set where the group runs. It runs in the `swarm-verify` Cloud Run job, which
scripts/verify-remote.sh executes with `--args` only: the job's environment is
its Terraform template's, so the settings are there or nowhere.

* the job renders each from its variable, and only when the variable is set,
  so prod, which sets neither, keeps SKIPping rather than carrying an empty
  value;
* dev sets them: the tenant BGD granted the QA identity (`smoke`) and the
  console's URL, which is `https://` + dev's own `frontend_hostname` -- the
  case judges "signed in" by that host, so a URL naming any other host would
  FAIL every run for a reason that is not the identity's.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERIFY_TF = ROOT / "terraform" / "infra" / "verify.tf"
VARIABLES_TF = ROOT / "terraform" / "infra" / "variables.tf"
DEV_TFVARS = ROOT / "terraform" / "environments" / "dev" / "dev.tfvars"
PROD_TFVARS = ROOT / "terraform" / "environments" / "prod" / "prod.tfvars"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "live-browser.sh"

#: env name -> the terraform/infra variable it is rendered from.
SETTINGS = {
    "SWARM_ACCEPTANCE_QA_TENANT": "acceptance_qa_tenant",
    "SWARM_ACCEPTANCE_CONSOLE_URL": "acceptance_console_url",
}


def _tfvar(path: Path, name: str) -> str | None:
    match = re.search(rf'^{name}\s*=\s*"([^"]*)"', path.read_text(), re.M)
    return match.group(1) if match else None


def _verify_job() -> str:
    text = VERIFY_TF.read_text()
    start = text.index('resource "google_cloud_run_v2_job" "verify"')
    return text[start:text.index('output "verify_job_name"', start)]


def test_the_group_reads_the_two_settings_the_job_carries():
    text = GROUP.read_text()
    for name in SETTINGS:
        assert f"${{{name}:-}}" in text, name


def test_the_verify_job_renders_each_setting_from_its_variable_only_when_set():
    """MUTATION: render the env unconditionally, or from the other variable."""
    job = _verify_job()
    for env, var in SETTINGS.items():
        block = re.search(
            r'dynamic "env" \{\s*for_each\s*=\s*var\.' + var + r'\s*==\s*""\s*\?\s*\[\]\s*:\s*\[var\.' + var
            + r'\]\s*content \{\s*name\s*=\s*"' + env + r'"\s*value\s*=\s*env\.value\s*\}',
            job)
        assert block, f"swarm-verify must render {env} from var.{var}, and only when it is set"


def test_the_variables_default_to_unset():
    text = VARIABLES_TF.read_text()
    for var in SETTINGS.values():
        block = re.search(r'variable "' + var + r'" \{(.*?)\n\}', text, re.S)
        assert block, var
        assert re.search(r'^\s*default\s*=\s*""', block.group(1), re.M), var


def test_dev_sets_the_granted_tenant_and_its_own_console():
    """MUTATION: point acceptance_console_url at another host."""
    assert _tfvar(DEV_TFVARS, "acceptance_qa_tenant") == "smoke"
    host = _tfvar(DEV_TFVARS, "frontend_hostname")
    assert host
    assert _tfvar(DEV_TFVARS, "acceptance_console_url") == f"https://{host}"


def test_prod_leaves_the_case_skipped():
    """Dev first (BGD, 2026-10-19, Q8): prod's acceptance has no QA tenant."""
    for var in SETTINGS.values():
        assert _tfvar(PROD_TFVARS, var) is None, var
