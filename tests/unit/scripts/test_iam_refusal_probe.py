"""The IAM refusal probe (#68): its workflow's shape, and its verdict.

`.github/workflows/iam-refusal-probe.yml` runs `scripts/iam-refusal-probe.sh`
as the CI deployer and asks for a role the deployer's scoped
`projectIamAdmin` condition must refuse. The owner dispatches it once. So this
file is the only place its behaviour is exercised before that run, and it holds
the three things that would make that one run lie:

* a verdict of PASS for anything but a PERMISSION_DENIED on the policy write
  -- an auth failure, a network error, a 403 on the READ, a disabled API;
* a revert that does not run, or removes a binding the probe did not make;
* the project policy of a project shared with another team printed into a
  public Actions log.

Nothing here touches the network. The script runs against a fake `gcloud` on
PATH that serves a fixture policy and records every call; the refusal it
returns is the sentence the Cloud SDK renders for a 403 on
`projects/<p>:setIamPolicy` (googlecloudsdk/api_lib/util/exceptions.py,
HttpErrorPayload._MakeDescription), not a literal "PERMISSION_DENIED", which
gcloud does not print.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "iam-refusal-probe.sh"
WORKFLOWS = REPO / ".github" / "workflows"
PROBE_WORKFLOW = WORKFLOWS / "iam-refusal-probe.yml"
CONDITIONS_TF = REPO / "terraform" / "bootstrap" / "deployer_conditions.tf"

PROJECT = "swarm-probe-test"
DEPLOYER_EMAIL = f"swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"
DEPLOYER = f"serviceAccount:{DEPLOYER_EMAIL}"
# Another team's identity in the shared project. It must never reach a log.
SENTINEL = "serviceAccount:their-sentinel-7f3a@other-team.iam.gserviceaccount.com"
SCOPED_ROLE = "roles/resourcemanager.projectIamAdmin"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq and bash are required",
)


def _probe_role() -> str:
    match = re.search(r'^PROBE_ROLE="([^"]+)"', SCRIPT.read_text(), re.MULTILINE)
    assert match, "scripts/iam-refusal-probe.sh no longer sets PROBE_ROLE on a line of its own"
    return match.group(1)


PROBE_ROLE = _probe_role()


def _workflow(path: Path = PROBE_WORKFLOW) -> dict:
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _only_job(workflow: dict) -> dict:
    jobs = workflow.get("jobs") or {}
    assert len(jobs) == 1, f"the probe is one job, found {sorted(jobs)}"
    return next(iter(jobs.values()))


def _step_index(steps: list[dict], predicate) -> int:
    for index, step in enumerate(steps):
        if predicate(step):
            return index
    return -1


def _runs(step: dict, subcommand: str) -> bool:
    return f"iam-refusal-probe.sh {subcommand}" in (step.get("run") or "")


# ---------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------


def test_the_probe_is_dispatched_by_hand_and_by_nothing_else():
    """MUTATION: add `push:`, `pull_request:` or `schedule:` to its `on:`, or give
    `workflow_dispatch` an input a dispatcher could aim at another role."""
    on = _workflow()["on"]
    triggers = {on} if isinstance(on, str) else set(on)
    assert triggers == {"workflow_dispatch"}, (
        f"iam-refusal-probe.yml triggers on {sorted(triggers)}. It writes to a shared "
        "project's policy if the condition is wrong, so it runs when the owner "
        "dispatches it and on nothing else."
    )
    dispatch = on["workflow_dispatch"] if isinstance(on, dict) else None
    assert not (dispatch or {}).get("inputs"), (
        "the probe takes no inputs: the role it asks for is fixed in the script, "
        "so no dispatcher can point it at roles/owner"
    )


@pytest.mark.parametrize(
    ("ref", "passes"),
    [
        ("refs/heads/main", True),
        ("refs/heads/lane/iam-refusal-probe", False),
        ("refs/pull/1/merge", False),
        ("refs/tags/v1", False),
        ("refs/heads/main-old", False),
    ],
)
def test_the_probe_refuses_every_ref_but_main_before_it_authenticates(ref, passes):
    """Runs the guard step's own shell. MUTATION: widen or drop the comparison,
    or move the guard after the auth step."""
    steps = _only_job(_workflow())["steps"]
    guard = _step_index(steps, lambda s: "refs/heads/main" in (s.get("run") or ""))
    auth = _step_index(steps, lambda s: str(s.get("uses", "")).startswith("google-github-actions/auth@"))
    assert guard >= 0, "no step compares the ref to refs/heads/main"
    assert auth >= 0, "no auth step found"
    assert guard < auth, "the main-only guard must run before the deployer's token is requested"
    assert not steps[guard].get("if"), "the guard is unconditional; an `if:` could skip it"

    env = {"PATH": os.environ["PATH"], "GITHUB_REF": ref}
    proc = subprocess.run(
        ["bash", "-e", "-c", steps[guard]["run"]],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert (proc.returncode == 0) is passes, (
        f"guard on {ref}: exit {proc.returncode}\n{proc.stdout}{proc.stderr}"
    )


def test_the_probe_authenticates_exactly_as_the_release_and_the_plan_do():
    """Same action, same variables. MUTATION: change a `with:` value in any one."""
    def auth_steps(path: Path) -> list[dict]:
        found = []
        for job in (_workflow(path).get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("google-github-actions/auth@"):
                    found.append({"uses": step["uses"], "with": step.get("with")})
        return found

    probe = auth_steps(PROBE_WORKFLOW)
    assert len(probe) == 1, probe
    reference = auth_steps(WORKFLOWS / "release.yml") + auth_steps(WORKFLOWS / "terraform.yml")
    assert reference, "read no auth step from release.yml or terraform.yml"
    for other in reference:
        assert probe[0] == other, f"the probe authenticates as {probe[0]}; the release/plan as {other}"
    assert "token_format" not in (probe[0]["with"] or {}), "no access token output: nothing to print"


def test_the_probe_holds_only_the_token_it_needs_and_names_no_environment():
    """MUTATION: widen a permission, drop `permissions: {}`, or name an environment."""
    workflow = _workflow()
    assert workflow.get("permissions") == {}, "workflow-level permissions must be empty"
    job = _only_job(workflow)
    assert job.get("permissions") == {"contents": "read", "id-token": "write"}, job.get("permissions")
    assert "environment" not in job, (
        "the deployer's token depends on the ref, not an environment, and GitHub "
        "creates any environment a workflow names with no protection"
    )


def test_the_revert_runs_on_every_path_after_a_write_could_have_happened():
    """MUTATION: drop `always()` from the revert, gate it on the grant's own
    output, or move it before the grant."""
    steps = _only_job(_workflow())["steps"]
    preflight = _step_index(steps, lambda s: _runs(s, "preflight"))
    grant = _step_index(steps, lambda s: _runs(s, "grant"))
    revert = _step_index(steps, lambda s: _runs(s, "revert"))
    assert -1 not in (preflight, grant, revert), (preflight, grant, revert)
    assert preflight < grant < revert

    gate_id = steps[preflight].get("id")
    assert gate_id, "preflight needs an id for its output to gate on"
    gate = f"steps.{gate_id}.outputs.ready == 'true'"
    grant_if = str(steps[grant].get("if") or "")
    revert_if = str(steps[revert].get("if") or "")

    assert gate in grant_if, f"the grant must be gated on preflight's output: `{grant_if}`"
    assert "always()" in revert_if, (
        f"the revert must run after a failed, timed-out or cancelled grant: `{revert_if}`"
    )
    # Its gate is an output written BEFORE any write, never one the grant step
    # writes itself: a cancelled step's outputs are not something to stake the
    # one step that must run on.
    assert gate in revert_if, f"the revert must be gated on preflight's output: `{revert_if}`"
    assert f"steps.{steps[grant].get('id')}.outputs" not in revert_if, revert_if


def test_the_workflow_is_linted_on_every_pull_request_that_touches_it():
    """It runs only when dispatched, so a pull request is the one place it is parsed."""
    application = _workflow(WORKFLOWS / "application.yml")
    paths = application["on"]["pull_request"]["paths"]
    assert ".github/workflows/iam-refusal-probe.yml" in paths, paths
    lint = [
        step.get("run") or ""
        for step in application["jobs"]["workflows"]["steps"]
        if "actionlint" in (step.get("run") or "")
    ]
    assert any(".github/workflows/iam-refusal-probe.yml" in run for run in lint), lint


# ---------------------------------------------------------------------------
# The role it asks for
# ---------------------------------------------------------------------------


def test_the_probe_role_is_one_the_condition_must_refuse():
    """Not on the grantable list, and granted by nothing in terraform/. MUTATION:
    set PROBE_ROLE to roles/run.viewer, or add roles/browser to the list."""
    text = CONDITIONS_TF.read_text()
    match = re.search(r"deployer_grantable_project_roles\s*=\s*concat\((.*?)\n  \)", text, re.DOTALL)
    assert match, "could not find deployer_grantable_project_roles in deployer_conditions.tf"
    grantable = match.group(1)
    # Control: the block read is the list, not an empty match.
    assert '"roles/run.viewer"' in grantable and "swarmSecretLister" in grantable, grantable
    assert f'"{PROBE_ROLE}"' not in grantable, f"{PROBE_ROLE} is on the grantable list"
    assert PROBE_ROLE.split("/")[-1] not in grantable

    files = [p for p in (REPO / "terraform").rglob("*") if p.suffix in {".tf", ".tfvars"} and p.is_file()]
    assert len(files) > 20, f"read only {len(files)} terraform files"
    naming = [str(p.relative_to(REPO)) for p in files if f'"{PROBE_ROLE}"' in p.read_text()]
    assert not naming, f"{PROBE_ROLE} is named in {naming}; the probe needs a role nothing grants"
    assert PROBE_ROLE not in {SCOPED_ROLE, "roles/owner", "roles/editor"}


# ---------------------------------------------------------------------------
# The verdict: classify
# ---------------------------------------------------------------------------

_CRED = (
    f" This command is authenticated as {DEPLOYER_EMAIL} using the credentials in"
    " /home/runner/work/SwarmCloud/SwarmCloud/gha-creds-0a1b2c3d.json, specified by"
    " the [auth/credential_file_override] property."
)


def _denied(instance: str, message: str = "Policy update access denied.") -> str:
    return (
        "ERROR: (gcloud.projects.add-iam-policy-binding) "
        f"[{DEPLOYER_EMAIL}] does not have permission to access projects instance "
        f"[{instance}] (or it may not exist): {message}{_CRED}\n"
    )


CLASSIFY_CASES = [
    # The refusal, as the Cloud SDK renders a 403 on the policy write.
    ("refused", "1", _denied(f"{PROJECT}:setIamPolicy")),
    ("refused", "1", _denied(f"{PROJECT}:setIamPolicy", "The caller does not have permission.")),
    # A gcloud that prints the canonical status and the method.
    ("refused", "1", f"ERROR: PERMISSION_DENIED: Policy update access denied. [{PROJECT}:setIamPolicy]\n"),
    # A grant.
    ("granted", "0", f"Updated IAM policy for project [{PROJECT}].\n"),
    ("granted", "0", ""),
    # Everything else is an error, never a pass.
    ("error", "1", ""),
    ("error", "124", ""),
    ("error", "x", _denied(f"{PROJECT}:setIamPolicy")),
    ("error", "", _denied(f"{PROJECT}:setIamPolicy")),
    ("error", "1", _denied(f"{PROJECT}:getIamPolicy", "The caller does not have permission.")),
    ("error", "1", _denied("some-other-project:setIamPolicy")),
    ("error", "1", "ERROR: PERMISSION_DENIED: The caller does not have permission [getIamPolicy]\n"),
    (
        "error", "1",
        _denied(
            f"{PROJECT}:setIamPolicy",
            "Cloud Resource Manager API has not been used in project 1234 before or it is disabled.",
        ),
    ),
    ("error", "1", _denied(f"{PROJECT}:setIamPolicy", "SERVICE_DISABLED")),
    ("error", "1", _denied(f"{PROJECT}:setIamPolicy", "BILLING_DISABLED: billing account closed")),
    (
        "error", "1",
        _denied(
            f"{PROJECT}:setIamPolicy",
            "Request is prohibited by organization's policy. vpcServiceControlsUniqueIdentifier: abc",
        ),
    ),
    ("error", "1", "ERROR: (gcloud.projects.add-iam-policy-binding) UNAUTHENTICATED: Request had invalid authentication credentials.\n"),
    (
        "error", "1",
        "ERROR: (gcloud.projects.add-iam-policy-binding) There was a problem refreshing your current auth tokens: "
        "('Unable to acquire impersonated credentials', 'unauthorized_client: The given credential is rejected "
        "by the attribute condition.')\n",
    ),
    (
        "error", "1",
        "ERROR: gcloud crashed (ConnectionError): HTTPSConnectionPool(host='cloudresourcemanager.googleapis.com', "
        "port=443): Max retries exceeded with url: /v1/projects/x:setIamPolicy\n",
    ),
    (
        "error", "1",
        "ERROR: (gcloud.projects.add-iam-policy-binding) Adding a binding without specifying a condition to a "
        "policy containing conditions is prohibited in non-interactive mode. Run the command again with "
        "`--condition=None`\n",
    ),
    ("error", "1", f"ERROR: Resource in projects [{PROJECT}] is the subject of a conflict: There were concurrent policy changes.\n"),
    ("error", "1", "ERROR: INVALID_ARGUMENT: One or more users named in the policy do not belong to a permitted customer.\n"),
]


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("GITHUB_"):
            del env[key]
    env.update(
        {
            "PROJECT_ID": PROJECT,
            "DEPLOYER_SA": DEPLOYER_EMAIL,
            # A path with nothing at it: no developer's .env is sourced.
            "SWARM_ENV_FILE": str(tmp_path / "no-env-file"),
            "NO_COLOR": "1",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_ACTIONS": "false",
        }
    )
    env.update(extra)
    return env


@pytest.mark.parametrize(("expected", "rc", "stderr"), CLASSIFY_CASES)
def test_only_a_permission_denial_on_the_policy_write_is_a_refusal(tmp_path, expected, rc, stderr):
    """MUTATION: accept any "does not have permission", or any "PERMISSION_DENIED",
    or stop excluding a disabled API."""
    errfile = tmp_path / "stderr.txt"
    errfile.write_text(stderr)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "classify", rc, str(errfile)],
        cwd=REPO, env=_env(tmp_path), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected, f"exit {rc!r}, stderr {stderr!r}: got {proc.stdout.strip()!r}"


def test_the_classifier_cases_cover_every_verdict():
    verdicts = {case[0] for case in CLASSIFY_CASES}
    assert verdicts == {"refused", "granted", "error"}, verdicts


# ---------------------------------------------------------------------------
# The verdict end to end, against a fake gcloud
# ---------------------------------------------------------------------------

FAKE_GCLOUD = r'''#!__PYTHON__
import json, os, sys

state_path = os.environ["FAKE_GCLOUD_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
with open(state_path + ".calls", "a") as handle:
    handle.write(json.dumps(args) + "\n")


def opt(name):
    for arg in args:
        if arg.startswith("--" + name + "="):
            return arg.split("=", 1)[1]
    return None


def save():
    with open(state_path, "w") as handle:
        json.dump(state, handle)


policy = state["policy"]
project = state["project"]
email = state["deployer"].split(":", 1)[1]

if args[:2] == ["projects", "get-iam-policy"]:
    if state.get("get_fails"):
        sys.stderr.write("ERROR: (gcloud.projects.get-iam-policy) HTTPSConnectionPool: Max retries exceeded\n")
        sys.exit(1)
    print(json.dumps(policy))
    sys.exit(0)

if args[:3] == ["iam", "roles", "describe"]:
    role = args[3]
    if opt("project"):
        role = "projects/" + opt("project") + "/roles/" + role
        if state.get("custom_unreadable"):
            sys.stderr.write("ERROR: (gcloud.iam.roles.describe) [" + email + "] does not have permission to "
                             "access roles instance [" + role + "] (or it may not exist)\n")
            sys.exit(1)
    elif state.get("predefined_unreadable"):
        sys.stderr.write("ERROR: gcloud crashed (ConnectionError): Max retries exceeded\n")
        sys.exit(1)
    perms = state.get("roles", {}).get(role, ["resourcemanager.projects.get"])
    print(json.dumps({"name": role, "includedPermissions": perms}))
    sys.exit(0)

if args[:2] == ["projects", "add-iam-policy-binding"]:
    member, role = opt("member"), opt("role")
    mode = state["add"]
    if mode == "refuse":
        sys.stderr.write(
            "ERROR: (gcloud.projects.add-iam-policy-binding) [" + email + "] does not have permission to "
            "access projects instance [" + project + ":setIamPolicy] (or it may not exist): Policy update "
            "access denied.\n"
        )
        sys.exit(1)
    if mode == "network":
        sys.stderr.write("ERROR: gcloud crashed (ConnectionError): Max retries exceeded\n")
        sys.exit(1)
    if mode in ("grant", "grant_then_timeout"):
        for binding in policy["bindings"]:
            if binding["role"] == role and "condition" not in binding:
                binding["members"].append(member)
                break
        else:
            policy["bindings"].append({"role": role, "members": [member]})
        save()
        # Real gcloud prints the whole new policy on success.
        print(json.dumps(policy))
        if mode == "grant_then_timeout":
            sys.stderr.write("ERROR: gcloud crashed (ReadTimeout): read timed out\n")
            sys.exit(1)
        sys.stderr.write("Updated IAM policy for project [" + project + "].\n")
        sys.exit(0)

if args[:2] == ["projects", "remove-iam-policy-binding"]:
    if state.get("remove_fails"):
        sys.stderr.write("ERROR: [" + email + "] does not have permission to access projects instance ["
                         + project + ":setIamPolicy] (or it may not exist): Policy update access denied.\n")
        sys.exit(1)
    member, role = opt("member"), opt("role")
    kept = []
    for binding in policy["bindings"]:
        if binding["role"] == role and "condition" not in binding:
            binding["members"] = [m for m in binding["members"] if m != member]
            if not binding["members"]:
                continue
        kept.append(binding)
    policy["bindings"] = kept
    save()
    print(json.dumps(policy))
    sys.exit(0)

sys.stderr.write("fake gcloud: unexpected call " + " ".join(args) + "\n")
sys.exit(97)
'''


def _live_policy() -> dict:
    """The deployer after #73's apply, in a project another team shares."""
    return {
        "version": 3,
        "etag": "BwYfake=",
        "bindings": [
            {
                "role": SCOPED_ROLE,
                "members": [DEPLOYER],
                "condition": {
                    "title": "only the roles terraform infra grants",
                    "expression": 'api.getAttribute("iam.googleapis.com/modifiedGrantsByRole", [])'
                    '.hasOnly(["roles/cloudtrace.agent", "roles/run.viewer"])',
                },
            },
            {"role": "roles/run.admin", "members": [DEPLOYER]},
            {"role": "roles/iam.roleAdmin", "members": [DEPLOYER]},
            {"role": f"projects/{PROJECT}/roles/swarmSecretProvisioner", "members": [DEPLOYER]},
            {
                "role": "roles/storage.admin",
                "members": [DEPLOYER],
                "condition": {"title": "swarm buckets", "expression": 'resource.name.startsWith("projects/_/buckets/swarm-")'},
            },
            # The other team, holding the probe role itself. Not the deployer's
            # binding: preflight must not refuse over it, the revert must not
            # touch it, and neither may print it.
            {"role": PROBE_ROLE, "members": [SENTINEL]},
            {"role": "roles/container.admin", "members": [SENTINEL]},
        ],
    }


