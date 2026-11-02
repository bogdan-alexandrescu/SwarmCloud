"""The pre-publish gate: rebase on the current base, then the repository's own checks (SPEED-D1).

WHY (owner decision 2026-10-14, delivery speed plan D1 "Pre-publish gate").
Measured over the last 150 merged pull requests: 0.52 red CI runs per pull
request -- checkov, shellcheck, the guards, the build mirror -- and 128 of 150
needed an update from main before they could merge. The agent was TOLD to run
its area tests, `scripts/changed-guards.sh` and `scripts/publish-scan.sh`, and
nothing held it to that; and its branch was cut from the commit the lane
started on, however far main had moved since. So, after the agent's turn ends
and while its session can still be continued, the claude-code runner
(`runners.cliagent.run_cli_agent`, `CliAgentSpec.prepublish`):

1. REBASES the lane's commits onto the CURRENT tip of the base branch, through
   the git tools' spool (`git_rebase_onto`, `agent_worker.gittools`): the
   container holds no forge token, and the worker that answers the spool
   fetches through swarm-api. The worker names the branch in
   `PREPUBLISH_BASE_ENV` only when a rebase is safe for this step
   (`lifecycle.Worker._prepublish_rebase_ref`): an author that opens its own
   pull request, cloned from the repository's branch, with no carrier branch,
   continuation or base pin that a moved base would break. On a CONFLICT the
   session is resumed for ONE turn naming the conflicting files; a rebase
   still in progress after it is aborted, and the branch is published on its
   original base -- never a conflicted one. The worker's publish then diffs
   from the commit the rebase landed on (`gittools.GitTools.rebased_onto`,
   `lifecycle.Worker._adopt_rebase_base`), so main's newer commits are not
   republished as the lane's.
2. RUNS A FIXED CHECK SET, LIMITED TO WHAT THE DIFF TOUCHES (`select_checks`):
   `scripts/changed-guards.sh` for any change; checkov over changed
   `terraform/` files and over the rendered manifests when `kubernetes/`
   changed; tflint over changed Terraform; `shellcheck -x` over changed
   shell scripts; `ruff format --check` and `ruff check` over changed Python
   when the repository configures ruff; and, when an image or the build
   mirror changed, the build mirror's manifest-vs-Dockerfile tests --
   `scripts/refresh-build-mirror.sh --check` needs gcloud and the mirror
   bucket, which this container has no credential for.
3. On a failure, resumes the session for ONE fix turn with each failing
   check's output, then runs the set again. When that fix turn itself broke
   a check that had passed (F07, #2379: vitest failed, the fix turn cleared
   it and broke tsc), ONE more turn with what fails now, then the set again
   -- only with `MIN_GATE_SECONDS` of the step's budget left, the bound the
   gate starts under. Never a third.
4. If anything still fails, publishes anyway -- work is never dropped -- and
   writes `PR_SECTION_NAME` into the artifacts directory, which the worker
   appends to the pull request's body under "Pre-publish checks failed"
   (`lifecycle.Worker._prepublish_section`), so the operator sees it.

Every check's name, status, duration and the files it read go into the
runner's result (`result_summary.runner.output.prepublish`); the output goes
only into the fix prompt and that section.

THE TESTS THE GRAPH SAYS COVER THE CHANGE (CTXQ-G, docs/design/context-quality.md
§4.6). §3 of that design read the root cause of every red run over the last
150 lane PRs: 8 were a covering test nobody ran (class T), and 14 a guard or
house rule not met (class G) -- and `changed-guards.sh` is a grep that runs no
vitest, no `tsc` and no `terraform fmt`. So the check set also holds:

* `graph-pytest` and `vitest`: the test files that cover the changed files,
  read from the graph snapshot staged beside the checkout
  (`CONTEXT_DIR_NAME`/`SNAPSHOT_DIR_NAME`, the `tests` layer `swarm-graph`'s
  `tests_for` answers from), plus the changed test files themselves, plus a
  script's sibling `X.test.tsx` -- the one selection that needs no graph, so
  an unstaged step still runs it. Python files `changed-guards.sh --list`
  already selected are not run twice; JavaScript ones run with the
  package's OWN `node_modules/.bin/vitest`, from the package, so its config
  applies. Blocking, with the same one fix turn as every other check.
* `tsc`, when a TypeScript file of a package with a `tsconfig.json` changed
  (the package's `typecheck` script's arguments, with its own tsc; a Next.js
  package's own `next typegen` first, #1856),
* `node-tests`, a changed package's own `npm run test:node` when its
  package.json declares one (#2455): its node:test `.mjs` files run nowhere
  else before publish, and four lanes went red in CI's ui-build on one that
  checks no source names the company (#2429, #2430, #2433, #2437), and
* `terraform fmt -check` over changed `.tf` files (#1217).

AND AN ADVISORY SECTION NO MERGE PATH READS (`ADVISORY_SECTION_NAME`, appended
to the pull request by `lifecycle.Worker._prepublish_advisory`): test reach
(M11) -- the changed symbols with no covering test edge after the change --
the agent's `self-check.md`, or a line saying it wrote none, and what the
graph selected. Auto-merge, the review verdict and the requirements never
read it (§4.1 "Advisory signals never decide a merge"). A step with no
snapshot says reach is unknown; it never says every symbol is covered.

J4, JEV'S TEST GAP (lane CTXQ-JW, §4.6 item 5, §5.2), only when the worker
set Jev up for this attempt (`judge.attempt`; the workspace opted in). The
changed symbols the graph knows, each with its hunk, and the tests that
cover the changed files go to Jev in one call: "which of these tests would
fail if this change were reverted?". The ones it answers `none` for are
listed in the same advisory section under "Advisory signals (Jev, not a
gate)". Advisory and all-or-nothing: a skipped, failed, timed-out or
malformed call adds nothing, so the section is byte for byte the local-only
one, and no merge path reads either.

THE GUARD SCRIPT RUNS ONCE PER TREE (GUARDS-DEDUP, part of #1694). Measured
over 74 implement lanes on 2026-10-19/20: the agent ran `changed-guards.sh`
and this gate ran it again on the same code in 73 of them (4.9 h), and five
lanes hit its 600 s limit in BOTH rounds, with a fix turn between that cannot
fix a timeout. So:

* the script records how each run ended, with the tree it ran on and its base
  commit, under the git directory (`GUARDS_RECORD_DIR`). The gate computes
  the tree ITSELF (`worktree_tree`: HEAD plus every uncommitted and untracked
  file, the tree the publish commits) and skips its run only when a record
  says `passed` on that tree from the same base, for the whole selection or
  for every part of one `--shard k/n` split (`guards_pass_on`). The agent's
  word is never read: a record naming another tree or base, or any status but
  `passed`, runs the script. The skip and its reason go into the advisory
  section of the pull request.
* a guard run that TIMED OUT (`guards_timed_out`: the script's `ran past` line,
  or this runner's kill) is reported as a timeout, is left out of the fix
  prompt, and runs once more as the parts the script advised
  (`guards_parts`), each its own command under the check limit -- never
  as-is. Parts that time out too are published as a timeout.

A CHECK THAT CANNOT RUN IS NOT A FAILURE. A tool missing from the image,
`tflint --init` that cannot reach its plugin source, an unknown base: the check
is `skipped` (or `error`) with the reason, recorded, and never sent to the
agent as something to fix. So is graph-pytest when the container lacks a
service the tests need (#1605: product-oracle's fixtures migrate into a
Postgres at 127.0.0.1:5432, and every test errored at setup): read from
pytest's junitxml report, when no test passed or failed on its own and every
error is a setup or collection error naming a refused connection to a local
address (`unreachable_services`). Anything else -- one assertion failure, one
pass, an ImportError, a fixture bug -- stays failed. tflint is the expected case: agent-runtime-base
installs it only with `INSTALL_TFLINT=1`, which is off because no tflint
release passes the image promote gate (images/agent-runtime-base/Dockerfile).

A CHECK GETS WHAT IS LEFT OF THE STEP WHEN IT STARTS (#2274). Each check's
limit is `min(CHECK_TIMEOUT_SECONDS, deadline - now - CHECK_MARGIN_SECONDS)`,
read from the clock as it starts, never one value computed for the round: with
640 s left after the agent's end_turn, three checks each given that one 610 s
limit ran ~1,800 s, the runner was SIGKILLed at the step's deadline, and no
pre-publish record was written. A check the budget cannot cover (under
`MIN_CHECK_SECONDS` left) is not started: it and every check after it are
`skipped` for budget -- advisory, never sent to the agent to fix.

ANY CHECK THE RUNNER KILLED AT ITS LIMIT IS A TIMEOUT (#2503), not only the
guard script: graph-pytest killed at `CHECK_TIMEOUT_SECONDS` was sent to the
agent as a failure to fix. A check whose output ends in `run_command`'s
`[timed out after Ns]` marker is `timed_out` and left out of the fix prompt --
unless its output shows a failure of its own before the kill
(`failed_before_timeout`), which the agent can fix and so still reads. Only a
timed-out GUARD run is split into parts; any other runs again as-is after a
fix turn, under the same limit. The fix prompt names the budget left.

AFTER A MERGE OF MAIN, THE BASE IS THE MERGE BASE (#1694). A fix or
conflict-fix step clones its branch at the pre-merge head (`SWARM_CLONE_BASE`)
and merges origin/main in; diffing from the clone base counted every file main
had changed as the lane's (PR 1696: 59 of main's files selected 154 test
files, and the guard script ran out of time with no test failing). When no
pre-publish rebase moved the base and HEAD holds commits of the default branch
the clone base lacks, the gate diffs from `git merge-base <default> HEAD`, what
the pull request shows (`merged_main_base`); the guard script and the related
tests take that base through their `--base`.

A DECLARED SERVICE'S TESTS RUN FOR REAL (TS-GATE, #1627;
docs/design/test-services.md §5, owner decisions of 2026-10-23 in §10). A
repository that checks in `.swarm/test-services.toml` (`agent_worker.testservices`)
has its database tests judged, not skipped. Each round (`Gate._services`):

1. the declaration is read from the tree being checked (§2.3). Invalid: a
   `test-services` check, `failed` -- one fix turn -- when the change touched
   the file, else `error`, in the advisory section only and never sent to the
   agent; nothing is started and the tests run as if nothing were declared.
2. only when a check that runs pytest (`SERVICE_CHECKS`) is selected, each
   declared service is found answering at its address (`probe_argv`: the
   worker started it after the clone and keeps it for the whole attempt,
   Q4, in ITS process, so the gate asks the address) or, when nothing
   answers -- a declaration the agent's own change added, a service that
   died -- started by the gate for this round. One that does not start is
   `test-services: error` (advisory), and the tests run without it.
3. the checks run with §5.1's variables (`service_env`: `PGHOST`, `PGPORT`,
   `PGUSER`, `PGDATABASE`, `DATABASE_URL`, `SWARM_TEST_SERVICES`), in the
   checks' environment only: the gate's own, which the agent's session
   continues with, never carries them.
4. after each pytest check the services are probed (`watched`): one that
   stopped during the run makes it `error`, with the exit status when the
   gate started it (signal 9 is the OOM killer, which ranks it first) -- no
   fix turn fixes a dead server.
5. what the gate started is stopped and its data removed after the round, so
   a fix round starts afresh; the worker's own runs on.

And `unreachable_services` stays the one judge of "every test was refused",
its addresses now matched against the declared ones (`_pytest_project`): an
address a running declared service owns is the tests connecting wrongly,
`failed`; any other is `skipped`, adding that it was not declared. With no
file nothing here runs, and the check is exactly as PR 1606 made it.

THE CHECKS LEAVE THE TREE AS THEY FOUND IT (PREPUB-UV-OUTSIDE, part of #1964).
Measured 2026-10-22 on sagaxyz/ai-studio (task_2df71fc1d5d34997957a,
task_65d0f0328cc6464f8358): graph-pytest's `uv run pytest`, run from
`runner/` inside the clone, created `runner/.venv` and -- that project had no
lock -- `runner/uv.lock`, and the publish's `git add --all` committed the
1,659-line lock into both pull requests; the venv turned that repository's
own pin guards red during the check. So:

* every `uv` a check runs gets `UV_PROJECT_ENVIRONMENT` in a directory of its
  own per project directory, under a temporary root outside the checkout that
  is removed after the round (`uv_outside`, in `run_checks`). Only that
  command's environment carries it: the gate's, which the agent's session
  continues with, never does. `UV_CACHE_DIR` stays as cachedirs.py set it.
* around each round, the working tree is recorded (`record_tree`: `git status
  --porcelain=v2 --untracked-files=all` and a content hash of every listed
  path) and, after it, every path a check created or changed is put back
  (`restore_tree`): a new untracked path removed, a clean tracked path
  restored from HEAD, a path the agent had changed given back the content it
  had. The agent's own changes -- recorded before -- are untouched, a fix
  turn's are recorded by the next round, and what was restored is logged and
  named in the advisory section. This holds for any tool, not only uv; what
  the checks run and decide is unchanged.

NOTHING SECRET-SHAPED LEAVES. The checks run without the provider credential
in their environment, and every byte of output is scrubbed of the runner's
own secrets and then of every credential pattern (`swarm_redaction.redact`)
before it reaches a prompt, a log or the pull request.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from xml.etree import ElementTree

from swarm_redaction import redact as redact_text

from . import testservices
from .graphmcp.snapshot import Snapshot, SnapshotError, load_freshness_record, path_of
from .redact import scrub_text

#: Set by the worker to the base BRANCH the lane is rebased onto, and only when
#: that is safe for this step (`lifecycle.Worker._prepublish_rebase_ref`).
#: Unset: no rebase, and the checks diff from the clone base.
PREPUBLISH_BASE_ENV = "SWARM_PREPUBLISH_BASE"
#: The instant (epoch seconds) the worker kills the runner at, exported by
#: `lifecycle._build_child_env` (#2981): the gate's budget never runs past it.
RUNNER_DEADLINE_ENV = "SWARM_RUNNER_DEADLINE_EPOCH"
#: `lifecycle.CLONE_BASE_ENV`, restated because this module is imported by the
#: runner, which never imports the worker.
CLONE_BASE_ENV = "SWARM_CLONE_BASE"
#: `gitops.EMPTY_CLONE_BASE`, restated for the same reason.
EMPTY_BASE = "empty"
#: The artifact the worker appends to the pull request's body.
PR_SECTION_NAME = "prepublish-checks.md"
PR_SECTION_HEADING = "## Pre-publish checks failed"

#: One fix turn and one conflict turn (owner decision 2026-10-14): a second
#: start rarely clears what one named failure did not, and each costs a turn.
#: The exception is a check the fix turn itself broke after it had passed
#: (owner decision 2026-10-26, #2379 F07): that failure was never named to
#: the agent, so it gets one more turn, and only one (`Gate._second_turn`).
FIX_TURNS = 1
CONFLICT_TURNS = 1
#: Below this much of the step's budget the gate does not start: a check set
#: that cannot finish would only turn a success into a timeout.
MIN_GATE_SECONDS = 120.0
#: Any one check. changed-guards.sh holds itself under ten minutes.
CHECK_TIMEOUT_SECONDS = 600.0
#: Kept back from the step's deadline by every check (#2274): after the last
#: check the gate still restores the tree, measures test reach and writes its
#: record and sections, and the runner uploads the transcript, all before the
#: step's SIGKILL.
CHECK_MARGIN_SECONDS = 30.0
#: How long the output of a check killed at its limit is read for before its
#: pipes are closed (`_drain`): a killed group's output is already written, so
#: seconds only, and never long enough to eat `CHECK_MARGIN_SECONDS`.
KILL_DRAIN_SECONDS = 5.0
#: Below this much budget left a check is not started but skipped for budget:
#: even the quickest check (shellcheck or ruff on a few files) needs seconds to
#: start, and one killed at its limit is only a timeout nobody can fix.
MIN_CHECK_SECONDS = 10.0
#: How long the runner waits for the worker to answer the rebase. The worker
#: answers on its control poll; a fetch of a large repository is the slow case.
REBASE_WAIT_SECONDS = 300.0
#: Output kept per failing check in a prompt or the pull request, from the
#: END (where pytest, checkov and shellcheck summarise), and for the section.
OUTPUT_TAIL_CHARS = 3000
SECTION_MAX_CHARS = 16000

#: The commands every check may use, and the variables they never see.
_SHELL_SUFFIXES = (".sh", ".bash")
_PY_SUFFIX = ".py"
_K8S_SKIP = "CKV_K8S_14,CKV_K8S_15,CKV_K8S_43"
#: What `tflint` exits with when it found issues (0 none, 1 it could not run).
_TFLINT_ISSUES = 2

#: Where the step's context is staged (docs/design/context-quality.md §4.4):
#: beside the checkout, never in it, so the publish never commits it. The
#: worker may name another directory in `CONTEXT_DIR_ENV`. Not `SWARM_CONTEXT`:
#: swarm-mcp already reads that name as a named API context (`swarm_mcp.config`).
CONTEXT_DIR_NAME = ".swarm-context"
SNAPSHOT_DIR_NAME = "snapshot"
CONTEXT_DIR_ENV = "SWARM_CONTEXT_DIR"
#: The advisory section the worker appends to the pull request's body, read by
#: people and by no merge path; `lifecycle.PREPUBLISH_ADVISORY_*` restates both.
ADVISORY_SECTION_NAME = "prepublish-advisory.md"
ADVISORY_SECTION_HEADING = "## Advisory signals (not a gate)"
ADVISORY_MAX_CHARS = 8000
#: The agent's own checklist answers, in the artifacts directory (§4.6 item 3).
SELF_CHECK_NAME = "self-check.md"
SELF_CHECK_MAX_CHARS = 3000
#: Symbols without a covering test listed by name; the rest are counted.
REACH_SHOWN = 30
#: J4's PR-body heading (§4.6 item 5): read by people, by no merge path.
JEV_SECTION_HEADING = "### Advisory signals (Jev, not a gate)"
#: J4 asks about at most this many changed symbols in its one call: the API's
#: own cap on questions in one request (`judge.client.MAX_QUESTIONS`).
J4_MAX_SYMBOLS = 64
#: Test files one graph check runs. Past this the graph's ranking is too wide
#: to be a selection, and CI runs the whole suite anyway.
GRAPH_TESTS_MAX = 60
_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
_JS_TEST = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mts|cts|mjs|cjs)$")
_TF_SUFFIX = ".tf"
#: The files whose symbols test reach counts: the languages the indexer parses.
_CODE_SUFFIXES = (_PY_SUFFIX, *_JS_SUFFIXES, ".go", _TF_SUFFIX)
#: `terraform fmt -check` exits 3 when a file is not formatted.
_FMT_UNFORMATTED = 3

PASSED, FAILED, SKIPPED, ERROR = "passed", "failed", "skipped", "error"

#: The guard script's check, and where the script records its runs, under the
#: git directory (`RECORD_DIR_NAME` in scripts/changed-guards.sh).
GUARDS_CHECK = "changed-guards"
GUARDS_SCRIPT = "scripts/changed-guards.sh"
GUARDS_RECORD_DIR = "swarm-changed-guards"
#: What a timed-out guard run prints: the script's own pytest deadline, or
#: `run_command`'s kill at the check limit.
_GUARDS_TIMEOUT = re.compile(r"ran past \d+ s|\[timed out after \d+s\]")
#: The split the script advises: `--shard 1/<n>`.
_GUARDS_SHARDS = re.compile(r"--shard 1/(\d+)")
#: Parts to split a timed-out run into when its output advised none: the
#: script's own minimum (`parts` in changed-guards.sh).
GUARDS_MIN_PARTS = 2

#: The check that reports `.swarm/test-services.toml` and the services it
#: names, when there is something to report (TS-GATE, #1627; design §5.3).
TEST_SERVICES_CHECK = "test-services"
#: §5.1: the services that answered for this round, comma-separated, so a
#: conftest can tell. Also how `_pytest_project` knows which refused
#: addresses a declared service owns.
TEST_SERVICES_ENV = "SWARM_TEST_SERVICES"
#: §5.2 step 2: the checks that run pytest, the ones a service is started for.
SERVICE_CHECKS = ("graph-pytest", GUARDS_CHECK)
#: Each catalogue service's port on `testservices.LISTEN_HOST`. A refused
#: local address on one of these ports, while the service ran, is the
#: tests connecting wrongly, not a service the container lacks (§5.3 row 4).
SERVICE_PORTS = {"postgres": testservices.LISTEN_PORT}
#: One `pg_isready` over TCP: `-t` bounds its own wait, this the command.
SERVICE_PROBE_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Running a command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ran:
    code: int
    output: str
    timed_out: bool = False
    #: stderr, when the command was run with it kept apart (`run_split`).
    errors: str = ""


Runner = Callable[[Sequence[str], Path, Mapping[str, str], float], Ran]


def run_command(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float,
                *, merge_stderr: bool = True) -> Ran:
    """Run `argv` in its own process group; stdout and stderr together, unless
    `merge_stderr` is false, when stderr is kept apart in `Ran.errors`.

    The group is killed on timeout, so a pytest-xdist worker or a checkov
    subprocess does not outlive the check that started it.
    """
    try:
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return Ran(127, f"{argv[0]}: could not start ({type(exc).__name__})")
    try:
        out, err = proc.communicate(timeout=max(1.0, timeout))
        return Ran(proc.returncode, out.decode("utf-8", errors="replace"),
                   errors=(err or b"").decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = _drain(proc)
        text = out.decode("utf-8", errors="replace")
        return Ran(124, f"{text}\n[timed out after {int(timeout)}s]", timed_out=True,
                   errors=err.decode("utf-8", errors="replace"))


def _drain(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """What a killed check printed, read for at most `KILL_DRAIN_SECONDS` (#2981).

    The kill reaches the check's whole process group, but a descendant that
    left it (`setsid`: a daemonised test service, a browser) still holds the
    output pipe open, and an unbounded `communicate()` waits on it for as long
    as it lives -- the gate would never return and the runner would be killed
    at the step's deadline with no record. So the read is bounded, then the
    pipes are closed and the check is reaped."""
    try:
        out, err = proc.communicate(timeout=KILL_DRAIN_SECONDS)
        return out or b"", err or b""
    except subprocess.TimeoutExpired as exc:
        out, err = exc.output or b"", exc.stderr or b""
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    note = b"\n[a process the check started outlived its kill and held its output open]"
    return bytes(out) + note, bytes(err)


def run_split(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Ran:
    """`run_command` with stderr kept apart: for a command whose stdout is a
    file another tool parses."""
    return run_command(argv, cwd, env, timeout, merge_stderr=False)


# ---------------------------------------------------------------------------
# What the diff touches
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, runner: Runner, env: Mapping[str, str]) -> Ran:
    return runner(["git", "-C", str(repo), *args], repo, env, 60.0)


def diff_base(repo: Path, base: str, *, runner: Runner, env: Mapping[str, str]) -> str | None:
    """`base` as a commit or tree git can diff from; the empty tree for `empty`."""
    if base == EMPTY_BASE:
        ran = _git(repo, "hash-object", "-t", "tree", "/dev/null", runner=runner, env=env)
        return ran.output.strip() if ran.code == 0 else None
    ran = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{object}}", runner=runner,
               env=env)
    return base if ran.code == 0 else None


def changed_files(repo: Path, base: str, *, runner: Runner = run_command,
                  env: Mapping[str, str] | None = None) -> list[str] | None:
    """Every path the publish would change: committed since `base`, uncommitted
    and untracked -- the worker commits the last two too. None when git could
    not say, which is never read as "nothing changed"."""
    env = dict(os.environ if env is None else env)
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--"),
        ("diff", "--name-only", "--no-renames", "-z", "HEAD", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        ran = _git(repo, *args, runner=runner, env=env)
        if ran.code != 0:
            return None
        paths.update(p for p in ran.output.split("\0") if p.strip())
    return sorted(paths)


#: Where the default branch is looked for, in order, after the pre-publish
#: base branch: the clone's remote HEAD, then the two usual names.
_DEFAULT_REFS = ("refs/remotes/origin/HEAD", "refs/remotes/origin/main",
                 "refs/remotes/origin/master")


def merged_main_base(repo: Path, base: str, *, runner: Runner = run_command,
                     env: Mapping[str, str] | None = None) -> str | None:
    """`git merge-base <default branch> HEAD`, when HEAD holds commits of the
    default branch that `base` lacks -- a merge of main after the clone (#1694)
    -- else None: `base` stands. The one place the rule lives; the guard
    script and the related tests take its answer through `--base`."""
    env = dict(os.environ if env is None else env)
    named = (env.get(PREPUBLISH_BASE_ENV) or "").strip()
    refs = ((f"refs/remotes/origin/{named}",) if named else ()) + _DEFAULT_REFS
    for ref in refs:
        if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                runner=runner, env=env).code != 0:
            continue
        ran = _git(repo, "merge-base", ref, "HEAD", runner=runner, env=env)
        merge_base = ran.output.strip()
        if ran.code != 0 or not merge_base:
            return None
        # The merge base already in `base`: nothing of main came in after it.
        if _git(repo, "merge-base", "--is-ancestor", merge_base, base,
                runner=runner, env=env).code == 0:
            return None
        return merge_base
    return None


# ---------------------------------------------------------------------------
# The check set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One check, chosen for this diff. `run` is None when it cannot run."""

    name: str
    files: tuple[str, ...]
    command: str
    run: Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]] | None = None
    skipped: str | None = None


