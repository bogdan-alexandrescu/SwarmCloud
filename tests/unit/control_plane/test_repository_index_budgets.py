"""An index run's time is sized to the repository (docs/repo-index.md §3.5; #1693).

Until lane INDEX-BUDGETS every full run got 1,800 s whatever the repository's
size (`FULL_TIMEOUT_SECONDS`), the worker gave the language-server pass 540 s
of it and the driver split that equally, so pyright had 270 s on ai-studio
(1,447 Python files, >= 86,805 call sites) against the ~680-890 s it needed,
and timed out in 16 of 16 sampled full runs. What is held here:

  * `index_timeout_seconds` follows §3.5's table by the promoted index's
    source-file count, and an unknown count keeps the old timeouts;
  * promotion records that count, and -- from a full run only -- what each
    language server cost (`lsp.cost_seconds` on the manifest's language rows),
    so the next run is sized and its servers granted from them;
  * `indexer_task` uses both: the timeout, and a signed `LSP_COST_LINE` the
    worker passes to the extractor;
  * the extractor version is 4, so the first run after the release is full.
"""

from __future__ import annotations

import pytest

from swarm_api import repoindex

from .conftest import auth_header
from .repo_fakes import TenantTokens, make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE = sha("one")


def _record(**index: object) -> dict:
    return {"repo_id": "repo_0123456789abcdef", "tenant_id": "acme", "owner": "acme",
            "repo": "app", "default_branch": "main",
            "repository_url": "https://github.com/acme/app", "index": dict(index)}


# --------------------------------------------------------------------------
# the size table
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("files", "full", "incremental"), [
    (0, 1_800, 900),
    (1_999, 1_800, 900),
    (2_000, 3_600, 1_200),
    (3_795, 3_600, 1_200),  # ai-studio, 2026-10-20
    (10_000, 3_600, 1_200),
    (10_001, 7_200, 1_800),
    (250_000, 7_200, 1_800),
])
def test_the_index_timeout_follows_the_size_table(files, full, incremental):
    # MUTATION: return FULL_TIMEOUT_SECONDS / INCREMENTAL_TIMEOUT_SECONDS
    # whatever the count and every row from 2,000 files up is red.
    assert repoindex.index_timeout_seconds("full", files) == full
    assert repoindex.index_timeout_seconds("incremental", files) == incremental


@pytest.mark.parametrize("unknown", [None, -1, True, "3795", 3795.0])
def test_an_unknown_size_keeps_the_old_timeouts(unknown):
    # A registration indexed before the count was recorded, or a value that
    # is not a count, is sized as before: never a longer run on a guess.
    # MUTATION: accept any number as a count and 3795.0 reads 3,600.
    assert repoindex.index_timeout_seconds("full", unknown) == repoindex.FULL_TIMEOUT_SECONDS == 1_800
    assert repoindex.index_timeout_seconds("incremental", unknown) \
        == repoindex.INCREMENTAL_TIMEOUT_SECONDS == 900


def test_the_largest_row_is_the_indexer_profiles_maximum():
    # terraform/infra/locals.tf gives the indexer profile 7,200 s; a task may
    # only shorten its profile's timeout, never lengthen it.
    # MUTATION: raise the top row past 7,200 and this is red.
    assert repoindex.index_timeout_seconds("full", 10**9) == repoindex.MAX_INDEX_TIMEOUT_SECONDS == 7_200


# --------------------------------------------------------------------------
# what promotion records, read pure
# --------------------------------------------------------------------------

def test_source_files_count_the_languages_with_a_grammar():
    document = {"languages": [
        {"language": "python", "files": 1_447, "grammar": "tree-sitter-python 0.23"},
        {"language": "typescript", "files": 2_348, "grammar": "tree-sitter-typescript 0.23"},
        # A language the extractor cannot parse is never a server's file.
        {"language": "markdown", "files": 900, "grammar": None},
    ]}
    # MUTATION: count every row and this reads 4,695.
    assert repoindex.source_files_of(document) == 3_795
    assert repoindex.source_files_of({"languages": []}) is None
    assert repoindex.source_files_of({}) is None


