"""swarm-graph's answer battery: the questions, how they are timed, and the
reference workload each answer is read against (knowledge-graph.md §6, KG4;
docs/design/graph-answer-cache.md §1.2, #1840).

Two measurements share it, so they ask the same questions measured the same
way and cannot drift apart:

  * the CI gate, `tests/unit/worker/test_graphmcp_budgets.py`, on its
    generated workload: answer p95 under `P95_BUDGET_RATIO` times the
    reference;
  * the indexer's opt-in `latency.json` (`repo_graph_shards.measure_latency`),
    on the snapshot it has just written, which the graph-latency-report
    schedule type reads as the same ratio.

Every timed run starts `python -m agent_worker.graphmcp` as a fresh child on
stdio, the way the agent's CLI runs it, so every run begins from cold shard
caches. The indexer runs this module as a child too
(`python -m agent_worker.graphmcp.battery`): its writer runs on the image's
system interpreter, which does not have this package.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from agent_worker.graphmcp import tools

#: How many runs `measure` makes by default; the indexer's reader takes the
#: median run by p95 (swarm_api.graphreports).
RUNS = 3
#: The slowest answers each run names in `measure`'s result.
SLOWEST = 5


class BatteryError(RuntimeError):
    """A battery that did not measure answers: a dead server, an answer over
    the cap, an answer about another commit, or mostly error answers."""


class _StdioServer:
    """`python -m agent_worker.graphmcp` as a fresh child, spoken to as an MCP client would."""

    def __init__(self, snapshot: Path, workdir: Path | None) -> None:
        argv = [sys.executable, "-m", "agent_worker.graphmcp", "--snapshot", str(snapshot)]
        if workdir is not None:
            argv += ["--workdir", str(workdir)]
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self._next = 0

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next += 1
        self.send({"jsonrpc": "2.0", "id": self._next, "method": method,
                   "params": params or {}})
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
            raise BatteryError(f"the graph server ended without answering: {stderr[-2000:]}")
        response = json.loads(line)
        if response.get("id") != self._next:
            raise BatteryError(f"the graph server answered request {response.get('id')}, "
                               f"not {self._next}")
        return response

    def call(self, tool: str, arguments: dict) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})["result"]
        return result["content"][0]["text"], result["isError"]

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]


def _questions(document: dict) -> list[tuple[str, dict]]:
    """A fixed battery over a graph document: the busiest callees (the worst
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


def _reference_ms() -> float:
    """A fixed interpreter-bound workload, of the kind an answer is (build,
    serialise, parse and sort Python objects): what the runner's load does to
    it, it does to the server's answers. Not the server's code, so a
    regression there cannot slow it too and cancel out.

    It runs in the battery's process while the answers come from the server's
    child process, and under `-n auto` the two can be scheduled on different
    cores, so the reference tracks the runner's load less tightly than a
    trivial call to the same server would. The trade is deliberate: a call to
    the server would slow down with a server regression and cancel it out.
    The ratios measured under 12 busy loops (the CI gate's P95_BUDGET_RATIO)
    stayed within 2.95-3.65, a third of the budget."""
    started = time.perf_counter()
    table = {str(i): [i, str(i) * 3, {"k": i}] for i in range(3000)}
    for _ in range(3):
        json.loads(json.dumps(table))
        sorted(table.items(), key=lambda p: p[1][1])
    return (time.perf_counter() - started) * 1000.0


def _battery(snapshot: Path, questions: list[tuple[str, dict]],
             commit_sha: str, workdir: Path | None) -> tuple[list[float], float]:
    """One timed run of `questions` on a fresh stdio server: a latency in ms
    each, in the order asked, and the median of `_reference_ms()` timed after
    each answer."""
    graph = _StdioServer(snapshot, workdir)
    try:
        graph.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "battery", "version": "0"}})
        graph.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        samples: list[float] = []
        references: list[float] = []
        answered = 0
        for tool, arguments in questions:
            started = time.perf_counter()
            text, failed = graph.call(tool, arguments)
            samples.append((time.perf_counter() - started) * 1000.0)
            references.append(_reference_ms())
            if len(text.encode()) > tools.ANSWER_CAP:
                raise BatteryError(f"{tool} {arguments} answered {len(text.encode())} bytes, "
                                   f"over the {tools.ANSWER_CAP}-byte cap")
            answer = json.loads(text)
            if next(iter(answer)) != "freshness":
                raise BatteryError(f"{tool} {arguments} answered without freshness first")
            if answer["freshness"]["index_sha"] != commit_sha:
                raise BatteryError(f"{tool} {arguments} answered from "
                                   f"{answer['freshness']['index_sha']}, not {commit_sha}")
            answered += not failed
        # Every question is about a symbol or file the extract holds, so a
        # battery that mostly failed would be measuring error paths.
        if answered < 0.9 * len(questions):
            raise BatteryError(f"{answered} of {len(questions)} questions answered")
    finally:
        graph.close()
    return samples, statistics.median(references)


def measure(snapshot: Path, document: dict, *, runs: int = RUNS, slowest: int = SLOWEST,
            workdir: Path | None = None) -> dict:
    """`runs` timed runs of the battery over `snapshot`, each on a fresh server:
    per run the p50, p95 and max answer, the reference and the slowest answers.
    These are the fields of latency.json the report's ratio is read from."""
    questions = _questions(document)
    if not questions:
        raise BatteryError("the graph document holds no symbol to ask about")
    measured = []
    for _run in range(max(1, runs)):
        samples, reference = _battery(snapshot, questions, document["commit_sha"], workdir)
        timed = sorted(zip(questions, samples), key=lambda qs: -qs[1])[:slowest]
        measured.append({
            "p50_ms": round(sorted(samples)[len(samples) // 2], 3),
            "p95_ms": round(_p95(samples), 3),
            "max_ms": round(max(samples), 3),
            "reference_ms": round(reference, 3),
            "slowest": [{"tool": tool, "symbol": arguments.get("symbol")
                         or arguments.get("query") or ",".join(arguments.get("files") or []),
                         "ms": round(ms, 3)} for (tool, arguments), ms in timed],
        })
    return {"questions": len(questions),
            "tools": list(dict.fromkeys(tool for tool, _arguments in questions)),
            "runs": measured}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="swarm-graph-battery",
        description="Time the answer battery over a staged snapshot; prints JSON.")
    parser.add_argument("--snapshot", required=True,
                        help="the staged snapshot directory (manifest.json, blobs/)")
    parser.add_argument("--document", required=True,
                        help="the graph document the snapshot was built from")
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--slowest", type=int, default=SLOWEST,
                        help="how many of each run's slowest answers to name")
    args = parser.parse_args(argv)
    document = json.loads(Path(args.document).read_bytes())
    try:
        result = measure(Path(args.snapshot), document, runs=args.runs, slowest=args.slowest)
    except BatteryError as exc:
        print(f"swarm-graph-battery: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