@dataclass
class Outcome:
    name: str
    status: str
    duration_seconds: float
    files: tuple[str, ...]
    command: str
    output: str = ""
    reason: str | None = None
    #: A failure by running out of time, which no fix turn can fix.
    timed_out: bool = False

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 3),
            "files": len(self.files),
        }
        if self.reason:
            out["reason"] = self.reason
        if self.timed_out:
            out["timed_out"] = True
        return out


def _which(tool: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(tool, path=env.get("PATH"))


def _verdict(ran: Ran, *, ok: Iterable[int] = (0,)) -> tuple[str, str]:
    return (PASSED if ran.code in tuple(ok) and not ran.timed_out else FAILED), ran.output


def _simple(argv: Sequence[str]) -> Callable[[Runner, Path, Mapping[str, str], float],
                                             tuple[str, str]]:
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        return _verdict(runner(argv, repo, env, timeout))
    return run


def guards_timed_out(output: str) -> bool:
    """Whether a guard run's output says it ran out of time."""
    return bool(_GUARDS_TIMEOUT.search(output))


#: `run_command`'s line when it killed a command at its limit, at the end.
_KILLED_AT_LIMIT = re.compile(r"\[timed out after \d+s\]\s*\Z")
#: A failure a check reported before it was killed: pytest's summary line
#: (`FAILED`/`ERROR <test>`) or an F/E in its progress dots, vitest's `FAIL`.
_FAILED_BEFORE = re.compile(
    r"^\s*(?:FAILED|ERROR|FAIL)\s|^[.sxXFE]*[FE][.sxXFE]*(?:\s+\[\s*\d+%\])?\s*$"
    r"|^\S+\.py [.sxXFE]*[FE][.sxXFE]*(?:\s+\[\s*\d+%\])?\s*$",
    re.MULTILINE)


def failed_before_timeout(output: str) -> bool:
    """Whether a killed check had already reported a failure of its own:
    one the agent can fix, so it still goes to the fix turn."""
    return bool(_FAILED_BEFORE.search(output))


def killed_at_limit(output: str) -> bool:
    """Whether a check's output ends in the runner's kill at its limit, with
    no failure of its own before it (#2503)."""
    return bool(_KILLED_AT_LIMIT.search(output)) and not failed_before_timeout(output)


def guards_parts(output: str) -> int:
    """The parts a timed-out guard run should be split into: the largest the
    script advised, else `GUARDS_MIN_PARTS`."""
    advised = [int(n) for n in _GUARDS_SHARDS.findall(output)]
    return max([GUARDS_MIN_PARTS, *advised])


def worktree_tree(repo: Path, *, runner: Runner = run_command,
                  env: Mapping[str, str] | None = None) -> str | None:
    """The tree the publish would commit: HEAD plus every uncommitted and
    untracked file, written from a scratch index, so the checkout's own index
    is untouched. The same computation as THE RECORD in changed-guards.sh;
    None when git could not say."""
    env = dict(os.environ if env is None else env)
    ran = _git(repo, "rev-parse", "--absolute-git-dir", runner=runner, env=env)
    if ran.code != 0:
        return None
    index = Path(ran.output.strip()) / "index"
    with tempfile.TemporaryDirectory(prefix="prepublish-tree-") as tmp:
        scratch = Path(tmp) / "index"
        scoped = {**env, "GIT_INDEX_FILE": str(scratch)}
        if index.is_file():
            shutil.copyfile(index, scratch)
        elif (_git(repo, "read-tree", "HEAD", runner=runner, env=scoped).code != 0
              and _git(repo, "read-tree", "--empty", runner=runner, env=scoped).code != 0):
            return None
        if _git(repo, "add", "-A", runner=runner, env=scoped).code != 0:
            return None
        ran = _git(repo, "write-tree", runner=runner, env=scoped)
    tree = ran.output.strip()
    return tree if ran.code == 0 and tree else None


def guards_pass_on(repo: Path, base: str, *, runner: Runner = run_command,
                   env: Mapping[str, str] | None = None) -> str | None:
    """Why the guard script need not run again, or None: a run recorded as
    `passed` on the tree the publish would commit, from the same base commit,
    for the whole selection or for every part of one `--shard k/n` split.

    The tree and the base are computed here, never read from the agent: a
    record is only ever compared against them."""
    env = dict(os.environ if env is None else env)
    ran = _git(repo, "rev-parse", "--absolute-git-dir", runner=runner, env=env)
    if ran.code != 0:
        return None
    folder = Path(ran.output.strip()) / GUARDS_RECORD_DIR
    if not folder.is_dir():
        return None
    commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}",
                  runner=runner, env=env)
    tree = worktree_tree(repo, runner=runner, env=env)
    if commit.code != 0 or not tree:
        return None
    want = commit.output.strip()
    passed: dict[str, str] = {}
    for path in sorted(folder.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(entry, dict) and entry.get("status") == PASSED
                and entry.get("tree") == tree and entry.get("base") == want):
            passed[str(entry.get("part") or "")] = str(entry.get("finished_at") or "")
    where = f"the same tree ({tree[:12]}) from the same base ({want[:12]})"
    if "all" in passed:
        return (f"the last `{GUARDS_SCRIPT}` run in this checkout passed on {where} "
                f"at {passed['all'] or 'an unrecorded time'}; not run again")
    splits = {part.split("/")[1] for part in passed if re.fullmatch(r"\d+/\d+", part)}
    for n in sorted(splits, key=int):
        if all(f"{k}/{n}" in passed for k in range(1, int(n) + 1)):
            return (f"every part of the last `{GUARDS_SCRIPT} --shard k/{n}` runs in this "
                    f"checkout passed on {where}; not run again")
    return None


