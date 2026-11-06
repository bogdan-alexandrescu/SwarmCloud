"""The plugin's manifest and its components, parsed as data (§1.3, §2.3 step 6).

The intake parses and lists so neither the console nor submission ever reads
the archive. Every shape it does not know is refused, never passed through, so
no hook or server can load that the report did not list.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from . import limits
from .archive import DIR, FILE
from .refusals import Refusal
from .tree import Node, Tree

MANIFEST = (".claude-plugin", "plugin.json")
HOOKS_FILE = ("hooks", "hooks.json")
MCP_FILE = (".mcp.json",)

#: Metadata keys a plugin.json may carry beside its components.
_METADATA_KEYS = frozenset(
    {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords", "category", "tags"}
)
#: Component keys the intake reads; any other key is a shape it does not know.
_COMPONENT_KEYS = frozenset({"commands", "agents", "skills", "hooks", "mcpServers", "userConfig"})
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_USER_CONFIG_TYPES = frozenset({"string", "number", "boolean", "directory", "file"})
_USER_CONFIG_KEYS = frozenset(
    {"type", "title", "description", "default", "required", "sensitive", "multiple", "min", "max"}
)
#: Claude Code's hook events, as of CLI 2.1.283.
HOOK_EVENTS = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "UserPromptSubmit",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "SessionStart",
        "SessionEnd",
        "PermissionRequest",
    }
)
_MCP_KEYS = frozenset({"type", "command", "args", "env", "cwd", "url", "headers"})
_ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s\"'`;|&<>()$]+)")

#: §5's closed list of runtimes, and the commands that need each.
RUNTIMES = ("node", "bun", "python3", "bash")
_RUNTIME_OF = {
    "node": "node",
    "npx": "node",
    "bun": "bun",
    "bunx": "bun",
    "python": "python3",
    "python3": "python3",
    "uv": "python3",
    "uvx": "python3",
    "bash": "bash",
    "sh": "bash",
}


def read_json(node: Node | None, code: str) -> Any:
    if node is None or node.kind != FILE or len(node.data) > limits.MAX_PARSED_FILE_BYTES:
        raise Refusal(code, None if node is None else node.index)
    try:
        return json.loads(node.data)
    except (ValueError, UnicodeDecodeError):
        raise Refusal(code, node.index) from None


def _text(value: Any, code: str, index: int | None, *, limit: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise Refusal(code, index)
    return value


def _rel_path(tree: Tree, value: Any, index: int | None) -> tuple[str, ...]:
    """A './path' a manifest names, inside the plugin root and present."""
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise Refusal("manifest_component_unknown", index)
    if value.startswith("/"):
        raise Refusal("manifest_path_outside_root", index)
    path = []
    for segment in value.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise Refusal("manifest_path_outside_root", index)
        path.append(segment)
    if tuple(path) not in tree:
        raise Refusal("manifest_path_missing", index)
    return tuple(path)


def _paths(tree: Tree, value: Any, index: int | None) -> list[tuple[str, ...]]:
    values = value if isinstance(value, list) else [value]
    return [_rel_path(tree, v, index) for v in values]


def parse_manifest(tree: Tree) -> tuple[dict[str, Any], dict[str, Any]]:
    """plugin.json's metadata and userConfig, and the document itself for its component pointers."""
    node = tree.get(MANIFEST)
    document = read_json(node, "manifest_invalid")
    index = node.index if node else None
    if not isinstance(document, dict):
        raise Refusal("manifest_invalid", index)
    for key in document:
        if key not in _METADATA_KEYS and key not in _COMPONENT_KEYS:
            raise Refusal("manifest_component_unknown", index)
    name = document.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise Refusal("manifest_invalid", index)
    author = document.get("author")
    if isinstance(author, dict):
        author = _text(author.get("name"), "manifest_invalid", index, limit=256)
    else:
        author = _text(author, "manifest_invalid", index, limit=256)
    repository = document.get("repository")
    if isinstance(repository, dict):
        repository = repository.get("url")
    return {
        "name": name,
        "version": _text(document.get("version"), "manifest_invalid", index, limit=128),
        "description": _text(document.get("description"), "manifest_invalid", index),
        "author": author,
        "homepage": _text(document.get("homepage"), "manifest_invalid", index, limit=2048),
        "repository": _text(repository, "manifest_invalid", index, limit=2048),
        "license": _text(document.get("license"), "manifest_invalid", index, limit=128),
        "user_config": _user_config(document.get("userConfig"), index),
    }, document


