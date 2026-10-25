"""The git-tools acceptance group's judge, offline (G7, part of #1225).

tests/acceptance/test_runner_git_tools.py judges what scripts/acceptance/
groups/git-tools.sh saved from a deployed run. These pin, with no deployment:

* each of the five checks PASSES on the evidence a working platform leaves,
  and FAILS on each defect it exists to catch -- a push that is not a
  lease-guarded rewrite, a pr_checks read at the wrong head, a forge
  credential in the runner's environment, a push swarm-api accepted, a branch
  that moved, a build step that SUCCEEDED with its push refused;
* a task the platform held on purpose is a SKIP, never a PASS;
* the environment report prints names, never values, and the owner's
  narrowing of `env | grep -i token` (CLAUDE_CODE_OAUTH_TOKEN allowed);
* the shell group, the Python, the fixture, sandbox-sync.sh and
  github-verify.sh agree on every name they share;
* github-verify.sh reads the rebase lane's pull request back as rebased and
  resolved, and fails it when it is not.

Every fake credential is built at run time (CLAUDE.md, "Nothing you add may
look like a credential").
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from .test_acceptance_target import FAKE_GH_CURL, SANDBOX, _clean_env, _gh_key, _pr

ROOT = Path(__file__).resolve().parents[3]
JUDGE_PATH = ROOT / "tests" / "acceptance" / "test_runner_git_tools.py"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "git-tools.sh"
SYNC = ROOT / "scripts" / "acceptance" / "sandbox-sync.sh"
VERIFY = ROOT / "scripts" / "acceptance" / "github-verify.sh"
FIXTURE = ROOT / "tests" / "acceptance" / "fixtures" / "git-tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gt = _load("acceptance_runner_git_tools", JUDGE_PATH)
env_report = _load("acceptance_git_tools_env_report", FIXTURE / "env_report.py")

LANE = "task_" + "1a" * 10
REBASE = "task_" + "2b" * 10
READER = "task_" + "3c" * 10
BUILD = "task_" + "4d" * 10
BRANCH = f"swarm/{LANE}"
MAIN_SHA = "a" * 40
LANE_TIP = "b" * 40
REWRITTEN = "c" * 40
BUILD_TIP = "d" * 40
READ_SHA = "e" * 40
PR = 42


def _sha(n: int) -> str:
    return f"{n:040x}"


def _row(tool: str, n: int, *, result: str = "ok", code: str | None = None, **fields) -> dict:
    return {"tool": tool, "result": result, "code": code, "at": f"2026-10-14T10:00:{n:02d}+00:00",
            **fields}


def _answer(step: str, answer: dict) -> str:
    return json.dumps({"step": step, "answer": answer})


def _refused(code: str) -> dict:
    return {"refused": {"code": code, "message": "refused", "retryable": False}}


def _clean_env_report() -> dict:
    environ = {"PATH": "/usr/bin", "HOME": "/home/agent", "SWARM_GIT": "/work/.swarm-git",
               "GIT_AUTHOR_NAME": "swarmcloud agent", "GIT_COMMITTER_EMAIL": "x@users.noreply.github.com",
               "CLAUDE_CODE_OAUTH_TOKEN": "sk-" + "o" * 30}
    return env_report.report(environ)


def _world() -> dict[str, dict]:
    """The evidence a working platform leaves, role by role."""
    planted_sum = hashlib.sha256((gt.MAIN_LINE + "\n").encode()).hexdigest()
    return {
        "lane": {
            "task": LANE, "state": "SUCCEEDED",
            "doc": {"state": "SUCCEEDED", "result_summary": {"git": {"pull_request": {"number": PR}}}},
            "calls": [],
        },
        "rebase": {
            "task": REBASE, "state": "SUCCEEDED", "doc": {"state": "SUCCEEDED"},
            "calls": [
                _row("git_fetch", 1, ref=BRANCH, resolved_sha=LANE_TIP),
                _row("git_fetch", 2, ref="main", resolved_sha=MAIN_SHA),
                _row("git_push_branch", 3, branch=BRANCH, old_sha=LANE_TIP, new_sha=REWRITTEN, rewrite=True),
                _row("pr_checks", 4, pr=PR, resolved_sha=REWRITTEN),
            ],
            "answers": [
                _answer("fetch-own", {"tool": "git_fetch", "result": {"resolved_sha": LANE_TIP}}),
                _answer("rebase", {"tool": "git_rebase_onto", "result": {
                    "onto": MAIN_SHA, "head": None, "conflicts": [gt.PLANTED], "in_progress": True}}),
                _answer("push", {"tool": "git_push_branch", "result": {
                    "branch": BRANCH, "old_sha": LANE_TIP, "new_sha": REWRITTEN}}),
                _answer("pr-checks", {"tool": "pr_checks", "result": {"state": "pending", "rows": []}}),
            ],
            "env": _clean_env_report(),
        },
        "reader": {
            "task": READER, "state": "SUCCEEDED", "doc": {"state": "SUCCEEDED"},
            "calls": [
                _row("git_fetch", 1, ref="main", resolved_sha=MAIN_SHA),
                _row("git_fetch", 2, ref="main", resolved_sha=MAIN_SHA),
                _row("git_fetch", 3, ref=BRANCH, resolved_sha=LANE_TIP),
                _row("git_fetch", 4, ref=BRANCH, resolved_sha=LANE_TIP),
                _row("read_private", 5, repository=SANDBOX, ref="main", path=gt.PLANTED, resolved_sha=READ_SHA),
                _row("read_private", 6, repository=SANDBOX, ref=READ_SHA, path=gt.PLANTED, resolved_sha=READ_SHA),
                _row("read_private", 7, result="refused", code="repo_not_allowlisted",
                     repository=gt.UNLISTED_PREFIX + "run", ref="main", path="README.md"),
            ],
            "answers": [
                _answer("push-main", _refused("not_this_tasks_branch")),
                _answer("push-other", _refused("not_this_tasks_branch")),
                _answer("read-sha", {"tool": "read_private", "result": {
                    "kind": "file", "text": gt.MAIN_LINE + "\n", "resolved_sha": READ_SHA}}),
                _answer("read-unlisted", _refused("repo_not_allowlisted")),
            ],
            "read": {"repo": SANDBOX, "path": gt.PLANTED, "ref": "main", "expected_sha256": planted_sum},
        },
        "build": {
            "task": BUILD, "state": "FAILED",
            "doc": {"state": "FAILED", "last_error": "publish_failed: the step is an 'integrate' "
                    "contributor, whose branch ... and its branch was not pushed: non-fast-forward",
                    "result_summary": {"git": {"published": False, "publish_failed": True}}},
            "calls": [_row("git_push_branch", 1, branch=f"swarm/{BUILD}", new_sha=BUILD_TIP)],
            "answers": [_answer("push-first", {"tool": "git_push_branch", "result": {"new_sha": BUILD_TIP}})],
        },
    }


def _write(base: Path, world: dict[str, dict]) -> Path:
    for role, data in world.items():
        d = base / role
        d.mkdir(parents=True, exist_ok=True)
        (d / "task").write_text(data.get("task", "") + "\n")
        (d / "state").write_text(data.get("state", "") + "\n")
        if data.get("doc") is not None:
            (d / "doc.json").write_text(json.dumps(data["doc"]))
        if data.get("calls") is not None:
            # Served newest first, as the route serves them.
            (d / "calls.json").write_text(json.dumps({"calls": list(reversed(data["calls"]))}))
        if data.get("answers"):
            (d / gt.ANSWERS).write_text("\n".join(data["answers"]) + "\n")
        if data.get("env") is not None:
            (d / gt.ENV_REPORT_ARTIFACT).write_text(json.dumps(data["env"]))
        if role == "reader" and "read" in data:
            (base / "read.json").write_text(json.dumps(data["read"]))
    return base


def _verdicts(tmp_path: Path, world: dict[str, dict]) -> dict[str, tuple[str, str]]:
    return {check: (verdict, reason) for check, verdict, reason in gt.judge(str(_write(tmp_path, world)))}


# ---------------------------------------------------------------------------
# The five checks
# ---------------------------------------------------------------------------


def test_a_working_platform_passes_every_check(tmp_path):
    verdicts = _verdicts(tmp_path, _world())
    assert list(verdicts) == list(gt.CHECKS)
    for check, (verdict, reason) in verdicts.items():
        assert verdict == "PASS", f"{check}: {reason}"


def _mutate(case: str) -> tuple[dict, str]:
    """(a world with one defect, the check that must FAIL on it)."""
    w = _world()
    rebase, reader, build, lane = w["rebase"], w["reader"], w["build"], w["lane"]
    if case == "push-not-a-rewrite":
        rebase["calls"][2]["rewrite"] = None
        return w, "rebase"
    if case == "push-replaced-another-tip":
        rebase["calls"][2]["old_sha"] = _sha(9)
        return w, "rebase"
    if case == "no-push":
        rebase["calls"][2].update(result="refused", code="non_fast_forward")
        return w, "rebase"
    if case == "pr-checks-at-another-head":
        rebase["calls"][3]["resolved_sha"] = LANE_TIP
        return w, "rebase"
    if case == "pr-checks-another-pr":
        rebase["calls"][3]["pr"] = PR + 1
        return w, "rebase"
    if case == "no-conflict-met":
        rebase["answers"][1] = _answer("rebase", {"tool": "git_rebase_onto", "result": {"conflicts": []}})
        return w, "rebase"
    if case == "branch-tip-never-fetched":
        del rebase["calls"][0]
        return w, "rebase"
    if case == "gh-token-in-runner":
        rebase["env"]["names"].append("GH_TOKEN")
        return w, "rebase"
    if case == "env-unread":
        rebase["env"] = None
        return w, "rebase"
    if case == "lane-opened-no-pr":
        lane["doc"]["result_summary"] = {"git": {}}
        return w, "rebase"
    if case == "rebase-lane-failed":
        rebase.update(state="FAILED", doc={"state": "FAILED", "last_error": "publish_failed: x"})
        return w, "rebase"
    if case == "read-at-sha-missing":
        del reader["calls"][5]
        return w, "read_private"
    if case == "read-wrong-content":
        reader["answers"][2] = _answer("read-sha", {"tool": "read_private", "result": {"text": "other\n"}})
        return w, "read_private"
    if case == "unlisted-read-not-refused":
        reader["calls"][6].update(result="ok", code=None)
        return w, "read_private"
    if case == "main-push-not-refused":
        reader["answers"][0] = _answer("push-main", {"tool": "git_push_branch", "result": {"new_sha": _sha(5)}})
        return w, "push_main"
    if case == "main-moved":
        reader["calls"][1]["resolved_sha"] = _sha(6)
        return w, "push_main"
    if case == "api-accepted-a-push":
        reader["calls"].append(_row("git_push_branch", 9, branch="main", new_sha=_sha(7)))
        return w, "push_main"
    if case == "other-push-not-refused":
        reader["answers"][1] = _answer("push-other", _refused("non_fast_forward"))
        return w, "push_other"
    if case == "other-branch-moved-by-someone-else":
        reader["calls"][3]["resolved_sha"] = _sha(8)
        return w, "push_other"
    if case == "build-succeeded-without-its-branch":
        build.update(state="SUCCEEDED", doc={"state": "SUCCEEDED", "result_summary": {
            "git": {"published": False, "publish_reason": "push failed"}}})
        return w, "push_fails"
    if case == "build-push-succeeded":
        build.update(state="SUCCEEDED", doc={"state": "SUCCEEDED", "result_summary": {"git": {"published": True}}})
        return w, "push_fails"
    if case == "build-failed-for-another-reason":
        build["doc"]["last_error"] = "the agent exited 1"
        return w, "push_fails"
    if case == "build-mid-task-push-never-landed":
        build["calls"] = [_row("git_push_branch", 1, result="refused", code="foreign_author",
                               branch=f"swarm/{BUILD}")]
        return w, "push_fails"
    raise AssertionError(case)


CASES = [
    "push-not-a-rewrite", "push-replaced-another-tip", "no-push", "pr-checks-at-another-head",
    "pr-checks-another-pr", "no-conflict-met", "branch-tip-never-fetched", "gh-token-in-runner",
    "env-unread", "lane-opened-no-pr", "rebase-lane-failed", "read-at-sha-missing",
    "read-wrong-content", "unlisted-read-not-refused", "main-push-not-refused", "main-moved",
    "api-accepted-a-push", "other-push-not-refused", "other-branch-moved-by-someone-else",
    "build-succeeded-without-its-branch", "build-push-succeeded", "build-failed-for-another-reason",
    "build-mid-task-push-never-landed",
]


@pytest.mark.parametrize("case", CASES)
def test_each_check_fails_on_the_defect_it_exists_to_catch(tmp_path, case):
    world, check = _mutate(case)
    verdict, reason = _verdicts(tmp_path, world)[check]
    assert verdict == "FAIL", f"{case}: {check} read {verdict}: {reason}"


def test_the_branch_may_move_by_its_own_lane_s_audited_push(tmp_path):
    """The reader runs beside the rebase lane, which rewrites that very branch."""
    w = _world()
    w["reader"]["calls"][3]["resolved_sha"] = REWRITTEN
    verdict, reason = _verdicts(tmp_path, w)["push_other"]
    assert verdict == "PASS", reason


def test_a_push_to_main_may_be_refused_as_protected_or_as_not_this_task_s(tmp_path):
    w = _world()
    w["reader"]["answers"][0] = _answer("push-main", _refused("protected_branch"))
    assert _verdicts(tmp_path, w)["push_main"][0] == "PASS"


def test_the_build_step_ending_succeeded_names_the_regression(tmp_path):
    world, _ = _mutate("build-succeeded-without-its-branch")
    reason = _verdicts(tmp_path, world)["push_fails"][1]
    assert "#1225" in reason and "SUCCEEDED" in reason


@pytest.mark.parametrize("state", ["PARKED:CREDENTIAL_MISSING", "HELD:QUEUED:pool closed"])
def test_a_task_the_platform_held_is_skipped_never_passed(tmp_path, state):
    w = _world()
    for role in ("lane", "reader", "build"):
        w[role]["state"] = state
    verdicts = _verdicts(tmp_path, w)
    for check in ("rebase", "read_private", "push_main", "push_other", "push_fails"):
        assert verdicts[check][0] == "SKIP", (check, verdicts[check])


def test_no_allowlisted_repository_is_a_skip_naming_who_sets_one(tmp_path):
    w = _world()
    w["reader"]["read"] = {"skip": "no repository registered in tenant smoke is readable_by_tasks"}
    verdict, reason = _verdicts(tmp_path, w)["read_private"]
    assert verdict == "SKIP" and "readable_by_tasks" in reason


def test_a_task_that_was_never_created_is_a_failure(tmp_path):
    w = _world()
    w["build"] = {"task": "", "state": ""}
    assert _verdicts(tmp_path, w)["push_fails"][0] == "FAIL"


def test_an_unreadable_audit_is_a_failure_not_a_clean_record(tmp_path):
    w = _world()
    w["reader"]["calls"] = None
    verdicts = _verdicts(tmp_path, w)
    assert verdicts["push_main"][0] == "FAIL"
    assert "audit" in verdicts["push_main"][1]


def test_the_cli_writes_one_line_per_check_in_order(tmp_path):
    base = _write(tmp_path / "evidence", _world())
    results = tmp_path / "results.jsonl"
    subprocess.run([sys.executable, str(JUDGE_PATH), "judge", "--dir", str(base), "--results", str(results)],
                   check=True, timeout=60)
    lines = [json.loads(line) for line in results.read_text().splitlines()]
    assert [line["check"] for line in lines] == list(gt.CHECKS)
    assert {line["verdict"] for line in lines} == {"PASS"}


# ---------------------------------------------------------------------------
# The environment: names, never values
# ---------------------------------------------------------------------------


def _forge_token() -> str:
    return "gh" + "p_" + "Z" * 36


def test_the_environment_report_prints_names_and_never_a_value():
    login = "sk-" + "q" * 40
    forge = _forge_token()
    report = env_report.report({"CLAUDE_CODE_OAUTH_TOKEN": login, "LEAKED": forge,
                                "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": forge})
    text = json.dumps(report)
    assert login not in text and forge not in text
    assert report["forge_shaped"] == ["GIT_CONFIG_VALUE_0", "LEAKED"]
    assert report["git_config_keys"] == {"GIT_CONFIG_KEY_0": "credential.helper"}


def test_the_claude_login_is_allowed_and_the_identity_is_not_a_credential():
    assert gt.judge_env(_clean_env_report()) == []


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN", "GIT_ASKPASS", "GIT_PASSWORD",
                                  "SWARM_GIT_TOKEN", "my_token", "GIT_HTTP_EXTRAHEADER", "GIT_CREDENTIALS"])
def test_a_credential_variable_in_the_runner_is_found(name):
    report = _clean_env_report()
    report["names"].append(name)
    assert gt.judge_env(report), name


@pytest.mark.parametrize("key", ["credential.helper", "http.https://github.com/.extraheader",
                                 "url.https://x@github.com/.insteadOf"])
def test_a_git_config_credential_key_in_the_runner_is_found(key):
    report = _clean_env_report()
    report["git_config_keys"] = {"GIT_CONFIG_KEY_0": key}
    assert gt.judge_env(report), key


def test_a_forge_shaped_value_under_any_name_is_found():
    report = env_report.report({"HARMLESS": _forge_token(), "PATH": "/bin"})
    assert gt.judge_env(report) == ["HARMLESS's value has a forge credential's shape"]


@pytest.mark.parametrize("report", [None, {}, {"names": []}, "not json"])
def test_an_unreadable_environment_report_is_never_clean(report):
    assert gt.judge_env(report)


# ---------------------------------------------------------------------------
# What the tasks are asked, and the names everyone shares
# ---------------------------------------------------------------------------


def test_each_prompt_names_every_step_the_judge_reads():
    rebase = gt.prompt("rebase", run_id="r", lane_branch=BRANCH)
    for step in ("fetch-own", "rebase", "push", "pr-checks", gt.MAIN_LINE, gt.LANE_LINE, gt.ENV_REPORT,
                 gt.ENV_REPORT_ARTIFACT, gt.ANSWERS, BRANCH):
        assert step in rebase, step
    reader = gt.prompt("reader", run_id="r", lane_branch=BRANCH, read_repo=SANDBOX,
                       read_path=gt.PLANTED, read_ref="main")
    for step in ("fetch-main-before", "push-main", "fetch-main-after", "fetch-other-before",
                 "push-other", "fetch-other-after", "read-ref", "read-sha", "read-unlisted",
                 gt.UNLISTED_PREFIX + "r"):
        assert step in reader, step
    assert "read-ref" not in gt.prompt("reader", run_id="r", lane_branch=BRANCH)
    assert "push-first" in gt.prompt("build", run_id="r")
    lane = gt.prompt("lane", run_id="r")
    assert gt.LANE_LINE in lane and gt.PR_MARKER in lane and gt.PLANTED in lane
    # The lane writes planted.txt itself, on a base without it: never main's line.
    assert gt.MAIN_LINE not in lane


def test_the_resolution_is_main_s_line_then_the_lane_s():
    assert gt.expected_planted() == f"{gt.MAIN_LINE}\n{gt.LANE_LINE}\n"
    assert gt.MAIN_LINE != gt.LANE_LINE


def test_main_s_planted_line_is_the_fixture_s():
    assert (FIXTURE / "planted.txt").read_text() == gt.MAIN_LINE + "\n"


def _shell_array(name: str) -> list[str]:
    return re.search(rf"^{name}=\(([^)]*)\)", GROUP.read_text(), re.M).group(1).split()


def _shell_value(path: Path, name: str) -> str:
    return re.search(rf'^\s*{name}="([^"]*)"', path.read_text(), re.M).group(1)


def test_the_shell_group_lists_one_check_per_verdict_key_in_order():
    assert tuple(_shell_array("GIT_TOOLS_KEYS")) == gt.CHECKS
    assert tuple(_shell_array("GIT_TOOLS_ROLES")) == gt.ROLES
    listed = re.search(r"git_tools_checks\(\) \{\n  cat <<'EOF'\n(.*?)\nEOF", GROUP.read_text(), re.S).group(1)
    assert len(listed.splitlines()) == len(gt.CHECKS)
    assert all(line.startswith("git-tools: ") for line in listed.splitlines())


def test_the_base_branch_is_one_name_and_outside_swarm():
    assert _shell_value(GROUP, "GIT_TOOLS_BASE") == gt.BASE_BRANCH
    assert _shell_value(SYNC, "GIT_TOOLS_BASE") == gt.BASE_BRANCH
    assert not gt.BASE_BRANCH.startswith("swarm/"), "a task could push its own base"
    assert _shell_value(GROUP, "GIT_TOOLS_PLANTED") == gt.PLANTED


def test_sandbox_sync_force_updates_only_the_base_and_test_services_branches():
    code = [line for line in SYNC.read_text().splitlines() if not line.lstrip().startswith("#")]
    forced = [line for line in code if "--force" in line]
    assert forced, "the base branch is never pointed"
    for line in forced:
        assert 'refs/heads/${GIT_TOOLS_BASE}' in line or 'refs/heads/${branch}' in line, line
    # ${branch} is ts_branch's argument (TS-ACCEPT), and its only callers name
    # the two test-services branches: outside swarm/, and never main.
    calls = [line.split()[1] for line in code if line.startswith("ts_branch ")]
    assert sorted(calls) == ['"acceptance/test-services"', '"acceptance/test-services-undeclared"'], calls
    # The fixture sync itself still never forces main.
    assert not any("--force" in line and "ACC_REF" in line for line in code)


def test_the_rebase_lane_continues_the_lane_and_the_reader_may_push_only_its_own():
    text = GROUP.read_text()
    assert 'continues_task: $t' in text and 'strategy: "direct-pr"' in text
    assert '"strategy": "collect", "carrier": "branches"' in text, \
        "a reader that pushes nothing is refused strategy_pushes_nothing before the branch rule"
    assert 'repository_ref: $r, max_attempts: 1, strategy: "direct-pr"' in text
    assert '--arg r "${GIT_TOOLS_BASE}"' in text


# ---------------------------------------------------------------------------
# github-verify.sh: the rebase lane's pull request, read back
# ---------------------------------------------------------------------------


def _verify_git_tools(tmp_path: Path, *, behind: int = 0, files: list[str] | None = None,
                      planted: str | None = None) -> subprocess.CompletedProcess:
    head_sha = "f" * 40
    pull = _pr(9, LANE, title="Add the git-tools acceptance lane's files",
               body=f"acceptance-run:x\n{gt.PR_MARKER}\n\nOpened by swarm task {LANE}.\n")
    pull["head"]["sha"] = head_sha
    gh = tmp_path / "gh"
    gh.mkdir()
    repo = f"repos/{SANDBOX}"
    (gh / _gh_key(f"{repo}/pulls?state=open&per_page=100")).write_text(json.dumps([pull]))
    changed = files if files is not None else [f"{gt.FIXTURE_DIR}/planted.txt", f"{gt.FIXTURE_DIR}/lane.txt"]
    (gh / _gh_key(f"{repo}/pulls/9/files?per_page=100")).write_text(
        json.dumps([{"filename": f, "patch": "+x\n"} for f in changed]))
    (gh / _gh_key(f"{repo}/compare/main...{BRANCH}")).write_text(
        json.dumps({"behind_by": behind, "files": [{"filename": f} for f in changed]}))
    (gh / _gh_key(f"{repo}/contents/{gt.FIXTURE_DIR}/planted.txt?ref={head_sha}")).write_text(
        gt.expected_planted() if planted is None else planted)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "curl"
    fake.write_text(FAKE_GH_CURL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = _clean_env(PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(tmp_path), TMPDIR=str(tmp_path),
                     FAKE_DIR=str(tmp_path), SWARM_ENV_FILE=str(tmp_path / "no.env"),
                     SWARM_ACCEPTANCE_GITHUB_API="http://gh.invalid",
                     SWARM_ACCEPTANCE_GITHUB_TOKEN="t" + "k" * 12)
    return subprocess.run(["bash", str(VERIFY)], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=60, check=False)


def test_verify_passes_a_rebased_and_resolved_lane_pull_request(tmp_path):
    result = _verify_git_tools(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "1 git-tools" in result.stderr


@pytest.mark.parametrize("case", ["behind-main", "extra-file", "unresolved"])
def test_verify_fails_a_lane_pull_request_that_was_not_rebased_and_resolved(tmp_path, case):
    kwargs = {
        "behind-main": {"behind": 2},
        "extra-file": {"files": [f"{gt.FIXTURE_DIR}/planted.txt", f"{gt.FIXTURE_DIR}/lane.txt",
                                 f"{gt.FIXTURE_DIR}/env_report.py"]},
        "unresolved": {"planted": f"<<<<<<< HEAD\n{gt.MAIN_LINE}\n=======\n{gt.LANE_LINE}\n>>>>>>> x\n"},
    }[case]
    result = _verify_git_tools(tmp_path, **kwargs)
    assert result.returncode != 0, f"{case} passed:\n{result.stderr}"
    assert "assertion(s) failed" in result.stderr


def test_pr_kind_reads_only_the_marker():
    assert gt.pr_kind(f"x\n{gt.PR_MARKER}\n") == "git-tools"
    assert gt.pr_kind("Integrates 1 contributor branch(es):") == "other"
    assert gt.pr_kind("") == "other"
