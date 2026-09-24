"""The runners themselves, in-process.

Every other worker test drives the mock runner as a genuine subprocess, which is
the right shape for the lifecycle but means no runner SOURCE is ever executed by
the test process: `--cov` reported 0% for all six runner modules. Two of the
untested paths decide security outcomes and one decides an invariant, so they
are covered here directly:

* `generic`'s command catalogue and its argument validation. The generic profile
  used to take `input.argv` plus `input.script` and run them, which is an
  authenticated remote shell in the tenant's pod however the allowlist is
  phrased. What replaced it is a fixed argv per NAME, and these tests are what
  keep it that way -- a future edit that reintroduces a caller-supplied command
  fails here.
* `runners/limits`. A caller may LOWER an execution limit and never raise one.
  Without that, `max_stdout_bytes: 10**12` removes the cap and a task fills the
  ephemeral disk the resource class sizes until the kubelet evicts the pod.
* `cliagent.detect_rate_limit`. This is invariant 4's trigger: it decides whether
  a provider 429 becomes a park (costing nothing) or burns one of the task's
  three attempts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker.runners import browser, generic
from agent_worker.runners.base import RunnerContext, RunnerFailure
from agent_worker.runners.cliagent import detect_rate_limit
from agent_worker.runners.limits import (
    HARD_MAX_STDOUT_BYTES,
    HARD_MAX_TIMEOUT_SECONDS,
    ChildLimits,
    platform_ceilings,
    resolve_limits,
)


def make_ctx(tmp_path: Path, payload: dict) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# generic: invariant 10 -- a caller names a command, never supplies one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["argv", "script", "env", "command_line", "shell"])
def test_the_generic_runner_refuses_a_caller_supplied_command(tmp_path, field):
    """`{"argv": ["bash"], "script": "..."}` was arbitrary code execution inside
    the tenant's pod, holding its Workload Identity and its provider key. The
    contract says a caller never supplies a COMMAND, and an argv is a command."""
    ctx = make_ctx(tmp_path, {"command": "pytest", field: ["bash"]})
    with pytest.raises(RunnerFailure) as exc:
        generic.body(ctx)
    assert field in str(exc.value)


def test_every_catalogue_argv_is_a_constant_in_this_module():
    """Nothing a caller sends contributes to argv[0], and no entry takes a flag
    from the caller, so no caller value can become one."""
    for command in generic.GENERIC_COMMANDS.values():
        assert isinstance(command.argv, tuple) and command.argv
        assert "/" not in command.argv[0]
        assert not command.argv[0].startswith("-")
        assert all(isinstance(part, str) for part in command.argv)


def test_an_unknown_command_name_fails_listing_the_catalogue(tmp_path):
    with pytest.raises(RunnerFailure) as exc:
        generic.resolve_command("bash")
    assert "unknown generic command" in str(exc.value)
    for name in generic.GENERIC_COMMANDS:
        assert name in str(exc.value)


@pytest.mark.parametrize("name", [None, "", 7, ["pytest"]])
def test_a_missing_command_name_is_a_clean_failure(name):
    with pytest.raises(RunnerFailure):
        generic.resolve_command(name)


@pytest.mark.parametrize(
    "value",
    [
        "-rf",                      # a leading dash is how a value becomes a flag
        "--upload-pack=/bin/sh",
        "../../etc/passwd",
        "a\nb",
        "a;b",
        "$(id)",
        "a b",
        "",
    ],
)
def test_a_caller_argument_that_could_become_a_flag_or_a_command_is_refused(value):
    with pytest.raises(RunnerFailure):
        generic._check_argument(value, field="input.paths")


def test_paths_must_exist_inside_the_workspace(tmp_path):
    ctx = make_ctx(tmp_path, {})
    command = generic.GENERIC_COMMANDS["pytest"]
    (ctx.work_dir / "tests").mkdir()

    assert generic._resolve_paths(ctx, ["tests"], command) == [
        str((ctx.work_dir / "tests").resolve())
    ]
    with pytest.raises(RunnerFailure, match="does not exist"):
        generic._resolve_paths(ctx, ["nope"], command)
    with pytest.raises(RunnerFailure):
        generic._resolve_paths(ctx, ["../outside"], command)
    with pytest.raises(RunnerFailure, match="must be a list"):
        generic._resolve_paths(ctx, "tests", command)
    with pytest.raises(RunnerFailure, match="limited to"):
        generic._resolve_paths(ctx, ["tests"] * (command.max_arguments + 1), command)


def test_the_working_directory_must_be_a_directory_inside_the_workspace(tmp_path):
    ctx = make_ctx(tmp_path, {"working_directory": "repo"})
    with pytest.raises(RunnerFailure, match="not a directory"):
        generic._resolve_working_directory(ctx)
    (ctx.work_dir / "repo").mkdir()
    assert generic._resolve_working_directory(ctx) == (ctx.work_dir / "repo").resolve()

    ctx.payload["working_directory"] = "../elsewhere"
    with pytest.raises(RunnerFailure):
        generic._resolve_working_directory(ctx)


def test_the_generic_runner_never_executes_a_file_from_the_workspace(tmp_path):
    """`_resolve_program` refuses anything path-shaped. A task that drops a
    binary in its own workspace and names it cannot get it started."""
    dropped = tmp_path / "work" / "payload.sh"
    dropped.parent.mkdir(parents=True, exist_ok=True)
    dropped.write_text("#!/bin/sh\necho pwned\n")
    dropped.chmod(0o700)

    for program in (str(dropped), "./payload.sh", "-c", "", "/bin/sh"):
        with pytest.raises(RunnerFailure):
            generic._resolve_program(program)


def test_a_catalogue_program_missing_from_the_image_fails_readably():
    with pytest.raises(RunnerFailure, match="is not installed in this image"):
        generic._resolve_program("definitely-not-a-real-program-9f3a")


def test_the_child_environment_is_built_not_inherited(tmp_path, monkeypatch):
    """An environment variable is a command in disguise: NODE_OPTIONS,
    PYTHONSTARTUP, LD_PRELOAD and PATH all change which code runs. The caller
    contributes nothing, and neither does the runner's own environment."""
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("PYTHONSTARTUP", "/tmp/evil.py")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-a-build")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/var/creds.json")

    ctx = make_ctx(tmp_path, {"env": {"LD_PRELOAD": "/x"}, "PATH": "/x"})
    env = generic._build_env(ctx)

    for leaked in (
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONSTARTUP",
        "ANTHROPIC_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        assert leaked not in env
    assert env["PATH"] == generic.BASE_PATH
    assert env["HOME"] == str(ctx.work_dir)


def test_the_generic_runner_runs_its_catalogue_command_for_real(tmp_path, monkeypatch):
    """End to end through `run_child`, with a catalogue entry pointed at an
    interpreter that exists on this machine, so the argv assembly, the limits and
    the artifact capture are all exercised rather than asserted."""
    monkeypatch.setitem(
        generic.GENERIC_COMMANDS,
        "selftest",
        generic.GenericCommand(
            name="selftest",
            argv=("python3", "-c", "import sys; print('generic ok'); sys.exit(0)"),
            description="test-only entry",
        ),
    )
    monkeypatch.setattr(generic, "BASE_PATH", os.environ.get("PATH", "/usr/bin:/bin"))

    ctx = make_ctx(tmp_path, {"command": "selftest"})
    out = generic.body(ctx)

    assert out["exit_code"] == 0
    assert "generic ok" in out["stdout_tail"]
    assert (ctx.artifacts_dir / "command.stdout.log").exists()


def test_a_failing_catalogue_command_raises_with_the_stderr_tail(tmp_path, monkeypatch):
    monkeypatch.setitem(
        generic.GENERIC_COMMANDS,
        "selftest-fail",
        generic.GenericCommand(
            name="selftest-fail",
            argv=("python3", "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"),
            description="test-only entry",
        ),
    )
    monkeypatch.setattr(generic, "BASE_PATH", os.environ.get("PATH", "/usr/bin:/bin"))

    ctx = make_ctx(tmp_path, {"command": "selftest-fail"})
    with pytest.raises(RunnerFailure) as exc:
        generic.body(ctx)
    assert "exited 3" in str(exc.value)
    assert "boom" in str(exc.value)


# ---------------------------------------------------------------------------
# limits: a caller may lower a limit, never raise one
# ---------------------------------------------------------------------------


def test_a_caller_may_lower_an_execution_limit():
    ceilings = ChildLimits(
        timeout_seconds=600, grace_seconds=20, max_stdout_bytes=1000, max_stderr_bytes=500
    )
    limits = resolve_limits({"timeout_seconds": 30, "max_stdout_bytes": 100}, ceilings)
    assert limits.timeout_seconds == 30
    assert limits.max_stdout_bytes == 100
    assert limits.clamped == ()


def test_a_caller_may_not_raise_an_execution_limit():
    """`max_stdout_bytes: 10**12` used to remove the cap entirely."""
    ceilings = ChildLimits(
        timeout_seconds=600, grace_seconds=20, max_stdout_bytes=1000, max_stderr_bytes=500
    )
    limits = resolve_limits(
        {
            "timeout_seconds": 10**9,
            "grace_seconds": 10**6,
            "max_stdout_bytes": 10**12,
            "max_stderr_bytes": 10**12,
        },
        ceilings,
    )
    assert limits.timeout_seconds == 600
    assert limits.grace_seconds == 20
    assert limits.max_stdout_bytes == 1000
    assert limits.max_stderr_bytes == 500
    assert set(limits.clamped) == {
        "timeout_seconds",
        "grace_seconds",
        "max_stdout_bytes",
        "max_stderr_bytes",
    }


@pytest.mark.parametrize("bad", [0, -1, "abc", None, {}, []])
def test_an_unusable_requested_limit_falls_back_to_the_ceiling(bad):
    ceilings = ChildLimits(
        timeout_seconds=600, grace_seconds=20, max_stdout_bytes=1000, max_stderr_bytes=500
    )
    limits = resolve_limits({"timeout_seconds": bad}, ceilings)
    assert limits.timeout_seconds == 600


def test_the_platform_ceiling_comes_from_the_worker_and_is_itself_bounded():
    """The worker exports its own config; the hard maximum bounds even a
    doctored worker environment."""
    env = {
        "SWARM_CHILD_TIMEOUT_SECONDS": "900",
        "SWARM_CHILD_MAX_STDOUT_BYTES": str(10**15),
    }
    ceilings = platform_ceilings(env)
    assert ceilings.timeout_seconds == 900
    assert ceilings.max_stdout_bytes == HARD_MAX_STDOUT_BYTES
    assert ceilings.timeout_seconds <= HARD_MAX_TIMEOUT_SECONDS


def test_the_generic_runner_clamps_against_the_workers_exported_ceiling(tmp_path, monkeypatch):
    monkeypatch.setitem(
        generic.GENERIC_COMMANDS,
        "selftest-limits",
        generic.GenericCommand(
            name="selftest-limits",
            argv=("python3", "-c", "print('x')"),
            description="test-only entry",
        ),
    )
    monkeypatch.setattr(generic, "BASE_PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    monkeypatch.setenv("SWARM_CHILD_MAX_STDOUT_BYTES", "4096")
    monkeypatch.setenv("SWARM_CHILD_TIMEOUT_SECONDS", "30")

    ctx = make_ctx(tmp_path, {"command": "selftest-limits", "max_stdout_bytes": 10**12})
    out = generic.body(ctx)

    assert out["limits"]["max_stdout_bytes"] == 4096
    assert "max_stdout_bytes" in out["limits"]["clamped"]


# ---------------------------------------------------------------------------
# cliagent: the rate-limit heuristic is invariant 4's trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [
        ('{"type":"rate_limit_error"}', None),
        ("HTTP 429 Too Many Requests", None),
        ("Retry-After: 900", 900),
        ('{"error":"rate limit","retry_after":42}', 42),
        ("You are rate-limited. Try again in 5 minutes.", 300),
        ("usage limit reached; resets in 30 minutes", 1800),
        ("Error: insufficient_quota", None),
        ("resource_exhausted", None),
        ("overloaded_error", None),
    ],
)
def test_a_provider_rate_limit_is_recognised(text, seconds):
    """An unrecognised rate limit becomes an ordinary failure and burns one of
    the task's three attempts on something that is not the task's fault."""
    hit, retry_after, _ = detect_rate_limit(text)
    assert hit is True
    assert retry_after == seconds


