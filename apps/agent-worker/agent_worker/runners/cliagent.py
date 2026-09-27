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

from .. import expected_outputs as expected_mod
from ..logs import StructuredLogger
from ..procman import TRUNCATION_MARK, run_child
from ..redact import collect_secrets, scrub_file, scrub_text
from .base import (
    SPEND_KEYS,
    CredentialRevokedSignal,
    QuotaExhaustedSignal,
    RunnerContext,
    RunnerFailure,
)
from .limits import platform_ceilings, resolve_limits
from .streams import AgentStreamFiles

BASE_PATH = "/usr/local/bin:/usr/local/share/npm-global/bin:/usr/bin:/bin"

#: The transcript artifact is written WHOLE or not at all. It used to be
#: `json.dumps(parsed, indent=2)[:4_000_000]`, which cut a long run's document
#: mid-token into JSON nothing could parse, under a name that promised JSON.
#: Past this size it is omitted and `transcript_skipped: "too_large"` says so
#: (the worker carries it into `result_summary.agent_streams`); the agent's own
#: stdout -- the NDJSON stream -- is the canonical transcript in every case and
#: is always uploaded.
TRANSCRIPT_MAX_CHARS = 4_000_000

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

#: Substrings that mean "this credential is no longer usable" -- as opposed to
#: "wait and try again", which is what _RATE_LIMIT_MARKERS covers.
#:
#: THIS EXISTS BECAUSE OF HOW SUBSCRIPTION CREDENTIALS BEHAVE. Refreshing an
#: OAuth credential REVOKES the previously issued access token; the platform
#: refreshes every registered account on a timer, including accounts an agent
#: is using right now. So a long-running attempt can have the token in its
#: environment revoked underneath it, mid-run, through no fault of its own.
#: The remedy is not to wait -- the token is gone permanently -- it is to read
#: the secret again and restart with the credential that replaced it.
#:
#: Deliberately NOT including a bare "401": it occurs in ordinary agent output
#: (a transcript discussing HTTP, a test fixture) and a false positive here
#: silently restarts a healthy run. Every marker below names an authentication
#: failure explicitly.
_CREDENTIAL_MARKERS = (
    "oauth access token has been revoked",
    "authentication_error",
    "invalid api key",
    "invalid_api_key",
    "invalid bearer token",
    "fix external api key",
    "please run /login",
    "unauthorized",
    "\"api_error_status\":401",
    "api_error_status: 401",
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
    #: Environment variable that must carry the tenant's credential.
    key_env: str
    #: Alternative credential variables, tried in order when `key_env` is unset.
    #:
    #: Claude Code accepts EITHER a pay-per-token API key (ANTHROPIC_API_KEY) or
    #: a subscription OAuth token (CLAUDE_CODE_OAUTH_TOKEN, from
    #: `claude setup-token`). A tenant paying for a Claude subscription has no
    #: API key at all, and requiring one would mean buying metered API access
    #: they already have a plan for. Whichever variable the tenant's secret
    #: supplies is the one passed to the child, and only that one.
    alt_key_envs: tuple[str, ...] = ()
    #: Flag used to select a model, if the CLI supports one.
    model_flag: str | None = "--model"
    transcript_name: str = "transcript.json"


def cli_stream_files(spec: CliAgentSpec) -> AgentStreamFiles:
    """The files this runner captures the agent CLI into, under `artifacts/`.

    The ONE place their names are built; `streams.agent_stream_files` calls it
    for the worker, and `run_cli_agent` below writes to exactly these paths.
    """
    return AgentStreamFiles(
        stdout=f"{spec.name}.stdout.log",
        stderr=f"{spec.name}.stderr.log",
        transcript=spec.transcript_name,
    )


#: Claude subscription tokens from `claude setup-token` carry this prefix.
#: Metered API keys are `sk-ant-api...`, so the two are distinguishable by value.
_OAUTH_TOKEN_PREFIX = "sk-ant-oat"


def _credential_env(spec: CliAgentSpec) -> str | None:
    """Which credential variable to hand the child, chosen by the VALUE's shape.

    A tenant has ONE secret per provider -- `swarm-tenant-<id>-anthropic` -- and
    the Cloud Run Job projects it into every variable the profile declares. So
    both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN arrive holding the SAME
    string, and picking by name order would put a subscription token into the
    API-key variable, where Claude Code would reject it.

    The value itself says which it is: `claude setup-token` mints
    `sk-ant-oat...`, while metered keys are `sk-ant-api...`. So the shape picks
    the variable, and only that one is passed to the child -- the other is
    dropped rather than handed over holding a credential of the wrong kind.
    """
    present = [(name, os.environ.get(name, "")) for name in (spec.key_env, *spec.alt_key_envs)]
    present = [(name, value) for name, value in present if value]
    if not present:
        return None

    oauth_names = [n for n in spec.alt_key_envs if "OAUTH" in n.upper()]
    looks_oauth = any(v.startswith(_OAUTH_TOKEN_PREFIX) for _, v in present)
    if looks_oauth and oauth_names:
        return oauth_names[0]
    return present[0][0]


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


def agent_working_directory(ctx: RunnerContext) -> Path:
    """Where the agent CLI starts: the checkout when the task has one, else `work/`.

    Owner decision of 2026-09-26 (#226): a SwarmCloud step behaves like a local
    lane wherever the difference is a choice. Locally Claude Code starts in the
    repository and loads its `CLAUDE.md` by itself; here it started in `work/`,
    with the checkout at `./repo`, and read `CLAUDE.md` only when a prompt told
    it to. So with a repository attached it starts in `work/repo`.

    `ctx.repo_dir` comes from `SWARM_REPO_DIR`, which the worker sets after the
    clone and a caller cannot. A checkout the worker named that is not there,
    or not inside `work/`, FAILS the runner: starting the agent in `work/`
    instead would run it without the code and without its instructions, and it
    would still report success.
    """
    if ctx.repo_dir is None:
        return ctx.work_dir
    repo = Path(ctx.repo_dir)
    try:
        inside = repo.resolve().is_relative_to(Path(ctx.work_dir).resolve())
    except OSError:
        inside = False
    if not inside or not repo.is_dir():
        raise RunnerFailure(
            f"the repository checkout {str(repo)!r} is not a directory inside the "
            "work directory; the agent is not started outside it, where it would "
            "have neither the code nor the repository's CLAUDE.md"
        )
    return repo


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


def detect_credential_failure(text: str) -> tuple[bool, str | None]:
    """Look for a refused credential in CLI output. Returns (hit, marker).

    Checked only AFTER `detect_rate_limit` has said no. A 429 body sometimes
    mentions authentication in passing, and mistaking a rate limit for a dead
    credential would reload a perfectly good secret and restart immediately
    into the same 429 -- turning a wait into a hot loop.
    """
    lowered = text.lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            return True, marker
    return False, None


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
    credential_env = _credential_env(spec)
    if credential_env is None:
        accepted = " or ".join((spec.key_env, *spec.alt_key_envs))
        raise RunnerFailure(
            f"{spec.name} requires {accepted}; the tenant has no {spec.provider} "
            "credential mounted for this attempt"
        )

    binary = _find_binary(spec)
    argv: list[str] = [binary, *_argv_prefix(spec), *extra_args]
    # THE MODEL IS THE JOB'S, NEVER THE CALLER'S (#226, owner decision of
    # 2026-09-26). `MODEL` is set on the profile's Cloud Run Job by Terraform
    # (`local.runner_models` in terraform/infra/locals.tf; `MODEL` on a Job the
    # scheduler creates comes from the same value) and the worker hands its
    # `config.model` to this process under the same name
    # (`lifecycle._build_child_env`). This used to read `input.model` first,
    # so a caller of the API or the console chose the model an agent ran -- an
    # execution parameter from a caller, which invariant 10 forbids. The API
    # now refuses `input.model` (claude-code and codex declare no input, #213)
    # and the worker drops a stored one; it is not read here either way.
    model = os.environ.get("MODEL", "").strip() or None
    if model and spec.model_flag:
        if not re.fullmatch(r"[A-Za-z0-9._:\-]{1,128}", model):
            raise RunnerFailure(f"MODEL {model!r} contains unsupported characters")
        argv += [spec.model_flag, model]
    # WHERE THE AGENT STARTS (#226). In the checkout when the task has one, so
    # Claude Code loads the repository's own CLAUDE.md (and codex its
    # AGENTS.md) the way a local lane does; in `work/` otherwise, as before.
    cwd = agent_working_directory(ctx)
    # WHERE DELIVERABLES GO, AND WHAT LATER STEPS NEED FROM THIS ONE. Every
    # prompt ends with one line naming $SWARM_ARTIFACTS_DIR (#184, owner
    # decision of 2026-09-26: `expected_mod.deliverables_line`). When a
    # dependant's `input_from` stages files from this task, the worker has put
    # their names in input.json, and they follow that line (#149). Both go into
    # the PROMPT because the prompt is the one instruction channel every CLI
    # runner shares. A system-prompt flag would have to exist in whichever CLI
    # release the image carries, and `*_ARGS` can replace the whole flag set.
    # This is the ONE call that builds them, for claude-code and codex alike.
    # See agent_worker/expected_outputs.py for what is measured and why.
    expected = expected_mod.parse_names(payload.get(expected_mod.METADATA_KEY))
    # THIS RUNNER'S OWN FILES ARE LEFT OUT. A dependant may stage the upstream
    # runner's log or transcript, and the API records that name like any
    # other, but this process writes those three files itself -- the logs
    # while the agent is running. An agent told to write one would be writing
    # into a file this runner holds open, or one it overwrites afterwards.
    files = cli_stream_files(spec)
    own_files = files.names()
    told = expected_mod.without_platform_names(expected.names, own_files)
    # STAGED INPUTS BY ABSOLUTE PATH, ONLY WHEN THE AGENT STARTS IN THE
    # CHECKOUT (#226). They land in `work/`, which is then the checkout's
    # parent, so a prompt's "read scan-01.md" resolves inside the repository
    # and finds nothing. A task with no repository starts in `work/`, where the
    # relative name works, and the owner kept its prompt unchanged.
    staged = (
        expected_mod.staged_paths(payload.get("staged_inputs"), ctx.work_dir)
        if cwd != ctx.work_dir
        else ()
    )
    # The prompt is the only caller-controlled value that reaches argv, and it
    # is passed as a single trailing argument with no shell in the picture.
    argv.append(expected_mod.with_instructions(prompt, told, ctx.artifacts_dir, staged))

    limits = resolve_limits(payload, platform_ceilings())
    log = StructuredLogger(stream=sys.stderr, component=f"{spec.name}-runner")
    log.info(
        "the agent starts in the repository checkout"
        if cwd != ctx.work_dir
        else "the agent starts in the work directory; the task has no repository",
        cwd=str(cwd),
        home=str(ctx.work_dir),
        model=model,
    )
    if told:
        log.info(
            "told the agent which files later steps need and where to write them",
            expected_outputs=list(told),
            artifacts_dir=os.path.abspath(ctx.artifacts_dir),
        )
    if staged:
        log.info(
            "named the staged inputs in the prompt by absolute path, because the "
            "agent starts in the checkout and they are in the work directory",
            staged_inputs=list(staged),
        )
    if len(told) != len(expected.names):
        log.info(
            "left this runner's own files out of the agent's instructions; the "
            "runner writes them itself",
            left_out=[name for name in expected.names if name not in told],
        )
    if expected.rejected:
        log.warning(
            "left unusable expected_outputs entries out of the agent's instructions: "
            + ", ".join(repr(entry) for entry in expected.rejected)
        )
    # This process's own stderr is captured by the worker and uploaded, and
    # `run_child` logs the argv it starts. Register the key here as well as in
    # the worker: a runner is a separate process and does not inherit the
    # worker logger's registered set.
    log.register_secret(os.environ.get(credential_env))
    for passthrough in _SENSITIVE_PASSTHROUGH:
        log.register_secret(os.environ.get(passthrough))
    if limits.clamped:
        log.warning(
            "requested limits exceed the platform ceiling and were clamped",
            clamped=list(limits.clamped),
            effective=limits.as_dict(),
        )
    stdout_path = ctx.artifacts_dir / files.stdout
    stderr_path = ctx.artifacts_dir / files.stderr

    env = {
        "PATH": BASE_PATH,
        # HOME IS THE ATTEMPT'S OWN `work/`, NEVER THE CHECKOUT, even when the
        # agent starts in the checkout (#226, owner decision of 2026-09-26).
        # The CLI writes its own state under HOME (`.claude.json`, `.claude/`,
        # `.codex/`), some of it describing the account it ran as; in the
        # checkout that would be in the harvest's patch and the pushed branch.
        "HOME": str(ctx.work_dir),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "CI": "1",
        "NO_COLOR": "1",
        # WHERE TO PUT ITS WORK. runners/base.py documents SWARM_ARTIFACTS_DIR
        # as the contract -- "files written here are uploaded when the attempt
        # ends" -- and runners/generic.py exports it. This runner did not, so a
        # claude-code agent was never told where its output should go.
        #
        # Measured on the first real multi-agent workflow
        # (wf_bcdc9180e4fb4a209f31, step `research`): the agent replied "The
        # environment variable SWARM_ARTIFACTS_DIR is not set in this
        # environment, so I can't determine the target directory" and exited 0
        # having written nothing. The attempt still SUCCEEDED, because writing
        # an artifact is not a success condition.
        #
        # The consequence was the platform's headline feature: this runner uses
        # ctx.artifacts_dir for its OWN stdout/stderr (above), so
        # result_summary.artifacts always contained exactly the runner's logs
        # and the transcript and never anything the agent produced. With no
        # agent artifact there is nothing for a downstream step's `input_from`
        # to stage, so work could not be routed between agents at all -- while
        # `input_from` itself was correct and tested, against the generic
        # runner.
        "SWARM_ARTIFACTS_DIR": str(ctx.artifacts_dir),
        "SWARM_WORK_DIR": str(ctx.work_dir),
        credential_env: os.environ[credential_env],
    }
    for passthrough in (*_SENSITIVE_PASSTHROUGH, *_PLAIN_PASSTHROUGH):
        if os.environ.get(passthrough):
            env[passthrough] = os.environ[passthrough]

    result = run_child(
        argv,
        cwd=cwd,
        env=env,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=limits.timeout_seconds,
        grace_seconds=limits.grace_seconds,
        max_stdout_bytes=limits.max_stdout_bytes,
        max_stderr_bytes=limits.max_stderr_bytes,
        logger=log,
        # THE END OF A CAPPED STREAM IS KEPT (#188 review). Under stream-json
        # the stdout is the whole conversation, and its LAST line is the
        # `result` event: the answer, the spend, the evidence the rate-limit
        # decision below reads. A capture that kept only the first
        # `max_stdout_bytes` lost exactly that line on every long session.
        keep_tail=True,
    )
    # Reported on every outcome from here on -- `write_result` carries it --
    # so a run that failed or parked after passing its cap still says so.
    capture = result.capture_report()
    ctx.report.update(capture)
    if result.stdout_truncated or result.stderr_truncated:
        log.warning(
            "the agent's output passed its size cap: the start and the end of each "
            "capped stream were kept and the middle dropped, with a notice where",
            **capture,
            max_stdout_bytes=limits.max_stdout_bytes,
            max_stderr_bytes=limits.max_stderr_bytes,
        )

    # Redact before anything is read back out. Everything below this line either
    # becomes an artifact in GCS or a field in Firestore, and both outlive the
    # pod. `env` is the set of values this process was given, so it is exactly
    # the set of secrets it could have leaked.
    secrets = collect_secrets(
        env.get(name) for name in (spec.key_env, *spec.alt_key_envs, *_SENSITIVE_PASSTHROUGH)
    )
    for captured in (stdout_path, stderr_path):
        scrub_file(captured, secrets)

    # PARSED BEFORE THE EXIT CODE IS JUDGED, not after. A CLI that fails still
    # prints its result object -- `claude --output-format json` reports
    # `is_error: true` WITH `usage` and `total_cost_usd` -- and every raise
    # below used to happen first, so the numbers for a run that hit a 429 an
    # hour in, or failed after doing real work, were discarded here and the
    # attempt read "not reported" in every cost figure. `spend` rides out on
    # the signal instead, and `run_runner` writes it into result.json.
    raw_stdout = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
    parsed = _parse_cli_output(raw_stdout)
    spend = _scrub_json(_spend_of(parsed), secrets)

    # What the rate-limit and credential heuristics read. For a streamed run
    # this is NOT the raw stdout tail -- see `_detection_text`. Neither half
    # includes the capture's own notices, which count bytes: `429` is a marker.
    combined = (
        _detection_text(raw_stdout, parsed)
        + "\n"
        + _without_capture_notices(_tail(stderr_path))
    )

    if result.exit_code != 0 or result.timed_out:
        hit, retry_after, reset_at = detect_rate_limit(combined)
        if hit:
            raise QuotaExhaustedSignal(
                provider=spec.provider,
                retry_after_seconds=retry_after,
                reset_at=reset_at,
                detail=f"{spec.name} reported a provider rate limit",
                spend=spend,
            )
        # Checked only after the rate-limit test has said no: a 429 body
        # sometimes mentions authentication in passing, and reading that as a
        # dead credential would reload a perfectly good secret and restart
        # straight back into the same 429 -- turning a wait into a hot loop.
        refused, marker = detect_credential_failure(combined)
        if refused:
            raise CredentialRevokedSignal(
                provider=spec.provider,
                detail=f"{spec.name} was refused its credential",
                marker=marker or "",
                spend=spend,
            )
    if result.timed_out:
        raise RunnerFailure(
            f"{spec.name} timed out after {limits.timeout_seconds:.0f}s", spend=spend
        )
    if result.exit_code != 0:
        raise RunnerFailure(
            f"{spec.name} exited {result.exit_code}: {_tail(stderr_path, 2000).strip()}",
            spend=spend,
        )

    # WHOLE OR NOT AT ALL -- see TRANSCRIPT_MAX_CHARS. The stdout capture above
    # is the canonical transcript and is uploaded either way.
    transcript_skipped: str | None = None
    if result.stdout_truncated:
        # A transcript re-serialised from a stream whose middle was dropped
        # would be valid JSON that says nothing of the gap. The stdout capture
        # carries the notice at the cut; it is the record.
        transcript_skipped = "capture_truncated"
    elif parsed is not None:
        document = json.dumps(parsed, indent=2)
        if len(document) <= TRANSCRIPT_MAX_CHARS:
            ctx.write_artifact(spec.transcript_name, scrub_text(document, secrets))
        else:
            transcript_skipped = "too_large"
            log.warning(
                "the transcript was not written: it is larger than the cap, and a "
                "cut JSON document parses as nothing; the agent's stdout is the "
                "complete record",
                transcript=spec.transcript_name,
                chars=len(document),
                cap_chars=TRANSCRIPT_MAX_CHARS,
            )

    # A rate limit can also appear in a run the CLI still reports as successful.
    hit, retry_after, reset_at = detect_rate_limit(combined)
    if hit and not _looks_complete(parsed):
        raise QuotaExhaustedSignal(
            provider=spec.provider,
            retry_after_seconds=retry_after,
            reset_at=reset_at,
            detail=f"{spec.name} output contains a rate-limit notice without a completed result",
            spend=spend,
        )

    return {
        # `result.json` is read by the worker and stored as `task.result_summary`,
        # which is a Firestore document. Neither the summary nor the structured
        # output may carry the key that produced it.
        "summary": scrub_text(_summarise(parsed, raw_stdout), secrets),
        "provider": spec.provider,
        # The model this runner ASKED for, the Job's MODEL. What the CLI says it
        # actually used is `modelUsage` in `structured_output`, which the worker
        # lifts into `result_summary.runner.usage.models`.
        "model": model if model and spec.model_flag else None,
        "exit_code": result.exit_code,
        # A streamed (one-object-per-line) run parses to a LIST, which this
        # field never carried -- so its numbers were dropped even on success.
        # The spend subset of its final result event stands in for it then.
        "structured_output": (
            _scrub_json(parsed, secrets)
            if isinstance(parsed, dict)
            else (spend or None)
        ),
        "limits": limits.as_dict(),
        # Read by the worker into `result_summary.agent_streams`; null when the
        # transcript was written (or there was no parsed output to write).
        "transcript_skipped": transcript_skipped,
        # Also in `ctx.report`, which `write_result` merges anyway; stated here
        # so this function's own return value is the whole answer.
        **capture,
        "metrics": {
            "duration_seconds": round(result.duration_seconds, 3),
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
        },
    }


def _parse_cli_output(raw_stdout: str) -> Any:
    """The CLI's stdout as JSON: one object, or the objects of a streamed run.

    None when neither shape is there -- a CLI that crashed before printing, or
    one whose output is prose.
    """
    try:
        return json.loads(raw_stdout)
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
        return events or None


#: Statuses a `rate_limit_event` reports when the request went through. Such an
#: event is a READING of the account's windows, not a refusal, and it is in
#: every streamed run -- so it must never be what parks one.
_RATE_LIMIT_ALLOWED = frozenset({"allowed", "allowed_warning"})


def _not_evidence(event: dict[str, Any]) -> bool:
    """True for a streamed event the rate-limit heuristics must not read.

    * `assistant` and `user` events are the agent's own words and its tools'
      output. An agent that reads a file about HTTP 429s, or runs a test named
      `test_rate_limit`, prints the markers `_RATE_LIMIT_MARKERS` looks for,
      and a FAILED run whose transcript mentioned them would be parked as
      rate-limited instead of failing -- a false park that also skips the
      failure a person needs to see. The exception is an event the CLI marks
      as an API error (`isApiErrorMessage`, or an `error` key): that is the
      provider speaking, not the agent.
    * a `rate_limit_event` whose status says the request was allowed.

    Everything else -- the `result` event, `system` events, any line that is
    not JSON, any event type this function does not know -- is kept, so an
    unrecognised way of reporting a 429 still reaches the heuristics.
    """
    kind = event.get("type")
    if kind in ("assistant", "user"):
        return not (event.get("isApiErrorMessage") or event.get("error"))
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info")
        status = event.get("status")
        if status is None and isinstance(info, dict):
            status = info.get("status")
        return isinstance(status, str) and status in _RATE_LIMIT_ALLOWED
    return False


def _detection_text(raw_stdout: str, parsed: Any) -> str:
    """The part of stdout the rate-limit and credential heuristics may read.

    One JSON object, or output that is not JSON at all: the last 8000
    characters, exactly as before. A STREAMED run (`--output-format
    stream-json`, one event per line, which claude-code runs with since #184):
    every line except the ones `_not_evidence` names, last 8000 characters.
    Under the single-object format the heuristics saw the final result and
    nothing else; this keeps that for a streamed run instead of widening it
    to the whole conversation.

    NEVER THE CAPTURE'S OWN NOTICE (#188 review). Where the output cap cut a
    stream, `procman.StreamCapture` writes a line counting the bytes it
    dropped -- and `429` is a marker matched anywhere, so 4,290,117 dropped
    bytes would park a failed run as rate-limited. Nor, in a streamed run,
    the line the cut went through, just above the notice: half an event is
    the agent's words or a tool's output as often as the provider's, and
    cannot be told apart.
    """
    if not isinstance(parsed, list):
        return _without_capture_notices(raw_stdout[-8000:])
    kept: list[str] = []
    previous_unparsed = False
    for line in raw_stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_capture_notice(stripped):
            if previous_unparsed and kept:
                kept.pop()
            previous_unparsed = False
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)
            previous_unparsed = True
            continue
        previous_unparsed = False
        if isinstance(event, dict) and _not_evidence(event):
            continue
        kept.append(stripped)
    return "\n".join(kept)[-8000:]


