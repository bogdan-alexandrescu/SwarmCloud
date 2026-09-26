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


def pushed_commits(bare: Path, branch: str, *, since: str | None = "main") -> list[dict]:
    """Every commit `branch` adds over `main` on the remote: what a reviewer sees.

    `since=None` lists every commit on the branch, for a remote that has no
    `main` to compare against (an empty repository the agent populated)."""
    revision = f"{since}..{branch}" if since else branch
    raw = subprocess.run(
        ["git", "log", "--format=%an%x1f%ae%x1f%cn%x1f%ce%x1f%P%x1f%B%x1e", revision],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        author, author_email, committer, committer_email, parents, message = record.split(
            "\x1f", 5
        )
        commits.append({
            "author": (author, author_email),
            "committer": (committer, committer_email),
            "parents": parents.split(),
            "message": message,
        })
    return commits


def tags(bare: Path) -> list[str]:
    """Every tag on the remote. A swarm attempt has no reason to publish one."""
    return subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/tags/"],
        cwd=str(bare), check=True, capture_output=True, text=True,
    ).stdout.split()


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
    resumed attempt that has lost it -- nothing in memory, nothing in the
    checkpoint it restored, and no marker in the workspace either -- does not
    have it, and pushing whatever HEAD holds is exactly the unchecked push the
    rule forbids.

    NO PATCH IS PRODUCED HERE EITHER, and this test says so rather than
    implying otherwise: a patch is a diff against the clone base, so without
    one the harvest can list the dirty files and nothing more. The work
    survives only in the checkpoint archive. (The case where the patch IS still
    harvested -- a checkpoint that records no base, with the marker still in
    the workspace -- is the next test.)"""
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

    # What a resumed attempt that knows no base looks like: nothing in memory
    # (neither the harvest's base nor the one the worker publishes from), and
    # nothing on disk to read it back from.
    worker._clone_base = None
    worker._publish_base = None
    marker = worker.ws.work / lifecycle.WORKER_STATE_DIR / lifecycle.CLONE_BASE_FILE
    marker.unlink(missing_ok=True)

    before = refs(origin)
    out = worker._harvest_git(publish=True)

    assert refs(origin) == before, "commits nobody checked were pushed"
    assert forge.pulls == []
    assert out["published"] is False
    assert "clone base" in out["publish_reason"]
    assert out.get("patch") is None, "a patch was produced with no base to diff against"
    assert not (worker.ws.artifacts / lifecycle.PATCH_NAME).exists()
    assert "uncommitted.txt" in out["dirty"], "the dirty list is what survives"


# -- the clone base the fold trusts is the worker's, not the workspace's -----
#
# A resumed attempt used to read the commit its clone landed on from
# `work/.swarm/clone-base` -- a file in the agent's own working directory, and
# in the checkpoint archive the next attempt restores. An agent that wrote a
# LATER commit of its own there moved the fold's floor above its own commits:
# `base..HEAD` then held only the commits above the forged base, and every one
# below it was pushed as the agent wrote it. The base the worker publishes
# from is now the one the worker recorded in the checkpoint MANIFEST, which is
# outside the tree the agent works in.


def _attempt(worker_factory, monkeypatch, origin, *, task_id: str, attempt: int, dispatch: dict):
    """One attempt's worker, workspace and task, with no clone yet."""
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}-{attempt}",
        lease_id=f"lease-{task_id}-{attempt}",
        generation=attempt,
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": task_id, "metadata": {"dispatch": dispatch}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    return worker, config, task


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_resumed_attempt_publishes_from_the_base_the_worker_recorded_not_the_workspace_marker(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The agent commits three times as Claude, writes its OWN third commit into
    the workspace's clone-base marker, and the attempt checkpoints. The next
    attempt restores that checkpoint, the agent commits once more, and the
    worker publishes. Every commit the branch adds over `main` must still be
    the worker's: the forged marker must not lift the fold above the agent's
    first three commits."""
    dispatch = {"strategy": "direct-pr"}
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-forged-base", attempt=1, dispatch=dispatch
    )
    assert first._maybe_clone(task)["commit"], "the clone did not land"
    repo = first.ws.work / lifecycle.REPO_DIR_NAME
    for n in (1, 2, 3):
        (repo / f"c{n}.txt").write_text(f"commit {n}, made as Claude\n")
        _commit_as_claude(repo, f"c{n}.txt")
    marker = first.ws.work / lifecycle.WORKER_STATE_DIR / lifecycle.CLONE_BASE_FILE
    marker.write_text(_head(repo) + "\n")
    record = first.checkpoints.create(first.ws, label="park")

    second, config, _ = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-forged-base", attempt=2, dispatch=dispatch
    )
    second._restore_checkpoint(record.uri)
    assert second._maybe_clone(task)["from_checkpoint"] is True
    repo = second.ws.work / lifecycle.REPO_DIR_NAME
    (repo / "c4.txt").write_text("commit 4, made as Claude after the resume\n")
    _commit_as_claude(repo, "c4.txt")

    out = second._harvest_git(publish=True)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert {"c1.txt", "c2.txt", "c3.txt", "c4.txt"} <= tree_at(origin, branch)
    assert_only_the_worker_wrote(pushed_commits(origin, branch), config)


def test_a_checkpoint_that_records_no_clone_base_is_harvested_but_not_published(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """A checkpoint written before the worker recorded the base in its manifest
    carries only the workspace marker. The marker is good enough to DESCRIBE
    the work -- the patch and the commit list stay in the platform -- and not
    good enough to decide what reaches the forge, because the agent can write
    it. So the resumed attempt harvests a patch and refuses the push."""
    dispatch = {"strategy": "direct-pr"}
    first, _, task = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-legacy-ckpt", attempt=1, dispatch=dispatch
    )
    assert first._maybe_clone(task)["commit"], "the clone did not land"
    agent_commits_with_claude_attribution(first.ws.work / lifecycle.REPO_DIR_NAME)
    # What a manifest written before this change holds: no clone base at all.
    first.checkpoints.clone_base = None
    record = first.checkpoints.create(first.ws, label="park")

    second, _, _ = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-legacy-ckpt", attempt=2, dispatch=dispatch
    )
    second._restore_checkpoint(record.uri)
    assert second._maybe_clone(task)["from_checkpoint"] is True

    before = refs(origin)
    out = second._harvest_git(publish=True)

    assert refs(origin) == before, "a push was decided by a marker the agent can write"
    assert forge.pulls == []
    assert out["published"] is False
    assert "clone base" in out["publish_reason"]
    assert out["patch"] == lifecycle.PATCH_NAME, "the work was not harvested"
    patch = (second.ws.artifacts / lifecycle.PATCH_NAME).read_text()
    assert "committed.txt" in patch and "uncommitted.txt" in patch


# -- identity and refs the agent can plant in the clone's own config ---------
#
# `.git/config` in the clone is the agent's to write, and the worker's git runs
# in that repository. git takes a commit's author from `author.*` and its
# committer from `committer.*` BEFORE `user.*`, so the worker's `-c user.name`
# overrode nothing an agent had put there. A cherry-pick left in progress hands
# the concluding commit the picked commit's author whatever the config says.
# `push.followTags` pushes every annotated tag reachable from the branch. And
# `core.fsmonitor` and `gpg.program` are programs git runs on the worker's
# behalf.


def _config(repo: Path, *pairs: tuple[str, str]) -> None:
    for key, value in pairs:
        subprocess.run(
            ["git", "config", key, value], cwd=str(repo), check=True, capture_output=True
        )


CLAUDE_IDENTITY_IN_CONFIG = (
    ("author.name", "Claude"),
    ("author.email", "noreply@anthropic.com"),
    ("committer.name", "Claude"),
    ("committer.email", "noreply@anthropic.com"),
)


@pytest.mark.parametrize("also_commits", [False, True], ids=["uncommitted-only", "committed-too"])
def test_an_identity_the_agent_wrote_into_git_config_reaches_no_pushed_commit(
    worker_factory, monkeypatch, origin, local_urls, forge, also_commits
):
    """Uncommitted-only is the case the fold never sees: the only commit is the
    worker's own auto-commit, so it is pushed as made -- and it was made as
    Claude. Committed-too is the fold commit itself."""
    def edit(repo: Path) -> None:
        _config(repo, *CLAUDE_IDENTITY_IN_CONFIG)
        if also_commits:
            (repo / "committed.txt").write_text("work the agent committed\n")
            _commit_as_claude(repo, "committed.txt")
        (repo / "uncommitted.txt").write_text("work the agent left uncommitted\n")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id=f"t-config-identity-{'committed' if also_commits else 'uncommitted'}",
        dispatch={"strategy": "direct-pr"},
        edit=edit,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert "uncommitted.txt" in tree_at(origin, branch)
    assert_only_the_worker_wrote(pushed_commits(origin, branch), config)


def test_an_integrators_merge_commits_are_the_workers_whatever_its_git_config_says(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The integrator's merges are made after the fold, in the same clone, so
    an identity in its config reached them too."""
    for name in ("t-cfg-a", "t-cfg-b"):
        run_attempt(
            worker_factory, monkeypatch, origin,
            task_id=name,
            dispatch={"strategy": "integrate", "carrier": "branches", "role": "contributor"},
            edit=contributor_edit(name),
        )

    def edit(repo: Path) -> None:
        _config(repo, *CLAUDE_IDENTITY_IN_CONFIG)
        (repo / "t-cfg-int.txt").write_text("the integrator's own work\n")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-cfg-int",
        dispatch={
            "strategy": "integrate",
            "carrier": "branches",
            "role": "integrator",
            "integrates": ["t-cfg-a", "t-cfg-b"],
        },
        edit=edit,
    )

    assert out["integrated"]["merged"] == ["swarm/t-cfg-a", "swarm/t-cfg-b"]
    branch = f"{config.git_branch_prefix}{config.task_id}"
    commits = pushed_commits(origin, branch)
    merges = [c for c in commits if len(c["parents"]) > 1]
    assert len(merges) == 2, "the merge commits were not among what was checked"
    assert_only_the_worker_wrote(commits, config)


