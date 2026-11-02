"""A leg whose captures never got past sign-in is reported as not reviewed (part of #2688).

On 2026-11-02 a visual QA run on an own-login target captured only the app's
"Couldn't check your session" card, because the live gate refused the app's
token request to its sign-in provider; the review, report and file steps
read green and filed a finding about the card. The capture's manifest now
says `reviewable` (swarm_qa's `sign_in_blocked`). Held here, as properties of
each rendered prompt and of `validate_findings`:

* the capture step opens its answer with NOT REVIEWABLE when the tool says so;
* the review step opens no image of such a capture and writes no finding;
* the report marks the leg `reviewed: false` with the manifests' reason;
* the file step files nothing from a report marked not reviewed, and
  `validate_findings` accepts nothing from it.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qaspec


def _target() -> dict[str, Any]:
    return {
        "id": "qat_3f9a", "revision": 4, "name": "Studio", "tenant_id": "tenant-a",
        "base_url": "https://app.example.com", "repository": "https://github.com/example-org/studio",
        "sign_in": {"kind": "none"}, "screens": [{"id": "home", "path": "/"}], "flows": [],
        "environment": "production",
    }


def _prompts() -> dict[str, str]:
    return {s["step_id"]: s["prompt"] for s in qaspec.render(_target())["steps"]}


def test_the_capture_prompt_says_a_not_reviewable_capture_first():
    capture = _prompts()["capture-g1"]
    # MUTATION: drop NOT_REVIEWABLE_CAPTURE from _capture_prompt and the capture step's answer reads clean.
    assert "`reviewable`" in capture
    assert "NOT REVIEWABLE:" in capture and "`not_reviewable`" in capture


def test_the_review_prompt_reviews_nothing_of_a_not_reviewable_capture():
    review = _prompts()["review-g1"]
    # MUTATION: drop NOT_REVIEWABLE_REVIEW and the sign-in card is reviewed as the app.
    assert "`reviewable`" in review
    tail = review.split("`reviewable`", 1)[1]
    assert "open no image" in tail and "write no finding" in tail
    assert '"reviewed\\": 0' in tail or '"reviewed": 0' in tail
    assert "sign_in_blocked.why" in tail and "findings-g1.json" in tail


def test_the_report_prompt_marks_the_leg_not_reviewed():
    report = _prompts()["report"]
    # MUTATION: drop _not_reviewable_report_line and the report merges the card's findings as the app's.
    assert "`reviewable`" in report
    assert '"reviewed": false' in report or '\\"reviewed\\": false' in report
    assert "Not reviewed:" in report and "sign_in_blocked.why" in report
    # Read before the gate's refusals open the report.
    assert report.index("`reviewable`") < report.index("Open report.md with the gate's refusals")


def test_the_file_prompt_files_nothing_from_a_report_not_reviewed():
    file_step = _prompts()["file"]
    # MUTATION: drop the not-reviewed sentence from _file_prompt and the card is filed as the app's defect.
    assert "`reviewed` is false" in file_step
    head = file_step.split("`reviewed` is false", 1)[1]
    assert "file and comment nothing" in head and "Not reviewed:" in head


def _finding() -> dict[str, Any]:
    return {**qaspec.FINDING_EXAMPLE}


def test_a_report_marked_not_reviewed_accepts_no_finding():
    reason = "the live gate refused the sign-in provider's token request"
    document = {"format": 1, "reviewed": False, "not_reviewed": reason, "findings": [_finding()],
                "about_run": [{"id": "run-1", "title": "Not reviewed", "actual": reason}]}
    check = qaspec.validate_findings(document)
    # MUTATION: ignore `reviewed: false` in validate_findings and the card's finding is filed.
    assert check.accepted == []
    assert check.not_reviewed == reason
    assert [r["id"] for r in check.refused] == [_finding()["id"]]
    assert qaspec.NOT_REVIEWED in check.refused[0]["problems"]
    assert check.about_run and check.about_run[0]["title"] == "Not reviewed"


def test_a_report_not_reviewed_with_no_reason_still_says_why():
    check = qaspec.validate_findings({"format": 1, "reviewed": False, "findings": []})
    assert check.not_reviewed == qaspec.NOT_REVIEWED


@pytest.mark.parametrize("reviewed", [True, None])
def test_a_reviewed_report_is_split_as_before(reviewed):
    document = {"format": 1, "findings": [_finding()]}
    if reviewed is not None:
        document["reviewed"] = reviewed
    check = qaspec.validate_findings(document)
    assert [f["id"] for f in check.accepted] == [_finding()["id"]]
    assert check.not_reviewed is None
