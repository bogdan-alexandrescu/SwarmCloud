"""The pre-publish checkov-kubernetes check scans what `kubernetes/render.py`
prints on STDOUT, never what it writes to stderr (#1334).

The check used to capture the render through `run_command`, which merges the
two streams, so one warning line made `tenant.yaml` non-YAML and checkov failed
CKV2_K8S_6 on every job pod (measured by lane GS-INFRA, PR 1399: 10 failures
with the merged stream, 0 once the warning left stdout). Run for real: a fake
render.py that warns on stderr, and a fake checkov that parses every file it is
pointed at, the way the real one does before it evaluates CKV2_K8S_6.
"""

from __future__ import annotations

import os
import secrets as pysecrets
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from agent_worker import prepublish

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

RENDER = '''\
import sys
sys.stderr.write("warning: {warning}\\n")
kind = sys.argv[1]
print("apiVersion: v1")
print("kind: ConfigMap")
print("metadata:")
print("  name: " + kind)
'''

PROFILES = "RUNNER_PROFILES = {'small': None}\n"

# Parses every scanned file as YAML; an unparseable one is what makes the real
# checkov fail the NetworkPolicy check on every pod in it.
CHECKOV = '''\
import os, shutil, sys
import yaml
directory = sys.argv[sys.argv.index("-d") + 1]
shutil.copytree(directory, os.environ["SCANNED"])
for name in sorted(os.listdir(directory)):
    try:
        list(yaml.safe_load_all(open(os.path.join(directory, name))))
    except yaml.YAMLError:
        print(f"Check: CKV2_K8S_6 FAILED for resource in {name}")
        sys.exit(1)
print("Passed checks: 3, Failed checks: 0")
'''


def _world(tmp_path: Path, warning: str) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    (repo / "kubernetes").mkdir(parents=True)
    (repo / "kubernetes" / "render.py").write_text(RENDER.format(warning=warning))
    (repo / "apps" / "common" / "swarm_common").mkdir(parents=True)
    (repo / "apps" / "common" / "swarm_common" / "__init__.py").write_text("")
    (repo / "apps" / "common" / "swarm_common" / "profiles.py").write_text(PROFILES)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    checkov = bin_dir / "checkov"
    checkov.write_text(f"#!{sys.executable}\n" + CHECKOV)
    checkov.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
           "SCANNED": str(tmp_path / "scanned")}
    return repo, env


def test_a_render_warning_on_stderr_leaves_the_scanned_yaml_parseable(tmp_path):
    # "warning: render.py: ..." is not YAML: a second `: ` on one line.
    repo, env = _world(tmp_path, "render.py: the tenant's quota is unset; using the default")
    status, output = prepublish._render_and_scan(sys.executable)(
        prepublish.run_command, repo, env, 60.0)
    assert "CKV2_K8S_6" not in output
    assert status == prepublish.PASSED, output
    scanned = tmp_path / "scanned"
    assert sorted(p.name for p in scanned.iterdir()) == ["job-small.yaml", "policies.yaml",
                                                         "tenant.yaml"]
    tenant = (scanned / "tenant.yaml").read_text()
    assert "warning:" not in tenant
    assert yaml.safe_load(tenant)["metadata"]["name"] == "tenant"
    # The warning is not lost: it is reported beside the scan as a note.
    assert "the tenant's quota is unset" in output


def test_the_stderr_note_is_redacted(tmp_path):
    token = "ghp_" + pysecrets.token_hex(18)
    repo, env = _world(tmp_path, f"token={token}")
    status, output = prepublish._render_and_scan(sys.executable)(
        prepublish.run_command, repo, env, 60.0)
    assert status == prepublish.PASSED, output
    assert token not in output
    assert "token=********" in output
