"""The generic runner: a NAMED command from this module's catalogue.

This is the platform's escape valve for work that is not an agent -- a test
suite, a build, a dependency install. It obeys invariant 10 the same way every
other part of the platform does: **the caller picks a name, and the platform
owns what that name runs.**

An earlier version of this file accepted `input.argv` and `input.script`, ran
the argv through an allowlist that contained `bash`, `sh` and `python`, and
wrote the script into the workspace to pass as a file argument. That is an
authenticated remote shell, however the allowlist is phrased: `{"argv":
["bash"], "script": "..."}` is arbitrary code in the tenant's pod, holding the
tenant's Workload Identity and its provider key. The contract says "Never accept
images, COMMANDS, resource specs or backend parameters from a caller", and an
argv is a command. So there is no caller-supplied argv here any more.

What replaced it:

* **`input.command` names an entry in `GENERIC_COMMANDS`.** The argv is a
  constant in this file. A name that is not in the catalogue is a clean
  failure listing the ones that are.
* **The only caller-varied parts are DATA, and each is validated for its own
  shape**: a make target matched against `^[A-Za-z0-9._][A-Za-z0-9._\\-/]*$`,
  and file paths that must resolve inside the workspace and already exist. No
  entry accepts a flag, so no value a caller sends can become one -- every
  varied value is appended after the fixed argv, and `_check_argument` refuses
  a leading `-`.
* **The environment is built, not inherited, and the caller adds nothing to
  it.** A child's environment decides which interpreter runs and which
  libraries load; `NODE_OPTIONS=--require /tmp/x` is a command by another name.
* **Limits come from the platform.** Timeout, SIGTERM grace and the output caps
  are the worker's, and `input` may only lower them (see `limits.py`).

The commands themselves execute the repository's own code -- `npm test` runs
whatever the cloned repository's package.json says. That is the intended shape
and it is the same trust boundary an agent profile has: the tenant's own code,
in the tenant's own sandbox, started by a command the platform chose.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logs import StructuredLogger
from ..procman import run_child
from .base import RunnerContext, RunnerFailure, run_runner
from .limits import platform_ceilings, resolve_limits

#: Where a program is looked up. `/opt/venv/bin` is first because that is the
#: image's own virtualenv -- the one that has the worker and pytest installed --
#: and a bare `python` that resolved to the system interpreter would be a
#: Python with no packages, which fails in a way nobody can read.
BASE_PATH = "/opt/venv/bin:/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"

#: A value a caller may supply as an argument. No leading dash (that is how a
#: value becomes a flag), no NUL, no newline, no shell metacharacters -- there is
#: no shell in this path, but a newline in an argument is never intentional.
_ARGUMENT_SAFE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}$")


class _ArgKind:
    """What, if anything, a command lets the caller vary."""

    NONE = "none"
    #: `input.paths`: existing paths inside the workspace, appended to the argv.
    PATHS = "paths"
    #: `input.target`: a single make target.
    TARGET = "target"


@dataclass(frozen=True)
class GenericCommand:
    """One admin-defined command. The argv is a constant; only data varies."""

    name: str
    #: argv[0] is a program name looked up on BASE_PATH, never a path.
    argv: tuple[str, ...]
    description: str
    arg_kind: str = _ArgKind.NONE
    #: Default appended when the caller supplies nothing for `arg_kind`.
    default_arguments: tuple[str, ...] = ()
    max_arguments: int = 32


#: The catalogue. Adding an entry is an admin action -- a code change reviewed
#: like any other -- which is exactly the property invariant 10 asks for.
GENERIC_COMMANDS: dict[str, GenericCommand] = {
    "pytest": GenericCommand(
        name="pytest",
        # `python -m pytest`, not `pytest`: the module form uses the same
        # interpreter the image installed the dependencies into, whichever name
        # the console script happens to have.
        argv=("python", "-m", "pytest", "-q", "--color=no"),
        description="run the repository's pytest suite",
        arg_kind=_ArgKind.PATHS,
    ),
    "npm-ci": GenericCommand(
        name="npm-ci",
        argv=("npm", "ci", "--no-audit", "--no-fund"),
        description="install Node dependencies from package-lock.json",
    ),
    "npm-test": GenericCommand(
        name="npm-test",
        argv=("npm", "test", "--silent"),
        description="run the repository's npm test script",
    ),
    "npm-build": GenericCommand(
        name="npm-build",
        argv=("npm", "run", "build", "--silent"),
        description="run the repository's npm build script",
    ),
    "make": GenericCommand(
        name="make",
        argv=("make",),
        description="run one make target from the repository's Makefile",
        arg_kind=_ArgKind.TARGET,
        default_arguments=("all",),
        max_arguments=1,
    ),
    "uv-sync": GenericCommand(
        name="uv-sync",
        argv=("uv", "sync", "--frozen", "--no-progress"),
        description="install Python dependencies from uv.lock",
    ),
}


def resolve_command(name: Any) -> GenericCommand:
    """Look up the caller's chosen command name. Nothing else selects an argv."""
    if not isinstance(name, str) or not name:
        raise RunnerFailure(
            "input.command is required and names one of: "
            + ", ".join(sorted(GENERIC_COMMANDS))
        )
    command = GENERIC_COMMANDS.get(name)
    if command is None:
        raise RunnerFailure(
            f"unknown generic command {name!r}; the catalogue is: "
            + ", ".join(sorted(GENERIC_COMMANDS))
        )
    return command


