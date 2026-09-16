"""Shared machinery for the runners that drive a coding-agent CLI.

`claude_code` and `codex` differ only in which binary they start and which flags
that binary takes. Everything else -- finding the binary, refusing to start
without the tenant's key, capping output, turning a rate limit into a park
signal, turning the transcript into an artifact -- is identical, and identical
code is the only way those two runners stay identical in behaviour.

On flags: the argv prefix has a conservative default and can be overridden with
an environment variable set by the PLATFORM (the image, or the Cloud Run Job
definition) -- never by a caller, whose input never reaches argv except as the
prompt. That is what keeps these runners working across CLI releases without
anybody guessing at flags in a Dockerfile.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..logs import StructuredLogger
from ..procman import run_child
from .base import QuotaExhaustedSignal, RunnerContext, RunnerFailure

BASE_PATH = "/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"

#: Substrings that mean "the provider said no, try later".
_RATE_LIMIT_MARKERS = (
    "rate_limit_error",
    "rate limit",
    "rate-limited",
    "ratelimit",
    "429",
    "too many requests",
    "overloaded_error",
    "insufficient_quota",
    "quota exceeded",
    "resource_exhausted",
    "usage limit reached",
)

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[-_ ]?after[\"':= ]+(\d+)", re.IGNORECASE),
    re.compile(r"try again in (\d+)\s*second", re.IGNORECASE),
    re.compile(r"try again in (\d+)\s*minute", re.IGNORECASE),
    re.compile(r"resets? in (\d+)\s*minute", re.IGNORECASE),
)
_RESET_AT_PATTERN = re.compile(
    r"\"(?:reset_at|resets_at|resetsAt|resets_at_utc)\"\s*:\s*\"([^\"]+)\"", re.IGNORECASE
)


@dataclass(frozen=True)
class CliAgentSpec:
    name: str
    provider: str
    #: Environment variable naming the binary, then the default binary name.
    binary_env: str
    binary_default: str
    #: Environment variable holding a JSON list of argv flags, then the default.
    args_env: str
    args_default: tuple[str, ...]
    #: Environment variable that must carry the tenant's key.
    key_env: str
    #: Flag used to select a model, if the CLI supports one.
    model_flag: str | None = "--model"
    transcript_name: str = "transcript.json"


def _argv_prefix(spec: CliAgentSpec) -> list[str]:
    raw = os.environ.get(spec.args_env, "").strip()
    if not raw:
        return list(spec.args_default)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunnerFailure(f"{spec.args_env} must be a JSON list of strings: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise RunnerFailure(f"{spec.args_env} must be a JSON list of strings")
    return parsed


def _find_binary(spec: CliAgentSpec) -> str:
    candidate = os.environ.get(spec.binary_env, "").strip() or spec.binary_default
    resolved = candidate if os.path.isabs(candidate) else shutil.which(candidate, path=BASE_PATH)
    if not resolved or not os.path.exists(resolved):
        raise RunnerFailure(
            f"{spec.name}: {candidate!r} is not installed in this image; "
            f"set {spec.binary_env} or rebuild agent-runtime-base"
        )
    return resolved


def detect_rate_limit(text: str) -> tuple[bool, int | None, str | None]:
    """Look for a provider rate limit in CLI output.

    Returns (hit, retry_after_seconds, reset_at). Heuristic by necessity: a CLI
    reports a 429 as prose or as JSON depending on the release, and treating an
    unrecognised rate limit as an ordinary failure would burn one of the task's
    three attempts on something that is not the task's fault.
    """
    lowered = text.lower()
    hit = any(marker in lowered for marker in _RATE_LIMIT_MARKERS)
    if not hit:
        return False, None, None
    retry_after: int | None = None
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if match:
            value = int(match.group(1))
            if "minute" in pattern.pattern:
                value *= 60
            retry_after = value if retry_after is None else min(retry_after, value)
            break
    reset_match = _RESET_AT_PATTERN.search(text)
    return True, retry_after, (reset_match.group(1) if reset_match else None)


def _tail(path: Path, limit: int = 8000) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-limit:]


def run_cli_agent(
    ctx: RunnerContext,
    spec: CliAgentSpec,
    *,
    extra_args: Sequence[str] = (),
) -> dict[str, Any]:
    payload = ctx.payload
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RunnerFailure(f"{spec.name} requires a non-empty string input.prompt")
    if not os.environ.get(spec.key_env):
        raise RunnerFailure(
            f"{spec.name} requires {spec.key_env}; the tenant has no {spec.provider} "
            "credential mounted for this attempt"
        )

    binary = _find_binary(spec)
    argv: list[str] = [binary, *_argv_prefix(spec), *extra_args]
    model = payload.get("model") or os.environ.get("MODEL", "").strip()
    if model and spec.model_flag:
        if not re.fullmatch(r"[A-Za-z0-9._:\-]{1,128}", str(model)):
            raise RunnerFailure(f"input.model {model!r} contains unsupported characters")
        argv += [spec.model_flag, str(model)]
    # The prompt is the only caller-controlled value that reaches argv, and it
    # is passed as a single trailing argument with no shell in the picture.
    argv.append(prompt)

    timeout = float(payload.get("timeout_seconds", 3600))
    log = StructuredLogger(stream=sys.stderr, component=f"{spec.name}-runner")
    stdout_path = ctx.artifacts_dir / f"{spec.name}.stdout.log"
    stderr_path = ctx.artifacts_dir / f"{spec.name}.stderr.log"

    env = {
        "PATH": BASE_PATH,
        "HOME": str(ctx.work_dir),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "CI": "1",
        "NO_COLOR": "1",
        spec.key_env: os.environ[spec.key_env],
    }
    for passthrough in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS"):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]

    result = run_child(
        argv,
        cwd=ctx.work_dir,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout,
        grace_seconds=float(payload.get("grace_seconds", 20)),
        max_stdout_bytes=int(payload.get("max_stdout_bytes", 32 * 1024 * 1024)),
        max_stderr_bytes=int(payload.get("max_stderr_bytes", 8 * 1024 * 1024)),
        logger=log,
    )

    combined = _tail(stdout_path) + "\n" + _tail(stderr_path)
    if result.exit_code != 0 or result.timed_out:
        hit, retry_after, reset_at = detect_rate_limit(combined)
        if hit:
            raise QuotaExhaustedSignal(
                provider=spec.provider,
                retry_after_seconds=retry_after,
                reset_at=reset_at,
                detail=f"{spec.name} reported a provider rate limit",
            )
    if result.timed_out:
        raise RunnerFailure(f"{spec.name} timed out after {timeout}s")
    if result.exit_code != 0:
        raise RunnerFailure(
            f"{spec.name} exited {result.exit_code}: {_tail(stderr_path, 2000).strip()}"
        )

    raw_stdout = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
    parsed: Any = None
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError:
        # Some CLI versions stream one JSON object per line.
        events = []
        for line in raw_stdout.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        parsed = events or None

    if parsed is not None:
        ctx.write_artifact(spec.transcript_name, json.dumps(parsed, indent=2)[:4_000_000])

    # A rate limit can also appear in a run the CLI still reports as successful.
    hit, retry_after, reset_at = detect_rate_limit(combined)
    if hit and not _looks_complete(parsed):
        raise QuotaExhaustedSignal(
            provider=spec.provider,
            retry_after_seconds=retry_after,
            reset_at=reset_at,
            detail=f"{spec.name} output contains a rate-limit notice without a completed result",
        )

    return {
        "summary": _summarise(parsed, raw_stdout),
        "provider": spec.provider,
        "model": str(model) if model else None,
        "exit_code": result.exit_code,
        "structured_output": parsed if isinstance(parsed, dict) else None,
        "metrics": {
            "duration_seconds": round(result.duration_seconds, 3),
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
        },
    }


def _looks_complete(parsed: Any) -> bool:
    if isinstance(parsed, dict):
        return bool(parsed.get("result") or parsed.get("output") or parsed.get("content"))
    if isinstance(parsed, list):
        return bool(parsed)
    return False


def _summarise(parsed: Any, raw: str) -> str:
    if isinstance(parsed, dict):
        for key in ("result", "output", "summary", "text", "content"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:2000]
    if isinstance(parsed, list) and parsed:
        last = parsed[-1]
        if isinstance(last, dict):
            for key in ("result", "text", "content"):
                value = last.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:2000]
    return raw.strip()[-2000:] or "agent produced no textual output"
