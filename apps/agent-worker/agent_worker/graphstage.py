"""Staging the repository's graph snapshot beside a step's checkout (lane CTXQ-S).

docs/design/context-quality.md §4.4 and §4.7, absorbing knowledge-graph.md's
lane KG5. Before a claude-code or codex agent starts, the worker copies the
tenant's promoted graph of the task's own repository into

    work/.swarm-context/snapshot/manifest.json
    work/.swarm-context/snapshot/blobs/<sha256>.jsonl.gz
    work/.swarm-context/snapshot/freshness.json
    work/.swarm-context/stage.json            what was decided, and why

and the runner names `swarm-graph` (`python -m agent_worker.graphmcp`) in the
agent's MCP configuration. `work/`, never the checkout: nothing here reaches
the harvested patch or the published branch (the `children.prepare` pattern).
CTXQ-P writes CONTEXT.md and pack.json into the same directory and reads
`stage.json` for the arm.

WHO DECIDES: A STORED SETTING, NEVER THE CALLER (§4.7, owner decision Q1,
2026-10-14). Invariant 10 holds because nothing in the task's input or
metadata is read here. The decision is, in order:

  1. the registration's `context` override: `on` or `off`; absent, or
     `inherit`, defers to
  2. the workspace's `tenant_settings/<tenant>.code_intelligence.context`:
     `on` or `off`; absent defers to
  3. `DEFAULT_CONTEXT`, `on`: local context never leaves the platform.

  Then `code_intelligence.context_ab` (default `DEFAULT_AB`) splits the
  staged steps for the online A/B (§6.2): with `ab`, `assign_arm` puts a task
  on arm A (staged nothing, today's runner) or arm B (staged) from
  sha256(task_id) mod 2, so every attempt of one task is on one arm and who
  submits it cannot bias the split. `on` or `off` runs no experiment.

  Both documents are swarm-api's to write (lane CTXQ-SET builds the routes);
  this module only reads them, and their field names are fixed HERE until it
  does: `SETTINGS_COLLECTION`, `CODE_INTELLIGENCE_FIELD`, `CONTEXT_FIELD`,
  `AB_FIELD`, and the registration's `REPOSITORY_CONTEXT_FIELD`.

WHAT IS STAGED, AND WHAT NEVER IS (knowledge-graph.md §5.2, §5.3):

  * only the registration the SIGNED spec names (`indexrun.target`: tenant
    and `repository_url`), and only when that registration is this
    tenant's; the objects are read from this tenant's own prefix with the
    step's own account (invariant 9);
  * only a promoted version with a graph, whose manifest matches the digest
    promotion recorded and whose blobs each match their names;
  * only a manifest of format `MIN_FORMAT` (3) or later, the format with the
    term index, communities and signatures the pack ranks with;
  * only an index commit PROVEN to be an ancestor of the step's base: the
    same commit, `git merge-base --is-ancestor` in the clone, or the compare
    swarm-api recorded for exactly this index and this base
    (`index.head_relation`, status `ahead`). A non-ancestor, or one whose
    ancestry cannot be shown, is never staged: a graph of a rewritten
    history describes code that is not there.

A STAGING FAILURE NEVER FAILS THE ATTEMPT. Every way this can go wrong -- an
unreachable Firestore, a missing blob, a digest mismatch, a full disk -- is
a reason in `stage.json`, a log line and, when the setting asked for
context, one prompt line saying none was staged (`UNSTAGED_ENV`). The agent
then runs as it did before this lane. `prepare` returns `{}` rather than
raise, and the lifecycle wraps it again.

WHAT STAGING COSTS IS MEASURED (lane GS-MEASURE, docs/design/graph-service.md
§9.1 and §9.3). Before it, staging had no timer, its bytes lived only in the
pod-local `stage.json`, and its time was folded into the lifecycle's
`credentials` phase. `stage` now times the whole decision on a monotonic
clock, and `prepare` hands the lifecycle `ready_detail`'s numbers, which it
emits as the RUNNING start-up mark `graph_ready` beside `clone_timed`:
`{delivery, seconds, bytes, blobs, reason}`. A week of those readings on
`staged` is the baseline the shared graph service is judged against.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import indexrun

# --------------------------------------------------------------------------
# names the runners, CTXQ-P and the tests share
# --------------------------------------------------------------------------

#: `work/.swarm-context/`: the context pack's directory (§4.4).
CONTEXT_DIR_NAME = ".swarm-context"
#: The snapshot `agent_worker.graphmcp` reads, inside the context directory.
SNAPSHOT_DIR_NAME = "snapshot"
#: What staging decided, machine-readable: CTXQ-P copies `arm` into pack.json.
STAGE_RECORD_NAME = "stage.json"
STAGE_SCHEMA = "swarm.graph-stage/v1"

#: The context directory, set only when a snapshot was staged.
CONTEXT_ENV = "SWARM_CONTEXT"
#: The staged snapshot directory, set only when one was: the runner names
#: `swarm-graph` exactly when this is set.
SNAPSHOT_ENV = "SWARM_GRAPH_SNAPSHOT"
#: Why nothing was staged, set only when the stored setting asked for context
#: and staging could not deliver it: the runner tells the agent in one line.
UNSTAGED_ENV = "SWARM_GRAPH_UNSTAGED"

#: The MCP server's name in the agent's configuration (`graphmcp.server.SERVER_NAME`,
#: held equal by tests/unit/worker/test_graphstage.py).
SERVER_NAME = "swarm-graph"
#: Its module, started with this runner's own interpreter.
SERVER_MODULE = "agent_worker.graphmcp"

#: The RUNNING start-up mark the lifecycle emits with `ready_detail`'s numbers
#: (graph-service.md §9.3). A `cause` string the worker writes, not a member
#: of the frozen contract (§11).
READY_MARK = "graph_ready"
#: How the graph reached this attempt, in `stage.json` and the mark:
#: `staged`, a snapshot copied into `work/` (today's only delivery);
#: `service`, pinned and served by `swarm-graph` (lane GS-WORKER, §4.1);
#: `none`, nothing delivered, and `reason` says why.
DELIVERY_STAGED, DELIVERY_SERVICE, DELIVERY_NONE = "staged", "service", "none"

#: The runners whose MCP configuration can name the server: claude-code's
#: `--mcp-config` and codex's `-c mcp_servers.*` (§4.4). The mock, generic and
#: browser runners start no agent that reads one.
GRAPH_RUNNERS = ("agent_worker.runners.claude_code", "agent_worker.runners.codex")

# --------------------------------------------------------------------------
# the stored setting (§4.7)
# --------------------------------------------------------------------------

#: `tenant_settings/<tenant_id>`: swarm-api's own document, NOT the frozen
#: `swarm_common.models.Tenant` (§4.7). Written by CTXQ-SET.
SETTINGS_COLLECTION = "tenant_settings"
CODE_INTELLIGENCE_FIELD = "code_intelligence"
#: `code_intelligence.context`: `on` | `off`.
CONTEXT_FIELD = "context"
#: `code_intelligence.context_ab`: `ab` | `on` | `off` (§6.2's `context.ab`).
AB_FIELD = "context_ab"
#: The registration's override (`RepositoryPatch`, CTXQ-SET): `inherit` | `on` | `off`.
REPOSITORY_CONTEXT_FIELD = "context"

#: Local context is ON unless a workspace or a repository turns it off: it never
#: leaves the platform (owner decision Q1, 2026-10-14).
DEFAULT_CONTEXT = "on"
#: The A/B runs unless a workspace says otherwise. Owner decision Q9
#: (2026-10-14): local context becomes default-on for EVERY step only after
#: §6.3's thresholds pass on the online A/B, so until then a workspace that
#: set nothing is in the experiment, half its tasks on arm B. Flip this to
#: `on` when the thresholds pass; a workspace can set `on` sooner.
DEFAULT_AB = "ab"

ON, OFF, INHERIT, AB = "on", "off", "inherit", "ab"
ARM_A, ARM_B = "A", "B"

# --------------------------------------------------------------------------
# what is staged
# --------------------------------------------------------------------------

#: The manifest format with `index_shards` (terms, communities, signatures).
MIN_FORMAT = 3
#: Stored bytes staged at most. This repository's whole graph is about 2.2 MB
#: gzipped (knowledge-graph.md §5.1) on a 4 GiB `standard` disk; past this the
#: graph is a monorepo the step's disk should not pay for unasked.
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
#: Promoted versions read when the current one is not an ancestor of the base.
#: swarm-api keeps 20 (`swarm_api.repoindex.KEPT_VERSIONS`).
MAX_VERSIONS_READ = 21
#: `swarm_api.repoindex.MAX_CHANGED_SHOWN`: a recorded compare keeps at most
#: this many changed files, so a list that long may be cut and is not passed
#: on as the complete set (graphmcp then says the count is unknown).
RELATION_FILES_CAP = 50
#: One git call, at most. Staging is bounded; the agent is waiting.
GIT_TIMEOUT_SECONDS = 10.0

MANIFEST_SCHEMA = "swarm.repo-graph-manifest/v1"
BLOB_SUFFIX = ".jsonl.gz"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class StageRefused(Exception):
    """A reason not to stage, said in `stage.json`; never an attempt failure."""


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------


def assign_arm(task_id: str) -> str:
    """Arm A or B, from sha256(task_id) mod 2 (§6.2): reproducible per task."""
    digest = hashlib.sha256(str(task_id).encode("utf-8")).digest()
    return ARM_B if digest[-1] % 2 else ARM_A


def _word(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


@dataclass(frozen=True)
class Decision:
    """Whether to stage, from the stored setting and the arm."""

    stage: bool
    context: str          # on | off: what the setting resolved to
    source: str           # repository | workspace | default
    ab: str               # ab | on | off
    arm: str | None       # A | B under the experiment, else None
    reason: str


def decide(*, repository_override: Any, workspace_settings: Mapping[str, Any] | None,
           task_id: str) -> Decision:
    """The stored setting's answer for this task. Unknown values read as unset."""
    code_intel = (workspace_settings or {}).get(CODE_INTELLIGENCE_FIELD)
    code_intel = code_intel if isinstance(code_intel, Mapping) else {}
    override = _word(repository_override)
    workspace = _word(code_intel.get(CONTEXT_FIELD))
    if override in (ON, OFF):
        context, source = override, "repository"
    elif workspace in (ON, OFF):
        context, source = workspace, "workspace"
    else:
        context, source = DEFAULT_CONTEXT, "default"
    ab = _word(code_intel.get(AB_FIELD))
    ab = ab if ab in (AB, ON, OFF) else DEFAULT_AB
    if context == OFF:
        return Decision(False, context, source, ab, None,
                        f"graph context is off ({source} setting)")
    if ab != AB:
        return Decision(True, context, source, ab, None, f"graph context is on ({source})")
    arm = assign_arm(task_id)
    if arm == ARM_A:
        return Decision(False, context, source, ab, arm,
                        "A/B arm A: this task runs without graph context")
    return Decision(True, context, source, ab, arm, "A/B arm B: graph context staged")


