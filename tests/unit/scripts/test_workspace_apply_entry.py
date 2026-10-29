"""The workspace job's entrypoint scrubs what a caller can override (W6b, #847).

docs/workspaces.md §2.2 step 0. The Cloud Run job `swarm-workspace-apply` runs
`python3 -I /opt/swarm/entry.py --spec-verify-keys-sha256=<hex> <w-id> <mode>`
(the first argument is the command's own, lane WK-APPROVAL-KEYS) as
swarm-workspace-deployer, an
identity with project-wide account-IAM power. Anyone holding
`run.jobs.runWithOverrides` on the job -- the scheduler, the acceptance account
and the release deployer, project-wide and unscopably (§2.1) -- may start an
execution with any arguments, any environment variables, any task count and
any timeout. The command is the one thing they cannot change, so this file is
the boundary. What these tests hold, each against the REAL entry.py:

* EVERY ENVIRONMENT VARIABLE HANDED IN IS GONE IN THE CHILD. entry.py really
  `execve`s, through the real coreutils `timeout` and the real /bin/bash, a
  script that copies its own /proc/<pid>/environ; that environment must be
  exactly the fixed one, carry no value the caller chose -- including for the
  names entry.py sets itself -- and an exported bash function or BASH_ENV must
  not have run;
* ANY ARGUMENT LIST BUT [<pin>, <w-id>, <mode>] IS REFUSED BEFORE ANYTHING
  RUNS: no metadata request, no directory made, no exec;
* SPEC_VERIFY_KEYS IS THE JOB SPEC'S OR NOTHING RUNS: its SHA-256 must be the
  one the fixed command carries, so an override that sets it (or drops it, or
  a command without the pin) is refused the same way, and it never reaches
  the child;
* A TASK COUNT OTHER THAN ONE IS REFUSED, the same way, and so is any task but
  the first;
* the run's own deadline holds whatever timeout the execution was given, and
  the build-id rule is register-tenant.sh's, not a second copy that drifts.

Each refusal test has its positive control beside it (the same call with the
one value made valid reaches the exec), so a refusal cannot pass by refusing
everything. No cloud, no metadata server: the metadata lookup is injected.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
ENTRY = REPO / "images" / "workspace-apply" / "entry.py"
REGISTER = REPO / "scripts" / "register-tenant.sh"

WORKSPACE = "w-3f9a2c"
EXECUTION = "swarm-workspace-apply-x7k2p"
PROJECT = "fixture-project-1"
REGION = "us-central1"

#: The job spec's SPEC_VERIFY_KEYS and the command element that pins it
#: (terraform/bootstrap/workspace_deployer.tf). No version here: these runs
#: are personal, whose approval is not verified.
KEYS = "{}"
PIN = "--spec-verify-keys-sha256=" + hashlib.sha256(KEYS.encode("utf-8")).hexdigest()

#: What the child's environment holds, by name, and nothing else (§2.2 step 0).
FIXED = {
    "PATH", "HOME", "PROJECT_ID", "REGION", "SWARM_ENV_FILE", "SWARM_CALL_GUARD",
    "SWARM_CALL_GUARD_ENFORCE", "SWARM_KUBECTL", "CLOUDSDK_PYTHON", "BUILD_ID", "NO_COLOR",
    # What the approval check read (lane WK-SIGNED-APPROVAL): always set, so
    # the guard always compares (tests/unit/scripts/test_workspace_apply_approval.py).
    "SWARM_APPROVAL_PIN",
}

needs_exec = pytest.mark.skipif(
    not (Path("/proc/self/environ").exists() and Path("/usr/bin/timeout").exists() and Path("/bin/bash").exists()),
    reason="the exec test needs Linux's /proc, /usr/bin/timeout and /bin/bash",
)


def _load():
    spec = importlib.util.spec_from_file_location("workspace_apply_entry", ENTRY)
    assert spec and spec.loader, ENTRY
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def entry():
    return _load()


def _cloud_run_env(**overrides: str | None) -> dict[str, str]:
    env = {
        "CLOUD_RUN_JOB": "swarm-workspace-apply",
        "CLOUD_RUN_EXECUTION": EXECUTION,
        "CLOUD_RUN_TASK_COUNT": "1",
        "CLOUD_RUN_TASK_INDEX": "0",
        "CLOUD_RUN_TASK_ATTEMPT": "0",
        "SPEC_VERIFY_KEYS": KEYS,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class PersonalCloud:
    """The approval check's Firestore, holding one approved personal record:
    a personal approval is not verified, so the run goes on to the exec. The
    approval check itself is tests/unit/scripts/test_workspace_apply_approval.py's."""

    def __init__(self, project: str) -> None:
        self.project = project

    def record(self, workspace_id: str):
        return ("projects/p/databases/swarm/documents/workspaces/u-alice", "2026-10-19T00:00:00Z", {
            "workspace_id": workspace_id, "tenant_id": "u-alice", "principal": "alice@example.com",
            "state": "approved", "decision": {"verdict": "approved", "by": "admin@example.com"}})


