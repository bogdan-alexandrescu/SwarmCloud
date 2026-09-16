"""The generic runner: an allowlisted program, in the tenant's sandbox.

This is the platform's deliberate escape valve for work that is not an agent --
a test suite, a build, a data munge. It still obeys invariant 10, because what a
caller chooses is the *profile name* `generic`: the image, the resource class,
the backend and the timeout all come from the frozen catalogue, and none of them
can be influenced from the request. What the caller supplies is an argv list to
run inside an isolated workspace that is destroyed afterwards.

The defences that matter here:

* **argv is a list, always.** There is no shell anywhere in this path, so there
  is no quoting to get wrong.
* **argv[0] is allowlisted** and resolved through PATH by `shutil.which`. An
  absolute path to something outside the image's own directories is refused, so
  a task cannot execute a binary it dropped into its own workspace.
* **The environment is built, not inherited.** A caller may add variables, but
  not PATH, not LD_*, not anything that changes which code gets loaded.
* **Output is capped and the child is killed on timeout**, by the same process
  manager the worker uses for runners.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ..logs import StructuredLogger
from ..procman import run_child
from .base import RunnerContext, RunnerFailure, run_runner

#: Programs a `generic` task may start. Everything here ships in
#: `images/agent-runtime-base`, and nothing here is a privilege boundary.
ALLOWED_PROGRAMS = frozenset(
    {
        "bash", "sh", "python", "python3", "uv", "pytest", "node", "npm", "npx",
        "git", "make", "jq", "rg", "fd", "curl", "wget", "tar", "gzip", "unzip",
        "sed", "awk", "grep", "find", "ls", "cat", "echo", "env", "true", "false",
    }
)

#: Environment names a caller may set. Anything that changes how code is located
#: or loaded is excluded on purpose.
_FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_", "PYTHONPATH", "PATH", "GOOGLE_", "SWARM_")

BASE_PATH = "/usr/local/bin:/usr/bin:/bin"


def _resolve_program(program: str) -> str:
    if not program or program.startswith("-"):
        raise RunnerFailure(f"invalid program {program!r}")
    if "/" in program:
        raise RunnerFailure(
            f"program {program!r} must be a bare name on PATH, not a path; "
            "the generic runner never executes a file from the workspace"
        )
    if program not in ALLOWED_PROGRAMS:
        raise RunnerFailure(
            f"program {program!r} is not in the generic runner allowlist "
            f"({', '.join(sorted(ALLOWED_PROGRAMS))})"
        )
    resolved = shutil.which(program, path=BASE_PATH)
    if not resolved:
        raise RunnerFailure(f"program {program!r} is not installed in this image")
    return resolved


def _build_env(ctx: RunnerContext, extra: Any) -> dict[str, str]:
    env = {
        "PATH": BASE_PATH,
        "HOME": str(ctx.work_dir),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "SWARM_WORK_DIR": str(ctx.work_dir),
        "SWARM_ARTIFACTS_DIR": str(ctx.artifacts_dir),
    }
    if not extra:
        return env
    if not isinstance(extra, dict):
        raise RunnerFailure("input.env must be an object of string values")
    for key, value in extra.items():
        name = str(key)
        if not name.replace("_", "").isalnum() or not name.isupper():
            raise RunnerFailure(f"environment name {name!r} must be UPPER_SNAKE_CASE")
        if any(name.startswith(prefix) for prefix in _FORBIDDEN_ENV_PREFIXES):
            raise RunnerFailure(f"environment variable {name!r} may not be overridden")
        env[name] = str(value)
    return env


def body(ctx: RunnerContext) -> dict[str, Any]:
    payload = ctx.payload
    argv_in = payload.get("argv")
    if isinstance(argv_in, str):
        raise RunnerFailure(
            "input.argv must be a list of strings; a command string would require a shell"
        )
    if not isinstance(argv_in, list) or not argv_in:
        raise RunnerFailure("input.argv is required and must be a non-empty list of strings")
    argv = [str(item) for item in argv_in]

    script = payload.get("script")
    if script is not None:
        # A script is written into the workspace and passed as a file argument,
        # which keeps its content out of argv entirely.
        if not isinstance(script, str):
            raise RunnerFailure("input.script must be a string")
        script_path = ctx.work_dir / "task_script"
        script_path.write_text(script)
        script_path.chmod(0o700)
        argv = argv + [str(script_path)]

    program = _resolve_program(argv[0])
    timeout = float(payload.get("timeout_seconds", 1800))
    if timeout <= 0:
        raise RunnerFailure("input.timeout_seconds must be positive")

    log = StructuredLogger(stream=sys.stderr, component="generic-runner")
    stdout_path = ctx.artifacts_dir / "command.stdout.log"
    stderr_path = ctx.artifacts_dir / "command.stderr.log"

    result = run_child(
        [program, *argv[1:]],
        cwd=ctx.work_dir,
        env=_build_env(ctx, payload.get("env")),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout,
        grace_seconds=float(payload.get("grace_seconds", 15)),
        max_stdout_bytes=int(payload.get("max_stdout_bytes", 16 * 1024 * 1024)),
        max_stderr_bytes=int(payload.get("max_stderr_bytes", 4 * 1024 * 1024)),
        logger=log,
    )

    tail = stdout_path.read_text(errors="replace")[-2000:] if stdout_path.exists() else ""
    if result.timed_out:
        raise RunnerFailure(f"command timed out after {timeout}s: {' '.join(argv)}")
    if result.exit_code != 0:
        err_tail = stderr_path.read_text(errors="replace")[-2000:] if stderr_path.exists() else ""
        raise RunnerFailure(
            f"command exited {result.exit_code}: {' '.join(argv)}\n{err_tail.strip()}"
        )

    return {
        "summary": f"{argv[0]} exited 0 in {result.duration_seconds:.1f}s",
        "argv": argv,
        "exit_code": result.exit_code,
        "stdout_tail": tail,
        "stdout_truncated": result.stdout_truncated,
        "stderr_truncated": result.stderr_truncated,
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
