"""The swarm-git issue writes (docs/design/agent-autonomy.md §3.2; lane AU-1 GIT-TOOLS, part of #1933).

`issue_create` and `issue_comment`, through the real route, over the in-memory
Firestore, with GitHub a fake transport under the real `GitHubWriter`. The
forms are THIS repository's own `.github/ISSUE_TEMPLATE/*.yml`, put on the fake
default branch, so a form that changes is the form these tests read.

What each case pins, from §6.1's row AU-1:

* with the registration's `agents_may_file_issues` off, both tools are
  `issue_writes_off` (owner decision Q2: per registration, admin-set, off by
  default; the platform's own registration on);
* a bug report without "what you expected" is `issue_form_incomplete`, naming
  it, and nothing is filed;
* `form: "plain"` where forms exist is `issue_form_required`; where none exist
  it renders `### <name>` headings in the order given;
* a second `issue_create` with the same `dedupe_key` answers `existing` and
  files nothing;
* a label the repository lacks is dropped and named, never created;
* an 11th write in one attempt is refused, and the 31st across a task's attempts;
* a body matching a redaction rule is `leak_found`, refused rather than masked,
  and the refusal does not repeat the value.
"""

from __future__ import annotations

import json
from typing import Any

from .test_git_tools_api import (  # noqa: F401  (fixtures)
    ATTEMPT,
    PARENT,
    app,
    audit_rows,
    call,
    client,
    origin,
    running_task,
    slots,
    worker_tokens,
)
from .test_git_tools_reads import (  # noqa: F401  (forge is a fixture)
    OWN,
    credential_shaped,
    forge,
    seed_registration,
)

BUG = {
    "summary": "The pool page shows 0 when the read failed.",
    "severity": "S2",
    "surface": "API",
    "environment": "dev",
    "steps": "1. Open /capacity/pools while the API is down.",
    "expected": "An error saying the read failed, not a zero.",
}


def create(client, attempt, request_id: str = "req-1", **fields: Any):
    body = {"form": "bug_report", "title": "A zero where the read failed",
            "sections": dict(BUG), **fields}
    return call(client, attempt, "issue_create", body, request_id=request_id)


def filed_payloads(forge) -> list[dict[str, Any]]:
    return [json.loads(b) for m, u, _, b in forge.calls
            if m == "POST" and u.split("?")[0].endswith(f"/repos/{OWN}/issues")]


# --------------------------------------------------------------------------
# who may write (owner decision Q2)
# --------------------------------------------------------------------------


def test_with_the_flag_off_issue_create_is_issue_writes_off(client, db, forge):
    seed_registration(db, files=False)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = create(client, attempt)
    assert response.status_code == 422, response.text
    # MUTATION: skip the `agents_may_file_issues_of` check and the issue is filed.
    assert response.json()["code"] == "issue_writes_off"
    assert "agents_may_file_issues" in response.json()["message"]
    assert forge.posted_issues() == []


def test_a_registration_without_the_field_is_off_and_no_registration_is_off(client, db, forge):
    forge.all_forms()
    attempt, _ = running_task(client, db)
    # No registration at all.
    first = create(client, attempt, request_id="req-a")
    # MUTATION: treat a missing registration as on and this is 200.
    assert first.json()["code"] == "issue_writes_off"
    seed_registration(db)  # registered, the field never set, not platform
    second = create(client, attempt, request_id="req-b")
    assert second.json()["code"] == "issue_writes_off"
    comment = call(client, attempt, "issue_comment", {"number": 5, "body": "hi"},
                   request_id="req-c")
    assert comment.json()["code"] == "issue_writes_off"
    assert forge.writes() == []


