"""The security review step's worker side: validate the audit, derive the verdict (owner, 2026-11-04).

docs/design/security-review-step.md. After the agent, the worker runs the
security-audit skill's two validators on its output and derives the step's
verdict from `findings.json`:

  * a CONFIRMED critical or high finding -> NOT_YET naming it, and one
    blocking owner question per finding (fix it now / accept the risk /
    false positive), named in `needs_owner`, so the merge parks in Needs you;
  * confirmed medium, low or informational -> MERGE, with the counts;
  * `needs_validation` alone -> MERGE: an unproven lead never blocks;
  * a failing validator -> the step fails with the validator's message, and
    no verdict is left for anything to read.

The validators are faked (`run=`): these tests need no node, no image and no
skill install. No credentials, no network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import questions as questions_mod
from agent_worker import security_review as sr
from agent_worker import verdict as verdict_mod

TASK = "task_sec"
STEP = "security-review"


def _finding(status: str, level: str | None = None, *, n: int = 1) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "verdict": status,
        "fingerprint": f"fp-{status}-{level}-{n}",
        "title": f"{status} {level or ''} finding {n}".strip(),
        "description": "Untrusted input reaches a sink.",
        "trace": [{"kind": "entrypoint", "file": "apps/api/routes.py", "line": 10 + n,
                   "scope": "handler", "description": "reads the parameter"}],
    }
    if level is not None:
        finding["severity"] = {"likelihood": {"score": level, "reason": "r"},
                               "impact": {"score": level, "reason": "r"},
                               "overall_severity": level}
    return finding


class Validators:
    """A fake `subprocess.run` for the two validators: passes, or fails one by name."""

    def __init__(self, fail: str | None = None, message: str = "") -> None:
        self.fail = fail
        self.message = message
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        assert kwargs["env"].keys() == {"PATH"}, "a validator is given PATH and nothing else"
        if self.fail and Path(argv[1]).name == self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.message)
        return subprocess.CompletedProcess(argv, 0, "PASS", "")


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    folder = tmp_path / "install" / "skills" / "security-audit"
    folder.mkdir(parents=True)
    for name in ("SKILL.md", sr.VALIDATE_FINDINGS, sr.VALIDATE_LEDGER):
        (folder / name).write_text("x")
    return folder


def _audit(artifacts: Path, findings: list[dict[str, Any]]) -> None:
    out = sr.output_dir(artifacts)
    out.mkdir(parents=True, exist_ok=True)
    (out / sr.FINDINGS).write_text(json.dumps(findings))
    (out / sr.LEDGER).write_text("[]")
    (out / sr.REPORT).write_text("# Report\n")


def _finish(artifacts: Path, skill: Path, run: Validators | None = None) -> dict[str, Any]:
    return sr.finish(artifacts, task_id=TASK, step_id=STEP, skill=skill,
                     node="/usr/bin/node", run=run or Validators())


def _verdict(artifacts: Path) -> dict[str, Any]:
    return json.loads((artifacts / sr.VERDICT_FILE).read_text())


def test_a_confirmed_high_blocks_with_owner_questions(tmp_path, skill):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", "high"), _finding("confirmed", "low")])

    record = _finish(artifacts, skill)

    assert record["verdict"] == "NOT_YET" and record["blocking"] == 1
    document = _verdict(artifacts)
    # Both names, byte for byte: the review's owed file and the merge's.
    assert (artifacts / sr.REVIEW_VERDICT).read_bytes() == (artifacts / sr.VERDICT_FILE).read_bytes()
    read = verdict_mod.verdict_of_document(document)
    assert read is not None and read.verdict == "NOT_YET"
    assert len(read.findings) == 1 and "[high]" in read.findings[0]
    assert read.locations[0].file == "apps/api/routes.py"
    (ref,) = read.needs_owner
    assert (ref.task_id, ref.step_id, ref.question_index) == (TASK, STEP, 0)

    asked = questions_mod.validate((artifacts / questions_mod.NAME).read_bytes())
    (question,) = asked
    assert [o["label"] for o in question["options"]] == [sr.FIX_NOW, sr.ACCEPT_RISK,
                                                         sr.FALSE_POSITIVE]
    assert question["blocking"] is True and question["built"] is None
    assert question["recommended"] == sr.FIX_NOW
    assert document[sr.SUMMARY_KEY]["counts"]["confirmed"]["high"] == 1
    assert document[sr.SUMMARY_KEY]["profile"] == "quick"


def test_a_confirmed_critical_blocks_and_comes_first(tmp_path, skill):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", "high"), _finding("confirmed", "critical")])

    _finish(artifacts, skill)

    document = _verdict(artifacts)
    assert document["verdict"] == "NOT_YET"
    assert [f["severity"] for f in document["findings"]] == ["critical", "high"]
    assert [r["question_index"] for r in document["needs_owner"]] == [0, 1]


@pytest.mark.parametrize("level", ["medium", "low", "informational"])
def test_a_confirmed_medium_or_lower_passes_with_counts(tmp_path, skill, level):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", level), _finding("rejected", n=2)])

    record = _finish(artifacts, skill)

    document = _verdict(artifacts)
    assert record["verdict"] == document["verdict"] == "MERGE"
    assert document["findings"] == [] and "needs_owner" not in document
    counts = document[sr.SUMMARY_KEY]["counts"]
    assert counts["confirmed"][level] == 1 and counts["rejected"] == 1
    assert not (artifacts / questions_mod.NAME).exists()


def test_needs_validation_alone_passes(tmp_path, skill):
    artifacts = tmp_path / "artifacts"
    lead = _finding("needs_validation", n=1)
    # An unconfirmed lead's claimed severity is not a confirmed one's.
    lead["severity"] = {"overall_severity": "critical"}
    _audit(artifacts, [lead, _finding("needs_validation", n=2)])

    _finish(artifacts, skill)

    document = _verdict(artifacts)
    assert document["verdict"] == "MERGE"
    assert document[sr.SUMMARY_KEY]["counts"]["needs_validation"] == 2
    assert document[sr.SUMMARY_KEY]["counts"]["confirmed"]["critical"] == 0


@pytest.mark.parametrize("validator", [sr.VALIDATE_FINDINGS, sr.VALIDATE_LEDGER])
def test_a_failing_validator_fails_the_step_with_its_message(tmp_path, skill, validator):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", "low")])
    # A verdict the agent wrote itself is never left to count.
    (artifacts / sr.REVIEW_VERDICT).write_text('{"verdict": "MERGE"}')
    run = Validators(fail=validator, message="ERROR: [0].trace is required\nFAIL: 1 error")

    with pytest.raises(sr.SecurityReviewInvalid) as raised:
        _finish(artifacts, skill, run)

    assert validator in str(raised.value)
    assert "ERROR: [0].trace is required" in str(raised.value)
    assert not (artifacts / sr.REVIEW_VERDICT).exists()
    assert not (artifacts / sr.VERDICT_FILE).exists()


def test_an_audit_with_no_findings_file_fails_rather_than_passing(tmp_path, skill):
    artifacts = tmp_path / "artifacts"
    sr.output_dir(artifacts).mkdir(parents=True)

    with pytest.raises(sr.SecurityReviewInvalid, match="wrote no findings.json"):
        _finish(artifacts, skill)
    assert not (artifacts / sr.VERDICT_FILE).exists()


def test_no_node_fails_rather_than_passing(tmp_path, skill, monkeypatch):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [])
    monkeypatch.setattr(sr.shutil, "which", lambda _name: None)

    with pytest.raises(sr.SecurityReviewInvalid, match="no node"):
        sr.finish(artifacts, task_id=TASK, step_id=STEP, skill=skill, run=Validators())


def test_the_agents_own_questions_are_kept_before_the_security_ones(tmp_path, skill):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", "high")])
    own = [{"question": "Which name?", "options": [{"label": "a"}, {"label": "b"}],
            "built": "a"}]
    (artifacts / questions_mod.NAME).write_text(json.dumps(own))

    _finish(artifacts, skill)

    asked = questions_mod.validate((artifacts / questions_mod.NAME).read_bytes())
    assert asked[0]["question"] == "Which name?"
    assert _verdict(artifacts)["needs_owner"][0]["question_index"] == 1


def test_past_twenty_blocking_findings_the_last_question_covers_the_rest(tmp_path, skill):
    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", "high", n=i) for i in range(25)])

    _finish(artifacts, skill)

    asked = questions_mod.validate((artifacts / questions_mod.NAME).read_bytes())
    assert len(asked) == questions_mod.MAX_QUESTIONS
    assert "6 more critical or high findings" in asked[-1]["question"]
    assert len(_verdict(artifacts)["needs_owner"]) == questions_mod.MAX_QUESTIONS


def test_the_real_validators_run_under_node_when_it_is_there(tmp_path):
    node = sr.shutil.which("node")
    if node is None:
        pytest.skip("no node on this machine")
    folder = tmp_path / "skill"
    folder.mkdir()
    (folder / sr.VALIDATE_FINDINGS).write_text(
        "const d=JSON.parse(require('fs').readFileSync(process.argv[2]));"
        "if(!Array.isArray(d)){console.error('ERROR: not an array');process.exit(1)}")
    (folder / sr.VALIDATE_LEDGER).write_text("process.exit(0)")
    out = tmp_path / "out"
    out.mkdir()
    (out / sr.FINDINGS).write_text("{}")
    (out / sr.LEDGER).write_text("[]")

    with pytest.raises(sr.SecurityReviewInvalid, match="ERROR: not an array"):
        sr.run_validators(folder, out, node=node)
    (out / sr.FINDINGS).write_text("[]")
    sr.run_validators(folder, out, node=node)


def test_the_guidance_states_the_owners_run_settings(tmp_path):
    text = sr.guidance(tmp_path / "artifacts")
    assert "FULL AUDIT MODE" in text and "`quick`" in text
    assert f"budget to {sr.SUBAGENT_BUDGET}" in text
    assert "NO OS-ENFORCED SANDBOX" in text
    assert str(tmp_path / "artifacts" / sr.OUTPUT_DIR_NAME) in text
    assert "merge-base" in text


def test_the_comment_is_redacted_and_cut_to_githubs_limit_with_a_pointer():
    secret = "s" + "k" * 30
    report = "# Report\n" + ("line of the report\n" * 5000) + f"leaked {secret}\n"
    body = sr.comment_body(report, artifact_pointer="`security-audit/REPORT.md`",
                           scrub=lambda text: text.replace(secret, "[REDACTED]"),
                           limit=20_000)
    assert len(body) <= 20_000
    assert body.startswith(sr.COMMENT_HEADING)
    assert "`security-audit/REPORT.md`" in body and "longer than a GitHub comment" in body
    assert secret not in body

    short = sr.comment_body(f"# Report\nleaked {secret}\n", artifact_pointer="x",
                            scrub=lambda text: text.replace(secret, "[REDACTED]"))
    assert "[REDACTED]" in short and "longer than" not in short


def test_a_step_is_a_security_review_only_when_its_started_plugins_load_the_skill():
    class P:
        def __init__(self, name: str) -> None:
            self.name = name

    class Started:
        def __init__(self, *names: str) -> None:
            self.plugins = tuple(P(n) for n in names)

    assert sr.applies(Started(sr.PLUGIN))
    assert not sr.applies(Started("gstack"))
    assert not sr.applies(None)


@pytest.mark.parametrize("answer,expected", [
    ({"choice": sr.ACCEPT_RISK, "text": "internal only"}, "merge"),
    ({"choice": sr.FALSE_POSITIVE}, "merge"),
    ({"choice": sr.FIX_NOW}, "fix"),
    ({"choice": None, "text": "hmm"}, "fix"),
    (None, None),
])
def test_an_answer_lets_the_merge_go_on_only_when_it_accepts_or_dismisses(answer, expected):
    assert sr.disposition(answer) == expected


# --- the worker's hook ---------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    def error(self, message: str, **fields: Any) -> None:
        self.lines.append(("error", message, fields))

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append(("info", message, fields))


def _worker(artifacts: Path) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        ws=SimpleNamespace(artifacts=artifacts), _security_review={}, _plugins=None,
        _task={"step_id": STEP}, cfg=SimpleNamespace(task_id=TASK),
        _scrub=lambda value: value, log=_Log(),
        _not_uploaded_causes={}, _skip_causes={}, _verdict_unreadable=False,
        _expected_outputs=(sr.REVIEW_VERDICT,),
    )


def test_the_worker_fails_the_step_not_retryably_on_a_failing_validator(tmp_path, monkeypatch):
    from agent_worker import expected_outputs as expected_mod
    from agent_worker import lifecycle

    def refuse(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise sr.SecurityReviewInvalid("validate-findings.cjs failed on findings.json (exit 1)")

    monkeypatch.setattr(lifecycle.security_review_mod, "finish", refuse)
    worker = _worker(tmp_path)

    lifecycle.Worker._finish_security_review(worker)

    assert worker._security_review == {
        "error": "validate-findings.cjs failed on findings.json (exit 1)"}
    causes = lifecycle.Worker._missing_causes(worker, {})
    assert causes[sr.REVIEW_VERDICT] == sr.CAUSE_INVALID
    assert not expected_mod.retryable([sr.REVIEW_VERDICT], causes)
    assert worker.log.lines[0][0] == "error"


def test_the_worker_records_the_derived_verdict(tmp_path, monkeypatch, skill):
    from agent_worker import lifecycle

    artifacts = tmp_path / "artifacts"
    _audit(artifacts, [_finding("confirmed", "high")])
    monkeypatch.setattr(lifecycle.security_review_mod, "install_dir", lambda: skill.parent.parent)
    real = sr.finish
    monkeypatch.setattr(lifecycle.security_review_mod, "finish",
                        lambda *a, **k: real(*a, **k, node="/usr/bin/node", run=Validators()))
    worker = _worker(artifacts)

    lifecycle.Worker._finish_security_review(worker)

    assert worker._security_review["verdict"] == "NOT_YET"
    assert worker._security_review["owner_questions"] == 1
    assert _verdict(artifacts)["needs_owner"][0]["step_id"] == STEP
    assert sr.REVIEW_VERDICT not in lifecycle.Worker._missing_causes(worker, {})
