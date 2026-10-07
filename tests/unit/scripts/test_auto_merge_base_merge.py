"""A new head drops `ready` and really turns auto-merge off -- unless it only merges the base (#795).

Measured 2026-10-07: `auto-merge.yml`'s `disable` job stripped `ready` on a
push but its `gh pr merge --disable-auto || true` failed with "Resource not
accessible by integration" and the `|| true` hid it, so 17 of the 24 pull
requests merged that day merged with no label, on an auto-merge that survived
the push. And the `enable` job, queued 400-600 s behind a runner, failed with
"expected head oid does not match" whenever the branch was updated meanwhile,
so auto-merge never turned on.

Owner decision, 2026-10-07:

* a PURE BASE MERGE -- a merge commit whose first parent is the head that
  carried `ready`, whose second parent is already on the base branch, and
  whose tree is exactly the merge of the two (what GitHub's "Update branch"
  makes) -- keeps `ready` and auto-merge;
* any other push really disables auto-merge, verified, and strips `ready`;
  a disable that fails fails the job;
* the enable job, refused because the head moved by a pure base merge while
  it was queued, arms once more at the new head; a head moved by anything
  else is never armed.

Every shell here is RUN: the classifier against a real git history in a
scratch `file://` repository, the `gh` calls against a fake that records them.
Nothing touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
AUTO_MERGE = WORKFLOWS / "auto-merge.yml"
CI_GATE_SCRIPT = REPO / "scripts" / "ci-gate.sh"
RUNBOOK = REPO / "docs" / "runbooks" / "merge-app.md"

PR = "4242"
# Which identity made each `gh` call. Plain words, not token-shaped: the fake
# logs them, and the tests read which one turned auto-merge off.
WORKFLOW_IDENTITY = "the-workflow-identity"
APP_IDENTITY = "the-merge-app-identity"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or shutil.which("git") is None,
    reason="bash, jq and git are required",
)


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _workflow(AUTO_MERGE)


@pytest.fixture(scope="module")
def enable_job(workflow: dict) -> dict:
    return workflow["jobs"]["enable"]


@pytest.fixture(scope="module")
def disable_job(workflow: dict) -> dict:
    return workflow["jobs"]["disable"]


def _step(job: dict, step_id: str) -> dict:
    matches = [step for step in job.get("steps") or [] if step.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}, found {len(matches)}"
    return matches[0]


def _fake(tmp_path: Path, text: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "gh"
    fake.write_text(text)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return bindir


def _outputs(path: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return outputs


# ---------------------------------------------------------------------------
# A real history: what "Update branch" makes, and what only looks like it
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout.strip()


def _commit(work: Path, name: str, text: str) -> str:
    (work / name).write_text(text)
    _git(work, "add", name)
    _git(work, "commit", "-q", "-m", f"change {name}")
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def history(tmp_path_factory) -> dict[str, str]:
    """`labelled` is the head that carried `ready`; main has moved on since.

    * pure         -- `git merge --no-ff main` on labelled: Update branch.
    * evil         -- the same two parents, plus a change of its own.
    * plain        -- an ordinary commit on labelled.
    * side_merge   -- labelled merged with a branch that is not on main.
    * stale_parent -- main merged into `plain`, not into the labelled head.
    """
    root = tmp_path_factory.mktemp("history")
    origin = root / "origin.git"
    _git(root, "init", "-q", "--bare", str(origin))
    _git(origin, "config", "uploadpack.allowFilter", "true")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    work = root / "work"
    _git(root, "init", "-q", "-b", "main", str(work))
    shas: dict[str, str] = {}
    shas["root"] = _commit(work, "a.txt", "one\n")
    _git(work, "checkout", "-q", "-b", "side")
    shas["side"] = _commit(work, "side.txt", "not on main\n")
    _git(work, "checkout", "-q", "main")
    _git(work, "checkout", "-q", "-b", "feature")
    shas["labelled"] = _commit(work, "b.txt", "the reviewed change\n")
    _git(work, "checkout", "-q", "main")
    shas["main"] = _commit(work, "c.txt", "main moved on\n")

    _git(work, "checkout", "-q", "feature")
    _git(work, "merge", "-q", "--no-ff", "main", "-m", "Merge branch 'main' into feature")
    shas["pure"] = _git(work, "rev-parse", "HEAD")

    (work / "b.txt").write_text("a change nobody reviewed\n")
    _git(work, "add", "b.txt")
    _git(work, "commit", "-q", "--amend", "--no-edit")
    shas["evil"] = _git(work, "rev-parse", "HEAD")

    _git(work, "reset", "-q", "--hard", shas["labelled"])
    shas["plain"] = _commit(work, "d.txt", "another commit\n")
    _git(work, "merge", "-q", "--no-ff", "main", "-m", "Merge branch 'main' into feature")
    shas["stale_parent"] = _git(work, "rev-parse", "HEAD")

    _git(work, "reset", "-q", "--hard", shas["labelled"])
    _git(work, "merge", "-q", "--no-ff", "side", "-m", "Merge branch 'side' into feature")
    shas["side_merge"] = _git(work, "rev-parse", "HEAD")

    _git(work, "push", "-q", str(origin), "main")
    for name in ("pure", "evil", "plain", "stale_parent", "side_merge", "side"):
        _git(work, "push", "-q", str(origin), f"{shas[name]}:refs/tests/{name}")
    # Sanity: the evil merge has the pure merge's parents and a different tree.
    assert _git(work, "rev-parse", f"{shas['evil']}^1", f"{shas['evil']}^2") == _git(
        work, "rev-parse", f"{shas['pure']}^1", f"{shas['pure']}^2"
    )
    assert _git(work, "rev-parse", f"{shas['evil']}^{{tree}}") != _git(work, "rev-parse", f"{shas['pure']}^{{tree}}")
    shas["remote"] = origin.as_uri()
    return shas


# ---------------------------------------------------------------------------
# The disable job: classify, then (unless pure) disarm and strip
# ---------------------------------------------------------------------------


def _run_classify(disable_job: dict, tmp_path: Path, history: dict, *, after: str, action: str = "synchronize",
                  before: str | None = None):
    script = _step(disable_job, "classify")["run"]
    bindir = _fake(tmp_path, "#!/usr/bin/env bash\necho \"fake gh: unexpected call: $*\" >&2\nexit 3\n")
    output = tmp_path / "output.txt"
    summary = tmp_path / "summary.md"
    for path in (output, summary):
        path.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "ACTION": action,
        "BEFORE": before if before is not None else history["labelled"],
        "AFTER": after,
        "BASE_REF": "main",
        "REMOTE_URL": history["remote"],
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False)
    return proc, _outputs(output)


def test_an_update_branch_merge_is_a_pure_base_merge(disable_job: dict, tmp_path: Path, history: dict):
    """The control for every refusal below: without it, a classifier that
    says "no" to everything passes them all."""
    proc, outputs = _run_classify(disable_job, tmp_path, history, after=history["pure"])
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("pure_base_merge") == "true", (outputs, proc.stdout, proc.stderr)


@pytest.mark.parametrize(
    "after, why",
    [
        ("evil", "the right parents, but a tree with a change of its own"),
        ("plain", "an ordinary commit"),
        ("side_merge", "a merge of a branch that is not on main"),
        ("stale_parent", "main merged into a commit that is not the labelled head"),
    ],
)
def test_any_other_push_is_not_a_pure_base_merge(disable_job: dict, tmp_path: Path, history: dict, after: str, why: str):
    """MUTATION: drop the tree comparison (evil passes), the first-parent
    check (stale_parent passes) or the is-on-main check (side_merge passes)."""
    proc, outputs = _run_classify(disable_job, tmp_path, history, after=history[after])
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("pure_base_merge") == "false", (why, outputs, proc.stdout, proc.stderr)


@pytest.mark.parametrize("action", ["reopened", "edited"])
def test_a_reopen_or_a_base_change_is_never_a_pure_base_merge(disable_job: dict, tmp_path: Path, history: dict,
                                                            action: str):
    """Only `synchronize` moves the head by a push; the other two always drop `ready`."""
    proc, outputs = _run_classify(disable_job, tmp_path, history, after=history["pure"], action=action)
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("pure_base_merge") == "false", outputs


def test_a_head_that_cannot_be_read_is_treated_as_any_other_push(disable_job: dict, tmp_path: Path, history: dict):
    """Could not tell is not "pure": the safe answer drops `ready`, and says why."""
    proc, outputs = _run_classify(disable_job, tmp_path, history, after="f" * 40)
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("pure_base_merge") == "false", outputs
    assert "::warning::" in proc.stdout, proc.stdout


def test_the_disable_job_acts_only_when_the_push_is_not_a_pure_base_merge(disable_job: dict):
    """A pure base merge does nothing at all: no token, no disable, no strip.
    Every other outcome -- including a classify step that failed -- does all
    three, and the strip runs even after a failed disable, so a failure leaves
    no stale `ready` behind it.
    MUTATION: drop the classify condition from any of the three steps, or the
    `!cancelled()` that keeps the strip after a failed disable."""
    ids = [step.get("id") for step in disable_job["steps"]]
    assert ids == ["classify", "app-token", "disarm", "strip"], ids
    not_pure = "steps.classify.outputs.pure_base_merge != 'true'"
    for step_id in ("app-token", "disarm", "strip"):
        condition = str(_step(disable_job, step_id).get("if") or "")
        assert not_pure in condition and "!cancelled()" in condition, (step_id, condition)
    assert "vars.MERGE_APP_ID != ''" in str(_step(disable_job, "app-token").get("if"))


def test_the_disable_job_reads_the_push_from_the_event_through_env(disable_job: dict):
    env = _step(disable_job, "classify").get("env") or {}
    assert env.get("ACTION") == "${{ github.event.action }}", env
    assert env.get("BEFORE") == "${{ github.event.before }}", env
    assert env.get("AFTER") == "${{ github.event.after }}", env
    assert env.get("BASE_REF") == "${{ github.event.pull_request.base.ref }}", env
    for step in disable_job["steps"]:
        assert "${{" not in str(step.get("run") or ""), step.get("id")


def test_the_disable_job_never_checks_out_and_holds_the_least_it_needs(disable_job: dict):
    """contents: read is the classifier's fetch (commits and trees, never a
    working tree); pull-requests: write the label and the comment. The disable
    itself is the App's (#795). MUTATION: add a checkout, or widen the token."""
    assert disable_job.get("permissions") == {"contents": "read", "pull-requests": "write"}
    for step in disable_job["steps"]:
        assert "checkout" not in str(step.get("uses") or ""), step


