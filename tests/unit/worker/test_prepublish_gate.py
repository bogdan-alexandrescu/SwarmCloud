"""A claude-code lane is rebased and checked before the worker publishes it (SPEED-D1).

Owner decision 2026-10-14 (delivery speed plan, D1 "Pre-publish gate"):
0.52 red CI runs per pull request (checkov, shellcheck, the guards, the build
mirror) and 128 of 150 needed an update from main. So, after the agent's turn
ends and while its session can still be continued, the runner
(`agent_worker.prepublish.Gate`, wired in `cliagent.run_cli_agent`):

* rebases the lane onto the CURRENT base through the git tools' spool, and
  on a conflict resumes the session for ONE turn naming the files;
* runs a fixed check set limited to the kinds of file the diff touches;
* on a failure resumes the session for ONE fix turn, then runs it again;
* writes what still fails into the artifact the worker appends to the pull
  request body, under "Pre-publish checks failed";
* records each check's name, status and duration in the result.

The rebase here is the real relay (`gittools.GitTools`, from
test_gittools.World) against a local bare origin. The tools are stand-ins on
PATH that record how they were called. Every credential-shaped value is
assembled at runtime.
"""

from __future__ import annotations

import io
import json
import os
import secrets as pysecrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle, prepublish
from agent_worker.gitmcp import server as shim
from agent_worker.gittools import GitTools
from agent_worker.logs import build_logger
from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext

from test_gittools import World, _commit  # noqa: E402 - the relay's own test world

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
        "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=_ENV).stdout.strip()


def _log() -> Any:
    return build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                        runner_profile="claude-code", stream=io.StringIO())


#: A stand-in tool: records its argv in $TOOL_LOG, then fails (printing what
#: `$FAKE_OUTPUT` holds, plus each failing file) when any file it was given
#: contains the word BAD.
FAKE_TOOL = r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["TOOL_LOG"], "a") as f:
    f.write(json.dumps({"tool": name, "argv": sys.argv[1:]}) + "\n")
bad = [a for a in sys.argv[1:] if pathlib.Path(a).is_file() and "BAD" in pathlib.Path(a).read_text()]
if bad:
    print(os.environ.get("FAKE_OUTPUT", ""))
    for path in bad:
        print(f"{path}:1:1: error - {name} says no")
    sys.exit(1)
