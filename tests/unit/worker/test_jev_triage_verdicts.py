"""J3's triage over every verdict file a review step owes (part of #2688).

The owner chose on 2 November 2026 that Jev's triage must cover the reviews'
verdict files. In the first experiment batch no finding carried `p_real`: the
triage read only `verdict.json`'s `findings`, and the reviews wrote
`code-verdict.json` and `ui-verdict.json` with `blockers`, `majors` and
`minors`. What these tests hold, against the fake Jev of test_jev_wiring:

* every verdict file the step declared, and any `*verdict.json` it wrote, is
  read, in either shape, each item a string or an object;
* each finding gets `p_real`, `verdict` and every other key never change, and
  nothing is dropped or reordered;
* the step's summary says how many findings were scored per file;
* a file that cannot be read is left byte-identical and reported;
* a Jev failure leaves every file byte-identical;
* the sanitizer runs on what is sent.

MUTATIONS, each turning a named test red: read only `VERDICT_FILE` in
`_verdict_files` (the declared and the written files go unscored); read only
`findings` in `_candidates` (blockers and majors unscored); write a file
before every answer is in (failure == local); drop the unreadable report.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from agent_worker import verdict as verdict_mod
from agent_worker.judge import poststeps

from test_jev_wiring import FAILURES, FakeJev, _j3_answer, _judge, _review

CODE_VERDICT = {
    "verdict": "NOT_YET",
    "blockers": [
        {"severity": "blocker", "file": "pkg/mod.py", "where": "covered()",
         "problem": "covered() now returns 3", "fix": "return 1"},
        "lonely() is untested",
    ],
    "majors": [{"file": "pkg/mod.py", "line": 2, "summary": "the return changed silently"}],
    "minors": [],
    "notes": {"kept": True},
}


def _reviews(tmp_path: Path, *, declared: tuple[str, ...] = ("code-verdict.json",),
             findings: list | None = None) -> tuple[Path, Path, Path, dict]:
    work, checkout, artifacts, payload = _review(tmp_path, findings or [])
    if findings is None:
        (artifacts / "verdict.json").unlink()
    (artifacts / "code-verdict.json").write_text(json.dumps(CODE_VERDICT))
    payload = {**payload, "expected_outputs": list(declared)}
    return work, checkout, artifacts, payload


def test_a_code_verdict_with_blockers_and_majors_gets_each_scored(tmp_path):
    work, checkout, artifacts, payload = _reviews(tmp_path)
    fake = FakeJev(_j3_answer([0.9, 0.1, 0.6]))
    done = poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work, payload)
    assert done is not None and done["asked"] == 3
    assert done["files"]["code-verdict.json"]["scored"] == 3
    assert done["files"]["code-verdict.json"]["findings"] == 3
    assert done["unreadable"] == {}
    doc = json.loads((artifacts / "code-verdict.json").read_text())
    assert doc["verdict"] == "NOT_YET" and doc["notes"] == {"kept": True} and doc["minors"] == []
    assert doc["blockers"][0] == {**CODE_VERDICT["blockers"][0], "p_real": 0.9}
    assert doc["blockers"][1] == {"summary": "lonely() is untested", "p_real": 0.1,
                                  "triage": verdict_mod.VERIFY_BEFORE_ACTING}
    assert doc["majors"] == [{**CODE_VERDICT["majors"][0], "p_real": 0.6}]
    # The `problem` shape is what is asked about, with its file.
    first = fake.states()[0]
    assert first["finding"] == {"summary": "covered() now returns 3", "file": "pkg/mod.py"}
    assert first["code"] == {"path": "pkg/mod.py"}  # no line: no window
    assert fake.states()[2]["finding"]["line"] == 2


def test_an_undeclared_verdict_file_the_step_wrote_is_scored_too(tmp_path):
    work, checkout, artifacts, payload = _reviews(tmp_path, declared=())
    (artifacts / "ui-verdict.json").write_text(json.dumps(
        {"verdict": "MERGE", "blockers": [], "majors": [], "minors": ["a label overlaps"]}))
    fake = FakeJev(_j3_answer([0.5, 0.5, 0.5, 0.8]))
    done = poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work, payload)
    assert done is not None and done["asked"] == 4
    assert done["files"]["code-verdict.json"]["scored"] == 3
    assert done["files"]["ui-verdict.json"]["scored"] == 1
    ui = json.loads((artifacts / "ui-verdict.json").read_text())
    assert ui == {"verdict": "MERGE", "blockers": [], "majors": [],
                  "minors": [{"summary": "a label overlaps", "p_real": 0.8}]}


def test_a_verdict_json_with_findings_still_works_as_today(tmp_path):
    work, checkout, artifacts, payload = _review(tmp_path, ["lonely() is untested"])
    fake = FakeJev(_j3_answer([0.7]))
    done = poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work, payload)
    assert done is not None and done["asked"] == 1
    assert done["files"] == {"verdict.json": {"findings": 1, "scored": 1,
                                              "p_real": {"findings[0]": 0.7}}}
    doc = json.loads((artifacts / "verdict.json").read_text())
    assert doc["verdict"] == "NOT_YET" and doc["extra"] == {"kept": True}
    assert doc["findings"] == [{"summary": "lonely() is untested", "p_real": 0.7}]


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"\xff\xfe"])
def test_an_unreadable_verdict_file_is_left_untouched_and_reported(tmp_path, raw):
    work, checkout, artifacts, payload = _reviews(
        tmp_path, declared=("code-verdict.json", "ui-verdict.json"))
    (artifacts / "ui-verdict.json").write_bytes(raw)
    done = poststeps.review_post_step(_judge(tmp_path, FakeJev(_j3_answer([0.9] * 3))),
                                      artifacts, checkout, work, payload)
    assert (artifacts / "ui-verdict.json").read_bytes() == raw
    assert done is not None and set(done["unreadable"]) == {"ui-verdict.json"}
    assert done["files"]["code-verdict.json"]["scored"] == 3
    assert "ui-verdict.json" not in done["files"]


def test_an_unreadable_file_alone_is_still_reported(tmp_path):
    work, checkout, artifacts, payload = _reviews(tmp_path)
    (artifacts / "code-verdict.json").write_text("{")
    fake = FakeJev()
    done = poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work, payload)
    assert done == {"asked": 0, "files": {}, "unreadable": {"code-verdict.json": "not JSON"}}
    assert fake.requests == [] and (artifacts / "code-verdict.json").read_text() == "{"


def test_a_declared_file_outside_the_artifacts_directory_is_never_read(tmp_path):
    work, checkout, artifacts, payload = _reviews(tmp_path, declared=("sub/review-verdict.json",))
    (artifacts / "sub").mkdir()
    outside = tmp_path / "elsewhere-verdict.json"
    outside.write_text(json.dumps({"verdict": "NOT_YET", "blockers": ["x"]}))
    (artifacts / "sub" / "review-verdict.json").symlink_to(outside)
    before = outside.read_bytes()
    done = poststeps.review_post_step(_judge(tmp_path, FakeJev(_j3_answer([0.5] * 3))),
                                      artifacts, checkout, work, payload)
    assert outside.read_bytes() == before
    assert done is not None and done["unreadable"] == {"sub/review-verdict.json": "a link"}


@pytest.mark.parametrize("p", [0.0, 0.05, 0.5, 1.0])
@pytest.mark.parametrize("verdict", verdict_mod.REVIEW_VERDICTS)
def test_the_verdict_value_never_changes(tmp_path, p, verdict):
    work, checkout, artifacts, payload = _reviews(tmp_path, findings=["a", {"summary": "b"}])
    for name in ("verdict.json", "code-verdict.json"):
        doc = json.loads((artifacts / name).read_text())
        (artifacts / name).write_text(json.dumps({**doc, "verdict": verdict}))
    done = poststeps.review_post_step(_judge(tmp_path, FakeJev(lambda n, q, s: {"noul": p})),
                                      artifacts, checkout, work, payload)
    assert done is not None and done["asked"] == poststeps.J3_MAX_FINDINGS
    for name, lists in (("verdict.json", ("findings",)),
                        ("code-verdict.json", ("blockers", "majors", "minors"))):
        doc = json.loads((artifacts / name).read_text())
        assert doc["verdict"] == verdict
        for key in lists:
            assert len(doc[key]) == len(CODE_VERDICT.get(key, ["a", "b"]))
    assert verdict_mod.read_verdict(artifacts / "verdict.json", task_id="r",
                                    filename="verdict.json").verdict == verdict


def test_blockers_are_asked_about_before_minors_under_the_cap(tmp_path):
    work, checkout, artifacts, payload = _reviews(tmp_path, declared=())
    (artifacts / "code-verdict.json").write_text(json.dumps(
        {"verdict": "NOT_YET", "minors": [f"minor {i}" for i in range(6)],
         "blockers": ["the blocker"], "majors": ["the major"]}))
    fake = FakeJev(lambda n, q, s: {"noul": 0.5})
    done = poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work, payload)
    assert done is not None and done["asked"] == poststeps.J3_MAX_FINDINGS
    asked = [s["finding"]["summary"] for s in fake.states()]
    assert asked[:2] == ["the blocker", "the major"]
    assert done["files"]["code-verdict.json"] == {
        "findings": 8, "scored": poststeps.J3_MAX_FINDINGS,
        "p_real": {"blockers[0]": 0.5, "majors[0]": 0.5, "minors[0]": 0.5, "minors[1]": 0.5,
                   "minors[2]": 0.5}}
    doc = json.loads((artifacts / "code-verdict.json").read_text())
    assert doc["minors"][3:] == ["minor 3", "minor 4", "minor 5"]  # unscored: as written


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_jev_failure_leaves_every_verdict_file_byte_identical(tmp_path, failure):
    work, checkout, artifacts, payload = _reviews(tmp_path, findings=["lonely() is untested"])
    before = {p.name: p.read_bytes() for p in artifacts.iterdir()}
    assert poststeps.review_post_step(FAILURES[failure](tmp_path), artifacts, checkout, work,
                                      payload) is None
    assert {p.name: p.read_bytes() for p in artifacts.iterdir()} == before


def test_the_sanitizer_runs_on_a_blockers_text(tmp_path):
    token = "ghp_" + secrets.token_hex(18)  # a GitHub token's shape, built here
    work, checkout, artifacts, payload = _reviews(tmp_path)
    doc = {**CODE_VERDICT, "blockers": [f"the config holds {token}"]}
    (artifacts / "code-verdict.json").write_text(json.dumps(doc))
    fake = FakeJev()
    assert poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work,
                                      payload) is not None
    assert token not in json.dumps(fake.requests)
    assert json.loads((artifacts / "code-verdict.json").read_text())["blockers"][0]["summary"] \
        == f"the config holds {token}"  # the local file keeps what the review wrote
