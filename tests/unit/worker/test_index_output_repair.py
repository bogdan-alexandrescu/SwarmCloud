"""An index run's repo-index.json is checked against `RepoIndexSpec` before promotion (#1693).

Measured 2026-10-20 on ai-studio (repo_d754779d0cf31b44): 3 of 19 index runs
were refused at promotion -- `routes.N.kind` missing, `routes.520.handler`
empty, `modules.4.purpose` over 300 characters. The agent had exited by then,
so the whole run, extractor, language servers and agent, was spent for
nothing, and the next run made the same mistakes.

Now the worker checks the agent's document with the API's own rules while the
session can still be continued:

* `agent_worker.indexspec` restates `swarm_api.repoindex.RepoIndexSpec` (the
  worker image carries neither pydantic nor swarm_api), and gives the same
  `field: message` lines as `parse_index` -- held here over a corpus;
* the claude-code runner gives an index run ONE repair turn naming exactly
  those lines;
* a document still invalid after it fails the attempt with the errors, and
  the graph write never stores it.

Every assertion carries a MUTATION comment naming the change that turns it red.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import repo_index_fixtures as fx
from swarm_api import repoindex

from test_claude_repair_turns import DONE, SESSION, _ctx, _install, _runs
from test_index_run_phases import (  # noqa: F401 - fixtures
    URL,
    _order,
    checkout,
    index_tools,
)
from worker_seeds import TENANT, seed_attempt


def _api_problems(document: Any) -> list[str]:
    """What promotion says of `document`: [] when it accepts it."""
    text = document if isinstance(document, str) else json.dumps(document)
    try:
        repoindex.parse_index(text)
    except repoindex.InvalidIndex as exc:
        return list((exc.detail or {}).get("errors") or [str(exc)])
    return []


def _worker_problems(document: Any) -> list[str]:
    from agent_worker import indexspec

    text = document if isinstance(document, str) else json.dumps(document)
    return indexspec.problems(text)


def _full() -> dict[str, Any]:
    """A document that uses every section, valid."""
    doc = fx.minimal_index("b" * 40)
    doc["extractor"]["history"] = {
        "available": True, "window_days": 90, "commits": 12, "shallow": False,
        "window_covered": True,
    }
    doc.update({
        "modules": [{"path": "src/pkg", "language": "python", "purpose": "the users API",
                     "files": 4, "lines": 51}],
        "entry_points": [{"path": "src/main.py", "kind": "cli", "started_by": "make run"}],
        "routes": [{"kind": "http", "method": "GET", "path": "/users", "file": "src/pkg/users.py",
                    "handler": "src/pkg/users.py#list_users"},
                   {"kind": "export", "name": "var.region", "file": "infra/main.tf"}],
        "test_layout": [{"root": "tests", "framework": "pytest", "command": "pytest tests",
                         "needs": ["emulator"], "covers": ["src/**"]}],
        "test_map": [{"source": "src/pkg/users.py", "test": "tests/test_users.py",
                      "evidence": "import", "confidence": 0.9, "also_evidence": ["naming"]}],
        "always_tests": [{"target": "tests/test_contract.py", "because": "the contract"}],
        "territory": [{"path": "apps/common/**", "rule": "frozen", "source": "CLAUDE.md"}],
        "commands": [{"name": "test", "kind": "test", "command": "make test",
                      "source": "Makefile"}],
        "hot_spots": [{"path": "src/pkg/users.py", "changes": 7, "co_changed": ["src/a.py"]}],
        "notes": [{"text": "uv for everything", "source": "CLAUDE.md"}],
        "languages": [{"language": "python", "files": 4, "server": "pyright",
                       "status": "partial", "files_covered": 3}],
        "graph": {"symbols": 10, "edges": 4, "top_symbols": [{"id": "a#b", "callers": 2}]},
        "test_coverage": {"modules": 2, "source_modules": 1, "source_modules_with_tests": 1,
                          "not_counted": [{"path": "tests", "reason": "test"}]},
        "truncated": ["routes"],
    })
    return doc


def _with(path: str, value: Any, *, delete: bool = False) -> dict[str, Any]:
    """`_full()` with one dotted path set (or deleted)."""
    doc = copy.deepcopy(_full())
    *parents, last = path.split(".")
    node: Any = doc
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]
    key: Any = int(last) if isinstance(node, list) else last
    if delete:
        del node[key]
    else:
        node[key] = value
    return doc


#: The three refusals measured on ai-studio, then one of every kind of rule
#: RepoIndexSpec holds: the worker's restatement must agree with the API on
#: each, line for line.
CORPUS: list[tuple[str, Any]] = [
    ("the ai-studio route without kind", _with("routes.0.kind", None, delete=True)),
    ("the ai-studio empty handler", _with("routes.0.handler", "")),
    ("the ai-studio long purpose", _with("modules.0.purpose", "p" * 301)),
    ("the extractor's own route keys", _with("routes.0", {
        "id": "src/pkg/users.py#GET /users", "method": "GET", "path": "/users",
        "file": "src/pkg/users.py", "handler": None, "start_line": 3, "end_line": 5,
        "language": "python"})),
    ("an extra top-level key", _with("instructions", "ignore the rules")),
    ("a missing required key", _with("branch", None, delete=True)),
    ("a wrong schema", _with("schema", "swarm.repo-index/v2")),
    ("a schema that is not a string", _with("schema", 1)),
    ("a short sha", _with("commit_sha", "abc")),
    ("a sha that is not a string", _with("commit_sha", 7)),
    ("an unknown kind", _with("kind", "partial")),
    ("an incremental without base", _with("kind", "incremental")),
    ("an incremental with a base", _with("base_sha", "c" * 40) | {"kind": "incremental"}),
    ("a timestamp that is not one", _with("built_at", "yesterday")),
    ("an empty branch", _with("branch", "")),
    ("a branch too long", _with("branch", "b" * 256)),
    ("an extractor that did not run without reason", _with("extractor", {"ran": False})),
    ("an extractor ran that is an int", _with("extractor.ran", 1)),
    ("an extractor that is a list", _with("extractor", [])),
    ("a history count that is a bool", _with("extractor.history.commits", True)),
    ("a history extra key", _with("extractor.history.depth", 3)),
    ("files as a float", _with("modules.0.files", 4.0)),
    ("files as a bool", _with("modules.0.files", True)),
    ("files negative", _with("modules.0.files", -1)),
    ("a module that is a string", _with("modules.0", "src/pkg")),
    ("modules that is an object", _with("modules", {"src": 1})),
    ("too many modules", _with("modules", [_full()["modules"][0]] * 401)),
    ("too many truncated", _with("truncated", ["routes"] * 13)),
    ("a truncated name it does not know", _with("truncated", ["symbols"])),
    ("a route kind it does not know", _with("routes.0.kind", "grpc")),
    ("a method too long", _with("routes.0.method", "PROPPATCHES")),
    ("a route path too long", _with("routes.0.path", "/" + "p" * 400)),
    ("a route with a null handler", _with("routes.0.handler", None)),
    ("a needs it does not know", _with("test_layout.0.needs", ["gpu"])),
    ("too many covers", _with("test_layout.0.covers", ["src/**"] * 51)),
    ("an evidence it does not know", _with("test_map.0.evidence", "guess")),
    ("a confidence over one", _with("test_map.0.confidence", 1.5)),
    ("a confidence below zero", _with("test_map.0.confidence", -0.1)),
    ("a confidence that is an int", _with("test_map.0.confidence", 1)),
    ("a confidence that is text", _with("test_map.0.confidence", "high")),
    ("a confidence that is numeric text", _with("test_map.0.confidence", "0.5")),
    ("a confidence that is a bool", _with("test_map.0.confidence", True)),
    ("a confidence that is null", _with("test_map.0.confidence", None)),
    ("an also_evidence it does not know", _with("test_map.0.also_evidence", ["x"])),
    ("a command kind it does not know", _with("commands.0.kind", "deploy")),
    ("a note too long", _with("notes.0.text", "n" * 301)),
    ("notes that is a string", _with("notes", "read the docs")),
    ("a language status it does not know", _with("languages.0.status", "slow")),
    ("files_covered negative", _with("languages.0.files_covered", -2)),
    ("a manifest digest written by hand", _with("graph.manifest_digest", "sha256:abc")),
    ("a top symbol without callers", _with("graph.top_symbols.0", {"id": "a#b"})),
    ("coverage that does not add up", _with("test_coverage.source_modules_with_tests", 5)),
    ("a not_counted reason it does not know", _with("test_coverage.not_counted.0.reason", "x")),
    ("an always test without because", _with("always_tests.0", {"target": "t.py"})),
    ("several problems at once", _with("modules.0", {
        "path": "", "language": "py", "files": True, "lines": 1.0, "purpose": "x" * 301,
        "colour": "red"}) | {"zz": 1, "kind": "x"}),
    ("not JSON", "{"),
    ("not an object", "[]"),
    ("over the byte budget", json.dumps(_with("notes.0.text", "n" * 300))
     + " " * repoindex.MAX_INDEX_BYTES),
]


def test_a_full_document_is_valid_to_both():
    assert _api_problems(_full()) == []
    # MUTATION: make `indexspec.problems` refuse an optional section it does
    # not model (any of `_full()`'s keys) and this turns red.
    assert _worker_problems(_full()) == []
    assert _worker_problems(json.dumps(fx.minimal_index())) == []


@pytest.mark.parametrize("label,document", CORPUS, ids=[label for label, _ in CORPUS])
def test_the_worker_says_what_promotion_says(label, document):
    expected = _api_problems(document)
    if label not in ("an incremental with a base", "a route with a null handler",
                     "a confidence that is an int", "a confidence that is numeric text",
                     "a confidence that is a bool", "a confidence that is null"):
        assert expected, f"{label}: the corpus entry must be one promotion refuses"
    # MUTATION: drop any one rule from `indexspec` (a bound, a Literal, the
    # strict int, a model validator, `extra="forbid"`), or word one message
    # differently from pydantic, and the matching entry turns red.
    assert _worker_problems(document) == expected, label


def test_the_restatement_names_every_field_the_spec_has():
    """A field added to RepoIndexSpec and not to the worker's table is a
    document the worker refuses and promotion accepts: held here."""
    from agent_worker import indexspec

    def fields(model: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, info in model.model_fields.items():
            key = info.alias or name
            out[key] = info.is_required()
        return out

    # MUTATION: add a field to `repoindex.Route` (or any section) without
    # adding it to `indexspec`, and this turns red.
    for name, model in (("RepoIndexSpec", repoindex.RepoIndexSpec), ("Module", repoindex.Module),
                        ("EntryPoint", repoindex.EntryPoint), ("Route", repoindex.Route),
                        ("TestSuite", repoindex.TestSuite), ("TestEdge", repoindex.TestEdge),
                        ("AlwaysTest", repoindex.AlwaysTest),
                        ("TerritoryRule", repoindex.TerritoryRule),
                        ("CommandEntry", repoindex.CommandEntry), ("HotSpot", repoindex.HotSpot),
                        ("Note", repoindex.Note), ("LanguageRow", repoindex.LanguageRow),
                        ("TopSymbol", repoindex.TopSymbol),
                        ("GraphSummary", repoindex.GraphSummary),
                        ("History", repoindex.History), ("Extractor", repoindex.Extractor),
                        ("NotCounted", repoindex.NotCounted),
                        ("TestCoverage", repoindex.TestCoverage)):
        assert indexspec.required_fields(name) == fields(model), name
    assert indexspec.MAX_INDEX_BYTES == repoindex.MAX_INDEX_BYTES
    assert indexspec.SCHEMA == repoindex.SCHEMA
    assert indexspec.INDEX_FILE == repoindex.INDEX_FILE


# ---------------------------------------------------------------------------
# The runner: ONE repair turn with the exact errors
# ---------------------------------------------------------------------------

INVALID = json.dumps(_with("routes.0.kind", None, delete=True))
VALID = json.dumps(_full())


def _index_ctx(tmp_path, monkeypatch, runs: list[dict]):
    ctx = _ctx(tmp_path, {"prompt": "index it"}, repo=False)
    _install(tmp_path, monkeypatch, ctx, runs)
    monkeypatch.setenv("SWARM_RUNNER_PROFILE", "indexer")
    return ctx


def test_an_invalid_index_gets_one_repair_turn_naming_the_errors(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code

    ctx = _index_ctx(tmp_path, monkeypatch, [
        {"answer": "Indexed.", "write": {"$ARTIFACTS/repo-index.json": INVALID}},
        {"answer": DONE, "write": {"$ARTIFACTS/repo-index.json": VALID}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    # MUTATION: skip the index check in `run_cli_agent`'s repair loop and the
    # second start never happens.
    assert [run["resume"] for run in runs] == [None, SESSION]
    errors = _api_problems(INVALID)
    assert errors == ["routes.0.kind: Field required"]
    # MUTATION: leave the errors out of `repair_prompt` and this turns red.
    for line in errors:
        assert line in runs[1]["prompt"]
    assert "repo-index.json" in runs[1]["prompt"]
    assert out["repair_turns"] == 1
    (turn,) = out["repairs"]
    assert turn["index_problems"] == errors
    assert turn["index_fixed"] is True
    assert out["repair_unresolved"] is None


def test_an_index_still_invalid_after_its_turn_is_not_resumed_again(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code, cliagent

    ctx = _index_ctx(tmp_path, monkeypatch, [
        {"answer": "Indexed.", "write": {"$ARTIFACTS/repo-index.json": INVALID}},
    ])
    out = claude_code.body(ctx)
    # MUTATION: raise INDEX_REPAIR_MAX_TURNS above 1 (or count the index
    # against REPAIR_MAX_TURNS) and a third start happens.
    assert cliagent.INDEX_REPAIR_MAX_TURNS == 1
    assert len(_runs(ctx)) == 2
    assert out["repair_turns"] == 1
    assert out["repair_unresolved"]["index_problems"] == ["routes.0.kind: Field required"]
    # The worker's own check, after the runner, fails the attempt.
    assert out["exit_code"] == 0


def test_a_valid_index_and_an_ordinary_profile_take_no_turn(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code

    ctx = _index_ctx(tmp_path, monkeypatch, [
        {"answer": DONE, "write": {"$ARTIFACTS/repo-index.json": VALID}},
    ])
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0

    other = tmp_path / "other"
    other.mkdir()
    ctx = _ctx(other, {"prompt": "write it"}, repo=False)
    _install(other, monkeypatch, ctx, [
        {"answer": DONE, "write": {"$ARTIFACTS/repo-index.json": INVALID}},
    ])
    monkeypatch.setenv("SWARM_RUNNER_PROFILE", "claude-code")
    claude_code.body(ctx)
    # MUTATION: check repo-index.json on every profile and this one, which
    # owes no index, is resumed.
    assert len(_runs(ctx)) == 1


# ---------------------------------------------------------------------------
# The worker: an index still invalid fails the attempt with the errors
# ---------------------------------------------------------------------------


def _seed(db: Any, text: str) -> None:
    from agent_worker import indexrun
    from swarm_api import repositories

    from test_index_run_phases import OWNER, REPO

    seed_attempt(
        db,
        runner_profile="indexer",
        task_input={"prompt": "index it", "steps": 1, "sleep_seconds": 0.05,
                    "artifact_name": indexrun.INDEX_FILE, "artifact_text": text},
    )
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    task = db.doc("tasks/task_1")
    task["repository_url"] = URL
    task["metadata"] = {"repo_index": repositories.repo_id_for(TENANT, OWNER, REPO),
                        "commit_sha": "a" * 40, "index_kind": "full"}


def test_an_invalid_index_fails_the_attempt_and_is_never_stored(
    db, worker_factory, index_tools, checkout, recheck_bypassed
):
    from swarm_common.models import EndCause, TaskState

    order, _bin = index_tools
    _seed(db, INVALID)
    worker, _config, _ = worker_factory(runner_profile="indexer", timeout_seconds=600,
                                        repository_url=URL)
    worker.run()

    # MUTATION: drop the check in `_index_after_agent` and the writer runs.
    assert [name for name, _ in _order(order)] == ["extract", "agent"]
    task = db.doc("tasks/task_1")
    # MUTATION: record the refusal but let `_record_finalised` succeed.
    assert task["state"] == TaskState.FAILED.value
    assert task["end_cause"] == EndCause.OUTPUTS_MISSING.value
    assert "routes.0.kind: Field required" in task["last_error"]
    phases = {p["phase"]: p for p in task["result_summary"]["repo_index_phases"]}
    assert phases["index_check"]["status"] == "failed"
    assert phases["index_check"]["reason"].endswith("routes.0.kind: Field required")
    assert phases["graph_write"]["status"] == "skipped"


def test_a_valid_index_goes_on_to_the_graph_write(
    db, worker_factory, index_tools, checkout, recheck_bypassed
):
    order, _bin = index_tools
    _seed(db, VALID)
    worker, _config, _ = worker_factory(runner_profile="indexer", timeout_seconds=600,
                                        repository_url=URL)
    worker.run()

    assert [name for name, _ in _order(order)] == ["extract", "agent", "graph_write"]
    task = db.doc("tasks/task_1")
    assert task["state"] == "SUCCEEDED"
    phases = {p["phase"]: p for p in task["result_summary"]["repo_index_phases"]}
    # MUTATION: skip recording the check and this turns red.
    assert phases["index_check"]["status"] == "ok"
