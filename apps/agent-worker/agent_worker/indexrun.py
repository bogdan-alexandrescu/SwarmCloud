"""The deterministic passes of an index run, as the worker's own steps (docs/repo-index.md §3.6).

An index run is an ordinary task on the `indexer` profile (claude-code on
agent-runtime-indexer, contract request 48). Until 2026-10-06 its prompt told
the AGENT to run the extractor (`swarm-repo-index`) first and the shard writer
(`swarm-repo-graph write`) last, through its Bash tool. Measured on
task_209ba9e0c9c948e284e9 (repo_4c5105947752b3f3, 2026-10-06 15:08-15:32):
the extractor took about 6 minutes, the agent's write ran into Claude Code's
10-minute command limit and was killed after 150 blobs, and the retry failed
with a 412 on the blobs the killed run had left. Neither pass reads anything
a model has to read: they are commands with fixed arguments. So the worker
runs them, in this order, around the agent:

    extract        swarm-repo-index --repo <checkout> --out <work>/repo-index.extract.json
                                    --graph-out <work>/repo-graph.json
    agent          the runner, from the extractor's output
    graph_write    swarm-repo-graph write --graph <work>/repo-graph.json --index
                                    <artifacts>/repo-index.json ...

Each with its own timeout (`budgets`), sized to the task's own timeout -- 30
minutes for a full run (`swarm_api.repoindex.FULL_TIMEOUT_SECONDS`) -- and
each phase's duration is recorded (`PhaseRecord`), in the step's
`result_summary` and in `work/repo-index.phases.json`, where the agent can
read why the extractor did not run when it did not.

WHAT A PHASE THAT FAILS DOES. Neither fails the run: an extractor that is not
installed, exits non-zero or times out leaves the agent to compute the
mechanical fields itself, exactly as the prompt has always said; a graph
write that fails leaves the index without `graph.manifest_digest`, which
promotion reads as "no graph" (§2.5). Both say why in their record. A run is
a less certain index, never a lost one (§3.5).

WHERE THE GRAPH GOES (invariant 9). Under `tenants/<tenant>/repos/<repo_id>/
graph`, with the tenant and the bucket this worker's own configuration
(`TENANT_ID`, `ARTIFACT_BUCKET`, set by dispatch), and the repo_id derived
here from SIGNED fields only: the spec's tenant and `repository_url`, by the
registration's own recipe (`swarm_api.repositories.repo_id_for`, held equal
by tests/unit/worker/test_index_run_phases.py). The task's metadata names
the repo_id too, but the spec signature does not cover it
(`swarm_common.specsign.SIGNED_METADATA_KEYS`), so the worker never reads
it: a rewritten metadata cannot point the write at another registration.

WHERE THE INDEX GOES, AND WHAT IS KEPT (§2, lane IX3, owner decision
2026-10-06). The agent's repo-index.json is uploaded as the task's artifact,
under `tasks/`, which the bucket's lifecycle cold-stores and expires. The
worker then copies the uploaded object, byte for byte, to
`tenants/<tenant>/repos/<repo_id>/index/<commit_sha>/repo-index.json`
(`Target.index_key`; the commit is the signed `repository_ref`), which no
lifecycle rule matches; promotion records that key in the version once its
bytes equal the artifact's. Before the graph write it reads the commits
whose versions swarm-api still holds (`kept_commits`: the last 20, plus
the promoted one and its own) and passes them as `--keep-commit`, so the
writer's sweep deletes every other commit's manifest and index copy and
then the blobs no kept manifest names. swarm-api cannot do either: it
reads the bucket and may not write or delete.

AN INCREMENTAL RUN'S BASE, BY REFERENCE (§3.4, lane IX2). swarm-api decides
incremental or full (`swarm_api.repoindex.choose_kind`) and names the base
in the task's PROMPT, as one line of its own (`BASE_LINE`). The prompt is
inside the signed `input`; the task's `metadata.index_kind` and `base_sha`
are not (`swarm_common.specsign.SIGNED_METADATA_KEYS`, frozen), so the
worker never reads them -- tests/unit/common/test_specsign_covers.py holds
it to that. Before the extractor the worker stages the base, as a fourth
phase, `stage_base`:

    the version     repositories/<repo_id>/index_versions/<base_sha>, under the
                    repo_id derived above from signed fields only
    the index       its copy under repos/ (above), or, for a version
                    promoted before the copy existed, that version's task's
                    repo-index.json, resolved and fetched
                    by the staged-input path every workflow input takes
                    (`inputs.fetch_upstream_task`, `inputs.artifact_reference`):
                    the successful attempt's manifest, a key inside this
                    tenant's own prefix, checked against the digest the
                    version recorded at promotion
    the graph       `swarm-repo-graph read` of the base commit's shards, its
                    manifest checked against the digest the version recorded

No workspace copy and no new grant: the step's own account already reads its
tenant's artifacts and graph prefix (invariant 9), and the object keys are
built from this worker's tenant, never from a document. The base line is
still a REQUEST, and everything staged for it is checked: the version is
read under the registration the signed spec names, and both documents
against the digests promotion recorded, so whatever names the base can only
make the run full or pick another promoted version of the same
registration, whose content the digests vouch for. Whatever cannot be staged makes
the run full, and the phase record says why; it never fails the run. The
extractor then decides for itself, from the files it can compare
(`repo_index_extract.incremental_changes`), and runs full when it must.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

#: The profile swarm-api submits index runs as (`swarm_api.repoindex.INDEXER_PROFILE`).
INDEXER_PROFILE = "indexer"

#: The image's two tools (images/agent-runtime-indexer/Dockerfile).
EXTRACTOR_COMMAND = "swarm-repo-index"
GRAPH_WRITER_COMMAND = "swarm-repo-graph"

#: In `work/`, beside the checkout and not in it, so neither reaches the
#: harvested patch, and not in the artifacts directory: §2.2 keeps the graph
#: out of the artifact. The prompt names the same paths
#: (`swarm_api.repoindex.EXTRACT_FILE`, `GRAPH_FILE`).
EXTRACT_FILE = "repo-index.extract.json"
GRAPH_FILE = "repo-graph.json"
#: What the agent reads when the extractor did not run, and why.
PHASES_FILE = "repo-index.phases.json"
#: The document promotion validates: the agent writes it, the graph write
#: sets its `graph.manifest_digest` (`swarm_api.repoindex.INDEX_FILE`).
INDEX_FILE = "repo-index.json"
#: An incremental run's base, staged in `work/` beside the extractor's
#: output (`swarm_api.repoindex.BASE_INDEX_FILE` names the first in the prompt).
BASE_INDEX_FILE = "repo-index.base.json"
BASE_GRAPH_FILE = "repo-graph.base.json"

#: Where swarm-api keeps a registration and its promoted versions
#: (`swarm_api.repositories.COLLECTION`, `swarm_api.repoindex.VERSIONS_COLLECTION`).
REPOSITORIES_COLLECTION = "repositories"
VERSIONS_COLLECTION = "index_versions"
#: The prompt line that names an incremental run's base
#: (`swarm_api.repoindex.BASE_LINE`): a line of its own, the prefix and a
#: 40-hex sha. A request, checked; see the module doc.
BASE_LINE = "swarm-index-base: "
#: A base index larger than the index's own bound (§2.2) is not one swarm-api promoted.
MAX_BASE_INDEX_BYTES = 512 * 1024

#: The share of the task's timeout the extractor may take. 0.4 of a full
#: run's 1,800 s is 720 s: twice the ~6 minutes measured on a 1,685-file
#: repository on 2026-10-06, and still leaves the agent most of the rest.
EXTRACT_SHARE = 0.4
EXTRACT_MIN_SECONDS = 120
#: The LSP pass is given the extractor's budget less this, so the extractor
#: stops its servers and writes what it has (every language it could not
#: finish marked `timed_out`, §3.5) before the worker would have to kill it
#: and lose the tree-sitter half too.
EXTRACT_LSP_RESERVE_SECONDS = 180
#: The graph write is reserved out of the task's timeout BEFORE the agent
#: starts, so an agent that uses all its time still leaves the write its
#: own: 0.15 of 1,800 s is 270 s. One batched upload of a full graph (~370
#: blobs, 1.3 MB) takes well under a minute; the serial upload it replaced
#: took ~3.9 s a blob, ~24 minutes, and could never fit.
GRAPH_WRITE_SHARE = 0.15
GRAPH_WRITE_MIN_SECONDS = 120
#: READING THE BASE GRAPH BACK (`swarm-repo-graph read`, #1469) is sized
#: from the graph's size, not a share of the extractor's budget. The flat
#: quarter it had, 90 s of an incremental run's 360, was measured running
#: past on a 63 MB base (#1334, task_6bf9dc7cc95544f59f0f, 2026-10-16).
#: Measured for #1469 on this repository's own graph (d2b2d14, 98 MB of
#: JSON): the read fetches 744 blobs, about 7.6 a MB, and decodes and writes
#: them back in ~4 s of CPU, 0.04 s a MB. Its cost is the download, per
#: object: ~480 blobs of the 63 MB base in two serial gcloud calls ran past
#: 90 s, at least 1.4 s a MB. The download now runs four calls at once
#: (`repo_graph_shards.GET_PROCESSES`), but the budget does not count on it:
#:
#:   FIXED      the tool's start, the manifest's `gcloud storage cat` and the
#:              download calls' own start, ~4 s each
#:   PER_MB     1.5 s a MB, the serial rate measured in production, so the
#:              budget holds even if running the calls side by side gains
#:              nothing; it is ~3x what four at once should take
#:
#: 20 + 1.5 x 63 is 114 s for the measured base. Never more than half the
#: extractor's budget (180 s of an incremental run's 360): a base that costs
#: more to read than half a full extraction is not worth building on, and a
#: read that runs out only makes the run full.
BASE_READ_FIXED_SECONDS = 20
BASE_READ_SECONDS_PER_MB = 1.5
#: The base's size, estimated from what its promoted repo-index.json says of
#: its graph (`graph.symbols` and `graph.edges`, swarm_api.repoindex
#: `GraphSummary`): 562 bytes of graph a symbol-or-edge on this repository's
#: graph (98,248,874 bytes, 54,542 symbols, 120,131 edges), rounded up, so
#: the estimate does not under-state it. An index that gives no counts gives
#: no size, and the read gets its ceiling.
BASE_GRAPH_BYTES_PER_ROW = 600

#: Environment names a phase inherits from the worker. Nothing else: the
#: worker's own environment carries control-plane identifiers and the spec
#: keys, and a phase needs none of them (`workspace.Workspace.child_env`
#: draws the same line for the agent).
PASS_THROUGH_ENV = ("PATH", "LANG", "LC_ALL", "TZ")

_GITHUB_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)
_REPO_ID_PREFIX = "repo_"
_FORGE_HOST = "github.com"


def is_index_run(runner_profile: str) -> bool:
    return runner_profile == INDEXER_PROFILE


#: How much history an index run's clone holds: the extractor's window
#: (`repo_index_extract.HISTORY_DAYS`, held equal by
#: tests/unit/worker/test_indexrun_clone_history.py). Measured 2026-10-07 on
#: the live index: cloned one commit deep, every hot spot read `changes: 1`
#: and co-change was empty (G4-06). Bounded by date, so the fetch costs the
#: window's commits and no older ones (`gitops.history_fetch_argv`).
HISTORY_DAYS = 90


def clone_history_days(runner_profile: str) -> int | None:
    """The days of history the task's clone fetches: the window for an index run, else none."""
    return HISTORY_DAYS if is_index_run(runner_profile) else None