# ---------------------------------------------------------------------------
# The disable itself: with the App's token, verified, loud on failure
# ---------------------------------------------------------------------------


FAKE_GH_DISARM = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s [as %s]\n' "$*" "${GH_TOKEN:-}" >> "${FAKE_GH_LOG}"
case "$1 $2" in
  "pr view")
    if [[ -f "${FAKE_GH_STATE}" ]]; then cat "${FAKE_GH_STATE}"; else echo '{"autoMergeRequest":null}'; fi
    exit 0 ;;
  "pr merge")
    [[ "$*" == "pr merge ${PR_NUMBER} --disable-auto" ]] || { echo "fake gh: unexpected merge: $*" >&2; exit 3; }
    if [[ "${GH_TOKEN:-}" != "${FAKE_GH_CAN_DISABLE}" ]]; then
      echo "GraphQL: Resource not accessible by integration (disablePullRequestAutoMerge)" >&2
      exit 1
    fi
    if [[ "${FAKE_GH_STICKS:-}" != "true" ]]; then rm -f "${FAKE_GH_STATE}"; fi
    exit 0 ;;
esac
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


def _disarm_script(job: dict) -> str:
    return _step(job, "disarm")["run"]


@pytest.fixture(params=["enable", "disable"])
def run_disarm(request, workflow: dict, tmp_path: Path):
    """The same step in both jobs: the disable job's on a push, the enable
    job's after a refusal on re-evaluation."""
    script = _disarm_script(workflow["jobs"][request.param])
    bindir = _fake(tmp_path, FAKE_GH_DISARM)

    def run(*, armed: bool = True, app: str | None = APP_IDENTITY, can_disable: str = APP_IDENTITY,
            sticks: bool = False):
        log = tmp_path / "gh.log"
        log.write_text("")
        state = tmp_path / "state.json"
        if armed:
            state.write_text(json.dumps({"autoMergeRequest": {"mergeMethod": "SQUASH"}}))
        elif state.exists():
            state.unlink()
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "PR_NUMBER": PR,
            "GH_TOKEN": WORKFLOW_IDENTITY,
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_STATE": str(state),
            "FAKE_GH_CAN_DISABLE": can_disable,
            "FAKE_GH_STICKS": "true" if sticks else "false",
        }
        if app is not None:
            env["APP_TOKEN"] = app
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)
        return proc, log.read_text().splitlines()

    return run


