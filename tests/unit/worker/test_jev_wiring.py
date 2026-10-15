"""Jev's advisory outputs wired into the pipeline (lane CTXQ-JW,
docs/design/context-quality.md §4.3, §4.6, §5).

J1 reranks the context pack (`contextpack/rank.py`), J4 lists the test gap in
the gate's advisory section (`prepublish.py`), J3 annotates the review's
findings (`judge/poststeps.py`, `verdict.annotate_findings`), J2 and J5 write
the planner's advisory (`judge/poststeps.py`). What these tests hold, each
against a fake Jev on the client's own transport seam:

* each J's happy path;
* a Jev outage, a timeout, a 429, a 5xx, a malformed answer, an exhausted
  budget and an open breaker each leave every output BYTE-IDENTICAL to the
  local-only run (`judge=None`);
* an annotation never changes `verdict`, and the merge path reads the same;
* the setting off (or no key, or no backend) makes ZERO calls;
* the budget is ONE ledger shared by J1-J5 in one attempt;
* §5.4: a denied path is sent by name only, and a credential-shaped excerpt
  (built at runtime) never leaves.

MUTATIONS, each turning a named test red: blend without `p` in `rank.rerank`
(happy J1); use partial answers in `rank.rerank` (malformed == local);
drop the `p < JEV_DEMOTE_BELOW` branch (demoted kept and paged); write
`verdict.json` before checking every J3 answer (outage == local); drop the
`[verify before acting]` prefix in `verdict._finding_text` (marked for the
fixer); return the hold for a `required` run in `jev_holds_for_human`
(control-plane test).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from agent_worker import contextpack, prepublish, verdict as verdict_mod
from agent_worker.contextpack import rank
from agent_worker.contextpack.build import build_pack
from agent_worker.contextpack.retrieve import Candidate
from agent_worker.judge import attempt, budget, client, poststeps, register, settings, shapes

from test_contextpack import _pack, _stage, _step_prompt, built, work  # noqa: F401 -- fixtures
from test_prepublish_gate import Turns, _change, _env, _log, _tools
from test_prepublish_graph_gate import MOD, _fixture_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

INTERNAL_URL = "http://decide.internal:8080/v1/decide"


# ---------------------------------------------------------------------------
# The fake Jev: answers every question the request asks, by a rule
# ---------------------------------------------------------------------------


def _choice(options: list[str], pick: str, p: float) -> dict[str, Any]:
    probabilities = {o: (p if o == pick else (1 - p) / max(len(options) - 1, 1)) for o in options}
    return {"choice": pick, "probabilities": probabilities, "confidence": p}


class FakeJev:
    """The client's transport. `answer(kind_of_question, name, question, state)`
    returns one answer; `failure` (an int status, an exception or "malformed")
    replaces every response. Records every request."""

    def __init__(self, answer: Callable[..., Any] | None = None, *,
                 failure: Any = None) -> None:
        self.answer = answer or (lambda name, q, state: {"noul": 0.9} if q["type"] == "noul"
                                 else _choice(list(q["criteria"]), list(q["criteria"])[0], 0.9))
        self.failure = failure
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float):
        sent = json.loads(body)
        self.requests.append(sent)
        if isinstance(self.failure, BaseException):
            raise self.failure
        if isinstance(self.failure, int):
            return self.failure, b"{}"
        if self.failure == "malformed":
            return 200, json.dumps({"answers": {"nope": 1}, "usage": {"input_tokens": 5}}).encode()
        state = sent["state"]
        state = json.loads(state) if isinstance(state, str) else state
        answers = {name: self.answer(name, q, state) for name, q in sent["questions"].items()}
        return 200, json.dumps({"answers": answers, "usage": {"input_tokens": 1200}}).encode()

    def states(self) -> list[Any]:
        return [json.loads(r["state"]) if isinstance(r["state"], str) else r["state"]
                for r in self.requests]


def _judge(tmp_path: Path, fake: FakeJev, *, ledger: Path | None = None,
           **limits: Any) -> client.JudgeClient:
    chosen = settings.resolve_backend(settings.JudgeSettings(code_opt_in=True), key=None,
                                      environ={settings.INTERNAL_URL_ENV: INTERNAL_URL})
    assert isinstance(chosen, settings.Backend)
    path = ledger or tmp_path / "judge-ledger.json"
    return client.JudgeClient(chosen, budget.Ledger.create(path, **limits), transport=fake,
                              sleep=lambda s: None, log=lambda *a, **k: None)


def _spent(tmp_path: Path, **limits: Any) -> client.JudgeClient:
    """A client whose budget is already gone."""
    return _judge(tmp_path, FakeJev(), max_calls=0, **limits)


def _broken(tmp_path: Path) -> client.JudgeClient:
    """A client whose breaker is already open."""
    jev = _judge(tmp_path, FakeJev(failure=503))
    jev.decide("J0", {"x": 1}, {"q": {"type": "noul", "instructions": "x?"}})
    jev.decide("J0", {"x": 1}, {"q": {"type": "noul", "instructions": "x?"}})
    assert jev.ledger.read()["breaker_open"]
    return jev


#: Each §5.5 failure, as a client factory. Every one must equal judge=None.
FAILURES: dict[str, Callable[[Path], client.JudgeClient]] = {
    "outage": lambda t: _judge(t, FakeJev(failure=ConnectionRefusedError())),
    "timeout": lambda t: _judge(t, FakeJev(failure=TimeoutError())),
    "429": lambda t: _judge(t, FakeJev(failure=429)),
    "5xx": lambda t: _judge(t, FakeJev(failure=502)),
    "malformed": lambda t: _judge(t, FakeJev(failure="malformed")),
    "budget": _spent,
    "breaker": _broken,
}


# ---------------------------------------------------------------------------
# J1: the pack's rerank
# ---------------------------------------------------------------------------


def _pack_bytes(work: Path) -> tuple[bytes, bytes]:
    where = work / contextpack.CONTEXT_DIR_NAME
    return (where / contextpack.CONTEXT_FILE).read_bytes(), (where / contextpack.PACK_FILE).read_bytes()


def _payload() -> dict:
    return {"prompt": _step_prompt(["src/pkg/store.py"])}


def test_j1_reranks_the_pack_and_says_so(work, tmp_path):  # noqa: F811
    local = _pack(work, _payload())
    local_order = [r["ref"] for r in local["rows"] if r["kind"] not in ("in_flight", "cannot_push")]
    last = local_order[-1]

    def answer(name, q, state):
        cand = state["candidates"][int(name[1:])]
        # Jev wants the locally LAST row most of all, and nothing else much.
        wanted = (cand.get("path"), cand.get("symbol")) == _ref_parts(last)
        return {"noul": 0.99 if wanted else 0.5}

    fake = FakeJev(answer)
    pack = _pack(work, _payload(), judge=_judge(tmp_path, fake))
    assert len(fake.requests) == 1
    assert pack["jev"]["state"] == "on" and pack["jev_calls"] == 1
    assert pack["jev_input_tokens"] == 1200
    order = [r["ref"] for r in pack["rows"] if r["kind"] not in ("in_flight", "cannot_push")]
    assert order.index(last) < local_order.index(last)
    assert all("jev_p" in r for r in pack["rows"][:5] if r["kind"] not in rank_always())
    text = (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()
    assert "Jev: on (" in text


def _ref_parts(ref: str) -> tuple[str, str | None]:
    path, _, symbol = ref.partition("#")
    return path, symbol or None


def rank_always() -> tuple[str, ...]:
    from agent_worker.contextpack.retrieve import ALWAYS

    return tuple(ALWAYS)


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_j1_failure_leaves_the_pack_byte_identical_to_local_only(work, tmp_path, failure):  # noqa: F811
    _pack(work, _payload())
    local = _pack_bytes(work)
    _pack(work, _payload(), judge=FAILURES[failure](tmp_path))
    assert _pack_bytes(work) == local


def test_j1_partial_answers_are_not_used(work, tmp_path):  # noqa: F811
    _pack(work, _payload())
    local = _pack_bytes(work)

    class Partial(FakeJev):
        def __call__(self, url, body, headers, timeout):
            status, raw = super().__call__(url, body, headers, timeout)
            doc = json.loads(raw)
            doc["answers"].pop(sorted(doc["answers"])[0])
            return status, json.dumps(doc).encode()

    _pack(work, _payload(), judge=_judge(tmp_path, Partial()))
    assert _pack_bytes(work) == local


def _cands() -> list[Candidate]:
    rows = [Candidate(kind="symbol", ref=f"src/a{i}.py#f{i}", path=f"src/a{i}.py", why="seed",
                      score=10.0 - i) for i in range(70)]
    rows += [Candidate(kind="in_flight", ref="lane-x", path="src/a1.py", why="lane"),
             Candidate(kind="cannot_push", ref=".github/workflows/**", path=".github/workflows/**",
                       why="forge")]
    return rows


def test_j1_sends_the_top_sixty_and_never_in_flight_or_cannot_push(tmp_path):
    fake = FakeJev()
    rows = _cands()
    assert rank.rerank(rows, "implement", "brief", _judge(tmp_path, fake)) is not None
    (state,) = fake.states()
    assert len(state["candidates"]) == rank.J1_CANDIDATES == 60
    assert {c["kind"] for c in state["candidates"]} == {"symbol"}
    always = [c for c in rows if c.kind in ("in_flight", "cannot_push")]
    assert all(c.score == 0.0 and not c.extra for c in always)


def test_j1_demotes_below_015_past_the_budget_line_but_keeps_the_row(tmp_path):
    rows = _cands()
    fake = FakeJev(lambda name, q, s: {"noul": 0.05 if name == "c0" else 0.9})
    rank.rerank(rows, "implement", "brief", _judge(tmp_path, fake))
    best = rows[0]
    assert best.extra[rank.JEV_DEMOTED] is True and best.extra[rank.JEV_P] == 0.05
    chosen = rank.select(rows, "implement")
    assert best in chosen and chosen[-1] is best  # paged last, never deleted
    assert rank.select(rows, "implement", budget_rows=10)[0] is not best


def test_j1_final_score_is_half_local_half_p(tmp_path):
    rows = [Candidate(kind="symbol", ref="a.py#a", path="a.py", why="x", score=2.0),
            Candidate(kind="symbol", ref="b.py#b", path="b.py", why="x", score=1.0)]
    fake = FakeJev(lambda name, q, s: {"noul": 0.2 if name == "c0" else 1.0})
    rank.rerank(rows, "implement", "brief", _judge(tmp_path, fake))
    assert rows[0].score == pytest.approx(0.5 * 1.0 + 0.5 * 0.2)
    assert rows[1].score == pytest.approx(0.5 * 0.5 + 0.5 * 1.0)
    assert [c.ref for c in rank.ordered(rows)] == ["b.py#b", "a.py#a"]


def test_j1_drops_a_credential_shaped_excerpt_and_sends_names_only_for_denied_paths(tmp_path):
    token = "ghp_" + "Z9" * 18  # a GitHub token's shape, built here
    rows = [Candidate(kind="symbol", ref="src/x.py#f", path="src/x.py", why="seed", score=2.0,
                      evidence=f"api = '{token}'"),
            Candidate(kind="doc", ref="config/.env.prod", path="config/.env.prod",
                      why="names the store", score=1.0, evidence="DB_HOST=db.internal")]
    fake = FakeJev()
    rank.rerank(rows, "implement", "brief", _judge(tmp_path, fake))
    sent = json.dumps(fake.requests)
    assert token not in sent and "Z9Z9Z9" not in sent
    first, denied = fake.states()[0]["candidates"]
    assert "excerpt" not in first and first["path"] == "src/x.py"
    assert denied["path"] == "config/.env.prod"
    assert "excerpt" not in denied and "why" not in denied and "db.internal" not in sent


# ---------------------------------------------------------------------------
# J4: the gate's test gap
# ---------------------------------------------------------------------------


def _gate(repo: Path, tmp_path: Path, env: dict, judge: Any) -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=repo, env=env, secrets=(), resume=Turns(),
                           remaining=lambda: 3600.0, log=_log(), artifacts_dir=artifacts,
                           judge=judge)


def _changed_mod(tmp_path: Path) -> tuple[Path, dict]:
    repo, base = _fixture_repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "pkg/mod.py", MOD.replace("return 1", "return 3").replace("return 2", "return 4"))
    return repo, env


def _advisory(tmp_path: Path) -> bytes:
    return (tmp_path / "artifacts" / prepublish.ADVISORY_SECTION_NAME).read_bytes()


def test_j4_lists_changed_behaviours_no_test_would_catch(tmp_path):
    repo, env = _changed_mod(tmp_path)

    def answer(name, q, state):
        symbol = state["changed"][int(name[1:])]["symbol"]
        options = list(q["criteria"])
        return _choice(options, "none" if symbol.endswith("#lonely") else "0", 0.9)

    fake = FakeJev(answer)
    record = _gate(repo, tmp_path, env, _judge(tmp_path, fake)).run()
    assert len(fake.requests) == 1
    (state,) = fake.states()
    assert {c["symbol"] for c in state["changed"]} == {"pkg/mod.py#covered", "pkg/mod.py#lonely"}
    assert all("return" in c["hunk"] for c in state["changed"])
    assert [t["path"] for t in state["tests"]] == ["tests/test_mod.py"]
    assert record["jev_test_gap"]["uncovered"] == ["pkg/mod.py#lonely"]
    section = _advisory(tmp_path).decode()
    assert prepublish.JEV_SECTION_HEADING in section
    assert "no listed test would catch: `pkg/mod.py#lonely`" in section
    # An advisory never moves the gate: nothing failed, nothing was sent to the agent.
    assert record["failed"] == [] and record["fix_turn"] is False


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_j4_failure_leaves_the_advisory_and_record_identical_to_local_only(tmp_path, failure):
    local_dir, jev_dir = tmp_path / "local", tmp_path / "jev"
    local_dir.mkdir()
    jev_dir.mkdir()
    repo, env = _changed_mod(local_dir)
    local = _gate(repo, local_dir, env, None).run()
    repo2, env2 = _changed_mod(jev_dir)
    got = _gate(repo2, jev_dir, env2, FAILURES[failure](jev_dir)).run()
    assert _advisory(local_dir) == _advisory(jev_dir)
    assert "jev_test_gap" not in got
    assert set(got) == set(local)


# ---------------------------------------------------------------------------
# J3: the review's finding triage
# ---------------------------------------------------------------------------

PATCH = (
    "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n"
    "@@ -1,2 +1,2 @@\n def covered():\n-    return 1\n+    return 3\n"
)


def _review(tmp_path: Path, findings: list) -> tuple[Path, Path, Path, dict]:
    work = tmp_path / "work"
    checkout = work / "repo"
    (checkout / "pkg").mkdir(parents=True)
    (checkout / "pkg" / "mod.py").write_text(MOD)
    (checkout / ".env").write_text("SECRET_NAME=value-that-must-stay\n")
    (work / "inputs").mkdir()
    (work / "inputs" / "swarm-work.patch").write_text(PATCH)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    document = {"verdict": "NOT_YET", "findings": findings, "extra": {"kept": True}}
    (artifacts / "verdict.json").write_text(json.dumps(document))
    payload = {"prompt": "review; write verdict.json",
               "staged_inputs": [{"path": "inputs/swarm-work.patch"}]}
    return work, checkout, artifacts, payload


FINDINGS = [
    {"summary": "covered() now returns 3", "file": "pkg/mod.py", "line": 2, "side": "new",
     "severity": "major"},
    "lonely() is untested",
    {"summary": "the env file holds a value", "file": ".env", "line": 1},
    {"summary": "style nit", "severity": "minor", "file": "pkg/mod.py"},
]


def _j3_answer(p_by_summary: dict[str, float]) -> Callable[..., Any]:
    return lambda name, q, state: {"noul": p_by_summary[state["finding"]["summary"]]}


def test_j3_annotates_p_real_marks_low_ones_and_never_drops_or_changes_the_verdict(tmp_path):
    work, checkout, artifacts, payload = _review(tmp_path, FINDINGS)
    before = verdict_mod.read_verdict(artifacts / "verdict.json", task_id="r", filename="verdict.json")
    fake = FakeJev(_j3_answer({"covered() now returns 3": 0.92, "lonely() is untested": 0.1,
                               "the env file holds a value": 0.5, "style nit": 0.3}))
    done = poststeps.review_post_step(_judge(tmp_path, fake), artifacts, checkout, work, payload)
    assert done is not None and done["asked"] == 4
    doc = json.loads((artifacts / "verdict.json").read_text())
    assert doc["verdict"] == "NOT_YET" and doc["extra"] == {"kept": True}
    assert len(doc["findings"]) == 4
    assert doc["findings"][0]["p_real"] == 0.92 and "triage" not in doc["findings"][0]
    assert doc["findings"][1] == {"summary": "lonely() is untested", "p_real": 0.1,
                                  "triage": verdict_mod.VERIFY_BEFORE_ACTING}
    after = verdict_mod.read_verdict(artifacts / "verdict.json", task_id="r", filename="verdict.json")
    # The verdict, the minors and the locations read exactly as before.
    assert after.verdict == before.verdict
    assert after.minors == before.minors and after.locations == before.locations
    assert len(after.findings) == len(before.findings)
    assert after.findings[1].startswith("[verify before acting")
    assert after.findings[1].endswith("lonely() is untested")
    assert after.findings[0] == before.findings[0]
    # The code window is sent with the hunk; the denied path by name only.
    states = fake.states()
    assert "return 3" in states[0]["diff_hunk"] and "def lonely" in states[0]["code"]["window"]
    assert states[2]["code"] == {"path": ".env"}
    assert "value-that-must-stay" not in json.dumps(fake.requests)


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_j3_failure_leaves_verdict_json_byte_identical(tmp_path, failure):
    work, checkout, artifacts, payload = _review(tmp_path, FINDINGS)
    before = (artifacts / "verdict.json").read_bytes()
    assert poststeps.review_post_step(FAILURES[failure](tmp_path), artifacts, checkout, work,
                                      payload) is None
    assert (artifacts / "verdict.json").read_bytes() == before


def test_j3_one_failed_finding_annotates_none(tmp_path):
    work, checkout, artifacts, payload = _review(tmp_path, FINDINGS[:2])
    before = (artifacts / "verdict.json").read_bytes()

    class SecondFails(FakeJev):
        def __call__(self, url, body, headers, timeout):
            if self.requests:
                self.failure = 503
            return super().__call__(url, body, headers, timeout)

    poststeps.review_post_step(_judge(tmp_path, SecondFails()), artifacts, checkout, work,
                               payload)
    assert (artifacts / "verdict.json").read_bytes() == before


@pytest.mark.parametrize("p", [0.0, 0.01, 0.5, 1.0])
def test_an_annotation_never_changes_the_verdict_for_any_p(p):
    for verdict in verdict_mod.REVIEW_VERDICTS:
        doc = {"verdict": verdict, "findings": ["a", {"summary": "b"}]}
        out = verdict_mod.annotate_findings(doc, {0: p, 1: p})
        assert out["verdict"] == verdict and len(out["findings"]) == 2
        assert doc == {"verdict": verdict, "findings": ["a", {"summary": "b"}]}  # not mutated


def test_the_verify_threshold_is_the_shapes_one():
    assert verdict_mod.FINDING_VERIFY_BELOW == shapes.FINDING_VERIFY_BELOW


# ---------------------------------------------------------------------------
# J2 and J5: the planner's advisory
# ---------------------------------------------------------------------------

PLAN = {
    "summary": "Make the widget list sortable by name.",
    "requirements": ["sort by name", "keep the header accessible"],
    "steps": [{"step_id": "sort-key", "title": "Add a sort key", "files": ["src/sort.py"],
               "prompt": "Add a name sort key."}],
}
LANES = {"rows": [{"title": "Sort widgets by name", "ref": "swarm/sort", "files": ["src/sort.py"],
                   "author": "someone@example.com"}]}


def _planner(tmp_path: Path, *, lanes: bool = True) -> tuple[Path, Path]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "plan.json").write_text(json.dumps(PLAN))
    context = tmp_path / "work" / ".swarm-context"
    context.mkdir(parents=True)
    if lanes:
        (context / "inflight.json").write_text(json.dumps(LANES))
    return artifacts, context


def _plan_answer(dup_p: float) -> Callable[..., Any]:
    def answer(name, q, state):
        options = list(q["criteria"])
        if name == "dup":
            return _choice(options, "0", dup_p)
        return _choice(options, "sort-key" if name == "r0" else "none", 0.9)
    return answer


def test_j2_flags_uncovered_requirements_and_j5_holds_a_likely_duplicate(tmp_path):
    artifacts, context = _planner(tmp_path)
    before = (artifacts / "plan.json").read_bytes()
    fake = FakeJev(_plan_answer(0.9))
    advisory = poststeps.plan_post_step(_judge(tmp_path, fake), artifacts, context)
    assert len(fake.requests) == 2
    written = json.loads((artifacts / poststeps.PLAN_ADVISORY_FILE).read_text())
    assert written == advisory
    assert written["coverage"]["flagged"] == [{"index": 1, "requirement": "keep the header accessible"}]
    assert written["duplicate"]["ref"] == "swarm/sort" and written["hold_for_human"] is True
    assert (artifacts / "plan.json").read_bytes() == before
    # §5.4: in-flight lanes go as titles and files; never an author.
    assert "someone@example.com" not in json.dumps(fake.requests)


def test_j5_under_085_does_not_hold(tmp_path):
    artifacts, context = _planner(tmp_path)
    advisory = poststeps.plan_post_step(_judge(tmp_path, FakeJev(_plan_answer(0.84))),
                                        artifacts, context)
    assert advisory["duplicate"] is None and advisory["hold_for_human"] is False


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_j2_j5_failure_writes_no_advisory(tmp_path, failure):
    artifacts, context = _planner(tmp_path)
    assert poststeps.plan_post_step(FAILURES[failure](tmp_path), artifacts, context) is None
    assert sorted(p.name for p in artifacts.iterdir()) == ["plan.json"]


def test_the_api_reads_the_advisory_file_the_worker_writes():
    from swarm_api.routes import runs

    assert runs.PLAN_ADVISORY_FILE == poststeps.PLAN_ADVISORY_FILE


# ---------------------------------------------------------------------------
# The setting decides; off is zero calls
# ---------------------------------------------------------------------------


class _Snap:
    def __init__(self, data: dict | None) -> None:
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> dict | None:
        return self._data


class _Db:
    """Just enough Firestore: `collection(c).document(d).get()`."""

    def __init__(self, docs: dict[str, dict]) -> None:
        self.docs = docs

    def collection(self, name: str):
        db = self

        class _Col:
            def document(self, ident: str):
                class _Doc:
                    def get(self, **_kw):
                        return _Snap(db.docs.get(f"{name}/{ident}"))
                return _Doc()
        return _Col()


URL = "https://github.com/acme/widgets"


def _setup(tmp_path: Path, workspace: dict | None, environ: dict | None = None) -> dict:
    work = tmp_path / "att" / "work"
    work.mkdir(parents=True)
    docs = {} if workspace is None else {"workspace_settings/acme": workspace}
    return attempt.setup(work=work, db=_Db(docs), tenant_id="acme", repository_url=URL,
                         environ=environ if environ is not None
                         else {settings.INTERNAL_URL_ENV: INTERNAL_URL})


@pytest.mark.parametrize("workspace", [
    None, {}, {"code_intelligence": {"jev": False}}, {"code_intelligence": {"jev": "true"}},
])
def test_the_setting_off_writes_nothing_and_loads_no_client(tmp_path, workspace):
    assert _setup(tmp_path, workspace) == {}
    assert not (tmp_path / "att" / register.JUDGE_DIR).exists()
    assert attempt.load({}) == (None, "jev is off for this attempt")


def test_no_backend_url_or_no_typesafe_key_is_off(tmp_path):
    on = {"code_intelligence": {"jev": True}}
    assert _setup(tmp_path / "a", on, environ={}) == {}
    typesafe = {"code_intelligence": {"jev": True}, "judgment_backend": "typesafe"}
    assert _setup(tmp_path / "b", typesafe) == {}


def test_off_means_zero_calls_through_every_j(work, tmp_path, monkeypatch):  # noqa: F811
    """With no config, the runner's client is None, and every J site is given
    None: the pack, the gate, the post-steps run their local-only paths."""
    monkeypatch.delenv(attempt.CONFIG_ENV, raising=False)
    judge, _why = attempt.load()
    assert judge is None
    calls: list = []
    monkeypatch.setattr(client, "urllib_transport", lambda *a: calls.append(a) or (500, b""))
    _pack(work, _payload(), judge=judge)
    assert poststeps.plan_post_step(judge, tmp_path, None) is None
    assert poststeps.review_post_step(judge, tmp_path, None, tmp_path, {}) is None
    assert calls == []


def test_opted_in_writes_the_config_and_the_runner_loads_it(tmp_path):
    env = _setup(tmp_path, {"code_intelligence": {"jev": True}})
    config = Path(env[attempt.CONFIG_ENV])
    assert config.is_file() and config.parent == tmp_path / "att" / register.JUDGE_DIR
    judge, why = attempt.load({attempt.CONFIG_ENV: str(config)})
    assert why == "on" and judge is not None and judge.backend.url == INTERNAL_URL


def test_a_repository_turned_off_keeps_jev_off(tmp_path):
    from agent_worker import indexrun

    where = indexrun.target("acme", URL)
    work = tmp_path / "att" / "work"
    work.mkdir(parents=True)
    db = _Db({"workspace_settings/acme": {"code_intelligence": {"jev": True}},
              f"{indexrun.REPOSITORIES_COLLECTION}/{where.repo_id}":
                  {"tenant_id": "acme", "context": "off"}})
    assert attempt.setup(work=work, db=db, tenant_id="acme", repository_url=URL,
                         environ={settings.INTERNAL_URL_ENV: INTERNAL_URL}) == {}


# ---------------------------------------------------------------------------
# One budget for J1-J5 in one attempt
# ---------------------------------------------------------------------------


def test_the_budget_is_one_ledger_shared_by_j1_to_j5(work, tmp_path):  # noqa: F811
    """Five Js, each its own client over the attempt's one ledger file (as the
    runner and the MCP tool build theirs): the eighth call is the last."""
    ledger = tmp_path / "attempt-ledger.json"
    fake = FakeJev(_plan_answer(0.9))
    spent: list[client.JudgeClient] = []

    def fresh() -> client.JudgeClient:
        jev = _judge(tmp_path, fake, ledger=ledger)
        spent.append(jev)
        return jev

    _pack(work, _payload(), judge=fresh())                         # J1: 1 call
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    w, checkout, artifacts, payload = _review(review_dir, FINDINGS)  # J3: 4 calls
    poststeps.review_post_step(fresh(), artifacts, checkout, w, payload)
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    plan_artifacts, context = _planner(plan_dir)                     # J2 + J5: 2 calls
    poststeps.plan_post_step(fresh(), plan_artifacts, context)
    assert budget.Ledger(ledger).read()["calls"] == 7 == len(fake.requests)
    repo_dir = tmp_path / "gate"
    repo_dir.mkdir()
    repo, env = _changed_mod(repo_dir)
    _gate(repo, repo_dir, env, fresh()).run()                        # J4: the 8th
    assert budget.Ledger(ledger).read()["calls"] == 8 == len(fake.requests)
    # Past the budget: no request is made, and the output is the local one.
    plan_dir2 = tmp_path / "plan2"
    plan_dir2.mkdir()
    again, context2 = _planner(plan_dir2)
    assert poststeps.plan_post_step(fresh(), again, context2) is None
    assert len(fake.requests) == 8
    outcomes = [row["outcome"] for row in budget.Ledger(ledger).read()["audit"]]
    assert outcomes[-1] == "skipped:budget_calls"
    assert {row["kind"] for row in budget.Ledger(ledger).read()["audit"]} >= {
        "J1", "J2", "J3", "J4", "J5"}


def test_graphstage_hands_the_runner_the_config_only_when_opted_in(tmp_path, monkeypatch):
    """The lifecycle's one setup call (`graphstage.prepare`) carries Jev's
    config to the runner; no lifecycle change."""
    from agent_worker import graphstage

    seen: list[dict] = []

    def fake_setup(**kwargs):
        seen.append(kwargs)
        return {attempt.CONFIG_ENV: "/att/judge/config.json"}

    monkeypatch.setattr(attempt, "setup", fake_setup)
    monkeypatch.setattr(graphstage, "stage", lambda **kw: graphstage.StageResult(False, "no index"))
    work = tmp_path / "work"
    work.mkdir()
    (work / "repo").mkdir()
    env = graphstage.prepare(work, profile_name="claude-code", runner_argv=["claude"],
                             db=object(), store=object(), tenant_id="acme",
                             repository_url=URL, task_id="t", checkout=work / "repo",
                             base_sha="a" * 40)
    assert env.get(attempt.CONFIG_ENV) == "/att/judge/config.json"
    assert seen and seen[0]["tenant_id"] == "acme" and seen[0]["repository_url"] == URL


def test_cli_runner_review_post_step_writes_report_only_when_jev_answered(tmp_path, monkeypatch):
    from agent_worker.runners import cliagent
    from agent_worker.runners.base import RunnerContext

    work, checkout, artifacts, payload = _review(tmp_path, FINDINGS[:1])
    ctx = RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "in.json",
                        result_path=work / "r.json", quota_path=work / "q.json", payload=payload,
                        repo_dir=checkout)
    out = cliagent._jev_post_steps(ctx, FAILURES["outage"](tmp_path), checkout, _log())
    assert out == {"review": None}
    (tmp_path / "ok").mkdir()
    answering = _judge(tmp_path / "ok", FakeJev(lambda n, q, s: {"noul": 0.7}))
    out = cliagent._jev_post_steps(ctx, answering, checkout, _log())
    assert out["review"]["asked"] == 1