#: The checkout's directory in `work/` (`workspace.REPO_DIR_NAME`), which an
#: index run's checkpoints leave out (`checkpoint_excludes`).
CHECKOUT_DIR = "repo"


def checkpoint_excludes(runner_profile: str) -> frozenset[str]:
    """The `work/`-relative directories the task's checkpoints leave out.

    AN INDEX RUN'S CHECKPOINT CARRIES THE GRAPH, NOT THE CHECKOUT (owner
    decision 2026-10-17). Measured 2026-10-16 on task_6bf9dc7cc95544f59f0f:
    its periodic ckpt-00005 was 417,940,037 bytes and ~36 s to upload, of
    which `repo/` was about 290 MB compressed (`.git/objects` 259.7 MB,
    `docs/design` 134.6 MB, ... before compression). The indexer only reads
    the checkout, and it is reproducible from the manifest's `clone_base`: a
    resumed attempt clones it again there (`resume_clone_ref`). Everything
    else in `work/` -- the graph files, the phases file, the agent's own
    progress -- is still archived, at every checkpoint (invariant 8).

    Every other profile leaves nothing out here: for claude-code and the
    rest the checkout IS the work.
    """
    return frozenset({CHECKOUT_DIR}) if is_index_run(runner_profile) else frozenset()


def resume_clone_ref(runner_profile: str, clone_base: str | None) -> str | None:
    """The commit a resumed index run clones again, or None to clone as a first attempt does.

    The restored checkpoint's `clone_base`, from the manifest the worker
    wrote (never the marker in `work/`, which the agent can write), when it
    is a full sha and the run is an index run: the commit the first attempt
    cloned, which is what its graph describes, wherever `repository_ref` has
    moved since. Anything else -- no checkpoint, a manifest with no base, an
    empty repository's marker -- clones exactly as a first attempt does.
    """
    if not is_index_run(runner_profile) or not isinstance(clone_base, str):
        return None
    return clone_base if is_commit_sha(clone_base) else None