def test_an_armed_auto_merge_is_turned_off_with_the_app_token_and_verified(run_disarm):
    """The GITHUB_TOKEN got "Resource not accessible by integration"; the
    App that armed it turns it off. MUTATION: disable with the GITHUB_TOKEN
    (the fake refuses it, as GitHub did), or skip the read-back."""
    proc, calls = run_disarm()
    assert proc.returncode == 0, proc.stderr
    assert f"pr merge {PR} --disable-auto [as {APP_IDENTITY}]" in calls, calls
    views = [c for c in calls if c.startswith("pr view")]
    assert len(views) == 2, f"read before and read back after: {calls}"


def test_a_disable_that_fails_fails_the_job_loudly(run_disarm):
    """The `|| true` that hid the failure for #795. MUTATION: put it back."""
    proc, calls = run_disarm(can_disable="nobody")
    assert proc.returncode != 0, calls
    assert "::error::" in proc.stdout, proc.stdout
    assert "Resource not accessible by integration" in proc.stderr, proc.stderr


def test_a_disable_github_did_not_apply_fails_the_job(run_disarm):
    """Exit 0 is not "off": the read-back is the proof. MUTATION: drop it."""
    proc, _calls = run_disarm(sticks=True)
    assert proc.returncode != 0
    assert "::error::" in proc.stdout, proc.stdout