#: How long the guard script's `--list` may take: it only selects, never runs
#: a test (`_guards_selection` gives it the same bound).
GUARDS_LIST_SECONDS = 120.0


def guards_split(repo: Path, base: str, *, runner: Runner, env: Mapping[str, str],
                 timeout: float = GUARDS_LIST_SECONDS) -> int:
    """The parts the guard script advises for this selection BEFORE it runs,
    or 0 when one run can hold it (#2981).

    THE THRESHOLD IS THE SCRIPT'S OWN: `changed-guards.sh --list` prints its
    `--shard 1/<n>` hint when the selection weighs more than its
    `SHARD_HINT_WEIGHT` (540 CI-seconds, ~400 s of wall clock in a SwarmCloud
    container, the 540 s pytest limit less a quarter). Unsharded, such a
    selection runs its parts in turn inside ONE command -- up to n x 540 s --
    so under the gate's 600 s check limit it was always killed: measured on
    #2981, every one of 15 steps burned its first 600 s that way (645-681
    files) and only then ran as 10 shards. The weight is read from the
    script, never restated here, so the gate and the script cannot disagree."""
    if not (repo / GUARDS_SCRIPT).is_file():
        return 0
    ran = runner(["bash", GUARDS_SCRIPT, "--base", base, "--list"], repo, env, timeout)
    if ran.code != 0 or ran.timed_out or not _GUARDS_SHARDS.search(ran.output):
        return 0
    return guards_parts(ran.output)