@dataclass(frozen=True)
class Budgets:
    """Seconds each phase may take, out of the task's own timeout."""

    extract: int
    lsp_total: int
    graph_write: int
    base_read: int


def budgets(timeout_seconds: int, *, base_graph_bytes: int | None = None) -> Budgets:
    """Each deterministic phase's timeout, sized to the task's timeout.

    1,800 s (a full run) gives 720 s to extract, 540 s of it to the LSP
    pass, and 270 s reserved for the graph write: the agent has at least the
    810 s between. Never more than the task has. The base read is sized from
    `base_graph_bytes` (`base_graph_bytes()`), up to half the extractor's
    budget, which is also what an unknown size gets.
    """
    total = max(1, int(timeout_seconds))
    extract = min(total, max(EXTRACT_MIN_SECONDS, int(total * EXTRACT_SHARE)))
    write = min(total, max(GRAPH_WRITE_MIN_SECONDS, int(total * GRAPH_WRITE_SHARE)))
    lsp = max(30, extract - EXTRACT_LSP_RESERVE_SECONDS)
    ceiling = extract // 2
    if base_graph_bytes is None:
        base_read = ceiling
    else:
        megabytes = max(0, base_graph_bytes) / (1024 * 1024)
        base_read = min(ceiling, int(BASE_READ_FIXED_SECONDS
                                     + BASE_READ_SECONDS_PER_MB * megabytes))
    return Budgets(extract=extract, lsp_total=lsp, graph_write=write, base_read=base_read)


