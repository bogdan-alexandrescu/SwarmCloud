"""`scripts/smoke-test.sh` must exercise every execution backend, not one.

THE DEFECT THIS PINS. The smoke suite ran a single `mock` task and the platform
was called proven. `mock` resolves to CLOUD_RUN_JOB, and so do `claude-code`,
`codex` and `generic`; `browser` is the ONLY profile whose resolved backend is
GKE_AUTOPILOT -- a different API, a different permission and a different
authorisation model. So a green `make smoke` said nothing whatsoever about half
the dispatch paths, and it said nothing for two days while GKE was totally
broken: seven browser tasks, seven 403s, `make smoke` green throughout.

The suite now derives one profile PER BACKEND from the frozen catalogue. This
file asserts the derivation rather than the shell around it, because the
derivation is the part that decides whether a backend is tested at all:

  * it must use `resolve_backend`, not `profile.backend`. AUTO is a real member
    of the frozen enum, so reading the raw field would put a row called `AUTO`
    in the matrix -- a backend with no API behind it -- while the real backend
    that profile resolves to went uncovered;
  * it must emit a row for every concrete backend even when no profile reaches
    one, because a backend that silently drops out of the matrix is a backend
    the suite then reports nothing about while staying green.

The probe is EXTRACTED FROM THE SCRIPT and run, in the same spirit as
`test_smoke_tenant_parse.py`: asserting against a copy of the logic would prove
only that the copy works.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / "scripts" / "smoke-test.sh"


def _probe_source() -> str:
    """The python heredoc inside `backends_to_cover`, verbatim."""
    text = SMOKE.read_text()
    match = re.search(
        r"backends_to_cover\(\) \{.*?<<'PY'\n(.*?)\nPY\n\}", text, re.S
    )
    assert match, (
        "could not find the backends_to_cover probe in scripts/smoke-test.sh. "
        "If it moved, point this test at it -- do not let it pass by finding "
        "nothing, which is the failure mode the suite itself was fixed for."
    )
    return match.group(1)


def _run_probe() -> list[tuple[str, str]]:
    result = subprocess.run(
        [sys.executable, "-", str(ROOT)],
        input=_probe_source(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    assert rows, "the probe printed nothing; an empty matrix reads as a clean run"
    return [(row[0], row[1]) for row in rows]


def test_gke_autopilot_resolves_to_the_browser_profile():
    """The row that was missing for two days, and which profile fills it."""
    covered = dict(_run_probe())
    assert covered.get("GKE_AUTOPILOT") == "browser", (
        f"GKE_AUTOPILOT is covered by {covered.get('GKE_AUTOPILOT')!r}; the smoke "
        "suite would not have submitted a browser task, which is the only kind "
        "that failed"
    )
    assert covered.get("CLOUD_RUN_JOB"), "CLOUD_RUN_JOB is not covered at all"


def test_every_concrete_backend_gets_a_row():
    """Including one no profile reaches: the script FAILS on a `-`, it does not skip.

    THE MUTATION THIS CATCHES: make the probe emit only the backends it found a
    profile for, and this fails -- which is the state the suite was in when
    GKE_AUTOPILOT was absent from the matrix entirely.
    """
    from swarm_common.profiles import Backend

    rows = dict(_run_probe())
    expected = {b.value for b in Backend if b is not Backend.AUTO}
    assert set(rows) == expected, (
        f"the probe covers {sorted(rows)} but the frozen contract has {sorted(expected)}"
    )
    # AUTO is an instruction to choose, not a backend. A row for it would mean
    # the probe read `profile.backend` instead of resolving it.
    assert "AUTO" not in rows


def test_the_probe_resolves_auto_rather_than_reading_the_raw_field():
    """Read out of the source, because the catalogue has no AUTO profile today.

    A profile with `backend=AUTO` and a large resource class resolves to
    GKE_AUTOPILOT. Reading `profile.backend` would file it under `AUTO` and
    leave GKE uncovered -- the original defect, one indirection deeper -- and no
    behavioural test can show that while every profile names its backend
    outright.
    """
    source = _probe_source()
    assert "resolve_backend(profile)" in source
    assert "profile.backend" not in source


def test_every_profile_the_probe_names_is_in_the_frozen_catalogue():
    """A row naming a profile that does not exist submits nothing and fails
    obscurely at submission instead of at derivation."""
    from swarm_common.profiles import RUNNER_PROFILES, resolve_backend

    for backend, profile_name in _run_probe():
        if profile_name == "-":
            continue
        assert profile_name in RUNNER_PROFILES, f"{backend}: no such profile {profile_name}"
        profile = RUNNER_PROFILES[profile_name]
        assert profile.available, f"{backend}: {profile_name} is not available"
        assert resolve_backend(profile).value == backend
