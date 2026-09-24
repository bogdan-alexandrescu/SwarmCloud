"""`scripts/lib/check-frozen-contract.sh` judges THIS pull request's change.

Not the PR's change plus everything `main` gained since the PR was opened.

THE DEFECT THIS PINS. application.yml's frozen-contract step compared
`github.event.pull_request.base.sha` with the merge commit it checked out.
GitHub does not move `base.sha` when main moves, or when the PR gets new
pushes. So the diff covered every commit that had landed on main since the PR
was opened, and the PR on top. Measured on PR #44's own green run 36039462416
(python job 107767715382): the checkout was `Merge 9c56167 into d45b1d69`, and
the step compared it against 0fac6d6, the base when the PR was opened.

The consequence: once a PR with an accepted contract change lands (PR #44),
every PR opened before it that edits `swarm_common` WITHOUT acceptance is
passed on #44's acceptance lines, and every PR that never touched the contract
is told it changed `states.py`.

What a pull_request run checks out is `Merge <head> into <main now>`. Its FIRST
parent is main as it stands, and its second is the PR head. So the PR's change
as it would merge is the first parent against the merge commit.

THE PROPERTIES ASSERTED, against the real script over a real git repository
built the way GitHub builds that checkout:

  * a stale base.sha never lends a PR main's acceptance, and never charges it
    with main's contract change;
  * a PR's own recorded acceptance is what its change is credited with;
  * anything that is not the PR's merge commit (a single-parent checkout, a
    merge of some other head) fails closed rather than passing;
  * a depth-1 checkout, which is what actions/checkout makes, has its first
    parent fetched; a first parent that cannot be fetched fails the step.

WHAT THIS CANNOT PROVE: that GitHub's merge commit keeps main as its first
parent. That was read from run 36039462416's log (`Merge <head> into <base>`
is `git merge <head>` run on the base) and from the run of the fix, whose step
prints both parents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "check-frozen-contract.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="the frozen-contract check needs git and bash",
)

FROZEN = "apps/common/swarm_common"
REQUESTS = "docs/contract-change-requests.md"

#: What PR #44 records on main. A PR that did not write it is never credited with it.
MAINS_ACCEPTANCE = (
    "**Status: ACCEPTED — accepted by the owner in session, 2026-09-24 (request 17, on main)**"
)
#: What a PR records when its own contract change is accepted.
OWN_ACCEPTANCE = (
    "**Status: ACCEPTED — accepted by the owner in session, 2026-09-25 (request 13, this PR)**"
)

#: Request 13 and request 17 far enough apart that editing both status lines
#: merges cleanly: main edits one, the pull request edits the other.
REQUESTS_BEFORE = "\n".join(
    [
        "# Contract change requests",
        "",
        "## 13. `models.py`: `Attempt` does not record which pool account it ran on",
        "",
        "**Status: open (request 13)**",
        "",
        "Why an event is not enough.",
        "",
        "The requested change.",
        "",
        "What it would break.",
        "",
        "## 17. `states.py`: a cancel that is only requested is recorded as `cancelled`",
        "",
        "**Status: open (request 17)**",
        "",
    ]
)


def _env() -> dict[str, str]:
    """Hermetic git: no user or system config, a fixed identity."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "NO_COLOR": "1",
        }
    )
    return env


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=_env()
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


