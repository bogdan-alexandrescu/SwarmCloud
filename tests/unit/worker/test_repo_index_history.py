"""Layers H (history) and R (rules) of the repository graph (lane CTXQ-H).

docs/design/context-quality.md §4.2 adds two retrieval layers to the graph,
built by the indexer from the clone it already holds and stored beside the
other shards (`context_shards`, never `index_shards`: swarm-api's
`territory._index_table` refuses a manifest whose `index_shards` names a layer
it does not know, so a new layer there would make every graph unreadable to
the impact and territory routes until swarm-api moved too):

  H  history     for each file, the last merged PRs that touched it, and
                 whether a fix round ran
     co_change   a file and the tests or guards that change with it
     test_fixes  the earlier commits that FIXED a test: the second fixer of a
                 time bomb is shown the first fix (§3.2 point 5, class L)
  R  rules       which doc, house rule (CLAUDE.md) and guard polices a path,
                 mined from path mentions and from co-change with guards: the
                 refusal guard discovers codes by scan, so its file never
                 names `refusals.py`, and only its history links them (class G)
     cannot_push `.github/workflows/**` and the paths the house rules freeze
                 (class W)
     test_runners which command runs a JS test: swarm-ui has three runners,
                 and vitest's `include` skips `tests/`, so a node test changed
                 there is run only by `node tests/run.mjs` (#1141, class T)

What the tests prove, from §7's row for CTXQ-H: on a fixture repository a
guard that co-changes with refusal additions is linked to `refusals.py`; a
doc naming a path is a rule row; swarm-ui's node tests are test files with
test edges, and each has its runner; the shard cap holds.

Offline: fixture repositories built with git in a temporary directory; the
store is a directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import repo_index_fixtures as fx

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"
HISTORY = TOOL_DIR / "repo_index_history.py"
WRITER = TOOL_DIR / "repo_graph_shards.py"
EXTRACTOR = TOOL_DIR / "repo_index_extract.py"
DOCKERFILE = REPO_ROOT / "images" / "agent-runtime-indexer" / "Dockerfile"
TENANT = "eng"
REPO_ID = "gh-example-org-widgets"
DAY = 86_400
T0 = 1_780_000_000


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"nothing at {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hist() -> Any:
    return _load("repo_index_history", HISTORY)


@pytest.fixture(scope="module")
def shards() -> Any:
    return _load("repo_graph_shards", WRITER)


# --- fixture repositories -----------------------------------------------------

Commit = tuple[int, str, str, Mapping[str, str]]


def make_repo(root: Path, files: Mapping[str, str], commits: Sequence[Commit]) -> Path:
    """`files` as one initial commit at T0, then each (time, subject, body, changes)."""
    root.mkdir(parents=True)
    home = root.parent / (root.name + "-home")
    home.mkdir()
    env = fx.git_env(home)

    def git(*args: str, when: int | None = None) -> str:
        run_env = dict(env)
        if when is not None:
            run_env.update(GIT_AUTHOR_DATE=f"@{when} +0000", GIT_COMMITTER_DATE=f"@{when} +0000")
        return subprocess.run(["git", "-C", str(root), *args], env=run_env, check=True,
                              capture_output=True, text=True).stdout

    fx.write_files(root, files)
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "Initial layout", when=T0)
    for when, subject, body, changes in commits:
        fx.write_files(root, changes)
        git("add", "-A")
        message = subject + ("\n\n" + body if body else "")
        git("commit", "-q", "-m", message, when=when)
    return root


def head(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def file_rows(root: Path, tests: Sequence[str] = ()) -> list[dict]:
    """The graph's `files` rows for every file in the checkout, as the extractor marks them."""
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        rel = path.relative_to(root).as_posix()
        rows.append({"path": rel, "status": "parsed", "test": rel in tests})
    return rows


GUARD = "tests/unit/control_plane/test_new_refusals_ship_off.py"
DOCS_GUARD = "tests/unit/scripts/test_docs_guard.py"
SWEEP_TEST = "tests/unit/test_sweep.py"
REFUSALS = "apps/api/swarm_api/refusals.py"

