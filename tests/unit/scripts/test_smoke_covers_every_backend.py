"""`scripts/smoke-test.sh` must exercise every execution backend, not one.

THE DEFECT THIS PINS, FIRST FORM. The smoke suite ran a single `mock` task and
the platform was called proven. `mock` resolves to CLOUD_RUN_JOB, and so do
`claude-code`, `codex` and `generic`; `browser` is the ONLY profile whose
resolved backend is GKE_AUTOPILOT -- a different API, a different permission and
a different authorisation model. So a green `make smoke` said nothing about half
the dispatch paths, and it said nothing for two days while GKE was totally
broken: seven browser tasks, seven 403s, `make smoke` green throughout.

SECOND FORM. The fix derived the matrix by importing `apps/common/swarm_common`
with python3. The image the suite runs in inside the VPC, images/swarm-verify,
is alpine with bash, curl and jq -- deliberately no python -- and carries only
scripts/ and the Makefile. So the first in-VPC run (release 36038727721,
execution swarm-verify-m9prt) printed "could not read the runner-profile
catalogue" and "the backend matrix visited 0 backends" in the same run whose
mock task SUCCEEDED.

The matrix now comes from the DEPLOYED API: GET /v1/runtimes, read with jq.
This file runs the suite's own jq program -- extracted from the script, not
copied -- against the production route's own output, so neither end can drift
from the other without a red test:

  * it must read `resolved_backend`, not `backend`. AUTO is a real member of
    the frozen enum; a row called AUTO is a backend with no API behind it, and
    the real backend that profile resolves to would go uncovered;
  * a backend whose profiles are ALL unavailable must still get a row (`-`),
    because the script fails on that row, and a backend that silently drops out
    of the matrix is one the suite then says nothing about while staying green;
  * the profile chosen for a backend must be one the verify tenant can run --
    `mock`, which needs no credential -- not the first name alphabetically,
    which is `claude-code` and needs an Anthropic key the tenant does not hold.

What the served catalogue cannot express, and the enum walk could: a backend
that NO profile references at all. The script says so where the matrix is
built; nothing can dispatch to such a backend (callers name a profile, never a
backend), so there is no path for the suite to be silent about.

The pool the suite reads to recognise a PAUSED backend is pinned here too:
`resource:<class>` is a restatement of `swarm_common.models.pool_names_for`,
and a restated value that drifts from its source is how this repository has
lost three deploys in two days.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_common.models import pool_names_for
from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / "scripts" / "smoke-test.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _matrix_program() -> str:
    """The jq program the suite runs over GET /v1/runtimes, verbatim."""
    match = re.search(r"^BACKEND_MATRIX_JQ='(.*?)'\n", SMOKE.read_text(), re.S | re.M)
    assert match, (
        "could not find BACKEND_MATRIX_JQ in scripts/smoke-test.sh. If the "
        "derivation moved, point this test at it -- do not let it pass by finding "
        "nothing, which is the failure mode the suite itself was fixed for."
    )
    return match.group(1)


def _served() -> dict:
    """What the deployed route answers, built by the route itself."""
    from swarm_api.routes.platform import runtimes

    return runtimes(auth=None)


def _matrix(served: dict, prefer: str = "mock") -> list[tuple[str, str, str]]:
    result = subprocess.run(
        ["jq", "-r", "--arg", "prefer", prefer, _matrix_program()],
        input=json.dumps(served),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    rows = [tuple(line.split()) for line in result.stdout.splitlines() if line.strip()]
    assert rows, "the matrix is empty; an empty matrix reads as a clean run"
    for row in rows:
        assert len(row) == 3, f"a matrix row must be BACKEND PROFILE CLASS, got {row!r}"
    return rows  # type: ignore[return-value]


def _refuses(served: dict) -> str:
    """The program must fail, not print an empty matrix."""
    result = subprocess.run(
        ["jq", "-r", "--arg", "prefer", "mock", _matrix_program()],
        input=json.dumps(served),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode != 0, (
        f"the matrix program accepted {served!r} and printed {result.stdout!r}; "
        "a catalogue it cannot read must be an error, never a quiet empty matrix"
    )
    return result.stderr


def test_the_suite_no_longer_needs_python():
    """The verify image has none. Any python in the suite is a row that cannot run."""
    code = [
        line for line in SMOKE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    offenders = [line for line in code if re.search(r"\bpython3?\b|PYTHON_BIN|swarm_common", line)]
    assert not offenders, (
        "scripts/smoke-test.sh runs in images/swarm-verify, which carries no python "
        "and no apps/ directory:\n" + "\n".join(offenders)
    )


def test_the_matrix_is_read_from_the_deployed_api():
    """GET /v1/runtimes through api_fetch, redirected -- not inside $(...)."""
    text = SMOKE.read_text()
    assert re.search(r'api_fetch "/runtimes" "\$\{RUNTIMES_FILE\}"', text), (
        "the suite does not read GET /v1/runtimes through api_fetch into a file"
    )
    assert not re.search(r'\$\(\s*api_(get|fetch)[^)]*runtimes', text), (
        "GET /v1/runtimes is read inside a command substitution, which throws away "
        "the API_STATUS the failure message reports (CLAUDE.md)"
    )


def test_gke_autopilot_is_covered_by_browser_and_cloud_run_by_mock():
    """The row that was missing for two days, and the row the verify tenant can run."""
    rows = {backend: (profile, rc) for backend, profile, rc in _matrix(_served())}
    assert rows.get("GKE_AUTOPILOT", (None,))[0] == "browser", (
        f"GKE_AUTOPILOT is covered by {rows.get('GKE_AUTOPILOT')!r}; the smoke suite "
        "would not submit a browser task, which is the only kind that failed"
    )
    assert rows.get("CLOUD_RUN_JOB", (None,))[0] == "mock", (
        f"CLOUD_RUN_JOB is covered by {rows.get('CLOUD_RUN_JOB')!r}. It must be mock: "
        "the verify tenant holds no provider credential, so a claude-code row fails at "
        "the runner with dispatch working perfectly"
    )
    # The resource class is what the pause check reads; it must be the profile's.
    for backend, (profile, rc) in rows.items():
        assert rc == RUNNER_PROFILES[profile].resource_class, (backend, profile, rc)


def test_every_backend_a_profile_resolves_to_gets_a_row():
    """Including one whose every profile is disabled: the script FAILS on `-`."""
    served = _served()
    rows = _matrix(served)
    expected = {resolve_backend(p).value for p in RUNNER_PROFILES.values()}
    assert {backend for backend, _, _ in rows} == expected
    assert "AUTO" not in {backend for backend, _, _ in rows}

    # THE MUTATION THIS CATCHES: a program that keeps only the backends it found
    # an AVAILABLE profile for. Disable browser and GKE_AUTOPILOT must still be
    # listed -- with `-` -- rather than simply stop being mentioned.
    disabled = copy.deepcopy(served)
    disabled["runtimes"]["browser"]["available"] = False
    rows = {backend: (profile, rc) for backend, profile, rc in _matrix(disabled)}
    assert rows["GKE_AUTOPILOT"] == ("-", "-"), rows


def test_a_profile_is_available_only_when_the_api_says_true():
    """`.available // true` would read a missing or false flag as available."""
    served = copy.deepcopy(_served())
    for entry in served["runtimes"].values():
        if entry["resolved_backend"] == "GKE_AUTOPILOT":
            entry.pop("available")
    rows = {backend: profile for backend, profile, _ in _matrix(served)}
    assert rows["GKE_AUTOPILOT"] == "-", (
        "a profile whose availability the API did not state was treated as available"
    )


def test_the_matrix_resolves_auto_rather_than_reading_the_raw_field():
    """A profile declared AUTO is filed under the backend it RESOLVES to.

    No profile in the frozen catalogue is AUTO today, so this is the served
    shape of one that would be: `backend: AUTO`, `resolved_backend` whatever
    `resolve_backend` decided. Reading `backend` would make a row called AUTO.
    """
    served = copy.deepcopy(_served())
    served["runtimes"]["browser"]["backend"] = Backend.AUTO.value
    rows = {backend: profile for backend, profile, _ in _matrix(served)}
    assert "AUTO" not in rows
    assert rows["GKE_AUTOPILOT"] == "browser"


def test_the_suites_own_profile_is_preferred_then_a_provider_less_one():
    served = _served()
    rows = {backend: profile for backend, profile, _ in _matrix(served, prefer="generic")}
    assert rows["CLOUD_RUN_JOB"] == "generic"
    # With no preference that resolves there, a profile needing no credential
    # wins over one that does, whatever the names.
    rows = {backend: profile for backend, profile, _ in _matrix(served, prefer="nothing")}
    chosen = RUNNER_PROFILES[rows["CLOUD_RUN_JOB"]]
    assert chosen.provider is None and chosen.available, rows


@pytest.mark.parametrize(
    "served",
    [
        {},
        {"runtimes": {}},
        {"runtimes": []},
        {"detail": "not found"},
    ],
    ids=["empty-object", "no-profiles", "wrong-type", "error-body"],
)
def test_an_unreadable_catalogue_is_an_error_not_an_empty_matrix(served):
    _refuses(served)


def test_a_profile_with_no_resolved_backend_is_an_error():
    served = copy.deepcopy(_served())
    served["runtimes"]["browser"].pop("resolved_backend")
    assert "resolved backend" in _refuses(served)


def test_every_profile_the_matrix_names_is_in_the_frozen_catalogue():
    """A row naming a profile that does not exist submits nothing useful."""
    for backend, profile_name, _ in _matrix(_served()):
        if profile_name == "-":
            continue
        assert profile_name in RUNNER_PROFILES, f"{backend}: no such profile {profile_name}"
        profile = RUNNER_PROFILES[profile_name]
        assert profile.available, f"{backend}: {profile_name} is not available"
        assert resolve_backend(profile).value == backend


def test_the_paused_pool_the_suite_reads_is_the_one_admission_acquires():
    """`resource:<class>` in the shell must be what pool_names_for calls it.

    Read the spelling out of the script's `resource_pool_for`, run it, and
    compare it with the frozen function for every profile. A suite reading
    `resources:browser` would find no document, read the pool as open, and
    submit straight into the pause it was meant to recognise.
    """
    match = re.search(r"^resource_pool_for\(\) \{ printf '([^']*)' \"\$1\"; \}$",
                      SMOKE.read_text(), re.M)
    assert match, "could not find resource_pool_for in scripts/smoke-test.sh"
    fmt = match.group(1)
    for name, profile in RUNNER_PROFILES.items():
        shell = subprocess.run(
            ["bash", "-c", f"printf '{fmt}' \"$1\"", "pool", profile.resource_class],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout
        pools = pool_names_for(
            tenant_id="t", provider=profile.provider,
            resource_class=profile.resource_class, runner_profile=name,
            backend=resolve_backend(profile).value,
        )
        assert shell in pools, f"{name}: the suite reads pools/{shell}; admission acquires {pools}"
        assert shell.startswith("resource:"), shell
