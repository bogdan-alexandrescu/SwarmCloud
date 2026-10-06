"""A claude-code step is shown the checks it would fail, while it can still fix them.

Issue #624, owner decision 2026-10-05 (history I1). About $201 (14.9% of all
spend) went to agents that finished and then failed a check they never saw:
an expected output not written (the worker fails the attempt retryably), or a
credential-shaped line in the final tree (the worker refuses to publish). Both
checks ran only after the agent had exited.

Now, after the agent's turn ends, the runner runs the same expected-outputs
check and the same publish credential scan (`agent_worker.publish_scan`, the
one implementation) against the tree. When either fails it resumes the
session with `--resume <session_id>` and a prompt naming exactly what is
missing, or which `path:line rule` was flagged -- never the matched text --
for up to `REPAIR_MAX_TURNS` (2) repair turns inside the step's budget. Then
the attempt ends exactly as before, and the worker's own checks still decide.

These run the real claude-code runner against a fake CLI (a Python script
that prints a stream-json conversation, writes the files its plan says, and
records how it was started) and a real git repository. Every
credential-shaped value is assembled at runtime.
"""

from __future__ import annotations

import json
import random
import secrets as pysecrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

SESSION = "sess-" + "a1b2c3d4e5f60718"
DONE = "Done: the change is in and 12 tests pass."
_PW = "pass" + "word"


def _key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _value() -> str:
    """A generic secret the scan's `key_value_assignment` rule accepts."""
    return "".join(random.Random(7).sample(string.ascii_letters + string.digits, 24))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _ctx(tmp_path: Path, payload: dict, *, repo: bool) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    checkout = None
    if repo:
        checkout = work / "repo"
        checkout.mkdir()
        _git(checkout, "init", "-q", "-b", "main")
        _git(checkout, "config", "user.email", "repair@example.invalid")
        _git(checkout, "config", "user.name", "repair")
        (checkout / "src").mkdir()
        (checkout / "src" / "app.py").write_text("def main():\n    return 1\n")
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-q", "-m", "base")
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
        repo_dir=checkout,
    )


#: The fake CLI. Its plan is `$HOME/plan.json` (HOME is the work directory);
#: each start appends how it was started to `$HOME/runs.jsonl`, then writes
#: the files its step names: `$ARTIFACTS/<name>` into the artifacts directory,
#: anything else relative to where it was started (the checkout).
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
home = pathlib.Path(os.environ["HOME"])
plan = json.loads((home / "plan.json").read_text())
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "prompt": sys.stdin.read()}) + "\n")
runs = sum(1 for _ in (home / "runs.jsonl").open())
step = plan["runs"][min(runs - 1, len(plan["runs"]) - 1)]
for name, text in step.get("write", {}).items():
    if name.startswith("$ARTIFACTS/"):
        path = pathlib.Path(os.environ["SWARM_ARTIFACTS_DIR"]) / name[len("$ARTIFACTS/"):]
    else:
        path = pathlib.Path.cwd() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

def say(event):
    print(json.dumps(event), flush=True)

say({"type": "system", "subtype": "init", "session_id": plan["session"]})
say({"type": "result", "subtype": "success", "is_error": False,
     "session_id": plan["session"], "result": step.get("answer", "Done."),
     "total_cost_usd": step.get("cost", 0.5), "num_turns": step.get("turns", 3)})
