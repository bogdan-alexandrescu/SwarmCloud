"""The out-of-tree read scanner finds what it must and passes what it must not
(part of issue 71).

WHY THIS IS A TEST. A unit test that opens docs prose, CI YAML, a root *.md
or the Makefile turns red on an edit to a tree it does not test.
scripts/check_test_reads.py lists every such read. Issue 71's steps deleted
them; step 8 removed the baselines and the --baseline-dir option, so the
scanner holds the whole tree with no exception. The whole-tree run below sits in
changed-guards.sh's GUARD_TESTS: a new read lands in a file no diff names, so
only a whole-tree guard reaches it. The planted cases run in scratch trees.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_test_reads.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_test_reads", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctr = _load()

HEAD = "from pathlib import Path\n\nROOT = Path(__file__).resolve().parents[3]\n\n"
WHERE = "tests/unit/scripts/test_planted.py"


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def _scan(tmp_path: Path, capsys, files: dict[str, str], *extra: str) -> tuple[int, str]:
    root = _tree(tmp_path, files)
    code = ctr.main(["--root", str(root), *extra])
    return code, capsys.readouterr().out


def test_the_script_is_executable_and_imports_only_the_standard_library():
    assert os.access(SCRIPT, os.X_OK)
    # Run in isolated mode with no site-packages: a third-party import fails.
    result = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "--help"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_a_docs_read_exits_1_naming_path_and_line(tmp_path):
    root = _tree(tmp_path, {WHERE: HEAD + 'def test_x():\n    (ROOT / "docs" / "x.md").read_text()\n'})
    result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert f"{WHERE}:6:" in result.stdout


@pytest.mark.parametrize("body", [
    'ROOT / "docs" / "ci.md"',
    'ROOT / "docs/ci.md"',
    'Path(ROOT, ".github", "workflows", "ci.yml")',
    'ROOT.joinpath("docs", "ci.md")',
    'os.path.join(ROOT, ".github", "workflows")',
    'ROOT / "CLAUDE.md"',
    'ROOT / "CONTRACT.md"',
    'ROOT / "README.md"',
    'ROOT / "Makefile"',
    'ROOT / ".github" / "workflows" / "ci.yml"',
    'ROOT.glob(".github/workflows/*.yml")',
    'f"{ROOT}/docs/ci.md"',
    'ROOT / "docs" / name',
])
def test_an_anchored_read_is_found(tmp_path, capsys, body):
    source = "import os\n" + HEAD + f"def test_x(name):\n    open({body}).read()\n"
    code, out = _scan(tmp_path, capsys, {WHERE: source})
    assert code == 1
    assert f"{WHERE}:7:" in out


@pytest.mark.parametrize("anchor,where", [
    ("Path(__file__).resolve().parents[3]", "tests/unit/scripts/test_a.py"),
    ("Path(__file__).parents[2]", "tests/unit/test_a.py"),
    ("Path(__file__).resolve().parent.parent.parent", "tests/unit/test_a.py"),
    ("Path(__file__).resolve().parents[0].parents[1]", "tests/unit/test_a.py"),
    ("Path(os.path.dirname(os.path.abspath(__file__))).parents[0]", "tests/test_a.py"),
    ("Path(__file__).resolve().parents[1]", "tests/test_a.py"),
])
def test_the_root_is_worked_out_from_the_files_depth(tmp_path, capsys, anchor, where):
    source = f"import os\nfrom pathlib import Path\nR = {anchor}\nX = (R / 'docs' / 'x.md').read_text()\n"
    code, out = _scan(tmp_path, capsys, {where: source})
    assert code == 1, out
    assert f"{where}:4:" in out


def test_a_parent_short_of_the_root_is_no_read(tmp_path, capsys):
    # parents[2] from tests/unit/scripts/ is tests/: tests/docs/ is the test tree's own.
    source = "from pathlib import Path\nR = Path(__file__).resolve().parents[2]\nX = (R / 'docs' / 'x.md').read_text()\n"
    code, out = _scan(tmp_path, capsys, {WHERE: source})
    assert code == 0, out


def test_a_name_is_followed_through_assignments(tmp_path, capsys):
    source = HEAD + ('DOCS = ROOT / "docs"\nCI = DOCS / "ci.md"\n\n'
                     'def test_x():\n    rel = "docs/a.md"\n    doc = ROOT / rel\n'
                     '    assert CI.read_text()\n    assert doc.exists()\n')
    code, out = _scan(tmp_path, capsys, {WHERE: source})
    assert code == 1
    lines = sorted(int(line.split(":")[1]) for line in out.splitlines())
    # The assignments are not reported; each use is.
    assert lines == [11, 12], out


def test_a_name_imported_from_a_sibling_helper_is_followed(tmp_path, capsys):
    helper = HEAD + 'APPLICATION = ROOT / ".github" / "workflows" / "application.yml"\n'
    test = ("from application_paths import APPLICATION, ROOT\nimport application_paths as ap\n\n"
            "def test_x():\n    APPLICATION.read_text()\n    (ROOT / 'Makefile').read_text()\n"
            "    ap.APPLICATION.read_text()\n")
    code, out = _scan(tmp_path, capsys, {
        "tests/unit/scripts/application_paths.py": helper, WHERE: test})
    assert code == 1
    assert f"{WHERE}:5:" in out and f"{WHERE}:6:" in out and f"{WHERE}:7:" in out
    # The helper only builds the path: it reads nothing.
    assert "application_paths.py" not in out


def test_a_helper_that_joins_a_relative_path_to_the_root_is_followed(tmp_path, capsys):
    source = HEAD + ('def _read(rel):\n    return (ROOT / rel).read_text()\n\n'
                     'def test_x():\n    _read("docs/ci.md")\n    _read("apps/x.py")\n'
                     '    _read(rel=".github/x.yml")\n')
    code, out = _scan(tmp_path, capsys, {WHERE: source})
    assert code == 1
    lines = sorted(int(line.split(":")[1]) for line in out.splitlines())
    assert lines == [9, 11], out


@pytest.mark.parametrize("source", [
    # A file the test writes under tmp_path.
    "def test_x(tmp_path):\n    (tmp_path / 'docs' / 'x.md').write_text('x')\n"
    "    assert (tmp_path / 'docs/x.md').read_text()\n",
    # A doc path as data.
    "def test_x():\n    assert 'docs/x.md'.endswith('.md')\n    paths = ['docs/x.md', '.github/x.yml']\n",
    # Reads of other trees, and a .json data file under docs/.
    "def test_x():\n    (ROOT / 'apps' / 'x.py').read_text()\n    (ROOT / 'scripts/x.sh').read_text()\n"
    "    (ROOT / 'docs' / 'data.json').read_text()\n",
    # Running make as a subprocess from the root.
    "import subprocess\ndef test_x():\n    subprocess.run(['make', '-n', 'lint'], cwd=ROOT)\n",
    # A doc path inside a string of generated code.
    "SRC = 'open(\"docs/x.md\").read()'\n",
])
def test_what_is_not_an_out_of_tree_read(tmp_path, capsys, source):
    code, out = _scan(tmp_path, capsys, {WHERE: HEAD + source})
    assert code == 0, out


def test_files_outside_tests_are_not_scanned(tmp_path, capsys):
    code, out = _scan(tmp_path, capsys, {
        "scripts/x.py": HEAD + "X = (ROOT / 'docs' / 'x.md').read_text()\n"})
    assert code == 0, out


def test_list_files_prints_each_offending_file_once(tmp_path, capsys):
    two = HEAD + "A = (ROOT / 'docs/a.md').read_text()\nB = (ROOT / 'docs/b.md').read_text()\n"
    code, out = _scan(tmp_path, capsys, {WHERE: two, "tests/unit/test_b.py": two.replace("[3]", "[2]")},
                      "--list-files")
    assert code == 1
    assert out.splitlines() == ["tests/unit/scripts/test_planted.py", "tests/unit/test_b.py"]


def test_a_bad_option_exits_2():
    result = subprocess.run([sys.executable, str(SCRIPT), "--no-such-option"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2


def test_the_baseline_option_and_directory_are_gone(tmp_path):
    # Step 8: no file may be tolerated. MUTATION: restore --baseline-dir or
    # add scripts/guards/test-reads/.
    assert not (ROOT / "scripts" / "guards" / "test-reads").exists()
    result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(tmp_path),
                             "--baseline-dir", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 2, result.stdout


def test_a_docs_read_added_to_a_clean_tree_is_red(tmp_path, capsys):
    # A clean tree passes with no option; add one test that opens
    # ROOT / "docs" / "ci.md" and the same whole-tree run is red: no baseline
    # can tolerate it.
    clean = {"tests/unit/control_plane/test_clean.py":
             HEAD + 'def test_x():\n    assert (ROOT / "apps" / "x.py")\n'}
    code, out = _scan(tmp_path, capsys, clean)
    assert code == 0, out
    planted = "tests/unit/control_plane/test_reads_ci_doc.py"
    code, out = _scan(tmp_path, capsys, {
        planted: HEAD + 'def test_x():\n    assert (ROOT / "docs" / "ci.md").read_text()\n'})
    assert code == 1
    assert f"{planted}:6:" in out


def test_the_tree_holds_no_read(capsys):
    # The whole tree: a read of docs prose, CI YAML, a root *.md or the
    # Makefile anywhere under tests/ is red here.
    # MUTATION: open docs/schedules.md in tests/unit/control_plane.
    code = ctr.main(["--root", str(ROOT)])
    assert code == 0, capsys.readouterr().out


def test_a_parametrized_or_looped_constant_is_followed(tmp_path, capsys):
    source = HEAD + ('import pytest\n\nTABLED = ("docs/a.md", "plugin/README.md")\n\n'
                     'def _read(rel):\n    return (ROOT / rel).read_text()\n\n'
                     '@pytest.mark.parametrize("rel", TABLED)\ndef test_x(rel):\n    _read(rel)\n\n'
                     'def test_y():\n    for name in ("CLAUDE.md", "x.py"):\n        (ROOT / name).read_text()\n'
                     '    [(ROOT / n).read_text() for n in ["Makefile", "y.py"]]\n')
    code, out = _scan(tmp_path, capsys, {WHERE: source})
    assert code == 1
    lines = sorted(int(line.split(":")[1]) for line in out.splitlines())
    assert lines == [14, 18, 19], out


def test_a_helper_that_reads_or_hands_its_parameter_on_is_followed(tmp_path, capsys):
    helper = HEAD + ('WORKFLOWS = ROOT / ".github" / "workflows"\n\n'
                     'def _load(name):\n    return (WORKFLOWS / name).read_text()\n\n'
                     'def _workflow(name):\n    return _load(name)\n\n'
                     'def changes_job():\n    return (ROOT / "Makefile").read_text()\n')
    test = ("from pathlib import Path\nfrom .test_helper import _workflow, changes_job\n\n"
            "REPO = Path(__file__).resolve().parents[3]\nRELEASE = REPO / '.github' / 'release.yml'\n\n"
            "def test_x():\n    _workflow(RELEASE.name)\n    changes_job()\n    print(RELEASE.name)\n")
    code, out = _scan(tmp_path, capsys, {"tests/unit/scripts/test_helper.py": helper, WHERE: test})
    assert code == 1
    mine = sorted(int(line.split(":")[1]) for line in out.splitlines() if line.startswith(WHERE))
    # The name of a path alone reads nothing (line 10).
    assert mine == [8, 9], out