'''


def _tools(tmp_path: Path, names: tuple[str, ...] = ("shellcheck", "checkov")) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        path = bin_dir / name
        # This interpreter by path: the checks' PATH holds no python3 of its own.
        path.write_text(f"#!{sys.executable}" + FAKE_TOOL)
        path.chmod(0o755)
    return bin_dir


def _env(tmp_path: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    # git and python3 from the host; nothing else on PATH is a check's tool.
    base = ":".join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
    git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    env = {"PATH": f"{bin_dir}:{git_dir}:{base}", "HOME": str(tmp_path),
           "TOOL_LOG": str(tmp_path / "tools.jsonl"), **_ENV_IDENT, **extra}
    return env


_ENV_IDENT = {k: v for k, v in _ENV.items() if k.startswith("GIT_")}


def _tool_calls(tmp_path: Path) -> list[dict]:
    path = tmp_path / "tools.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _change(repo: Path, path: str, text: str, *, commit: bool = True) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    if commit:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"change {path}")


class Turns:
    """The agent's session: each resume records its prompt and runs `act`."""

    def __init__(self, *acts: Any) -> None:
        self.prompts: list[str] = []
        self._acts = list(acts)

    def __call__(self, text: str) -> bool:
        self.prompts.append(text)
        if self._acts:
            self._acts.pop(0)()
        return True


def _gate(repo: Path, tmp_path: Path, env: dict[str, str], turns: Turns, *,
          secrets: tuple[str, ...] = (), submit: Any = None) -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=repo, env=env, secrets=secrets, resume=turns,
                           remaining=lambda: 3600.0, log=_log(), artifacts_dir=artifacts,
                           submit=submit)


def _section(tmp_path: Path) -> str | None:
    path = tmp_path / "artifacts" / prepublish.PR_SECTION_NAME
    return path.read_text() if path.exists() else None


# ---------------------------------------------------------------------------
# The check set follows what the diff touches
# ---------------------------------------------------------------------------


def _names(checks: list[prepublish.Check]) -> list[str]:
    return [c.name for c in checks]


def test_the_check_set_is_limited_to_the_kinds_of_file_the_diff_touches(tmp_path):
    repo, base = _repo(tmp_path)
    env = _env(tmp_path, _tools(tmp_path, ("shellcheck", "checkov", "tflint")))
    (repo / "scripts").mkdir()
    (repo / "scripts" / "changed-guards.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    for path in ("docs/a.md", "scripts/x.sh", "terraform/infra/main.tf", "apps/w/x.py",
                 "kubernetes/job.yaml", "images/agent-runtime-base/Dockerfile",
                 "tests/unit/scripts/test_build_mirror.py"):
        _change(repo, path, "x\n", commit=False)

    def chosen(*files: str) -> list[str]:
        return _names(prepublish.select_checks(repo, list(files), base=base, env=env))

    assert chosen() == []
    assert chosen("docs/a.md") == ["changed-guards"]
    assert chosen("docs/a.md", "scripts/x.sh") == ["changed-guards", "shellcheck"]
    assert chosen("terraform/infra/main.tf") == ["changed-guards", "checkov-terraform", "tflint",
                                                "terraform-fmt"]
    assert chosen("kubernetes/job.yaml") == ["changed-guards", "checkov-kubernetes"]
    assert chosen("apps/w/x.py") == ["changed-guards", "ruff-format", "ruff-check"]
    assert chosen("images/agent-runtime-base/Dockerfile") == ["changed-guards", "build-mirror"]
    # A deleted file has nothing left to lint: only the guards read it.
    assert chosen("scripts/gone.sh") == ["changed-guards"]
    # Without the guards script (another repository), the guards are not run.
    (repo / "scripts" / "changed-guards.sh").unlink()
    assert chosen("docs/a.md") == []


def test_a_tool_missing_from_the_image_is_skipped_with_its_reason_never_sent_to_the_agent(
    tmp_path,
):
    repo, base = _repo(tmp_path)
    env = _env(tmp_path, _tools(tmp_path, ("checkov",)))  # no tflint, as in the image
    _change(repo, "terraform/main.tf", 'resource "x" "y" {}\n')
    turns = Turns()
    record = _gate(repo, tmp_path, {**env, prepublish.CLONE_BASE_ENV: base}, turns).run()
    (round_,) = record["rounds"]
    by_name = {c["name"]: c for c in round_}
    assert by_name["checkov-terraform"]["status"] == "passed"
    assert by_name["tflint"]["status"] == "skipped"
    assert "INSTALL_TFLINT" in by_name["tflint"]["reason"]
    assert turns.prompts == [] and record["fix_turn"] is False
    assert _section(tmp_path) is None


def test_ruff_runs_only_where_the_repository_configures_it(tmp_path):
    repo, base = _repo(tmp_path)
    env = _env(tmp_path, _tools(tmp_path, ("ruff",)))
    _change(repo, "apps/x.py", "x = 1\n")
    skipped = prepublish.select_checks(repo, ["apps/x.py"], base=base, env=env)
    assert all(c.run is None and "configures no ruff" in (c.skipped or "") for c in skipped)
    (repo / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    ran = prepublish.run_checks(
        repo, prepublish.select_checks(repo, ["apps/x.py"], base=base, env=env), env=env)
    assert [(o.name, o.status) for o in ran] == [("ruff-format", "passed"),
                                                 ("ruff-check", "passed")]
    assert [c["argv"][:2] for c in _tool_calls(tmp_path)] == [["format", "--check"],
                                                              ["check", "apps/x.py"]]


# ---------------------------------------------------------------------------
# One fix turn
# ---------------------------------------------------------------------------


def test_a_failing_check_triggers_exactly_one_fix_turn_and_its_output_reaches_the_pr_body(
    tmp_path,
):
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "scripts/deploy.sh", "#!/usr/bin/env bash\necho BAD\n")
    turns = Turns(lambda: None, lambda: None)  # the agent does not fix it
    record = _gate(repo, tmp_path, env, turns).run()

    assert len(turns.prompts) == 1, "a failing check gets ONE fix turn, never more"
    assert "shellcheck" in turns.prompts[0] and "scripts/deploy.sh" in turns.prompts[0]
    assert record["fix_turn"] is True
    assert [[c["status"] for c in r] for r in record["rounds"]] == [["failed"], ["failed"]]
    assert all(isinstance(c["duration_seconds"], float) for r in record["rounds"] for c in r)
    assert record["failed"] == ["shellcheck"] and record["pr_section"] is True
    shellchecked = [c for c in _tool_calls(tmp_path) if c["tool"] == "shellcheck"]
    assert len(shellchecked) == 2 and shellchecked[0]["argv"] == ["-x", "scripts/deploy.sh"]
    section = _section(tmp_path)
    assert section is not None and section.startswith(prepublish.PR_SECTION_HEADING)
    assert "shellcheck failed" in section and "shellcheck says no" in section


def test_a_fix_turn_that_clears_the_check_leaves_no_section(tmp_path):
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "scripts/deploy.sh", "#!/usr/bin/env bash\necho BAD\n")
    turns = Turns(lambda: _change(repo, "scripts/deploy.sh", "#!/usr/bin/env bash\necho ok\n"))
    record = _gate(repo, tmp_path, env, turns).run()
    assert len(turns.prompts) == 1
    assert [[c["status"] for c in r] for r in record["rounds"]] == [["failed"], ["passed"]]
    assert record["failed"] == [] and record["pr_section"] is False
    assert _section(tmp_path) is None


def _file_checks(monkeypatch, *names: str) -> None:
    """The check set as named checks that fail while `<name>.state` holds BAD:
    vitest and tsc without a package, node_modules or a graph snapshot."""

    def check(name: str) -> prepublish.Check:
        def run(runner: Any, repo: Path, env: Any, timeout: float) -> tuple[str, str]:
            state = repo / f"{name}.state"
            if state.exists() and "BAD" in state.read_text():
                return prepublish.FAILED, f"{name}.state:1:1: error - {name} says no"
            return prepublish.PASSED, ""
        return prepublish.Check(name, (f"{name}.state",), name, run)

    monkeypatch.setattr(prepublish, "select_checks",
                        lambda *a, **k: [check(name) for name in names])


def test_a_fix_turn_that_breaks_a_check_that_had_passed_gets_one_more_turn(
    tmp_path, monkeypatch,
):
    # F07 (#2379): tsc passed before the fix turn and failed after it, and the
    # branch was published knowing CI would fail. Owner decision 2026-10-26:
    # one more turn, bounded by the same budget.
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _file_checks(monkeypatch, "vitest", "tsc")
    _change(repo, "vitest.state", "BAD\n")

    def fix_vitest_break_tsc() -> None:
        _change(repo, "vitest.state", "ok\n")
        _change(repo, "tsc.state", "BAD\n")

    turns = Turns(fix_vitest_break_tsc, lambda: _change(repo, "tsc.state", "ok\n"))
    record = _gate(repo, tmp_path, env, turns).run()

    # MUTATION: one fix turn only publishes with failed: [tsc].
    assert len(turns.prompts) == 2, record
    assert "## vitest" in turns.prompts[0] and "## tsc" not in turns.prompts[0]
    # The second prompt names what the fix turn broke, and only what fails now.
    assert "## tsc" in turns.prompts[1] and "## vitest" not in turns.prompts[1]
    assert "broke" in turns.prompts[1] and "tsc" in turns.prompts[1].split("\n")[0]
    assert record["fix_turn"] is True and record["second_turn"] is True
    assert record["broken_by_fix_turn"] == ["tsc"]
    assert [{c["name"]: c["status"] for c in r} for r in record["rounds"]] == [
        {"vitest": "failed", "tsc": "passed"},
        {"vitest": "passed", "tsc": "failed"},
        {"vitest": "passed", "tsc": "passed"},
    ]
    assert record["failed"] == [] and record["pr_section"] is False
    assert _section(tmp_path) is None


def test_the_second_turn_is_the_last_and_what_it_leaves_is_published(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _file_checks(monkeypatch, "vitest", "tsc")
    _change(repo, "vitest.state", "BAD\n")
    # The fix turn breaks tsc, the second turn breaks vitest again.
    turns = Turns(lambda: (_change(repo, "vitest.state", "ok\n"),
                           _change(repo, "tsc.state", "BAD\n")),
                  lambda: (_change(repo, "tsc.state", "ok\n"),
                           _change(repo, "vitest.state", "BAD\n")),
                  lambda: None)
    record = _gate(repo, tmp_path, env, turns).run()
    # MUTATION: a loop that gives a turn for every newly broken check.
    assert len(turns.prompts) == 2 and len(record["rounds"]) == 3
    assert record["failed"] == ["vitest"] and record["pr_section"] is True


def test_a_check_still_failing_after_the_fix_turn_gets_no_second_turn(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _file_checks(monkeypatch, "vitest", "tsc")
    _change(repo, "vitest.state", "BAD\n")
    _change(repo, "tsc.state", "BAD\n")
    # The fix turn fixes vitest but not tsc, which failed before it too.
    turns = Turns(lambda: _change(repo, "vitest.state", "ok\n"), lambda: None)
    record = _gate(repo, tmp_path, env, turns).run()
    # MUTATION: a second turn for any failure, not only one the fix turn caused.
    assert len(turns.prompts) == 1 and record["second_turn"] is False
    assert record["broken_by_fix_turn"] == [] and record["failed"] == ["tsc"]


def test_the_second_turn_is_bounded_by_the_steps_budget(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _file_checks(monkeypatch, "vitest", "tsc")
    _change(repo, "vitest.state", "BAD\n")
    left = [3600.0]

    def fix_turn() -> None:
        _change(repo, "vitest.state", "ok\n")
        _change(repo, "tsc.state", "BAD\n")
        left[0] = prepublish.MIN_GATE_SECONDS - 1  # the fix turn spent the budget

    turns = Turns(fix_turn, lambda: _change(repo, "tsc.state", "ok\n"))
    gate = _gate(repo, tmp_path, env, turns)
    gate.remaining = lambda: left[0]
    record = gate.run()
    # MUTATION: a second turn started with too little budget for its round.
    assert len(turns.prompts) == 1 and record["second_turn"] is False
    assert record["second_turn_skipped"] == "budget"
    assert record["broken_by_fix_turn"] == ["tsc"] and record["failed"] == ["tsc"]


def test_a_passing_set_takes_no_turn(tmp_path):
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "scripts/deploy.sh", "#!/usr/bin/env bash\necho ok\n")
    turns = Turns()
    record = _gate(repo, tmp_path, env, turns).run()
    assert turns.prompts == [] and record["fix_turn"] is False
    assert [[c["name"] for c in r] for r in record["rounds"]] == [["shellcheck"]]


def test_uncommitted_and_untracked_files_are_checked_too(tmp_path):
    """The worker commits them at publish, so they are part of what ships."""
    repo, base = _repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "scripts/new.sh", "echo BAD\n", commit=False)
    record = _gate(repo, tmp_path, env, Turns()).run()
    assert record["rounds"][0] == [
        {"name": "shellcheck", "status": "failed", "duration_seconds":
         record["rounds"][0][0]["duration_seconds"], "files": 1}]


# ---------------------------------------------------------------------------
# The rebase, and one conflict turn
# ---------------------------------------------------------------------------


def _relay(world: World):
    """The runner's submit, answered synchronously by the worker's relay."""
    def submit(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_id = f"pp-{pysecrets.token_hex(4)}"
        shim.write_request(GitTools.spool(world.work), request_id, tool, arguments)
        world.tools.tick(world.work)
        path = GitTools.spool(world.work) / "responses" / f"{request_id}.json"
        return json.loads(path.read_text())
    return submit


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, shallow=True)


def _world_env(world: World, tmp_path: Path) -> dict[str, str]:
    return {**_env(tmp_path, _tools(tmp_path)), prepublish.CLONE_BASE_ENV: world.base,
            prepublish.PREPUBLISH_BASE_ENV: "main"}


def test_a_clean_rebase_puts_the_lane_on_the_current_base_and_checks_from_there(
    world, tmp_path,
):
    onto = world.advance_main("docs/theirs.md", "theirs\n", "main moved")
    _commit(world.clone, "scripts/mine.sh", "echo ok\n", "mine")
    turns = Turns()
    record = _gate(world.clone, tmp_path, _world_env(world, tmp_path), turns,
                   submit=_relay(world)).run()
    assert record["rebase"]["status"] == "rebased" and record["rebase"]["onto"] == onto
    assert _git(world.clone, "rev-parse", "HEAD~1") == onto
    assert world.tools.rebased_onto == onto
    # The checks diff from where the lane landed: main's own file is not read.
    assert record["base"] == onto
    assert [c["name"] for c in record["rounds"][0]] == ["shellcheck"]
    assert turns.prompts == []


def test_a_rebase_conflict_triggers_the_conflict_turn_naming_the_files(world, tmp_path):
    world.advance_main("README.md", "line one\nTHEIR line two\n", "theirs")
    _commit(world.clone, "README.md", "line one\nMY line two\n", "mine")
    mine = _git(world.clone, "rev-parse", "HEAD")
    turns = Turns(lambda: None)  # the agent leaves the conflict
    record = _gate(world.clone, tmp_path, _world_env(world, tmp_path), turns,
                   submit=_relay(world)).run()

    assert len(turns.prompts) == 1 and record["conflict_turn"] is True
    assert "README.md" in turns.prompts[0] and "git rebase --continue" in turns.prompts[0]
    # Never a conflicted branch: the rebase is aborted and HEAD is the lane's.
    assert record["rebase"]["status"] == "conflict_unresolved"
    assert not (world.clone / ".git" / "rebase-merge").exists()
    assert _git(world.clone, "rev-parse", "HEAD") == mine
    section = _section(tmp_path)
    assert section is not None and "rebase onto origin/main conflicted" in section
    assert "`README.md`" in section


def test_a_conflict_the_agent_resolves_is_kept(world, tmp_path):
    onto = world.advance_main("README.md", "line one\nTHEIR line two\n", "theirs")
    _commit(world.clone, "README.md", "line one\nMY line two\n", "mine")

    def resolve() -> None:
        (world.clone / "README.md").write_text("line one\nTHEIR and MY line two\n")
        _git(world.clone, "add", "README.md")
        subprocess.run(["git", "-C", str(world.clone), "rebase", "--continue"], check=True,
                       capture_output=True, env={**_ENV, "GIT_EDITOR": "true"})

    turns = Turns(resolve)
    record = _gate(world.clone, tmp_path, _world_env(world, tmp_path), turns,
                   submit=_relay(world)).run()
    assert record["rebase"]["status"] == "conflict_resolved"
    assert _git(world.clone, "rev-parse", "HEAD~1") == onto
    assert record["base"] == onto and _section(tmp_path) is None


def test_no_rebase_is_asked_for_without_a_base_branch_from_the_worker(world, tmp_path):
    world.advance_main("docs/theirs.md", "theirs\n", "main moved")
    _commit(world.clone, "src/app.py", "x = 1\n", "mine")
    env = _world_env(world, tmp_path)
    del env[prepublish.PREPUBLISH_BASE_ENV]
    calls: list[str] = []
    record = _gate(world.clone, tmp_path, env, Turns(),
                   submit=lambda tool, args: calls.append(tool) or {}).run()
    assert record["rebase"] is None and calls == []
    assert _git(world.clone, "rev-parse", "HEAD~1") == world.base


# ---------------------------------------------------------------------------
# Nothing secret-shaped, and the worker's side of the names
# ---------------------------------------------------------------------------


def test_nothing_secret_shaped_reaches_the_fix_prompt_or_the_pr_body(tmp_path):
    repo, base = _repo(tmp_path)
    runner_secret = "sk-ant-" + "api03-" + pysecrets.token_hex(20)
    forge_token = "gh" + "p_" + pysecrets.token_hex(18)
    env = {**_env(tmp_path, _tools(tmp_path),
                  FAKE_OUTPUT=f"key={runner_secret} token={forge_token}"),
           prepublish.CLONE_BASE_ENV: base}
    _change(repo, "scripts/deploy.sh", "echo BAD\n")
    turns = Turns(lambda: None)
    _gate(repo, tmp_path, env, turns, secrets=(runner_secret,)).run()
    section = _section(tmp_path) or ""
    for text in (turns.prompts[0], section):
        assert runner_secret not in text and forge_token not in text
        assert "shellcheck says no" in text


def test_the_checks_never_see_the_provider_credential():
    env = prepublish.check_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "x" * 8, "HOME": "/w"},
                               drop=("ANTHROPIC_API_KEY",), base="b" * 40)
    assert "ANTHROPIC_API_KEY" not in env
    assert env[prepublish.CLONE_BASE_ENV] == "b" * 40


def test_the_runner_and_the_worker_agree_on_the_names():
    assert prepublish.PR_SECTION_NAME == lifecycle.PREPUBLISH_SECTION_FILE
    assert prepublish.PR_SECTION_HEADING == lifecycle.PREPUBLISH_SECTION_HEADING
    assert prepublish.PREPUBLISH_BASE_ENV == lifecycle.PREPUBLISH_BASE_ENV
    assert prepublish.CLONE_BASE_ENV == lifecycle.CLONE_BASE_ENV
    assert claude_code.SPEC.prepublish is True


# ---------------------------------------------------------------------------
# Through the real claude-code runner
# ---------------------------------------------------------------------------


SESSION = "sess-" + "a1b2c3d4e5f60718"

FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
home = pathlib.Path(os.environ["HOME"])
plan = json.loads((home / "plan.json").read_text())
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "prompt": sys.stdin.read(),
                        "env": sorted(os.environ)}) + "\n")
runs = sum(1 for _ in (home / "runs.jsonl").open())
step = plan["runs"][min(runs - 1, len(plan["runs"]) - 1)]
for name, text in step.get("write", {}).items():
    path = pathlib.Path.cwd() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
print(json.dumps({"type": "system", "subtype": "init", "session_id": plan["session"]}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": plan["session"], "result": "Done.",
                  "total_cost_usd": 0.1, "num_turns": 1}))
'''


