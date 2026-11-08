"""A step's managed plugins at the start of a step: verified, fetched by digest, unpacked and handed to the CLI.

docs/design/managed-plugins.md section 4 (lane 8, "Worker install, load and
launcher"), with what the loading spike measured
(docs/design/managed-plugins-spike.md). The built-ins (gstack,
security-audit) stay in `plugins.py`; this module is the managed half, and
`plugins.start` calls it, so the lifecycle's one call sets up both.

WHAT THE WORKER READS (section 4.1). Only `metadata.dispatch.plugins`, the
step's signed plugin set, which swarm-api wrote before it signed the spec
(`swarm_api.plugins.StepPlugins.record_on`). The lifecycle verified the
signature before this runs (`specverify.verify_step_spec`), and a task whose
signature does not verify never gets here; a task the legacy window admitted
UNSIGNED does, so a set on a task with no signature is refused
(`plugins_unsigned`): the tenant can write the task document, never the
signature. No catalogue document is read: the set's digest is the only
thing trusted about the bytes.

INSTALL (section 4.2), per plugin, before the agent: download
`<object>` from the plugins bucket into `private/plugins/<sha256>.tar`
(private: never checkpointed, never uploaded), refuse the step when the
object is missing, larger than the intake's cap or not the signed sha256,
unpack it into `private/plugins/<install_id>/` with the intake's entry
checks again, write its data folder, then make the tree read-only. A copy is
downloaded and verified by every attempt, never restored from a checkpoint.

EVERY REFUSAL IS A FIXED CODE AND A FIXED SENTENCE (`PluginRefused`), with
the platform-minted `install_id` and `version_id` as separate fields: never
the plugin's name or version string, never an entry path, never the prompt,
because those are upstream text.

THE LAUNCHER (section 4.4). Every hook command and MCP server command runs
through `/opt/swarm/bin/swarm-plugin-exec <install_id> -- <the original>`.
The intake job's rewrite is meant to put it there; a command the verified
tree still holds unwrapped is wrapped here, at unpack, as a function of the
verified bytes alone, so no hook of a managed plugin ever runs outside the
launcher. The launcher reads `private/plugin-env/<install_id>.json`, written
here: that plugin's non-secret values, the PATHS of its secret and file
settings (each a 0600 file under `private/`), its data folder and its runs
file. A secret never enters the runner's environment, the CLI's settings or
argv.

SETTINGS WITHOUT A KEYCHAIN (spike section 2). The CLI takes `userConfig`
only from `pluginConfigs["<name>@inline"].options` in a `--settings` file,
and drops each MCP server naming an option that is not there, without a word.
So the load file names, per plugin, the options the runner writes: each
non-secret value as itself, each sensitive field as `PLACEHOLDER`. The real
secret reaches the hook or server only through the launcher, whose
`CLAUDE_PLUGIN_OPTION_<KEY>` overrides the placeholder (measured).

A BINDING BRINGS ITS ADAPTER (section 6.2). A secret bound to the workspace's
TypeSafe key is written as the key file the quality gates read (the key, or
the forwarder's shim), and the plugin-env file names the Jev adapter: the
launcher runs that plugin's `node` commands with the adapter preloaded and
never hands the key to anything else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from .errors import ConfigError

#: The signed dispatch block's key and, inside it, the set's.
DISPATCH_KEY = "dispatch"
BLOCK_KEY = "plugins"

#: What the intake job puts before every hook and server command (section 4.4);
#: `swarm_api.pluginconfig.LAUNCHER`, restated (the worker never imports the
#: API); tests/unit/worker/test_pluginload.py holds them equal.
LAUNCHER = "/opt/swarm/bin/swarm-plugin-exec"
#: The Jev adapter the launcher preloads into a TypeSafe-bound plugin's `node`.
JEV_ADAPTER = "/opt/swarm/jev-gates/bin/jev-adapter.mjs"

#: The runner's variables: the load file's PATH and the launcher's folder of
#: per-plugin env files. Paths, never a value.
LOAD_ENV = "SWARM_PLUGIN_LOAD"
ENV_DIR_ENV = "SWARM_PLUGIN_ENV_DIR"

#: The plugins bucket (terraform/infra/plugins.tf): its name when the job
#: names one, else the name Terraform gives it by default.
BUCKET_ENV = "SWARM_PLUGINS_BUCKET"
PROJECT_ENV = "PROJECT_ID"
BUCKET_SUFFIX = "-swarm-plugins"

#: Under `private/` (never checkpointed, never uploaded).
PLUGINS_DIR = "plugins"
LOAD_FILE = "load.json"
ENV_DIR = "plugin-env"
DATA_DIR = "plugin-data"
RUNS_DIR = "plugin-runs"
SECRETS_DIR = "plugin-secrets"
FILES_DIR = "plugin-files"
#: The Jev calls file the worker already settles (`judge.account.TOOL_CALL_FILES`,
#: `qualitygates.CALLS_FILE_NAME`), so a managed Jev plugin is accounted
#: exactly as the baked gates are.
JEV_CALLS_FILE = "jev-gates.calls"

#: What a sensitive option holds in the CLI's settings: the launcher supplies
#: the real value (spike section 2, `placeholder-launcher`).
PLACEHOLDER = "set-by-swarm-plugin-exec"

#: The intake's caps (apps/plugin-intake/plugin_intake/limits.py, the owner's
#: answer to Q8), restated: the runnable tar is uncompressed, so the object is
#: at most the unpacked cap plus tar headers.
MIB = 1024 * 1024
MAX_UNPACKED_BYTES = 500 * MIB
TAR_OVERHEAD_BYTES = 64 * MIB
MAX_OBJECT_BYTES = MAX_UNPACKED_BYTES + TAR_OVERHEAD_BYTES
MAX_FILES = 20_000
MAX_ENTRIES = 2 * MAX_FILES
MAX_SEGMENTS = 32
MAX_SEGMENT_BYTES = 255
MAX_LINK_TARGET_BYTES = 4096
MAX_PARSED_FILE_BYTES = 1 * MIB
#: At most eight plugins per step (section 13, invariant 7).
MAX_PLUGINS = 8

#: Claude Code's managed settings, which can turn `--plugin-dir` off
#: (`disableSideloadFlags`, spike "What this spike did not measure").
MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")

#: The shapes the signed set may hold (swarm_api.pluginstore's patterns).
INSTALL_ID = re.compile(r"^[pw]-[0-9a-f]{12}$")
VERSION_ID = re.compile(r"^v-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PLUGIN_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
FIELD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_EVENT = re.compile(r"^[A-Za-z]{1,40}$")
_SERVER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
OPTION_PREFIX = "CLAUDE_PLUGIN_OPTION_"
BINDING_TYPESAFE = "provider_key:typesafe"

#: Section 6.3's deny list, checked again here: a signed value still never
#: redirects the runtime that runs the hook.
_DENIED_EXACT = ("PATH", "HOME", "NODE_OPTIONS")
_DENIED_PREFIXES = ("LD_", "PYTHON", "SWARM_", "GOOGLE_", "GIT_")


def variable_denied(name: str) -> bool:
    if name in _DENIED_EXACT or name.startswith(_DENIED_PREFIXES):
        return True
    return name.startswith("CLAUDE_") and not name.startswith(OPTION_PREFIX)


#: code -> the fixed sentence. Nothing is ever interpolated into one.
REFUSALS = {
    "plugins_unsigned": "The step's plugin set is not signed, so no plugin was loaded.",
    "plugin_set_invalid": "The step's signed plugin set is not in the shape this worker reads.",
    "plugin_object_foreign": "A plugin's pinned copy is not in this workspace's or the platform's folder.",
    "plugin_object_missing": "A plugin's pinned copy is missing from the plugins bucket.",
    "plugin_object_too_large": "A plugin's pinned copy is larger than any pinned copy may be.",
    "plugin_digest_mismatch": "A plugin's pinned copy does not match its signed digest.",
    "plugin_entry_refused": "A plugin's pinned copy holds an entry this worker will not unpack.",
    "plugin_manifest_mismatch": "A plugin's pinned copy names a different plugin than its signed set.",
    "plugin_name_duplicate": "Two plugins in the step's signed set share one name.",
    "plugin_sideload_disabled": "This image's Claude Code settings turn off loading a plugin from a folder.",
    "plugins_bucket_unknown": "This worker has no plugins bucket to fetch a pinned copy from.",
}


class PluginRefused(ConfigError):
    """The step's managed plugins cannot run; the agent never starts.

    A ConfigError, so the step ends CANNOT_START (`lifecycle._end_cause_of`):
    another attempt would meet the same signed set and the same bytes. The
    message is `REFUSALS[code]`; `install_id` and `version_id` travel as
    separate fields, and are the only details."""

    def __init__(self, code: str, *, install_id: str | None = None, version_id: str | None = None) -> None:
        super().__init__(REFUSALS[code])
        self.code = code
        self.install_id = install_id if isinstance(install_id, str) and INSTALL_ID.match(install_id) else None
        self.version_id = version_id if isinstance(version_id, str) and VERSION_ID.match(version_id) else None

    def detail(self) -> dict[str, str]:
        out = {"code": self.code}
        if self.install_id:
            out["install_id"] = self.install_id
        if self.version_id:
            out["version_id"] = self.version_id
        return out


@dataclass(frozen=True)
class Entry:
    """One plugin of the signed set, its shape checked."""

    install_id: str
    name: str
    scope: str
    version_id: str
    runnable_sha256: str
    object: str
    env: Mapping[str, str] = field(hash=False)
    files: Mapping[str, Mapping[str, str]] = field(hash=False)
    secrets: Mapping[str, Mapping[str, str]] = field(hash=False)
    skills: tuple[str, ...] | None
    disabled_skills: tuple[str, ...]


@dataclass(frozen=True)
class Loaded:
    """One managed plugin, installed for this attempt."""

    entry: Entry
    directory: Path
    options: Mapping[str, Any] = field(hash=False)
    runs_file: Path
    secrets_missing: tuple[str, ...] = ()
    adapter: bool = False


@dataclass(frozen=True)
class Load:
    """What `load` set up: the plugins, the runner's variables (paths only)."""

    plugins: tuple[Loaded, ...]
    env: Mapping[str, str] = field(hash=False)