@pytest.mark.parametrize(
    "text",
    [
        "",
        "the build succeeded",
        "ModuleNotFoundError: No module named 'foo'",
        "connection reset by peer",
    ],
)
def test_an_ordinary_failure_is_not_mistaken_for_a_rate_limit(text):
    hit, retry_after, reset_at = detect_rate_limit(text)
    assert (hit, retry_after, reset_at) == (False, None, None)


def test_a_reset_timestamp_is_extracted_when_the_cli_reports_one():
    hit, _, reset_at = detect_rate_limit(
        '{"type":"rate_limit_error","reset_at":"2026-09-16T12:00:00Z"}'
    )
    assert hit is True
    assert reset_at == "2026-09-16T12:00:00Z"


# ---------------------------------------------------------------------------
# browser: no arbitrary scheme, no unbounded action list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://host/x", "javascript:alert(1)", "", None, 7, "https://"],
)
def test_the_browser_runner_refuses_a_url_it_should_not_fetch(url):
    with pytest.raises(RunnerFailure):
        browser._check_url(url)


@pytest.mark.parametrize("url", ["https://example.com/a", "http://example.com"])
def test_the_browser_runner_accepts_ordinary_web_urls(url):
    assert browser._check_url(url) == url


def test_the_browser_runner_has_no_evaluate_javascript_action():
    """Arbitrary script execution in a browser holding a tenant's session is an
    exfiltration primitive. The action list is a closed set."""
    source = Path(browser.__file__).read_text()
    assert "page.evaluate" not in source
    assert "add_init_script" not in source


