"""Every merge strategy, run end to end against a real git repository.

WHY THIS FILE EXISTS. `test_integrate_strategy.py` proves the *decisions*: it
fakes `push_branch`, `commit_dirty` and `merge_branches` and asserts that the
right branch of `_publish_git` is taken. `test_harvest.py` proves the *helpers*:
real git, but each function called on its own. Between the two there was a seam
nobody had ever executed -- clone, agent edit, harvest, commit, push, merge,
pull request, in that order, in one attempt -- and a seam nobody executes is
where this codebase keeps putting its bugs.

So here the ONLY thing faked is HTTP. `probe_repository` and
`open_pull_request` are replaced, because GitHub is not available to an offline
test and a credential is not available at all (see
`docs/runbooks/merge-strategy-live-proof.md` for what a live proof would need).
Everything else is the production code against a real bare repository on disk:
real `shallow_clone`, real `summarize_work`, real `commit_dirty`, real
`push_branch`, real `merge_branches`. The assertions are made against the
REMOTE -- which refs exist, and what is in the tree at their tips -- rather than
against the dict the worker returns, because the dict is what the worker
believes and the remote is what actually happened.

Each strategy is then pinned against the others. `integrate` once opened one
pull request per step because the worker read only `strategy` and branched only
on `collect`; the cheapest way for that to come back is for a strategy to
quietly start behaving like its neighbour, so every test below fails if it does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops, lifecycle, workspace as workspace_mod
from agent_worker.forge import PullRequest, RepoAccess, RepoRef

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- a real repository, and a real remote to push to ------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository with one commit on `main`, standing in for GitHub."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("the repository as the agent finds it\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")

    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return bare


def refs(bare: Path) -> dict[str, str]:
    """Every branch on the remote, mapped to its tip. The ground truth."""
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"],
        cwd=str(bare),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return dict(zip(out[0::2], out[1::2]))


def tree_at(bare: Path, ref: str) -> set[str]:
    """The files present at a ref on the remote, so a push can be believed."""
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=str(bare),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {line for line in listing.splitlines() if line}


@pytest.fixture
def local_urls(monkeypatch, tmp_path: Path) -> None:
    """Let git reach a `file://` remote inside tmp_path, and nothing else.

    `validate_repository_url` allows only https and ssh. That rule is correct,
    is what stops `ext::sh -c id` reaching git, and has its own tests -- so it
    is not weakened here. It is WRAPPED: a `file://` URL under this test's own
    tmp_path passes, and every other URL still goes through the real validator,
    including the `file://` URL of a path outside tmp_path.
    """
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"

    def validate(url: str) -> str:
        if url.startswith(allowed):
            return url
        return real(url)

    monkeypatch.setattr(gitops, "validate_repository_url", validate)


# -- the forge, faked at the HTTP boundary and nowhere else -----------------


class FakeForge:
    """Records what would have been asked of GitHub. No network, no token."""

    def __init__(self, *, can_push: bool = True, default_branch: str = "main") -> None:
        self.can_push = can_push
        self.default_branch = default_branch
        self.probes: list[str] = []
        self.pulls: list[dict] = []

    def probe(self, *, url: str, token: str | None) -> RepoAccess:
        self.probes.append(url)
        return RepoAccess(
            ref=RepoRef(host="github.com", owner="acme", name="widgets"),
            default_branch=self.default_branch,
            can_push=self.can_push,
            reason=("the token has write permission on this repository"
                    if self.can_push else "the token has pull but not push"),
        )

    def open_pull_request(self, **kwargs) -> PullRequest:
        self.pulls.append(kwargs)
        number = len(self.pulls)
        return PullRequest(
            number=number,
            url=f"https://github.com/acme/widgets/pull/{number}",
            state="open",
            created=True,
        )


@pytest.fixture
def forge(monkeypatch) -> FakeForge:
    fake = FakeForge()
    monkeypatch.setattr(lifecycle, "probe_repository", fake.probe)
    monkeypatch.setattr(lifecycle, "open_pull_request", fake.open_pull_request)
    return fake


# -- one whole attempt ------------------------------------------------------


def run_attempt(
    worker_factory,
    monkeypatch,
    origin: Path,
    *,
    task_id: str,
    dispatch: dict,
    edit,
    publish: bool = True,
):
    """Clone, let an "agent" edit, then harvest exactly as teardown does.

    `edit(repo)` stands in for the runner. It is handed the working tree the
    clone produced and may do anything an agent would -- including nothing,
    which is the case that turned out to matter most.
    """
    url = f"file://{origin}"
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}",
        lease_id=f"lease-{task_id}",
        repository_url=url,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": dispatch}}
    worker._task = task

    # A credential the tenant does not have. `_git_token` reads
    # `swarm-tenant-<tenant>-git`, and the `eng` tenant registers only
    # anthropic and openai -- which is exactly why no strategy but `collect`
    # has ever run. The runbook says what granting one would take.
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")

    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"], "the clone did not land"
    edit(worker.ws.work / lifecycle.REPO_DIR_NAME)

    return worker, config, worker._harvest_git(publish=publish)


def agent_edits_without_committing(repo: Path) -> None:
    """The common case, and the reason `commit_dirty` exists at all."""
    (repo / "agent.txt").write_text("work the agent never committed\n")
    (repo / "README.md").write_text("the repository as the agent left it\n")


def agent_does_nothing(repo: Path) -> None:
    pass


def contributor_edit(name: str):
    def edit(repo: Path) -> None:
        (repo / f"{name}.txt").write_text(f"work from {name}\n")
    return edit


# -- collect: harvests, and cannot be talked into pushing -------------------


def test_collect_harvests_a_patch_and_pushes_nothing_with_a_repository_attached(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The refusal that matters, against a remote a push WOULD have reached.

    The existing refusal test calls `_publish_git` with a path that is not a
    repository and no remote behind it, so "nothing was pushed" was true there
    whatever the code did. Here the clone is real, the changes are real, the
    credential is accepted by the fake forge with `can_push=True`, and the
    remote is one `git push` away -- so if `collect` ever pushed, `refs`
    below would show it.
    """
    before = refs(origin)

    worker, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-collect",
        dispatch={"strategy": "collect", "carrier": "checkpoints"},
        edit=agent_edits_without_committing,
    )

    assert refs(origin) == before, "collect pushed to the remote"
    assert forge.pulls == [], "collect opened a pull request"
    assert forge.probes == [], "collect contacted the forge at all"
    assert out["published"] is False
    assert "nothing is pushed" in out["publish_reason"]

    # The deliverable is still produced: a patch that carries the work.
    patch = worker.ws.artifacts / lifecycle.PATCH_NAME
    assert patch.exists(), "collect harvested nothing, so it delivered nothing"
    body = patch.read_text()
    assert "agent.txt" in body and "work the agent never committed" in body


