"""The CI fixer (#263): a red SwarmCloud pull request gets a fix attempt with no operator.

`.github/workflows/ci-fix.yml` runs `scripts/ci-fix.sh` when the `application`
workflow fails on a `swarm/<task-id>` branch. The script reads the failed jobs'
log, passes it through `redact` and caps it, and submits a one-step
`claude-code` workflow that continues that task's branch. Each attempt comments
on the pull request; after `MAX_FIX_ATTEMPTS` it comments once more to say it
stopped, and submits nothing.

Nothing here touches the network. The script runs against fake `gh`, `gcloud`
and `curl` on PATH that serve fixtures and record every call, so each test can
assert on what WOULD have reached GitHub and the swarm API -- the request body
above all, because that body is where a secret from a CI log, an image or a
command would travel if the script ever put one there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from .application_paths import app_paths, reaches

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "ci-fix.sh"
WORKFLOW = REPO / ".github" / "workflows" / "ci-fix.yml"
APPLICATION = REPO / ".github" / "workflows" / "application.yml"

TASK = "task_0123456789abcdef0123"
BRANCH = f"swarm/{TASK}"
HEAD = "a" * 40
RUN_ID = "4242"
PR = "77"
GH_REPO = "acme/widgets"
OWNER = GH_REPO.split("/", 1)[0]
FORK_OWNER = "attacker"
BOT = "github-actions[bot]"

# Shapes the redaction must catch. Fake, and each distinct, so a leak is
# attributable to the rule that missed it.
GH_TOKEN_VALUE = "ghp_" + "Z" * 36
PASSWORD_VALUE = "hunter2-not-real-9f3a"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="jq and bash are required",
)


def _constant(name: str) -> int:
    match = re.search(rf"^{name}=(\d+)\b", SCRIPT.read_text(), re.MULTILINE)
    assert match, f"scripts/ci-fix.sh no longer sets {name} on a line of its own"
    return int(match.group(1))


def _log(lines_per_job: int = 5) -> str:
    """`gh run view --log-failed`: job TAB step TAB timestamped line."""
    out = []
    for job in ("format / unit tests", "shellcheck"):
        for i in range(lines_per_job):
            out.append(f"{job}\tRun tests\t2026-09-28T10:00:{i % 60:02d}.1234567Z noise line {i}")
    out += [
        "format / unit tests\tRun tests\t2026-09-28T10:01:00.0000000Z \x1b[31mFAILED\x1b[0m "
        "tests/unit/x/test_y.py::test_z - assert 1 == 2",
        f"format / unit tests\tRun tests\t2026-09-28T10:01:01.0000000Z GH_TOKEN={GH_TOKEN_VALUE}",
        f"format / unit tests\tRun tests\t2026-09-28T10:01:02.0000000Z password: {PASSWORD_VALUE}",
        "shellcheck\tshellcheck\t2026-09-28T10:01:03.0000000Z SC2086: Double quote to prevent globbing",
    ]
    return "\n".join(out) + "\n"


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pr_list(*prs: dict) -> str:
    """`gh pr list --json number,isCrossRepository,headRepositoryOwner`."""
    return json.dumps(list(prs))


def _same_repo_pr(number: str = PR, owner: str = OWNER) -> dict:
    return {"number": int(number), "isCrossRepository": False, "headRepositoryOwner": {"login": owner}}


def _fork_pr(number: str = "999", owner: str = FORK_OWNER) -> dict:
    return {"number": int(number), "isCrossRepository": True, "headRepositoryOwner": {"login": owner}}


def _pr_view(head_sha: str = HEAD, owner: str = OWNER, branch: str = BRANCH, cross: bool = False) -> str:
    """`gh pr view --json headRefOid,isCrossRepository,headRepositoryOwner,headRefName`."""
    return json.dumps(
        {
            "headRefOid": head_sha,
            "isCrossRepository": cross,
            "headRepositoryOwner": {"login": owner},
            "headRefName": branch,
        }
    )


def _comments_json(bodies: list[str], login: str = BOT) -> str:
    """`gh api .../comments`: the raw shape, before the script's own author filter."""
    return json.dumps([{"user": {"login": login}, "body": b} for b in bodies])


FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gh.calls"
case "$1 $2" in
  "pr list")    cat "${FAKE_DIR}/pr_list.json" ;;
  "pr view")    cat "${FAKE_DIR}/pr_view.json" ;;
  "run view")   cat "${FAKE_DIR}/log" ;;
  "pr comment")
    n=$(ls "${FAKE_DIR}" | grep -c '^comment-' || true)
    while [ $# -gt 0 ]; do
      if [ "$1" = "--body-file" ]; then cp "$2" "${FAKE_DIR}/comment-$((n + 1)).md"; fi
      shift
    done
    ;;
  api\ *)       cat "${FAKE_DIR}/comments.json" ;;
  *) echo "fake gh: unexpected: $*" >&2; exit 3 ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gcloud.calls"
case "$*" in
  *print-access-token*) printf 'ya29.fake-access-token-for-tests' ;;
  *) echo "fake gcloud: unexpected: $*" >&2; exit 3 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null  # the Authorization header arrives on stdin
url=""
method=""
while [ $# -gt 0 ]; do
  case "$1" in
    --data-binary) printf '%s' "$2" >"${FAKE_DIR}/request.json"; shift 2 ;;
    -X) method="$2"; shift 2 ;;
    https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
# A read is recorded apart from the submission, and served from
# get-<last path segment>.json with its status beside it; a read with no
# fixture is a 404. The script makes none since CI-FIXER-GATE (the API
# decides whether the lane owns the pull request), and a test holds it there.
if [ "${method}" = "GET" ]; then
  printf '%s\n' "${url}" >>"${FAKE_DIR}/curl-get.calls"
  name="${url##*/}"
  if [ -f "${FAKE_DIR}/get-${name}.json" ]; then
    cat "${FAKE_DIR}/get-${name}.json"
    printf '\n%s' "$(cat "${FAKE_DIR}/get-${name}.status" 2>/dev/null || printf 200)"
  else
    printf '{"code":"not_found"}\n404'
  fi
  exit 0
