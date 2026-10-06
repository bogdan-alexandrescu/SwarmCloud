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

THE 256 MiB CEILING (§2.5). Over it, symbol edges below confidence 0.4 are
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
import gzip
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

GRAPH_SCHEMA = "swarm.repo-graph/v1"
MANIFEST_SCHEMA = "swarm.repo-graph-manifest/v1"
COMPRESSION = "gzip"
BLOB_SUFFIX = ".jsonl.gz"
TOOL_NAME = "swarm-repo-graph"
TOOL_VERSION = "1"

#: §2.5's hard ceiling per commit, on the stored bytes of every blob a
#: manifest names (reused blobs included: it bounds what one commit's reader
#: may have to fetch, not what this run uploaded).
MAX_COMMIT_BYTES = 256 * 1024 * 1024
#: §2.5: over the ceiling, symbol edges below this confidence go first.
CEILING_MIN_CONFIDENCE = 0.4
#: The sweep keeps an unreferenced blob younger than this: a writer may be
#: between its blobs and its manifest. Far above §3.5's longest timeout (2 h).
SWEEP_GRACE = timedelta(days=1)

#: The layers a manifest names, in the order they are written.
LAYERS = ("symbols", "callers", "callees", "tests", "files")

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_BLOB_NAME = re.compile(r"^([0-9a-f]{64})" + re.escape(BLOB_SUFFIX) + r"$")
#: The `#<generation>` a versioned bucket's listing appends to an object's url.
_GENERATION = re.compile(r"#\d+$")
#: Files per `gcloud storage cp`: far below any argv limit (a blob path is
#: about 120 bytes), and a full graph's ~370 blobs go up in one call.
BATCH_FILES = 500
#: Upload rounds: a round whose batch stopped on a blob that appeared
#: meanwhile (a 412) is followed by one for what is still absent.
UPLOAD_ROUNDS = 3


# --- naming -------------------------------------------------------------------

def _segment(value: str, what: str) -> str:
    """One traversal-free key segment: the tenant id IS the isolation boundary."""
    if not isinstance(value, str) or not _SEGMENT.match(value) or ".." in value:
        raise ValueError(f"{what} {value!r} is not a single safe path segment")
    return value


def graph_root(tenant_id: str, repo_id: str) -> str:
    return (f"tenants/{_segment(tenant_id, 'tenant_id')}/repos/"
            f"{_segment(repo_id, 'repo_id')}/graph")


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
#:   get_many(keys) -> {key: bytes}              several reads in one call
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

    def md5s(self, prefix: str) -> dict[str, str | None]:
        return {key: md5_of(self._path(key).read_bytes()) for key, _ in self.list(prefix)}

    def get_many(self, keys: Iterable[str]) -> dict[str, bytes]:
        return {key: self.get(key) for key in keys}

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

    def get_many(self, keys: Iterable[str]) -> dict[str, bytes]:
        """One `gcloud storage cp` of up to BATCH_FILES objects per directory, into scratch."""
        wanted = {key: b"" for key in keys}
        out: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="repo-graph-get-") as scratch:
            for n, (directory, names) in enumerate(sorted(_by_directory(wanted).items())):
                staged = Path(scratch) / str(n)
                staged.mkdir()
                for start in range(0, len(names), BATCH_FILES):
                    batch = names[start:start + BATCH_FILES]
                    try:
                        self._run(["gcloud", "storage", "cp"]
                                  + [self._url(f"{directory}/{name}") for name in batch]
                                  + [str(staged) + "/"])
                    except FileNotFoundError:
                        raise StoreError(f"{directory}: not found") from None
                for name in names:
                    try:
                        out[f"{directory}/{name}"] = (staged / name).read_bytes()
                    except OSError:
                        raise StoreError(f"{directory}/{name}: not read") from None
        return out

    def delete(self, key: str) -> None:
        self._run(["gcloud", "storage", "rm", self._url(key)])


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


_ORDER: dict[str, Callable[[dict], tuple]] = {
    "symbols": lambda s: (str(s.get("path")), s.get("start_line") or 0, str(s.get("id"))),
    "callers": lambda e: (str(e.get("to")), str(e.get("from")), str(e.get("kind"))),
    "callees": lambda e: (str(e.get("from")), str(e.get("to")), str(e.get("kind"))),
    "tests": lambda t: (str(t.get("symbol")), str(t.get("test"))),
    "files": lambda f: (str(f.get("path")),),
}


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
    """(stored bytes, raw bytes, records): sorted canonical JSON lines, gzip mtime 0."""
    lines = sorted(((_ORDER[layer](row), canonical(row)) for row in rows),
                   key=lambda pair: (pair[0], pair[1]))
    raw = b"".join(line + b"\n" for _key, line in lines)
    return gzip.compress(raw, compresslevel=9, mtime=0), len(raw), len(lines)


