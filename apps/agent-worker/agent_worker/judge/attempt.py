"""One attempt's Jev, from the worker's setup to the runner's calls (lane CTXQ-JW).

TWO PROCESSES, ONE LEDGER. The worker reads the stored settings (it holds
Firestore); the runner makes J1-J5 (it holds the checkout and the agent's
outputs). So:

* `setup` runs in the WORKER, inside `graphstage.prepare` -- the lifecycle's
  one existing call at attempt setup, which also hands it the task profile's
  registered secret NAMES (`RunnerProfile.secrets`, lane JEV-SCRUB). It
  reads the workspace's ONE settings document (`workspace_settings/{tenant}`:
  `code_intelligence.jev`, beside JUDGE-SELECT's `judgment_backend`,
  context-quality.md §4.7) and the repository's registration, and calls
  `register.prepare`, which writes `<attempt>/judge/config.json` (those
  names among it, never a value) and the shared ledger -- or nothing at all when Jev is off. It returns
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
workspace's key, and without a workflow switch this module reads only what
the worker already resolved into `private/` (`jevkey.KEY_FILE_NAME`, written
for a live-browser attempt); with no key there, TypeSafe is `no_key` and no
call is made (§5.5's first row).

THE WORKFLOW SWITCH (the owner's experiment, 2026-11-01). The signed
dispatch block's `jev_judgments` (`SWITCH`, `swarm_api.validation
.DISPATCH_SWITCHES`) decides per step: `off` sets nothing up, whatever the
workspace allows; `on` runs the judgments on the step's own profile,
claude-code included, wherever the workspace lets Jev see code
(`code_intelligence.jev`, and no repository `off`) and its stored backend
can answer. For that, on the `typesafe` backend with no shim and no key in
`private/`, `setup` asks `resolve_key` for the WORKSPACE's TypeSafe key
(`jevkey.resolve_workspace`, exactly as the quality gates resolve theirs: no
submitter, registered for redaction), which `register.prepare` writes as the
one 0600 file `private/jev-key`; `finish` removes it after the agent. With
the forwarder on, the shim is used instead and no key is read. The switch
names no backend: it only narrows, or reaches a step the live browser does
not, under the workspace's own choice (invariant 10). `setup` fills
`record` with the switch, its value, the deciding source (`workflow switch`,
`repository`, `workspace` or `default`) and the outcome, which
`graphstage.prepare` writes into `stage.json`.

NEVER RAISES. A settings read that fails is "off" with its reason, and the
attempt runs exactly as the local-only run does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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
#: The workflow switch in the signed dispatch block (2026-11-01): `on` | `off`.
SWITCH = "jev_judgments"
ON, OFF = "on", "off"
SOURCE_SWITCH = "workflow switch"


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


def _read(db: Any, tenant_id: str, repository_url: str | None,
          options: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any] | None]:
    """`(workspace_settings, registration or None)`, by either path."""
    from agent_worker import indexrun

    state = indexrun.state_routes(db)
    if state is None:
        snap = db.collection(WORKSPACE_SETTINGS_COLLECTION).document(tenant_id).get(**options)
        workspace = (snap.to_dict() or {}) if snap.exists else {}
        return workspace, _registration(db, tenant_id, repository_url, options)
    served = indexrun.route_context(state).get("workspace_settings")
    workspace = served if isinstance(served, Mapping) else {}
    where = indexrun.target(tenant_id, repository_url)
    if isinstance(where, str):
        return workspace, None
    found = indexrun.route_repository(state, where)
    return workspace, None if isinstance(found, str) else found[0]


def _source(switch: str | None, given: settings_mod.JudgeSettings,
            workspace: Mapping[str, Any]) -> str:
    """What decided whether this step's judgments may run."""
    if switch == OFF:
        return SOURCE_SWITCH
    if given.repository_override == OFF:
        return "repository"
    block = workspace.get(CODE_INTELLIGENCE_FIELD)
    stored = block.get("jev") if isinstance(block, Mapping) else None
    if not given.code_opt_in:
        return "workspace" if stored is not None else "default"
    return SOURCE_SWITCH if switch == ON else "workspace"


