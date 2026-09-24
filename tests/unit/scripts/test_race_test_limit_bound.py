"""race-test.sh's copy of the admin API's ceiling bound matches the API.

scripts/race-test.sh refuses, before its first write, to narrow a pool whose
current ceiling the admin API could not put back: `PUT /v1/admin/limits/*`
takes a `LimitRequest`, whose `limit` is bounded, and a restore above that bound
is a 422 that leaves the pool pinned at one slot. The script cannot ask the API
what the bound is, so it carries a copy -- `API_LIMIT_MAX` -- and a copy is the
thing this repository has watched drift three times in two days.

Both directions of drift are defects, and not the same one:

  * the script's number HIGHER than the API's: a pool between the two is
    narrowed, and its restore is refused. The pool stays narrowed on a live
    deployment and the only record is an error line in a job log.
  * the script's number LOWER: a pool the API could restore is refused, and the
    suite reports a setup failure over a platform that was fine.

So they must be equal, and this compares them at the source rather than
trusting a comment beside either one.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_api.schemas import LimitRequest

ROOT = Path(__file__).resolve().parents[3]
RACE_TEST = ROOT / "scripts" / "race-test.sh"


def _script_bound() -> int:
    m = re.search(r"^API_LIMIT_MAX=(\d+)\s*$", RACE_TEST.read_text(), re.M)
    assert m, (
        "scripts/race-test.sh no longer assigns API_LIMIT_MAX; it cannot check "
        "that a ceiling is restorable before narrowing it"
    )
    return int(m.group(1))


def _api_bound() -> int:
    bounds = [
        getattr(meta, "le")
        for meta in LimitRequest.model_fields["limit"].metadata
        if hasattr(meta, "le")
    ]
    assert len(bounds) == 1, f"LimitRequest.limit has {len(bounds)} upper bounds: {bounds}"
    return int(bounds[0])


def test_the_script_refuses_exactly_what_the_api_cannot_restore() -> None:
    script, api = _script_bound(), _api_bound()
    assert script == api, (
        f"race-test.sh API_LIMIT_MAX is {script} and LimitRequest.limit accepts up to "
        f"{api}. "
        + (
            "A pool between the two would be narrowed and its restore refused 422."
            if script > api
            else "Pools the API could restore are being refused."
        )
    )
