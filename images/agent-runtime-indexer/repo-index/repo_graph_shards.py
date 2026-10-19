#!/usr/bin/env python3
"""The graph shard writer and the blob sweep (docs/repo-index.md §2.5, lane RI9).

RI3's extractor (`swarm-repo-index --graph-out <file>`) writes the whole graph
as one `swarm.repo-graph/v1` document. A 2,000-file repository's graph is
tens of megabytes, which no reader should download to answer "who calls X".
This tool stores it the way §2.5 lays out, under the tenant's own prefix:

    tenants/<tenant>/repos/<repo_id>/graph/<commit_sha>/manifest.json
    tenants/<tenant>/repos/<repo_id>/graph/blobs/<sha256>.jsonl.gz

WHAT A SHARD IS. One module (a directory holding code, as RI3's `modules`
counts them; `.` for the root) of one layer, as JSON lines:

  symbols   the module's symbols
  callers   every edge whose CALLEE is in the module -- "who calls X" reads
            X's module's shard alone
  callees   every edge whose CALLER is in the module
  tests     `symbol_test_map` rows for the module's symbols
  files     the module's files, with status and reason (a file that was not
            parsed is listed with why, never silently dropped)

FORMAT 3 (lane KG2, extractor version 3). The manifest also carries
`format_version: 3` and `index_shards`, five more layers kept OUT of
`shards` so a reader that knows only the five above still reads the
manifest (swarm-api refuses unknown layers under `shards`):

  communities        the module communities, one shard keyed `*`
  terms              the BM25 postings over symbols, by term bucket
  signatures         each callable's signature fingerprint, by module
  signature_changes  fingerprints that moved since an incremental run's base
  flows              bounded call flows from each entry point, by module

`term_stats` (documents, average length, k1, b, tokenizer) sits beside them.
A manifest without `format_version` is format 2 and is read with those lists
empty; `tokenize` and `bm25_search` below are the one implementation the
extractor indexes with and readers query with.

CONTEXT LAYERS (lane CTXQ-H, docs/design/context-quality.md §4.2). The
manifest also carries `context_shards`, the history and rules layers
`repo_index_history.py` mines from the index run's own clone, and `context`,
what that clone covered and why a layer is missing:

  history       per file, the merged PRs that touched it    by file directory
  co_change     a file and the tests or guards that change with it
  test_fixes    per test, the earlier commits that fixed it  by test directory
  rules         the docs, house rules and guards policing a path: by the
                file's directory, or by the directory a directory rule
                names, so a reader of a path reads its directory and every
                ancestor (`context_keys`, `rule_reaches`)
  cannot_push   `.github/workflows/**` and frozen paths, one shard keyed `*`
  test_runners  per JS/TS test file, the command that runs it

A THIRD key, not `index_shards`, for the reason format 3 did not touch
`shards`: swarm-api's `territory._index_table` refuses a manifest whose
`index_shards` names a layer it does not know, so a context layer there
would make every new graph unreadable to the impact, territory and plan
routes. Every reader ignores a top-level key it does not know, so the format
stays 3. The writer finds the clone at `--repo`, by default `repo/` beside
the `--graph` file: the worker writes `work/repo-graph.json` beside its
clone `work/repo` (agent_worker.indexrun GRAPH_FILE, workspace.REPO_DIR_NAME).
Without a clone, or when mining fails, the graph is written exactly as
before, with `context.available` false and the reason: a context layer is
never worth a graph.

THE DOCUMENT LAYER (lane GS-INDEXDOC, docs/design/graph-service.md §2.3,
owner decision Q7). The manifest also carries `document_shards`, one layer
named `document` holding the repo-index document (`--index`,
`swarm.repo-index/v1`, docs/repo-index.md §2.1) one shard per section --
modules, entry_points, routes, test_layout, test_map, territory, commands,
hot_spots and notes -- and `document`, which says whether it is there, why
not, each section's count and the index's own `truncated`. The shared graph
service may read only `graph/**` (§3.4), so the document has to be inside the
graph the ticket's manifest digest pins rather than at `index/<sha>/`.

A FOURTH key, NOT `index_shards`, as the design names it: swarm-api's
`territory._index_table` refuses an `index_shards` layer it does not know,
exactly as for the context layers above, so a `document` layer there would
turn every new graph away from the territory, impact and search routes.

A section's rows keep the document's own order (the biggest module and the
hottest file first, as the extractor wrote them), so a reader paging the
layer reads them as the planner's rendering does. The index's `graph` block
is not stored: the writer records this manifest's digest in it AFTER the
write, and a manifest cannot name its own digest. Without `--index`, or with
a document for another commit, of another schema, malformed, or over §2.2's
512 KiB budget, the graph is written exactly as before with
`document.available` false and the reason: the document is never worth a
graph.

CONTENT-ADDRESSED. A blob is named by the sha256 of its stored bytes, so a
reader checks a blob BEFORE it decompresses it, and an unchanged shard of the
next commit is the same blob: an incremental commit writes the shards whose
content changed and a new manifest naming the rest. Rows are sorted and every
line is canonical JSON, and gzip runs with mtime 0, so the same graph gives
the same bytes on every run -- the manifest's digest is what promotion records
(`repoindex.py`), and a rerun that came out different would read as a
rewritten graph.

gzip, NOT zstd. §2.5 names `.jsonl.zst`; Python 3.11 has no zstd in its
standard library, and adding `zstandard` means a new hash-pinned wheel here,
in the worker test environment and in swarm-api, which reads the shards.
The manifest names its `compression`, so a later zstd writer is a new value,
not a format break. Measured on this repository (lane RI9): gzip -9 holds
the graph to about a seventh of its JSON-lines size.

THE 256 MiB CEILING (§2.5). Over it, the context layers go first (they are
advisory, and rebuilt from the clone on every run): history, co-change, test
fixes and test runners (`context:history` in `truncated`), then rules
(`context:rules`); `cannot_push` stays. Still over, symbol edges below confidence 0.4 are
dropped first (`call_edges:below_0.4` in `truncated`); still over, every
symbol edge goes and the module-level `import` edges stay
(`call_edges:symbol`); still over, nothing is written and the run says why.

ORDER OF WRITES, AND A RE-RUN (lane IX1, 2026-10-06). Blobs first, the
manifest last, so a manifest never names a blob that is not there yet. A
blob's name is the sha256 of its bytes, so a blob already at its path is
WRITTEN when its content is ours -- checked against the listing's MD5, or by
reading it back when the listing has none -- and a run interrupted after any
number of blobs completes when it is run again. A path holding DIFFERENT
bytes is a hard error (`ConflictError`): nothing overwrites it, and no
manifest is written over it. Measured on task_209ba9e0c9c948e284e9: a
write killed after 150 blobs, and the retry failed with a 412 on its first
blob, because `GcsStore.list` kept the `#<generation>` a versioned bucket
appends to every listed url, so no listed key ever matched a blob key, every
blob read as absent and was put again with `--no-clobber`. The listing now
names each object by its name, without the generation.

IN BATCHES. One `gcloud storage cp --no-clobber` of many files per batch
(`GcsStore.put_many`), which gcloud uploads in parallel, and only the blobs
not already there. The serial writer it replaced started one process per
blob at ~3.9 s each: a full graph of ~370 blobs took ~24 minutes, more than
the 30-minute index run has once the extractor's ~6 minutes are spent.
After the upload every blob is checked again, so a blob that appeared
between the listing and the upload (a 412 in the batch) is accepted when it
holds our bytes and refused when it does not.

READING ONE BACK (lane IX2). `read --commit <sha> --manifest-digest <d>`
rebuilds a stored commit's graph document from its manifest -- symbols,
the edges by caller module, the test map and the files -- for an incremental
run's base (§3.4). The manifest is checked against the digest promotion
recorded and every blob against its name before it is decompressed, so a
rewritten graph is refused, never built on. `write --base-commit <sha>`
counts the shards whose blob the base manifest already names
(`shards_carried`): the unchanged modules, which it does not upload again.

THE SWEEP. `sweep` lists every manifest of one registration and every blob,
and deletes a blob only when (a) no manifest names it -- a promoted manifest
is one of them, so a blob any promoted manifest references is never removed
-- and (b) it is older than a day, because a writer between its blobs and its
manifest has unreferenced blobs that are about to be referenced (an index
run's longest timeout, §3.5, is two hours). If ANY manifest cannot be read
or parsed, the sweep deletes nothing: a manifest it cannot read is a set of
references it cannot see. Run as the tenant's own worker account, which may
delete under `tenants/<tenant>/` and nowhere else (invariant 9); swarm-api
reads the bucket and cannot delete, by design (`objects.py`).

RETENTION (lane IX3, owner decision 2026-10-06). The bucket's lifecycle no
longer touches `tenants/<t>/repos/` (terraform/modules/storage), so this
sweep is the only thing that deletes there. Given `--keep-commit` for every
commit whose index version swarm-api still holds (the last 20, §2.3; the
worker reads them and adds them to its `write`), it first deletes the
manifest `graph/<sha>/manifest.json` and the index copy
`index/<sha>/repo-index.json` of every other commit older than a day, then
sweeps the blobs no remaining manifest names. At most `MAX_SWEEP_DELETES`
objects go per sweep, in batched `gcloud storage rm` calls; whatever is past
the bound waits for the next run. Without `--keep-commit` it retires nothing.

WHERE IT WRITES (lane RI9b; run by the worker since lane IX1). The worker
runs `swarm-repo-graph write --graph <file> --repo-id <r> --destination
tenants/<t>/repos/<r>/graph --tenant <t> --store gs://<bucket> --index
<repo-index.json>` after the agent (agent_worker/indexrun.py), no longer the
agent through its shell. The tenant and the bucket are the step's own
configuration (`TENANT_ID`, `ARTIFACT_BUCKET`, set by dispatch), read by
`resolve_target`. A destination, `--tenant` or `gs://` store that
configuration contradicts is refused before anything is read or written
(invariant 9).

Standard library only: it runs with the image's python3.11, outside the
extractor's tree-sitter environment, and on GCS through the image's `gcloud`
with the account the step already has. No credential passes through it.
"""

from __future__ import annotations

import argparse
import base64
import functools
import gzip
import hashlib
import importlib.util
import json
import math
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

