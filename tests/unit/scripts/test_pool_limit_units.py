"""`scripts/pool-limit.sh` warns when a limit shuts out every task of a class (part of #66).

On 2026-09-24 `pools/resource:browser` sat at `hard_limit 1`. A browser task
needed 2 units then, so admission refused it on every drain for twenty minutes
while the screen called the class busy. The script that set the limit printed
`ok resource:browser hard_limit=1` and nothing else.

Contract request 79 (2026-10-14) made a browser task 1 unit, so `browser` can
no longer be shut out this way, and no profile in the catalogue reaches a class
above 1 unit. The warning is still the script's job for `large` (4 units), so
these tests judge `resource:large`, and the `global` case serves one synthetic
profile on `large` beside the real catalogue.

A limit above 0 and below the units of the largest task that can reach a pool
now draws a warning, from `--pool` and from `--sync`. It is a warning and not a
refusal: shutting a class out on purpose is a legitimate choice (#66 says so).
A limit of 0 is not warned about -- it shuts out everything, which is its
purpose, and the UI already says so.

The units come from terraform's `resource_classes` and `runner_profiles`
outputs, the mirror tests/terraform/catalogue.tftest.hcl holds to
swarm_common.profiles. The fake terraform here serves those outputs built from
swarm_common itself, so the expectations below are the contract's numbers.

Driven end to end with a fake terraform, curl and gcloud on PATH. Nothing is
reached: the fake curl IS Firestore.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "pool-limit.sh"
PROJECT = "swarm-test-project"
NEVER = "can never be admitted"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq and bash are required",
)


def _value(v):
    return getattr(v, "value", v)


CLASSES = {
    name: {"cpu": rc.cpu, "memory_gib": rc.memory_gib, "disk_gib": rc.disk_gib, "units": rc.units}
    for name, rc in RESOURCE_CLASSES.items()
}
PROFILES = {
    name: {
        "image": p.image,
        "resource_class": p.resource_class,
        "backend": _value(resolve_backend(p)),
        "provider": _value(p.provider),
    }
    for name, p in RUNNER_PROFILES.items()
}
LARGEST_REACHABLE = max(CLASSES[p["resource_class"]]["units"] for p in PROFILES.values())
#: The heaviest class in the catalogue: `large`, 4 units.
HEAVY = max(CLASSES, key=lambda name: CLASSES[name]["units"])
HEAVY_UNITS = CLASSES[HEAVY]["units"]
HEAVY_POOL = f"resource:{HEAVY}"
#: A profile on that class, served only where a test says so. Until contract
#: request 113 (2026-10-22, claude-code-large) no real profile reached it.
HEAVY_PROFILE = {"image": "agent-runtime-base", "resource_class": HEAVY,
                 "backend": "GKE_AUTOPILOT", "provider": None}

FAKE_GCLOUD = """#!/usr/bin/env bash
echo fake-access-token
"""

FAKE_TERRAFORM = """#!/usr/bin/env bash
case "$*" in
  *"output -json pool_limits"*)       cat "${FAKE_TF_DIR}/pool_limits.json" ;;
  *"output -json resource_classes"*)
    [[ -z "${FAKE_TF_NO_CATALOGUE:-}" ]] || { echo "Error: output not found" >&2; exit 1; }
    cat "${FAKE_TF_DIR}/resource_classes.json" ;;
  *"output -json runner_profiles"*)
    [[ -z "${FAKE_TF_NO_CATALOGUE:-}" ]] || { echo "Error: output not found" >&2; exit 1; }
    cat "${FAKE_TF_DIR}/runner_profiles.json" ;;
  *) echo "unexpected terraform invocation: $*" >&2; exit 9 ;;
esac
"""

#: fs_request's shape: -K - [-X M] [--data-binary B] -o FILE -w '%{http_code}' URL
FAKE_CURL = r"""#!/usr/bin/env bash
set -uo pipefail
out=""; method="GET"; url=""; prev=""; from_stdin=0
for arg in "$@"; do
  case "${prev}" in
    -o) out="${arg}" ;;
    -X) method="${arg}" ;;
    -K) from_stdin=1 ;;
  esac
  case "${arg}" in http*) url="${arg}" ;; esac
  prev="${arg}"
