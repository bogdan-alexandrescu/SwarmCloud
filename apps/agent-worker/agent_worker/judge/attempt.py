"""One attempt's Jev, from the worker's setup to the runner's calls (lane CTXQ-JW).

TWO PROCESSES, ONE LEDGER. The worker reads the stored settings (it holds
Firestore); the runner makes J1-J5 (it holds the checkout and the agent's
outputs). So:

* `setup` runs in the WORKER, inside `graphstage.prepare` -- the lifecycle's
  one existing call at attempt setup, so `lifecycle.py` is not touched. It
  reads the workspace's ONE settings document (`workspace_settings/{tenant}`:
  `code_intelligence.jev`, beside JUDGE-SELECT's `judgment_backend`,
  context-quality.md §4.7) and the repository's registration, and calls
  `register.prepare`, which writes `<attempt>/judge/config.json` and the
  shared ledger -- or nothing at all when Jev is off. It returns
  `{CONFIG_ENV: <path>}` for the runner's environment, or `{}`.
* `load` runs in the RUNNER: the client from that config, or `(None, why)`.

Every J1-J5 call in an attempt goes through the one ledger file the config
names, so the owner's 8 calls / 120k tokens (§8 Q4) and §5.5's breaker are
per attempt, whichever step asks.

THE FORWARDER (SEC-MIGRATE-TYPESAFE). On GKE with `SWARM_TYPESAFE_INJECT`
on, the worker serves a loopback shim and writes `private/jev-typesafe.shim`
(`agent_worker.typesafe_shim`) instead of the key file; then TypeSafe is the
shim's URL (`settings.typesafe_url`) and the bearer the shim's local token.
A judge on Cloud Run has no shim and keeps the key file below.

THE KEY. The Internal backends (the default) carry none. TypeSafe needs the
workspace's key, and this module reads only what the worker already resolved
into `private/` (`jevkey.KEY_FILE_NAME`, written for a live-browser attempt);
with no key there, TypeSafe is `no_key` and no call is made (§5.5's first
row). Resolving a TypeSafe key for every attempt is a lifecycle change and is
outside this lane.

NEVER RAISES. A settings read that fails is "off" with its reason, and the
attempt runs exactly as the local-only run does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_worker.judge import client as client_mod
from agent_worker.judge import register, settings as settings_mod

#: The runner's variable: the path of the attempt's Jev config, never a key.
CONFIG_ENV = "SWARM_JUDGE_CONFIG"
#: The workspace's one settings document (§4.7, owner decision 2026-10-14).
WORKSPACE_SETTINGS_COLLECTION = "workspace_settings"
CODE_INTELLIGENCE_FIELD = "code_intelligence"
#: JUDGE-SELECT's field, a sibling of `code_intelligence` in that document.
JUDGMENT_BACKEND_FIELD = "judgment_backend"
#: The registration's own Jev override when one is stored; else its
#: `context` override (CTXQ-SET's `RepositoryPatch.context`): a repository
#: whose context is `off` sends nothing to Jev either.
REPOSITORY_JEV_FIELD = "jev"
REPOSITORY_CONTEXT_FIELD = "context"
#: `jevkey.KEY_FILE_NAME`, restated so the runner never imports the key code.
TYPESAFE_KEY_FILE = "jev-typesafe.key"
#: `typesafe_shim.SHIM_FILE_NAME`, written by the worker on GKE with the
#: forwarder switched on (SEC-MIGRATE-TYPESAFE). With it, TypeSafe is the
#: shim's loopback URL and the bearer is its local token: no key is read.
TYPESAFE_SHIM_FILE = "jev-typesafe.shim"


def judge_settings(workspace: Mapping[str, Any] | None,
                   registration: Mapping[str, Any] | None) -> settings_mod.JudgeSettings:
    """The stored documents as `JudgeSettings`: the backend is the document's
    top-level `judgment_backend`, merged into the block `from_mapping` reads."""
    doc = workspace if isinstance(workspace, Mapping) else {}
    block = doc.get(CODE_INTELLIGENCE_FIELD)
    block = dict(block) if isinstance(block, Mapping) else {}
    if JUDGMENT_BACKEND_FIELD not in block and isinstance(doc.get(JUDGMENT_BACKEND_FIELD), str):
        block[JUDGMENT_BACKEND_FIELD] = doc[JUDGMENT_BACKEND_FIELD]
    reg = registration if isinstance(registration, Mapping) else {}
    override = reg.get(REPOSITORY_JEV_FIELD)
    if override not in settings_mod.OVERRIDES:
        override = reg.get(REPOSITORY_CONTEXT_FIELD)
    return settings_mod.JudgeSettings.from_mapping({CODE_INTELLIGENCE_FIELD: block},
                                                   {"jev": override})


def _hard_stops(registration: Mapping[str, Any] | None) -> tuple[str, ...]:
    stored = (registration or {}).get("hard_stop_paths") if isinstance(registration, Mapping) \
        else None
    return tuple(p for p in stored if isinstance(p, str) and p) if isinstance(stored, list) \
        else ()


def _read_key(private_dir: Path) -> str | None:
    path = private_dir / TYPESAFE_KEY_FILE
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_shim(private_dir: Path) -> tuple[str, str] | None:
    """`(shim_url, token)` from the worker's shim file, or None."""
    from agent_worker.typesafe_shim import read_shim_file

    return read_shim_file(private_dir / TYPESAFE_SHIM_FILE)