GRAPH_SCHEMA = "swarm.repo-graph/v1"
MANIFEST_SCHEMA = "swarm.repo-graph-manifest/v1"
COMPRESSION = "gzip"
BLOB_SUFFIX = ".jsonl.gz"
TOOL_NAME = "swarm-repo-graph"
# "2" (lane KG2): the manifest carries `format_version` and `index_shards`.
TOOL_VERSION = "2"
#: The shard format (lane KG2, docs/design/knowledge-graph.md §6). Format 2 is
#: what extractor version 2's graph was stored as: the five `LAYERS` under
#: `shards`, and no `format_version` key. Format 3 adds `INDEX_LAYERS` under
#: a SEPARATE key, `index_shards`, and leaves `shards` exactly as format 2 had
#: it, because swarm-api's reader (`swarm_api/repograph.py::parse_manifest`)
#: refuses a manifest whose `shards` names any layer outside its own five: a
#: new layer there would make every graph unreadable to the explorer and the
#: impact route until swarm-api moved too. Readers here take either format;
#: a format-2 manifest simply has no index layers.
FORMAT_VERSION = 3

#: §2.5's hard ceiling per commit, on the stored bytes of every blob a
#: manifest names (reused blobs included: it bounds what one commit's reader
#: may have to fetch, not what this run uploaded).
MAX_COMMIT_BYTES = 256 * 1024 * 1024
#: §2.5: over the ceiling, symbol edges below this confidence go first.
CEILING_MIN_CONFIDENCE = 0.4
#: The sweep keeps an unreferenced blob younger than this: a writer may be
#: between its blobs and its manifest. Far above §3.5's longest timeout (2 h).
SWEEP_GRACE = timedelta(days=1)
#: Objects one sweep may delete, manifests, index copies and blobs together.
#: A version retired by the 20-version rule (§2.3) is one manifest, one index
#: copy and the few blobs no other manifest shares, so one run's sweep keeps
#: up with one promotion many times over; a backlog is worked off over the
#: next runs instead of by one sweep that outlives the graph write's budget.
MAX_SWEEP_DELETES = 2000
#: The name of the index copy under `index/<commit_sha>/` (§2).
INDEX_NAME = "repo-index.json"

#: The layers a manifest names, in the order they are written.
LAYERS = ("symbols", "callers", "callees", "tests", "files")
#: Format 3's layers (`index_shards`), and the graph list each one holds:
#:
#:   communities        the module communities, one shard keyed `WHOLE`: a
#:                      reader wants the whole partition or one community, and
#:                      the table is a few hundred rows
#:   terms              the BM25 term index over application symbols (a test is
#:                      found through the test map, not by search), sharded by
#:                      `term_bucket(term)`, so a
#:                      query reads the buckets of its own terms and nothing else
#:   signatures         each callable's signature and fingerprint, by module
#:   signature_changes  on an incremental run, the callables whose fingerprint
#:                      differs from the base's, by module
#:   flows              the bounded call flow from each entry point, by the
#:                      entry's module
INDEX_LAYERS = {"communities": "communities", "terms": "terms", "signatures": "signatures",
                "signature_changes": "signature_changes", "flows": "flows"}
#: The key of a layer stored as one shard. Not a directory `module_of` can
#: produce: a module is a path or `.`.
WHOLE = "*"
#: The context layers (lane CTXQ-H), under `context_shards`, and the version
#: of their row shapes, recorded as `context.version`. Restated from
#: repo_index_history.LAYERS so this module still reads and sweeps a manifest
#: without that file; a test holds the two equal.
CONTEXT_LAYERS = ("history", "co_change", "test_fixes", "rules", "cannot_push", "test_runners")
CONTEXT_VERSION = 1
#: Over the ceiling, the context layers dropped first, then next, each group
#: named in `truncated`. `cannot_push` is never dropped: it is a handful of
#: rows, and the miss it prevents (a lane editing a workflow file) costs the
#: whole lane (context-quality.md §3, class W).
CONTEXT_DROP = (("context:history", ("history", "co_change", "test_fixes", "test_runners")),
                ("context:rules", ("rules",)))
#: The repo-index document's layer (lane GS-INDEXDOC), under `document_shards`,
#: and its sections, one shard each, keyed by the section's name
#: (docs/design/graph-service.md §2.3; docs/repo-index.md §2.1).
DOCUMENT_LAYER = "document"
DOCUMENT_SECTIONS = ("modules", "entry_points", "routes", "test_layout", "test_map",
                     "territory", "commands", "hot_spots", "notes")
#: The schema the document must carry (swarm_api.repoindex.RepoIndexSpec).
INDEX_SCHEMA = "swarm.repo-index/v1"
#: docs/repo-index.md §2.2's budget for the whole document; its sections alone
#: over it is a document the API would have refused, so it is not stored.
MAX_DOCUMENT_BYTES = 512 * 1024
#: The history module, beside this file in the image (/opt/repo-index/).
HISTORY_MODULE = "repo_index_history"
#: Term buckets are the first TERM_BUCKET_CHARS hex characters of the term's
#: sha256: 256 buckets. A query of a handful of terms reads that many shards,
#: and a one-file change rewrites only the buckets of the terms it touched.
TERM_BUCKET_CHARS = 2

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_BLOB_NAME = re.compile(r"^([0-9a-f]{64})" + re.escape(BLOB_SUFFIX) + r"$")
#: The `#<generation>` a versioned bucket's listing appends to an object's url.
_GENERATION = re.compile(r"#\d+$")
#: Files per `gcloud storage cp`: far below any argv limit (a blob path is
#: about 120 bytes), and a full graph's ~370 blobs go up in one call.
BATCH_FILES = 500
#: `gcloud storage cp` processes a download runs side by side (#1469). A
#: read's cost is per OBJECT, not per byte: measured on this repository's
#: own graph (d2b2d14, 98 MB of JSON), a base read fetches 744 blobs of 8.8
#: MB stored, and decoding them takes ~4 s of CPU; the 63 MB base that ran
#: past its 90 s budget (#1334) was ~480 blobs in two batches of one call
#: each, one after the other -- at least 0.16 s an object. Split into this
#: many calls at once, the per-object cost is divided by it. Four: the
#: indexer's `standard` class has 4 vCPU, the read runs before the extractor
#: and the agent, so nothing else wants them, and each gcloud is a few
#: hundred MB at most of the class's 8 GiB.
GET_PROCESSES = 4
#: Upload rounds: a round whose batch stopped on a blob that appeared
#: meanwhile (a 412) is followed by one for what is still absent.
UPLOAD_ROUNDS = 3


# --- naming -------------------------------------------------------------------

def _segment(value: str, what: str) -> str:
    """One traversal-free key segment: the tenant id IS the isolation boundary."""
    if not isinstance(value, str) or not _SEGMENT.match(value) or ".." in value:
        raise ValueError(f"{what} {value!r} is not a single safe path segment")
    return value


def repo_root(tenant_id: str, repo_id: str) -> str:
    """`tenants/<tenant>/repos/<repo_id>`: the registration's own prefix (§2)."""
    return f"tenants/{_segment(tenant_id, 'tenant_id')}/repos/{_segment(repo_id, 'repo_id')}"


def graph_root(tenant_id: str, repo_id: str) -> str:
    return f"{repo_root(tenant_id, repo_id)}/graph"


def index_key(tenant_id: str, repo_id: str, commit_sha: str) -> str:
    """Where the worker keeps a commit's repo-index.json beside its graph (§2,
    `agent_worker.indexrun.index_key`, `swarm_api.repoindex.index_key`)."""
    if not _SHA.match(commit_sha or ""):
        raise ValueError(f"commit_sha {commit_sha!r} is not a 40-hex commit sha")
    return f"{repo_root(tenant_id, repo_id)}/index/{commit_sha}/{INDEX_NAME}"


def manifest_key(tenant_id: str, repo_id: str, commit_sha: str) -> str:
    if not _SHA.match(commit_sha or ""):
        raise ValueError(f"commit_sha {commit_sha!r} is not a 40-hex commit sha")
    return f"{graph_root(tenant_id, repo_id)}/{commit_sha}/manifest.json"


def blob_key(tenant_id: str, repo_id: str, hexdigest: str) -> str:
    return f"{graph_root(tenant_id, repo_id)}/blobs/{hexdigest}{BLOB_SUFFIX}"


def module_of(symbol_id: str) -> str:
    """The module a symbol or file id lives in: its file's directory, `.` at the root."""
    path = symbol_id.split("#", 1)[0]
    return posixpath.dirname(path) or "."


# --- symbol search (format 3's `terms` layer) ---------------------------------
#
# BM25 (Robertson and Zaragoza, "The Probabilistic Relevance Framework: BM25
# and Beyond", 2009), written here from the published formula. The extractor
# builds the postings with `tokenize` and every reader scores with it, so the
# two can never split a word differently: this module is the one copy.

#: Bumped whenever `tokenize` changes what it returns; the manifest records it,
#: and a reader with another tokenizer must not query the postings.
TOKENIZER_VERSION = "1"
BM25_K1 = 1.2
BM25_B = 0.75
_WORD = re.compile(r"[A-Za-z0-9]+")
# camelCase, PascalCase and HTTPServer-style acronyms, then digits; a word's
# trailing digits stay with it (`v2`, `sha256`, `base64`).
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*|[0-9]+")
#: English function words and the keywords every symbol shares; a term in
#: nearly every document scores nothing and only grows the index.
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "if", "in", "into",
    "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were",
    "where", "which", "with", "what", "when", "how", "def", "self", "cls", "none",
    "return", "returns", "true", "false", "str", "int", "dict", "list", "any",
    "not", "no", "so", "one", "every", "but", "than", "then", "there", "these", "those",
    "has", "have", "can", "may", "must", "do", "does", "here", "only", "also", "all",
})


def _stem(token: str) -> str:
    """A plural's `s`, and nothing else: `runs` finds `run`. Same on both sides."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


@functools.lru_cache(maxsize=1 << 16)
def _word_terms(word: str) -> tuple[str, ...]:
    out = []
    for part in _CAMEL.findall(word):
        lowered = part.lower()
        if lowered in STOPWORDS:
            continue
        token = _stem(lowered)
        if len(token) >= 2 and token not in STOPWORDS:
            out.append(token)
    return tuple(out)


def tokenize(text: str) -> list[str]:
    """Lower-case terms of identifiers and prose: `planner_prompt`, `plannerPrompt`
    and "the planner's prompt" all give `planner`, `prompt`."""
    out: list[str] = []
    for word in _WORD.findall(text or ""):
        out.extend(_word_terms(word))
    return out


def term_bucket(term: str) -> str:
    """The `terms` shard a term's postings live in."""
    return hashlib.sha256(term.encode("utf-8")).hexdigest()[:TERM_BUCKET_CHARS]