# --- which plugins: the signed set ---------------------------------------------


def signed_set(task: Mapping[str, Any]) -> dict[str, Any]:
    """`metadata.dispatch.plugins`, or {} for a step with none. Refuses a set
    on a task that carries no signature (`plugins_unsigned`)."""
    metadata = task.get("metadata")
    dispatch = metadata.get(DISPATCH_KEY) if isinstance(metadata, Mapping) else None
    block = dispatch.get(BLOCK_KEY) if isinstance(dispatch, Mapping) else None
    if block is None or block == {}:
        return {}
    signature, version = task.get("spec_signature"), task.get("spec_key_version")
    if not (isinstance(signature, str) and signature and isinstance(version, str) and version):
        raise PluginRefused("plugins_unsigned")
    if not isinstance(block, Mapping):
        raise PluginRefused("plugin_set_invalid")
    return dict(block)


def _strings(value: Any, key: re.Pattern[str]) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not key.match(k) or not isinstance(v, str) or "\x00" in v:
            return None
        out[k] = v
    return out


def _entry(install_id: Any, raw: Any, *, tenant_id: str) -> Entry:
    """One signed entry, checked; `plugin_set_invalid` or `plugin_object_foreign`."""
    if not isinstance(install_id, str) or not INSTALL_ID.match(install_id) or not isinstance(raw, Mapping):
        raise PluginRefused("plugin_set_invalid")
    version_id = raw.get("version_id")

    def bad(code: str = "plugin_set_invalid") -> PluginRefused:
        return PluginRefused(code, install_id=install_id, version_id=version_id)

    name, scope, digest, obj = raw.get("name"), raw.get("scope"), raw.get("runnable_sha256"), raw.get("object")
    if not (isinstance(version_id, str) and VERSION_ID.match(version_id) and isinstance(name, str)
            and PLUGIN_NAME.match(name) and scope in ("platform", "workspace")
            and isinstance(digest, str) and SHA256.match(digest) and isinstance(obj, str)):
        raise bad()
    if (scope == "platform") != install_id.startswith("p-"):
        raise bad()
    expected = (f"platform/{digest}.tar" if scope == "platform"
                else f"tenants/{tenant_id}/{digest}.tar" if TENANT.match(tenant_id or "") else None)
    if obj != expected:
        raise bad("plugin_object_foreign")
    env = _strings(raw.get("env") or {}, VARIABLE)
    if env is None or any(variable_denied(k) for k in env):
        raise bad()
    files: dict[str, dict[str, str]] = {}
    for variable, ref in (raw.get("files") or {}).items() if isinstance(raw.get("files") or {}, Mapping) else [(0, 0)]:
        if (not isinstance(variable, str) or not VARIABLE.match(variable) or variable_denied(variable)
                or not isinstance(ref, Mapping) or not isinstance(ref.get("field"), str)
                or not FIELD.match(ref["field"]) or not isinstance(ref.get("sha256"), str)
                or not SHA256.match(ref["sha256"])):
            raise bad()
        files[variable] = {"field": ref["field"], "sha256": ref["sha256"]}
    secrets: dict[str, dict[str, str]] = {}
    for variable, ref in (raw.get("secrets") or {}).items() if isinstance(raw.get("secrets") or {}, Mapping) else [(0, 0)]:
        if (not isinstance(variable, str) or not VARIABLE.match(variable) or variable_denied(variable)
                or not isinstance(ref, Mapping) or not isinstance(ref.get("field"), str)
                or not FIELD.match(ref["field"])):
            raise bad()
        if ref.get("binding") == BINDING_TYPESAFE:
            secrets[variable] = {"field": ref["field"], "binding": BINDING_TYPESAFE}
        elif ref.get("secret") == "plugin":
            secrets[variable] = {"field": ref["field"], "secret": "plugin"}
        else:
            raise bad()
    skills_raw, disabled_raw = raw.get("skills"), raw.get("disabled_skills") or []
    skills: tuple[str, ...] | None = None
    if skills_raw is not None:
        if not isinstance(skills_raw, list) or not all(isinstance(s, str) and SKILL.match(s) for s in skills_raw):
            raise bad()
        skills = tuple(skills_raw)
    if not isinstance(disabled_raw, list) or not all(isinstance(s, str) and SKILL.match(s) for s in disabled_raw):
        raise bad()
    return Entry(install_id=install_id, name=name, scope=scope, version_id=version_id,
                 runnable_sha256=digest, object=obj, env=env, files=files, secrets=secrets,
                 skills=skills, disabled_skills=tuple(disabled_raw))