def test_collect_does_not_push_even_when_push_branch_would_have_succeeded(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A second lock on the same door, from the other side.

    The test above proves the remote did not move. This proves the push
    FUNCTION was never entered, so a future refactor that reordered the checks
    and pushed before consulting the strategy could not pass by leaving the
    remote coincidentally unchanged.
    """
    def never(**kwargs):
        raise AssertionError("collect reached push_branch")

    monkeypatch.setattr(lifecycle, "push_branch", never)
    monkeypatch.setattr(lifecycle, "merge_branches", never)

    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-collect-2",
        dispatch={"strategy": "collect"},
        edit=agent_edits_without_committing,
    )
    assert out["published"] is False


def test_an_unknown_strategy_behaves_as_collect_against_a_real_remote(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A newer control plane against an older worker. The safe reading is the
    one that pushes nothing, and this proves it is the one that happens."""
    before = refs(origin)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-unknown",
        dispatch={"strategy": "publish-everything-immediately"},
        edit=agent_edits_without_committing,
    )
    assert refs(origin) == before
    assert forge.pulls == []
    assert out["published"] is False


# -- direct-pr: one branch and one pull request, per task -------------------


def test_direct_pr_pushes_a_real_branch_and_opens_one_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The whole chain, executed: clone -> edit -> harvest -> commit -> push.

    The agent committed nothing, so every byte on that branch got there through
    `commit_dirty`. A branch identical to its base would be the failure, and
    `tree_at` is what tells the two apart.
    """
    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-direct",
        dispatch={"strategy": "direct-pr", "carrier": "checkpoints"},
        edit=agent_edits_without_committing,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    landed = refs(origin)
    assert branch in landed, f"direct-pr did not push {branch}; remote has {sorted(landed)}"
    assert landed[branch] != landed["main"], "the branch is identical to its base"
    assert "agent.txt" in tree_at(origin, branch), "the agent's work is not on the branch"

    assert out["published"] is True
    assert out["auto_committed"] is True, "uncommitted work reached the branch by itself?"
    assert len(forge.pulls) == 1
    assert forge.pulls[0]["head"] == branch
    assert forge.pulls[0]["base"] == "main"
    assert out["pull_request"]["number"] == 1


def test_direct_pr_carries_work_the_agent_did_commit_itself(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The other half of `commit_dirty`'s contract: an agent that commits
    properly must not have its commits re-made, and must still reach the
    branch."""
    def edit(repo: Path) -> None:
        (repo / "committed.txt").write_text("the agent used git\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "--quiet", "-m", "agent's own commit")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-direct-committed",
        dispatch={"strategy": "direct-pr"},
        edit=edit,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert "committed.txt" in tree_at(origin, branch)
    assert out["auto_committed"] is False, "the worker re-committed a clean tree"
    assert out["commit_count"] == 1, "the agent's own commit was not harvested"


def test_direct_pr_pushes_nothing_when_the_agent_changed_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """An empty branch and an empty pull request are noise a human closes by
    hand. Nothing to push is a reason, not a failure."""
    before = refs(origin)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-direct-empty",
        dispatch={"strategy": "direct-pr"},
        edit=agent_does_nothing,
    )
    assert refs(origin) == before
    assert forge.pulls == []
    assert out["published"] is False
    assert "changed nothing" in out["publish_reason"]


