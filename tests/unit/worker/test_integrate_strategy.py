"""`integrate` promises ONE pull request. This is where that promise is kept.

swarm-api resolves a four-field integration contract for every step of an
`integrate` workflow -- strategy, carrier, role and integrates -- and stores it
in `task.metadata["dispatch"]`. The worker read only `strategy`, and branched
only on `collect`.

The consequence was not subtle. Every step of an `integrate` workflow took the
`direct-pr` path: each one pushed `swarm/<its own task id>` and opened its own
pull request titled `[swarm] <its own task id>`, against an API whose schema
(schemas.py), validator (validation.py) and 201 response all state that
`integrate` produces exactly one. A six-step workflow produced six pull
requests, and `carrier: "branches"` -- which forces `needs_repository` and was
then read by nothing -- produced no behaviour at all.

These tests pin the three things that make the difference observable: a
contributor opens NO pull request, an integrator merges its upstream branches
before pushing, and an integration that could not take everything SAYS SO on
the pull request rather than only in the run result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_worker.gitops import GitError, merge_branches


# -- the dispatch block, read in full ---------------------------------------


def _worker(worker_factory, dispatch):
    worker, config, _ = worker_factory()
    worker._task = {"task_id": config.task_id, "metadata": {"dispatch": dispatch}}
    return worker


def test_role_and_integrates_are_read_back(worker_factory):
    w = _worker(
        worker_factory,
        {"strategy": "integrate", "role": "integrator", "integrates": ["t-a", "t-b"]},
    )
    assert w._dispatch_role() == "integrator"
    assert w._dispatch_integrates() == ["t-a", "t-b"]


def test_integrates_order_is_preserved(worker_factory):
    """swarm-api builds this from the workflow's TOPOLOGICAL prefix, so it is
    the order the patches have to be applied in. Sorting it would turn a clean
    sequence into an artificial conflict."""
    ids = ["t-c", "t-a", "t-b"]
    w = _worker(worker_factory, {"strategy": "integrate", "role": "integrator", "integrates": ids})
    assert w._dispatch_integrates() == ids


@pytest.mark.parametrize("value", ["boss", "", None, 7, [], {}])
def test_an_unrecognised_role_is_a_contributor_never_an_integrator(worker_factory, value):
    """Same newer-control-plane-older-worker reasoning as the strategy fallback.
    A contributor pushes a branch and opens nothing, which is the outcome that
    cannot surprise a caller; guessing `integrator` would open a pull request
    claiming to contain work this worker never merged."""
    w = _worker(worker_factory, {"strategy": "integrate", "role": value})
    assert w._dispatch_role() != "integrator"


def test_role_matching_is_case_insensitive_like_the_strategy(worker_factory):
    """Not leniency for its own sake -- consistency with the parser two methods
    up. _dispatch_strategy lowercases, and two adjacent parsers that disagree
    about case is exactly the kind of difference that drifts into a bug."""
    assert _worker(worker_factory, {"strategy": "integrate", "role": "INTEGRATOR"})._dispatch_role() == "integrator"
    assert _worker(worker_factory, {"strategy": "INTEGRATE"})._dispatch_strategy() == "integrate"


def test_integrates_ignores_non_string_entries(worker_factory):
    w = _worker(
        worker_factory,
        {"strategy": "integrate", "role": "integrator", "integrates": ["t-a", 7, None, "  ", "t-b"]},
    )
    assert w._dispatch_integrates() == ["t-a", "t-b"]


def test_carrier_defaults_to_checkpoints_and_reads_branches(worker_factory):
    """`checkpoints`, not `patches`.

    This test used to assert `patches` -- a word swarm-api's validator refuses
    at submission and can never send -- and so pinned the drift in place rather
    than catching it. `test_dispatch_contract_parity.py` now imports
    `DISPATCH_CARRIERS` instead of restating it, which is what makes this pair
    of values checkable rather than merely written down twice.
    """
    assert _worker(worker_factory, {"strategy": "integrate"})._dispatch_carrier() == "checkpoints"
    assert (
        _worker(worker_factory, {"strategy": "integrate", "carrier": "branches"})._dispatch_carrier()
        == "branches"
    )


def test_role_is_empty_for_every_strategy_but_integrate(worker_factory):
    """Only `integrate` gives its steps distinct roles. A `direct-pr` task that
    happened to carry a stale role must still open its own pull request."""
    worker, config, _ = worker_factory()
    worker._task = {
        "task_id": config.task_id,
        "metadata": {"dispatch": {"strategy": "direct-pr", "role": "contributor"}},
    }
    # _publish_git only consults the role when the strategy is `integrate`;
    # this pins the accessor's own answer so the branch above stays honest.
    assert worker._dispatch_strategy() == "direct-pr"


# -- merge_branches, against real repositories ------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def remote(tmp_path):
    """A bare remote with `main` plus three contributor branches.

    `swarm/clean` touches a file nothing else does. `swarm/conflict` rewrites
    the SAME line as `swarm/clean`, so merging both must conflict.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "shared.txt").write_text("base\n")
    (work / "README.md").write_text("readme\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")

    _git(work, "checkout", "-qb", "swarm/clean")
    (work / "clean.txt").write_text("from the clean contributor\n")
    (work / "shared.txt").write_text("clean wins\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "clean work")

    _git(work, "checkout", "-q", "main")
    _git(work, "checkout", "-qb", "swarm/conflict")
    (work / "shared.txt").write_text("conflict wins\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "conflicting work")

    _git(work, "checkout", "-q", "main")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return bare


@pytest.fixture
def integrator(tmp_path, remote):
    """A clone of `main` with one commit of the integrator's own work."""
    repo = tmp_path / "integrator"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    (repo / "integrator.txt").write_text("the integrator's own step\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "integrator work")
    return repo


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass


def _merge(repo, remote, branches, tmp_path, **kw):
    return merge_branches(
        repo=repo,
        url=str(remote),
        branches=branches,
        token="t",
        private_dir=tmp_path / "private",
        logs_dir=tmp_path / "logs",
        timeout_seconds=60,
        logger=_Log(),
        author_name="swarm",
        author_email="swarm@example.invalid",
        **kw,
    )


@pytest.fixture(autouse=True)
def _logs_dir(tmp_path):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "private").mkdir(exist_ok=True)


@pytest.fixture
def local_remote(remote, monkeypatch):
    """Let the merge reach a local bare repo.

    validate_repository_url allows only https and ssh, which is correct and has
    its own tests. These tests are about MERGE semantics, so the validator is
    replaced with identity rather than the scheme rule being weakened for
    everyone.
    """
    from agent_worker import gitops

    monkeypatch.setattr(gitops, "validate_repository_url", lambda u: u)
    return str(remote)


def test_a_clean_contributor_is_merged_and_its_file_lands(integrator, local_remote, tmp_path):
    """The property the whole strategy exists for: the integrator's tree ends up
    containing the contributor's work, so ONE pull request can carry it."""
    out = _merge(integrator, local_remote, ["swarm/clean"], tmp_path)

    assert out.merged == ("swarm/clean",)
    assert out.conflicted == () and out.missing == ()
    assert out.complete is True
    assert (integrator / "clean.txt").read_text() == "from the clean contributor\n"
    assert (integrator / "integrator.txt").exists(), "the integrator's own work survived the merge"


def test_a_conflicting_contributor_is_named_and_the_rest_still_merge(
    integrator, local_remote, tmp_path
):
    """The decision this encodes: an integrator is the SINK of a workflow whose
    other steps have already run and been billed. Refusing the whole pull
    request because one contributor conflicts throws away every successful
    attempt and leaves the caller nothing. So it takes what merges and names
    what it could not take."""
    out = _merge(integrator, local_remote, ["swarm/clean", "swarm/conflict"], tmp_path)

    assert out.merged == ("swarm/clean",)
    assert out.conflicted == ("swarm/conflict",)
    assert out.complete is False
    assert (integrator / "clean.txt").exists(), "the clean contributor still landed"


def test_a_conflict_leaves_no_half_applied_merge_behind(integrator, local_remote, tmp_path):
    """`git merge --abort` runs before the next branch is attempted. Without it
    a failed merge leaks a conflicted index into the branch that follows, and
    the pull request contains a state nobody can reason about."""
    _merge(integrator, local_remote, ["swarm/conflict"], tmp_path)

    assert not (integrator / ".git" / "MERGE_HEAD").exists(), "still mid-merge"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=integrator, capture_output=True, text=True
    )
    assert status.stdout.strip() == "", f"working tree is dirty after the abort: {status.stdout!r}"


def test_a_contributor_that_never_pushed_is_missing_not_conflicted(
    integrator, local_remote, tmp_path
):
    """The distinction tells a reviewer whether to re-run a step or resolve a
    conflict. Collapsing them would send them to the wrong one."""
    out = _merge(integrator, local_remote, ["swarm/never-ran"], tmp_path)

    assert out.missing == ("swarm/never-ran",)
    assert out.conflicted == ()
    assert out.complete is False


def test_merge_order_is_the_callers(integrator, local_remote, tmp_path):
    """Branch N may depend on N-1 having landed, so the topological order
    swarm-api computed has to survive this function."""
    out = _merge(integrator, local_remote, ["swarm/conflict", "swarm/clean"], tmp_path)

    # Reversed from the previous test: now `conflict` merges first and wins the
    # shared line, so `clean` is the one that conflicts.
    assert out.merged == ("swarm/conflict",)
    assert out.conflicted == ("swarm/clean",)


def test_merge_branches_refuses_a_branch_outside_the_prefix(integrator, local_remote, tmp_path):
    """The names arrive through task metadata. The same prefix rule that governs
    what this worker may PUSH has to govern what it may pull into a branch it is
    about to push, or metadata could point it at any ref on the remote."""
    out = _merge(integrator, local_remote, ["main", "refs/heads/main"], tmp_path)
    assert out.merged == ()
    assert "main" in out.missing
    assert out.complete is False


def test_an_empty_outcome_is_complete_but_a_conflicted_one_is_not():
    from agent_worker.gitops import MergeOutcome

    assert MergeOutcome().complete is True
    assert MergeOutcome(merged=("a",)).complete is True
    assert MergeOutcome(merged=("a",), conflicted=("b",)).complete is False
    assert MergeOutcome(merged=("a",), missing=("b",)).complete is False


# -- the pull request body tells the truth about what it contains -----------


def test_the_body_names_conflicted_and_missing_branches(worker_factory):
    """A reviewer who cannot see that an integration is partial will read it as
    complete. That is the most misleading thing this page could do, so it is
    stated on the pull request and not only in the run result."""
    from agent_worker.gitops import MergeOutcome

    worker, config, _ = worker_factory()
    body = worker._pull_request_body(
        branch=f"swarm/{config.task_id}",
        auto_committed=False,
        merge=MergeOutcome(
            merged=("swarm/t-a",), conflicted=("swarm/t-b",), missing=("swarm/t-c",)
        ),
    )
    assert "swarm/t-a" in body
    assert "swarm/t-b" in body and "conflict" in body.lower()
    assert "swarm/t-c" in body and "not found" in body.lower()
    assert "INCOMPLETE" in body


def test_the_body_is_unchanged_when_there_was_no_merge(worker_factory):
    """`direct-pr` has no contributors, so it must not grow an integration
    section that would read as an empty integration."""
    worker, config, _ = worker_factory()
    body = worker._pull_request_body(branch=f"swarm/{config.task_id}", auto_committed=False)
    assert "Integrates" not in body


# -- the crux, through _publish_git itself ----------------------------------
#
# Everything above tests a helper. This tests the branch that actually decides
# how many pull requests a workflow produces, because that is the defect: both
# helpers existed and the seam between them did not.


def _publish(worker_factory, tmp_path, monkeypatch, dispatch, *, opened):
    """Run _publish_git against a fake forge, recording pull requests opened."""
    from agent_worker import lifecycle, workspace as workspace_mod

    class _Access:
        can_push = True
        default_branch = "main"
        reason = ""

        class ref:
            full_name = "acme/widgets"

    monkeypatch.setattr(lifecycle, "probe_repository", lambda **kw: _Access())
    monkeypatch.setattr(lifecycle, "commit_dirty", lambda **kw: "")
    # Faked like every other git helper here: `repo` is not a repository and
    # there is no clone base. The fold itself runs for real, against a real
    # remote, in test_strategy_end_to_end.py.
    monkeypatch.setattr(lifecycle, "fold_agent_commits", lambda **kw: 0)
    # The check before the push reads commits, and there are none here. It
    # runs for real in test_strategy_end_to_end.py. `raising=False` because it
    # is added by the same change as this line.
    monkeypatch.setattr(lifecycle, "verify_worker_authorship", lambda **kw: 0, raising=False)
    monkeypatch.setattr(lifecycle, "push_branch", lambda **kw: "deadbeef")
    monkeypatch.setattr(
        lifecycle,
        "merge_branches",
        lambda **kw: __import__(
            "agent_worker.gitops", fromlist=["MergeOutcome"]
        ).MergeOutcome(merged=tuple(kw["branches"])),
    )

    def _open(**kw):
        opened.append(kw)
        class _PR:
            number, url, state, created = 1, "https://forge/pr/1", "open", True
        return _PR()

    monkeypatch.setattr(lifecycle, "open_pull_request", _open)

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker._task = {"task_id": config.task_id, "metadata": {"dispatch": dispatch}}
    worker._repo_url = "https://github.com/acme/widgets.git"
    monkeypatch.setattr(worker, "_git_token", lambda: "t")
    return worker._publish_git(repo=tmp_path, work_head="abc123", publish=True)


def test_a_contributor_pushes_its_branch_and_opens_no_pull_request(
    worker_factory, tmp_path, monkeypatch
):
    """THE defect, stated as a test. Every step used to reach the pull-request
    code, so a six-step `integrate` workflow opened six."""
    opened = []
    result = _publish(
        worker_factory,
        tmp_path,
        monkeypatch,
        {"strategy": "integrate", "role": "contributor"},
        opened=opened,
    )

    assert opened == [], "a contributor opened a pull request"
    assert result["published"] is True, "its branch still has to be pushed"
    assert result["branch"].startswith("swarm/")
    assert "integrator" in result["publish_reason"]


def test_the_integrator_opens_exactly_one_pull_request_after_merging(
    worker_factory, tmp_path, monkeypatch
):
    opened = []
    result = _publish(
        worker_factory,
        tmp_path,
        monkeypatch,
        {"strategy": "integrate", "role": "integrator", "integrates": ["t-a", "t-b"]},
        opened=opened,
    )

    assert len(opened) == 1, f"expected exactly one pull request, got {len(opened)}"
    assert result["integrated"]["merged"] == ["swarm/t-a", "swarm/t-b"]
    assert result["integrated"]["complete"] is True
    assert result["pull_request"]["number"] == 1


def test_direct_pr_still_opens_its_own_pull_request(worker_factory, tmp_path, monkeypatch):
    """The regression guard. `direct-pr` is the strategy where one PR per task
    IS the promise, and the contributor short-circuit must not reach it."""
    opened = []
    _publish(worker_factory, tmp_path, monkeypatch, {"strategy": "direct-pr"}, opened=opened)
    assert len(opened) == 1


def test_an_integrator_with_no_upstream_still_opens_its_pull_request(
    worker_factory, tmp_path, monkeypatch
):
    """A single-step `integrate` workflow is legal: the only step is the sink."""
    opened = []
    result = _publish(
        worker_factory,
        tmp_path,
        monkeypatch,
        {"strategy": "integrate", "role": "integrator", "integrates": []},
        opened=opened,
    )
    assert len(opened) == 1
    assert "integrated" not in result
