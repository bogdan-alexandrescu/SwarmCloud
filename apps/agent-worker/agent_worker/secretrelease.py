"""The workspace secrets release, the worker's half (SEC-RELEASE, part of #1347).

docs/design/workspace-secrets.md §3.3, §4.1, §4.2 and §4.5. Once per attempt,
BEFORE the runner is spawned, the worker asks swarm-api for the workspace
secrets granted to what this attempt is -- its profile, the tools its signed
`browser_tool` launches -- and hands them to the agent as files.

WHO ASKS. Only this process, which holds the attempt key in its non-dumpable
heap (`children.ChildPath`): the request is `{generation}` and nothing else,
signed under `swarm-secrets-release/v1`, so the agent -- which runs as the same
uid and can mint the same ID token -- can neither ask nor ask for more. swarm-
api derives the consumers from the signed spec; nothing here names a secret.

ONLY WHEN THERE IS SOMETHING TO ASK FOR. A profile, or a tool its
`browser_tool` launches, that declares a secret slot (`consumer_slots`, from
the frozen catalogue). A worker action, the mock and anything else with no slot
makes no call.

HANDED OVER HOW (§4.1). Each value is one file,
`private/secrets/<slot>/<name>`, mode 0600, created afresh (unlinked, then
O_EXCL | O_NOFOLLOW) as `jevkey._write_private` does. `private/` is the
worker's own scratch: never checkpointed, never uploaded (workspace.py). Nothing
goes in `work/`, which is. `private/secrets/manifest.json` lists `{name, slot,
kind, path}` and no value, and the runner gets ONE variable, its path,
`SWARM_SECRETS_MANIFEST`. Never a value in the runner's environment, argv, the
MCP config, a checkpoint, an artifact or a log line.

REDACTION (§4.5). Every value -- and, for a structured kind, every field of it
-- is registered with this worker's logger the moment the answer is parsed,
before any file is written and before the runner exists. That registered set
is also what the publish-time credential scan refuses by value
(`lifecycle.Worker._leaks_in_added_text`: "a registered secret, in ANY file"),
so a diff that echoes a released value is refused by the value itself.

DEFENCE IN DEPTH (§3.4). An item whose slot this attempt's own catalogue does
not declare, whose kind its slot does not accept, whose name is not a store
name, or which arrives twice, is refused here by name and slot, never written.

WHEN IT CANNOT ASK (§4.2). No attempt key, swarm-api down, a 503 because its
read grant is not applied yet, a refusal: the attempt is UNREACHABLE. With a
required slot it parks CREDENTIAL_MISSING, at no cost (invariant 1), and is
asked again by its next attempt; with optional slots only it starts without
them. A 409 is the fence (invariant 5): `SecretReleaseFenced`, a
`ChildSubmitFenced`, so the lifecycle stands down without touching the lease.

A GATE SLOT IS NEVER A FILE (CF-ACCESS-TOKEN, part of #2442). An item in a
slot of `GATE_SLOTS` -- `cf-access`, a Cloudflare Access service token -- is
for the live gate, which adds it to requests for the hosts its grant lists,
and the agent must never read it. `private/` is the agent's uid's to read, so
such an item is held in this heap (`Released.gate`, with the grant's `hosts`)
and never written, never named in the manifest and never in `env`; the
lifecycle hands it to the gate over ctl.sock (`agent_worker.cf_access`) and
drops it. Its fields are registered for redaction like every other value.

REMOVED ON EVERY EXIT. `remove` deletes `private/secrets/` and is the first
cleanup step after the runner is gone (`lifecycle.Worker._cleanup`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from swarm_common.profiles import RUNTIME_TOOLS, RunnerProfile, SecretSlot, tools_launched

from .children import ChildApiError, ChildSubmitFenced
from .handoff import sign_headers

#: swarm-api's purpose for this route (`swarm_api.routes.attempts_secrets.
#: RELEASE_PURPOSE`), restated: this image does not carry swarm-api.
#: tests/unit/worker/test_secretrelease.py holds the two equal.
RELEASE_PURPOSE = "swarm-secrets-release/v1"

#: The runner's one variable: the manifest's PATH, never a value.
MANIFEST_ENV = "SWARM_SECRETS_MANIFEST"
SECRETS_DIR = "secrets"
MANIFEST_NAME = "manifest.json"

#: The store's name rule (`swarm_api.workspace_secrets.NAME_PATTERN`): a name
#: becomes a file name here, so nothing else is written.
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SLOT = re.compile(r"[a-z][a-z0-9-]{0,31}")

#: Slots whose items go to the live gate and never to a file (module docstring).
#: `swarm_api.workspace_secrets.HOST_SCOPED_SLOTS`, restated (this image does
#: not carry swarm-api); tests/unit/worker/test_cf_access_worker.py holds them equal.
GATE_SLOTS = ("cf-access",)
#: A gate slot's host list, bounded as swarm-api bounds it (`MAX_SLOT_HOSTS`).
MAX_GATE_HOSTS = 16

#: The structured kinds (design §2.1): a JSON object whose fields are each
#: registered for redaction too, so a password echoed alone is still caught.
_STRUCTURED = ("basic_auth", "oauth_client")

#: Why the release did not happen, as a word for the log and the park.
NO_KEY = "no_attempt_key"
UNREACHABLE = "swarm_api_unreachable"


class SecretReleaseFenced(ChildSubmitFenced):
    """409 from the release: this attempt is superseded (invariant 5)."""


def consumer_slots(profile: RunnerProfile, inputs: Mapping[str, Any] | None) -> dict[str, SecretSlot]:
    """The slots this attempt's consumers declare: its profile's, and those of
    each RUNTIME_TOOLS entry its signed `browser_tool` launches. swarm-api
    derives the same set (`workspace_secrets.attempt_consumers`)."""
    slots: dict[str, SecretSlot] = dict(profile.secret_slots)
    for name in tools_launched(profile, dict(inputs or {})):
        for slot, declared in RUNTIME_TOOLS[name].secret_slots.items():
            slots.setdefault(slot, declared)
    return slots


def wants_release(profile: RunnerProfile, inputs: Mapping[str, Any] | None) -> bool:
    return profile.worker_action is None and bool(consumer_slots(profile, inputs))


@dataclass
class Released:
    """What one attempt was handed. `env` is what the runner may see."""

    env: dict[str, str] = field(default_factory=dict)
    #: `(name, slot)` written, in order.
    delivered: list[tuple[str, str]] = field(default_factory=list)
    #: Required slots this attempt has nothing in.
    missing_required: list[str] = field(default_factory=list)
    #: Gate-slot items, held in this heap for the live gate and never written:
    #: `{name, slot, hosts, value}` each (module docstring). Emptied once handed over.
    gate: list[dict[str, Any]] = field(default_factory=list, repr=False)
    #: None when swarm-api answered; else why not (NO_KEY, UNREACHABLE, a code).
    unreachable: str | None = None
    asked: bool = False


def secrets_dir(private_dir: Path) -> Path:
    return Path(private_dir) / SECRETS_DIR


def _write_private(path: Path, text: str) -> None:
    """`jevkey._write_private`: whatever is there goes, then a fresh 0600 file."""
    if path.is_symlink() or path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _private_dir(path: Path) -> None:
    """A real 0700 directory at `path`: a symlink or a file there is removed first."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise OSError("not a directory")


