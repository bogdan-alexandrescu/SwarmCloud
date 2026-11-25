"""The out-of-tree read scanner finds what it must and passes what it must not
(part of issue 71).

WHY THIS IS A TEST. A unit test that opens docs prose, CI YAML, a root *.md
or the Makefile turns red on an edit to a tree it does not test.
scripts/check_test_reads.py lists every such read, in a .py file under tests/
or in a JS/TS test (apps/swarm-ui's console tests included). Issue 71's steps deleted
them; step 8 removed the baselines and the --baseline-dir option. The one
exception is a short named list, SECURITY_GUARDS in the scanner: the tests
that hold CI security decisions, kept by the owner's decision of 2026-11-25.
A listed file passes; the same read anywhere else is red; a listed path that
is gone or reads nothing out of its tree is red, so the list cannot rot. The
planted cases run with an empty list (the autouse fixture below) unless they
plant one. The whole-tree run below sits in
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


REAL_GUARDS = ("test_the_tree_holds_no_read", "test_the_list_is_the_owners_and_each_entry_says_what_it_holds")


@pytest.fixture(autouse=True)
def _no_listed_guards(request, monkeypatch):
    """A scratch tree holds none of the real listed guards: scan it with an
    empty list. The whole-tree run keeps the real one."""
    if request.node.name not in REAL_GUARDS:
        monkeypatch.setattr(ctr, "SECURITY_GUARDS", {}, raising=False)

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
    # Makefile anywhere under tests/, or in a JS/TS test, is red here.
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


# JavaScript and TypeScript tests: a console test reads only its own tree too.
UI_TEST = "apps/swarm-ui/src/__tests__/planted.test.tsx"
UI_HEAD = ("import { readFileSync } from 'node:fs'\nimport { dirname, join, resolve } from 'node:path'\n"
           "import { fileURLToPath } from 'node:url'\n\nconst REPO = join(__dirname, '..', '..', '..', '..')\n")


@pytest.mark.parametrize("where,body", [
    (UI_TEST, "const DOC = join(REPO, 'docs', 'web-ui', 'x.md')"),
    (UI_TEST, "const doc = readFileSync(join(__dirname, '..', '..', '..', '..', 'docs', 'x.md'), 'utf8')"),
    (UI_TEST, "readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../../../../.github/workflows/ci.yml'))"),
    (UI_TEST, "readFileSync(new URL('../../../../CLAUDE.md', import.meta.url))"),
    (UI_TEST, "readFileSync(`${REPO}/Makefile`, 'utf8')"),
    (UI_TEST, "for (const f of readdirSync(join(REPO, 'docs', 'user-guide'))) readFileSync(f)"),
    ("apps/swarm-ui/tests/planted.test.ts", "readFileSync(resolve(__dirname, '..', '..', '..', 'docs', 'web-ui', 'x.md'))"),
    ("tests/browser/planted.mjs", "readFileSync(join(import.meta.dirname, '..', '..', 'CONTRACT.md'))"),
])
def test_a_js_or_ts_test_that_builds_a_docs_path_is_found(tmp_path, capsys, where, body):
    # MUTATION: drop is_js_test's __tests__ directory, or JsScan's join call.
    code, out = _scan(tmp_path, capsys, {where: UI_HEAD + "// a comment's 'docs' is no path\n" + body + "\n"})
    assert code == 1, out
    assert out.startswith(f"{where}:7:"), out


@pytest.mark.parametrize("body", [
    # The console's own tree, a sibling app and the repository's tests.
    "readFileSync(join(__dirname, '..', 'styles', 'x.css'), 'utf8')",
    "readFileSync(join(REPO, 'apps', 'swarm-api', 'swarm_api', 'routes.py'), 'utf8')",
    "readFileSync(join(REPO, 'plugin', 'skills', 'sc', 'SKILL.md'), 'utf8')",
    # A JSON data file under docs/ is data.
    "JSON.parse(readFileSync(join(REPO, 'docs', 'x.json'), 'utf8'))",
    # A doc's path as data, and a directory short of the root.
    "expect(text).toContain('docs/workspaces.md §10')",
    "const NEAR = join(__dirname, '..', '..', 'docs')",
    # A regex literal holding a quote does not swallow what follows it.
    "const RE = /\"([a-z]+)'/\nconst X = join(__dirname, '..', 'x.css')",
])
def test_what_a_js_or_ts_test_may_read(tmp_path, capsys, body):
    code, out = _scan(tmp_path, capsys, {UI_TEST: UI_HEAD + body + "\n"})
    assert code == 0, out


def test_a_js_file_that_is_not_a_test_is_not_scanned(tmp_path, capsys):
    # apps/swarm-ui/guide-sync.mjs reads docs/user-guide to write the copy.
    code, out = _scan(tmp_path, capsys, {
        "apps/swarm-ui/guide-sync.mjs": UI_HEAD.replace("'..', '..', '..', '..'", "'..', '..'")
        + "readFileSync(join(REPO, 'docs', 'user-guide', 'index.md'))\n"})
    assert code == 0, out


# The owner's named exceptions (2026-11-25): the CI security guards.
GUARD = "tests/unit/scripts/test_planted_guard.py"
WORKFLOW_READ = HEAD + 'def test_x():\n    (ROOT / ".github" / "workflows" / "release.yml").read_text()\n'


def test_a_listed_guard_passes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ctr, "SECURITY_GUARDS", {GUARD: "holds a planted security decision"})
    code, out = _scan(tmp_path, capsys, {GUARD: WORKFLOW_READ})
    assert code == 0, out


def test_an_unlisted_file_with_the_same_read_fails(tmp_path, capsys, monkeypatch):
    # MUTATION: skip every file whose name ends in _guard.py, or match the list by stem.
    monkeypatch.setattr(ctr, "SECURITY_GUARDS", {GUARD: "holds a planted security decision"})
    code, out = _scan(tmp_path, capsys, {GUARD: WORKFLOW_READ, WHERE: WORKFLOW_READ})
    assert code == 1, out
    assert f"{WHERE}:6:" in out
    assert GUARD not in out, out


def test_a_listed_path_that_does_not_exist_fails(tmp_path, capsys, monkeypatch):
    gone = "tests/unit/scripts/test_gone_guard.py"
    monkeypatch.setattr(ctr, "SECURITY_GUARDS", {GUARD: "holds a planted decision", gone: "was deleted"})
    code, out = _scan(tmp_path, capsys, {GUARD: WORKFLOW_READ})
    assert code == 1, out
    assert gone in out, out


def test_a_listed_file_that_reads_nothing_out_of_tree_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ctr, "SECURITY_GUARDS", {GUARD: "no longer reads the workflow"})
    code, out = _scan(tmp_path, capsys, {GUARD: HEAD + 'def test_x():\n    (ROOT / "apps" / "x.py").read_text()\n'})
    assert code == 1, out
    assert GUARD in out, out


@pytest.mark.parametrize("read", [
    '(ROOT / "docs" / "ci.md").read_text()',
    '(ROOT / "CLAUDE.md").read_text()',
    '(ROOT / "Makefile").read_text()',
])
def test_a_listed_guard_may_read_ci_yaml_only(tmp_path, capsys, monkeypatch, read):
    # The exception is CI YAML: a listed file that also reads docs prose, a
    # root *.md or the Makefile is reported at that read, and only there.
    # MUTATION: skip every listed file whole again.
    monkeypatch.setattr(ctr, "SECURITY_GUARDS", {GUARD: "holds a planted security decision"})
    code, out = _scan(tmp_path, capsys, {GUARD: WORKFLOW_READ + f"\n\ndef test_y():\n    {read}\n"})
    assert code == 1, out
    assert f"{GUARD}:10:" in out, out
    assert f"{GUARD}:6:" not in out, out


def test_a_listed_guard_whose_helper_reads_prose_is_reported(tmp_path, capsys, monkeypatch):
    # A function that reads CI YAML and docs prose is reported where it is called.
    monkeypatch.setattr(ctr, "SECURITY_GUARDS", {GUARD: "holds a planted security decision"})
    source = HEAD + ('def _both():\n    (ROOT / ".github" / "workflows" / "ci.yml").read_text()\n'
                     '    return (ROOT / "docs" / "ci.md").read_text()\n\n'
                     'def test_x():\n    assert _both()\n')
    code, out = _scan(tmp_path, capsys, {GUARD: source})
    assert code == 1, out
    assert f"{GUARD}:7:" in out, out


def test_the_list_is_the_owners_and_each_entry_says_what_it_holds():
    # The owner's list of 2026-11-25, the helper the release guards import
    # that is a release gate itself, and the hotfix and merge-workflow guards
    # added that day on the security review's recommendation, and every test
    # the issue 71 pull request had deleted that holds a CI security decision
    # under its one rule (the files restored on 2026-11-25).
    # MUTATION: add a docs pin to the list.
    assert set(ctr.SECURITY_GUARDS) == {
        "tests/unit/scripts/test_fork_prs_off_self_hosted.py",
        "tests/unit/scripts/test_release_prod_gate.py",
        "tests/unit/scripts/test_release_dev_iam_gate.py",
        "tests/unit/scripts/test_release_id_token_scope.py",
        "tests/unit/scripts/test_repo_secrets_environment.py",
        "tests/unit/scripts/test_accept_tenant_isolation_job.py",
        "tests/unit/scripts/test_trivy_new_findings.py",
        "tests/unit/scripts/test_ci_early.py",
        "tests/unit/scripts/test_release_reuses_ci_images.py",
        "tests/unit/scripts/test_workflow_id_token_scope.py",
        "tests/unit/scripts/test_hotfix_release.py",
        "tests/unit/scripts/test_auto_merge_workflow.py",
        "tests/unit/scripts/test_auto_merge_ready_on_green.py",
        "tests/unit/scripts/test_auto_merge_base_merge.py",
        "tests/unit/scripts/test_merge_train.py",
        "tests/unit/scripts/test_acceptance_target.py",
        "tests/unit/scripts/test_ci_gate.py",
        "tests/unit/scripts/test_deployment_values_workflow.py",
        "tests/unit/scripts/test_coverage_workflow.py",
        "tests/unit/install/test_addons.py",
        "tests/unit/scripts/test_acceptance_roles.py",
        "tests/unit/scripts/test_build_images_incremental.py",
        "tests/unit/scripts/test_build_images_local.py",
        "tests/unit/scripts/test_build_images_pr_check.py",
        "tests/unit/scripts/test_check_action_pins.py",
        "tests/unit/scripts/test_check_closing_references.py",
        "tests/unit/scripts/test_check_pr_dependencies.py",
        "tests/unit/scripts/test_ci_built_images.py",
        "tests/unit/scripts/test_ci_fix.py",
        "tests/unit/scripts/test_ci_fold.py",
        "tests/unit/scripts/test_ci_once_per_head.py",
        "tests/unit/scripts/test_ci_reuse.py",
        "tests/unit/scripts/test_ci_select.py",
        "tests/unit/scripts/test_ci_tfvars_workflow.py",
        "tests/unit/scripts/test_ci_tool_versions.py",
        "tests/unit/scripts/test_egress_rollout.py",
        "tests/unit/scripts/test_fast_checks_on_lane_prs.py",
        "tests/unit/scripts/test_flaky_quarantine.py",
        "tests/unit/scripts/test_hotfix_superseded.py",
        "tests/unit/scripts/test_iam_refusal_probe.py",
        "tests/unit/scripts/test_image_streaming.py",
        "tests/unit/scripts/test_install_trivy.py",
        "tests/unit/scripts/test_issue_forms.py",
        "tests/unit/scripts/test_join_acceptance_probe.py",
        "tests/unit/scripts/test_merge_app_permissions_doc.py",
        "tests/unit/scripts/test_merge_queue_workflows.py",
        "tests/unit/scripts/test_plan_text_never_public.py",
        "tests/unit/scripts/test_plugin_tag.py",
        "tests/unit/scripts/test_pr_image_scan.py",
        "tests/unit/scripts/test_private_tfvars.py",
        "tests/unit/scripts/test_refresh_weights_workflow.py",
        "tests/unit/scripts/test_release_acceptance_job.py",
        "tests/unit/scripts/test_release_held_plan_workflow.py",
        "tests/unit/scripts/test_release_newest_only.py",
        "tests/unit/scripts/test_release_size_record.py",
        "tests/unit/scripts/test_runner_fallback_workflow.py",
        "tests/unit/scripts/test_unit_shards.py",
        "tests/unit/scripts/test_warm_jobs.py",
        "tests/unit/scripts/test_workflow_needs_result_status.py",
    }
    assert all(reason.strip() for reason in ctr.SECURITY_GUARDS.values())
