"""swarm-graph's `similar_changes` and `rules_for` (docs/design/context-quality.md
§4.2 layers H and R, §4.4; lane CTXQ-T).

The snapshot is the KG2 one `graphmcp_fixtures` builds from a small extracted
repository, with a `history` and a `rules` layer added under `index_shards`
in the shape `agent_worker.graphmcp.snapshot` documents for lane CTXQ-H to
write: content-addressed, gzip JSON lines, named by the sha256 of the stored
bytes, exactly as the indexer stores every other layer.

What the lane's row says these prove: the schema stays under 2k tokens
(test_graphmcp_budgets.py holds the total; the two new tools are checked here
too), answers stay under 4 KiB with paging, and zero rows is `UNKNOWN`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import graphmcp_fixtures as gf
import pytest
import repo_index_fixtures as fx

from agent_worker.graphmcp import server, tools
from agent_worker.graphmcp.freshness import Freshness
from agent_worker.graphmcp.snapshot import Snapshot

FILES = {**fx.PYTHON_APP, "src/pkg/orphan.py": "def never_called():\n    return 0\n"}
USERS = "src/pkg/users.py"
STORE = "src/pkg/store.py"
ORPHAN = "src/pkg/orphan.py"
LOAD_USER = "src/pkg/users.py#load_user"

HISTORY = [
    {"path": USERS, "kind": "change", "pr": 1181, "title": "users sort by name",
     "sha": "a" * 40, "merged_at": "2026-10-12T10:00:00Z", "fix_round": True,
     "files": [USERS, "tests/test_users.py"]},
    {"path": USERS, "kind": "change", "pr": 1090, "title": "load_user normalises",
     "sha": "b" * 40, "merged_at": "2026-10-01T10:00:00Z", "fix_round": False,
     "files": [USERS, STORE]},
    {"path": STORE, "kind": "change", "pr": 1090, "title": "load_user normalises",
     "sha": "b" * 40, "merged_at": "2026-10-01T10:00:00Z", "fix_round": False,
     "files": [USERS, STORE]},
    {"path": USERS, "kind": "co_change", "other": "tests/test_users.py", "support": 6,
     "confidence": 0.8},
    {"path": USERS, "kind": "co_change", "other": STORE, "support": 2, "confidence": 0.3},
    {"path": USERS, "kind": "fix", "sha": "c" * 40, "pr": 1200,
     "subject": "test_users no longer pins today's date", "test": "tests/test_users.py"},
]
RULES = [
    {"glob": "src/pkg/**", "kind": "doc", "ref": "docs/pkg.md#Users",
     "why": "names the users module", "evidence": "path-ref", "confidence": 0.9},
    {"glob": "src/pkg/users.py", "kind": "guard", "ref": "tests/test_guard.py",
     "why": "co-changes with users.py", "evidence": "co-change", "confidence": 0.7},
    {"glob": "src/vendor/", "kind": "cannot_push", "ref": "CLAUDE.md#rules",
     "why": "frozen", "evidence": "declared", "confidence": 1.0},
    {"glob": "*.md", "kind": "doc", "ref": "docs/style.md", "why": "top-level docs only",
     "evidence": "path-ref", "confidence": 0.5},
]


def _blob(rows: list[dict]) -> tuple[str, bytes]:
    raw = b"".join(json.dumps(r, sort_keys=True, separators=(",", ":")).encode() + b"\n"
                   for r in rows)
    stored = gzip.compress(raw, mtime=0)
    return hashlib.sha256(stored).hexdigest(), stored


def add_layer(snapshot: Path, layer: str, shards: dict[str, list[dict]]) -> None:
    """Add `layer` to the snapshot's `index_shards`, stored as the indexer stores one."""
    manifest = json.loads((snapshot / "manifest.json").read_text())
    table = {}
    for key, rows in shards.items():
        hexdigest, stored = _blob(rows)
        (snapshot / "blobs" / f"{hexdigest}.jsonl.gz").write_bytes(stored)
        table[key] = {"blob": "sha256:" + hexdigest, "records": len(rows),
                      "bytes": len(stored), "raw_bytes": len(gzip.decompress(stored))}
    manifest.setdefault("index_shards", {})[layer] = table
    (snapshot / "manifest.json").write_text(json.dumps(manifest))


def by_module(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["path"].rsplit("/", 1)[0], []).append(row)
    return out


@pytest.fixture(scope="module")
def document(tmp_path_factory) -> tuple[Path, dict]:
    root = tmp_path_factory.mktemp("graphmcp-ctx")
    repo = fx.build_repo(root / "repo", FILES)
    return repo, gf.graph_of(repo)