GUARDED_FILES = {
    REFUSALS: "SWITCHES = {}\n",
    "apps/api/swarm_api/errors.py": "class ApiError(Exception):\n    code = 'x'\n",
    "apps/api/swarm_api/routes.py": "ROUTES = []\n",
    "apps/api/swarm_api/sweep.py": "def sweep():\n    return 0\n",
    GUARD: (
        '"""A new refusal ships switched off.\n\n'
        "It enumerates every code `swarm_api.errors` can raise and fails on one\n"
        'that is not behind a switch; this file is the guard.\n"""\n'
        "from pathlib import Path\n\n\n"
        "def test_every_code_is_switched():\n"
        "    for path in Path('.').rglob('*.py'):\n"
        "        assert path\n"
    ),
    DOCS_GUARD: '"""Docs describe what was built."""\n\n\ndef test_docs():\n    assert True\n',
    SWEEP_TEST: "WHEN = '2026-01-01'\n\n\ndef test_sweep():\n    assert WHEN\n",
    "lib/contract/types.py": "class Task:\n    pass\n",
    "scripts/changed-guards.sh": (
        "#!/usr/bin/env bash\nset -euo pipefail\nGUARD_TESTS=(\n"
        f"  {DOCS_GUARD}\n)\n"
    ),
    "docs/ops.md": (
        "# Operations\n\n## Retention\n\n"
        "The sweep lives in `apps/api/swarm_api/sweep.py`; the whole package is\n"
        "apps/api/swarm_api/ and `docs/never-written.md` does not exist.\n"
    ),
    "CLAUDE.md": (
        "# Working here\n\n## The rules\n\n"
        "**1. `lib/contract/` is FROZEN.** Import from it; never edit it.\n\n"
        "Read `docs/ops.md` before touching retention.\n"
    ),
}

GUARDED_COMMITS: list[Commit] = [
    (T0 + 1 * DAY, "REFUSAL_A ships switched off (#101)", "",
     {REFUSALS: "SWITCHES = {'a': 1}\n", GUARD: GUARDED_FILES[GUARD] + "# a\n"}),
    (T0 + 2 * DAY, "REFUSAL_B ships switched off (#102)", "",
     {REFUSALS: "SWITCHES = {'a': 1, 'b': 1}\n", GUARD: GUARDED_FILES[GUARD] + "# a b\n"}),
    (T0 + 3 * DAY, "REFUSAL_C ships switched off (#103)", "",
     {REFUSALS: "SWITCHES = {'a': 1, 'b': 1, 'c': 1}\n",
      GUARD: GUARDED_FILES[GUARD] + "# a b c\n"}),
    (T0 + 4 * DAY, "Routes gain /x (#104)", "", {"apps/api/swarm_api/routes.py": "ROUTES = [1]\n"}),
    (T0 + 5 * DAY, "The sweep test's fixture date no longer expires (#110)", "",
     {SWEEP_TEST: "WHEN = 'far future'\n\n\ndef test_sweep():\n    assert WHEN\n"}),
    (T0 + 6 * DAY, "Sweep keeps young blobs (#111)",
     "* young blobs are kept\n\n* the sweep test covers them",
     {"apps/api/swarm_api/sweep.py": "def sweep():\n    return 1\n",
      SWEEP_TEST: "WHEN = 'far future'\n\n\ndef test_sweep():\n    assert WHEN != 1\n"}),
    (T0 + 7 * DAY, "Sweep retires old manifests (#112)",
     "## Continuation\n\nCI round 1: the sweep test read the wall clock.",
     {"apps/api/swarm_api/sweep.py": "def sweep():\n    return 2\n",
      SWEEP_TEST: "WHEN = 'pinned'\n\n\ndef test_sweep():\n    assert WHEN != 2\n"}),
    (T0 + 8 * DAY, "Fix the sweep test's second clock (#113)", "",
     {"apps/api/swarm_api/sweep.py": "def sweep():\n    return 3\n",
      SWEEP_TEST: "WHEN = 'pinned twice'\n\n\ndef test_sweep():\n    assert WHEN != 3\n"}),
]
GUARDED_TESTS = (GUARD, DOCS_GUARD, SWEEP_TEST)


@pytest.fixture(scope="module")
def guarded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_repo(tmp_path_factory.mktemp("guarded") / "repo", GUARDED_FILES, GUARDED_COMMITS)


@pytest.fixture(scope="module")
def context(hist: Any, guarded: Path) -> dict:
    return hist.build_context(guarded, commit_sha=head(guarded),
                              files=file_rows(guarded, GUARDED_TESTS))


def rows(context: dict, layer: str, **match: Any) -> list[dict]:
    return [r for r in context["layers"][layer]
            if all(r.get(k) == v for k, v in match.items())]