class Probe:
    def __init__(self, tmp_path: Path, policy: dict, add: str = "refuse", **state) -> None:
        self.tmp = tmp_path
        self.state_file = tmp_path / "gcloud-state.json"
        self.state_file.write_text(
            json.dumps({"policy": policy, "project": PROJECT, "deployer": DEPLOYER, "add": add, **state})
        )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(FAKE_GCLOUD.replace("__PYTHON__", sys.executable))
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IXUSR)
        # The revert's retry waits; not here.
        fake_sleep = bin_dir / "sleep"
        fake_sleep.write_text("#!/bin/sh\nexit 0\n")
        fake_sleep.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.outputs_file = tmp_path / "github-output"
        self.summary_file = tmp_path / "step-summary"
        self.transcript = ""

    def run(self, subcommand: str, **extra: str) -> subprocess.CompletedProcess:
        env = _env(
            self.tmp,
            PATH=self.path,
            FAKE_GCLOUD_STATE=str(self.state_file),
            GITHUB_OUTPUT=str(self.outputs_file),
            GITHUB_STEP_SUMMARY=str(self.summary_file),
            **extra,
        )
        proc = subprocess.run(
            ["bash", str(SCRIPT), subcommand],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
        )
        self.transcript += proc.stdout + proc.stderr
        return proc

    def outputs(self) -> dict[str, str]:
        if not self.outputs_file.exists():
            return {}
        pairs = [line.split("=", 1) for line in self.outputs_file.read_text().splitlines() if "=" in line]
        return {key: value for key, value in pairs}

    def calls(self, verb: str) -> list[list[str]]:
        path = Path(str(self.state_file) + ".calls")
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text().splitlines()]
        return [call for call in calls if call[:2] == ["projects", verb]]

    def policy(self) -> dict:
        return json.loads(self.state_file.read_text())["policy"]