def base_graph_bytes(index: Any) -> int | None:
    """The base graph's size in bytes, estimated from its promoted index's
    `graph.symbols` and `graph.edges`; None when the index gives no counts."""
    graph = index.get("graph") if isinstance(index, Mapping) else None
    if not isinstance(graph, Mapping):
        return None
    counts = [graph.get(key) for key in ("symbols", "edges")]
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in counts):
        return None
    rows = sum(counts)
    return rows * BASE_GRAPH_BYTES_PER_ROW if rows else None


def repo_id_for(tenant_id: str, owner: str, repo: str) -> str:
    """The registration's id, by `swarm_api.repositories.repo_id_for`'s recipe."""
    key = f"{tenant_id}{_FORGE_HOST}/{f'{owner}/{repo}'.lower()}"
    return _REPO_ID_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Target:
    """Where one index run's graph goes, from signed fields only."""

    tenant_id: str
    repo_id: str

    @property
    def destination(self) -> str:
        return f"tenants/{self.tenant_id}/repos/{self.repo_id}/graph"

    def index_key(self, commit_sha: str) -> str:
        """The index's own home, beside the graph and outside every artifact
        lifecycle rule (§2): `tenants/<t>/repos/<repo_id>/index/<sha>/repo-index.json`.
        The same key as `swarm_api.repoindex.index_key` and the sweep's
        `repo_graph_shards.index_key`."""
        if not _SHA.match(commit_sha or ""):
            raise ValueError("the commit is not a 40-hex sha")
        return f"tenants/{self.tenant_id}/repos/{self.repo_id}/index/{commit_sha}/{INDEX_FILE}"


def target(tenant_id: str, repository_url: str | None) -> Target | str:
    """The graph's target, or why there is none (a reason, never an exception)."""
    match = _GITHUB_URL.match(repository_url or "")
    if match is None:
        return "the task's repository_url is not a github.com repository"
    return Target(tenant_id=tenant_id, repo_id=repo_id_for(tenant_id, match.group(1),
                                                           match.group(2)))


def resolve(command: str, env: Mapping[str, str]) -> str | None:
    """The command's absolute path on the phase's PATH, or None when the image lacks it."""
    return shutil.which(command, path=env.get("PATH") or os.defpath)