# --------------------------------------------------------------------------
# ancestry and freshness
# --------------------------------------------------------------------------


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.quotepath=off", "-C", str(checkout),
             *args],
            capture_output=True, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def is_ancestor(checkout: Path | None, ancestor: str, descendant: str) -> bool | None:
    """True or False from the clone, or None when the clone cannot say (a
    shallow clone without the index commit, which is every depth-1 lane)."""
    if ancestor == descendant:
        return True
    if checkout is None:
        return None
    present = _git(checkout, "cat-file", "-e", f"{ancestor}^{{commit}}")
    if present is None or present.returncode != 0:
        return None
    done = _git(checkout, "merge-base", "--is-ancestor", ancestor, descendant)
    if done is None or done.returncode not in (0, 1):
        return None
    return done.returncode == 0


@dataclass
class Ancestry:
    basis: str                       # same | git | recorded
    behind_by: int | None = None
    changed: list[str] | None = None


def prove_ancestry(checkout: Path | None, index_sha: str, base_sha: str,
                   index_state: Mapping[str, Any]) -> Ancestry | str:
    """How `index_sha` is known to be an ancestor of `base_sha`, or why it is not."""
    if index_sha == base_sha:
        return Ancestry(basis="same", behind_by=0, changed=[])
    local = is_ancestor(checkout, index_sha, base_sha)
    if local is False:
        return f"the index commit {index_sha[:12]} is not an ancestor of the base " \
               f"{base_sha[:12]}"
    if local is True and checkout is not None:
        count = _git(checkout, "rev-list", "--count", f"{index_sha}..{base_sha}")
        diff = _git(checkout, "diff", "--name-only", "-z", "--no-renames", index_sha, base_sha)
        behind = count.stdout.decode().strip() if count and count.returncode == 0 else ""
        changed = ([p for p in diff.stdout.decode("utf-8", "replace").split("\0") if p]
                   if diff and diff.returncode == 0 else None)
        return Ancestry(basis="git", behind_by=int(behind) if behind.isdigit() else None,
                        changed=changed)
    relation = index_state.get("head_relation")
    relation = relation if isinstance(relation, Mapping) else {}
    if relation.get("base") == index_sha and relation.get("head") == base_sha:
        if relation.get("status") in ("ahead", "identical"):
            files = relation.get("files")
            files = [str(f) for f in files] if isinstance(files, list) else None
            ahead = relation.get("ahead_by")
            return Ancestry(
                basis="recorded",
                behind_by=ahead if isinstance(ahead, int) and not isinstance(ahead, bool)
                else None,
                changed=files if files is not None and len(files) < RELATION_FILES_CAP
                else None)
        return f"the index commit {index_sha[:12]} is not an ancestor of the base " \
               f"{base_sha[:12]} (recorded compare: {relation.get('status')})"
    return f"the ancestry of the index commit {index_sha[:12]} and the base " \
           f"{base_sha[:12]} cannot be shown from this clone or the recorded compare"


