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

PASSED, FAILED, SKIPPED, ERROR = "passed", "failed", "skipped", "error"


# ---------------------------------------------------------------------------
# Running a command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ran:
    code: int
    output: str
    timed_out: bool = False


Runner = Callable[[Sequence[str], Path, Mapping[str, str], float], Ran]


def run_command(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout: float) -> Ran:
    """Run `argv` in its own process group; stdout and stderr together.

    The group is killed on timeout, so a pytest-xdist worker or a checkov
    subprocess does not outlive the check that started it.
    """
    try:
        proc = subprocess.Popen(
            list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except OSError as exc:
        return Ran(127, f"{argv[0]}: could not start ({type(exc).__name__})")
    try:
        out, _ = proc.communicate(timeout=max(1.0, timeout))
        return Ran(proc.returncode, out.decode("utf-8", errors="replace"))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        text = out.decode("utf-8", errors="replace")
        return Ran(124, f"{text}\n[timed out after {int(timeout)}s]", timed_out=True)


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


def _render_and_scan(python: str) -> Callable[[Runner, Path, Mapping[str, str], float],
                                              tuple[str, str]]:
    """`make security`'s Kubernetes half: checkov reads the RENDERED manifests,
    because a raw template's placeholder either reads as an unpinned image or
    makes checkov skip the file and still exit 0 (Makefile `security`)."""
    def run(runner: Runner, repo: Path, env: Mapping[str, str], timeout: float) -> tuple[str, str]:
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
                ran = runner([python, "kubernetes/render.py", *args], repo, env, timeout)
                if ran.code != 0:
                    return FAILED, f"kubernetes/render.py {' '.join(args)} failed:\n{ran.output}"
                (out / name).write_text(ran.output)
            return _verdict(runner(
                ["checkov", "-d", str(out), "--framework", "kubernetes", "--skip-check",
                 _K8S_SKIP, "--quiet", "--compact"], repo, env, timeout))
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


def select_checks(repo: Path, files: Sequence[str], *, base: str,
                  env: Mapping[str, str], python: str = "python3") -> list[Check]:
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
        return record

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
        checks = select_checks(self.repo, files, base=resolved, env=self.env, python=self.python)
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
