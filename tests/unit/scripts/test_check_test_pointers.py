"""No comment or docstring in non-test code under apps/ points at a test
(part of issue 69).

WHY THIS IS A TEST. A pointer such as "pinned by tests/unit/x/test_y.py" rots
as tests move and get renamed, and find-references already answers what
covers a line; issue 69 removed 200-odd of them. scripts/check_test_pointers.py
says what a pointer is. This runs it over the whole tree, so it sits in
changed-guards.sh's GUARD_TESTS: a new pointer in any file names no test, so
only a whole-tree guard reaches it. The planted cases prove the check flags
what it must and passes what it must not, in a scratch git repository.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_test_pointers.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_test_pointers", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctp = _load()


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


TESTS = {
    "tests/unit/worker/test_widget.py": "def test_the_widget_is_held_equal():\n    pass\n",
    "apps/swarm-ui/src/__tests__/Phone.test.tsx": "it('renders', () => {})\n",
    "tests/fixtures/live/frame.json": "{}\n",
}


@pytest.mark.parametrize("comment", [
    "# held equal by tests/unit/worker/test_widget.py",
    "# pinned by test_widget.py",
    "# see test_widget.py::test_the_widget_is_held_equal",
    "# `test_the_widget_is_held_equal` holds the two together",
    "# the fixture tests/fixtures/live/*.json is parsed here",
    "# apps/app/tests/test_x.py holds it",
    "# the earlier Phone.test.tsx failed here",
])
def test_a_planted_pointer_is_flagged(tmp_path, capsys, comment):
    root = _repo(tmp_path, {**TESTS, "apps/app/app/mod.py": f"X = 1\n{comment}\n"})
    assert ctp.main(["--root", str(root)]) == 1
    assert "apps/app/app/mod.py:2:" in capsys.readouterr().out


def test_an_untracked_new_file_with_a_pointer_is_flagged(tmp_path, capsys):
    root = _repo(tmp_path, TESTS)
    new = root / "apps" / "app" / "app" / "new.py"
    new.parent.mkdir(parents=True)
    new.write_text("X = 1\n# held equal by tests/unit/worker/test_widget.py\n", encoding="utf-8")
    assert ctp.main(["--root", str(root)]) == 1
    assert "apps/app/app/new.py:2:" in capsys.readouterr().out


def test_a_pointer_in_a_docstring_is_flagged(tmp_path, capsys):
    source = 'def f():\n    """Held equal by\n    tests/unit/worker/test_widget.py."""\n'
    root = _repo(tmp_path, {**TESTS, "apps/app/app/mod.py": source})
    assert ctp.main(["--root", str(root)]) == 1
    assert "apps/app/app/mod.py:3:" in capsys.readouterr().out


@pytest.mark.parametrize("comment", [
    "# a file under a `tests/` or `__tests__/` directory is test code",
    "# a shared fixture (`conftest.py`) changes what every test runs under",
    "# `X.test.tsx` beside a changed script `X.tsx`",
    "# 8 pytest runs over whole tests/unit directories",
    "# the `test_map` section of the document",
    "# held equal by a test",
])
def test_a_description_of_tests_is_not_a_pointer(tmp_path, capsys, comment):
    root = _repo(tmp_path, {**TESTS, "apps/app/app/mod.py": f"X = 1\n{comment}\n"})
    assert ctp.main(["--root", str(root)]) == 0, capsys.readouterr().out


def test_a_string_literal_is_not_a_comment(tmp_path):
    source = 'GUARD = "tests/unit/worker/test_widget.py"\n'
    root = _repo(tmp_path, {**TESTS, "apps/app/app/mod.py": source})
    assert ctp.main(["--root", str(root)]) == 0


def test_test_code_and_the_allowlist_are_not_scanned(tmp_path):
    pointer = "# held equal by tests/unit/worker/test_widget.py\n"
    files = {**TESTS, "apps/app/tests/helper.py": pointer, "apps/app/app/test_x.py": pointer}
    files.update({path: pointer for path in ctp.ALLOWLIST})
    files["apps/common/swarm_common/models.py"] = pointer
    root = _repo(tmp_path, files)
    assert ctp.main(["--root", str(root)]) == 0


def test_the_allowlist_is_exactly_the_files_other_pull_requests_hold():
    # Issue 69 left these for a follow-up; the list only ever shrinks.
    assert ctp.ALLOWLIST_PREFIXES == ("apps/common/swarm_common/", "apps/swarm-ui/")
    assert ctp.ALLOWLIST == {
        "apps/agent-worker/agent_worker/lifecycle.py",
        "apps/agent-worker/agent_worker/heartbeat.py",
        "apps/swarm-api/swarm_api/routes/tasks.py",
        "apps/swarm-api/swarm_api/wire_types.py",
    }


def test_no_comment_under_apps_points_at_a_test(capsys):
    code = ctp.main(["--root", str(ROOT)])
    out = capsys.readouterr().out
    assert code == 0, f"keep the WHY, drop the pointer:\n{out}"


def test_the_check_runs_in_the_guard_set():
    # A new pointer names no test, so no changed-path selection reaches this
    # file: only GUARD_TESTS, which pull-request CI always runs, does.
    text = (ROOT / "scripts" / "changed-guards.sh").read_text(encoding="utf-8")
    guards = text.split("GUARD_TESTS=(", 1)[1].split(")", 1)[0].split()
    # MUTATION: drop this file from GUARD_TESTS and this is red.
    assert "tests/unit/scripts/test_check_test_pointers.py" in guards
