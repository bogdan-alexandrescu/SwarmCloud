# source-text: fixture - parses planted sample sources to prove what the detector selects
"""The source-text check finds the tests that read source as text, holds their
markers, and runs the forbidden-pattern rules once (part of issue 73).

WHY THIS IS A TEST. scripts/check_source_text.py says what a source-text test
is, what its marker looks like and how a rule is shaped. This runs it over the
whole tree, so it sits in changed-guards.sh's GUARD_TESTS: a new rule hit or a
malformed marker can sit in any file, and no diff names this test. The planted
cases prove each detector technique, the marker grammar, the rule samples and
the one-read-per-file engine on scratch git repositories.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_source_text.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_source_text", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses resolve their module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cst = _load()
RULES = cst.discover_rules()


@pytest.fixture
def no_exemptions(monkeypatch):
    """The real rules without their exempt paths, for a planted repository: it
    holds none of the files they name, so each would be a stale exemption."""
    bare = [dataclasses.replace(rule, exempt=frozenset()) for rule in RULES]
    monkeypatch.setattr(cst, "discover_rules", lambda: bare)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _techniques(source: str) -> set[str]:
    return set(cst.detect(source, ast.parse(source)))


# ---------------------------------------------------------------- detector

@pytest.mark.parametrize(("source", "technique"), [
    ("from pathlib import Path\nfiles = Path('apps').rglob('*.py')\n", "glob"),
    ("import glob\nfiles = glob.glob('apps/**/*.tsx', recursive=True)\n", "glob"),
    ("from pathlib import Path\nfiles = list(Path('ui').glob('src/*.ts'))\n", "glob"),
    ("import ast\ntree = ast.parse(text)\n", "ast-parse"),
    ("import inspect\nsrc = inspect.getsource(f)\n", "getsource"),
    ("from inspect import getsource\nsrc = getsource(f)\n", "getsource"),
    ("from pathlib import Path\nt = (Path('apps') / 'x' / 'main.py').read_text()\n", "read"),
    ("from pathlib import Path\nb = Path('apps/ui/src/App.tsx').read_bytes()\n", "read"),
    ("t = open('apps/x/y.ts').read()\n", "read"),
    ("import mod\nfrom pathlib import Path\nt = Path(mod.__file__).read_text()\n", "read"),
    # Followed through plain assignments, three hops.
    ("from pathlib import Path\nA = Path('apps/x/main.py')\nB = A\nC = B\n"
     "t = C.read_text()\n", "read"),
    ("from pathlib import Path\nROOT = Path('.')\nSRC: Path = ROOT / 'app.py'\n"
     "def test_x():\n    assert 'x' in SRC.read_text()\n", "read"),
    # A glob whose pattern holds a source suffix, not only ends with one.
    ("from pathlib import Path\nfiles = Path('ui/src').rglob('*.ts*')\n", "glob"),
    # Through a helper's parameter to what its callers pass (the commonest
    # shape: `src("api.ts")`), directly and through a local.
    ("from pathlib import Path\nUI = Path('ui/src')\ndef src(name):\n"
     "    return (UI / name).read_text()\ndef test_x():\n    assert 'x' in src('api.ts')\n",
     "read"),
    ("from pathlib import Path\nUI = Path('ui/src')\ndef src(name):\n    path = UI / name\n"
     "    return path.read_text()\ndef test_x():\n    assert 'x' in src(name='App.tsx')\n",
     "read"),
    ("from pathlib import Path\nROOT = Path('.')\nclass Box:\n    def src(self, rel):\n"
     "        return (ROOT / rel).read_text()\ndef test_x():\n"
     "    assert 'x' in Box().src('apps/x/main.py')\n", "read"),
    # ... and through a parametrized test's argument to its values.
    ("import pytest\nfrom pathlib import Path\nUI = Path('ui/src')\ndef src(name):\n"
     "    return (UI / name).read_text()\n@pytest.mark.parametrize('name', ['a.tsx', 'b.tsx'])\n"
     "def test_x(name):\n    assert 'x' in src(name)\n", "read"),
])
def test_each_technique_selects_a_file(source, technique):
    assert technique in _techniques(source)


@pytest.mark.parametrize("source", [
    # Its own file, docs, YAML, JSON and shell are issue 71's, not this check's.
    "from pathlib import Path\nt = Path(__file__).read_text()\n",
    "from pathlib import Path\nt = (Path(__file__).parent / 'docs' / 'ci.md').read_text()\n",
    "from pathlib import Path\nDOC = Path('docs/ci.md')\nt = DOC.read_text()\n",
    "import json\nd = json.loads(open('a/b.json').read())\n",
    "from pathlib import Path\nt = Path('scripts/x.sh').read_text()\n",
    "from pathlib import Path\nt = list(Path('docs').rglob('*.md'))\n",
    "from pathlib import Path\nt = list(Path('tests').rglob('worker_seeds.py'))\n",
    # Four hops is past the three the detector follows.
    "from pathlib import Path\nA = 'm.py'\nB = A\nC = B\nD = C\nE = D\nt = Path(E).read_text()\n",
    # Compiled files and TSV are not source.
    "from pathlib import Path\nt = list(Path('a').rglob('*.pyc')) + list(Path('b').glob('*.tsv'))\n",
    # A helper whose callers pass no source name.
    "from pathlib import Path\nDOCS = Path('docs')\ndef doc(name):\n    return (DOCS / name).read_text()\n"
    "def test_x():\n    assert 'x' in doc('ci.md')\n",
    # A local of one helper does not lead to the callers of another: `path`
    # read here is a JSON file, whatever `put`'s own `path` was built from.
    "import json\nfrom pathlib import Path\ndef put(root, rel):\n    path = root / rel\n"
    "    path.write_text('')\ndef test_x(tmp_path):\n    put(tmp_path, 'tests/a.py')\n"
    "    (path,) = tmp_path.glob('cache-*.json')\n    json.loads(path.read_text())\n",
])
def test_a_reader_of_anything_but_source_is_not_selected(source):
    assert _techniques(source) == set()


# ------------------------------------------------------------------ marker

READER = "from pathlib import Path\nT = Path('apps/x/main.py').read_text()\n"
PLAIN = "def test_x():\n    assert True\n"


def test_a_good_marker_passes_and_is_listed(tmp_path, capsys, no_exemptions):
    marked = "# source-text: contract - the route table is read as written\n" + READER
    root = _repo(tmp_path, {"tests/unit/test_a.py": marked})
    assert cst.main(["--root", str(root)]) == 0
    assert cst.main(["--root", str(root)]) == 0
    assert cst.main(["--root", str(root), "--list"]) == 0
    out = capsys.readouterr().out
    assert "tests/unit/test_a.py" in out and "read" in out and "marked contract" in out


def test_a_marker_follows_a_shebang(tmp_path, no_exemptions):
    marked = "#!/usr/bin/env python3\n# source-text: fixture - parses a sample\n" + READER
    root = _repo(tmp_path, {"apps/app/tests/helper.py": marked})
    assert cst.main(["--root", str(root)]) == 0


@pytest.mark.parametrize("first", [
    "# source-text: contract\n",                   # no reason
    "# source-text: contract - \n",                # empty reason
    "# source-text contract - no colon\n",
    "# source-text:contract - no space\n",
])
def test_a_malformed_marker_fails(tmp_path, capsys, first):
    root = _repo(tmp_path, {"tests/unit/test_a.py": first + READER})
    assert cst.main(["--root", str(root)]) == 1
    assert "tests/unit/test_a.py:1:" in capsys.readouterr().out


def test_an_unknown_class_fails(tmp_path, capsys):
    root = _repo(tmp_path, {"tests/unit/test_a.py": "# source-text: vibes - because\n" + READER})
    assert cst.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "tests/unit/test_a.py:1:" in out and "vibes" in out


def test_a_marker_below_the_first_line_fails(tmp_path, capsys):
    late = '"""Doc."""\n# source-text: contract - read as written\n' + READER
    root = _repo(tmp_path, {"tests/unit/test_a.py": late})
    assert cst.main(["--root", str(root)]) == 1
    assert "tests/unit/test_a.py:" in capsys.readouterr().out


def test_a_marker_on_a_file_that_reads_no_source_fails(tmp_path, capsys):
    root = _repo(tmp_path, {"tests/unit/test_a.py": "# source-text: contract - why\n" + PLAIN})
    assert cst.main(["--root", str(root)]) == 1
    assert "tests/unit/test_a.py:1:" in capsys.readouterr().out


def test_a_missing_marker_fails_the_default_run(tmp_path, capsys, no_exemptions):
    # The default run is strict (issue 73's last step): a detected file with
    # no marker is a failure naming its line and how it reads source, not a
    # warning, so the next unmarked source-text test is refused.
    root = _repo(tmp_path, {"tests/unit/test_a.py": READER, "tests/unit/test_b.py": PLAIN})
    assert cst.main(["--root", str(root)]) == 1
    captured = capsys.readouterr()
    assert "tests/unit/test_a.py:1: no source-text marker (reads source by read)" in captured.out
    assert "test_b.py" not in captured.out
    assert "warning" not in captured.err
    assert cst.main(["--root", str(root), "--list"]) == 0
    assert "UNMARKED" in capsys.readouterr().out


def test_list_takes_paths(tmp_path, capsys):
    root = _repo(tmp_path, {"tests/unit/test_a.py": READER, "tests/unit/test_c.py": READER})
    assert cst.main(["--root", str(root), "--list", "tests/unit/test_c.py"]) == 0
    out = capsys.readouterr().out
    assert "test_c.py" in out and "test_a.py" not in out


def test_list_takes_directories(tmp_path, capsys):
    # Every area's check is `--list <its directory>`: a directory lists every
    # detected file under it, and only those.
    root = _repo(tmp_path, {"tests/unit/a/test_a.py": READER, "tests/unit/ab/test_b.py": READER,
                            "tests/unit/c/test_c.py": READER})
    assert cst.main(["--root", str(root), "--list", "tests/unit/a", "./tests/unit/c/"]) == 0
    out = capsys.readouterr().out
    assert "tests/unit/a/test_a.py" in out and "tests/unit/c/test_c.py" in out
    assert "test_b.py" not in out


def test_files_outside_the_test_trees_are_not_selected(tmp_path, capsys, no_exemptions):
    root = _repo(tmp_path, {"apps/app/app/main.py": READER, "scripts/x.py": READER})
    assert cst.main(["--root", str(root)]) == 0


# ------------------------------------------------------------------- rules

def test_rules_are_discovered_by_module():
    assert RULES, "the package ships at least one rule"
    ids = [rule.id for rule in RULES]
    assert len(ids) == len(set(ids))
    assert "no-bare-conftest-import" in ids


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.id)
def test_each_rule_hits_its_bad_samples_and_passes_its_good(rule):
    assert rule.why.strip() and rule.roots
    assert rule.bad and rule.good
    for path, source in rule.bad + rule.good:
        assert rule.covers(path), f"{rule.id}: sample {path} is outside its roots"
    for path, source in rule.bad:
        assert cst.apply_rule(rule, path, source), f"{rule.id} missed bad {path}"
    for path, source in rule.good:
        assert not cst.apply_rule(rule, path, source), f"{rule.id} hit good {path}"


def _rule(rule_id: str, roots: tuple[str, ...], needle: str, exempt=frozenset()):
    def check(path, text, tree):
        return [(n, f"says {needle}") for n, line in enumerate(text.splitlines(), 1)
                if needle in line]
    return cst.Rule(id=rule_id, why="planted", roots=roots, exempt=frozenset(exempt),
                    check=check, bad=(), good=())


def test_a_file_under_two_rules_roots_is_read_and_parsed_once(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"apps/a/x.py": "x = 1\n", "apps/a/y.py": "y = 2\n"})
    rules = [_rule("one", ("apps",), "never"), _rule("two", ("apps/a/*.py",), "never")]
    parsed: list[str] = []
    real = cst.parse_python
    monkeypatch.setattr(cst, "parse_python",
                        lambda text, path: parsed.append(path) or real(text, path))
    result = cst.run_rules(root, rules, cst.Sources(root))
    assert result.failures == []
    assert sorted(parsed) == ["apps/a/x.py", "apps/a/y.py"]
    assert result.visited == {"one": 2, "two": 2}


def test_a_rule_hit_fails_with_its_id(tmp_path, capsys):
    root = _repo(tmp_path, {"apps/a/x.py": "x = 1  # forbidden\n"})
    result = cst.run_rules(root, [_rule("planted", ("apps",), "forbidden")], cst.Sources(root))
    assert result.failures == ["apps/a/x.py:1: planted: says forbidden"]


def test_an_exempt_path_passes_while_it_matches(tmp_path):
    root = _repo(tmp_path, {"apps/a/x.py": "forbidden\n"})
    rule = _rule("planted", ("apps",), "forbidden", {"apps/a/x.py"})
    assert cst.run_rules(root, [rule], cst.Sources(root)).failures == []


@pytest.mark.parametrize("files", [
    {"apps/a/x.py": "clean\n"},          # no longer matches
    {"apps/a/y.py": "clean\n"},          # no longer exists
])
def test_a_stale_exemption_fails(tmp_path, files):
    root = _repo(tmp_path, files)
    rule = _rule("planted", ("apps",), "forbidden", {"apps/a/x.py"})
    failures = cst.run_rules(root, [rule], cst.Sources(root)).failures
    assert len(failures) == 1 and "apps/a/x.py" in failures[0] and "stale" in failures[0]


def test_the_conftest_rule_fails_the_check(tmp_path, capsys):
    bad = "from " + "conftest import seed\n"
    root = _repo(tmp_path, {"tests/unit/worker/test_x.py": bad})
    assert cst.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "tests/unit/worker/test_x.py:1: no-bare-conftest-import:" in out


# --------------------------------------------------------------- the tree

@pytest.fixture(scope="module")
def tree():
    # One read of the real tree for the three tests below: the run is most of
    # this file's time.
    sources = cst.Sources(ROOT)
    return sources, cst.check_tree(ROOT, sources)


def test_the_tree_passes_the_check(tree):
    # The single lint step in its default, strict mode: every detected file
    # marked, every marker well formed and every rule over its roots, as
    # `check_source_text.py --root .` runs it (exit 0 is no failure).
    _, result = tree
    assert result.failures == [], "\n".join(result.failures)


def test_the_rules_visit_the_tree(tree):
    # The control: a rule that found nothing because it read nothing passes
    # vacuously.
    sources, result = tree
    assert result.rules.visited["no-bare-conftest-import"] > 100
    # Every shipped rule's roots reach at least one file of the tree.
    assert not [r for r, n in result.rules.visited.items() if n == 0], result.rules.visited
    assert "tests/unit/worker/conftest.py" in sources.read_paths
    assert "tests/unit/reconciler/conftest.py" in sources.read_paths


def test_the_worker_rules_visit_the_files_their_sweeps_read(tree):
    # The controls the moved worker sweeps carried: the forge sweep refused a
    # walk that found fewer than three modules, and each single-file rule
    # passes vacuously if its file moves away from its root.
    _, result = tree
    visited = result.rules.visited
    assert visited["worker-no-dead-diff-credential-helper"] > 50
    assert visited["forge-token-modules-use-the-one-opener"] > 100
    assert visited["repo-index-imports-only-its-pins"] >= 3
    for rule_id in ("issue-reads-github-hosts-from-forge", "gate-wait-hook-standard-library-only",
                    "browser-runner-no-script-evaluation", "merge-no-not-mergeable-code",
                    "render-takes-name-class-from-identity"):
        assert visited[rule_id] == 1, rule_id


#: The floors the service tests' own sweeps held before they became rules in
#: scripts/source_text_rules/services.py (`visited >= 30`, `len(modules) > 5`,
#: `len(trees) > 20`, `scanned > 100`), and one file for each rule over named
#: files: a root that moved would otherwise leave its rule passing on nothing.
SERVICE_RULE_FLOORS = {
    "fix-rounds-running-only-in-the-control-plane-park": 1,
    "no-capacity-module-reads-stored-task-totals": 30,
    "install-reads-only-the-desktop-secret": 1,
    "install-names-no-resume-flag": 1,
    "install-names-no-brands-api": 3,
    "tfvars-reads-no-committed-tfvars": 1,
    "team-add-prompts-for-nothing": 1,
    "hooks-receiver-compares-in-constant-time": 1,
    "mcp-builds-its-client-in-one-place": 1,
    "mcp-launcher-spelled-only-in-invocation": 6,
    "mcp-no-hand-spelled-command": 6,
    "mcp-reads-the-server-registry-and-mints-only-secret-links": 1,
    "mcp-tail-command-only-through-follow": 2,
    "mcp-launch-names-no-dropped-release": 1,
    "mcp-qa-targets-never-sends-the-credential": 1,
    "mcp-no-handler-reaches-a-role-write": 21,
    "sc-pane-starts-no-timer-and-reaches-no-model": 2,
    "sc-debug-report-reads-only-the-api": 1,
    "sc-reads-no-swarm-tenant-itself": 1,
    "no-writer-moves-failed-to-ready": 101,
    "plugin-intake-reads-no-mount": 1,
    "speech-watchdog-stdlib-only": 1,
    "voice-reads-no-personal-chatgpt-sign-in": 4,
}


@pytest.mark.parametrize("rule_id", sorted(SERVICE_RULE_FLOORS))
def test_the_service_rules_visit_what_their_tests_did(tree, rule_id):
    _, result = tree
    assert result.rules.visited[rule_id] >= SERVICE_RULE_FLOORS[rule_id], (
        rule_id, result.rules.visited[rule_id])


def test_the_detector_finds_the_tree_s_source_text_tests(tree):
    _, result = tree
    assert len(result.detected) > 100
    assert all(rel.startswith(("tests/", "apps/")) for rel in result.detected)
    assert "tests/unit/scripts/test_check_source_text.py" in result.detected
    # The helper shape, `def src(name): return (UI / name).read_text()`
    # called as src("api.ts"), the review of issue 73 found unselected.
    assert "tests/unit/control_plane/test_blocker_ui_surface.py" in result.detected
    assert "tests/unit/control_plane/test_outcomes_classifier_parity.py" in result.detected


def test_the_check_runs_in_the_guard_set_and_lint():
    text = (ROOT / "scripts" / "changed-guards.sh").read_text(encoding="utf-8")
    guards = text.split("GUARD_TESTS=(", 1)[1].split(")", 1)[0].split()
    # MUTATION: drop this file from GUARD_TESTS and this is red.
    assert "tests/unit/scripts/test_check_source_text.py" in guards
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "check_source_text.py" in makefile.split("\nlint:", 1)[1].split("\n\n", 1)[0]
