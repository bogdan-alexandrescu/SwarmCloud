"""Differential tests: an incremental index equals a full one of the same commit.

The owner's agent quality program (BGD, 2026-11-03) proves the landed
incremental carry before relying on it: a graph cut only in its test maps is
carried, and an incremental run's facts are the facts a full extraction of
the same commit gives. Each case here indexes a fixture repository in full,
stores the graph through the shard writer and reads it back as the base, as
the worker stages it (lane IX2), changes the repository, and compares the
incremental run with a fresh full one, key by key:

* source edits, renames, deletions and additions are carried, and the
  facts are equal;
* a chain of incremental runs, each built on the last one's stored graph,
  stays equal to a full run at every step (drift would accumulate here);
* a build-configuration or dependency change rebuilds in full and says why;
* a base whose only cut is its test maps is carried, and the head's maps are
  rebuilt whole, not narrowed to the base's cut;
* the extractor's `work` counts say what it reused and recomputed, and reach
  swarm-api's run metrics through the manifest.

`communities` is left out of the comparison on purpose: an incremental run
seeds Louvain from the base's partition to keep ids stable (lane KG2), so it
is held to stability (test_repo_index_communities.py), not equality.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import repo_index_fixtures as fx

FAKE_SERVER = Path(__file__).resolve().parent / "fake_lsp_server.py"

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_DIR = REPO_ROOT / "images" / "agent-runtime-indexer" / "repo-index"

TENANT = "eng"
REPO_ID = "repo_00112233445566ff"
DAY = 86_400
START = 1_780_000_000

#: Every fact an incremental run must agree with a full run on.
FACT_KEYS = ("symbols", "call_edges", "symbol_test_map", "files", "routes", "test_map",
             "test_coverage", "languages", "truncated", "terms", "term_stats", "signatures",
             "flows")

FILES: dict[str, str] = {
    **fx.PYTHON_APP,
    **fx.PYTHON_AMBIGUOUS,
    "pyproject.toml": "[project]\nname = \"fixture\"\n",
}


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"nothing at {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> Any:
    return _load("repo_index_extract", TOOL_DIR / "repo_index_extract.py")


@pytest.fixture(scope="module")
def shards() -> Any:
    return _load("repo_graph_shards", TOOL_DIR / "repo_graph_shards.py")


@pytest.fixture(autouse=True)
def _no_ambient_configuration(shards, tmp_path, monkeypatch):
    # The machine running the tests may be a worker whose PID 1 names a tenant.
    monkeypatch.setattr(shards, "CONTAINER_ENVIRON", tmp_path / "no-such-environ")
    for name in ("TENANT_ID", "ARTIFACT_BUCKET"):
        monkeypatch.delenv(name, raising=False)


class Repo:
    """A fixture repository, its bucket, and the last promoted base."""

    def __init__(self, tool: Any, shards: Any, root: Path, files: dict[str, str]) -> None:
        self.tool, self.shards = tool, shards
        self.root = fx.build_repo(root / "repo", files)
        self.store = shards.LocalStore(root / "bucket")
        self.when = START
        self.base: Any = None
        self.digests: dict[str, str] = {}
        self.promote(tool.extract(self.root, tool.Budget()))

    def _env(self) -> dict[str, str]:
        return fx.git_env(self.root.parent / (self.root.name + "-home"))

    def commit(self, changes: dict[str, str | None]) -> str:
        """One commit: a text writes a file, None deletes it. Returns the head."""
        env = self._env()
        for rel, text in changes.items():
            if text is None:
                subprocess.run(["git", "-C", str(self.root), "rm", "-q", rel], env=env,
                               check=True)
            else:
                fx.write_files(self.root, {rel: text})
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], env=env, check=True)
        self.when += DAY
        stamp = f"@{self.when} +0000"
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "change"],
                       env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
                       check=True)
        return subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def promote(self, facts: dict) -> dict:
        """Store `facts`' graph, read it back and make it the next run's base."""
        written = self.shards.write_graph(
            self.tool.graph_document(facts), self.store, tenant_id=TENANT, repo_id=REPO_ID,
            base_commit=facts["base_sha"])
        self.digests[facts["commit_sha"]] = written["manifest_digest"]
        graph = self.shards.read_graph(self.store, tenant_id=TENANT, repo_id=REPO_ID,
                                       commit_sha=facts["commit_sha"],
                                       manifest_digest=written["manifest_digest"])
        self.base = self.tool.Base(sha=facts["commit_sha"], graph=graph,
                                   index=self.tool.index_document(facts))
        return self.manifest(facts["commit_sha"])

    def manifest(self, commit: str) -> dict:
        return json.loads(self.store.get(
            f"tenants/{TENANT}/repos/{REPO_ID}/graph/{commit}/manifest.json"))

    def both(self) -> tuple[dict, dict]:
        """(incremental on the base, full) of the head."""
        return (self.tool.extract(self.root, self.tool.Budget(), base=self.base),
                self.tool.extract(self.root, self.tool.Budget()))


def _differs(incremental: dict, full: dict) -> list[str]:
    return [key for key in FACT_KEYS if incremental.get(key) != full.get(key)]


def _modules_without_reading(facts: dict) -> list[dict]:
    """The modules' mechanical fields: an incremental run also carries the
    agent's reading and the commit it was read at, which a full run has not."""
    return [{k: v for k, v in m.items() if k not in ("commit_sha", "purpose")}
            for m in facts["modules"]]


