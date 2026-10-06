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
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

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


@dataclass(frozen=True)
class Budgets:
    """Seconds each phase may take, out of the task's own timeout."""

    extract: int
    lsp_total: int
    graph_write: int


def budgets(timeout_seconds: int) -> Budgets:
    """Each deterministic phase's timeout, sized to the task's timeout.

    1,800 s (a full run) gives 720 s to extract, 540 s of it to the LSP
    pass, and 270 s reserved for the graph write: the agent has at least the
    810 s between. Never more than the task has.
    """
    total = max(1, int(timeout_seconds))
    extract = min(total, max(EXTRACT_MIN_SECONDS, int(total * EXTRACT_SHARE)))
    write = min(total, max(GRAPH_WRITE_MIN_SECONDS, int(total * GRAPH_WRITE_SHARE)))
    lsp = max(30, extract - EXTRACT_LSP_RESERVE_SECONDS)
    return Budgets(extract=extract, lsp_total=lsp, graph_write=write)


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


def extractor_argv(program: str, *, checkout: Path, work: Path, lsp_total: int) -> list[str]:
    return [
        program, "--repo", str(checkout), "--out", str(work / EXTRACT_FILE),
        "--graph-out", str(work / GRAPH_FILE),
        "--lsp-total-budget-seconds", str(lsp_total),
    ]


def graph_write_argv(
    program: str, *, work: Path, artifacts: Path, where: Target, bucket: str
) -> list[str]:
    return [
        program, "write", "--graph", str(work / GRAPH_FILE),
        "--repo-id", where.repo_id, "--destination", where.destination,
        "--tenant", where.tenant_id, "--store", f"gs://{bucket}",
        "--index", str(artifacts / INDEX_FILE),
    ]


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
    "Budgets", "EXTRACTOR_COMMAND", "EXTRACT_FILE", "GRAPH_FILE", "GRAPH_WRITER_COMMAND",
    "INDEXER_PROFILE", "INDEX_FILE", "PHASES_FILE", "PhaseRecord", "Target", "budgets",
    "extractor_argv", "graph_write_argv", "is_index_run", "last_line", "phase_env",
    "repo_id_for", "resolve", "target", "write_phases",
]