'''


def _install(
    tmp_path: Path, monkeypatch, ctx: RunnerContext, runs: list[dict], *, session: str = SESSION
) -> None:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    (ctx.work_dir / "plan.json").write_text(json.dumps({"session": session, "runs": runs}))
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    monkeypatch.setenv("ANTHROPIC_API_KEY", _key())
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)
    if ctx.repo_dir is not None:
        # The worker exports the clone base the publish will diff from.
        monkeypatch.setenv("SWARM_CLONE_BASE", _git(ctx.repo_dir, "rev-parse", "HEAD").strip())
    else:
        monkeypatch.delenv("SWARM_CLONE_BASE", raising=False)


def _runs(ctx: RunnerContext) -> list[dict]:
    path = ctx.work_dir / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _everything_written(ctx: RunnerContext, out: dict) -> str:
    """Every record of the step a reader could see: the result, the captures."""
    texts = [json.dumps(out)]
    for path in ctx.artifacts_dir.iterdir():
        if path.is_file():
            texts.append(path.read_text(errors="replace"))
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# A missing expected output
# ---------------------------------------------------------------------------


def test_a_missing_expected_output_is_repaired_on_the_first_turn(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "Scanned; findings are in my answer.", "cost": 0.5, "turns": 3},
        {"answer": DONE, "write": {"$ARTIFACTS/scan-01.md": "# findings\n"}, "cost": 0.25,
         "turns": 1},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert [run["resume"] for run in runs] == [None, SESSION]
    # The repair prompt names the file and where it must be written, exactly.
    target = str((ctx.artifacts_dir / "scan-01.md").resolve())
    assert target in runs[1]["prompt"]
    assert out["repair_turns"] == 1
    (turn,) = out["repairs"]
    assert turn["turn"] == 1
    assert turn["missing_outputs"] == ["scan-01.md"]
    assert turn["fixed_outputs"] == ["scan-01.md"]
    assert out["repair_unresolved"] is None
    assert out["summary"] == DONE
    # The spend covers both invocations of the session.
    assert out["structured_output"]["total_cost_usd"] == pytest.approx(0.75)
    assert out["structured_output"]["num_turns"] == 4


def test_an_output_that_is_a_link_is_not_counted_as_written(tmp_path, monkeypatch):
    """The worker refuses a link at upload, so the runner does not take one."""
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("x\n")
    (ctx.artifacts_dir / "scan-01.md").symlink_to(elsewhere)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 3
    assert out["repair_unresolved"]["missing_outputs"] == ["scan-01.md"]


# ---------------------------------------------------------------------------
# A credential-shaped line in the tree
# ---------------------------------------------------------------------------


def test_a_credential_line_is_named_by_path_line_and_rule_and_repaired(tmp_path, monkeypatch):
    value = _value()
    ctx = _ctx(tmp_path, {"prompt": "fix it"}, repo=True)
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "Fixed.",
         "write": {"src/app.py": f'def main():\n    {_PW} = "{value}"\n    return 1\n'}},
        {"answer": DONE,
         "write": {"src/app.py": "import os\n\ndef main():\n    return os.environ['X']\n"}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert [run["resume"] for run in runs] == [None, SESSION]
    assert "src/app.py:2 key_value_assignment" in runs[1]["prompt"]
    # Never the matched text: not in the prompt, the result or any capture.
    assert value not in runs[1]["prompt"]
    assert value not in _everything_written(ctx, out)
    assert out["repair_turns"] == 1
    (turn,) = out["repairs"]
    assert turn["credential_lines"] == ["src/app.py:2 key_value_assignment"]
    assert turn["fixed_lines"] == ["src/app.py:2 key_value_assignment"]
    assert out["repair_unresolved"] is None


def test_an_untracked_file_with_a_credential_is_flagged_too(tmp_path, monkeypatch):
    """The worker's `git add -A` commits it, so the scan reads it (publish_scan)."""
    value = _value()
    ctx = _ctx(tmp_path, {"prompt": "fix it"}, repo=True)
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "Fixed.", "write": {"deploy/settings.json": f'{{\n  "{_PW}": "{value}"\n}}\n'}},
        {"answer": DONE, "write": {"deploy/settings.json": "{}\n"}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert "deploy/settings.json:2 key_value_assignment" in runs[1]["prompt"]
    assert out["repair_turns"] == 1


# ---------------------------------------------------------------------------
# Two failed repairs, and nothing to repair
# ---------------------------------------------------------------------------


def test_two_failed_repairs_end_the_attempt_as_today(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [{"answer": "I wrote it."}])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert cliagent.REPAIR_MAX_TURNS == 2
    assert [run["resume"] for run in runs] == [None, SESSION, SESSION]
    assert out["repair_turns"] == 2
    assert [turn["fixed_outputs"] for turn in out["repairs"]] == [[], []]
    assert out["repair_unresolved"] == {"missing_outputs": ["scan-01.md"], "credential_lines": []}
    # It returns the way it always has: the worker's own end-of-attempt check
    # still fails the attempt for the missing output (`_fail_for_missing_outputs`).
    assert out["exit_code"] == 0
    assert ctx.report["repair_turns"] == 2


def test_no_check_failure_means_no_resume(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it", "expected_outputs": ["scan-01.md"]}, repo=True)
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": DONE, "write": {"$ARTIFACTS/scan-01.md": "ok\n", "src/new.py": "VALUE = 3\n"}},
    ])
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0
    assert out["repairs"] == []
    assert out["repair_unresolved"] is None
    assert out["repair_skipped"] is None


def test_a_scan_that_cannot_run_is_not_read_as_a_failure_to_repair(tmp_path, monkeypatch):
    """No clone base: the scan says it could not run (never clean); the runner
    does not invent a repair from it, and records why it did not scan."""
    ctx = _ctx(tmp_path, {"prompt": "fix it"}, repo=True)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    monkeypatch.setenv("SWARM_CLONE_BASE", "0" * 40)
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0
    assert "names no commit" in out["repair_scan_error"]


# ---------------------------------------------------------------------------
# The limits of a repair
# ---------------------------------------------------------------------------


def test_no_repair_when_the_remaining_budget_is_below_the_minimum(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"],
                          "timeout_seconds": 20}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    assert cliagent.FINISH_MIN_SECONDS > 20
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0
    assert out["repair_skipped"] == "budget"
    assert out["repair_unresolved"]["missing_outputs"] == ["scan-01.md"]


def test_no_repair_without_a_session_id(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}], session="bad id!")
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0
    assert out["repair_skipped"] == "no_session"


def test_a_finish_pass_runs_before_the_repair_checks(tmp_path, monkeypatch):
    """The checks read the tree the agent's LAST turn left: an answer that
    announced pending work is resumed to finish first, and only what is still
    wrong after that is repaired."""
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "The suite is still running, I'll report."},
        {"answer": DONE},
        {"answer": DONE, "write": {"$ARTIFACTS/scan-01.md": "ok\n"}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert len(runs) == 3
    assert runs[1]["prompt"] == cliagent.FINISH_PROMPT
    assert "scan-01.md" in runs[2]["prompt"]
    assert out["resumed_to_finish"] is True
    assert out["repair_turns"] == 1


def test_the_session_id_never_reaches_the_runner_log(tmp_path, monkeypatch, capfd):
    ctx = _ctx(tmp_path, {"prompt": "scan it", "expected_outputs": ["scan-01.md"]}, repo=False)
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "Done."},
        {"answer": DONE, "write": {"$ARTIFACTS/scan-01.md": "ok\n"}},
    ])
    claude_code.body(ctx)
    err = capfd.readouterr().err
    assert SESSION not in err
    assert "repair turn 1 of 2" in err


def test_codex_never_takes_repair_turns():
    from agent_worker.runners import codex

    assert codex.SPEC.repair_checks is False
    assert claude_code.SPEC.repair_checks is True