fi
printf '%s\n' "${url}" >>"${FAKE_DIR}/curl.calls"
cat "${FAKE_DIR}/api_response"
printf '\n%s' "$(cat "${FAKE_DIR}/api_status")"
"""


@pytest.fixture
def fake(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    _write_exe(bin_dir / "gh", FAKE_GH)
    _write_exe(bin_dir / "gcloud", FAKE_GCLOUD)
    _write_exe(bin_dir / "curl", FAKE_CURL)
    (state / "pr_list.json").write_text(_pr_list(_same_repo_pr()))
    (state / "pr_view.json").write_text(_pr_view())
    (state / "comments.json").write_text(_comments_json([]))
    (state / "log").write_text(_log())
    (state / "api_status").write_text("201")
    (state / "api_response").write_text(
        json.dumps(
            {
                "workflow": {
                    "workflow_id": "wf_0000000000000000000a",
                    "steps": [{"step_id": "ci-fix", "task_id": "task_bbbbbbbbbbbbbbbbbbbb"}],
                },
                "dispatch": {"strategy": "direct-pr", "continues_task": TASK},
            }
        )
    )
    return bin_dir, state


def _run(fake, *args: str, stdin: str | None = None, **env: str) -> subprocess.CompletedProcess:
    bin_dir, state = fake
    base = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(state),
        "FAKE_DIR": str(state),
        "NO_COLOR": "1",
        "SWARM_ENV_FILE": str(state / "no.env"),
        "GITHUB_REPOSITORY": GH_REPO,
        "GITHUB_SERVER_URL": "https://github.com",
        "CI_FIX_RUN_ID": RUN_ID,
        "CI_FIX_HEAD_BRANCH": BRANCH,
        "CI_FIX_HEAD_SHA": HEAD,
        "API_HOST": "swarm.example.test",
        "SWARM_IMPERSONATE_SA": "swarm-ci-fixer@example.iam.gserviceaccount.com",
    }
    base.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=base,
        cwd=str(REPO),
        timeout=60,
    )


def _comments(state: Path) -> list[str]:
    return [p.read_text() for p in sorted(state.glob("comment-*.md"))]


def _calls(state: Path, tool: str) -> list[str]:
    path = state / f"{tool}.calls"
    return path.read_text().splitlines() if path.exists() else []


# --------------------------------------------------------------------------
# The constants, and why they are what they are
# --------------------------------------------------------------------------

def test_the_attempt_cap_is_a_named_constant_with_its_reason_beside_it():
    text = SCRIPT.read_text()
    cap = _constant("MAX_FIX_ATTEMPTS")
    assert 1 <= cap <= 3, "a cap above three is a loop, not a fixer"
    at = text.index("MAX_FIX_ATTEMPTS=")
    assert "#" in text[max(0, at - 1500):at], "the cap has no reason written beside it"
    assert _constant("LOG_EXCERPT_MAX_BYTES") <= 64 * 1024


# --------------------------------------------------------------------------
# The excerpt: redacted, then capped
# --------------------------------------------------------------------------

def test_the_excerpt_is_redacted_before_anything_else_sees_it(fake):
    out = _run(fake, "excerpt", stdin=_log())
    assert out.returncode == 0, out.stderr
    assert GH_TOKEN_VALUE not in out.stdout
    assert PASSWORD_VALUE not in out.stdout
    # What the fixer needs survives, without the terminal colour codes and
    # without the timestamp column the log viewer adds.
    assert "tests/unit/x/test_y.py::test_z - assert 1 == 2" in out.stdout
    assert "SC2086" in out.stdout
    assert "\x1b[" not in out.stdout
    assert "2026-09-28T10:01:00" not in out.stdout


def test_the_excerpt_is_capped_and_keeps_every_failed_jobs_tail(fake):
    cap = _constant("LOG_EXCERPT_MAX_BYTES")
    out = _run(fake, "excerpt", stdin=_log(lines_per_job=5000))
    assert out.returncode == 0, out.stderr
    assert len(out.stdout.encode()) <= cap
    # Both jobs failed; a cap that kept only the last job's tail would hand
    # the fixer one failure and leave the PR red on the other.
    assert "assert 1 == 2" in out.stdout
    assert "SC2086" in out.stdout


# --------------------------------------------------------------------------
# One attempt
# --------------------------------------------------------------------------

def test_a_red_swarm_pr_gets_one_fix_step_that_names_a_profile_and_nothing_else(fake):
    _, state = fake
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr

    body = json.loads((state / "request.json").read_text())
    assert _calls(state, "curl") == ["https://swarm.example.test/v1/workflows"]

    # Invariant 10: a profile by name; no image, command, backend or resources
    # anywhere in what is sent.
    assert set(body) == {"steps", "strategy", "continues_task", "metadata"}
    (step,) = body["steps"]
    assert set(step) == {"step_id", "runner_profile", "input"}
    assert step["runner_profile"] == "claude-code"
    assert set(step["input"]) == {"prompt"}
    assert body["strategy"] == "direct-pr"
    assert body["continues_task"] == TASK
    flat = json.dumps(body)
    for forbidden in ('"image"', '"command"', '"backend"', '"cpu"', '"memory"', '"repository_ref"'):
        assert forbidden not in flat

    prompt = step["input"]["prompt"]
    assert GH_TOKEN_VALUE not in flat and PASSWORD_VALUE not in flat
    assert "assert 1 == 2" in prompt
    assert BRANCH in prompt and RUN_ID in prompt
    assert body["metadata"]["ci_fix"] == {
        "pull_request": int(PR),
        "run_id": int(RUN_ID),
        "head_sha": HEAD,
        "attempt": 1,
        # The cap travels with the attempt, so the API can hold it and the
        # post-back can say when it is reached (#263).
        "max_attempts": _constant("MAX_FIX_ATTEMPTS"),
    }

    (comment,) = _comments(state)
    cap = _constant("MAX_FIX_ATTEMPTS")
    assert f"attempt 1 of {cap}" in comment
    assert "<!-- swarm-ci-fix:attempt -->" in comment
    assert "task_bbbbbbbbbbbbbbbbbbbb" in comment
    assert GH_TOKEN_VALUE not in comment and PASSWORD_VALUE not in comment


def test_the_next_attempt_is_numbered_from_the_comments_already_on_the_pr(fake):
    _, state = fake
    (state / "comments.json").write_text(_comments_json(["<!-- swarm-ci-fix:attempt -->\nattempt 1"]))
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    body = json.loads((state / "request.json").read_text())
    assert body["metadata"]["ci_fix"]["attempt"] == 2


# --------------------------------------------------------------------------
# The pull request is never picked by searching for its branch name alone
# (#273 review): `gh pr list --head` matches any pull request with that head
# branch, including one a fork author opened naming their own branch after
# the real task, which would redirect the attempt count and every comment
# onto a pull request they control.
# --------------------------------------------------------------------------

def test_ci_fix_pr_number_from_the_event_is_used_and_no_branch_search_happens(fake):
    """When the workflow_run event already names the pull request, the
    script never falls back to a branch-name search for it."""
    _, state = fake
    out = _run(fake, "run", CI_FIX_PR_NUMBER=PR)
    assert out.returncode == 0, out.stderr
    calls = _calls(state, "gh")
    assert not any(c.startswith("pr list") for c in calls), "searched by branch name despite CI_FIX_PR_NUMBER"
    assert any(c.startswith("pr view") for c in calls)
    body = json.loads((state / "request.json").read_text())
    assert body["metadata"]["ci_fix"]["pull_request"] == int(PR)


def test_a_fork_pull_request_with_the_same_branch_name_is_never_picked(fake):
    """A fork can open a PR whose head branch is named swarm/<the real task
    id> -- a string only the fork author chooses. The branch-name fallback
    must not select it, and nothing about it may be commented on."""
    _, state = fake
    (state / "pr_list.json").write_text(_pr_list(_fork_pr()))
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    assert _calls(state, "curl") == [], "a fix was submitted for a fork's pull request"
    assert _comments(state) == [], "a comment was posted on a fork's pull request"


def test_a_same_named_fork_pr_does_not_shadow_the_real_same_repo_pr(fake):
    """Both a legitimate same-repository PR and an attacker's same-named
    fork PR can be open at once; the real one is still the one picked."""
    _, state = fake
    (state / "pr_list.json").write_text(_pr_list(_fork_pr(), _same_repo_pr()))
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    body = json.loads((state / "request.json").read_text())
    assert body["metadata"]["ci_fix"]["pull_request"] == int(PR)


def test_a_forged_ci_fix_pr_number_pointing_at_a_fork_pr_is_refused(fake):
    """Defence in depth: even if CI_FIX_PR_NUMBER named a cross-repository
    pull request, the script's own `gh pr view` check refuses it rather than
    trusting the number alone."""
    _, state = fake
    (state / "pr_view.json").write_text(_pr_view(owner=FORK_OWNER, cross=True))
    out = _run(fake, "run", CI_FIX_PR_NUMBER=PR)
    assert out.returncode == 0, out.stderr
    assert _calls(state, "curl") == []
    assert _comments(state) == []


# --------------------------------------------------------------------------
# Only github-actions[bot]'s own comments count toward the attempt cap
# (#273 review): the cap is a count over comments on the pull request, and
# without an author check a third party could post the marker themselves.
# --------------------------------------------------------------------------

def test_attempt_markers_from_a_third_party_do_not_count_toward_the_cap(fake):
    _, state = fake
    cap = _constant("MAX_FIX_ATTEMPTS")
    (state / "comments.json").write_text(
        _comments_json(["<!-- swarm-ci-fix:attempt -->"] * (cap + 5), login="some-random-user")
    )
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    # Spoofed markers from a non-bot author did not push the count to the
    # cap: this is still attempt 1, and a fix was still submitted.
    assert _calls(state, "curl") != [], "third-party comments stopped the fixer"
    body = json.loads((state / "request.json").read_text())
    assert body["metadata"]["ci_fix"]["attempt"] == 1


def test_attempt_markers_from_the_bot_still_count(fake):
    """The filter narrows by author; it does not stop counting altogether."""
    _, state = fake
    # One real bot attempt and one third-party comment mimicking the marker.
    (state / "comments.json").write_text(
        json.dumps(
            [
                {"user": {"login": BOT}, "body": "<!-- swarm-ci-fix:attempt -->"},
                {"user": {"login": "some-random-user"}, "body": "<!-- swarm-ci-fix:attempt -->"},
            ]
        )
    )
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    body = json.loads((state / "request.json").read_text())
    # Only the one bot comment counted, so this is attempt 2, not 3.
    assert body["metadata"]["ci_fix"]["attempt"] == 2


# --------------------------------------------------------------------------
# Stopping, and saying so
# --------------------------------------------------------------------------

def test_at_the_cap_nothing_is_submitted_and_the_pr_says_why_once(fake):
    _, state = fake
    cap = _constant("MAX_FIX_ATTEMPTS")
    (state / "comments.json").write_text(_comments_json(["<!-- swarm-ci-fix:attempt -->"] * cap))

    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    assert _calls(state, "curl") == [], "a fix was submitted past the cap"
    (comment,) = _comments(state)
    assert "<!-- swarm-ci-fix:stopped -->" in comment
    assert str(cap) in comment

    # The next red run on the same PR says nothing more.
    (state / "comments.json").write_text(
        _comments_json(["<!-- swarm-ci-fix:attempt -->"] * cap + ["<!-- swarm-ci-fix:stopped -->"])
    )
    again = _run(fake, "run")
    assert again.returncode == 0, again.stderr
    assert len(_comments(state)) == 1
    assert _calls(state, "curl") == []


def test_a_run_for_a_commit_that_is_no_longer_the_head_is_ignored(fake):
    """Something already moved the branch; fixing the old failure would fight it."""
    _, state = fake
    (state / "pr_view.json").write_text(_pr_view(head_sha="b" * 40))
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    assert _calls(state, "curl") == []
    assert _comments(state) == []


@pytest.mark.parametrize(
    "branch",
    ["main", "feature/x", "swarm/not-a-task", f"swarm/{TASK}/../main", f"swarm/{TASK}x"],
)
def test_only_a_swarm_task_branch_is_touched(fake, branch):
    _, state = fake
    out = _run(fake, "run", CI_FIX_HEAD_BRANCH=branch)
    assert out.returncode == 0, out.stderr
    assert _calls(state, "curl") == []
    assert _calls(state, "gh") == []


def test_a_refused_submission_is_reported_on_the_pr_and_fails_the_run(fake):
    _, state = fake
    (state / "api_status").write_text("422")
    (state / "api_response").write_text(
        json.dumps({"code": "invalid_dispatch", "message": f"no task {TASK} token=abc123secret"})
    )
    out = _run(fake, "run")
    assert out.returncode != 0
    (comment,) = _comments(state)
    assert "422" in comment and "invalid_dispatch" in comment
    # Not an attempt: nothing ran, so nothing may count against the cap.
    assert "<!-- swarm-ci-fix:attempt -->" not in comment
    assert "abc123secret" not in comment


LIVE_TASK = "task_cccccccccccccccccccc"
LIVE_WORKFLOW = "wf_dddddddddddddddddddd"


def _in_flight(code: str = "ci_fix_in_flight") -> str:
    """The API's 409 when this failure already has a live fix (cifix.CiFixInFlight)."""
    return json.dumps({
        "code": code,
        "message": f"pull request #{PR}'s red run already has a live CI-fix task token=abc123secret",
        "detail": {"metadata": "ci_fix", "task_id": LIVE_TASK, "workflow_id": LIVE_WORKFLOW,
                   "pull_request": int(PR), "head_sha": HEAD, "run_id": int(RUN_ID)},
    })


