"""The base-graph read fits the budget its size is given, or says which read ran out (#1469).

An incremental index run reads its base graph back (`swarm-repo-graph read`,
phase `stage_base`) before the extractor. It was given a flat quarter of the
extractor's budget, 90 s of a 900-s run, and was measured running past it
(#1334 box 6181714762) on a base graph of about 63 MB.

Measured for this lane on this repository's own graph (`swarm-repo-index` at
d2b2d14: 98,248,874 bytes, 54,542 symbols, 120,131 call edges): the read
fetches 744 blobs, 8.8 MB stored, and decoding them and writing the document
back takes about 4 s of CPU. The rest is the download: every blob went through
`gcloud storage cp` in batches of up to 500, run one after another, so its cost
was per object and serial. What is held here:

* the download runs its batches side by side (`GET_PROCESSES` gcloud
  processes), so a 60 MB graph shaped like the real one -- with more blobs
  a MB than it has, ~1,070 to read against 7.6 a MB -- is read inside the
  budget its size gives, against a gcloud that charges per call and per
  object (scaled down by `SCALE`, CPU not scaled). Read in the old serial
  batches, the same charges come to 49 s scaled (measured), over its 25 s;
* the budget is sized from the base graph's size (`indexrun.budgets(...,
  base_graph_bytes=...)`), which the base index's graph counts give;
* a read that runs out of its budget is named in the phase record and the
  log with the last progress line the tool wrote, and the run is FULL: no
  partial base graph is left for the extractor and it is given no base.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import indexrun
from swarm_api import repoindex

from test_index_run_incremental import (  # noqa: F401 - fixtures
    BASE,
    URL,
    _order,
    _phases,
    _seed,
    checkout,
    index_tools,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WRITER = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index" / "repo_graph_shards.py"
TENANT, REPO_ID, BUCKET = "eng", "repo_" + "1" * 16, "swarm-artifacts-test"

#: Production's per-call and per-object gcloud cost, divided by this so the
#: test runs in seconds; the budget is divided by it too. The CPU the read
#: spends decoding is NOT scaled, which only makes the bound harder to meet.
SCALE = 5.0
#: About 4 s to start `gcloud storage` (the writer's ~3.9 s a process, measured
#: on the serial upload), and at least 0.16 s an object: 63 MB is ~480 blobs at
#: the 7.6 blobs/MB measured here, and their two serial batches and the
#: manifest's `cat` ran past 90 s.
GCLOUD_CALL_SECONDS = 4.0
GCLOUD_OBJECT_SECONDS = 0.2


@pytest.fixture(scope="module")
def shards() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# a 60 MB graph, shaped like this repository's
# --------------------------------------------------------------------------

#: Per file, as this repository's graph has them (3,069 files): 18 symbols,
#: 39 call edges, 53 test-map rows, 9 signatures, a search term; 16
#: files a directory (187 directories).
FILES_PER_DIRECTORY = 16


def _synthetic_graph(target_bytes: int) -> dict:
    files = target_bytes // 29_000 + 1
    symbols, edges, tests, rows, signatures, terms, flows = [], [], [], [], [], [], []
    paths = [f"apps/component_{n // FILES_PER_DIRECTORY:03d}/package/module_{n:05d}.py"
             for n in range(files)]
    for n, path in enumerate(paths):
        ids = [f"{path}#function_with_a_name_{s:02d}"
               for s in range(18)]
        for s, sid in enumerate(ids):
            symbols.append({"end_line": s * 12 + 10, "exported": True, "id": sid,
                            "kind": "function", "language": "python", "path": path,
                            "start_line": s * 12 + 1})
        for e in range(39):
            other = paths[(n * 7 + e) % files]
            edges.append({"also_evidence": [], "confidence": 0.6, "evidence": "ast",
                          "from": ids[e % 18], "kind": "call", "line": e * 3 + 2,
                          "path": path, "sites": 1,
                          "to": f"{other}#function_with_a_name_"
                                f"{(e * 5) % 18:02d}"})
        for t in range(53):
            tests.append({"confidence": 0.36, "depth": 2, "symbol": ids[t % 18],
                          "test": f"tests/unit/component_{n % 40:02d}/test_behaviour_"
                                  f"{n:05d}.py#test_a_behaviour_that_is_held_here_{t:02d}"})
        rows.append({"blob": hashlib.sha1(path.encode()).hexdigest(), "bytes": 18_447,
                     "language": "python", "lines": 370, "path": path,
                     "reason": None, "status": "parsed", "test": False,
                     "tests": [{"also_evidence": [], "confidence": 0.35,
                                "evidence": "path-ref", "test": f"tests/unit/test_{n}.py"}]
                     * 3})
        for s in range(9):
            signatures.append({"fingerprint": "sha256:" + hashlib.sha256(
                ids[s].encode()).hexdigest()[:16], "signature": "(self, first, second=None)",
                               "symbol": ids[s]})
        terms.append({"term": f"term{n:05d}", "postings": {
            paths[(n + k) % files]: [[f"function_with_a_long_name_{k}", 1, 26]]
            for k in range(12)}})
        if n % 10 == 0:
            flows.append({"entry": ids[0], "files": paths[n:n + 8], "kind": "route",
                          "path": path, "steps": [{"depth": 1, "symbol": sid, "via": path}
                                                  for sid in ids[:6]]})
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full",
        "commit_sha": hashlib.sha1(b"base").hexdigest(), "branch": "main", "base_sha": None,
        "languages": [{"language": "python", "files": files}], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "3"},
        "symbols": symbols, "call_edges": edges, "symbol_test_map": tests, "files": rows,
        "signatures": signatures, "terms": terms, "flows": flows, "communities": [],
        "signature_changes": [], "term_stats": {"documents": len(symbols)},
    }


class _ChargingGcloud:
    """`gcloud storage` over a dict, charging (scaled) for each call and object.

    Thread-safe, and it counts how many calls were in flight at once.
    """

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.calls: list[list[str]] = []
        self.in_flight = 0
        self.most_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, argv: list[str], data: bytes | None = None) -> bytes:
        with self._lock:
            self.calls.append(list(argv))
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            urls = [part for part in argv if part.startswith("gs://")]
            time.sleep((GCLOUD_CALL_SECONDS + GCLOUD_OBJECT_SECONDS * len(urls)) / SCALE)
            verb = argv[2]
            if verb == "cat":
                if argv[-1] not in self.objects:
                    raise FileNotFoundError(argv[-1])
                return self.objects[argv[-1]]
            if verb == "cp":
                *sources, target = argv[3:]
                for source in sources:
                    (Path(target) / source.rsplit("/", 1)[-1]).write_bytes(self.objects[source])
                return b""
            raise AssertionError(f"unexpected gcloud call {argv}")
        finally:
            with self._lock:
                self.in_flight -= 1


def test_a_60_mb_graph_is_read_within_the_budget_its_size_gives(shards, tmp_path):
    document = _synthetic_graph(60 * 1024 * 1024)
    raw = shards.canonical(document) + b"\n"
    assert len(raw) >= 60 * 1024 * 1024, len(raw)
    key, manifest, blobs = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID,
                                        graph_digest=shards.digest_of(raw))
    objects = {f"gs://{BUCKET}/{shards.blob_key(TENANT, REPO_ID, h)}": data
               for h, data in blobs.items()}
    objects[f"gs://{BUCKET}/{key}"] = manifest
    fake = _ChargingGcloud(objects)

    # The size the worker sizes the budget from: the counts the base's
    # promoted index carries, which never under-state the graph.
    index = {"graph": {"symbols": len(document["symbols"]),
                       "edges": len(document["call_edges"])}}
    estimate = indexrun.base_graph_bytes(index)
    assert estimate is not None and estimate >= len(raw)
    budget = indexrun.budgets(repoindex.INCREMENTAL_TIMEOUT_SECONDS,
                              base_graph_bytes=estimate).base_read

    said: list[str] = []
    out = tmp_path / "repo-graph.base.json"
    started = time.monotonic()
    report = shards.read_to_file(
        shards.GcsStore(BUCKET, run=fake), tenant_id=TENANT, repo_id=REPO_ID,
        commit_sha=document["commit_sha"], manifest_digest=shards.digest_of(manifest),
        out=out, progress=said.append,
    )
    elapsed = time.monotonic() - started

    assert elapsed <= budget / SCALE, (elapsed, budget, len(blobs))
    assert report["symbols"] == len(document["symbols"])
    assert out.stat().st_size > 60 * 1024 * 1024
    # The batches ran side by side: the per-object cost is no longer serial.
    assert fake.most_in_flight > 1, fake.calls
    # It says how far it got, to the last blob.
    downloads = [line for line in said if line.startswith("downloaded ")]
    assert downloads and downloads[-1].startswith("downloaded "), said
    done, _, total = downloads[-1].split()[1].partition("/")
    assert done == total


def test_the_budget_is_sized_from_the_base_graph_not_a_flat_share():
    small = indexrun.budgets(repoindex.INCREMENTAL_TIMEOUT_SECONDS, base_graph_bytes=1_000_000)
    measured = indexrun.budgets(repoindex.INCREMENTAL_TIMEOUT_SECONDS,
                                base_graph_bytes=63 * 1024 * 1024)
    unknown = indexrun.budgets(repoindex.INCREMENTAL_TIMEOUT_SECONDS)
    # 63 MB, the size measured on 2026-10-16, gets more than the 90 s it overran.
    assert measured.base_read > 90
    assert small.base_read < measured.base_read
    # Never more than half the extraction it would save, nor more than the run.
    for budget in (small, measured, unknown):
        assert budget.base_read <= budget.extract // 2
    assert unknown.base_read == unknown.extract // 2
    # No counts in the index is no size: the bound, not a guess below it.
    assert indexrun.base_graph_bytes({"graph": {}}) is None
    assert indexrun.base_graph_bytes({"schema": "swarm.repo-index/v1"}) is None
    assert indexrun.base_graph_bytes(None) is None


# --------------------------------------------------------------------------
# a read that runs out: named, and the run is full
# --------------------------------------------------------------------------

#: `read` says how far it got, leaves a partial file, and never finishes.
SLOW_READ = r'''#!PYTHON
import os, sys, time
args = sys.argv[1:]
if args[0] == "read":
    open(args[args.index("--out") + 1], "w").write('{"partial": ')
    print("swarm-repo-graph read: downloaded 120/744 blobs in 61.2 s", file=sys.stderr,
          flush=True)
    time.sleep(60)
    sys.exit(0)
index = args[args.index("--index") + 1]
import json
doc = json.load(open(index))
doc["graph"] = {"manifest_digest": "sha256:" + "0" * 64}
open(index, "w").write(json.dumps(doc))
'''


@pytest.fixture
def slow_read(index_tools, tmp_path, monkeypatch):
    tool = tmp_path / "bin" / indexrun.GRAPH_WRITER_COMMAND
    tool.write_text(SLOW_READ.replace("PYTHON", sys.executable, 1))
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    real = indexrun.budgets

    def one_second_read(timeout_seconds: int, **kwargs: Any) -> indexrun.Budgets:
        return dataclasses.replace(real(timeout_seconds, **kwargs), base_read=1)

    monkeypatch.setattr(indexrun, "budgets", one_second_read)
    return index_tools


def test_a_base_read_that_runs_out_is_named_and_the_run_is_full(
    db, store, worker_factory, slow_read, checkout, recheck_bypassed, log_stream
):
    _seed(db, store)
    worker, _, _ = worker_factory(runner_profile="indexer", timeout_seconds=900,
                                  repository_url=URL)

    worker.run()

    rows = _order(slow_read)
    # The extractor ran full: no base, and no partial base graph left for it.
    extract = [row for name, row in rows if name == "extract"]
    assert extract and "--base-sha" not in extract[0]["argv"], rows
    assert extract[0]["seen"] == {}
    stage = _phases(db)["stage_base"]
    assert stage["status"] == "skipped", stage
    reason = stage["reason"]
    assert reason.startswith("a full run: "), reason
    # Which read ran out, of which base, and how far it got.
    assert f"swarm-repo-graph read of base {BASE[:12]}" in reason, reason
    assert "1-second budget" in reason, reason
    assert "downloaded 120/744 blobs" in reason, reason
    logged = log_stream.getvalue()
    assert "index base graph read ran out of its budget" in logged
    assert "downloaded 120/744 blobs" in logged
    assert db.doc("tasks/task_1")["state"] == "SUCCEEDED"


def test_the_docs_describe_the_size_based_budget_not_the_flat_share():
    """repo-index.md §3.6 and knowledge-graph.md state the rule `budgets` keeps."""
    incremental = indexrun.budgets(repoindex.INCREMENTAL_TIMEOUT_SECONDS)
    ceiling = incremental.extract // 2
    fixed = f"{indexrun.BASE_READ_FIXED_SECONDS} s"
    per_mb = f"{indexrun.BASE_READ_SECONDS_PER_MB:g} s a MB"
    for name in ("repo-index.md", "design/knowledge-graph.md"):
        text = " ".join((REPO_ROOT / "docs" / name).read_text(encoding="utf-8").split())
        assert "quarter of the extractor" not in text, name
        assert "0.25 of the extract" not in text, name
        assert fixed in text and per_mb in text, (name, fixed, per_mb)
        assert "half the extract" in text, name
        assert f"{ceiling} s of an incremental run's {incremental.extract}" in text, name
