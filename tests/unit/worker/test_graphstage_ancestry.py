"""A depth-1 lane clone proves its index commit an ancestor after one bounded fetch (lane GRAPH-ANCESTRY, part of #2540).

Measured 2026-10-20/27: 1,053 of eng's 1,799 graph-asking steps staged no
graph because "the ancestry of the index commit ... and the base ... cannot be
shown from this clone or the recorded compare". Two causes, both in the code:

* every lane clone is `--depth 1` (`gitops.shallow_clone`), so the index
  commit is never in it and `git merge-base --is-ancestor` cannot answer;
* swarm-api records the compare of ONE pair, the promoted index against the
  head its last poll read (`repoindex.refresh_relation`), so a lane whose
  base is any other main commit never matches it.

The rule stays: only a proof stages. What changes is that a shallow clone is
deepened behind its base (`gitops.fetch_base_history`, bounded in depth and
time) and git is asked again. What this file holds:

* a shallow clone missing the index commit is proven an ancestor after the
  fetch, and a true non-ancestor is still refused;
* a recorded compare for another head no longer blocks a provable step;
* a fetch that times out leaves a reason and no graph, and the attempt goes on;
* the fetch runs once per staging, never for a full clone, and a token never
  reaches a credential helper the clone's own config names.

Offline: local git repositories over `file://`, the worker's FakeFirestore and
a LocalObjectStore. Each assertion names the MUTATION it catches.
"""

from __future__ import annotations

import json
import secrets
import stat
import subprocess
from pathlib import Path
from typing import Any

from agent_worker import gitops, graphstage

from test_graphstage import TENANT, URL, _no_ab, _promote

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env={**_ENV, "HOME": str(repo)})
    return done.stdout.decode().strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name} = 1\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


