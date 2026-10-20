"""Live QA 2026-10-11, section work-repos-setup-access: the repository reads
are recomputed from the registration's own shas, and a capped count says so.

* RSA-3 -- an index at the head served `behind_by: 2` and a 20-file
  `head_relation` from a compare of an older pair. `behind_by` and
  `head_relation` are now derived from `current_sha`/`head_sha` on every
  read (`repositories.settle_relation`) and on the writes that move either.
* RSA-4 -- "1,000 edges" read as a total when it was the cut. The coverage
  block names what was cut and whether `test_map_edges` is capped, and a
  block stored before that is completed from the promoted version.
* RSA-6 -- a language whose server timed out was visible only in Settings,
  and its status contradicted its parse counters. The coverage block lists
  degraded languages; the languages table says which measurement is which.
"""

from __future__ import annotations

import json

import pytest

from swarm_api import impact, repoindex, repositories

from .conftest import auth_header
from .repo_fakes import GitHubRepos, TenantTokens, make_client, repo_entry
from .repo_index_fakes import fixture_index

A = "a" * 40
B = "b" * 40
C = "c" * 40
WIDGETS = repo_entry("saga-xyz/widgets", default_branch="develop", private=True)


@pytest.fixture
def client(db, tokens, group_map, objects):
    return make_client(db, tokens, group_map, objects, forge_tokens=TenantTokens(),
                       transport=GitHubRepos(repos={"saga-xyz/widgets": WIDGETS}))


def _registered(client, db) -> str:
    answer = client.post("/v1/repositories", json={"repository": "saga-xyz/widgets"},
                         headers=auth_header("alice"))
    assert answer.status_code in (200, 201), answer.text
    return answer.json()["repository"]["repo_id"]


def _relation(base: str, head: str, ahead: int) -> dict:
    return {"base": base, "head": head, "status": "ahead", "ahead_by": ahead,
            "files": [f"src/f{i}.py" for i in range(20)], "read_at": "2026-10-09T16:30:00+00:00"}


# --------------------------------------------------------------------------
# RSA-3
# --------------------------------------------------------------------------

def test_an_index_at_the_head_is_zero_behind_whatever_compare_is_stored():
    """MUTATION: return `dict(index)` from `settle_relation` and this serves
    `behind_by: 2` and the older pair's 20 files, as the live API did."""
    stored = {"current_sha": B, "head_sha": B, "behind_by": 2,
              "head_relation": _relation(A, B, 2)}
    served = repositories.to_api({"index": stored})["index"]
    assert served["behind_by"] == 0
    assert served["head_relation"] is None


def test_a_compare_of_another_pair_is_not_this_pairs_count():
    stored = {"current_sha": B, "head_sha": C, "behind_by": 2,
              "head_relation": _relation(A, C, 2)}
    served = repositories.settle_relation(stored)
    assert served["behind_by"] is None and served["head_relation"] is None


def test_a_compare_of_exactly_this_pair_is_kept():
    stored = {"current_sha": A, "head_sha": C, "behind_by": None,
              "head_relation": _relation(A, C, 3)}
    served = repositories.settle_relation(stored)
    assert served["behind_by"] == 3
    assert served["head_relation"]["files"][0] == "src/f0.py"


def test_the_list_and_the_get_serve_the_settled_count(client, db):
    repo_id = _registered(client, db)
    doc = db.docs[f"repositories/{repo_id}"]
    doc["index"] = {**doc["index"], "current_sha": B, "head_sha": B, "behind_by": 2,
                    "head_relation": _relation(A, B, 2)}
    listed = client.get("/v1/repositories", headers=auth_header("alice")).json()
    one = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice")).json()
    for served in (listed["repositories"][0]["index"], one["repository"]["index"]):
        assert served["behind_by"] == 0
        assert served["head_relation"] is None


# --------------------------------------------------------------------------
# RSA-4
# --------------------------------------------------------------------------

def test_a_cut_test_map_says_its_edge_count_is_capped():
    """MUTATION: drop `capped_counts` from `coverage` and nothing tells a
    reader that 1,000 edges is the cut."""
    document = {"test_map": [{"source": f"src/m{i}.py"} for i in range(1000)],
                "truncated": ["test_map", "hot_spots"], "modules": []}
    block = repoindex.coverage(document)
    assert block["test_map_edges"] == 1000
    assert block["test_map_edges_capped"] is True
    assert block["truncated"] == ["hot_spots", "test_map"]


def test_an_uncut_test_map_is_a_total():
    block = repoindex.coverage({"test_map": [{"source": "a.py"}], "modules": []})
    assert block["test_map_edges_capped"] is False and block["truncated"] == []


def test_a_block_stored_before_the_flag_is_completed_from_the_version(client, db):
    """The live SwarmCloud registration: coverage stored without the flag,
    the promoted version saying `truncated: [hot_spots, test_map]`."""
    repo_id = _registered(client, db)
    doc = db.docs[f"repositories/{repo_id}"]
    doc["index"] = {**doc["index"], "current_sha": A, "head_sha": A,
                    "coverage": {"basis": "legacy", "modules": 2, "modules_with_tests": 1,
                                 "test_map_edges": 1000, "always_tests": 0}}
    db.docs[f"repositories/{repo_id}/index_versions/{A}"] = {
        "commit_sha": A, "repo_id": repo_id, "truncated": ["hot_spots", "test_map"]}
    served = client.get("/v1/repositories", headers=auth_header("alice")).json()
    coverage = served["repositories"][0]["index"]["coverage"]
    assert coverage["test_map_edges"] == 1000
    assert coverage["test_map_edges_capped"] is True
    # Not recorded for that index: unknown, never "none degraded".
    assert coverage["degraded_languages"] is None