def test_a_failure_with_a_live_fix_is_logged_with_no_comment_and_passes(fake):
    """409 `ci_fix_in_flight` (FIX-MERGE-2, epic #1334): the failure is being
    handled by the named task, so the run names it and passes, and the pull
    request gets no "not submitted" comment -- nothing failed."""
    _, state = fake
    (state / "api_status").write_text("409")
    (state / "api_response").write_text(_in_flight())

    out = _run(fake, "run")

    assert out.returncode == 0, out.stderr
    assert _comments(state) == []
    logged = out.stdout + out.stderr
    assert LIVE_TASK in logged and LIVE_WORKFLOW in logged
    # The ids, never the body wholesale.
    assert "abc123secret" not in logged and "live CI-fix task" not in logged


def test_a_live_fix_still_being_submitted_is_logged_without_a_task_id(fake):
    _, state = fake
    body = json.loads(_in_flight())
    body["detail"]["task_id"] = None
    body["detail"]["workflow_id"] = None
    (state / "api_status").write_text("409")
    (state / "api_response").write_text(json.dumps(body))

    out = _run(fake, "run")

    assert out.returncode == 0, out.stderr
    assert _comments(state) == []


def test_an_id_in_the_answer_that_is_not_an_id_is_not_logged(fake):
    _, state = fake
    body = json.loads(_in_flight())
    body["detail"]["task_id"] = "task_x $(whoami) token=abc123secret"
    (state / "api_status").write_text("409")
    (state / "api_response").write_text(json.dumps(body))

    out = _run(fake, "run")

    assert out.returncode == 0, out.stderr
    assert "abc123secret" not in out.stdout + out.stderr