def _resolve_program(program: str) -> str:
    """Find argv[0] on the image's own PATH, refusing anything path-shaped.

    A program with a separator in it could name a file the task wrote into its
    own workspace, which is the thing this runner must never execute. Every
    catalogue entry uses a bare name, so this can only ever fail on a bad
    catalogue edit or a missing package in the image.
    """
    if not program or program.startswith("-") or "/" in program:
        raise RunnerFailure(
            f"catalogue program {program!r} must be a bare name on PATH; "
            "the generic runner never executes a file from the workspace"
        )
    resolved = shutil.which(program, path=BASE_PATH)
    if not resolved:
        raise RunnerFailure(
            f"program {program!r} is not installed in this image; "
            "images/agent-runtime-base is what the generic profile runs on"
        )
    return resolved


def _check_argument(value: Any, *, field: str) -> str:
    text = str(value)
    if not _ARGUMENT_SAFE.match(text):
        raise RunnerFailure(
            f"{field} entry {text!r} is not allowed: it must start with a letter, "
            "digit, '.' or '_' and contain only those plus '-' and '/'"
        )
    if ".." in text:
        raise RunnerFailure(f"{field} entry {text!r} must not contain '..'")
    return text


def _resolve_paths(ctx: RunnerContext, raw: Any, command: GenericCommand) -> list[str]:
    """Workspace-relative paths, checked to exist INSIDE the work directory."""
    if raw is None:
        return list(command.default_arguments)
    if not isinstance(raw, list):
        raise RunnerFailure("input.paths must be a list of workspace-relative paths")
    if len(raw) > command.max_arguments:
        raise RunnerFailure(f"input.paths is limited to {command.max_arguments} entries")
    work = ctx.work_dir.resolve()
    out: list[str] = []
    for entry in raw:
        relative = _check_argument(entry, field="input.paths")
        candidate = (work / relative).resolve()
        if candidate != work and not str(candidate).startswith(str(work) + os.sep):
            raise RunnerFailure(f"input.paths entry {relative!r} escapes the workspace")
        if not candidate.exists():
            raise RunnerFailure(f"input.paths entry {relative!r} does not exist in the workspace")
        out.append(str(candidate))
    return out


def _resolve_target(raw: Any, command: GenericCommand) -> list[str]:
    if raw is None:
        return list(command.default_arguments)
    return [_check_argument(raw, field="input.target")]


def build_argv(ctx: RunnerContext, command: GenericCommand) -> list[str]:
    """The constant argv from the catalogue, plus validated data."""
    payload = ctx.payload
    program = _resolve_program(command.argv[0])
    argv = [program, *command.argv[1:]]
    if command.arg_kind == _ArgKind.PATHS:
        argv += _resolve_paths(ctx, payload.get("paths"), command)
    elif command.arg_kind == _ArgKind.TARGET:
        argv += _resolve_target(payload.get("target"), command)
    return argv


