#!/usr/bin/env python3
"""Layers H (history) and R (rules) of the repository graph (lane CTXQ-H).

docs/design/context-quality.md §4.2. The graph's symbols, edges and test map
answer "what does this code touch". They do not answer the questions behind
most red lane PRs measured in §2: which guard polices a change of this kind
(class G), which test the change needs that no import reaches (T), which path
the lane cannot push (W), and how this same test was fixed last time (L).
This module answers those from what the index run already holds: the clone,
with its 90-day history (`agent_worker.indexrun.HISTORY_DAYS`), and the graph's
`files` list. `repo_graph_shards.py` stores what it returns as `context_shards`
beside the other shards.

THE LAYERS (`build_context(...)["layers"]`):

  history      per file, the merged PRs that touched it in the window, newest
               first: number, title, the files, `fix` and `fix_round`
  co_change    a file and the tests or guards that changed with it in at least
               CO_CHANGE_MIN_COMMITS commits, with the Jaccard support
  test_fixes   per test, the earlier commits that FIXED it: a fix round, a fix
               subject, or a change to tests alone. The second fixer of a time
               bomb reads the first fix here (§3.2 point 5)
  rules        per path, the docs, house rules (CLAUDE.md, AGENTS.md) and
               guards that police it: by path mention, by a guard's
               co-change, or by the guard set a repository declares
  cannot_push  path patterns no lane can push: `.github/workflows/**` always,
               and the paths a house rule marks frozen
  test_runners per JS/TS test file, the command that runs it

WHAT `fix_round` MEANS. True when the commit's body has a `## Continuation`
section (the worker appends one per fix round) or lists squashed commit
subjects (GitHub's squash body, `* subject` lines) one of which is a fix.
False when it lists squashed subjects and none is. None when it lists none:
a SwarmCloud merge body ("Merged by SwarmCloud task ...") names no commits,
and "no fix round" is not something that body can say. Unknown is never
written as false.

WHICH COMMITS ARE READ. `git log --first-parent` from the indexed commit, over
HISTORY_DAYS before it, with each merge diffed against its first parent: a
squash-merge history is one commit per PR, and a merge-commit history reads
each PR as its merge. A commit's PR number is the `(#N)` its squash subject
ends with, or the `#N` of "Merge pull request #N"; a commit with neither is
not a merged PR and is not in `history`, though it still counts toward
co-change and `test_fixes`. A root commit, or a shallow clone's boundary
commit, diffs against nothing, so it would read as "every file changed
together": it is counted (`root_commits`, `boundary_commits`) and never
paired. A commit touching more than CO_CHANGE_MAX_FILES files (a mass rename,
a reformat) is in `history` but pairs nothing, as in the extractor.

WHAT A GUARD IS. A test file (the graph's `files[].test`, or one under a test
directory) that the repository declares in a guard list (a `GUARD_TESTS=(`
array in any tracked shell script, the shape of scripts/changed-guards.sh),
or whose name says `guard`, or whose leading docstring or comment calls it
one. The refusal guard (`test_new_refusals_ship_off.py`) is the third kind:
it discovers codes by scanning the package, so its text never names
`refusals.py`, and only its history links the two -- which is why `rules`
reads co-change, not only mentions.

WHAT A PATH MENTION IS. In a markdown file, or a guard's leading docstring: a
backticked span or a bare token that is a tracked file, a tracked directory
(with or without its trailing `/`), a dotted Python module that ends exactly
one tracked `.py` file (`swarm_api.refusals`), or a basename only one tracked
file has. A token that names nothing tracked is not a rule: a doc naming a
file that was never written says nothing about any file that was.

BOUNDS. Every layer has a row cap and each path at most MAX_RULES_PER_PATH
rules, ordered house rule, guard, doc, then by confidence; a cut is named in
`meta.truncated`, never silent. The shard writer drops `history`, `co_change`,
`test_fixes` and `test_runners` first and `rules` next when a commit's shards
would pass the 256 MiB ceiling, and keeps `cannot_push` always (it is a
handful of rows and the miss it prevents costs a whole lane).

Deterministic: the same clone and commit give the same rows in the same order,
so the manifest digest promotion records does not move on a rerun.

Standard library only, like the writer that imports it: it runs under the
image's python3.11 outside the extractor's environment. It reads the clone
and runs `git`; it writes nothing and reaches no network.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: The layers, in the order the writer stores them.
LAYERS = ("history", "co_change", "test_fixes", "rules", "cannot_push", "test_runners")
#: The layers the writer drops first over the ceiling, then the next group.
#: `cannot_push` is never dropped.
DROP_FIRST = ("history", "co_change", "test_fixes", "test_runners")
DROP_NEXT = ("rules",)

#: The window: the index run's clone holds this much history
#: (agent_worker.indexrun.HISTORY_DAYS; repo_index_extract.HISTORY_DAYS).
HISTORY_DAYS = 90
#: Lower than the extractor's 5 (`CO_CHANGE_MIN_COMMITS`, which turns a pair
#: into a test EDGE a gate may select by): here a pair is a row an agent reads,
#: and a guard missed costs a red CI round (§2.2), a row read costs ~40 tokens.
CO_CHANGE_MIN_COMMITS = 3
#: Same as the extractor: a commit touching more says nothing about pairs.
CO_CHANGE_MAX_FILES = 50
#: Per file: the newest PRs kept in `history`, and the files listed per PR.
PRS_PER_FILE = 10
FILES_PER_CHANGE = 20
#: Per test: the newest fixes kept.
FIXES_PER_TEST = 5
#: Per path: the rules kept, strongest first.
MAX_RULES_PER_PATH = 20
#: Row caps per layer, far above this repository's (about 2,000 files).
MAX_ROWS = {"history": 20_000, "co_change": 50_000, "test_fixes": 20_000,
            "rules": 50_000, "cannot_push": 200, "test_runners": 20_000}
#: A markdown file or guard larger than this is not read for mentions.
MAX_TEXT_BYTES = 1024 * 1024
#: `git log` may take this long on the window's commits.
GIT_TIMEOUT = 600.0

#: The forge credential cannot push workflow files (GitHub refuses a token
#: without the `workflows` permission), so a lane that edits one fails at
#: publish. swarm_api.issueruns.WORKFLOWS_PATTERN refuses such a plan.
WORKFLOWS_PATTERN = ".github/workflows/**"
WORKFLOWS_WHY = ("the forge credential cannot push workflow files; a change here "
                 "is a request in the PR body, not a commit")

HOUSE_RULE_FILES = frozenset({"CLAUDE.md", "AGENTS.md"})
DOC_SUFFIXES = (".md", ".mdx")
TEST_DIRECTORY_NAMES = frozenset({"tests", "test", "__tests__", "testdata"})
#: Basenames many files share: a mention of one names no file in particular.
GENERIC_BASENAMES = frozenset({
    "__init__.py", "conftest.py", "README.md", "main.py", "app.py", "config.py",
    "models.py", "utils.py", "index.ts", "index.tsx", "index.js", "types.ts",
    "package.json", "pyproject.toml", "Makefile", "Dockerfile", "setup.py",
})

#: Rule kinds, strongest first: a house rule binds every lane, a guard fails
#: CI, a doc explains.
KIND_RANK = {"house_rule": 0, "guard": 1, "doc": 2}
#: A mention's confidence by how it resolved.
CONFIDENCE = {"file": 0.9, "dir": 0.7, "module": 0.6, "basename": 0.5, "declared": 1.0}

_PR_SUFFIX = re.compile(r"\s*\(#(\d+)\)\s*$")
_PR_MERGE = re.compile(r"^Merge pull request #(\d+)\b")
_FIX = re.compile(r"\bfix(?:es|ed|up)?\b|\bfix round\b", re.IGNORECASE)
_SQUASHED = re.compile(r"^\*\s+(\S.*)$")
_CONTINUATION = re.compile(r"^##\s+Continuation\b", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_BACKTICK = re.compile(r"`([^`\n]{1,300})`")
_BARE = re.compile(r"(?<![\w./`-])((?:[\w.-]+/)+[\w.-]*|[\w-]+(?:\.[\w-]+)+)")
_DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
#: A house-rule line that freezes the path it names: "`x/` is FROZEN", "never
#: edit `y`". Not the bare word: "a frozen-contract change is also an entry in
#: `docs/contract-change-requests.md`" names a doc every lane must edit.
_FROZEN = re.compile(r"\b(?:is|are)\s+frozen\b|\b(?:never|do not|don't|must not)\s+"
                     r"(?:edit|modify|touch)\b", re.IGNORECASE)
_GUARD_WORD = re.compile(r"\bguard\b", re.IGNORECASE)
_GUARD_ARRAY = re.compile(r"\bGUARD_TESTS=\(([^)]*)\)", re.DOTALL)
_JS_TEST = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_JS_SOURCE = re.compile(r"\.[cm]?[jt]sx?$")
_VITEST_CONFIGS = ("vitest.config.ts", "vitest.config.mts", "vitest.config.js",
                   "vitest.config.mjs", "vite.config.ts", "vite.config.mts", "vite.config.js",
                   "vite.config.mjs")
_INCLUDE = re.compile(r"\binclude\s*:\s*\[([^\]]*)\]", re.DOTALL)
_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
#: vitest's default `include`.
VITEST_DEFAULT = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")


# --- the tracked tree ----------------------------------------------------------

class Tree:
    """The graph's `files`, with what a mention resolves against."""

    def __init__(self, root: Path, files: Iterable[dict]) -> None:
        self.root = root
        self.tests: set[str] = set()
        paths: list[str] = []
        for row in files:
            path = str(row.get("path") or "")
            if not path:
                continue
            paths.append(path)
            if row.get("test"):
                self.tests.add(path)
        self.files = sorted(set(paths))
        self.file_set = set(self.files)
        dirs: set[str] = set()
        for path in self.files:
            parent = posixpath.dirname(path)
            while parent:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
        self.dirs = dirs
        self.by_basename: dict[str, list[str]] = {}
        for path in self.files:
            self.by_basename.setdefault(posixpath.basename(path), []).append(path)
        # Every dotted name a `.py` file is importable as, from its last two
        # parts up: `swarm_api.refusals` -> apps/swarm-api/swarm_api/refusals.py.
        self.modules: dict[str, list[str]] = {}
        for path in self.files:
            if path.endswith(".py"):
                parts = path[:-3].split("/")
                for start in range(len(parts) - 2, -1, -1):
                    self.modules.setdefault(".".join(parts[start:]), []).append(path)

    def test_side(self, path: str) -> bool:
        return path in self.tests or any(
            part in TEST_DIRECTORY_NAMES for part in path.split("/")[:-1])

    def text(self, path: str) -> str | None:
        full = self.root / path
        try:
            if full.is_symlink() or not full.is_file() or full.stat().st_size > MAX_TEXT_BYTES:
                return None
            return full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def resolve(self, token: str, near: str) -> tuple[str, str, str] | None:
        """(path, target, how) for a mention, or None when it names nothing tracked."""
        token = token.strip().strip("'\"()[]{}<>,;").rstrip(".:")
        token = re.sub(r":\d+(?:-\d+)?$", "", token)  # path:line
        token = re.sub(r"^\./", "", token)
        if not token or len(token) > 300 or token.startswith(("http:", "https:", "/")):
            return None
        bare = token.rstrip("/")
        if bare in self.file_set and not token.endswith("/"):
            return bare, "file", "file"
        if bare in self.dirs:
            return bare, "dir", "dir"
        # Relative to the mentioning file's own directory.
        here = posixpath.normpath(posixpath.join(posixpath.dirname(near), bare))
        if here in self.file_set and "/" in bare:
            return here, "file", "file"
        if _DOTTED.match(token) and token not in self.by_basename:
            # The module itself, or a symbol in it (`swarm_api.refusals.SWITCHES`).
            parts = token.split(".")
            for name in (token, ".".join(parts[:-1]) if len(parts) > 2 else ""):
                hits = self.modules.get(name) or []
                if len(hits) == 1:
                    return hits[0], "file", "module"
            return None
        if "/" not in bare and bare not in GENERIC_BASENAMES:
            hits = self.by_basename.get(bare) or []
            if len(hits) == 1 and "." in bare:
                return hits[0], "file", "basename"
        return None


