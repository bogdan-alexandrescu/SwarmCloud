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

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "changed-guards.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "changed_guards"

GUARDS = (
    "tests/unit/scripts/test_docs_describe_what_was_built.py",
    "tests/unit/scripts/test_docs_spec_amendments.py",
    "tests/unit/control_plane/test_runtimes_screen.py",
    "tests/unit/common/test_specsign_covers.py",
)


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
    result = _run("--list", "--files", str(FIXTURES / "unread.txt"))
    assert result.returncode == 0, result.stderr
    assert sorted(_selected(result.stdout)) == sorted(GUARDS)


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
        cwd=tmp_path, env=env, input="docs/x.md\n", capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "env.args").read_text()
    assert args.startswith("-m pytest -q -n auto -p no:warnings ")
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
