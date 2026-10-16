"""swarm-graph's two measurable targets (docs/design/knowledge-graph.md §6, KG4; §7.7).

  * schema <= 2k tokens: every tool schema plus the server's instructions,
    which the agent pays for on every turn (GitNexus's is about 17.9k);
  * answer p95 < 300 ms on THIS repository's snapshot -- the design's target.
    CI holds it to P95_BUDGET_RATIO times a reference workload timed in the
    same loop, see there: a wall-clock limit measured the runner.

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
import statistics
import time
from collections import Counter
from pathlib import Path

import graphmcp_fixtures as gf

from agent_worker.graphmcp import server, tools

SCHEMA_TOKEN_BUDGET = 2000
BYTES_PER_TOKEN = 3
#: The design's target (knowledge-graph.md §6, KG4), printed, NOT asserted.
P95_TARGET_MS = 300.0
#: The CI gate: answer p95 over the median of `_reference_ms()`, the two timed
#: alternately in one loop, so a loaded runner slows both and the ratio holds.
#: Measured 2026-10-16 in a 6-core SwarmCloud container: 3.0-3.5 alone (p95
#: 78-85 ms), 2.95-3.65 beside 12 busy loops, where the absolute p95 was
#: 435-567 ms and the 450 ms limit this replaces failed two runs of three.
#: That limit (owner decision 2026-10-15, up from 300 after 304-411 ms on
#: loaded shared runners, #1334) measured the runner, not the server. 10 is
#: about 3x the measured ratio -- the headroom 450 ms gave over CI's 158 ms
#: unloaded -- so a server ~3x slower is red, and the test asserts below that
#: a 10x slower one is.
P95_BUDGET_RATIO = 10.0
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
    # did. Each run's p95 is read against a reference timed in the same loop
    # (see P95_BUDGET_RATIO): the unit shard runs this beside its `-n auto`
    # workers, and on 2026-10-15 a loaded shard put p95 at 327-351 ms where
    # the same tree, alone, measured 158 ms. A server over budget is over it
    # in every run, so it is still a red test.
    runs: list[float] = []
    for _ in range(BATTERIES):
        samples, reference = _battery(snapshot, questions, document["commit_sha"])
        p95 = _p95(samples)
        ratio = p95 / reference
        runs.append(ratio)
        print(f"swarm-graph: {len(samples)} answers, p50 {sorted(samples)[len(samples) // 2]:.1f} "
              f"ms, p95 {p95:.1f} ms (target {P95_TARGET_MS:.0f}), max {max(samples):.1f} ms; "
              f"reference {reference:.1f} ms, ratio {ratio:.2f}")
        # The budget must still tell: the same answers, ten times slower, fail it.
        assert _p95([s * 10 for s in samples]) / reference >= P95_BUDGET_RATIO, (
            f"a server 10x slower than this one ({10 * p95:.1f} ms) would pass")
        if ratio < P95_BUDGET_RATIO:
            break
    assert min(runs) < P95_BUDGET_RATIO, (
        f"answer p95 over {P95_BUDGET_RATIO}x the reference in all {len(runs)} runs: "
        f"{', '.join(f'{r:.2f}' for r in runs)}")


def _reference_ms() -> float:
    """A fixed interpreter-bound workload, of the kind an answer is (build,
    serialise, parse and sort Python objects): what the runner's load does to
    it, it does to the server's answers. Not the server's code, so a
    regression there cannot slow it too and cancel out."""
    started = time.perf_counter()
    table = {str(i): [i, str(i) * 3, {"k": i}] for i in range(3000)}
    for _ in range(3):
        json.loads(json.dumps(table))
        sorted(table.items(), key=lambda p: p[1][1])
    return (time.perf_counter() - started) * 1000.0


def _battery(snapshot: Path, questions: list[tuple[str, dict]],
             commit_sha: str) -> tuple[list[float], float]:
    """One timed run of `questions` on a fresh stdio server: a latency in ms
    each, and the median of `_reference_ms()` timed after each answer."""
    graph = gf.StdioServer(snapshot, workdir=gf.REPO_ROOT)
    try:
        graph.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
        graph.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        samples: list[float] = []
        references: list[float] = []
        answered = 0
        for tool, arguments in questions:
            started = time.perf_counter()
            text, failed = graph.call(tool, arguments)
            samples.append((time.perf_counter() - started) * 1000.0)
            references.append(_reference_ms())
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
    return samples, statistics.median(references)