# --- git ----------------------------------------------------------------------

def _git(root: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                          capture_output=True, timeout=timeout, env=env, check=False)


def _shallow_boundary(root: Path) -> set[str]:
    done = _git(root, "rev-parse", "--git-path", "shallow")
    if done.returncode != 0:
        return set()
    path = Path(done.stdout.decode().strip())
    if not path.is_absolute():
        path = root / path
    try:
        return {line.strip() for line in path.read_text().splitlines() if line.strip()}
    except OSError:
        return set()


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr_of(subject: str) -> int | None:
    for pattern in (_PR_SUFFIX, _PR_MERGE):
        found = pattern.search(subject)
        if found:
            return int(found.group(1))
    return None


def _title_of(subject: str, body: str) -> str:
    if _PR_MERGE.search(subject):
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        return first or subject
    return _PR_SUFFIX.sub("", subject)


def _fix_round(body: str) -> tuple[bool | None, list[str]]:
    """(whether a fix round ran, the fix subjects the body lists). See the module doc."""
    squashed = [m.group(1).strip() for line in body.splitlines()
                if (m := _SQUASHED.match(line.strip()))]
    fixes = [s for s in squashed if _FIX.search(s)]
    if _CONTINUATION.search(body):
        return True, fixes
    if not squashed:
        return None, []
    return bool(fixes), fixes


