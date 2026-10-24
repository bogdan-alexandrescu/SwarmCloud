"""The QA run's prompts know the gate's refusals (QA-GATE-REFUSALS; part of #2067).

On 2026-10-23 the review, report and file steps of a visual-QA run read the
live gate's own 403 (a `POST` to the app's identity provider, refused, so
Chrome logged a CORS block) as the app's defect, and filed it into the app's
repository. The capture now writes each screen's refusals as `gate_refused`;
these hold the property of each rendered prompt, not its wording:

* the capture step is told the tool records the gate's refusals and not to
  work around them;
* the review step names `gate_refused` and puts a screen it explains under
  `about_run`, never among the app's findings;
* the report opens with how many requests the gate refused, by method and
  origin, and keeps such findings in `about_run`;
* the file step never files a finding whose evidence is a gate refusal.
"""

from __future__ import annotations

from typing import Any

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


def test_the_capture_prompt_says_the_tool_records_the_gates_refusals():
    capture = _prompts()["capture-g1"]
    # MUTATION: drop the sentence from _capture_prompt and the capture agent retries around a refusal.
    assert "gate_refused" in capture


def test_the_review_prompt_puts_a_screen_the_gate_refused_for_in_about_run():
    review = _prompts()["review-g1"]
    # MUTATION: drop the gate sentence from _review_prompt and a gate 403 is reviewed as the app's CORS defect.
    assert "gate_refused" in review
    assert "about_run" in review
    # The console error that names the same URL is the tell, and it is the run's, not the app's.
    assert "console error" in review and "same URL" in review
    assert "never" in review.split("gate_refused", 1)[1]


def test_the_report_prompt_opens_with_the_gates_refusals_by_method_and_origin():
    report = _prompts()["report"]
    # MUTATION: drop the opening line from _report_prompt and the report reads as the app's defects alone.
    assert "gate_refused" in report and "gate_refusals" in report
    assert "the gate refused" in report
    assert "by method and origin" in report
    assert "show the app without them" in report
    assert "about_run" in report.split("gate_refused", 1)[1]


def test_the_file_prompt_never_files_a_finding_whose_evidence_is_a_gate_refusal():
    filed = _prompts()["file"]
    # MUTATION: drop the sentence from _file_prompt and a gate refusal is filed into the app's repository.
    assert "gate_refused" in filed
    tail = filed.split("gate_refused", 1)[1]
    assert "never file" in (filed.split("gate_refused", 1)[0][-200:] + tail[:200]).lower()


def test_the_report_names_the_idp_token_refreshes_the_gate_let_through_separately():
    """QA-IDP-POST (part of #2067): a target's IdP token path is let through
    after the gate's sign-in, and the manifest lists those requests as
    `gate_refusals.allowed`. The report names them on their own line."""
    report = _prompts()["report"]
    # MUTATION: drop the allowed sentence from _gate_report_line and a run never shows what the gate let through.
    assert "allowed: IdP token refresh" in report
    assert "gate_refusals.allowed" in report
    tail = report.split("allowed: IdP token refresh", 1)[1]
    # They are not refusals, and never count toward N.
    assert "not" in tail[:300] and "refus" in tail[:300]
