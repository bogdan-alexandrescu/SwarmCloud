"""A job-made workspace's worker reads the managed plugins bucket: platform/ and its own tenants/<t>/.

terraform/infra/plugins.tf (PR 3485, managed-plugins.md section 3) grants
roles/storage.objectViewer on the plugins bucket only to the tenants Terraform
declares, and a Day One install declares none, so stage 14's verify.plugins
named the read as pending and `swarm doctor` reported plugins.infrastructure
unmet. The owner picked scripts/register-tenant.sh for the grant (2026-11-06):
section W, beside the artifact bucket grants, under the same call guard.

What these tests hold:

* `plugins_read_expr` renders plugins.tf's `local.plugins_tenant_read`
  character for character, with startsWith and never matches(), within the
  condition limits, and names its own tenant's prefix and no other;
* when the guard's expectation names the plugins bucket, A5 makes the one
  conditioned binding, a re-run adds no second one, A9 (--mode verify) finds a
  missing one, and an expectation whose read names another tenant stops at A1
  with nothing made;
* when it does not (today's guard), a create runs as before.

The guard and its rules are not this lane's files: the copy below patches
workspace-guard.sh's `expect` and workspace-calls.json's C1 and C4 the way the
lane's question asks the owner to (questions.json, the pull request). Nothing
here needs credentials, a network or a cluster.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_register_tenant_workspace import (  # noqa: F401  (fixtures, by name)
    ADMIN,
    PROJECT,
    REPO,
    TENANT,
    WORKER,
    WORKSPACE,
    Job,
    _out,
    _s,
    created,
)

SCRIPT = REPO / "scripts" / "register-tenant.sh"
PLUGINS_TF = REPO / "terraform" / "infra" / "plugins.tf"
#: plugins.tf's default name, `<project>-swarm-plugins`.
PLUGINS = f"{PROJECT}-swarm-plugins"
PLUGINS_URL = f"gs://{PLUGINS}"
MEMBER = f"serviceAccount:{WORKER}"
VIEWER = "roles/storage.objectViewer"
#: Cloud IAM's limits on a condition (cloud.google.com/iam/quotas).
EXPRESSION_MAX = 12800
TITLE_MAX = 100


def _render(bucket: str, tenant: str) -> str:
    """The script's own `plugins_read_expr`, executed in bash."""
    body = re.search(r"^plugins_read_expr\(\) \{\n.*?^\}\n", SCRIPT.read_text(), re.M | re.S)
    assert body, "scripts/register-tenant.sh defines no plugins_read_expr"
    done = subprocess.run(["bash", "-c", f'set -euo pipefail\n{body.group(0)}\nplugins_read_expr "$1" "$2"', "_",
                           bucket, tenant], capture_output=True, text=True, check=True)
    return done.stdout


def _terraform(bucket: str, tenant: str) -> str:
    """plugins.tf's local.plugins_tenant_read for one tenant, from its own text."""
    text = PLUGINS_TF.read_text()
    objects = re.search(r'^\s*plugins_objects\s*=\s*"(.*)"\s*$', text, re.M).group(1)
    platform = re.search(r'^\s*platform\s*=\s*"(.*)"\s*$', text, re.M).group(1)
    read = re.search(r'^\s*t => "(.*)"\s*$', text, re.M).group(1)
    objects = objects.replace("${local.plugins_bucket}", bucket)
    platform = platform.replace("${local.plugins_objects}", objects)
    read = (read.replace("${local.plugins_prefix.platform}", platform)
            .replace("${local.plugins_objects}", objects).replace("${t}", tenant))
    assert "${" not in read, read
    return read.replace('\\"', '"')


def test_the_read_is_plugins_tf_s_and_names_only_its_own_tenant() -> None:
    expr = _render(PLUGINS, TENANT)
    assert expr == _terraform(PLUGINS, TENANT)
    assert expr == (f'resource.name.startsWith("projects/_/buckets/{PLUGINS}/objects/platform/") || '
                    f'resource.name.startsWith("projects/_/buckets/{PLUGINS}/objects/tenants/{TENANT}/")')
    assert "matches(" not in expr
    # Exactly two prefixes: platform/ and this tenant's, with its trailing
    # slash, so u-alice never reads u-alice2's tenants/ either.
    prefixes = re.findall(r'resource\.name\.startsWith\("([^"]*)"\)', expr)
    assert prefixes == [f"projects/_/buckets/{PLUGINS}/objects/platform/",
                        f"projects/_/buckets/{PLUGINS}/objects/tenants/{TENANT}/"]
    other = _render(PLUGINS, "u-bob")
    assert f"/tenants/{TENANT}/" not in other and "/tenants/u-bob/" in other
    longest = _render("x" * 63, "u-" + "a" * 61)
    assert len(longest) < EXPRESSION_MAX
    assert len(f"swarm-plugins-read-{'u-' + 'a' * 61}") <= TITLE_MAX


