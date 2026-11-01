"""Runner: OpenAI Codex CLI, non-interactive.

Same shape as the Claude Code runner -- deliberately, because the two profiles
must behave identically from the platform's point of view: the same workspace,
the same artifact layout, the same rate-limit handling, the same park decision.
Only the binary, the flags and the credential differ, and all three are data.

THE GIT TOOLS (docs/runner-git-tools.md §1.3, lane G6). When the worker sets
`SWARM_GIT` -- only for an attempt offered the runner git tools -- the
`swarm-git` shim is named in codex's MCP configuration as `-c` overrides on
`codex exec`, the same server claude-code gets in `--mcp-config`. Overrides,
not `$HOME/.codex/config.toml`: HOME is `work/`, which a checkpoint restores,
so a file written there would be the agent's to edit for the next attempt and
would outlive an attempt that is not offered the tools.

THE GRAPH TOOLS (docs/design/context-quality.md §4.4, lane CTXQ-S). When the
worker staged the repository's graph snapshot it sets `SWARM_GRAPH_SNAPSHOT`,
and `swarm-graph` is named the same way, as `-c mcp_servers.swarm-graph.*`.
`SWARM_GRAPH_UNSTAGED` adds the one prompt line claude-code adds.

SERVED INSTEAD OF STAGED (docs/design/graph-service.md §4.2, lane GS-WORKER).
With `SWARM_GRAPH_PIN` and `SWARM_GRAPH_TICKET_FILE` set, the same keys name
the pod's shim (`graphstage.shim_argv`), plus
`-c mcp_servers.swarm-graph.env={"SWARM_GRAPH_URL" = …}`: a TOML inline table,
because codex parses every `-c` value as TOML (the design's sketch wrote it
as JSON, which TOML does not read as a table).

THE CALLER'S MCP SERVERS (docs/design/registered-mcp-servers.md §4.7, lane
MCP-RUNNERS, part of #1433). When the worker serves its relay and sets
`SWARM_MCP_RELAY`, each attached server is named as
`-c mcp_servers.<name>.url=<the relay's loopback URL>` and
`-c mcp_servers.<name>.bearer_token_env_var="SWARM_MCP_RELAY_TOKEN"`, the
per-attempt LOCAL token in the CLI's environment only, never in the argv or a
file; the caller's credential is in neither (the relay adds it on the way
out). The prompt gains claude-code's line per server
(`claude_code.mcp_servers_note`). Measured against the pinned 0.154.0 with a
loopback fake (tests/unit/worker/test_mcp_runner_config.py): both keys are
accepted and the CLI initializes and lists the server's tools through the
relay. 0.154.0 offers MCP tools to the model as deferred nested tools of its
`exec` tool (found through `ALL_TOOLS`), not in the first request's tool list.

ON A CHATGPT PLAN FROM THE SHARED POOL (part of #2688; BGD, 2026-11-01: codex
runs on a pool of ChatGPT-plan accounts the way claude-code runs on the pool of
Claude subscriptions). The worker leases a provider "openai" account and hands
this runner the plan's ACCESS token (about an hour) and its account id as
`CODEX_PLAN_ACCESS_TOKEN` and `CODEX_PLAN_ACCOUNT_ID`; never the refresh token,
which only the broker holds, because each refresh REPLACES it and a pod that
refreshed would sign every other holder of the account out. What this runner
does with them, each measured against the pinned 0.154.0 in agent-runtime-base:

  * writes `auth.json` in a PRIVATE `CODEX_HOME`, beside the worker's account
    channel in `private/` (never `work/`, which a checkpoint archives), mode
    0600, removed when the run ends whichever way it ends. Its shape is the
    one `codex login` writes -- `auth_mode` "chatgpt", `tokens.{id_token,
    access_token, refresh_token, account_id}`, `last_refresh` -- and
    `codex login status` answers "Logged in using ChatGPT" for it. codex
    parses `id_token` as a JWT and refuses one with an empty part ("invalid ID
    token format"), so it is an unsigned stand-in carrying the account id; no
    request sends it. `refresh_token` is empty;
  * points `CODEX_REFRESH_TOKEN_URL_OVERRIDE` at a loopback port nothing
    listens on: codex 0.154.0 tries a refresh on a 401, and that attempt now
    fails in the pod ("error sending request for url (http://127.0.0.1:9/...)")
    and leaves auth.json as written, so the worker's reload asks the broker;
  * links `CODEX_HOME/sessions` to the workspace's `.codex/sessions`, so the
    rollouts stay where the checkpoint keeps them and `latest_session` reads
    them; `exec resume <id>` from a fresh CODEX_HOME so linked finds the
    session (measured);
  * stops codex EXPIRY_MARGIN_SECONDS before the token's `exp` (on a held
    account, whose run is watched) and reports a refused credential marked
    EXPIRY_MARKER: the worker re-reads the account the broker has refreshed
    and resumes the session with `exec resume`, the path #626 built;
  * reads the plan's refusals: a model the plan does not serve fails the step
    with the plan's own sentence; a usage limit is a quota signal with a
    reading for the account (the worker swaps or parks, as for a Claude 429);
    a 401 or 403 is a refused credential, which the worker releases as
    unusable when a reload brings back the same token;
  * copies codex's rate-limit report (`token_count.rate_limits` in the
    rollout: `primary`/`secondary`, `used_percent`, `window_minutes`,
    `resets_at` in epoch seconds) onto the account channel as readings, which
    the worker forwards through the broker's readings route.

What is ASSUMED, not measured (no live plan login in this image): the exact
text of a live usage-limit and model refusal, taken from the strings in the
0.154.0 binary and AI Studio's first-hand record; and that a plan access token
is a JWT with `exp` -- one that is not runs to its 401 instead.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import graphstage
# The plan's access token and account id, as the worker names them for an
# account leased from the provider "openai" pool, and the marker of the stop
# before the token's hour: one definition, the worker's.
from ..accountlease import PLAN_ACCOUNT_ENV, PLAN_EXPIRY_MARKER, PLAN_TOKEN_ENV
from ..children import git_shim_argv
from ..gitmcp.server import SERVER_NAME as GIT_MCP_SERVER_NAME
from ..gitmcp.server import SPOOL_ENV as GIT_SPOOL_ENV
from .base import (
    CredentialRevokedSignal,
    QuotaExhaustedSignal,
    RunnerContext,
    RunnerFailure,
    run_runner,
)
from .claude_code import RelayServer, mcp_servers_note, relay_servers
from . import codex_events
from .cliagent import (
    _SENSITIVE_PASSTHROUGH,
    ACCOUNT_STREAM_ENV,
    SESSION_FILE_ENV,
    CliAgentSpec,
    cli_stream_files,
    run_cli_agent,
)
from ..redact import collect_secrets, scrub_text

#: The CLI child's variable holding the relay's LOCAL token, named by every
#: relay server's `bearer_token_env_var` (never the caller's credential).
MCP_RELAY_TOKEN_ENV = "SWARM_MCP_RELAY_TOKEN"

#: `$HOME/.codex/sessions/YYYY/MM/DD/rollout-<local time>-<session uuid>.jsonl`,
#: one per session, appended to as it runs and again when it is resumed.
_ROLLOUT_GLOB = ".codex/sessions/*/*/*/rollout-*.jsonl"
_ROLLOUT_NAME = re.compile(
    r"^rollout-.+-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.jsonl$"
)


def rollout_files(home: Path) -> list[Path]:
    """Every rollout under `home`, by the name codex gives one. The budget
    watcher reads the `token_count` records each gains while a start runs
    (`budgetwatch`; BGD's pick "Also read the rollout", 2026-10-27): codex's
    `--json` stream reports usage only when the start's one turn ends."""
    return [path for path in home.glob(_ROLLOUT_GLOB) if _ROLLOUT_NAME.match(path.name)]


def latest_session(home: Path, since: float) -> str | None:
    """The session of the newest rollout written at or after `since`, or None.

    `codex exec` names its session only here: its stdout is the final answer
    in prose. NOT the newest rollout of all: a checkpoint restores `.codex/`
    with the workspace (`checkpoint.py`), so the previous attempt's session is
    on disk before this run writes a line, and resuming it would continue a
    conversation this attempt never had. A link is not followed -- the agent
    owns this directory -- and the id comes from the name, which codex writes
    and the `_SESSION_ID` shape check in `cliagent` still guards.
    """
    best: tuple[float, str] | None = None
    for path in home.glob(_ROLLOUT_GLOB):
        match = _ROLLOUT_NAME.match(path.name)
        if match is None:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_mtime < since:
            continue
        if best is None or info.st_mtime > best[0]:
            best = (info.st_mtime, match.group(1))
    return best[1] if best else None


SPEC = CliAgentSpec(
    name="codex",
    provider="openai",
    binary_env="CODEX_BIN",
    binary_default="codex",
    args_env="CODEX_ARGS",
    # `--json` (CODEX-JSON, BGD's answer to BUDGET-CAP question 6, 2026-10-26):
    # stdout is codex's event stream, whose `turn.completed` usage the budget
    # meter counts. The capture is rendered readable when the run is done
    # (`codex_events.finish_capture`); the raw stream is kept beside it.
    args_default=("exec", "--json", "--skip-git-repo-check"),
    key_env="OPENAI_API_KEY",
    model_flag="--model",
    transcript_name="codex-transcript.json",
    # `codex exec -` reads the prompt from stdin, where `cliagent` writes it:
    # never in the argv, which every process listing shows (owner decision
    # 2026-10-06, observer P9).
    stdin_arg=("-",),
    # A refused credential is reloaded and the session CONTINUED, not started
    # again from the prompt (#626): `codex exec [flags] resume <id> -`, the
    # flags still the parent `exec`'s, the reload's message on stdin.
    resume_flag="resume",
    # Its rollout names the session. Under `--json` the stream's
    # `thread.started` does too, as `thread_id`, which `cliagent` does not
    # read; the rollout is read either way.
    session_locator=latest_session,
    # The rollout's `token_count` records, read as the start runs: the
    # running usage the stream reports only at `turn.completed`.
    usage_files=rollout_files,
)


# ---------------------------------------------------------------------------
# A ChatGPT plan from the shared pool (part of #2688; see the module docstring)
# ---------------------------------------------------------------------------

#: Where codex reads auth.json and writes its state.
CODEX_HOME_ENV = "CODEX_HOME"

#: codex 0.154.0's override of the URL it refreshes a ChatGPT sign-in at.
REFRESH_URL_ENV = "CODEX_REFRESH_TOKEN_URL_OVERRIDE"

#: Port 9 (discard) on loopback: nothing listens there in a runner pod, so a
#: refresh codex attempts fails at once, in the pod, and never reaches
#: auth.openai.com with anything.
REFUSED_REFRESH_URL = "http://127.0.0.1:9/oauth/token"

#: How long before the access token's `exp` codex is stopped for a reload.
#: Ten minutes: the broker refreshes a held account before that, and the stop
#: waits for no turn boundary (codex's stream marks none mid-turn), so the
#: resumed session repeats at most the one step it was in.
EXPIRY_MARGIN_SECONDS = 600.0

#: The refused-credential marker of the runner's own stop before the hour.
#: The worker reads it (`lifecycle`) as a scheduled reload: not an auth error
#: on the account's health, not one of the bounded refusal reloads.
EXPIRY_MARKER = PLAN_EXPIRY_MARKER

#: The marker of a 403, which cliagent's credential markers do not name.
FORBIDDEN_MARKER = "403 forbidden"

#: The plan's refusal of a model by name, as AI Studio recorded it live
#: ("The 'gpt-4o' model is not supported when using Codex with a ChatGPT
#: account"): the step fails with this sentence, not as a credential.
_MODEL_REFUSAL = re.compile(
    r"The '([A-Za-z0-9._:\-]{1,128})' model is not supported when using Codex "
    r"with a ChatGPT account\.?"
)

#: The plan's usage limit, as the 0.154.0 binary words it ("You've hit your
#: usage limit. ...") and as its error type names it.
_USAGE_LIMIT = re.compile(
    r"you(?:'|’|\\u2019)ve hit your usage limit|usage_limit_reached", re.IGNORECASE
)

#: A 403 standing as a status: `unexpected status 403 Forbidden`, `HTTP 403`.
_FORBIDDEN = re.compile(r"(?<![\w.,-])403(?!\w|[.,]\d)\s*forbidden|\bstatus\W{0,3}403\b",
                        re.IGNORECASE)

#: codex's windows by length, named as the Claude pool names its windows so
#: the broker's readings route reads both alike.
_WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}