def test_an_armed_auto_merge_without_the_app_fails_rather_than_staying_armed_quietly(run_disarm):
    proc, calls = run_disarm(app="")
    assert proc.returncode != 0
    assert "::error::" in proc.stdout, proc.stdout
    assert not [c for c in calls if c.startswith("pr merge")], calls


def test_nothing_armed_is_nothing_to_turn_off(run_disarm):
    """A push that raced the enable job, or a label whose gate still waits."""
    proc, calls = run_disarm(armed=False, app=None)
    assert proc.returncode == 0, proc.stderr
    assert not [c for c in calls if c.startswith("pr merge")], calls


def test_the_two_disarm_steps_are_one_script(enable_job: dict, disable_job: dict):
    """One rule in two places drifts (CLAUDE.md). MUTATION: edit one copy."""
    assert _disarm_script(enable_job) == _disarm_script(disable_job)
    for job in (enable_job, disable_job):
        env = _step(job, "disarm").get("env") or {}
        assert env.get("GH_TOKEN") == "${{ github.token }}", env
        assert env.get("APP_TOKEN") == "${{ steps.app-token.outputs.token }}", env


def test_no_disable_in_the_workflow_hides_its_failure():
    text = AUTO_MERGE.read_text()
    assert "--disable-auto" in text
    assert not re.search(r"--disable-auto[^\n]*\|\|\s*true", text), "a disable failure is hidden again"


def test_the_disable_job_mints_the_app_token_without_the_workflows_permission(disable_job: dict):
    """Turning auto-merge off writes no file, so it asks for no `workflows`."""
    token = _step(disable_job, "app-token")
    assert str(token.get("uses", "")).startswith("actions/create-github-app-token@"), token
    inputs = token.get("with") or {}
    assert inputs.get("permission-contents") == "write", inputs
    assert inputs.get("permission-pull-requests") == "write", inputs
    assert "permission-workflows" not in inputs, inputs


FAKE_GH_STRIP = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "$1 $2" == "pr comment" ]]; then
  shift 2
  while [[ $# -gt 0 ]]; do
    case "$1" in --body-file) cat "$2" >> "${FAKE_GH_COMMENTS}"; shift 2 ;; *) shift ;; esac
  done
  exit 0
fi
[[ "$*" == "pr edit ${PR_NUMBER} --remove-label ready" ]] && exit 0
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.mark.parametrize("disarm, said", [("success", "turned off auto-merge"),
                                          ("skipped", "turned off auto-merge"),
                                          ("failure", "could not be turned off")])
