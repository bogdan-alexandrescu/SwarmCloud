"""Snapshots for the swarm-graph tests, built by KG2's own extractor and writer.

A snapshot is what lane KG5 will stage beside a step's checkout: the
manifest `repo_graph_shards.build` produces and every blob it names, laid out
as `agent_worker.graphmcp.snapshot` reads them. Building it here from a real
extract, rather than checking one in, means the server is tested against the
format the indexer writes today, not against a copy of it that cannot drift.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
TENANT = "eng"
REPO_ID = "repo_0123456789abcdef"


def load_tool(name: str) -> Any:
    """`repo_index_extract` or `repo_graph_shards`, loaded once per process."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"no {name} under {TOOL_DIR}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def graph_of(repo: Path) -> dict:
    """The KG2 graph document of the checkout at `repo`, without the LSP pass."""
    extract = load_tool("repo_index_extract")
    facts = extract.extract(Path(repo), extract.Budget())
    return json.loads(extract.dumps(extract.graph_document(facts)))


def write_snapshot(document: dict, dest: Path) -> Path:
    """`dest` laid out as a staged snapshot: manifest.json and blobs/."""
    shards = load_tool("repo_graph_shards")
    _key, manifest, blobs = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID)
    (dest / "blobs").mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_bytes(manifest)
    for hexdigest, data in blobs.items():
        (dest / "blobs" / f"{hexdigest}{shards.BLOB_SUFFIX}").write_bytes(data)
    return dest


class StdioServer:
    """`python -m agent_worker.graphmcp` as a child, spoken to as an MCP client would."""

    def __init__(self, snapshot: Path, workdir: Path | None = None) -> None:
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
        assert line, self.proc.stderr.read().decode() if self.proc.stderr else "no answer"
        response = json.loads(line)
        assert response["id"] == self._next
        return response

    def call(self, tool: str, arguments: dict) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": tool, "arguments": arguments})["result"]
        return result["content"][0]["text"], result["isError"]

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