class Recorder:
    """The two things entry.py may do once it has decided: ask, and exec."""

    def __init__(self) -> None:
        self.metadata_paths: list[str] = []
        self.execs: list[tuple[str, list[str], dict[str, str]]] = []

    def metadata(self, path: str) -> str:
        self.metadata_paths.append(path)
        return {"project/project-id": PROJECT,
                "instance/region": f"projects/123456789012/regions/{REGION}"}[path]

    def execve(self, program: str, args: list[str], env: dict[str, str]) -> None:
        self.execs.append((program, args, env))


def _call(entry, tmp_path: Path, argv: list[str], environ: dict[str, str]) -> tuple[int, Recorder]:
    rec = Recorder()
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    rc = entry.main(argv, environ, metadata=rec.metadata, scratch_root=str(scratch),
                    target=Path("/opt/swarm/scripts/workspace-apply.sh"), execve=rec.execve,
                    cloud=PersonalCloud)
    return rc, rec


def _pinned(argv: list[str]) -> list[str]:
    """ARGV as an execution passes it: after the command's own pin."""
    return [PIN, *argv]


def _ran_nothing(rc: int, rec: Recorder, tmp_path: Path) -> None:
    assert rc != 0, "the call was not refused"
    assert rec.metadata_paths == [], f"refused only after asking the metadata server: {rec.metadata_paths}"
    assert rec.execs == [], "refused, but still exec'd"
    assert list((tmp_path / "scratch").iterdir()) == [], "refused, but made the run's directory first"


# ---------------------------------------------------------------------------
# The arguments: exactly [<pin>, <w-id>, <mode>], the pin the command's.
# ---------------------------------------------------------------------------
BAD_ARGUMENTS = [
    pytest.param([], id="none"),
    pytest.param([WORKSPACE], id="id-only"),
    pytest.param(["create"], id="mode-only"),
    pytest.param([WORKSPACE, "create", "extra"], id="three"),
    pytest.param([WORKSPACE, "create", "--mode", "verify"], id="four"),
    pytest.param(["create", WORKSPACE], id="swapped"),
    pytest.param(["--workspace", WORKSPACE], id="a-flag"),
    pytest.param([WORKSPACE, "verify"], id="mode-verify"),
    pytest.param([WORKSPACE, "destroy"], id="mode-destroy"),
    pytest.param([WORKSPACE, "Create"], id="mode-case"),
    pytest.param([WORKSPACE, "create "], id="mode-trailing-space"),
    pytest.param([WORKSPACE, ""], id="mode-empty"),
    pytest.param(["w-3F9A2C", "create"], id="id-upper"),
    pytest.param(["w-3f9a2", "create"], id="id-short"),
    pytest.param(["w-3f9a2cc", "create"], id="id-long"),
    pytest.param(["u-alice", "create"], id="id-tenant"),
    pytest.param([WORKSPACE + "\n", "create"], id="id-newline"),
    pytest.param([WORKSPACE + ";id", "create"], id="id-shell"),
    pytest.param(["w-３f9a2c", "create"], id="id-unicode-digit"),
    pytest.param(["", ""], id="empty"),
]


