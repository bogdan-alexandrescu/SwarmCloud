"""Jev's review and judgment of a work contract (`swarm_api.contractjudge`), and
the contract fields they need (`swarm_api.workcontract`: the `judgment` kind,
`acceptance`, the `reviewer` author).

The design is docs/design/agent-quality/jev-contract.md, sections 3, 4.3,
4.4, 5.3 and 6, with the owner's answers 3, 6 and 12 of 3 November 2026. No
credentials, no network, no emulator: the module is pure, and Jev's answers
are built here in the client's wire shape.
"""

from __future__ import annotations

import json
import secrets

import pytest

from swarm_api import contractjudge as cj
from swarm_api import workcontract as wc

COMMIT = "c" * 40


def _doc(**over):
    doc = {
        "schema": 1,
        "contract_id": "wc-login-flow",
        "tenant_id": "tenant-" + secrets.token_hex(4),
        "revision": 1,
        "previous_digest": None,
        "strictness": "advisory",
        "requirements": [
            {"id": "REQ-1", "text": "The login page accepts SSO.", "verification": ["unit_tests"]},
            {"id": "REQ-2", "text": "The error copy is clear.", "verification": ["review"]},
            {"id": "REQ-3", "text": "It feels fast.", "verification": []},
        ],
        "repositories": [{"repository_url": "https://github.com/acme/app", "ref": "main",
                          "commit": "a" * 40}],
        "scope": {"write": ["src/login/**"]},
        "protected": {"interfaces": ["swarm_common.models.Task"]},
        "limits": {"runner_profile": "claude-code"},
        "artifacts": [{"kind": "pull_request"}],
        "ownership": {"owner": "owner-" + secrets.token_hex(4) + "@example.com"},
    }
    doc.update(over)
    return doc


def _choice(choice, p):
    return {"choice": choice, "probabilities": {choice: p}, "confidence": p}


def _review_answers(*rows):
    answers = {}
    for k, cls, reason, p in rows:
        answers[f"c{k}"] = _choice(cls, p)
        answers[f"r{k}"] = _choice(reason, p)
    return answers


# --- the contract fields -------------------------------------------------------


def test_judgment_is_a_verification_kind_and_a_component_of_its_own():
    doc = _doc()
    doc["requirements"][1]["verification"] = ["review", "judgment"]
    c = wc.decode(doc)
    assert "judgment" in wc.VERIFICATION_KINDS
    assert c.requirements[1].verification == ("judgment", "review")
    assert wc.COMPONENT_JUDGMENT in c.components()
    assert wc.COMPONENT_JUDGMENT not in wc.decode(_doc()).components()


def test_a_strict_contract_naming_judgment_is_refused_where_jev_cannot_run():
    doc = _doc(strictness="strict", scope={}, protected={}, artifacts=[])
    doc["requirements"] = [{"id": "REQ-1", "text": "Clear copy.", "verification": ["judgment"]}]
    c = wc.decode(doc)
    with pytest.raises(wc.ContractNotEnforceable) as raised:
        wc.compile_contract(c, step_id="s",
                            enforced=wc.WORKER_ENFORCED_TODAY | {wc.COMPONENT_VERIFICATION})
    assert raised.value.detail["unenforced"] == ["judgment"]
    compiled = wc.compile_contract(c, step_id="s", enforced=wc.enforced_with_judgment(
        wc.WORKER_ENFORCED_TODAY | {wc.COMPONENT_VERIFICATION}))
    assert compiled.advisory == ()


def test_acceptance_is_strict_by_default_and_the_default_keeps_the_digest():
    base = _doc()
    absent = wc.decode(base)
    explicit = wc.decode(dict(base, acceptance="strict"))
    assert absent.acceptance == explicit.acceptance == "strict"
    assert wc.digest(absent) == wc.digest(explicit)
    assert "acceptance" not in absent.to_document()
    auto = wc.decode(dict(base, acceptance="auto"))
    assert auto.to_document()["acceptance"] == "auto"
    assert wc.digest(auto) != wc.digest(absent)
    assert wc.decode(auto.to_document()) == auto
    for bad in ("advisory", None, "", ["auto"]):
        with pytest.raises(wc.ContractInvalid):
            wc.decode(_doc(acceptance=bad))


def test_weakening_acceptance_is_a_widening_an_agent_cannot_make():
    old = wc.decode(_doc())
    proposal = old.to_document()
    proposal["acceptance"] = "auto"
    assert "acceptance" in wc.widening(old, wc.decode(dict(proposal)))
    with pytest.raises(wc.ScopeWideningRefused) as raised:
        wc.amend(old, proposal, author_is_agent=True)
    assert "acceptance" in raised.value.detail["widens"]
    back = wc.decode(dict(old.to_document(), acceptance="auto"))
    assert "acceptance" not in wc.widening(back, old)


