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
   check's output, then runs the set again.
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
  (the package's `typecheck` script's arguments, with its own tsc), and
  `terraform fmt -check` over changed `.tf` files (#1217).

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

A CHECK THAT CANNOT RUN IS NOT A FAILURE. A tool missing from the image,
`tflint --init` that cannot reach its plugin source, an unknown base: the check
is `skipped` (or `error`) with the reason, recorded, and never sent to the
agent as something to fix. tflint is the expected case: agent-runtime-base
installs it only with `INSTALL_TFLINT=1`, which is off because no tflint
release passes the image promote gate (images/agent-runtime-base/Dockerfile).

NOTHING SECRET-SHAPED LEAVES. The checks run without the provider credential
in their environment, and every byte of output is scrubbed of the runner's
own secrets and then of every credential pattern (`swarm_redaction.redact`)
before it reaches a prompt, a log or the pull request.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from swarm_redaction import redact as redact_text

from .graphmcp.snapshot import Snapshot, SnapshotError, load_freshness_record, path_of
from .redact import scrub_text

#: Set by the worker to the base BRANCH the lane is rebased onto, and only when
#: that is safe for this step (`lifecycle.Worker._prepublish_rebase_ref`).
#: Unset: no rebase, and the checks diff from the clone base.
PREPUBLISH_BASE_ENV = "SWARM_PREPUBLISH_BASE"
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
FIX_TURNS = 1
CONFLICT_TURNS = 1
#: Below this much of the step's budget the gate does not start: a check set
#: that cannot finish would only turn a success into a timeout.
MIN_GATE_SECONDS = 120.0
#: Any one check. changed-guards.sh holds itself under ten minutes.
CHECK_TIMEOUT_SECONDS = 600.0
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
        out, err = proc.communicate()
        text = out.decode("utf-8", errors="replace")
        return Ran(124, f"{text}\n[timed out after {int(timeout)}s]", timed_out=True,
                   errors=(err or b"").decode("utf-8", errors="replace"))


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

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(self.duration_seconds, 3),
            "files": len(self.files),
        }
        if self.reason:
            out["reason"] = self.reason
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


def _package_root(repo: Path, path: str) -> str | None:
    """The nearest directory above `path` holding a package.json, relative to `repo`."""
    parts = path.split("/")[:-1]
    while parts:
        if (repo / "/".join(parts) / "package.json").is_file():
            return "/".join(parts)
        parts.pop()
    return "" if (repo / "package.json").is_file() else None


def _node_bin(repo: Path, package: str, tool: str) -> Path | None:
    candidate = repo / package / "node_modules" / ".bin" / tool
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _relative(package: str, path: str) -> str:
    return path[len(package) + 1:] if package else path


