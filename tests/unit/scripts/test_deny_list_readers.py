"""Who reads the never-touch list, and how narrowly.

Acceptance was down because accept.yml's swarm-accept identity and the
swarm-verify job's identity could not list config/<env>/ in the state bucket,
and sourcing scripts/lib/common.sh with a project loads the list from there.
The loader now reads the one object (test_common_config_fetch_one.py); this
file holds the rest of that change:

* terraform/bootstrap/deny_list_readers.tf grants each identity objects.get on
  exactly config/dev/deny-list.json -- an exact-name condition built from the
  bucket resource, never a prefix and never a literal bucket name;
* the swarm-verify job's template sets SWARM_CONFIG_TRANSPORT=rest and
  TF_STATE_BUCKET, and verify-remote.sh passes neither per execution;
* the three acceptance scripts that touch GitHub only never load the list.

Text and offline runs only: no provider, no plan, no credential.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

# These prove what loads the list and what does not: none is served for them.
pytestmark = pytest.mark.no_placeholder_deployment

REPO = Path(__file__).resolve().parents[3]
READERS_TF = REPO / "terraform" / "bootstrap" / "deny_list_readers.tf"
ACCEPTANCE_TF = REPO / "terraform" / "bootstrap" / "acceptance.tf"
VERIFY_TF = REPO / "terraform" / "infra" / "verify.tf"
VERIFY_REMOTE = REPO / "scripts" / "verify-remote.sh"
GITHUB_ONLY = [REPO / "scripts" / "acceptance" / f"{n}.sh" for n in ("sandbox-sync", "github-verify", "github-cleanup")]
RESOURCE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{(.*?)^\}', re.MULTILINE | re.DOTALL)
EXACT = ('resource.name == \\"projects/_/buckets/${google_storage_bucket.state.name}'
         '/objects/config/dev/deny-list.json\\"')


def _code(text: str) -> str:
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def _resources() -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(READERS_TF.parent.glob("*.tf")):
        for m in RESOURCE.finditer(_code(path.read_text())):
            if "deny_list" in m.group(2):
                found[m.group(2)] = (m.group(1), m.group(3))
    return found


def _attr(body: str, name: str) -> str:
    m = re.search(rf"^\s*{name}\s*=\s*(.+)$", body, re.MULTILINE)
    assert m, f"no {name} in\n{body}"
    return m.group(1).strip()


def test_exactly_two_rows_grant_the_list_one_per_identity() -> None:
    rows = _resources()
    assert set(rows) == {"acceptance_reads_deny_list", "verify_reads_deny_list"}
    for kind, _ in rows.values():
        assert kind == "google_storage_bucket_iam_member"
    acc = rows["acceptance_reads_deny_list"][1]
    ver = rows["verify_reads_deny_list"][1]
    assert _attr(acc, "count") == "local.wif_enabled"
    assert _attr(acc, "member") == '"serviceAccount:${google_service_account.acceptance[0].email}"'
    assert not re.search(r"^\s*count\s*=", ver, re.MULTILINE)
    assert _attr(ver, "member") == '"serviceAccount:${google_service_account.verify.email}"'


@pytest.mark.parametrize("row", ["acceptance_reads_deny_list", "verify_reads_deny_list"])
def test_each_row_reaches_that_one_object_and_no_prefix(row: str) -> None:
    # MUTATION: a startsWith(".../objects/config/dev/") condition, objectViewer,
    # or the bucket's name as a literal, and this goes red.
    body = _resources()[row][1]
    assert _attr(body, "bucket") == "google_storage_bucket.state.name"
    assert _attr(body, "role") == '"roles/storage.legacyObjectReader"'
    assert body.count("condition {") == 1
    assert _attr(body, "expression") == f'"{EXACT}"'
    assert "startsWith" not in body and "||" not in body and "&&" not in body
    description = _attr(body, "description")
    assert "dev" in description.lower() and "noncurrent generation" in description
    assert "managed-by=swarm-terraform" in description


def test_the_acceptance_identity_header_names_the_grant() -> None:
    text = ACCEPTANCE_TF.read_text()
    header = text.split("resource ", 1)[0]
    assert "config/dev/deny-list.json" in header and "deny_list_readers.tf" in header


def _verify_env() -> dict[str, str]:
    code = _code(VERIFY_TF.read_text())
    return dict(re.findall(r'env\s*\{\s*name\s*=\s*"([A-Z_]+)"\s*value\s*=\s*([^\n]+?)\s*\n\s*\}', code))


def test_the_verify_job_reads_the_list_over_rest_from_its_own_template() -> None:
    env = _verify_env()
    assert env.get("SWARM_CONFIG_TRANSPORT") == '"rest"'
    assert env.get("TF_STATE_BUCKET") == '"${var.name_prefix}-tfstate-${var.project_id}"'
    assert {"PROJECT_ID", "ENVIRONMENT"} <= set(env)


def test_verify_remote_passes_neither_setting_per_execution() -> None:
    # The bucket the list is read from is the template's, never a caller's.
    code = _code(VERIFY_REMOTE.read_text())
    assert "TF_STATE_BUCKET" not in code
    assert "SWARM_CONFIG_TRANSPORT" not in code
    header = VERIFY_REMOTE.read_text().split("set -euo pipefail", 1)[0]
    assert "config/dev/deny-list.json" in header and "deny_list_readers.tf" in header


@pytest.mark.parametrize("script", GITHUB_ONLY, ids=lambda p: p.name)
def test_a_github_only_acceptance_script_says_so_before_it_sources_common(script: Path) -> None:
    lines = script.read_text().splitlines()
    source = next(i for i, line in enumerate(lines) if line.startswith("source ") and "lib/common.sh" in line)
    opt_out = [i for i, line in enumerate(lines) if line.strip() == "SWARM_NO_PROJECT=1"]
    assert opt_out and opt_out[0] < source, script.name


@pytest.mark.parametrize("script", GITHUB_ONLY, ids=lambda p: p.name)
def test_a_github_only_acceptance_script_never_loads_the_list(tmp_path: Path, script: Path) -> None:
    # A self-hosted sandbox runner with a project named and no swarm-accept
    # login: the list cannot be read there. MUTATION: drop the opt-out and the
    # script stops on the list's refusal, having called gcloud for it.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("gcloud", "curl", "gh", "git"):
        fake = bindir / tool
        fake.write_text(f'#!/usr/bin/env bash\nprintf "{tool} %s\\n" "$*" >>"{tmp_path}/calls.log"\nexit 1\n')
        fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SWARM_", "ACC_", "GITHUB_", "GH_")) and k not in ("PROJECT_ID", "TF_STATE_BUCKET")}
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}",
        PROJECT_ID="example-project", REGION="us-central1", ENVIRONMENT="dev",
        SWARM_ENV_FILE=str(tmp_path / "no-env-file"),
        XDG_CACHE_HOME=str(tmp_path / "cache"), HOME=str(tmp_path),
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60,
                          check=False, env=env, cwd=tmp_path)
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "never-touch list" not in proc.stderr, proc.stderr
    assert "deny-list.json" not in calls + proc.stderr
    assert not (tmp_path / "cache" / "swarm").exists()