# --- R: rules -----------------------------------------------------------------

def test_a_guard_that_co_changes_with_refusal_additions_is_linked_to_refusals(context):
    linked = rows(context, "rules", path=REFUSALS, kind="guard", source=GUARD)
    assert linked, context["layers"]["rules"]
    assert linked[0]["evidence"] == "co-change"
    assert linked[0]["commits"] == 3
    assert linked[0]["target"] == "file"
    assert 0 < linked[0]["confidence"] <= 1


def test_a_guard_never_named_in_a_doc_is_still_linked_only_by_its_history(context):
    """The refusal guard's file does not name `refusals.py`: the link is history alone."""
    assert {r["evidence"] for r in rows(context, "rules", path=REFUSALS, source=GUARD)} \
        == {"co-change"}


def test_a_guard_docstring_naming_a_module_is_a_rule_row(context):
    named = rows(context, "rules", path="apps/api/swarm_api/errors.py", source=GUARD)
    assert [r["evidence"] for r in named] == ["path-ref"]
    assert named[0]["kind"] == "guard"


def test_a_doc_naming_a_path_is_a_rule_row(context):
    named = rows(context, "rules", path="apps/api/swarm_api/sweep.py", source="docs/ops.md")
    assert len(named) == 1
    row = named[0]
    assert row["kind"] == "doc"
    assert row["evidence"] == "path-ref"
    assert row["line"] == 5
    assert row["section"] == "Retention"


def test_a_doc_naming_a_directory_is_a_directory_rule(context):
    named = rows(context, "rules", path="apps/api/swarm_api", source="docs/ops.md")
    assert [r["target"] for r in named] == ["dir"]


def test_a_path_that_does_not_exist_is_not_a_rule(context):
    assert not rows(context, "rules", path="docs/never-written.md")


def test_claude_md_naming_a_path_is_a_house_rule(context):
    named = rows(context, "rules", path="docs/ops.md", source="CLAUDE.md")
    assert [r["kind"] for r in named] == ["house_rule"]


def test_the_declared_guard_set_polices_the_whole_tree(context):
    declared = rows(context, "rules", source=DOCS_GUARD, evidence="declared")
    assert [(r["path"], r["target"], r["kind"]) for r in declared] == [(".", "dir", "guard")]


def test_workflows_and_frozen_paths_cannot_be_pushed(context):
    patterns = {r["pattern"]: r for r in context["layers"]["cannot_push"]}
    assert ".github/workflows/**" in patterns
    assert "lib/contract/**" in patterns
    assert patterns["lib/contract/**"]["source"] == "CLAUDE.md"
    assert patterns["lib/contract/**"]["line"] == 5
    assert "FROZEN" in patterns["lib/contract/**"]["why"]


# --- H: history ----------------------------------------------------------------

def test_a_files_history_lists_its_merged_prs_newest_first(context):
    (row,) = rows(context, "history", path="apps/api/swarm_api/sweep.py")
    assert [c["pr"] for c in row["changes"]] == [113, 112, 111]
    newest = row["changes"][0]
    assert newest["title"] == "Fix the sweep test's second clock"
    assert newest["files"] == ["apps/api/swarm_api/sweep.py", SWEEP_TEST]
    assert newest["date"].endswith("Z")


def test_whether_a_fix_round_ran_is_true_false_or_unknown(context):
    (row,) = rows(context, "history", path="apps/api/swarm_api/sweep.py")
    by_pr = {c["pr"]: c for c in row["changes"]}
    assert by_pr[112]["fix_round"] is True  # a `## Continuation` section
    assert by_pr[111]["fix_round"] is False  # squashed subjects, none a fix
    assert by_pr[113]["fix_round"] is None  # no squashed subjects: not known
    assert by_pr[113]["fix"] is True  # but its own subject is a fix


def test_co_change_pairs_a_file_with_its_guard(context):
    (pair,) = rows(context, "co_change", path=REFUSALS, partner=GUARD)
    assert pair["partner_kind"] == "guard"
    assert pair["commits"] == 3
    assert pair["support"] == 1.0


def test_the_second_fixer_of_a_test_is_shown_the_earlier_fixes(context):
    fixes = rows(context, "test_fixes", test=SWEEP_TEST)
    assert [f["pr"] for f in fixes] == [113, 112, 110]
    assert {f["pr"]: f["why"] for f in fixes} == {
        113: "fix subject", 112: "fix round", 110: "test-only change"}
    assert fixes[2]["subject"] == "The sweep test's fixture date no longer expires (#110)"


