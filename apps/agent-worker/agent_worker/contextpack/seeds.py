"""The seeds of a step's pack: the files, tests and symbols it starts from (§4.5).

Each task type reads the platform's own record of what the step is about:

| type | seeds |
|---|---|
| implement, test, design | the plan step's "Files this step is planned to touch" and "Tests this step adds" lines (`issueruns._step_detail`), else paths the brief names; with neither, BM25 over the brief |
| review | the changed symbols of the staged `swarm-work.patch`: the symbols whose lines a hunk touches |
| fix | a CI round's red tests, from the FAILING CHECKS excerpt; a review fix's finding files, from the staged `verdict.json` |
| plan | the paths the issue file and the brief name, and BM25 over the issue's text |

A path counts only when the snapshot or the checkout has it: a seed the graph
does not know is said in the pack's header (`unknown`), never invented.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..graphmcp.snapshot import Snapshot, SnapshotError
from ..graphmcp.tools import _Unresolved, resolve
from .tasktype import FAILING_CHECKS, PATCH_FILE, VERDICT_FILE

#: Seeds past these are not followed: a brief naming forty files is a sweep,
#: and the pack's budget is better spent on the first ones' neighbourhood.
MAX_SEED_FILES = 12
MAX_SEED_SYMBOLS = 12
#: The BM25 hits a brief without paths is seeded with.
SEARCH_SEEDS = 5
#: The brief's text the search reads, as J0/J1 cap it (§5.2).
BRIEF_CHARS = 4000
#: A staged patch or verdict larger than this is read only up to it.
MAX_STAGED_BYTES = 2 * 1024 * 1024
ISSUE_FILE = "issue.md"

_PLANNED_FILES = re.compile(r"^Files this step is planned to touch: (.+)$", re.M)
_PLANNED_TESTS = re.compile(r"^Tests this step adds -- write them first:\n((?:- .+\n?)+)",
                            re.M)
_PATH = re.compile(r"(?<![\w/.@-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,6})(?![\w/-])")
_BACKTICKED = re.compile(r"`([A-Za-z_][\w.]*(?:#[\w.]+)?)\(?\)?`")
_RED_TEST = re.compile(r"((?:[\w.-]+/)*[\w.-]*test[\w.-]*\.(?:py|ts|tsx|js|mjs|go))", re.I)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Seeds:
    files: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    #: The brief's text, for BM25 (layer S terms) and the design pipeline.
    brief: str = ""
    #: path -> the new-side lines a reviewed diff touched.
    changed_lines: dict[str, set[int]] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)
    #: True when the symbols came from BM25 over the brief, not from a name.
    searched: bool = False
    #: symbol -> its BM25 score against the brief, for the ranking's w_t term.
    bm25: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"files": list(self.files), "tests": list(self.tests),
                               "symbols": list(self.symbols)}
        if self.searched:
            out["from"] = "the brief's terms (BM25)"
        if self.unknown:
            out["unknown"] = list(self.unknown)
        return out

    def paths(self) -> list[str]:
        """Every path a seed lives in, in order, each once."""
        out: list[str] = []
        for path in [*self.files, *self.tests, *(s.split("#", 1)[0] for s in self.symbols)]:
            if path not in out:
                out.append(path)
        return out


def _read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    with path.open("rb") as handle:
        return handle.read(MAX_STAGED_BYTES).decode("utf-8", "replace")


def _known(snap: Snapshot, checkout: Path, path: str) -> bool:
    clean = posixpath.normpath(path.strip())
    if clean.startswith(("/", "../")) or clean == "..":
        return False
    if snap.file(clean) is not None:
        return True
    candidate = checkout / clean
    try:
        return (candidate.is_file() and not candidate.is_symlink()
                and candidate.resolve().is_relative_to(checkout.resolve()))
    except OSError:
        return False


def _add(out: list[str], value: str, limit: int) -> None:
    if value and value not in out and len(out) < limit:
        out.append(value)


def _paths_in(text: str, snap: Snapshot, checkout: Path, seeds: Seeds) -> list[str]:
    found: list[str] = []
    for match in _PATH.finditer(text):
        raw = match.group(1).rstrip(".")
        if "/" not in raw and "." not in raw:
            continue
        path = posixpath.normpath(raw[2:] if raw.startswith("./") else raw)
        if _known(snap, checkout, path):
            _add(found, path, MAX_SEED_FILES)
    return found


def _symbols_named(text: str, snap: Snapshot) -> list[str]:
    out: list[str] = []
    for match in _BACKTICKED.finditer(text):
        try:
            _add(out, resolve(snap, match.group(1)), MAX_SEED_SYMBOLS)
        except (_Unresolved, SnapshotError):
            continue
    return out


def _search(snap: Snapshot, text: str, seeds: Seeds) -> None:
    if seeds.files or seeds.symbols or not text.strip():
        return
    try:
        hits = snap.search(text[:BRIEF_CHARS], SEARCH_SEEDS)
    except SnapshotError:
        return
    for symbol, score in hits:
        _add(seeds.symbols, symbol, MAX_SEED_SYMBOLS)
        seeds.bm25[symbol] = float(score)
    seeds.searched = bool(seeds.symbols)


def _planned(prompt: str, snap: Snapshot, checkout: Path, seeds: Seeds) -> None:
    """The plan step's own lines, as `issueruns._step_detail` writes them."""
    for match in _PLANNED_FILES.finditer(prompt):
        for raw in match.group(1).split(","):
            path = posixpath.normpath(raw.strip())
            if not path or path == ".":
                continue
            if _known(snap, checkout, path):
                _add(seeds.files, path, MAX_SEED_FILES)
            else:
                _add(seeds.unknown, path, MAX_SEED_FILES)
    for match in _PLANNED_TESTS.finditer(prompt):
        for line in match.group(1).splitlines():
            test = line[2:].strip()
            path = test.split("::", 1)[0]
            if _known(snap, checkout, path):
                _add(seeds.tests, path, MAX_SEED_FILES)