# -- integrate: N branches in, exactly ONE pull request out -----------------


def _contributors(worker_factory, monkeypatch, origin, forge, names):
    """Run each contributor for real, and return what it reported."""
    results = {}
    for name in names:
        _, config, out = run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=name,
            dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
            edit=contributor_edit(name),
        )
        results[name] = (config, out)
    return results


def test_a_contributor_pushes_its_branch_and_opens_no_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """THE defect, at the level it actually happened. Every step used to reach
    the pull-request code, so a six-step workflow produced six."""
    results = _contributors(worker_factory, monkeypatch, origin, forge, ["t-a", "t-b"])

    landed = refs(origin)
    assert "swarm/t-a" in landed and "swarm/t-b" in landed
    assert "t-a.txt" in tree_at(origin, "swarm/t-a")
    assert "t-b.txt" in tree_at(origin, "swarm/t-b")
    assert forge.pulls == [], f"a contributor opened {len(forge.pulls)} pull request(s)"

    for name in ("t-a", "t-b"):
        _, out = results[name]
        assert out["published"] is True, "a contributor's branch still has to be pushed"
        assert "integrator" in out["publish_reason"]
        assert "pull_request" not in out


def test_the_integrator_merges_every_contributor_and_opens_exactly_one_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The promise, end to end and against the remote.

    The integrator's branch has to carry `t-a.txt`, `t-b.txt` AND its own file,
    because a pull request that integrates a workflow while containing a subset
    of it is the single most misleading thing this platform can produce.
    """
    _contributors(worker_factory, monkeypatch, origin, forge, ["t-a", "t-b"])
    assert forge.pulls == []

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-int",
        dispatch={
            "strategy": "integrate",
            "carrier": "branches",
            "role": "integrator",
            "integrates": ["t-a", "t-b"],
        },
        edit=contributor_edit("t-int"),
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    landed = tree_at(origin, branch)
    assert {"t-a.txt", "t-b.txt", "t-int.txt"} <= landed, (
        f"the single pull request does not contain the whole workflow: {sorted(landed)}"
    )

    assert out["integrated"]["merged"] == ["swarm/t-a", "swarm/t-b"]
    assert out["integrated"]["complete"] is True
    assert len(forge.pulls) == 1, f"expected ONE pull request, got {len(forge.pulls)}"
    assert forge.pulls[0]["head"] == branch
    body = forge.pulls[0]["body"]
    assert "swarm/t-a" in body and "swarm/t-b" in body


def test_the_integrator_opens_its_pull_request_even_when_it_changed_nothing_itself(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """An integrator's deliverable is the OTHER steps' work.

    `_harvest_git` returns early when the agent changed nothing -- correct for
    every other step and wrong for exactly this one. An integrator whose agent
    edited no files (an ordinary outcome for a step whose prompt is "bring
    these together") returned before `_publish_git` ran: no contributor branch
    was merged, no branch was pushed, and the ONE pull request `integrate`
    promises was never opened. Zero, for a workflow whose contributors had all
    already pushed and been billed.
    """
    _contributors(worker_factory, monkeypatch, origin, forge, ["t-a", "t-b"])

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-int-idle",
        dispatch={
            "strategy": "integrate",
            "carrier": "branches",
            "role": "integrator",
            "integrates": ["t-a", "t-b"],
        },
        edit=agent_does_nothing,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), "the integrator never pushed its branch"
    assert {"t-a.txt", "t-b.txt"} <= tree_at(origin, branch)
    assert len(forge.pulls) == 1, (
        "an integrator that edited nothing opened no pull request, so the "
        "workflow produced zero instead of one"
    )
    assert out["integrated"]["merged"] == ["swarm/t-a", "swarm/t-b"]


def test_a_contributor_that_changed_nothing_pushes_no_branch(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The other edge of the integrator exemption, held in place.

    `_integration_is_pending` lets an integrator past the empty-work short
    circuit. Widening it to the whole `integrate` strategy would make an idle
    CONTRIBUTOR push a branch identical to `main` -- which the integrator would
    then merge as a no-op and report as `merged`, turning a step that produced
    nothing into a step that looks like it succeeded. Absent is the honest
    answer, and `missing` on the pull request is how a reviewer learns to
    re-run it.
    """
    before = refs(origin)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-idle-contributor",
        dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
        edit=agent_does_nothing,
    )
    assert refs(origin) == before, "an idle contributor pushed an empty branch"
    assert out["published"] is False
    assert "changed nothing" in out["publish_reason"]