def test_auto_acceptance_on_a_judgment_alone_is_refused_without_a_review_step():
    doc = _doc(acceptance="auto")
    doc["requirements"][2]["verification"] = ["judgment"]
    c = wc.decode(doc)
    with pytest.raises(wc.ContractNotEnforceable) as raised:
        wc.compile_contract(c, step_id="s")
    assert raised.value.detail["judgment_only"] == ["REQ-3"]
    assert wc.compile_contract(c, step_id="s", workflow_has_review=True).advisory
    strict = wc.decode(dict(doc, acceptance="strict"))
    assert wc.compile_contract(strict, step_id="s").advisory


# --- the reviewer may only add judgment -----------------------------------------


def test_the_reviewer_adds_judgment_as_revision_2_and_nothing_else_moves():
    old = wc.decode(_doc())
    proposal = old.to_document()
    proposal["requirements"][1]["verification"] = ["judgment", "review"]
    result = wc.amend(old, proposal, author=wc.AUTHOR_REVIEWER)
    new = result.contract
    assert (new.revision, new.previous_digest) == (2, wc.digest(old))
    assert result.changed == ("REQ-2",) and result.added == () and result.withdrawn == ()
    assert wc.widening(old, new) == ()
    before, after = old.to_document(), new.to_document()
    after["requirements"][1]["verification"].remove("judgment")
    for d in (before, after):
        d.pop("revision"), d.pop("previous_digest")
    assert before == after


@pytest.mark.parametrize("mutate", [
    lambda d: d["requirements"][0].update(text="Reworded."),
    lambda d: d["requirements"][0].update(verification=["judgment"]),
    lambda d: d["requirements"][0].update(verification=["unit_tests", "ci"]),
    lambda d: d["requirements"].reverse(),
    lambda d: d["requirements"].append({"id": "REQ-9", "text": "New.",
                                        "verification": ["judgment"]}),
    lambda d: d.update(strictness="strict"),
    lambda d: d.update(acceptance="auto"),
    lambda d: d.update(scope={"write": ["src/**"]}),
    lambda d: d.update(protected={}),
    lambda d: d["limits"].update(max_turns=10),
    lambda d: d.update(artifacts=[]),
    lambda d: d["ownership"].update(owner="someone-else@example.com"),
    lambda d: d["requirements"][1].update(verification=["review"]),
], ids=["text", "drops_a_kind", "adds_a_non_judgment_kind", "order", "new_requirement",
        "strictness", "acceptance", "scope", "protected", "limits", "artifacts", "owner",
        "no_change"])
def test_the_reviewer_may_only_add_judgment(mutate):
    old = wc.decode(_doc())
    proposal = old.to_document()
    proposal["requirements"][1]["verification"] = ["judgment", "review"]
    mutate(proposal)
    with pytest.raises(wc.AmendmentRefused) as raised:
        wc.amend(old, proposal, author=wc.AUTHOR_REVIEWER)
    assert raised.value.detail["author"] == "reviewer"


def test_the_reviewer_never_withdraws():
    old = wc.decode(_doc())
    proposal = old.to_document()
    proposal["requirements"].pop()
    with pytest.raises(wc.AmendmentRefused):
        wc.amend(old, proposal, author=wc.AUTHOR_REVIEWER, withdraw={"REQ-3": "vague"})


def test_amend_names_its_author_and_a_person_s_and_an_agent_s_rules_hold():
    old = wc.decode(_doc())
    proposal = old.to_document()
    proposal["requirements"][0]["text"] = "Reworded."
    with pytest.raises(TypeError):
        wc.amend(old, proposal)
    with pytest.raises(TypeError):
        wc.amend(old, proposal, author="robot")
    with pytest.raises(TypeError):
        wc.amend(old, proposal, author=wc.AUTHOR_PERSON, author_is_agent=True)
    assert wc.amend(old, proposal, author=wc.AUTHOR_PERSON).changed == ("REQ-1",)
    with pytest.raises(wc.AmendmentRefused):
        wc.amend(old, proposal, author=wc.AUTHOR_AGENT)


# --- JC1: the submission review --------------------------------------------------


