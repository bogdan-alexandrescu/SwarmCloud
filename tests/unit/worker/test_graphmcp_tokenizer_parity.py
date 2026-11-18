"""swarm-graph's copy of KG2's tokenizer, BM25 and shard naming is KG2's, exactly.

`repo_graph_shards.tokenize` is the one implementation the term postings are
built with, and the server must query with the same split: a word split
differently finds nothing and says nothing. The worker cannot import it (it
ships in the indexer image, not `agent-runtime-base`), so
`agent_worker.graphmcp.snapshot` carries a copy, and this file holds the copy
equal: every constant, the split of every word in this repository's worker and
console sources, the ranking over a real term index, and the bucket and
module a reader looks a row up by.
"""

from __future__ import annotations

from pathlib import Path

import graphmcp_fixtures as gf
import pytest
import repo_index_fixtures as fx

from agent_worker.graphmcp import snapshot as ours

CORPUS_GLOBS = ("apps/agent-worker/agent_worker/**/*.py", "apps/swarm-api/swarm_api/*.py",
                "apps/swarm-ui/src/*.tsx", "terraform/modules/*/*.tf")


@pytest.fixture(scope="module")
def theirs():
    return gf.load_tool("repo_graph_shards")


def test_every_constant_is_kg2s(theirs):
    assert ours.TOKENIZER_VERSION == theirs.TOKENIZER_VERSION
    assert ours.STOPWORDS == theirs.STOPWORDS
    assert (ours.BM25_K1, ours.BM25_B) == (theirs.BM25_K1, theirs.BM25_B)
    assert ours.TERM_BUCKET_CHARS == theirs.TERM_BUCKET_CHARS
    assert ours.WHOLE == theirs.WHOLE
    assert ours.MANIFEST_SCHEMA == theirs.MANIFEST_SCHEMA
    assert (ours.COMPRESSION, ours.BLOB_SUFFIX) == (theirs.COMPRESSION, theirs.BLOB_SUFFIX)
    assert ours.LAYERS == theirs.LAYERS
    assert ours.INDEX_LAYERS == tuple(theirs.INDEX_LAYERS)
    assert ours._WORD.pattern == theirs._WORD.pattern
    assert ours._CAMEL.pattern == theirs._CAMEL.pattern


def test_every_word_of_the_repositorys_sources_splits_the_same(theirs):
    files = sorted({p for pattern in CORPUS_GLOBS for p in gf.REPO_ROOT.glob(pattern)})
    assert len(files) > 100, len(files)
    words = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert ours.tokenize(text) == theirs.tokenize(text), path
        words += len(ours.tokenize(text))
    assert words > 100_000, words
    for odd in ("", "HTTPServer", "sha256Digest", "v2_API", "runs", "class", "__init__",
                "planner's prompt", "x", "ÜberCase"):
        assert ours.tokenize(odd) == theirs.tokenize(odd), odd


def test_bm25_ranks_a_real_term_index_the_same(theirs, tmp_path: Path):
    repo = fx.build_repo(tmp_path / "repo", {**fx.PYTHON_APP, **fx.TS_APP})
    document = gf.graph_of(repo)
    assert document["terms"], "the extract built no term index"
    for query in ("load user", "list users", "format name", "health", "store fetch save"):
        assert ours.bm25_search(document["terms"], document["term_stats"], query, 50) == \
            theirs.bm25_search(document["terms"], document["term_stats"], query, 50), query


def test_a_reader_looks_rows_up_where_the_writer_put_them(theirs):
    for term in ("user", "load", "fencing", "x9"):
        assert ours.term_bucket(term) == theirs.term_bucket(term)
    for symbol in ("src/pkg/users.py#load_user", "main.py#run", "a/b/c.ts#X.y", "README.md"):
        assert ours.module_of(symbol) == theirs.module_of(symbol)