def plan_credential(environ: Any = None) -> tuple[str, str] | None:
    """(access token, account id) for a run on a ChatGPT plan, or None.

    None whenever an OpenAI API key is present: a tenant's bare secret fills
    every declared name with the same string (`secrets.resolve_credentials`),
    and that is the tenant's key, never a plan sign-in. The pool's account
    fills the two plan names alone (`accountlease.credential_env_from_account`).
    """
    env = os.environ if environ is None else environ
    if (env.get(SPEC.key_env) or "").strip():
        return None
    token = (env.get(PLAN_TOKEN_ENV) or "").strip()
    account = (env.get(PLAN_ACCOUNT_ENV) or "").strip()
    if not token or not account or token == account:
        return None
    return token, account


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).rstrip(b"=").decode()


def _unsigned_id_token(account_id: str) -> str:
    """A JWT-shaped stand-in for the sign-in's `id_token`, carrying the account.

    codex 0.154.0 decodes `tokens.id_token` for its claims and never verifies
    or sends it; one with an empty part is refused (measured), so the third
    part is a fixed word, not a signature anybody could check.
    """
    header = _b64({"alg": "none", "typ": "JWT"})
    claims = _b64({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}})
    return f"{header}.{claims}.{_b64({'unsigned': True})}"


def auth_json(token: str, account_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The auth.json codex reads for a ChatGPT sign-in, with NO refresh token.

    `last_refresh` is now, so codex does not try its periodic refresh of a
    sign-in it believes old.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _unsigned_id_token(account_id),
            "access_token": token,
            "refresh_token": "",
            "account_id": account_id,
        },
        "last_refresh": stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def _private_dir(environ: Any = None) -> Path | None:
    """The worker's private scratch, from the files it named there, or None."""
    env = os.environ if environ is None else environ
    for name in (ACCOUNT_STREAM_ENV, SESSION_FILE_ENV):
        value = (env.get(name) or "").strip()
        if value and Path(value).parent.is_dir():
            return Path(value).parent
    return None


