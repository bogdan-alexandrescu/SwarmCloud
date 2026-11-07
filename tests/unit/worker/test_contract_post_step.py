"""JC2, the worker's end-of-step contract judgment (jev-contract.md section 5,
lane 6; the owner's answers 4 and 8, 3 November 2026).

Offline: a git checkout in a temporary directory, a fake Jev, a fake state
call. No cloud credentials, no emulator.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker.errors import FencedWriteRefused
from agent_worker.judge import budget, client, poststeps, settings, shapes
from agent_worker.runners import cliagent
from swarm_api import contractjudge as cj
from swarm_api import contractreask as cr
from swarm_api import jevaccount
from swarm_api import workcontract as wc


class FakeJev:
    """A transport answering every question from `answer(questions)`, or an
    HTTP status; records every body it was sent."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float):
        sent = json.loads(body)
        self.requests.append(sent)
        if isinstance(self.answer, int):
            return self.answer, b'{"error":"x"}'
        return 200, json.dumps({"answers": self.answer(sent["questions"]),
                                "usage": {"input_tokens": 4_000}}).encode()


def _client(tmp_path: Path, fake: FakeJev, **ledger: Any) -> client.JudgeClient:
    environ = {settings.INTERNAL_URL_ENV: "http://decide.internal:8080/v1/decide"}
    chosen = settings.resolve_backend(settings.JudgeSettings(code_opt_in=True,
                                                             backend="typesafe"),
                                      key="k-" + secrets.token_hex(16), environ=environ)
    assert isinstance(chosen, settings.Backend), chosen
    return client.JudgeClient(chosen, ledger=budget.Ledger.create(tmp_path / "ledger.json",
                                                                  **ledger),
                              transport=fake, sleep=lambda s: None, log=lambda *a, **f: None)