def _commits(root: Path, commit_sha: str, meta: dict) -> list[dict]:
    """The window's first-parent commits, newest first, each with its touched files."""
    done = _git(root, "show", "-s", "--format=%ct", commit_sha)
    if done.returncode != 0:
        meta["reason"] = "git could not read the indexed commit"
        return []
    end = int(done.stdout.decode().strip() or 0)
    start = end - HISTORY_DAYS * 86_400
    since = _iso(start)
    log = _git(root, "log", "--first-parent", "--diff-merges=first-parent", "--no-renames",
               "--name-status", "--format=%x1e%H%x1f%ct%x1f%P%x1f%s%x1f%b%x1f",
               f"--since={since}", commit_sha, timeout=GIT_TIMEOUT)
    if log.returncode != 0:
        meta["reason"] = "git log failed"
        return []
    boundary = _shallow_boundary(root)
    meta.update(available=True, reason=None, window_start=since, window_end=_iso(end))
    out: list[dict] = []
    for record in log.stdout.decode("utf-8", errors="replace").split("\x1e"):
        if not record.strip():
            continue
        fields = record.split("\x1f")
        if len(fields) < 6 or not fields[1].isdigit() or int(fields[1]) < start:
            continue
        sha, stamp, parents, subject, body, status = fields[:6]
        # A shallow clone's boundary shows no parents too: checked first, so
        # it is counted as what it is.
        if sha in boundary:
            meta["boundary_commits"] += 1
            continue
        if not parents.strip():
            meta["root_commits"] += 1
            continue
        touched: dict[str, str] = {}
        for line in status.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0]:
                touched[parts[-1]] = parts[0][0]
        meta["commits"] += 1
        out.append({"sha": sha, "time": int(stamp), "subject": subject.strip(),
                    "body": body.strip(), "touched": touched})
    if not out and meta["boundary_commits"]:
        # As the extractor says it (G4-06): one commit deep is no history,
        # never "nothing changed".
        meta.update(available=False,
                    reason="the checkout is shallow and holds no commit inside the "
                           f"{HISTORY_DAYS}-day window but the one it stops at; history "
                           "and co-change are not known")
    return out


