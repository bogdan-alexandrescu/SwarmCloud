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

WHO DECIDES: THE STORED SETTINGS, NARROWED BY THE SIGNED WORKFLOW SWITCH
(§4.7, owner decision Q1, 2026-10-14; revised by the owner 2026-11-01 for the
signed dispatch block only). Until 2026-11-01 this was "a stored setting,
never the caller", and nothing in the task's input or metadata was read.
The owner's experiment -- one feature built four ways at once, with and
without code intelligence -- needs a workflow's every step to stage or not
as its spec says, so the switch `code_intelligence: on|off` in the SIGNED
`task.metadata.dispatch` block (`swarm_api.validation.DISPATCH_SWITCHES`,
covered by the spec signature) is read too, and nothing else of the task's.
Invariant 10 holds: the switch is an on/off word that names no image,
command, resource spec or backend. The decision is, in order:

  0. the workflow switch `off`: nothing is staged, whatever is stored;
  1. the registration's `context` override: `on` or `off`; absent, or
     `inherit`, defers to
  2. the workspace's `workspace_settings/<tenant>.code_intelligence.context`:
     `on` or `off`; absent defers to
  3. `DEFAULT_CONTEXT`, `on`: local context never leaves the platform.

  A stored `off` (1 or 2) stays off under the switch `on`: that is an
  admin's decision. Otherwise the switch `on` stages, on either arm, and
  runs no experiment. Without a switch, `code_intelligence.context_ab`
  (default `DEFAULT_AB`) splits the staged steps for the online A/B (§6.2):
  with `ab`, `assign_arm` puts a task on arm A (staged nothing, today's
  runner) or arm B (staged) from sha256(task_id) mod 2, so every attempt of
  one task is on one arm and who submits it cannot bias the split. `on` or
  `off` runs no experiment. `stage.json` names the switch, its value and the
  deciding source (`workflow switch`, `repository`, `workspace` or
  `default`), which is how the experiment proves each step's condition. A
  step the switch turns off gets no graph tool at all: no snapshot, no pin,
  no ticket, and no additional repository's graph.

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
    (`index.head_relation`, status `ahead`); failing those, `merge-base`
    again after a bounded fetch deepens the depth-1 clone behind the base
    (`gitops.fetch_base_history`, lane GRAPH-ANCESTRY). A non-ancestor, or one whose
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

