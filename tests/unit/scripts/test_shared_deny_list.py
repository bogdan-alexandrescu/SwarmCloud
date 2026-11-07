"""The never-touch list comes from the deployment's config object and fails closed.

`SHARED_DENY_LIST` used to be a literal array of the shared project's
other-team resources in scripts/lib/common.sh. Those are deployment values, so
the list moved to `gs://<state-bucket>/config/<env>/deny-list.json`
(docs/design/generic-repository.md, section 2.4), read by `deny_list_load`.
The literal was kept for years because "an environment variable an operator
forgets would silently empty the live guard"; the object keeps that property:

1. no object, an unreadable one or a malformed one: every guard refuses;
2. an object written for another project: refuse;
3. fewer entries of a kind than the object's own floor: refuse;
4. empty only when written so (`"entries": []`, every floor zero, reviewed);
5. `guard_deny_json` prints `["(default)"]` for that empty list, never `[""]`;
6. with no object, `kube_context_allowed` refuses a context naming the shared
   cluster, even with GKE_CLUSTER pointed at it.

Offline: sources common.sh with SWARM_DEPLOY_CONFIG_DIR, the local stand-in for the
bucket prefix, and every name from the placeholder set. No cloud call.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

# It proves what common.sh does with no list, or with its own: none is served for it.
pytestmark = pytest.mark.no_placeholder_deployment

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
EXAMPLE = REPO / "scripts" / "lib" / "deny-list.example.json"
PROJECT = "example-project"
SHARED_SA = "peer-runner@example-shared-project.iam.gserviceaccount.com"
REFUSAL = "is not readable; nothing that mutates runs without it"


def _example() -> dict:
    return json.loads(EXAMPLE.read_text())


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PROJECT_ID", "SWARM_ENV_FILE", "KUBECONFIG", "GKE_CLUSTER", "SWARM_DEPLOY_CONFIG_DIR")
    }
    env.update(
        PROJECT_ID=PROJECT, REGION="us-central1", ZONE="us-central1-a", ENVIRONMENT="dev",
        SWARM_ENV_FILE=str(tmp_path / "no-env-file"),
        SWARM_DEPLOY_CONFIG_DIR=str(tmp_path / "config"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        HOME=str(tmp_path),
    )
    env.update(extra)
    return env


def _write(tmp_path: Path, body: object, *, raw: str | None = None) -> None:
    folder = tmp_path / "config" / "dev"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / "deny-list.json"
    target.write_text(raw if raw is not None else json.dumps(body))
    target.chmod(0o600)


def _run(tmp_path: Path, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=False, env=_env(tmp_path, **extra),
    )


def _refused(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode != 0, proc.stdout
    assert "reached" not in proc.stdout, "a guard let the caller carry on without the list"
    assert REFUSAL in proc.stderr, proc.stderr


GUARDS = [
    "is_shared_resource swarm-api",
    "kube_context_allowed swarm-dev",
    "guard_deny_json >/dev/null",
    'x="$(guard_deny_json)"',
]


@pytest.mark.parametrize("guard", GUARDS)
def test_no_object_refuses_every_guard(tmp_path: Path, guard: str) -> None:
    # MUTATION: treat a missing object as an empty list in deny_list_load and
    # this goes green-for-the-wrong-reason: "reached" is printed.
    (tmp_path / "config" / "dev").mkdir(parents=True, mode=0o700)
    proc = _run(tmp_path, f"{guard}; echo reached")
    _refused(proc)
    assert "deny-list.json does not exist" in proc.stderr


def test_no_config_prefix_at_all_refuses(tmp_path: Path) -> None:
    _refused(_run(tmp_path, "is_shared_resource swarm-api; echo reached"))


def test_no_object_refuses_the_shared_cluster_context(tmp_path: Path) -> None:
    # The red-first case of the design: with no object, a context naming the
    # shared cluster is refused even when GKE_CLUSTER was pointed at it, which
    # the old kube_context_allowed accepted by name alone.
    (tmp_path / "config" / "dev").mkdir(parents=True, mode=0o700)
    ctx = f"gke_{PROJECT}_us-central1_peer-cluster"
    proc = _run(tmp_path, f'if kube_context_allowed "{ctx}"; then echo allowed; fi; echo reached',
                GKE_CLUSTER="peer-cluster", GKE_LOCATION="us-central1")
    assert "allowed" not in proc.stdout
    _refused(proc)


def test_a_listed_cluster_is_refused_as_a_context(tmp_path: Path) -> None:
    _write(tmp_path, _example())
    ctx = f"gke_{PROJECT}_us-central1_peer-cluster"
    proc = _run(tmp_path, f'kube_context_allowed "{ctx}" && echo allowed || echo refused',
                GKE_CLUSTER="peer-cluster", GKE_LOCATION="us-central1")
    assert proc.stdout.strip() == "refused", proc.stderr
    ours = _run(tmp_path, f'kube_context_allowed "gke_{PROJECT}_us-central1_swarm-autopilot" && echo allowed',
                GKE_CLUSTER="swarm-autopilot", GKE_LOCATION="us-central1")
    assert ours.stdout.strip() == "allowed", ours.stderr


@pytest.mark.parametrize("raw", ["{not json", "[]", '"a string"', ""])
def test_a_malformed_object_refuses(tmp_path: Path, raw: str) -> None:
    _write(tmp_path, None, raw=raw)
    _refused(_run(tmp_path, "is_shared_resource swarm-api; echo reached"))


def test_an_unreadable_object_refuses(tmp_path: Path) -> None:
    _write(tmp_path, _example())
    target = tmp_path / "config" / "dev" / "deny-list.json"
    target.chmod(0o000)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("running as a user who reads mode-000 files")
        _refused(_run(tmp_path, "is_shared_resource swarm-api; echo reached"))
    finally:
        target.chmod(0o600)


def test_an_object_for_another_project_refuses(tmp_path: Path) -> None:
    # A list copied from another deployment does not protect this one.
    _write(tmp_path, {**_example(), "project": "example-shared-project"})
    proc = _run(tmp_path, "is_shared_resource swarm-api; echo reached")
    _refused(proc)
    assert "written for project example-shared-project, not example-project" in proc.stderr


@pytest.mark.parametrize("drop", ["peer-cluster", SHARED_SA])
def test_an_object_below_its_floor_refuses(tmp_path: Path, drop: str) -> None:
    # A truncated list fails the way verify-destroy-guard.sh's inventory check
    # did: the floors are asserted independently of the entries.
    body = _example()
    body["entries"] = [e for e in body["entries"] if e != drop]
    _write(tmp_path, body)
    proc = _run(tmp_path, "is_shared_resource swarm-api; echo reached")
    _refused(proc)
    assert "fewer entries than its floors" in proc.stderr


@pytest.mark.parametrize(
    "change",
    [
        {"floors": None},
        {"floors": {"total": -1}},
        {"floors": {"total": 1.5}},
        {"entries": "peer-cluster"},
        {"entries": ["peer-cluster", ""]},
        {"entries": ["peer-cluster\nswarm-api"]},
        {"reviewed_by": ""},
        {"reviewed_at": None},
        {"classify": [{"glob": "peer-*"}]},
    ],
)
def test_a_badly_shaped_object_refuses(tmp_path: Path, change: dict) -> None:
    body = {**_example(), **change}
    body = {k: v for k, v in body.items() if v is not None}
    _write(tmp_path, body)
    _refused(_run(tmp_path, "is_shared_resource swarm-api; echo reached"))


def test_an_empty_list_without_floors_refuses(tmp_path: Path) -> None:
    # Empty is a statement, never the absence of one.
    body = {k: v for k, v in _example().items() if k != "floors"}
    body["entries"] = []
    _write(tmp_path, body)
    _refused(_run(tmp_path, "is_shared_resource swarm-api; echo reached"))


def test_an_empty_list_with_a_nonzero_floor_refuses(tmp_path: Path) -> None:
    _write(tmp_path, {**_example(), "entries": []})
    _refused(_run(tmp_path, "is_shared_resource swarm-api; echo reached"))


def test_a_list_written_empty_allows(tmp_path: Path) -> None:
    # A project that shares nothing: entries [] with every floor zero, reviewed.
    _write(tmp_path, {**_example(), "entries": [], "classify": [],
                      "floors": {"total": 0, "cluster": 0, "sa": 0}})
    proc = _run(
        tmp_path,
        'deny_list_load && echo loaded; is_shared_resource peer-cluster || echo not-shared; '
        'printf "guard=%s\\n" "$(guard_deny_json)"; '
        'kube_context_allowed "gke_${PROJECT_ID}_us-central1_swarm-autopilot" && echo context-ok',
        GKE_CLUSTER="swarm-autopilot", GKE_LOCATION="us-central1",
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines == ["loaded", "not-shared", 'guard=["(default)"]', "context-ok"], proc.stdout


def test_the_example_object_loads_and_guards(tmp_path: Path) -> None:
    _write(tmp_path, _example())
    names = ["peer-cluster", "peer-vpc", "peer-subnet", "peer-bucket", SHARED_SA]
    script = "; ".join(f'is_shared_resource "{n}" && echo "{n}=shared"' for n in names)
    proc = _run(tmp_path, script + '; is_shared_resource swarm-api || echo "swarm-api=ours"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [f"{n}=shared" for n in names] + ["swarm-api=ours"]


def test_guard_deny_json_is_the_list_less_default_plus_firestore(tmp_path: Path) -> None:
    body = _example()
    body["entries"] = [*body["entries"], "default"]
    _write(tmp_path, body)
    proc = _run(tmp_path, "guard_deny_json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [*_example()["entries"], "(default)"]


def test_a_failed_list_inside_a_substitution_stops_the_caller(tmp_path: Path) -> None:
    # destroy.sh and the plan guard read it as DENY_JSON="$(guard_deny_json)":
    # under `set -e` that line must end the script, not leave DENY_JSON empty.
    (tmp_path / "config" / "dev").mkdir(parents=True, mode=0o700)
    proc = _run(tmp_path, 'DENY_JSON="$(guard_deny_json)"; echo "reached [${DENY_JSON}]"')
    _refused(proc)


def test_kinds_and_floors_come_from_the_object(tmp_path: Path) -> None:
    body = _example()
    body["entries"] = [*body["entries"], "peer-web"]
    body["classify"] = [*body["classify"], {"glob": "peer-web", "kind": "run-service"}]
    body["floors"] = {**body["floors"], "run-service": 1}
    _write(tmp_path, body)
    proc = _run(tmp_path, 'deny_list_floors; shared_resource_kind peer-web; echo; shared_resource_kind peer-vpc')
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "run-service\t1" in lines
    assert lines[-2:] == ["run-service", "network"]


def test_a_group_writable_object_is_refused(tmp_path: Path) -> None:
    # The source is held to load_env's rule for .env: another user who can
    # write it can empty the list.
    _write(tmp_path, _example())
    (tmp_path / "config" / "dev" / "deny-list.json").chmod(0o620)
    proc = _run(tmp_path, "is_shared_resource swarm-api; echo reached")
    _refused(proc)
    assert "group- or world-writable" in proc.stderr


def test_no_project_refuses(tmp_path: Path) -> None:
    # SWARM_NO_PROJECT=1 runs with PROJECT_ID empty: no list can be read for
    # it, so a guard it reaches refuses rather than judging by nothing.
    proc = _run(tmp_path, "is_shared_resource swarm-api; echo reached",
                PROJECT_ID="", SWARM_NO_PROJECT="1")
    _refused(proc)
    assert "no project is named" in proc.stderr


def test_no_deployment_value_is_written_in_common_sh() -> None:
    text = COMMON.read_text()
    assert "SHARED_DENY_LIST=(\n" not in text, "the literal list is back in common.sh"
    assert "SHARED_PROJECT_ID=" not in text
    assert "gserviceaccount.com\"\n" not in text


WALK = 'for e in ${SHARED_DENY_LIST[@]+"${SHARED_DENY_LIST[@]}"}; do :; done; echo "reached ${#SHARED_DENY_LIST[@]}"'


def test_sourcing_with_a_project_and_no_object_stops_before_the_script(tmp_path: Path) -> None:
    # destroy.sh and the roll-*.sh pre-checks walk SHARED_DENY_LIST themselves.
    # MUTATION: load the list only inside the guards and this walks an empty
    # array: "reached 0" is printed.
    (tmp_path / "config" / "dev").mkdir(parents=True, mode=0o700)
    _refused(_run(tmp_path, WALK))


def test_a_script_walking_the_list_itself_sees_every_entry(tmp_path: Path) -> None:
    # Loaded in the sourcing shell, so a guard first reached inside $(...) is
    # not the only shell that holds it.
    _write(tmp_path, _example())
    proc = _run(tmp_path, f'x="$(guard_deny_json)"; {WALK}')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"reached {len(_example()['entries'])}"


def test_resolve_defers_the_list_so_the_makefile_parses_offline(tmp_path: Path) -> None:
    # The Makefile resolves PROJECT_ID through resolve.sh when it is parsed;
    # `make help` and `make test` must not need the bucket.
    proc = subprocess.run(
        [str(REPO / "scripts" / "lib" / "resolve.sh"), "env", "PROJECT_ID"],
        capture_output=True, text=True, timeout=60, check=False, env=_env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == PROJECT


def test_only_the_real_resolve_sh_defers(tmp_path: Path) -> None:
    # Told apart by the sourcing file's path, so a file merely NAMED resolve.sh
    # elsewhere still loads the list, and nothing in the environment can.
    (tmp_path / "config" / "dev").mkdir(parents=True, mode=0o700)
    fake = tmp_path / "scripts" / "lib" / "resolve.sh"
    fake.parent.mkdir(parents=True)
    fake.write_text(f'#!/usr/bin/env bash\nset -euo pipefail\nsource "{COMMON}"\n{WALK}\n')
    fake.chmod(0o755)
    proc = subprocess.run([str(fake)], capture_output=True, text=True, timeout=60, check=False,
                          env=_env(tmp_path, _deny_list_deferred="1"))
    _refused(proc)


def test_the_workspace_guard_refusal_reason_is_a_fixed_sentence(tmp_path: Path) -> None:
    # The reason lands in the job's needs_owner stop record and its build log,
    # so it is one fixed sentence: the object's own text (here the `.project` its
    # writer chose) and the bucket path stay on stderr, never in the record.
    # MUTATION: interpolate DENY_LIST_ERROR into GUARD_REASON and this fails.
    chosen = "writer-chosen-text"
    _write(tmp_path, {**_example(), "project": chosen})
    expect = tmp_path / "expect.json"
    expect.write_text(json.dumps({"workspace_id": "w-0a1b2c"}))
    expect.chmod(0o600)
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "lib" / "workspace-guard.sh"), "check", "--", "gcloud", "projects", "list"],
        capture_output=True, text=True, timeout=60, check=False,
        # SWARM_NO_PROJECT defers the load from the sourcing to the judge, which
        # is the path this refusal is on.
        env=_env(tmp_path, SWARM_NO_PROJECT="1", SWARM_CALL_GUARD=str(expect)),
    )
    assert proc.returncode == 86, proc.stderr
    stop = json.loads((tmp_path / "expect.json.stop").read_text())
    assert stop["reason"] == "the never-touch list is not readable, and nothing runs without it", stop
    assert chosen in proc.stderr, proc.stderr