def entries(task: Mapping[str, Any]) -> tuple[Entry, ...]:
    """The signed set's entries, checked, in install-id order. Refuses the
    whole set when any entry is not in the shape this worker reads, when it
    holds more than MAX_PLUGINS, or two plugins of one name (the CLI keys
    every component by the manifest's name, spike section 1)."""
    block = signed_set(task)
    if not block:
        return ()
    if len(block) > MAX_PLUGINS:
        raise PluginRefused("plugin_set_invalid")
    tenant_id = str(task.get("tenant_id") or "")
    out = tuple(_entry(k, block[k], tenant_id=tenant_id) for k in sorted(block, key=str))
    seen: set[str] = set()
    for e in out:
        if e.name in seen:
            raise PluginRefused("plugin_name_duplicate", install_id=e.install_id, version_id=e.version_id)
        seen.add(e.name)
    return out


# --- install: fetch by digest, unpack -----------------------------------------


def bucket_name(environ: Mapping[str, str]) -> str | None:
    named = (environ.get(BUCKET_ENV) or "").strip()
    if named:
        return named
    project = (environ.get(PROJECT_ENV) or "").strip()
    return f"{project}{BUCKET_SUFFIX}" if project else None


def default_store(environ: Mapping[str, str]) -> Any:
    """The plugins bucket as the tenant GSA reads it (get-only, section 3)."""
    from .objectstore import GcsObjectStore

    bucket = bucket_name(environ)
    if not bucket:
        raise PluginRefused("plugins_bucket_unknown")
    return GcsObjectStore(bucket, project_id=(environ.get(PROJECT_ENV) or None))