def _deployer_holds(policy: dict, role: str) -> bool:
    return any(b["role"] == role and DEPLOYER in b["members"] for b in policy["bindings"])


def test_a_refusal_passes_and_the_revert_only_reads(tmp_path):
    probe = Probe(tmp_path, _live_policy(), add="refuse")
    preflight = probe.run("preflight")
    assert preflight.returncode == 0, preflight.stderr
    assert probe.outputs().get("ready") == "true"

    grant = probe.run("grant")
    assert grant.returncode == 0, grant.stderr
    assert probe.outputs().get("verdict") == "refused"
    assert probe.outputs().get("attempted") == "true"
    (call,) = probe.calls("add-iam-policy-binding")
    assert f"--member={DEPLOYER}" in call and f"--role={PROBE_ROLE}" in call and "--condition=None" in call

    revert = probe.run("revert", GRANT_VERDICT="refused")
    assert revert.returncode == 0, revert.stderr
    assert probe.calls("remove-iam-policy-binding") == [], "nothing was granted, so nothing is removed"

    summary = probe.run(
        "summary", PREFLIGHT_OUTCOME="success", GRANT_VERDICT="refused", REVERT_OUTCOME="success"
    )
    assert summary.returncode == 0
    assert "PASS" in probe.summary_file.read_text()