class Fixture:
    """A repository with main, one pull request, and the merge GitHub checks out."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        # So a depth-1 clone can fetch a commit by id, as actions/checkout's
        # clone can from GitHub.
        git(root, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.write(f"{FROZEN}/models.py", "class Attempt:\n    attempt_id: str\n")
        self.write(f"{FROZEN}/profiles.py", "class RunnerProfile:\n    runner_argv: tuple\n")
        self.write(f"{FROZEN}/states.py", "CANCELLED = 'cancelled'\n")
        self.write(REQUESTS, REQUESTS_BEFORE)
        self.write("README.md", "the repository\n")
        #: main when the pull request was opened: `github.event.pull_request.base.sha`.
        self.opened = self.commit("main when the pull request was opened")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def replace(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text()
        assert old in text, f"{old!r} is not in {rel}"
        path.write_text(text.replace(old, new))

    def commit(self, message: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def main_lands_an_accepted_contract_change(self) -> str:
        """What PR #44 did to main: a swarm_common edit and its recorded acceptance."""
        git(self.root, "checkout", "-q", "main")
        self.write(f"{FROZEN}/states.py", "CANCELLED = 'cancelled'\nCANCEL_REQUESTED = 'cancel_requested'\n")
        self.replace(REQUESTS, "**Status: open (request 17)**", MAINS_ACCEPTANCE)
        return self.commit("contract request 17 lands on main")

    def pull_request(
        self, *, touches_the_contract: bool, records_acceptance: bool = False, from_: str
    ) -> str:
        git(self.root, "checkout", "-q", "--detach", from_)
        self.write("README.md", "the repository, edited by the pull request\n")
        if touches_the_contract:
            self.write(f"{FROZEN}/models.py", "class Attempt:\n    attempt_id: str\n    pool_account: str\n")
        if records_acceptance:
            self.replace(REQUESTS, "**Status: open (request 13)**", OWN_ACCEPTANCE)
        return self.commit("the pull request")

    def merge(self, head: str, *, into: str) -> str:
        """The commit a pull_request run checks out: first parent main, second the PR head."""
        git(self.root, "checkout", "-q", "--detach", into)
        git(self.root, "merge", "-q", "--no-ff", "--no-edit", "-m", f"Merge {head} into {into}", head)
        return git(self.root, "rev-parse", "HEAD")

    def depth_one_clone(self, merge: str, dest: Path) -> Path:
        """What actions/checkout makes: the merge commit alone, its parents absent."""
        git(self.root, "branch", "-f", "pr-merge", merge)
        done = subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--branch", "pr-merge", f"file://{self.root}", str(dest)],
            capture_output=True,
            text=True,
            env=_env(),
        )
        assert done.returncode == 0, done.stderr
        return dest


def check(repo: Path, *, pr_base: str, pr_head: str) -> tuple[int, str]:
    done = subprocess.run(
        ["bash", str(SCRIPT), "--repo", str(repo), "--pr-base", pr_base, "--pr-head", pr_head],
        capture_output=True,
        text=True,
        env=_env(),
    )
    return done.returncode, done.stdout + done.stderr


@pytest.fixture()
def repo(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path / "repo")


# --------------------------------------------------------------------------
# A stale base.sha: the PR was opened before main landed an accepted change
# --------------------------------------------------------------------------


def test_a_stale_base_does_not_lend_a_pull_request_mains_acceptance(repo):
    """The failure scenario the review named: request 13 is NOT accepted.

    A PR opened before #44 edits models.py for it and records nothing. #44
    lands. The PR's next run must still refuse it, and must not print #44's
    acceptance as the grounds for a change #44 never saw.
    """
    pr = repo.pull_request(touches_the_contract=True, from_=repo.opened)
    main_now = repo.main_lands_an_accepted_contract_change()
    repo.merge(pr, into=main_now)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=pr)

    assert code == 1, f"an unaccepted contract change was passed:\n{out}"
    assert "models.py" in out, out
    assert MAINS_ACCEPTANCE not in out, (
        f"the PR was credited with an acceptance main recorded for another change:\n{out}"
    )
    assert "states.py" not in out, f"main's contract change was charged to this PR:\n{out}"


def test_a_pull_request_that_never_touched_the_contract_is_not_charged_with_mains_change(repo):
    """The nine PRs that were open when #44 was about to land touch no contract file."""
    pr = repo.pull_request(touches_the_contract=False, from_=repo.opened)
    main_now = repo.main_lands_an_accepted_contract_change()
    repo.merge(pr, into=main_now)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=pr)

    assert code == 0, out
    assert "frozen contract untouched" in out, out
    assert "states.py" not in out, f"main's contract change was charged to this PR:\n{out}"
    assert MAINS_ACCEPTANCE not in out, out