def _missing(exc: BaseException) -> bool:
    """A download failure that means the object is not there to read."""
    from .objectstore import ObjectStoreError

    return isinstance(exc, (FileNotFoundError, ObjectStoreError)) or type(exc).__name__ in ("NotFound", "Forbidden")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(store: Any, entry: Entry, private_dir: Path) -> Path:
    """The pinned copy in `private/plugins/<sha256>.tar`, its digest the
    signed one; else the step is refused, and nothing is left behind."""
    target = Path(private_dir) / PLUGINS_DIR / f"{entry.runnable_sha256}.tar"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.unlink(missing_ok=True)

    def refuse(code: str) -> PluginRefused:
        target.unlink(missing_ok=True)
        return PluginRefused(code, install_id=entry.install_id, version_id=entry.version_id)

    try:
        store.download_file(entry.object, target)
    except Exception as exc:
        if _missing(exc):
            raise refuse("plugin_object_missing") from None
        target.unlink(missing_ok=True)
        raise
    if not target.is_file():
        raise refuse("plugin_object_missing")
    if target.stat().st_size > MAX_OBJECT_BYTES:
        raise refuse("plugin_object_too_large")
    if _sha256_file(target) != entry.runnable_sha256:
        raise refuse("plugin_digest_mismatch")
    return target


def _make_writable(root: Path) -> None:
    for dirpath, dirnames, _ in os.walk(root):
        os.chmod(dirpath, 0o700)
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                os.chmod(p, 0o700)