def bm25_search(rows: Iterable[dict], stats: dict, query: str,
                limit: int = 20) -> list[tuple[str, float]]:
    """(symbol id, score), best first, for `query` over `terms` rows.

    `rows` may be every row or only the query's buckets; `stats` is the
    manifest's (or graph document's) `term_stats`. A row's `postings` maps a
    file to `[name, tf, dl]` lists, the symbol being `<file>#<name>`: the
    term's weighted count in the symbol's text and that text's length, so the
    score needs no other table, and a file's path is stored once per term
    rather than once per symbol.
    """
    wanted = set(tokenize(query))
    documents = max(int(stats.get("documents") or 0), 1)
    average = float(stats.get("average_length") or 1.0) or 1.0
    k1 = float(stats.get("k1") or BM25_K1)
    b = float(stats.get("b") if stats.get("b") is not None else BM25_B)
    scores: dict[str, float] = {}
    for row in rows:
        if row.get("term") not in wanted:
            continue
        postings = [(f"{path}#{name}", tf, dl)
                    for path, entries in sorted((row.get("postings") or {}).items())
                    for name, tf, dl in entries]
        df = len(postings)
        idf = math.log(1.0 + (documents - df + 0.5) / (df + 0.5))
        for symbol, tf, dl in postings:
            norm = tf + k1 * (1.0 - b + b * dl / average)
            scores[symbol] = scores.get(symbol, 0.0) + idf * tf * (k1 + 1.0) / norm
    ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(symbol, round(score, 4)) for symbol, score in ranked[:max(limit, 0)]]


def signature_changes(before: Iterable[dict], after: Iterable[dict]) -> list[dict]:
    """The callables whose signature fingerprint differs between two `signatures` lists.

    `changed`: the fingerprint moved, so every caller is a candidate for
    change; `removed`: the symbol is gone; `added`: new. A symbol in neither
    list is not mentioned.
    """
    old = {str(r.get("symbol")): r for r in before if isinstance(r, dict)}
    new = {str(r.get("symbol")): r for r in after if isinstance(r, dict)}
    out: list[dict] = []
    for symbol in sorted(set(old) | set(new)):
        a, b = old.get(symbol), new.get(symbol)
        if a is not None and b is not None:
            if a.get("fingerprint") == b.get("fingerprint"):
                continue
            change = "changed"
        else:
            change = "removed" if b is None else "added"
        out.append({"symbol": symbol, "change": change,
                    "before": None if a is None else a.get("signature"),
                    "after": None if b is None else b.get("signature")})
    return out


def canonical(value: Any) -> bytes:
    """Sorted keys, no whitespace: the one serialisation every digest is of."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- stores -------------------------------------------------------------------

class StoreError(Exception):
    """The store could not do what was asked. Never read as "absent"."""


class ConflictError(StoreError):
    """A write-once path already holds different bytes. Never overwritten."""


class Store(Protocol):
    def list(self, prefix: str) -> list[tuple[str, datetime | None]]: ...

    def get(self, key: str) -> bytes: ...

    def put(self, key: str, data: bytes, *, no_clobber: bool) -> None: ...

    def delete(self, key: str) -> None: ...


#: Optional on a store, with a fallback for one that lacks it (`_md5s`,
#: `_get_many`, `_put_many`):
#:   md5s(prefix) -> {key: base64 MD5 or None}   what a listing says of each object
#:   get_many(keys, progress=None) -> {key: bytes}  several reads in one call;
#:                                               `progress(done, total)` after each part
#:   put_many({key: bytes}, no_clobber=...)      several writes in one call


def md5_of(data: bytes) -> str:
    """The base64 MD5 GCS reports for an object's bytes (`md5Hash`)."""
    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


class LocalStore:
    """A directory standing in for the bucket: keys are relative paths."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve()) + os.sep):
            raise ValueError(f"key {key!r} leaves the store")
        return path

    def list(self, prefix: str) -> list[tuple[str, datetime | None]]:
        base = self.root / prefix
        directory = base if base.is_dir() else base.parent
        if not directory.is_dir():
            return []
        rows = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                moment = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                rows.append((key, moment))
        return rows

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            raise StoreError(f"{key}: {exc.strerror or exc}") from None

    def put(self, key: str, data: bytes, *, no_clobber: bool) -> None:
        path = self._path(key)
        if no_clobber and path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(data)
        os.replace(handle.name, path)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_many(self, keys: Iterable[str]) -> None:
        for key in sorted(keys):
            self.delete(key)

    def md5s(self, prefix: str) -> dict[str, str | None]:
        return {key: md5_of(self._path(key).read_bytes()) for key, _ in self.list(prefix)}

    def get_many(self, keys: Iterable[str],
                 progress: Callable[[int, int], None] | None = None) -> dict[str, bytes]:
        out = {key: self.get(key) for key in keys}
        if progress is not None:
            progress(len(out), len(out))
        return out

    def put_many(self, items: dict[str, bytes], *, no_clobber: bool) -> None:
        for key in sorted(items):
            self.put(key, items[key], no_clobber=no_clobber)


def _gcloud(argv: list[str], data: bytes | None = None) -> bytes:
    """Run `gcloud storage ...`. stderr is kept short and carries no object data."""
    try:
        done = subprocess.run(argv, input=data, capture_output=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StoreError(f"{' '.join(argv[:3])}: {exc}") from None
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise StoreError(f"{' '.join(argv[:3])} exited {done.returncode}: "
                         f"{message[-1][:300] if message else 'no message'}")
    return done.stdout


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class GcsStore:
    """The artifact bucket through the image's `gcloud storage`, as the step's own account."""

    def __init__(self, bucket: str, *, run: Callable[..., bytes] = _gcloud) -> None:
        if not re.match(r"^[a-z0-9][a-z0-9._-]{1,220}$", bucket or ""):
            raise ValueError(f"bucket {bucket!r} is not a bucket name")
        self.bucket = bucket
        self._run = run

    def _url(self, key: str) -> str:
        return f"gs://{self.bucket}/{key}"

    def _rows(self, prefix: str) -> list[tuple[str, datetime | None, str | None]]:
        """(key, created, base64 MD5) for every live object under `prefix`."""
        try:
            raw = self._run(["gcloud", "storage", "ls", "--json", self._url(prefix) + "**"])
        except StoreError as exc:
            if "matched no objects" in str(exc) or "One or more URLs matched" in str(exc):
                return []
            raise
        try:
            rows = json.loads(raw.decode("utf-8") or "[]")
        except ValueError:
            raise StoreError(f"gcloud storage ls answered something that is not JSON for "
                             f"{prefix}") from None
        out: list[tuple[str, datetime | None, str | None]] = []
        for row in rows if isinstance(rows, list) else []:
            key = self._key_of(row)
            if key is None or not key.startswith(prefix):
                continue
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            # A time that cannot be read is None, which the sweep treats as young.
            created = _parse_time(row.get("creation_time") or meta.get("timeCreated")
                                  or meta.get("creation_time"))
            md5 = meta.get("md5Hash") or meta.get("md5_hash") or row.get("md5_hash")
            out.append((key, created, md5 if isinstance(md5, str) and md5 else None))
        return sorted(out, key=lambda r: r[0])

    def _key_of(self, row: Any) -> str | None:
        """The object's name, WITHOUT the generation.

        On a versioned bucket `ls --json` gives every url as
        `gs://<bucket>/<name>#<generation>`. Kept, that suffix made every
        listed key differ from the key it was compared with (lane IX1): the
        writer re-put blobs that existed, and the sweep never saw a manifest.
        The name in `metadata` is the object's own; the url, less its
        `#<digits>`, is the fallback.
        """
        if not isinstance(row, dict):
            return None
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        name = meta.get("name")
        if isinstance(name, str) and name and meta.get("bucket") in (None, self.bucket):
            return name
        url = row.get("url")
        head = f"gs://{self.bucket}/"
        if not isinstance(url, str) or not url.startswith(head):
            return None
        return _GENERATION.sub("", url[len(head):]) or None

    def list(self, prefix: str) -> list[tuple[str, datetime | None]]:
        return [(key, created) for key, created, _md5 in self._rows(prefix)]

    def md5s(self, prefix: str) -> dict[str, str | None]:
        return {key: md5 for key, _created, md5 in self._rows(prefix)}

    def get(self, key: str) -> bytes:
        try:
            return self._run(["gcloud", "storage", "cat", self._url(key)])
        except FileNotFoundError:
            raise StoreError(f"{key}: not found") from None

    def put(self, key: str, data: bytes, *, no_clobber: bool) -> None:
        argv = ["gcloud", "storage", "cp"]
        if no_clobber:
            argv.append("--no-clobber")
        self._run(argv + ["-", self._url(key)], data)

    def put_many(self, items: dict[str, bytes], *, no_clobber: bool) -> None:
        """One `gcloud storage cp` of up to BATCH_FILES files per directory.

        Each object goes to `<its directory>/<its name>`, so the files are
        staged under their own names and copied into their directory. gcloud
        uploads a multi-file copy in parallel. A batch that fails is raised
        after every batch has been tried: the caller checks each object
        afterwards (`write_graph`), so one 412 costs no other blob.
        """
        failed: StoreError | None = None
        with tempfile.TemporaryDirectory(prefix="repo-graph-put-") as scratch:
            for n, (directory, names) in enumerate(sorted(_by_directory(items).items())):
                staged = Path(scratch) / str(n)
                staged.mkdir()
                for name in names:
                    (staged / name).write_bytes(items[f"{directory}/{name}"])
                for start in range(0, len(names), BATCH_FILES):
                    argv = ["gcloud", "storage", "cp"]
                    if no_clobber:
                        argv.append("--no-clobber")
                    argv += [str(staged / name) for name in names[start:start + BATCH_FILES]]
                    try:
                        self._run(argv + [self._url(directory) + "/"])
                    except StoreError as exc:
                        failed = exc
        if failed is not None:
            raise failed

    def get_many(self, keys: Iterable[str],
                 progress: Callable[[int, int], None] | None = None) -> dict[str, bytes]:
        """`gcloud storage cp` of the objects into scratch, in up to GET_PROCESSES
        calls at once, each of at most BATCH_FILES objects of one directory.

        `progress(done, total)` is called as each call finishes, so a read cut
        short by its budget has said how far it got. A call that fails is
        raised once every call has finished: none is left writing to scratch.
        """
        wanted = sorted(set(keys))
        out: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="repo-graph-get-") as scratch:
            parts: list[tuple[str, list[str], Path]] = []
            for n, (directory, names) in enumerate(sorted(_by_directory(wanted).items())):
                staged = Path(scratch) / str(n)
                staged.mkdir()
                size = min(BATCH_FILES, max(1, -(-len(wanted) // GET_PROCESSES)))
                parts += [(directory, names[start:start + size], staged)
                          for start in range(0, len(names), size)]

            def fetch(directory: str, batch: list[str], staged: Path) -> int:
                try:
                    self._run(["gcloud", "storage", "cp"]
                              + [self._url(f"{directory}/{name}") for name in batch]
                              + [str(staged) + "/"])
                except FileNotFoundError:
                    raise StoreError(f"{directory}: not found") from None
                return len(batch)

            failed: StoreError | None = None
            done = 0
            with ThreadPoolExecutor(max_workers=GET_PROCESSES) as pool:
                for future in as_completed([pool.submit(fetch, *part) for part in parts]):
                    try:
                        done += future.result()
                    except StoreError as exc:
                        failed = failed or exc
                        continue
                    if progress is not None:
                        progress(done, len(wanted))
            if failed is not None:
                raise failed
            for directory, batch, staged in parts:
                for name in batch:
                    try:
                        out[f"{directory}/{name}"] = (staged / name).read_bytes()
                    except OSError:
                        raise StoreError(f"{directory}/{name}: not read") from None
        return out

    def delete(self, key: str) -> None:
        self._run(["gcloud", "storage", "rm", self._url(key)])

    def delete_many(self, keys: Iterable[str]) -> None:
        """One `gcloud storage rm` of up to BATCH_FILES objects: one process per
        object took ~3.9 s each, which no sweep inside the graph write's budget
        could afford. A batch that fails is raised; the sweep stops there."""
        ordered = sorted(keys)
        for start in range(0, len(ordered), BATCH_FILES):
            self._run(["gcloud", "storage", "rm"]
                      + [self._url(key) for key in ordered[start:start + BATCH_FILES]])


def _by_directory(items: Iterable[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for key in items:
        directory, _, name = key.rpartition("/")
        grouped.setdefault(directory, []).append(name)
    return {directory: sorted(names) for directory, names in grouped.items()}


def open_store(spec: str) -> Store:
    """`gs://<bucket>` is the bucket; anything else is a local directory."""
    if spec.startswith("gs://"):
        bucket = spec[len("gs://"):].rstrip("/")
        if "/" in bucket:
            raise ValueError("--store takes gs://<bucket> only; the prefix is derived from "
                             "--tenant and --repo-id")
        return GcsStore(bucket)
    return LocalStore(spec)


# --- the writer ---------------------------------------------------------------

def _check_graph(document: Any) -> None:
    if not isinstance(document, dict) or document.get("schema") != GRAPH_SCHEMA:
        raise ValueError(f"the input is not a {GRAPH_SCHEMA} document")
    if not _SHA.match(str(document.get("commit_sha") or "")):
        raise ValueError("the graph's commit_sha is not a 40-hex commit sha")
    for key in ("symbols", "call_edges", "symbol_test_map", "files"):
        if not isinstance(document.get(key), list):
            raise ValueError(f"the graph has no {key} list")
    # Version 3's lists are optional: a version-2 graph (no index layers) is
    # still a graph, stored as format 3 with empty index layers.
    for key in INDEX_LAYERS.values():
        if document.get(key) is not None and not isinstance(document.get(key), list):
            raise ValueError(f"the graph's {key} is not a list")


_RULE_RANK = {"house_rule": 0, "guard": 1, "doc": 2}


def _epoch(value: Any) -> int:
    """An ISO-8601 UTC stamp as seconds; 0 when it is not one."""
    try:
        return int(datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


_ORDER: dict[str, Callable[[dict], tuple]] = {
    "symbols": lambda s: (str(s.get("path")), s.get("start_line") or 0, str(s.get("id"))),
    "callers": lambda e: (str(e.get("to")), str(e.get("from")), str(e.get("kind"))),
    "callees": lambda e: (str(e.get("from")), str(e.get("to")), str(e.get("kind"))),
    "tests": lambda t: (str(t.get("symbol")), str(t.get("test"))),
    "files": lambda f: (str(f.get("path")),),
    "communities": lambda c: (str(c.get("id")),),
    "terms": lambda t: (str(t.get("term")),),
    "signatures": lambda r: (str(r.get("symbol")),),
    "signature_changes": lambda r: (str(r.get("symbol")),),
    "flows": lambda f: (str(f.get("entry")),),
    "history": lambda r: (str(r.get("path")),),
    "co_change": lambda r: (str(r.get("path")), -int(r.get("commits") or 0),
                            str(r.get("partner"))),
    # Newest fix first, as the fixer reads them.
    "test_fixes": lambda r: (str(r.get("test")), -_epoch(r.get("date")), str(r.get("sha"))),
    # Strongest rule first: house rule, guard, doc (repo_index_history.KIND_RANK).
    "rules": lambda r: (str(r.get("path")), _RULE_RANK.get(str(r.get("kind")), 9),
                        -float(r.get("confidence") or 0), str(r.get("source")),
                        str(r.get("evidence")), int(r.get("line") or 0)),
    "cannot_push": lambda r: (str(r.get("pattern")),),
    "test_runners": lambda r: (str(r.get("test")),),
}


def _index_layers(document: dict) -> dict[str, dict[str, list[dict]]]:
    """Format 3's layers: each graph list by the key its readers look it up by."""
    grouped: dict[str, dict[str, list[dict]]] = {layer: {} for layer in INDEX_LAYERS}
    for row in document.get("communities") or []:
        grouped["communities"].setdefault(WHOLE, []).append(row)
    for row in document.get("terms") or []:
        grouped["terms"].setdefault(term_bucket(str(row.get("term"))), []).append(row)
    for layer in ("signatures", "signature_changes"):
        for row in document.get(layer) or []:
            grouped[layer].setdefault(module_of(str(row.get("symbol"))), []).append(row)
    for row in document.get("flows") or []:
        grouped["flows"].setdefault(module_of(str(row.get("entry"))), []).append(row)
    return grouped