# --- H: history -----------------------------------------------------------------

def _history_layers(tree: Tree, commits: list[dict], guards: set[str], meta: dict
                    ) -> tuple[list[dict], list[dict], list[dict], dict[tuple[str, str], dict]]:
    """(history, co_change, test_fixes, guard pairs by (path, guard))."""
    per_file: dict[str, list[dict]] = {}
    fixes: dict[str, list[dict]] = {}
    changes: dict[str, int] = {}
    pairs: dict[tuple[str, str], int] = {}
    for commit in commits:  # newest first
        touched = {p: s for p, s in commit["touched"].items() if p in tree.file_set}
        paths = sorted(touched)
        fix_round, fix_subjects = _fix_round(commit["body"])
        fix_subject = bool(_FIX.search(commit["subject"]))
        pr = _pr_of(commit["subject"])
        date = _iso(commit["time"])
        if pr is not None:
            change = {"pr": pr, "sha": commit["sha"],
                      "title": _title_of(commit["subject"], commit["body"]), "date": date,
                      "fix": bool(fix_round) or fix_subject, "fix_round": fix_round,
                      "fix_subjects": fix_subjects[:FILES_PER_CHANGE],
                      "files": paths[:FILES_PER_CHANGE], "files_total": len(paths)}
            for path in paths:
                listed = per_file.setdefault(path, [])
                if len(listed) < PRS_PER_FILE:
                    listed.append(change)
        test_only = bool(paths) and all(tree.test_side(p) for p in paths)
        why = ("fix round" if fix_round else "fix subject" if fix_subject
               else "test-only change" if test_only else None)
        if why is not None:
            for path in paths:
                if path in tree.tests and touched[path] == "M":
                    listed = fixes.setdefault(path, [])
                    if len(listed) < FIXES_PER_TEST:
                        listed.append({"test": path, "sha": commit["sha"], "pr": pr,
                                       "subject": commit["subject"], "date": date, "why": why,
                                       "fix_subjects": fix_subjects[:FILES_PER_CHANGE],
                                       "files": paths[:FILES_PER_CHANGE]})
        for path in paths:
            changes[path] = changes.get(path, 0) + 1
        if len(paths) <= CO_CHANGE_MAX_FILES:
            for i, a in enumerate(paths):
                for b in paths[i + 1:]:
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1
    history = [{"path": path, "changes": per_file[path]} for path in sorted(per_file)]
    test_fixes = [row for path in sorted(fixes) for row in fixes[path]]
    co_change: list[dict] = []
    guard_pairs: dict[tuple[str, str], dict] = {}
    for (a, b), n in sorted(pairs.items()):
        if n < CO_CHANGE_MIN_COMMITS:
            continue
        support = round(n / (changes[a] + changes[b] - n), 3)
        for path, partner in ((a, b), (b, a)):
            if tree.test_side(path) or not tree.test_side(partner):
                continue
            kind = "guard" if partner in guards else "test"
            row = {"path": path, "partner": partner, "partner_kind": kind, "commits": n,
                   "support": support, "confidence": support}
            co_change.append(row)
            if kind == "guard":
                guard_pairs[(path, partner)] = row
    co_change.sort(key=lambda r: (r["path"], -r["commits"], r["partner"]))
    return history, co_change, test_fixes, guard_pairs


