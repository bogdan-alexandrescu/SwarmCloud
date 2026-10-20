"""shellcheck runs in parallel, and on a pull request only over what the change reaches.

LANE CI-EARLY (part of #1694, owner decision 2026-10-20). `shellcheck` was the
slowest check on 19 of 65 pull-request heads, at a 291 s median: one serial
`shellcheck -x` over every script, whatever the change touched.
`scripts/lib/shellcheck-ci.sh` now runs it:

  * over the runner's cores (`xargs -P`), each file still its own
    `shellcheck -x` root, so a finding is the finding the serial run made;
  * on main, a push or a queue entry -- no `--changed` -- over exactly the set
    the serial command named, so main still checks everything;
  * on a pull request, over the changed scripts, the libraries they source
    (their `# shellcheck source=` directives, what `-x` follows), and every
    script that sources a changed file, through any chain: under `-x` a
    library is analysed in its callers' context, so a change to common.sh can
    only be judged in the scripts that source it.

Nothing here needs shellcheck itself: the runs use a fake one on PATH that
records what it was handed.
"""

from __future__ import annotations

import glob
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "shellcheck-ci.sh"
APPLICATION = REPO / ".github" / "workflows" / "application.yml"

#: The roots the serial command named (application.yml's shell job, read from
#: main at 0760d29). Main must keep checking exactly these.
MAIN_GLOBS = (
    "scripts/*.sh",
    "scripts/lib/*.sh",
    "scripts/lib/guard-bin/*",
    "scripts/acceptance/*.sh",
    "scripts/acceptance/groups/*.sh",
)


def _main_set() -> list[str]:
    found: set[str] = set()
    for pattern in MAIN_GLOBS:
        found |= {str(Path(p).relative_to(REPO)) for p in glob.glob(str(REPO / pattern)) if Path(p).is_file()}
    return sorted(found)