def _user_config(value: Any, index: int | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise Refusal("manifest_user_config_invalid", index)
    fields = []
    for key in sorted(value):
        spec = value[key]
        if not _KEY.match(key) or not isinstance(spec, dict) or set(spec) - _USER_CONFIG_KEYS:
            raise Refusal("manifest_user_config_invalid", index)
        if spec.get("type") not in _USER_CONFIG_TYPES:
            raise Refusal("manifest_user_config_invalid", index)
        for flag in ("required", "sensitive", "multiple"):
            if flag in spec and not isinstance(spec[flag], bool):
                raise Refusal("manifest_user_config_invalid", index)
        sensitive = spec.get("sensitive", False)
        default = spec.get("default")
        if default is not None and not isinstance(default, (str, int, float, bool, list)):
            raise Refusal("manifest_user_config_invalid", index)
        fields.append(
            {
                "key": key,
                "type": spec["type"],
                "title": _text(spec.get("title"), "manifest_user_config_invalid", index, limit=256),
                "description": _text(spec.get("description"), "manifest_user_config_invalid", index),
                "required": spec.get("required", False),
                "sensitive": sensitive,
                # A sensitive field's default is never copied out of the archive.
                "default": None if sensitive else default,
                "multiple": spec.get("multiple", False),
            }
        )
    return fields


def _check_root_refs(tree: Tree, text: str, code: str, index: int | None) -> None:
    for match in _ROOT_REF.finditer(text):
        path = tuple(s for s in match.group(1).split("/") if s not in ("", "."))
        if ".." in path or path not in tree:
            raise Refusal(code, index)


def parse_hooks(tree: Tree, document: Any, index: int | None) -> list[dict[str, Any]]:
    """hooks.json's shape: {"hooks": {Event: [{"matcher", "hooks": [{"type": "command", ...}]}]}}."""
    if not isinstance(document, dict) or set(document) - {"hooks", "description"}:
        raise Refusal("hooks_invalid", index)
    events = document.get("hooks")
    if not isinstance(events, dict):
        raise Refusal("hooks_invalid", index)
    hooks = []
    for event in sorted(events):
        if event not in HOOK_EVENTS:
            raise Refusal("hook_unknown", index)
        groups = events[event]
        if not isinstance(groups, list):
            raise Refusal("hooks_invalid", index)
        for group in groups:
            if not isinstance(group, dict) or set(group) - {"matcher", "hooks"}:
                raise Refusal("hooks_invalid", index)
            matcher = _text(group.get("matcher"), "hooks_invalid", index, limit=1024)
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise Refusal("hooks_invalid", index)
            for handler in handlers:
                if not isinstance(handler, dict) or set(handler) - {"type", "command", "timeout"}:
                    raise Refusal("hooks_invalid", index)
                if handler.get("type") != "command":
                    raise Refusal("hook_unknown", index)
                command = handler.get("command")
                if not isinstance(command, str) or not command.strip() or len(command) > 4096:
                    raise Refusal("hooks_invalid", index)
                timeout = handler.get("timeout")
                if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
                    raise Refusal("hooks_invalid", index)
                _check_root_refs(tree, command, "hook_path_missing", index)
                hooks.append({"event": event, "matcher": matcher, "type": "command", "command": command, "timeout": timeout})
    return hooks


def parse_mcp(tree: Tree, document: Any, index: int | None) -> list[dict[str, Any]]:
    """.mcp.json: {"mcpServers": {name: server}} or the map itself. Environment keys, never values."""
    if not isinstance(document, dict):
        raise Refusal("mcp_invalid", index)
    servers = document["mcpServers"] if set(document) == {"mcpServers"} else document
    if not isinstance(servers, dict):
        raise Refusal("mcp_invalid", index)
    listed = []
    for name in sorted(servers):
        spec = servers[name]
        if not _NAME.match(name) or not isinstance(spec, dict) or set(spec) - _MCP_KEYS:
            raise Refusal("mcp_invalid", index)
        transport = spec.get("type", "stdio" if "command" in spec else None)
        args = spec.get("args", [])
        env = spec.get("env", {})
        headers = spec.get("headers", {})
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise Refusal("mcp_invalid", index)
        if not isinstance(env, dict) or not isinstance(headers, dict):
            raise Refusal("mcp_invalid", index)
        entry: dict[str, Any] = {
            "name": name,
            "transport": transport,
            "command": None,
            "args": args,
            "env_keys": sorted(env),
            "header_keys": sorted(headers),
            "url_host": None,
        }
        if transport == "stdio":
            command = spec.get("command")
            if not isinstance(command, str) or not command.strip() or "url" in spec:
                raise Refusal("mcp_invalid", index)
            entry["command"] = command
            for text in [command, *args]:
                _check_root_refs(tree, text, "mcp_path_missing", index)
        elif transport in ("http", "sse"):
            url = spec.get("url")
            if not isinstance(url, str) or "command" in spec:
                raise Refusal("mcp_invalid", index)
            parts = urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.hostname:
                raise Refusal("mcp_invalid", index)
            entry["url_host"] = parts.hostname
        else:
            raise Refusal("mcp_invalid", index)
        listed.append(entry)
    return listed


def _frontmatter(node: Node) -> dict[str, str]:
    """`name:` and `description:` from a Markdown file's leading --- block."""
    if len(node.data) > limits.MAX_PARSED_FILE_BYTES:
        raise Refusal("skill_invalid", node.index)
    try:
        text = node.data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("skill_invalid", node.index) from None
    lines = text.splitlines()
    found: dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return found
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("name", "description") and not line.startswith((" ", "\t")):
            found[key.strip()] = value.strip().strip("'\"")[:1024]
    return found


def _markdown_under(tree: Tree, folder: tuple[str, ...]) -> list[tuple[tuple[str, ...], Node]]:
    return sorted(
        (path[len(folder) :], node)
        for path, node in tree.items()
        if path[: len(folder)] == folder and len(path) > len(folder) and node.kind == FILE and path[-1].endswith(".md")
    )


def list_components(tree: Tree, document: dict[str, Any] | None, manifest_index: int | None) -> dict[str, Any]:
    """Skills, commands, agents, hooks and MCP servers: the default folders plus what the manifest points at."""
    document = document or {}
    skill_roots = [("skills",)]
    command_roots = [("commands",)]
    agent_roots = [("agents",)]
    if "skills" in document:
        skill_roots += _paths(tree, document["skills"], manifest_index)
    if "commands" in document:
        command_roots += _paths(tree, document["commands"], manifest_index)
    if "agents" in document:
        agent_roots += _paths(tree, document["agents"], manifest_index)

    skills = []
    for root in dict.fromkeys(skill_roots):
        candidates = [root] if (root + ("SKILL.md",)) in tree else sorted(
            p for p, n in tree.items() if len(p) == len(root) + 1 and p[: len(root)] == root and n.kind == DIR
        )
        for folder in candidates:
            node = tree.get(folder + ("SKILL.md",))
            if node is None or node.kind != FILE:
                continue
            front = _frontmatter(node)
            skills.append({"name": front.get("name", folder[-1]), "description": front.get("description"), "path": "/".join(folder)})

    def markdown(roots: list[tuple[str, ...]]) -> list[dict[str, Any]]:
        listed = []
        for root in dict.fromkeys(roots):
            node = tree.get(root)
            if node is not None and node.kind == FILE:
                files = [((root[-1],), root, node)]
            else:
                files = [(rel, root + rel, md) for rel, md in _markdown_under(tree, root)]
            for rel, full, md in files:
                front = _frontmatter(md)
                default = ":".join(rel)
                if default.endswith(".md"):
                    default = default[: -len(".md")]
                listed.append({"name": front.get("name", default), "description": front.get("description"), "path": "/".join(full)})
        return listed

    hooks: list[dict[str, Any]] = []
    hooks_node = tree.get(HOOKS_FILE)
    if hooks_node is not None:
        hooks += parse_hooks(tree, read_json(hooks_node, "hooks_invalid"), hooks_node.index)
    declared = document.get("hooks")
    if isinstance(declared, dict):
        hooks += parse_hooks(tree, declared, manifest_index)
    elif declared is not None:
        for path in _paths(tree, declared, manifest_index):
            if path == HOOKS_FILE:
                continue
            node = tree[path]
            hooks += parse_hooks(tree, read_json(node, "hooks_invalid"), node.index)

    servers: list[dict[str, Any]] = []
    mcp_node = tree.get(MCP_FILE)
    if mcp_node is not None:
        servers += parse_mcp(tree, read_json(mcp_node, "mcp_invalid"), mcp_node.index)
    declared = document.get("mcpServers")
    if isinstance(declared, dict):
        servers += parse_mcp(tree, declared, manifest_index)
    elif declared is not None:
        for path in _paths(tree, declared, manifest_index):
            if path == MCP_FILE:
                continue
            node = tree[path]
            servers += parse_mcp(tree, read_json(node, "mcp_invalid"), node.index)

    return {
        "skills": skills,
        "commands": markdown(command_roots),
        "agents": markdown(agent_roots),
        "hooks": hooks,
        "mcp_servers": servers,
    }


def has_own_hooks(tree: Tree, document: dict[str, Any] | None) -> bool:
    return HOOKS_FILE in tree or bool(document and document.get("hooks"))


def suggest_requires(tree: Tree, components: dict[str, Any]) -> dict[str, Any]:
    """What the tree suggests it needs (§5): runtimes from commands and package.json, hosts from MCP URLs."""
    runtimes: dict[str, str | None] = {}
    commands = [h["command"] for h in components["hooks"]] + [s["command"] for s in components["mcp_servers"] if s["command"]]
    for command in commands:
        first = command.strip().split()[0].rsplit("/", 1)[-1]
        runtime = _RUNTIME_OF.get(first)
        if runtime is None and first.endswith((".py",)):
            runtime = "python3"
        if runtime is None and first.endswith((".sh",)):
            runtime = "bash"
        if runtime is not None:
            runtimes.setdefault(runtime, None)
    package = tree.get(("package.json",))
    if package is not None and package.kind == FILE:
        document = read_json(package, "package_json_invalid")
        engines = document.get("engines") if isinstance(document, dict) else None
        node_range = engines.get("node") if isinstance(engines, dict) else None
        if isinstance(node_range, str):
            match = re.search(r"(\d+(?:\.\d+){0,2})", node_range)
            runtimes["node"] = match.group(1) if match else None
    hosts = sorted({s["url_host"] for s in components["mcp_servers"] if s["url_host"]})
    return {
        "runtimes": [{"name": name, "min_version": runtimes[name]} for name in RUNTIMES if name in runtimes],
        "hosts": hosts,
    }