def phase_env(*, home: Path, tmp: Path, tenant_id: str, bucket: str) -> dict[str, str]:
    """The environment a phase runs with: an allowlist, plus the step's own target.

    `TENANT_ID` and `ARTIFACT_BUCKET` are what `swarm-repo-graph` checks its
    target against (`repo_graph_shards.resolve_target`); they are this
    worker's own configuration, set by dispatch, and the same values PID 1
    holds. `gcloud` authenticates as the step's service account through the
    metadata server, so no credential passes through here.
    """
    env = {name: os.environ[name] for name in PASS_THROUGH_ENV if os.environ.get(name)}
    env.update({"HOME": str(home), "TMPDIR": str(tmp), "TENANT_ID": tenant_id,
                "ARTIFACT_BUCKET": bucket})
    return env


def extractor_argv(
    program: str, *, checkout: Path, work: Path, lsp_total: int, base_sha: str | None = None
) -> list[str]:
    argv = [
        program, "--repo", str(checkout), "--out", str(work / EXTRACT_FILE),
        "--graph-out", str(work / GRAPH_FILE),
        "--lsp-total-budget-seconds", str(lsp_total),
    ]
    if base_sha is not None:
        argv += ["--base-sha", base_sha, "--base-index", str(work / BASE_INDEX_FILE),
                 "--base-graph", str(work / BASE_GRAPH_FILE)]
    return argv


def graph_write_argv(
    program: str, *, work: Path, artifacts: Path, where: Target, bucket: str,
    base_sha: str | None = None, keep: Sequence[str] | None = None,
) -> list[str]:
    argv = [
        program, "write", "--graph", str(work / GRAPH_FILE),
        "--repo-id", where.repo_id, "--destination", where.destination,
        "--tenant", where.tenant_id, "--store", f"gs://{bucket}",
        "--index", str(artifacts / INDEX_FILE),
    ]
    if base_sha is not None:
        argv += ["--base-commit", base_sha]
    for commit in keep or ():
        argv += ["--keep-commit", commit]
    return argv


def graph_read_argv(
    program: str, *, work: Path, where: Target, bucket: str, base: "BaseVersion"
) -> list[str]:
    return [
        program, "read", "--commit", base.sha, "--manifest-digest", base.graph_digest,
        "--repo-id", where.repo_id, "--destination", where.destination,
        "--tenant", where.tenant_id, "--store", f"gs://{bucket}",
        "--out", str(work / BASE_GRAPH_FILE),
    ]


# --------------------------------------------------------------------------
# an incremental run's base (§3.4)
# --------------------------------------------------------------------------

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


_BASE_LINE = re.compile(r"^" + re.escape(BASE_LINE) + r"([0-9a-f]{40})$", re.MULTILINE)


def is_commit_sha(value: str) -> bool:
    return bool(_SHA.match(value))


def requested_base(task_input: Any) -> str | None:
    """The base swarm-api asked this run to build on, from the SIGNED prompt; None for full.

    Exactly one base line, or none: two that disagree are no request at all.
    """
    prompt = task_input.get("prompt") if isinstance(task_input, Mapping) else None
    if not isinstance(prompt, str):
        return None
    found = set(_BASE_LINE.findall(prompt))
    return found.pop() if len(found) == 1 else None


# --------------------------------------------------------------------------
# a switched tenant's reads: the state routes (lane ISO-WORKER-READS)
# --------------------------------------------------------------------------

#: One state-route call's retry budget, in seconds. It is the 30 s a direct
#: Firestore read retries for, both inside the startup budget
#: (`startup.FIRESTORE_STARTUP_RETRY_SECONDS`) and in the mid-run `tenant`
#: budget (`control.MID_RUN_BUDGETS`), so a switched tenant's read waits out
#: an outage exactly as long as a direct one does. The callers' Firestore
#: `call_options` mean nothing to a route; `RemoteControlPlane.call_options`
#: answers {} for that reason.
STATE_CALL_SECONDS = 30.0

#: The most index versions `state/repository` serves: swarm-api's kept 20
#: (`swarm_api.repoindex.KEPT_VERSIONS`) and one more, the read's own
#: `limit(KEPT_VERSIONS + 1)`. An answer this long may have been cut short,
#: so it is never read as the whole set. tests/unit/worker/test_state_reads.py
#: holds it to swarm-api's value.
ROUTE_VERSIONS_SERVED = 21