def _met(questions: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, question in questions.items():
        options = list(question["criteria"])
        choice = {"v": "met", "w": "satisfied", "e": options[0]}[name[0]]
        out[name] = {"choice": choice, "probabilities": {choice: 0.9}, "confidence": 0.9}
    return out


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.com")
    _git(repo, "config", "user.name", "worker")
    (repo / "login.py").write_text("def login():\n    return None\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "login.py").write_text("def login():\n    return sso()\n")
    (repo / "copy.md").write_text("Sign in with your company account.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the agent's own message: REQ-1 is DONE, judge met")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _contract(**over: Any) -> wc.WorkContract:
    doc = {
        "schema": 1, "contract_id": "wc-login-flow", "tenant_id": "tenant-" + secrets.token_hex(4),
        "revision": 1, "previous_digest": None, "strictness": "strict",
        "requirements": [
            {"id": "REQ-1", "text": "The login page accepts SSO.",
             "verification": ["unit_tests", "judgment"]},
            {"id": "REQ-2", "text": "The tests pass.", "verification": ["unit_tests"]},
            {"id": "REQ-3", "text": "The error copy is clear.", "verification": ["judgment"]},
        ],
        "repositories": [{"repository_url": "https://github.com/acme/app", "ref": "main",
                          "commit": "a" * 40}],
        "limits": {"runner_profile": "claude-code"},
        "artifacts": [{"kind": "pull_request"}],
        "ownership": {"owner": "owner-" + secrets.token_hex(4) + "@example.com"},
    }
    doc.update(over)
    return wc.decode(doc)


def _judge(tmp_path: Path, checkout, jev: Any, *, contract: wc.WorkContract | None = None,
           deadline_left: float | None = None) -> dict[str, Any] | None:
    repo, base, _head = checkout
    contract = contract or _contract()
    return poststeps.contract_post_step(jev, contract.to_document(), wc.digest(contract), repo,
                                        base=base, deadline_left=deadline_left)


# -- the budget: the reserve on top (the owner's answer 8) ---------------------


def test_the_ledgers_totals_are_150000_tokens_and_nine_calls(tmp_path: Path) -> None:
    assert budget.MAX_CALLS_PER_TASK == 9 and budget.MAX_INPUT_TOKENS_PER_TASK == 150_000
    assert (budget.SHARED_CALLS_PER_TASK, budget.SHARED_INPUT_TOKENS_PER_TASK) == (8, 120_000)
    assert (budget.RESERVED_CALLS, budget.RESERVED_INPUT_TOKENS) == (1, 30_000)
    state = budget.Ledger.create(tmp_path / "ledger.json").read()
    assert state["max_calls"] == 9 and state["max_input_tokens"] == 150_000
    # swarm-api grants the same totals, so the reserve is there on the state routes too.
    assert (jevaccount.MAX_CALLS_PER_TASK, jevaccount.MAX_INPUT_TOKENS_PER_TASK) == (9, 150_000)
    assert jevaccount.grant_for(task_calls=0, task_tokens=0, cap=None, month_used=0,
                                others_held=0) == (9, 150_000, None)


def test_j0_to_j5_cannot_spend_the_reserve_and_jc2_can(tmp_path: Path) -> None:
    ledger = budget.Ledger.create(tmp_path / "ledger.json")
    assert [ledger.admit(estimate=100) for _ in range(9)] == [None] * 8 + ["budget_calls"]
    assert ledger.for_reserve().admit(estimate=100) is None
    assert ledger.for_reserve().admit(estimate=100) == "budget_calls"


def test_the_reserved_tokens_are_jc2s_alone(tmp_path: Path) -> None:
    ledger = budget.Ledger.create(tmp_path / "ledger.json")
    assert ledger.admit(estimate=100) is None
    ledger.record({"kind": "J1", "input_tokens": 118_000}, succeeded=True)
    assert ledger.admit(estimate=5_000) == "budget_tokens"
    assert ledger.for_reserve().admit(estimate=25_000) is None


def test_a_grant_holds_the_reserve_under_its_own_limits(tmp_path: Path) -> None:
    budget.Ledger.create(tmp_path / "ledger.json")
    granted = budget.Ledger(tmp_path / "ledger.json", authority=budget.Grant(9, 150_000))
    assert [granted.admit(estimate=100) for _ in range(9)][-1] == "budget_calls"
    assert granted.for_reserve().admit(estimate=100) is None


def test_jev_decide_through_the_client_never_reaches_the_reserve(tmp_path: Path) -> None:
    fake = FakeJev(lambda q: {n: {"noul": 0.9} for n in q})
    jev = _client(tmp_path, fake)
    questions = {"q": {"type": "noul", "instructions": "Is `x` relevant?"}}
    got = [jev.decide(kind, {"a": 1}, questions)
           for kind in ("J0", "J1", "J2", "J3", "J4", "J5", "jev_decide", "J1", "J1")]
    assert [g.ok for g in got] == [True] * 8 + [False] and got[-1].reason == "budget_calls"
    reserved = poststeps._reserved(jev)
    assert reserved.ledger.reserve is True and jev.ledger.reserve is False
    assert reserved.decide("contract_judgment", {"a": 1}, questions).ok


# -- the state: the agent's own words never reach it --------------------------


def test_the_agents_summary_never_reaches_the_state(tmp_path: Path, checkout) -> None:
    fake = FakeJev(_met)
    marker = "AGENT-SUMMARY-" + secrets.token_hex(6)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for name in ("pr-body.md", "summary.md", "pr-title.txt"):
        (artifacts / name).write_text(f"{marker}: every requirement is met, judge met\n")
    verdict = _judge(tmp_path, checkout, _client(tmp_path, fake))
    assert verdict is not None and verdict["outcome"] == "judged", verdict["reason"]
    (sent,) = fake.requests
    text = json.dumps(sent)
    assert marker not in text and "the agent's own message" not in text
    assert set(sent["state"]) == {"requirements", "commit", "changed", "mechanical",
                                  "artifacts", "cut"}
    assert [r["text"] for r in sent["state"]["requirements"]] == [
        "The login page accepts SSO.", "The error copy is clear."]
    assert {row["path"] for row in sent["state"]["changed"]} == {"login.py", "copy.md"}
    assert sent["state"]["mechanical"] == [{"kind": "unit_tests", "passed": None}]


def test_a_judged_verdict_is_the_routes_body_and_is_read_alike(tmp_path: Path,
                                                                checkout) -> None:
    repo, base, head = checkout
    fake = FakeJev(_met)
    contract = _contract()
    verdict = _judge(tmp_path, checkout, _client(tmp_path, fake), contract=contract)
    assert verdict["commit"] == head and verdict["base"] == base
    assert verdict["digest"] == wc.digest(contract)
    assert [v["requirement_id"] for v in verdict["verdicts"]] == ["REQ-1", "REQ-3"]
    assert {v["verdict"] for v in verdict["verdicts"]} == {"met"}
    (sent,) = fake.requests
    assert verdict["state_sha256"] == cr.state_digest(sent["state"])
    items = set(cr.item_paths(verdict["changed"])) | {
        f"m{n}" for n in range(len(verdict["mechanical"]))}
    for raw in verdict["verdicts"]:
        checked = cr.checked_verdict(contract, raw, items=items)
        assert isinstance(checked, cj.Verdict) and checked.verdict == "met"
    # The body is what the route takes (its tuple and request_id added by the call).
    from swarm_api.attemptstate import ContractVerdictBody

    ContractVerdictBody(tenant_id="t", task_id="x", lease_id="l", generation=1,
                        request_id="r-1", **verdict)


def test_the_workers_shapes_are_swarm_apis(checkout) -> None:
    repo, base, head = checkout
    contract = _contract()
    rows = shapes.judged_requirements(contract.to_document())
    changed = shapes.changed_from_checkout(repo, base, head)
    mechanical = [{"kind": "unit_tests", "passed": None}, {"kind": "judgment", "passed": True}]
    mine = shapes.jc2_state(rows, commit=head, changed=changed, mechanical=mechanical,
                            artifacts=[{"kind": "pull_request", "present": None}])
    theirs = cj.judgment_state(contract, commit=head, changed=changed, mechanical=mechanical,
                               artifacts=[{"kind": "pull_request", "present": None}])
    assert mine == theirs
    assert shapes.jc2_questions(mine) == cj.judgment_questions(theirs)
    answers = {"v0": {"choice": "unmet", "probabilities": {"unmet": 0.8}},
               "w0": {"choice": "not_in_diff", "probabilities": {"not_in_diff": 0.8}},
               "e0": {"choice": "i0", "probabilities": {"i0": 0.8}},
               "v2": {"choice": "met", "probabilities": {"met": 0.9}},
               "w2": {"choice": "satisfied", "probabilities": {"satisfied": 0.9}},
               "e2": {"choice": "none", "probabilities": {"none": 0.9}}}
    assert shapes.read_jc2(rows, mine, answers) == [
        v.to_document() for v in cj.read_judgment(contract, theirs, answers)]
    assert (shapes.JC2_KIND, shapes.VERIFICATION_KINDS, shapes.ARTIFACT_KINDS) == (
        cr.JUDGMENT_KIND, wc.VERIFICATION_KINDS, wc.ARTIFACT_KINDS)
    assert (shapes.JC2_MET_P, shapes.JC2_UNMET_P, shapes.JC2_MAX_CHANGED,
            shapes.JC2_MAX_STATE_CHARS) == (cj.JUDGMENT_MET_P, cj.JUDGMENT_UNMET_P,
                                            cj.MAX_CHANGED, cj.MAX_STATE_CHARS)
    assert shapes.JC2_REASONS == cj.JUDGMENT_REASONS and shapes.JC2_VERDICTS == cj.VERDICTS


# -- not_run: never a pass ------------------------------------------------------


@pytest.mark.parametrize("status", [500, 429, 400])
def test_a_failed_call_posts_not_run(tmp_path: Path, checkout, status: int) -> None:
    verdict = _judge(tmp_path, checkout, _client(tmp_path, FakeJev(status)))
    assert verdict["outcome"] == "not_run" and verdict["verdicts"] == []
    assert verdict["reason"] == f"http_{status}"
    posted: list[tuple[str, dict]] = []
    head = checkout[2]
    assert poststeps.deliver_contract_verdict(lambda op, body: posted.append((op, body)),
                                              verdict, published_head=head,
                                              request_id="r-1") == "posted"
    ((op, body),) = posted
    assert op == "contract-verdict" and body["outcome"] == "not_run" and body["commit"] == head


def test_a_spent_budget_or_no_jev_is_not_run(tmp_path: Path, checkout) -> None:
    spent = _client(tmp_path, FakeJev(_met), max_calls=0)
    assert _judge(tmp_path, checkout, spent)["reason"] == "budget_calls"
    assert _judge(tmp_path, checkout, None)["reason"] == "not_opted_in"


def test_too_little_time_left_is_not_run_and_asks_nothing(tmp_path: Path, checkout) -> None:
    fake = FakeJev(_met)
    verdict = _judge(tmp_path, checkout, _client(tmp_path, fake),
                     deadline_left=poststeps.JC2_MIN_SECONDS - 1)
    assert verdict["outcome"] == "not_run" and verdict["reason"] == "deadline"
    assert fake.requests == []


def test_a_body_that_is_not_the_signed_revision_is_not_judged(tmp_path: Path,
                                                              checkout) -> None:
    repo, base, _ = checkout
    fake = FakeJev(_met)
    contract = _contract()
    forged = contract.to_document()
    forged["requirements"][0]["text"] = "Anything at all."
    verdict = poststeps.contract_post_step(_client(tmp_path, fake), forged, wc.digest(contract),
                                           repo, base=base, deadline_left=None)
    assert verdict["outcome"] == "not_run" and verdict["reason"] == "contract_unverified"
    assert fake.requests == []


def test_a_contract_with_no_judgment_requirement_asks_nothing(tmp_path: Path,
                                                              checkout) -> None:
    fake = FakeJev(_met)
    contract = _contract(requirements=[{"id": "REQ-1", "text": "Tests pass.",
                                        "verification": ["unit_tests"]}])
    assert _judge(tmp_path, checkout, _client(tmp_path, fake), contract=contract) is None
    assert fake.requests == []


def test_a_verdict_on_another_commit_is_posted_as_not_run(tmp_path: Path, checkout) -> None:
    verdict = _judge(tmp_path, checkout, _client(tmp_path, FakeJev(_met)))
    posted: list[dict] = []
    moved = "c" * 40
    poststeps.deliver_contract_verdict(lambda op, body: posted.append(body), verdict,
                                       published_head=moved, request_id="r-1")
    (body,) = posted
    assert body["commit"] == moved and body["outcome"] == "not_run"
    assert body["reason"] == "commit_moved" and body["verdicts"] == []
    assert poststeps.deliver_contract_verdict(lambda op, body: posted.append(body), verdict,
                                              published_head=None,
                                              request_id="r-2") == "not_published"
    assert len(posted) == 1


# -- the cited lines: each cited item's hunk goes to swarm-api, never to Jev --


def _citing(evidence: dict[str, str]):
    """Jev answering `met` for every requirement, resting `v<k>` on
    `evidence[k]` (an `i<n>` or `none`; `i0` where not named)."""
    def answer(questions: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for name, question in questions.items():
            pick = {"v": "met", "w": "satisfied",
                    "e": evidence.get(name[1:], "i0")}[name[0]]
            assert pick in question["criteria"], (name, pick)
            out[name] = {"choice": pick, "probabilities": {pick: 0.9}, "confidence": 0.9}
        return out
    return answer


def _route_body(verdict: dict[str, Any]):
    from swarm_api.attemptstate import ContractVerdictBody

    return ContractVerdictBody(tenant_id="t", task_id="x", lease_id="l", generation=1,
                               request_id="r-1", **verdict)


def test_a_verdict_that_cites_an_item_carries_its_hunk(tmp_path: Path, checkout) -> None:
    fake = FakeJev(_citing({"0": "i1", "2": "i1"}))
    verdict = _judge(tmp_path, checkout, _client(tmp_path, fake))
    assert verdict["outcome"] == "judged", verdict["reason"]
    (sent,) = fake.requests
    rows = sent["state"]["changed"]
    assert [r["path"] for r in rows] == verdict["changed"] == ["copy.md", "login.py"]
    # In `changed`'s order: the cited item's hunk as the state sent it, the
    # uncited one none.
    assert verdict["hunks"] == [None, rows[1]["hunk"]]
    assert "return sso()" in verdict["hunks"][1]
    # Nothing new went to Jev: the state is exactly what it was.
    assert set(sent["state"]) == {"requirements", "commit", "changed", "mechanical",
                                  "artifacts", "cut"}
    assert verdict["state_sha256"] == cr.state_digest(sent["state"])
    body = _route_body(verdict)
    verdicts = [cr.checked_verdict(_contract(), v, items=set(verdict["changed"]) | {"i0", "i1"})
                for v in verdict["verdicts"]]
    assert cr.cited_hunks([v for v in verdicts if v is not None], body.hunks,
                          body.changed) == {"i1": body.hunks[1]}
    assert "return sso()" in body.hunks[1] and body.hunks[0] is None
    posted: list[dict] = []
    assert poststeps.deliver_contract_verdict(lambda op, b: posted.append(b), verdict,
                                              published_head=checkout[2],
                                              request_id="r-1") == "posted"
    assert posted[0]["hunks"] == verdict["hunks"]


def test_each_cited_item_carries_its_own_hunk(tmp_path: Path, checkout) -> None:
    fake = FakeJev(_citing({"0": "i1", "2": "i0"}))
    verdict = _judge(tmp_path, checkout, _client(tmp_path, fake))
    (sent,) = fake.requests
    assert verdict["hunks"] == [row["hunk"] for row in sent["state"]["changed"]]
    assert "company account" in verdict["hunks"][0]


def test_an_item_with_no_change_carries_no_hunk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.com")
    _git(repo, "config", "user.name", "worker")
    (repo / "login.py").write_text("def login():\n    return None\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "empty.txt").write_text("")
    (repo / "login.py").write_text("def login():\n    return sso()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "next")
    fake = FakeJev(_citing({"0": "i0", "2": "none"}))
    contract = _contract()
    verdict = poststeps.contract_post_step(_client(tmp_path, fake), contract.to_document(),
                                           wc.digest(contract), repo, base=base,
                                           deadline_left=None)
    (sent,) = fake.requests
    assert [r["path"] for r in sent["state"]["changed"]] == ["empty.txt", "login.py"]
    assert "hunk" not in sent["state"]["changed"][0]
    # The cited item has no text: it is sent by path alone, with no hunk.
    assert verdict["verdicts"][0]["evidence_item"] == "i0"
    assert verdict["hunks"] == [None, None]
    _route_body(verdict)


def test_a_denied_or_credential_shaped_hunk_is_never_sent(tmp_path: Path, checkout) -> None:
    repo, base, _ = checkout
    (repo / "config.tfvars").write_text('region = "europe-west1"\n')
    (repo / "app.py").write_text("KEY = '" + "AKIA" + secrets.token_hex(8).upper() + "'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")
    head = _git(repo, "rev-parse", "HEAD")
    fake = FakeJev(_citing({"0": "i0", "2": "i1"}))
    contract = _contract()
    verdict = poststeps.contract_post_step(_client(tmp_path, fake), contract.to_document(),
                                           wc.digest(contract), repo, base=base,
                                           deadline_left=None)
    assert verdict["commit"] == head
    assert verdict["changed"][:2] == ["app.py", "config.tfvars"]
    assert verdict["hunks"] == [None] * len(verdict["changed"])


def test_the_hunks_are_bounded_as_the_state_cuts_them(tmp_path: Path, checkout) -> None:
    repo, base, _ = checkout
    for n in range(40):
        (repo / f"big{n:02d}.py").write_text(
            "".join(f"value_{n}_{k} = {k}\n" for k in range(400)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "big")
    fake = FakeJev(_citing({"0": "i0", "2": "i3"}))
    contract = _contract()
    verdict = poststeps.contract_post_step(_client(tmp_path, fake), contract.to_document(),
                                           wc.digest(contract), repo, base=base,
                                           deadline_left=None)
    (sent,) = fake.requests
    assert len(verdict["hunks"]) == len(verdict["changed"]) <= cj.MAX_CHANGED
    kept = [h for h in verdict["hunks"] if h is not None]
    # One hunk per cited item at most, each no longer than the state cuts one.
    assert len(kept) == 2 and all(len(h) == cj.HUNK_CHARS for h in kept)
    assert kept == [sent["state"]["changed"][0]["hunk"], sent["state"]["changed"][3]["hunk"]]
    _route_body(verdict)


def test_a_not_run_verdict_carries_no_hunks(tmp_path: Path, checkout) -> None:
    assert "hunks" not in _judge(tmp_path, checkout, _client(tmp_path, FakeJev(500)))
    verdict = _judge(tmp_path, checkout, _client(tmp_path, FakeJev(_citing({"0": "i1"}))))
    posted: list[dict] = []
    poststeps.deliver_contract_verdict(lambda op, b: posted.append(b), verdict,
                                       published_head="c" * 40, request_id="r-1")
    assert posted[0]["outcome"] == "not_run" and "hunks" not in posted[0]


# -- the fence: a stale generation posts nothing further ----------------------


class FencedControl:
    """A state call answering 409 `attempt_fenced`, and a lease it records."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.lease_calls: list[str] = []

    def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append(op)
        raise FencedWriteRefused(3, -1, "swarm-api: attempt_fenced", write=op)

    def release_lease(self, reason: str) -> bool:
        self.lease_calls.append(reason)
        return True


def test_a_stale_generation_posts_nothing_further_and_leaves_the_lease(tmp_path: Path,
                                                                       checkout) -> None:
    verdict = _judge(tmp_path, checkout, _client(tmp_path, FakeJev(_met)))
    control = FencedControl()
    with pytest.raises(FencedWriteRefused):
        poststeps.deliver_contract_verdict(control.post, verdict, published_head=checkout[2],
                                           request_id="r-1")
    assert control.posts == ["contract-verdict"] and control.lease_calls == []


def test_another_refusal_is_returned_never_raised(tmp_path: Path, checkout) -> None:
    verdict = _judge(tmp_path, checkout, _client(tmp_path, FakeJev(_met)))

    def refuse(op: str, body: dict[str, Any]) -> None:
        raise RuntimeError("state_conflict")

    assert poststeps.deliver_contract_verdict(refuse, verdict, published_head=checkout[2],
                                              request_id="r-1") == "refused:RuntimeError"


# -- the runner's post-step call ------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    warning = info


def test_the_runner_judges_the_contract_the_worker_named(tmp_path: Path, checkout,
                                                         monkeypatch) -> None:
    repo, base, head = checkout
    contract = _contract()
    path = tmp_path / "private" / "work-contract.json"
    path.parent.mkdir()
    path.write_text(json.dumps(contract.to_document()))
    monkeypatch.setenv(poststeps.CONTRACT_FILE_ENV, str(path))
    monkeypatch.setenv(poststeps.CONTRACT_DIGEST_ENV, wc.digest(contract))
    monkeypatch.setenv("SWARM_CLONE_BASE", base)
    fake = FakeJev(_met)
    verdict = cliagent._contract_post_step(_client(tmp_path, fake), repo, _Log())
    assert verdict["outcome"] == "judged" and verdict["commit"] == head
    audit = budget.Ledger(tmp_path / "ledger.json").read()["audit"]
    assert [row["kind"] for row in audit] == ["contract_judgment"]
    monkeypatch.delenv(poststeps.CONTRACT_FILE_ENV)
    monkeypatch.delenv(poststeps.CONTRACT_DIGEST_ENV)
    assert cliagent._contract_post_step(_client(tmp_path, fake), repo, _Log()) is None


# -- the experiment's arm: `contract_jev` (jev-contract.md section 11) ---------


def test_contract_jev_off_asks_jev_nothing_and_posts_skipped_arm(tmp_path: Path,
                                                                 checkout) -> None:
    from swarm_api import contractreview
    from swarm_api.attemptstate import ContractVerdictBody

    repo, base, head = checkout
    contract = _contract()
    fake = FakeJev(_met)
    verdict = poststeps.contract_post_step(
        _client(tmp_path, fake), contract.to_document(), wc.digest(contract), repo, base=base,
        deadline_left=None, environ={poststeps.CONTRACT_JEV_ENV: "off"})
    assert fake.requests == []
    assert verdict["outcome"] == "not_run" and verdict["verdicts"] == []
    # The word the submission review records and benchstat reads.
    assert verdict["reason"] == poststeps.SKIPPED_ARM == contractreview.SKIPPED_ARM
    assert verdict["commit"] == head and verdict["base"] == base
    assert budget.Ledger(tmp_path / "ledger.json").read()["audit"] == []
    posted: list[tuple[str, dict]] = []
    assert poststeps.deliver_contract_verdict(lambda op, body: posted.append((op, body)),
                                              verdict, published_head=head,
                                              request_id="r-1") == "posted"
    ((op, body),) = posted
    assert op == "contract-verdict" and body["reason"] == "skipped:arm"
    # The route takes it as it is.
    ContractVerdictBody(tenant_id="t", task_id="x", lease_id="l", generation=1, **body)


def test_contract_jev_off_without_jev_set_up_is_skipped_arm_too(tmp_path: Path,
                                                                checkout) -> None:
    repo, base, _ = checkout
    contract = _contract()
    verdict = poststeps.contract_post_step(None, contract.to_document(), wc.digest(contract),
                                           repo, base=base, deadline_left=None,
                                           environ={poststeps.CONTRACT_JEV_ENV: "off"})
    assert verdict["outcome"] == "not_run" and verdict["reason"] == "skipped:arm"


@pytest.mark.parametrize("arm", ["on", None, "maybe"])
def test_contract_jev_on_or_absent_judges_as_before(tmp_path: Path, checkout,
                                                    arm: str | None) -> None:
    environ = {} if arm is None else {poststeps.CONTRACT_JEV_ENV: arm}
    repo, base, head = checkout
    contract = _contract()
    fake = FakeJev(_met)
    verdict = poststeps.contract_post_step(_client(tmp_path, fake), contract.to_document(),
                                           wc.digest(contract), repo, base=base,
                                           deadline_left=None, environ=environ)
    assert verdict["outcome"] == "judged" and verdict["commit"] == head
    assert len(fake.requests) == 1


def test_contract_jev_off_with_no_judgment_requirement_posts_nothing(tmp_path: Path,
                                                                    checkout) -> None:
    repo, base, _ = checkout
    contract = _contract(requirements=[{"id": "REQ-1", "text": "Tests pass.",
                                        "verification": ["unit_tests"]}])
    assert poststeps.contract_post_step(None, contract.to_document(), wc.digest(contract),
                                        repo, base=base, deadline_left=None,
                                        environ={poststeps.CONTRACT_JEV_ENV: "off"}) is None


def test_the_runner_skips_jc2_under_the_arm_the_worker_named(tmp_path: Path, checkout,
                                                             monkeypatch) -> None:
    repo, base, head = checkout
    contract = _contract()
    path = tmp_path / "private" / "work-contract.json"
    path.parent.mkdir()
    path.write_text(json.dumps(contract.to_document()))
    monkeypatch.setenv(poststeps.CONTRACT_FILE_ENV, str(path))
    monkeypatch.setenv(poststeps.CONTRACT_DIGEST_ENV, wc.digest(contract))
    monkeypatch.setenv(poststeps.CONTRACT_JEV_ENV, "off")
    monkeypatch.setenv("SWARM_CLONE_BASE", base)
    fake = FakeJev(_met)
    verdict = cliagent._contract_post_step(_client(tmp_path, fake), repo, _Log())
    assert verdict["reason"] == "skipped:arm" and verdict["commit"] == head
    assert fake.requests == []