def test_a_feature_that_extends_a_test_is_not_a_fix(context):
    assert not rows(context, "test_fixes", test=SWEEP_TEST, pr=111)
    assert not rows(context, "test_fixes", test=GUARD)


def test_the_context_is_deterministic(hist, guarded, context):
    again = hist.build_context(guarded, commit_sha=head(guarded),
                               files=file_rows(guarded, GUARDED_TESTS))
    assert json.dumps(again, sort_keys=True) == json.dumps(context, sort_keys=True)


def test_the_meta_says_what_the_history_covered(context):
    meta = context["meta"]
    assert meta["available"] is True
    # The root commit diffs against nothing, so it would read as "every file
    # changed together": it is counted apart and never paired.
    assert meta["commits"] == len(GUARDED_COMMITS)
    assert meta["root_commits"] == 1
    assert meta["window_days"] == 90


def test_a_checkout_without_git_has_rules_but_no_history(hist, tmp_path):
    fx.write_files(tmp_path, {"docs/a.md": "See `src/x.py`.\n", "src/x.py": "x = 1\n"})
    built = hist.build_context(tmp_path, commit_sha="0" * 40, files=file_rows(tmp_path))
    assert built["meta"]["available"] is False
    assert built["meta"]["reason"]
    assert built["layers"]["history"] == []
    assert rows(built, "rules", path="src/x.py", source="docs/a.md")
    assert [r["pattern"] for r in built["layers"]["cannot_push"]] == [".github/workflows/**"]


def test_a_one_commit_deep_clone_says_its_history_is_not_known(hist, guarded, tmp_path):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", guarded.as_uri(), str(shallow)],
                   check=True, capture_output=True)
    built = hist.build_context(shallow, commit_sha=head(shallow),
                               files=file_rows(shallow, GUARDED_TESTS))
    assert built["meta"]["available"] is False
    assert "shallow" in built["meta"]["reason"]
    assert built["meta"]["boundary_commits"] == 1
    assert built["meta"]["root_commits"] == 0
    assert built["layers"]["history"] == []
    assert rows(built, "rules", path="apps/api/swarm_api/sweep.py", source="docs/ops.md")


def test_a_path_named_by_many_docs_keeps_the_strongest_rows(hist, tmp_path):
    docs = {f"docs/d{i:03}.md": "See `src/x.py`.\n" for i in range(hist.MAX_RULES_PER_PATH + 15)}
    fx.write_files(tmp_path, {**docs, "src/x.py": "x = 1\n",
                              "CLAUDE.md": "Never break `src/x.py`.\n"})
    built = hist.build_context(tmp_path, commit_sha="0" * 40, files=file_rows(tmp_path))
    named = rows(built, "rules", path="src/x.py")
    assert len(named) == hist.MAX_RULES_PER_PATH
    assert named[0]["kind"] == "house_rule"
    assert "rules:per_path" in built["meta"]["truncated"]


# --- swarm-ui's node tests (#1141) -------------------------------------------------

UI = "apps/swarm-ui"
UI_FILES = {
    f"{UI}/package.json": json.dumps({"name": "swarm-ui", "scripts": {
        "test": "npm run test:components && npm run test:node",
        "test:components": "vitest run",
        "test:node": "node tests/run.mjs && node --import ./test/register-ts.mjs "
                     "--test test/*.test.mjs",
    }}, indent=2) + "\n",
    f"{UI}/vitest.config.ts": (
        "import { defineConfig } from 'vitest/config'\n\nexport default defineConfig({\n"
        "  test: {\n"
        "    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', 'test/**/*.test.tsx'],\n"
        "  },\n})\n"
    ),
    f"{UI}/src/help.ts": (
        "export const HELP = { a: 1 }\n\n"
        "export function topicFor(id: string): string {\n  return id\n}\n"
    ),
    f"{UI}/src/checks.ts": "export function deriveChecks(x: number): number {\n  return x + 1\n}\n",
    f"{UI}/tests/help.test.ts": (
        "import assert from 'node:assert/strict'\nimport test from 'node:test'\n"
        "import { topicFor } from '../src/help'\n\n"
        "test('topic', () => {\n  assert.equal(topicFor('a'), 'a')\n})\n"
    ),
    f"{UI}/tests/run.mjs": "import { build } from 'esbuild'\n\nexport const built = build\n",
    f"{UI}/test/checks.test.mjs": (
        "import test from 'node:test'\nimport { deriveChecks } from '../src/checks.ts'\n\n"
        "test('derive', () => {\n  deriveChecks(1)\n})\n"
    ),
    f"{UI}/test/LiveThumb.test.tsx": (
        "import { HELP } from '../src/help'\n\nexport const seen = HELP.a\n"
    ),
    f"{UI}/src/__tests__/help.test.tsx": (
        "import { topicFor } from '../help'\n\nexport const seen = topicFor('b')\n"
    ),
    f"{UI}/legacy/old.test.ts": "import { HELP } from '../src/help'\n\nexport const o = HELP\n",
}