# --- R: rules -------------------------------------------------------------------

def _declared_guards(tree: Tree) -> set[str]:
    declared: set[str] = set()
    for path in tree.files:
        if not path.endswith(".sh"):
            continue
        text = tree.text(path) or ""
        for block in _GUARD_ARRAY.findall(text):
            for token in block.split():
                token = token.strip("'\"")
                if token in tree.file_set:
                    declared.add(token)
    return declared


def _leading_text(text: str) -> str:
    """A test's leading docstring, or its first comment block."""
    stripped = text.lstrip()
    for quote in ('"""', "'''"):
        if stripped.startswith(quote):
            end = stripped.find(quote, 3)
            return stripped[3:end] if end > 0 else ""
    if stripped.startswith("/**") or stripped.startswith("/*"):
        end = stripped.find("*/")
        return stripped[:end] if end > 0 else ""
    lines = []
    for line in stripped.splitlines()[:60]:
        if line.startswith(("#", "//")):
            lines.append(line)
        elif line.strip():
            break
    return "\n".join(lines)


def _guards(tree: Tree, declared: set[str]) -> dict[str, str]:
    """Each guard test with its leading text."""
    guards: dict[str, str] = {}
    for path in sorted(tree.tests):
        text = tree.text(path)
        lead = _leading_text(text or "")
        if (path in declared or "guard" in posixpath.basename(path).lower()
                or _GUARD_WORD.search(lead)):
            guards[path] = lead
    return guards


def _mentions(tree: Tree, text: str, source: str, line_offset: int = 0
              ) -> list[tuple[int, str, str, str, str]]:
    """(line, nearest heading, path, target, how) for every path the text names."""
    found: list[tuple[int, str, str, str, str]] = []
    section = ""
    fence = False
    for number, line in enumerate(text.splitlines(), start=1 + line_offset):
        if line.lstrip().startswith("```"):
            fence = not fence
        heading = None if fence else _HEADING.match(line)
        if heading:
            section = heading.group(1).strip("*_ `")[:120]
        tokens = _BACKTICK.findall(line)
        bare_line = _BACKTICK.sub(" ", line)
        tokens += [m.group(1) for m in _BARE.finditer(bare_line)]
        for token in tokens:
            for part in token.split():
                resolved = tree.resolve(part, source)
                if resolved is not None and resolved[0] != source:
                    found.append((number, section, *resolved))
    return found