def test_a_grant_fails_the_run_and_is_removed_and_read_back(tmp_path):
    """MUTATION: drop the remove, drop --condition=None from it, or let a grant exit 0."""
    probe = Probe(tmp_path, _live_policy(), add="grant")
    assert probe.run("preflight").returncode == 0

    grant = probe.run("grant")
    assert grant.returncode != 0, "a grant of an unlisted role must fail the job"
    assert probe.outputs().get("verdict") == "granted"
    assert _deployer_holds(probe.policy(), PROBE_ROLE), "control: the fake did grant it"

    revert = probe.run("revert", GRANT_VERDICT="granted")
    assert revert.returncode != 0, "the revert fails the job even after a clean removal"
    assert "#68 must be reopened" in revert.stderr
    (call,) = probe.calls("remove-iam-policy-binding")
    assert f"--member={DEPLOYER}" in call and f"--role={PROBE_ROLE}" in call and "--condition=None" in call
    assert not _deployer_holds(probe.policy(), PROBE_ROLE), "the deployer still holds the probe role"
    assert probe.outputs().get("removed") == "true"
    # The other team's binding of the same role is untouched.
    assert any(b["role"] == PROBE_ROLE and SENTINEL in b["members"] for b in probe.policy()["bindings"])


def test_an_error_after_the_write_landed_is_still_reverted(tmp_path):
    """gcloud timed out after the server applied the grant: not a pass, and removed."""
    probe = Probe(tmp_path, _live_policy(), add="grant_then_timeout")
    assert probe.run("preflight").returncode == 0
    grant = probe.run("grant")
    assert grant.returncode != 0
    assert probe.outputs().get("verdict") == "error"

    revert = probe.run("revert", GRANT_VERDICT="error")
    assert revert.returncode != 0
    assert probe.calls("remove-iam-policy-binding"), "the binding that landed was not removed"
    assert not _deployer_holds(probe.policy(), PROBE_ROLE)