def context_key(layer: str, row: dict) -> str:
    """The shard a context row is stored in: the directory a reader looks it up by.

    A file's rows go under the file's directory, a directory rule under the
    directory it names, and `cannot_push` under `WHOLE`: a reader of a path
    reads `context_keys(path)` and finds every rule that reaches it.
    """
    if layer == "cannot_push":
        return WHOLE
    if layer == "rules" and row.get("target") == "dir":
        return str(row.get("path") or ".")
    return module_of(str(row.get("test") if layer in ("test_fixes", "test_runners")
                         else row.get("path")))


def context_keys(path: str) -> list[str]:
    """The shard keys a reader of `path` reads: its directory, each ancestor, then `.`."""
    keys = []
    current = posixpath.dirname(path.strip("/"))
    while current:
        keys.append(current)
        current = posixpath.dirname(current)
    return keys + ["."]


def rule_reaches(row: dict, path: str) -> bool:
    """Whether a `rules` row applies to `path`: its own file, or a directory above it."""
    if row.get("target") != "dir":
        return row.get("path") == path
    root = str(row.get("path") or ".")
    return root == "." or path == root or path.startswith(root + "/")


def _context_layers(layers: dict[str, list[dict]]) -> dict[str, dict[str, list[dict]]]:
    grouped: dict[str, dict[str, list[dict]]] = {}
    for layer in CONTEXT_LAYERS:
        if layer not in layers:
            continue
        grouped[layer] = {}
        for row in layers[layer]:
            grouped[layer].setdefault(context_key(layer, row), []).append(row)
    return grouped


def _layers(document: dict, edges: list[dict]) -> dict[str, dict[str, list[dict]]]:
    grouped: dict[str, dict[str, list[dict]]] = {layer: {} for layer in LAYERS}
    for symbol in document["symbols"]:
        grouped["symbols"].setdefault(module_of(str(symbol.get("path") or symbol.get("id"))),
                                      []).append(symbol)
    for edge in edges:
        grouped["callers"].setdefault(module_of(str(edge.get("to"))), []).append(edge)
        grouped["callees"].setdefault(module_of(str(edge.get("from"))), []).append(edge)
    for row in document["symbol_test_map"]:
        grouped["tests"].setdefault(module_of(str(row.get("symbol"))), []).append(row)
    for row in document["files"]:
        grouped["files"].setdefault(module_of(str(row.get("path"))), []).append(row)
    return grouped


def encode_shard(layer: str, rows: Iterable[dict]) -> tuple[bytes, int, int]:
    """(stored bytes, raw bytes, records): sorted canonical JSON lines, gzip mtime 0.

    The `document` layer's rows keep their given order: the document is
    already deterministic, and its order is part of what it says.
    """
    if layer == DOCUMENT_LAYER:
        lines = [canonical(row) for row in rows]
    else:
        lines = [line for _key, line in sorted(((_ORDER[layer](row), canonical(row))
                                                for row in rows),
                                               key=lambda pair: (pair[0], pair[1]))]
    raw = b"".join(line + b"\n" for line in lines)
    return gzip.compress(raw, compresslevel=9, mtime=0), len(raw), len(lines)


def _encode_layers(grouped: dict[str, dict[str, list[dict]]], blobs: dict[str, bytes]
                   ) -> dict[str, dict[str, dict]]:
    table: dict[str, dict[str, dict]] = {}
    for layer, modules in grouped.items():
        table[layer] = {}
        for module in sorted(modules):
            stored, raw_bytes, records = encode_shard(layer, modules[module])
            hexdigest = hashlib.sha256(stored).hexdigest()
            blobs[hexdigest] = stored
            table[layer][module] = {"blob": "sha256:" + hexdigest, "records": records,
                                    "bytes": len(stored), "raw_bytes": raw_bytes}
    return table


def _shard_all(document: dict, edges: list[dict], context: dict[str, list[dict]],
               sections: dict[str, list[dict]]
               ) -> tuple[dict, dict, dict, dict, dict[str, bytes], int, int]:
    """(manifest shards, index shards, context shards, document shards, blobs by
    hex digest, stored bytes, raw bytes)."""
    blobs: dict[str, bytes] = {}
    table = _encode_layers(_layers(document, edges), blobs)
    index_table = _encode_layers(_index_layers(document), blobs)
    context_table = _encode_layers(_context_layers(context), blobs)
    document_table = _encode_layers(
        {DOCUMENT_LAYER: {name: rows for name, rows in sections.items() if rows}}
        if sections else {}, blobs)
    stored_total = sum(len(b) for b in blobs.values())
    raw_total = sum(entry["raw_bytes"]
                    for tab in (table, index_table, context_table, document_table)
                    for layer in tab.values() for entry in layer.values())
    return table, index_table, context_table, document_table, blobs, stored_total, raw_total


