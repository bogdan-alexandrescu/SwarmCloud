"""A merge train red on a code-scanning result (issue 377).

On 2026-11-24 two trains were red on one check alone: `Trivy`, the check run
GitHub's code scanning (app `github-advanced-security`) creates after the
security workflow uploads its results, concluded `failure` with the title
"2 new alerts including 2 high severity security vulnerabilities" and two
annotations naming one file of one pull request on the train. Every Actions
job was green. The train said rule 1's retry "could not run", bisected over
three cycles, and its comments named the check with nothing to act on.

What the train does now, and what these tests hold:

1. every failing check run is named, whichever app made it, with its title;
2. a failing code-scanning check's annotations are read, each annotated path
   is mapped to the pull requests on the train that change it (their files,
   read page by page and bounded), and those are ejected directly, each told
   the alert's rule, file and line; a mapping that names no pull request, or
   every one on the train, bisects as before and still names the check;
3. rule 1 says the check is not an Actions job instead of "the retry could
   not run";
4. this file, with a fake check-runs answer.

Never weakened: a train with any failing check never lands, whatever the
mapping says.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import json

from .test_merge_train import (REQUIRED, REQUIRED_TICK, SCRIPT, TRAIN_PR, UNIT, RedTick, _at, _main,
                               _required_file, _run)

TRIVY = "Trivy"
TRIVY_TITLE = "2 new alerts including 2 high severity security vulnerabilities"
TRIVY_ID = 5150
MANIFEST = "deploy/k8s/app.yaml"


def _alert(path: str, line: int, rule: str) -> dict:
    return {"path": path, "start_line": line, "end_line": line, "annotation_level": "failure",
            "title": rule, "message": f"{rule}: a container adds a capability the scanner rates high",
            "raw_details": ""}


ALERTS = [_alert(MANIFEST, 12, "KSV-0005"), _alert(MANIFEST, 30, "KSV-0022")]


def _judge_rows(tmp_path: Path, check_runs: list[dict], statuses: list[dict] | None = None) -> list[list[str]]:
    cr = tmp_path / "check-runs.json"
    cr.write_text(json.dumps({"total_count": len(check_runs), "check_runs": check_runs}))
    st = tmp_path / "status.json"
    st.write_text(json.dumps({"state": "pending", "statuses": statuses or []}))
    proc = _run("judge", "--check-runs", str(cr), "--statuses", str(st),
                "--required", str(_required_file(tmp_path, REQUIRED)))
    assert proc.returncode == 0, proc.stderr
    return [line.split("\t") for line in proc.stdout.splitlines() if line]


def _code_scanning_run(title: str = TRIVY_TITLE) -> dict:
    """The check run code scanning makes: not an Actions job, no Actions run."""
    return {"id": TRIVY_ID, "name": TRIVY, "status": "completed", "conclusion": "failure",
            "html_url": f"https://example.invalid/runs/{TRIVY_ID}",
            "details_url": "https://example.invalid/security/code-scanning",
            "check_suite": {"id": 5151}, "app": {"slug": "github-advanced-security"},
            "output": {"title": title, "summary": "", "annotations_count": 0}}


def _red_on_code_scanning(t: RedTick, alerts: list[dict], files: dict[int, list[str]],
                          failing: list[str] = ()) -> None:
    at = _at(REQUIRED_TICK, list(failing))
    at["check_runs"].append(_code_scanning_run())
    t.world["checks"][t.head] = at
    t.world["annotations"] = {str(TRIVY_ID): alerts}
    t.world["files"] = {str(n): names for n, names in files.items()}
    _main(t, [*REQUIRED_TICK, TRIVY, UNIT])


@pytest.fixture
def train(tmp_path: Path):
    return lambda members, suspects="": RedTick(tmp_path, members, suspects)


# --------------------------------------------------------------------------
# 1. the judge names every failing check run with its title
# --------------------------------------------------------------------------


def test_the_judge_names_a_failing_code_scanning_check_with_its_title_app_and_id(tmp_path):
    rows = _judge_rows(tmp_path, [
        {"name": "ci-gate", "status": "completed", "conclusion": "success", "html_url": "u",
         "app": {"slug": "github-actions"}},
        {"name": "secret scan", "status": "completed", "conclusion": "success", "html_url": "u"},
        _code_scanning_run(),
    ])
    assert ["verdict", "red"] in rows
    failed = [row for row in rows if row[0] == "failed"]
    # MUTATION: drop the title, app or id columns -> nothing reads the alert.
    assert failed == [["failed", TRIVY, f"https://example.invalid/runs/{TRIVY_ID}", TRIVY_TITLE,
                       "github-advanced-security", str(TRIVY_ID)]]


def test_the_judge_reads_a_failed_commit_status_as_no_actions_job(tmp_path):
    rows = _judge_rows(tmp_path, [{"name": "ci-gate", "status": "completed", "conclusion": "success",
                                   "html_url": "u"}],
                       statuses=[{"context": "secret scan", "state": "error", "target_url": "https://x/y",
                                  "description": "a leaked key"}])
    row = next(line for line in rows if line[0] == "failed")
    assert row[1:] == ["secret scan", "https://x/y", "a leaked key", "(commit status)", ""]


# --------------------------------------------------------------------------
# 2 and 3. the tick: eject the pull request the alert names, or bisect
# --------------------------------------------------------------------------


def test_a_code_scanning_red_ejects_the_pull_request_whose_file_the_alert_names(train):
    t = train([101, 102, 103])
    _red_on_code_scanning(t, ALERTS, {101: ["a.py"], 102: ["README.md", MANIFEST], 103: ["b.py"]})
    out = t.run()
    # Rule 1: the check is not an Actions job; nothing re-run, no retry spent.
    assert not t.after.get("reruns")
    assert t.recorded("retry") is None
    assert "not an Actions job" in out
    assert "the retry could not run" not in out
    # Ejected directly: only #102, never a bisection.
    assert t.ejected() == ["issues/102/labels/ready"]
    told = t.comments(102)
    for want in (TRIVY, "KSV-0005", "KSV-0022", f"{MANIFEST}:12", f"{MANIFEST}:30"):
        assert want in told, want
    assert not t.comments(101) and not t.comments(103)
    assert TRAIN_PR in t.after.get("closed_now", [])
    closed = t.comments()
    assert "Bisecting" not in closed
    assert TRIVY in closed and TRIVY_TITLE in closed
    assert "#101, #103" in closed, "the rest boards the next train"
    assert f"{TRIVY} ({TRIVY_TITLE})" in out, "the log names the check on one line"


def test_a_code_scanning_red_without_annotations_bisects_and_names_the_check(train):
    t = train([101, 102, 103, 104])
    _red_on_code_scanning(t, [], {101: ["a.py"], 102: [MANIFEST], 103: ["b.py"], 104: ["c.py"]})
    out = t.run()
    assert not t.after.get("reruns")
    assert "not an Actions job" in out
    assert not t.ejected()
    closed = t.comments()
    assert "Bisecting" in closed
    assert TRIVY in closed and TRIVY_TITLE in closed
    assert "rule 3 (bisect)" in out


def test_an_alert_on_a_file_no_pull_request_changes_bisects(train):
    t = train([101, 102])
    _red_on_code_scanning(t, ALERTS, {101: ["a.py"], 102: ["b.py"]})
    t.run()
    assert not t.ejected()
    assert "Bisecting" in t.comments() and TRIVY_TITLE in t.comments()


def test_an_alert_on_a_file_every_pull_request_changes_bisects(train):
    t = train([101, 102])
    _red_on_code_scanning(t, ALERTS, {101: [MANIFEST], 102: [MANIFEST, "b.py"]})
    t.run()
    assert not t.ejected()
    assert "Bisecting" in t.comments() and TRIVY in t.comments()


def test_a_train_of_one_the_alert_names_is_ejected_by_rule_3_and_told_the_alert(train):
    t = train([101])
    _red_on_code_scanning(t, ALERTS, {101: [MANIFEST]})
    t.run()
    assert t.ejected() == ["issues/101/labels/ready"]
    assert TRIVY in t.comments(101) and f"{MANIFEST}:12" in t.comments(101)


def test_the_files_of_a_pull_request_are_read_past_the_first_page(train):
    t = train([101, 102, 103])
    many = [f"src/f{i}.py" for i in range(140)] + [MANIFEST]
    _red_on_code_scanning(t, ALERTS, {101: ["a.py"], 102: many, 103: ["b.py"]})
    t.run()
    assert t.ejected() == ["issues/102/labels/ready"]
    log = (t.tmp / "gh.log").read_text()
    assert "pulls/102/files?per_page=100&page=2" in log


def test_annotations_are_read_past_the_first_page(train):
    t = train([101, 102, 103])
    alerts = [_alert("a.py", i, "KSV-0001") for i in range(1, 51)] + [_alert(MANIFEST, 7, "KSV-0005")]
    # 101 changes a.py too: only the second page's alert singles out #102 as well.
    _red_on_code_scanning(t, alerts, {101: ["a.py"], 102: [MANIFEST], 103: ["b.py"]})
    t.run()
    assert sorted(t.ejected()) == ["issues/101/labels/ready", "issues/102/labels/ready"]
    assert f"{MANIFEST}:7" in t.comments(102)


def test_a_code_scanning_red_beside_a_failed_actions_job_still_retries_the_job_first(train):
    t = train([101, 102])
    _red_on_code_scanning(t, ALERTS, {101: [MANIFEST], 102: ["b.py"]}, failing=[UNIT])
    out = t.run()
    failing_run = next(r["id"] for r in t.world["checks"][t.head]["runs"] if r["conclusion"] == "failure")
    assert t.after.get("reruns") == [failing_run]
    assert TRAIN_PR not in t.after.get("closed_now", [])
    assert "not an Actions job" in out


def test_a_code_scanning_red_after_the_retry_still_ejects_and_never_lands(train):
    t = train([101, 102])
    t.extra = [f"retry={int(time.time()) - 600}:{t.head}"]
    _red_on_code_scanning(t, ALERTS, {101: [MANIFEST], 102: ["b.py"]}, failing=[UNIT])
    t.run()
    assert not t.after.get("merged"), "a red train never lands"
    assert t.ejected() == ["issues/101/labels/ready"]
    assert UNIT in t.comments() and TRIVY in t.comments()


def test_an_unreadable_annotation_list_bisects(train):
    t = train([101, 102])
    _red_on_code_scanning(t, ALERTS, {101: [MANIFEST], 102: ["b.py"]})
    t.world["annotations"] = "not a list"  # the fake then fails the read
    t.run()
    assert not t.ejected()
    assert "Bisecting" in t.comments() and TRIVY in t.comments()


def test_the_script_documents_the_judge_s_new_columns():
    header = SCRIPT.read_text().split("\nset -euo pipefail\n", 1)[0]
    assert "`failed\\t<name>\\t<url>\\t<title>\\t<app>\\t<check run id>`" in header