_CAPTURE_MARK = TRUNCATION_MARK.decode("ascii")


def _is_capture_notice(line: str) -> bool:
    return line.lstrip().startswith(_CAPTURE_MARK)


def _without_capture_notices(text: str) -> str:
    """`text` less every line the output capture wrote where it cut a stream."""
    if _CAPTURE_MARK not in text:
        return text
    return "\n".join(line for line in text.splitlines() if not _is_capture_notice(line))


def _spend_of(parsed: Any) -> dict[str, Any]:
    """The `SPEND_KEYS` subset of a CLI result, or {} when it reported none.

    For a streamed run, the LAST event that carries them: that is the final
    `result` event, whose totals cover the whole session. Earlier events carry
    per-message usage, nested, and summing those would count the same tokens
    the result event already totals.
    """
    candidates: list[Any]
    if isinstance(parsed, dict):
        candidates = [parsed]
    elif isinstance(parsed, list):
        candidates = list(reversed(parsed))
    else:
        return {}
    for candidate in candidates:
        if isinstance(candidate, dict) and any(key in candidate for key in SPEND_KEYS):
            return {key: candidate[key] for key in SPEND_KEYS if key in candidate}
    return {}


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
        # The LAST event that carries text, not the last event: a streamed run
        # can end with a reading (`rate_limit_event`) after its `result`.
        for event in reversed(parsed):
            if not isinstance(event, dict):
                continue
            for key in ("result", "text", "content"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:2000]
    return raw.strip()[-2000:] or "agent produced no textual output"