def _shard_all(document: dict, edges: list[dict]) -> tuple[dict, dict[str, bytes], int, int]:
    """(manifest shards, blobs by hex digest, stored bytes, raw bytes)."""
    table: dict[str, dict[str, dict]] = {}
    blobs: dict[str, bytes] = {}
    for layer, modules in _layers(document, edges).items():
        table[layer] = {}
        for module in sorted(modules):
            stored, raw_bytes, records = encode_shard(layer, modules[module])
            hexdigest = hashlib.sha256(stored).hexdigest()
            blobs[hexdigest] = stored
            table[layer][module] = {"blob": "sha256:" + hexdigest, "records": records,
                                    "bytes": len(stored), "raw_bytes": raw_bytes}
    stored_total = sum(len(b) for b in blobs.values())
    raw_total = sum(entry["raw_bytes"] for layer in table.values() for entry in layer.values())
    return table, blobs, stored_total, raw_total


def build(document: dict, *, tenant_id: str, repo_id: str,
          max_commit_bytes: int = MAX_COMMIT_BYTES,
          graph_digest: str | None = None) -> tuple[str, bytes, dict[str, bytes]]:
    """(manifest key, manifest bytes, blobs by hex digest). Writes nothing.

    Raises ValueError for a document that is not a graph, an unsafe tenant or
    repo id, or a graph still over the ceiling with only module-level edges.
    """
    _check_graph(document)
    key = manifest_key(tenant_id, repo_id, document["commit_sha"])
    edges = list(document["call_edges"])
    truncated = set(str(t) for t in document.get("truncated") or [])
    table, blobs, stored, raw = _shard_all(document, edges)
    if stored > max_commit_bytes:
        edges = [e for e in edges if e.get("kind") == "import"
                 or float(e.get("confidence") or 0) >= CEILING_MIN_CONFIDENCE]
        truncated.add("call_edges:below_0.4")
        table, blobs, stored, raw = _shard_all(document, edges)
    if stored > max_commit_bytes:
        edges = [e for e in edges if e.get("kind") == "import"]
        truncated.add("call_edges:symbol")
        table, blobs, stored, raw = _shard_all(document, edges)
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
        "counts": {"symbols": len(document["symbols"]), "call_edges": len(edges),
                   "symbol_test_map": len(document["symbol_test_map"]),
                   "files": len(document["files"])},
        "truncated": sorted(truncated),
        "compression": COMPRESSION,
        "blob_suffix": BLOB_SUFFIX,
        "max_commit_bytes": max_commit_bytes,
        "bytes": {"stored": stored, "raw": raw},
        "shards": table,
    }
    return key, canonical(manifest) + b"\n", blobs


def _md5s(store: Store, prefix: str) -> dict[str, str | None]:
    md5s = getattr(store, "md5s", None)
    if callable(md5s):
        return md5s(prefix)
    return {key: None for key, _ in store.list(prefix)}


def _get_many(store: Store, keys: list[str]) -> dict[str, bytes]:
    get_many = getattr(store, "get_many", None)
    if callable(get_many):
        return get_many(keys)
    return {key: store.get(key) for key in keys}


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


def write_graph(document: dict, store: Store, *, tenant_id: str, repo_id: str,
                max_commit_bytes: int = MAX_COMMIT_BYTES,
                graph_digest: str | None = None) -> dict:
    """Shard `document` into `store`: blobs first (never clobbered), manifest last.

    RESUMABLE: a blob already at its path with our bytes counts as written
    (`blobs_reused`), so a run interrupted after any number of blobs
    completes when it is run again; one with other bytes raises
    `ConflictError` and the manifest is not written.
    """
    key, manifest, blobs = build(document, tenant_id=tenant_id, repo_id=repo_id,
                                 max_commit_bytes=max_commit_bytes, graph_digest=graph_digest)
    blob_prefix = f"{graph_root(tenant_id, repo_id)}/blobs/"
    expected = {blob_key(tenant_id, repo_id, hexdigest): data
                for hexdigest, data in blobs.items()}
    missing = _check_present(store, expected, _md5s(store, blob_prefix))
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
    return {
        "manifest": key,
        "manifest_digest": digest_of(manifest),
        "blobs_written": len(missing),
        "blobs_reused": reused,
        "stored_bytes": sum(len(b) for b in blobs.values()),
        "manifest_bytes": len(manifest),
    }