def test_any_other_push_strips_ready_and_says_whether_auto_merge_is_off(disable_job: dict, tmp_path: Path,
                                                                        disarm: str, said: str):
    """MUTATION: drop the label removal, or claim it is off after a failed disable."""
    script = _step(disable_job, "strip")["run"]
    env_spec = _step(disable_job, "strip").get("env") or {}
    assert env_spec.get("DISARM_OUTCOME") == "${{ steps.disarm.outcome }}", env_spec
    bindir = _fake(tmp_path, FAKE_GH_STRIP)
    log = tmp_path / "gh.log"
    comments = tmp_path / "comments.md"
    for path in (log, comments):
        path.write_text("")
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "PR_NUMBER": PR,
        "DISARM_OUTCOME": disarm,
        "FAKE_GH_LOG": str(log),
        "FAKE_GH_COMMENTS": str(comments),
    }
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert f"pr edit {PR} --remove-label ready" in log.read_text()
    assert said in comments.read_text(), comments.read_text()


# ---------------------------------------------------------------------------
# The enable job's disarm after a refusal on re-evaluation
# ---------------------------------------------------------------------------


def test_a_re_evaluation_refusal_disarms_with_the_app_token(enable_job: dict):
    """The gate's own `--disable-auto || true` failed the same way. It now
    says `disarm`, and the App token is minted for that as well as the merge.
    MUTATION: mint only on `merge`, or call the disable from the gate."""
    assert "--disable-auto" not in _step(enable_job, "gate")["run"]
    assert _step(enable_job, "disarm").get("if") == "steps.gate.outputs.disarm == 'true'"
    mint = str(_step(enable_job, "app-token").get("if"))
    assert "steps.gate.outputs.decision == 'merge'" in mint and "steps.gate.outputs.disarm == 'true'" in mint, mint
    ids = [step.get("id") for step in enable_job["steps"]]
    assert ids.index("app-token") < ids.index("disarm") < ids.index("merge"), ids


# ---------------------------------------------------------------------------
# The enable job: a head moved while it was queued
# ---------------------------------------------------------------------------


FAKE_GH_ENABLE = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
if [[ "$1 $2" == "pr merge" ]]; then
  pinned=""
  prev=""
  for arg in "$@"; do
    [[ "${prev}" == "--match-head-commit" ]] && pinned="${arg}"
    prev="${arg}"
  done
  if [[ "${pinned}" != "${FAKE_GH_CURRENT_HEAD}" || "${FAKE_GH_MOVES_AGAIN:-}" == "true" ]]; then
    echo "GraphQL: expected head oid does not match the pull request's head (enablePullRequestAutoMerge)" >&2
    exit 1
  fi
  exit 0
fi
if [[ "$1 $2" == "pr view" ]]; then
  jq -n --arg head "${FAKE_GH_CURRENT_HEAD}" \
    '{id: "PR_example", state: "OPEN", mergeStateStatus: "BLOCKED", autoMergeRequest: null, headRefOid: $head}'
  exit 0
fi
if [[ "$1 $2" == "api graphql" ]]; then
  printf '%s\n' false
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


@pytest.fixture
def run_enable(enable_job: dict, tmp_path: Path, history: dict):
    script = _step(enable_job, "merge")["run"]
    bindir = _fake(tmp_path, FAKE_GH_ENABLE)

    def run(current: str, *, queue: str = "false", moves_again: bool = False):
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        for path in (log, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "PR_NUMBER": PR,
            "PR_TITLE": "A fact-style headline",
            "HEAD_SHA": history["labelled"],
            "BASE_REF": "main",
            "REMOTE_URL": history["remote"],
            "QUEUE": queue,
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_CURRENT_HEAD": history[current],
            "FAKE_GH_MOVES_AGAIN": "true" if moves_again else "false",
        }
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60, check=False)
        merges = [line for line in log.read_text().splitlines() if line.startswith("pr merge")]
        return proc, merges, summary.read_text()

    return run