def test_a_block_whose_version_is_gone_is_unknown_not_uncapped(client, db):
    repo_id = _registered(client, db)
    doc = db.docs[f"repositories/{repo_id}"]
    doc["index"] = {**doc["index"], "current_sha": A, "head_sha": A,
                    "coverage": {"basis": "legacy", "test_map_edges": 1000}}
    one = client.get(f"/v1/repositories/{repo_id}", headers=auth_header("alice")).json()
    assert one["repository"]["index"]["coverage"]["test_map_edges_capped"] is None


# --------------------------------------------------------------------------
# RSA-6
# --------------------------------------------------------------------------

LANGUAGES = [
    {"language": "python", "files": 1046, "status": "timed_out", "server": "pyright",
     "parsed": 1046, "timed_out": 0, "failed": 0, "too_large": 0, "over_budget": 0},
    {"language": "javascript", "files": 40, "status": "timed_out",
     "reason": "the server ran past its 300-second budget",
     "parsed": 40, "timed_out": 0, "failed": 0, "too_large": 0, "over_budget": 0},
    {"language": "hcl", "files": 30, "status": "unsupported"},
    {"language": "typescript", "files": 300, "status": "ok"},
]


def test_coverage_lists_the_languages_whose_server_timed_out_most_files_first():
    """MUTATION: drop `degraded_languages` from `coverage` and nothing but
    Settings says that Python's index is tree-sitter only."""
    block = repoindex.coverage({"modules": [], "languages": LANGUAGES})
    assert [(r["language"], r["status"]) for r in block["degraded_languages"]] == [
        ("python", "timed_out"), ("javascript", "timed_out")]


def test_the_graphs_language_table_wins_over_the_documents():
    manifest = {"languages": [{"language": "go", "files": 5, "status": "failing"}]}
    block = repoindex.coverage({"modules": [], "languages": LANGUAGES}, manifest)
    assert [r["language"] for r in block["degraded_languages"]] == ["go"]


def test_the_languages_table_says_whose_status_and_whose_counters():
    rows = {r["language"]: r for r in impact.languages_table(None, {"languages": LANGUAGES})}
    python = rows["python"]
    assert python["status"] == "timed_out" and python["status_of"] == "language_server"
    assert python["parse"]["timed_out"] == 0
    # A server status with no reason of its own is given one.
    assert "language server ran past its budget" in python["reason"]
    # A reason the extractor gave is kept.
    assert rows["javascript"]["reason"] == "the server ran past its 300-second budget"


# --------------------------------------------------------------------------
# #1693: a language-server pass cut by its budget keeps the files it finished
# --------------------------------------------------------------------------

PARTIAL = {"language": "python", "files": 1447, "status": "partial", "server": "pyright",
           "files_covered": 702,
           "reason": "pyright reached 702 of 1447 files (40210 of 86805 sites) in its "
                     "270-second budget"}


def test_a_partial_language_is_degraded_and_says_how_much_it_covered():
    """MUTATION: leave `partial` out of DEGRADED_LANGUAGE_STATUSES and the
    list card and the Overview say nothing about 745 uncovered files."""
    others = [row for row in LANGUAGES if row["language"] != "python"]
    block = repoindex.coverage({"modules": [], "languages": [PARTIAL, *others]})
    rows = {r["language"]: r for r in block["degraded_languages"]}
    assert rows["python"]["status"] == "partial"
    # MUTATION: drop `files_covered` from `degraded_languages()`'s rows and
    # this is a KeyError.
    assert rows["python"]["files_covered"] == 702
    assert rows["javascript"]["files_covered"] is None


def test_the_languages_table_serves_a_partial_rows_coverage():
    """MUTATION: leave `files_covered` out of `languages_table`'s kept keys
    and the banner cannot say "covered 702 of 1,447 files"."""
    rows = {r["language"]: r for r in impact.languages_table(None, {"languages": [PARTIAL]})}
    assert (rows["python"]["status"], rows["python"]["files_covered"]) == ("partial", 702)
    assert rows["python"]["reason"] == PARTIAL["reason"]


def test_an_index_document_may_mark_a_language_partial():
    """The agent copies the extractor's rows into repo-index.json.

    MUTATION: leave `partial` out of `LanguageRow.status`'s Literal (or
    `files_covered` out of its fields) and every run whose server was cut is
    refused at promotion."""
    row = {"language": "python", "files": 1447, "grammar": "tree-sitter-python 0.23.6",
           "server": "pyright", "status": "partial", "files_covered": 702, "fallback": "ast"}
    parsed = repoindex.parse_index(json.dumps(fixture_index("a" * 40, languages=[row])))
    assert parsed["languages"][0]["status"] == "partial"
    assert parsed["languages"][0]["files_covered"] == 702