done
[[ "${from_stdin}" -eq 1 ]] && cat >/dev/null 2>&1
printf '%s %s\n' "${method}" "${url}" >>"${FAKE_CURL_LOG}"
case "${method} ${url}" in
  "GET "*"/documents/pools?"*) body="$(cat "${FAKE_POOLS}")"; status=200 ;;
  "PATCH "*"/documents/pools/"*) body='{}'; status=200 ;;
  *) body='{"error":{"code":404,"status":"NOT_FOUND","message":"not faked"}}'; status=404 ;;
esac
if [[ -n "${out}" ]]; then printf '%s' "${body}" >"${out}"; else printf '%s' "${body}"; fi
printf '%s' "${status}"
"""


def _pool_doc(name: str, hard_limit: int) -> dict:
    return {
        "name": f"projects/{PROJECT}/databases/swarm/documents/pools/{name}",
        "fields": {
            "name": {"stringValue": name},
            "hard_limit": {"integerValue": str(hard_limit)},
            "active": {"integerValue": "0"},
            "enabled": {"booleanValue": True},
        },
    }


@pytest.fixture
def harness(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM), ("curl", FAKE_CURL)):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(0o755)
    tf_dir = tmp_path / "tf"
    tf_dir.mkdir()
    (tf_dir / "resource_classes.json").write_text(json.dumps(CLASSES))
    (tf_dir / "runner_profiles.json").write_text(json.dumps(PROFILES))

    live = {"global": 40, "resource:browser": 10, "resource:standard": 10,
            HEAVY_POOL: 10, "runner:mock": 10,
            "provider:anthropic": 10, "provider:anthropic:tenant:eng": 10}
    (tmp_path / "pools.json").write_text(
        json.dumps({"documents": [_pool_doc(n, v) for n, v in live.items()]})
    )
    (tf_dir / "pool_limits.json").write_text(json.dumps(live))

    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nREGION=us-central1\nENVIRONMENT=dev\nFIRESTORE_DATABASE=swarm\n")
    env_file.chmod(0o600)

    env = {k: v for k, v in os.environ.items() if k not in ("K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA")}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        SWARM_TERRAFORM=str(bin_dir / "terraform"),
        SWARM_ENV_FILE=str(env_file),
        FAKE_TF_DIR=str(tf_dir),
        FAKE_POOLS=str(tmp_path / "pools.json"),
        FAKE_CURL_LOG=str(tmp_path / "curl.log"),
        NO_COLOR="1",
    )

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(SCRIPT), *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )

    return {"run": run, "log": tmp_path / "curl.log", "tf_dir": tf_dir}


def _patched(harness, pool: str) -> bool:
    log = harness["log"].read_text() if harness["log"].exists() else ""
    return any(line.startswith("PATCH ") and f"/documents/pools/{pool}?" in line for line in log.splitlines())


def test_a_limit_below_the_class_units_warns_and_still_writes(harness) -> None:
    assert HEAVY_UNITS > 1, "the catalogue has no class above one unit; this case proves nothing"
    proc = harness["run"]("--pool", HEAVY_POOL, "--limit", str(HEAVY_UNITS - 1))
    assert proc.returncode == 0, proc.stderr
    assert NEVER in proc.stderr, proc.stderr
    assert HEAVY_POOL in proc.stderr
    assert f"{HEAVY_UNITS} units" in proc.stderr, proc.stderr
    # A warning, not a refusal: the value the operator asked for is written.
    assert _patched(harness, HEAVY_POOL), harness["log"].read_text()


def test_a_limit_that_admits_one_task_is_silent(harness) -> None:
    proc = harness["run"]("--pool", HEAVY_POOL, "--limit", str(HEAVY_UNITS))
    assert proc.returncode == 0, proc.stderr
    assert NEVER not in proc.stderr, proc.stderr
    assert _patched(harness, HEAVY_POOL)


def test_a_browser_limit_of_one_admits_a_browser_task(harness) -> None:
    """#66's own limit: since contract request 79 it admits one browser agent."""
    assert CLASSES["browser"]["units"] == 1
    proc = harness["run"]("--pool", "resource:browser", "--limit", "1")
    assert proc.returncode == 0, proc.stderr
    assert NEVER not in proc.stderr, proc.stderr
    assert _patched(harness, "resource:browser")


