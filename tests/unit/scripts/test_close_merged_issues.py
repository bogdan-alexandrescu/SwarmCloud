"""A merge the swarmcloud-merge App makes closes the issues its keywords name (#621).

GitHub parses a merged pull request's closing keywords -- its GraphQL
`closingIssuesReferences` lists them -- yet of 33 issues named by pull requests
the App merged since 2026-10-01, 28 stayed open, while owner merges closed 77
of 77 (history analysis, 2026-10-05). Owner decision, 2026-10-05: close them
explicitly after the merge, like the workflow `merge` step does (#581), with
no change to the App's permissions.

`scripts/close-merged-issues.sh` is what `auto-merge.yml`'s `close-issues` job
runs on a merged pull request. These tests RUN it against a fake `gh` that
answers the GraphQL query from a fixture and records every call, so the
parsing and the close decision are exercised, not read:

* `Closes #N` (an OPEN closing reference in this repository) is closed, with a
  comment naming the merging pull request;
* `part of #N` is never closed: GitHub does not list it as a closing
  reference, and the script reads nothing else -- never the body;
* an already-closed reference is left alone, and so is one in another
  repository;
* an unreadable answer, an unmerged pull request, or a merge into a branch
  other than the default one closes nothing, and the first two fail loudly.

The job's own shape (trigger, permissions, checkout) is held in
`test_auto_merge_workflow.py`. Nothing here touches the network.
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

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "close-merged-issues.sh"
THIS_REPO = "bogdan-alexandrescu/SwarmCloud"

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


def test_the_script_reads_only_githubs_closing_references_never_the_body():
    """`part of #N` must never close N. The script cannot get that wrong by
    parsing keywords itself if it parses none: it asks GitHub.
    MUTATION: read `body` in the query and grep it for `#[0-9]+`."""
    text = SCRIPT.read_text()
    assert "closingIssuesReferences" in text
    assert not re.search(r"\bbody\b(?!-file)", re.sub(r"#.*", "", text)), "the script reads a body"


# ---------------------------------------------------------------------------
# Running it against a fake gh
# ---------------------------------------------------------------------------

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "${1:-} ${2:-}" == "api graphql" ]]; then
  cat "${FAKE_GH_GRAPHQL}"
  exit "${FAKE_GH_GRAPHQL_EXIT:-0}"
fi
if [[ "${1:-} ${2:-}" == "issue close" ]]; then
  number="$3"
  shift 3
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --comment) printf '#%s: %s\n' "${number}" "$2" >> "${FAKE_GH_COMMENTS}"; shift 2 ;;
      *) shift ;;
    esac
  done
  case " ${FAKE_GH_REFUSE:-} " in
    *" ${number} "*) echo "fake gh: refused to close #${number}" >&2; exit 1 ;;
  esac
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


def _node(number: int, state: str = "OPEN", repository: str = THIS_REPO) -> dict:
    return {"number": number, "state": state, "repository": {"nameWithOwner": repository}}


def _answer(
    nodes: list[dict],
    *,
    merged: bool = True,
    base: str = "main",
    default: str = "main",
    total: int | None = None,
) -> dict:
    return {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": default},
                "pullRequest": {
                    "number": 4242,
                    "merged": merged,
                    "baseRefName": base,
                    "closingIssuesReferences": {
                        "totalCount": len(nodes) if total is None else total,
                        "nodes": nodes,
                    },
                },
            }
        }
    }


@pytest.fixture
def run_close(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(answer: dict | str, *, pr: str = "4242", refuse: str = "", graphql_exit: int = 0):
        graphql = tmp_path / "graphql.json"
        graphql.write_text(answer if isinstance(answer, str) else json.dumps(answer))
        log = tmp_path / "gh.log"
        comments = tmp_path / "comments.txt"
        summary = tmp_path / "summary.md"
        for path in (log, comments, summary):
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
            "FAKE_GH_COMMENTS": str(comments),
            "FAKE_GH_REFUSE": refuse,
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--pr", pr],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        closes = re.findall(r"^issue close (\d+)", log.read_text(), re.MULTILINE)
        return proc, [int(n) for n in closes], log.read_text(), comments.read_text(), summary.read_text()

    return run


def test_closes_keyword_references_are_closed_with_a_comment_naming_the_pr(run_close):
    """The control: without it, every 'closes nothing' case below could be a
    script that closes nothing at all."""
    proc, closed, calls, comments, summary = run_close(_answer([_node(124), _node(125)]))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert closed == [124, 125], calls
    for number in (124, 125):
        assert re.search(rf"^#{number}: .*#4242\b", comments, re.MULTILINE), comments
    assert f"--repo {THIS_REPO}" in calls, calls
    assert "--reason completed" in calls, calls
    assert "#124" in summary and "#125" in summary, summary


def test_the_graphql_query_asks_for_this_pull_requests_closing_references(run_close):
    proc, _closed, calls, _comments, _summary = run_close(_answer([]))
    assert proc.returncode == 0, proc.stderr
    graphql = [line for line in calls.splitlines() if line.startswith("api graphql")]
    assert len(graphql) == 1, calls
    assert "closingIssuesReferences" in graphql[0]
    assert "owner=bogdan-alexandrescu" in graphql[0] and "name=SwarmCloud" in graphql[0], graphql[0]
    assert "number=4242" in graphql[0], graphql[0]


def test_a_part_of_reference_is_never_closed(run_close):
    """A body saying `part of #503` gives GitHub no closing reference, so the
    answer lists none; the script closes nothing and says so.
    MUTATION: fall back to the body's `#N` mentions when the list is empty."""
    proc, closed, calls, comments, summary = run_close(_answer([]))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert closed == [], calls
    assert comments == ""
    assert "no closing" in (summary + proc.stderr).lower(), summary + proc.stderr