def adds_nothing(repo: Path, base: str, patches: Sequence[str | Path], *,
                 runner: Runner = run_command, env: Mapping[str, str] | None = None
                 ) -> str | None:
    """Why the branch adds nothing of this step's own, or None (#2981).

    Read from git, never from the step's name or stage: the tree the publish
    would commit (`worktree_tree`) is the base's own tree (no commit, no
    change), or exactly the base with the step's staged input patches applied
    (a review, visual QA or acceptance step applies its upstream step's patch
    as one commit and changes nothing else). Those checks already ran on that
    same diff in the step that wrote it, and CI is the gate. A patch that does
    not apply, an unreadable base or any change of the step's own: None, and
    the checks run as before."""
    env = dict(os.environ if env is None else env)
    tree = worktree_tree(repo, runner=runner, env=env)
    base_tree = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{tree}}",
                     runner=runner, env=env)
    if not tree or base_tree.code != 0:
        return None
    if tree == base_tree.output.strip():
        return "the branch changes nothing beyond its base"
    files = [Path(p) for p in patches if Path(p).is_file()]
    if not files:
        return None
    with tempfile.TemporaryDirectory(prefix="prepublish-patch-") as tmp:
        scoped = {**env, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if _git(repo, "read-tree", base_tree.output.strip(), runner=runner, env=scoped).code:
            return None
        for path in files:
            if _git(repo, "apply", "--cached", "--whitespace=nowarn", str(path),
                    runner=runner, env=scoped).code != 0:
                return None
        expected = _git(repo, "write-tree", runner=runner, env=scoped)
    if expected.code != 0 or expected.output.strip() != tree:
        return None
    names = ", ".join(p.name for p in files)
    return (f"the branch is exactly its staged input patch ({names}) applied to its base, "
            "with no change of this step's own: the step that wrote that diff ran these "
            "checks on it, and CI is the gate")


def _sharded_guards(argv: Sequence[str], parts: int, remaining: Callable[[], float]
                    ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    """The guard script as `parts` commands, `--shard 1/n` ... `--shard n/n`,
    each under the check limit: how a timed-out run runs again.

    A passing part is one summary line, its last; the failing and timed-out
    parts follow in full, then the budget note. `clean_output` keeps only
    the END of the output for the fix prompt and the pull request, so a long
    passing part after a failing one cut the failure away (#2927)."""
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        status, passed, failed = PASSED, [], []
        for k in range(1, parts + 1):
            left = remaining()
            if left < MIN_GATE_SECONDS:
                note = f"[the step's budget ran out before part {k} of {parts}]"
                return ERROR, "\n".join([*passed, *failed, note])
            ran = runner([*argv, "--shard", f"{k}/{parts}"], repo, env,
                         max(MIN_CHECK_SECONDS, min(timeout, left - CHECK_MARGIN_SECONDS)))
            if ran.code != 0 or ran.timed_out:
                status = FAILED
                failed.append(f"--- part {k}/{parts} ---\n{ran.output}")
            else:
                last = next((line.strip() for line in reversed(ran.output.splitlines())
                             if line.strip()), "")
                passed.append(f"--- part {k}/{parts} passed --- {last}".rstrip())
        return status, "\n".join([*passed, *failed])
    return run


def _configures_ruff(repo: Path) -> bool:
    if (repo / "ruff.toml").is_file() or (repo / ".ruff.toml").is_file():
        return True
    try:
        return "[tool.ruff" in (repo / "pyproject.toml").read_text(errors="replace")
    except OSError:
        return False


def _tflint(files: Sequence[str]) -> Callable[[Runner, Path, Mapping[str, str], float],
                                              tuple[str, str]]:
    """tflint as CI runs it (`--chdir=terraform --recursive`), failing only on
    an issue in a file this diff changed: an issue elsewhere is not the lane's."""
    names = {f for f in files} | {f[len("terraform/"):] for f in files}

    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        init = runner(["tflint", "--init", "--chdir=terraform"], repo, env, timeout)
        if init.code != 0:
            return ERROR, f"tflint --init exited {init.code}:\n{init.output}"
        ran = runner(["tflint", "--chdir=terraform", "--recursive", "--format=compact"],
                     repo, env, timeout)
        if ran.code == 0:
            return PASSED, ran.output
        if ran.code != _TFLINT_ISSUES:
            return ERROR, f"tflint exited {ran.code}:\n{ran.output}"
        ours = [line for line in ran.output.splitlines()
                if line.split(":", 1)[0].lstrip("./") in names]
        return (FAILED, "\n".join(ours)) if ours else (PASSED, ran.output)
    return run


def _render_and_scan(python: str, *, render: Runner = run_split
                     ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    """`make security`'s Kubernetes half: checkov reads the RENDERED manifests,
    because a raw template's placeholder either reads as an unpinned image or
    makes checkov skip the file and still exit 0 (Makefile `security`).

    Each render runs through `render`, which keeps stderr apart: only stdout
    is the manifest. A merged stream put render.py's warning line into the
    file checkov scanned, which then was not YAML, and every job pod failed
    CKV2_K8S_6 (#1334, PR 1399). What render.py wrote to stderr is reported,
    redacted, as a note beside the scan."""
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        notes: list[str] = []
        with tempfile.TemporaryDirectory(prefix="prepublish-k8s-") as tmp:
            out = Path(tmp)
            listed = runner(
                [python, "-c",
                 "import sys; sys.path.insert(0, 'apps/common'); "
                 "from swarm_common.profiles import RUNNER_PROFILES; "
                 "print(' '.join(sorted(RUNNER_PROFILES)))"],
                repo, env, timeout)
            if listed.code != 0:
                return FAILED, f"listing the runner profiles failed:\n{listed.output}"
            renders = [("policies.yaml", ["policies"]),
                       ("tenant.yaml", ["tenant", "--tenant", "lint"])]
            renders += [(f"job-{p}.yaml",
                         ["job", "--tenant", "lint", "--profile", p, "--task", "tsk_lint",
                          "--attempt", "att_lint", "--lease", "lease_lint", "--generation", "1"])
                        for p in listed.output.split()]
            for name, args in renders:
                ran = render([python, "kubernetes/render.py", *args], repo, env, timeout)
                errors = redact_text(ran.errors).text.strip()
                if ran.code != 0:
                    return FAILED, (f"kubernetes/render.py {' '.join(args)} failed:\n"
                                    f"{ran.output}\n{errors}").rstrip()
                (out / name).write_text(ran.output)
                if errors:
                    notes.append(f"note: kubernetes/render.py {' '.join(args)} wrote to "
                                 f"stderr (not scanned):\n{errors}")
            status, output = _verdict(runner(
                ["checkov", "-d", str(out), "--framework", "kubernetes", "--skip-check",
                 _K8S_SKIP, "--quiet", "--compact"], repo, env, timeout))
            return status, "\n".join([output.rstrip(), *notes]).strip() + "\n"
    return run


def _pytest_files(repo: Path, names: Sequence[str]) -> list[str]:
    return [n for n in names if (repo / n).is_file()]


#: The build mirror's consistency tests: every pin in an image's Dockerfile
#: has its line in the mirror manifest, and every GitHub release an image
#: fetches comes through the mirror.
BUILD_MIRROR_TESTS = (
    "tests/unit/scripts/test_build_mirror.py",
    "tests/unit/scripts/test_build_mirror_switch.py",
)


def _touches_build_mirror(path: str) -> bool:
    return (
        (path.startswith("images/") and (path.endswith("/Dockerfile")
                                         or path.endswith("/tool-mirror.sha256")))
        or path in ("scripts/refresh-build-mirror.sh", "scripts/lib/build-mirror.sh")
    )


# ---------------------------------------------------------------------------
# The graph's covering tests (CTXQ-G)
# ---------------------------------------------------------------------------


def context_dir(repo: Path, env: Mapping[str, str]) -> Path:
    """Where the step's context is staged: `CONTEXT_DIR_ENV`, else beside the checkout."""
    named = (env.get(CONTEXT_DIR_ENV) or "").strip()
    return Path(named) if named else Path(repo).parent / CONTEXT_DIR_NAME


def load_snapshot(directory: Path) -> tuple[Snapshot | None, str]:
    """The staged snapshot under `directory`, and a line saying what was found.

    None when nothing is staged or the snapshot cannot be read as it is: the
    gate then selects without the graph, and says so, rather than failing.
    """
    root = Path(directory) / SNAPSHOT_DIR_NAME
    if not (root / "manifest.json").is_file():
        return None, "no graph snapshot is staged for this step"
    try:
        record = load_freshness_record(root)
        snapshot = Snapshot(root, manifest_digest=(record or {}).get("manifest_digest"))
    except (SnapshotError, OSError, ValueError) as exc:
        return None, f"the staged graph snapshot could not be read ({exc})"
    sha = snapshot.commit_sha[:12] or "an unknown commit"
    return snapshot, f"graph snapshot of {sha} (format {snapshot.format_version})"


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name.endswith(_PY_SUFFIX):
        return name.startswith("test_") or name.endswith("_test.py")
    if not name.endswith(_JS_SUFFIXES):
        return False
    return bool(_JS_TEST.search(name)) or "/__tests__/" in f"/{path}"


def _graph_tests(snapshot: Snapshot, path: str) -> set[str]:
    """The test files the snapshot's `tests` layer says cover `path`: the file's
    own test rows and every one of its symbols' (`graphmcp.tools.tool_tests_for`)."""
    out: set[str] = set()
    try:
        file = snapshot.file(path)
        if file is None:
            return out
        rows = list(file.get("tests") or [])
        for symbol in snapshot.symbols_in(path):
            rows.extend(snapshot.tests(str(symbol.get("id"))))
    except SnapshotError:
        return out
    for row in rows:
        test = str(row.get("test") or "")
        if test:
            out.add(path_of(test))
    return out


def _sibling_tests(path: str) -> list[str]:
    """`X.test.tsx` and `__tests__/X.test.tsx` beside a changed script `X.tsx`."""
    if not path.endswith(_JS_SUFFIXES) or is_test_path(path):
        return []
    directory, _, name = path.rpartition("/")
    stem, _, _suffix = name.rpartition(".")
    prefix = f"{directory}/" if directory else ""
    return [f"{prefix}{where}{stem}.{kind}.{suffix}"
            for where in ("", "__tests__/") for kind in ("test", "spec")
            for suffix in ("ts", "tsx", "js", "jsx")]


def select_tests(repo: Path, files: Sequence[str],
                 snapshot: Snapshot | None) -> dict[str, str]:
    """Each test file that covers `files`, and why: `changed`, `graph` or `sibling`.

    Only files present in the checkout: a test the change deleted, or one the
    graph remembers from before a rename, has nothing left to run.
    """
    chosen: dict[str, str] = {}

    def add(test: str, why: str) -> None:
        if test not in chosen and (repo / test).is_file() and is_test_path(test):
            chosen[test] = why

    for path in files:
        if is_test_path(path):
            add(path, "changed")
    for path in files:
        if is_test_path(path):
            continue
        if snapshot is not None:
            for test in sorted(_graph_tests(snapshot, path)):
                add(test, "graph")
        for test in _sibling_tests(path):
            add(test, "sibling")
    return chosen


def _package_root(repo: Path, path: str, marker: str = "package.json") -> str | None:
    """The nearest directory above `path` holding `marker` (a package.json, or
    a pyproject.toml for a Python project), relative to `repo`: "" for the
    repository root, None when no directory up to the root holds one."""
    parts = path.split("/")[:-1]
    while parts:
        if (repo / "/".join(parts) / marker).is_file():
            return "/".join(parts)
        parts.pop()
    return "" if (repo / marker).is_file() else None


def _node_bin(repo: Path, package: str, tool: str) -> Path | None:
    candidate = repo / package / "node_modules" / ".bin" / tool
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _has_test_node(repo: Path, package: str) -> bool:
    """Whether the package's package.json declares a `test:node` script."""
    try:
        scripts = json.loads((repo / package / "package.json").read_text()).get("scripts")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(scripts, dict) and bool(scripts.get("test:node"))


def _relative(package: str, path: str) -> str:
    return path[len(package) + 1:] if package else path


def _in_package(repo: Path, package: str, binary: Path, args: Sequence[str]
                ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    """Run a package's own tool from the package directory, so its config applies."""
    def run(runner: Runner, root: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        return _verdict(runner([str(binary), *args], Path(root) / package if package else
                               Path(root), env, timeout))
    return run


#: A `typecheck` script that generates Next.js's route types, then type-checks.
_NEXT_TYPEGEN = ["next", "typegen", "&&"]
_TSC_FALLBACK = ["--noEmit", "-p", "tsconfig.json"]


def _typecheck_plan(repo: Path, package: str) -> tuple[bool, list[str]]:
    """Whether the package needs `next typegen` before tsc, and tsc's arguments.

    Next.js writes the `PageProps`/`LayoutProps` route types only during `next
    build` or `next typegen`, so tsc alone on a clean checkout of a Next
    package fails on route files no lane touched: 17 errors on Product
    Oracle's repository, every lane, 2026-10-21 (#1856). So a package that
    depends on `next`, or whose script runs `next typegen`, generates them
    first. tsc's arguments are the package's own `typecheck` script's when it
    is a plain `tsc ...`, or exactly `next typegen && tsc ...`; any other
    compound script falls back to `--noEmit -p tsconfig.json`.
    """
    try:
        manifest = json.loads((repo / package / "package.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    manifest = manifest if isinstance(manifest, dict) else {}
    scripts = manifest.get("scripts")
    words = str((scripts if isinstance(scripts, dict) else {}).get("typecheck") or "").split()
    typegen = "next typegen" in " ".join(words) or any(
        isinstance(deps, dict) and "next" in deps
        for deps in (manifest.get("dependencies"), manifest.get("devDependencies")))
    if words[:3] == _NEXT_TYPEGEN:
        words = words[3:]
    if words and words[0] == "tsc" and not any(w in ("&&", "||", ";", "|") for w in words):
        return typegen, words[1:]
    return typegen, list(_TSC_FALLBACK)


def _typegen_then_tsc(package: str, next_bin: Path | None, tsc: Path, args: Sequence[str]
                      ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    """The package's own `next typegen`, then its tsc, in one check's budget.

    When typegen cannot run (no `next` binary, a Next.js too old to have the
    command, a failure) tsc still runs: a pass is a pass, but a failure may be
    nothing but the missing route types, so it is skipped with the reason --
    an advisory, never sent to the agent as its fault.
    """
    where = package or "."

    def run(runner: Runner, root: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        cwd = Path(root) / package if package else Path(root)
        deadline = time.monotonic() + timeout
        unrun = None
        if next_bin is None:
            unrun = f"next is not installed in {where} (no node_modules/.bin/next)"
        else:
            ran = runner([str(next_bin), "typegen"], cwd, env, timeout)
            if ran.timed_out:
                return SKIPPED, (f"`next typegen` timed out in {where}, so tsc was not run; "
                                 "CI runs it")
            if ran.code != 0:
                unrun = (f"`next typegen` exited {ran.code} in {where} (a Next.js without "
                         "the command, or a failure)")
        status, output = _verdict(runner([str(tsc), *args], cwd, env,
                                         max(1.0, deadline - time.monotonic())))
        if unrun and status == FAILED:
            return SKIPPED, (f"{unrun}, so tsc's errors may be Next's missing route types "
                             "(PageProps/LayoutProps), not this change; CI runs it")
        return status, output
    return run


def _guards_selection(runner: Runner, repo: Path, env: Mapping[str, str], base: str,
                      timeout: float) -> set[str]:
    """The test files `changed-guards.sh --list` selects: the changed-guards
    check runs those, so graph-pytest does not run them twice. Empty when the
    script cannot say -- then the graph's files all run, which costs time,
    never coverage."""
    if not (repo / "scripts" / "changed-guards.sh").is_file():
        return set()
    ran = runner(["bash", "scripts/changed-guards.sh", "--base", base, "--list"], repo, env,
                 min(timeout, 120.0))
    if ran.code != 0:
        return set()
    return {line.strip() for line in ran.output.splitlines()
            if line.strip().endswith(_PY_SUFFIX) and " " not in line.strip()}


#: What a refused or unreachable connection says, in asyncio
#: (`Connect call failed`), the OS (`Connection refused`, errno 111), psycopg2
#: (`could not connect to server`) and node (`ECONNREFUSED`).
_UNREACHABLE = re.compile(r"Connect call failed|Connection refused|ECONNREFUSED|"
                          r"could not connect to server|\[Errno 111\]", re.IGNORECASE)
#: The address beside it: `('127.0.0.1', 5432)`, `127.0.0.1:5432`, and
#: psycopg's `"localhost" (::1), port 5432` / `host "localhost" (127.0.0.1)
#: and accepting TCP/IP connections on port 5432`.
_ADDRESSES = (
    re.compile(r"\(\s*'(?P<host>[^']+)',\s*(?P<port>\d+)"),
    re.compile(r"(?P<host>localhost|127(?:\.\d+){3}|\[::1\]|0\.0\.0\.0):(?P<port>\d+)"),
    re.compile(r'"(?P<host>[^"]+)"(?:\s*\([^)]*\))?[\s,]+(?:and accepting\s+TCP/IP '
               r"connections on\s+)?port (?P<port>\d+)"),
)
#: How pytest's junitxml labels an error at setup and at collection; an error
#: at teardown follows a test that already ran, so it never counts.
_SETUP_ERROR = ("failed on setup", "collection failure")


def _local(host: str) -> bool:
    return host.strip("[]") in ("localhost", "::1", "0.0.0.0") or host.startswith("127.")


def unreachable_services(report: str) -> list[str] | None:
    """The local addresses a pytest junitxml `report` says every test failed
    to reach, or None when the run says anything else.

    Conservative (#1605): None unless at least one test errored, NO test
    passed or failed on its own, and EVERY error is at setup or collection
    and names a refused connection to localhost. A single assertion failure,
    pass, ImportError or fixture bug, an address that is not local, or a
    report that does not parse, keeps the check failed. Skipped tests are
    neither a pass nor a failure.
    """
    try:
        root = ElementTree.fromstring(report)
    except ElementTree.ParseError:
        return None
    addresses: set[str] = set()
    errors = 0
    for case in root.iter("testcase"):
        if case.find("failure") is not None:
            return None
        found = case.findall("error")
        if not found:
            if case.find("skipped") is not None:
                continue
            return None
        for error in found:
            message = error.get("message") or ""
            text = f"{message}\n{error.text or ''}"
            if not message.startswith(_SETUP_ERROR) or not _UNREACHABLE.search(text):
                return None
            here = {f"{m.group('host')}:{m.group('port')}"
                    for pattern in _ADDRESSES for m in pattern.finditer(text)
                    if _local(m.group("host"))}
            if not here:
                return None
            addresses |= here
            errors += 1
    return sorted(addresses) if errors else None


#: uv's own first line when it could not start pytest at all: no pytest in
#: the environment it resolved ("Failed to spawn"), or no project there (#1756).
_UV_CANNOT_RUN = re.compile(r"^error: (?:Failed to spawn: `?pytest`?|No `?project`? .*found|"
                            r"No `pyproject\.toml` found)")


def _uv_cannot_run(ran: Ran) -> str | None:
    """uv's first line when it says it could not start pytest, else None. Only
    the first line counts: a failing test that prints the same words is a
    failing test."""
    if ran.code == 0 or ran.timed_out:
        return None
    first = next((line.strip() for line in ran.output.splitlines() if line.strip()), "")
    return first if _UV_CANNOT_RUN.match(first) else None


def _pytest_project(runner: Runner, repo: Path, project: str, members: Sequence[str],
                    env: Mapping[str, str], timeout: float) -> tuple[str, str]:
    """One `uv run pytest` from `project`'s directory, with paths relative to it."""
    # pytest's own report, outside the checkout: the console text is a
    # summary the repository's config can reshape (#1605).
    with tempfile.TemporaryDirectory(prefix="prepublish-pytest-") as tmp:
        report = Path(tmp) / "junit.xml"
        argv = ["uv", "run", "--quiet", "pytest", "-q", "-n", "auto", "-p",
                "no:cacheprovider", f"--junitxml={report}",
                *(_relative(project, t) for t in members)]
        ran = runner(argv, Path(repo) / project if project else Path(repo), env, timeout)
        status, output = _verdict(ran)
        if status != FAILED or ran.timed_out:
            return status, output
        cannot = _uv_cannot_run(ran)
        if cannot:
            return SKIPPED, cannot
        try:
            services = unreachable_services(report.read_text(errors="replace"))
        except OSError:
            services = None
    if services:
        # §5.3: an address a declared, running service owns is the tests'
        # error (row 4); any other stays skipped, saying it was not declared
        # (row 3), or exactly as PR 1606 made it when nothing is (row 1).
        declared = declared_services(env)
        ports = {SERVICE_PORTS[name] for name in declared}
        owned = [a for a in services if _port(a) in ports]
        if owned:
            return FAILED, (f"every selected test was refused at {', '.join(owned[:3])}, the "
                            f"address of the declared {', '.join(declared)} "
                            f"({testservices.DECLARATION_PATH}), which was running and "
                            "answering: the tests connect to it wrongly. DATABASE_URL and "
                            f"PGHOST/PGPORT name it.\n{output}")
        shown = ", ".join(services[:3]) + (" and more" if len(services) > 3 else "")
        undeclared = (f" and not declared in {testservices.DECLARATION_PATH}" if declared
                      else "")
        return SKIPPED, (f"a service the tests need is unreachable ({shown}){undeclared}; "
                         "every selected test errored at setup connecting to it; CI runs it")
    return status, output


def declared_services(env: Mapping[str, str]) -> list[str]:
    """The catalogue services the gate found answering for this round
    (`TEST_SERVICES_ENV`, which only the gate sets in a check's environment)."""
    names = (env.get(TEST_SERVICES_ENV) or "").split(",")
    return [n.strip() for n in names if n.strip() in SERVICE_PORTS]


def _port(address: str) -> int | None:
    port = address.rpartition(":")[2]
    return int(port) if port.isdigit() else None


def service_env(services: Iterable[testservices.DeclaredService]) -> dict[str, str]:
    """§5.1: what the checks' environment gains while `services` answer.

    The catalogue's fixed address, user and the FIRST declared database. No
    password exists (`trust` on loopback, §3.1), so no value is secret.
    """
    out: dict[str, str] = {}
    names: list[str] = []
    for service in services:
        names.append(service.name)
        if service.name == "postgres":
            host, port = testservices.LISTEN_HOST, testservices.LISTEN_PORT
            user, database = testservices.SERVICE_USER, service.databases[0]
            out.update({"PGHOST": host, "PGPORT": str(port), "PGUSER": user,
                        "PGDATABASE": database,
                        "DATABASE_URL": f"postgresql://{user}@{host}:{port}/{database}"})
    out[TEST_SERVICES_ENV] = ",".join(names)
    return out


def probe_argv(service: testservices.DeclaredService) -> list[str]:
    """`pg_isready` over TCP at the address the tests use: the gate runs in
    the runner's process, so it asks the address, not the worker's handle."""
    return [str(testservices.bin_dir(service.version) / "pg_isready"),
            "-h", testservices.LISTEN_HOST, "-p", str(testservices.LISTEN_PORT),
            "-U", testservices.SERVICE_USER, "-d", testservices.MAINTENANCE_DATABASE,
            "-t", "2"]


def _exit_why(status: int) -> str:
    """A service process's exit status, said for a person."""
    if status == -signal.SIGKILL:
        return "killed by signal 9: the kernel's OOM killer, which ranks it first"
    if status < 0:
        return f"killed by signal {-status}"
    return f"exited with status {status}"


def watched(check: Check, stopped: Callable[[], str | None]) -> Check:
    """`check`, followed by a probe of the declared services (§5.2 step 4):
    a service that stopped during the run makes the run `error` -- its
    result says nothing about the change, and no fix turn can fix it (§5.3
    row 5)."""
    inner = check.run
    if inner is None:
        return check

    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float
            ) -> tuple[str, str]:
        status, output = inner(runner, repo, env, timeout)
        why = stopped()
        if why:
            return ERROR, (f"{why}; this run's result says nothing about the change\n\n"
                           f"{output}")
        return status, output
    return Check(check.name, check.files, check.command, run=run, skipped=check.skipped)


def _graph_pytest(tests: Sequence[str], base: str
                  ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    """One `uv run pytest` per Python project: the nearest directory above
    each selected file holding a pyproject.toml, run from there (#1756: a
    repository whose projects live in subfolders has no project at the root
    for uv to find). Passes only when every project's run passes."""
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        already = _guards_selection(runner, repo, env, base, timeout)
        rest = [t for t in tests if t not in already]
        if not rest:
            return PASSED, "every selected test file is already run by changed-guards"
        projects: dict[str, list[str]] = {}
        orphans: list[str] = []
        for test in rest:
            project = _package_root(repo, test, "pyproject.toml")
            if project is None:
                orphans.append(test)
            else:
                projects.setdefault(project, []).append(test)
        notes = [f"no Python project above {t}; CI runs it" for t in orphans]
        # The repository's one root project: the run it always was, unwrapped.
        if not orphans and list(projects) == [""]:
            return _pytest_project(runner, repo, "", rest, env, timeout)
        if not projects:
            return SKIPPED, "\n".join(notes)
        deadline = time.monotonic() + timeout
        results: list[tuple[str, str, str]] = []
        for project, members in sorted(projects.items()):
            left = max(1.0, deadline - time.monotonic())
            status, output = _pytest_project(runner, repo, project, members, env, left)
            results.append((project or ".", status, output))
        statuses = {status for _where, status, _output in results}
        overall = (FAILED if FAILED in statuses else PASSED if PASSED in statuses
                   else SKIPPED)
        lines = [f"{where}: {status}" + (f" ({output.strip()})" if status == SKIPPED else "")
                 for where, status, output in results]
        if overall == SKIPPED:
            # Every project could not run: the reason is the per-project lines.
            return SKIPPED, "\n".join(lines + notes)
        details = [f"--- {where} ({status}) ---\n{output.strip()}"
                   for where, status, output in results if status != SKIPPED]
        return overall, "\n".join(lines + notes + [""] + details)
    return run


def _terraform_fmt(files: Sequence[str]
                   ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        ran = runner(["terraform", "fmt", "-check", "-diff", "-no-color", *files], repo, env,
                     timeout)
        if ran.code == 0 and not ran.timed_out:
            return PASSED, ran.output
        if ran.code == _FMT_UNFORMATTED:
            return FAILED, ("these files are not `terraform fmt` formatted; run "
                            f"`terraform fmt` on them:\n{ran.output}")
        return FAILED, ran.output
    return run


def graph_checks(repo: Path, files: Sequence[str], *, base: str, env: Mapping[str, str],
                 snapshot: Snapshot | None) -> list[Check]:
    """graph-pytest, vitest, tsc, node-tests and terraform-fmt for this diff
    (CTXQ-G; node-tests #2455)."""
    present = [f for f in files if (repo / f).is_file()]
    checks: list[Check] = []
    tests = list(select_tests(repo, files, snapshot))[:GRAPH_TESTS_MAX]

    py_tests = [t for t in tests if t.endswith(_PY_SUFFIX)]
    if py_tests:
        if not _which("uv", env):
            checks.append(Check("graph-pytest", tuple(py_tests), "uv run pytest -n auto",
                                skipped="uv is not on PATH in this image"))
        else:
            checks.append(Check("graph-pytest", tuple(py_tests),
                                "uv run pytest -q -n auto <graph-selected files> "
                                "(in each Python project)",
                                run=_graph_pytest(py_tests, base)))

    by_package: dict[str, list[str]] = {}
    for test in tests:
        if test.endswith(_JS_SUFFIXES) and not test.endswith((".mjs", ".cjs")):
            package = _package_root(repo, test)
            if package is not None:
                by_package.setdefault(package, []).append(test)
    for package, members in sorted(by_package.items()):
        where = package or "."
        binary = _node_bin(repo, package, "vitest")
        if binary is None:
            checks.append(Check(
                "vitest", tuple(members), f"vitest run (in {where})",
                skipped=f"vitest is not installed in {where} (no node_modules/.bin/vitest; "
                        "the gate does not run npm install); CI runs it"))
            continue
        # A file outside the package's vitest `include` (a node:test file) is
        # simply not collected; it is not the agent's failure.
        args = ["run", "--passWithNoTests", *(_relative(package, t) for t in members)]
        checks.append(Check("vitest", tuple(members), f"vitest run <files> (in {where})",
                            run=_in_package(repo, package, binary, args)))

    ts_packages: dict[str, list[str]] = {}
    for path in files:
        if path.endswith(_TS_SUFFIXES):
            package = _package_root(repo, path)
            if package is not None and (repo / package / "tsconfig.json").is_file():
                ts_packages.setdefault(package, []).append(path)
    for package, members in sorted(ts_packages.items()):
        where = package or "."
        binary = _node_bin(repo, package, "tsc")
        if binary is None:
            checks.append(Check(
                "tsc", tuple(members), f"tsc (in {where})",
                skipped=f"tsc is not installed in {where} (no node_modules/.bin/tsc; the "
                        "gate does not run npm install); CI runs it"))
            continue
        typegen, args = _typecheck_plan(repo, package)
        if typegen:
            checks.append(Check(
                "tsc", tuple(members), f"next typegen, then tsc {' '.join(args)} (in {where})",
                run=_typegen_then_tsc(package, _node_bin(repo, package, "next"), binary, args)))
        else:
            checks.append(Check("tsc", tuple(members), f"tsc {' '.join(args)} (in {where})",
                                run=_in_package(repo, package, binary, args)))

    # A package's node:test files (`.mjs`, which the vitest selection above
    # drops) run only through its own `test:node` script -- CI's ui-build
    # runs it, and four lanes went red there on a company-name test the gate
    # never ran (#2429, #2430, #2433, #2437; #2455). A deleted file counts:
    # removing one can break a test that walks the tree.
    node_packages: dict[str, list[str]] = {}
    for path in files:
        package = _package_root(repo, path)
        if package is not None and _has_test_node(repo, package):
            node_packages.setdefault(package, []).append(path)
    for package, members in sorted(node_packages.items()):
        where = package or "."
        command = f"npm run test:node (in {where})"
        npm = _which("npm", env)
        if npm is None:
            checks.append(Check("node-tests", tuple(members), command,
                                skipped="npm is not on PATH in this image; CI runs it"))
        elif not (repo / package / "node_modules").is_dir():
            checks.append(Check(
                "node-tests", tuple(members), command,
                skipped=f"{where} has no node_modules (the gate does not run npm install); "
                        "CI runs it"))
        else:
            checks.append(Check("node-tests", tuple(members), command,
                                run=_in_package(repo, package, Path(npm), ["run", "test:node"])))

    tf = [f for f in present if f.endswith(_TF_SUFFIX)]
    if tf:
        if _which("terraform", env):
            checks.append(Check("terraform-fmt", tuple(tf), "terraform fmt -check -diff <files>",
                                run=_terraform_fmt(tf)))
        else:
            checks.append(Check("terraform-fmt", tuple(tf), "terraform fmt -check",
                                skipped="terraform is not on PATH in this image"))
    return checks


# ---------------------------------------------------------------------------
# Test reach (M11, advisory)
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Per file, the base-side line ranges a `git diff -U0` touches. A pure
    insertion after line N is `(N, 0)`: it touches the symbol N is inside."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            current = line[6:] if line.startswith("--- a/") else None
        elif current is not None and (match := _HUNK.match(line)):
            start, count = int(match.group(1)), int(match.group(2) or "1")
            out.setdefault(current, []).append((start, count))
    return out


def _touches(symbol: Mapping[str, Any], hunks: Sequence[tuple[int, int]]) -> bool:
    try:
        first, last = int(symbol.get("start_line") or 0), int(symbol.get("end_line") or 0)
    except (TypeError, ValueError):
        return False
    if first <= 0:
        return False
    last = max(first, last)
    for start, count in hunks:
        if count == 0:
            if first <= start < last:
                return True
        elif start <= last and start + count - 1 >= first:
            return True
    return False


def _short_name(symbol_id: str) -> str:
    name = symbol_id.split("#", 1)[-1]
    return re.split(r"[.:]", name)[-1]


def measure_test_reach(repo: Path, base: str, files: Sequence[str], snapshot: Snapshot, *,
               runner: Runner, env: Mapping[str, str]) -> dict[str, Any] | None:
    """M11 over this change: the changed symbols the graph knows, and which of
    them have a covering test -- an edge in the snapshot's `tests` layer, or a
    test file this change adds or edits that names them. None when git could
    not say which lines changed."""
    ran = _git(repo, "diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", base, "--",
               runner=runner, env=env)
    if ran.code != 0:
        return None
    hunks = changed_lines(ran.output)
    test_texts: list[str] = []
    for path in files:
        if is_test_path(path) and (repo / path).is_file():
            try:
                test_texts.append((repo / path).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    changed: list[str] = []
    reached: list[str] = []
    unindexed: list[str] = []
    for path in files:
        if is_test_path(path) or path not in hunks or not path.endswith(_CODE_SUFFIXES):
            continue
        try:
            if snapshot.file(path) is None:
                unindexed.append(path)
                continue
            symbols = snapshot.symbols_in(path)
        except SnapshotError:
            unindexed.append(path)
            continue
        for symbol in symbols:
            symbol_id = str(symbol.get("id") or "")
            if not symbol_id or symbol.get("kind") == "test" or not _touches(symbol, hunks[path]):
                continue
            changed.append(symbol_id)
            try:
                covered = bool(snapshot.tests(symbol_id))
            except SnapshotError:
                covered = False
            if not covered:
                word = re.compile(rf"\b{re.escape(_short_name(symbol_id))}\b")
                covered = any(word.search(text) for text in test_texts)
            if covered:
                reached.append(symbol_id)
    for path in files:
        if (not is_test_path(path) and path.endswith(_CODE_SUFFIXES) and path not in hunks
                and path not in unindexed and (repo / path).is_file()):
            unindexed.append(path)  # untracked: new to the index too
    return {
        "changed_symbols": len(changed),
        "reached": len(reached),
        "unreached": sorted(set(changed) - set(reached)),
        "unindexed_files": sorted(unindexed),
    }


def hunk_texts(diff: str) -> dict[str, list[tuple[int, int, str]]]:
    """Per file, each `git diff -U0` hunk's base range and its text."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    current: str | None = None
    lines: list[str] = []
    start = count = 0

    def close() -> None:
        if current is not None and lines:
            out.setdefault(current, []).append((start, count, "\n".join(lines)))

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            close()
            current, lines = None, []
        elif line.startswith("--- "):
            current = line[6:] if line.startswith("--- a/") else None
        elif current is not None and (match := _HUNK.match(line)):
            close()
            start, count = int(match.group(1)), int(match.group(2) or "1")
            lines = [line]
        elif lines and line[:1] in ("+", "-", " ") and not line.startswith("+++ "):
            lines.append(line)
    close()
    return out


def changed_symbols(repo: Path, base: str, files: Sequence[str], snapshot: Snapshot, *,
                    runner: Runner, env: Mapping[str, str]) -> list[dict[str, str]]:
    """J4's `changed`: each changed, indexed, non-test symbol with the hunks
    that touch it -- the same symbols `measure_test_reach` counts."""
    ran = _git(repo, "diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", base, "--",
               runner=runner, env=env)
    if ran.code != 0:
        return []
    hunks = hunk_texts(ran.output)
    out: list[dict[str, str]] = []
    for path in files:
        if is_test_path(path) or path not in hunks or not path.endswith(_CODE_SUFFIXES):
            continue
        try:
            if snapshot.file(path) is None:
                continue
            symbols = snapshot.symbols_in(path)
        except SnapshotError:
            continue
        for symbol in symbols:
            symbol_id = str(symbol.get("id") or "")
            touching = [text for first, count, text in hunks[path]
                        if _touches(symbol, [(first, count)])]
            if symbol_id and symbol.get("kind") != "test" and touching:
                out.append({"symbol": symbol_id, "path": path, "hunk": "\n".join(touching)})
    return out


def _test_excerpt(repo: Path, path: str, limit: int = 800) -> str:
    """The head of a test file, or "" for one §5.4 never reads (`is_denied_path`)."""
    from .judge.sanitize import is_denied_path

    target = repo / path
    if is_denied_path(path) or target.is_symlink() or not target.is_file():
        return ""
    try:
        return target.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def jev_test_gap(judge: Any, repo: Path, base: str, files: Sequence[str],
                 snapshot: Snapshot, tests: Sequence[str], *, runner: Runner,
                 env: Mapping[str, str]) -> dict[str, Any] | None:
    """J4: the changed symbols no listed test would catch, or None -- for no
    symbols, no tests, or any call that did not answer in full."""
    from .judge import shapes
    from .judge.sanitize import is_denied_path

    changed = changed_symbols(repo, base, files, snapshot, runner=runner, env=env)
    # §5.4: a denied path (hard stops included) is sent by name only. The
    # `changed` rows carry no `path` key, so the client's sanitizer cannot
    # see the path and the hunk has to be withheld here.
    stops = tuple(getattr(judge, "hard_stop_paths", ()) or ())
    asked = [dict(row, hunk="") if is_denied_path(row.get("path", ""), stops) else row
             for row in changed[:J4_MAX_SYMBOLS]]
    test_rows = [{"path": t, "name": Path(t).name, "excerpt": _test_excerpt(repo, t)}
                 for t in list(tests)[:shapes.MAX_CHOICE_TESTS]]
    if not asked or not test_rows:
        return None
    state, questions = shapes.j4_test_gap(asked, test_rows)
    try:
        decision = judge.decide("J4", state, questions)
    except Exception:  # noqa: BLE001 -- a refused question adds nothing
        return None
    if not decision.ok or decision.answers is None:
        return None
    if any(f"s{k}" not in decision.answers for k in range(len(asked))):
        return None
    uncovered = [asked[k]["symbol"] for k in shapes.read_test_gap(decision.answers)
                 if k < len(asked)]
    return {"asked": len(asked), "not_asked": len(changed) - len(asked), "tests": len(test_rows),
            "uncovered": uncovered, "input_tokens": decision.input_tokens}


def select_checks(repo: Path, files: Sequence[str], *, base: str,
                  env: Mapping[str, str], python: str = "python3",
                  snapshot: Snapshot | None = None) -> list[Check]:
    """The fixed check set, narrowed to the kinds of file `files` holds.

    A check whose files are all deleted is not chosen: there is nothing left
    to lint. A check chosen but unable to run is returned with `skipped`.
    """
    present = [f for f in files if (repo / f).is_file()]
    checks: list[Check] = []
    if not files:
        return checks

    if (repo / GUARDS_SCRIPT).is_file():
        argv = ["bash", GUARDS_SCRIPT, "--base", base]
        checks.append(Check(GUARDS_CHECK, tuple(files), " ".join(argv), run=_simple(argv)))

    tf = [f for f in present if f.startswith("terraform/") and f.endswith(".tf")]
    if tf:
        argv = ["checkov", "--framework", "terraform", "--quiet", "--compact", "-f", *tf]
        if _which("checkov", env):
            checks.append(Check("checkov-terraform", tuple(tf), " ".join(argv[:5]) + " -f <files>",
                                run=_simple(argv)))
        else:
            checks.append(Check("checkov-terraform", tuple(tf), "checkov",
                                skipped="checkov is not on PATH in this image"))
        if _which("tflint", env):
            checks.append(Check("tflint", tuple(tf),
                                "tflint --chdir=terraform --recursive --format=compact",
                                run=_tflint(tf)))
        else:
            checks.append(Check(
                "tflint", tuple(tf), "tflint",
                skipped="tflint is not installed in agent-runtime-base (INSTALL_TFLINT=0: no "
                        "tflint release passes the image promote gate); CI runs it"))

    k8s = [f for f in files if f.startswith("kubernetes/")]
    if k8s:
        if not _which("checkov", env):
            checks.append(Check("checkov-kubernetes", tuple(k8s), "checkov",
                                skipped="checkov is not on PATH in this image"))
        elif (repo / "kubernetes" / "render.py").is_file():
            checks.append(Check(
                "checkov-kubernetes", tuple(k8s),
                f"kubernetes/render.py, then checkov -d <rendered> --framework kubernetes "
                f"--skip-check {_K8S_SKIP}",
                run=_render_and_scan(python)))
        else:
            manifests = [f for f in k8s if f in present and f.endswith((".yaml", ".yml"))]
            if manifests:
                argv = ["checkov", "--framework", "kubernetes", "--quiet", "--compact", "-f",
                        *manifests]
                checks.append(Check("checkov-kubernetes", tuple(manifests),
                                    " ".join(argv[:5]) + " -f <files>", run=_simple(argv)))

    shell = [f for f in present if f.endswith(_SHELL_SUFFIXES)]
    if shell:
        argv = ["shellcheck", "-x", *shell]
        if _which("shellcheck", env):
            checks.append(Check("shellcheck", tuple(shell), "shellcheck -x <files>",
                                run=_simple(argv)))
        else:
            checks.append(Check("shellcheck", tuple(shell), "shellcheck",
                                skipped="shellcheck is not on PATH in this image"))

    py = [f for f in present if f.endswith(_PY_SUFFIX)]
    if py:
        if not _configures_ruff(repo):
            reason = "the repository configures no ruff (no ruff.toml, no [tool.ruff])"
            checks.append(Check("ruff-format", tuple(py), "ruff format --check", skipped=reason))
            checks.append(Check("ruff-check", tuple(py), "ruff check", skipped=reason))
        else:
            # The repository's own ruff, at the version its lock pins, when the
            # image has none; `uv run` installs the project's dev group.
            prefix = ["ruff"] if _which("ruff", env) else (
                ["uv", "run", "--quiet", "ruff"] if _which("uv", env) else None)
            for name, sub in (("ruff-format", ["format", "--check"]), ("ruff-check", ["check"])):
                if prefix is None:
                    checks.append(Check(name, tuple(py), f"ruff {' '.join(sub)}",
                                        skipped="neither ruff nor uv is on PATH in this image"))
                else:
                    argv = [*prefix, *sub, *py]
                    checks.append(Check(name, tuple(py), f"ruff {' '.join(sub)} <files>",
                                        run=_simple(argv)))

    mirror = [f for f in files if _touches_build_mirror(f)]
    if mirror:
        tests = _pytest_files(repo, BUILD_MIRROR_TESTS)
        if not tests:
            pass  # a repository without the build mirror has nothing to hold equal
        elif not _which("uv", env):
            checks.append(Check("build-mirror", tuple(mirror), "uv run pytest <mirror tests>",
                                skipped="uv is not on PATH in this image"))
        else:
            # `scripts/refresh-build-mirror.sh --check` reads the mirror BUCKET
            # with gcloud, and this container holds no cloud credential; the
            # manifest-vs-Dockerfile tests are the part that needs none.
            argv = ["uv", "run", "--quiet", "pytest", "-q", "-p", "no:cacheprovider", *tests]
            checks.append(Check("build-mirror", tuple(mirror),
                                "uv run pytest " + " ".join(tests), run=_simple(argv)))
    checks += graph_checks(repo, files, base=base, env=env, snapshot=snapshot)
    return checks


def run_checks(repo: Path, checks: Sequence[Check], *, env: Mapping[str, str],
               runner: Runner = run_command, timeout: float = CHECK_TIMEOUT_SECONDS,
               clock: Callable[[], float] = time.monotonic,
               deadline: float | None = None) -> list[Outcome]:
    """Run `checks` in order, each under `timeout` -- and, given `deadline`
    (an instant on `clock`), under what is left of it when the check starts."""
    # uv's project environments live outside the clone, and go with the round (#1964).
    with tempfile.TemporaryDirectory(prefix="prepublish-uv-", dir=_outside(repo, env),
                                     ignore_cleanup_errors=True) as uv_root:
        return _run_checks(repo, checks, env=env, runner=uv_outside(runner, Path(uv_root)),
                           timeout=timeout, clock=clock, deadline=deadline)


def _run_checks(repo: Path, checks: Sequence[Check], *, env: Mapping[str, str],
                runner: Runner, timeout: float, clock: Callable[[], float],
                deadline: float | None = None) -> list[Outcome]:
    outcomes: list[Outcome] = []
    out_of_budget: str | None = None
    for check in checks:
        if check.run is None:
            outcomes.append(Outcome(check.name, SKIPPED, 0.0, check.files, check.command,
                                    reason=check.skipped))
            continue
        limit = timeout
        if deadline is not None and out_of_budget is None:
            left = deadline - clock() - CHECK_MARGIN_SECONDS
            if left < MIN_CHECK_SECONDS:
                out_of_budget = (f"the step's budget ran out before this check "
                                 f"({max(0, int(left))} s left)")
            limit = min(timeout, left)
        if out_of_budget is not None:
            # Once out of budget, no later check starts either (#2274).
            outcomes.append(Outcome(check.name, SKIPPED, 0.0, check.files, check.command,
                                    reason=out_of_budget))
            continue
        started = clock()
        try:
            status, output = check.run(runner, repo, env, limit)
        except OSError as exc:
            status, output = ERROR, f"{type(exc).__name__}: the check could not run"
        # A check that finds at run time it cannot run (graph-pytest's
        # unreachable service) returns SKIPPED with its reason as the output.
        first = next((line for line in output.strip().splitlines() if line.strip()), "")
        reason = {ERROR: first or "the check could not run",
                  SKIPPED: output.strip() or None}.get(status)
        # The guard script says it ran past its own limit; any check, the
        # guard script too, may end in the runner's kill (#2503).
        timed_out = status == FAILED and (
            guards_timed_out(output) if check.name == GUARDS_CHECK
            else killed_at_limit(output))
        if timed_out:
            reason = "ran out of time"
        outcomes.append(Outcome(check.name, status, clock() - started, check.files,
                                check.command, output=output, reason=reason,
                                timed_out=timed_out))
    return outcomes


# ---------------------------------------------------------------------------
# The checks leave no trace in the clone (#1964)
# ---------------------------------------------------------------------------


#: Where uv puts a project's environment; uv's default is `<project>/.venv`,
#: inside the clone the publish stages with `git add --all`.
UV_PROJECT_ENV = "UV_PROJECT_ENVIRONMENT"
#: How many restored paths the log and the advisory section name.
RESTORED_SHOWN = 10
#: A path the agent had changed keeps its bytes up to this size to be put
#: back if a check changes it; a larger one keeps only its hash, and a check
#: changing it is reported as not restored. 16 MiB holds any source file
#: without holding an agent's stray dataset in memory.
RESTORE_MAX_BYTES = 16 * 1024 * 1024


def _outside(repo: Path, env: Mapping[str, str]) -> Path:
    """A directory not inside `repo` for a temporary root: the checks' TMPDIR,
    else the worker's, else the checkout's parent."""
    root = Path(repo).resolve()
    for candidate in (env.get("TMPDIR"), tempfile.gettempdir()):
        if candidate and Path(candidate).is_dir():
            path = Path(candidate).resolve()
            if not path.is_relative_to(root):
                return path
    return root.parent


def uv_outside(runner: Runner, root: Path) -> Runner:
    """`runner`, with every `uv` given `UV_PROJECT_ENVIRONMENT` under `root`:
    one directory per working directory, so two projects never share (and
    re-sync) one environment. Any other command runs as it was asked."""
    homes: dict[str, Path] = {}

    def run(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Ran:
        if argv and Path(argv[0]).name == "uv":
            key = str(Path(cwd).resolve())
            if key not in homes:
                homes[key] = root / f"project-{len(homes)}"
            env = {**env, UV_PROJECT_ENV: str(homes[key])}
        return runner(argv, cwd, env, timeout)
    return run


@dataclass(frozen=True)
class _Entry:
    """One path as it stood: `kind` is file, link, dir (an untracked nested
    repository), missing, or unknown (unreadable: never touched)."""

    kind: str
    digest: str | None = None
    executable: bool = False
    data: bytes | None = field(default=None, compare=False, repr=False)


TreeState = dict[str, _Entry]


def _status_paths(repo: Path, *, runner: Runner, env: Mapping[str, str]
                  ) -> dict[str, bool] | None:
    """Every path `git status` lists, mapped to whether git tracks it; None
    when git could not say."""
    ran = _git(repo, "status", "--porcelain=v2", "--untracked-files=all", "-z",
               runner=runner, env=env)
    if ran.code != 0:
        return None
    fields = ran.output.split("\0")
    out: dict[str, bool] = {}
    i = 0
    while i < len(fields):
        entry, i = fields[i], i + 1
        if entry.startswith("? "):
            out[entry[2:]] = False
        elif entry.startswith("1 "):
            out[entry.split(" ", 8)[8]] = True
        elif entry.startswith("2 "):
            out[entry.split(" ", 9)[9]] = True
            if i < len(fields) and fields[i]:
                out[fields[i]] = True  # the rename's original path
            i += 1
        elif entry.startswith("u "):
            out[entry.split(" ", 10)[10]] = True
    return out


def _fingerprint(repo: Path, path: str, *, keep: bool) -> _Entry:
    full = Path(repo) / path.rstrip("/")
    try:
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            return _Entry("link", os.readlink(full))
        if stat.S_ISDIR(st.st_mode):
            return _Entry("dir")
        digest = hashlib.sha256()
        kept: list[bytes] | None = [] if keep and st.st_size <= RESTORE_MAX_BYTES else None
        with open(full, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                if kept is not None:
                    kept.append(chunk)
        return _Entry("file", digest.hexdigest(), bool(st.st_mode & 0o111),
                      b"".join(kept) if kept is not None else None)
    except FileNotFoundError:
        return _Entry("missing")
    except OSError:
        return _Entry("unknown")


def record_tree(repo: Path, *, runner: Runner, env: Mapping[str, str]) -> TreeState | None:
    """The working tree before a round of checks: every path `git status`
    lists (the agent's changes) with its content. None when git could not say."""
    paths = _status_paths(repo, runner=runner, env=env)
    if paths is None:
        return None
    return {path: _fingerprint(repo, path, keep=True) for path in paths}


def _remove(repo: Path, path: str) -> None:
    """Remove `path` and then each directory above it it leaves empty."""
    full = Path(repo) / path.rstrip("/")
    if full.is_dir() and not full.is_symlink():
        shutil.rmtree(full)
    elif full.is_symlink() or full.exists():
        full.unlink()
    parent, root = full.parent, Path(repo)
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _put_back(repo: Path, path: str, entry: _Entry) -> bool:
    """Give `path` the content `entry` recorded; False when it kept too little to."""
    if entry.kind == "file" and entry.data is None:
        return False
    _remove(repo, path)
    full = Path(repo) / path
    if entry.kind == "missing":
        return True
    full.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "link":
        os.symlink(str(entry.digest), full)
        return True
    full.write_bytes(entry.data or b"")
    mode = full.stat().st_mode
    full.chmod(mode | 0o111 if entry.executable else mode & ~0o111)
    return True


def restore_tree(repo: Path, state: TreeState, *, runner: Runner, env: Mapping[str, str]
                 ) -> tuple[list[str], list[str]] | None:
    """Undo whatever changed in the working tree since `state` was recorded:
    a path no longer as recorded gets its recorded content back, a tracked
    path that was clean is restored from HEAD, and a new untracked path is
    removed. Returns (restored, could not be restored), or None when git
    could not list the tree."""
    now = _status_paths(repo, runner=runner, env=env)
    if now is None:
        return None
    restored: list[str] = []
    failed: list[str] = []
    for path in sorted(set(state) | set(now)):
        try:
            if path in state:
                before = state[path]
                if before.kind in ("unknown", "dir"):
                    continue
                if _fingerprint(repo, path, keep=False) == before:
                    continue
                (restored if _put_back(repo, path, before) else failed).append(path)
            elif now[path]:
                ran = _git(repo, "checkout", "HEAD", "--", path, runner=runner, env=env)
                if ran.code != 0:
                    # Not in HEAD: a new file a check staged.
                    ran = _git(repo, "rm", "-q", "--cached", "-f", "--ignore-unmatch", "--",
                               path, runner=runner, env=env)
                    _remove(repo, path)
                (restored if ran.code == 0 else failed).append(path)
            else:
                _remove(repo, path)
                restored.append(path.rstrip("/"))
        except OSError:
            failed.append(path)
    return sorted(restored), sorted(failed)


def _named(paths: Sequence[str]) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:RESTORED_SHOWN])
    return shown + (f" and {len(paths) - RESTORED_SHOWN} more"
                    if len(paths) > RESTORED_SHOWN else "")


# ---------------------------------------------------------------------------
# What the agent and the pull request are told
# ---------------------------------------------------------------------------


def clean_output(text: str, secrets: Sequence[str], limit: int = OUTPUT_TAIL_CHARS) -> str:
    """The END of `text`, with every registered secret and credential pattern
    masked. Scrubbed before it is cut, so a cut never splits a value in two
    halves that each escape the patterns."""
    masked = redact_text(scrub_text(text, secrets)).text
    if len(masked) > limit:
        masked = "[... earlier output cut ...]\n" + masked[-limit:]
    return masked.strip()


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}text\n{text}\n{ticks}"


def _files_line(files: Sequence[str], cap: int = 20) -> str:
    shown = ", ".join(f"`{f}`" for f in files[:cap])
    return shown + (f" and {len(files) - cap} more" if len(files) > cap else "")


def fix_prompt(failed: Sequence[Outcome], secrets: Sequence[str],
               timed_out: Sequence[Outcome] = (), broke: Sequence[str] = (),
               remaining: float | None = None) -> str:
    """The fix turn's prompt; `broke` names the checks that passed before the
    previous fix turn and fail after it, which makes this the second turn.
    `remaining` is the step's budget left, in seconds, when it is known."""
    lines = []
    if broke:
        lines += [
            "Your fix turn broke " + ", ".join(broke) + ", which passed before it. You have "
            "ONE more turn, the last: fix every check below without breaking the others "
            "again, commit, and end with a short report.",
            "",
        ]
    lines += [
        "Before publishing your branch, the worker ran the repository's own checks on the "
        "files you changed, and these failed. You have ONE turn to fix them: fix the cause "
        "in the code (not the check), commit, and end with a short report. The checks run "
        "again after this turn; whatever still fails is published with its output in the "
        "pull request body for the operator.",
    ]
    if remaining is not None:
        lines += ["", f"About {max(0, int(remaining))} s of this step's budget is left, for "
                  "this turn and the checks after it."]
    if timed_out:
        guards = [o.name for o in timed_out if o.name == GUARDS_CHECK]
        others = [o.name for o in timed_out if o.name != GUARDS_CHECK]
        again = []
        if guards:
            again.append("the guard script in parts")
        if others:
            again.append(", ".join(others) + " as-is, under the same time limit")
        lines += ["", "Not yours to fix: " + ", ".join(o.name for o in timed_out)
                  + " ran out of time, which no change of yours fixes. The worker runs "
                  + " and ".join(again) + " again after this turn; do not run a timed-out "
                  "check or its test selection yourself."]
    for outcome in failed:
        lines += ["", f"## {outcome.name} ({outcome.command})",
                  f"Files: {_files_line(outcome.files)}",
                  _fence(clean_output(outcome.output, secrets) or "(no output)")]
    return "\n".join(lines)


def conflict_prompt(ref: str, onto: str, conflicts: Sequence[str]) -> str:
    listed = "\n".join(f"- {path}" for path in conflicts[:50])
    more = f"\n- and {len(conflicts) - 50} more" if len(conflicts) > 50 else ""
    return (
        f"Before publishing, the worker rebased your commits onto the current origin/{ref} "
        f"({onto[:12]}), and the rebase stopped on conflicts in these files:\n{listed}{more}\n\n"
        "The rebase is in progress in the checkout. Resolve every conflict keeping both "
        "your change and what landed on the base, `git add` each file, and run "
        "`GIT_EDITOR=true git rebase --continue` until the rebase completes. Do not run "
        "`git rebase --abort`. You have ONE turn; a rebase still in progress after it is "
        "aborted and your branch is published on its original base. End with a short report."
    )


def pr_section(rebase: Mapping[str, Any] | None, outcomes: Sequence[Outcome],
               secrets: Sequence[str]) -> str | None:
    """The markdown the worker appends to the pull request's body, or None."""
    # A declaration from the base, or a service that did not start, is the
    # platform's or the base's, not this branch's: advisory only (§5.3).
    bad = [o for o in outcomes if o.status == FAILED
           or (o.status == ERROR and o.name != TEST_SERVICES_CHECK)]
    conflicted = bool(rebase and rebase.get("status") == "conflict_unresolved")
    if not bad and not conflicted:
        return None
    parts = [
        PR_SECTION_HEADING, "",
        "The worker ran these checks on the files this branch changes before publishing, "
        "gave the agent a turn to fix them, and they still did not pass. The branch was "
        "published anyway so no work is lost; read this before merging.",
    ]
    if conflicted and rebase is not None:
        conflicts = list(rebase.get("conflicts") or [])
        parts += ["", f"### rebase onto origin/{rebase.get('ref')} conflicted",
                  f"The conflict turn did not finish the rebase, so it was aborted and the branch "
                  f"is on its original base. Conflicting files: {_files_line(conflicts)}"]
    for outcome in bad:
        verb = ("timed out" if outcome.timed_out
                else "failed" if outcome.status == FAILED else "could not run")
        parts += ["", f"### {outcome.name} {verb} ({outcome.duration_seconds:.1f} s)",
                  f"`{outcome.command}` on {_files_line(outcome.files)}",
                  _fence(clean_output(outcome.output, secrets) or "(no output)")]
    text = "\n".join(parts)
    if len(text) > SECTION_MAX_CHARS:
        text = text[:SECTION_MAX_CHARS].rstrip() + "\n\n_[cut by the worker]_"
    return text + "\n"


def read_self_check(artifacts_dir: Path) -> str | None:
    """The agent's `self-check.md`, or None when it wrote none (or a symlink)."""
    path = Path(artifacts_dir) / SELF_CHECK_NAME
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def advisory_section(*, graph: Mapping[str, Any], outcomes: Sequence[Outcome],
                     reach: Mapping[str, Any] | None, self_check: str | None,
                     secrets: Sequence[str], jev_gap: Mapping[str, Any] | None = None,
                     guards: Sequence[str] = (), restored: Sequence[str] = (),
                     unrestored: Sequence[str] = ()) -> str:
    """The advisory markdown the worker appends to the pull request's body.

    Read by people. No merge path reads it: auto-merge, the review verdict and
    the requirements decide exactly as they would without it.
    """
    parts = [
        ADVISORY_SECTION_HEADING, "",
        "Written by the worker's pre-publish gate for the reader, not for a merge: "
        "auto-merge, the review verdict and the requirements never read this section.",
        "", "### Graph-selected tests", f"Source: {graph.get('snapshot')}.",
    ]
    selected = dict(graph.get("selected") or {})
    if selected:
        shown = [f"`{t}` ({why})" for t, why in list(selected.items())[:20]]
        more = f" and {len(selected) - 20} more" if len(selected) > 20 else ""
        parts.append(f"{len(selected)} test file(s) cover the changed files: "
                     f"{', '.join(shown)}{more}.")
    else:
        parts.append("No test file was found covering the changed files.")
    ran = [o for o in outcomes if o.name in ("graph-pytest", "vitest", "tsc", "node-tests",
                                                "terraform-fmt")]
    if ran:
        parts.append("Ran: " + ", ".join(
            f"{o.name} {o.status}" + (f" ({o.reason})" if o.status == SKIPPED and o.reason
                                       else "") for o in ran) + ".")
    if guards:
        parts += ["", f"### {GUARDS_CHECK}", *(f"- {note}" for note in guards)]
    services = [o for o in outcomes if o.name == TEST_SERVICES_CHECK and o.status == ERROR]
    for outcome in services:
        parts += ["", f"### {TEST_SERVICES_CHECK} (error, not the change's)",
                  f"{outcome.reason}. The tests ran as if nothing were declared.",
                  _fence(clean_output(outcome.output, secrets) or "(no output)")]
    if restored or unrestored:
        parts += ["", "### Side effects of the checks"]
        if restored:
            parts.append(f"The checks changed {len(restored)} path(s) in the working tree, "
                         f"and the worker restored each before publishing: {_named(restored)}.")
        if unrestored:
            parts.append(f"Changed by the checks and NOT restored, so published as the checks "
                         f"left them: {_named(unrestored)}.")

    parts += ["", "### Test reach (M11)"]
    if reach is None:
        parts.append(f"Test reach is unknown: {graph.get('snapshot')}. This does not mean "
                     "the change is covered.")
    else:
        total, hit = int(reach.get("changed_symbols") or 0), int(reach.get("reached") or 0)
        parts.append(f"{hit} of {total} changed symbols have a covering test (an edge in "
                     "the graph's tests layer, or a test file this change adds or edits "
                     "that names them).")
        missing = list(reach.get("unreached") or [])
        if missing:
            more = (f" and {len(missing) - REACH_SHOWN} more"
                    if len(missing) > REACH_SHOWN else "")
            parts.append("Without one: " + ", ".join(f"`{m}`" for m in missing[:REACH_SHOWN])
                         + more + ".")
        unindexed = list(reach.get("unindexed_files") or [])
        if unindexed:
            parts.append(f"Not in the index (new, or changed since it was built), reach "
                         f"unknown: {_files_line(unindexed)}.")

    parts += ["", "### Self-check"]
    if self_check is None:
        parts.append(f"The agent wrote no `{SELF_CHECK_NAME}`.")
    else:
        text = clean_output(self_check, secrets, limit=SELF_CHECK_MAX_CHARS) or "(empty)"
        parts += [f"The agent's own `{SELF_CHECK_NAME}`:", _fence(text)]
    if jev_gap is not None:
        parts += ["", JEV_SECTION_HEADING,
                  f"J4 test gap: Jev was asked which of {jev_gap['tests']} covering test "
                  f"file(s) would fail if each of {jev_gap['asked']} changed symbol(s) were "
                  "reverted. A judge's opinion: the checks above, not this, ran the tests."]
        uncovered = list(jev_gap.get("uncovered") or [])
        if uncovered:
            more = (f" and {len(uncovered) - REACH_SHOWN} more"
                    if len(uncovered) > REACH_SHOWN else "")
            parts.append("Changed behaviour no listed test would catch: "
                         + ", ".join(f"`{u}`" for u in uncovered[:REACH_SHOWN]) + more + ".")
        else:
            parts.append("Every changed symbol asked about has a test Jev expects to fail.")
        if jev_gap.get("not_asked"):
            parts.append(f"{jev_gap['not_asked']} more changed symbol(s) were not asked about.")
    text = "\n".join(parts)
    if len(text) > ADVISORY_MAX_CHARS:
        text = text[:ADVISORY_MAX_CHARS].rstrip() + "\n\n_[cut by the worker]_"
    return text + "\n"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


Submit = Callable[..., dict[str, Any]]


@dataclass
class _RoundServices:
    """The declared services for one round: those answering, what the gate
    itself started (stopped with the round), and the `test-services`
    outcome when there is one to report."""

    answers: Callable[[testservices.DeclaredService], bool]
    ready: list[testservices.DeclaredService] = field(default_factory=list)
    running: testservices.RunningServices | None = None
    root: Path | None = None
    outcome: Outcome | None = None

    def stopped(self) -> str | None:
        """Why a ready service is no longer there, or None (§5.2 step 4)."""
        mine = {p.service: p for p in (self.running.services if self.running else ())}
        for service in self.ready:
            proc = mine.get(service)
            status = proc.exit_status() if proc is not None else None
            if status is not None:
                return (f"the declared {service.name} stopped during the run "
                        f"({_exit_why(status)})")
            if not self.answers(service):
                return (f"the declared {service.name} stopped during the run (it no longer "
                        f"answers on {testservices.LISTEN_HOST}:{SERVICE_PORTS[service.name]})")
        return None

    def stop(self) -> None:
        if self.running is not None:
            self.running.stop()
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class Gate:
    """One pre-publish pass over a checkout. `resume(text)` continues the
    agent's session for one turn and returns False when it could not (no
    session, too little budget); `remaining()` is the step's budget left."""

    repo: Path
    env: Mapping[str, str]
    secrets: Sequence[str]
    resume: Callable[[str], bool]
    remaining: Callable[[], float]
    log: Any
    artifacts_dir: Path
    runner: Runner = run_command
    submit: Submit | None = None
    python: str = "python3"
    clock: Callable[[], float] = time.monotonic
    record: dict[str, Any] = field(default_factory=dict)
    #: Where the step's context is staged; None reads `context_dir`.
    context: Path | None = None
    #: The attempt's Jev client for J4 (`judge.attempt.load`), or None: no call.
    judge: Any = None
    #: The step's staged input patches (absolute paths): a branch that is
    #: exactly these applied to its base runs no checks (`adds_nothing`).
    staged_patches: Sequence[str] = ()
    _snapshot: Snapshot | None = field(default=None, init=False, repr=False)
    _diff: tuple[str, list[str]] | None = field(default=None, init=False, repr=False)
    #: What happened to the guard script in each round, for the advisory section.
    _guards: list[str] = field(default_factory=list, init=False, repr=False)
    #: How a service the GATE starts is run (`testservices.ProcessRunner`);
    #: None runs the binaries. The tests replace it.
    service_runner: Any = None
    #: The paths a check changed, put back (or not) after its round (#1964).
    _restored: set[str] = field(default_factory=set, init=False, repr=False)
    _unrestored: set[str] = field(default_factory=set, init=False, repr=False)

    def run(self) -> dict[str, Any]:
        record = self.record
        record.update({"rebase": None, "rounds": [], "fix_turn": False,
                       "second_turn": False, "broken_by_fix_turn": [],
                       "conflict_turn": False, "failed": [], "pr_section": False,
                       "guards": self._guards, "restored": []})
        if self.remaining() < MIN_GATE_SECONDS:
            record["skipped"] = "budget"
            self.log.warning("too little of the step's budget is left for the pre-publish "
                             "checks; publishing without them",
                             remaining_seconds=round(self.remaining(), 1))
            return record
        base = (self.env.get(CLONE_BASE_ENV) or "").strip()
        rebase = self._rebase()
        record["rebase"] = rebase
        if rebase and rebase.get("status") in ("rebased", "conflict_resolved"):
            base = str(rebase["onto"])
        elif base and base != EMPTY_BASE:
            merged = merged_main_base(self.repo, base, runner=self.runner, env=self.env)
            if merged is not None:
                self.log.info("HEAD holds a merge of the default branch; diffing from the "
                              "merge base", clone_base=base[:12], merge_base=merged[:12])
                base = merged
        record["base"] = base or None
        self._snapshot, graph_status = load_snapshot(
            self.context if self.context is not None else context_dir(self.repo, self.env))
        record.update({"graph": {"snapshot": graph_status, "selected": {}},
                       "test_reach": None, "self_check": False, "advisory_section": False})
        outcomes: list[Outcome] = []
        nothing = adds_nothing(self.repo, base, self.staged_patches, runner=self.runner,
                               env=self.env) if base else None
        if not base:
            record["skipped"] = "unknown_base"
        elif nothing:
            # A step that adds no commit of its own re-pays nothing (#2981).
            record.update(skipped="adds_nothing", skipped_reason=nothing)
            self.log.info("the branch adds nothing of this step's own; no pre-publish checks",
                          reason=nothing)
        else:
            outcomes = self._round(base)
            # A timed-out guard run is no fix turn's to fix, and never runs
            # as-is again: the next round runs it in the parts it advised.
            # Only the guard script is split; any other timed-out check runs
            # as-is again after a fix turn (#2503).
            timed_out = [o for o in outcomes if o.timed_out]
            parts = max((guards_parts(o.output) for o in timed_out
                         if o.name == GUARDS_CHECK), default=0)
            failed = [o for o in outcomes if o.status == FAILED and not o.timed_out]
            if failed:
                self.log.warning("pre-publish checks failed; one fix turn",
                                 failed=[o.name for o in failed])
                if self.resume(fix_prompt(failed, self.secrets, timed_out,
                                          remaining=self.remaining())):
                    record["fix_turn"] = True
                    before = {o.name: o.status for o in outcomes}
                    outcomes = self._round(base, guard_parts=parts)
                    outcomes = self._second_turn(base, before, outcomes, parts)
                    parts = 0
                else:
                    record["fix_skipped"] = "no_session_or_budget"
            if parts:
                self.log.warning("the guard script ran out of time; running it in parts",
                                 parts=parts)
                again = {o.name: o for o in self._round(base, guard_parts=parts,
                                                        only=GUARDS_CHECK)}
                outcomes = [again.pop(o.name, o) for o in outcomes] + list(again.values())
        record["failed"] = [o.name for o in outcomes if o.status == FAILED]
        record["restored"] = sorted(self._restored)
        if self._unrestored:
            record["unrestored"] = sorted(self._unrestored)
        section = pr_section(rebase, outcomes, self.secrets)
        target = Path(self.artifacts_dir) / PR_SECTION_NAME
        if section:
            target.write_text(section, encoding="utf-8")
            record["pr_section"] = True
            self.log.warning("pre-publish checks still fail; publishing with their output in "
                             "the pull request body", failed=record["failed"])
        else:
            target.unlink(missing_ok=True)
        self._advise(outcomes)
        return record

    def _second_turn(self, base: str, before: Mapping[str, str],
                     outcomes: list[Outcome], parts: int) -> list[Outcome]:
        """F07 (#2379): a check that passed before the fix turn and fails
        after it was broken BY that turn, and published as-is it is a red CI
        run the gate saw coming. One more turn, with what fails now, when the
        step's budget still holds a round (`MIN_GATE_SECONDS`, the bound the
        gate starts under); otherwise what the fix turn left is published."""
        failed = [o for o in outcomes if o.status == FAILED and not o.timed_out]
        broke = [o.name for o in failed if before.get(o.name) == PASSED]
        self.record["broken_by_fix_turn"] = broke
        if not broke:
            return outcomes
        if self.remaining() < MIN_GATE_SECONDS:
            self.record["second_turn_skipped"] = "budget"
            self.log.warning("the fix turn broke a check that had passed; too little of the "
                             "step's budget is left for another turn", broke=broke,
                             remaining_seconds=round(self.remaining(), 1))
            return outcomes
        self.log.warning("the fix turn broke a check that had passed; one more turn",
                         broke=broke, failed=[o.name for o in failed])
        timed_out = [o for o in outcomes if o.timed_out]
        if not self.resume(fix_prompt(failed, self.secrets, timed_out, broke=broke,
                                      remaining=self.remaining())):
            self.record["second_turn_skipped"] = "no_session_or_budget"
            return outcomes
        self.record["second_turn"] = True
        return self._round(base, guard_parts=parts)

    def _advise(self, outcomes: Sequence[Outcome]) -> None:
        """The advisory section: never a failure, never sent to the agent."""
        record = self.record
        target = Path(self.artifacts_dir) / ADVISORY_SECTION_NAME
        if self._diff is None:
            target.unlink(missing_ok=True)
            return
        resolved, files = self._diff
        reach = None
        if self._snapshot is not None:
            try:
                reach = measure_test_reach(self.repo, resolved, files, self._snapshot,
                                           runner=self.runner, env=self.env)
            except (SnapshotError, OSError) as exc:
                self.log.warning("test reach could not be measured", error=type(exc).__name__)
        record["test_reach"] = reach
        gap = None
        if self.judge is not None and self._snapshot is not None:
            try:
                gap = jev_test_gap(self.judge, self.repo, resolved, files, self._snapshot,
                                   list(dict(record["graph"].get("selected") or {})),
                                   runner=self.runner, env=self.env)
            except (SnapshotError, OSError) as exc:
                self.log.warning("the J4 test gap could not be asked", error=type(exc).__name__)
        if gap is not None:
            # Only when Jev answered: a failed call leaves the record as it was.
            record["jev_test_gap"] = {k: gap[k] for k in ("asked", "tests", "uncovered")}
        self_check = read_self_check(self.artifacts_dir)
        record["self_check"] = self_check is not None
        target.write_text(advisory_section(graph=record["graph"], outcomes=outcomes,
                                           reach=reach, self_check=self_check,
                                           secrets=self.secrets, jev_gap=gap,
                                           guards=self._guards,
                                           restored=sorted(self._restored),
                                           unrestored=sorted(self._unrestored)),
                          encoding="utf-8")
        record["advisory_section"] = True

    # -- the checks ---------------------------------------------------------

    def _round(self, base: str, *, guard_parts: int = 0, only: str | None = None
               ) -> list[Outcome]:
        """One pass of the check set (only the check named `only`, when
        given). `guard_parts` > 0 runs the guard script as that many
        `--shard` parts: the round after it timed out."""
        resolved = diff_base(self.repo, base, runner=self.runner, env=self.env)
        files = None if resolved is None else changed_files(
            self.repo, resolved, runner=self.runner, env=self.env)
        if files is None:
            outcome = Outcome("diff", ERROR, 0.0, (), "git diff --name-only",
                              output="git could not list the changed files",
                              reason="the diff could not be read")
            self.record["rounds"].append([outcome.record()])
            return [outcome]
        self._diff = (resolved, files)
        self.record.setdefault("graph", {})["selected"] = select_tests(
            self.repo, files, self._snapshot)
        checks = select_checks(self.repo, files, base=resolved, env=self.env, python=self.python,
                               snapshot=self._snapshot)
        if only is not None:
            checks = [c for c in checks if c.name == only]
        checks = [self._guards_check(c, resolved, guard_parts) if c.name == GUARDS_CHECK else c
                  for c in checks]
        services = self._services(files, checks)
        env = self.env
        if services.ready:
            # §5.1: the checks' environment only; the gate's own -- which
            # the agent's session continues with -- never carries it.
            env = {**self.env, **service_env(services.ready)}
            checks = [watched(c, services.stopped) if c.name in SERVICE_CHECKS else c
                      for c in checks]
        else:
            env = {k: v for k, v in self.env.items() if k != TEST_SERVICES_ENV}
        # Each check's limit is read as it starts, from this deadline (#2274).
        deadline = self.clock() + self.remaining()
        state = record_tree(self.repo, runner=self.runner, env=self.env)
        try:
            outcomes = run_checks(self.repo, checks, env=env, runner=self.runner,
                                  timeout=CHECK_TIMEOUT_SECONDS, clock=self.clock,
                                  deadline=deadline)
        finally:
            # §5.2 step 5: what the gate started goes with the round, so a
            # fix round starts it afresh. The worker's own runs on (Q4).
            services.stop()
        if services.outcome is not None:
            outcomes.insert(0, services.outcome)
        self._restore(state)
        self.record["rounds"].append([o.record() for o in outcomes])
        self.log.info("pre-publish checks ran", changed_files=len(files),
                      checks=[f"{o.name}:{o.status}" for o in outcomes])
        return outcomes

    def _services(self, files: Sequence[str], checks: Sequence[Check]) -> _RoundServices:
        """§5.2 steps 1-2: read `.swarm/test-services.toml` from the tree being
        checked (§2.3), and when a check that runs pytest is selected, find
        each declared service answering or start it.

        The worker starts a valid declaration's services after the clone and
        keeps them for the whole attempt (TS-RUN, Q4), in ITS process; the
        gate runs in the runner's, so it asks the address (`probe_argv`).
        Nothing answering -- a declaration the agent's change added, or a
        service that died -- the gate starts one itself, for this round.
        """
        out = _RoundServices(answers=self._answers)
        declaration = testservices.read_declaration(self.repo)
        where = testservices.DECLARATION_PATH
        if not declaration.valid:
            # §2.2: the agent's own file is its error to fix; the base's is not.
            mine = where in files
            out.outcome = Outcome(
                TEST_SERVICES_CHECK, FAILED if mine else ERROR, 0.0, (where,), f"read {where}",
                output=declaration.report(),
                reason=("the declaration this change made is invalid" if mine else
                        f"the base branch's {where} is invalid; nothing was started"))
            return out
        if not declaration.declared or not any(
                c.run is not None and c.name in SERVICE_CHECKS for c in checks):
            return out
        started = self.clock()
        down = tuple(s for s in declaration.services if not self._answers(s))
        out.ready = [s for s in declaration.services if s not in down]
        if not down:
            return out
        out.root = Path(tempfile.mkdtemp(prefix="pp-svc-", dir=_outside(self.repo, self.env)))
        out.running = testservices.start(
            testservices.Declaration(present=True, services=down), out.root,
            scrub=lambda text: redact_text(scrub_text(text, self.secrets)).text,
            runner=self.service_runner)
        out.ready += [p.service for p in out.running.services if p.error is None]
        errors = out.running.errors()
        if errors:
            out.outcome = Outcome(
                TEST_SERVICES_CHECK, ERROR, self.clock() - started, (where,),
                f"start the services {where} declares", output="\n\n".join(errors),
                reason="a declared service did not start; the tests ran without it")
        self.log.info("test services for the pre-publish checks",
                      ready=[s.name for s in out.ready], started_by_gate=len(down),
                      errors=len(errors))
        return out

    def _answers(self, service: testservices.DeclaredService) -> bool:
        return self.runner(probe_argv(service), self.repo, self.env,
                           SERVICE_PROBE_SECONDS).code == 0

    def _restore(self, state: TreeState | None) -> None:
        """Put back what this round's checks changed in the tree, and say so once per path."""
        result = None if state is None else restore_tree(self.repo, state, runner=self.runner,
                                                         env=self.env)
        if result is None:
            self.log.warning("git could not list the working tree around the pre-publish "
                             "checks; anything they changed in it is published as they left it")
            return
        restored, failed = result
        new = [p for p in restored if p not in self._restored]
        if new:
            self.log.warning("the pre-publish checks changed the working tree; restored "
                             "before publishing", count=len(new), paths=new[:RESTORED_SHOWN])
        if failed:
            self.log.error("the pre-publish checks changed paths that could not be restored",
                           count=len(failed), paths=failed[:RESTORED_SHOWN])
        self._restored.update(restored)
        self._unrestored.update(failed)

    def _guards_check(self, check: Check, base: str, parts: int) -> Check:
        """The guard script's check for this round: skipped when a run already
        passed on this tree from this base, else as `parts` shards after a
        timeout, else as selected."""
        if check.run is None:
            return check
        why = guards_pass_on(self.repo, base, runner=self.runner, env=self.env)
        if why:
            self._guards.append(f"skipped: {why}.")
            self.log.info("the guard script already passed on this tree; not run again")
            return Check(check.name, check.files, check.command, skipped=why)
        why = "the previous run timed out"
        if not parts:
            parts = guards_split(self.repo, base, runner=self.runner, env=self.env,
                                 timeout=max(MIN_CHECK_SECONDS, min(
                                     GUARDS_LIST_SECONDS,
                                     self.remaining() - CHECK_MARGIN_SECONDS)))
            why = "the script said its selection is too heavy for one run"
        if parts:
            argv = ["bash", GUARDS_SCRIPT, "--base", base]
            shards = " ... ".join(f"--shard {k}/{parts}" for k in (1, parts))
            self._guards.append(f"{why}, so this round ran it as {parts} "
                                f"parts ({shards}), each its own command under the "
                                f"{int(CHECK_TIMEOUT_SECONDS)} s check limit.")
            return Check(check.name, check.files, f"{' '.join(argv)} {shards}",
                         run=_sharded_guards(argv, parts, self.remaining))
        return check

    # -- the rebase ---------------------------------------------------------

    def _rebase(self) -> dict[str, Any] | None:
        ref = (self.env.get(PREPUBLISH_BASE_ENV) or "").strip()
        if not ref:
            return None
        out: dict[str, Any] = {"ref": ref, "status": "skipped"}
        if self.submit is None:
            out["reason"] = "the git tools are not offered to this attempt"
            return out
        if self._in_progress():
            out["reason"] = "a rebase or merge the agent started is still in progress"
            return out
        started = self.clock()
        answer = self.submit("git_rebase_onto", {"ref": ref})
        out["duration_seconds"] = round(self.clock() - started, 3)
        refused = answer.get("refused") if isinstance(answer, dict) else None
        result = answer.get("result") if isinstance(answer, dict) else None
        if refused or not isinstance(result, dict):
            code = refused.get("code") if isinstance(refused, dict) else "no_answer"
            out.update(status="refused", reason=str(code))
            self.log.warning("the pre-publish rebase was not made; publishing on the "
                             "original base", code=code)
            return out
        onto = str(result.get("onto") or "")
        conflicts = [str(p) for p in result.get("conflicts") or []]
        out.update(onto=onto, conflicts=conflicts)
        if not conflicts:
            out["status"] = "rebased"
            self.log.info("rebased onto the current base before publishing", ref=ref,
                          onto=onto[:12])
            return out
        self.log.warning("the pre-publish rebase conflicted; one conflict turn",
                         conflicts=conflicts[:25])
        if self.resume(conflict_prompt(ref, onto, conflicts)):
            self.record["conflict_turn"] = True
        if not self._in_progress() and self._descends(onto):
            out["status"] = "conflict_resolved"
            return out
        self._abort()
        out["status"] = "conflict_unresolved"
        return out

    def _git_dir(self) -> Path:
        return self.repo / ".git"

    def _in_progress(self) -> bool:
        g = self._git_dir()
        return any((g / name).exists() for name in
                   ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"))

    def _descends(self, onto: str) -> bool:
        if not onto:
            return False
        ran = _git(self.repo, "merge-base", "--is-ancestor", onto, "HEAD",
                   runner=self.runner, env=self.env)
        return ran.code == 0

    def _abort(self) -> None:
        if (self._git_dir() / "rebase-merge").exists() or (self._git_dir() / "rebase-apply").exists():
            ran = _git(self.repo, "rebase", "--abort", runner=self.runner, env=self.env)
            if ran.code != 0:
                self.log.error("git rebase --abort failed after the conflict turn",
                               exit_code=ran.code)


def deadline_left(env: Mapping[str, str], *, now: Callable[[], float] = time.time
                  ) -> float | None:
    """Seconds until the worker's kill (`RUNNER_DEADLINE_ENV`), or None when
    the worker named no deadline (a runner started by hand)."""
    raw = str(env.get(RUNNER_DEADLINE_ENV, "")).strip()
    try:
        instant = float(raw)
    except ValueError:
        return None
    return instant - now() if math.isfinite(instant) else None


def check_env(env: Mapping[str, str], *, drop: Iterable[str], base: str | None) -> dict[str, str]:
    """The checks' environment: the agent's, without its provider credential."""
    dropped = set(drop)
    out = {k: v for k, v in env.items() if k not in dropped}
    if base:
        out[CLONE_BASE_ENV] = base
    return out
