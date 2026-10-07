"""A claude-code Bash command reads stdin from /dev/null, not from nothing (#750).

The CLI's Bash tool attaches `< /dev/null` to a command itself, but not to one
that carries a heredoc or any `<` redirect (2.1.283), so a stray `cat` or
`read` in such a command waited on a stdin nobody would ever close until the
Bash timeout killed it -- two minutes then, ten since #736. The runner's
headless settings register a PreToolUse hook for Bash that returns the
command, through the CLI's documented `hookSpecificOutput.updatedInput`,
wrapped as `{ <command>\\n} < /dev/null`: only the OUTER stdin is replaced, so a
pipe or a redirect inside it is untouched, and a command that already
redirects stdin passes through unchanged. Input the hook cannot read passes
through too (fail open).

The rewritten commands are run here under bash with a stdin pipe the test
never closes, which is exactly what made the original hang.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_worker.runners import claude_code

BASH = shutil.which("bash")


def _settings(tmp_path: Path) -> dict:
    return json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())


def _stdin_entry(settings: dict) -> dict:
    entries = [
        entry
        for entry in settings["hooks"]["PreToolUse"]
        if any(claude_code.STDIN_HOOK_NAME in hook["command"] for hook in entry["hooks"])
    ]
    assert len(entries) == 1, settings["hooks"]["PreToolUse"]
    return entries[0]


def _run_hook(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(claude_code.STDIN_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _call(command: object, **extra: object) -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, **extra},
        }
    )


def _rewritten(command: str, **extra: object) -> dict | None:
    """The tool input the hook hands back, or None when it passes the call through."""
    done = _run_hook(_call(command, **extra))
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return None
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    # No permission decision: the CLI applies an updatedInput on its own, and
    # the hook must not allow anything the background refusal denies.
    assert "permissionDecision" not in out
    return out["updatedInput"]


def _bash_with_open_stdin(command: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run `command` with stdin a pipe that is never written to nor closed."""
    with subprocess.Popen(
        [BASH, "-c", command],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            # Not `communicate()`: it closes stdin first, curing the very hang
            # this measures. The outputs are small enough not to fill a pipe.
            proc.wait(timeout=10)
            out, err = proc.stdout.read(), proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


# --- the settings register it ------------------------------------------------


def test_the_headless_settings_register_the_stdin_hook_for_bash(tmp_path):
    entry = _stdin_entry(_settings(tmp_path))
    assert entry["matcher"] == "Bash"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    # The script ships in the worker package and is named by absolute path.
    assert claude_code.STDIN_HOOK.is_absolute()
    assert claude_code.STDIN_HOOK.is_file()
    assert claude_code.STDIN_HOOK.parent == Path(claude_code.__file__).resolve().parent
    assert str(claude_code.STDIN_HOOK) in hook["command"]
    assert shlex.split(hook["command"])[0] == sys.executable


def test_the_background_refusal_stays_registered_beside_it(tmp_path):
    entries = _settings(tmp_path)["hooks"]["PreToolUse"]
    assert len(entries) == 2
    assert any("refuse-background.py" in h["command"] for e in entries for h in e["hooks"])


# --- the rewrite ---------------------------------------------------------------


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_a_bare_stdin_read_hangs_without_the_rewrite(tmp_path):
    # The control: without the hook, this is the issue's hang.
    with pytest.raises(subprocess.TimeoutExpired):
        with subprocess.Popen([BASH, "-c", "cat > /dev/null"], stdin=subprocess.PIPE) as proc:
            try:
                proc.wait(timeout=1)
            finally:
                proc.kill()
                proc.wait()


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_the_hook_rewrites_cat_to_dev_null_so_it_returns_at_once(tmp_path):
    updated = _rewritten("cat > /dev/null", description="drain", timeout=5000)
    assert updated is not None
    # Every other field of the call is handed back as it came.
    assert updated["description"] == "drain"
    assert updated["timeout"] == 5000
    assert updated["command"] != "cat > /dev/null"
    assert "cat > /dev/null" in updated["command"]
    done = _bash_with_open_stdin(updated["command"], tmp_path)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_read_after_a_heredoc_sees_end_of_file(tmp_path):
    # The shape the CLI does not protect itself: a heredoc, then a stdin read.
    command = "cat <<'EOF'\nbody\nEOF\nread x || echo eof"
    updated = _rewritten(command)
    done = _bash_with_open_stdin(updated["command"], tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "body\neof\n"


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_a_heredoc_body_with_a_less_than_sign_is_still_rewritten(tmp_path):
    # The issue's measured shape: PR text in a heredoc, then a stray stdin
    # read. A `<` in the body is data, not a redirect of the command's stdin.
    # Two heredocs on one line: their bodies follow in turn, and the last wins.
    command = (
        "cat <<EOF\n<!-- template -->\n<details>\na < b\nEOF\n"
        "cat <<-'X' <<\"Y\"\n\t<x>\n\tX\n< y\nY\n"
        "cat > /dev/null"
    )
    updated = _rewritten(command)
    assert updated is not None
    done = _bash_with_open_stdin(updated["command"], tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "<!-- template -->\n<details>\na < b\n< y\n"


def test_a_stdin_redirect_after_a_heredoc_is_still_left_alone():
    assert _rewritten("cat <<EOF\nbody\nEOF\ncat < file") is None
    assert _rewritten("cat <<EOF < file\nbody\nEOF") is None


def test_an_unterminated_heredoc_is_left_alone():
    # Wrapped, the body would take in the closing brace: a syntax error where
    # bash alone runs it with a warning.
    assert _rewritten("cat <<EOF\nno terminator") is None
    assert _rewritten("cat <<EOF\nEOF2") is None


def test_a_herestring_is_not_a_heredoc():
    updated = _rewritten("cat <<< word\ncat > /dev/null")
    assert updated is not None


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_a_pipe_between_commands_still_gets_its_pipe(tmp_path):
    updated = _rewritten("printf x | cat")
    # The pipeline itself is not edited: it is carried verbatim inside the wrap.
    assert "printf x | cat" in updated["command"]
    done = _bash_with_open_stdin(updated["command"], tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "x"


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_the_wrap_keeps_exit_status_cwd_and_a_trailing_comment(tmp_path):
    (tmp_path / "sub").mkdir()
    updated = _rewritten("cd sub && pwd -P # a comment")
    done = _bash_with_open_stdin(updated["command"] + "\npwd -P", tmp_path)
    assert done.stdout.split() == [str((tmp_path / "sub").resolve())] * 2
    updated = _rewritten("exit 3")
    assert _bash_with_open_stdin(updated["command"], tmp_path).returncode == 3


@pytest.mark.parametrize(
    "command",
    [
        "cmd < file",
        "sort <input.txt",
        "wc -l 0< data",
        "cat < /dev/null",
    ],
)
def test_a_command_that_already_redirects_stdin_is_left_alone(command):
    assert _rewritten(command) is None


@pytest.mark.skipif(BASH is None, reason="needs bash")
def test_a_command_reading_a_file_by_redirect_still_reads_it(tmp_path):
    (tmp_path / "file").write_text("from file\n")
    assert _rewritten("cat < file") is None
    done = _bash_with_open_stdin("cat < file", tmp_path)
    assert done.stdout == "from file\n"


# --- fail open -----------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        "[1, 2]",
        json.dumps({"tool_name": "Bash"}),
        json.dumps({"tool_name": "Bash", "tool_input": "cat"}),
        _call(None),
        _call(42),
        _call(""),
        _call("   \n"),
        # A trailing backslash would continue onto the closing brace.
        _call("echo a \\"),
        json.dumps({"tool_name": "Read", "tool_input": {"command": "cat"}}),
    ],
)
def test_input_the_hook_cannot_use_passes_the_command_through(payload):
    done = _run_hook(payload)
    assert done.returncode == 0
    assert done.stdout == ""