def remove(private_dir: Path | None) -> None:
    """Delete `private/secrets/`, never following a link out of it."""
    if private_dir is None:
        return
    root = secrets_dir(private_dir)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)


def _register(logger: Any, kind: str, value: str) -> None:
    logger.register_secret(value)
    if kind in _STRUCTURED:
        try:
            parsed = json.loads(value)
        except ValueError:
            return
        if isinstance(parsed, dict):
            for part in parsed.values():
                if isinstance(part, str):
                    logger.register_secret(part)


def _gate_hosts(hosts: Any) -> bool:
    """A gate slot's list as swarm-api sends it: 1 to MAX_GATE_HOSTS host names."""
    return (isinstance(hosts, list) and 0 < len(hosts) <= MAX_GATE_HOSTS
            and all(isinstance(h, str) and h for h in hosts))


class SecretRelease:
    """The release for one attempt. `children` is its `ChildPath`, whose key
    and API this signs with and calls; nothing here holds the key."""

    def __init__(self, cfg: Any, *, children: Any, logger: Any,
                 wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.log = logger
        self._children = children
        self._wall = wall
        self.result: Released | None = None

    def route(self) -> str:
        return f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/secrets:release"

    def release(self, *, profile: RunnerProfile, inputs: Mapping[str, Any] | None,
                private_dir: Path) -> Released:
        """Ask once, write the files, return what the runner may see. Raises
        only `SecretReleaseFenced`; every other failure is `unreachable`."""
        if self.result is not None:
            return self.result
        slots = consumer_slots(profile, inputs)
        required = sorted(name for name, slot in slots.items() if slot.required)
        result = Released()
        self.result = result
        if profile.worker_action is not None or not slots:
            return result
        key = getattr(self._children, "_key", None)
        api = getattr(self._children, "_api", None)
        if key is None or api is None or not getattr(self._children, "keyed", False):
            return self._unreachable(result, NO_KEY, required)
        result.asked = True
        body = json.dumps({"generation": int(self.cfg.generation)}).encode("utf-8")
        route = self.route()
        try:
            headers = sign_headers(key, RELEASE_PURPOSE, "POST", route, body, now=self._wall())
            _, payload = api.call("POST", route, body=body, headers=headers)
        except ChildApiError as exc:
            if exc.status == 409:
                raise SecretReleaseFenced(exc.status, exc.code, exc.message,
                                          retryable=False) from None
            return self._unreachable(result, exc.code or UNREACHABLE, required)
        items = payload.get("secrets") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return self._unreachable(result, "bad_answer", required)
        accepted = self._accept(items, slots)
        result.gate = [{"name": name, "slot": slot, "hosts": hosts, "value": value}
                       for name, slot, _, value, hosts in accepted if slot in GATE_SLOTS]
        accepted = [item for item in accepted if item[1] not in GATE_SLOTS]
        try:
            self._write(accepted, private_dir, result)
        except OSError as exc:
            remove(private_dir)
            result.delivered.clear()
            result.env.clear()
            result.gate.clear()
            return self._unreachable(result, f"write_failed:{type(exc).__name__}", required)
        filled = {slot for _, slot in result.delivered} | {item["slot"] for item in result.gate}
        result.missing_required = [slot for slot in required if slot not in filled]
        withheld = payload.get("withheld") if isinstance(payload.get("withheld"), list) else []
        self.log.info(
            "workspace secrets released",
            names=[name for name, _ in result.delivered],
            for_the_gate=[item["name"] for item in result.gate],
            withheld=[w.get("name") for w in withheld if isinstance(w, dict)],
            missing_required=result.missing_required,
        )
        return result

    def _unreachable(self, result: Released, why: str, required: list[str]) -> Released:
        result.unreachable = why
        result.missing_required = list(required)
        self.log.warning("workspace secrets not released", reason=why,
                         required=required, starts_without=not required)
        return result

    def _accept(self, items: list[Any], slots: Mapping[str, SecretSlot]
                ) -> list[tuple[str, str, str, str, list[str]]]:
        """`(name, slot, kind, value, hosts)` for each item this attempt may
        hold; `hosts` is a gate slot's list, empty for every other slot.
        Every value is registered for redaction here, before anything else
        reads it -- a refused one included, since it has been in this heap."""
        out: list[tuple[str, str, str, str, list[str]]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name, slot, kind, value = (item.get("name"), item.get("slot"), item.get("kind"),
                                       item.get("value"))
            if isinstance(value, str) and value:
                self.log.register_secret(value)
                _register(self.log, str(kind), value)
            declared = slots.get(slot) if isinstance(slot, str) else None
            reason = None
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                reason = "name"
            elif not isinstance(slot, str) or not _SLOT.fullmatch(slot) or declared is None:
                reason = "slot"
            elif kind not in declared.kinds:
                reason = "kind"
            elif not isinstance(value, str) or not value:
                reason = "value"
            elif (name, slot) in seen:
                reason = "duplicate"
            elif slot in GATE_SLOTS and not _gate_hosts(item.get("hosts")):
                # A gate item with no list would be sent nowhere; one with a bad
                # list is not this worker's to repair. The gate checks again.
                reason = "hosts"
            if reason:
                self.log.warning("workspace secret refused by this worker",
                                 name=name if isinstance(name, str) and _NAME.fullmatch(name)
                                 else None,
                                 slot=slot if isinstance(slot, str) and _SLOT.fullmatch(slot)
                                 else None, reason=reason)
                continue
            seen.add((name, slot))
            hosts = list(item["hosts"]) if slot in GATE_SLOTS else []
            out.append((name, slot, str(kind), value, hosts))
        return out

    def _write(self, accepted: list[tuple[str, str, str, str, list[str]]], private_dir: Path,
               result: Released) -> None:
        if not accepted:
            return
        root = secrets_dir(private_dir)
        _private_dir(root)
        manifest: list[dict[str, str]] = []
        for name, slot, kind, value, _ in accepted:
            directory = root / slot
            _private_dir(directory)
            path = directory / name
            _write_private(path, value)
            manifest.append({"name": name, "slot": slot, "kind": kind, "path": str(path)})
            result.delivered.append((name, slot))
        target = root / MANIFEST_NAME
        _write_private(target, json.dumps({"secrets": manifest}, indent=2, sort_keys=True))
        result.env[MANIFEST_ENV] = str(target)