@pytest.mark.parametrize("status,body", [
    ("409", _in_flight(code="conflict")),
    ("500", json.dumps({"code": "internal", "message": "boom"})),
    ("500", _in_flight()),
])
def test_any_other_refusal_is_still_reported_on_the_pr_and_fails_the_run(fake, status, body):
    _, state = fake
    (state / "api_status").write_text(status)
    (state / "api_response").write_text(body)

    out = _run(fake, "run")

    assert out.returncode != 0
    (comment,) = _comments(state)
    assert "<!-- swarm-ci-fix:refused -->" in comment and status in comment
    assert "<!-- swarm-ci-fix:attempt -->" not in comment


def test_an_unconfigured_fixer_says_so_on_the_pr_once_and_submits_nothing(fake):
    _, state = fake
    out = _run(fake, "run", SWARM_IMPERSONATE_SA="")
    assert out.returncode == 0, out.stderr
    assert _calls(state, "curl") == []
    (comment,) = _comments(state)
    assert "<!-- swarm-ci-fix:unconfigured -->" in comment

    (state / "comments.json").write_text(_comments_json(["<!-- swarm-ci-fix:unconfigured -->"]))
    _run(fake, "run", SWARM_IMPERSONATE_SA="")
    assert len(_comments(state)) == 1