def _assert_equal(incremental: dict, full: dict) -> None:
    assert incremental["kind"] == "incremental", incremental["extractor"].get("incremental")
    assert incremental["commit_sha"] == full["commit_sha"]
    # MUTATION: carrying a changed file's base edges, or dropping an untouched
    # file's, and one of these differs.
    assert _differs(incremental, full) == []
    assert _modules_without_reading(incremental) == _modules_without_reading(full)


USERS = fx.PYTHON_APP["src/pkg/users.py"]

CASES = {
    "a source edit": {
        "src/pkg/web.py": fx.PYTHON_APP["src/pkg/web.py"]
        + "\n\n@app.route(\"/ready\")\ndef ready():\n    return ping()\n",
    },
    "an edit that changes who a caller resolves to": {
        "lib/use.py": "from lib.c import only_here\n\n\ndef go():\n    return only_here()\n",
    },
    "a rename": {
        "src/pkg/store.py": None,
        "src/pkg/storage.py": fx.PYTHON_APP["src/pkg/store.py"],
        "src/pkg/users.py": USERS.replace("from .store import", "from .storage import"),
    },
    "a deletion": {"src/pkg/store.py": None},
    # users.py is untouched and its base edge to `fetch` must not be carried.
    "a callee renamed under an untouched caller": {
        "src/pkg/store.py": fx.PYTHON_APP["src/pkg/store.py"].replace("def fetch", "def lookup"),
    },
    "an addition": {
        "src/pkg/audit.py": "from .store import save\n\n\ndef record(row):\n    return save(row)\n",
        "tests/test_audit.py": "from pkg.audit import record\n\n\ndef test_record():\n"
                               "    assert record(1)\n",
    },
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_an_incremental_run_equals_a_full_run_of_the_same_commit(tool, shards, tmp_path, case):
    repo = Repo(tool, shards, tmp_path, FILES)
    repo.commit(CASES[case])

    incremental, full = repo.both()

    _assert_equal(incremental, full)
    changed = set(incremental["changes"]["added"]) | set(incremental["changes"]["modified"])
    assert changed | set(incremental["changes"]["deleted"]) == {
        path for path in CASES[case]}, "the run names exactly the files that changed"


#: The chain applies every case in turn, so each must still apply to the
#: tree the ones before it left.
CHAIN = ("a source edit", "an addition", "an edit that changes who a caller resolves to",
         "a callee renamed under an untouched caller")


def test_a_chain_of_incremental_runs_never_drifts_from_a_full_run(tool, shards, tmp_path):
    """Each run is built on the previous run's stored graph, as production does."""
    repo = Repo(tool, shards, tmp_path, FILES)
    for case in CHAIN + ("a deletion",):
        repo.commit(CASES[case])
        incremental, full = repo.both()
        _assert_equal(incremental, full)
        repo.promote(incremental)


def _fake_server(tool: Any, tmp_path: Path, name: str) -> Any:
    """lsp.LspOptions for fake_lsp_server.py answering users.py's `fetch` site."""
    lsp = sys.modules["lsp"]
    scenario = {"definitions": {
        "src/pkg/users.py:8": {"path": "src/pkg/store.py", "line": 0, "character": 4,
                               "name": "fetch"},
    }, "log": str(tmp_path / f"{name}.log")}
    path = tmp_path / f"{name}-scenario.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    spec = lsp.ServerSpec(name="fake", command=(sys.executable, str(FAKE_SERVER), str(path)),
                          languages=("python",), language_ids={".py": "python"},
                          call_hierarchy=True, references=False, reference_kinds=(),
                          settings={})
    return lsp.LspOptions(servers={"fake": spec}, request_timeout_seconds=5.0,
                          memory_limit_mib=2048)