def _clear(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        _make_writable(path)
        shutil.rmtree(path)


def _check_name(name: str) -> tuple[str, ...] | None:
    """The entry's segments when its path is one this worker unpacks (the
    intake's section 2.3 step 2), else None."""
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = tuple(p for p in name.split("/") if p not in ("", "."))
    if not parts or len(parts) > MAX_SEGMENTS or ".." in parts:
        return None
    if any(len(p.encode("utf-8")) > MAX_SEGMENT_BYTES for p in parts):
        return None
    return parts


def unpack(archive: Path, destination: Path, entry: Entry) -> None:
    """Unpack the verified tar into `destination` with the intake's entry
    checks again (defence in depth, section 4.2 step 3): relative paths, no
    `..`, no duplicates after NFC and case folding, only files, directories
    and links that stay inside and do not point at a link, and the caps
    counted on the bytes written. Every byte is written by this function:
    `tarfile.extractall` is never used."""

    def refuse() -> PluginRefused:
        return PluginRefused("plugin_entry_refused", install_id=entry.install_id, version_id=entry.version_id)

    _clear(destination)
    destination.mkdir(mode=0o700, parents=True)
    try:
        tar = tarfile.open(archive, mode="r:")
    except (tarfile.TarError, OSError):
        raise refuse() from None
    with tar:
        members = []
        folded: set[str] = set()
        kinds: dict[tuple[str, ...], str] = {}
        try:
            for count, member in enumerate(tar):
                if count >= MAX_ENTRIES:
                    raise refuse()
                parts = _check_name(member.name)
                if parts is None:
                    raise refuse()
                key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
                if key in folded:
                    raise refuse()
                folded.add(key)
                if member.isdir():
                    kinds[parts] = "dir"
                elif member.isreg():
                    kinds[parts] = "file"
                elif member.issym():
                    kinds[parts] = "link"
                else:
                    raise refuse()
                members.append((parts, member))
        except tarfile.TarError:
            raise refuse() from None
        files = sum(1 for k in kinds.values() if k != "dir")
        if files > MAX_FILES:
            raise refuse()
        # A link's target, resolved inside the tree, stays inside and is not a link.
        for parts, member in members:
            if not member.issym():
                continue
            target = member.linkname
            if (not target or len(target.encode("utf-8")) > MAX_LINK_TARGET_BYTES or target.startswith("/")
                    or "\x00" in target or "\\" in target):
                raise refuse()
            resolved: list[str] = list(parts[:-1])
            for seg in PurePosixPath(target).parts:
                if seg in ("", "."):
                    continue
                if seg == "..":
                    if not resolved:
                        raise refuse()
                    resolved.pop()
                else:
                    resolved.append(seg)
            if not resolved or kinds.get(tuple(resolved)) == "link":
                raise refuse()
        written = 0
        root = destination.resolve()
        for parts, member in sorted(members, key=lambda m: m[0]):
            path = destination.joinpath(*parts)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.resolve() != root and root not in path.parent.resolve().parents:
                raise refuse()
            if member.isdir():
                path.mkdir(mode=0o700, exist_ok=True)
                continue
            if os.path.lexists(path):
                raise refuse()
            if member.issym():
                os.symlink(member.linkname, path)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise refuse()
            mode = 0o700 if member.mode & 0o111 else 0o600
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, "wb") as out, source:
                for chunk in iter(lambda: source.read(MIB), b""):
                    written += len(chunk)
                    if written > MAX_UNPACKED_BYTES:
                        raise refuse()
                    out.write(chunk)


def _read_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_PARSED_FILE_BYTES:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def manifest(root: Path) -> dict[str, Any]:
    found = _read_json(root / ".claude-plugin" / "plugin.json")
    return found if isinstance(found, dict) else {}


# --- the launcher's rewrite ------------------------------------------------------


def _wrapped(command: str) -> bool:
    words = command.split()
    return bool(words) and words[0] == LAUNCHER


def wrap_hooks(hooks: Any, install_id: str) -> Any:
    """`hooks` (the `hooks/hooks.json` shape: event -> [{matcher, hooks:
    [{type, command, args?}]}]) with every command run by the launcher: a
    shell-form command as `<launcher> <id> -- <text>` (the shell still
    expands `${CLAUDE_PLUGIN_ROOT}` and splits words, spike section 3), an
    exec-form one with the launcher as `command` and the original after
    `<id> --` in `args`. A command already wrapped is left as it is."""
    if not isinstance(hooks, dict):
        return hooks
    out: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            out[event] = groups
            continue
        new_groups = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                group = {**group, "hooks": [_wrap_hook(h, install_id) for h in group["hooks"]]}
            new_groups.append(group)
        out[event] = new_groups
    return out


def _wrap_hook(hook: Any, install_id: str) -> Any:
    if not isinstance(hook, dict) or hook.get("type", "command") != "command":
        return hook
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        return hook
    args = hook.get("args")
    if isinstance(args, list):
        if command == LAUNCHER:
            return hook
        return {**hook, "command": LAUNCHER, "args": [install_id, "--", command, *args]}
    if _wrapped(command):
        return hook
    return {**hook, "command": f"{LAUNCHER} {install_id} -- {command}"}