# ---------------------------------------------------------------------------
# mock: the runner every smoke and concurrency test depends on
# ---------------------------------------------------------------------------


def test_the_mock_runner_resumes_from_state_left_by_a_previous_attempt(tmp_path):
    from agent_worker.runners import mock

    ctx = make_ctx(tmp_path, {"steps": 4, "sleep_seconds": 0.0, "prompt": "hi"})
    first = mock.body(ctx)
    assert first["completed_steps"] == 4
    assert first["was_resumed"] is False

    # A resumed attempt finds the state file the checkpoint carried over.
    again = mock.body(make_ctx(tmp_path, {"steps": 4, "sleep_seconds": 0.0}))
    assert again["was_resumed"] is True
    assert again["completed_steps"] == 4


def test_the_mock_runner_needs_no_provider_key(tmp_path, monkeypatch):
    """It is the only runner that works for a brand-new tenant, which is what
    makes "is the execution plane healthy?" answerable without a third party."""
    from swarm_common.profiles import RUNNER_PROFILES

    assert RUNNER_PROFILES["mock"].provider is None
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    out = __import__(
        "agent_worker.runners.mock", fromlist=["body"]
    ).body(make_ctx(tmp_path, {"steps": 1, "sleep_seconds": 0.0}))
    assert out["completed_steps"] == 1


