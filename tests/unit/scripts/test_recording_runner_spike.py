"""`scripts/recording-runner-spike.sh`'s guards (docs/runbooks/recording-runner-spike.md).

The spike creates two 16-core pods and a disk claim in a tenant namespace, so
the properties that matter are the ones that keep it to what it says:

  * nothing is created without the namespace TYPED at a terminal: no terminal,
    a wrong answer, or SWARM_ASSUME_YES alone creates nothing;
  * a namespace on the deployment's never-touch list is refused before kubectl
    runs at all;
  * the delete is by the run's own label, in the namespace given, after the
    run id is typed, and by nothing else: never a name, never `--all`, never
    across namespaces.

Each runs the REAL script with a fake `kubectl` (SWARM_KUBECTL) that answers
like an admitting cluster and logs every call. No cloud credentials, no
cluster, no network. What this cannot prove: that a real API server, GKE
Sandbox and Docker answer as the fake does -- that is the spike itself.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "recording-runner-spike.sh"
NAMESPACE = "swarm-tenant-eng"
#: A tenant namespace this test's never-touch list names.
PEER_NAMESPACE = "swarm-tenant-peer"
DIGEST = "0" * 64
IMAGE = f"registry.example.com/spike/dind@sha256:{DIGEST}"
STORAGE_CLASS = "swarm-cache-pd-balanced"
RUN_ID = "rrs-20261124120000"
SELECTOR = f"swarm-spike=recording-runner,swarm-spike-run={RUN_ID}"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the script needs bash and jq",
)

FAKE_KUBECTL = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
rest = list(args)
if rest[:1] == ["--context"]:
    rest = rest[2:]
if rest[:1] == ["--namespace"]:
    rest = rest[2:]
verb = rest[0] if rest else ""
if verb == "version":
    print("Client Version: v1.31.0"); sys.exit(0)
if verb == "config":
    print("swarm-dev"); sys.exit(0)
if verb == "get":
    kind = rest[1]
    if kind == "namespace":
        print(json.dumps({"metadata": {"labels": {
            "swarm-tenant": "eng", "pod-security.kubernetes.io/enforce": "restricted"}}}))
    elif kind in ("limitrange", "resourcequota"):
        print(json.dumps({"items": []}))
    elif kind == "pod" and "--output" in rest and rest[rest.index("--output") + 1] == "json":
        print(json.dumps({"spec": {"runtimeClassName": "gvisor"}, "status": {"qosClass": "Guaranteed"}}))
    elif kind == "pod,pvc":
        pass  # nothing left
    else:
        print(kind + "/x")
    sys.exit(0)
if verb in ("create", "wait", "delete"):
    print("pod/x"); sys.exit(0)
if verb == "exec":
    cmd = rest[rest.index("--") + 1:]
    if cmd[:2] == ["sh", "-s"]:
        sys.stdin.read()
        print("SPIKE_PHASE name=fork_exec seconds=10 rc=0")
        print("SPIKE_WALL_SECONDS=120"); print("SPIKE_WORKLOAD_RC=0")
    elif cmd[:2] == ["sh", "-c"] and "docker run" in cmd[2]:
        print("recording-spike-ok")
    elif cmd[:2] == ["docker", "info"]:
        print("27.0.0 overlay2")
    else:
        print("4.4.0")
    sys.exit(0)
sys.exit(0)
'''


def _deny_list(root: Path) -> Path:
    """The example never-touch list, for this process's project, plus one
    tenant namespace. Private, as deny_list_load requires."""
    config = root / "config"
    env_dir = config / "dev"
    env_dir.mkdir(parents=True, mode=0o700)
    config.chmod(0o700)
    listed = json.loads((REPO / "scripts" / "lib" / "deny-list.example.json").read_text())
    listed["project"] = os.environ["PROJECT_ID"]
    listed["entries"].append(PEER_NAMESPACE)
    path = env_dir / "deny-list.json"
    path.write_text(json.dumps(listed))
    path.chmod(0o600)
    return config


def _run(tmp_path: Path, *args: str, typed: list[str] | None = None, **env_extra: str):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "kubectl"
    fake.write_text(FAKE_KUBECTL.replace("{python}", sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "kubectl.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_ASSUME_YES", "KUBECONFIG")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "SWARM_KUBECTL": str(fake),
        "FAKE_KUBECTL_LOG": str(log),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "SWARM_DEPLOY_CONFIG_DIR": str(_deny_list(tmp_path)),
        "XDG_CACHE_HOME": str(cache),
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        **env_extra,
    })
    cmd = ["bash", str(SCRIPT), *args, "--out", str(tmp_path / "result.md")]
    if typed is None:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=120, check=False)
        rc, stderr = proc.returncode, proc.stderr
    else:
        # A real terminal on stdin, so `[[ -t 0 ]]` holds and the answers are TYPED.
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            os.close(slave)
            os.write(master, "".join(line + "\n" for line in typed).encode())
            _, stderr = proc.communicate(timeout=120)
            rc = proc.returncode
        finally:
            os.close(master)
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return rc, stderr, calls


def _verb(call: list[str]) -> str:
    rest = call[2:] if call[:1] == ["--context"] else call
    rest = rest[2:] if rest[:1] == ["--namespace"] else rest
    return rest[0] if rest else ""


def _mutations(calls: list[list[str]]) -> list[list[str]]:
    """Every call that writes: a create that is not a dry run, and any delete."""
    return [c for c in calls
            if (_verb(c) == "create" and "--dry-run=server" not in c) or _verb(c) == "delete"]


APPLY = ("--apply", "--namespace", NAMESPACE, "--daemon-image", IMAGE,
         "--storage-class", STORAGE_CLASS, "--run-id", RUN_ID)


# --- the typed confirmation ---------------------------------------------------


def test_apply_without_a_terminal_creates_nothing(tmp_path):
    rc, stderr, calls = _run(tmp_path, *APPLY)
    assert rc != 0, stderr[-2000:]
    assert "interactive confirmation" in stderr
    # The control: the run got as far as the dry run, so it was the
    # confirmation, not an earlier refusal, that stopped it.
    assert any(_verb(c) == "create" and "--dry-run=server" in c for c in calls), calls
    assert _mutations(calls) == []


def test_swarm_assume_yes_does_not_replace_the_typed_confirmation(tmp_path):
    rc, stderr, calls = _run(tmp_path, *APPLY, SWARM_ASSUME_YES="1")
    assert rc != 0, stderr[-2000:]
    assert "ignoring SWARM_ASSUME_YES" in stderr
    assert _mutations(calls) == []


def test_a_wrong_answer_at_the_terminal_creates_nothing(tmp_path):
    rc, stderr, calls = _run(tmp_path, *APPLY, typed=["swarm-tenant-other"])
    assert rc != 0, stderr[-2000:]
    assert "confirmation did not match" in stderr
    assert _mutations(calls) == []


def test_preflight_writes_nothing(tmp_path):
    rc, stderr, calls = _run(tmp_path, "--namespace", NAMESPACE, "--daemon-image", IMAGE,
                             "--storage-class", STORAGE_CLASS)
    assert rc == 0, stderr[-2000:]
    assert _mutations(calls) == []
    assert "| 2. daemon | NOT RUN |" in (tmp_path / "result.md").read_text()


# --- the never-touch list -----------------------------------------------------


@pytest.mark.parametrize("namespace", [PEER_NAMESPACE, "swarm-tenant-peer-vpc"],
                         ids=["namespace-listed", "tenant-listed"])
def test_a_namespace_on_the_never_touch_list_is_refused_before_kubectl_runs(tmp_path, namespace):
    rc, stderr, calls = _run(tmp_path, "--apply", "--namespace", namespace, "--daemon-image", IMAGE,
                             "--storage-class", STORAGE_CLASS, typed=[namespace, RUN_ID])
    assert rc != 0, stderr[-2000:]
    assert "never-touch list" in stderr
    assert calls == []


@pytest.mark.parametrize("namespace", ["kube-system", "default", "swarm-system", "swarm-tenant-", "swarm-tenant-Eng"])
def test_a_namespace_that_is_not_a_tenant_namespace_is_refused(tmp_path, namespace):
    rc, stderr, calls = _run(tmp_path, "--cleanup", "--namespace", namespace, "--run-id", RUN_ID,
                             typed=[RUN_ID])
    assert rc != 0, stderr[-2000:]
    assert "not a tenant namespace" in stderr
    assert calls == []


# --- the delete: its own label, its namespace, nothing else -------------------


def _assert_delete_is_by_the_run_label(calls: list[list[str]]) -> None:
    deletes = [c for c in calls if _verb(c) == "delete"]
    assert len(deletes) == 1, deletes
    call = deletes[0]
    assert call[:4] == ["--context", "swarm-dev", "--namespace", NAMESPACE], call
    assert call[call.index("--selector") + 1] == SELECTOR, call
    for flag in ("--all", "--all-namespaces", "-A", "--field-selector", "--filename", "-f"):
        assert flag not in call, call
    # The only positional argument is the kinds; no object is named.
    positional = [a for a in call[5:] if not a.startswith("-") and a != SELECTOR]
    assert positional == ["pod,pvc"], call


def test_cleanup_deletes_only_by_its_own_label_after_the_run_id_is_typed(tmp_path):
    rc, stderr, calls = _run(tmp_path, "--cleanup", "--namespace", NAMESPACE, "--run-id", RUN_ID,
                             typed=[RUN_ID])
    assert rc == 0, stderr[-2000:]
    _assert_delete_is_by_the_run_label(calls)
    assert [c for c in _mutations(calls) if _verb(c) == "create"] == []
    assert "| cleanup: nothing left | PASS |" in (tmp_path / "result.md").read_text()


def test_cleanup_without_the_run_id_typed_deletes_nothing(tmp_path):
    rc, stderr, calls = _run(tmp_path, "--cleanup", "--namespace", NAMESPACE, "--run-id", RUN_ID,
                             typed=["rrs-someone-else"])
    assert rc != 0, stderr[-2000:]
    assert _mutations(calls) == []


def test_a_full_run_creates_after_the_namespace_and_deletes_only_its_own(tmp_path):
    rc, stderr, calls = _run(tmp_path, *APPLY, typed=[NAMESPACE, RUN_ID])
    assert rc == 0, stderr[-3000:]
    creates = [c for c in _mutations(calls) if _verb(c) == "create"]
    assert len(creates) == 2, creates
    assert all(c[:4] == ["--context", "swarm-dev", "--namespace", NAMESPACE] for c in creates), creates
    _assert_delete_is_by_the_run_label(calls)
    result = (tmp_path / "result.md").read_text()
    for check in ("1. admission (create)", "2. daemon", "3. speed", "cleanup: nothing left"):
        assert f"| {check} | PASS |" in result, result


def test_every_cluster_call_is_pinned_to_the_asserted_context(tmp_path):
    rc, stderr, calls = _run(tmp_path, "--namespace", NAMESPACE, "--daemon-image", IMAGE,
                             "--storage-class", STORAGE_CLASS)
    assert rc == 0, stderr[-2000:]
    # `config current-context` and `version` are how the context is read.
    cluster = [c for c in calls if c[:1] != ["config"] and c[:1] != ["version"]]
    assert cluster, calls
    for call in cluster:
        assert call[:2] == ["--context", "swarm-dev"], call