# ---------------------------------------------------------------------------
# The job, behind a guard that names the plugins bucket
# ---------------------------------------------------------------------------

#: The guard's `expect`, plus the three plugins names. TEST_PLUGINS_TENANT
#: lets a test hand it another tenant's read.
EXPECT_PATCH = (
    "' | jq --arg pb \"${TEST_PLUGINS_BUCKET}\" --arg pt \"${TEST_PLUGINS_TENANT:-}\" '"
    '(if $pt == "" then .tenant_id else $pt end) as $t'
    ' | ("projects/_/buckets/\\($pb)/objects") as $o'
    ' | . + {plugins_bucket: $pb, plugins_bucket_url: "gs://\\($pb)",'
    ' plugins_read_expr: "resource.name.startsWith(\\"\\($o)/platform/\\") || '
    'resource.name.startsWith(\\"\\($o)/tenants/\\($t)/\\")"}'
    "' | guard_write_private \"${file}\""
)


def _patched_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    for entry in REPO.iterdir():
        if entry.name != "scripts":
            (repo / entry.name).symlink_to(entry)
    shutil.copytree(REPO / "scripts", repo / "scripts", symlinks=True)
    guard = repo / "scripts" / "lib" / "workspace-guard.sh"
    text = guard.read_text()
    old = "      ' | guard_write_private \"${file}\""
    assert text.count(old) == 1
    guard.write_text(text.replace(old, "      " + EXPECT_PATCH))
    rules_file = repo / "scripts" / "lib" / "workspace-calls.json"
    rules = json.loads(rules_file.read_text())
    by_id = {rule["id"]: rule for rule in rules["rules"]}
    by_id["C1"]["shapes"].append(
        {"tool": "gcloud", "command": ["storage", "buckets", "get-iam-policy"], "args": ["{plugins_bucket_url}"]})
    by_id["C4"]["shapes"].append(
        {"tool": "gcloud", "command": ["storage", "buckets", "add-iam-policy-binding"],
         "args": ["{plugins_bucket_url}"],
         "flags": {"--member": "serviceAccount:{worker_email}", "--role": "roles/storage.objectViewer",
                   "--condition-from-file": {"file_expression": "{plugins_read_expr}"}},
         "require": ["--member", "--role", "--condition-from-file"]})
    rules_file.write_text(json.dumps(rules, indent=2))
    return repo


