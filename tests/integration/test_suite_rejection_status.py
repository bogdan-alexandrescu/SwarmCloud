"""Which HTTP statuses a verification suite may count as "the API refused this".

Three checks in the operator suites exist to prove that the API's validator
refuses something: malformed bodies and an oversized input in
`scripts/failure-test.sh`, and a request carrying an image and a command in
`scripts/smoke-test.sh` (CONTRACT invariant 10). The sweep in
docs/audits/2026-09-18/13-swallowed-stderr-sweep.md found both counting ANY
non-2xx as that refusal, so a dead API, an expired token and a missing
run.invoker grant all printed PASS -- the suite was greenest when nothing
answered at all.

The 2026-09-19 fix narrowed "any non-2xx" to "any 4xx". That still counts a
401 (expired session), a 403 (the caller lacks run.invoker, or IAP refused the
credential) and a 404 (the wrong address -- a *.run.app URL answers 404 to every
path from outside the VPC) as the validator having spoken. Only two statuses
mean the request reached validation and was turned away: 422, which is
`ValidationFailed` and FastAPI's own request-validation answer
(apps/swarm-api/swarm_api/errors.py, main.py), and 400, the `ApiError` base.

The rule now lives once, as `validation_rejected` in scripts/lib/testlib.sh, and
this file holds it to exactly that set. The second test holds the three call
sites to the rule: the mutation that produced this finding is a hand-written
`-ge 400 && -lt 500` window, and it is the thing most likely to come back.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

REJECTIONS = {400, 422}
NOT_REJECTIONS = {0, 200, 201, 401, 403, 404, 409, 413, 429, 500, 502, 503}


def _classify(statuses: list[int]) -> dict[int, bool]:
    script = (
        "set -euo pipefail\n"
        f'source "{ROOT}/scripts/lib/common.sh"\n'
        f'source "{ROOT}/scripts/lib/testlib.sh"\n'
        'for s in "$@"; do\n'
        '  if validation_rejected "${s}"; then echo "${s} yes"; else echo "${s} no"; fi\n'
        "done\n"
    )
    env = dict(os.environ, SWARM_ENV_FILE="/dev/null", NO_COLOR="1")
    proc = subprocess.run(
        ["bash", "-c", script, "classify", *[str(s) for s in statuses]],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out: dict[int, bool] = {}
    for line in proc.stdout.splitlines():
        status, verdict = line.split()
        out[int(status)] = verdict == "yes"
    # Every status asked about was answered. An empty stdout would otherwise
    # make every assertion below vacuous.
    assert sorted(out) == sorted(statuses), proc.stdout
    return out


@pytest.mark.parametrize("status", sorted(REJECTIONS))
def test_a_validator_refusal_counts(status: int) -> None:
    assert _classify([status])[status] is True


@pytest.mark.parametrize("status", sorted(NOT_REJECTIONS))
def test_nothing_else_counts_as_the_validator_having_spoken(status: int) -> None:
    assert _classify([status])[status] is False, (
        f"HTTP {status} was counted as the API refusing the request. It is not: "
        "the request never reached validation, or reached it and was not refused."
    )


def test_no_suite_counts_a_4xx_window_as_a_rejection() -> None:
    """The regression guard for the exact shape the sweep found, twice.

    A numeric range over API_STATUS is how 401 and 403 get back in. The suites
    ask `validation_rejected`; any range comparison on API_STATUS in a suite is
    a second, weaker copy of that rule.
    """
    window = re.compile(r'API_STATUS\}?"?\s+-(ge|gt)\s+"?(399|400)')
    offenders = []
    suites = sorted((ROOT / "scripts").glob("*-test.sh"))
    assert suites, "no *-test.sh suites found; this guard would pass having read nothing"
    for script in suites:
        for n, line in enumerate(script.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if window.search(line):
                offenders.append(f"{script.name}:{n}: {line.strip()}")
    assert not offenders, (
        "a suite counts a range of statuses as the API's refusal; use "
        "validation_rejected from scripts/lib/testlib.sh:\n" + "\n".join(offenders)
    )