def test_a_network_error_is_not_a_pass(tmp_path):
    probe = Probe(tmp_path, _live_policy(), add="network")
    assert probe.run("preflight").returncode == 0
    grant = probe.run("grant")
    assert grant.returncode != 0
    assert probe.outputs().get("verdict") == "error"
    revert = probe.run("revert", GRANT_VERDICT="error")
    assert revert.returncode == 0, "nothing was granted; the read-back is clean"
    assert probe.calls("remove-iam-policy-binding") == []


def test_a_revert_that_cannot_remove_says_so_and_how(tmp_path):
    probe = Probe(tmp_path, _live_policy(), add="grant", remove_fails=True)
    assert probe.run("preflight").returncode == 0
    assert probe.run("grant").returncode != 0
    revert = probe.run("revert", GRANT_VERDICT="granted")
    assert revert.returncode != 0
    assert "REVERT FAILED" in revert.stderr
    assert "remove-iam-policy-binding" in revert.stderr and "--condition=None" in revert.stderr
    assert len(probe.calls("remove-iam-policy-binding")) == 3
    assert probe.outputs().get("removed") == "false"


def test_the_revert_never_removes_a_binding_the_probe_did_not_make(tmp_path):
    """The grant step found the role already on the deployer and wrote nothing."""
    policy = _live_policy()
    policy["bindings"].append({"role": PROBE_ROLE, "members": [DEPLOYER]})
    probe = Probe(tmp_path, policy, add="grant")
    revert = probe.run("revert", GRANT_CONFLICT="true")
    assert revert.returncode != 0
    assert probe.calls("remove-iam-policy-binding") == []
    assert _deployer_holds(probe.policy(), PROBE_ROLE)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("wide", "the unconditioned projectIamAdmin is still live"),
        ("no_scoped", "there is no conditioned projectIamAdmin"),
        ("lists_probe", "the condition lists the probe role"),
        ("other_condition", "the condition is not the modifiedGrantsByRole one"),
        ("pre_existing", "the deployer already holds the probe role"),
        ("pre_existing_conditioned", "the deployer holds the probe role under a condition"),
        ("second_setter", "another role the deployer holds carries setIamPolicy"),
        ("unreadable", "the policy cannot be read"),
        ("predefined_unreadable", "a predefined role the deployer holds cannot be read"),
    ],
)
def test_preflight_refuses_whenever_the_verdict_would_not_be_the_conditions(tmp_path, change, reason):
    """MUTATION: drop any one preflight check."""
    policy = _live_policy()
    scoped = policy["bindings"][0]
    state: dict = {}
    if change == "wide":
        policy["bindings"].append({"role": SCOPED_ROLE, "members": [DEPLOYER]})
    elif change == "no_scoped":
        policy["bindings"].pop(0)
    elif change == "lists_probe":
        scoped["condition"]["expression"] = scoped["condition"]["expression"].replace(
            '"roles/run.viewer"', f'"roles/run.viewer", "{PROBE_ROLE}"'
        )
    elif change == "other_condition":
        scoped["condition"]["expression"] = 'resource.name.startsWith("projects/x")'
    elif change == "pre_existing":
        policy["bindings"].append({"role": PROBE_ROLE, "members": [DEPLOYER]})
    elif change == "pre_existing_conditioned":
        policy["bindings"].append(
            {"role": PROBE_ROLE, "members": [DEPLOYER], "condition": {"title": "t", "expression": "true"}}
        )
    elif change == "second_setter":
        state["roles"] = {
            f"projects/{PROJECT}/roles/swarmSecretProvisioner": [
                "secretmanager.secrets.create",
                "resourcemanager.projects.setIamPolicy",
            ]
        }
    elif change == "unreadable":
        state["get_fails"] = True
    elif change == "predefined_unreadable":
        state["predefined_unreadable"] = True

    probe = Probe(tmp_path, policy, add="grant", **state)
    preflight = probe.run("preflight")
    assert preflight.returncode != 0, f"preflight passed although {reason}"
    assert probe.outputs().get("ready") != "true"
    assert "Nothing was attempted" in preflight.stderr or "nothing was attempted" in preflight.stderr
    assert probe.calls("add-iam-policy-binding") == []