def test_each_runner_module_is_executable_as_the_catalogue_names_it():
    """`RUNNER_PROFILES[...].runner_argv` is `python -m agent_worker.runners.<name>`,
    and the worker lifecycle starts exactly that as its child. A module that cannot be imported as
    `__main__` is a dispatch-time outage, so every one is checked here."""
    from swarm_common.profiles import RUNNER_PROFILES

    for profile in RUNNER_PROFILES.values():
        module = profile.runner_argv[-1]
        assert profile.runner_argv[:2] in (("python", "-m"), ("python3", "-m"))
        result = subprocess.run(
            [sys.executable, "-c", f"import {module} as m; assert callable(m.main)"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"


def test_a_runner_that_writes_no_result_is_reported_as_a_failure(tmp_path):
    """"Succeeded with no result" is indistinguishable from "was killed before it
    could write anything", so `run_runner` always leaves a result.json behind."""
    from agent_worker.runners.base import EXIT_FAILED, run_runner

    work = tmp_path / "work"
    work.mkdir()
    env = {
        "SWARM_WORK_DIR": str(work),
        "SWARM_ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        "SWARM_INPUT": str(work / "input.json"),
        "SWARM_RESULT": str(work / "result.json"),
        "SWARM_QUOTA_SIGNAL": str(work / "quota.json"),
    }
    (work / "input.json").write_text(json.dumps({}))

    original = dict(os.environ)
    os.environ.update(env)
    try:
        code = run_runner(lambda ctx: (_ for _ in ()).throw(RunnerFailure("nope")), name="t")
    finally:
        os.environ.clear()
        os.environ.update(original)

    assert code == EXIT_FAILED
    written = json.loads((work / "result.json").read_text())
    assert written["status"] == "failed"
    assert written["error"] == "nope"


# ---------------------------------------------------------------------------
# cliagent end to end: the CLI runners' only shared code path
# ---------------------------------------------------------------------------


def _fake_cli(tmp_path: Path, body: str) -> Path:
    """A stand-in for `claude` / `codex`: a real executable, run for real."""
    binary = tmp_path / "fake-cli"
    binary.write_text("#!/usr/bin/env python3\n" + body)
    binary.chmod(0o755)
    return binary


def test_a_cli_agent_run_redacts_the_key_from_every_channel_it_produces(
    tmp_path, monkeypatch
):
    """`run_cli_agent` writes the CLI's raw stdout and stderr into `artifacts/`,
    writes the transcript, and returns up to 2000 characters of that stdout as
    the summary that becomes `task.result_summary` in Firestore. All three
    outlive the pod, and this runner is the one holding the key.

    This test is also the regression for a NameError: `_SENSITIVE_PASSTHROUGH`
    was referenced here and defined nowhere, so every claude-code and codex
    attempt crashed at the redaction step -- and nothing executed this function.
    """
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    key = "sk-ant-supersecret-value-0123456789"
    cli = _fake_cli(
        tmp_path,
        "import json, os, sys\n"
        "print(json.dumps({'result': 'done with key ' + os.environ['FAKE_KEY']}))\n"
        "print('debug: FAKE_KEY=' + os.environ['FAKE_KEY'], file=sys.stderr)\n",
    )
    monkeypatch.setenv("FAKE_KEY", key)
    monkeypatch.setenv("FAKE_BIN", str(cli))
    monkeypatch.setenv("HTTPS_PROXY", "http://user:proxypassword123@proxy.internal:8080")

    spec = CliAgentSpec(
        name="fake",
        provider="anthropic",
        binary_env="FAKE_BIN",
        binary_default="fake-cli",
        args_env="FAKE_ARGS",
        args_default=(),
        key_env="FAKE_KEY",
        model_flag=None,
        transcript_name="fake-transcript.json",
    )
    ctx = make_ctx(tmp_path, {"prompt": "do the thing"})
    out = run_cli_agent(ctx, spec)

    assert key not in json.dumps(out)
    assert "proxypassword123" not in json.dumps(out)
    assert "***REDACTED***" in out["summary"]
    for artifact in ("fake.stdout.log", "fake.stderr.log", "fake-transcript.json"):
        body = (ctx.artifacts_dir / artifact).read_text()
        assert key not in body, artifact


def test_a_cli_agent_refuses_to_start_without_the_tenants_key(tmp_path, monkeypatch):
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    monkeypatch.delenv("FAKE_KEY", raising=False)
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY",
    )
    with pytest.raises(RunnerFailure, match="requires FAKE_KEY"):
        run_cli_agent(make_ctx(tmp_path, {"prompt": "hi"}), spec)


def test_a_cli_agent_requires_a_non_empty_prompt(tmp_path, monkeypatch):
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY",
    )
    for payload in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 7}):
        with pytest.raises(RunnerFailure, match="non-empty string input.prompt"):
            run_cli_agent(make_ctx(tmp_path, payload), spec)


