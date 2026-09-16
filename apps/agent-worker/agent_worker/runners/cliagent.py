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

Two properties that are load-bearing rather than incidental:

* **`CLAUDE_CODE_BIN` / `CODEX_BIN` and their `_ARGS` really are platform-set.**
  They arrive in this process's environment, and the only things the worker puts
  there are its own configuration and the values the frozen profile declares in
  `RunnerProfile.secrets`. `secrets.resolve_credentials` exports exactly those
  declared names and nothing else, which is what stops somebody who may only ADD
  a secret version -- a per-tenant admin, who is deliberately not trusted to read
  the key back -- from shipping `{"CLAUDE_CODE_BIN": "/bin/sh"}` and getting a
  shell.

* **The CLI's output is redacted before it becomes an artifact or a summary.**
  The worker's logger scrubs its own log lines, but this process writes the raw
  stdout and stderr into `artifacts/`, writes the transcript, and returns up to
  2000 characters of that stdout as the summary that becomes
  `task.result_summary` in Firestore. A CLI that echoes its configuration, or a
  tool error quoting an `Authorization` header, would otherwise land verbatim in
  a GCS object and a Firestore document. This runner holds the key, so this
  runner scrubs it.
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
from ..redact import collect_secrets, scrub_file, scrub_text
from .base import QuotaExhaustedSignal, RunnerContext, RunnerFailure
from .limits import platform_ceilings, resolve_limits

BASE_PATH = "/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"

#: Environment values that are copied through to the CLI and may themselves
#: carry a credential. A proxy URL routinely embeds `user:password@`, so it is
#: registered for redaction alongside the provider key rather than assumed
#: harmless. `NO_PROXY` and `NODE_EXTRA_CA_CERTS` are a host list and a path,
#: never a secret, so they are passed through without being redacted -- a path
#: appearing in output is diagnostic information worth keeping readable.
_SENSITIVE_PASSTHROUGH: tuple[str, ...] = ("HTTPS_PROXY", "HTTP_PROXY")
_PLAIN_PASSTHROUGH: tuple[str, ...] = ("NO_PROXY", "NODE_EXTRA_CA_CERTS")

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
    # A CLI that surfaces the raw HTTP header and nothing else. `Retry-After` is
    # only ever sent with a 429 or a 503, so it is a rate limit or an outage --
    # both of which must park rather than burn one of the task's three attempts.
    # `_RETRY_AFTER_PATTERNS` then reads the value out of the same line.
    "retry-after",
    "retry_after",
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

    limits = resolve_limits(payload, platform_ceilings())
    log = StructuredLogger(stream=sys.stderr, component=f"{spec.name}-runner")
    # This process's own stderr is captured by the worker and uploaded, and
    # `run_child` logs the argv it starts. Register the key here as well as in
    # the worker: a runner is a separate process and does not inherit the
    # worker logger's registered set.
    log.register_secret(os.environ.get(spec.key_env))
    for passthrough in _SENSITIVE_PASSTHROUGH:
        log.register_secret(os.environ.get(passthrough))
    if limits.clamped:
        log.warning(
            "requested limits exceed the platform ceiling and were clamped",
            clamped=list(limits.clamped),
            effective=limits.as_dict(),
        )
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
    for passthrough in (*_SENSITIVE_PASSTHROUGH, *_PLAIN_PASSTHROUGH):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]

    result = run_child(
        argv,
        cwd=ctx.work_dir,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=limits.timeout_seconds,
        grace_seconds=limits.grace_seconds,
        max_stdout_bytes=limits.max_stdout_bytes,
        max_stderr_bytes=limits.max_stderr_bytes,
        logger=log,
    )

    # Redact before anything is read back out. Everything below this line either
    # becomes an artifact in GCS or a field in Firestore, and both outlive the
    # pod. `env` is the set of values this process was given, so it is exactly
    # the set of secrets it could have leaked.
    secrets = collect_secrets(env.get(name) for name in (spec.key_env, *_SENSITIVE_PASSTHROUGH))
    for captured in (stdout_path, stderr_path):
        scrub_file(captured, secrets)

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
        raise RunnerFailure(f"{spec.name} timed out after {limits.timeout_seconds:.0f}s")
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
        ctx.write_artifact(
            spec.transcript_name,
            scrub_text(json.dumps(parsed, indent=2)[:4_000_000], secrets),
        )

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
        # `result.json` is read by the worker and stored as `task.result_summary`,
        # which is a Firestore document. Neither the summary nor the structured
        # output may carry the key that produced it.
        "summary": scrub_text(_summarise(parsed, raw_stdout), secrets),
        "provider": spec.provider,
        "model": str(model) if model else None,
        "exit_code": result.exit_code,
        "structured_output": _scrub_json(parsed, secrets) if isinstance(parsed, dict) else None,
        "limits": limits.as_dict(),
        "metrics": {
            "duration_seconds": round(result.duration_seconds, 3),
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
        },
    }


def _scrub_json(value: Any, secrets: Sequence[str]) -> Any:
    """Redact every string in a JSON-able structure, keys included."""
    if isinstance(value, str):
        return scrub_text(value, secrets)
    if isinstance(value, dict):
        return {scrub_text(str(k), secrets): _scrub_json(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v, secrets) for v in value]
    return value


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