def test_an_unreadable_custom_role_is_a_caveat_on_a_grant_and_not_on_a_refusal(tmp_path):
    """After #150 the deployer cannot read its custom roles. A refusal is still
    proof (nothing admitted the write); a grant says it may not be the condition's."""
    (tmp_path / "refused").mkdir()
    (tmp_path / "granted").mkdir()

    refused = Probe(tmp_path / "refused", _live_policy(), add="refuse", custom_unreadable=True)
    assert refused.run("preflight").returncode == 0
    assert refused.outputs().get("unread") == "1"
    grant = refused.run("grant", PREFLIGHT_UNREAD="1")
    assert grant.returncode == 0 and refused.outputs().get("verdict") == "refused"

    granted = Probe(tmp_path / "granted", _live_policy(), add="grant", custom_unreadable=True)
    assert granted.run("preflight").returncode == 0
    grant = granted.run("grant", PREFLIGHT_UNREAD="1")
    assert grant.returncode != 0
    assert "custom role(s) preflight could not read" in grant.stderr
    assert "#68 must be reopened" in grant.stderr
    revert = granted.run("revert", GRANT_VERDICT="granted", PREFLIGHT_UNREAD="1")
    assert revert.returncode != 0 and not _deployer_holds(granted.policy(), PROBE_ROLE)