@pytest.mark.parametrize("argv", BAD_ARGUMENTS)
def test_any_argument_list_but_an_id_and_a_mode_is_refused_before_anything_runs(entry, tmp_path, argv):
    rc, rec = _call(entry, tmp_path, _pinned(argv), _cloud_run_env())
    _ran_nothing(rc, rec, tmp_path)


OTHER_PIN = "--spec-verify-keys-sha256=" + hashlib.sha256(b'{"x":"y"}').hexdigest()


@pytest.mark.parametrize("argv", [
    pytest.param([WORKSPACE, "create"], id="no-pin"),
    pytest.param([WORKSPACE, PIN, "create"], id="pin-second"),
    pytest.param([WORKSPACE, "create", PIN], id="pin-last"),
    pytest.param([OTHER_PIN, WORKSPACE, "create"], id="another-sets-pin"),
    pytest.param([PIN.upper(), WORKSPACE, "create"], id="pin-upper"),
    pytest.param([PIN[:-1], WORKSPACE, "create"], id="pin-short"),
    pytest.param([PIN + "0", WORKSPACE, "create"], id="pin-long"),
    pytest.param([PIN.replace("sha256", "sha512"), WORKSPACE, "create"], id="pin-other-name"),
    pytest.param([PIN, PIN, WORKSPACE, "create"], id="pin-twice"),
])
def test_an_argument_list_without_the_commands_pin_first_is_refused(entry, tmp_path, argv):
    rc, rec = _call(entry, tmp_path, argv, _cloud_run_env())
    _ran_nothing(rc, rec, tmp_path)


@pytest.mark.parametrize("keys", [
    pytest.param(None, id="absent"),
    pytest.param("", id="empty"),
    pytest.param(KEYS + " ", id="one-byte-more"),
    pytest.param('{"x":"y"}', id="another-key-set"),
])
def test_spec_verify_keys_other_than_the_job_specs_is_refused_before_anything_runs(entry, tmp_path, keys):
    """A caller holding runWithOverrides may set any environment variable,
    SPEC_VERIFY_KEYS included, but not the command: a value whose SHA-256 is
    not the command's pin is not the job spec's, and nothing runs."""
    rc, rec = _call(entry, tmp_path, _pinned([WORKSPACE, "create"]), _cloud_run_env(SPEC_VERIFY_KEYS=keys))
    _ran_nothing(rc, rec, tmp_path)


@pytest.mark.parametrize("mode", ["create", "limits"])
def test_an_id_and_a_mode_reach_the_exec(entry, tmp_path, mode):
    """The positive control for every refusal above and below."""
    rc, rec = _call(entry, tmp_path, _pinned([WORKSPACE, mode]), _cloud_run_env())
    assert rc == 0
    assert rec.metadata_paths == ["project/project-id", "instance/region"]
    [(program, args, env)] = rec.execs
    assert program == "/usr/bin/timeout"
    assert args == ["/usr/bin/timeout", "--kill-after=30s", "1800s", "/bin/bash",
                    "/opt/swarm/scripts/workspace-apply.sh", WORKSPACE, mode]
    assert set(env) == FIXED
    assert env["PROJECT_ID"] == PROJECT and env["REGION"] == REGION
    assert env["BUILD_ID"] == EXECUTION
    assert env["SWARM_CALL_GUARD_ENFORCE"] == "1"