def _context_meta(context: dict | None) -> tuple[dict[str, list[dict]], dict]:
    """(layers, the manifest's `context`) from `repo_index_history.build_context`'s answer."""
    if context is None:
        return {}, {"version": CONTEXT_VERSION, "available": False,
                    "reason": "no clone was given to the writer; history and rules are not known"}
    layers = {layer: list(rows) for layer, rows in (context.get("layers") or {}).items()
              if layer in CONTEXT_LAYERS and isinstance(rows, list)}
    meta = dict(context.get("meta") or {})
    meta["version"] = CONTEXT_VERSION
    meta.setdefault("available", bool(layers))
    meta.setdefault("reason", None)
    return layers, meta


def _document_meta(index: Any, commit_sha: str) -> tuple[dict[str, list[dict]], dict]:
    """(sections, the manifest's `document`) from a `swarm.repo-index/v1` document.

    A document that cannot be stored whole is not stored at all, and the
    reason says why: a reader is told the document is missing, never handed
    part of one.
    """
    def missing(reason: str) -> tuple[dict[str, list[dict]], dict]:
        return {}, {"available": False, "reason": reason, "counts": {}, "truncated": []}

    if index is None:
        return missing("no repo-index document was given to the writer")
    if not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA:
        return missing(f"the repo-index document is not a {INDEX_SCHEMA} document")
    if index.get("commit_sha") != commit_sha:
        return missing("the repo-index document describes another commit than the graph's")
    sections: dict[str, list[dict]] = {}
    for name in DOCUMENT_SECTIONS:
        rows = index.get(name)
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            return missing(f"the repo-index document's {name} is not a list")
        if not all(isinstance(row, dict) for row in rows):
            return missing(f"the repo-index document's {name} holds a row that is not an "
                           "object")
        sections[name] = list(rows)
    size = len(canonical(sections))
    if size > MAX_DOCUMENT_BYTES:
        return missing(f"the repo-index document's sections are {size} bytes, over the "
                       f"{MAX_DOCUMENT_BYTES}-byte budget of docs/repo-index.md §2.2")
    truncated = index.get("truncated")
    return sections, {"available": True, "reason": None,
                      "counts": {name: len(rows) for name, rows in sections.items()},
                      "truncated": sorted(str(t) for t in truncated)
                      if isinstance(truncated, list) else []}


def build(document: dict, *, tenant_id: str, repo_id: str,
          max_commit_bytes: int = MAX_COMMIT_BYTES,
          graph_digest: str | None = None,
          context: dict | None = None,
          index: dict | None = None) -> tuple[str, bytes, dict[str, bytes]]:
    """(manifest key, manifest bytes, blobs by hex digest). Writes nothing.

    `context` is `repo_index_history.build_context`'s answer, stored as
    `context_shards`; None stores none and says so in `context`. `index` is
    the commit's `swarm.repo-index/v1` document, stored as the `document`
    layer of `document_shards`; None, or one that cannot be stored, stores
    none and says why in `document`.

    Raises ValueError for a document that is not a graph, an unsafe tenant or
    repo id, or a graph still over the ceiling with only module-level edges.
    """
    _check_graph(document)
    key = manifest_key(tenant_id, repo_id, document["commit_sha"])
    edges = list(document["call_edges"])
    truncated = set(str(t) for t in document.get("truncated") or [])
    layers, context_meta = _context_meta(context)
    sections, document_meta = _document_meta(index, document["commit_sha"])
    table, index_table, context_table, document_table, blobs, stored, raw = _shard_all(
        document, edges, layers, sections)
    # The context layers first: advisory, and mined again on every run.
    for name, dropped in CONTEXT_DROP:
        if stored <= max_commit_bytes:
            break
        if any(layer in layers for layer in dropped):
            layers = {k: v for k, v in layers.items() if k not in dropped}
            truncated.add(name)
            table, index_table, context_table, document_table, blobs, stored, raw = \
                _shard_all(document, edges, layers, sections)
    if stored > max_commit_bytes:
        edges = [e for e in edges if e.get("kind") == "import"
                 or float(e.get("confidence") or 0) >= CEILING_MIN_CONFIDENCE]
        truncated.add("call_edges:below_0.4")
        table, index_table, context_table, document_table, blobs, stored, raw = _shard_all(
            document, edges, layers, sections)
    if stored > max_commit_bytes:
        edges = [e for e in edges if e.get("kind") == "import"]
        truncated.add("call_edges:symbol")
        table, index_table, context_table, document_table, blobs, stored, raw = _shard_all(
            document, edges, layers, sections)
    if stored > max_commit_bytes:
        raise ValueError(
            f"the graph of {document['commit_sha']} is {stored} bytes stored with only "
            f"module-level edges, over the {max_commit_bytes}-byte ceiling; nothing written"
        )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "tenant_id": tenant_id,
        "repo_id": repo_id,
        "commit_sha": document["commit_sha"],
        "branch": document.get("branch"),
        "kind": document.get("kind"),
        "base_sha": document.get("base_sha"),
        "graph_digest": graph_digest or digest_of(canonical(document) + b"\n"),
        "languages": document.get("languages") or [],
        "extractor": document.get("extractor") or {},
        "writer": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "format_version": FORMAT_VERSION,
        "counts": {"symbols": len(document["symbols"]), "call_edges": len(edges),
                   "symbol_test_map": len(document["symbol_test_map"]),
                   "files": len(document["files"]),
                   **{key: len(document.get(key) or []) for key in INDEX_LAYERS.values()}},
        # What a `terms` reader needs besides the postings (`bm25_search`).
        "term_stats": document.get("term_stats") or {},
        "truncated": sorted(truncated),
        "compression": COMPRESSION,
        "blob_suffix": BLOB_SUFFIX,
        "max_commit_bytes": max_commit_bytes,
        "bytes": {"stored": stored, "raw": raw},
        "shards": table,
        "index_shards": index_table,
        # Counted after the ceiling: a dropped layer is absent, not zero.
        "context": {**context_meta,
                    "counts": {layer: len(rows) for layer, rows in sorted(layers.items())}},
        "context_shards": context_table,
        "document": document_meta,
        "document_shards": document_table,
    }
    return key, canonical(manifest) + b"\n", blobs


def _md5s(store: Store, prefix: str) -> dict[str, str | None]:
    md5s = getattr(store, "md5s", None)
    if callable(md5s):
        return md5s(prefix)
    return {key: None for key, _ in store.list(prefix)}


def _get_many(store: Store, keys: list[str],
              progress: Callable[[int, int], None] | None = None) -> dict[str, bytes]:
    get_many = getattr(store, "get_many", None)
    if callable(get_many):
        return get_many(keys) if progress is None else get_many(keys, progress=progress)
    out = {key: store.get(key) for key in keys}
    if progress is not None:
        progress(len(out), len(out))
    return out


def _put_many(store: Store, items: dict[str, bytes]) -> None:
    put_many = getattr(store, "put_many", None)
    if callable(put_many):
        put_many(items, no_clobber=True)
        return
    for key in sorted(items):
        store.put(key, items[key], no_clobber=True)


def _check_present(store: Store, expected: dict[str, bytes], listed: dict[str, str | None]
                   ) -> list[str]:
    """The keys of `expected` that are NOT in the store; raises on any holding other bytes.

    By the listing's MD5 where it gives one, else by reading the object back:
    a blob's name is the sha256 of its bytes, so one that reads back as ours
    is ours, whoever put it.
    """
    absent = sorted(key for key in expected if key not in listed)
    unknown = sorted(key for key in expected if key in listed and listed[key] is None)
    conflicts = sorted(key for key in expected if listed.get(key) is not None
                       and listed[key] != md5_of(expected[key]))
    if unknown:
        fetched = _get_many(store, unknown)
        conflicts += [key for key in unknown if fetched.get(key) != expected[key]]
    if conflicts:
        raise ConflictError(
            f"{len(conflicts)} write-once path(s) already hold different content, first "
            f"{sorted(conflicts)[0]}; nothing was overwritten and no manifest was written"
        )
    return absent


def _carried_shards(store: Store, manifest: bytes, *, tenant_id: str, repo_id: str,
                    base_commit: str) -> tuple[dict[str, tuple[int, int]], set[str]]:
    """({key: (shards carried, shards rewritten)}, the base's blob digests)
    against a base commit, for `shards` and `index_shards` apart.

    A shard is CARRIED when the base manifest names the same blob for the
    same layer and module: an incremental run's unchanged modules (§2.5).
    A base manifest that cannot be read carries nothing; the write itself
    never depends on it.
    """
    try:
        base = json.loads(store.get(manifest_key(tenant_id, repo_id, base_commit)))
        base_blobs = referenced_blobs(base)
    except (StoreError, ValueError):
        return {}, set()
    ours = json.loads(manifest)
    counts: dict[str, tuple[int, int]] = {}
    for key in ("shards", "index_shards", "context_shards", "document_shards"):
        carried = rewritten = 0
        for layer, modules in (ours.get(key) or {}).items():
            before = (base.get(key) or {}).get(layer) or {}
            for module, entry in modules.items():
                if (before.get(module) or {}).get("blob") == entry["blob"]:
                    carried += 1
                else:
                    rewritten += 1
        counts[key] = (carried, rewritten)
    return counts, base_blobs