class PluginsJob(Job):
    """A workspace job whose guard's expectation names the plugins bucket."""

    def __init__(self, tmp: Path, repo: Path) -> None:
        super().__init__(tmp)
        self.repo = repo
        shims = REPO / "scripts" / "lib" / "guard-bin"
        self.env["PATH"] = self.env["PATH"].replace(str(shims), str(repo / "scripts" / "lib" / "guard-bin"), 1)
        self.env["TEST_PLUGINS_BUCKET"] = PLUGINS
        self.edit(lambda world: world.setdefault("bucket_policies", {}).update(
            {PLUGINS_URL: {"version": 1, "etag": "BwP=", "bindings": []}}))

    def init_guard(self) -> None:
        if self.expect.exists():
            self.expect.unlink()
        Path(str(self.expect) + ".stop").unlink(missing_ok=True)
        done = subprocess.run([str(self.repo / "scripts" / "lib" / "workspace-guard.sh"), "init",
                               "--workspace-id", WORKSPACE], env=self.env,
                              capture_output=True, text=True, timeout=120, check=False)
        assert done.returncode == 0, done.stderr

    def run(self, *args: str, env: dict | None = None, init: bool = True) -> subprocess.CompletedProcess[str]:
        if init:
            self.init_guard()
        return subprocess.run([str(self.repo / "scripts" / "register-tenant.sh"), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=900, check=False)

    def plugins_policy(self) -> dict:
        return self.read()["bucket_policies"][PLUGINS_URL]


def _plugins_bindings(policy: dict) -> list[dict]:
    return [b for b in policy.get("bindings", []) if MEMBER in b.get("members", [])]


def _plugins_changes(job: Job, since: int = 0) -> list[list[str]]:
    return [c for c in job.calls("gcloud", since) if "add-iam-policy-binding" in c and PLUGINS_URL in c]


@pytest.fixture(scope="module")
def plugins_repo(tmp_path_factory) -> Path:
    return _patched_repo(tmp_path_factory.mktemp("plugins-repo"))


@pytest.fixture(scope="module")
def plugins_created(plugins_repo: Path, tmp_path_factory) -> dict:
    job = PluginsJob(tmp_path_factory.mktemp("plugins-created"), plugins_repo)
    proc = job.create()
    return {"proc": proc, "world": job.read(), "stopped": job.stopped()}


def test_create_grants_the_worker_platform_and_its_own_tenant_prefix(plugins_created: dict) -> None:
    proc = plugins_created["proc"]
    assert proc.returncode == 0 and not plugins_created["stopped"], _out(proc)
    bindings = _plugins_bindings(plugins_created["world"]["bucket_policies"][PLUGINS_URL])
    assert len(bindings) == 1, bindings
    binding = bindings[0]
    assert binding["role"] == VIEWER
    assert binding["members"] == [MEMBER]
    assert binding["condition"]["title"] == f"swarm-plugins-read-{TENANT}"
    assert binding["condition"]["expression"] == _terraform(PLUGINS, TENANT)
    # The artifact bucket's policy holds no plugins read.
    artifact = plugins_created["world"]["bucket_policy"]
    assert not any("swarm-plugins" in json.dumps(b) for b in artifact.get("bindings", []))
    assert plugins_created["world"]["firestore"][f"workspaces/{TENANT}"]["fields"]["state"] == _s("ready")


def _ready(job: PluginsJob, plugins_created: dict) -> None:
    assert plugins_created["proc"].returncode == 0, _out(plugins_created["proc"])
    world = copy.deepcopy(plugins_created["world"])
    world["calls"] = []
    world["writes"] = []
    job.write(world)


def test_a_rerun_adds_no_second_binding(plugins_repo: Path, plugins_created: dict, tmp_path: Path) -> None:
    job = PluginsJob(tmp_path, plugins_repo)
    _ready(job, plugins_created)
    job.edit(lambda world: world["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        state=_s("failed"),
        retry={"mapValue": {"fields": {"by": _s(ADMIN), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}}))
    proc = job.create()
    assert proc.returncode == 0, _out(proc)
    assert _plugins_changes(job) == []
    assert len(_plugins_bindings(job.plugins_policy())) == 1
    assert "plugins: read of platform/ and tenants/u-alice/ already granted" in proc.stdout + proc.stderr


def test_verify_reads_the_plugins_grant(plugins_repo: Path, plugins_created: dict, tmp_path: Path) -> None:
    job = PluginsJob(tmp_path, plugins_repo)
    _ready(job, plugins_created)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 0, _out(proc)
    assert any("get-iam-policy" in c and PLUGINS_URL in c for c in job.calls("gcloud"))
    assert _plugins_changes(job) == []


def test_verify_finds_a_missing_plugins_grant(plugins_repo: Path, plugins_created: dict, tmp_path: Path) -> None:
    job = PluginsJob(tmp_path, plugins_repo)
    _ready(job, plugins_created)
    job.edit(lambda world: world["bucket_policies"][PLUGINS_URL].update(bindings=[]))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    fields = job.record()
    assert fields["state"] == _s("failed")
    failure = fields["failure"]["mapValue"]["fields"]
    assert failure["object"] == _s("plugins grant"), failure
    assert _plugins_changes(job) == []


def test_an_expectation_naming_another_tenant_s_prefix_stops_at_a1(plugins_repo: Path, tmp_path: Path) -> None:
    job = PluginsJob(tmp_path, plugins_repo)
    env = dict(job.env, TEST_PLUGINS_TENANT="u-bob")
    proc = job.run("--workspace", WORKSPACE, env=env)
    assert proc.returncode == 1, _out(proc)
    assert "plugins read that is not tenant u-alice's" in proc.stderr
    assert _plugins_changes(job) == []
    assert job.plugins_policy()["bindings"] == []
    assert not any(c for c in job.calls("gcloud") if "add-iam-policy-binding" in c or "create" in c)


def test_without_the_plugins_bucket_in_the_expectation_a_create_makes_no_plugins_call(created: dict) -> None:
    """Today's guard names no plugins bucket: the run is as before, and says so."""
    assert created["returncode"] == 0, created["stderr"][-6000:]
    assert "the guard's expectation names no plugins bucket" in created["stdout"] + created["stderr"]
    calls = [c["argv"] for c in created["world"].get("calls", [])]
    assert not any("swarm-plugins" in " ".join(c) for c in calls)


def test_the_patched_guard_is_only_this_test_s(plugins_repo: Path) -> None:
    """The repository's own guard is untouched by the copy."""
    assert "TEST_PLUGINS_BUCKET" not in (REPO / "scripts" / "lib" / "workspace-guard.sh").read_text()
    assert os.path.islink(plugins_repo / "apps")