@pytest.mark.parametrize("argv", [
    pytest.param([], id="none"),
    pytest.param([WORKSPACE, "create", "extra"], id="three"),
    pytest.param(["w-zzzzzz", "create"], id="bad-id"),
    pytest.param([WORKSPACE, "verify"], id="bad-mode"),
])
def test_the_real_command_refuses_bad_arguments_without_the_metadata_server(tmp_path, argv):
    """`python3 -I entry.py ...`, as the job runs it: no metadata server here, so
    a check that came after the lookup would fail on the lookup instead."""
    proc = subprocess.run([sys.executable, "-I", str(ENTRY), PIN, *argv], env=_cloud_run_env(),
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 1, proc.stderr
    assert "refused: the" in proc.stderr and "argument" in proc.stderr, proc.stderr
    assert "metadata" not in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# The task count: one, and this the first task.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("count", [None, "", "0", "2", "10", "01", " 1", "1 ", "1\n", "one", "-1"])
def test_a_task_count_other_than_one_is_refused_before_anything_runs(entry, tmp_path, count):
    rc, rec = _call(entry, tmp_path, _pinned([WORKSPACE, "create"]), _cloud_run_env(CLOUD_RUN_TASK_COUNT=count))
    _ran_nothing(rc, rec, tmp_path)


@pytest.mark.parametrize("index", [None, "1", "2", ""])
def test_any_task_but_the_first_is_refused(entry, tmp_path, index):
    rc, rec = _call(entry, tmp_path, _pinned([WORKSPACE, "create"]), _cloud_run_env(CLOUD_RUN_TASK_INDEX=index))
    _ran_nothing(rc, rec, tmp_path)


@pytest.mark.parametrize("execution", [None, "", "a b", "x" * 81, "exec$(id)", "exec\n"])
def test_an_execution_name_outside_the_build_id_rule_is_refused(entry, tmp_path, execution):
    rc, rec = _call(entry, tmp_path, _pinned([WORKSPACE, "create"]), _cloud_run_env(CLOUD_RUN_EXECUTION=execution))
    _ran_nothing(rc, rec, tmp_path)


def test_the_build_id_rule_is_register_tenants(entry):
    """WS_BUILD_RE is checked again by register-tenant.sh on BUILD_ID; a second
    copy here that drifted would pass a name the script then refuses, or the
    reverse."""
    found = re.search(r"^WS_BUILD_RE='([^']*)'$", REGISTER.read_text(), re.M)
    assert found, "register-tenant.sh no longer defines WS_BUILD_RE on one line"
    assert entry.BUILD_RE == found.group(1)
    found = re.search(r"^WS_ID_RE='\^([^']*)\$'$", REGISTER.read_text(), re.M)
    assert found and entry.WORKSPACE_RE.pattern == found.group(1)


@pytest.mark.parametrize("answers", [
    {"project/project-id": "Evil Project", "instance/region": "projects/1/regions/us-central1"},
    {"project/project-id": PROJECT, "instance/region": "projects/1/regions/us central1"},
    {"project/project-id": PROJECT + "\n--impersonate", "instance/region": "projects/1/regions/us-central1"},
])
def test_a_metadata_answer_that_is_not_a_name_is_refused(entry, tmp_path, answers):
    execs: list = []
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    rc = entry.main([PIN, WORKSPACE, "create"], _cloud_run_env(), metadata=answers.__getitem__,
                    scratch_root=str(scratch), execve=lambda *a: execs.append(a), cloud=PersonalCloud)
    assert rc == 1 and execs == [] and list(scratch.iterdir()) == []


def test_the_caller_environment_is_emptied_before_anything_reads_it(entry, tmp_path):
    """Given os.environ, plan() empties the process's own environment, so the
    metadata request (urllib reads http_proxy from it) and anything else in
    the process see nothing a caller chose."""
    environ = _cloud_run_env(http_proxy="http://127.0.0.1:9", PATH="/evil")
    seen: list[dict] = []

    def metadata(path: str) -> str:
        seen.append(dict(environ))
        return Recorder().metadata(path)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    rc = entry.main([PIN, WORKSPACE, "create"], environ, metadata=metadata, scratch_root=str(scratch),
                    execve=lambda *a: None, cloud=PersonalCloud)
    assert rc == 0 and seen == [{}, {}] and environ == {}


# ---------------------------------------------------------------------------
# The environment: none of it reaches the child.
# ---------------------------------------------------------------------------
_DRIVER = textwrap.dedent("""
    import importlib.util, sys
    from pathlib import Path
    entry_path, scratch, target, deadline, *argv = sys.argv[1:]
    spec = importlib.util.spec_from_file_location("entry", entry_path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.DEADLINE_SECONDS = int(deadline)
    entry.KILL_AFTER_SECONDS = 1
    answers = {"project/project-id": "%s", "instance/region": "projects/123456789012/regions/%s"}
    class PersonalCloud:
        def __init__(self, project):
            pass
        def record(self, workspace_id):
            return ("n", "t", {"workspace_id": workspace_id, "tenant_id": "u-alice",
                               "principal": "alice@example.com",
                               "decision": {"verdict": "approved", "by": "admin@example.com"}})
    sys.exit(entry.main(argv, None, metadata=answers.__getitem__, scratch_root=scratch, target=Path(target),
                        cloud=PersonalCloud))
""" % (PROJECT, REGION))


def _hostile_environment(tmp_path: Path) -> dict[str, str]:
    """What a caller holding runWithOverrides could hand an execution."""
    marker = tmp_path / "hijacked"
    bash_env = tmp_path / "bash_env.sh"
    bash_env.write_text(f'echo BASH_ENV >> "{marker}"\n')
    evil_bin = tmp_path / "evil-bin"
    evil_bin.mkdir()
    for tool in ("cat", "bash", "env", "timeout"):
        path = evil_bin / tool
        path.write_text(f'#!/bin/sh\necho PATH:{tool} >> "{marker}"\n')
        path.chmod(0o755)
    # Built at run time: nothing in this file may look like a credential.
    random_name = "X_" + secrets.token_hex(6).upper()
    return {
        "PATH": f"{evil_bin}:/usr/bin:/bin",
        "BASH_ENV": str(bash_env),
        "ENV": str(bash_env),
        "BASH_FUNC_cat%%": f'() {{ echo FUNCTION >> "{marker}"; }}',
        "SHELLOPTS": "xtrace",
        "LD_LIBRARY_PATH": str(tmp_path / "no-such-lib-dir"),
        "PYTHONPATH": str(tmp_path / "py"),
        "PYTHONSTARTUP": str(bash_env),
        "PYTHONINSPECT": "caller-inspect",
        "http_proxy": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HOME": str(tmp_path / "caller-home"),
        "PROJECT_ID": "someone-elses-project",
        "REGION": "europe-west9",
        "SWARM_ENV_FILE": str(bash_env),
        "SWARM_CALL_GUARD": str(tmp_path / "caller-expect.json"),
        "SWARM_CALL_GUARD_ENFORCE": "0",
        "SWARM_CALL_GUARD_MODE": "report-only",
        "SWARM_KUBECTL": str(evil_bin / "kubectl"),
        "CLOUDSDK_PYTHON": str(evil_bin / "python3"),
        "CLOUDSDK_CORE_PROJECT": "someone-elses-project",
        "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": str(tmp_path / "caller-token"),
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "caller-key.json"),
        "KUBECONFIG": str(tmp_path / "caller-kubeconfig"),
        "BUILD_ID": "caller-chosen-build",
        "NO_COLOR": "caller",
        "ARTIFACT_BUCKET": "someone-elses-bucket",
        "FIRESTORE_DATABASE": "(default)",
        "GKE_CLUSTER": "agents-staging",
        "SWARM_WORKSPACE_RETRY_DELAYS": "100000",
        random_name: secrets.token_hex(8),
    }