def test_a_cli_rate_limit_becomes_a_park_signal_not_a_burned_attempt(tmp_path, monkeypatch):
    """Invariant 4 end to end: a provider 429 must not consume one of the task's
    three attempts."""
    from agent_worker.runners.base import QuotaExhaustedSignal
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    cli = _fake_cli(
        tmp_path,
        "import sys\n"
        "print('{\"type\":\"rate_limit_error\"}', file=sys.stderr)\n"
        "print('Retry-After: 1800', file=sys.stderr)\n"
        "sys.exit(1)\n",
    )
    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(cli))
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY", model_flag=None,
    )

    with pytest.raises(QuotaExhaustedSignal) as exc:
        run_cli_agent(make_ctx(tmp_path, {"prompt": "hi"}), spec)
    assert exc.value.provider == "anthropic"
    assert exc.value.retry_after_seconds == 1800


def test_the_model_is_the_only_other_caller_value_that_reaches_argv(tmp_path, monkeypatch):
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path, "print('{\"result\":\"ok\"}')\n")))
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=(), key_env="FAKE_KEY", model_flag="--model",
    )

    with pytest.raises(RunnerFailure, match="unsupported characters"):
        run_cli_agent(
            make_ctx(tmp_path, {"prompt": "hi", "model": "x; rm -rf /"}), spec
        )
    out = run_cli_agent(make_ctx(tmp_path, {"prompt": "hi", "model": "claude-opus-4"}), spec)
    assert out["model"] == "claude-opus-4"