@pytest.fixture(scope="module")
def ui_graph(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    pytest.importorskip("tree_sitter")
    root = make_repo(tmp_path_factory.mktemp("ui") / "repo", UI_FILES, [])
    out = root.parent / "graph.json"
    done = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--repo", str(root), "--out", str(root.parent / "i.json"),
         "--graph-out", str(out), "--no-lsp"], capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stderr
    return root, json.loads(out.read_text())


def test_swarm_ui_node_tests_are_test_files(ui_graph):
    _root, graph = ui_graph
    flagged = {f["path"]: f.get("test") for f in graph["files"]}
    assert flagged[f"{UI}/tests/help.test.ts"] is True
    assert flagged[f"{UI}/test/checks.test.mjs"] is True
    assert flagged[f"{UI}/tests/run.mjs"] is False


def test_swarm_ui_node_tests_are_test_edges(ui_graph):
    _root, graph = ui_graph
    tested = {(r["symbol"], r["test"].split("#", 1)[0]) for r in graph["symbol_test_map"]}
    assert (f"{UI}/src/help.ts#topicFor", f"{UI}/tests/help.test.ts") in tested
    assert (f"{UI}/src/checks.ts#deriveChecks", f"{UI}/test/checks.test.mjs") in tested


def test_each_swarm_ui_test_names_the_runner_that_runs_it(hist, ui_graph):
    root, graph = ui_graph
    built = hist.build_context(root, commit_sha=head(root), files=graph["files"])
    runners = {r["test"]: r for r in built["layers"]["test_runners"]}
    node = runners[f"{UI}/tests/help.test.ts"]
    assert (node["runner"], node["command"], node["cwd"], node["script"]) == (
        "node", "node tests/run.mjs", UI, "test:node")
    node_test = runners[f"{UI}/test/checks.test.mjs"]
    assert node_test["runner"] == "node:test"
    assert node_test["command"] == "node --import ./test/register-ts.mjs --test test/*.test.mjs"
    for vitest in (f"{UI}/src/__tests__/help.test.tsx", f"{UI}/test/LiveThumb.test.tsx"):
        rel = vitest[len(UI) + 1:]
        assert (runners[vitest]["runner"], runners[vitest]["command"],
                runners[vitest]["script"]) == ("vitest", f"npx vitest run {rel}",
                                               "test:components")
    orphan = runners[f"{UI}/legacy/old.test.ts"]
    assert orphan["runner"] is None
    assert orphan["evidence"] == "no package script or vitest include runs it"


def test_vitest_without_an_include_uses_its_default(hist, tmp_path):
    fx.write_files(tmp_path, {
        "web/package.json": json.dumps({"scripts": {"test": "vitest"}}),
        "web/src/a.spec.ts": "export const a = 1\n",
    })
    built = hist.build_context(tmp_path, commit_sha="0" * 40,
                               files=file_rows(tmp_path, ["web/src/a.spec.ts"]))
    (row,) = built["layers"]["test_runners"]
    assert (row["runner"], row["command"], row["cwd"]) == (
        "vitest", "npx vitest run src/a.spec.ts", "web")


# --- the shard writer -----------------------------------------------------------

def _graph_doc(commit_sha: str) -> dict:
    symbol = {"id": "apps/api/swarm_api/refusals.py#SWITCHES", "kind": "variable",
              "path": REFUSALS, "start_line": 1, "end_line": 1}
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": commit_sha,
        "branch": "main", "base_sha": None, "languages": [], "truncated": [], "extractor": {},
        "symbols": [symbol], "call_edges": [], "symbol_test_map": [],
        "files": [{"path": REFUSALS, "status": "parsed", "test": False}],
    }