# --------------------------------------------------------------------------
# One fixer per red pull request (CI-FIXER-ONE, then CI-FIXER-GATE, part of
# #1822). A lane whose workflow has a merge step runs its own CI-fix rounds
# on that pull request; a standalone fix submitted beside them fought the
# round for the branch, and 22 of 91 standalone runs in 72 h ended in a
# rejected push. CI-FIXER-ONE had the script read the lane's workflow, but
# the fixer's continuation-scoped identity reads only what it submitted, so
# that read answered 404 for every real lane. The API now decides, inside
# its ci-fix submit path (swarm_api.cifix, `ci_fix_lane_owns_pr`): the script
# makes no read of its own and treats that 409 as "the lane owns this PR".
# --------------------------------------------------------------------------

MERGE_TASK = "task_cccccccccccccccccccc"
LANE_WORKFLOW = "wf_0123456789abcdef0123"


def _lane_owns_pr(task_id: object = MERGE_TASK, workflow_id: object = LANE_WORKFLOW) -> str:
    """The API's 409 while the lane's merge step is live (cifix.CiFixLaneOwnsPr)."""
    return json.dumps({
        "code": "ci_fix_lane_owns_pr",
        "message": f"pull request #{PR} belongs to a lane whose merge step is live token=abc123secret",
        "detail": {"metadata": "ci_fix", "task_id": task_id, "workflow_id": workflow_id,
                   "pull_request": int(PR)},
    })


def test_a_lane_pr_whose_merge_step_is_live_is_skipped_with_no_comment_and_passes(fake):
    _, state = fake
    (state / "api_status").write_text("409")
    (state / "api_response").write_text(_lane_owns_pr())

    out = _run(fake, "run")

    # MUTATION: drop the `ci_fix_lane_owns_pr` branch -- the 409 is reported
    # on the pull request as a refusal and the fixer run goes red.
    assert out.returncode == 0, out.stderr
    # MUTATION: post a comment when standing down -- the lane's own round is
    # the fix, and nothing failed.
    assert _comments(state) == []
    logged = out.stdout + out.stderr
    # MUTATION: log the skip without the merge step's id.
    assert MERGE_TASK in logged and LANE_WORKFLOW in logged
    assert "owns" in logged, "the log does not say the lane owns the pull request"
    # MUTATION: print the answer's message -- only the minted ids are read.
    assert "abc123secret" not in logged