def test_the_argv_prefix_env_must_be_a_json_list_of_strings(monkeypatch):
    from agent_worker.runners.cliagent import CliAgentSpec, _argv_prefix

    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-cli",
        args_env="FAKE_ARGS", args_default=("--print",), key_env="FAKE_KEY",
    )
    monkeypatch.delenv("FAKE_ARGS", raising=False)
    assert _argv_prefix(spec) == ["--print"]

    monkeypatch.setenv("FAKE_ARGS", '["--print","--json"]')
    assert _argv_prefix(spec) == ["--print", "--json"]

    for bad in ("not json", '"a string"', '[1,2]', '{"a":1}'):
        monkeypatch.setenv("FAKE_ARGS", bad)
        with pytest.raises(RunnerFailure, match="JSON list of strings"):
            _argv_prefix(spec)


def test_the_shipped_cli_specs_name_the_credential_the_frozen_catalogue_declares():
    """A spec whose `key_env` disagreed with `RunnerProfile.secrets` would make
    the runner refuse to start with a credential the worker had just exported."""
    from swarm_common.profiles import RUNNER_PROFILES

    from agent_worker.runners import claude_code, codex

    for module, profile_name in ((claude_code, "claude-code"), (codex, "codex")):
        profile = RUNNER_PROFILES[profile_name]
        assert module.SPEC.name == profile_name
        assert module.SPEC.provider == profile.provider
        assert module.SPEC.key_env in profile.secrets
        assert callable(module.main)


def test_the_claude_code_runner_passes_max_turns_through_as_a_flag(tmp_path, monkeypatch):
    from agent_worker.runners import claude_code

    captured: dict = {}

    def fake_run(ctx, spec, *, extra_args=()):
        captured["extra"] = list(extra_args)
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)

    claude_code.body(make_ctx(tmp_path, {"prompt": "hi"}))
    assert captured["extra"] == []

    claude_code.body(make_ctx(tmp_path, {"prompt": "hi", "max_turns": "12"}))
    assert captured["extra"] == ["--max-turns", "12"]

    # A non-numeric value is refused by int() rather than concatenated into argv.
    with pytest.raises(ValueError):
        claude_code.body(make_ctx(tmp_path, {"prompt": "hi", "max_turns": "3; id"}))