def write_graph(document: dict, store: Store, *, tenant_id: str, repo_id: str,
                max_commit_bytes: int = MAX_COMMIT_BYTES,
                graph_digest: str | None = None, base_commit: str | None = None,
                context: dict | None = None, index: dict | None = None) -> dict:
    """Shard `document` into `store`: blobs first (never clobbered), manifest last.

    RESUMABLE: a blob already at its path with our bytes counts as written
    (`blobs_reused`), so a run interrupted after any number of blobs
    completes when it is run again; one with other bytes raises
    `ConflictError` and the manifest is not written.

    INCREMENTAL (`base_commit`, §2.5): the shards whose blob the base
    manifest already names are the unchanged modules' and are counted
    `shards_carried`; a listed blob the base names is taken as ours without
    reading it back, since a manifest is written only after its blobs and
    the sweep never deletes a blob a manifest names. Only the rest is
    checked and uploaded.
    """
    key, manifest, blobs = build(document, tenant_id=tenant_id, repo_id=repo_id,
                                 max_commit_bytes=max_commit_bytes, graph_digest=graph_digest,
                                 context=context, index=index)
    blob_prefix = f"{graph_root(tenant_id, repo_id)}/blobs/"
    expected = {blob_key(tenant_id, repo_id, hexdigest): data
                for hexdigest, data in blobs.items()}
    counts: dict[str, tuple[int, int]] = {}
    base_blobs: set[str] = set()
    if base_commit is not None:
        counts, base_blobs = _carried_shards(
            store, manifest, tenant_id=tenant_id, repo_id=repo_id, base_commit=base_commit)
    listed = _md5s(store, blob_prefix)
    known = {blob_key(tenant_id, repo_id, hexdigest) for hexdigest in base_blobs}
    missing = _check_present(
        store, {k: v for k, v in expected.items() if not (k in known and k in listed)}, listed)
    reused = len(expected) - len(missing)
    todo = missing
    upload_error: StoreError | None = None
    for _round in range(UPLOAD_ROUNDS):
        if not todo:
            break
        upload_error = None
        try:
            _put_many(store, {k: expected[k] for k in todo})
        except StoreError as exc:
            # A 412 here is a blob that appeared since the listing, and it may
            # have stopped the rest of its batch: the check accepts it if it
            # is ours, and the next round puts what is still absent.
            upload_error = exc
        todo = _check_present(store, {k: expected[k] for k in todo}, _md5s(store, blob_prefix))
    if todo:
        raise StoreError(f"{len(todo)} blob(s) were not written, first {todo[0]}"
                         + (f": {upload_error}" if upload_error else ""))
    # The manifest is per commit and not write-once: a re-index of the same
    # commit may describe it differently. The same bytes are not put twice.
    current = _md5s(store, key)
    if current.get(key) is None or current[key] != md5_of(manifest):
        store.put(key, manifest, no_clobber=False)
    report = {
        "manifest": key,
        "manifest_digest": digest_of(manifest),
        "blobs_written": len(missing),
        "blobs_reused": reused,
        "stored_bytes": sum(len(b) for b in blobs.values()),
        "manifest_bytes": len(manifest),
    }
    stored_document = json.loads(manifest)["document"]
    report["document"] = {"available": stored_document["available"],
                          "reason": stored_document["reason"]}
    if base_commit is not None:
        # `shards_*` count the five format-2 layers, as they did before format
        # 3; the index layers are counted apart, so neither number changes
        # meaning for a reader of the report.
        carried, rewritten = counts.get("shards", (0, 0))
        index_carried, index_rewritten = counts.get("index_shards", (0, 0))
        context_carried, context_rewritten = counts.get("context_shards", (0, 0))
        document_carried, document_rewritten = counts.get("document_shards", (0, 0))
        report.update(base_commit=base_commit, shards_carried=carried,
                      shards_rewritten=rewritten, index_shards_carried=index_carried,
                      index_shards_rewritten=index_rewritten,
                      context_shards_carried=context_carried,
                      context_shards_rewritten=context_rewritten,
                      document_shards_carried=document_carried,
                      document_shards_rewritten=document_rewritten)
    return report


# --- reading a stored graph back (an incremental run's base, §3.4) ------------

#: The layers a graph document is rebuilt from, and the list each one fills.
#: Every edge is in `callees` exactly once (sharded by its caller's module),
#: so `callers`, the same edges by callee, is not read.
READ_LAYERS = {"symbols": "symbols", "callees": "call_edges", "tests": "symbol_test_map",
               "files": "files"}


def decode_shard(data: bytes) -> list[dict]:
    """A blob's rows: gzip, then one canonical JSON object per line."""
    rows = []
    for line in gzip.decompress(data).splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("a shard line is not a JSON object")
            rows.append(row)
    return rows


def read_manifest(store: Store, *, tenant_id: str, repo_id: str, commit_sha: str,
                  manifest_digest: str | None = None) -> dict:
    """The commit's manifest, checked against the digest promotion recorded, when given."""
    if not _SHA.match(commit_sha or ""):
        raise ValueError("the commit to read is not a 40-hex commit sha")
    raw = store.get(manifest_key(tenant_id, repo_id, commit_sha))
    if manifest_digest is not None and digest_of(raw) != manifest_digest:
        raise ValueError(f"the manifest of {commit_sha} does not match the digest promotion "
                         "recorded for it; it was rewritten, so it is not read")
    manifest = json.loads(raw)
    referenced_blobs(manifest)
    for name, want in (("tenant_id", tenant_id), ("repo_id", repo_id),
                       ("commit_sha", commit_sha)):
        if manifest.get(name) != want:
            raise ValueError(f"the manifest at {commit_sha}'s path names another {name}")
    return manifest


def read_graph(store: Store, *, tenant_id: str, repo_id: str, commit_sha: str,
               manifest_digest: str | None = None,
               progress: Callable[[str], None] | None = None) -> dict:
    """The `swarm.repo-graph/v1` document a commit's shards hold.

    Every blob is checked against the digest its name is BEFORE it is
    decompressed, so a rewritten blob is refused, not read. `progress` is
    told each step as it ends, with the seconds since the read began: the
    worker records the last of them when the read runs out of its budget.
    """
    started = time.monotonic()

    def say(message: str) -> None:
        if progress is not None:
            progress(f"{message} in {time.monotonic() - started:.1f} s")

    manifest = read_manifest(store, tenant_id=tenant_id, repo_id=repo_id,
                             commit_sha=commit_sha, manifest_digest=manifest_digest)
    wanted: dict[str, list[str]] = {}
    for layer in READ_LAYERS:
        for entry in (manifest["shards"].get(layer) or {}).values():
            hexdigest = entry["blob"].split(":", 1)[1]
            wanted.setdefault(layer, []).append(hexdigest)
    # Format 3's layers; a format-2 manifest has none, and its graph is read
    # with those lists empty.
    for layer, entries in _index_shards(manifest).items():
        for entry in entries.values():
            wanted.setdefault("index:" + layer, []).append(entry["blob"].split(":", 1)[1])
    keys = sorted({blob_key(tenant_id, repo_id, h) for hs in wanted.values() for h in hs})
    stored = (manifest.get("bytes") or {}).get("stored")
    say(f"manifest of {commit_sha[:12]} read: {len(keys)} blobs to fetch of a graph "
        f"{stored} bytes stored")
    fetched = _get_many(store, keys,
                        lambda done, total: say(f"downloaded {done}/{total} blobs"))
    document: dict[str, Any] = {
        "schema": GRAPH_SCHEMA, "kind": manifest.get("kind"), "commit_sha": commit_sha,
        "branch": manifest.get("branch"), "base_sha": manifest.get("base_sha"),
        "languages": manifest.get("languages") or [],
        "truncated": list(manifest.get("truncated") or []),
        "extractor": manifest.get("extractor") or {},
    }
    if manifest_format(manifest) >= 3:
        document["term_stats"] = manifest.get("term_stats") or {}
    targets = [(layer, target) for layer, target in READ_LAYERS.items()]
    targets += [("index:" + layer, target) for layer, target in INDEX_LAYERS.items()]
    for layer, target in targets:
        rows: list[dict] = []
        for hexdigest in wanted.get(layer, []):
            data = fetched.get(blob_key(tenant_id, repo_id, hexdigest))
            if data is None or hashlib.sha256(data).hexdigest() != hexdigest:
                raise ValueError(f"blob {hexdigest} does not hold the bytes its name is the "
                                 "digest of; the graph is not read")
            rows.extend(decode_shard(data))
        document[target] = rows
    say(f"decoded {len(fetched)} blobs")
    document["symbols"].sort(key=lambda s: (str(s.get("path")), s.get("start_line") or 0,
                                            str(s.get("id"))))
    document["call_edges"].sort(key=lambda e: (str(e.get("from")), str(e.get("to")),
                                               str(e.get("kind"))))
    document["symbol_test_map"].sort(key=lambda t: (str(t.get("symbol")), str(t.get("test"))))
    document["files"].sort(key=lambda f: str(f.get("path")))
    for layer, target in INDEX_LAYERS.items():
        document[target].sort(key=_ORDER[layer])
    return document


def read_to_file(store: Store, *, tenant_id: str, repo_id: str, commit_sha: str,
                 manifest_digest: str | None, out: Path,
                 progress: Callable[[str], None] | None = None) -> dict:
    """`read`: the commit's graph document, written to `out`; the report it prints."""
    started = time.monotonic()
    graph = read_graph(store, tenant_id=tenant_id, repo_id=repo_id, commit_sha=commit_sha,
                       manifest_digest=manifest_digest, progress=progress)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(graph) + b"\n"
    out.write_bytes(data)
    seconds = round(time.monotonic() - started, 1)
    if progress is not None:
        progress(f"wrote {len(data)} bytes in {seconds} s")
    return {"read": commit_sha, "symbols": len(graph["symbols"]),
            "call_edges": len(graph["call_edges"]), "files": len(graph["files"]),
            "communities": len(graph["communities"]), "bytes": len(data), "seconds": seconds}


def read_context(store: Store, *, tenant_id: str, repo_id: str, commit_sha: str,
                 layer: str, path: str | None = None,
                 manifest_digest: str | None = None) -> list[dict]:
    """A context layer's rows: every row, or those that reach `path`.

    For a path, the shards of its directory and each ancestor are read
    (`context_keys`), and `rules` keeps the rows `rule_reaches` says apply;
    the other per-path layers keep the rows naming the path itself. Each blob
    is checked against its name before it is decompressed, as `read_graph`
    does. A manifest without `context_shards` answers nothing.
    """
    if layer not in CONTEXT_LAYERS:
        raise ValueError(f"{layer!r} is not a context layer")
    manifest = read_manifest(store, tenant_id=tenant_id, repo_id=repo_id,
                             commit_sha=commit_sha, manifest_digest=manifest_digest)
    entries = _context_shards(manifest).get(layer) or {}
    keys = sorted(entries) if path is None or layer == "cannot_push" \
        else [k for k in context_keys(path) if k in entries]
    digests = [entries[k]["blob"].split(":", 1)[1] for k in keys]
    fetched = _get_many(store, [blob_key(tenant_id, repo_id, h) for h in digests])
    rows: list[dict] = []
    for hexdigest in digests:
        data = fetched.get(blob_key(tenant_id, repo_id, hexdigest))
        if data is None or hashlib.sha256(data).hexdigest() != hexdigest:
            raise ValueError(f"blob {hexdigest} does not hold the bytes its name is the "
                             "digest of; the context is not read")
        rows.extend(decode_shard(data))
    if path is None or layer == "cannot_push":
        return rows
    if layer == "rules":
        return [r for r in rows if rule_reaches(r, path)]
    name = "test" if layer in ("test_fixes", "test_runners") else "path"
    return [r for r in rows if r.get(name) == path]