@pytest.fixture
def bare(document, tmp_path) -> Path:
    """A snapshot as the indexer writes it today: no history, no rules."""
    return gf.write_snapshot(document[1], tmp_path / "snapshot")


@pytest.fixture
def snapshot(bare) -> Path:
    add_layer(bare, "history", by_module(HISTORY))
    add_layer(bare, "rules", {"*": RULES})
    return bare


def ask(snapshot: Path, tool: str, arguments: dict, *, workdir: Path | None = None
        ) -> tuple[dict, bool]:
    snap = Snapshot(snapshot)
    fresh = Freshness.establish(snap.commit_sha, workdir=workdir, record=None)
    text, failed = tools.call(snap, fresh.observe(), tool, arguments)
    assert len(text.encode()) <= tools.ANSWER_CAP, len(text)
    answer = json.loads(text)
    assert next(iter(answer)) == "freshness", answer
    return answer, failed


def rows(answer: dict, rel: str) -> list[dict]:
    return [r for r in answer["rows"] if r.get("rel") == rel]


# --- the schema ---------------------------------------------------------------

def test_both_tools_are_served_and_listed():
    assert "similar_changes" in tools.TOOL_NAMES
    assert "rules_for" in tools.TOOL_NAMES
    assert set(tools.HANDLERS) == set(tools.TOOL_NAMES)


def test_the_two_new_schemas_fit_in_the_budget_the_six_left():
    """§7.7's 2k tokens at 3 bytes a token covers all eight tools and the instructions."""
    listed = json.dumps({"tools": tools.TOOLS}, separators=(",", ":"))
    assert len(listed.encode()) + len(server.INSTRUCTIONS.encode()) <= 2000 * 3
    new = [t for t in tools.TOOLS if t["name"] in ("similar_changes", "rules_for")]
    assert len(json.dumps(new, separators=(",", ":"))) <= 900


# --- similar_changes ----------------------------------------------------------

def test_similar_changes_lists_earlier_prs_fixes_and_co_changes_for_a_file(snapshot):
    answer, failed = ask(snapshot, "similar_changes", {"files": [USERS]})
    assert not failed, answer
    assert [r["pr"] for r in rows(answer, "change")] == [1181, 1090]
    assert rows(answer, "change")[0]["fix_round"] is True
    fix = rows(answer, "fix")[0]
    assert fix["sha"] == "c" * 40 and fix["test"] == "tests/test_users.py"
    assert [r["path"] for r in rows(answer, "co_change")] == ["tests/test_users.py", STORE]
    assert answer["changes"] == 2 and answer["fixes"] == 1 and answer["co_changes"] == 2
    assert "history" not in answer


def test_a_pr_touching_several_inputs_is_one_row_ranked_first(snapshot):
    answer, _ = ask(snapshot, "similar_changes", {"files": [USERS, STORE]})
    changes = rows(answer, "change")
    assert [r["pr"] for r in changes] == [1090, 1181]
    assert changes[0]["touches"] == [STORE, USERS]
    # an input is never its own co-change
    assert STORE not in [r["path"] for r in rows(answer, "co_change")]


def test_similar_changes_takes_a_symbol_as_its_file(snapshot):
    answer, failed = ask(snapshot, "similar_changes", {"symbols": [LOAD_USER]})
    assert not failed, answer
    assert [r["pr"] for r in rows(answer, "change")] == [1181, 1090]


def test_zero_history_rows_is_unknown_never_no_precedent(snapshot):
    answer, failed = ask(snapshot, "similar_changes", {"files": [ORPHAN]})
    assert not failed, answer
    assert answer["history"] == "UNKNOWN"
    assert "UNKNOWN" in answer["note"]
    assert answer["total"] == 0


def test_an_input_with_no_history_beside_one_with_some_is_named(snapshot):
    answer, _ = ask(snapshot, "similar_changes", {"files": [USERS, ORPHAN]})
    assert answer["no_history"] == [ORPHAN]
    assert "history" not in answer


def test_a_snapshot_without_the_history_layer_answers_unknown_and_says_why(bare):
    answer, failed = ask(bare, "similar_changes", {"files": [USERS]})
    assert not failed, answer
    assert answer["history"] == "UNKNOWN"
    assert "no history layer" in answer["note"]


def test_similar_changes_needs_an_input_and_refuses_a_path_outside_the_repo(snapshot):
    answer, failed = ask(snapshot, "similar_changes", {})
    assert failed and "files" in answer["error"]
    answer, failed = ask(snapshot, "similar_changes", {"files": ["../etc/passwd"]})
    assert failed and "repository-relative" in answer["error"]