def _registration(db: Any, tenant_id: str, repository_url: str | None,
                  options: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The repository's registration in THIS tenant, or None."""
    from agent_worker import indexrun

    where = indexrun.target(tenant_id, repository_url)
    if isinstance(where, str):
        return None
    snap = db.collection(indexrun.REPOSITORIES_COLLECTION).document(where.repo_id).get(**options)
    data = (snap.to_dict() or {}) if snap.exists else None
    return data if data is not None and data.get("tenant_id") == tenant_id else None


def setup(*, work: Path, db: Any, tenant_id: str, repository_url: str | None,
          call_options: Mapping[str, Any] | None = None,
          environ: Mapping[str, str] | None = None, log: Any = None) -> dict[str, str]:
    """The worker's half: `{CONFIG_ENV: path}` when this attempt may ask Jev, else {}.

    An unregistered repository, or one registered by another tenant, reads as
    "no override"; the workspace's own opt-in still decides."""
    try:
        options = dict(call_options or {})
        snap = db.collection(WORKSPACE_SETTINGS_COLLECTION).document(tenant_id).get(**options)
        workspace = (snap.to_dict() or {}) if snap.exists else {}
        registration = _registration(db, tenant_id, repository_url, options)
        given = judge_settings(workspace, registration)
        attempt_dir = Path(work).parent
        private = attempt_dir / register.KEY_DIR
        shim = _read_shim(private)
        if shim is not None:
            # The forwarder (GKE, switched on): the shim, never the key file.
            shim_url, key = shim
        else:
            shim_url, key = None, _read_key(private)
        done = register.prepare(attempt_dir=attempt_dir, settings=given, key=key,
                                environ=environ, hard_stop_paths=_hard_stops(registration),
                                shim_url=shim_url)
    except Exception as exc:  # noqa: BLE001 -- Jev is never worth the attempt
        if log is not None:
            log.warning("jev not set up", error=type(exc).__name__)
        return {}
    if log is not None:
        log.info("jev", on=done.registered, reason=done.reason,
                 backend=done.backend.name if done.backend is not None else None)
    if not done.registered or done.config_path is None:
        return {}
    return {CONFIG_ENV: str(done.config_path)}


def load(environ: Mapping[str, str] | None = None, *,
         transport: client_mod.Transport | None = None,
         sleep: Callable[[float], None] | None = None,
         ) -> tuple[client_mod.JudgeClient | None, str]:
    """The runner's half: this attempt's client, or None and why not."""
    env = os.environ if environ is None else environ
    path = (env.get(CONFIG_ENV) or "").strip()
    if not path:
        return None, "jev is off for this attempt"
    from agent_worker.judge.mcp import ConfigError, JevServer

    try:
        extra: dict[str, Any] = {} if transport is None else {"transport": transport}
        server = JevServer.from_config(Path(path), sleep=sleep, environ=env, **extra)
    except ConfigError:
        return None, "the attempt's Jev config could not be read"
    return server.client, "on"
