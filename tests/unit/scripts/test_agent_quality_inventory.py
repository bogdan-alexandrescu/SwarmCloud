"""The agent quality baseline is reproducible from the tree it names.

docs/design/agent-quality/ is the agent quality program's baseline and
capability inventory (BGD, 3 November 2026). Its capability matrix cites a
test id for every capability it calls behaviourally tested, and its baseline
names the model each class runs. Both go stale silently: a renamed test still
reads as "tested" and a re-pinned model still reads as the baseline.
scripts/agent_quality_inventory.py reads the tree (never the network, never a
cloud credential) and says what no longer holds, so these tests hold:

  * every pytest id a document under docs/design/agent-quality/ cites names a
    file that exists and a test function defined in it, and every vitest file
    it cites exists ("never report a test as passing that did not run" starts
    with never citing one that does not exist);
  * every model id in terraform/infra/locals.tf `runner_models` is the one
    baseline.md states for its class;
  * known-failures.md lists every skip, expected failure and quarantined test
    by where it is (file, enclosing test, kind) with its reason, and nothing
    the tree no longer has. It holds no count: a count two green changes each
    bump conflicts when they land together, and a list two changes each add a
    row to does not (BGD, 6 November 2026: merge-safe whole-tree checks);
  * every capability matrix row has its four columns, and its live-verified
    cell names a record, says "not read from here" or says why it does not
    apply ("n/a: ..."); a bare "none", "no" or "yes" is refused, because an
    unknown is recorded as unknown, never as zero.

BREAK IT: rename a cited test function, re-pin a model class in locals.tf,
add a `pytest.mark.xfail` under tests/ without its row, delete a skipped test
and leave its row, or write `none` in a live-verified cell: the matching test
below names it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "agent_quality_inventory.py"
DOCS = REPO / "docs" / "design" / "agent-quality"

_spec = importlib.util.spec_from_file_location("agent_quality_inventory", SCRIPT)
inv = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["agent_quality_inventory"] = inv
_spec.loader.exec_module(inv)


# --- the parsers, over planted fixtures -------------------------------------


def test_a_cited_pytest_id_is_found_in_inline_code_and_split_at_the_double_colon():
    text = (
        "| x | `tests/unit/worker/test_a.py::test_one` and "
        "`tests/unit/worker/test_b.py::test_two[case]` |\n"
        "plain tests/unit/worker/test_c.py::test_not_in_code is prose\n"
        "| `apps/swarm-graph/swarm_graph/cache.py::ResultCache` is code, not a test |\n"
    )
    assert inv.cited_pytest_ids(text) == [
        ("tests/unit/worker/test_a.py", "test_one"),
        ("tests/unit/worker/test_b.py", "test_two"),
    ]


def test_a_cited_vitest_file_is_found_in_inline_code():
    text = "`apps/swarm-ui/src/__tests__/cost.totals.test.tsx` and `src/x.ts`"
    assert inv.cited_vitest_files(text) == ["apps/swarm-ui/src/__tests__/cost.totals.test.tsx"]


def test_a_missing_file_and_a_missing_function_are_both_named(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_here.py").write_text(
        "def test_present():\n    pass\n\nasync def test_async_present():\n    pass\n"
    )
    ids = [
        ("tests/test_here.py", "test_present"),
        ("tests/test_here.py", "test_async_present"),
        ("tests/test_here.py", "test_gone"),
        ("tests/test_absent.py", "test_any"),
    ]
    assert inv.missing_pytest_ids(tmp_path, ids) == [
        "tests/test_here.py::test_gone (no such test function)",
        "tests/test_absent.py::test_any (no such file)",
    ]


def test_a_name_that_only_appears_in_a_comment_is_not_a_test_function(tmp_path):
    (tmp_path / "test_c.py").write_text("# def test_ghost():\nx = 'test_ghost'\n")
    assert inv.missing_pytest_ids(tmp_path, [("test_c.py", "test_ghost")]) == [
        "test_c.py::test_ghost (no such test function)"
    ]


def test_runner_models_are_read_from_the_locals_block_and_nowhere_else():
    text = (
        "locals {\n"
        "  # runner_models = { opus = \"claude-opus-1\" } in a comment\n"
        "  runner_models = {\n"
        '    opus   = "claude-opus-5-5"\n'
        '    haiku  = "claude-haiku-5-5"\n'
        "  }\n"
        "  other = {\n"
        '    opus = "not-a-model"\n'
        "  }\n"
        "}\n"
    )
    assert inv.runner_models(text) == {"opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}


def test_runner_models_absent_is_empty_never_a_guess():
    assert inv.runner_models("locals {\n}\n") == {}


def test_markers_are_counted_by_kind_and_a_comment_or_string_mention_is_not_one(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_m.py").write_text(
        "import pytest\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "def test_a():\n    pytest.skip('y')\n"
        "@pytest.mark.xfail(strict=True)\n"
        "def test_b():\n    pass\n"
        "@pytest.mark.skip(reason='z')\n"
        "def test_c():\n    pytest.importorskip('q')\n"
        "# pytest.mark.xfail in a comment is not a marker\n"
    )
    (tests / "helper.txt").write_text("@pytest.mark.xfail\n")
    counts = inv.marker_counts(tmp_path)
    assert counts == {"skipif": 1, "skip_call": 1, "xfail": 1, "skip": 1, "importorskip": 1}


def test_the_quarantine_list_is_its_non_comment_lines(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "flaky.txt").write_text(
        "# header\n\n"
        "tests/unit/worker/test_x.py::test_y #123 2026-11-01\n"
    )
    assert inv.quarantined(tmp_path) == ["tests/unit/worker/test_x.py::test_y #123 2026-11-01"]


def test_the_report_is_json_and_exits_non_zero_when_a_cited_test_is_missing(tmp_path):
    docs = tmp_path / "docs" / "design" / "agent-quality"
    docs.mkdir(parents=True)
    (docs / "capability-matrix.md").write_text("`tests/test_gone.py::test_nothing`\n")
    (tmp_path / "terraform" / "infra").mkdir(parents=True)
    (tmp_path / "terraform" / "infra" / "locals.tf").write_text("locals {\n}\n")
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    report = json.loads(done.stdout)
    assert report["cited_pytest_ids"] == 1
    assert report["missing"] == ["tests/test_gone.py::test_nothing (no such file)"]
    # Not a git checkout: the commit is unknown, never a made-up value.
    assert report["commit"] is None


def test_a_bare_live_verified_cell_and_a_short_row_are_both_named():
    matrix = "\n".join(
        [
            "| Capability | Implemented | Behaviourally tested | Enabled in configuration | Live-verified |",
            "|---|---|---|---|---|",
            "| A | `a.py` | `t` | on | not read from here |",
            "| B | `b.py` | `t` | on | none |",
            "| C | `c.py` | `t` | on | Yes |",
            "| D | `d.py` | `t` | on |",
            "| E | not implemented | none | none | n/a: not implemented |",
            "| F | `f.py` | `t` | on | Batch three, `wf_x`: called twice |",
            "| G \\| H | `g.py` | `t` | on | not read from here |",
        ]
    )
    assert inv.unnamed_live_verified(matrix) == [
        "B: live-verified is 'none'",
        "C: live-verified is 'Yes'",
        "D: 4 columns, not 5",
    ]


def test_the_report_counts_matrix_rows_and_fails_on_an_unnamed_live_cell(tmp_path):
    docs = tmp_path / "docs" / "design" / "agent-quality"
    docs.mkdir(parents=True)
    (docs / "capability-matrix.md").write_text(
        "| Capability | Implemented | Behaviourally tested | Enabled in configuration | Live-verified |\n"
        "|---|---|---|---|---|\n"
        "| A | `a.py` | `t` | on | no |\n"
    )
    (tmp_path / "terraform" / "infra").mkdir(parents=True)
    (tmp_path / "terraform" / "infra" / "locals.tf").write_text("locals {\n}\n")
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    report = json.loads(done.stdout)
    assert report["matrix_rows"] == 1
    assert report["unnamed_live_verified"] == ["A: live-verified is 'no'"]
    assert report["missing"] == []


# --- the skip table: listed by location, never counted ---------------------

_TABLE_HEAD = (
    "| File | Test | Kind | Reason |\n"
    "|---|---|---|---|\n"
)
_ROW_A = "| `tests/test_a.py` | `test_a` | `skipif` | git is not installed |\n"
_ROW_B = "| `tests/test_b.py` | `Checks.test_b` | `skip_call` | node is not installed |\n"
_SKIP_A = (
    "import shutil\nimport pytest\n\n"
    "@pytest.mark.skipif(shutil.which('git') is None, reason='git is not installed')\n"
    "def test_a():\n    pass\n"
)
_SKIP_B = (
    "import shutil\nimport pytest\n\n"
    "class Checks:\n"
    "    def test_b(self):\n"
    "        if shutil.which('node') is None:\n"
    "            pytest.skip('node is not installed')\n"
)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_a_skip_is_found_at_its_file_test_and_kind_with_the_reason_its_source_gives(tmp_path):
    _tree(
        tmp_path,
        {
            "tests/test_a.py": _SKIP_A,
            "tests/test_b.py": _SKIP_B,
            "tests/unit/test_m.py": (
                "import pytest\n"
                "pytestmark = pytest.mark.skipif(True, reason='module wide')\n"
                "cryptography = pytest.importorskip('cryptography')\n"
                "@pytest.mark.xfail(strict=True, reason='E2E-2')\n"
                "def test_c():\n"
                "    # pytest.skip('a comment is not a skip')\n"
                "    x = \"pytest.mark.skip in a string is not one\"\n"
                "    pytest.skip(f'no {x}')\n"
                "    pytest.skip('twice in one test is one row')\n"
            ),
        },
    )
    assert [tuple(s) for s in inv.skip_sites(tmp_path)] == [
        ("tests/test_a.py", "test_a", "skipif", "git is not installed"),
        ("tests/test_b.py", "Checks.test_b", "skip_call", "node is not installed"),
        ("tests/unit/test_m.py", "<module>", "skipif", "module wide"),
        ("tests/unit/test_m.py", "<module>", "importorskip", "cryptography is not importable"),
        ("tests/unit/test_m.py", "test_c", "xfail", "E2E-2"),
        ("tests/unit/test_m.py", "test_c", "skip_call", "no {x}"),
    ]


def test_a_new_skip_the_table_does_not_list_fails_naming_it_and_the_row_to_add(tmp_path):
    _tree(tmp_path, {"tests/test_a.py": _SKIP_A, "tests/test_b.py": _SKIP_B})
    assert inv.skip_table_drift(tmp_path, _TABLE_HEAD + _ROW_A) == [
        "unlisted skip tests/test_b.py Checks.test_b skip_call; add the row: "
        + _ROW_B.strip()
    ]


def test_two_changes_that_each_add_a_listed_skip_pass_alone_and_together(tmp_path):
    base = _tree(tmp_path / "base", {"tests/test_base.py": "def test_ok():\n    pass\n"})
    assert inv.skip_table_drift(base, _TABLE_HEAD) == []
    change_a = _tree(tmp_path / "a", {"tests/test_a.py": _SKIP_A})
    assert inv.skip_table_drift(change_a, _TABLE_HEAD + _ROW_A) == []
    change_b = _tree(tmp_path / "b", {"tests/test_b.py": _SKIP_B})
    assert inv.skip_table_drift(change_b, _TABLE_HEAD + _ROW_B) == []
    # Both landed: each brought its file and its own row; neither edited a
    # number the other also edited, so the merged tree passes as each did.
    both = _tree(tmp_path / "both", {"tests/test_a.py": _SKIP_A, "tests/test_b.py": _SKIP_B})
    assert inv.skip_table_drift(both, _TABLE_HEAD + _ROW_A + _ROW_B) == []
    assert inv.skip_table_drift(both, _TABLE_HEAD + _ROW_B + _ROW_A) == []


def test_a_removed_skip_still_listed_fails_naming_the_stale_line(tmp_path):
    _tree(tmp_path, {"tests/test_a.py": "def test_a():\n    pass\n"})
    doc = "# Known failures\n\n" + _TABLE_HEAD + _ROW_A
    assert inv.skip_table_drift(tmp_path, doc) == [
        "stale row at line 5, no such skip in the tree: " + _ROW_A.strip()
    ]


def test_a_listed_skip_with_no_reason_is_named(tmp_path):
    _tree(tmp_path, {"tests/test_a.py": _SKIP_A})
    doc = _TABLE_HEAD + "| `tests/test_a.py` | `test_a` | `skipif` |  |\n"
    assert inv.skip_table_drift(tmp_path, doc) == [
        "row at line 3 gives no reason: | `tests/test_a.py` | `test_a` | `skipif` |  |"
    ]


def test_a_table_of_another_shape_is_not_read_as_skip_rows():
    doc = (
        "| Test | Condition | Area |\n|---|---|---|\n"
        "| `tests/unit/worker/test_x.py` | node is installed | Jev |\n"
        "| `tests/test_a.py` | `test_a` | `not_a_kind` | why |\n"
    )
    assert inv.listed_skips(doc) == []


def test_a_quarantined_test_must_be_listed_and_a_listed_one_must_be_quarantined(tmp_path):
    _tree(
        tmp_path,
        {"tests/flaky.txt": "# header\ntests/unit/test_x.py::test_y #123 2026-11-01\n"},
    )
    assert inv.quarantine_drift(tmp_path, "nothing quarantined\n") == [
        "unlisted quarantine tests/unit/test_x.py::test_y; add the row: "
        "| `tests/unit/test_x.py::test_y` | quarantined | #123 2026-11-01 |"
    ]
    listed = "| `tests/unit/test_x.py::test_y` | quarantined | #123 |\n"
    assert inv.quarantine_drift(tmp_path, listed) == []
    (tmp_path / "tests" / "flaky.txt").write_text("# header\n")
    assert inv.quarantine_drift(tmp_path, "intro\n" + listed) == [
        "stale row at line 2, not in tests/flaky.txt: " + listed.strip()
    ]


def test_the_report_exits_non_zero_on_an_unlisted_skip(tmp_path):
    _tree(
        tmp_path,
        {
            "tests/test_a.py": _SKIP_A,
            "docs/design/agent-quality/known-failures.md": _TABLE_HEAD,
            "terraform/infra/locals.tf": "locals {\n}\n",
        },
    )
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    report = json.loads(done.stdout)
    assert report["missing"] == []
    assert report["skip_table"] == [
        "unlisted skip tests/test_a.py test_a skipif; add the row: " + _ROW_A.strip()
    ]


# --- the documents, against this tree -----------------------------------------


def _docs_text() -> str:
    return "\n".join(p.read_text() for p in sorted(DOCS.glob("*.md")))


def test_every_test_the_agent_quality_documents_cite_exists_on_this_tree():
    text = _docs_text()
    ids = inv.cited_pytest_ids(text)
    assert len(ids) >= 60, f"the matrix cites only {len(ids)} pytest ids: the reader would miss it"
    assert inv.missing_pytest_ids(REPO, ids) == []
    vitest = inv.cited_vitest_files(text)
    assert vitest, "the matrix cites no console test"
    assert [f for f in vitest if not (REPO / f).is_file()] == []


def test_the_baseline_states_each_class_s_model_as_locals_tf_pins_it():
    models = inv.runner_models((REPO / "terraform" / "infra" / "locals.tf").read_text())
    assert set(models) == {"opus", "fable", "sonnet", "haiku"}
    baseline = (DOCS / "baseline.md").read_text()
    for cls, model in models.items():
        assert f"| `{cls}` | `{model}` |" in baseline, f"baseline.md does not pin {cls} to {model}"


def test_known_failures_lists_every_skip_and_quarantine_on_this_tree_and_nothing_else():
    known = (DOCS / "known-failures.md").read_text()
    assert inv.skip_table_drift(REPO, known) == []
    assert inv.quarantine_drift(REPO, known) == []
    assert "| Count |" not in known, "known-failures.md lists skips; it never counts them"


def test_every_matrix_row_has_four_columns_and_a_named_or_unknown_live_cell():
    matrix = (DOCS / "capability-matrix.md").read_text()
    assert inv.unnamed_live_verified(matrix) == []
    assert inv.matrix_rows(matrix) >= 60