# --------------------------------------------------------------------------
# reading and staging
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Version:
    sha: str
    graph_digest: str
    built_at: str


def _version_of(data: Mapping[str, Any] | None, sha: str) -> Version | None:
    if not isinstance(data, Mapping):
        return None
    digest = data.get("graph_digest")
    if not isinstance(digest, str) or not _DIGEST.match(digest) or not _SHA.match(sha):
        return None
    return Version(sha=sha, graph_digest=digest, built_at=str(data.get("built_at") or ""))


def referenced_blobs(manifest: Mapping[str, Any]) -> set[str]:
    """Every blob a manifest names (`repo_graph_shards.referenced_blobs`'s rule).

    Raises StageRefused on a malformed table rather than staging part of it.
    """
    found: set[str] = set()
    tables = [manifest.get("shards"), manifest.get("index_shards") or {}]
    for table in tables:
        if not isinstance(table, Mapping):
            raise StageRefused("the manifest's shard table is not an object")
        for layer in table.values():
            if not isinstance(layer, Mapping):
                raise StageRefused("a manifest layer is not an object")
            for entry in layer.values():
                match = _DIGEST.match(str((entry or {}).get("blob") or "")
                                      if isinstance(entry, Mapping) else "")
                if match is None:
                    raise StageRefused("a manifest shard names no sha256 blob")
                found.add(match.group(1))
    return found