def _blob_rows(shards: Any, root: Path, entry: dict) -> list[dict]:
    digest = entry["blob"].split(":", 1)[1]
    raw = (root / shards.graph_root(TENANT, REPO_ID) / "blobs" / f"{digest}{shards.BLOB_SUFFIX}"
           ).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    return shards.decode_shard(raw)


def test_the_layers_are_stored_beside_the_index_shards_not_in_them(shards, context, guarded,
                                                                     tmp_path):
    document = _graph_doc(head(guarded))
    report = shards.write_graph(document, shards.LocalStore(tmp_path), tenant_id=TENANT,
                                repo_id=REPO_ID, context=context)
    manifest = json.loads((tmp_path / report["manifest"]).read_text())
    # swarm-api's territory reader refuses any other key under index_shards.
    assert set(manifest["index_shards"]) <= {
        "communities", "terms", "signatures", "signature_changes", "flows"}
    assert set(manifest["context_shards"]) == set(shards.CONTEXT_LAYERS)
    assert manifest["context"]["available"] is True
    assert manifest["context"]["version"] == shards.CONTEXT_VERSION
    rules = _blob_rows(shards, tmp_path, manifest["context_shards"]["rules"]["apps/api/swarm_api"])
    assert any(r["path"] == REFUSALS and r["source"] == GUARD for r in rules)
    (cannot,) = manifest["context_shards"]["cannot_push"].values()
    assert ".github/workflows/**" in {r["pattern"] for r in _blob_rows(shards, tmp_path, cannot)}
    fixes = _blob_rows(shards, tmp_path, manifest["context_shards"]["test_fixes"]["tests/unit"])
    assert [f["pr"] for f in fixes if f["test"] == SWEEP_TEST] == [113, 112, 110]


def test_context_rows_are_read_back_for_a_path_and_its_ancestors(shards, context, guarded,
                                                                   tmp_path):
    store = shards.LocalStore(tmp_path)
    shards.write_graph(_graph_doc(head(guarded)), store, tenant_id=TENANT, repo_id=REPO_ID,
                       context=context)
    found = shards.read_context(store, tenant_id=TENANT, repo_id=REPO_ID,
                                commit_sha=head(guarded), layer="rules", path=REFUSALS)
    sources = {(r["source"], r["evidence"]) for r in found}
    assert (GUARD, "co-change") in sources
    assert (DOCS_GUARD, "declared") in sources  # the `.` rule reaches every path
    assert all(r["path"] == REFUSALS or r["target"] == "dir" for r in found)


def test_the_sweep_keeps_every_context_blob(shards, context, guarded, tmp_path):
    from datetime import timedelta

    store = shards.LocalStore(tmp_path)
    report = shards.write_graph(_graph_doc(head(guarded)), store, tenant_id=TENANT,
                                repo_id=REPO_ID, context=context)
    manifest = json.loads((tmp_path / report["manifest"]).read_text())
    named = {e["blob"].split(":", 1)[1] for layer in manifest["context_shards"].values()
             for e in layer.values()}
    assert named <= shards.referenced_blobs(manifest)
    swept = shards.sweep(store, tenant_id=TENANT, repo_id=REPO_ID, grace=timedelta(0))
    assert swept["deleted"] == 0


def test_the_manifest_is_byte_identical_on_a_rerun(shards, hist, guarded):
    def once() -> bytes:
        built = hist.build_context(guarded, commit_sha=head(guarded),
                                   files=file_rows(guarded, GUARDED_TESTS))
        _key, manifest, _blobs = shards.build(_graph_doc(head(guarded)), tenant_id=TENANT,
                                              repo_id=REPO_ID, context=built)
        return manifest

    assert once() == once()