def _in_package(repo: Path, package: str, binary: Path, args: Sequence[str]
                ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    """Run a package's own tool from the package directory, so its config applies."""
    def run(runner: Runner, root: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        return _verdict(runner([str(binary), *args], Path(root) / package if package else
                               Path(root), env, timeout))
    return run


def _typecheck_args(repo: Path, package: str) -> list[str]:
    """The arguments of the package's own `typecheck` script when it is a plain
    `tsc ...`, else `--noEmit -p tsconfig.json`."""
    try:
        scripts = json.loads((repo / package / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        scripts = {}
    words = str(scripts.get("typecheck") or "").split()
    if words and words[0] == "tsc" and not any(w in ("&&", "||", ";", "|") for w in words):
        return words[1:]
    return ["--noEmit", "-p", "tsconfig.json"]


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


def _graph_pytest(tests: Sequence[str], base: str
                  ) -> Callable[[Runner, Path, Mapping[str, str], float], tuple[str, str]]:
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
        already = _guards_selection(runner, repo, env, base, timeout)
        rest = [t for t in tests if t not in already]
        if not rest:
            return PASSED, "every selected test file is already run by changed-guards"
        argv = ["uv", "run", "--quiet", "pytest", "-q", "-n", "auto", "-p", "no:cacheprovider",
                *rest]
        return _verdict(runner(argv, repo, env, timeout))
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
    """graph-pytest, vitest, tsc and terraform-fmt for this diff (CTXQ-G)."""
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
                                "uv run pytest -q -n auto <graph-selected files>",
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
        args = _typecheck_args(repo, package)
        checks.append(Check("tsc", tuple(members), f"tsc {' '.join(args)} (in {where})",
                            run=_in_package(repo, package, binary, args)))

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

    if (repo / "scripts" / "changed-guards.sh").is_file():
        argv = ["bash", "scripts/changed-guards.sh", "--base", base]
        checks.append(Check("changed-guards", tuple(files), " ".join(argv), run=_simple(argv)))

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
               clock: Callable[[], float] = time.monotonic) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for check in checks:
        if check.run is None:
            outcomes.append(Outcome(check.name, SKIPPED, 0.0, check.files, check.command,
                                    reason=check.skipped))
            continue
        started = clock()
        try:
            status, output = check.run(runner, repo, env, timeout)
        except OSError as exc:
            status, output = ERROR, f"{type(exc).__name__}: the check could not run"
        outcomes.append(Outcome(check.name, status, clock() - started, check.files,
                                check.command, output=output,
                                reason=None if status != ERROR else "the check could not run"))
    return outcomes


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


def fix_prompt(failed: Sequence[Outcome], secrets: Sequence[str]) -> str:
    lines = [
        "Before publishing your branch, the worker ran the repository's own checks on the "
        "files you changed, and these failed. You have ONE turn to fix them: fix the cause "
        "in the code (not the check), commit, and end with a short report. The checks run "
        "again after this turn; whatever still fails is published with its output in the "
        "pull request body for the operator.",
    ]
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
    bad = [o for o in outcomes if o.status in (FAILED, ERROR)]
    conflicted = bool(rebase and rebase.get("status") == "conflict_unresolved")
    if not bad and not conflicted:
        return None
    parts = [
        PR_SECTION_HEADING, "",
        "The worker ran these checks on the files this branch changes before publishing, "
        "gave the agent one turn to fix them, and they still did not pass. The branch was "
        "published anyway so no work is lost; read this before merging.",
    ]
    if conflicted and rebase is not None:
        conflicts = list(rebase.get("conflicts") or [])
        parts += ["", f"### rebase onto origin/{rebase.get('ref')} conflicted",
                  f"The conflict turn did not finish the rebase, so it was aborted and the branch "
                  f"is on its original base. Conflicting files: {_files_line(conflicts)}"]
    for outcome in bad:
        verb = "failed" if outcome.status == FAILED else "could not run"
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
                     secrets: Sequence[str], jev_gap: Mapping[str, Any] | None = None) -> str:
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
    ran = [o for o in outcomes if o.name in ("graph-pytest", "vitest", "tsc", "terraform-fmt")]
    if ran:
        parts.append("Ran: " + ", ".join(
            f"{o.name} {o.status}" + (f" ({o.reason})" if o.status == SKIPPED and o.reason
                                       else "") for o in ran) + ".")

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
    _snapshot: Snapshot | None = field(default=None, init=False, repr=False)
    _diff: tuple[str, list[str]] | None = field(default=None, init=False, repr=False)

    def run(self) -> dict[str, Any]:
        record = self.record
        record.update({"rebase": None, "rounds": [], "fix_turn": False,
                       "conflict_turn": False, "failed": [], "pr_section": False})
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
        record["base"] = base or None
        self._snapshot, graph_status = load_snapshot(
            self.context if self.context is not None else context_dir(self.repo, self.env))
        record.update({"graph": {"snapshot": graph_status, "selected": {}},
                       "test_reach": None, "self_check": False, "advisory_section": False})
        outcomes: list[Outcome] = []
        if not base:
            record["skipped"] = "unknown_base"
        else:
            outcomes = self._round(base)
            if any(o.status == FAILED for o in outcomes):
                failed = [o for o in outcomes if o.status == FAILED]
                self.log.warning("pre-publish checks failed; one fix turn",
                                 failed=[o.name for o in failed])
                if self.resume(fix_prompt(failed, self.secrets)):
                    record["fix_turn"] = True
                    outcomes = self._round(base)
                else:
                    record["fix_skipped"] = "no_session_or_budget"
        record["failed"] = [o.name for o in outcomes if o.status == FAILED]
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
                                           secrets=self.secrets, jev_gap=gap),
                          encoding="utf-8")
        record["advisory_section"] = True

    # -- the checks ---------------------------------------------------------

    def _round(self, base: str) -> list[Outcome]:
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
        timeout = max(30.0, min(CHECK_TIMEOUT_SECONDS, self.remaining() - 30.0))
        outcomes = run_checks(self.repo, checks, env=self.env, runner=self.runner,
                              timeout=timeout, clock=self.clock)
        self.record["rounds"].append([o.record() for o in outcomes])
        self.log.info("pre-publish checks ran", changed_files=len(files),
                      checks=[f"{o.name}:{o.status}" for o in outcomes])
        return outcomes

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


def check_env(env: Mapping[str, str], *, drop: Iterable[str], base: str | None) -> dict[str, str]:
    """The checks' environment: the agent's, without its provider credential."""
    dropped = set(drop)
    out = {k: v for k, v in env.items() if k not in dropped}
    if base:
        out[CLONE_BASE_ENV] = base
    return out