def state_routes(db: Any) -> Any | None:
    """`db` itself when it is this attempt's state client, else None.

    WHY (docs/design/tenant-settings-integrity.md §4, #1457). A tenant
    switched to the state routes (`SWARM_STATE_VIA_API`) builds no Firestore
    client: its worker reaches its documents only through swarm-api's
    `/v1/attempts/{id}/state/...` routes, which derive every document id from
    the attempt's tuple and its signed spec. The readers here and in
    `secrets`, `jevkey`, `inputs`, `graphstage`, `judge.attempt` take that
    attempt's `statecall.StateCall` where they took the Firestore client, and
    then make no Firestore call at all. Anything else in `db` is a Firestore
    client, read exactly as before: the switch defaults off.
    """
    from .statecall import StateCall  # lazy: statecall -> children -> inputs

    return db if isinstance(db, StateCall) else None


def route_context(state: Any) -> dict[str, Any]:
    """`GET state/context`: this attempt's OWN tenant's `tenants/<t>`,
    `workspace_settings/<t>`, `tenant_settings/<t>`, its TypeSafe key records
    and the submitter's forge grant, each id derived by swarm-api."""
    return state.get("context", deadline_seconds=STATE_CALL_SECONDS)


def route_repository(
    state: Any, where: "Target"
) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]] | str:
    """`GET state/repository`: the registration of the task's SIGNED
    repository and its kept versions (each with its document `id`), or why
    there are none.

    swarm-api derives the registration from the task's signed
    `repository_url`; the worker derived `where` from the same tenant and the
    URL it was handed. An answer for any other registration is refused, not
    read. A registration that is absent and one that is another tenant's are
    the same answer from the route (it serves neither), and the same reason
    here.
    """
    answer = state.get("repository", deadline_seconds=STATE_CALL_SECONDS)
    if answer.get("repo_id") != where.repo_id:
        return f"the state route serves no registration {where.repo_id} for this task"
    registration = answer.get("registration")
    if not isinstance(registration, Mapping) or registration.get("tenant_id") != where.tenant_id:
        return f"the registration {where.repo_id} has no document this tenant can read"
    versions = answer.get("versions")
    rows = [row for row in versions if isinstance(row, Mapping)] \
        if isinstance(versions, list) else []
    return registration, rows


@dataclass(frozen=True)
class BaseVersion:
    """A promoted index version: what produced it, and the digests promotion recorded."""

    sha: str
    task_id: str
    digest: str
    graph_digest: str


def resolve_base(
    db: Any, *, tenant_id: str, where: Target, base_sha: str,
    call_options: Mapping[str, Any] | None = None,
) -> BaseVersion | str:
    """The base's promoted version, or why there is none (a reason, never an exception).

    Read under the registration the SIGNED spec names (`where.repo_id`), and
    only when that registration is this tenant's.
    """
    state = state_routes(db)
    if state is not None:
        # A switched tenant: the versions swarm-api serves, the base among them by id.
        served = route_repository(state, where)
        if isinstance(served, str):
            return served
        found = [row for row in served[1] if row.get("id") == base_sha]
        if not found:
            return f"no promoted index of {base_sha[:12]} is kept for this repository"
        data = dict(found[0])
    else:
        options = dict(call_options or {})
        registration = db.collection(REPOSITORIES_COLLECTION).document(where.repo_id)
        snapshot = registration.get(**options)
        if not snapshot.exists:
            return f"the registration {where.repo_id} has no document"
        if (snapshot.to_dict() or {}).get("tenant_id") != tenant_id:
            return f"the registration {where.repo_id} is not this tenant's"
        version = registration.collection(VERSIONS_COLLECTION).document(base_sha).get(**options)
        if not version.exists:
            return f"no promoted index of {base_sha[:12]} is kept for this repository"
        data = version.to_dict() or {}
    task_id, digest, graph_digest = data.get("task_id"), data.get("digest"), \
        data.get("graph_digest")
    if not isinstance(task_id, str) or not task_id:
        return f"the index version of {base_sha[:12]} names no task"
    if not isinstance(digest, str) or not _DIGEST.match(digest):
        return f"the index version of {base_sha[:12]} records no digest"
    if not isinstance(graph_digest, str) or not _DIGEST.match(graph_digest):
        return f"the index version of {base_sha[:12]} has no graph to build on"
    return BaseVersion(sha=base_sha, task_id=task_id, digest=digest, graph_digest=graph_digest)


# --------------------------------------------------------------------------
# retention of tenants/<t>/repos/ (§2.3, lane IX3)
# --------------------------------------------------------------------------