def test_the_platforms_own_registration_is_on_without_the_field(client, db, forge):
    seed_registration(db, platform=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = create(client, attempt)
    # MUTATION: read only the stored field (not `agents_may_file_issues_of`) and this is off.
    assert response.status_code == 200, response.text
    assert response.json()["filed"] is True


# --------------------------------------------------------------------------
# the form
# --------------------------------------------------------------------------


def test_a_bug_report_without_what_you_expected_is_issue_form_incomplete(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    sections = {k: v for k, v in BUG.items() if k != "expected"}
    response = create(client, attempt, sections=sections)
    assert response.status_code == 422, response.text
    body = response.json()
    # MUTATION: stop checking the form's `validations.required` and the issue is filed.
    assert body["code"] == "issue_form_incomplete"
    assert "expected" in body["message"] and "What you expected instead" in body["message"]
    assert forge.posted_issues() == []


def test_a_blank_required_section_is_incomplete_too(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = create(client, attempt, sections={**BUG, "expected": "   "})
    # MUTATION: count a whitespace-only answer as given and this is filed.
    assert response.json()["code"] == "issue_form_incomplete"


def test_a_section_the_form_does_not_have_is_refused(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = create(client, attempt, sections={**BUG, "made_up": "x"})
    assert response.status_code == 422
    # MUTATION: ignore unknown section ids and "made_up" is silently dropped.
    assert response.json()["code"] == "issue_form_unknown_field"
    assert "made_up" in response.json()["message"]


def test_a_full_bug_report_is_filed_in_the_forms_sections_order_with_its_label(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    # Given in another order than the form's.
    sections = dict(reversed(list(BUG.items())))
    response = create(client, attempt, sections=sections)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["filed"] is True and isinstance(answer["number"], int)
    [payload] = filed_payloads(forge)
    text = payload["body"]
    headings = [line[4:] for line in text.split("\n") if line.startswith("### ")]
    # MUTATION: render in the order given instead of the form's and the headings are reversed.
    assert headings[:6] == ["What happened", "Severity", "Where you saw it", "Environment",
                            "Steps to reproduce", "What you expected instead"]
    # A dropdown answer is the option it names, in full.
    assert "S2 — wrong data on screen" in text
    # An optional field left out reads as GitHub renders it.
    assert "_No response_" in text
    # The form's label and title prefix.
    assert payload["labels"] == ["bug"]
    assert payload["title"].startswith("[bug] ")
    # The body opens with the marker naming the attempt.
    assert text.split("\n", 1)[0].startswith("<!-- swarm-agent-issue ")
    assert ATTEMPT in text.split("\n", 1)[0]


def test_a_dropdown_answer_that_is_no_option_is_refused(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = create(client, attempt, sections={**BUG, "severity": "S9"})
    assert response.status_code == 422
    # MUTATION: accept any dropdown text and "S9" is filed.
    assert response.json()["code"] == "issue_form_invalid"


def test_plain_where_forms_exist_is_issue_form_required(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "issue_create", {
        "form": "plain", "title": "x", "sections": {"What": "y"}})
    assert response.status_code == 422, response.text
    # MUTATION: accept `plain` whatever the repository has and this is filed unformed.
    assert response.json()["code"] == "issue_form_required"
    assert "bug_report" in response.json()["message"]
    assert forge.posted_issues() == []


def test_plain_in_a_repository_with_no_forms_renders_headings_in_the_order_given(
        client, db, forge):
    seed_registration(db, files=True)
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "issue_create", {
        "form": "plain", "title": "Button overlaps the footer",
        "sections": {"What happened": "It overlaps.", "Expected": "It does not.",
                     "Evidence": "console link"}})
    assert response.status_code == 200, response.text
    [payload] = filed_payloads(forge)
    headings = [line[4:] for line in payload["body"].split("\n") if line.startswith("### ")]
    # MUTATION: sort the sections and "Evidence" comes first.
    assert headings == ["What happened", "Expected", "Evidence"]
    assert payload["title"] == "Button overlaps the footer"
    assert payload["labels"] == []


def test_a_form_the_repository_does_not_have_is_refused(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    response = create(client, attempt, form="no_such_form")
    # MUTATION: fall back to plain for an unknown form and this is filed.
    assert response.json()["code"] == "issue_form_unknown"


# --------------------------------------------------------------------------
# dedupe and retries
# --------------------------------------------------------------------------


def test_a_second_issue_create_with_the_same_dedupe_key_answers_existing(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    first = create(client, attempt, request_id="req-a", dedupe_key="qa:target-1:footer")
    assert first.status_code == 200, first.text
    number = first.json()["number"]
    second = create(client, attempt, request_id="req-b", dedupe_key="qa:target-1:footer",
                    title="Another title for the same finding")
    assert second.status_code == 200, second.text
    # MUTATION: skip the dedupe marker search and a second issue is filed.
    assert second.json()["existing"] == number
    assert second.json()["filed"] is False
    assert len(filed_payloads(forge)) == 1


def test_a_closed_issue_with_the_key_does_not_stop_a_new_one(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    first = create(client, attempt, request_id="req-a", dedupe_key="k1")
    forge.issues[first.json()["number"]]["state"] = "closed"
    second = create(client, attempt, request_id="req-b", dedupe_key="k1", title="again")
    # MUTATION: match closed issues for a dedupe_key and nothing is filed.
    assert second.json()["filed"] is True
    assert len(filed_payloads(forge)) == 2


def test_a_retried_call_finds_the_issue_its_attempt_already_filed(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    attempt, _ = running_task(client, db)
    first = create(client, attempt, request_id="req-a")
    again = create(client, attempt, request_id="req-b")
    # MUTATION: drop the attempt marker search and the retry files a duplicate.
    assert again.json()["existing"] == first.json()["number"]
    assert len(filed_payloads(forge)) == 1


# --------------------------------------------------------------------------
# labels: existing only
# --------------------------------------------------------------------------


def test_a_label_the_repository_lacks_is_dropped_and_named_never_created(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    forge.labels = ["bug", "needs-owner"]
    attempt, _ = running_task(client, db)
    response = create(client, attempt, labels=["needs-owner", "made-up-label"])
    assert response.status_code == 200, response.text
    answer = response.json()
    # MUTATION: pass the requested labels straight to GitHub and "made-up-label" is created.
    assert answer["labels_dropped"] == ["made-up-label"]
    [payload] = filed_payloads(forge)
    assert sorted(payload["labels"]) == ["bug", "needs-owner"]
    # No label was created.
    assert not [p for m, p in forge.writes() if p.endswith("/labels")]


# --------------------------------------------------------------------------
# the bounds
# --------------------------------------------------------------------------


def test_an_eleventh_write_in_one_attempt_is_refused(client, db, forge):
    seed_registration(db, files=True)
    forge.issue(5, body="x")
    attempt, _ = running_task(client, db)
    answers = [call(client, attempt, "issue_comment", {"number": 5, "body": f"note {n}"},
                    request_id=f"req-{n}") for n in range(11)]
    assert [a.status_code for a in answers[:10]] == [200] * 10, answers[9].text
    assert answers[10].status_code == 422, answers[10].text
    # MUTATION: raise ISSUE_WRITES_PER_ATTEMPT to 11 and the eleventh is posted.
    assert answers[10].json()["code"] == "issue_write_limit"
    assert len(forge.posted_comments()) == 10


def test_a_refused_write_does_not_spend_the_budget(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    forge.issue(5, body="x")
    attempt, _ = running_task(client, db)
    for n in range(12):
        create(client, attempt, request_id=f"bad-{n}", sections={"summary": "x"})
    response = call(client, attempt, "issue_comment", {"number": 5, "body": "ok"},
                    request_id="req-ok")
    # MUTATION: count a write at admission, before the policy, and this is issue_write_limit.
    assert response.status_code == 200, response.text


def test_the_thirty_first_write_across_a_tasks_attempts_is_refused(client, db, forge):
    seed_registration(db, files=True)
    forge.issue(5, body="x")
    attempt, _ = running_task(client, db)
    # Thirty writes by the task's earlier attempts.
    db.docs[f"git_tool_quota/{PARENT}:issue-writes"] = {
        "tenant_id": "eng", "task_id": PARENT, "writes": 30}
    response = call(client, attempt, "issue_comment", {"number": 5, "body": "x"})
    # MUTATION: count writes per attempt only and this is posted.
    assert response.json()["code"] == "issue_write_limit"
    assert forge.posted_comments() == []


def test_a_body_matching_a_redaction_rule_is_leak_found_and_not_repeated(client, db, forge):
    seed_registration(db, files=True)
    forge.all_forms()
    forge.issue(5, body="x")
    attempt, _ = running_task(client, db)
    leaked = credential_shaped()
    response = create(client, attempt, sections={**BUG, "notes": f"use {leaked}"})
    assert response.status_code == 422, response.text
    # MUTATION: mask the match and file the issue instead of refusing.
    assert response.json()["code"] == "leak_found"
    assert "notes" in response.json()["message"]
    assert leaked not in response.text
    comment = call(client, attempt, "issue_comment", {"number": 5, "body": f"k={leaked}"},
                   request_id="req-c")
    assert comment.json()["code"] == "leak_found"
    assert forge.writes() == []
    assert leaked not in json.dumps(audit_rows(db), default=str)


# --------------------------------------------------------------------------
# issue_comment
# --------------------------------------------------------------------------


def test_issue_comment_posts_on_an_open_issue(client, db, forge):
    seed_registration(db, files=True)
    forge.issue(5, body="x")
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "issue_comment", {"number": 5, "body": "found it"})
    assert response.status_code == 200, response.text
    assert response.json()["number"] == 5
    assert [c["body"] for c in forge.on_issue(5)] == ["found it"]


def test_issue_comment_on_a_pull_request_is_refused(client, db, forge):
    seed_registration(db, files=True)
    forge.issue(6, pull=True)
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "issue_comment", {"number": 6, "body": "x"})
    assert response.status_code == 422
    # MUTATION: skip the `pull_request` check and the comment lands on the PR.
    assert response.json()["code"] == "not_an_issue"
    assert forge.posted_comments() == []


def test_issue_comment_on_a_closed_issue_needs_reopen_false_stated(client, db, forge):
    seed_registration(db, files=True)
    forge.issue(8, state="closed")
    attempt, _ = running_task(client, db)
    refused = call(client, attempt, "issue_comment", {"number": 8, "body": "x"},
                   request_id="req-a")
    # MUTATION: comment on a closed issue unasked and this is 200.
    assert refused.json()["code"] == "issue_closed"
    stated = call(client, attempt, "issue_comment", {"number": 8, "body": "x", "reopen": False},
                  request_id="req-b")
    assert stated.status_code == 200, stated.text
    assert forge.issues[8]["state"] == "closed"