def _rules(tree: Tree, guards: dict[str, str], declared: set[str],
           guard_pairs: dict[tuple[str, str], dict], meta: dict
           ) -> tuple[list[dict], list[dict]]:
    """(rules, cannot_push)."""
    best: dict[tuple[str, str, str], dict] = {}

    def add(row: dict) -> None:
        key = (row["path"], row["source"], row["evidence"])
        if key not in best:
            best[key] = row

    cannot: dict[str, dict] = {WORKFLOWS_PATTERN: {
        "pattern": WORKFLOWS_PATTERN, "why": WORKFLOWS_WHY, "source": None, "line": None}}
    for source in tree.files:
        if not source.endswith(DOC_SUFFIXES):
            continue
        text = tree.text(source)
        if text is None:
            continue
        kind = "house_rule" if posixpath.basename(source) in HOUSE_RULE_FILES else "doc"
        lines = text.splitlines()
        for number, section, path, target, how in _mentions(tree, text, source):
            add({"path": path, "target": target, "kind": kind, "source": source,
                 "line": number, "section": section, "evidence": "path-ref",
                 "confidence": CONFIDENCE[how], "commits": None})
            if kind == "house_rule" and _FROZEN.search(lines[number - 1]):
                pattern = path + "/**" if target == "dir" else path
                cannot.setdefault(pattern, {"pattern": pattern, "source": source,
                                            "line": number,
                                            "why": lines[number - 1].strip()[:240]})
    for guard, lead in guards.items():
        for number, section, path, target, how in _mentions(tree, lead, guard):
            add({"path": path, "target": target, "kind": "guard", "source": guard,
                 "line": number, "section": section, "evidence": "path-ref",
                 "confidence": CONFIDENCE[how], "commits": None})
    for (path, guard), pair in sorted(guard_pairs.items()):
        add({"path": path, "target": "file", "kind": "guard", "source": guard, "line": None,
             "section": None, "evidence": "co-change", "confidence": pair["confidence"],
             "commits": pair["commits"]})
    for guard in sorted(declared & set(guards)):
        add({"path": ".", "target": "dir", "kind": "guard", "source": guard, "line": None,
             "section": None, "evidence": "declared", "confidence": CONFIDENCE["declared"],
             "commits": None})
    by_path: dict[str, list[dict]] = {}
    for row in best.values():
        by_path.setdefault(row["path"], []).append(row)
    rules: list[dict] = []
    for path in sorted(by_path):
        ranked = sorted(by_path[path], key=lambda r: (
            KIND_RANK[r["kind"]], -r["confidence"], r["source"], r["evidence"], r["line"] or 0))
        if len(ranked) > MAX_RULES_PER_PATH:
            _truncate(meta, "rules:per_path")
        rules.extend(ranked[:MAX_RULES_PER_PATH])
    cannot_push = [cannot[p] for p in sorted(cannot)]
    return rules, cannot_push


# --- test runners ---------------------------------------------------------------

def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A vitest/node glob as a regex: `**/` any directories, `*` within one, `{a,b}`."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += r"(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += r".*"
            i += 2
        elif pattern[i] == "*":
            out += r"[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += r"[^/]"
            i += 1
        elif pattern[i] == "{":
            end = pattern.find("}", i)
            if end < 0:
                out += re.escape(pattern[i:])
                break
            out += "(?:" + "|".join(re.escape(a) for a in pattern[i + 1:end].split(",")) + ")"
            i = end + 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"$")