def manifest_format(manifest: Mapping[str, Any]) -> int:
    """`repo_graph_shards.manifest_format`: 2 when the manifest does not say."""
    value = manifest.get("format_version")
    return value if isinstance(value, int) and not isinstance(value, bool) else 2


def write_snapshot(store: Any, *, where: indexrun.Target, version: Version,
                   destination: Path, ancestry: Ancestry, base_sha: str) -> dict[str, Any]:
    """Stage one version's manifest and blobs at `destination`, checked; return its facts.

    Built in a sibling temporary directory and renamed into place, so a
    half-staged snapshot is never what the server opens.
    """
    root = f"{where.destination}"
    manifest_bytes = store.download_bytes(f"{root}/{version.sha}/manifest.json")
    digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if digest != version.graph_digest:
        raise StageRefused(f"the manifest of {version.sha[:12]} no longer matches the digest "
                           "promotion recorded")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError:
        raise StageRefused("the manifest is not JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise StageRefused(f"the manifest is not a {MANIFEST_SCHEMA} document")
    if manifest.get("commit_sha") != version.sha:
        raise StageRefused("the manifest names another commit than its version")
    fmt = manifest_format(manifest)
    if fmt < MIN_FORMAT:
        raise StageRefused(f"the graph is format {fmt}, older than format {MIN_FORMAT}; the "
                           "next full index run brings it up to date")
    blobs = sorted(referenced_blobs(manifest))
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
    try:
        (scratch / "blobs").mkdir()
        total = len(manifest_bytes)
        for hexdigest in blobs:
            data = store.download_bytes(f"{root}/blobs/{hexdigest}{BLOB_SUFFIX}")
            if hashlib.sha256(data).hexdigest() != hexdigest:
                raise StageRefused(f"blob {hexdigest[:12]} does not match its name")
            total += len(data)
            if total > MAX_SNAPSHOT_BYTES:
                raise StageRefused(f"the graph is over {MAX_SNAPSHOT_BYTES} stored bytes")
            (scratch / "blobs" / f"{hexdigest}{BLOB_SUFFIX}").write_bytes(data)
        (scratch / "manifest.json").write_bytes(manifest_bytes)
        record: dict[str, Any] = {
            "index_sha": version.sha, "base_sha": base_sha,
            "behind_by": ancestry.behind_by, "manifest_digest": digest,
            "basis": ancestry.basis,
        }
        if ancestry.changed is not None:
            record["changed"] = sorted(ancestry.changed)
        (scratch / "freshness.json").write_text(json.dumps(record, indent=2) + "\n",
                                                encoding="utf-8")
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        os.replace(scratch, destination)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return {"index_sha": version.sha, "format_version": fmt, "blobs": len(blobs),
            "bytes": total, "behind_by": ancestry.behind_by, "basis": ancestry.basis,
            "changed": len(ancestry.changed) if ancestry.changed is not None else None}


def _candidates(registration: Any, index_state: Mapping[str, Any],
                options: Mapping[str, Any]) -> list[Version]:
    """The promoted current version first, then the other kept ones, newest first."""
    versions = registration.collection(indexrun.VERSIONS_COLLECTION)
    out: list[Version] = []
    current = index_state.get("current_sha")
    if isinstance(current, str) and _SHA.match(current):
        snap = versions.document(current).get(**options)
        found = _version_of(snap.to_dict() if snap.exists else None, current)
        if found is not None:
            out.append(found)
    others: list[Version] = []
    for row in versions.limit(MAX_VERSIONS_READ).stream(**options):
        data = row.to_dict() or {}
        found = _version_of(data, str(data.get("commit_sha") or ""))
        if found is not None and found.sha != current:
            others.append(found)
    others.sort(key=lambda v: v.built_at, reverse=True)
    return out + others


@dataclass
class StageResult:
    staged: bool
    reason: str
    decision: Decision | None = None
    snapshot: Path | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    #: The decision's wall time on the monotonic clock: the Firestore reads,
    #: the ancestry git calls and every download (`stage` sets it).
    seconds: float | None = None

    @property
    def delivery(self) -> str:
        return DELIVERY_STAGED if self.staged else DELIVERY_NONE

    @property
    def wanted(self) -> bool:
        """The setting asked for context on this task (so its absence is said)."""
        return self.decision is not None and self.decision.stage

    def record(self) -> dict[str, Any]:
        return {
            "schema": STAGE_SCHEMA,
            "staged": self.staged,
            "reason": self.reason,
            "decision": asdict(self.decision) if self.decision is not None else None,
            "snapshot": str(self.snapshot) if self.snapshot is not None else None,
            "delivery": self.delivery,
            "seconds": self.seconds,
            **self.facts,
        }


def ready_detail(result: StageResult) -> dict[str, Any]:
    """The `graph_ready` mark's detail: numbers and the reason, nothing else.

    No path, URL, commit or digest: the API serves an event's detail as
    stored. `bytes` and `blobs` are None when nothing was delivered, never a
    zero that would read as an empty graph staged.
    """
    return {"graph": {
        "delivery": result.delivery,
        "seconds": result.seconds,
        "bytes": result.facts.get("bytes") if result.staged else None,
        "blobs": result.facts.get("blobs") if result.staged else None,
        "reason": result.reason,
    }}


def stage(*, db: Any, store: Any, tenant_id: str, repository_url: str | None, task_id: str,
          checkout: Path | None, base_sha: str | None, context_dir: Path,
          call_options: Mapping[str, Any] | None = None,
          clock: Callable[[], float] = time.monotonic) -> StageResult:
    """Decide and stage, timed on `clock`. Never raises: every failure is a reason."""
    started = clock()
    try:
        result = _stage(db=db, store=store, tenant_id=tenant_id,
                        repository_url=repository_url, task_id=task_id, checkout=checkout,
                        base_sha=base_sha, context_dir=context_dir,
                        options=dict(call_options or {}))
    except StageRefused as refused:
        result = StageResult(False, str(refused))
    except Exception as exc:  # noqa: BLE001 -- a staging failure never fails the attempt
        result = StageResult(False, f"staging failed ({type(exc).__name__})")
    result.seconds = round(clock() - started, 3)
    return result


def _stage(*, db: Any, store: Any, tenant_id: str, repository_url: str | None, task_id: str,
           checkout: Path | None, base_sha: str | None, context_dir: Path,
           options: dict[str, Any]) -> StageResult:
    where = indexrun.target(tenant_id, repository_url)
    if isinstance(where, str):
        return StageResult(False, where)
    registration = db.collection(indexrun.REPOSITORIES_COLLECTION).document(where.repo_id)
    snap = registration.get(**options)
    data = (snap.to_dict() or {}) if snap.exists else None
    if data is None:
        return StageResult(False, "the repository is not registered in this workspace")
    if data.get("tenant_id") != tenant_id:
        return StageResult(False, f"the registration {where.repo_id} is not this tenant's")
    settings_snap = db.collection(SETTINGS_COLLECTION).document(tenant_id).get(**options)
    settings = (settings_snap.to_dict() or {}) if settings_snap.exists else {}
    decision = decide(repository_override=data.get(REPOSITORY_CONTEXT_FIELD),
                      workspace_settings=settings, task_id=task_id)
    if not decision.stage:
        return StageResult(False, decision.reason, decision)
    result = StageResult(False, "", decision)
    try:
        if not base_sha or not _SHA.match(base_sha):
            raise StageRefused("the step has no base commit to compare the index with")
        index_state = data.get("index") if isinstance(data.get("index"), Mapping) else {}
        candidates = _candidates(registration, index_state, options)
        if not candidates:
            raise StageRefused("no promoted index with a graph is kept for this repository")
        reasons: list[str] = []
        for version in candidates:
            ancestry = prove_ancestry(checkout, version.sha, base_sha,
                                      index_state if version.sha == index_state.get(
                                          "current_sha") else {})
            if isinstance(ancestry, str):
                reasons.append(ancestry)
                continue
            destination = context_dir / SNAPSHOT_DIR_NAME
            result.facts = write_snapshot(store, where=where, version=version,
                                          destination=destination, ancestry=ancestry,
                                          base_sha=base_sha)
            result.staged, result.snapshot = True, destination
            result.reason = f"staged the graph of {version.sha[:12]} ({ancestry.basis})"
            return result
        raise StageRefused(reasons[0])
    except StageRefused as refused:
        result.reason = str(refused)
    except Exception as exc:  # noqa: BLE001 -- see `stage`
        result.reason = f"staging failed ({type(exc).__name__})"
    return result


# --------------------------------------------------------------------------
# the lifecycle's one call, and what the runners read
# --------------------------------------------------------------------------


def offers_graph(runner_argv: Sequence[str]) -> bool:
    """Whether this profile's runner can name `swarm-graph` to its agent."""
    return any(part in GRAPH_RUNNERS for part in runner_argv)


def prepare(work: Path, *, profile_name: str, runner_argv: Sequence[str], db: Any,
            store: Any, tenant_id: str, repository_url: str | None, task_id: str,
            checkout: Path | None, base_sha: str | None,
            call_options: Mapping[str, Any] | None = None,
            log: Any = None,
            mark: Callable[[dict[str, Any]], None] | None = None) -> dict[str, str]:
    """Stage for this attempt and return the runner's variables. Never raises.

    `{}` for an index run (it builds the graph; it does not read one), a
    runner that names no MCP server, or a step with no checkout.

    `mark` receives `ready_detail(result)` once, when the stored setting
    asked for context on this step (`StageResult.wanted`): the steps where a
    graph was to be delivered, staged or not, which is the denominator of
    §8.2's fallback rate. Arm A and `off` deliver nothing by decision and are
    not readings. A mark that fails costs only the mark.
    """
    try:
        if profile_name == indexrun.INDEXER_PROFILE or not offers_graph(runner_argv) \
                or checkout is None or not repository_url:
            return {}
        context_dir = work / CONTEXT_DIR_NAME
        if context_dir.is_symlink() or context_dir.is_file():
            context_dir.unlink()
        context_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = stage(db=db, store=store, tenant_id=tenant_id, repository_url=repository_url,
                       task_id=task_id, checkout=checkout, base_sha=base_sha,
                       context_dir=context_dir, call_options=call_options)
        if not result.staged:
            stale = context_dir / SNAPSHOT_DIR_NAME
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
            elif stale.exists():
                # A restored checkpoint's snapshot from an earlier attempt is
                # not this attempt's decision; never left for the server.
                shutil.rmtree(stale, ignore_errors=True)
        (context_dir / STAGE_RECORD_NAME).write_text(
            json.dumps(result.record(), indent=2, default=str) + "\n", encoding="utf-8")
        if log is not None:
            log.info("graph context", staged=result.staged, reason=result.reason,
                     arm=result.decision.arm if result.decision else None,
                     delivery=result.delivery, seconds=result.seconds,
                     bytes=result.facts.get("bytes"), blobs=result.facts.get("blobs"))
        if mark is not None and result.wanted:
            try:
                mark(ready_detail(result))
            except Exception as exc:  # noqa: BLE001 -- a measurement is never worth the graph
                if log is not None:
                    log.warning("graph_ready not marked", error=type(exc).__name__)
        # Jev for J1-J5 (lane CTXQ-JW): decided by the workspace's own stored
        # opt-in, written as `<attempt>/judge/config.json` or not at all.
        # Here, not in the lifecycle, because this is its one setup call.
        from .judge import attempt as judge_attempt

        jev = judge_attempt.setup(work=work, db=db, tenant_id=tenant_id,
                                  repository_url=repository_url, call_options=call_options,
                                  log=log)
        if result.staged and result.snapshot is not None:
            return {CONTEXT_ENV: str(context_dir), SNAPSHOT_ENV: str(result.snapshot), **jev}
        if result.wanted:
            return {UNSTAGED_ENV: result.reason, **jev}
        return dict(jev)
    except Exception as exc:  # noqa: BLE001 -- a staging failure never fails the attempt
        if log is not None:
            log.warning("graph context not staged", error=type(exc).__name__)
        return {}


def server_argv(snapshot: str, workdir: str | None) -> list[str]:
    """The command a CLI runs as `swarm-graph`: this interpreter, `-I` so a module
    the agent wrote in the checkout is never what runs (`children.git_shim_argv`)."""
    argv = [sys.executable, "-I", "-m", SERVER_MODULE, "--snapshot", snapshot]
    if workdir:
        argv += ["--workdir", workdir]
    return argv


#: The prompt line when the setting asked for context and none could be staged.
UNSTAGED_NOTE = ("No repository graph was staged for this step ({reason}); the swarm-graph "
                 "tools are not available, so read callers and tests from the code.")


def unstaged_note(environ: Mapping[str, str]) -> str | None:
    """One prompt line saying why no graph was staged, or None."""
    reason = (environ.get(UNSTAGED_ENV) or "").strip()
    if not reason:
        return None
    return UNSTAGED_NOTE.format(reason=reason.rstrip(".")[:300])


__all__ = [
    "AB_FIELD", "ARM_A", "ARM_B", "CODE_INTELLIGENCE_FIELD", "CONTEXT_DIR_NAME",
    "CONTEXT_ENV", "CONTEXT_FIELD", "DEFAULT_AB", "DEFAULT_CONTEXT", "DELIVERY_NONE",
    "DELIVERY_SERVICE", "DELIVERY_STAGED", "Decision", "GRAPH_RUNNERS", "MIN_FORMAT",
    "READY_MARK", "REPOSITORY_CONTEXT_FIELD", "SERVER_NAME",
    "SETTINGS_COLLECTION", "SNAPSHOT_DIR_NAME", "SNAPSHOT_ENV", "STAGE_RECORD_NAME",
    "StageResult", "UNSTAGED_ENV", "assign_arm", "decide", "is_ancestor", "offers_graph",
    "prepare", "prove_ancestry", "ready_detail", "server_argv", "stage", "unstaged_note", "write_snapshot",
]