def _run(tmp_path: Path, *args: str, changed: list[str] | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess[str]:
    argv = ["bash", str(SCRIPT), *args]
    if changed is not None:
        listing = tmp_path / "changed"
        listing.write_text("".join(f"{c}\n" for c in changed))
        argv += ["--changed", str(listing)]
    return subprocess.run(argv, cwd=REPO, env={**os.environ, **(env or {})},
                          capture_output=True, text=True, timeout=300)


def _listed(tmp_path: Path, changed: list[str] | None = None) -> list[str]:
    proc = _run(tmp_path, "--list", changed=changed)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return [line for line in proc.stdout.splitlines() if line]


def test_the_script_is_executable_and_strict():
    """MUTATION: drop the executable bit, or put anything before `set -euo pipefail`."""
    assert SCRIPT.is_file(), "scripts/lib/shellcheck-ci.sh is missing"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "scripts/lib/shellcheck-ci.sh is not executable"
    effective = [
        line for i, line in enumerate(SCRIPT.read_text().splitlines())
        if not (i == 0 and line.startswith("#!")) and line.strip() and not line.lstrip().startswith("#")
    ]
    assert effective[0] == "set -euo pipefail", effective[0]


def test_without_a_change_list_it_checks_every_script_main_checks(tmp_path):
    """Main keeps `shellcheck -x scripts/*.sh scripts/lib/*.sh ...` exactly.
    MUTATION: drop scripts/acceptance/groups/*.sh (or any glob) from the script's set."""
    listed = _listed(tmp_path)
    assert listed == _main_set(), set(listed) ^ set(_main_set())
    assert len(listed) > 100, f"only {len(listed)} script(s) listed: the set did not expand"


def test_the_set_is_the_one_the_other_shell_steps_walk(tmp_path):
    """The strict-mode step names the same globs; two sets drift apart.
    MUTATION: add a glob to the strict step's loop and not to the script."""
    steps = yaml.safe_load(APPLICATION.read_text())["jobs"]["shell"]["steps"]
    (strict,) = [s for s in steps if s.get("name") == "scripts are executable and strict"]
    loop = re.search(r"for f in ([^;]+);", strict["run"])
    assert loop, strict["run"]
    assert tuple(loop.group(1).split()) == MAIN_GLOBS, loop.group(1)


def test_a_changed_script_brings_the_libraries_it_sources(tmp_path):
    """bench-cost.sh sources lib/common.sh and lib/benchlib.sh.
    MUTATION: select only the changed files, without their sourced libraries."""
    listed = _listed(tmp_path, ["scripts/bench-cost.sh"])
    for want in ("scripts/bench-cost.sh", "scripts/lib/common.sh", "scripts/lib/benchlib.sh"):
        assert want in listed, (want, listed)
    assert "scripts/api.sh" not in listed, listed
    assert len(listed) < len(_main_set()), "a leaf script's change checked every script"


def test_a_changed_library_brings_every_script_that_sources_it(tmp_path):
    """MUTATION: drop the reverse walk (callers of a changed library)."""
    listed = _listed(tmp_path, ["scripts/lib/benchlib.sh"])
    for want in ("scripts/lib/benchlib.sh", "scripts/bench-cost.sh", "scripts/bench-coldstart.sh",
                 "scripts/bench-dispatch.sh"):
        assert want in listed, (want, listed)
    assert "scripts/api.sh" not in listed, listed


def test_a_changed_common_sh_checks_every_script_that_sources_it(tmp_path):
    """MUTATION: walk callers one level only, or by the source line instead of the directive."""
    listed = set(_listed(tmp_path, ["scripts/lib/common.sh"]))
    sourcing = {
        p for p in _main_set()
        if re.search(r"#\s*shellcheck\s+source=(?:\S*/)?common\.sh\b", (REPO / p).read_text(errors="replace"))
    }
    assert sourcing, "no script carries a `# shellcheck source=...common.sh` directive"
    assert sourcing <= listed, sorted(sourcing - listed)[:10]


def test_a_file_outside_the_set_is_never_a_root_but_its_callers_are(tmp_path):
    """kubernetes/cluster-network.sh is sourced, never named by main's command.
    MUTATION: add every changed shell file as a root."""
    listed = _listed(tmp_path, ["kubernetes/cluster-network.sh"])
    assert "scripts/apply-tenant-network-policies.sh" in listed, listed
    assert "kubernetes/cluster-network.sh" not in listed, listed


@pytest.mark.parametrize("changed", [["docs/ci.md"], ["apps/swarm-api/swarm_api/main.py", "README.md"], []])
def test_a_change_that_reaches_no_script_checks_none(tmp_path, changed):
    """MUTATION: fall back to the whole set when nothing is selected."""
    assert _listed(tmp_path, changed) == []


@pytest.mark.parametrize("changed", [
    "scripts/lib/shellcheck-ci.sh",
    ".shellcheckrc",
    ".github/workflows/application.yml",
])
def test_a_change_to_how_shellcheck_runs_checks_everything(tmp_path, changed):
    """MUTATION: treat the runner itself, its config or the workflow as an ordinary file."""
    assert _listed(tmp_path, ["docs/ci.md", changed]) == _main_set()


def test_a_deleted_script_is_not_handed_to_shellcheck(tmp_path):
    """MUTATION: list every changed path under scripts/ whether or not it exists."""
    listed = _listed(tmp_path, ["scripts/no-such-script-anymore.sh", "scripts/bench-cost.sh"])
    assert "scripts/no-such-script-anymore.sh" not in listed
    assert "scripts/bench-cost.sh" in listed


def test_an_unreadable_change_list_is_refused(tmp_path):
    """MUTATION: read a missing --changed file as an empty change."""
    proc = subprocess.run(["bash", str(SCRIPT), "--list", "--changed", str(tmp_path / "absent")],
                          cwd=REPO, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, proc.stdout


def _fake_shellcheck(tmp_path: Path, *, bad: str = "", status: int = 1) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    fake = bindir / "shellcheck"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"log={str(log)!r}\n"
        "start=\"$(date +%s.%N)\"\n"
        "sleep 0.4\n"
        "printf '%s %s %s\\n' \"$start\" \"$(date +%s.%N)\" \"$*\" >> \"$log\"\n"
        "rc=0\n"
        "for f in \"$@\"; do\n"
        f"  if [ \"$f\" = {bad!r} ]; then echo \"In $f line 1:\"; echo \"SC2086 (info): Double quote\"; rc={status}; fi\n"
        "done\n"
        "exit \"$rc\"\n"
    )
    fake.chmod(0o755)
    return bindir, log


def _calls(log: Path) -> list[tuple[float, float, list[str]]]:
    out = []
    for line in log.read_text().splitlines():
        start, end, *args = line.split()
        out.append((float(start), float(end), args))
    return out


def test_it_hands_every_file_to_shellcheck_x_once_in_parallel(tmp_path):
    """MUTATION: run with `-P 1` (serial), or drop `-x`."""
    bindir, log = _fake_shellcheck(tmp_path)
    proc = _run(tmp_path, "--jobs", "4", changed=["scripts/lib/benchlib.sh"],
                env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(log)
    assert all(args[0] == "-x" for _, _, args in calls), calls
    handed = [f for _, _, args in calls for f in args[1:]]
    assert sorted(handed) == _listed(tmp_path, ["scripts/lib/benchlib.sh"]), handed
    assert len(handed) == len(set(handed)), "a file was checked twice"
    assert len(calls) > 1, "one serial shellcheck call"
    overlap = any(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(calls) for b in calls[i + 1:])
    assert overlap, "no two shellcheck calls ran at the same time"
    assert f"{len(handed)} file(s)" in proc.stdout + proc.stderr, "it did not say how many files it visited"


def test_a_finding_in_any_file_fails_the_run_and_is_printed(tmp_path):
    """MUTATION: ignore xargs' exit status, or drop a batch's output."""
    bindir, _ = _fake_shellcheck(tmp_path, bad="scripts/bench-cost.sh")
    proc = _run(tmp_path, "--jobs", "3", changed=["scripts/lib/benchlib.sh"],
                env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "In scripts/bench-cost.sh line 1:" in proc.stdout, proc.stdout


def test_a_shellcheck_that_crashes_fails_the_run(tmp_path):
    """exit 2 is shellcheck's own failure, not a finding. MUTATION: treat only exit 1 as red."""
    bindir, _ = _fake_shellcheck(tmp_path, bad="scripts/lib/benchlib.sh", status=2)
    proc = _run(tmp_path, changed=["scripts/lib/benchlib.sh"],
                env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
    assert proc.returncode != 0, proc.stdout + proc.stderr


def test_nothing_to_check_passes_and_says_so(tmp_path):
    """MUTATION: hand xargs an empty list (GNU xargs runs shellcheck with no file once)."""
    bindir, log = _fake_shellcheck(tmp_path)
    proc = _run(tmp_path, changed=["docs/ci.md"], env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not log.exists(), log.read_text()
    assert "0 file(s)" in proc.stdout + proc.stderr


def _shell_steps() -> list[dict]:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]["shell"]["steps"]


def test_the_shell_job_runs_the_script_with_the_pull_requests_change():
    """MUTATION: keep the serial `shellcheck -x ...` line in the shell job."""
    (step,) = [s for s in _shell_steps() if s.get("name") == "shellcheck"]
    run = step["run"]
    assert "scripts/lib/shellcheck-ci.sh" in run, run
    assert "shellcheck -x" not in run, run
    assert "--changed" in run, run
    assert step["env"].get("EVENT") == "${{ github.event_name }}", step.get("env")
    assert step["env"].get("CHANGED") == "${{ needs.changes.outputs.changed }}", step.get("env")
    # Only a pull request narrows; everything else runs the whole set.
    assert re.search(r'"\$\{EVENT\}" = "pull_request"', run), run


def test_the_changes_job_hands_the_shell_job_its_diff():
    """MUTATION: drop the `changed` output, so a pull request has nothing to narrow by."""
    job = yaml.safe_load(APPLICATION.read_text())["jobs"]["changes"]
    assert job["outputs"].get("changed") == "${{ steps.changed.outputs.changed }}", job["outputs"]
    (step,) = [s for s in job["steps"] if s.get("id") == "changed"]
    assert "git diff --name-only" in step["run"], step["run"]
