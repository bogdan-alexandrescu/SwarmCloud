"""The merge train says why it passes over a `ready` pull request
(MERGE-TRAIN-SAYS-WHY, part of #2688).

WHY. On 4 November 2026 the workflow page redesign's pull request was
labelled `ready` at 13:57 UTC; at 14:00 its `queue for auto-merge` check
failed on a network timeout on the self-hosted runner. The train boards only
`ready` pull requests whose checks are all green, so it passed over this one
on every tick for eight hours, logging an `info` line nobody read. Now, when a
`ready` pull request does not board for a reason that will not clear by
itself -- a failed or cancelled check at its head, a conflict, its closing
keywords -- the train comments on it ONCE per head commit and reason, under a
hidden marker, naming the check, its run link, and what clears it. A reason
that clears by itself (checks still running, a train already full) is not
said.

All offline: bash + jq, and a fake `gh` that keeps each pull request's
comments in a file, so a second tick reads what the first one wrote.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
GH_REPO = "bogdan-alexandrescu/SwarmCloud"
RUN_URL = "https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/42000000001"
FAILED_URL = "https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/41842863519/job/7"

FAKE_GH = """#!/usr/bin/env bash
set -euo pipefail
# gh api repos/<R>/issues/<N>/comments --paginate --jq '.[].body': the bodies
# kept in $FAKE_COMMENTS/<N> (none when absent; a read failure when
# $FAKE_COMMENTS/<N>.unreadable exists). gh pr comment <N> --repo <R>
# --body-file <F>: appends <F> to $FAKE_COMMENTS/<N> and logs it.
printf '%s\\n' "$*" >>"${FAKE_GH_LOG}"
if [[ "$1" == "api" ]]; then
  n="$(sed -n 's|^repos/[^/]*/[^/]*/issues/\\([0-9]*\\)/comments$|\\1|p' <<<"$2")"
  [[ -n "${n}" ]] || { echo "fake gh: unexpected: $*" >&2; exit 64; }
  [[ ! -e "${FAKE_COMMENTS}/${n}.unreadable" ]] || { echo "dial tcp: i/o timeout" >&2; exit 1; }
  [[ ! -f "${FAKE_COMMENTS}/${n}" ]] || cat "${FAKE_COMMENTS}/${n}"
  exit 0
fi
if [[ "$1 $2" == "pr comment" ]]; then
  cat "$7" >>"${FAKE_COMMENTS}/$3"
  printf 'COMMENTED %s\\n' "$3" >>"${FAKE_GH_LOG}"
  exit 0