def _patch(text: str, snap: Snapshot, seeds: Seeds) -> None:
    """Changed files and the new-side lines a unified diff touches."""
    current: str | None = None
    line = 0
    for row in text.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip()
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                _add(seeds.files, current, MAX_SEED_FILES)
                seeds.changed_lines.setdefault(current, set())
            continue
        hunk = _HUNK.match(row)
        if hunk:
            line = int(hunk.group(1))
            continue
        if current is None or row.startswith(("--- ", "diff --git", "index ")):
            continue
        if row.startswith("+"):
            seeds.changed_lines[current].add(line)
            line += 1
        elif row.startswith("-"):
            seeds.changed_lines[current].add(line)
        elif row.startswith(" "):
            line += 1
    for path, lines in seeds.changed_lines.items():
        for symbol in snap.symbols_in(path):
            if symbol.get("kind") == "local":
                continue
            start, end = symbol.get("start_line") or 0, symbol.get("end_line") or 0
            if any(start <= n <= end for n in lines):
                _add(seeds.symbols, str(symbol.get("id")), MAX_SEED_SYMBOLS)


def _verdict_files(text: str) -> list[str]:
    try:
        verdict = json.loads(text)
    except ValueError:
        return []
    files: list[str] = []
    for finding in (verdict.get("findings") if isinstance(verdict, dict) else None) or []:
        if isinstance(finding, dict) and isinstance(finding.get("file"), str):
            _add(files, posixpath.normpath(finding["file"]), MAX_SEED_FILES)
    return files


def _staged_file(payload: Mapping[str, Any], work_dir: Path, name: str) -> Path:
    """`work/<path>` of the staged input called `name`, else `work/<name>`."""
    for entry in payload.get("staged_inputs") or ():
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(raw, str) and posixpath.basename(raw) == name:
            clean = posixpath.normpath(raw)
            if not clean.startswith(("/", "../")):
                return work_dir / clean
    return work_dir / name


def seeds_for(task_type: str, payload: Mapping[str, Any], *, snap: Snapshot,
              checkout: Path, work_dir: Path) -> Seeds:
    prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else ""
    seeds = Seeds(brief=prompt[:BRIEF_CHARS])
    if task_type == "review":
        _patch(_read_text(_staged_file(payload, work_dir, PATCH_FILE)), snap, seeds)
    elif task_type == "fix":
        fence = FAILING_CHECKS.search(prompt)
        if fence:
            excerpt = prompt[fence.end():]
            for match in _RED_TEST.finditer(excerpt):
                path = posixpath.normpath(match.group(1))
                if _known(snap, checkout, path):
                    _add(seeds.tests, path, MAX_SEED_FILES)
        for path in _verdict_files(_read_text(_staged_file(payload, work_dir, VERDICT_FILE))):
            if _known(snap, checkout, path):
                _add(seeds.files, path, MAX_SEED_FILES)
            else:
                _add(seeds.unknown, path, MAX_SEED_FILES)
    elif task_type == "plan":
        issue = _read_text(work_dir / ISSUE_FILE)
        seeds.brief = (issue + "\n" + prompt)[:BRIEF_CHARS]
        for path in _paths_in(issue + "\n" + prompt, snap, checkout, seeds):
            _add(seeds.files, path, MAX_SEED_FILES)
        for symbol in _symbols_named(issue, snap):
            _add(seeds.symbols, symbol, MAX_SEED_SYMBOLS)
    else:
        _planned(prompt, snap, checkout, seeds)
        if not seeds.files:
            for path in _paths_in(prompt, snap, checkout, seeds):
                if path not in seeds.tests:
                    _add(seeds.files, path, MAX_SEED_FILES)
        for symbol in _symbols_named(prompt, snap):
            _add(seeds.symbols, symbol, MAX_SEED_SYMBOLS)
    if task_type in ("implement", "test", "design", "plan"):
        _search(snap, seeds.brief, seeds)
    return seeds