def test_the_review_state_never_carries_the_tenant_the_owner_or_the_repository():
    c = wc.decode(_doc())
    state = cj.review_state(c)
    raw = json.dumps(state)
    for secret_ish in (c.tenant_id, c.owner, c.contract_id, "github.com", "a" * 40, "REQ-"):
        assert secret_ish not in raw
    assert [r["index"] for r in state["requirements"]] == [0, 1, 2]
    assert set(state) == {"contract", "verification_kinds", "requirements", "artifacts",
                          "write_scope"}
    questions = cj.review_questions(state)
    assert sorted(questions) == ["c0", "c1", "c2", "r0", "r1", "r2"]
    assert set(questions["c0"]["criteria"]) == {"mechanical", "judgeable", "unenforceable"}
    assert "none" in questions["r0"]["criteria"]


def test_a_large_contract_is_reviewed_in_calls_of_32():
    doc = _doc()
    doc["requirements"] = [{"id": f"REQ-{i}", "text": f"Thing {i}.", "verification": []}
                           for i in range(1, 71)]
    c = wc.decode(doc)
    calls = cj.batches(c.requirement_ids(), cj.REVIEW_PER_CALL)
    assert [len(b) for b in calls] == [32, 32, 6]
    with pytest.raises(ValueError):
        cj.review_state(c)
    last = cj.review_state(c, calls[2])
    assert [r["index"] for r in last["requirements"]] == list(range(64, 70))
    assert len(cj.review_questions(last)) == 12


@pytest.mark.parametrize("answers", [
    None,
    "mechanical",
    {},
    {"c0": {"choice": "mechanical"}, "r0": _choice("none", 0.9)},
    {"c0": _choice("enforceable", 0.9), "r0": _choice("none", 0.9)},
    {"c0": _choice("mechanical", 1.5), "r0": _choice("none", 0.9)},
    {"c0": _choice("mechanical", float("nan")), "r0": _choice("none", 0.9)},
    {"c0": _choice("mechanical", True), "r0": _choice("none", 0.9)},
    {"c0": _choice("mechanical", 0.9), "r0": _choice("because I said so", 0.9)},
    {"c0": _choice("mechanical", 0.9)},
    {"c0": {"choice": "mechanical", "probabilities": {"judgeable": 0.9}},
     "r0": _choice("none", 0.9)},
], ids=["none", "text", "empty", "no_probability", "unknown_class", "p_over_one", "nan",
        "bool_p", "unknown_reason", "no_reason", "p_of_another_choice"])
def test_a_malformed_review_answer_reads_as_not_judged(answers):
    c = wc.decode(_doc())
    state = cj.review_state(c, ["REQ-1"])
    (cls,) = cj.read_review(c, state, answers)
    assert (cls.requirement_id, cls.cls, cls.p) == ("REQ-1", "not_judged", 0.0)
    review = cj.review_outcome(c, [cls])
    assert review.outcome == "ok" and review.add_judgment == ()


def test_a_judgeable_requirement_is_amended_by_the_reviewer_at_the_threshold():
    c = wc.decode(_doc())
    state = cj.review_state(c)
    classes = cj.read_review(c, state, _review_answers(
        (0, "mechanical", "none", 0.95),
        (1, "judgeable", "proof_kind_mismatch", 0.6),
        (2, "judgeable", "subjective", 0.59)))
    review = cj.review_outcome(c, classes)
    assert review.outcome == "amended" and review.add_judgment == ("REQ-2",)
    amendment = cj.review_amendment(c, review)
    assert amendment.contract.revision == 2
    assert amendment.changed == ("REQ-2",)
    assert amendment.contract.requirements[1].verification == ("judgment", "review")
    assert amendment.contract.requirements[2].verification == ()
    record = cj.review_record(c, review, call_id="call-1", backend="internal-jev",
                              at="2026-11-03T00:00:00Z")
    assert record["digest"] == wc.digest(c) and record["outcome"] == "amended"
    assert {r["class"] for r in record["classes"]} == {"mechanical", "judgeable"}


def test_a_strict_contract_with_an_unenforceable_requirement_is_refused_without_its_text():
    c = wc.decode(_doc(strictness="strict"))
    classes = cj.read_review(c, cj.review_state(c), _review_answers(
        (0, "mechanical", "none", 0.9), (1, "judgeable", "subjective", 0.9),
        (2, "unenforceable", "vague_outcome", 0.83)))
    review = cj.review_outcome(c, classes)
    assert review.outcome == "refused" and review.unenforceable == ("REQ-3",)
    assert cj.review_amendment(c, review) is None
    detail = cj.refusal_detail(c, review, call_id="call-2", backend="internal-jev")
    assert detail["reason"] == "work_contract_not_enforceable"
    assert detail["jev"]["requirements"] == [
        {"id": "REQ-3", "class": "unenforceable", "reason": "vague_outcome", "p": 0.83}]
    assert "feels fast" not in json.dumps(detail)
    advisory = cj.review_outcome(wc.decode(_doc()), classes)
    assert advisory.outcome == "amended" and advisory.advisory == ("REQ-3",)