def test_zero_is_a_deliberate_stop_and_is_not_warned(harness) -> None:
    proc = harness["run"]("--pool", "resource:browser", "--limit", "0")
    assert proc.returncode == 0, proc.stderr
    assert NEVER not in proc.stderr, proc.stderr


def test_global_is_judged_against_the_largest_class_any_profile_reaches(harness) -> None:
    # The real catalogue: since contract request 113 (owner, 2026-10-22)
    # claude-code-large reaches `large`, 4 units, so a global limit of 3 shuts
    # it out and 4 does not.
    assert LARGEST_REACHABLE == HEAVY_UNITS == 4
    shut = harness["run"]("--pool", "global", "--limit", str(HEAVY_UNITS - 1))
    assert NEVER in shut.stderr, shut.stderr
    quiet = harness["run"]("--pool", "global", "--limit", str(HEAVY_UNITS))
    assert NEVER not in quiet.stderr, quiet.stderr

    # One profile on the heaviest class, and the same limit now warns.
    (harness["tf_dir"] / "runner_profiles.json").write_text(
        json.dumps({**PROFILES, "heavy-test": HEAVY_PROFILE}))
    proc = harness["run"]("--pool", "global", "--limit", str(HEAVY_UNITS - 1))
    assert proc.returncode == 0, proc.stderr
    assert NEVER in proc.stderr, proc.stderr
    assert f"{HEAVY_UNITS} units" in proc.stderr, proc.stderr

    quiet = harness["run"]("--pool", "global", "--limit", str(HEAVY_UNITS))
    assert NEVER not in quiet.stderr, quiet.stderr


def test_a_runner_pool_is_judged_by_its_own_profiles_class(harness) -> None:
    mock_units = CLASSES[PROFILES["mock"]["resource_class"]]["units"]
    proc = harness["run"]("--pool", "runner:mock", "--limit", str(mock_units))
    assert proc.returncode == 0, proc.stderr
    assert NEVER not in proc.stderr, (
        "runner:mock only ever carries mock tasks; judging it by the largest class "
        "in the catalogue warns about a limit that admits every task it can see\n" + proc.stderr
    )


def test_sync_warns_about_the_values_it_copies(harness) -> None:
    limits = json.loads((harness["tf_dir"] / "pool_limits.json").read_text())
    limits[HEAVY_POOL] = HEAVY_UNITS - 1
    (harness["tf_dir"] / "pool_limits.json").write_text(json.dumps(limits))
    proc = harness["run"]("--sync", SWARM_ASSUME_YES="1")
    assert proc.returncode == 0, proc.stderr
    assert NEVER in proc.stderr, proc.stderr
    assert HEAVY_POOL in proc.stderr
    assert _patched(harness, HEAVY_POOL), harness["log"].read_text()


def test_an_unreadable_catalogue_is_said_rather_than_read_as_fine(harness) -> None:
    proc = harness["run"]("--pool", "resource:browser", "--limit", "1", FAKE_TF_NO_CATALOGUE="1")
    assert proc.returncode == 0, proc.stderr
    assert "not checked" in proc.stderr, (
        "a catalogue that could not be read must be named, not taken as 'no warning'\n" + proc.stderr
    )


def test_a_provider_pool_is_judged_at_one_unit_whatever_the_class(harness) -> None:
    """#2950: a large agent takes 1 unit in a provider pool (contract request 128).

    A provider limit of 1 admits one claude-code-large agent, so it is not
    "never admits"; under the old rule it warned at the class's 4 units.
    """
    assert any(p.get("provider") == "anthropic" and CLASSES[p["resource_class"]]["units"] == HEAVY_UNITS
               for p in PROFILES.values()), "no large anthropic profile; this case proves nothing"
    for pool in ("provider:anthropic", "provider:anthropic:tenant:eng"):
        proc = harness["run"]("--pool", pool, "--limit", "1")
        assert proc.returncode == 0, proc.stderr
        assert NEVER not in proc.stderr, proc.stderr
        assert _patched(harness, pool)