def test_a_history_row_on_a_changed_path_is_marked_stale(snapshot, document):
    repo = document[0]
    (repo / USERS).write_text((repo / USERS).read_text() + "\n# edited\n")
    answer, _ = ask(snapshot, "similar_changes", {"files": [USERS]}, workdir=repo)
    fix = rows(answer, "fix")[0]
    assert fix.get("stale"), fix


# --- rules_for ----------------------------------------------------------------

def test_rules_for_matches_globs_directories_and_exact_paths(snapshot):
    answer, failed = ask(snapshot, "rules_for", {"files": [USERS]})
    assert not failed, answer
    assert [(r["rel"], r["ref"]) for r in answer["rows"]] == [
        ("guard", "tests/test_guard.py"), ("doc", "docs/pkg.md#Users")]
    assert answer["guards"] == 1 and answer["docs"] == 1
    assert answer["cannot_push"] == []


def test_a_single_star_does_not_cross_a_directory(snapshot):
    answer, _ = ask(snapshot, "rules_for", {"files": ["docs/pkg.md"]})
    assert answer["rules"] == "UNKNOWN"
    answer, _ = ask(snapshot, "rules_for", {"files": ["README.md"]})
    assert [r["ref"] for r in answer["rows"]] == ["docs/style.md"]


def test_a_cannot_push_path_is_named_in_the_head_and_ranked_first(snapshot):
    vendored = "src/vendor/lib.py"
    answer, _ = ask(snapshot, "rules_for", {"files": [USERS, vendored]})
    assert answer["cannot_push"] == [vendored]
    assert answer["rows"][0]["rel"] == "cannot_push"
    assert answer["rows"][0]["files"] == [vendored]


def test_zero_rule_rows_is_unknown_never_unconstrained(snapshot):
    answer, failed = ask(snapshot, "rules_for", {"files": ["lib/other.py"]})
    assert not failed, answer
    assert answer["rules"] == "UNKNOWN" and "UNKNOWN" in answer["note"]
    assert answer["no_rules"] == ["lib/other.py"]


def test_a_snapshot_without_the_rules_layer_answers_unknown_and_says_why(bare):
    answer, failed = ask(bare, "rules_for", {"files": [USERS]})
    assert not failed, answer
    assert answer["rules"] == "UNKNOWN"
    assert "no rules layer" in answer["note"]


def test_rules_sharded_by_module_are_read_as_well_as_one_whole_shard(bare):
    add_layer(bare, "rules", {"src/pkg": RULES[:2], "src/vendor": RULES[2:3]})
    answer, _ = ask(bare, "rules_for", {"files": [USERS]})
    assert {r["ref"] for r in answer["rows"]} == {"tests/test_guard.py", "docs/pkg.md#Users"}


# --- the 4 KiB cap and paging ---------------------------------------------------

def test_many_rows_page_under_the_cap_and_every_row_is_reached_once(bare):
    many = [{"path": USERS, "kind": "change", "pr": 2000 + n, "title": "x" * 120,
             "sha": f"{n:040x}", "merged_at": f"2026-09-{1 + n % 28:02d}T00:00:00Z",
             "fix_round": False, "files": [f"src/pkg/f{k}.py" for k in range(40)]}
            for n in range(120)]
    add_layer(bare, "history", by_module(many))
    add_layer(bare, "rules", {"*": [{"glob": "src/**", "kind": "doc", "ref": f"docs/d{n}.md",
                                     "why": "y" * 150, "evidence": "path-ref",
                                     "confidence": 0.5} for n in range(120)]})
    for tool, expected in (("similar_changes", 120), ("rules_for", 120)):
        seen: list[dict] = []
        cursor = None
        pages = 0
        while True:
            arguments: dict = {"files": [USERS]}
            if cursor is not None:
                arguments["cursor"] = cursor
            answer, failed = ask(bare, tool, arguments)
            assert not failed, answer
            assert answer["total"] == expected
            seen += answer["rows"]
            pages += 1
            cursor = answer["next_cursor"]
            if cursor is None:
                break
        assert pages > 1, tool
        assert len(seen) == expected, tool
    # A PR's file list is cut to a few, with the count, so one row stays small.
    first = ask(bare, "similar_changes", {"files": [USERS]})[0]["rows"][0]
    assert len(first["files"]) <= tools.SHOWN_FILES and first["files_total"] == 40