# --- the sweep ----------------------------------------------------------------

def referenced_blobs(manifest: dict) -> set[str]:
    """Every blob hex digest a manifest names. Raises ValueError on a malformed one."""
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"not a {MANIFEST_SCHEMA} manifest")
    shards = manifest.get("shards")
    if not isinstance(shards, dict):
        raise ValueError("the manifest has no shards")
    found: set[str] = set()
    for layer in shards.values():
        if not isinstance(layer, dict):
            raise ValueError("a manifest layer is not an object")
        for entry in layer.values():
            blob = entry.get("blob") if isinstance(entry, dict) else None
            if not isinstance(blob, str) or not re.match(r"^sha256:[0-9a-f]{64}$", blob):
                raise ValueError("a manifest shard names no sha256 blob")
            found.add(blob.split(":", 1)[1])
    return found


def sweep(store: Store, *, tenant_id: str, repo_id: str, now: datetime | None = None,
          grace: timedelta = SWEEP_GRACE) -> dict:
    """Delete the registration's blobs no manifest names and older than `grace`.

    Deletes nothing, and says why in `refused`, if any manifest is unreadable.
    """
    now = now or datetime.now(timezone.utc)
    root = graph_root(tenant_id, repo_id)
    blob_prefix = f"{root}/blobs/"
    referenced: set[str] = set()
    manifests = 0
    for key, _created in store.list(f"{root}/"):
        if key.startswith(blob_prefix) or not key.endswith("/manifest.json"):
            continue
        manifests += 1
        try:
            referenced |= referenced_blobs(json.loads(store.get(key).decode("utf-8")))
        except (StoreError, ValueError, UnicodeDecodeError) as exc:
            return {"manifests": manifests, "deleted": 0, "kept_referenced": 0,
                    "kept_young": 0, "refused": f"{key} could not be read ({exc}); "
                    "a manifest the sweep cannot read is references it cannot see"}
    deleted = kept_referenced = kept_young = 0
    for key, created in store.list(blob_prefix):
        match = _BLOB_NAME.match(key[len(blob_prefix):])
        if match is None:
            continue
        if match.group(1) in referenced:
            kept_referenced += 1
            continue
        if created is None or now - created < grace:
            kept_young += 1
            continue
        store.delete(key)
        deleted += 1
    return {"manifests": manifests, "deleted": deleted, "kept_referenced": kept_referenced,
            "kept_young": kept_young, "refused": None}


# --- the CLI ------------------------------------------------------------------

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
    }
    with tempfile.TemporaryDirectory(prefix="repo-graph-self-test-") as scratch:
        store = LocalStore(scratch)
        first = write_graph(document, store, tenant_id="t", repo_id="r")
        second = write_graph(document, store, tenant_id="t", repo_id="r")
        swept = sweep(store, tenant_id="t", repo_id="r", grace=timedelta(0))
    if (first["manifest_digest"] != second["manifest_digest"] or second["blobs_written"]
            or swept["deleted"] or swept["refused"]):
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
    for command in (write, sweeper):
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
        if args.command == "sweep":
            report: dict = {"sweep": sweep(store, tenant_id=tenant, repo_id=args.repo_id)}
        else:
            raw = Path(args.graph).read_bytes()
            report = write_graph(json.loads(raw), store, tenant_id=tenant,
                                 repo_id=args.repo_id, max_commit_bytes=args.max_commit_bytes,
                                 graph_digest=digest_of(raw))
            if args.index:
                index_path = Path(args.index)
                index = json.loads(index_path.read_bytes())
                graph = index.get("graph") if isinstance(index.get("graph"), dict) else {}
                graph["manifest_digest"] = report["manifest_digest"]
                index["graph"] = graph
                index_path.write_bytes(canonical(index) + b"\n")
            if not args.no_sweep:
                report["sweep"] = sweep(store, tenant_id=tenant, repo_id=args.repo_id)
    except (ValueError, StoreError, OSError) as exc:
        print(f"{TOOL_NAME}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