def test_the_claude_code_runner_gives_a_failing_check_its_fix_turn(tmp_path, monkeypatch):
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    repo, base = _repo(work)  # the checkout is inside the work directory
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    (work / "plan.json").write_text(json.dumps({"session": SESSION, "runs": [
        {"write": {"scripts/deploy.sh": "#!/usr/bin/env bash\necho BAD\n"}},
        {"write": {"scripts/deploy.sh": "#!/usr/bin/env bash\necho fixed\n"}},
    ]}))
    bin_dir = _tools(tmp_path)
    monkeypatch.setattr(cliagent, "BASE_PATH", f"{bin_dir}:{cliagent.BASE_PATH}")
    monkeypatch.setenv("TOOL_LOG", str(tmp_path / "tools.jsonl"))
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "api03-" + pysecrets.token_hex(16))
    monkeypatch.setenv(prepublish.CLONE_BASE_ENV, base)
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
                 prepublish.PREPUBLISH_BASE_ENV, shim.SPOOL_ENV,
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)
    ctx = RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "do it"}, repo_dir=repo)
    # TOOL_LOG reaches the stand-in tools only through the checks' environment,
    # which is the agent's: pass it the way the image's PATH is passed.
    monkeypatch.setattr(cliagent, "_PLAIN_PASSTHROUGH",
                        (*cliagent._PLAIN_PASSTHROUGH, "TOOL_LOG"))

    out = claude_code.body(ctx)

    runs = [json.loads(line) for line in (work / "runs.jsonl").read_text().splitlines()]
    assert [r["resume"] for r in runs] == [None, SESSION]
    assert "shellcheck" in runs[1]["prompt"] and "scripts/deploy.sh" in runs[1]["prompt"]
    gate = out["prepublish"]
    assert gate["fix_turn"] is True and gate["failed"] == []
    assert [[c["status"] for c in r] for r in gate["rounds"]] == [["failed"], ["passed"]]
    assert not (artifacts / prepublish.PR_SECTION_NAME).exists()
    # The stand-in shellcheck ran with the agent's environment minus its key.
    assert all("ANTHROPIC_API_KEY" not in json.dumps(c) for c in _tool_calls(tmp_path))