#: The most versions read for the keep set. swarm-api keeps 20
#: (`swarm_api.repoindex.KEPT_VERSIONS`); more than this means its pruning is
#: behind, and the run then retires nothing rather than guess which to keep.
MAX_KEPT_READ = 200


def kept_commits(
    db: Any, *, tenant_id: str, where: Target, own: str | None,
    call_options: Mapping[str, Any] | None = None,
) -> list[str] | str:
    """The commits whose objects under repos/ the sweep must keep, or why there are none.

    Every `index_versions` entry of the registration the SIGNED spec names
    (swarm-api prunes them to the last 20 at promotion, `_prune_versions`),
    the promoted `current_sha`, and this run's own commit. A reason (and so
    no `--keep-commit`, and nothing retired) when the registration is not
    this tenant's, a version names no sha, or there are more than
    `MAX_KEPT_READ`: the sweep then works as it did before lane IX3.
    """
    state = state_routes(db)
    if state is not None:
        # A switched tenant: swarm-api runs the read the worker's role never
        # could (§1.3), and serves at most ROUTE_VERSIONS_SERVED. That many
        # may be a cut-short list, and keeping too few would let the sweep
        # delete a kept commit's objects: so it retires nothing.
        served = route_repository(state, where)
        if isinstance(served, str):
            return served
        data, listed = dict(served[0]), served[1]
        if len(listed) >= ROUTE_VERSIONS_SERVED:
            return (f"the state route serves at most {ROUTE_VERSIONS_SERVED} index versions, "
                    "and this registration may keep more; none is retired")
        rows: Any = listed
    else:
        options = dict(call_options or {})
        registration = db.collection(REPOSITORIES_COLLECTION).document(where.repo_id)
        snapshot = registration.get(**options)
        if not snapshot.exists:
            return f"the registration {where.repo_id} has no document"
        data = snapshot.to_dict() or {}
        if data.get("tenant_id") != tenant_id:
            return f"the registration {where.repo_id} is not this tenant's"
        rows = (row.to_dict() or {} for row in registration.collection(
            VERSIONS_COLLECTION).limit(MAX_KEPT_READ + 1).stream(**options))
    kept: set[str] = set()
    for count, row in enumerate(rows, start=1):
        if count > MAX_KEPT_READ:
            return f"more than {MAX_KEPT_READ} index versions are kept; none is retired"
        commit = row.get("commit_sha")
        if not isinstance(commit, str) or not _SHA.match(commit):
            return "an index version names no commit; none is retired"
        kept.add(commit)
    current = (data.get("index") or {}).get("current_sha")
    for commit in (current, own):
        if isinstance(commit, str) and _SHA.match(commit):
            kept.add(commit)
    return sorted(kept)


def content_digest(data: bytes) -> str:
    """`sha256:<hex>`, as promotion records it (`swarm_api.repoindex.content_digest`)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class PhaseRecord:
    """One phase of an index run: what happened and how long it took."""

    phase: str
    status: str  # ok | failed | timed_out | skipped
    seconds: float
    exit_code: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def write_phases(work: Path, records: list[PhaseRecord]) -> None:
    """`work/repo-index.phases.json`: what the agent reads to know what ran before it."""
    (work / PHASES_FILE).write_text(
        json.dumps([record.as_dict() for record in records], indent=2, sort_keys=True) + "\n"
    )


def last_line(path: Path, limit: int = 300) -> str | None:
    """The last non-empty line of a phase's stderr: the reason it gives for a failure."""
    try:
        lines = [line for line in path.read_text("utf-8", "replace").splitlines() if line.strip()]
    except OSError:
        return None
    return lines[-1][:limit] if lines else None


__all__ = [
    "BASE_GRAPH_FILE", "BASE_INDEX_FILE", "BaseVersion", "Budgets", "EXTRACTOR_COMMAND",
    "EXTRACT_FILE", "GRAPH_FILE", "GRAPH_WRITER_COMMAND", "INDEXER_PROFILE", "INDEX_FILE",
    "MAX_BASE_INDEX_BYTES", "PHASES_FILE", "PhaseRecord", "Target", "base_graph_bytes", "budgets",
    "content_digest", "extractor_argv", "graph_read_argv", "graph_write_argv", "is_index_run",
    "last_line", "phase_env", "repo_id_for", "requested_base", "resolve", "resolve_base",
    "target", "write_phases",
]