def test_an_unmoved_head_is_armed_once_at_the_labelled_head(run_enable, history: dict):
    """The control: no head move, no second attempt."""
    proc, merges, summary = run_enable("labelled")
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1 and f"--match-head-commit {history['labelled']}" in merges[0], merges
    assert "Queued for auto-merge" in summary, summary


@pytest.mark.parametrize("queue", ["false", "true"])
def test_a_head_moved_by_a_pure_base_merge_is_armed_at_the_new_head(run_enable, history: dict, queue: str):
    """The #782/#784 stall: update-branch while the enable job was queued.
    MUTATION: drop the retry, or retry without the classifier."""
    proc, merges, summary = run_enable("pure", queue=queue)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert len(merges) == 2, merges
    assert f"--match-head-commit {history['labelled']}" in merges[0], merges
    assert f"--match-head-commit {history['pure']}" in merges[1] and "--auto" in merges[1], merges
    assert "Queued for auto-merge" in summary, summary


@pytest.mark.parametrize("current", ["plain", "evil", "side_merge", "stale_parent"])
def test_a_head_moved_by_anything_else_is_never_armed(run_enable, current: str):
    """The new head was never labelled: the disable job strips `ready` for it.
    MUTATION: retry on any head move."""
    proc, merges, summary = run_enable(current)
    assert proc.returncode == 0, proc.stderr
    assert len(merges) == 1, merges
    assert "### Not queued for auto-merge" in summary and "### Queued" not in summary, summary


def test_the_retry_happens_once(run_enable):
    """A head that moves again is not chased: the run fails as any refused
    arming does. MUTATION: loop on the retry."""
    proc, merges, _summary = run_enable("pure", moves_again=True)
    assert proc.returncode != 0
    assert len(merges) == 2, merges


def test_the_classifier_is_one_function_in_both_jobs(enable_job: dict, disable_job: dict):
    """MUTATION: edit one copy of is_pure_base_merge."""
    pattern = re.compile(r"^is_pure_base_merge\(\) \{\n.*?^\}\n", re.S | re.M)
    enable = pattern.search(_step(enable_job, "merge")["run"])
    disable = pattern.search(_step(disable_job, "classify")["run"])
    assert enable and disable, "is_pure_base_merge() is defined in both steps"
    assert enable.group(0) == disable.group(0)
    for step in (_step(enable_job, "merge"), _step(disable_job, "classify")):
        env = step.get("env") or {}
        assert env.get("REMOTE_URL") == "${{ github.server_url }}/${{ github.repository }}.git", env


# ---------------------------------------------------------------------------
# Fan-out: re-evaluate only on the completions that can be the last
# ---------------------------------------------------------------------------


def _gated_workflow_files() -> set[str]:
    match = re.search(r'^GATED_WORKFLOWS="\$\{CI_GATE_WORKFLOWS:-([^}]*)\}"', CI_GATE_SCRIPT.read_text(), re.M)
    assert match, "scripts/ci-gate.sh no longer names the workflows it waits for"
    return set(match.group(1).split())


def test_completions_ci_gate_waits_for_do_not_start_a_run(workflow: dict):
    """ci-gate completes only after every application and terraform run at its
    head has completed, so their own completions can never be the last one:
    each started an auto-merge run for nothing (294+ runs in one night, #795).
    What is left is ci-gate itself and every pull-request workflow it does not
    gate. MUTATION: add `application` or `terraform` back, or drop `security`."""
    gated = _gated_workflow_files()
    assert gated == {"application.yml", "terraform.yml"}, gated  # the control
    pull_request_names: dict[str, str] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        data = _workflow(path)
        on = data.get("on") or {}
        if "pull_request" in (on if isinstance(on, (dict, list)) else {on}):
            pull_request_names[path.name] = data["name"]
    expected = {name for file, name in pull_request_names.items() if file not in gated}
    assert "ci-gate" in expected and "security" in expected, expected
    listened = set(workflow["on"]["workflow_run"]["workflows"])
    assert listened == expected, listened
    assert workflow["on"]["workflow_run"]["types"] == ["completed"]


def test_the_runbook_says_why_the_app_turns_auto_merge_off():
    text = RUNBOOK.read_text()
    assert "#795" in text
    assert "--disable-auto" in text and "Resource not accessible by integration" in text
    assert "pure base merge" in text.lower()
