"""The auto-merge scripts' GitHub reads survive a transient failure.

MEASURED 2026-11-04. The workflow page redesign's pull request was labelled
`ready` at 13:57 UTC; at 14:00 auto-merge.yml's "queue for auto-merge" check
(run 41842863519) failed because scripts/check-closing-references.sh's
`gh api graphql` read hit `dial tcp ...:443: i/o timeout` on the self-hosted
runner, and the merge train passed the pull request over for eight hours.

`gh_api_read_retry` (scripts/lib/common.sh) now carries every READ in
check-closing-references.sh, check-pr-dependencies.sh and
close-merged-issues.sh: a connection timeout or reset, a DNS failure, HTTP
502/503/504 or a rate limit (429, or a 403 with a rate-limit header) is asked
again up to three more times, after 5, 15 and 30 s (at least the Retry-After
GitHub sent); any other answer -- a 404, a 401, a 403 without a rate-limit
header -- fails at once; and a read that never succeeds fails closed with the
message it always had.

These tests RUN the scripts against a fake `gh` that answers from a plan, one
outcome per call, and a fake `sleep` that records each wait and returns at
once. Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
THIS_REPO = "bogdan-alexandrescu/SwarmCloud"
PR = "4242"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required"
)

# Each `gh api` call takes the next line of FAKE_GH_PLAN (the last line repeats
# once the plan runs out):
#   ok              the body in FAKE_GH_BODY, after a status line and headers
#                   when --include is given, as real gh prints them (CRLF);
#   ok:<text>       <text> as the body, the same way;
#   timeout         no answer at all, the 2026-11-04 failure's own words;
#   reset, dns      a connection reset, a DNS failure;
#   <status>        that HTTP status, with gh's own `gh: ... (HTTP <status>)`;
#   403-retry:<s>   a 403 carrying `Retry-After: <s>`;
#   403-limit       a 403 carrying `X-RateLimit-Remaining: 0`.
# `gh issue close` and `gh pr comment` are writes: logged, and they succeed.
FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "${1:-}" != "api" ]]; then
  exit 0
fi
include=0
for arg in "$@"; do
  [[ "${arg}" == "--include" ]] && include=1
done
calls="$(grep -c '^api ' "${FAKE_GH_LOG}")"
outcome="$(sed -n "${calls}p" "${FAKE_GH_PLAN}")"
[[ -n "${outcome}" ]] || outcome="$(tail -n 1 "${FAKE_GH_PLAN}")"
headers() {
  if [[ "${include}" -eq 1 ]]; then
    printf 'HTTP/2.0 %s\r\nContent-Type: application/json; charset=utf-8\r\n' "$1"
    shift
    for h in "$@"; do printf '%s\r\n' "${h}"; done
    printf '\r\n'
  fi
}
case "${outcome}" in
  ok)
    headers "200 OK" "X-Ratelimit-Remaining: 4999"
    cat "${FAKE_GH_BODY}"
    exit 0
    ;;
  ok:*)
    headers "200 OK"
    printf '%s\n' "${outcome#ok:}"
    exit 0
    ;;
  timeout)
    echo 'Post "https://api.github.com/graphql": dial tcp 140.82.114.6:443: i/o timeout' >&2
    exit 1
    ;;
  reset)
    echo 'Get "https://api.github.com/x": read tcp 10.0.0.2:51234->140.82.114.6:443: read: connection reset by peer' >&2
    exit 1
    ;;
  dns)
    echo 'Get "https://api.github.com/x": dial tcp: lookup api.github.com: no such host' >&2
    exit 1
    ;;
  403-retry:*)
    headers "403 Forbidden" "Retry-After: ${outcome#403-retry:}"
    echo '{"message":"You have exceeded a secondary rate limit."}'
    echo 'gh: You have exceeded a secondary rate limit. (HTTP 403)' >&2
    exit 1
    ;;
  403-limit)
    headers "403 Forbidden" "X-Ratelimit-Remaining: 0"
    echo '{"message":"API rate limit exceeded."}'
    echo 'gh: API rate limit exceeded. (HTTP 403)' >&2
    exit 1
    ;;
  [0-9][0-9][0-9])
    headers "${outcome} Status" "X-Ratelimit-Remaining: 4999"
    echo '{"message":"An answer"}'
    echo "gh: An answer (HTTP ${outcome})" >&2
    exit 1
    ;;
esac
echo "fake gh: no such outcome '${outcome}'" >&2
exit 3
"""

