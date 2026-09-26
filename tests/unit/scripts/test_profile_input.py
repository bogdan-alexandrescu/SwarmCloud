"""Every suite submits an input its runner can COMPLETE, per profile.

THE DEFECT THIS PINS. `agent_worker/runners/browser.py` refuses an input with
neither `url` nor `actions`: "browser runner needs input.url or at least one
action". `scripts/smoke-test.sh` submitted `{message, run_id}` to every profile
in its backend matrix -- and `browser` is the matrix's only GKE_AUTOPILOT row --
so that row could not pass even with GKE dispatch working perfectly. So could
`smoke-test.sh --profile browser`, which is the exact command
docs/incidents/2026-09-24-gke-dispatch.md section 5 names as "the cheapest
proof".

`profile_input` in scripts/lib/testlib.sh is now the one place a suite's
per-profile input is written, and both the smoke suite and
scripts/prove-gke-dispatch.sh submit through it. The function is run for real
(sourced by bash, as the suites source it), for every AVAILABLE profile in the
frozen catalogue, so a new profile is covered the day it is added.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES

ROOT = Path(__file__).resolve().parents[3]


def profile_input(profile: str, run_id: str = "test-run") -> dict:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env.pop("SWARM_ENV_FILE", None)
    script = (
        f'source "{ROOT}/scripts/lib/common.sh"; '
        f'source "{ROOT}/scripts/lib/testlib.sh"; '
        f'profile_input "$1" "$2"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "profile-input", profile, run_id],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


AVAILABLE = sorted(n for n, p in RUNNER_PROFILES.items() if getattr(p, "available", True))


@pytest.mark.parametrize("profile", AVAILABLE)
def test_every_profile_gets_an_object_carrying_the_run_id(profile):
    """In the PROMPT. It used to be a key of its own, `run_id`, beside a
    `message` no runner read; since contract request 25 the API refuses a key
    the profile does not declare, and `prompt` is the one every profile takes."""
    body = profile_input(profile, "run-123")
    assert isinstance(body, dict)
    assert "run-123" in str(body.get("prompt", "")), (
        "the run id is how test traffic is found again, and the prompt is the "
        f"one place every profile accepts it: {body!r}"
    )


@pytest.mark.parametrize("profile", AVAILABLE)
def test_every_profile_input_is_one_its_profile_declares(profile):
    """The suites submit through this against a live platform, where a key the
    catalogue does not declare is a 422 `invalid_input` and the run proves
    nothing. Checked with the rule the API applies, not a copy of it, and for
    every profile: what a profile whose inputs are not declared yet takes is
    that rule's to say too, not a special case here."""
    from swarm_common.profiles import check_inputs

    body = profile_input(profile, "run-123")
    check_inputs(RUNNER_PROFILES[profile], {k: v for k, v in body.items() if k != "prompt"})


def test_the_browser_input_is_one_the_browser_runner_accepts():
    body = profile_input("browser")
    actions = body.get("actions")
    # The runner's own gate, restated as the property: a url, or a non-empty
    # list of actions.
    assert body.get("url") or (isinstance(actions, list) and actions), (
        f"{body!r} carries neither url nor actions; the browser runner refuses it "
        "before Chromium starts, so the task fails with dispatch working"
    )
    for action in actions or []:
        assert action.get("type") in {
            "goto", "click", "fill", "press", "wait_for", "wait", "screenshot", "extract",
        }, f"{action!r} is not an action the browser runner implements"
        if action.get("type") == "goto":
            assert re.match(r"^https?://", action.get("url", "")), action


def test_the_browser_input_needs_no_site_outside_the_platform():
    # A proof that depends on a third-party page being up fails for reasons
    # that are not the platform's. --url exists for proving egress on purpose.
    body = profile_input("browser")
    assert not body.get("url")
    assert all(a.get("type") != "goto" for a in body.get("actions") or [])


@pytest.mark.parametrize("script", ["scripts/smoke-test.sh", "scripts/prove-gke-dispatch.sh"])
def test_the_suites_submit_through_it(script):
    """The call sites, because a helper nothing calls fixes nothing."""
    text = (ROOT / script).read_text()
    submissions = re.findall(r"submit_task\s+[^\n]*", text)
    assert submissions, f"{script} submits nothing"
    for line in submissions:
        assert "profile_input" in line, (
            f"{script} submits a task without profile_input: {line.strip()!r}"
        )