def setup(*, work: Path, db: Any, tenant_id: str, repository_url: str | None,
          call_options: Mapping[str, Any] | None = None,
          environ: Mapping[str, str] | None = None, log: Any = None,
          secret_env_names: Iterable[str] = (), switch: str | None = None,
          resolve_key: Callable[[], str | None] | None = None,
          record: dict[str, Any] | None = None) -> dict[str, str]:
    """The worker's half: `{CONFIG_ENV: path}` when this attempt may ask Jev, else {}.

    `secret_env_names` are the task profile's registered secrets
    (`RunnerProfile.secrets`), NAMES only: the config carries them, and the
    runner's client reads their values from its own environment and drops
    every string holding one (context-quality.md §5.4 step 4). Without them
    a profile secret that does not look like a credential would be sent.

    An unregistered repository, or one registered by another tenant, reads as
    "no override"; the workspace's own opt-in still decides.

    A tenant switched to the state routes passes its attempt's state client
    as `db` (`indexrun.state_routes`): the workspace's settings from
    `state/context`, the registration from `state/repository`, and no
    Firestore call.

    `switch` is the signed dispatch block's `jev_judgments` (`SWITCH`), None
    when unset; `resolve_key` the worker's resolver of the workspace's
    TypeSafe key, asked only under `on` (see the module docstring). `record`,
    when given, is filled with `{switch, value, source, on, reason}`.
    """
    switch = switch if switch in (ON, OFF) else None
    note = record if record is not None else {}
    note.update({"switch": SWITCH, "value": switch})
    if switch == OFF:
        note.update({"source": SOURCE_SWITCH, "on": False,
                     "reason": "jev judgments are off (workflow switch)"})
        if log is not None:
            log.info("jev", on=False, reason="workflow_switch_off")
        return {}
    try:
        options = dict(call_options or {})
        workspace, registration = _read(db, tenant_id, repository_url, options)
        given = judge_settings(workspace, registration)
        note["source"] = _source(switch, given, workspace)
        attempt_dir = Path(work).parent
        private = attempt_dir / register.KEY_DIR
        shim = _read_shim(private)
        if shim is not None:
            # The forwarder (GKE, switched on): the shim, never the key file.
            shim_url, key = shim
        else:
            shim_url, key = None, _read_key(private)
        if switch == ON and shim is None and not key and resolve_key is not None \
                and given.backend == settings_mod.TYPESAFE \
                and settings_mod.opted_in(given) is None:
            # The WORKSPACE's key, as the quality gates resolve it; written by
            # `register.prepare` as the one 0600 file `private/jev-key`.
            key = resolve_key()
        done = register.prepare(attempt_dir=attempt_dir, settings=given, key=key,
                                environ=environ, hard_stop_paths=_hard_stops(registration),
                                secret_env_names=secret_env_names, shim_url=shim_url)
    except Exception as exc:  # noqa: BLE001 -- Jev is never worth the attempt
        if log is not None:
            log.warning("jev not set up", error=type(exc).__name__)
        note.update({"on": False, "reason": f"error ({type(exc).__name__})"})
        return {}
    note.update({"on": bool(done.registered), "reason": done.reason,
                 "backend": done.backend.name if done.backend is not None else None})
    if log is not None:
        log.info("jev", on=done.registered, reason=done.reason,
                 backend=done.backend.name if done.backend is not None else None)
    if not done.registered or done.config_path is None:
        return {}
    return {CONFIG_ENV: str(done.config_path)}


def finish(env: Mapping[str, str]) -> None:
    """After the agent, for a step whose switch said `on`: the key file
    `register.prepare` wrote (`private/jev-key`) removed. Only that file: the
    shim file is the worker's. Never raises."""
    path = (env.get(CONFIG_ENV) or "").strip()
    if not path:
        return
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        key_file = config.get("key_file") if isinstance(config, dict) else None
        if isinstance(key_file, str) and Path(key_file).name == register.KEY_FILE:
            Path(key_file).unlink(missing_ok=True)
    except (OSError, ValueError):
        return


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
