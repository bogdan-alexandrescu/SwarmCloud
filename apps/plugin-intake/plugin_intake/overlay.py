"""The admin's overlay (§6.3): a manifest, hooks, declared settings and `requires`.

An overlay may name a plugin that ships no plugin.json, give hooks to one that
ships none, declare settings and say what the plugin needs. It never replaces
what upstream ships: what upstream ships is what runs. It is written into the
runnable tree, so the digest covers it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import limits
from .archive import DIR, FILE
from .manifest import HOOK_EVENTS, HOOKS_FILE, MANIFEST, RUNTIMES
from .refusals import Refusal
from .tree import Node, Tree

#: Where the overlay itself lives in the runnable tree.
OVERLAY_FILE = (".claude-plugin", "swarm-overlay.json")

_KEYS = frozenset({"plugin", "hooks", "settings", "requires"})
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_ARG = re.compile(r"^[A-Za-z0-9._:/=@+,-]{1,256}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_VERSION = re.compile(r"^\d+(\.\d+){0,2}$")
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SETTING_TYPES = frozenset({"string", "number", "boolean", "file"})
_ROOT = "${CLAUDE_PLUGIN_ROOT}/"

#: §6.3's deny list: a setting cannot redirect the runtime that runs the hook.
_DENIED_EXACT = frozenset({"PATH", "HOME", "NODE_OPTIONS"})
_DENIED_PREFIXES = ("LD_", "PYTHON", "SWARM_", "GOOGLE_", "GIT_")


def variable_denied(name: str) -> bool:
    if name in _DENIED_EXACT or name.startswith(_DENIED_PREFIXES):
        return True
    return name.startswith("CLAUDE_") and not name.startswith("CLAUDE_PLUGIN_OPTION_")


def parse_overlay(raw: str) -> dict[str, Any] | None:
    """The overlay as the execution received it; None when there is none."""
    if not raw:
        return None
    if len(raw.encode("utf-8")) > limits.MAX_OVERLAY_BYTES:
        raise Refusal("overlay_invalid")
    try:
        overlay = json.loads(raw)
    except ValueError:
        raise Refusal("overlay_invalid") from None
    if not isinstance(overlay, dict) or set(overlay) - _KEYS:
        raise Refusal("overlay_invalid")
    return overlay


def _plugin(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"name", "version", "description"}:
        raise Refusal("overlay_invalid")
    name = value.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise Refusal("overlay_invalid")
    rendered = {"name": name}
    for key, limit in (("version", 128), ("description", 4096)):
        if key in value:
            if not isinstance(value[key], str) or len(value[key]) > limit:
                raise Refusal("overlay_invalid")
            rendered[key] = value[key]
    return rendered


def _hook_command(tree: Tree, command: Any) -> str:
    """A §5 runtime, a path inside the plugin and fixed arguments; never a free shell line."""
    if not isinstance(command, str):
        raise Refusal("overlay_invalid")
    words = command.split(" ")
    if len(words) < 2 or words[0] not in RUNTIMES or not words[1].startswith(_ROOT):
        raise Refusal("overlay_invalid")
    path = tuple(words[1][len(_ROOT) :].split("/"))
    if any(s in ("", ".", "..") or not _ARG.match(s) for s in path) or not all(_ARG.match(w) for w in words[2:]):
        raise Refusal("overlay_invalid")
    node = tree.get(path)
    if node is None or node.kind != FILE:
        raise Refusal("overlay_hook_path_missing")
    return command


def _hooks(tree: Tree, value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or not value:
        raise Refusal("overlay_invalid")
    events: dict[str, list[dict[str, Any]]] = {}
    for hook in value:
        if not isinstance(hook, dict) or set(hook) - {"event", "matcher", "timeout", "command"}:
            raise Refusal("overlay_invalid")
        if hook.get("event") not in HOOK_EVENTS:
            raise Refusal("overlay_invalid")
        timeout = hook.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= 600):
            raise Refusal("overlay_invalid")
        matcher = hook.get("matcher")
        if matcher is not None and (not isinstance(matcher, str) or len(matcher) > 1024):
            raise Refusal("overlay_invalid")
        handler: dict[str, Any] = {"type": "command", "command": _hook_command(tree, hook.get("command"))}
        if timeout is not None:
            handler["timeout"] = timeout
        group: dict[str, Any] = {"hooks": [handler]}
        if matcher is not None:
            group["matcher"] = matcher
        events.setdefault(hook["event"], []).append(group)
    return {"hooks": events}


def _settings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise Refusal("overlay_invalid")
    seen = set()
    settings = []
    for setting in value:
        if not isinstance(setting, dict) or set(setting) - {"key", "type", "description", "sensitive", "target", "variable"}:
            raise Refusal("overlay_invalid")
        key, variable = setting.get("key"), setting.get("variable")
        if not isinstance(key, str) or not _NAME.match(key) or key in seen:
            raise Refusal("overlay_invalid")
        if setting.get("type") not in _SETTING_TYPES or setting.get("target", "env") not in ("env", "file"):
            raise Refusal("overlay_invalid")
        if not isinstance(variable, str) or not _VARIABLE.match(variable):
            raise Refusal("overlay_invalid")
        if variable_denied(variable):
            raise Refusal("overlay_variable_denied")
        sensitive = setting.get("sensitive", False)
        description = setting.get("description")
        if not isinstance(sensitive, bool) or (description is not None and (not isinstance(description, str) or len(description) > 4096)):
            raise Refusal("overlay_invalid")
        seen.add(key)
        settings.append(
            {
                "key": key,
                "type": setting["type"],
                "description": description,
                "sensitive": sensitive,
                "target": setting.get("target", "env"),
                "variable": variable,
            }
        )
    return settings


def _requires(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"runtimes", "hosts", "profiles"}:
        raise Refusal("overlay_invalid")
    runtimes = value.get("runtimes", [])
    hosts = value.get("hosts", [])
    profiles = value.get("profiles", [])
    if not all(isinstance(v, list) for v in (runtimes, hosts, profiles)):
        raise Refusal("overlay_invalid")
    rendered_runtimes = []
    for runtime in runtimes:
        if not isinstance(runtime, dict) or set(runtime) - {"name", "min_version"} or runtime.get("name") not in RUNTIMES:
            raise Refusal("overlay_invalid")
        version = runtime.get("min_version")
        if version is not None and (not isinstance(version, str) or not _VERSION.match(version)):
            raise Refusal("overlay_invalid")
        rendered_runtimes.append({"name": runtime["name"], "min_version": version})
    if not all(isinstance(h, str) and _HOST.match(h) for h in hosts):
        raise Refusal("overlay_invalid")
    if not all(isinstance(p, str) and _PROFILE.match(p) for p in profiles):
        raise Refusal("overlay_invalid")
    return {"runtimes": rendered_runtimes, "hosts": sorted(set(hosts)), "profiles": sorted(set(profiles))}


def _canonical(document: Any) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def apply_overlay(tree: Tree, overlay: dict[str, Any] | None, *, has_manifest: bool, has_hooks: bool) -> dict[str, Any] | None:
    """Check the overlay against the tree and write it in; the checked overlay, or None."""
    if overlay is None:
        return None
    if OVERLAY_FILE in tree:
        raise Refusal("overlay_conflict", tree[OVERLAY_FILE].index)
    checked: dict[str, Any] = {}
    if "plugin" in overlay:
        if has_manifest:
            raise Refusal("overlay_replaces_manifest")
        checked["plugin"] = _plugin(overlay["plugin"])
    if "hooks" in overlay:
        if has_hooks:
            raise Refusal("overlay_replaces_hooks")
        checked["hooks"] = _hooks(tree, overlay["hooks"])
    checked["settings"] = _settings(overlay.get("settings", []))
    checked["requires"] = _requires(overlay.get("requires", {}))

    writes = {OVERLAY_FILE: checked}
    if "plugin" in checked:
        writes[MANIFEST] = checked["plugin"]
    if "hooks" in checked:
        writes[HOOKS_FILE] = checked["hooks"]
    for path, document in writes.items():
        for depth in range(1, len(path)):
            folder = tree.get(path[:depth])
            if folder is not None and folder.kind != DIR:
                raise Refusal("overlay_conflict", folder.index)
            tree.setdefault(path[:depth], Node(DIR))
        tree[path] = Node(FILE, _canonical(document))
    return checked