def test_lsp_costs_are_read_from_a_full_runs_manifest_one_per_server():
    rows = [
        {"language": "python", "server": "pyright", "lsp": {"cost_seconds": 890}},
        {"language": "typescript", "server": "tsserver", "lsp": {"cost_seconds": 120}},
        # tsserver serves both: the same counts, once.
        {"language": "javascript", "server": "tsserver", "lsp": {"cost_seconds": 120}},
        # A server that measured nothing (failing, never started) has no cost.
        {"language": "go", "server": "gopls", "lsp": {"requests": 0}},
        {"language": "hcl", "server": None},
    ]
    # MUTATION: drop the `kind == "full"` check and the incremental manifest
    # below reads a cost an incremental run measured over a few files.
    assert repoindex.lsp_costs_of({"kind": "full", "languages": rows}) == {
        "pyright": 890, "tsserver": 120}
    assert repoindex.lsp_costs_of({"kind": "incremental", "languages": rows}) is None
    assert repoindex.lsp_costs_of({"kind": "full", "languages": rows[3:]}) is None
    assert repoindex.lsp_costs_of(None) is None


def test_a_cost_that_is_not_a_positive_count_or_a_server_name_is_dropped():
    rows = [
        {"server": "pyright", "lsp": {"cost_seconds": True}},
        {"server": "gopls", "lsp": {"cost_seconds": -5}},
        {"server": "bad name", "lsp": {"cost_seconds": 10}},
        {"server": "tsserver", "lsp": {"cost_seconds": 40}},
    ]
    # MUTATION: skip the name pattern and "bad name" reaches the signed
    # prompt, where the worker refuses the whole line.
    assert repoindex.lsp_costs_of({"kind": "full", "languages": rows}) == {"tsserver": 40}


# --------------------------------------------------------------------------
# the task
# --------------------------------------------------------------------------

def test_indexer_task_sizes_its_timeout_from_the_promoted_count():
    small = repoindex.indexer_task(_record(source_files=17), ONE)
    big = repoindex.indexer_task(_record(source_files=3_795), ONE)
    unknown = repoindex.indexer_task(_record(), ONE)
    incremental = repoindex.indexer_task(_record(source_files=3_795), ONE, "incremental",
                                         base_sha=sha("base"))
    # MUTATION: keep `FULL_TIMEOUT_SECONDS if kind == "full" else ...` in
    # indexer_task() and ai-studio's 3,795 files read 1,800 again.
    assert big.timeout_seconds == 3_600
    assert incremental.timeout_seconds == 1_200
    assert small.timeout_seconds == unknown.timeout_seconds == 1_800


def test_indexer_task_names_the_previous_costs_in_the_signed_prompt():
    task = repoindex.indexer_task(_record(lsp_costs={"tsserver": 120, "pyright": 890}), ONE)
    lines = [line for line in task.input["prompt"].splitlines()
             if line.startswith(repoindex.LSP_COST_LINE)]
    # MUTATION: drop the cost line from indexer_prompt() and the worker
    # grants every server the equal split forever.
    assert lines == [f"{repoindex.LSP_COST_LINE}pyright=890 tsserver=120"]
    bare = repoindex.indexer_task(_record(), ONE)
    assert repoindex.LSP_COST_LINE not in bare.input["prompt"]


def test_indexer_task_drops_a_stored_cost_it_would_not_promote():
    # The record is this service's own, but the line is a request the worker
    # parses: anything promotion would not have written is not passed on.
    task = repoindex.indexer_task(
        _record(lsp_costs={"pyright": 890, "x y": 3, "gopls": "fast"}), ONE)
    assert f"{repoindex.LSP_COST_LINE}pyright=890\n" in task.input["prompt"] + "\n"