def test_a_review_that_did_not_run_is_recorded_as_not_run():
    c = wc.decode(_doc(strictness="strict"))
    record = cj.review_record(c, None, call_id="", backend="internal-jev",
                              at="2026-11-03T00:00:00Z", not_run_reason="budget_monthly")
    assert (record["outcome"], record["reason"], record["classes"]) == (
        "not_run", "budget_monthly", [])
    with pytest.raises(ValueError):
        cj.review_record(c, None, call_id="", backend="b", at="t")


# --- JC2: the acceptance judgment ------------------------------------------------


def _judged_contract(**over):
    doc = _doc(**over)
    doc["requirements"][1]["verification"] = ["judgment", "review"]
    doc["requirements"][2]["verification"] = ["judgment"]
    return wc.decode(doc)


def _judgment_state(c, **over):
    kwargs = dict(
        commit=COMMIT,
        changed=[{"path": "src/login/view.py", "status": "modified", "hunk": "@@ -1 +1 @@",
                  "summary": "I did everything", "pr_body": "Trust me"},
                 {"path": "src/login/copy.py", "status": "added", "hunk": "x" * 5000}],
        mechanical=[{"kind": "unit_tests", "passed": True, "named": ["tests/test_view.py"]},
                    {"kind": "ci", "passed": "yes"},
                    {"kind": "judgment", "passed": True}],
        artifacts=[{"kind": "pull_request", "present": True, "body": "Trust me"}],
    )
    kwargs.update(over)
    return cj.judgment_state(c, **kwargs)


def test_the_judgment_state_never_carries_the_tenant_the_owner_or_the_agent_s_summary():
    c = _judged_contract()
    state = _judgment_state(c)
    raw = json.dumps(state)
    for absent in (c.tenant_id, c.owner, c.contract_id, "github.com", "I did everything",
                   "Trust me", "REQ-"):
        assert absent not in raw
    assert [r["index"] for r in state["requirements"]] == [1, 2]
    assert set(state["changed"][0]) == {"item", "path", "status", "hunk"}
    assert len(state["changed"][1]["hunk"]) == cj.HUNK_CHARS
    assert state["mechanical"] == [
        {"kind": "unit_tests", "passed": True, "named": ["tests/test_view.py"]},
        {"kind": "ci", "passed": None}]
    assert state["artifacts"] == [{"kind": "pull_request", "present": True}]
    import inspect
    assert set(inspect.signature(cj.judgment_state).parameters) == {
        "contract", "commit", "changed", "mechanical", "artifacts", "requirement_ids"}
    questions = cj.judgment_questions(state)
    assert sorted(questions) == ["e1", "e2", "v1", "v2", "w1", "w2"]
    assert set(questions["e1"]["criteria"]) == {"i0", "i1", "m0", "m1", "none"}


def test_the_judgment_state_holds_under_the_cap_and_counts_what_it_cut():
    c = _judged_contract()
    changed = [{"path": f"src/f{i}.py", "status": "modified", "hunk": "y" * 1200}
               for i in range(300)]
    state = _judgment_state(c, changed=changed)
    assert len(json.dumps(state)) <= cj.MAX_STATE_CHARS
    assert len(state["changed"]) <= cj.MAX_CHANGED
    with_hunk = sum(1 for row in state["changed"] if "hunk" in row)
    assert state["cut"] == 300 - with_hunk
    assert len(cj.evidence_options(state)) <= cj.MAX_CHOICE_OPTIONS


def test_the_judgment_asks_only_requirements_naming_judgment():
    c = _judged_contract()
    with pytest.raises(ValueError):
        _judgment_state(c, requirement_ids=["REQ-1"])
    with pytest.raises(ValueError):
        _judgment_state(c, commit="main")


def _judgment_answers(k, verdict, reason, item, p=0.9):
    return {f"v{k}": _choice(verdict, p), f"w{k}": _choice(reason, p),
            f"e{k}": _choice(item, p)}


