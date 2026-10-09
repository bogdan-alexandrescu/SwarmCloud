"""`scripts/verify-deployer-trust.sh`: the live deployer trust policy against the code (#457).

#465 pinned the deployer's workload identity binding to
`attribute.job_workflow_ref` for the workflow files in
`terraform/bootstrap/wif.tf` `deployer_workflows`, replacing the
repository-wide `attribute.repo_ref/<repo>@<ref>` principalSet that let ANY
workflow on main become the deployer. That is IAM in the bootstrap root, which
only the owner applies, so until the apply the old member may still be live --
and nothing in the code can say whether it is. The verifier reads the live
policy and compares it with the Terraform-rendered `github_principals`. These
tests hold the four ways that comparison could lie:

* a pre-apply policy (repo_ref, repository or subject member) read as pinned;
* a workflow file the deployer should refuse, or an expected one missing,
  passing unnoticed;
* an EMPTY policy passing because nothing in it was wrong (CLAUDE.md: empty
  output is not success);
* the verifier writing to IAM, or restating the workflow list in shell, where
  it would drift from wif.tf.

Nothing here touches the network. The script runs against a fake `gcloud` and
a fake `terraform` first on PATH, each serving fixture JSON and recording every
call.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "verify-deployer-trust.sh"
WORKFLOWS = REPO / ".github" / "workflows"

REPOSITORY = "acme/swarm-test"
PROJECT_NUMBER = "123456789012"
POOL = f"projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/swarm-github"
PROVIDER = f"{POOL}/providers/swarm-github-oidc"
DEPLOYER_EMAIL = "swarm-tf-deployer@swarm-trust-test.iam.gserviceaccount.com"
MAIN = "refs/heads/main"
ROLE = "roles/iam.workloadIdentityUser"
GOOD_CONDITION = f'assertion.repository == "{REPOSITORY}" && (assertion.ref == "{MAIN}")'

# The fixture's deployer workflows. The script must take these from the
# terraform output, never from a list of its own (test_no_workflow_file_is_named_in_the_script).
FIXTURE_WORKFLOWS = ["release.yml", "application.yml", "terraform.yml"]


def _pinned(workflow: str, ref: str = MAIN) -> str:
    return f"principalSet://iam.googleapis.com/{POOL}/attribute.job_workflow_ref/{REPOSITORY}/.github/workflows/{workflow}@{ref}"


REPO_REF = f"principalSet://iam.googleapis.com/{POOL}/attribute.repo_ref/{REPOSITORY}@{MAIN}"
REPOSITORY_MEMBER = f"principalSet://iam.googleapis.com/{POOL}/attribute.repository/{REPOSITORY}"
SUBJECT_MEMBER = f"principal://iam.googleapis.com/{POOL}/subject/repo:{REPOSITORY}:ref:{MAIN}"
CI_FIX = _pinned("ci-fix.yml")


def _expected() -> dict[str, str]:
    """What `terraform output -json github_principals` renders: key -> member."""
    return {f"{w}@{MAIN}": _pinned(w) for w in FIXTURE_WORKFLOWS}


def _policy(*members: str, extra_bindings: list[dict] | None = None) -> dict:
    bindings = [{"role": ROLE, "members": list(members)}] if members else []
    return {"version": 1, "etag": "BwYfake=", "bindings": bindings + list(extra_bindings or [])}


FAKE_GCLOUD = r'''#!__PYTHON__
import json, os, sys

state_path = os.environ["FAKE_TRUST_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
with open(state_path + ".gcloud-calls", "a") as handle:
    handle.write(json.dumps(args) + "\n")

if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    if args[3] != state["deployer"]:
        sys.stderr.write("ERROR: (gcloud.iam.service-accounts.get-iam-policy) NOT_FOUND: " + args[3] + "\n")
        sys.exit(1)
    if state.get("policy_fails"):
        sys.stderr.write("ERROR: (gcloud.iam.service-accounts.get-iam-policy) PERMISSION_DENIED\n")
        sys.exit(1)
    print(json.dumps(state["policy"]))
    sys.exit(0)

if args[:4] == ["iam", "workload-identity-pools", "providers", "describe"]:
    print(json.dumps({"name": state["provider"], "attributeCondition": state["condition"]}))
    sys.exit(0)

sys.stderr.write("fake gcloud: unexpected call " + " ".join(args) + "\n")
sys.exit(97)
'''

FAKE_TERRAFORM = r'''#!__PYTHON__
import json, os, sys

state_path = os.environ["FAKE_TRUST_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
with open(state_path + ".terraform-calls", "a") as handle:
    handle.write(json.dumps(args) + "\n")

rest = [a for a in args if not a.startswith("-chdir=")]
if rest[:1] == ["init"]:
    sys.exit(0)
if rest[:1] == ["output"]:
    name = rest[-1]
    outputs = state["outputs"]
    if name not in outputs:
        sys.stderr.write("Error: Output \"" + name + "\" not found\n")
        sys.exit(1)
    value = outputs[name]
    if "-json" in rest:
        print(json.dumps(value))
    else:
        sys.stdout.write(value)
    sys.exit(0)

sys.stderr.write("fake terraform: unexpected call " + " ".join(args) + "\n")
sys.exit(97)
'''


class Verifier:
    def __init__(
        self,
        tmp_path: Path,
        policy: dict,
        expected: dict[str, str] | None = None,
        condition: str = GOOD_CONDITION,
    ) -> None:
        self.tmp = tmp_path
        self.state_file = tmp_path / "trust-state.json"
        self.state_file.write_text(
            json.dumps(
                {
                    "deployer": DEPLOYER_EMAIL,
                    "provider": PROVIDER,
                    "policy": policy,
                    "condition": condition,
                    "outputs": {
                        "github_deployer_service_account": DEPLOYER_EMAIL,
                        "github_workload_identity_provider": PROVIDER,
                        "github_principals": _expected() if expected is None else expected,
                    },
                }
            )
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name, body in (("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM)):
            tool = bin_dir / name
            tool.write_text(body.replace("__PYTHON__", sys.executable))
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def run(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        # Nothing may resolve terraform anywhere but the fake: not an override,
        # not ~/.local/bin, not the workspace call guard.
        for key in ("SWARM_TERRAFORM", "SWARM_CALL_GUARD"):
            env.pop(key, None)
        env.update(
            {
                "PATH": self.path,
                "HOME": str(self.tmp),
                "FAKE_TRUST_STATE": str(self.state_file),
                # A path with nothing at it: no developer's .env is sourced.
                "SWARM_ENV_FILE": str(self.tmp / "no-env-file"),
                "PROJECT_ID": "swarm-trust-test",
                "NO_COLOR": "1",
            }
        )
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
        )
        proc.output = proc.stdout + proc.stderr  # type: ignore[attr-defined]
        return proc

    def calls(self, tool: str) -> list[list[str]]:
        path = Path(f"{self.state_file}.{tool}-calls")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_pre_apply_policy_holding_repo_ref_fails_and_names_it(tmp_path):
    """The live state before the owner's apply. MUTATION: skip members that are
    not job_workflow_ref, or pass because every expected member is present."""
    proc = Verifier(tmp_path, _policy(REPO_REF, *_expected().values())).run()
    assert proc.returncode != 0, proc.output
    assert REPO_REF in proc.output, proc.output
    assert "bootstrap apply has not happened" in proc.output, proc.output


@pytest.mark.parametrize("member", [REPOSITORY_MEMBER, SUBJECT_MEMBER], ids=["repository", "subject"])
def test_repository_and_subject_members_fail(tmp_path, member):
    """MUTATION: refuse only `attribute.repo_ref`."""
    proc = Verifier(tmp_path, _policy(member, *_expected().values())).run()
    assert proc.returncode != 0, proc.output
    assert member in proc.output, proc.output
    assert "bootstrap apply has not happened" in proc.output, proc.output


def test_a_workflow_file_not_on_the_list_fails_and_is_named(tmp_path):
    """ci-fix.yml grants itself id-token at workflow level and federates as its
    own account. MUTATION: accept any job_workflow_ref in the right repository."""
    proc = Verifier(tmp_path, _policy(CI_FIX, *_expected().values())).run()
    assert proc.returncode != 0, proc.output
    assert CI_FIX in proc.output, proc.output


def test_a_job_workflow_ref_on_another_ref_fails(tmp_path):
    """Same file, a ref the code does not allow. MUTATION: compare file names only."""
    stray = _pinned("release.yml", "refs/heads/lane/x")
    proc = Verifier(tmp_path, _policy(stray, *_expected().values())).run()
    assert proc.returncode != 0, proc.output
    assert stray in proc.output, proc.output


def test_a_missing_expected_member_fails_and_is_named(tmp_path):
    """That file's auth step would fail on main. MUTATION: check only that no
    live member is unexpected."""
    expected = list(_expected().values())
    proc = Verifier(tmp_path, _policy(*expected[1:])).run()
    assert proc.returncode != 0, proc.output
    assert expected[0] in proc.output, proc.output


def test_zero_workload_identity_user_members_fails(tmp_path):
    """Empty is not success. MUTATION: pass when no live member is wrong."""
    other = {"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:someone@example.com"]}
    proc = Verifier(tmp_path, _policy(extra_bindings=[other])).run()
    assert proc.returncode != 0, proc.output
    assert "all pinned" not in proc.output, proc.output
    assert ROLE in proc.output, proc.output


def test_an_empty_expected_set_fails(tmp_path):
    """`enable_github_wif = false`, or a state read from the wrong root, renders
    `{}`. MUTATION: pass an empty live policy against an empty expected set."""
    proc = Verifier(tmp_path, _policy(), expected={}).run()
    assert proc.returncode != 0, proc.output
    assert "all pinned" not in proc.output, proc.output


@pytest.mark.parametrize(
    "condition",
    [
        f'assertion.repository == "{REPOSITORY}" && (assertion.ref == "{MAIN}" || assertion.ref.startsWith("refs/pull/"))',
        "",
    ],
    ids=["refs-pull", "empty"],
)
def test_a_provider_condition_admitting_pull_requests_or_nothing_fails(tmp_path, condition):
    """The boundary tests/terraform/bootstrap.tftest.hcl asserts on the rendered
    condition, read live. MUTATION: skip the provider read."""
    proc = Verifier(tmp_path, _policy(*_expected().values()), condition=condition).run()
    assert proc.returncode != 0, proc.output
    assert "all pinned" not in proc.output, proc.output


def test_an_unreadable_policy_fails_and_is_not_read_as_empty(tmp_path):
    verifier = Verifier(tmp_path, _policy(*_expected().values()))
    state = json.loads(verifier.state_file.read_text())
    state["policy_fails"] = True
    verifier.state_file.write_text(json.dumps(state))
    proc = verifier.run()
    assert proc.returncode != 0, proc.output
    assert "all pinned" not in proc.output, proc.output


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def test_exactly_the_expected_set_passes_and_prints_the_count(tmp_path):
    """MUTATION: print a fixed message, or count something other than the members visited."""
    expected = list(_expected().values())
    # A conditioned second binding on the same role is still read.
    policy = _policy(*expected[:2], extra_bindings=[
        {"role": ROLE, "members": expected[2:]},
        {"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:someone@example.com"]},
    ])
    proc = Verifier(tmp_path, policy).run()
    assert proc.returncode == 0, proc.output
    assert f"checked {len(expected)} members, all pinned to workflow files" in proc.output, proc.output


# ---------------------------------------------------------------------------
# Read-only, and no restated list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("passes", [False, True], ids=["refusal", "pass"])
def test_only_read_only_verbs_are_called(tmp_path, passes):
    """MUTATION: any set-iam-policy, add-iam-policy-binding, update, apply or plan."""
    members = list(_expected().values()) + ([] if passes else [REPO_REF])
    verifier = Verifier(tmp_path, _policy(*members))
    assert (verifier.run().returncode == 0) is passes

    gcloud = verifier.calls("gcloud")
    assert gcloud, "the verifier never called gcloud: it read no live policy"
    for call in gcloud:
        assert call[:3] == ["iam", "service-accounts", "get-iam-policy"] or call[:4] == [
            "iam", "workload-identity-pools", "providers", "describe",
        ], call
    terraform = verifier.calls("terraform")
    assert any("github_principals" in call for call in terraform), (
        "the verifier never read github_principals: its expected set came from somewhere else"
    )
    for call in terraform:
        sub = [a for a in call if not a.startswith("-")]
        assert sub and sub[0] in ("init", "output"), call


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def test_the_script_is_strict_and_executable():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK), "scripts/verify-deployer-trust.sh is not executable"
    assert re.search(r'^source .*lib/common\.sh"?$', text, re.MULTILINE), "it must source scripts/lib/common.sh"


def test_no_workflow_file_is_named_in_the_script():
    """The expected set is terraform's `github_principals`, never a second list
    that drifts from wif.tf's `deployer_workflows`."""
    names = sorted(p.name for p in WORKFLOWS.glob("*.yml"))
    assert "release.yml" in names, "read no workflow files"
    text = SCRIPT.read_text()
    restated = [name for name in names if name in text]
    assert not restated, f"scripts/verify-deployer-trust.sh names workflow files: {restated}"
    assert "github_principals" in text