def test_a_lone_integrator_that_changed_nothing_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A single-step `integrate` workflow is legal and has no upstream, so an
    idle one genuinely has nothing to publish. The exemption is for a step that
    still owes a merge, not for the role."""
    before = refs(origin)
    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-lone-integrator",
        dispatch={"strategy": "integrate", "role": "integrator", "integrates": []},
        edit=agent_does_nothing,
    )
    assert refs(origin) == before
    assert forge.pulls == []
    assert out["published"] is False
    assert "changed nothing" in out["publish_reason"]


def test_a_contributor_that_never_pushed_is_named_on_the_pull_request(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A partial integration says so, on the page a human reads. `t-b` never
    ran, so its branch is not on the remote and the merge cannot take it."""
    _contributors(worker_factory, monkeypatch, origin, forge, ["t-a"])

    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-int-partial",
        dispatch={
            "strategy": "integrate",
            "role": "integrator",
            "integrates": ["t-a", "t-b"],
        },
        edit=contributor_edit("t-int-partial"),
    )

    assert out["integrated"]["merged"] == ["swarm/t-a"]
    assert out["integrated"]["missing"] == ["swarm/t-b"]
    assert out["integrated"]["complete"] is False
    assert len(forge.pulls) == 1
    body = forge.pulls[0]["body"]
    assert "NOT included" in body, "a partial integration read as a complete one"
    assert "swarm/t-b" in body and "not found" in body.lower()
    # Deliberately NOT asserting the word "INCOMPLETE" here. It appears only in
    # the conflicted block, and a missing branch is a different message to a
    # reviewer -- re-run the step, rather than resolve a conflict. Asserting it
    # would be this test demanding a wording change it has no case for.


def test_a_conflicting_contributor_is_left_out_and_the_rest_still_land(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Both contributors rewrite README.md, so the second cannot merge. The
    integrator takes what merges rather than throwing away a workflow's worth
    of finished attempts."""
    def rewrite(text: str):
        def edit(repo: Path) -> None:
            (repo / "README.md").write_text(text)
        return edit

    for name, text in (("t-x", "x wins\n"), ("t-y", "y wins\n")):
        run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=name,
            dispatch={"strategy": "integrate", "role": "contributor"},
            edit=rewrite(text),
        )

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-int-conflict",
        dispatch={
            "strategy": "integrate",
            "role": "integrator",
            "integrates": ["t-x", "t-y"],
        },
        edit=contributor_edit("t-int-conflict"),
    )

    assert out["integrated"]["merged"] == ["swarm/t-x"]
    assert out["integrated"]["conflicted"] == ["swarm/t-y"]
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert branch in refs(origin), "a conflict aborted the whole integration"
    assert len(forge.pulls) == 1
    assert "INCOMPLETE" in forge.pulls[0]["body"]


# -- the strategies must not be able to impersonate one another -------------


@pytest.mark.parametrize(
    "dispatch, pushes, pull_requests",
    [
        ({"strategy": "collect"}, False, 0),
        ({"strategy": "direct-pr"}, True, 1),
        ({"strategy": "integrate", "role": "contributor"}, True, 0),
        ({"strategy": "integrate", "role": "integrator"}, True, 1),
    ],
    ids=["collect", "direct-pr", "integrate-contributor", "integrate-integrator"],
)
def test_each_strategy_has_its_own_observable_outcome(
    worker_factory, monkeypatch, origin, local_urls, forge, dispatch, pushes, pull_requests
):
    """The matrix, stated once. Four dispatches over identical work, and no two
    of them may produce the same pair of observable facts -- did a branch reach
    the remote, and how many pull requests were opened. Every way these four
    have collapsed into each other so far is one cell of this table changing.
    """
    task_id = "t-matrix"
    _, config, _ = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id=task_id,
        dispatch=dispatch,
        edit=agent_edits_without_committing,
    )

    branch = f"{config.git_branch_prefix}{task_id}"
    assert (branch in refs(origin)) is pushes
    assert len(forge.pulls) == pull_requests


def test_the_park_path_publishes_nothing_under_any_strategy(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A parked attempt resumes and reaches the terminal path later. Pushing
    work still in progress would put a half-finished change in front of a
    reviewer, and do it again on every quota bounce."""
    before = refs(origin)
    for strategy in ("collect", "direct-pr", "integrate"):
        _, _, out = run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=f"t-park-{strategy}",
            dispatch={"strategy": strategy, "role": "integrator", "integrates": ["t-a"]},
            edit=agent_edits_without_committing,
            publish=False,
        )
        assert out["published"] is False
        assert "parked" in out["publish_reason"]
    assert refs(origin) == before
    assert forge.pulls == []