FAKE_SLEEP = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_SLEEP_LOG}"
"""

# What each script's first read gets on success, shaped so that nothing after
# it needs another answer from GitHub.
CLOSING_ANSWER = {
    "data": {
        "repository": {
            "pullRequest": {
                "number": int(PR),
                "title": "A fact-style headline",
                "body": "Nothing named.",
                "closingIssuesReferences": {"totalCount": 0, "nodes": []},
                "commits": {"totalCount": 1, "nodes": [{"commit": {"message": "one commit"}}]},
                "labels": {"totalCount": 1, "nodes": [{"name": "ready"}]},
            }
        }
    }
}
PULL_ANSWER = {"number": int(PR), "body": "No dependency."}
MERGED_ANSWER = {
    "data": {
        "repository": {
            "defaultBranchRef": {"name": "main"},
            "pullRequest": {
                "number": int(PR),
                "merged": True,
                "baseRefName": "main",
                "closingIssuesReferences": {"totalCount": 0, "nodes": []},
            },
        }
    }
}


@pytest.fixture
def run(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, text in (("gh", FAKE_GH), ("sleep", FAKE_SLEEP)):
        tool = bindir / name
        tool.write_text(text)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)

    def go(script: str, plan: list[str], body: dict | str, *, delays: str | None = None):
        (tmp_path / "plan").write_text("\n".join(plan) + "\n")
        (tmp_path / "body.json").write_text(body if isinstance(body, str) else json.dumps(body))
        for name in ("gh.log", "sleep.log", "summary.md"):
            (tmp_path / name).write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": THIS_REPO,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "NO_COLOR": "1",
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "FAKE_GH_PLAN": str(tmp_path / "plan"),
            "FAKE_GH_BODY": str(tmp_path / "body.json"),
            "FAKE_SLEEP_LOG": str(tmp_path / "sleep.log"),
        }
        if delays is not None:
            env["SWARM_GH_RETRY_DELAYS"] = delays
        proc = subprocess.run(
            ["bash", str(SCRIPTS / script), "--pr", PR],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        calls = [line for line in (tmp_path / "gh.log").read_text().splitlines() if line.startswith("api ")]
        sleeps = (tmp_path / "sleep.log").read_text().split()
        return proc, calls, sleeps

    return go


# ---------------------------------------------------------------------------
# check-closing-references.sh: the read that failed on 2026-11-04
# ---------------------------------------------------------------------------

CLOSING = "check-closing-references.sh"
CLOSING_FAILED = f"could not read #{PR}'s text and closing references from {THIS_REPO}"


def test_a_timeout_then_an_answer_passes(run):
    """The 2026-11-04 case. MUTATION: call `gh api graphql` once, as before."""
    proc, calls, sleeps = run(CLOSING, ["timeout", "ok"], CLOSING_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 2 and all(c.startswith("api graphql") for c in calls), calls
    assert sleeps == ["5"]
    assert "i/o timeout on attempt 1 of 4; retry 1 of 3 in 5s" in proc.stderr, proc.stderr
    assert proc.stdout == ""


def test_four_timeouts_fail_closed_with_the_old_message(run):
    """Fail closed stays the rule: a read that never succeeds still blocks.
    MUTATION: retry forever, or pass once the retries run out."""
    proc, calls, sleeps = run(CLOSING, ["timeout"], CLOSING_ANSWER)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls) == 4, calls
    assert sleeps == ["5", "15", "30"]
    for n, wait in ((1, 5), (2, 15), (3, 30)):
        assert f"on attempt {n} of 4; retry {n} of 3 in {wait}s" in proc.stderr, proc.stderr
    assert "on attempt 4 of 4; giving up" in proc.stderr, proc.stderr
    assert CLOSING_FAILED in proc.stderr, proc.stderr
    assert "i/o timeout" in proc.stderr


@pytest.mark.parametrize("answer", ["404", "401", "403", "422"])
def test_an_answer_fails_at_once(run, answer):
    """A 404, a 401 or a 403 without a rate-limit header is GitHub answering;
    asking again gets the same answer. MUTATION: retry every failure."""
    proc, calls, sleeps = run(CLOSING, [answer, "ok"], CLOSING_ANSWER)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls) == 1, calls
    assert sleeps == []
    assert CLOSING_FAILED in proc.stderr, proc.stderr
    assert f"(HTTP {answer})" in proc.stderr, proc.stderr
    assert "retry" not in proc.stderr


@pytest.mark.parametrize("blip", ["reset", "dns", "502", "503", "504", "429", "403-limit"])
def test_every_transient_failure_is_asked_again(run, blip):
    """MUTATION: drop any one of the transient classes."""
    proc, calls, sleeps = run(CLOSING, [blip, "ok"], CLOSING_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 2, calls
    assert sleeps == ["5"]


def test_a_secondary_rate_limit_waits_its_retry_after(run):
    """MUTATION: ignore Retry-After and wait only the backoff step."""
    proc, calls, sleeps = run(CLOSING, ["403-retry:45", "ok"], CLOSING_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 2, calls
    assert sleeps == ["45"]
    assert "rate limited (HTTP 403) on attempt 1 of 4; retry 1 of 3 in 45s" in proc.stderr, proc.stderr


def test_a_retry_after_shorter_than_the_step_waits_the_step(run):
    proc, _calls, sleeps = run(CLOSING, ["timeout", "403-retry:2", "ok"], CLOSING_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sleeps == ["5", "15"]


def test_a_huge_retry_after_is_capped(run):
    """A CI step stays bounded. MUTATION: drop the cap."""
    proc, _calls, sleeps = run(CLOSING, ["403-retry:3600", "ok"], CLOSING_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sleeps == ["120"]


def test_a_retried_success_reads_the_body_alone(run):
    """--include puts the status line and headers in front of the body; the
    script must read the body alone. MUTATION: write gh's output whole."""
    proc, _calls, _sleeps = run(
        CLOSING, ["timeout", "ok"], {**CLOSING_ANSWER, "data": {"repository": {"pullRequest": None}}}
    )
    assert proc.returncode == 1
    assert f"has no pull request #{PR} in the answer" in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# The other two scripts' reads fail the same way and are retried the same way