def agent_leaves_a_cherry_pick_in_progress(repo: Path) -> None:
    """Two commits as Claude on a side branch, the second picked onto the cloned
    branch, the conflict resolved in the tree, and the pick never concluded.

    Nothing is committed on the cloned branch, so the worker's auto-commit is
    the only commit over the base -- the case the fold leaves as it is."""
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=str(repo), check=True)
    for text in ("first side edit\n", "second side edit\n"):
        (repo / "README.md").write_text(text)
        _commit_as_claude(repo, "README.md")
    subprocess.run(["git", "checkout", "-q", "-"], cwd=str(repo), check=True)
    picked = subprocess.run(
        ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
         "cherry-pick", "side"],
        cwd=str(repo), capture_output=True,
    )
    assert picked.returncode != 0, "the pick applied cleanly, so nothing is left in progress"
    (repo / "README.md").write_text("resolved by the agent\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    assert (repo / ".git" / "CHERRY_PICK_HEAD").exists()


def test_a_cherry_pick_the_agent_left_in_progress_lends_its_author_to_no_pushed_commit(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-cherry-pick",
        dispatch={"strategy": "direct-pr"},
        edit=agent_leaves_a_cherry_pick_in_progress,
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert_only_the_worker_wrote(pushed_commits(origin, branch), config)


def test_a_tag_the_agent_made_is_not_pushed_even_with_follow_tags_set(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """An annotated tag carries a tagger and a message the agent chose, and
    `push.followTags` pushes it alongside the branch it points into. The fold
    rewrites commits; it never sees a tag."""
    def edit(repo: Path) -> None:
        _config(repo, ("push.followTags", "true"))
        subprocess.run(
            ["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
             "tag", "-a", "claude-was-here", "HEAD", "-m", CLAUDE_COMMIT_MESSAGE],
            cwd=str(repo), check=True, capture_output=True,
        )
        (repo / "agent.txt").write_text("work the agent left uncommitted\n")

    assert tags(origin) == [], "the fixture's remote already has a tag"
    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-follow-tags",
        dispatch={"strategy": "direct-pr"},
        edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    assert tags(origin) == [], "the agent's tag reached the remote"


def test_a_program_the_agent_named_in_git_config_is_never_run_by_the_worker(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """`core.fsmonitor` is a hook by another name, and `commit.gpgSign` makes
    every worker commit run `gpg.program`. Both are read from the clone's
    config, both run under the worker after the runner has exited, and the
    null hooks path stops neither. A signing program the agent wrote would
    also put text it chose into the worker's own commit object."""
    ran = tmp_path / "ran-under-the-worker"
    program = tmp_path / "agent-program.sh"
    program.write_text(f'#!/bin/sh\necho "$0 $*" >> "{ran}"\nexit 1\n')
    program.chmod(0o755)

    def edit(repo: Path) -> None:
        _config(
            repo,
            ("core.fsmonitor", str(program)),
            ("commit.gpgSign", "true"),
            ("gpg.program", str(program)),
        )
        (repo / "agent.txt").write_text("work the agent left uncommitted\n")

    _, config, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-config-programs",
        dispatch={"strategy": "direct-pr"},
        edit=edit,
    )

    assert not ran.exists(), f"the agent's program ran under the worker:\n{ran.read_text()}"
    assert out["published"] is True, out.get("publish_reason")
    assert_only_the_worker_wrote(
        pushed_commits(origin, f"{config.git_branch_prefix}{config.task_id}"), config
    )


# -- the property, checked where the work leaves ------------------------------


def test_a_commit_the_worker_did_not_write_is_refused_at_the_push_even_without_the_fold(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The fold is how the property is MADE; the push is where it is CHECKED.
    With the fold taken away, the agent's attributed commit reaches the push
    step -- and the push must refuse it rather than trust that nothing
    upstream of it ever regresses."""
    monkeypatch.setattr(lifecycle, "fold_agent_commits", lambda **kw: 0)
    before = refs(origin)

    _, _, out = run_attempt(
        worker_factory, monkeypatch, origin,
        task_id="t-unfolded",
        dispatch={"strategy": "direct-pr"},
        edit=agent_commits_with_claude_attribution,
    )

    assert refs(origin) == before, "a commit the worker did not write was pushed"
    assert forge.pulls == []
    assert out["published"] is False
    assert "authored by Claude" in out["publish_reason"]


# -- an empty repository has no base, and that is not an unknown one ---------


@pytest.fixture
def empty_origin(tmp_path: Path) -> Path:
    """A bare repository with no commits at all: 'scaffold the project here'."""
    bare = tmp_path / "empty.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    return bare


@pytest.mark.parametrize(
    "edit",
    [agent_edits_without_committing, agent_commits_with_claude_attribution],
    ids=["uncommitted-only", "committed-too"],
)
def test_work_in_an_empty_repository_is_one_parentless_worker_commit(
    worker_factory, monkeypatch, empty_origin, local_urls, forge, edit
):
    """`git clone` of an empty repository succeeds and lands on no commit, so
    there is no clone base -- but that is KNOWN, unlike a base a resumed
    attempt lost. Everything the agent did is the agent's, and the worker
    replaces it with one commit of its own that has no parent."""
    worker, config, task = _attempt(
        worker_factory, monkeypatch, empty_origin,
        task_id=f"t-empty-{edit.__name__.split('_')[1]}", attempt=1,
        dispatch={"strategy": "direct-pr"},
    )
    cloned = worker._maybe_clone(task)
    assert cloned is not None and cloned["commit"] is None, "the repository was not empty"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    edit(repo)
    expected = {p.name for p in repo.iterdir() if p.name != ".git"}

    out = worker._harvest_git(publish=True)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    commits = pushed_commits(empty_origin, branch, since=None)
    assert len(commits) == 1 and commits[0]["parents"] == [], (
        f"expected one parentless commit, got {[c['parents'] for c in commits]}"
    )
    assert_only_the_worker_wrote(commits, config)
    assert expected <= tree_at(empty_origin, branch)