def test_a_pull_requests_own_acceptance_is_what_its_change_rests_on(repo):
    pr = repo.pull_request(touches_the_contract=True, records_acceptance=True, from_=repo.opened)
    main_now = repo.main_lands_an_accepted_contract_change()
    repo.merge(pr, into=main_now)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=pr)

    assert code == 0, out
    assert OWN_ACCEPTANCE in out, out
    assert MAINS_ACCEPTANCE not in out, (
        f"the change was shown resting on an acceptance this PR did not record:\n{out}"
    )


# --------------------------------------------------------------------------
# The same answers when main has not moved -- the fixture itself is sound
# --------------------------------------------------------------------------


def test_an_unaccepted_change_fails_when_main_has_not_moved(repo):
    pr = repo.pull_request(touches_the_contract=True, from_=repo.opened)
    repo.merge(pr, into=repo.opened)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=pr)

    assert code == 1, out
    assert "models.py" in out, out
    assert "FROZEN" in out, out


def test_an_untouched_contract_passes_when_main_has_not_moved(repo):
    pr = repo.pull_request(touches_the_contract=False, from_=repo.opened)
    repo.merge(pr, into=repo.opened)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=pr)

    assert code == 0, out
    assert "frozen contract untouched" in out, out


# --------------------------------------------------------------------------
# Anything that is not the PR's merge commit fails closed
# --------------------------------------------------------------------------


def test_a_checkout_that_is_not_a_merge_commit_fails_closed(repo):
    """A single-parent HEAD has no "main as it stands" to compare against.

    Checking out the PR head instead of the merge (a `ref:` on the checkout
    step) would make the first parent the PR's own previous commit, and the
    diff one commit of the PR. That must stop the step, not pass it.
    """
    pr = repo.pull_request(touches_the_contract=False, from_=repo.opened)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=pr)

    assert code != 0, f"a single-parent checkout was judged as though it were the PR:\n{out}"
    assert "frozen contract untouched" not in out, out
    assert "merge commit" in out, out


def test_a_merge_of_some_other_head_fails_closed(repo):
    pr = repo.pull_request(touches_the_contract=False, from_=repo.opened)
    main_now = repo.main_lands_an_accepted_contract_change()
    repo.merge(pr, into=main_now)

    code, out = check(repo.root, pr_base=repo.opened, pr_head=repo.opened)

    assert code != 0, f"a merge of something other than the PR head was passed:\n{out}"
    assert "frozen contract untouched" not in out, out


# --------------------------------------------------------------------------
# A depth-1 checkout, which is what the job actually has
# --------------------------------------------------------------------------


def test_a_depth_one_checkout_fetches_the_first_parent_and_compares_against_it(repo, tmp_path):
    pr = repo.pull_request(touches_the_contract=False, from_=repo.opened)
    main_now = repo.main_lands_an_accepted_contract_change()
    merge = repo.merge(pr, into=main_now)
    clone = repo.depth_one_clone(merge, tmp_path / "checkout")
    # The premise: the first parent is NOT in the checkout, so the fetch path runs.
    absent = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{main_now}^{{commit}}"],
        capture_output=True,
        env=_env(),
    )
    assert absent.returncode != 0, "the depth-1 clone already holds the first parent"

    code, out = check(clone, pr_base=repo.opened, pr_head=pr)

    assert code == 0, out
    assert "frozen contract untouched" in out, out
    assert "states.py" not in out, out


def test_a_first_parent_that_cannot_be_fetched_fails_closed(repo, tmp_path):
    pr = repo.pull_request(touches_the_contract=False, from_=repo.opened)
    main_now = repo.main_lands_an_accepted_contract_change()
    merge = repo.merge(pr, into=main_now)
    clone = repo.depth_one_clone(merge, tmp_path / "checkout")
    git(clone, "remote", "set-url", "origin", f"file://{tmp_path / 'no-such-remote'}")

    code, out = check(clone, pr_base=repo.opened, pr_head=pr)

    assert code != 0, f"a comparison that could not be made was passed:\n{out}"
    assert "frozen contract untouched" not in out, out