# ---------------------------------------------------------------------------


def test_check_pr_dependencies_retries_its_read(run):
    """MUTATION: leave check-pr-dependencies.sh on a bare `gh api`."""
    proc, calls, sleeps = run("check-pr-dependencies.sh", ["timeout", "502", "ok"], PULL_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 3, calls
    assert sleeps == ["5", "15"]


def test_check_pr_dependencies_still_fails_closed(run):
    proc, calls, sleeps = run("check-pr-dependencies.sh", ["timeout"], PULL_ANSWER)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls) == 4 and sleeps == ["5", "15", "30"]
    assert f"could not read #{PR}'s body from {THIS_REPO}" in proc.stderr, proc.stderr


def test_check_pr_dependencies_does_not_retry_a_404(run):
    proc, calls, sleeps = run("check-pr-dependencies.sh", ["404", "ok"], PULL_ANSWER)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls) == 1 and sleeps == []


def test_close_merged_issues_retries_its_read(run):
    """MUTATION: leave close-merged-issues.sh on a bare `gh api graphql`."""
    proc, calls, sleeps = run("close-merged-issues.sh", ["dns", "ok"], MERGED_ANSWER)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 2, calls
    assert sleeps == ["5"]


def test_close_merged_issues_still_fails_closed(run):
    proc, calls, sleeps = run("close-merged-issues.sh", ["504"], MERGED_ANSWER)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert len(calls) == 4 and sleeps == ["5", "15", "30"]
    assert f"could not read #{PR}'s closing references from {THIS_REPO}" in proc.stderr, proc.stderr


def test_the_waits_can_be_shortened(run):
    proc, calls, sleeps = run(CLOSING, ["timeout"], CLOSING_ANSWER, delays="1 2")
    assert proc.returncode == 1
    assert len(calls) == 3 and sleeps == ["1", "2"]
    assert "on attempt 3 of 3; giving up" in proc.stderr, proc.stderr


def test_check_closing_references_retries_its_per_reference_read(run):
    """Whether #840 is an open pull request is a read too."""
    answer = json.loads(json.dumps(CLOSING_ANSWER))
    answer["data"]["repository"]["pullRequest"]["body"] = "fixes #840"
    item = json.dumps({"number": 840, "state": "open", "pull_request": {"merged_at": None}})
    proc, calls, sleeps = run(CLOSING, ["ok", "503", f"ok:{item}"], answer)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert [c.split()[1] for c in calls] == ["graphql", f"repos/{THIS_REPO}/issues/840", f"repos/{THIS_REPO}/issues/840"]
    assert sleeps == ["5"]
    assert "open pull request #840" in proc.stdout


def test_check_closing_references_still_reads_a_404_reference_as_nothing(run):
    """The helper keeps gh's stderr for the caller: a 404 still passes."""
    answer = json.loads(json.dumps(CLOSING_ANSWER))
    answer["data"]["repository"]["pullRequest"]["body"] = "fixes #99999"
    proc, calls, sleeps = run(CLOSING, ["ok", "404"], answer)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 2 and sleeps == []


def test_close_merged_issues_retries_its_pull_request_check(run):
    """The REST read that says whether a reference is a pull request; the
    close after it is a write and is made once."""
    answer = json.loads(json.dumps(MERGED_ANSWER))
    answer["data"]["repository"]["pullRequest"]["closingIssuesReferences"] = {
        "totalCount": 1,
        "nodes": [
            {
                "__typename": "Issue",
                "number": 124,
                "state": "OPEN",
                "url": f"https://github.com/{THIS_REPO}/issues/124",
                "repository": {"nameWithOwner": THIS_REPO},
            }
        ],
    }
    proc, calls, sleeps = run("close-merged-issues.sh", ["ok", "timeout", "ok:issue"], answer)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(calls) == 3, calls
    assert sleeps == ["5"]