def wrap_servers(servers: Any, install_id: str) -> Any:
    """`mcpServers` with every stdio server's command run by the launcher,
    named to it (`--mcp <name>`, so its record says which server started);
    a server with a `url` transport has no process to wrap."""
    if not isinstance(servers, dict):
        return servers
    out: dict[str, Any] = {}
    for name, server in servers.items():
        if isinstance(server, dict) and isinstance(server.get("command"), str) and server["command"] != LAUNCHER:
            args = server.get("args") if isinstance(server.get("args"), list) else []
            label = ["--mcp", name] if isinstance(name, str) and _SERVER.match(name) else ["--mcp", "server"]
            server = {**server, "command": LAUNCHER, "args": [install_id, *label, "--", server["command"], *args]}
        out[name] = server
    return out


def _inside(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    path = (root / relative).resolve()
    return path if path == root or root in path.parents else None


def _rewrite_json(path: Path, rewrite: Callable[[Any], Any]) -> None:
    document = _read_json(path)
    if document is None:
        return
    updated = rewrite(document)
    if updated != document:
        path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")


def wrap_tree(root: Path, install_id: str) -> None:
    """Every hook and server command of the unpacked plugin, through the
    launcher: `hooks/hooks.json`, `.mcp.json`, and what `plugin.json`
    declares inline or points at inside the plugin."""
    root = root.resolve()

    def hooks_file(doc: Any) -> Any:
        if isinstance(doc, dict) and isinstance(doc.get("hooks"), dict):
            return {**doc, "hooks": wrap_hooks(doc["hooks"], install_id)}
        return doc

    def servers_file(doc: Any) -> Any:
        if isinstance(doc, dict) and isinstance(doc.get("mcpServers"), dict):
            return {**doc, "mcpServers": wrap_servers(doc["mcpServers"], install_id)}
        return wrap_servers(doc, install_id) if isinstance(doc, dict) else doc

    _rewrite_json(root / "hooks" / "hooks.json", hooks_file)
    _rewrite_json(root / ".mcp.json", servers_file)
    plugin_json = root / ".claude-plugin" / "plugin.json"
    doc = _read_json(plugin_json)
    if not isinstance(doc, dict):
        return
    for pointed, rewrite in ((doc.get("hooks"), hooks_file), (doc.get("mcpServers"), servers_file)):
        for relative in pointed if isinstance(pointed, list) else [pointed]:
            target = _inside(root, relative)
            if target is not None and target != plugin_json.resolve():
                _rewrite_json(target, rewrite)

    def inline(document: Any) -> Any:
        out = dict(document)
        if isinstance(out.get("hooks"), dict):
            out["hooks"] = (hooks_file(out["hooks"]) if "hooks" in out["hooks"]
                            else wrap_hooks(out["hooks"], install_id))
        if isinstance(out.get("mcpServers"), dict):
            out["mcpServers"] = wrap_servers(out["mcpServers"], install_id)
        return out

    _rewrite_json(plugin_json, inline)


def make_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                mode = os.stat(path).st_mode
                os.chmod(path, 0o555 if mode & stat.S_IXUSR else 0o444)
        os.chmod(dirpath, 0o555)


# --- settings and secrets -----------------------------------------------------------


def _write_private(path: Path, text: str) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def option_variable(key: str) -> str:
    """`CLAUDE_PLUGIN_OPTION_<KEY>` as swarm-api names it (`pluginconfig._option_variable`)."""
    return OPTION_PREFIX + re.sub(r"[^A-Z0-9_]", "_", key.upper())


def _typed(text: str, kind: Any) -> Any:
    if kind == "boolean":
        return text == "true"
    if kind == "number":
        try:
            number = float(text)
        except ValueError:
            return text
        return int(number) if number.is_integer() else number
    if text.startswith("[") and text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def options(plugin_manifest: Mapping[str, Any], entry: Entry) -> dict[str, Any]:
    """`pluginConfigs["<name>@inline"].options` for the runner's settings
    (spike section 2): every `userConfig` field with a signed value as that
    value, every sensitive field as PLACEHOLDER. No secret is ever written
    here: the file is on disk for the whole attempt."""
    config = plugin_manifest.get("userConfig")
    out: dict[str, Any] = {}
    if not isinstance(config, Mapping):
        return out
    for key, spec in config.items():
        if not isinstance(key, str) or not FIELD.match(key) or not isinstance(spec, Mapping):
            continue
        variable = option_variable(key)
        if spec.get("sensitive") is True:
            out[key] = PLACEHOLDER
        elif variable in entry.env:
            out[key] = _typed(entry.env[variable], spec.get("type"))
    return out


#: (install_id, variable) -> the released value: a plugin secret's or a file
#: setting's content (`routes/attempts_secrets.py`, section 6.2).
Released = Mapping[tuple[str, str], str]
#: provider -> the workspace key's text, or the forwarder's shim JSON, or None.
ProviderKey = Callable[[str], "str | None"]


def _plugin_env(entry: Entry, private_dir: Path, *, released: Released, provider_key: ProviderKey | None,
                others: Iterable[str], secret_env_names: Iterable[str] = (),
                hard_stop_paths: Iterable[str] = ()) -> tuple[dict[str, Any], tuple[str, ...], bool]:
    """The launcher's env file for one plugin, the variables whose secret was
    not delivered, and whether the Jev adapter runs it. The adapter's block
    carries the baked gate's outbound filter inputs (`qualitygates.prepare`'s
    `secret_env_names` and `hard_stop_paths`), so the managed copy filters
    what the gate it replaces filters (section 12)."""
    secrets_dir = private_dir / SECRETS_DIR / entry.install_id
    files_dir = private_dir / FILES_DIR / entry.install_id
    for folder in (secrets_dir, files_dir):
        _clear(folder)
    missing: list[str] = []
    secret_paths: dict[str, str] = {}
    adapter_vars: list[str] = []
    key_file: Path | None = None
    for variable, ref in sorted(entry.secrets.items()):
        if ref.get("binding") == BINDING_TYPESAFE:
            value = provider_key("typesafe") if provider_key is not None else None
            if not value:
                missing.append(variable)
                continue
            if key_file is None:
                key_file = _write_private(secrets_dir / "typesafe.key", value)
            adapter_vars.append(variable)
            continue
        value = released.get((entry.install_id, variable))
        if not value:
            missing.append(variable)
            continue
        secret_paths[variable] = str(_write_private(secrets_dir / variable, value))
    file_paths: dict[str, str] = {}
    for variable, ref in sorted(entry.files.items()):
        value = released.get((entry.install_id, variable))
        if value is None or hashlib.sha256(value.encode("utf-8")).hexdigest() != ref["sha256"]:
            missing.append(variable)
            continue
        file_paths[variable] = str(_write_private(files_dir / ref["field"], value))
    own = set(entry.env) | set(entry.secrets) | set(entry.files)
    document: dict[str, Any] = {
        "install_id": entry.install_id,
        "env": dict(entry.env),
        "files": file_paths,
        "secrets": secret_paths,
        "data_dir": str(private_dir / DATA_DIR / entry.install_id),
        "runs_file": str(private_dir / RUNS_DIR / f"{entry.install_id}.jsonl"),
        "drop": sorted(set(others) - own),
        "adapter": None,
    }
    if key_file is not None:
        document["adapter"] = {
            "kind": "typesafe", "module": JEV_ADAPTER, "key_file": str(key_file), "hook": entry.name,
            "variables": sorted(adapter_vars), "calls_file": str(private_dir / JEV_CALLS_FILE),
            "counts_file": str(private_dir / RUNS_DIR / f"{entry.install_id}.jev.jsonl"),
            "secret_env_names": sorted({n for n in secret_env_names if isinstance(n, str) and n}),
            "hard_stop_paths": [p for p in hard_stop_paths if isinstance(p, str) and p],
        }
    return document, tuple(missing), key_file is not None


def sideload_disabled(path: Path = MANAGED_SETTINGS) -> bool:
    found = _read_json(path)
    return isinstance(found, dict) and found.get("disableSideloadFlags") is True


# --- the whole start ------------------------------------------------------------


def load(task: Mapping[str, Any], *, private_dir: Path, store: Any = None,
         environ: Mapping[str, str] | None = None, released: Released | None = None,
         provider_key: ProviderKey | None = None,
         secret_env_names: Iterable[str] = (), hard_stop_paths: Iterable[str] = (),
         managed_settings: Path = MANAGED_SETTINGS) -> Load | None:
    """Install every plugin of the step's signed set before the agent, or
    None for a step with none. Raises PluginRefused, before the agent, for a
    set it cannot verify or a copy that is not the signed one.

    `released` holds the plugin secrets and file contents swarm-api released
    to this attempt; `provider_key` resolves a binding. A secret neither
    delivers is left unset for its plugin and counted in the record
    (`secrets_missing`): the plugin still loads, as the release route lets a
    plugin go without a secret removed after submission.

    `secret_env_names` and `hard_stop_paths` are the step's registered secret
    names and the repository's hard-stop paths, the baked gate's filter
    inputs, which a TypeSafe-bound plugin's adapter applies too. The load
    file says per plugin whether its adapter runs (`adapter`): the runner
    drops a built-in gate only for a managed Jev plugin that has its key."""
    secret_env_names = tuple(secret_env_names)
    hard_stop_paths = tuple(hard_stop_paths)
    wanted = entries(task)
    if not wanted:
        return None
    if sideload_disabled(managed_settings):
        raise PluginRefused("plugin_sideload_disabled")
    environ = os.environ if environ is None else environ
    if store is None:
        store = default_store(environ)
    private_dir = Path(private_dir)
    released = released or {}
    every_variable = {v for e in wanted for v in (*e.env, *e.secrets, *e.files)}
    loaded: list[Loaded] = []
    plugin_dirs = []
    for entry in wanted:
        archive = fetch(store, entry, private_dir)
        root = private_dir / PLUGINS_DIR / entry.install_id
        try:
            unpack(archive, root, entry)
        except PluginRefused:
            _clear(root)
            raise
        finally:
            archive.unlink(missing_ok=True)
        found = manifest(root)
        if found.get("name") != entry.name:
            _clear(root)
            raise PluginRefused("plugin_manifest_mismatch", install_id=entry.install_id,
                                version_id=entry.version_id)
        wrap_tree(root, entry.install_id)
        make_read_only(root)
        data = private_dir / DATA_DIR / entry.install_id
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        runs = private_dir / RUNS_DIR / f"{entry.install_id}.jsonl"
        runs.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        runs.unlink(missing_ok=True)
        document, missing, adapter = _plugin_env(entry, private_dir, released=released,
                                                 provider_key=provider_key, others=every_variable,
                                                 secret_env_names=secret_env_names,
                                                 hard_stop_paths=hard_stop_paths)
        _write_private(private_dir / ENV_DIR / f"{entry.install_id}.json",
                       json.dumps(document, indent=2, sort_keys=True))
        opts = options(found, entry)
        loaded.append(Loaded(entry=entry, directory=root, options=opts, runs_file=runs,
                             secrets_missing=missing, adapter=adapter))
        plugin_dirs.append({"install_id": entry.install_id, "name": entry.name, "dir": str(root),
                            "options": opts, "disabled_skills": list(entry.disabled_skills),
                            "adapter": adapter})
    load_file = _write_private(private_dir / PLUGINS_DIR / LOAD_FILE,
                               json.dumps({"plugins": plugin_dirs}, indent=2, sort_keys=True))
    return Load(plugins=tuple(loaded),
                env={LOAD_ENV: str(load_file), ENV_DIR_ENV: str(private_dir / ENV_DIR)})


# --- after the agent ----------------------------------------------------------------


def _runs(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines()[:100_000]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def record(plugin: Loaded, skill_calls: Iterable[str]) -> dict[str, Any]:
    """One managed plugin's part of the attempt summary (section 4.5): names
    and counts only, never arguments, output or settings values. Hook runs
    come from the launcher's records alone (spike section 4: the stream
    reports only SessionStart): per event the runs, the blocks (exit 2, a
    Stop hook's "not done yet") and the errors (any other non-zero exit)."""
    e = plugin.entry
    hooks: dict[str, dict[str, int]] = {}
    servers: dict[str, bool] = {}
    for row in _runs(plugin.runs_file):
        if row.get("kind") == "mcp":
            name = row.get("server")
            servers[name if isinstance(name, str) and _SERVER.match(name) else "server"] = True
            continue
        event = row.get("event")
        event = event if isinstance(event, str) and _EVENT.match(event) else "unknown"
        counts = hooks.setdefault(event, {"runs": 0, "blocks": 0, "errors": 0})
        counts["runs"] += 1
        code = row.get("exit")
        if code == 2:
            counts["blocks"] += 1
        elif isinstance(code, int) and code != 0:
            counts["errors"] += 1
    prefix = f"{e.name}:"
    skills: dict[str, int] = {}
    for call in skill_calls:
        if isinstance(call, str) and call.startswith(prefix):
            name = call[len(prefix):]
            if SKILL.match(name):
                skills[name] = skills.get(name, 0) + 1
    out: dict[str, Any] = {
        "managed": True, "name": e.name, "scope": e.scope, "version_id": e.version_id,
        "runnable_sha256": e.runnable_sha256, "hooks": dict(sorted(hooks.items())),
        "mcp_servers_started": sorted(servers), "skill_runs": dict(sorted(skills.items())),
    }
    if plugin.secrets_missing:
        out["secrets_missing"] = len(plugin.secrets_missing)
    if plugin.adapter:
        out["adapter"] = "typesafe"
    return out


__all__ = [
    "BLOCK_KEY",
    "ENV_DIR_ENV",
    "JEV_ADAPTER",
    "LAUNCHER",
    "LOAD_ENV",
    "Load",
    "Loaded",
    "PLACEHOLDER",
    "PluginRefused",
    "REFUSALS",
    "entries",
    "fetch",
    "load",
    "options",
    "record",
    "signed_set",
    "unpack",
    "wrap_hooks",
    "wrap_servers",
    "wrap_tree",
]