PINNED AND SERVED (lane GS-WORKER, docs/design/graph-service.md §4.1, §8).
The workspace's stored `code_intelligence.graph_delivery` decides HOW a step
that gets graph context gets it; it never decides WHETHER (`decide` does).
`staged`, the default, is everything above, exactly: nothing below runs and
no ticket is asked for. `service` keeps the whole decision and the ancestry
proof, and then, where staging would copy the graph, it

  1. PINS it: `pin` downloads only the manifest, makes the same checks
     staging makes (the promoted digest, the schema, the commit, the format)
     and writes `pin.json` (freshness.json's record plus the digest) and
     `pin-manifest.json` (the manifest's bytes, for the context pack) into
     the context directory. Neither holds a credential, so `work/` is their
     home and a resumed attempt reads the same pin;
  2. MINTS the attempt's first graph ticket into `private/graph-ticket`
     (`graph_ticket.GraphTicket.start`), never into `work/`;
  3. PROBES the service once (`graph_ticket.probe`, one `initialize`, 2 s);
  4. returns `SWARM_CONTEXT`, `SWARM_GRAPH_PIN`, `SWARM_GRAPH_TICKET_FILE`
     and `SWARM_GRAPH_URL`, and never `SWARM_GRAPH_SNAPSHOT`. The runners
     register `shim_argv` as `swarm-graph` when they see the pin.

NO GAP (§8.2). Any failure in 1-3 -- a mint refused for a reason other than
the setting, a service down or switched off, no attempt key -- stages the
same attempt exactly as today, and `stage.json` and `graph_ready` say
`delivery: staged` with the reason in `fallback`. Two outcomes are not
fallbacks. A 409 at the mint is the fence: `GraphTicketFenced` is raised
through `prepare` for the lifecycle's fenced exit (invariant 5). A 403 is
swarm-api's stored setting saying no graph for this repository: nothing is
served and nothing is staged (§10, "context disabled").

CLOUD RUN JOB STEPS KEEP STAGING (owner decision Q2, 2026-10-15): a step on
the `CLOUD_RUN_JOB` backend (today `claude-code-review`) cannot reach the
headless in-cluster Service, so it stages whatever the setting says, and its
`stage.json` says why.

WHERE THE SETTING IS READ. swarm-api's `codeintel` writes `context`,
`context_ab` and `graph_delivery` into
`workspace_settings/<tenant>.code_intelligence` (`swarm_api.codeintel
.COLLECTION`), and all three are read from there (SETTINGS_COLLECTION), by
Firestore and from `state/context`'s `workspace_settings` alike. Until
2026-11-01 `context` and `context_ab` were read from `tenant_settings`,
which nothing writes, so a workspace's choice never reached the worker.

ADDITIONAL REPOSITORIES (docs/design/multi-repository-steps.md §4.9, lane
MR-GRAPH, part of #1434). `prepare` takes the task's SIGNED
`additional_repositories` and, for each entry, makes the decision above
against that entry's own registration and pins the graph of a version proven
an ancestor of the entry's `commit` -- the proof runs in its checkout under
`work/repos/<name>`, or on the compare swarm-api recorded. Delivered the way
the step's own graph is, under `work/.swarm-context/repos/<name>/`:

  * served, when `graph_delivery` is `service` and the step's own graph was
    served: `pin.json` and `pin-manifest.json`, its ticket at
    `private/graph-ticket-<name>`, swarm-api accepting the entry's `repo_id`
    only from the signed list. The shim the runner registers for the step's
    own graph finds them there and takes a `repo` argument;
  * staged otherwise, the default: `snapshot/`, written as the step's own is.
    The service is never asked for an additional ticket when the step's own
    graph was not served, because only that graph's shim reaches one. The
    staged server (`graphmcp.server`) reads the step's own snapshot today;
    serving the additional ones beside it is that module's change.

Every other outcome -- unindexed, refused by the setting, not an ancestor --
is that entry's `stage.json` reason (`graph: false`), and the step still has
its checkout and `rg`. A malformed list is refused whole. An entry never
fails the attempt; only the fence is raised.
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
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from swarm_common.models import check_additional_repositories

from . import graph_ticket as graph_ticket_mod
from . import gitops, graphroute, indexrun

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

#: `work/.swarm-context/repos/<name>/`: one additional repository's pin or
#: snapshot, and its stage record (§4.9). Cleared at every `prepare`: a restored checkpoint's is
#: not this attempt's.
REPOS_CONTEXT_DIR_NAME = "repos"
#: `work/repos/<name>`, where the worker checks out each additional
#: repository (multi-repository-steps.md §4.4, lane MR-WORKER's
#: `workspace.REPOS_DIR_NAME`).
REPOS_DIR_NAME = "repos"

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

#: The pin `pin` writes in place of snapshot/ (§4.1 step 1), and the pinned
#: manifest's bytes beside it: `contextpack.PIN_FILE` and `PIN_MANIFEST_FILE`,
#: held equal by tests/unit/worker/test_graphstage.py.
PIN_NAME = "pin.json"
PIN_MANIFEST_NAME = "pin-manifest.json"
#: The pin's path, set only when the graph is served: the runner registers
#: the shim exactly when this and TICKET_FILE_ENV are set.
PIN_ENV = "SWARM_GRAPH_PIN"
#: `private/graph-ticket`'s path (owner Q1): the shim's argument, and what the
#: context pack reads the ticket from (`contextpack.build.TICKET_FILE_ENV`).
TICKET_FILE_ENV = "SWARM_GRAPH_TICKET_FILE"
#: The service's URL, the worker's own setting (`config.graph_service_url`),
#: passed in the shim's server entry `env` (§4.2), never an argument.
URL_ENV = "SWARM_GRAPH_URL"
#: The shim's module (lane GS-SHIM), started with this runner's interpreter.
SHIM_MODULE = "agent_worker.graphmcp.shim"
#: Backends whose steps stage whatever the setting says (owner Q2).
STAGING_BACKENDS = ("CLOUD_RUN_JOB",)

# --------------------------------------------------------------------------
# the stored setting (§4.7)
# --------------------------------------------------------------------------

#: `workspace_settings/<tenant_id>`: swarm-api's own document, NOT the frozen
#: `swarm_common.models.Tenant` (§4.7). Written by `swarm_api.codeintel`
#: (`COLLECTION`); read from `tenant_settings` until 2026-11-01, which nothing
#: wrote (see the module docstring).
SETTINGS_COLLECTION = "workspace_settings"
CODE_INTELLIGENCE_FIELD = "code_intelligence"
#: `code_intelligence.context`: `on` | `off`.
CONTEXT_FIELD = "context"
#: `code_intelligence.context_ab`: `ab` | `on` | `off` (§6.2's `context.ab`).
AB_FIELD = "context_ab"
#: The registration's override (`RepositoryPatch`, CTXQ-SET): `inherit` | `on` | `off`.
REPOSITORY_CONTEXT_FIELD = "context"
#: `code_intelligence.graph_delivery`: `staged` | `service` (graph-service.md
#: §8.1, GS-SETTING's `swarm_api.codeintel.DELIVERY_FIELD`).
DELIVERY_FIELD = "graph_delivery"
#: Where swarm-api writes it: `workspace_settings/<tenant_id>`
#: (`swarm_api.codeintel.COLLECTION`), the same document as the rest.
DELIVERY_SETTINGS_COLLECTION = SETTINGS_COLLECTION

#: The workflow switch in the signed `task.metadata.dispatch` block
#: (`swarm_api.validation.DISPATCH_SWITCHES`, 2026-11-01): `on` | `off`.
SWITCH_CODE_INTELLIGENCE = "code_intelligence"
#: The deciding source when the switch decided.
SOURCE_SWITCH = "workflow switch"
#: `stage.json`'s record of the workflow switches: `{code_intelligence:
#: {value, source, graph, reason}, jev_judgments: {value, source, on, reason}}`,
#: and the attempt's `result_summary` key it is copied to when the signed
#: block set any switch (`switches_summary`).
SWITCHES_FIELD = "switches"

#: Local context is ON unless a workspace or a repository turns it off: it never
#: leaves the platform (owner decision Q1, 2026-10-14).
DEFAULT_CONTEXT = "on"
#: The A/B runs unless a workspace says otherwise. Owner decision Q9
#: (2026-10-14): local context becomes default-on for EVERY step only after
#: §6.3's thresholds pass on the online A/B, so until then a workspace that
#: set nothing is in the experiment, half its tasks on arm B. Flip this to
#: `on` when the thresholds pass; a workspace can set `on` sooner.
DEFAULT_AB = "ab"
#: Staging until a workspace's owner switches it (§8.1-§8.2): with this, the
#: worker behaves exactly as it did before the service existed.
#: `swarm_api.codeintel.DEFAULT_GRAPH_DELIVERY`, held equal by the tests.
DEFAULT_GRAPH_DELIVERY = DELIVERY_STAGED

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


def switch_of(dispatch: Mapping[str, Any] | None, name: str) -> str | None:
    """`on` or `off` from the signed dispatch block's switch `name`; None when
    absent or not one of the two words (an older or newer control plane)."""
    value = dispatch.get(name) if isinstance(dispatch, Mapping) else None
    return value if value in (ON, OFF) else None


@dataclass(frozen=True)
class Decision:
    """Whether to stage, from the stored setting, the switch and the arm."""

    stage: bool
    context: str          # on | off: what the setting resolved to
    source: str           # workflow switch | repository | workspace | default
    ab: str               # ab | on | off
    arm: str | None       # A | B under the experiment, else None
    reason: str
    #: The signed workflow switch `code_intelligence`: on | off, None when unset.
    switch: str | None = None


def decide(*, repository_override: Any, workspace_settings: Mapping[str, Any] | None,
           task_id: str, switch: str | None = None) -> Decision:
    """The stored setting's answer for this task, narrowed by the signed
    workflow `switch` (`switch_of`). Unknown values read as unset.

    `off` never stages. `on` stages on either A/B arm wherever the stored
    context resolves on, and never over a stored `off`."""
    switch = switch if switch in (ON, OFF) else None
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
    if switch == OFF:
        return Decision(False, OFF, SOURCE_SWITCH, ab, None,
                        "graph context is off (workflow switch)", switch)
    if context == OFF:
        return Decision(False, context, source, ab, None,
                        f"graph context is off ({source} setting)", switch)
    if switch == ON:
        return Decision(True, context, SOURCE_SWITCH, ab, None,
                        f"graph context is on (workflow switch; {source} allows it)", switch)
    if ab != AB:
        return Decision(True, context, source, ab, None, f"graph context is on ({source})")
    arm = assign_arm(task_id)
    if arm == ARM_A:
        return Decision(False, context, source, ab, arm,
                        "A/B arm A: this task runs without graph context")
    return Decision(True, context, source, ab, arm, "A/B arm B: graph context staged")


def delivery_of(workspace_settings: Mapping[str, Any] | None) -> str:
    """`service` only when the stored `graph_delivery` says exactly that;
    anything else, `inherit`, malformed or absent, is DEFAULT_GRAPH_DELIVERY."""
    code_intel = (workspace_settings or {}).get(CODE_INTELLIGENCE_FIELD)
    value = code_intel.get(DELIVERY_FIELD) if isinstance(code_intel, Mapping) else None
    return DELIVERY_SERVICE if value == DELIVERY_SERVICE else DEFAULT_GRAPH_DELIVERY


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


def is_shallow(checkout: Path | None) -> bool:
    """Whether the clone is shallow: only then can a fetch add history the
    ancestry proof lacks. A full clone already holds every ancestor."""
    if checkout is None:
        return False
    done = _git(checkout, "rev-parse", "--is-shallow-repository")
    return done is not None and done.returncode == 0 and done.stdout.strip() == b"true"


def _git_ancestry(checkout: Path, index_sha: str, base_sha: str) -> Ancestry:
    count = _git(checkout, "rev-list", "--count", f"{index_sha}..{base_sha}")
    diff = _git(checkout, "diff", "--name-only", "-z", "--no-renames", index_sha, base_sha)
    behind = count.stdout.decode().strip() if count and count.returncode == 0 else ""
    changed = ([p for p in diff.stdout.decode("utf-8", "replace").split("\0") if p]
               if diff and diff.returncode == 0 else None)
    return Ancestry(basis="git", behind_by=int(behind) if behind.isdigit() else None,
                    changed=changed)


def _not_an_ancestor(index_sha: str, base_sha: str) -> str:
    return f"the index commit {index_sha[:12]} is not an ancestor of the base " \
           f"{base_sha[:12]}"


def prove_ancestry(checkout: Path | None, index_sha: str, base_sha: str,
                   index_state: Mapping[str, Any],
                   deepen: Callable[[], str | None] | None = None) -> Ancestry | str:
    """How `index_sha` is known to be an ancestor of `base_sha`, or why it is not.

    `deepen` (lane GRAPH-ANCESTRY, part of #2540) makes the clone's history
    reach further back -- `gitops.fetch_base_history`, once per staging --
    and answers None when it did, or why not. It is asked only when neither
    the clone nor the recorded compare could answer: a lane's clone is one
    commit deep, so git could never see the index commit, and swarm-api
    records the compare of ONE pair (the promoted index against the head
    its last poll read), which a lane based on any other main commit never
    matches. Measured 2026-10-20/27: 1,053 of eng's 1,799 graph-asking steps
    were refused here, against 3 proven by git. The rule is unchanged: only
    a proof stages, and a fetch that fails or does not reach the index
    commit leaves the ancestry not shown.
    """
    if index_sha == base_sha:
        return Ancestry(basis="same", behind_by=0, changed=[])
    local = is_ancestor(checkout, index_sha, base_sha)
    if local is False:
        return _not_an_ancestor(index_sha, base_sha)
    if local is True and checkout is not None:
        return _git_ancestry(checkout, index_sha, base_sha)
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
    unshown = f"the ancestry of the index commit {index_sha[:12]} and the base " \
              f"{base_sha[:12]} cannot be shown from this clone or the recorded compare"
    if deepen is None or checkout is None:
        return unshown
    why = deepen()
    if why is not None:
        return f"{unshown} ({why})"
    local = is_ancestor(checkout, index_sha, base_sha)
    if local is True:
        return _git_ancestry(checkout, index_sha, base_sha)
    if local is False:
        return _not_an_ancestor(index_sha, base_sha)
    return f"{unshown} (not within the history fetched behind the base)"


class _HistoryOnce:
    """One staging's `deepen`: the fetch runs at most once, for the first
    candidate that needs it, and every later candidate reads its answer."""

    def __init__(self, fetch: Callable[[Path, str], str | None], checkout: Path,
                 base_sha: str) -> None:
        self._fetch, self._checkout, self._base = fetch, checkout, base_sha
        self._asked = False
        self._answer: str | None = None
        #: Seconds the fetch took, or None when it was not needed.
        self.seconds: float | None = None

    def __call__(self) -> str | None:
        if not self._asked:
            self._asked = True
            started = time.monotonic()
            try:
                self._answer = self._fetch(self._checkout, self._base)
            except Exception as exc:  # noqa: BLE001 -- a fetch is a reason, never a failure
                self._answer = f"the history fetch failed ({type(exc).__name__})"
            self.seconds = round(time.monotonic() - started, 3)
        return self._answer


def history_fetcher(repository_url: str | None, private_dir: Path, *,
                    token: Callable[[], str | None] | None = None,
                    ) -> Callable[[Path, str], str | None]:
    """The production `deepen` fetch: `gitops.fetch_base_history` from the
    task's SIGNED `repository_url`, validated as a clone validates it.

    `token` supplies the tenant's forge token for a private repository; with
    none, the fetch is anonymous, which a public repository answers and a
    private one refuses -- a reason in `stage.json`, as before this lane."""
    def fetch(checkout: Path, base_sha: str) -> str | None:
        try:
            url = gitops.validate_repository_url(repository_url or "")
        except gitops.GitError:
            return "the repository URL is not one a fetch may use"
        # Asked here, only once a fetch is needed, so a step whose proof
        # needs no fetch never reads the token at all. A read that fails is
        # an anonymous fetch, and the reason says so if that fails too.
        unread = ""
        try:
            secret = token() if token is not None else None
        except Exception as exc:  # noqa: BLE001 -- a token read is a reason, never a failure
            secret, unread = None, f" (forge token not read: {type(exc).__name__})"
        why = gitops.fetch_base_history(checkout, url=url, base_sha=base_sha,
                                        private_dir=private_dir, token=secret or None)
        return f"{why}{unread}" if why is not None else None
    return fetch


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


def checked_manifest(store: Any, *, where: indexrun.Target,
                     version: Version) -> tuple[bytes, dict[str, Any], int]:
    """One version's manifest, `(bytes, parsed, format)`, checked as staging
    checks it: the digest promotion recorded, the schema, the commit and the
    format. Raises StageRefused on any of them."""
    manifest_bytes = store.download_bytes(f"{where.destination}/{version.sha}/manifest.json")
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
    return manifest_bytes, manifest, fmt


def freshness_record(version: Version, ancestry: Ancestry, base_sha: str) -> dict[str, Any]:
    """`freshness.json`'s record (`graphmcp.snapshot.load_freshness_record`),
    which is also the pin's (§4.1 step 1)."""
    record: dict[str, Any] = {
        "index_sha": version.sha, "base_sha": base_sha,
        "behind_by": ancestry.behind_by, "manifest_digest": version.graph_digest,
        "basis": ancestry.basis,
    }
    if ancestry.changed is not None:
        record["changed"] = sorted(ancestry.changed)
    return record


def _write_atomic(path: Path, data: bytes) -> None:
    fd, scratch = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(scratch, path)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


def remove_pin(context_dir: Path) -> None:
    """The pin and its manifest, gone: a pin left behind is not this attempt's."""
    for name in (PIN_NAME, PIN_MANIFEST_NAME):
        target = context_dir / name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)


def pin(store: Any, *, where: indexrun.Target, version: Version, context_dir: Path,
        ancestry: Ancestry, base_sha: str) -> dict[str, Any]:
    """Pin one version for the service (§4.1 step 1); return its facts.

    Downloads the manifest only, checks it as staging does, and writes
    `pin-manifest.json` then `pin.json`, each by rename, so a `pin.json` on
    disk always has its manifest beside it. `bytes` is the manifest's and
    `blobs` 0: what the pin cost, against staging's every blob.
    """
    manifest_bytes, _manifest, fmt = checked_manifest(store, where=where, version=version)
    context_dir.mkdir(parents=True, exist_ok=True)
    try:
        _write_atomic(context_dir / PIN_MANIFEST_NAME, manifest_bytes)
        record = freshness_record(version, ancestry, base_sha)
        _write_atomic(context_dir / PIN_NAME,
                      (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    except BaseException:
        remove_pin(context_dir)
        raise
    return {"index_sha": version.sha, "format_version": fmt, "blobs": 0,
            "bytes": len(manifest_bytes), "behind_by": ancestry.behind_by,
            "basis": ancestry.basis,
            "changed": len(ancestry.changed) if ancestry.changed is not None else None}


def write_snapshot(store: Any, *, where: indexrun.Target, version: Version,
                   destination: Path, ancestry: Ancestry, base_sha: str) -> dict[str, Any]:
    """Stage one version's manifest and blobs at `destination`, checked; return its facts.

    Built in a sibling temporary directory and renamed into place, so a
    half-staged snapshot is never what the server opens.
    """
    root = f"{where.destination}"
    manifest_bytes, manifest, fmt = checked_manifest(store, where=where, version=version)
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
        record = freshness_record(version, ancestry, base_sha)
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
    """The promoted current version, read by its id: the direct path's one candidate.

    NO LISTING (lane GRAPH-STAGING, part of #1694). This read once went on to
    list the other kept versions (`limit(MAX_VERSIONS_READ).stream()`), a
    Firestore query the tenant worker's role cannot run: it has
    `entities.get`, not `entities.list` (tenant-settings-integrity.md §1.3),
    and the query's PermissionDenied failed the whole staging, the promoted
    version included. The older versions now come only from swarm-api, which
    lists them server-side (`graphroute`, `state/repository`); a worker
    reading Firestore directly -- one with no attempt key, or whose route was
    not answered -- stages the promoted version or nothing.
    """
    versions = registration.collection(indexrun.VERSIONS_COLLECTION)
    current = index_state.get("current_sha")
    if not isinstance(current, str) or not _SHA.match(current):
        return []
    snap = versions.document(current).get(**options)
    found = _version_of(snap.to_dict() if snap.exists else None, current)
    return [found] if found is not None else []


def _served_candidates(rows: Sequence[Mapping[str, Any]],
                       index_state: Mapping[str, Any]) -> list[Version]:
    """`_candidates`, over the versions `state/repository` served (each with
    its document `id`, the commit): the promoted current one first, by id,
    then the others, newest first. swarm-api serves at most
    `indexrun.ROUTE_VERSIONS_SERVED`, which is MAX_VERSIONS_READ."""
    out: list[Version] = []
    current = index_state.get("current_sha")
    if isinstance(current, str) and _SHA.match(current):
        for row in rows:
            if row.get("id") == current:
                found = _version_of(row, current)
                if found is not None:
                    out.append(found)
                break
    others: list[Version] = []
    for row in rows[:MAX_VERSIONS_READ]:
        found = _version_of(row, str(row.get("commit_sha") or ""))
        if found is not None and found.sha != current:
            others.append(found)
    others.sort(key=lambda v: v.built_at, reverse=True)
    return out + others


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
    #: Pinned, ticketed and probed: the graph is served (§4.1). Never with `staged`.
    served: bool = False
    pin: Path | None = None
    ticket_file: Path | None = None
    #: Why `service` was asked for and the attempt staged instead (§8.2).
    fallback: str | None = None
    #: Why swarm-api's graph context route was not read from (lane
    #: GRAPH-STAGING); None when it was, or when the tenant reads the state
    #: routes. Logged, not recorded: `stage.json` keeps its shape.
    route: str | None = None
    #: Seconds the ancestry proof's history fetch took (lane GRAPH-ANCESTRY);
    #: None when no fetch was needed. In `stage.json` only when it ran.
    history_seconds: float | None = None

    @property
    def delivery(self) -> str:
        if self.served:
            return DELIVERY_SERVICE
        return DELIVERY_STAGED if self.staged else DELIVERY_NONE

    @property
    def wanted(self) -> bool:
        """The setting asked for context on this task (so its absence is said)."""
        return self.decision is not None and self.decision.stage

    def switch_record(self, switch: str | None) -> dict[str, Any]:
        """The `code_intelligence` switch's line in `stage.json`: its value,
        the deciding source and whether a graph was delivered."""
        source = self.decision.source if self.decision is not None else None
        return {"value": switch, "source": source, "graph": self.delivery != DELIVERY_NONE,
                "reason": self.reason}

    def record(self) -> dict[str, Any]:
        return {
            "schema": STAGE_SCHEMA,
            "staged": self.staged,
            "reason": self.reason,
            "decision": asdict(self.decision) if self.decision is not None else None,
            "snapshot": str(self.snapshot) if self.snapshot is not None else None,
            "delivery": self.delivery,
            "seconds": self.seconds,
            # Only when they say something, so a `staged` record is the one
            # staging wrote before the service existed.
            **({"pin": str(self.pin)} if self.served and self.pin is not None else {}),
            **({"fallback": self.fallback} if self.fallback else {}),
            **({"history_seconds": self.history_seconds}
               if self.history_seconds is not None else {}),
            **self.facts,
        }


def ready_detail(result: StageResult) -> dict[str, Any]:
    """The `graph_ready` mark's detail: numbers and the reason, nothing else.

    No path, URL, commit or digest: the API serves an event's detail as
    stored. `bytes` and `blobs` are None when nothing was delivered, never a
    zero that would read as an empty graph staged. Served, they are the
    pinned manifest's bytes and 0 blobs: what the pin downloaded.
    """
    delivered = result.staged or result.served
    return {"graph": {
        "delivery": result.delivery,
        "seconds": result.seconds,
        "bytes": result.facts.get("bytes") if delivered else None,
        "blobs": result.facts.get("blobs") if delivered else None,
        "reason": result.reason,
    }}


@dataclass(frozen=True)
class ServiceRoute:
    """What the service path needs from the lifecycle (§4.1 steps 2-3)."""

    #: `graph_ticket.GraphTicket` (`start`, `discard`, `path`), or None.
    ticket: Any = None
    private_dir: Path | None = None
    #: `WorkerConfig.backend`: a STAGING_BACKENDS step keeps staging (owner Q2).
    backend: str | None = None
    url: str | None = None
    #: `graph_ticket.probe`'s shape: (url, ticket file) -> None or why not.
    probe: Callable[[str, Path], str | None] | None = None

    def unavailable(self) -> str | None:
        """Why this attempt cannot be served whatever the setting says, or None."""
        if self.backend in STAGING_BACKENDS:
            return f"a {self.backend} step keeps staging (owner decision Q2)"
        if self.ticket is None or self.private_dir is None:
            return "this attempt has no graph ticket upkeep"
        parts = urlsplit(self.url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return f"{URL_ENV} is not an http(s) URL with a host"
        return None

    def serve(self, version: Version) -> str | None:
        """Mint the first ticket and probe the service: None when served, else why not.
        Raises GraphTicketFenced and GraphTicketForbidden from the mint."""
        why = self.ticket.start(self.private_dir, version.sha)
        if why is not None:
            return why
        why = (self.probe or graph_ticket_mod.probe)(self.url, self.ticket.path)
        if why is not None:
            return f"the graph service did not answer the probe ({why})"
        return None


def stage(*, db: Any, store: Any, tenant_id: str, repository_url: str | None, task_id: str,
          checkout: Path | None, base_sha: str | None, context_dir: Path,
          call_options: Mapping[str, Any] | None = None,
          clock: Callable[[], float] = time.monotonic,
          service: ServiceRoute | None = None,
          unserved: str | None = None,
          graph_context: graphroute.GraphContextRoute | None = None,
          fetch_history: Callable[[Path, str], str | None] | None = None,
          switch: str | None = None) -> StageResult:
    """Decide and stage (or pin), timed on `clock`. Every failure is a reason;
    only the fence, GraphTicketFenced, is raised (invariant 5).

    `unserved` is why this graph cannot be served whatever the setting says,
    beside `ServiceRoute.unavailable`: an additional repository's when the
    step's own graph was not served (§4.9). It is staged instead.

    `graph_context` is swarm-api's answer for the task's OWN repository
    (`graphroute`, lane GRAPH-STAGING), read in place of Firestore when the
    tenant is not switched to the state routes. Never an additional
    repository's: the route serves the signed `repository_url` only.

    `fetch_history` deepens a shallow `checkout` behind `base_sha` when the
    ancestry proof needs it (`history_fetcher`, lane GRAPH-ANCESTRY); None
    proves from the clone and the recorded compare alone.

    `switch` is the signed dispatch block's `code_intelligence` (`switch_of`):
    `off` stages nothing, `on` stages on either A/B arm (`decide`)."""
    started = clock()
    route_note: str | None = None
    try:
        if graph_context is not None and indexrun.state_routes(db) is None:
            # An unswitched tenant: the documents from swarm-api's
            # `context/graph`, one call. A 409 is the fence and is raised;
            # any other non-answer reads by document id, as the role allows.
            try:
                graph_context.fetch()
            except graphroute.GraphContextUnavailable as unavailable:
                route_note, graph_context = str(unavailable), None
        else:
            graph_context = None
        result = _stage(db=db, store=store, tenant_id=tenant_id,
                        repository_url=repository_url, task_id=task_id, checkout=checkout,
                        base_sha=base_sha, context_dir=context_dir,
                        options=dict(call_options or {}), service=service or ServiceRoute(),
                        unserved=unserved, graph_context=graph_context,
                        fetch_history=fetch_history, switch=switch)
    except graph_ticket_mod.GraphTicketFenced:
        raise
    except StageRefused as refused:
        result = StageResult(False, str(refused))
    except Exception as exc:  # noqa: BLE001 -- a staging failure never fails the attempt
        result = StageResult(False, f"staging failed ({type(exc).__name__})")
    result.route = route_note
    result.seconds = round(clock() - started, 3)
    return result


def _stage(*, db: Any, store: Any, tenant_id: str, repository_url: str | None, task_id: str,
           checkout: Path | None, base_sha: str | None, context_dir: Path,
           options: dict[str, Any], service: ServiceRoute,
           unserved: str | None = None,
           graph_context: graphroute.GraphContextRoute | None = None,
           fetch_history: Callable[[Path, str], str | None] | None = None,
           switch: str | None = None) -> StageResult:
    where = indexrun.target(tenant_id, repository_url)
    if isinstance(where, str):
        return StageResult(False, where)
    # A switched tenant's state client, else the graph context route that
    # `stage` found answered (lane GRAPH-STAGING): read alike.
    state: Any = indexrun.state_routes(db)
    if state is None:
        state = graph_context
    candidates_of: Callable[[Mapping[str, Any]], list[Version]]
    if state is not None:
        # A switched tenant (`indexrun.state_routes`), or the graph context
        # route: the registration and its versions from `state/repository`,
        # both settings documents from one `state/context`, every id derived
        # by swarm-api. No Firestore call.
        served = indexrun.route_repository(state, where)
        if isinstance(served, str):
            return StageResult(False, served)
        data, rows = dict(served[0]), served[1]
        context = indexrun.route_context(state)
        # The one document swarm-api writes all three fields into (see the
        # module docstring): never `tenant_settings`, which nothing writes.
        settings = _mapping(context.get("workspace_settings"))
        delivery = delivery_of(settings)

        def candidates_of(index_state: Mapping[str, Any]) -> list[Version]:
            return _served_candidates(rows, index_state)
    else:
        registration = db.collection(indexrun.REPOSITORIES_COLLECTION).document(where.repo_id)
        snap = registration.get(**options)
        found = (snap.to_dict() or {}) if snap.exists else None
        if found is None:
            return StageResult(False, "the repository is not registered in this workspace")
        if found.get("tenant_id") != tenant_id:
            return StageResult(False, f"the registration {where.repo_id} is not this tenant's")
        data = found
        settings_snap = db.collection(SETTINGS_COLLECTION).document(tenant_id).get(**options)
        settings = (settings_snap.to_dict() or {}) if settings_snap.exists else {}
        delivery = delivery_of(settings)

        def candidates_of(index_state: Mapping[str, Any]) -> list[Version]:
            return _candidates(registration, index_state, options)
    decision = decide(repository_override=data.get(REPOSITORY_CONTEXT_FIELD),
                      workspace_settings=settings, task_id=task_id, switch=switch)
    if not decision.stage:
        return StageResult(False, decision.reason, decision)
    result = StageResult(False, "", decision)
    # HOW, once WHETHER is settled (§8.1), from the same document.
    wants_service = delivery == DELIVERY_SERVICE
    if wants_service:
        result.fallback = unserved or service.unavailable()
    try:
        if not base_sha or not _SHA.match(base_sha):
            raise StageRefused("the step has no base commit to compare the index with")
        index_state = data.get("index") if isinstance(data.get("index"), Mapping) else {}
        candidates = candidates_of(index_state)
        if not candidates:
            raise StageRefused("no promoted index with a graph is kept for this repository")
        reasons: list[str] = []
        # Only a shallow clone can be deepened; a full one already holds
        # every ancestor the proof could ask for, so it fetches nothing.
        deepen = (_HistoryOnce(fetch_history, checkout, base_sha)
                  if fetch_history is not None and checkout is not None
                  and is_shallow(checkout) else None)
        for version in candidates:
            ancestry = prove_ancestry(checkout, version.sha, base_sha,
                                      index_state if version.sha == index_state.get(
                                          "current_sha") else {}, deepen=deepen)
            if deepen is not None:
                result.history_seconds = deepen.seconds
            if isinstance(ancestry, str):
                reasons.append(ancestry)
                continue
            if wants_service and result.fallback is None:
                if _pin_and_serve(service, store, where=where, version=version,
                                  context_dir=context_dir, ancestry=ancestry,
                                  base_sha=base_sha, result=result):
                    return result
            destination = context_dir / SNAPSHOT_DIR_NAME
            result.facts = write_snapshot(store, where=where, version=version,
                                          destination=destination, ancestry=ancestry,
                                          base_sha=base_sha)
            result.staged, result.snapshot = True, destination
            result.reason = f"staged the graph of {version.sha[:12]} ({ancestry.basis})"
            if result.fallback:
                result.reason += f"; the graph service was not used: {result.fallback}"
            return result
        raise StageRefused(reasons[0])
    except graph_ticket_mod.GraphTicketFenced:
        raise
    except StageRefused as refused:
        result.reason = str(refused)
    except Exception as exc:  # noqa: BLE001 -- see `stage`
        result.reason = f"staging failed ({type(exc).__name__})"
    return result


def _pin_and_serve(service: ServiceRoute, store: Any, *, where: indexrun.Target,
                   version: Version, context_dir: Path, ancestry: Ancestry, base_sha: str,
                   result: StageResult) -> bool:
    """§4.1 steps 1-3 for one proven version. True when served; False with
    `result.fallback` set, the pin and the ticket removed, to stage instead.

    A 409 (GraphTicketFenced) is raised as it is. A 403 (GraphTicketForbidden)
    is a StageRefused: the stored setting refused the graph, so it is not
    staged behind its back either.
    """
    try:
        facts = pin(store, where=where, version=version, context_dir=context_dir,
                    ancestry=ancestry, base_sha=base_sha)
        why = service.serve(version)
    except graph_ticket_mod.GraphTicketFenced:
        remove_pin(context_dir)
        raise
    except graph_ticket_mod.GraphTicketForbidden as refused:
        remove_pin(context_dir)
        raise StageRefused(str(refused)) from None
    except StageRefused as refused:
        why = str(refused)
    except Exception as exc:  # noqa: BLE001 -- the service is never worth the graph
        why = f"pinning failed ({type(exc).__name__})"
    if why is None:
        result.served, result.facts = True, facts
        result.pin, result.ticket_file = context_dir / PIN_NAME, service.ticket.path
        result.reason = (f"pinned the graph of {version.sha[:12]} ({ancestry.basis}) for the "
                         "graph service")
        return True
    remove_pin(context_dir)
    service.ticket.discard()
    result.fallback = why
    return False


# --------------------------------------------------------------------------
# the lifecycle's one call, and what the runners read
# --------------------------------------------------------------------------


def switches_summary(work: Path, dispatch: Mapping[str, Any] | None
                     ) -> dict[str, Any] | None:
    """`stage.json`'s `SWITCHES_FIELD` for the attempt's `result_summary`,
    only when the signed dispatch block set a workflow switch, so every other
    attempt's summary is the one it wrote before. When nothing was staged for
    the step (no `stage.json`), each set switch with its value alone. Never
    raises."""
    from .judge import attempt as judge_attempt

    names = (SWITCH_CODE_INTELLIGENCE, judge_attempt.SWITCH, "quality_gates")
    set_ = {name: switch_of(dispatch, name) for name in names}
    if not any(set_.values()):
        return None
    try:
        stored = json.loads((Path(work) / CONTEXT_DIR_NAME / STAGE_RECORD_NAME)
                            .read_text(encoding="utf-8")).get(SWITCHES_FIELD)
    except (OSError, ValueError, AttributeError):
        stored = None
    stored = dict(stored) if isinstance(stored, Mapping) else {}
    return {name: stored.get(name) if isinstance(stored.get(name), Mapping)
            else {"value": set_[name]}
            for name in (SWITCH_CODE_INTELLIGENCE, judge_attempt.SWITCH)}


def offers_graph(runner_argv: Sequence[str]) -> bool:
    """Whether this profile's runner can name `swarm-graph` to its agent."""
    return any(part in GRAPH_RUNNERS for part in runner_argv)


def prepare(work: Path, *, profile_name: str, runner_argv: Sequence[str], db: Any,
            store: Any, tenant_id: str, repository_url: str | None, task_id: str,
            checkout: Path | None, base_sha: str | None,
            call_options: Mapping[str, Any] | None = None,
            log: Any = None,
            mark: Callable[[dict[str, Any]], None] | None = None,
            graph_ticket: Any = None,
            private_dir: Path | None = None,
            backend: str | None = None,
            service_url: str | None = None,
            probe: Callable[[str, Path], str | None] | None = None,
            secret_env_names: Iterable[str] = (),
            additional_repositories: Sequence[Mapping[str, Any]] | None = None,
            repos_dir: Path | None = None,
            graph_context: graphroute.GraphContextRoute | None = None,
            forge_token: Callable[[], str | None] | None = None,
            dispatch: Mapping[str, Any] | None = None,
            jev_key: Callable[[], str | None] | None = None) -> dict[str, str]:
    """Stage (or pin) for this attempt and return the runner's variables.

    Raises only `graph_ticket.GraphTicketFenced`, the fence (invariant 5);
    every other failure is a reason, and `{}` at worst.

    `graph_ticket`, `private_dir`, `backend`, `service_url` and `probe` are the
    service path's (§4.1): the attempt's `GraphTicket`, `private/`, the
    backend owner Q2 keeps on staging, the worker's `SWARM_GRAPH_URL` and the
    probe (`graph_ticket.probe` when None). Used only when the stored
    `graph_delivery` says `service`.

    `{}` for an index run (it builds the graph; it does not read one), a
    runner that names no MCP server, or a step with no checkout.

    `mark` receives `ready_detail(result)` once, when the stored setting
    asked for context on this step (`StageResult.wanted`): the steps where a
    graph was to be delivered, staged or not, which is the denominator of
    §8.2's fallback rate. Arm A and `off` deliver nothing by decision and are
    not readings. A mark that fails costs only the mark.

    `secret_env_names` is the task profile's `secrets`, handed on to Jev's
    setup so its client drops every string holding one (§5.4 step 4).

    `additional_repositories` is the task's SIGNED list (§4.9), never a
    caller's, and `repos_dir` where its checkouts are (`work/repos` when
    None): each entry's graph is pinned and served beside the step's own,
    or staged (`prepare_additional`). The returned variables are the step's
    own: the shim finds the additional pins under the context directory.

    `graph_context` is the attempt's `graphroute.GraphContextRoute`: where an
    unswitched tenant's step reads its own repository's registration,
    versions and settings, so the worker lists no Firestore documents
    (lane GRAPH-STAGING). None reads by document id.

    `forge_token` is the tenant's forge token, asked for only when the
    ancestry proof has to deepen the clone (`history_fetcher`, lane
    GRAPH-ANCESTRY); None fetches anonymously, which proves a public
    repository's steps and leaves a private one's a reason. It is used only
    with a `private_dir` (lane GRAPH-TOKEN-2563): never written beside the
    context dir the agent reads.

    `dispatch` is the task's SIGNED `metadata.dispatch` block (the worker
    verified the spec before this runs); only its workflow switches are read
    (`switch_of`): `code_intelligence` here, `jev_judgments` by the judge's
    setup, which asks `jev_key` for the workspace's TypeSafe key under `on`
    (`judge.attempt.setup`). `stage.json` records both (`SWITCHES_FIELD`). A
    step whose `code_intelligence` switch is `off` gets no graph tool at all:
    nothing staged or pinned, no ticket, no additional repository's graph.
    """
    try:
        if profile_name == indexrun.INDEXER_PROFILE or not offers_graph(runner_argv) \
                or checkout is None or not repository_url:
            return {}
        context_dir = work / CONTEXT_DIR_NAME
        if context_dir.is_symlink() or context_dir.is_file():
            context_dir.unlink()
        context_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        service = ServiceRoute(ticket=graph_ticket, private_dir=private_dir, backend=backend,
                               url=service_url, probe=probe)
        switch = switch_of(dispatch, SWITCH_CODE_INTELLIGENCE)
        result = stage(db=db, store=store, tenant_id=tenant_id, repository_url=repository_url,
                       task_id=task_id, checkout=checkout, base_sha=base_sha,
                       context_dir=context_dir, call_options=call_options, service=service,
                       graph_context=graph_context, switch=switch,
                       # The token only ever lands in the worker's own
                       # `private_dir`: with none, the fetch is anonymous and
                       # its logs go to the context dir, which then holds no
                       # credential to leak (lane GRAPH-TOKEN-2563).
                       fetch_history=history_fetcher(
                           repository_url, private_dir if private_dir is not None
                           else context_dir,
                           token=forge_token if private_dir is not None else None))
        if not result.served:
            # A restored checkpoint's pin is not this attempt's decision either.
            remove_pin(context_dir)
        if not result.staged:
            stale = context_dir / SNAPSHOT_DIR_NAME
            if stale.is_symlink() or stale.is_file():
                stale.unlink()
            elif stale.exists():
                # A restored checkpoint's snapshot from an earlier attempt is
                # not this attempt's decision; never left for the server.
                shutil.rmtree(stale, ignore_errors=True)
        if log is not None:
            log.info("graph context", staged=result.staged, reason=result.reason,
                     arm=result.decision.arm if result.decision else None,
                     delivery=result.delivery, seconds=result.seconds,
                     bytes=result.facts.get("bytes"), blobs=result.facts.get("blobs"),
                     fallback=result.fallback, route=result.route)
        prepare_additional(
            # The switch `off` runs no graph, so no additional repository's
            # either: `prepare_additional` still clears what a checkpoint left.
            context_dir, entries=None if switch == OFF else additional_repositories,
            primary_unserved=None if result.served else (result.fallback or result.reason),
            db=db, store=store, tenant_id=tenant_id, task_id=task_id,
            repos_dir=repos_dir if repos_dir is not None else work / REPOS_DIR_NAME,
            call_options=call_options, graph_ticket=graph_ticket, private_dir=private_dir,
            backend=backend, service_url=service_url, probe=probe, log=log)
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

        judgments: dict[str, Any] = {}
        jev = judge_attempt.setup(work=work, db=db, tenant_id=tenant_id,
                                  repository_url=repository_url, call_options=call_options,
                                  log=log, secret_env_names=secret_env_names,
                                  switch=switch_of(dispatch, judge_attempt.SWITCH),
                                  resolve_key=jev_key, record=judgments)
        # What this attempt ran with, and why (2026-11-01): the experiment
        # proves each step's condition from this record.
        (context_dir / STAGE_RECORD_NAME).write_text(
            json.dumps({**result.record(), SWITCHES_FIELD: {
                SWITCH_CODE_INTELLIGENCE: result.switch_record(switch),
                judge_attempt.SWITCH: {k: v for k, v in judgments.items() if k != "switch"},
            }}, indent=2, default=str) + "\n", encoding="utf-8")
        if result.served and result.pin is not None and result.ticket_file is not None:
            return {CONTEXT_ENV: str(context_dir), PIN_ENV: str(result.pin),
                    TICKET_FILE_ENV: str(result.ticket_file), URL_ENV: str(service_url),
                    **jev}
        if result.staged and result.snapshot is not None:
            return {CONTEXT_ENV: str(context_dir), SNAPSHOT_ENV: str(result.snapshot), **jev}
        if result.wanted:
            return {UNSTAGED_ENV: result.reason, **jev}
        return dict(jev)
    except graph_ticket_mod.GraphTicketFenced:
        raise
    except Exception as exc:  # noqa: BLE001 -- a staging failure never fails the attempt
        if log is not None:
            log.warning("graph context not staged", error=type(exc).__name__)
        return {}


def _clear(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)


def prepare_additional(context_dir: Path, *, entries: Sequence[Mapping[str, Any]] | None,
                       primary_unserved: str | None, db: Any, store: Any, tenant_id: str,
                       task_id: str, repos_dir: Path,
                       call_options: Mapping[str, Any] | None = None,
                       graph_ticket: Any = None, private_dir: Path | None = None,
                       backend: str | None = None, service_url: str | None = None,
                       probe: Callable[[str, Path], str | None] | None = None,
                       log: Any = None) -> list[dict[str, Any]]:
    """Pin and serve, or stage, each signed additional repository's graph
    (§4.9); return `[{name, delivery, graph, reason}]`, one per entry. `graph`
    is whether one was delivered: what `repositories.json` says (§7 Q7).

    `context_dir/repos/` is emptied first, whatever `entries` is: a pin or
    snapshot a restored checkpoint carried is not this attempt's.
    `primary_unserved` is why the step's own graph was not served, None when
    it was: an additional graph is served only through that graph's shim, so
    it is staged instead. An entry not served has its ticket upkeep
    discarded, so the control poll never renews a ticket nothing reads.
    Raises only GraphTicketFenced.
    """
    root = context_dir / REPOS_CONTEXT_DIR_NAME
    _clear(root)
    if not entries:
        return []
    try:
        listed = list(entries)
        check_additional_repositories(listed)
    except (TypeError, ValueError):
        # The worker verified the signature over this list; a list the
        # contract refuses is still read as none rather than in part.
        if log is not None:
            log.warning("additional repositories' graphs not staged", code="malformed_list")
        return []
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for entry in listed:
        name = entry["name"]
        directory = root / name
        directory.mkdir(mode=0o700)
        try:
            result = _stage_additional(
                entry, directory=directory, primary_unserved=primary_unserved, db=db,
                store=store, tenant_id=tenant_id, task_id=task_id, checkout=repos_dir / name,
                call_options=call_options, graph_ticket=graph_ticket,
                private_dir=private_dir, backend=backend, service_url=service_url,
                probe=probe)
        except graph_ticket_mod.GraphTicketFenced:
            raise
        except Exception as exc:  # noqa: BLE001 -- an entry never fails the attempt
            result = StageResult(False, f"staging failed ({type(exc).__name__})")
        if not result.served:
            remove_pin(directory)
            extra = graph_ticket.additional.get(name) if graph_ticket is not None else None
            if extra is not None:
                extra.discard()
        delivered = result.served or result.staged
        record = {**result.record(), "name": name, "repository": entry["repository"],
                  "commit": entry["commit"], "graph": delivered}
        (directory / STAGE_RECORD_NAME).write_text(
            json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
        if log is not None:
            log.info("graph context", repository=name, delivery=result.delivery,
                     reason=result.reason, seconds=result.seconds,
                     bytes=result.facts.get("bytes"))
        out.append({"name": name, "delivery": result.delivery, "graph": delivered,
                    "reason": result.reason})
    return out


def _stage_additional(entry: Mapping[str, Any], *, directory: Path,
                      primary_unserved: str | None, db: Any, store: Any, tenant_id: str,
                      task_id: str, checkout: Path, call_options: Mapping[str, Any] | None,
                      graph_ticket: Any, private_dir: Path | None, backend: str | None,
                      service_url: str | None,
                      probe: Callable[[str, Path], str | None] | None) -> StageResult:
    where = indexrun.target(tenant_id, entry["repository_url"])
    if isinstance(where, str) or where.repo_id != entry["repo_id"]:
        return StageResult(False, "the entry's repo_id is not this tenant's registration of "
                                  "its repository")
    unserved = None
    if primary_unserved is not None:
        unserved = ("an additional repository's graph is served only beside the step's "
                    f"own, which was not served ({primary_unserved.rstrip('.')[:200]})")
    upkeep = graph_ticket.for_repository(entry["name"], entry["repo_id"]) \
        if graph_ticket is not None and unserved is None else None
    service = ServiceRoute(ticket=upkeep, private_dir=private_dir, backend=backend,
                           url=service_url, probe=probe)
    # A link at work/repos/<name> to the cache checkout (§4.4); the proof runs
    # in it, or on swarm-api's recorded compare when it is not there.
    present = checkout if checkout.is_dir() else None
    return stage(db=db, store=store, tenant_id=tenant_id,
                 repository_url=entry["repository_url"], task_id=task_id, checkout=present,
                 base_sha=entry["commit"], context_dir=directory, call_options=call_options,
                 service=service, unserved=unserved)


def server_argv(snapshot: str, workdir: str | None) -> list[str]:
    """The command a CLI runs as `swarm-graph`: this interpreter, `-I` so a module
    the agent wrote in the checkout is never what runs (`children.git_shim_argv`)."""
    argv = [sys.executable, "-I", "-m", SERVER_MODULE, "--snapshot", snapshot]
    if workdir:
        argv += ["--workdir", workdir]
    return argv


def shim_argv(pin: str, ticket_file: str, workdir: str | None) -> list[str]:
    """The command a CLI runs as `swarm-graph` when the graph is served (§4.2):
    the shim, under this interpreter with `-I`, as `server_argv`. It names no
    host, bucket or commit; the URL is the server entry's `env`."""
    argv = [sys.executable, "-I", "-m", SHIM_MODULE, "--pin", pin, "--ticket-file", ticket_file]
    if workdir:
        argv += ["--workdir", workdir]
    return argv


def served_env(environ: Mapping[str, str]) -> tuple[str, str, str] | None:
    """`(pin, ticket file, service URL)` when the worker served the graph, else None.

    The runners' one reading of the service path: both paths set, and the URL
    the worker's setting or the shim's in-cluster default.
    """
    pin_path = (environ.get(PIN_ENV) or "").strip()
    ticket = (environ.get(TICKET_FILE_ENV) or "").strip()
    if not pin_path or not ticket:
        return None
    url = (environ.get(URL_ENV) or "").strip() or DEFAULT_SERVICE_URL
    return pin_path, ticket, url


#: `graphmcp.shim.DEFAULT_URL`, the in-cluster Service (§4.2); also
#: `config.DEFAULT_GRAPH_SERVICE_URL`. Held equal by the tests.
DEFAULT_SERVICE_URL = "http://swarm-graph.swarm-system.svc.cluster.local:8080/mcp"


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
    "CONTEXT_ENV", "CONTEXT_FIELD", "DEFAULT_AB", "DEFAULT_CONTEXT", "DEFAULT_GRAPH_DELIVERY",
    "DEFAULT_SERVICE_URL", "DELIVERY_FIELD", "DELIVERY_NONE", "DELIVERY_SERVICE",
    "DELIVERY_SETTINGS_COLLECTION", "DELIVERY_STAGED", "Decision", "GRAPH_RUNNERS",
    "MIN_FORMAT", "PIN_ENV", "PIN_MANIFEST_NAME", "PIN_NAME", "READY_MARK",
    "REPOSITORY_CONTEXT_FIELD", "REPOS_CONTEXT_DIR_NAME", "REPOS_DIR_NAME", "SERVER_NAME",
    "SETTINGS_COLLECTION", "SHIM_MODULE", "SOURCE_SWITCH", "SWITCHES_FIELD",
    "SWITCH_CODE_INTELLIGENCE",
    "SNAPSHOT_DIR_NAME", "SNAPSHOT_ENV", "STAGE_RECORD_NAME", "STAGING_BACKENDS",
    "ServiceRoute", "StageResult", "TICKET_FILE_ENV", "UNSTAGED_ENV", "URL_ENV",
    "assign_arm", "checked_manifest", "decide", "delivery_of", "history_fetcher",
    "is_ancestor", "is_shallow", "offers_graph",
    "pin", "prepare", "prepare_additional", "prove_ancestry", "ready_detail", "remove_pin",
    "server_argv", "switch_of", "switches_summary",
    "served_env", "shim_argv", "stage", "unstaged_note", "write_snapshot",
]
