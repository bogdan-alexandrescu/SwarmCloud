"""A review finding keeps WHERE it is (diff viewer variant 3, worker half).

docs/design/diff-viewer.md, variant 3 and the "later · findings" row: the
console pins each finding beside the line it names. That needs the worker to
keep a finding's `file`, `line` and `side` out of `verdict.json`, validated,
plus the digest of the patch the review read, so a pin made against one patch
is never drawn on another after a fix round moved the lines.

Pinned here, each against the way it would most likely go wrong:

  * a valid location is kept, indexed to its finding;
  * an invalid path, line or side is dropped and the FINDING is kept -- the
    path is displayed, never opened, and a hostile one never reaches the
    record;
  * a verdict with no location reads, and records, exactly as before;
  * the digest is the worker's measurement of what the review staged, not the
    agent's claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode

from test_verdict_gate import _verdict_text, run_gated, seed_gated, upstream_verdict


def _read(tmp_path: Path, findings: Any, verdict: str = "NOT_YET") -> verdict_mod.Verdict:
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps({"verdict": verdict, "findings": findings}))
    return verdict_mod.read_verdict(path, task_id="task_up", filename="verdict.json")


# ---------------------------------------------------------------------------
# reading a location
# ---------------------------------------------------------------------------


def test_a_finding_with_a_valid_location_keeps_it(tmp_path: Path):
    read = _read(tmp_path, [
        "a string finding first, so the index is not always 0",
        {"summary": "the guard reads the wrong field", "file": "apps/x/guard.py",
         "line": 42, "side": "new"},
        {"summary": "a removed check", "file": "src/old.ts", "line": 7, "side": " OLD "},
    ])

    assert read.findings == (
        "a string finding first, so the index is not always 0",
        "the guard reads the wrong field",
        "a removed check",
    )
    assert [loc.as_dict() for loc in read.locations] == [
        {"finding": 1, "file": "apps/x/guard.py", "line": 42, "side": "new"},
        {"finding": 2, "file": "src/old.ts", "line": 7, "side": "old"},
    ]
    assert read.locations_dropped == 0


def test_a_file_without_a_line_is_kept_as_the_file_alone(tmp_path: Path):
    # The review briefs' own shape: `file` and a free-text `where`, no line.
    read = _read(tmp_path, [{"severity": "major", "file": "a/b.py", "where": "f()",
                             "problem": "p"}])
    assert [loc.as_dict() for loc in read.locations] == [{"finding": 0, "file": "a/b.py"}]
    assert read.locations_dropped == 0


@pytest.mark.parametrize("path", [
    "/etc/passwd",
    "../outside.py",
    "apps/../../outside.py",
    "apps/./x.py",
    "apps//x.py",
    "apps/x/",
    "~/x.py",
    "C:/x.py",
    "apps\\x.py",
    "apps/x\n.py",
    "apps/x\x00.py",
    "apps/\x1b[31mx.py",
    "",
    "   ",
    "a" * (verdict_mod.MAX_LOCATION_PATH_CHARS + 1),
    7,
    ["apps/x.py"],
])
def test_an_invalid_path_drops_the_location_and_keeps_the_finding(tmp_path: Path, path: Any):
    read = _read(tmp_path, [{"summary": "s", "file": path, "line": 3, "side": "new"}])
    assert read.findings == ("s",)
    assert read.locations == ()
    assert read.locations_dropped == 1


@pytest.mark.parametrize("line", [0, -1, True, False, 1.5, "12", None,
                                  verdict_mod.MAX_LOCATION_LINE + 1])
def test_an_invalid_line_drops_the_line_and_side_and_keeps_the_file(tmp_path: Path, line: Any):
    read = _read(tmp_path, [{"summary": "s", "file": "a.py", "line": line, "side": "new"}])
    assert read.findings == ("s",)
    assert [loc.as_dict() for loc in read.locations] == [{"finding": 0, "file": "a.py"}]
    assert read.locations_dropped == 1


@pytest.mark.parametrize("side", ["left", "", None, 1, "both", "newer"])
def test_an_invalid_side_drops_the_line_and_side_and_keeps_the_file(tmp_path: Path, side: Any):
    # A line number with no half of the patch to count in cannot be placed,
    # and a placement is never guessed.
    read = _read(tmp_path, [{"summary": "s", "file": "a.py", "line": 3, "side": side}])
    assert read.findings == ("s",)
    assert [loc.as_dict() for loc in read.locations] == [{"finding": 0, "file": "a.py"}]
    assert read.locations_dropped == 1


def test_locations_follow_the_findings_bound(tmp_path: Path):
    many = [{"summary": f"f{i}", "file": "a.py", "line": i + 1, "side": "new"}
            for i in range(verdict_mod.MAX_FINDINGS + 5)]
    read = _read(tmp_path, many)
    assert len(read.findings) == verdict_mod.MAX_FINDINGS
    assert [loc.finding for loc in read.locations] == list(range(verdict_mod.MAX_FINDINGS))


def test_no_location_reads_exactly_as_before(tmp_path: Path):
    findings = ["one", {"summary": "two"}, {"severity": "minor", "summary": "three"}]
    read = _read(tmp_path, findings)
    assert read == verdict_mod.Verdict(
        verdict="NOT_YET",
        findings=("one", "two", "three"),
        minors=(verdict_mod.MinorFinding(text="three"),),
    )


# ---------------------------------------------------------------------------
# the patch the review read
# ---------------------------------------------------------------------------


def test_reviewed_patches_reads_the_review_workers_digests_only():
    good = "ab" * 32
    review = {"result_summary": {"staged_inputs": [
        {"task_id": "task_impl", "filename": "swarm-work.patch", "sha256": good},
        {"task_id": "task_impl", "filename": "notes.md", "sha256": good},
        {"task_id": "task_old", "filename": "old.patch"},
        {"task_id": "task_bad", "filename": "bad.patch", "sha256": "not-hex"},
        "junk",
    ]}}
    assert verdict_mod.reviewed_patches(review) == [
        {"task_id": "task_impl", "filename": "swarm-work.patch", "sha256": good},
    ]
    assert verdict_mod.reviewed_patches({}) == []
    assert verdict_mod.reviewed_patches(None) == []


def test_a_staged_input_records_the_digest_of_what_landed():
    item = inputs_mod.StagedInput(
        upstream_task_id="task_up", filename="x.patch", path="x.patch", size_bytes=1,
        sha256="cd" * 32,
    )
    assert item.as_dict()["sha256"] == "cd" * 32


# ---------------------------------------------------------------------------
# end to end: the gated step's record
# ---------------------------------------------------------------------------


def test_the_gated_step_records_locations_and_the_reviewed_patch_digest(
    db, worker_factory, monkeypatch
):
    upstream_verdict(db, worker_factory, _verdict_text("NOT_YET", [
        {"summary": "placed", "file": "apps/x.py", "line": 9, "side": "new"},
        {"summary": "hostile", "file": "../../etc/passwd", "line": 1, "side": "old"},
        "no location",
    ]))
    # The review's own worker recorded what it staged; a real one writes this.
    patch_digest = hashlib.sha256(b"diff --git a/apps/x.py b/apps/x.py\n").hexdigest()
    db.doc("tasks/task_up")["result_summary"]["staged_inputs"] = [
        {"task_id": "task_impl", "filename": "swarm-work.patch", "path": "swarm-work.patch",
         "bytes": 36, "sha256": patch_digest},
    ]
    seed_gated(db, verdict_in=["NOT_YET"])

    assert run_gated(worker_factory, monkeypatch, runner_allowed=True) == ExitCode.OK

    gate = db.doc("tasks/task_2")["result_summary"]["verdict_gate"]
    assert gate["findings"] == ["placed", "hostile", "no location"]
    assert gate["finding_locations"] == [
        {"finding": 0, "file": "apps/x.py", "line": 9, "side": "new"},
    ]
    assert gate["locations_dropped"] == 1
    assert gate["reviewed_patches"] == [
        {"task_id": "task_impl", "filename": "swarm-work.patch", "sha256": patch_digest},
    ]
    assert "etc/passwd" not in json.dumps(gate["finding_locations"])


def test_a_gated_step_with_no_location_records_what_it_always_did(
    db, worker_factory, monkeypatch
):
    upstream_verdict(db, worker_factory, _verdict_text("MERGE", ["nothing blocks"]))
    seed_gated(db, verdict_in=["NOT_YET"])

    assert run_gated(worker_factory, monkeypatch, runner_allowed=False) == ExitCode.OK

    gate = db.doc("tasks/task_2")["result_summary"]["verdict_gate"]
    assert set(gate) == {"task_id", "file", "verdict", "verdict_in", "agent_ran",
                         "findings", "findings_dropped"}
