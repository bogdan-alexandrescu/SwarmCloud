"""The claude-code runner trusts the attempt's own checkout, so the
repository's `.claude/settings.json` allow rules apply (WK-TRUST, part of #1194).

Measured 2026-10-12 (task_4e0cf1c649634b949703, attempt
att_75e17009c2a7418b85e7): the agent_stderr of every claude-code(-browser)
attempt began with "Ignoring 23 permissions.allow entries from
.claude/settings.json: this workspace has not been trusted", and named the fix:
`projects["/workspace/<attempt>/work/repo"].hasTrustDialogAccepted: true` in
`/workspace/<attempt>/work/.claude.json`. HOME is the attempt's `work/`, so
that is the file the CLI reads.

So before the CLI starts the runner MERGES that one flag into the file, for
the exact checkout path of this attempt. Merged, not overwritten: the CLI keeps
its own state there, and a resumed attempt restores `work/` from a checkpoint
that holds an earlier attempt's entry. Only the flag is added: trust makes the
repository's own settings apply -- its deny rules included -- and grants
nothing those settings do not.

These run the real claude-code runner against a fake CLI that copies the
`.claude.json` it finds at launch.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

SESSION = "sess-" + "7a6b5c4d3e2f1001"


def _key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _ctx(root: Path, *, repo: bool = True) -> RunnerContext:
    work = root / "work"
    artifacts = root / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    checkout = None
    if repo:
        checkout = work / "repo"
        checkout.mkdir(exist_ok=True)
        subprocess.run(["git", "-C", str(checkout), "init", "-q", "-b", "main"], check=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": "Say done."},
        repo_dir=checkout,
    )


#: Copies `$HOME/.claude.json` as it is at launch to `$HOME/seen-config.json`
#: (or records that there was none), then ends the session.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
config = home / ".claude.json"
seen = json.loads(config.read_text()) if config.exists() else None
(home / "seen-config.json").write_text(json.dumps({"config": seen, "cwd": os.getcwd()}))
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "SESSION"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "SESSION", "result": "Done.",
                  "total_cost_usd": 0.1, "num_turns": 1}), flush=True)
'''.replace("SESSION", SESSION)


def _run(tmp_path: Path, monkeypatch, ctx: RunnerContext) -> dict:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    monkeypatch.setenv("ANTHROPIC_API_KEY", _key())
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
                 claude_code.BROWSERS_PATH_ENV):
        monkeypatch.delenv(name, raising=False)
    claude_code.body(ctx)
    return json.loads((ctx.work_dir / "seen-config.json").read_text())


def test_the_config_the_cli_reads_at_launch_trusts_the_attempts_checkout(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path / "att_new")
    seen = _run(tmp_path, monkeypatch, ctx)
    assert seen["config"] is not None, "no .claude.json in HOME when the CLI started"
    repo = str(ctx.repo_dir)
    assert seen["config"]["projects"][repo]["hasTrustDialogAccepted"] is True
    # The path trusted is the one the CLI was started in.
    assert Path(seen["cwd"]).resolve() == Path(repo).resolve()


def test_existing_keys_in_the_cli_config_survive(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path / "att_new")
    repo = str(ctx.repo_dir)
    before = {
        "numStartups": 7,
        "theme": "dark",
        "projects": {
            "/somewhere/else": {"lastCost": 2.5, "hasTrustDialogAccepted": False},
            repo: {"lastCost": 1.25, "allowedTools": []},
        },
    }
    (ctx.work_dir / ".claude.json").write_text(json.dumps(before))
    seen = _run(tmp_path, monkeypatch, ctx)["config"]
    assert seen["numStartups"] == 7
    assert seen["theme"] == "dark"
    # Another project's entry is untouched, its explicit "not trusted" included.
    assert seen["projects"]["/somewhere/else"] == {"lastCost": 2.5, "hasTrustDialogAccepted": False}
    # This project's own entry gains the flag and nothing else.
    assert seen["projects"][repo] == {"lastCost": 1.25, "allowedTools": [], "hasTrustDialogAccepted": True}


def test_a_resumed_attempt_trusts_its_own_checkout_path(tmp_path, monkeypatch):
    """`work/` travels in checkpoints: the restored config holds the earlier
    attempt's path, and the new attempt's checkout is somewhere else."""
    earlier = _ctx(tmp_path / "att_old")
    _run(tmp_path, monkeypatch, earlier)
    later = _ctx(tmp_path / "att_later", repo=False)
    shutil.copy2(earlier.work_dir / ".claude.json", later.work_dir / ".claude.json")
    later = _ctx(tmp_path / "att_later")
    seen = _run(tmp_path, monkeypatch, later)["config"]
    assert str(later.repo_dir) != str(earlier.repo_dir)
    assert seen["projects"][str(later.repo_dir)]["hasTrustDialogAccepted"] is True
    # The restored entry is carried, not dropped.
    assert seen["projects"][str(earlier.repo_dir)]["hasTrustDialogAccepted"] is True


def test_trust_adds_no_permission_of_its_own(tmp_path, monkeypatch):
    """Trust only lets the repository's own settings apply, deny rules
    included; the runner writes no allow list, tool list or mode."""
    ctx = _ctx(tmp_path / "att_new")
    seen = _run(tmp_path, monkeypatch, ctx)["config"]
    assert seen == {"projects": {str(ctx.repo_dir): {"hasTrustDialogAccepted": True}}}
    settings = json.loads(
        (ctx.work_dir / claude_code.SETTINGS_DIR_NAME / "settings.json").read_text()
    )
    assert "permissions" not in settings


def test_without_a_checkout_nothing_is_trusted(tmp_path, monkeypatch):
    """No repository, no repository settings to apply: HOME is not marked trusted."""
    ctx = _ctx(tmp_path / "att_new", repo=False)
    seen = _run(tmp_path, monkeypatch, ctx)["config"]
    assert seen is None or str(ctx.work_dir) not in seen.get("projects", {})


def test_a_link_left_at_the_config_path_is_replaced_not_followed(tmp_path):
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"untouched": true}')
    os.symlink(outside, work / ".claude.json")
    path = claude_code.trust_workspace(work, repo)
    assert outside.read_text() == '{"untouched": true}'
    assert not path.is_symlink()
    assert json.loads(path.read_text())["projects"][str(repo)]["hasTrustDialogAccepted"] is True
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"projects": [1]}', '{"projects": {"X": 3}}'])
def test_an_unreadable_config_still_ends_up_trusting_the_checkout(tmp_path, text):
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    (work / ".claude.json").write_text(text.replace("X", str(repo)))
    config = json.loads(claude_code.trust_workspace(work, repo).read_text())
    assert config["projects"][str(repo)]["hasTrustDialogAccepted"] is True
