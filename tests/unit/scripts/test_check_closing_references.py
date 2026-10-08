"""Auto-merge refuses a pull request whose text would make GitHub close another
open pull request.

MEASURED 2026-10-08. The text of the pull request merged as 857 named the
App-settings pull request (840) with a closing keyword while 840 was still
open and unmerged. At 04:16:21, two seconds after the swarmcloud-merge App
merged 857, GitHub's own keyword handling closed 840 -- not
scripts/close-merged-issues.sh, which logged no references. Owner decision,
2026-10-08: check BEFORE auto-merge is enabled.

GraphQL `closingIssuesReferences` is an IssueConnection, and it does not list a
pull request at all: the pull request merged as 817 says "fixes" before 777, a
pull request, and its `closingIssuesReferences` is empty (read 2026-10-08). So
`scripts/check-closing-references.sh` takes GitHub's list AND the closing
keywords it finds itself in the title, the body and every commit message (the
squash commit's message is the commit messages here, so a keyword in one lands
on main), and asks REST `repos/<R>/issues/<n>` of each whether it is a pull
request and whether it is open.

These tests RUN it against a fake `gh`:

* no references, or only issues, or a closed or merged pull request: exit 0;
* an open pull request, named in the body, the title or a commit message, or
  listed by GitHub: exit 2 and the refusal comment on stdout;
* an unreadable answer or a failed read: non-zero and not 2 -- fail closed.

The workflow wiring is held at the bottom. Nothing here touches the network.
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

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "check-closing-references.sh"
AUTO_MERGE = REPO / ".github" / "workflows" / "auto-merge.yml"
THIS_REPO = "bogdan-alexandrescu/SwarmCloud"
REFUSED = 2

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required"
)


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def test_the_script_has_the_house_shape():
    assert SCRIPT.is_file(), f"{SCRIPT.relative_to(REPO)} does not exist"
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.MULTILINE)


# ---------------------------------------------------------------------------
# Running it against a fake gh
# ---------------------------------------------------------------------------

# `api graphql` answers from a fixture; `api repos/<R>/issues/<n>` from
# issues/<n>.json, a missing one being GitHub's 404, and FAKE_GH_REST_FAIL
# naming a number whose read fails like a 502. Every call is logged.
FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "${1:-} ${2:-}" == "api graphql" ]]; then
  cat "${FAKE_GH_GRAPHQL}"
  exit "${FAKE_GH_GRAPHQL_EXIT:-0}"
fi
if [[ "${1:-}" == "api" ]]; then
  path=""
  for arg in "$@"; do
    case "${arg}" in repos/*) path="${arg}" ;; esac
  done
  case "${path}" in
    repos/*/issues/*)
      number="${path##*/}"
      if [[ " ${FAKE_GH_REST_FAIL:-} " == *" ${number} "* ]]; then
        echo "gh: Server Error (HTTP 502)" >&2
        exit 1
      fi
      if [[ -f "${FAKE_GH_ISSUES}/${number}.json" ]]; then
        cat "${FAKE_GH_ISSUES}/${number}.json"
        exit 0
      fi
      echo '{"message":"Not Found","status":"404"}'
      echo "gh: Not Found (HTTP 404)" >&2
      exit 1
      ;;
  esac
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


def _issue(number: int, *, state: str = "open", pull: bool = False, merged: bool = False) -> dict:
    item: dict = {"number": number, "state": state, "title": f"item {number}"}
    if pull:
        item["pull_request"] = {
            "url": f"https://api.github.com/repos/{THIS_REPO}/pulls/{number}",
            "merged_at": "2026-10-08T04:16:19Z" if merged else None,
        }
    return item


def _answer(
    *,
    title: str = "A fact-style headline",
    body: str = "",
    commits: list[str] | None = None,
    references: list[int] | None = None,
    reference_repo: str = THIS_REPO,
    references_total: int | None = None,
    commits_total: int | None = None,
) -> dict:
    refs = references or []
    messages = commits if commits is not None else ["one commit"]
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "number": 4242,
                    "title": title,
                    "body": body,
                    "closingIssuesReferences": {
                        "totalCount": len(refs) if references_total is None else references_total,
                        "nodes": [
                            {
                                "number": n,
                                "url": f"https://github.com/{reference_repo}/issues/{n}",
                                "repository": {"nameWithOwner": reference_repo},
                            }
                            for n in refs
                        ],
                    },
                    "commits": {
                        "totalCount": len(messages) if commits_total is None else commits_total,
                        "nodes": [{"commit": {"message": m}} for m in messages],
                    },
                }
            }
        }
    }


@pytest.fixture
def run_check(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    issues_dir = tmp_path / "issues"
    issues_dir.mkdir()

    def run(
        answer: dict | str,
        items: list[dict] | None = None,
        *,
        pr: str = "4242",
        graphql_exit: int = 0,
        rest_fail: str = "",
    ):
        for old in issues_dir.iterdir():
            old.unlink()
        for item in items or []:
            (issues_dir / f"{item['number']}.json").write_text(json.dumps(item))
        graphql = tmp_path / "graphql.json"
        graphql.write_text(answer if isinstance(answer, str) else json.dumps(answer))
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        for path in (log, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": THIS_REPO,
            "GITHUB_STEP_SUMMARY": str(summary),
            "NO_COLOR": "1",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_GRAPHQL": str(graphql),
            "FAKE_GH_GRAPHQL_EXIT": str(graphql_exit),
            "FAKE_GH_ISSUES": str(issues_dir),
            "FAKE_GH_REST_FAIL": rest_fail,
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--pr", pr],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        looked_up = [int(n) for n in re.findall(r"^api repos/[^ ]*/issues/(\d+)", log.read_text(), re.MULTILINE)]
        return proc, looked_up, log.read_text()

    return run


def _refusal(number: int) -> str:
    return (
        "**Not merged, and `ready` removed.** This pull request's text names open pull request "
        f"#{number} with a closing keyword (for example \"fix #{number}\"), so GitHub would close "
        f"#{number} when this merges. Reword it (for example \"PR {number}\") or use "
        f"\"part of #{number}\", then add `ready` again."
    )


def test_an_open_pull_request_named_with_a_closing_keyword_is_refused(run_check):
    """The 857 shape: GitHub lists nothing, the body says "fix" before an open
    pull request. MUTATION: read only `closingIssuesReferences`."""
    proc, looked_up, _calls = run_check(
        _answer(body="The App-settings change is not merged yet, fix #840 first."),
        [_issue(840, pull=True)],
    )
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert looked_up == [840]
    assert proc.stdout.strip() == _refusal(840), proc.stdout


def test_no_references_passes(run_check):
    """The control for the refusal above: the same script says nothing when
    nothing is named. MUTATION: refuse whenever the body mentions a number."""
    proc, looked_up, _calls = run_check(_answer(body="Nothing is closed here; see 840 for context."))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == []
    assert proc.stdout.strip() == ""


def test_a_part_of_reference_is_not_a_closing_one(run_check):
    proc, looked_up, _calls = run_check(_answer(body="part of #780, see #840"), [_issue(840, pull=True)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == []


def test_references_to_issues_only_pass(run_check):
    """Closing an issue is what the keyword is for.
    MUTATION: refuse every reference, or drop the `.pull_request` check."""
    proc, looked_up, _calls = run_check(
        _answer(body="Closes #124 and fixes #125.", references=[124, 125]),
        [_issue(124), _issue(125)],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == [124, 125]
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize(
    "item",
    [_issue(777, state="closed", pull=True, merged=True), _issue(777, state="closed", pull=True)],
    ids=["merged", "closed-unmerged"],
)
def test_a_closed_or_merged_pull_request_passes(run_check, item: dict):
    """A merged pull request cannot be closed or reopened by a keyword, and
    GitHub's keyword closes; it never reopens a closed one. Only an OPEN pull
    request loses anything. MUTATION: drop the state check."""
    proc, looked_up, _calls = run_check(_answer(body="fixes #777"), [item])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == [777]


@pytest.mark.parametrize(
    "where",
    ["title", "commit", "listed"],
)
def test_an_open_pull_request_is_found_wherever_the_keyword_is(run_check, where: str):
    """The squash commit's message is the commit messages in this repository
    (squash_merge_commit_message COMMIT_MESSAGES, read 2026-10-08), so a keyword
    in a commit lands on main and closes too. GitHub's own list counts as well.
    MUTATION: read the body only."""
    answer = {
        "title": _answer(title="Resolves #840: the settings land"),
        "commit": _answer(commits=["first", "Wire the settings\n\nCloses: #840"]),
        "listed": _answer(references=[840]),
    }[where]
    proc, looked_up, _calls = run_check(answer, [_issue(840, pull=True)])
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert looked_up == [840]
    assert "#840" in proc.stdout


@pytest.mark.parametrize(
    "text",
    [
        "CLOSES #840",
        "fixed #840",
        f"Resolved {THIS_REPO}#840",
        f"fixes https://github.com/{THIS_REPO}/pull/840",
        f"closes https://github.com/{THIS_REPO.lower()}/issues/840",
    ],
)
def test_every_closing_keyword_form_github_reads_is_read(run_check, text: str):
    proc, looked_up, _calls = run_check(_answer(body=text), [_issue(840, pull=True)])
    assert proc.returncode == REFUSED, text + "\n" + proc.stdout + proc.stderr
    assert looked_up == [840]


@pytest.mark.parametrize("text", ["prefix #840", "unfixed #840", "closest #840", "fix 840"])
def test_words_that_only_contain_a_keyword_are_not_one(run_check, text: str):
    proc, looked_up, _calls = run_check(_answer(body=text), [_issue(840, pull=True)])
    assert proc.returncode == 0, text + "\n" + proc.stdout + proc.stderr
    assert looked_up == []


def test_a_reference_in_another_repository_is_not_looked_up(run_check):
    proc, looked_up, _calls = run_check(
        _answer(body="fixes someone-else/elsewhere#840", references=[9], reference_repo="someone-else/elsewhere"),
        [_issue(840, pull=True)],
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == []


def test_its_own_number_is_not_a_reason_to_refuse(run_check):
    """Merging closes it anyway."""
    proc, looked_up, _calls = run_check(_answer(body="fixes #4242"), [_issue(4242, pull=True)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == []


def test_several_open_pull_requests_are_all_named(run_check):
    proc, looked_up, _calls = run_check(
        _answer(body="fixes #840, closes #841, closes #124"),
        [_issue(840, pull=True), _issue(841, pull=True), _issue(124)],
    )
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert sorted(looked_up) == [124, 840, 841]
    assert "#840" in proc.stdout and "#841" in proc.stdout
    assert "#124" not in proc.stdout


def test_a_number_that_names_nothing_passes(run_check):
    """GitHub closes nothing for a number that does not exist (404)."""
    proc, looked_up, _calls = run_check(_answer(body="fixes #99999"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == [99999]


# Fail closed: an error must never read as "nothing named".


@pytest.mark.parametrize(
    "answer",
    [
        {"errors": [{"message": "Could not resolve to a PullRequest with the number of 4242."}]},
        {"data": {"repository": {"pullRequest": None}}},
        {"data": None},
        {"data": {"repository": {"pullRequest": {"number": 4242, "title": "t", "body": "fixes #840"}}}},
        "this is not json",
        "",
    ],
    ids=["graphql-errors", "no-pull-request", "no-data", "no-lists", "not-json", "empty"],
)
def test_an_unreadable_answer_fails_closed(run_check, answer):
    """MUTATION: treat a missing list as empty."""
    proc, _looked_up, _calls = run_check(answer, [_issue(840, pull=True)])
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr


def test_a_failed_graphql_call_fails_closed(run_check):
    proc, _looked_up, _calls = run_check(_answer(), graphql_exit=1)
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr


def test_a_failed_rest_read_fails_closed(run_check):
    """A 502 is not a 404: whether #840 is an open pull request is unknown.
    MUTATION: treat every failed read as "not found"."""
    proc, _looked_up, _calls = run_check(_answer(body="fixes #840"), [_issue(840, pull=True)], rest_fail="840")
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr


@pytest.mark.parametrize("field", ["references_total", "commits_total"])
def test_more_than_one_page_fails_closed(run_check, field: str):
    proc, _looked_up, _calls = run_check(_answer(**{field: 101}))
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr
    assert "101" in proc.stderr


@pytest.mark.parametrize("pr", ["", "0", "12a", "$(id)", "-1"])
def test_a_pull_request_number_that_is_not_a_number_is_refused(run_check, pr: str):
    proc, looked_up, calls = run_check(_answer(), pr=pr)
    assert proc.returncode not in (0, REFUSED)
    assert "api graphql" not in calls and looked_up == []


# ---------------------------------------------------------------------------
# The workflow calls it before auto-merge is enabled
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def enable() -> dict:
    return yaml.safe_load(AUTO_MERGE.read_text())["jobs"]["enable"]


def _ids(job: dict) -> list:
    return [step.get("id") for step in job.get("steps") or []]


def test_the_enable_job_runs_the_check_from_the_default_branch_before_the_gate(enable: dict):
    """Main's script, never the pull request's: pull_request_target holds a
    write token. MUTATION: check out the head, or move the check after the merge."""
    steps = enable["steps"]
    ids = _ids(enable)
    assert "closing-references" in ids and "gate" in ids, ids
    assert ids.index("pr") < ids.index("closing-references") < ids.index("gate") < ids.index("merge"), ids
    step = steps[ids.index("closing-references")]
    assert "scripts/check-closing-references.sh" in step["run"]
    assert step.get("if") == "steps.pr.outputs.evaluate == 'true'", step.get("if")
    checkouts = [s for s in steps if "checkout" in str(s.get("uses") or "")]
    assert len(checkouts) == 1, checkouts
    assert ids.index(checkouts[0].get("id")) < ids.index("closing-references")
    inputs = checkouts[0].get("with") or {}
    assert inputs.get("ref") == "${{ github.event.repository.default_branch }}", inputs
    assert inputs.get("persist-credentials") is False, inputs
    assert inputs.get("sparse-checkout") == "scripts", inputs


def test_the_gate_refuses_on_the_checks_finding(enable: dict):
    gate = next(s for s in enable["steps"] if s.get("id") == "gate")
    assert (gate.get("env") or {}).get("CLOSING_REFUSAL") == "${{ steps.closing-references.outputs.refusal }}"
    assert "CLOSING_REFUSAL" in gate["run"]


@pytest.fixture
def run_gate_with_refusal(enable: dict, tmp_path: Path):
    """The gate's own shell with a refusal handed in, against a fake gh that
    records the label removal and the comment."""
    script = next(s for s in enable["steps"] if s.get("id") == "gate")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '%s\\n' \"$*\" >> \"${FAKE_GH_LOG}\"\n"
        "if [[ \"${1:-} ${2:-}\" == \"pr comment\" ]]; then cat \"$5\" >> \"${FAKE_GH_COMMENTS}\"; exit 0; fi\n"
        "if [[ \"$*\" == \"pr edit 4242 --remove-label ready\" ]]; then exit 0; fi\n"
        "echo \"fake gh: unexpected call: $*\" >&2\nexit 3\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(event_name: str):
        log = tmp_path / "gh.log"
        comments = tmp_path / "comments.md"
        output = tmp_path / "output.txt"
        summary = tmp_path / "summary.md"
        for path in (log, comments, output, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": THIS_REPO,
            "EVENT_NAME": event_name,
            "PR_NUMBER": "4242",
            "PR_TITLE": "A fact-style headline",
            "BASE_REF": "main",
            "HEAD_SHA": "0" * 40,
            "MERGE_APP_ID": "123456",
            "HAS_MERGE_APP_KEY": "true",
            "CLOSING_REFUSAL": _refusal(840),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_COMMENTS": str(comments),
        }
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)
        return proc, log.read_text(), comments.read_text(), output.read_text()

    return run


@pytest.mark.parametrize("event_name", ["pull_request_target", "workflow_dispatch"])
def test_a_refusal_removes_ready_comments_and_arms_nothing(run_gate_with_refusal, event_name: str):
    """Both on the label and on a re-evaluation. MUTATION: keep `ready`, or
    say `merge`."""
    proc, calls, comments, output = run_gate_with_refusal(event_name)
    assert "pr edit 4242 --remove-label ready" in calls, calls
    assert comments.strip().startswith(_refusal(840)), comments
    assert "decision=refused" in output, output
    assert "decision=merge" not in output and "api " not in calls, calls
    if event_name == "workflow_dispatch":
        # A re-evaluation may find auto-merge armed by an earlier run.
        assert proc.returncode == 0 and "disarm=true" in output, output
    else:
        assert proc.returncode != 0
