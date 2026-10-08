"""The workspace job's call guard refuses everything not named for one workspace.

docs/workspaces.md §2.5 (lane W3 of #847). `swarm-workspace-deployer` holds
project-wide account-IAM power in a project it shares with another team, and
Google cannot narrow it (§2.4). What bounds it is scripts/lib/workspace-guard.sh:
every gcloud, kubectl and curl the job makes passes through the shims in
scripts/lib/guard-bin/ and runs only if one shape in
scripts/lib/workspace-calls.json matches it whole, with the names of the one
workspace the job is for.

So these tests hold four things:

* every case in scripts/lib/workspace-guard-cases.json comes out as it says --
  each rule refusing a call built to trip it and allowing the call it exists for;
* the guard and the script cannot drift: every gcloud and kubectl command the
  workspace path names has a rule (allow, refuse-always, or known-unused), and
  nothing on that path calls the three tools by an absolute path that would
  step around the shims;
* the wiring works as a process: a refusal exits 86 before the real binary
  runs, writes the needs_owner stop file, and latches; report-only runs and
  records nothing; the owner's report-only cannot be switched on under
  SWARM_CALL_GUARD_ENFORCE;
* the names the guard derives agree with the code that makes them: the real
  kubernetes/render.py manifest passes C7 for its own tenant and fails it for
  another, the bucket condition lines are register-tenant.sh's, and the forge
  slot is swarm_api.gittokens's.

Nothing here needs credentials, a network or a cluster; the "real" binaries are
stand-ins written to a temporary directory.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib"
GUARD = LIB / "workspace-guard.sh"
RULES = LIB / "workspace-calls.json"
CASES = LIB / "workspace-guard-cases.json"
SHIMS = LIB / "guard-bin"
REGISTER = REPO / "scripts" / "register-tenant.sh"
COMMON = LIB / "common.sh"
APPLY = REPO / "kubernetes" / "apply.sh"
CLUSTER_NETWORK = REPO / "kubernetes" / "cluster-network.sh"

REFUSED = 86
WORKSPACE = "w-3f9a2c"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

RULE_DOC = json.loads(RULES.read_text())
CASE_DOC = json.loads(CASES.read_text())
CALL_CASES = [c["name"] for c in CASE_DOC["cases"]]
RECORD_CASES = [c["name"] for c in CASE_DOC["expect_refusals"]]


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    """A fixed, credential-free environment, as the self-test uses."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SWARM_CALL_GUARD", "CLOUDSDK_", "GOOGLE_")) and k != "KUBECONFIG"
    }
    env.update(
        SWARM_ENV_FILE=str(tmp / "no.env"),
        PROJECT_ID="saga-agents-staging",
        REGION="us-central1",
        ENVIRONMENT="dev",
        FIRESTORE_DATABASE="swarm",
        GKE_CLUSTER="swarm-autopilot",
        GKE_LOCATION="us-central1",
        SWARM_CALL_GUARD=str(tmp / "expect.json"),
        NO_COLOR="1",
    )
    env.pop("ARTIFACT_BUCKET", None)
    env.update(extra)
    return env


def _guard(*args: str, env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GUARD), *args], input=stdin, capture_output=True, text=True, env=env, timeout=120, check=False
    )


@pytest.fixture(scope="module")
def self_test() -> dict[str, dict]:
    """One run of the guard's own self-test, every case, as JSON lines."""
    out = subprocess.run(
        [str(GUARD), "self-test", "--json"], capture_output=True, text=True, timeout=600, check=False
    )
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
    assert rows, f"the self-test printed no case at all (exit {out.returncode}):\n{out.stderr[-2000:]}"
    by_name = {row["name"]: row for row in rows}
    by_name["__exit__"] = {"code": out.returncode, "stderr": out.stderr}
    return by_name