fi
echo "fake gh: unexpected: $*" >&2
exit 64
"""


def _need(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required")


def _sha() -> str:
    return secrets.token_hex(20)


def _pr(number: int, head: str, *, rollup: list[dict], mergeable: str = "MERGEABLE",
        labels: tuple[str, ...] = ("ready",), draft: bool = False) -> dict:
    return {
        "number": number, "title": f"Change {number}", "body": "",
        "headRefOid": head, "headRefName": f"feature/{number}", "baseRefName": "main",
        "isDraft": draft, "isCrossRepository": False, "mergeable": mergeable,
        "labels": [{"name": name} for name in labels], "statusCheckRollup": rollup,
    }


def _run_check(name: str, status: str = "COMPLETED", conclusion: str = "SUCCESS",
               url: str = "https://example.invalid/run") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "completedAt": "" if status != "COMPLETED" else "2026-11-04T14:00:00Z",
            "detailsUrl": url}


def _red(head: str, number: int = 3240, conclusion: str = "FAILURE") -> dict:
    return _pr(number, head, rollup=[
        _run_check("ci-gate"),
        _run_check("queue for auto-merge", conclusion=conclusion, url=FAILED_URL),
    ])


class Train:
    """The fake GitHub one pull request list at a time is told against."""

    def __init__(self, tmp_path: Path):
        _need("bash", "jq", "awk")
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.comments = tmp_path / "comments"
        self.comments.mkdir()
        self.log = tmp_path / "gh.log"
        self.log.write_text("")

    def tell(self, prs: list[dict]) -> subprocess.CompletedProcess:
        path = self.tmp / "prs.json"
        path.write_text(json.dumps(prs))
        env = dict(os.environ)
        env.update({"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "GH_REPO": GH_REPO,
                    "RUN_URL": RUN_URL, "FAKE_GH_LOG": str(self.log),
                    "FAKE_COMMENTS": str(self.comments),
                    "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0"})
        env.pop("GITHUB_STEP_SUMMARY", None)
        proc = subprocess.run(["bash", str(SCRIPT), "tell", "--prs", str(path)],
                              capture_output=True, text=True, timeout=60, check=False, env=env)
        assert proc.returncode == 0, proc.stderr
        return proc

    def commented(self) -> list[str]:
        return [line.split()[1] for line in self.log.read_text().splitlines()
                if line.startswith("COMMENTED ")]

    def thread(self, number: int) -> str:
        path = self.comments / str(number)
        return path.read_text() if path.exists() else ""


# --------------------------------------------------------------------------
# once per head and reason
# --------------------------------------------------------------------------


def test_a_red_check_is_told_once_and_not_again_on_the_next_tick_at_the_same_head(tmp_path):
    train = Train(tmp_path)
    head = _sha()
    train.tell([_red(head)])
    # MUTATION: no telling step -> the pull request sits silent for hours,
    # as 4 November's did.
    assert train.commented() == ["3240"]
    train.tell([_red(head)])
    # MUTATION: no marker check -> every five-minute tick comments again.
    assert train.commented() == ["3240"], train.thread(3240)


def test_a_new_head_with_the_same_red_check_is_told_again(tmp_path):
    train = Train(tmp_path)
    train.tell([_red(_sha())])
    train.tell([_red(_sha())])
    # MUTATION: a marker without the head -> a fix push that fails the same
    # check again is never told.
    assert train.commented() == ["3240", "3240"]


def test_the_comment_names_the_check_its_run_and_what_clears_it(tmp_path):
    train = Train(tmp_path)
    head = _sha()
    train.tell([_red(head)])
    body = train.thread(3240)
    first, second = body.splitlines()[:2]
    assert first == f"<!-- merge-train-skip head={head} reason=check:queue_for_auto-merge -->"
    assert second == "**The merge train skips it: `queue for auto-merge` failed.**"
    assert f"[`queue for auto-merge`]({FAILED_URL}): failed" in body
    assert "re-run that job" in body and "push a fix" in body
    assert RUN_URL in body
    # A green check is not named as a reason.
    assert "ci-gate" not in body


def test_a_cancelled_check_is_told_as_cancelled(tmp_path):
    train = Train(tmp_path)
    train.tell([_red(_sha(), conclusion="CANCELLED")])
    assert "`queue for auto-merge` was cancelled" in train.thread(3240)


def test_checks_still_running_are_told_nothing(tmp_path):
    train = Train(tmp_path)
    running = _pr(3241, _sha(), rollup=[
        _run_check("ci-gate", status="IN_PROGRESS", conclusion=""),
        _run_check("queue for auto-merge", status="QUEUED", conclusion=""),
    ])
    green = _pr(3242, _sha(), rollup=[_run_check("ci-gate")])
    proc = train.tell([running, green])
    # MUTATION: tell every `ready` pull request not boarding -> a pull request
    # whose CI is merely running is told something that clears by itself.
    assert train.commented() == [], proc.stderr
    assert not any(line.startswith("api ") for line in train.log.read_text().splitlines())


def test_a_conflict_is_told_once_per_head(tmp_path):
    train = Train(tmp_path)
    head = _sha()
    conflicting = _pr(3243, head, rollup=[_run_check("ci-gate")], mergeable="CONFLICTING")
    train.tell([conflicting])
    train.tell([conflicting])
    assert train.commented() == ["3243"]
    assert train.thread(3243).splitlines()[1] == "**The merge train skips it: it conflicts with main.**"


def test_a_pull_request_the_train_does_not_serve_is_told_nothing(tmp_path):
    train = Train(tmp_path)
    unlabelled = _pr(3244, _sha(), rollup=[_run_check("x", conclusion="FAILURE")], labels=())
    draft = _pr(3245, _sha(), rollup=[_run_check("x", conclusion="FAILURE")], draft=True)
    train.tell([unlabelled, draft])
    assert train.commented() == []


def test_an_unreadable_thread_is_not_commented_on(tmp_path):
    train = Train(tmp_path)
    (train.comments / "3240.unreadable").write_text("")
    proc = train.tell([_red(_sha())])
    # Never said twice: a thread the train cannot read may already hold it.
    assert train.commented() == []
    assert "could not read #3240's comments" in proc.stderr


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}\n", start)]


def test_the_tick_tells_before_it_boards_and_never_in_a_dry_run():
    tick = _function("cmd_tick")
    listed = tick.index('list_open_prs "${WORK}/prs.json"')
    told = tick.index('tell_skipped "${WORK}/prs.json"')
    assert listed < told < tick.index("handle_train")
    assert re.search(r'if \[\[ "\$\{dry\}" != "true" \]\]; then\s+tell_skipped', tick), tick


def test_a_closing_keyword_refusal_is_told_once_but_a_read_that_could_not_tell_is_not():
    guards = _function("guards_pass")
    assert re.search(r'"\$\{rc\}" -eq 2 && "\$\{dry\}" != "true"', guards), guards
    assert "tell_skip " in guards and "closing-references" in guards
    assert 'guards_pass "${n}" "${sha}" "${dry}"' in _function("start_train")


def test_a_review_not_yet_refusal_is_told_under_its_own_reason():
    """check-closing-references.sh refuses a pull request carrying
    `review-not-yet` (owner decision, 9 November 2026), and marks its refusal
    with `<!-- review-not-yet -->`. The train tells that under its own reason
    and says what clears it. MUTATION: tell it as a closing keyword, and the
    author is told to edit a body that is not the problem."""
    guards = _function("guards_pass")
    assert re.search(r'grep -qxF -- "<!-- review-not-yet -->" "\$\{refusal\}"', guards), guards
    assert re.search(r'tell_skip "\$\{number\}" "\$\{head\}" review-not-yet', guards), guards
    tell = _function("tell_skip")
    clears = tell[tell.index("review-not-yet)"):]
    clears = clears[:clears.index(";;")]
    assert "a later review that says MERGE" in clears and "A CI fix does not" in clears