def _lsp_facts(facts: dict) -> dict:
    """What a server's answers decide: the edges and which files it covered.
    The language rows' request counts are the pass's own and differ by design."""
    return {"call_edges": facts["call_edges"],
            "covered": sorted(row["path"] for row in facts["files"] if row.get("lsp"))}


@pytest.mark.parametrize("case", ["an unrelated edit", "a callee renamed under an untouched caller"])
def test_carried_language_server_answers_equal_a_full_runs(tool, shards, tmp_path, case):
    """With a server: an untouched file's `lsp` edges are carried, never re-asked,
    and still equal what a full run with the same server finds."""
    changes = (CASES[case] if case in CASES
               else {"lib/c.py": "def only_here():\n    return 2\n"})
    repo = Repo(tool, shards, tmp_path, FILES)
    base_facts = tool.extract(repo.root, tool.Budget(), lsp=_fake_server(tool, tmp_path, "base"))
    edge = [e for e in base_facts["call_edges"] if e["from"] == "src/pkg/users.py#load_user"
            and e["to"] == "src/pkg/store.py#fetch"]
    assert edge and edge[0]["evidence"] == "lsp", "the fixture's server answers the site"
    repo.promote(base_facts)
    repo.commit(changes)

    incremental = tool.extract(repo.root, tool.Budget(), base=repo.base,
                               lsp=_fake_server(tool, tmp_path, "incremental"))
    full = tool.extract(repo.root, tool.Budget(), lsp=_fake_server(tool, tmp_path, "full"))

    assert incremental["kind"] == "incremental"
    # MUTATION: carrying users.py's answer about a `fetch` that no longer exists.
    assert _lsp_facts(incremental) == _lsp_facts(full)
    work = incremental["extractor"]["work"]
    assert work["lsp_asked"] is not None and work["lsp_carried"] is not None
    assert work["lsp_asked"] + work["lsp_carried"] >= len(_lsp_facts(full)["covered"])


@pytest.mark.parametrize("path,text", [
    ("pyproject.toml", "[project]\nname = \"fixture\"\nversion = \"2\"\n"),
    ("requirements.txt", "fastapi==0.110.0\n"),
    ("uv.lock", "version = 1\n"),
    ("tests/conftest.py", "import pytest\n"),
])
def test_a_configuration_or_dependency_change_rebuilds_in_full_and_says_why(
    tool, shards, tmp_path, path, text
):
    from swarm_api import repoindex

    repo = Repo(tool, shards, tmp_path, FILES)
    repo.commit({path: text, "lib/c.py": "def only_here():\n    return 2\n"})

    incremental, full = repo.both()

    assert incremental["kind"] == "full" and "changes" not in incremental
    reason = incremental["extractor"]["incremental"]["reason"]
    assert "configuration changed" in reason and path in reason
    assert _differs(incremental, full) == []
    # The reason reaches the run's metrics through the manifest.
    manifest = repo.promote(incremental)
    run = {"kind": "incremental", "kind_reason": None}
    assert path in repoindex.rebuild_reason(run, "full", manifest)
    assert repoindex.files_work(manifest)["reused"] == 0