class _Lane:
    """An origin with six commits on main and one off it, and a depth-1 clone
    of main's tip, as `gitops.shallow_clone` makes one."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin"
        self.origin.mkdir()
        _git(self.origin, "init", "-q", "-b", "main")
        self.main = [_commit(self.origin, f"m{i}.py") for i in range(6)]
        _git(self.origin, "checkout", "-q", "-b", "rewritten", self.main[2])
        self.off_main = _commit(self.origin, "x.py")
        _git(self.origin, "checkout", "-q", "main")
        self.url = f"file://{self.origin}"
        self.checkout = tmp_path / "work" / "repo"
        self.checkout.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-tags", "--single-branch",
                        "--", self.url, str(self.checkout)],
                       check=True, capture_output=True, env={**_ENV, "HOME": str(tmp_path)})
        self.base = self.main[-1]
        self.private = tmp_path / "private"

    def fetch(self, checkout: Path, base_sha: str, **kwargs: Any) -> str | None:
        return gitops.fetch_base_history(checkout, url=self.url, base_sha=base_sha,
                                         private_dir=self.private, **kwargs)


def _stage(db: Any, store: Any, lane: _Lane, fetch: Any) -> graphstage.StageResult:
    return graphstage.stage(db=db, store=store, tenant_id=TENANT, repository_url=URL,
                            task_id="task_b", checkout=lane.checkout, base_sha=lane.base,
                            context_dir=lane.checkout.parent / graphstage.CONTEXT_DIR_NAME,
                            fetch_history=fetch)


# --------------------------------------------------------------------------
# the proof
# --------------------------------------------------------------------------


def test_a_shallow_clone_proves_its_index_an_ancestor_after_the_bounded_fetch(tmp_path):
    lane = _Lane(tmp_path)
    index = lane.main[3]
    # The premise: depth 1, so git cannot tell, and nothing was recorded.
    assert graphstage.is_shallow(lane.checkout)
    assert graphstage.is_ancestor(lane.checkout, index, lane.base) is None
    assert isinstance(graphstage.prove_ancestry(lane.checkout, index, lane.base, {}), str)

    proof = graphstage.prove_ancestry(
        lane.checkout, index, lane.base, {},
        deepen=lambda: lane.fetch(lane.checkout, lane.base))

    # MUTATION: `prove_ancestry` returning `unshown` without calling `deepen`,
    # or not asking git again after it, leaves a str here.
    assert isinstance(proof, graphstage.Ancestry), proof
    # MUTATION: `_git_ancestry` dropped for a bare Ancestry(basis="git") loses
    # the count and the changed files the freshness record reports.
    assert (proof.basis, proof.behind_by, proof.changed) == ("git", 2, ["m4.py", "m5.py"])


def test_a_true_non_ancestor_is_still_refused_after_the_fetch(tmp_path):
    lane = _Lane(tmp_path)
    fetched: list[str | None] = []

    def deepen() -> str | None:
        fetched.append(lane.fetch(lane.checkout, lane.base))
        return fetched[-1]

    proof = graphstage.prove_ancestry(lane.checkout, lane.off_main, lane.base, {},
                                      deepen=deepen)

    assert fetched == [None]
    # MUTATION: treating a fetch that landed as the proof itself (returning an
    # Ancestry when `deepen()` is None) stages a graph of code not on main.
    assert isinstance(proof, str) and str(lane.off_main[:12]) in proof
    assert "not within the history fetched" in proof


def test_an_index_older_than_the_fetch_depth_is_not_shown_not_guessed(tmp_path):
    lane = _Lane(tmp_path)
    proof = graphstage.prove_ancestry(
        lane.checkout, lane.main[0], lane.base, {},
        deepen=lambda: lane.fetch(lane.checkout, lane.base, depth=3))
    # MUTATION: a `--depth` that is not passed (an unbounded fetch) reaches
    # main[0] and proves it; the bound is what keeps start-up from growing.
    assert isinstance(proof, str) and "not within the history fetched" in proof


# --------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------


def test_a_recorded_compare_for_another_head_no_longer_blocks_a_provable_step(
        db, store, tmp_path):
    lane = _Lane(tmp_path)
    _no_ab(db)
    index = lane.main[4]
    other_head = "d" * 40
    _promote(db, store, index, relation={"base": index, "head": other_head,
                                         "status": "ahead", "ahead_by": 7, "files": []})

    before = _stage(db, store, lane, None)
    assert not before.staged and "cannot be shown" in before.reason

    result = _stage(db, store, lane, lane.fetch)

    # MUTATION: `_stage` not handing `deepen` to `prove_ancestry` (or building
    # none for a shallow clone) leaves the step refused as before this lane.
    assert result.staged, result.reason
    assert result.facts["basis"] == "git" and result.facts["behind_by"] == 1
    # MUTATION: dropping `history_seconds` from the result hides what the
    # fetch cost from stage.json.
    assert isinstance(result.record()["history_seconds"], float)


def test_a_fetch_that_times_out_leaves_a_reason_and_no_graph_and_the_attempt_goes_on(
        db, store, tmp_path):
    lane = _Lane(tmp_path)
    _no_ab(db)
    _promote(db, store, lane.main[4])
    slow = tmp_path / "bin" / "git"
    slow.parent.mkdir()
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(slow.stat().st_mode | stat.S_IXUSR)

    def fetch(checkout: Path, base_sha: str) -> str | None:
        return lane.fetch(checkout, base_sha, git_binary=str(slow), timeout_seconds=0.5)

    result = _stage(db, store, lane, fetch)

    # MUTATION: letting TimeoutExpired escape `fetch_base_history` makes the
    # reason "staging failed (TimeoutExpired)" -- no word of what timed out.
    assert not result.staged
    assert "cannot be shown" in result.reason
    assert "the history fetch timed out after 0.5 s" in result.reason
    assert not (lane.checkout.parent / graphstage.CONTEXT_DIR_NAME / "snapshot").exists()


def test_prepare_goes_on_with_the_reason_when_the_fetch_fails(db, store, tmp_path,
                                                                monkeypatch):
    lane = _Lane(tmp_path)
    _no_ab(db)
    _promote(db, store, lane.main[4])
    asked: list[dict[str, Any]] = []

    def refused(checkout: Path, **kwargs: Any) -> str | None:
        asked.append(kwargs)
        return "the history fetch failed (git exit 128)"

    monkeypatch.setattr(gitops, "fetch_base_history", refused)
    env = graphstage.prepare(lane.checkout.parent, profile_name="claude-code",
                             runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                             db=db, store=store, tenant_id=TENANT, repository_url=URL,
                             task_id="task_b", checkout=lane.checkout, base_sha=lane.base,
                             private_dir=lane.private)

    # MUTATION: `prepare` not building `history_fetcher` never fetches.
    assert len(asked) == 1 and asked[0]["url"] == URL and asked[0]["token"] is None
    # MUTATION: a failed fetch raised instead of returned ends the staging
    # with no prompt line; the agent must still be told none was staged.
    assert "git exit 128" in env[graphstage.UNSTAGED_ENV]
    record = json.loads((lane.checkout.parent / graphstage.CONTEXT_DIR_NAME /
                         graphstage.STAGE_RECORD_NAME).read_text())
    assert record["staged"] is False and "git exit 128" in record["reason"]


def test_the_fetch_runs_once_per_staging_and_never_for_a_full_clone(db, store, tmp_path):
    lane = _Lane(tmp_path)
    _no_ab(db)
    # Two kept versions, neither provable: the route's answer would list both.
    calls: list[str] = []

    def counting(checkout: Path, base_sha: str) -> str | None:
        calls.append(base_sha)
        return "the history fetch failed (git exit 128)"

    deepen = graphstage._HistoryOnce(counting, lane.checkout, lane.base)
    for index in (lane.main[3], lane.main[2]):
        graphstage.prove_ancestry(lane.checkout, index, lane.base, {}, deepen=deepen)
    # MUTATION: `_HistoryOnce` fetching on every call costs a step one fetch
    # per kept version, each up to the time limit.
    assert calls == [lane.base]

    full = tmp_path / "full"
    subprocess.run(["git", "clone", "-q", "--", lane.url, str(full)], check=True,
                   capture_output=True, env={**_ENV, "HOME": str(tmp_path)})
    # A commit the full clone does not hold: git cannot answer, and a fetch
    # could add nothing a full clone lacks.
    _promote(db, store, "e" * 40)
    calls.clear()
    result = graphstage.stage(db=db, store=store, tenant_id=TENANT, repository_url=URL,
                              task_id="task_b", checkout=full, base_sha=lane.base,
                              context_dir=tmp_path / "ctx", fetch_history=counting)
    # MUTATION: dropping the `is_shallow` gate fetches for a clone that
    # already holds every ancestor.
    assert calls == [] and not result.staged
    assert "cannot be shown" in result.reason


def test_a_token_is_held_in_a_scoped_file_and_no_planted_helper_is_handed_it(tmp_path):
    recorder = tmp_path / "bin" / "git"
    recorder.parent.mkdir()
    log = tmp_path / "argv.json"
    recorder.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"argv = sys.argv[1:]\nfiles = [a.split('--file=', 1)[1] for a in argv if '--file=' in a]\n"
        f"json.dump({{'argv': argv, 'held': [os.path.exists(f) for f in files]}}, "
        f"open({str(log)!r}, 'w'))\n")
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    token = secrets.token_hex(20)
    private = tmp_path / "private"

    why = gitops.fetch_base_history(tmp_path, url="https://github.com/acme/widgets",
                                    base_sha="a" * 40, private_dir=private, token=token,
                                    git_binary=str(recorder))

    assert why is None
    seen = json.loads(log.read_text())
    argv = seen["argv"]
    # MUTATION: no `credential.helper=` reset before the store helper hands
    # the token to a helper a restored `.git/config` planted.
    reset = argv.index("credential.helper=")
    store_at = next(i for i, a in enumerate(argv) if a.startswith("credential.helper=store"))
    assert reset < store_at
    # MUTATION: a token in argv is world-readable through /proc.
    assert all(token not in a for a in argv)
    assert seen["held"] == [True]
    # MUTATION: skipping `_remove_credentials` leaves the token on disk for
    # the agent that starts next.
    assert not any(token in p.read_text(errors="replace")
                   for p in private.rglob("*") if p.is_file())
    assert f"--depth={gitops.ANCESTRY_FETCH_DEPTH}" in argv and argv[-1] == "a" * 40