@pytest.mark.parametrize(
    ("preflight", "verdict", "revert", "found", "says"),
    [
        ("success", "refused", "success", "0", "PASS"),
        ("success", "refused", "failure", "0", "could not confirm"),
        ("success", "granted", "failure", "1", "Reopen #68"),
        # gcloud failed, but the revert found the grant had landed.
        ("success", "error", "failure", "1", "Reopen #68"),
        ("success", "error", "success", "0", "inconclusive"),
        ("success", "", "failure", "", "inconclusive"),
        ("failure", "", "skipped", "", "NOT RUN"),
        ("skipped", "", "skipped", "", "NOT RUN"),
    ],
)
def test_the_run_summary_never_calls_anything_but_a_clean_refusal_a_pass(
    tmp_path, preflight, verdict, revert, found, says
):
    probe = Probe(tmp_path, _live_policy())
    proc = probe.run(
        "summary",
        PREFLIGHT_OUTCOME=preflight, GRANT_VERDICT=verdict, REVERT_OUTCOME=revert, REVERT_FOUND=found,
    )
    assert proc.returncode == 0, proc.stderr
    text = probe.summary_file.read_text()
    assert says in text, text
    assert ("PASS" in text) is (says == "PASS"), text


def test_preflight_reads_every_other_role_the_deployer_holds(tmp_path):
    """The loop over roles visited them all (a command reading stdin inside a
    `while read` loop would end it after the first)."""
    probe = Probe(tmp_path, _live_policy())
    assert probe.run("preflight").returncode == 0
    calls = [json.loads(line) for line in Path(str(probe.state_file) + ".calls").read_text().splitlines()]
    described = [call[3] for call in calls if call[:3] == ["iam", "roles", "describe"]]
    # run.admin, iam.roleAdmin, swarmSecretProvisioner, storage.admin; never the
    # scoped role, never the other team's roles.
    assert sorted(described) == sorted(
        ["roles/run.admin", "roles/iam.roleAdmin", "swarmSecretProvisioner", "roles/storage.admin"]
    ), described


def test_no_step_prints_the_project_policy_or_asks_for_a_token(tmp_path):
    """The fake prints the whole policy on every call, as gcloud does on a grant.
    MUTATION: drop `>/dev/null` / `--format=none` from the grant or the remove,
    or cat the policy file."""
    probe = Probe(tmp_path, _live_policy(), add="grant")
    probe.run("preflight")
    probe.run("grant")
    probe.run("revert", GRANT_VERDICT="granted")
    probe.run("summary", PREFLIGHT_OUTCOME="success", GRANT_VERDICT="granted", REVERT_OUTCOME="failure")
    assert probe.calls("add-iam-policy-binding"), "control: the grant ran"
    assert probe.calls("remove-iam-policy-binding"), "control: the remove ran"
    assert SENTINEL.split(":", 1)[1] not in probe.transcript, "another team's member reached the log"
    assert SENTINEL.split(":", 1)[1] not in probe.summary_file.read_text()
    all_calls = Path(str(probe.state_file) + ".calls").read_text()
    assert "print-access-token" not in all_calls and "print-identity-token" not in all_calls
    # The code, not the header that says it never does this.
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "print-access-token" not in code and "print-identity-token" not in code