def test_the_prompt_states_the_one_line_bound_for_every_free_text_field():
    # #2978: an agent wrote a note over 300 characters because the prompt
    # never said so, and the worker's indexspec check failed the whole run.
    # The figure is read from the model, so the prompt cannot drift from it.
    bound = next(m.max_length for m in repoindex.Note.model_fields["text"].metadata
                 if hasattr(m, "max_length"))
    purpose = next(m.max_length for m in repoindex.Module.model_fields["purpose"].metadata
                   if hasattr(m, "max_length"))
    assert purpose == bound == 300
    prompt = repoindex.indexer_prompt(REPOSITORY, ONE, "main", tenant_id="eng",
                                      repo_id="repo_" + "0" * 16)
    line = f"<one line, at most {bound} characters>"
    # MUTATION: write "<one line>" back into _INDEX_SHAPE and the agent is
    # never told a note's text or a module's purpose has a length limit.
    assert f'"text": "{line}"' in prompt
    assert f'"purpose": "{line}"' in prompt
    bounds = next(p for p in prompt.split("\n\n") if "Bounds: " in p)
    bounds = bounds[bounds.index("Bounds: "):]
    sentence = next(s for s in bounds.split(". ") if f"{bound} characters" in s)
    # MUTATION: drop a field from the Bounds sentence and the agent may write
    # that one past the limit.
    for field in ("purpose", "text", "because", "rule", "started_by", "fallback",
                  "reason"):
        assert field in sentence, field


def test_the_extractor_version_is_bumped_so_the_first_run_after_release_is_full():
    # Version 4 records each server's grant and cost; a version-3 base graph
    # is refused by `graph_carry_refusal`, so the first run is full and
    # measures every server.
    # MUTATION: leave INDEXER_EXTRACTOR_VERSION at "3" and this is red.
    # (At least 4: lane INDEX-LANGS raised it to 5 for terraform-ls.)
    assert int(repoindex.INDEXER_EXTRACTOR_VERSION) >= 4
    refusal = repoindex.graph_carry_refusal(
        {"version": "3", "blob_ids": True, "files_not_listed": 0, "truncated": []})
    assert refusal is not None


# --------------------------------------------------------------------------
# promotion, through the API
# --------------------------------------------------------------------------

@pytest.fixture
def github():
    return IndexGitHub(heads={"main": ONE})


@pytest.fixture
def client(db, tokens, group_map, objects, github):
    return make_client(
        db, tokens, group_map, objects, forge_tokens=TenantTokens(), transport=github
    )


def test_promotion_records_the_source_file_count_and_the_next_run_uses_it(
    client, db, objects, github
):
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    first = started.json()["run"]["task_id"]
    assert db.docs[f"tasks/{first}"]["timeout_seconds"] == 1_800
    finish_index_task(db, objects, first, fixture_index(ONE, languages=[
        {"language": "python", "files": 1_447, "grammar": "tree-sitter-python",
         "server": "pyright", "status": "partial", "files_covered": 300, "fallback": "ast"},
        {"language": "typescript", "files": 2_348, "grammar": "tree-sitter-typescript",
         "server": "tsserver", "status": "ok", "files_covered": 2_348, "fallback": None},
    ]))
    assert client.get(f"/v1/repositories/{repo_id}/index",
                      headers=auth_header("alice")).status_code == 200
    index = db.docs[f"repositories/{repo_id}"]["index"]
    assert index["current_sha"] == ONE
    # MUTATION: drop `source_files=` from the promotion's index.update() and
    # this is a KeyError.
    assert index["source_files"] == 3_795

    two = sha("two")
    github.heads["main"] = two
    again = client.post(f"/v1/repositories/{repo_id}/index", json={"kind": "full"},
                        headers=auth_header("alice"))
    assert again.status_code == 202, again.text
    task = db.docs[f"tasks/{again.json()['run']['task_id']}"]
    assert task["timeout_seconds"] == 3_600
