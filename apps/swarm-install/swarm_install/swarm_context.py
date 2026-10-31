"""The engine's own swarm CLI context for the new deployment (design §4;
plan §6.3; lessons 18 and 43).

Two steps use it. `edge.desktop-client` (stage 2) takes the Desktop client's
secret at a hidden prompt (`secret_entry`) and, with the same typed value,
writes it into the swarm CLI's credential store for this context (`swarm
context add --client-secret-stdin`, the value on stdin, never in argv), so
`verify.end-to-end` (stage 14) only signs in to a context that already holds
it. The engine never reads a secret's value from Secret Manager
(tests/unit/install/test_switches.py), so the prompt is the only place the
value is ever in this process, and only while it is typed.

The context is named after the hostname. The person's current context is
never switched (`context add` without `--use`), and a context of that name
pointing elsewhere is never rewritten: the engine's is then
`<hostname>-install` (`context_name`).

The CLI is spawned by its path (`swarm_cli`): the person may have typed
`~/.local/bin/swarm install` because that directory is not yet on the PATH of
the terminal install.sh ran in, and a bare `swarm` would then not be found by
the engine's own children either.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

#: The tier-forcing variables the swarm CLI honours before the signed-in tier
#: (apps/swarm-mcp/swarm_mcp/auth.py `detect`), and gcloud's own token
#: overrides: none of them reaches a plugin run (lesson 18).
NOT_FOR_PLUGIN_RUNS = (
    "SWARM_ID_TOKEN",
    "SWARM_IMPERSONATE_SA",
    "SWARM_IAP_CLIENT_ID",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
)

#: What would send a run to a deployment other than the engine's own context
#: (apps/swarm-mcp/swarm_mcp/config.py: SWARM_URL and SWARM_CONTEXT win over
#: `--context`'s default), or as a tenant other than the one verify picked.
NOT_FOR_VERIFY_RUNS = ("SWARM_URL", "SWARM_API_URL", "SWARM_CONTEXT", "SWARM_TENANT")


def plugin_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a plugin run gets: the person's own, less anything
    that would put the swarm CLI on a tier other than the Desktop-client
    sign-in (lesson 18: a gcloud token is refused at IAP)."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items() if k not in NOT_FOR_PLUGIN_RUNS}


def verify_env(tenant: str | None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`plugin_env` less any address or tenant the person's shell names, with
    the team verify picked as SWARM_TENANT."""
    env = {k: v for k, v in plugin_env(environ).items() if k not in NOT_FOR_VERIFY_RUNS}
    if tenant:
        env["SWARM_TENANT"] = tenant
    return env


#: `run(argv, stdin, env) -> (exit code, stdout, stderr)`.
Run = Callable[[Sequence[str], str | None, dict[str, str] | None], tuple[int, str, str]]


def swarm_cli(argv0: str | None = None, executable: str | None = None,
              which: Callable[[str], str | None] = shutil.which) -> str:
    """The swarm CLI's path: the one running this engine (`swarm install`
    is `sys.argv[0]`), else the `swarm` beside this interpreter (uv's tool
    environment holds both), else the one on PATH; a bare "swarm" only when
    none is found, so the caller's error names it. The running CLI comes
    first because it is the release install.sh put beside this engine; one
    found first on PATH may be another (an older install, a checkout)."""
    running = sys.argv[0] if argv0 is None and sys.argv else (argv0 or "")
    beside = os.path.join(os.path.dirname(executable or sys.executable), "swarm")
    for path in ([os.path.abspath(running)] if os.path.basename(running) == "swarm" else []) + [beside]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return which("swarm") or "swarm"


def _not_found(swarm: str) -> RuntimeError:
    return RuntimeError(f"the swarm CLI was not found at {swarm}, beside this engine or on PATH; "
                        "install.sh installs it beside the engine")


def run_command(argv: Sequence[str], stdin: str | None = None,
                env: dict[str, str] | None = None) -> tuple[int, str, str]:
    done = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, timeout=300,
                          check=False, env=env)
    return done.returncode, done.stdout or "", done.stderr or ""


def run_interactive(argv: Sequence[str], stdin: str | None = None,
                    env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """A command the person takes part in (a browser sign-in): on the
    terminal, not captured, bounded at 15 minutes."""
    done = subprocess.run(list(argv), timeout=15 * 60, check=False, env=env)
    return done.returncode, "", ""


def _rows(found: Any, key: str) -> list[dict[str, Any]]:
    rows = found.get(key) if isinstance(found, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def deployment_url(ctx: Any) -> str:
    """`https://<hostname>`, the hostname stage 0 recorded; "" before that."""
    hostname = str(ctx.state.hostname or ctx.outputs.get("hostname") or "")
    return f"https://{hostname}" if hostname else ""


def list_contexts(run: Run = run_command, swarm: str | None = None,
                  env: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """`swarm context list --json`, by name: url, client id, signed in; never a secret."""
    swarm = swarm or swarm_cli()
    try:
        code, out, err = run([swarm, "context", "list", "--json"], None, verify_env(None) if env is None else env)
    except FileNotFoundError:
        raise _not_found(swarm) from None
    if code != 0:
        raise RuntimeError(f"swarm context list failed: {err.strip()[:300]}")
    return {str(row.get("name")): row for row in _rows(json.loads(out or "{}"), "contexts")}


def points_at(row: Mapping[str, Any] | None, url: str) -> bool:
    return row is not None and str(row.get("url") or "").rstrip("/") == url


def context_name(rows: Mapping[str, Mapping[str, Any]], hostname: str) -> str:
    """The hostname, or `<hostname>-install` when a context of that name
    points elsewhere; never a name that points elsewhere."""
    url = f"https://{hostname}"
    name = hostname
    if name in rows and not points_at(rows[name], url):
        name = f"{hostname}-install"
        if name in rows and not points_at(rows[name], url):
            raise RuntimeError(f"swarm contexts {hostname} and {name} both point elsewhere; "
                               f"neither is rewritten (swarm context list)")
    return name


def stored_context(ctx: Any, run: Run = run_command, swarm: str | None = None) -> str:
    """The context `store_desktop_secret` recorded (`verify_context`), when it
    is on this machine and points at the new deployment; else ""."""
    name = str(ctx.outputs.get("verify_context") or "")
    url = deployment_url(ctx)
    if not name or not url:
        return ""
    return name if points_at(list_contexts(run, swarm).get(name), url) else ""


def store_desktop_secret(ctx: Any, secret: bytes, *, run: Run = run_command, swarm: str | None = None) -> str:
    """Write the context for the new deployment with the Desktop client's id,
    and its secret into the swarm credential store, on stdin; returns the
    context's name, which the caller records as `verify_context`."""
    hostname = str(ctx.state.hostname or ctx.outputs.get("hostname") or "")
    client_id = str(ctx.outputs.get("desktop_client_id") or "")
    if not hostname or not client_id:
        raise LookupError("the hostname and the Desktop client id are recorded before the secret is stored")
    swarm = swarm or swarm_cli()
    name = context_name(list_contexts(run, swarm), hostname)
    try:
        code, _, err = run([swarm, "context", "add", name, f"--url=https://{hostname}",
                            f"--client-id={client_id}", "--client-secret-stdin"],
                           secret.decode() + "\n", verify_env(None))
    except FileNotFoundError:
        raise _not_found(swarm) from None
    if code != 0:
        raise RuntimeError(f"swarm context add {name} failed: {err.strip()[:300]}")
    return name