def test_closes_and_part_of_in_one_pull_request_close_only_the_closes(run_close):
    """`Closes #124, part of #503`: GitHub lists only 124."""
    proc, closed, calls, _comments, _summary = run_close(_answer([_node(124)]))
    assert proc.returncode == 0, proc.stderr
    assert closed == [124], calls
    assert "503" not in calls


def test_an_already_closed_reference_is_left_alone(run_close):
    """An owner merge, or GitHub itself, may already have closed it: a second
    comment and close would be noise.
    MUTATION: drop the state check."""
    proc, closed, calls, comments, summary = run_close(
        _answer([_node(89, "CLOSED"), _node(90), _node(91, "CLOSED")])
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert closed == [90], calls
    assert "#89" not in comments and "#91" not in comments, comments
    assert "already closed" in summary.lower() and "#89" in summary, summary


def test_a_reference_in_another_repository_is_left_alone(run_close):
    """The job's token reaches this repository only; another repository's
    issue is recorded, not closed.
    MUTATION: drop the repository comparison."""
    proc, closed, calls, _comments, summary = run_close(
        _answer([_node(7, "OPEN", "someone-else/elsewhere"), _node(8)])
    )
    assert proc.returncode == 0, proc.stderr
    assert closed == [8], calls
    assert "someone-else/elsewhere#7" in summary, summary


def test_the_repository_comparison_ignores_case(run_close):
    proc, closed, _calls, _comments, _summary = run_close(
        _answer([_node(12, "OPEN", THIS_REPO.lower())])
    )
    assert proc.returncode == 0, proc.stderr
    assert closed == [12]


def test_an_unmerged_pull_request_closes_nothing_and_fails(run_close):
    """A closed-without-merge pull request's keywords mean nothing.
    MUTATION: drop the `merged` check."""
    proc, closed, calls, _comments, _summary = run_close(_answer([_node(124)], merged=False))
    assert proc.returncode != 0
    assert closed == [], calls
    assert "not merged" in proc.stderr.lower(), proc.stderr


def test_a_merge_into_a_branch_other_than_the_default_closes_nothing(run_close):
    """GitHub's closing keywords act only on a merge into the default branch."""
    proc, closed, calls, _comments, summary = run_close(_answer([_node(124)], base="release-1"))
    assert proc.returncode == 0, proc.stderr
    assert closed == [], calls
    assert "release-1" in (summary + proc.stderr), summary + proc.stderr


@pytest.mark.parametrize(
    "answer",
    [
        {"errors": [{"message": "Could not resolve to a PullRequest with the number of 4242."}]},
        {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequest": None}}},
        {"data": None},
        "this is not json",
    ],
    ids=["graphql-errors", "no-pull-request", "no-data", "not-json"],
)
def test_an_unreadable_answer_fails_rather_than_reading_as_nothing_to_close(run_close, answer):
    """Empty is not success: an error must not look like a pull request with
    no closing references.
    MUTATION: treat a missing `closingIssuesReferences` as `[]`."""
    proc, closed, _calls, _comments, _summary = run_close(answer)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert closed == []


def test_a_failed_graphql_call_fails(run_close):
    proc, closed, _calls, _comments, _summary = run_close(_answer([_node(124)]), graphql_exit=1)
    assert proc.returncode != 0
    assert closed == []


def test_one_refused_close_does_not_stop_the_others_but_fails_the_job(run_close):
    """MUTATION: stop at the first failure, or swallow it."""
    proc, closed, _calls, _comments, summary = run_close(
        _answer([_node(1), _node(2), _node(3)]), refuse="2"
    )
    assert proc.returncode != 0
    assert closed == [1, 2, 3]
    assert "#2" in (summary + proc.stderr)


def test_more_references_than_one_page_fails_after_closing_the_page(run_close):
    """GitHub links far fewer, but a truncated list is a silent loss."""
    proc, closed, _calls, _comments, _summary = run_close(_answer([_node(1)], total=101))
    assert proc.returncode != 0
    assert closed == [1]
    assert "101" in proc.stderr, proc.stderr


@pytest.mark.parametrize("pr", ["", "0", "12a", "$(id)", "-1"])
def test_a_pull_request_number_that_is_not_a_number_is_refused(run_close, pr: str):
    proc, closed, calls, _comments, _summary = run_close(_answer([_node(1)]), pr=pr)
    assert proc.returncode != 0
    assert closed == [] and "api graphql" not in calls, calls
