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
    every profile: every one declares its inputs since contract request 32
    (#218), `generic`'s required `command` included, so what each takes is
    that rule's to say, not a special case here."""
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


def profile_extra(profile: str) -> dict:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env.pop("SWARM_ENV_FILE", None)
    env.pop("SWARM_SMOKE_FIXTURE_REPOSITORY", None)
    env.pop("SWARM_SMOKE_FIXTURE_REF", None)
    script = (
        f'source "{ROOT}/scripts/lib/common.sh"; '
        f'source "{ROOT}/scripts/lib/testlib.sh"; '
        f'profile_extra "$1"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "profile-extra", profile],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# -- generic runs a command that PASSES, on a fixture the smoke clones ------------
#
# The owner's decision on #345 (2026-09-29): the generic smoke row sends
# `command=pytest` and asserts exit 0, rather than proving dispatch alone. On
# an empty workspace pytest exits 5, "no tests collected", so the row clones
# this repository and runs pytest in a directory that holds one passing test.

#: The public repository the smoke clones for the fixture: this one. Public,
#: so the clone needs no tenant credential (`Worker._git_token` returns None
#: and the clone runs anonymously).
FIXTURE_REPOSITORY = "https://github.com/bogdan-alexandrescu/SwarmCloud.git"


def _fixture_dir() -> Path:
    body = profile_input("generic")
    working = body.get("working_directory")
    assert isinstance(working, str) and working.startswith("repo/"), (
        "the generic input must run pytest inside the clone (`work/repo`), in the "
        f"fixture directory, not the bare workspace: {body!r}"
    )
    return ROOT / working[len("repo/"):]


def test_the_generic_input_runs_pytest_in_the_fixture_directory():
    body = profile_input("generic")
    assert body.get("command") == "pytest", body
    fixture = _fixture_dir()
    assert fixture.is_dir(), f"{fixture} is not a directory in this repository"
    assert (fixture / "pytest.ini").is_file(), (
        "the fixture needs its own pytest.ini, so pytest takes it as the rootdir "
        "and reads neither this repository's pyproject.toml nor a conftest above it"
    )
    assert "paths" not in body, (
        "input.paths resolves against the workspace but reaches pytest's argv as "
        "written, beside a working_directory -- pytest would look for it twice deep"
    )


def test_the_fixture_has_a_test_that_passes():
    """Run it the way the generic runner does: `python -m pytest -q --color=no`,
    in the fixture directory. Exit 0 with exactly one test passed -- not 5,
    "no tests collected", which is what the row got on an empty workspace."""
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider"],
        cwd=_fixture_dir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"\b1 passed\b", result.stdout), result.stdout


def test_generic_is_submitted_with_the_fixture_repository_and_nothing_else_is():
    extra = profile_extra("generic")
    assert extra.get("repository_url") == FIXTURE_REPOSITORY, extra
    assert extra.get("repository_ref") == "main", extra
    from swarm_api.validation import check_repository_url

    assert check_repository_url(extra["repository_url"]) == extra["repository_url"]
    for profile in AVAILABLE:
        if profile != "generic":
            assert profile_extra(profile) == {}, profile


def test_the_smoke_asserts_the_generic_command_exited_zero():
    """SUCCEEDED alone is the worker's reading; the row names the command and
    its exit code from the runner's own result, so a pass is pytest's."""
    text = (ROOT / "scripts" / "smoke-test.sh").read_text()
    assert ".result_summary.runner.output.exit_code" in text, (
        "smoke-test.sh no longer reads the generic runner's exit code"
    )
    assert ".result_summary.runner.output.command" in text


@pytest.mark.parametrize("script", ["scripts/smoke-test.sh", "scripts/prove-gke-dispatch.sh"])
def test_the_suites_submit_with_profile_extra(script):
    """The generic row cannot pass without its clone, so every suite that
    submits through `profile_input` merges `profile_extra` into the body."""
    text = (ROOT / script).read_text()
    assert "profile_extra" in text, f"{script} submits through profile_input without profile_extra"


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
