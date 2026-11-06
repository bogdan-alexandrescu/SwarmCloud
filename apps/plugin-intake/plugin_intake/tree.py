"""From checked archive members to the plugin's tree, and the tree to one canonical tar.

§2.3 steps 4, 5, 7 and 8: pick the plugin root, prove every link stays inside
it, refuse dependencies that are not shipped, normalise modes, owners and times,
and write the runnable tree as one tar in a fixed order so the same input always
gives the same bytes and the same sha256.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass

from . import limits
from .archive import DIR, FILE, HARDLINK, SYMLINK, Entry, split_path
from .refusals import Refusal

#: A single top folder is the wrapper GitHub's tarballs add, unless it is one of
#: a plugin's own folders (an upload of a plugin that is only `skills/`).
_PLUGIN_FOLDERS = frozenset({".claude-plugin", "skills", "commands", "agents", "hooks"})

FILE_MODE = 0o444
EXEC_MODE = 0o555
DIR_MODE = 0o555
LINK_MODE = 0o777


@dataclass
class Node:
    kind: str
    data: bytes = b""
    executable: bool = False
    link: str = ""
    #: The archive index of the member this came from; None for implied folders
    #: and for files the overlay wrote.
    index: int | None = None


Tree = dict[tuple[str, ...], Node]


def plugin_tree(entries: list[Entry], subdir: str) -> Tree:
    """The members under the plugin root, re-rooted, links proven, hard links materialised."""
    for entry in entries:
        if not entry.path and entry.kind != DIR:
            raise Refusal("entry_path_invalid", entry.index)
    members = [e for e in entries if e.path]
    prefix = _wrapper(members)
    if subdir:
        try:
            prefix += split_path(subdir, None)
        except Refusal:
            raise Refusal("subdir_invalid") from None
    by_path = {e.path: e for e in members}
    tree: Tree = {}
    for entry in members:
        if entry.path[: len(prefix)] != prefix or len(entry.path) == len(prefix):
            continue
        rel = entry.path[len(prefix) :]
        if entry.kind == HARDLINK:
            target = by_path.get(entry.link)  # type: ignore[arg-type]
            inside = target is not None and target.path[: len(prefix)] == prefix
            if not inside or target.kind != FILE or target.index >= entry.index:
                raise Refusal("hardlink_escapes_root", entry.index)
            tree[rel] = Node(FILE, target.data, target.executable, index=entry.index)
        elif entry.kind == SYMLINK:
            tree[rel] = Node(SYMLINK, link=entry.link, index=entry.index)  # type: ignore[arg-type]
        elif entry.kind == FILE:
            tree[rel] = Node(FILE, entry.data, entry.executable, index=entry.index)
        else:
            tree[rel] = Node(DIR, index=entry.index)
    if not any(n.kind != DIR for n in tree.values()):
        raise Refusal("subdir_missing" if subdir else "archive_empty")
    if sum(len(n.data) for n in tree.values()) > limits.MAX_UNPACKED_BYTES:
        # Hard links materialised past the cap: counted from bytes the tree holds.
        raise Refusal("unpacked_too_large")
    add_implied_dirs(tree)
    _check_links(tree)
    return tree


def _wrapper(members: list[Entry]) -> tuple[str, ...]:
    tops = {e.path[0] for e in members}
    if len(tops) != 1:
        return ()
    (top,) = tops
    if top in _PLUGIN_FOLDERS:
        return ()
    if any(len(e.path) == 1 and e.kind != DIR for e in members):
        return ()
    return (top,)


def add_implied_dirs(tree: Tree) -> None:
    for path in list(tree):
        for depth in range(1, len(path)):
            tree.setdefault(path[:depth], Node(DIR))


def _check_links(tree: Tree) -> None:
    for path, node in sorted(tree.items()):
        for depth in range(1, len(path)):
            if tree[path[:depth]].kind == SYMLINK:
                raise Refusal("entry_under_link", node.index)
        if node.kind != SYMLINK:
            continue
        target = resolve_link(path, node.link, node.index)
        found = tree.get(target)
        if found is None:
            raise Refusal("link_target_missing", node.index)
        if found.kind == SYMLINK or any(tree[target[:d]].kind == SYMLINK for d in range(1, len(target))):
            raise Refusal("link_to_link", node.index)


def resolve_link(path: tuple[str, ...], target: str, index: int | None) -> tuple[str, ...]:
    """A link's target as a path from the plugin root, or `link_escapes_root`."""
    if "\x00" in target or "\\" in target:
        raise Refusal("entry_path_invalid", index)
    if not target or target.startswith("/"):
        raise Refusal("link_escapes_root", index)
    stack = list(path[:-1])
    for segment in target.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not stack:
                raise Refusal("link_escapes_root", index)
            stack.pop()
        else:
            stack.append(segment)
    if not stack:
        # The root itself: a loop for anything that walks the tree.
        raise Refusal("link_escapes_root", index)
    return tuple(stack)


def check_dependencies(tree: Tree) -> None:
    """§2.3 step 7: a package.json listing dependencies ships its node_modules beside it."""
    for path, node in sorted(tree.items()):
        if path[-1] != "package.json" or node.kind != FILE or "node_modules" in path[:-1]:
            continue
        if len(node.data) > limits.MAX_PARSED_FILE_BYTES:
            raise Refusal("package_json_invalid", node.index)
        try:
            document = json.loads(node.data)
        except (ValueError, UnicodeDecodeError):
            raise Refusal("package_json_invalid", node.index) from None
        if not isinstance(document, dict):
            raise Refusal("package_json_invalid", node.index)
        dependencies = document.get("dependencies")
        if dependencies is None:
            continue
        if not isinstance(dependencies, dict):
            raise Refusal("package_json_invalid", node.index)
        modules = tree.get(path[:-1] + ("node_modules",))
        if dependencies and (modules is None or modules.kind != DIR):
            raise Refusal("dependencies_not_shipped", node.index)


def canonical_tar(tree: Tree) -> bytes:
    """The runnable tree as one tar: paths sorted, fixed modes, owners and times zeroed."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8") as tar:
        for path in sorted(tree):
            node = tree[path]
            info = tarfile.TarInfo("/".join(path))
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if node.kind == DIR:
                info.type = tarfile.DIRTYPE
                info.mode = DIR_MODE
                tar.addfile(info)
            elif node.kind == SYMLINK:
                info.type = tarfile.SYMTYPE
                info.mode = LINK_MODE
                info.linkname = node.link
                tar.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.mode = EXEC_MODE if node.executable else FILE_MODE
                info.size = len(node.data)
                tar.addfile(info, io.BytesIO(node.data))
    return out.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