def _segments(script: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||;", script) if s.strip()]


def _runner_of(segment: str, rel: str, package_dir: str, tree: Tree
               ) -> tuple[str, str] | None:
    """(runner, command) when this script segment runs the test at `rel`."""
    words = segment.split()
    if not words:
        return None
    if words[0] in ("npx", "pnpm", "yarn") and len(words) > 1:
        words = words[1:]
    if words[0] == "vitest":
        include = _vitest_include(tree, package_dir)
        hit = (any(_glob_regex(g).match(rel) for g in include) if include
               else bool(VITEST_DEFAULT.search(rel)) and "node_modules/" not in rel)
        return ("vitest", f"npx vitest run {rel}") if hit else None
    if words[0] != "node":
        return None
    if "--test" in words:
        globs = [w for w in words[words.index("--test") + 1:] if not w.startswith("-")]
        if any(_glob_regex(g.removeprefix("./")).match(rel) for g in globs):
            return "node:test", segment
        return None
    # `node <dir>/<runner>.mjs`: a runner script that runs the tests beside it.
    script = next((w for w in words[1:] if not w.startswith("-")), "")
    if script.endswith((".mjs", ".js", ".cjs")) and "/" in script:
        runner_dir = posixpath.dirname(script.removeprefix("./"))
        if posixpath.dirname(rel) == runner_dir:
            return "node", segment
    return None


def _vitest_include(tree: Tree, package_dir: str) -> list[str]:
    for name in _VITEST_CONFIGS:
        path = posixpath.join(package_dir, name) if package_dir != "." else name
        if path not in tree.file_set:
            continue
        found = _INCLUDE.search(tree.text(path) or "")
        if found:
            return _QUOTED.findall(found.group(1))
        return []
    return []


def _test_runners(tree: Tree) -> list[dict]:
    packages: dict[str, dict[str, str]] = {}
    for path in tree.files:
        if posixpath.basename(path) != "package.json" or "node_modules/" in path:
            continue
        try:
            scripts = json.loads(tree.text(path) or "{}").get("scripts") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(scripts, dict):
            packages[posixpath.dirname(path) or "."] = {
                str(k): str(v) for k, v in scripts.items() if isinstance(v, str)}
    rows: list[dict] = []
    for test in sorted(tree.tests):
        if not _JS_SOURCE.search(test) or not _JS_TEST.search(test):
            continue
        owners = [d for d in packages if d == "." or test.startswith(d + "/")]
        if not owners:
            continue
        package_dir = max(owners, key=len)
        rel = test if package_dir == "." else test[len(package_dir) + 1:]
        row = {"test": test, "runner": None, "command": None, "cwd": package_dir,
               "script": None, "evidence": "no package script or vitest include runs it"}
        for name in sorted(packages[package_dir]):
            for segment in _segments(packages[package_dir][name]):
                hit = _runner_of(segment, rel, package_dir, tree)
                if hit is not None:
                    row.update(runner=hit[0], command=hit[1], script=name,
                               evidence=f"package.json scripts.{name}")
                    break
            if row["runner"] is not None:
                break
        rows.append(row)
    return rows


# --- the whole ------------------------------------------------------------------

def _truncate(meta: dict, what: str) -> None:
    if what not in meta["truncated"]:
        meta["truncated"].append(what)


def build_context(repo: Path, *, commit_sha: str, files: Iterable[dict]) -> dict:
    """{"layers": {layer: rows}, "meta": {...}} for the clone at `repo`.

    Never raises for a clone git cannot read: the history layers are empty
    and `meta.reason` says why, and the rules are still mined from the tree.
    """
    tree = Tree(Path(repo), files)
    meta: dict[str, Any] = {
        "available": False, "reason": None, "window_days": HISTORY_DAYS,
        "window_start": None, "window_end": None, "commits": 0, "root_commits": 0,
        "boundary_commits": 0, "co_change_min_commits": CO_CHANGE_MIN_COMMITS,
        "truncated": [],
    }
    commits: list[dict] = []
    if not (Path(repo) / ".git").exists():
        meta["reason"] = "the checkout is not a git clone; history is not known"
    else:
        try:
            commits = _commits(Path(repo), commit_sha, meta)
        except (OSError, subprocess.TimeoutExpired) as exc:
            meta.update(available=False, reason=f"git could not run: {type(exc).__name__}")
    declared = _declared_guards(tree)
    guards = _guards(tree, declared)
    history, co_change, test_fixes, guard_pairs = _history_layers(
        tree, commits, set(guards), meta)
    rules, cannot_push = _rules(tree, guards, declared, guard_pairs, meta)
    layers = {"history": history, "co_change": co_change, "test_fixes": test_fixes,
              "rules": rules, "cannot_push": cannot_push, "test_runners": _test_runners(tree)}
    for layer, cap in MAX_ROWS.items():
        if len(layers[layer]) > cap:
            layers[layer] = layers[layer][:cap]
            _truncate(meta, f"{layer}:rows")
    meta["truncated"].sort()
    meta["guards"] = len(guards)
    return {"layers": layers, "meta": meta}