def test_a_base_cut_only_in_its_test_maps_is_carried_and_the_maps_rebuilt_whole(
    tool, shards, tmp_path, monkeypatch
):
    caps = tool.MAX_SYMBOL_TEST_MAP, tool.MAX_FILE_TEST_MAP
    monkeypatch.setattr(tool, "MAX_SYMBOL_TEST_MAP", 1)
    monkeypatch.setattr(tool, "MAX_FILE_TEST_MAP", 1)
    repo = Repo(tool, shards, tmp_path, FILES)
    assert {"symbol_test_map", "file_test_map"} <= set(repo.base.graph["truncated"])
    monkeypatch.setattr(tool, "MAX_SYMBOL_TEST_MAP", caps[0])
    monkeypatch.setattr(tool, "MAX_FILE_TEST_MAP", caps[1])
    repo.commit(CASES["a source edit"])

    incremental, full = repo.both()

    _assert_equal(incremental, full)
    # MUTATION: reading the base's cut maps forward, and the head's map is narrower.
    assert len(incremental["test_map"]) > 1 and len(incremental["symbol_test_map"]) > 1
    assert "symbol_test_map" not in incremental["truncated"]


def test_the_work_counts_say_what_was_reused_and_recomputed(tool, shards, tmp_path):
    from swarm_api import repoindex

    repo = Repo(tool, shards, tmp_path, FILES)
    first = repo.manifest(repo.base.sha)
    parsed = sum(1 for row in tool.extract(repo.root, tool.Budget())["files"]
                 if row["status"] == "parsed")
    # A full run recomputes every parsed file and reuses none; no LSP pass ran.
    assert repoindex.files_work(first) == {"parsed": parsed, "recomputed": parsed,
                                           "reused": 0, "lsp_asked": None, "lsp_carried": None}
    repo.commit({"lib/c.py": "def only_here():\n    return 2\n"})

    incremental, _full = repo.both()

    affected = set(incremental["changes"]["affected"])
    work = incremental["extractor"]["work"]
    # MUTATION: counting the changed files alone, not their callers too.
    assert work["recomputed"] == len(affected) >= 2
    assert work["reused"] == parsed - len(affected) > 0
    assert repoindex.files_work(repo.promote(incremental)) == work


def test_the_work_counts_keep_the_output_byte_identical(tool, shards, tmp_path):
    """Counts, never a duration: the same input still gives the same bytes (§2.3)."""
    repo = Repo(tool, shards, tmp_path, FILES)
    repo.commit({"lib/c.py": "def only_here():\n    return 2\n"})
    one = tool.dumps(tool.graph_document(tool.extract(repo.root, tool.Budget(), base=repo.base)))
    two = tool.dumps(tool.graph_document(tool.extract(repo.root, tool.Budget(), base=repo.base)))
    assert one == two


def test_the_sweep_keeps_every_version_swarm_api_keeps_readable(tool, shards, tmp_path):
    """Retention end to end: swarm-api's keep set (the newest versions, the
    last day's, the promoted one and the run's own) given to the bucket's
    sweep. An incremental graph names its base's blobs, so retiring an old
    base must never take a blob a kept version still reads."""
    from swarm_api import repoindex

    repo = Repo(tool, shards, tmp_path, FILES)
    commits = [repo.base.sha]
    for case in CHAIN:
        repo.commit(CASES[case])
        incremental, _full = repo.both()
        assert incremental["kind"] == "incremental"
        repo.promote(incremental)
        commits.append(incremental["commit_sha"])
    now = datetime.now(timezone.utc) + 2 * shards.SWEEP_GRACE
    # Recorded: the first two a week ago, the rest within the last day.
    recorded = [now - timedelta(days=7, hours=2), now - timedelta(days=7, hours=1),
                *(now - timedelta(hours=len(commits) - n) for n in range(2, len(commits)))]
    rows = [{"commit_sha": c, "recorded_at": at} for c, at in zip(commits, recorded)]
    keep = repoindex.kept_commits(rows, keep_versions=1, current_sha=commits[-1],
                                  own=commits[-1], now=now)
    # The week-old two are past the newest one and the last day's: retired.
    assert set(keep) == set(commits[2:])

    report = shards.sweep(repo.store, tenant_id=TENANT, repo_id=REPO_ID, now=now, keep=keep)

    assert not report.get("refused"), report
    for commit in commits[2:]:
        graph = shards.read_graph(repo.store, tenant_id=TENANT, repo_id=REPO_ID,
                                  commit_sha=commit, manifest_digest=repo.digests[commit])
        assert graph["commit_sha"] == commit
    for commit in commits[:2]:
        with pytest.raises(Exception):
            repo.manifest(commit)
    # MUTATION: a sweep that counts only the retired manifests' blobs as
    # free deletes blobs the kept incremental graphs carry, and a read above fails.
    assert report["deleted_copies"] >= 2