def test_an_id_in_the_lane_answer_that_is_not_an_id_is_not_logged(fake):
    _, state = fake
    (state / "api_status").write_text("409")
    (state / "api_response").write_text(_lane_owns_pr(task_id="task_x $(whoami) token=abc123secret"))

    out = _run(fake, "run")

    assert out.returncode == 0, out.stderr
    # MUTATION: drop the `task_<20 hex>` shape check on detail.task_id.
    assert "abc123secret" not in out.stdout + out.stderr
    assert _comments(state) == []


def test_the_lane_code_on_another_status_is_still_a_refusal(fake):
    _, state = fake
    (state / "api_status").write_text("500")
    (state / "api_response").write_text(_lane_owns_pr())

    out = _run(fake, "run")

    # MUTATION: match on the code alone, not on HTTP 409 beside it.
    assert out.returncode != 0
    (comment,) = _comments(state)
    assert "<!-- swarm-ci-fix:refused -->" in comment


def test_the_script_makes_no_read_of_its_own_the_api_decides(fake):
    """One source of truth: CI-FIXER-ONE's task and workflow reads, which the
    fixer's identity is never served, are gone."""
    _, state = fake
    out = _run(fake, "run")
    assert out.returncode == 0, out.stderr
    # MUTATION: keep the client-side `GET /v1/tasks/<task>` read.
    assert _calls(state, "curl-get") == []
    assert _calls(state, "curl") == ["https://swarm.example.test/v1/workflows"]
    # MUTATION: keep the `decide` subcommand -- a second, unreachable rule.
    assert _run(fake, "decide", stdin="{}").returncode != 0
    assert "/tasks/" not in SCRIPT.read_text()


# --------------------------------------------------------------------------
# The workflow that runs it
# --------------------------------------------------------------------------

def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_workflow_runs_when_application_completes_and_only_on_a_red_swarm_branch():
    wf = _workflow()
    on = wf.get("on", wf.get(True))
    assert on["workflow_run"]["workflows"] == [yaml.safe_load(APPLICATION.read_text())["name"]]
    assert on["workflow_run"]["types"] == ["completed"]
    (job,) = wf["jobs"].values()
    condition = job["if"]
    assert "github.event.workflow_run.conclusion == 'failure'" in condition
    assert "startsWith(github.event.workflow_run.head_branch, 'swarm/')" in condition
    # A fork's branch can be called swarm/<anything>; its CI failure is not ours to fix.
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in condition


def test_the_workflow_never_checks_out_or_interpolates_the_pr_it_is_fixing():
    """workflow_run runs with this repository's secrets. Running the red
    branch's code, or pasting its branch name into a shell, hands them over."""
    wf = _workflow()
    (job,) = wf["jobs"].values()
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith("actions/checkout"):
            assert "ref" not in (step.get("with") or {}), "the fixer checked out the PR"
        run = step.get("run", "")
        assert "github.event.workflow_run" not in run, (
            "an event field is interpolated into a shell; pass it through env"
        )
    permissions = wf.get("permissions") or {}
    assert permissions.get("pull-requests") == "write"
    assert permissions.get("contents") == "read"
    assert permissions.get("actions") == "read"


def test_the_workflow_is_linted_and_its_tests_run_on_the_prs_that_change_it():
    """It runs only after a red CI on a swarm branch, so a broken expression in
    it would first show on the PR it was meant to fix."""
    app = yaml.safe_load(APPLICATION.read_text())
    assert reaches(".github/workflows/ci-fix.yml"), app_paths()
    runs = "\n".join(step.get("run", "") for step in app["jobs"]["workflows"]["steps"])
    assert ".github/workflows/ci-fix.yml" in runs, "ci-fix.yml is not linted"