def read_document(store: Store, *, tenant_id: str, repo_id: str, commit_sha: str,
                  section: str | None = None,
                  manifest_digest: str | None = None) -> dict[str, list[dict]]:
    """The repo-index document's sections a commit's `document` layer holds.

    Every section, or the one named, in the order it was written; a section
    with no rows is `[]`. A manifest without the layer, or one whose
    `document` says it is not available, answers `{}`. Each blob is checked
    against its name before it is decompressed, as `read_graph` does.
    """
    if section is not None and section not in DOCUMENT_SECTIONS:
        raise ValueError(f"{section!r} is not a section of the repo-index document")
    manifest = read_manifest(store, tenant_id=tenant_id, repo_id=repo_id,
                             commit_sha=commit_sha, manifest_digest=manifest_digest)
    meta = manifest.get("document")
    if not isinstance(meta, dict) or meta.get("available") is not True:
        return {}
    entries = _document_shards(manifest).get(DOCUMENT_LAYER) or {}
    names = [section] if section is not None else list(DOCUMENT_SECTIONS)
    digests = {name: entries[name]["blob"].split(":", 1)[1] for name in names if name in entries}
    fetched = _get_many(store, [blob_key(tenant_id, repo_id, h) for h in digests.values()])
    sections: dict[str, list[dict]] = {}
    for name in names:
        hexdigest = digests.get(name)
        if hexdigest is None:
            sections[name] = []
            continue
        data = fetched.get(blob_key(tenant_id, repo_id, hexdigest))
        if data is None or hashlib.sha256(data).hexdigest() != hexdigest:
            raise ValueError(f"blob {hexdigest} does not hold the bytes its name is the "
                             "digest of; the document is not read")
        sections[name] = decode_shard(data)
    return sections


def manifest_format(manifest: dict) -> int:
    """The shard format a manifest was written in: 2 when it does not say."""
    value = manifest.get("format_version")
    return value if isinstance(value, int) and not isinstance(value, bool) else 2


def _index_shards(manifest: dict) -> dict[str, dict]:
    """Format 3's layers, checked; `{}` for a format-2 manifest."""
    table = manifest.get("index_shards")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ValueError("the manifest's index_shards is not an object")
    for layer, entries in table.items():
        if layer not in INDEX_LAYERS or not isinstance(entries, dict):
            raise ValueError(f"the manifest names an unknown index layer {layer!r}")
    return table


def _context_shards(manifest: dict) -> dict[str, dict]:
    """The context layers (lane CTXQ-H), checked; `{}` for a manifest without them."""
    table = manifest.get("context_shards")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ValueError("the manifest's context_shards is not an object")
    for layer, entries in table.items():
        if layer not in CONTEXT_LAYERS or not isinstance(entries, dict):
            raise ValueError(f"the manifest names an unknown context layer {layer!r}")
    return table


def _document_shards(manifest: dict) -> dict[str, dict]:
    """The document layer (lane GS-INDEXDOC), checked; `{}` for a manifest without it."""
    table = manifest.get("document_shards")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ValueError("the manifest's document_shards is not an object")
    for layer, entries in table.items():
        if layer != DOCUMENT_LAYER or not isinstance(entries, dict):
            raise ValueError(f"the manifest names an unknown document layer {layer!r}")
    return table


# --- the sweep ----------------------------------------------------------------

def referenced_blobs(manifest: dict) -> set[str]:
    """Every blob hex digest a manifest names. Raises ValueError on a malformed one."""
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"not a {MANIFEST_SCHEMA} manifest")
    shards = manifest.get("shards")
    if not isinstance(shards, dict):
        raise ValueError("the manifest has no shards")
    found: set[str] = set()
    # The sweep deletes what no manifest names: an index, context or document
    # layer's blob left out here would be deleted under a live manifest.
    for layer in [*shards.values(), *_index_shards(manifest).values(),
                  *_context_shards(manifest).values(), *_document_shards(manifest).values()]:
        if not isinstance(layer, dict):
            raise ValueError("a manifest layer is not an object")
        for entry in layer.values():
            blob = entry.get("blob") if isinstance(entry, dict) else None
            if not isinstance(blob, str) or not re.match(r"^sha256:[0-9a-f]{64}$", blob):
                raise ValueError("a manifest shard names no sha256 blob")
            found.add(blob.split(":", 1)[1])
    return found


def _delete_many(store: Store, keys: list[str]) -> None:
    """One batched delete when the store has one (`GcsStore.delete_many`), else one by one."""
    if not keys:
        return
    many = getattr(store, "delete_many", None)
    if many is not None:
        many(keys)
        return
    for key in sorted(keys):
        store.delete(key)


def _commit_in(key: str, prefix: str, name: str) -> str | None:
    """The commit of `<prefix><commit_sha>/<name>`, or None for any other key."""
    if not key.startswith(prefix):
        return None
    commit, _, rest = key[len(prefix):].partition("/")
    return commit if rest == name and _SHA.match(commit) else None


def check_keep(commits: Iterable[str]) -> frozenset[str]:
    """`--keep-commit` values, each a 40-hex sha. Raises ValueError on any other."""
    kept = frozenset(commits)
    for commit in kept:
        if not _SHA.match(commit or ""):
            raise ValueError(f"--keep-commit {commit!r} is not a 40-hex commit sha")
    return kept


def sweep(store: Store, *, tenant_id: str, repo_id: str, now: datetime | None = None,
          grace: timedelta = SWEEP_GRACE, keep: Iterable[str] | None = None,
          max_deletes: int = MAX_SWEEP_DELETES) -> dict:
    """Retire what no kept version names, then delete the blobs no manifest names.

    `keep` is the set of commits whose index versions swarm-api still holds
    (the last 20, §2.3), which the worker reads and passes as `--keep-commit`.
    With it, every manifest and every index copy of a commit NOT in `keep` and
    older than `grace` is deleted first, and its references stop counting.
    Without it (None) no manifest or index copy is deleted: what the sweep did
    before lane IX3, and what a run that could not read the versions gets.

    Blobs: deleted only when no remaining manifest names them and older than
    `grace`. Deletes nothing at all, and says why in `refused`, if any
    manifest it keeps cannot be read. At most `max_deletes` objects go per
    sweep; a retired manifest past the bound is kept for the next sweep and
    its references still count, so the bound can only delay a deletion.
    """
    now = now or datetime.now(timezone.utc)
    kept = None if keep is None else check_keep(keep)
    root = graph_root(tenant_id, repo_id)
    blob_prefix = f"{root}/blobs/"
    index_prefix = f"{repo_root(tenant_id, repo_id)}/index/"

    def old(created: datetime | None) -> bool:
        return created is not None and now - created >= grace

    retire: list[str] = []
    readable: list[str] = []
    for key, created in store.list(f"{root}/"):
        if key.startswith(blob_prefix) or not key.endswith("/manifest.json"):
            continue
        commit = _commit_in(key, f"{root}/", "manifest.json")
        if kept is not None and commit is not None and commit not in kept and old(created):
            retire.append(key)
        else:
            readable.append(key)
    retire_index: list[str] = []
    kept_indexes = 0
    if kept is not None:
        for key, created in store.list(index_prefix):
            commit = _commit_in(key, index_prefix, INDEX_NAME)
            if commit is None:
                continue
            if commit in kept or not old(created):
                kept_indexes += 1
            else:
                retire_index.append(key)
    budget = max(0, max_deletes)
    retiring = retire[:budget]
    readable += retire[budget:]
    budget -= len(retiring)
    retiring_index = retire_index[:budget]
    budget -= len(retiring_index)
    truncated = len(retiring) < len(retire) or len(retiring_index) < len(retire_index)
    report = {"manifests": len(readable), "retired_manifests": 0, "retired_indexes": 0,
              "kept_indexes": kept_indexes, "deleted": 0, "kept_referenced": 0,
              "kept_young": 0, "truncated": truncated, "refused": None}
    # Every manifest that stays is read BEFORE anything is deleted.
    referenced: set[str] = set()
    for key in readable:
        try:
            referenced |= referenced_blobs(json.loads(store.get(key).decode("utf-8")))
        except (StoreError, ValueError, UnicodeDecodeError) as exc:
            report["refused"] = (f"{key} could not be read ({exc}); a manifest the sweep "
                                 "cannot read is references it cannot see")
            return report
    try:
        _delete_many(store, retiring + retiring_index)
    except StoreError as exc:
        report["refused"] = (f"retiring {len(retiring)} manifests and {len(retiring_index)} "
                             f"index copies failed ({exc}); no blob was swept")
        return report
    report["retired_manifests"] = len(retiring)
    report["retired_indexes"] = len(retiring_index)
    doomed: list[str] = []
    for key, created in store.list(blob_prefix):
        match = _BLOB_NAME.match(key[len(blob_prefix):])
        if match is None:
            continue
        if match.group(1) in referenced:
            report["kept_referenced"] += 1
            continue
        if not old(created):
            report["kept_young"] += 1
            continue
        if len(doomed) >= budget:
            report["truncated"] = True
            continue
        doomed.append(key)
    try:
        _delete_many(store, doomed)
    except StoreError as exc:
        report["refused"] = f"deleting {len(doomed)} unreferenced blobs failed ({exc})"
        return report
    report["deleted"] = len(doomed)
    return report


# --- the CLI ------------------------------------------------------------------