def test_the_shard_cap_holds_by_dropping_history_before_rules_and_never_cannot_push(
        shards, context, guarded):
    document = _graph_doc(head(guarded))
    padded = json.loads(json.dumps(context))
    # Incompressible history rows: far over a small ceiling on their own.
    noise = hashlib.sha256(b"seed").hexdigest()
    for i in range(400):
        noise = hashlib.sha256(noise.encode()).hexdigest()
        padded["layers"]["history"].append({"path": f"pad/{i}.py", "changes": [
            {"pr": i, "sha": noise[:40], "title": noise * 4, "date": "2026-01-01T00:00:00Z",
             "fix": False, "fix_round": None, "fix_subjects": [], "files": [], "files_total": 0}]})
    _key, unbounded, _ = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID,
                                      context=padded)
    _key, graph_only, _ = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID)
    ceiling = json.loads(graph_only)["bytes"]["stored"] + 6_000
    assert json.loads(unbounded)["bytes"]["stored"] > ceiling
    _key, raw, blobs = shards.build(document, tenant_id=TENANT, repo_id=REPO_ID,
                                    context=padded, max_commit_bytes=ceiling)
    manifest = json.loads(raw)
    assert sum(len(b) for b in blobs.values()) <= ceiling
    assert manifest["bytes"]["stored"] <= ceiling
    assert "context:history" in manifest["truncated"]
    assert "history" not in manifest["context_shards"]
    assert "cannot_push" in manifest["context_shards"]
    # The graph's own edges are untouched: context goes first.
    assert "call_edges:below_0.4" not in manifest["truncated"]


def test_without_context_the_manifest_says_why(shards, guarded):
    _key, raw, _ = shards.build(_graph_doc(head(guarded)), tenant_id=TENANT, repo_id=REPO_ID)
    manifest = json.loads(raw)
    assert manifest["context"]["available"] is False
    assert manifest["context"]["reason"]
    assert manifest["context_shards"] == {}


def test_the_cli_reads_the_clone_beside_the_graph_file(shards, hist, tmp_path):
    """The worker writes `work/repo-graph.json` beside the clone `work/repo`
    (agent_worker.indexrun GRAPH_FILE, workspace.REPO_DIR_NAME)."""
    work = tmp_path / "work"
    clone = make_repo(work / "repo", GUARDED_FILES, GUARDED_COMMITS)
    graph = work / "repo-graph.json"
    graph.write_text(json.dumps({**_graph_doc(head(clone)),
                                 "files": file_rows(clone, GUARDED_TESTS)}))
    store = tmp_path / "store"
    assert shards.main(["write", "--graph", str(graph), "--repo-id", REPO_ID, "--tenant",
                        TENANT, "--store", str(store), "--no-sweep"]) == 0
    manifest = json.loads(
        (store / shards.manifest_key(TENANT, REPO_ID, head(clone))).read_text())
    assert manifest["context"]["available"] is True
    assert manifest["context_shards"]["rules"]
    assert manifest["context"]["counts"]["test_fixes"] == 3


def test_the_cli_takes_an_explicit_repo_and_writes_the_graph_without_one(shards, tmp_path):
    graph = tmp_path / "g.json"
    commit = hashlib.sha1(b"lonely").hexdigest()
    graph.write_text(json.dumps(_graph_doc(commit)))
    store = tmp_path / "store"
    assert shards.main(["write", "--graph", str(graph), "--repo-id", REPO_ID, "--tenant",
                        TENANT, "--store", str(store), "--no-sweep",
                        "--repo", str(tmp_path / "absent")]) == 0
    manifest = json.loads((store / shards.manifest_key(TENANT, REPO_ID, commit)).read_text())
    assert manifest["context"]["available"] is False
    assert "absent" in manifest["context"]["reason"]


def test_the_writer_and_the_miner_name_the_same_layers(shards, hist):
    assert shards.CONTEXT_LAYERS == hist.LAYERS
    dropped = {layer for _name, group in shards.CONTEXT_DROP for layer in group}
    assert dropped == set(hist.DROP_FIRST) | set(hist.DROP_NEXT)
    assert "cannot_push" not in dropped


def test_the_self_test_mines_through_the_module_beside_the_writer(tmp_path):
    """As the image runs it: `python3.11 -I`, which puts no script directory on sys.path."""
    done = subprocess.run([sys.executable, "-I", str(WRITER), "--self-test"],
                          capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr
    alone = tmp_path / "alone"
    alone.mkdir()
    (alone / WRITER.name).write_bytes(WRITER.read_bytes())
    done = subprocess.run([sys.executable, "-I", str(alone / WRITER.name), "--self-test"],
                          capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert done.returncode == 1, "a writer without its history module must fail the build"


def test_the_image_ships_the_history_module_beside_the_writer():
    text = DOCKERFILE.read_text()
    assert "repo-index/repo_index_history.py" in text
    assert "/opt/repo-index/repo_index_history.py" in text
