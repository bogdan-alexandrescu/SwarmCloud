"""`scripts/changed-guards.sh` picks the guard tests a diff can break (#642).

25% of lane PRs were red on their first CI run, mostly on repo-wide guard
tests that a lane's narrowed `pytest tests/unit/<area>` never reached: the docs
guards, the UI route seam, `test_specsign_covers` and the contract-parity
script. The script is the one pre-finish command a SwarmCloud agent runs: the
fixed guard set, plus every unit test file whose text names a changed path.

These tests hold its selection against fixture diffs, with `--list`, which
prints the selection and runs nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "changed-guards.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "changed_guards"

_spec = importlib.util.spec_from_file_location("select_unit_tests", REPO / "scripts" / "lib" / "select_unit_tests.py")
_sel = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_sel)

# The guard set, read from the script's own GUARD_TESTS array the way
# pull-request CI reads it (CI-GUARDS-ALWAYS, part of #1822): no copy here to
# drift. The ones every set must keep are held by name below.
GUARDS = tuple(_sel.guard_tests(REPO) or ())
DOC_GUARDS = tuple(_sel.guard_tests(REPO, "DOC_GUARD_TESTS") or ())
UNIT_GUARDS = tuple(g for g in GUARDS if g not in DOC_GUARDS)


def test_the_guard_set_keeps_the_repo_wide_guards_and_the_whole_tree_tests():
    # MUTATION: drop any of these from GUARD_TESTS and this is red. The last
    # two turned main red for 19 and 6 commits that no pull request ran them on.
    for guard in (
        "tests/unit/scripts/test_docs_describe_what_was_built.py",
        "tests/unit/scripts/test_docs_spec_amendments.py",
        "tests/unit/control_plane/test_runtimes_screen.py",
        "tests/unit/common/test_specsign_covers.py",
        "tests/unit/scripts/test_collection_stays_cheap.py",
        "tests/unit/common/test_awaiting_input_readers.py",
    ):
        assert guard in GUARDS, guard
    # MUTATION: a doc guard that is not in GUARD_TESTS is a second list -- red.
    assert DOC_GUARDS and set(DOC_GUARDS) <= set(GUARDS), DOC_GUARDS
    for guard in GUARDS:
        assert (REPO / guard).is_file(), guard


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "SWARM_CLONE_BASE"}
    env["NO_COLOR"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO, env=env, input=stdin, capture_output=True, text=True, timeout=120,
    )


def _selected(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("tests/")]


def test_the_script_has_the_house_shape():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.MULTILINE)
    assert "-n auto" in text
    assert "check-contract-parity.sh" in text


def test_a_fixture_diff_selects_the_guards_and_every_test_that_reads_a_changed_path():
    result = _run("--list", "--files", str(FIXTURES / "diff.txt"))
    assert result.returncode == 0, result.stderr
    selected = _selected(result.stdout)
    for guard in GUARDS:
        assert guard in selected
    # test_publish_scan_script.py reads scripts/publish-scan.sh by name.
    assert "tests/unit/scripts/test_publish_scan_script.py" in selected
    # Selection is de-duplicated and every entry exists.
    assert len(selected) == len(set(selected))
    for path in selected:
        assert (REPO / path).is_file(), path
    assert "scripts/lib/check-contract-parity.sh" in result.stdout


def test_a_generic_basename_does_not_select_every_test_that_mentions_it():
    # `__init__.py` is named by many tests that read other packages; only the
    # full path may select a reader for it.
    fixture = FIXTURES / "generic.txt"
    result = _run("--list", "--files", str(fixture))
    assert result.returncode == 0, result.stderr
    assert sorted(_selected(result.stdout)) == sorted(GUARDS)


def test_a_path_no_test_reads_selects_only_the_guard_set():
    result = _run("--list", "--files", str(FIXTURES / "unread-code.txt"))
    assert result.returncode == 0, result.stderr
    assert sorted(_selected(result.stdout)) == sorted(GUARDS)


# DOCS-ONLY (GUARDS-DEDUP, part of #1694). Docs-only lanes spent 44-50 min in
# guards on 2026-10-19/20, running the UI route seam, test_specsign_covers and
# the contract-parity check, none of which reads a doc (the parity check skips
# `.md` outright). A change made only of docs/** and *.md files selects the
# doc guards (DOC_GUARDS, above) and the tests that read the changed docs, and
# nothing else.


def test_a_docs_only_change_selects_only_the_doc_guards():
    result = _run("--list", "--files", str(FIXTURES / "unread.txt"))
    assert result.returncode == 0, result.stderr
    # MUTATION: dropping the docs-only branch in changed-guards.sh (selecting
    # GUARD_TESTS for every diff) turns this red with every guard.
    assert sorted(_selected(result.stdout)) == sorted(DOC_GUARDS)
    # MUTATION: printing/running the parity check for a docs-only diff turns this red.
    assert "check-contract-parity.sh" not in result.stdout
    assert "docs-only" in result.stderr


def _names_a_changed_doc(test: str, changed: tuple[str, ...]) -> bool:
    """The selector's reader rule: the test's text names a changed path, by
    its repo-relative path or its basename (none of these docs has a basename
    in GENERIC_BASENAMES)."""
    text = (REPO / test).read_text(encoding="utf-8")
    return any(name in text for path in changed for name in (path, path.rsplit("/", 1)[-1]))


def test_a_docs_only_change_still_selects_the_tests_that_read_the_changed_doc():
    changed = ("docs/runbooks/merge-app.md", "CLAUDE.md")
    result = _run("--list", "--files", "-", stdin="".join(p + "\n" for p in changed))
    assert result.returncode == 0, result.stderr
    selected = _selected(result.stdout)
    for guard in DOC_GUARDS:
        assert guard in selected
    # test_merge_step_docs.py reads docs/runbooks/merge-app.md by name.
    # MUTATION: skipping the reader grep for a docs-only diff turns this red.
    assert "tests/unit/scripts/test_merge_step_docs.py" in selected
    # A non-doc guard may also be a READER of a changed doc: test_auto_merge_
    # base_merge reads docs/runbooks/merge-app.md and test_issue_forms reads
    # CLAUDE.md (CI-GUARDS-THREE, part of #1822). Such a guard is selected as a
    # reader, by the same rule that picks test_merge_step_docs, not because the
    # guard set leaked in -- so it is allowed here only when its own text names
    # a changed doc, and every other non-doc guard is still refused.
    refused = [g for g in UNIT_GUARDS if not _names_a_changed_doc(g, changed)]
    # MUTATION: keeping the non-doc guards for a docs-only diff turns this red,
    # naming the first guard that reads neither doc.
    for unit_guard in refused:
        assert unit_guard not in selected, unit_guard
    # MUTATION: widen the allowance (say, let every guard through) and the
    # refused list empties; the route seam reads neither doc, so it must be in
    # it and must stay out of the selection -- red here.
    seam = "tests/unit/control_plane/test_runtimes_screen.py"
    assert seam in refused, refused
    assert seam not in selected
    assert "check-contract-parity.sh" not in result.stdout


def test_the_cheap_walkers_that_read_a_doc_are_in_the_guard_set():
    # The three CI-GUARDS-DOCS left out only because the docs-only assertion
    # above read them as leaks (CI-GUARDS-THREE, part of #1822): each walks
    # .github/ or scripts/ and weighs under 5 CI-seconds.
    for guard in (
        "tests/unit/scripts/test_auto_merge_base_merge.py",
        "tests/unit/scripts/test_issue_forms.py",
        "tests/unit/scripts/test_verify_deployer_trust.py",
    ):
        # MUTATION: drop any of the three from GUARD_TESTS and this is red, naming it.
        assert guard in GUARDS, guard
        # MUTATION: list one of them in DOC_GUARD_TESTS and every docs-only
        # diff runs it -- red here.
        assert guard not in DOC_GUARDS, guard


def test_a_doc_beside_a_code_change_keeps_the_whole_guard_set():
    # Read from the fixtures: a test naming either path would select itself.
    both = (FIXTURES / "unread.txt").read_text() + (FIXTURES / "unread-code.txt").read_text()
    result = _run("--list", "--files", "-", stdin=both)
    assert result.returncode == 0, result.stderr
    # MUTATION: reading "any doc changed" as docs-only turns this red.
    assert sorted(_selected(result.stdout)) == sorted(GUARDS)
    assert "check-contract-parity.sh" in result.stdout


def test_a_changed_test_file_is_selected_itself():
    path = "tests/unit/scripts/test_issue_forms.py"
    result = _run("--list", "--files", "-", stdin=path + "\n")
    assert result.returncode == 0, result.stderr
    assert path in _selected(result.stdout)


def test_an_empty_diff_runs_nothing_and_succeeds():
    result = _run("--files", os.devnull)
    assert result.returncode == 0, result.stderr
    assert _selected(result.stdout) == []
    assert "nothing changed" in (result.stdout + result.stderr)


def test_an_unknown_base_is_refused_not_read_as_an_empty_diff():
    result = _run("--list", "--base", "refs/heads/no-such-branch-642")
    assert result.returncode == 2
    assert "no-such-branch-642" in result.stderr


def test_an_unknown_option_is_refused():
    result = _run("--no-such-option")
    assert result.returncode == 2


def test_the_tests_run_in_the_callers_environment_not_common_sh_s(tmp_path: Path):
    # Measured 2026-10-06: sourcing common.sh exports PROJECT_ID, IMAGE_REPO,
    # ENVIRONMENT and more, and 20 worker and push-images tests inherited them
    # and failed under the script while passing in a plain pytest. A copy of the
    # script runs here against a fake project python that records its env.
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "changed-guards.sh")
    shutil.copy(REPO / "scripts" / "lib" / "common.sh", tmp_path / "scripts" / "lib" / "common.sh")
    parity = tmp_path / "scripts" / "lib" / "check-contract-parity.sh"
    parity.write_text("#!/usr/bin/env bash\nenv > \"$ENV_DUMP.parity\"\n")
    for guard in GUARDS:
        (tmp_path / guard).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / guard).write_text("")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$ENV_DUMP.args\"\nenv > \"$ENV_DUMP\"\n")
    python.chmod(0o755)
    dump = tmp_path / "env"
    env = {k: v for k, v in os.environ.items()
           if k not in {"PROJECT_ID", "IMAGE_REPO", "ENVIRONMENT", "REPO_ROOT", "SWARM_CLONE_BASE"}}
    env.update(NO_COLOR="1", ENV_DUMP=str(dump), SWARM_ENV_FILE=str(tmp_path / "no.env"))
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "changed-guards.sh"), "--files", "-"],
        cwd=tmp_path, env=env, input="scripts/x.sh\n", capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "env.args").read_text()
    assert args.startswith("-m pytest -q -n auto -p no:warnings ")
    # Box 102 (#888): the run names its slowest tests, and a test that runs
    # past a minute dumps its stack, so the next overrun names the slow one.
    assert "--durations=10" in args.split()
    assert "faulthandler_timeout=60" in args.split()
    for guard in GUARDS:
        assert guard in args
    for recorded in (dump, tmp_path / "env.parity"):
        names = {line.split("=", 1)[0] for line in recorded.read_text().splitlines() if "=" in line}
        assert "ENV_DUMP" in names
        for leaked in ("PROJECT_ID", "IMAGE_REPO", "ENVIRONMENT", "REPO_ROOT"):
            assert leaked not in names, leaked
    # The venv interpreter runs as an activated venv, as `uv run` runs it:
    # worker tests start the venv's own commands from PATH.
    recorded = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert recorded["PATH"].split(":")[0] == str(python.parent)
    assert recorded["VIRTUAL_ENV"] == str(python.parent.parent)


def test_ci_doc_and_claude_md_name_it_as_the_pre_finish_command():
    assert "scripts/changed-guards.sh" in (REPO / "docs" / "ci.md").read_text()
    claude = (REPO / "CLAUDE.md").read_text()
    paragraph = claude[claude.index("**A SwarmCloud agent is not this machine.**"):]
    paragraph = paragraph[: paragraph.index("\n\n")]
    assert "scripts/changed-guards.sh" in paragraph


def test_an_overrun_names_the_test_that_was_still_running(tmp_path: Path):
    # Box 102 (#888): lanes reported the 540 s cap reached with nothing naming
    # the slow test. `timeout` kills pytest with SIGTERM, so `--durations`
    # never prints on an overrun (measured 2026-10-09 under xdist); what does
    # survive is the faulthandler dump of a test past its minute, which names
    # the test's file, line and function. A fake python writes such a dump and
    # exits 124, as `timeout` does.
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "changed-guards.sh")
    shutil.copy(REPO / "scripts" / "lib" / "common.sh", tmp_path / "scripts" / "lib" / "common.sh")
    (tmp_path / "scripts" / "lib" / "check-contract-parity.sh").write_text("#!/usr/bin/env bash\n")
    for guard in GUARDS:
        (tmp_path / guard).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / guard).write_text("")
    slow = tmp_path / "tests" / "unit" / "scripts" / "test_slow_thing.py"
    dump = [
        "Timeout (0:01:00)!",
        "Thread 0x00007b3099e66b80 (most recent call first):",
        f'  File "{slow}", line 42 in test_waits_forever',
        '  File "/venv/lib/python3.11/site-packages/_pytest/python.py", line 167 in pytest_pyfunc_call',
    ] + [f'  File "/venv/lib/python3.11/site-packages/pluggy/_callers.py", line {n} in _multicall'
         for n in range(40)]
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\ncat <<'DUMP'\n" + "\n".join(dump) + "\nDUMP\nexit 124\n")
    python.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in {"PROJECT_ID", "IMAGE_REPO", "ENVIRONMENT", "REPO_ROOT", "SWARM_CLONE_BASE"}}
    env.update(NO_COLOR="1", SWARM_ENV_FILE=str(tmp_path / "no.env"))
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts" / "changed-guards.sh"), "--files", "-"],
        cwd=tmp_path, env=env, input="docs/x.md\n", capture_output=True, text=True, timeout=120,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "ran past 540 s" in out
    # The dump's test frame is 40 lines above the end of the log, past the
    # 25-line tail; the script must print it by name anyway.
    assert "test_slow_thing.py" in out
    assert "test_waits_forever" in out


# THE SPLIT (#1334, finding 4). A selection over SHARD_HINT_WEIGHT CI-seconds
# runs as parts, each its own pytest call under the 540 s limit, so a passing
# heavy selection completes and a hang still ends. These hold that against a
# fake repository: five readers of the changed path weigh 300 CI-seconds each,
# 1,500 in all, which is three parts of 540.
HEAVY = tuple(f"tests/unit/scripts/test_heavy_{n}.py" for n in range(5))


def _fake_repo(tmp_path: Path, python_body: str) -> tuple[Path, dict[str, str]]:
    (tmp_path / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "changed-guards.sh")
    shutil.copy(REPO / "scripts" / "lib" / "common.sh", tmp_path / "scripts" / "lib" / "common.sh")
    (tmp_path / "scripts" / "lib" / "check-contract-parity.sh").write_text(
        "#!/usr/bin/env bash\necho parity >> \"$CALLS\"\n")
    for guard in GUARDS:
        (tmp_path / guard).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / guard).write_text("")
    weights = {guard: 0.1 for guard in GUARDS}
    for heavy in HEAVY:
        (tmp_path / heavy).write_text('PATH = "scripts/x.sh"\n')
        weights[heavy] = 300
    (tmp_path / "tests" / "unit" / "scripts" / "unit_test_durations.json").write_text(
        json.dumps(weights))
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/usr/bin/env bash\nprintf 'pytest %s\\n' \"$*\" >> \"$CALLS\"\n" + python_body)
    python.chmod(0o755)
    # A fake `timeout` records its limit and runs the command it guards, so
    # the test sees which calls ran under which limit.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "timeout").write_text(
        "#!/usr/bin/env bash\nprintf 'timeout %s\\n' \"$1\" >> \"$CALLS\"\nshift\nexec \"$@\"\n")
    (bin_dir / "timeout").chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in {"PROJECT_ID", "IMAGE_REPO", "ENVIRONMENT", "REPO_ROOT", "SWARM_CLONE_BASE"}}
    env.update(NO_COLOR="1", SWARM_ENV_FILE=str(tmp_path / "no.env"),
               CALLS=str(tmp_path / "calls"), PATH=f"{bin_dir}:{env.get('PATH', '')}")
    return tmp_path, env


def _run_fake(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "changed-guards.sh"), "--files", "-", *args],
        cwd=root, env=env, input="scripts/x.sh\n", capture_output=True, text=True, timeout=120,
    )


def test_the_shards_of_a_selection_hold_every_file_exactly_once(tmp_path: Path):
    root, env = _fake_repo(tmp_path, "")
    whole = _run_fake(root, env, "--list")
    assert whole.returncode == 0, whole.stderr
    expected = sorted(_selected(whole.stdout))
    assert expected == sorted(GUARDS + HEAVY)
    parts = []
    for k in (1, 2, 3):
        part = _run_fake(root, env, "--list", "--shard", f"{k}/3")
        assert part.returncode == 0, part.stderr
        parts.append(_selected(part.stdout))
        # The parity check belongs to part 1 alone.
        assert ("check-contract-parity.sh" in part.stdout) == (k == 1), k
    assert all(parts), parts
    flat = [path for part in parts for path in part]
    assert sorted(flat) == expected
    assert len(flat) == len(set(flat))
    assert _run_fake(root, env, "--list", "--shard", "4/3").returncode == 2


def test_a_heavy_selection_runs_as_parts_each_under_the_540_s_limit(tmp_path: Path):
    root, env = _fake_repo(tmp_path, "")
    result = _run_fake(root, env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    calls = (root / "calls").read_text().splitlines()
    pytest_calls = [line for line in calls if line.startswith("pytest ")]
    assert len(pytest_calls) == 3, calls
    # Every pytest call is preceded by its own `timeout 540`: a hang in any
    # part still ends.
    for i, line in enumerate(calls):
        if line.startswith("pytest "):
            assert calls[i - 1] == "timeout 540", calls
    ran = [word for line in pytest_calls for word in line.split() if word.startswith("tests/")]
    assert sorted(ran) == sorted(GUARDS + HEAVY)
    assert calls.count("parity") == 1
    assert "all 3 parts" in out


def test_a_failing_part_fails_the_whole_run(tmp_path: Path):
    # The part holding test_heavy_4.py fails; the parts after it still run.
    root, env = _fake_repo(tmp_path, 'case "$*" in *test_heavy_4.py*) exit 1 ;; esac\n')
    result = _run_fake(root, env)
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    pytest_calls = [line for line in (root / "calls").read_text().splitlines()
                    if line.startswith("pytest ")]
    assert len(pytest_calls) == 3, pytest_calls
    assert "a part failed" in out


# THE STYLESHEET GUARDS (RELATED-CSS-GUARDS, part of #1914). Measured
# 2026-10-22 on PR 1905: a change to apps/swarm-ui/src/styles/intake.css
# declared one selector twice in a media block; CI's stylesheet.gate.test.ts
# and sections.stylesheet.test.ts refused it, and no agent of the lane had run
# them, because this script ran no vitest at all and related-tests.sh's import
# graph never reaches a test that globs the sheets from disk. So a change to a
# swarm-ui stylesheet runs those guards here too, from the one list in
# scripts/lib/related_tests.py (STYLESHEET_GUARDS).
UI = "apps/swarm-ui"
STYLE_GUARDS = (f"{UI}/src/__tests__/stylesheet.gate.test.ts",
                f"{UI}/src/__tests__/sections.stylesheet.test.ts")


def test_a_ui_stylesheet_change_lists_both_stylesheet_guards_once():
    result = _run("--list", "--files", "-", stdin=f"{UI}/src/styles/intake.css\n")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for guard in STYLE_GUARDS:
        # MUTATION: drop the stylesheet step from changed-guards.sh -> the
        # guard is not listed and this is red.
        assert lines.count(guard) == 1, lines
    # The pytest guard set still runs beside them.
    assert set(UNIT_GUARDS) <= set(_selected(result.stdout))


def test_a_change_with_no_ui_stylesheet_lists_no_stylesheet_guard():
    result = _run("--list", "--files", "-",
                  stdin=f"{UI}/src/widget.ts\ndocs/assets/site.css\napps/other/static/intake.css\n")
    assert result.returncode == 0, result.stderr
    # MUTATION: run the stylesheet guards for any change, or for a .css
    # outside apps/swarm-ui -> they are listed and this is red.
    for guard in STYLE_GUARDS:
        assert guard not in result.stdout.splitlines(), result.stdout


def _fake_ui_repo(tmp_path: Path, vitest_body: str | None) -> tuple[Path, dict[str, str]]:
    """The fake repository, plus the selector and a swarm-ui tree holding the
    stylesheet guards; `vitest_body` None leaves vitest uninstalled."""
    root, env = _fake_repo(tmp_path, "")
    for lib in ("related_tests.py", "select_unit_tests.py"):
        shutil.copy(REPO / "scripts" / "lib" / lib, root / "scripts" / "lib" / lib)
    for guard in STYLE_GUARDS:
        (root / guard).parent.mkdir(parents=True, exist_ok=True)
        (root / guard).write_text("const S = import.meta.glob('../styles/*.css')\n")
    (root / UI / "src" / "styles").mkdir(parents=True, exist_ok=True)
    (root / UI / "src" / "styles" / "intake.css").write_text(".x { gap: 4px }\n")
    if vitest_body is not None:
        vitest = root / UI / "node_modules" / ".bin" / "vitest"
        vitest.parent.mkdir(parents=True)
        vitest.write_text("#!/usr/bin/env bash\nprintf 'vitest %s|%s\\n' \"$*\" \"${PWD##*/}\" >> \"$CALLS\"\n"
                          + vitest_body)
        vitest.chmod(0o755)
    return root, env


def _run_css(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts" / "changed-guards.sh"), "--files", "-", *args],
        cwd=root, env=env, input=f"{UI}/src/styles/intake.css\n",
        capture_output=True, text=True, timeout=120,
    )


def test_a_ui_stylesheet_change_runs_the_stylesheet_guards_under_the_limit(tmp_path: Path):
    root, env = _fake_ui_repo(tmp_path, "")
    result = _run_css(root, env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    calls = (root / "calls").read_text().splitlines()
    vitest = [line for line in calls if line.startswith("vitest ")]
    # MUTATION: list the guards but never run them -> no vitest call, red.
    assert len(vitest) == 1, calls
    # MUTATION: run vitest from the repository root (its config would not
    # load) -> the recorded directory is not swarm-ui, red.
    assert vitest[0].endswith("|swarm-ui"), vitest
    # MUTATION: run vitest with no file arguments (the whole suite) -> red.
    assert vitest[0].startswith("vitest run "), vitest
    for guard in STYLE_GUARDS:
        assert guard[len(UI) + 1:] in vitest[0].rpartition("|")[0].split(), vitest
    # MUTATION: run vitest without the 540 s limit -> red.
    assert calls[calls.index(vitest[0]) - 1] == "timeout 540", calls


def test_a_failing_stylesheet_guard_fails_the_run(tmp_path: Path):
    root, env = _fake_ui_repo(tmp_path, "exit 1\n")
    result = _run_css(root, env)
    out = result.stdout + result.stderr
    # MUTATION: ignore vitest's exit status -> exit 0 and this is red.
    assert result.returncode == 1, out
    assert "stylesheet guard" in out


def test_an_uninstalled_vitest_is_a_failure_that_says_how_to_install_it(tmp_path: Path):
    root, env = _fake_ui_repo(tmp_path, None)
    result = _run_css(root, env)
    out = result.stdout + result.stderr
    # MUTATION: skip the guards quietly when vitest is missing -> exit 0, red:
    # an unrun guard is the measured defect, not a pass.
    assert result.returncode == 1, out
    assert "npm ci" in out
