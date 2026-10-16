"""swarm-graph's two measurable targets (docs/design/knowledge-graph.md §6, KG4; §7.7).

  * schema <= 2k tokens: every tool schema plus the server's instructions,
    which the agent pays for on every turn (GitNexus's is about 17.9k);
  * answer p95 < 300 ms on THIS repository's snapshot -- the design's target.
    CI holds it to P95_BUDGET_MS (450 ms), see there.

The snapshot is built here, from the checkout under test, with KG2's own
extractor and shard writer (`graphmcp_fixtures`), and the server is run as the
agent's CLI will run it: a child process on stdio. That costs about 20 s of
extract and shard work, so everything that needs the real snapshot is ONE
test: under `pytest -n auto` a module fixture would be built once per worker
that picks up one of its tests.

Tokens are counted without a tokenizer, which the unit run does not have:
at most `BYTES_PER_TOKEN` bytes a token is a conservative ratio for JSON
(English prose runs about four), so a schema under 2,000 * 3 bytes is under
2,000 tokens however the model splits it.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path

import graphmcp_fixtures as gf

from agent_worker.graphmcp import server, tools

SCHEMA_TOKEN_BUDGET = 2000
BYTES_PER_TOKEN = 3
#: The CI gate, NOT the design's 300 ms target (knowledge-graph.md §6, KG4).
#: Owner decision 2026-10-15: raised from 300 after the test failed 3 of 4
#: runs on loaded shared runners (304-411 ms, best of three batteries), on
#: PRs and on main, where a red unit shard skips `build images` and the
#: release ships nothing. The same tree alone measured 158 ms, so 450 still
#: catches a server that regressed by ~3x. Bringing the real p95 back under
#: 300 on a busy runner is the caching follow-up, not this number.
P95_BUDGET_MS = 450.0
#: Timed runs of the battery the p95 test allows; it passes on the best (see there).
BATTERIES = 3
#: §7.7's cap, written here as the number, so a larger `ANSWER_CAP` is a red test.
ANSWER_CAP_BYTES = 4096


def test_the_answer_cap_is_four_kibibytes():
    assert tools.ANSWER_CAP == ANSWER_CAP_BYTES


def test_the_tool_schemas_and_instructions_are_under_two_thousand_tokens():
    listed = json.dumps({"tools": tools.TOOLS}, separators=(",", ":"))
    spent = len(listed.encode()) + len(server.INSTRUCTIONS.encode())
    assert spent <= SCHEMA_TOKEN_BUDGET * BYTES_PER_TOKEN, (
        f"{spent} bytes of schema is over {SCHEMA_TOKEN_BUDGET} tokens at "
        f"{BYTES_PER_TOKEN} bytes a token")


def test_there_are_at_most_ten_tools_and_they_are_the_designs():
    # KG4's six, context-quality.md §4.4's two offline additions (CTXQ-T), and
    # graph-service.md §2.3's two (GS-CORE), inside the same 2k-token budget.
    assert tools.TOOL_NAMES == ("search", "context", "impact", "tests_for", "neighbours",
                                "territory", "similar_changes", "rules_for",
                                "call_graph", "index")
    assert set(tools.HANDLERS) == set(tools.TOOL_NAMES)


def test_the_two_service_tools_fit_in_what_the_eight_left():
    """graph-service.md §2.3: the ceiling is not raised for call_graph and index;
    if they do not fit, their descriptions are shortened."""
    new = [t for t in tools.TOOLS if t["name"] in ("call_graph", "index")]
    assert len(json.dumps(new, separators=(",", ":"))) <= 1000


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]


def _questions(document: dict) -> list[tuple[str, dict]]:
    """A fixed battery over this repository: the busiest callees (the worst
    case for `impact` and `context`) and an even sample of the rest."""
    test_files = {f["path"] for f in document["files"] if f.get("test")}
    application = sorted(s["id"] for s in document["symbols"]
                         if s["kind"] in ("function", "method", "class")
                         and s["path"] not in test_files and "@" not in s["id"])
    fan_in = Counter(e["to"] for e in document["call_edges"] if e["kind"] != "import")
    busiest = [s for s, _n in sorted(fan_in.items(), key=lambda p: (-p[1], p[0]))
               if s in set(application)][:4]
    step = max(len(application) // 12, 1)
    sample = application[::step][:12]
    questions: list[tuple[str, dict]] = []
    for symbol in busiest + sample:
        name = symbol.split("#", 1)[1].split(".")[-1]
        path = symbol.split("#", 1)[0]
        questions += [
            ("search", {"query": name.replace("_", " ")}),
            ("context", {"symbol": symbol}),
            ("impact", {"symbol": symbol, "depth": 3}),
            ("tests_for", {"files": [path]}),
            ("neighbours", {"symbol": symbol}),
            ("territory", {"files": [path]}),
        ]
    return questions


def test_answers_on_this_repositorys_snapshot_are_under_the_p95_budget(tmp_path: Path):
    document = gf.graph_of(gf.REPO_ROOT)
    assert len(document["symbols"]) > 10_000, "the extract did not read this repository"
    snapshot = gf.write_snapshot(document, tmp_path / "snapshot")
    questions = _questions(document)
    assert len(questions) >= 90, len(questions)

    # THE BEST OF UP TO `BATTERIES` RUNS, each the whole battery on a fresh
    # server, so every run starts from cold shard caches exactly as one run
    # did. The unit shard runs this beside its `-n auto` workers, and one
    # wall-clock run measures the runner's contention as much as the server:
    # on 2026-10-15 a loaded shard put p95 at 327-351 ms where the same tree,
    # alone, measured 158 ms. A server over budget is over it in every run, so
    # it is still a red test. The budget moved once, to 450 ms (see
    # P95_BUDGET_MS), by owner decision; do not move it again to fix a flake.
    runs: list[float] = []
    for _ in range(BATTERIES):
        samples = _battery(snapshot, questions, document["commit_sha"])
        p95 = _p95(samples)
        runs.append(p95)
        print(f"swarm-graph: {len(samples)} answers, p50 {sorted(samples)[len(samples) // 2]:.1f} "
              f"ms, p95 {p95:.1f} ms, max {max(samples):.1f} ms")
        if p95 < P95_BUDGET_MS:
            break
    assert min(runs) < P95_BUDGET_MS, (
        f"answer p95 over {P95_BUDGET_MS} ms in all {len(runs)} runs: "
        f"{', '.join(f'{r:.1f}' for r in runs)} ms")


def _battery(snapshot: Path, questions: list[tuple[str, dict]], commit_sha: str) -> list[float]:
    """One timed run of `questions` on a fresh stdio server: a latency in ms each."""
    graph = gf.StdioServer(snapshot, workdir=gf.REPO_ROOT)
    try:
        graph.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
        graph.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        samples: list[float] = []
        answered = 0
        for tool, arguments in questions:
            started = time.perf_counter()
            text, failed = graph.call(tool, arguments)
            samples.append((time.perf_counter() - started) * 1000.0)
            assert len(text.encode()) <= ANSWER_CAP_BYTES, (tool, arguments, len(text))
            answer = json.loads(text)
            assert next(iter(answer)) == "freshness", (tool, answer)
            assert answer["freshness"]["index_sha"] == commit_sha
            answered += not failed
        # Every question is about a symbol or file the extract holds, so a
        # battery that mostly failed would be measuring error paths.
        assert answered >= 0.9 * len(questions), answered
    finally:
        graph.close()
    return samples