@pytest.mark.parametrize("answers", [
    None,
    {},
    _judgment_answers(2, "passed", "satisfied", "i0"),
    _judgment_answers(2, "met", "looks good", "i0"),
    _judgment_answers(2, "met", "satisfied", "i7"),
    _judgment_answers(2, "met", "satisfied", "src/login/view.py"),
    _judgment_answers(2, "met", "satisfied", "none"),
    {"v2": _choice("met", 0.9), "w2": _choice("satisfied", 0.9)},
    {"v2": {"choice": "met"}, "w2": _choice("satisfied", 0.9), "e2": _choice("i0", 0.9)},
    _judgment_answers(2, "met", "satisfied", "i0", p=0.69),
    _judgment_answers(2, "unmet", "not_in_diff", "i0", p=0.69),
], ids=["none", "empty", "unknown_verdict", "unknown_reason", "item_not_in_state",
        "item_as_a_path", "met_citing_none", "no_item", "no_probability", "met_under_0_7",
        "unmet_under_0_7"])
def test_a_malformed_or_unsure_judgment_reads_as_cannot_judge(answers):
    c = _judged_contract()
    state = _judgment_state(c, requirement_ids=["REQ-3"])
    (verdict,) = cj.read_judgment(c, state, answers)
    assert (verdict.requirement_id, verdict.verdict) == ("REQ-3", "cannot_judge")
    assert cj.judgment_outcome([verdict]) == "judgment_pending"
    assert cj.judgment_evidence(c, [verdict], commit=COMMIT) == ()


def test_met_writes_passing_judgment_evidence_and_unmet_sends_back():
    c = _judged_contract()
    state = _judgment_state(c)
    answers = {**_judgment_answers(1, "met", "satisfied", "i0"),
               **_judgment_answers(2, "unmet", "not_in_diff", "none", p=0.8)}
    verdicts = cj.read_judgment(c, state, answers)
    assert [(v.requirement_id, v.verdict, v.evidence_item) for v in verdicts] == [
        ("REQ-2", "met", "i0"), ("REQ-3", "unmet", None)]
    assert cj.judgment_outcome(verdicts) == "sent_back"
    evidence = cj.judgment_evidence(c, verdicts, commit=COMMIT)
    assert [(e.requirement_id, e.kind, e.passed, e.contract_digest) for e in evidence] == [
        ("REQ-2", "judgment", True, wc.digest(c)), ("REQ-3", "judgment", False, wc.digest(c))]
    unmet = wc.unmet(c, evidence, commit=COMMIT)
    assert "REQ-2" in unmet  # its review kind has no evidence yet
    assert "REQ-3" in unmet
    met_only = cj.read_judgment(c, state, {**_judgment_answers(1, "met", "satisfied", "i0"),
                                           **_judgment_answers(2, "met", "satisfied", "m0")})
    assert cj.judgment_outcome(met_only) == "met"
    assert cj.judgment_outcome([]) == "judgment_pending"


def test_two_send_backs_then_the_owner():
    assert cj.after_send_back(send_backs=0, retries_left=2) == "sent_back"
    assert cj.after_send_back(send_backs=1, retries_left=1) == "sent_back"
    assert cj.after_send_back(send_backs=2, retries_left=5) == "judgment_pending"
    assert cj.after_send_back(send_backs=0, retries_left=0) == "judgment_pending"


# --- protected.interfaces is never enforced by a judgment ------------------------


def test_protected_interfaces_are_never_enforced_by_a_judgment():
    doc = _doc(strictness="strict", scope={}, artifacts=[])
    doc["requirements"] = [{"id": "REQ-1", "text": "Never change the public API of Task.",
                            "verification": ["judgment"]}]
    c = wc.decode(doc)
    assert c.protected_interfaces == ("swarm_common.models.Task",)
    enforced = wc.enforced_with_judgment(wc.WORKER_ENFORCED_TODAY | {wc.COMPONENT_VERIFICATION})
    assert enforced - wc.WORKER_ENFORCED_TODAY == {wc.COMPONENT_VERIFICATION,
                                                     wc.COMPONENT_JUDGMENT}
    assert wc.COMPONENT_PROTECTED not in enforced
    with pytest.raises(wc.ContractNotEnforceable) as raised:
        wc.compile_contract(c, step_id="s", enforced=enforced)
    assert raised.value.detail["unenforced"] == ["protected"]
    state = _judgment_state(c)
    assert "swarm_common.models.Task" not in json.dumps(
        {k: v for k, v in state.items() if k != "requirements"})
    assert cj.judged_requirements(c) == ("REQ-1",)
    verdicts = cj.read_judgment(c, state, _judgment_answers(0, "met", "satisfied", "i0"))
    evidence = cj.judgment_evidence(c, verdicts, commit=COMMIT)
    assert {e.requirement_id for e in evidence} == {"REQ-1"}
    with pytest.raises(wc.ContractNotEnforceable):
        wc.compile_contract(c, step_id="s", enforced=enforced)