@pytest.fixture()
def ready(tmp_path: Path) -> dict[str, str]:
    """An expectation built the way the job builds it: init, then expect --record."""
    env = _env(tmp_path)
    assert _guard("init", "--workspace-id", WORKSPACE, env=env).returncode == 0
    record = tmp_path / "record.json"
    record.write_text(json.dumps(CASE_DOC["record"]))
    done = _guard("expect", "--record", str(record), env=env)
    assert done.returncode == 0, done.stderr
    return env


# --------------------------------------------------------------------------
# every case
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", CALL_CASES)
def test_call_case(self_test: dict[str, dict], name: str) -> None:
    row = self_test.get(name)
    assert row is not None, f"the self-test never visited {name!r}"
    assert row["pass"], (
        f"{row['rule']} must {row['expect']} {name!r} (by {row['by']}); "
        f"the guard said {row['verdict']} by {row['got_rule']}: {row['reason']}"
    )


@pytest.mark.parametrize("name", RECORD_CASES)
def test_record_case(self_test: dict[str, dict], name: str) -> None:
    row = self_test.get(name)
    assert row is not None, f"the self-test never visited {name!r}"
    assert row["pass"], f"expect turned {name!r} into an expectation"


def test_self_test_exits_zero_and_visits_every_case(self_test: dict[str, dict]) -> None:
    assert self_test["__exit__"]["code"] == 0, self_test["__exit__"]["stderr"][-3000:]
    visited = set(self_test) - {"__exit__"}
    assert visited == set(CALL_CASES) | set(RECORD_CASES)


def test_the_rules_are_c0_to_c9() -> None:
    assert [r["id"] for r in RULE_DOC["rules"]] == [f"C{i}" for i in range(10)]


def test_every_rule_has_a_case_to_refuse_and_a_case_to_allow() -> None:
    seen = {(c["rule"], c["expect"]) for c in CASE_DOC["cases"]}
    missing = [
        f"{r['id']} {want}" for r in RULE_DOC["rules"] for want in ("allow", "refuse") if (r["id"], want) not in seen
    ]
    assert not missing, f"rules without a case: {missing}"


def test_the_wd9_fallback_ships_off() -> None:
    # On, C5 would let the job add project-level roles per person; the
    # principal-set grant makes that unnecessary (docs/workspaces.md §2.3, WD9).
    assert RULE_DOC["wd9_fallback"] is False