def write_plan_home(token: str, account_id: str, work_dir: Path) -> Path:
    """A private CODEX_HOME holding auth.json (0600), its sessions linked home.

    In the worker's `private/` when it named one, else the system's temporary
    directory; never `work/`. The directory is 0700 and auth.json is created
    0600 in one call, so there is no moment it is readable by anybody else.
    """
    home = Path(tempfile.mkdtemp(prefix="codex-home-", dir=_private_dir()))
    os.chmod(home, 0o700)
    sessions = work_dir / ".codex" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (home / "sessions").symlink_to(sessions, target_is_directory=True)
    fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(auth_json(token, account_id), handle)
    return home


def remove_plan_home(home: Path) -> None:
    """auth.json first, then the directory; the linked sessions are kept."""
    try:
        (home / "auth.json").unlink(missing_ok=True)
    except OSError:
        pass
    try:
        (home / "sessions").unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(home, ignore_errors=True)


def token_expiry(token: str) -> float | None:
    """The access token's `exp`, epoch seconds, read without verifying; or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return float(exp)


class ExpiryStop:
    """Asks the watched run to stop EXPIRY_MARGIN_SECONDS before the token's hour.

    It sets `ctx.stop_requested`, which the watched loop honours by stopping
    the CLI (`cliagent._run_watched`), and remembers that it did, so `body`
    reports a refused credential rather than a SIGTERM. A token already inside
    the margin is not stopped at all: it runs to its 401, which the ordinary
    refused-credential path answers, rather than looping on restarts.
    """

    def __init__(self, ctx: RunnerContext, token: str) -> None:
        self.fired = False
        self._ctx = ctx
        self._timer: threading.Timer | None = None
        exp = token_expiry(token)
        if exp is None:
            return
        delay = exp - EXPIRY_MARGIN_SECONDS - time.time()
        if delay <= 0:
            return
        self._timer = threading.Timer(delay, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        if not self._ctx.stop_requested:
            self.fired = True
            self._ctx.stop_requested = True

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()


def readings_of(rate_limits: Any) -> dict[str, dict[str, Any]]:
    """codex's `rate_limits` as account readings: {window: {utilization, resets_at}}.

    A window not 300 or 10080 minutes long is named `window_<minutes>m`.
    Anything that does not say a percentage, a length and a reset is dropped
    rather than guessed at, as `cliagent._reading_of` drops a Claude one.
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(rate_limits, dict):
        return out
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        minutes = window.get("window_minutes")
        resets = window.get("resets_at")
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               for v in (used, minutes, resets)):
            continue
        if not 0.0 <= float(used) <= 100.0 or int(minutes) <= 0:
            continue
        try:
            at = datetime.fromtimestamp(float(resets), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        name = _WINDOW_NAMES.get(int(minutes), f"window_{int(minutes)}m")
        out[name] = {"utilization": round(float(used) / 100.0, 6), "resets_at": at.isoformat()}
    return out


def latest_rate_limits(home: Path, since: float) -> dict[str, dict[str, Any]]:
    """The readings of the last `rate_limits` in a rollout written since `since`."""
    best: tuple[float, Path] | None = None
    for path in rollout_files(home):
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_mtime < since:
            continue
        if best is None or info.st_mtime > best[0]:
            best = (info.st_mtime, path)
    if best is None:
        return {}
    found: dict[str, dict[str, Any]] = {}
    try:
        lines = best[1].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        if '"rate_limits"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            readings = readings_of(payload.get("rate_limits"))
            if readings:
                found = readings
    return found


def note_on_channel(*, session: str | None, readings: dict[str, dict[str, Any]]) -> None:
    """Merge the session and the readings into the account channel. Never raises.

    The channel is the worker's (`lifecycle._watch_account` forwards its
    readings through the broker's readings route; `_move_account_after` reads
    the session a swap or reload resumes). cliagent's watcher writes it from
    claude-code's stream, whose events codex does not emit, so this adds what
    codex reports and keeps every other key. Replaced atomically, mode 0600.
    """
    channel = os.environ.get(ACCOUNT_STREAM_ENV, "").strip()
    if not channel or (not session and not readings):
        return
    path = Path(channel)
    try:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if session:
            data["session_id"] = session
        if readings:
            merged = data.get("readings") if isinstance(data.get("readings"), dict) else {}
            data["readings"] = {**merged, **readings}
        tmp = path.with_name(f".{path.name}.codex.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        return


class ReadingPump:
    """Copies codex's rate limits to the channel while it runs, once a minute.

    The worker forwards what it finds there (`ReadingForwarder` throttles), so
    a long run's windows reach the broker before the run ends, not only at it.
    """

    INTERVAL_SECONDS = 60.0

    def __init__(self, home: Path, since: float) -> None:
        self._home = home
        self._since = since
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.INTERVAL_SECONDS):
            note_on_channel(session=None, readings=latest_rate_limits(self._home, self._since))

    def stop(self) -> None:
        self._stop.set()


def _refusal_text(ctx: RunnerContext) -> str:
    """The capture's stdout and stderr tails, where codex reports a refusal."""
    files = cli_stream_files(SPEC)
    text = []
    for name in (files.stdout, files.stderr):
        try:
            text.append((ctx.artifacts_dir / name).read_text(errors="replace")[-20000:])
        except OSError:
            continue
    return "\n".join(text)


def judge_plan_refusal(
    exc: Exception, text: str, readings: dict[str, dict[str, Any]], expired: bool
) -> Exception:
    """What a failed run on a plan raises instead of `exc`, or `exc` itself.

    In order: the runner's own stop before the hour; a model the plan
    refuses (the plan's sentence, as the failure); the usage limit (a quota
    signal, reset at the spent window's reset); a 403 (a refused credential).
    A 401 is already a refused credential (`cliagent`'s "unauthorized").
    """
    spend = getattr(exc, "spend", None)
    if expired:
        return CredentialRevokedSignal(
            provider=SPEC.provider,
            detail="codex was stopped for a credential reload before its access token expired",
            marker=EXPIRY_MARKER,
            spend=spend,
        )
    refused = _MODEL_REFUSAL.search(text)
    if refused is not None:
        sentence = refused.group(0)
        return RunnerFailure(sentence if sentence.endswith(".") else sentence + ".", spend=spend)
    if isinstance(exc, QuotaExhaustedSignal):
        return exc
    if _USAGE_LIMIT.search(text):
        spent = [r for r in readings.values() if r.get("utilization", 0) >= 1.0]
        reset = min((r["resets_at"] for r in spent), default=None)
        return QuotaExhaustedSignal(
            provider=SPEC.provider,
            reset_at=reset,
            detail="codex reported the ChatGPT plan's usage limit",
            spend=spend,
        )
    if not isinstance(exc, CredentialRevokedSignal) and _FORBIDDEN.search(text):
        return CredentialRevokedSignal(
            provider=SPEC.provider,
            detail="codex was refused its ChatGPT plan credential (403)",
            marker=FORBIDDEN_MARKER,
            spend=spend,
        )
    return exc


def run_on_plan(
    ctx: RunnerContext,
    plan: tuple[str, str],
    extra_args: list[str],
    extra_env: dict[str, str],
) -> dict[str, Any]:
    """`run_cli_agent` on a ChatGPT plan: auth.json written, watched, removed."""
    token, account_id = plan
    since = time.time()
    home = write_plan_home(token, account_id, ctx.work_dir)
    env = {**extra_env, CODEX_HOME_ENV: str(home), REFRESH_URL_ENV: REFUSED_REFRESH_URL}
    # The plan's token is the credential the child is handed; the account id
    # and any API key are not (codex reads both from auth.json).
    spec = replace(SPEC, key_env=PLAN_TOKEN_ENV, alt_key_envs=())
    expiry = ExpiryStop(ctx, token)
    pump = ReadingPump(ctx.work_dir, since)
    try:
        out = run_cli_agent(ctx, spec, extra_args=extra_args, extra_env=env)
    except (RunnerFailure, QuotaExhaustedSignal, CredentialRevokedSignal) as exc:
        expiry.cancel()
        expired = expiry.fired
        if expired:
            # The runner's own stop, not the worker's SIGTERM.
            ctx.stop_requested = False
        readings = latest_rate_limits(ctx.work_dir, since)
        if _USAGE_LIMIT.search(_refusal_text(ctx)) and readings:
            # The spent window, read as full: codex's last report before the
            # refusal can trail it by a request.
            name = max(readings, key=lambda key: readings[key]["utilization"])
            readings[name] = {**readings[name], "utilization": 1.0}
        judged = judge_plan_refusal(exc, _refusal_text(ctx), readings, expired)
        note_on_channel(session=latest_session(ctx.work_dir, since), readings=readings)
        if judged is exc:
            raise
        raise judged from None
    else:
        note_on_channel(session=latest_session(ctx.work_dir, since),
                        readings=latest_rate_limits(ctx.work_dir, since))
        return out
    finally:
        expiry.cancel()
        pump.stop()
        remove_plan_home(home)


def git_tools_args(spool: str) -> list[str]:
    """`-c` overrides registering the `swarm-git` shim as a stdio MCP server.

    Each value is TOML; a JSON string and a JSON array of strings are both
    valid TOML, so `json.dumps` quotes them.
    """
    command, *args = git_shim_argv(spool)
    key = f"mcp_servers.{GIT_MCP_SERVER_NAME}"
    return ["-c", f"{key}.command={json.dumps(command)}",
            "-c", f"{key}.args={json.dumps(args)}"]


def graph_tools_args(snapshot: str, workdir: str | None) -> list[str]:
    """`-c` overrides registering `swarm-graph`, as `git_tools_args` does the shim."""
    command, *args = graphstage.server_argv(snapshot, workdir)
    key = f"mcp_servers.{graphstage.SERVER_NAME}"
    return ["-c", f"{key}.command={json.dumps(command)}",
            "-c", f"{key}.args={json.dumps(args)}"]


def graph_shim_args(pin: str, ticket_file: str, url: str, workdir: str | None) -> list[str]:
    """`-c` overrides registering the graph shim as `swarm-graph`, its URL in `env`."""
    command, *args = graphstage.shim_argv(pin, ticket_file, workdir)
    key = f"mcp_servers.{graphstage.SERVER_NAME}"
    env = "{" + f"{json.dumps(graphstage.URL_ENV)} = {json.dumps(url)}" + "}"
    return ["-c", f"{key}.command={json.dumps(command)}",
            "-c", f"{key}.args={json.dumps(args)}",
            "-c", f"{key}.env={env}"]


def relay_tools_args(servers: list[RelayServer]) -> list[str]:
    """`-c` overrides naming each of the caller's servers at the relay's
    loopback URL, its bearer token read from MCP_RELAY_TOKEN_ENV."""
    args: list[str] = []
    for server in servers:
        key = f"mcp_servers.{server.name}"
        args += ["-c", f"{key}.url={json.dumps(server.url)}",
                 "-c", f"{key}.bearer_token_env_var={json.dumps(MCP_RELAY_TOKEN_ENV)}"]
    return args


def body(ctx: RunnerContext) -> dict[str, Any]:
    spool = os.environ.get(GIT_SPOOL_ENV, "").strip()
    extra_args: list[str] = []
    extra_env: dict[str, str] = {}
    if spool:
        extra_args += git_tools_args(spool)
        extra_env[GIT_SPOOL_ENV] = spool
    snapshot = os.environ.get(graphstage.SNAPSHOT_ENV, "").strip()
    served = graphstage.served_env(os.environ)
    workdir = str(ctx.repo_dir) if ctx.repo_dir else None
    if served:
        extra_args += graph_shim_args(*served, workdir)
    elif snapshot:
        extra_args += graph_tools_args(snapshot, workdir)
    if served or snapshot:
        context = os.environ.get(graphstage.CONTEXT_ENV, "").strip()
        if context:
            extra_env[graphstage.CONTEXT_ENV] = context
    # The caller's registered MCP servers, through the worker's relay
    # (MCP-RUNNERS): loopback URLs in the argv, the local token in the env.
    token, attached = relay_servers()
    if attached:
        extra_args += relay_tools_args(attached)
        extra_env[MCP_RELAY_TOKEN_ENV] = token
    unstaged = graphstage.unstaged_note(os.environ)
    prompt = ctx.payload.get("prompt")
    if unstaged and isinstance(prompt, str) and prompt.strip():
        # A new dict on the same context: `run_runner` holds this object.
        prompt = f"{prompt.rstrip()}\n\n{unstaged}"
        ctx.payload = {**ctx.payload, "prompt": prompt}
    if attached and isinstance(prompt, str) and prompt.strip():
        ctx.payload = {**ctx.payload, "prompt": f"{prompt.rstrip()}\n\n{mcp_servers_note(attached)}"}
    plan = plan_credential()
    try:
        if plan is not None:
            # A ChatGPT plan from the pool: auth.json, never an API key.
            out = run_on_plan(ctx, plan, extra_args, extra_env)
        elif not extra_args and not extra_env:
            out = run_cli_agent(ctx, SPEC)
        else:
            out = run_cli_agent(ctx, SPEC, extra_args=extra_args, extra_env=extra_env)
    finally:
        # Every outcome, the budget's stop and every failure included: the
        # Logs tab reads this capture whichever way the run ended.
        answer = finish_log(ctx, extra_env.get(MCP_RELAY_TOKEN_ENV))
    if answer:
        # The agent's last message, not the tail of the event stream that
        # `cliagent._summarise` falls back to for events it does not know.
        out["summary"] = answer
    return out


def finish_log(ctx: RunnerContext, relay_token: str | None = None) -> str | None:
    """Render the codex capture readable (`codex_events.finish_capture`) and
    return the agent's last message, or None. The values `run_cli_agent`
    redacts the capture of are redacted again from every decoded string."""
    secrets = collect_secrets(
        [os.environ.get(name) for name in (SPEC.key_env, *SPEC.alt_key_envs, PLAN_TOKEN_ENV,
                                           *_SENSITIVE_PASSTHROUGH)]
        + [relay_token]
    )
    files = cli_stream_files(SPEC)
    return codex_events.finish_capture(
        ctx.artifacts_dir, files.stdout, files.transcript or SPEC.transcript_name,
        lambda text: scrub_text(text, secrets),
    )


def main() -> int:
    return run_runner(body, name="codex")


if __name__ == "__main__":
    raise SystemExit(main())