def _history_module() -> Any:
    """repo_index_history, from beside this file.

    Loaded by path: the image runs this file as `python3.11 -I`, which puts
    no script directory on `sys.path`.
    """
    loaded = sys.modules.get(HISTORY_MODULE)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().with_name(HISTORY_MODULE + ".py")
    spec = importlib.util.spec_from_file_location(HISTORY_MODULE, path)
    if spec is None or spec.loader is None or not path.is_file():
        raise OSError(f"{path.name} is not installed beside the writer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[HISTORY_MODULE] = module
    spec.loader.exec_module(module)
    return module


def mine_context(repo: Path, document: dict) -> dict:
    """The history and rules layers of the clone at `repo`, or why there are none.

    Never raises: the graph is written whatever happens here, and the
    manifest's `context` says what did.
    """
    if not repo.is_dir():
        return {"layers": {}, "meta": {"available": False,
                                       "reason": f"no clone at {repo}; history and rules "
                                                 "are not known"}}
    try:
        return _history_module().build_context(repo, commit_sha=document["commit_sha"],
                                               files=document.get("files") or [])
    except Exception as exc:  # noqa: BLE001 -- a context layer is never worth the graph
        return {"layers": {}, "meta": {"available": False,
                                       "reason": f"mining the clone failed: "
                                                 f"{type(exc).__name__}"}}


def self_test() -> int:
    """Shard and sweep a two-symbol graph in a scratch directory."""
    commit = hashlib.sha1(b"self-test").hexdigest()
    document = {
        "schema": GRAPH_SCHEMA, "kind": "full", "commit_sha": commit, "branch": "main",
        "base_sha": None, "languages": [], "truncated": [], "extractor": {},
        "symbols": [{"id": "a/b.py#f", "path": "a/b.py", "start_line": 1},
                    {"id": "c.py#g", "path": "c.py", "start_line": 1}],
        "call_edges": [{"from": "a/b.py#f", "to": "c.py#g", "kind": "call",
                        "confidence": 0.6}],
        "symbol_test_map": [], "files": [{"path": "a/b.py"}, {"path": "c.py"}],
        "terms": [{"term": "self", "postings": {"a/b.py": [["f", 1, 1]]}}],
        "term_stats": {"documents": 2, "average_length": 1.0, "k1": BM25_K1, "b": BM25_B,
                       "tokenizer": TOKENIZER_VERSION},
    }
    with tempfile.TemporaryDirectory(prefix="repo-graph-self-test-") as scratch:
        # The history module mines a two-file checkout: it is installed, and a
        # doc naming a path is a rule the sweep keeps.
        checkout = Path(scratch) / "repo"
        (checkout / "a").mkdir(parents=True)
        (checkout / "a" / "b.py").write_text("def f():\n    return 1\n")
        (checkout / "README.md").write_text("Start at `a/b.py`.\n")
        context = mine_context(checkout, {**document, "files": [
            {"path": "a/b.py"}, {"path": "README.md"}]})
        store = LocalStore(str(Path(scratch) / "store"))
        index = {"schema": INDEX_SCHEMA, "commit_sha": commit,
                 "notes": [{"text": "c.py is the entry", "source": "README.md"}]}
        first = write_graph(document, store, tenant_id="t", repo_id="r", context=context,
                            index=index)
        second = write_graph(document, store, tenant_id="t", repo_id="r", context=context,
                             index=index)
        swept = sweep(store, tenant_id="t", repo_id="r", grace=timedelta(0))
        back = read_graph(store, tenant_id="t", repo_id="r", commit_sha=commit)
        rules = read_context(store, tenant_id="t", repo_id="r", commit_sha=commit,
                             layer="rules", path="a/b.py")
        notes = read_document(store, tenant_id="t", repo_id="r", commit_sha=commit,
                              section="notes")
    if (notes != {"notes": index["notes"]} or first["manifest_digest"] != second["manifest_digest"] or second["blobs_written"]
            or swept["deleted"] or swept["refused"] or back["terms"] != document["terms"]
            or [r.get("source") for r in rules] != ["README.md"]):
        print("repo-graph self-test: failed", file=sys.stderr)
        return 1
    print("repo-graph self-test: ok")
    return 0


#: Where the step's own configuration is read: PID 1's environment. Dispatch
#: sets `TENANT_ID` and `ARTIFACT_BUCKET` on the worker's container
#: (scheduler/dispatch.py `worker_env`), where no caller can reach them; the
#: agent this tool runs under gets an allowlisted environment without either
#: (runners/cliagent.py), and can set its own environment to anything, but not
#: PID 1's, which every process of the container can read (hardening.py, "what
#: it does not cover"). The process's own environment is the fallback, for a
#: run outside a worker.
CONTAINER_ENVIRON = Path("/proc/1/environ")
TENANT_ENV = "TENANT_ID"
BUCKET_ENV = "ARTIFACT_BUCKET"


def configuration(name: str) -> str | None:
    """`name` from the container's configuration, else this process's; None when neither has it."""
    try:
        raw = CONTAINER_ENVIRON.read_bytes()
    except OSError:
        raw = b""
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep and key.decode("utf-8", "replace") == name:
            text = value.decode("utf-8", "replace").strip()
            if text:
                return text
    return os.environ.get(name, "").strip() or None


def resolve_target(*, tenant: str | None, store: str | None, repo_id: str,
                   destination: str | None) -> tuple[str, str]:
    """(tenant, store spec) for one run, from the step's configuration.

    The indexer prompt names the repo_id and the destination; the tenant and
    the bucket are this step's own. A `--tenant` or `gs://` `--store` the
    configuration contradicts is refused, not preferred: the writer writes
    only under its own tenant's prefix (invariant 9), in its own bucket. A
    local `--store` is a directory standing in for the bucket and is allowed.
    A `--destination` must be exactly the registration's graph prefix under
    that tenant; anything else is outside it and refused before anything is
    read or written.
    """
    configured = configuration(TENANT_ENV)
    if tenant and configured and tenant != configured:
        raise ValueError(f"--tenant {tenant!r} is not this step's tenant; the configuration "
                         f"names another")
    tenant = configured or tenant
    if not tenant:
        raise ValueError(f"no tenant: {TENANT_ENV} is not in this step's configuration and "
                         "--tenant was not given")
    bucket = configuration(BUCKET_ENV)
    if store is None:
        if not bucket:
            raise ValueError(f"no store: {BUCKET_ENV} is not in this step's configuration and "
                             "--store was not given")
        store = f"gs://{bucket}"
    elif store.startswith("gs://") and bucket and store[len("gs://"):].rstrip("/") != bucket:
        raise ValueError(f"--store {store} is not this step's bucket; the configuration names "
                         "another")
    if destination is not None:
        expected = graph_root(tenant, repo_id)
        if destination.strip().rstrip("/") != expected:
            raise ValueError(f"--destination {destination!r} is outside this step's tenant "
                             f"prefix for {repo_id!r}: it must be {expected}")
    return tenant, store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Graph shards and the blob sweep (docs/repo-index.md §2.5).",
    )
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")
    write = sub.add_parser("write", help="shard a swarm-repo-index --graph-out document")
    sweeper = sub.add_parser("sweep", help="delete blobs no manifest names")
    reader = sub.add_parser("read", help="rebuild a stored commit's graph document "
                                         "(an incremental run's base)")
    for command in (write, sweeper, reader):
        command.add_argument("--store",
                             help="gs://<bucket>, or a local directory standing in for it "
                                  f"(default: gs://${BUCKET_ENV} from the step's configuration)")
        command.add_argument("--tenant",
                             help=f"default: ${TENANT_ENV} from the step's configuration, "
                                  "which a different value contradicts")
        command.add_argument("--repo-id", required=True)
        command.add_argument("--destination",
                             help="tenants/<tenant>/repos/<repo_id>/graph, as the task names it; "
                                  "refused unless it is exactly this step's tenant's prefix")
    write.add_argument("--graph", required=True, help="the --graph-out file")
    write.add_argument("--index", help="repo-index.json: its graph.manifest_digest is set")
    write.add_argument("--max-commit-bytes", type=int, default=MAX_COMMIT_BYTES)
    write.add_argument("--no-sweep", action="store_true", help="skip the sweep after writing")
    write.add_argument("--base-commit",
                       help="incremental: the base's commit, to count the shards carried")
    write.add_argument("--repo",
                       help="the clone the graph was extracted from, mined for the history "
                            "and rules layers (default: repo/ beside the --graph file, the "
                            "worker's work/repo)")
    for command in (write, sweeper):
        command.add_argument("--keep-commit", action="append", metavar="SHA",
                             help="a commit whose index version is kept (repeat it); with any, "
                                  "the sweep retires every other commit's manifest and index "
                                  "copy older than a day (§2.3). The written commit is kept too")
    reader.add_argument("--commit", required=True, help="the commit whose graph to read")
    reader.add_argument("--manifest-digest",
                        help="sha256:<hex> promotion recorded; a manifest that differs is refused")
    reader.add_argument("--out", required=True, help="where to write the graph document")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        tenant, spec = resolve_target(tenant=args.tenant, store=args.store,
                                      repo_id=args.repo_id, destination=args.destination)
        store = open_store(spec)
        keep = None if args.command == "read" or args.keep_commit is None \
            else check_keep(args.keep_commit)
        if args.command == "sweep":
            report: dict = {"sweep": sweep(store, tenant_id=tenant, repo_id=args.repo_id,
                                           keep=keep)}
        elif args.command == "read":
            # Each step on stderr as it ends: the worker names the last one
            # when the read runs out of its budget (#1469).
            report = read_to_file(
                store, tenant_id=tenant, repo_id=args.repo_id, commit_sha=args.commit,
                manifest_digest=args.manifest_digest, out=Path(args.out),
                progress=lambda message: print(f"{TOOL_NAME} read: {message}",
                                               file=sys.stderr, flush=True))
        else:
            raw = Path(args.graph).read_bytes()
            document = json.loads(raw)
            _check_graph(document)
            repo = Path(args.repo) if args.repo else Path(args.graph).resolve().parent / "repo"
            context = mine_context(repo, document)
            # Read before the write: its sections are the `document` layer. The
            # `graph` block it gains below is not one of them.
            index_path = Path(args.index) if args.index else None
            index = json.loads(index_path.read_bytes()) if index_path is not None else None
            report = write_graph(document, store, tenant_id=tenant,
                                 repo_id=args.repo_id, max_commit_bytes=args.max_commit_bytes,
                                 graph_digest=digest_of(raw), base_commit=args.base_commit,
                                 context=context, index=index)
            report["context"] = {"available": bool(context["meta"].get("available")),
                                 "reason": context["meta"].get("reason")}
            if index_path is not None and isinstance(index, dict):
                graph = index.get("graph") if isinstance(index.get("graph"), dict) else {}
                graph["manifest_digest"] = report["manifest_digest"]
                index["graph"] = graph
                index_path.write_bytes(canonical(index) + b"\n")
            if not args.no_sweep:
                if keep is not None:
                    # The commit just written is never retired by its own run.
                    keep = keep | {document["commit_sha"]}
                report["sweep"] = sweep(store, tenant_id=tenant, repo_id=args.repo_id,
                                        keep=keep)
    except (ValueError, StoreError, OSError) as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