def test_the_self_test_fails_when_a_rule_is_loosened(tmp_path: Path) -> None:
    """The cases can go red: widen C3's member to anything and a refusal case fails.

    Run against a copy of lib/ so the real rules are never touched.
    """
    lib = tmp_path / "scripts" / "lib"
    shutil.copytree(LIB, lib)
    shutil.copytree(REPO / "apps" / "common", tmp_path / "apps" / "common")
    shutil.copytree(REPO / "terraform" / "modules" / "service_account_ids", tmp_path / "terraform" / "modules" / "service_account_ids")
    rules = json.loads((lib / "workspace-calls.json").read_text())
    c3 = next(r for r in rules["rules"] if r["id"] == "C3")
    for shape in c3["shapes"]:
        shape["flags"]["--member"] = "*"
    (lib / "workspace-calls.json").write_text(json.dumps(rules))
    out = subprocess.run(
        [str(lib / "workspace-guard.sh"), "self-test", "--json", "--case", "act-as for a person"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("{")]
    assert rows and rows[0]["pass"] is False and out.returncode != 0, out.stdout + out.stderr


# --------------------------------------------------------------------------
# the script and the guard cannot drift apart
# --------------------------------------------------------------------------


def _code(path: Path) -> str:
    """The file with comment lines dropped and continuations joined."""
    lines = [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]
    return re.sub(r"\\\n\s*", " ", "\n".join(lines))


GCLOUD_GROUPS = ("auth", "config", "container", "iam", "identity", "projects", "secrets", "storage", "compute", "run")


def _gcloud_commands(path: Path) -> set[tuple[str, ...]]:
    found = set()
    for match in re.finditer(r"(?<![\w/.$-])gcloud((?:[ \t]+[a-z][a-z0-9-]*)+)", _code(path)):
        words = tuple(match.group(1).split())
        if words and words[0] in GCLOUD_GROUPS:
            found.add(words)
    return found


def _kubectl_commands(path: Path) -> set[tuple[str, ...]]:
    found = set()
    pattern = (
        r'"\$\{(?:KUBECTL|KUBECTL_BIN|kubectl)\}"'
        r"((?:[ \t]+(?:\"?\$\S+|-n[ \t]+\S+|--context[ \t]+\S+))*)[ \t]+([a-z][a-z-]*)(?:[ \t]+([a-z][a-z-]*))?"
    )
    for match in re.finditer(pattern, _code(path)):
        verb, sub = match.group(2), match.group(3)
        found.add((verb, sub) if verb == "config" else (verb,))
    return found


def _covered(tool: str, words: tuple[str, ...]) -> bool:
    for rule in RULE_DOC["rules"]:
        for shape in rule.get("shapes", []):
            if shape["tool"] == tool and tuple(words[: len(shape["command"])]) == tuple(shape["command"]):
                if tool == "kubectl" or len(words) == len(shape["command"]):
                    return True
        for unused in rule.get("known_unused", []):
            if unused["tool"] == tool and tuple(unused["command"]) == words:
                return True
        never = rule.get("never")
        if never:
            if tool == "gcloud":
                if any(w in never["gcloud_words"] for w in words):
                    return True
                for seq in never["gcloud_sequences"]:
                    if any(list(words[i : i + len(seq)]) == seq for i in range(len(words))):
                        return True
            if tool == "kubectl" and words[0] in never["kubectl_verbs"]:
                return True
    return False


def test_every_gcloud_command_on_the_workspace_path_has_a_rule() -> None:
    commands = set()
    for path in (REGISTER, APPLY, CLUSTER_NETWORK):
        commands |= _gcloud_commands(path)
    # Empty is not success: the scan must find what it scans for.
    assert len(commands) >= 15, f"the gcloud scan found only {sorted(commands)}"
    assert ("iam", "service-accounts", "create") in commands
    missing = sorted(" ".join(c) for c in commands if not _covered("gcloud", c))
    assert not missing, (
        "register-tenant.sh (or the kubernetes/ scripts it runs) names a gcloud command no rule in "
        f"scripts/lib/workspace-calls.json decides: {missing}. Add a shape, a C9 word, or a C0 "
        "known_unused entry saying why --workspace never runs it."
    )


def test_every_kubectl_command_on_the_workspace_path_has_a_rule() -> None:
    commands = set()
    for path in (REGISTER, APPLY, CLUSTER_NETWORK):
        commands |= _kubectl_commands(path)
    assert {("apply",), ("get",), ("config", "current-context")} <= commands, sorted(commands)
    missing = sorted(" ".join(c) for c in commands if not _covered("kubectl", c))
    assert not missing, f"kubectl commands no rule decides: {missing}"


def test_nothing_on_the_workspace_path_calls_the_tools_by_absolute_path() -> None:
    """An absolute path steps around a guard installed on PATH (§2.5).

    kubectl_bin's candidate list names two absolute kubectl paths; it is exempt
    only because its first statement returns the shim under SWARM_CALL_GUARD.
    """
    offenders = []
    for path in (REGISTER, COMMON, APPLY, CLUSTER_NETWORK):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for match in re.finditer(r"/[\w./${}-]*/(gcloud|kubectl|curl)\b", line):
                if "guard-bin/" in match.group(0):
                    continue
                offenders.append((path.name, number, line.strip()))
    allowed = [o for o in offenders if o[0] == "common.sh" and ("candidates+=" in o[2] or "SWARM_KUBECTL=" in o[2])]
    assert [o for o in offenders if o not in allowed] == []
    body = COMMON.read_text().split("kubectl_bin() {", 1)[1]
    first = next(line.strip() for line in body.splitlines() if line.strip())
    assert first == 'if [[ -n "${SWARM_CALL_GUARD:-}" ]]; then', first


@pytest.mark.parametrize("func,name", [("kubectl_bin", ""), ("prefer_local_bin", "gcloud"), ("prefer_local_bin", "curl")])
def test_common_sh_resolves_to_the_shim_under_the_guard(tmp_path: Path, func: str, name: str) -> None:
    script = f'source "{COMMON}"; KUBECTL=/usr/bin/false; {func} {name}'
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False,
        env={**os.environ, "SWARM_CALL_GUARD": str(tmp_path / "e.json")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == str(SHIMS / (name or "kubectl"))


def test_common_sh_runs_no_unguarded_tool_under_the_guard(tmp_path: Path) -> None:
    out = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"; prefer_local_bin terraform'], capture_output=True, text=True,
        check=False, env={**os.environ, "SWARM_CALL_GUARD": str(tmp_path / "e.json")},
    )
    assert out.returncode != 0 and out.stdout == ""


@pytest.mark.parametrize("tool", ["gcloud", "kubectl", "curl"])
def test_each_shim_hands_its_call_to_the_guard(tool: str) -> None:
    shim = SHIMS / tool
    assert shim.stat().st_mode & stat.S_IXUSR
    code = [line for line in shim.read_text().splitlines() if line and not line.startswith("#")]
    assert code == [
        "set -euo pipefail",
        f'exec "$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/.." && pwd)/workspace-guard.sh" run {tool} "$@"',
    ]
    assert sorted(p.name for p in SHIMS.iterdir()) == ["curl", "gcloud", "kubectl"]


# --------------------------------------------------------------------------
# the wiring, as processes
# --------------------------------------------------------------------------


def _fake_bins(tmp: Path) -> Path:
    """Stand-in gcloud and kubectl that record that they ran, and their stdin."""
    bin_dir = tmp / "realbin"
    bin_dir.mkdir()
    for tool in ("gcloud", "kubectl", "curl"):
        fake = bin_dir / tool
        fake.write_text(f'#!/usr/bin/env bash\ncat > "{tmp}/{tool}.stdin"\necho "{tool} $*" >> "{tmp}/ran"\n')
        fake.chmod(0o755)
    return bin_dir


def _shim_env(env: dict[str, str], tmp: Path) -> dict[str, str]:
    return {**env, "PATH": f"{SHIMS}:{_fake_bins(tmp)}:{os.environ['PATH']}"}


def _ran(tmp: Path) -> list[str]:
    ran = tmp / "ran"
    return ran.read_text().splitlines() if ran.exists() else []


def test_an_allowed_call_runs_the_real_binary(tmp_path: Path, ready: dict[str, str]) -> None:
    env = _shim_env(ready, tmp_path)
    out = subprocess.run(
        ["gcloud", "iam", "service-accounts", "create", "swarm-agent-worker-u-alice", "--project", "saga-agents-staging"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert _ran(tmp_path) == ["gcloud iam service-accounts create swarm-agent-worker-u-alice --project saga-agents-staging"]
    assert not Path(ready["SWARM_CALL_GUARD"] + ".stop").exists()


def test_a_refused_call_never_runs_and_records_needs_owner(tmp_path: Path, ready: dict[str, str]) -> None:
    env = _shim_env(ready, tmp_path)
    out = subprocess.run(
        ["gcloud", "iam", "service-accounts", "create", "swarm-agent-worker-u-bob", "--project", "saga-agents-staging"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert out.returncode == REFUSED
    assert "REFUSED (rule C2)" in out.stderr
    assert _ran(tmp_path) == []
    stop = json.loads(Path(ready["SWARM_CALL_GUARD"] + ".stop").read_text())
    assert stop["state"] == "needs_owner" and stop["rule"] == "C2" and stop["workspace_id"] == WORKSPACE
    assert stat.S_IMODE(Path(ready["SWARM_CALL_GUARD"] + ".stop").stat().st_mode) == 0o600

    # Latched: the next call, allowed a moment ago, is refused too.
    again = subprocess.run(
        ["gcloud", "iam", "service-accounts", "create", "swarm-agent-worker-u-alice", "--project", "saga-agents-staging"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert again.returncode == REFUSED and _ran(tmp_path) == []
    # The first refusal's reason is the one kept.
    assert json.loads(Path(ready["SWARM_CALL_GUARD"] + ".stop").read_text())["rule"] == "C2"


def test_report_only_lets_the_owner_run_and_records_nothing(tmp_path: Path, ready: dict[str, str]) -> None:
    env = {**_shim_env(ready, tmp_path), "SWARM_CALL_GUARD_MODE": "report-only"}
    out = subprocess.run(
        ["gcloud", "iam", "service-accounts", "delete", "swarm-agent-worker-u-alice@saga-agents-staging.iam.gserviceaccount.com"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "would refuse (rule C9)" in out.stderr
    assert len(_ran(tmp_path)) == 1
    assert not Path(ready["SWARM_CALL_GUARD"] + ".stop").exists()


def test_report_only_is_ignored_where_the_job_enforces(tmp_path: Path, ready: dict[str, str]) -> None:
    env = {**_shim_env(ready, tmp_path), "SWARM_CALL_GUARD_MODE": "report-only", "SWARM_CALL_GUARD_ENFORCE": "1"}
    out = subprocess.run(
        ["gcloud", "iam", "service-accounts", "delete", "swarm-agent-worker-u-alice@saga-agents-staging.iam.gserviceaccount.com"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert out.returncode == REFUSED and _ran(tmp_path) == []


def test_curl_reaches_the_real_binary_with_its_config_on_stdin(tmp_path: Path, ready: dict[str, str]) -> None:
    env = _shim_env(ready, tmp_path)
    # The line fs_request writes, from common.sh's own auth_config, around a
    # token made here: nothing in this file may look like a credential.
    config = subprocess.run(
        ["bash", "-c", f'source "{COMMON}"; auth_config "$1"', "_", secrets.token_hex(20)],
        capture_output=True, text=True, check=True,
    ).stdout + "\n"
    url = (
        "https://firestore.googleapis.com/v1/projects/saga-agents-staging/databases/swarm/documents/"
        "workspaces/u-alice?updateMask.fieldPaths=state"
    )
    out = subprocess.run(
        ["curl", "-sS", "-K", "-", "-X", "PATCH", "-H", "Content-Type: application/json",
         "--data-binary", '{"fields":{}}', url],
        input=config, capture_output=True, text=True, env=env, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "curl.stdin").read_text() == config


def test_no_expectation_means_no_call(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.pop("SWARM_CALL_GUARD")
    out = _guard("check", "--", "gcloud", "config", "get-value", "account", env=env)
    assert out.returncode == REFUSED and "nothing runs unguarded" in out.stderr


def test_an_expectation_others_can_write_is_refused(tmp_path: Path, ready: dict[str, str]) -> None:
    Path(ready["SWARM_CALL_GUARD"]).chmod(0o622)
    out = _guard("check", "--", "gcloud", "config", "get-value", "account", env=ready)
    assert out.returncode == REFUSED and "mode 0600" in out.stderr


def test_the_expectation_is_private_and_names_the_workspace(ready: dict[str, str]) -> None:
    path = Path(ready["SWARM_CALL_GUARD"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    exp = json.loads(path.read_text())
    assert exp["workspace_id"] == WORKSPACE and exp["tenant_id"] == "u-alice"
    assert exp["worker_email"] == "swarm-agent-worker-u-alice@saga-agents-staging.iam.gserviceaccount.com"
    assert exp["namespace"] == "swarm-tenant-u-alice"
    assert exp["contexts"] == ["swarm-dev", "gke_saga-agents-staging_us-central1_swarm-autopilot"]
    assert "principal" not in exp and "alice@saga.xyz" not in path.read_text()


def test_init_refuses_a_malformed_workspace_id(tmp_path: Path) -> None:
    out = _guard("init", "--workspace-id", "u-alice", env=_env(tmp_path))
    assert out.returncode != 0 and not (tmp_path / "expect.json").exists()


def test_init_refuses_a_second_workspace(tmp_path: Path, ready: dict[str, str]) -> None:
    out = _guard("init", "--workspace-id", "w-000000", env=ready)
    assert out.returncode != 0
    assert json.loads(Path(ready["SWARM_CALL_GUARD"]).read_text())["workspace_id"] == WORKSPACE


# --------------------------------------------------------------------------
# the derived names agree with the code that makes the resources
# --------------------------------------------------------------------------


def _render(tenant: str) -> str:
    out = subprocess.run(
        [sys.executable, str(REPO / "kubernetes" / "render.py"), "tenant", "--tenant", tenant,
         "--bound-ksa", "swarm-worker",
         "--scheduler-uid", "1" * 21, "--reconciler-uid", "2" * 21],
        capture_output=True, text=True, check=True, timeout=120,
    )
    return out.stdout


def test_the_real_tenant_render_passes_c7_for_its_own_tenant(ready: dict[str, str]) -> None:
    out = _guard("check", "--", "kubectl", "--context", "swarm-dev", "apply", "-f", "-", env=ready, stdin=_render("u-alice"))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "allow C7"


def test_the_real_tenant_render_of_another_tenant_is_refused(ready: dict[str, str]) -> None:
    out = _guard("check", "--", "kubectl", "--context", "swarm-dev", "apply", "-f", "-", env=ready, stdin=_render("u-bob"))
    assert out.returncode == REFUSED and "rule C7" in out.stderr


def _assignment_lines(path: Path, names: tuple[str, ...]) -> list[str]:
    lines = []
    for line in path.read_text().splitlines():
        stripped = line.strip().removeprefix("local ")
        if any(stripped.startswith(f"{n}=") for n in names):
            lines.append(stripped)
    return lines


def test_the_bucket_conditions_and_role_ids_are_register_tenants() -> None:
    names = (
        "OBJECT_PREFIX_EXPR", "LIST_PREFIX_EXPR", "VERDICTS_PREFIX_EXPR", "READ_EXPR", "WRITE_EXPR",
        "CUSTOM_ROLE_SUFFIX_MODULE", "ROLE_ID_SUFFIX", "FIRESTORE_ROLE_ID", "BUCKET_METADATA_ROLE_ID",
    )
    script = _assignment_lines(REGISTER, names)
    guard = _assignment_lines(GUARD, names)
    assert len(guard) == len(names), guard
    assert set(guard) <= set(script), sorted(set(guard) - set(script))


def test_the_forge_slot_is_the_one_swarm_api_creates(ready: dict[str, str]) -> None:
    from swarm_api.gittokens import Scope, provider_suffix

    exp = json.loads(Path(ready["SWARM_CALL_GUARD"]).read_text())
    suffix = provider_suffix(Scope.USER, user="alice@saga.xyz")
    assert exp["forge_slot"] == f"swarm-tenant-u-alice-{suffix}"
    assert exp["forge_slot_twin"] == f"swarm-tenant-u-alice-{suffix}-refresh"


def test_curl_runs_with_q_first_so_no_curlrc_joins_the_call(tmp_path: Path, ready: dict[str, str]) -> None:
    env = _shim_env(ready, tmp_path)
    out = subprocess.run(
        ["curl", "-sf", "-H", "Metadata-Flavor: Google",
         "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert _ran(tmp_path)[0].startswith("curl -q -sf ")


@pytest.mark.parametrize(
    "variable,value",
    [
        ("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"),
        ("CLOUDSDK_CORE_PROJECT", "saga-agents-prod"),
    ],
)
def test_gcloud_environment_that_changes_identity_or_project_is_refused(
    ready: dict[str, str], variable: str, value: str
) -> None:
    allowed = ("gcloud", "iam", "service-accounts", "describe",
               "swarm-agent-worker-u-alice@saga-agents-staging.iam.gserviceaccount.com", "--project", "saga-agents-staging")
    assert _guard("check", "--", *allowed, env=ready).returncode == 0
    out = _guard("check", "--", *allowed, env={**ready, variable: value})
    assert out.returncode == REFUSED and variable in out.stderr