def _dumper(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Step 1's stand-in: copies the environment execve handed its bash."""
    env_out = tmp_path / "child.environ"
    args_out = tmp_path / "child.args"
    script = tmp_path / "workspace-apply.sh"
    # `cat` unqualified on purpose: an exported BASH_FUNC_cat%% or a PATH that
    # survived would run instead and leave its mark.
    script.write_text(textwrap.dedent(f"""\
        printf '%s\\0' "$@" > "{args_out}"
        cat /proc/$$/environ > "{env_out}"
    """))
    return script, env_out, args_out


def _run_entry(tmp_path: Path, env: dict[str, str], target: Path, deadline: int = 1800,
               argv: tuple[str, ...] = (PIN, WORKSPACE, "create")) -> subprocess.CompletedProcess[str]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-I", "-c", _DRIVER, str(ENTRY), str(scratch), str(target), str(deadline), *argv],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )


@needs_exec
def test_every_environment_variable_handed_in_is_gone_in_the_child(tmp_path):
    hostile = _hostile_environment(tmp_path)
    target, env_out, args_out = _dumper(tmp_path)
    proc = _run_entry(tmp_path, {**hostile, **_cloud_run_env()}, target)
    assert proc.returncode == 0, proc.stderr
    assert env_out.exists(), f"the child never ran: {proc.stderr}"

    child = dict(item.split("=", 1) for item in env_out.read_bytes().decode().split("\0") if item)
    assert set(child) == FIXED, f"names in the child beyond the fixed set: {sorted(set(child) - FIXED)}"
    for name in _cloud_run_env():
        assert name not in child, f"{name} reached the child"
    for name, value in hostile.items():
        assert child.get(name) != value, f"{name} reached the child with the caller's value"
        for fixed_value in child.values():
            assert value not in fixed_value.split(":"), f"the caller's {name} is inside the child's environment"
    assert child["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/google-cloud-sdk/bin"
    assert child["SWARM_CALL_GUARD_ENFORCE"] == "1"
    assert child["PROJECT_ID"] == PROJECT and child["REGION"] == REGION
    assert child["BUILD_ID"] == EXECUTION
    assert child["SWARM_KUBECTL"] == "/usr/local/bin/kubectl"
    assert child["CLOUDSDK_PYTHON"] == "/usr/bin/python3"
    assert child["SWARM_CALL_GUARD"].startswith(str(tmp_path / "scratch"))
    assert child["HOME"].startswith(str(tmp_path / "scratch"))
    assert oct(Path(child["HOME"]).stat().st_mode & 0o777) == "0o700"
    assert not Path(child["SWARM_ENV_FILE"]).exists()

    assert args_out.read_bytes().decode().split("\0")[:-1] == [WORKSPACE, "create"]
    assert not (tmp_path / "hijacked").exists(), (tmp_path / "hijacked").read_text()


@needs_exec
def test_the_control_a_leaked_environment_would_be_seen(tmp_path):
    """The dumper above can see a leak: bash, handed the hostile environment
    directly, runs the caller's BASH_ENV and exported function, and its
    environ carries the caller's names. Without this, an empty dump would
    pass the test above."""
    hostile = _hostile_environment(tmp_path)
    target, env_out, _ = _dumper(tmp_path)
    marker = tmp_path / "hijacked"
    subprocess.run(["/bin/bash", str(target)], env=hostile, capture_output=True, timeout=60, check=False)
    assert marker.read_text().split() == ["BASH_ENV", "FUNCTION"]
    # Without the function and the PATH, which would each replace `cat`, the
    # dump itself carries the caller's names.
    marker.unlink()
    plain = {k: v for k, v in hostile.items() if k not in ("BASH_FUNC_cat%%", "PATH")}
    subprocess.run(["/bin/bash", str(target)], env=plain, capture_output=True, timeout=60, check=False)
    child = dict(item.split("=", 1) for item in env_out.read_bytes().decode().split("\0") if item)
    assert child.get("SWARM_CALL_GUARD_ENFORCE") == "0" and child.get("BUILD_ID") == "caller-chosen-build"
    assert marker.read_text().split() == ["BASH_ENV"]


@needs_exec
def test_the_run_has_its_own_deadline(tmp_path):
    """Whatever timeout the execution was given, step 1 runs under entry.py's
    deadline (shortened here to one second): `timeout` ends it, and the
    execution fails rather than running on as the deployer."""
    target = tmp_path / "slow.sh"
    target.write_text("sleep 30\n")
    proc = _run_entry(tmp_path, _cloud_run_env(), target, deadline=1)
    assert proc.returncode == 124, proc.stderr


def test_the_child_is_step_one_through_a_fixed_bash_and_timeout(entry):
    """No program is looked up on a PATH: bash and timeout by absolute path,
    the script at its place in the image, and the image's deadline."""
    assert entry.TIMEOUT == "/usr/bin/timeout" and entry.BASH == "/bin/bash"
    assert str(entry.APPLY_SCRIPT) == "/opt/swarm/scripts/workspace-apply.sh"
    assert entry.DEADLINE_SECONDS == 1800
    assert os.access(REPO / "scripts" / "workspace-apply.sh", os.X_OK)


# ---------------------------------------------------------------------------
# The trusted signing key is the fixed command's (DAYONE-BOOT-1, plan R1).
# ---------------------------------------------------------------------------
#: The step-spec key of a project that is NOT the live deployment's: until
#: DAYONE-BOOT-1 the image trusted one project's key, so a fresh project's
#: group or service workspace always stopped at APPROVAL_UNSIGNED (#2688).
SIGNING_KEY = f"projects/{PROJECT}/locations/{REGION}/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
SIGNING = "--spec-signing-key=" + SIGNING_KEY
OWNER = "owner@example.com"
REQUEST_ID = "5d0c6a3e-1b7f-4c55-9a51-0f1c2b3d4e5f"
SERVICE_DOC = f"projects/{PROJECT}/databases/swarm/documents/workspaces/u-re-75c2e5"


class SignedService:
    """A P-256 key standing in for Cloud KMS's step-spec key version, the
    SPEC_VERIFY_KEYS the bootstrap renders for it, and a service record
    approved and signed with it as swarm-api's approval writes one."""

    def __init__(self, entry, version: str) -> None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        private = ec.generate_private_key(ec.SECP256R1())
        self.version = version
        self.keys = json.dumps({version: private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()})
        self.pin = "--spec-verify-keys-sha256=" + hashlib.sha256(self.keys.encode("utf-8")).hexdigest()
        self.doc = {
            "workspace_id": WORKSPACE, "tenant_id": "u-re-75c2e5", "kind": "service",
            "principal": f"reports-bot@{PROJECT}.iam.gserviceaccount.com", "state": "approved",
            "request_id": REQUEST_ID, "decision": {"verdict": "approved", "by": OWNER},
        }
        payload = {key: self.doc[key] for key in ("workspace_id", "tenant_id", "kind", "principal")}
        payload.update(decided_by=OWNER, decided_at="2026-10-19T09:30:00+00:00", request_id=REQUEST_ID)
        der = private.sign(entry.specsign.spec_digest(entry.canonical_approval(payload)),
                           ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        self.doc["approval"] = {
            "decided_by": OWNER, "decided_at": payload["decided_at"], "request_id": REQUEST_ID,
            "key_version": version, "signature": base64.b64encode(der).decode("ascii"),
        }
        self.commits: list[Any] = []

    def __call__(self, project: str) -> SignedService:
        assert project == PROJECT
        return self

    def record(self, workspace_id: str):
        return (SERVICE_DOC, "2026-10-19T09:31:00.000001Z", copy.deepcopy(self.doc))

    def commit(self, write: Any) -> None:
        self.commits.append(write)


def _signed_run(entry, tmp_path: Path, service: SignedService, command: list[str],
                environ: dict[str, str] | None = None) -> tuple[int, Recorder]:
    rec = Recorder()
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    rc = entry.main([*command, WORKSPACE, "create"],
                    _cloud_run_env(SPEC_VERIFY_KEYS=service.keys) if environ is None else environ,
                    metadata=rec.metadata, scratch_root=str(scratch),
                    target=Path("/opt/swarm/scripts/workspace-apply.sh"), execve=rec.execve,
                    cloud=service)
    return rc, rec


def test_signing_key_from_fixed_command(entry, tmp_path):
    """A project other than the live one trusts its OWN step-spec key, named by
    the job's fixed command: the service approval verifies and step 1 runs,
    pinned as verified. The negative control beside it: the command naming
    another key (prod's ring) stops the same record for the owner."""
    service = SignedService(entry, SIGNING_KEY + "/cryptoKeyVersions/1")
    rc, rec = _signed_run(entry, tmp_path, service, [service.pin, SIGNING])
    assert rc == 0 and service.commits == [], "the project's own key was not trusted"
    [(_, args, env)] = rec.execs
    assert args[-2:] == [WORKSPACE, "create"]
    assert json.loads(env["SWARM_APPROVAL_PIN"])["verified"] is True

    other = SignedService(entry, SIGNING_KEY + "/cryptoKeyVersions/1")
    rc, rec = _signed_run(entry, tmp_path, other,
                          [other.pin, SIGNING.replace("swarm-dev-specs", "swarm-prod-specs")])
    assert rc == 0 and rec.execs == [], "a version of a key the command does not name was trusted"
    assert len(other.commits) == 1, "the untrusted approval was not stopped for the owner"


@pytest.mark.parametrize("variable", ["SPEC_SIGNING_KEY", "SIGNING_KEYS", "SPEC_SIGNING_KEYS"])
def test_signing_key_not_from_environment(entry, tmp_path, variable):
    """An execution override may set any variable but cannot touch the
    command. A variable carrying the record's key changes nothing: with the
    command naming another key the record still stops for the owner, and with
    a command that names no key nothing runs at all."""
    service = SignedService(entry, SIGNING_KEY + "/cryptoKeyVersions/1")
    environ = _cloud_run_env(SPEC_VERIFY_KEYS=service.keys, **{variable: SIGNING_KEY})
    other_key = SIGNING.replace("swarm-dev-specs", "swarm-prod-specs")
    rc, rec = _signed_run(entry, tmp_path, service, [service.pin, other_key], environ=dict(environ))
    assert rc == 0 and rec.execs == [] and len(service.commits) == 1

    again = tmp_path / "again"
    rc, rec = _signed_run(entry, again, service, [service.pin], environ=dict(environ))
    _ran_nothing(rc, rec, again)


@pytest.mark.parametrize("value", [
    pytest.param("", id="empty"),
    pytest.param(SIGNING_KEY + "/cryptoKeyVersions/1", id="a-version"),
    pytest.param(SIGNING_KEY.replace("step-spec", "other"), id="not-step-spec"),
    pytest.param(SIGNING_KEY.replace(PROJECT, "Fixture-project-1"), id="project-upper"),
    pytest.param(SIGNING_KEY.replace(PROJECT, "p1"), id="project-short"),
    pytest.param(SIGNING_KEY.replace("keyRings/swarm-dev-specs", "keyRings/a/b"), id="ring-slash"),
    pytest.param(SIGNING_KEY + "\n", id="newline"),
    pytest.param(" " + SIGNING_KEY, id="leading-space"),
])
def test_a_signing_key_argument_that_is_not_a_step_spec_key_is_refused(entry, tmp_path, value):
    rc, rec = _call(entry, tmp_path, [PIN, "--spec-signing-key=" + value, WORKSPACE, "create"],
                    _cloud_run_env())
    _ran_nothing(rc, rec, tmp_path)


def test_a_signing_key_of_another_project_than_the_jobs_is_refused(entry, tmp_path):
    """The command is the deployment's own: a key in another project than the
    metadata server's is a job spec rendered for somewhere else."""
    rc, rec = _call(entry, tmp_path,
                    [PIN, SIGNING.replace(PROJECT, "another-project-1"), WORKSPACE, "create"],
                    _cloud_run_env())
    assert rc == 1 and rec.execs == []