def _build_env(ctx: RunnerContext) -> dict[str, str]:
    """Built, never inherited, and never extended by the caller.

    An environment variable is a command in disguise -- `NODE_OPTIONS`,
    `PYTHONSTARTUP`, `LD_PRELOAD` and `PATH` all change which code runs -- so
    `input` contributes nothing here.
    """
    return {
        "PATH": BASE_PATH,
        "HOME": str(ctx.work_dir),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "CI": "1",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_FUND": "false",
        "SWARM_WORK_DIR": str(ctx.work_dir),
        "SWARM_ARTIFACTS_DIR": str(ctx.artifacts_dir),
    }


def _resolve_working_directory(ctx: RunnerContext) -> Path:
    """`input.working_directory`, a path, not a command.

    Present because the useful case is "run the build in the repository I asked
    you to clone", and the clone lands in `work/repo`. Validated exactly like
    `input.paths`: inside the workspace, existing, and a directory.
    """
    raw = ctx.payload.get("working_directory")
    work = ctx.work_dir.resolve()
    if raw is None:
        return work
    relative = _check_argument(raw, field="input.working_directory")
    candidate = (work / relative).resolve()
    if candidate != work and not str(candidate).startswith(str(work) + os.sep):
        raise RunnerFailure(f"input.working_directory {relative!r} escapes the workspace")
    if not candidate.is_dir():
        raise RunnerFailure(
            f"input.working_directory {relative!r} is not a directory in the workspace"
        )
    return candidate


def body(ctx: RunnerContext) -> dict[str, Any]:
    payload = ctx.payload
    for rejected in ("argv", "script", "env", "command_line", "shell"):
        if rejected in payload:
            raise RunnerFailure(
                f"input.{rejected} is not accepted: the generic runner executes a NAMED "
                f"command from its catalogue ({', '.join(sorted(GENERIC_COMMANDS))}), "
                "never a command supplied with the task"
            )

    command = resolve_command(payload.get("command"))
    argv = build_argv(ctx, command)
    cwd = _resolve_working_directory(ctx)
    limits = resolve_limits(payload, platform_ceilings())

    log = StructuredLogger(stream=sys.stderr, component="generic-runner")
    if limits.clamped:
        log.warning(
            "requested limits exceed the platform ceiling and were clamped",
            clamped=list(limits.clamped),
            effective=limits.as_dict(),
        )
    stdout_path = ctx.artifacts_dir / "command.stdout.log"
    stderr_path = ctx.artifacts_dir / "command.stderr.log"

    log.info("running catalogue command", command=command.name, argv=argv, cwd=str(cwd))
    result = run_child(
        argv,
        cwd=cwd,
        env=_build_env(ctx),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=limits.timeout_seconds,
        grace_seconds=limits.grace_seconds,
        max_stdout_bytes=limits.max_stdout_bytes,
        max_stderr_bytes=limits.max_stderr_bytes,
        logger=log,
    )

    tail = stdout_path.read_text(errors="replace")[-2000:] if stdout_path.exists() else ""
    if result.timed_out:
        raise RunnerFailure(
            f"{command.name} timed out after {limits.timeout_seconds:.0f}s"
        )
    if result.exit_code != 0:
        err_tail = stderr_path.read_text(errors="replace")[-2000:] if stderr_path.exists() else ""
        raise RunnerFailure(
            f"{command.name} exited {result.exit_code}\n{err_tail.strip()}"
        )

    return {
        "summary": f"{command.name} exited 0 in {result.duration_seconds:.1f}s",
        "command": command.name,
        "argv": argv,
        "exit_code": result.exit_code,
        "stdout_tail": tail,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
        "limits": limits.as_dict(),
        "metrics": {
            "duration_seconds": round(result.duration_seconds, 3),
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
        },
    }


def main() -> int:
    return run_runner(body, name="generic")


if __name__ == "__main__":
    raise SystemExit(main())