def test_a_read_only_token_is_a_stated_fact_not_a_failed_attempt(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Today's live state for the `eng` tenant, which has no git credential at
    all. The forge is asked and says no; the patch is the deliverable."""
    forge.can_push = False
    before = refs(origin)

    worker, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-readonly",
        dispatch={"strategy": "direct-pr"},
        edit=agent_edits_without_committing,
    )

    assert refs(origin) == before
    assert forge.pulls == []
    assert out["published"] is False
    assert out["can_push"] is False
    assert "push" in out["publish_reason"]
    assert (worker.ws.artifacts / lifecycle.PATCH_NAME).exists()


# -- no tool's attribution reaches the forge --------------------------------
#
# THE OWNER'S RULE (2026-09-25): nothing this platform puts on GitHub carries
# Claude attribution -- no `Co-Authored-By: Claude` trailer, no "Generated with
# Claude Code" footer, no commit authored as Claude.
#
# The worker's own commits never did. The AGENT's could: the claude-code runner
# starts Claude Code with no settings of its own and with HOME set to the
# attempt's workspace (`runners/cliagent.py`), so a settings file baked into
# the image's home directory is never read, and an agent that runs `git commit`
# itself follows Claude Code's default instruction to add the trailer. In a
# container with no git identity it may also pick an identity of its own. Those
# commits were pushed exactly as the agent made them.
#
# So the guarantee is made where the work leaves the worker, for every runner
# at once, and these tests hold it against the REMOTE: every commit a `swarm/`
# branch adds over `main` is authored AND committed by the worker's identity,
# with a message the worker wrote.

#: Case-insensitive fragments that only attribution produces. Deliberately not
#: the bare word "claude": the platform's own runner profile is `claude-code`,
#: and the pull request body names it as provenance.
ATTRIBUTION_MARKERS = (
    "co-authored-by",
    "generated with",
    "noreply@anthropic.com",
    "anthropic.com",
    "claude.com/claude-code",
    "claude.ai/code",
)

CLAUDE_COMMIT_MESSAGE = (
    "Add committed.txt\n"
    "\n"
    "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n"
    "\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\n"
)


def _commit_as_claude(repo: Path, name: str) -> None:
    """Commit the way an agent in the claude-code runner does by default."""
    ident = ["-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com"]
    subprocess.run(["git", *ident, "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", *ident, "commit", "--quiet", "-m",
         CLAUDE_COMMIT_MESSAGE.replace("committed.txt", name)],
        cwd=str(repo), check=True, capture_output=True,
    )


def agent_commits_with_claude_attribution(repo: Path) -> None:
    """One attributed commit, then more work left uncommitted on top of it."""
    (repo / "committed.txt").write_text("work the agent committed as Claude\n")
    _commit_as_claude(repo, "committed.txt")
    (repo / "uncommitted.txt").write_text("work the agent left uncommitted\n")


def contributor_commits_with_claude_attribution(name: str):
    def edit(repo: Path) -> None:
        (repo / f"{name}.txt").write_text(f"work from {name}, committed as Claude\n")
        _commit_as_claude(repo, f"{name}.txt")
    return edit


def pushed_commits(bare: Path, branch: str) -> list[dict]:
    """Every commit `branch` adds over `main` on the remote: what a reviewer sees."""
    raw = subprocess.run(
        ["git", "log", "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", f"main..{branch}"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        author, author_email, committer, committer_email, message = record.split("\x1f", 4)
        commits.append({
            "author": (author, author_email),
            "committer": (committer, committer_email),
            "message": message,
        })
    return commits


def assert_only_the_worker_wrote(commits: list[dict], config) -> None:
    assert commits, "the branch adds no commits, so there was nothing to check"
    worker = (config.git_author_name, config.git_author_email)
    for commit in commits:
        assert commit["author"] == worker, (
            f"a pushed commit is authored by {commit['author']}, not the worker"
        )
        assert commit["committer"] == worker, (
            f"a pushed commit is committed by {commit['committer']}, not the worker"
        )
        lowered = commit["message"].lower()
        found = [m for m in ATTRIBUTION_MARKERS if m in lowered]
        assert not found, f"a pushed commit message carries attribution {found}:\n{commit['message']}"


def assert_no_attribution_in(text: str, what: str) -> None:
    lowered = text.lower()
    found = [m for m in ATTRIBUTION_MARKERS if m in lowered]
    assert not found, f"the {what} carries attribution {found}:\n{text}"


def test_direct_pr_pushes_no_commit_the_agent_attributed_to_claude(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The agent committed as Claude with the trailer and the footer, then left
    more work uncommitted. All of the work reaches the branch; none of the
    attribution does."""
    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-direct-attributed",
        dispatch={"strategy": "direct-pr"},
        edit=agent_commits_with_claude_attribution,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert {"committed.txt", "uncommitted.txt"} <= tree_at(origin, branch), (
        "replacing the agent's commits lost some of its work"
    )
    assert_only_the_worker_wrote(pushed_commits(origin, branch), config)

    assert len(forge.pulls) == 1
    assert_no_attribution_in(forge.pulls[0]["title"], "pull request title")
    assert_no_attribution_in(forge.pulls[0]["body"], "pull request body")


def test_integrate_pushes_no_commit_any_step_attributed_to_claude(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Every contributor and the integrator commit as Claude. The contributor
    branches and the one integrated branch carry every step's work and only
    the worker's commits -- the integrator merges what the contributors
    PUSHED, so a contributor that pushed attribution would put it here too."""
    for name in ("t-a", "t-b"):
        run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=name,
            dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
            edit=contributor_commits_with_claude_attribution(name),
        )

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-int-attributed",
        dispatch={
            "strategy": "integrate",
            "carrier": "branches",
            "role": "integrator",
            "integrates": ["t-a", "t-b"],
        },
        edit=contributor_commits_with_claude_attribution("t-int-attributed"),
    )

    assert out["integrated"]["merged"] == ["swarm/t-a", "swarm/t-b"]
    for name in ("t-a", "t-b"):
        assert_only_the_worker_wrote(pushed_commits(origin, f"swarm/{name}"), config)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert {"t-a.txt", "t-b.txt", "t-int-attributed.txt"} <= tree_at(origin, branch)
    assert_only_the_worker_wrote(pushed_commits(origin, branch), config)
    assert len(forge.pulls) == 1
    assert_no_attribution_in(forge.pulls[0]["body"], "pull request body")


def test_an_unknown_clone_base_publishes_nothing_rather_than_unchecked_commits(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Replacing the agent's commits needs the commit the clone landed on. A
    resumed attempt whose marker was lost does not have it, and pushing
    whatever HEAD holds is exactly the unchecked push the rule forbids. The
    patch is still harvested, so the work is not lost -- only not pushed."""
    url = f"file://{origin}"
    worker, config, _ = worker_factory(
        task_id="t-no-base", attempt_id="att-t-no-base", lease_id="lease-t-no-base",
        repository_url=url,
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-no-base", "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None
    agent_commits_with_claude_attribution(worker.ws.work / lifecycle.REPO_DIR_NAME)

    # What a resumed attempt with no marker looks like: nothing in memory, and
    # nothing on disk to read it back from.
    worker._clone_base = None
    marker = worker.ws.work / lifecycle.WORKER_STATE_DIR / lifecycle.CLONE_BASE_FILE
    marker.unlink(missing_ok=True)

    before = refs(origin)
    out = worker._harvest_git(publish=True)

    assert refs(origin) == before, "commits nobody checked were pushed"
    assert forge.pulls == []
    assert out["published"] is False
    assert "clone base" in out["publish_reason"]
