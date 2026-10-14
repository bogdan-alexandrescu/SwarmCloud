"""Retrieval: every candidate row a step's seeds reach (§4.2), before ranking.

THE LAYERS, and where each is read from today:

| layer | read from |
|---|---|
| S symbols and edges | the staged snapshot: callers N hops, callees 1 hop, the module's siblings |
| T test map | the snapshot's `tests` layer and its file-level test edges |
| H history | the snapshot's `history` index layer when the indexer wrote one (lane CTXQ-H), and the checkout's own `git log` |
| R rules | the snapshot's `rules` index layer when there is one, and the checkout's tracked Markdown and test files that NAME a seed path |
| I in-flight | the stager's `inflight.json` (phase 2's route); absent is UNKNOWN |
| cannot-push | `.github/workflows/**` always, the stager's `cannot_push` and `hard_stop_paths`, and R rows marked so |

WHY THE CHECKOUT AS WELL AS THE SHARDS. A guard that polices a path by naming
it (`changed-guards.sh` selects by exactly that) and a doc that restates a
fact about it are read straight from the files the agent is about to edit, so
they are never older than the checkout. The clone is usually shallow, so
`git log` finds little history in production: the indexer's H layer is what
carries it there, and this reads both.

THE H AND R SHARD ROWS this reader takes (one shard per module, as KG2's
layers are keyed, or one keyed `*`); unknown fields are ignored:

    history: {"path", "changes": [{"pr", "sha", "title", "at", "fix", "files"}],
              "co_change": [{"path", "support", "confidence"}]}
    rules:   {"path", "docs": [{"path", "section"}], "guards": [{"test", "support"}],
              "cannot_push": bool, "why"}

Every walk is bounded and none recurses: tracked files come from
`git ls-files`, which never follows a link, and each read is capped.
"""

from __future__ import annotations

import posixpath
import re
import subprocess
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable

from ..graphmcp.snapshot import WHOLE, Snapshot, SnapshotError, module_of, path_of
from .seeds import Seeds

KINDS = ("symbol", "caller", "callee", "test", "guard", "rule", "doc", "similar_pr",
         "in_flight", "cannot_push")
#: Rows that are never cut by the budget (§4.3): P and W are the costliest misses.
ALWAYS = ("in_flight", "cannot_push")
#: Workflow files: the forge credential cannot push them, for any repository
#: (`swarm_api.repositories.HARD_STOP_PATHS_DEFAULT` says why).
WORKFLOW_GLOB = ".github/workflows/**"
#: How far callers are walked, per type (§4.5: implement "callers 2 hops").
CALLER_HOPS = {"implement": 2, "review": 2, "fix": 1, "test": 1, "design": 1, "plan": 2}
#: Bounds on what one pack walks, so a hub symbol cannot make it slow.
MAX_GRAPH_NODES = 300
MAX_SIBLINGS = 4
MAX_TRACKED_FILES = 6000
MAX_SCANNED_BYTES = 512 * 1024
MAX_SCANNED_TOTAL = 64 * 1024 * 1024
MAX_HISTORY_COMMITS = 200
#: A commit touching more files than this is a sweep, not a similar change.
MAX_COMMIT_FILES = 40
GIT_TIMEOUT_SECONDS = 10.0
RULE_DOCS = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md")
_PR = re.compile(r"\(#(\d+)\)\s*$")
_FIX = re.compile(r"\b(?:fix|fixes|fixed|repair|repairs|revert|reverts)\b", re.I)
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


@dataclass
class Candidate:
    """One row of the pack: `{kind, ref, why, score, evidence, stale}` (§4.3)."""

    kind: str
    ref: str
    path: str
    why: str
    score: float = 0.0
    hops: int | None = None
    confidence: float | None = None
    support: int | None = None
    bm25: float | None = None
    age_days: float | None = None
    evidence: str | None = None
    stale: str | None = None
    pr: int | None = None
    fix: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str]:
        return self.kind, self.ref

    def as_row(self) -> dict[str, Any]:
        row = {k: v for k, v in asdict(self).items() if v is not None and k != "extra"}
        row.update(self.extra)
        return row


class Pool:
    """Candidates by (kind, ref), merged: the best evidence of each, every reason."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], Candidate] = {}

    def add(self, cand: Candidate) -> None:
        have = self._rows.get(cand.key())
        if have is None:
            self._rows[cand.key()] = cand
            return
        if cand.why and cand.why not in have.why:
            have.why = f"{have.why}; {cand.why}"
        if cand.hops is not None and (have.hops is None or cand.hops < have.hops):
            have.hops = cand.hops
        for name in ("confidence", "support", "bm25"):
            new, old = getattr(cand, name), getattr(have, name)
            if new is not None and (old is None or new > old):
                setattr(have, name, new)

    def has(self, kind: str, ref: str) -> bool:
        return (kind, ref) in self._rows

    def rows(self) -> list[Candidate]:
        return list(self._rows.values())


# --- S and T: the graph ---------------------------------------------------------

def _is_test_symbol(snap: Snapshot, symbol_id: str) -> bool:
    row = snap.symbol(symbol_id)
    if row is not None and row.get("kind") == "test":
        return True
    file = snap.file(path_of(symbol_id))
    return bool(file and file.get("test"))


def _roots(snap: Snapshot, seeds: Seeds, task_type: str) -> list[str]:
    roots: list[str] = list(seeds.symbols)
    if task_type != "review":  # a review's roots are the changed symbols alone
        for path in seeds.files:
            for row in snap.symbols_in(path):
                if row.get("kind") != "local" and str(row.get("id")) not in roots:
                    roots.append(str(row.get("id")))
    return roots[:MAX_GRAPH_NODES]


def _test_rows(pool: Pool, snap: Snapshot, symbol: str, why: str) -> None:
    for row in snap.tests(symbol):
        test = str(row.get("test"))
        pool.add(Candidate(kind="test", ref=test, path=path_of(test), why=why,
                           hops=row.get("depth") if isinstance(row.get("depth"), int) else 1,
                           confidence=float(row.get("confidence") or 0),
                           evidence=row.get("evidence")))


def graph(pool: Pool, snap: Snapshot, seeds: Seeds, task_type: str) -> None:
    roots = _roots(snap, seeds, task_type)
    for symbol in roots:
        sig = snap.signature(symbol)
        why = ("BM25 hit for the brief" if seeds.searched and symbol in seeds.symbols
               else "changed by the diff" if task_type == "review"
               else "named by the step" if symbol in seeds.symbols
               else "defined in a file the step touches")
        pool.add(Candidate(kind="symbol", ref=symbol, path=path_of(symbol), why=why, hops=0,
                           confidence=1.0, bm25=seeds.bm25.get(symbol),
                           extra={"signature": sig.get("signature")} if sig else {}))
        _test_rows(pool, snap, symbol, f"covers {symbol}")
    for path in seeds.files:
        file = snap.file(path)
        for row in (file or {}).get("tests") or []:
            test = str(row.get("test"))
            pool.add(Candidate(kind="test", ref=test, path=path_of(test),
                               why=f"tests {path}", hops=1,
                               confidence=float(row.get("confidence") or 0),
                               evidence=row.get("evidence")))
    _callers(pool, snap, seeds, roots, CALLER_HOPS.get(task_type, 1), task_type)
    if task_type in ("implement", "test", "design"):
        for symbol in roots:
            for edge in snap.callees(symbol):
                callee = str(edge.get("to"))
                if edge.get("kind") == "import" or callee in roots or "#" not in callee:
                    continue
                pool.add(Candidate(kind="callee", ref=callee, path=path_of(callee),
                                   why=f"called by {symbol}", hops=1,
                                   confidence=float(edge.get("confidence") or 0),
                                   evidence=edge.get("evidence")))
    if task_type == "fix":
        _tested_code(pool, snap, seeds)
    if task_type == "review":
        _siblings(pool, snap, seeds, roots, "sibling in the same module: compare its idiom")
    if task_type == "test":
        _sibling_tests(pool, snap)


def _callers(pool: Pool, snap: Snapshot, seeds: Seeds, roots: list[str], depth: int,
             task_type: str) -> None:
    seen = set(roots)
    frontier = list(roots)
    for level in range(1, depth + 1):
        following: list[str] = []
        for node in frontier:
            for edge in sorted(snap.callers(node), key=lambda e: str(e.get("from"))):
                caller = str(edge.get("from"))
                if edge.get("kind") == "import" or caller in seen:
                    continue
                if len(seen) >= MAX_GRAPH_NODES:
                    return
                seen.add(caller)
                confidence = float(edge.get("confidence") or 0)
                if _is_test_symbol(snap, caller):
                    pool.add(Candidate(kind="test", ref=caller, path=path_of(caller),
                                       why=f"calls {node}", hops=level, confidence=confidence,
                                       evidence=edge.get("evidence")))
                    continue
                why = f"calls {node}"
                if task_type == "review" and not _in_diff(seeds, caller, snap):
                    why += ", outside the diff: check it still holds"
                pool.add(Candidate(kind="caller", ref=caller, path=path_of(caller), why=why,
                                   hops=level, confidence=confidence,
                                   evidence=edge.get("evidence")))
                following.append(caller)
        frontier = following


def _in_diff(seeds: Seeds, symbol_id: str, snap: Snapshot) -> bool:
    lines = seeds.changed_lines.get(path_of(symbol_id))
    if not lines:
        return False
    row = snap.symbol(symbol_id) or {}
    start, end = row.get("start_line") or 0, row.get("end_line") or 0
    return any(start <= n <= end for n in lines)


def _tested_code(pool: Pool, snap: Snapshot, seeds: Seeds) -> None:
    """What a red test exercises: KG6's TESTED CODE, from the test's callees."""
    for test_path in seeds.tests:
        for test in snap.symbols_in(test_path):
            for edge in snap.callees(str(test.get("id"))):
                code = str(edge.get("to"))
                if edge.get("kind") == "import" or "#" not in code or \
                        _is_test_symbol(snap, code):
                    continue
                pool.add(Candidate(kind="symbol", ref=code, path=path_of(code),
                                   why=f"exercised by the red test {test_path}", hops=0,
                                   confidence=float(edge.get("confidence") or 0)))
                _test_rows(pool, snap, code, f"also covers {code}")


def _siblings(pool: Pool, snap: Snapshot, seeds: Seeds, roots: list[str], why: str) -> None:
    for symbol in roots:
        row = snap.symbol(symbol) or {}
        kind = row.get("kind")
        found = 0
        for other in sorted(snap.shard("symbols", module_of(symbol)).rows,
                            key=lambda r: (str(r.get("path")), r.get("start_line") or 0)):
            ident = str(other.get("id"))
            if (other.get("kind") != kind or ident in roots
                    or other.get("path") == path_of(symbol)):
                continue
            pool.add(Candidate(kind="symbol", ref=ident, path=path_of(ident), why=why,
                               hops=1, confidence=0.5))
            found += 1
            if found >= MAX_SIBLINGS:
                break


def _sibling_tests(pool: Pool, snap: Snapshot) -> None:
    """The test files beside the ones already found: their layout and fixtures."""
    known = {c.path for c in pool.rows() if c.kind == "test"}
    for module in sorted({module_of(p) for p in known}):
        count = 0
        for row in sorted(snap.shard("files", module).rows, key=lambda r: str(r.get("path"))):
            path = str(row.get("path"))
            if not row.get("test") or path in known:
                continue
            pool.add(Candidate(kind="test", ref=path, path=path, hops=2, confidence=0.4,
                               why="sibling test file: follow its layout and fixtures"))
            count += 1
            if count >= MAX_SIBLINGS:
                break


# --- R: rules, guards and docs that name a path ---------------------------------

def _git(checkout: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "--no-optional-locks", "-c", "core.quotepath=off",
                               "-C", str(checkout), *args], capture_output=True,
                              timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def tracked_files(checkout: Path) -> list[str]:
    out = _git(checkout, "ls-files", "-z", "--cached")
    if out is None:
        return []
    return sorted(p for p in out.split("\0") if p)[:MAX_TRACKED_FILES]


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    base = parts[-1]
    return (any(p in ("tests", "test", "__tests__") for p in parts[:-1])
            or base.startswith("test_") or base.endswith(("_test.py", "_test.go"))
            or re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", base) is not None)


def _section(text: str, offset: int) -> str:
    heading = ""
    for line in text[:offset].splitlines():
        match = _HEADING.match(line)
        if match:
            heading = match.group(1).strip()
    return heading


def _read_tracked(checkout: Path, rel: str, budget: list[int]) -> str | None:
    path = checkout / rel
    try:
        if path.is_symlink() or not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_SCANNED_BYTES or budget[0] < size:
            return None
        budget[0] -= size
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def rules(pool: Pool, checkout: Path, seeds: Seeds, extra_paths: Iterable[str],
          tracked: list[str]) -> None:
    """Docs, house rules and guard tests that NAME one of the seed paths."""
    targets = [p for p in dict.fromkeys([*seeds.paths(), *extra_paths]) if "/" in p
               or "." in p]
    if not targets:
        return
    budget = [MAX_SCANNED_TOTAL]
    seed_tests = set(seeds.tests)
    for rel in tracked:
        markdown = rel.endswith(".md")
        test = is_test_path(rel)
        if not (markdown or test) or rel in targets or rel in seed_tests:
            continue
        text = _read_tracked(checkout, rel, budget)
        if text is None:
            continue
        for target in targets:
            at = text.find(target)
            if at < 0:
                continue
            if markdown:
                section = _section(text, at)
                rule = posixpath.basename(rel) in RULE_DOCS
                ref = f"{rel} § {section}" if section else rel
                pool.add(Candidate(kind="rule" if rule else "doc", ref=ref, path=rel,
                                   why=f"names {target}: keep it true",
                                   support=text.count(target)))
            else:
                pool.add(Candidate(kind="guard", ref=rel, path=rel,
                                   why=f"names {target}: it polices that file",
                                   support=text.count(target)))
            break


def _index_rows(snap: Snapshot, layer: str, paths: Iterable[str]) -> list[dict]:
    """Rows of an H or R index layer for `paths`, when the indexer wrote it."""
    table = ((snap.manifest.get("index_shards") or {}).get(layer) or {}) \
        if snap.format_version >= 3 else {}
    if not isinstance(table, dict) or not table:
        return []
    wanted = set(paths)
    rows: list[dict] = []
    for key in sorted({module_of(p) for p in wanted} | {WHOLE}):
        entry = table.get(key)
        if not isinstance(entry, dict):
            continue
        try:
            shard = snap._decode(entry)  # noqa: SLF001 -- the same checked reader
        except SnapshotError:
            continue
        rows.extend(r for r in shard.rows if str(r.get("path")) in wanted
                    or any(fnmatchcase(p, str(r.get("path"))) for p in wanted))
    return rows


def index_rules(pool: Pool, snap: Snapshot, seeds: Seeds) -> list[dict]:
    """Layer R from the snapshot. Returns its cannot-push rows for `cannot_push`."""
    blocked: list[dict] = []
    for row in _index_rows(snap, "rules", seeds.paths()):
        target = str(row.get("path"))
        for doc in row.get("docs") or []:
            if not isinstance(doc, dict) or not doc.get("path"):
                continue
            rel, section = str(doc["path"]), str(doc.get("section") or "")
            rule = posixpath.basename(rel) in RULE_DOCS
            pool.add(Candidate(kind="rule" if rule else "doc",
                               ref=f"{rel} § {section}" if section else rel, path=rel,
                               why=f"names {target}: keep it true", evidence="index"))
        for guard in row.get("guards") or []:
            if isinstance(guard, dict) and guard.get("test"):
                test = str(guard["test"])
                support = guard.get("support")
                pool.add(Candidate(kind="guard", ref=test, path=path_of(test),
                                   why=f"polices {target}", evidence="index",
                                   support=support if isinstance(support, int) else None))
        if row.get("cannot_push"):
            blocked.append({"glob": target, "why": str(row.get("why") or "a lane cannot "
                                                                         "push this path")})
    return blocked


# --- H: history ---------------------------------------------------------------

def history(pool: Pool, snap: Snapshot, checkout: Path, seeds: Seeds) -> None:
    paths = seeds.paths()
    if not paths:
        return
    for row in _index_rows(snap, "history", paths):
        target = str(row.get("path"))
        for change in row.get("changes") or []:
            if isinstance(change, dict):
                _similar(pool, target, change.get("pr"), str(change.get("sha") or ""),
                         str(change.get("title") or ""), change.get("age_days"),
                         bool(change.get("fix")) or bool(_FIX.search(str(change.get("title")
                                                                         or ""))),
                         evidence="index")
        for pair in row.get("co_change") or []:
            if isinstance(pair, dict) and pair.get("path") and is_test_path(str(pair["path"])):
                support = pair.get("support")
                pool.add(Candidate(kind="guard", ref=str(pair["path"]),
                                   path=str(pair["path"]), evidence="co-change",
                                   why=f"changed together with {target} before",
                                   support=support if isinstance(support, int) else 1))
    _git_history(pool, checkout, paths)


def _similar(pool: Pool, target: str, pr: Any, sha: str, title: str, age: Any, fix: bool,
             evidence: str) -> None:
    number = pr if isinstance(pr, int) else None
    ref = f"#{number}" if number else sha[:12]
    if not ref:
        return
    pool.add(Candidate(kind="similar_pr", ref=ref, path=target,
                       why=f"{'a fix that ' if fix else ''}touched {target}: {title[:140]}",
                       pr=number, fix=fix, evidence=evidence,
                       age_days=round(float(age), 2) if isinstance(age, (int, float)) else None))


def _git_history(pool: Pool, checkout: Path, paths: list[str]) -> None:
    head = _git(checkout, "log", "-1", "--format=%ct")
    if not head or not head.strip().isdigit():
        return
    now = int(head.strip())
    out = _git(checkout, "log", "--no-merges", "--full-diff", "--name-only",
               f"-n{MAX_HISTORY_COMMITS}", "--format=%x00%H%x1f%P%x1f%ct%x1f%s", "--",
               *paths)
    if not out:
        return
    for record in out.split("\0")[1:]:
        head_line, _, body = record.partition("\n")
        parts = head_line.split("\x1f")
        if len(parts) != 4 or not parts[1].strip():
            continue  # the root commit adds everything; it is not a similar change
        sha, _parents, when, subject = parts
        files = [f for f in body.splitlines() if f.strip()]
        if len(files) > MAX_COMMIT_FILES:
            continue
        touched = [p for p in paths if p in files]
        if not touched:
            continue
        match = _PR.search(subject)
        age = max(now - int(when), 0) / 86_400 if when.isdigit() else None
        _similar(pool, touched[0], int(match.group(1)) if match else None, sha, subject, age,
                 bool(_FIX.search(subject)), evidence="git")
        for other in files:
            if other not in paths and is_test_path(other) and not pool.has("test", other):
                pool.add(Candidate(kind="guard", ref=other, path=other, evidence="co-change",
                                   why=f"changed together with {touched[0]} before",
                                   support=1))


# --- I and W: never cut -----------------------------------------------------------

def _overlaps(path: str, territory: Iterable[str]) -> str | None:
    clean = path.rstrip("/")
    for held in territory:
        if held == clean or held.startswith(clean + "/") or clean.startswith(held + "/"):
            return held
    return None


def in_flight(pool: Pool, staged: Any, territory: list[str]) -> str | int:
    """Layer I rows overlapping this step's territory; "UNKNOWN" when none was staged."""
    if not isinstance(staged, dict) or not isinstance(staged.get("rows"), list):
        return "UNKNOWN"
    listed = 0
    for row in staged["rows"]:
        if not isinstance(row, dict):
            continue
        files = [str(f) for f in row.get("files") or [] if isinstance(f, str)]
        hits = [h for h in (_overlaps(f, territory) for f in files) if h]
        if not hits:
            continue
        ref = str(row.get("ref") or row.get("title") or "")[:120]
        title = str(row.get("title") or "")[:160]
        pool.add(Candidate(kind="in_flight", ref=ref, path=hits[0],
                           why=f"{title}: edits {', '.join(sorted(set(hits))[:4])}"))
        listed += 1
    return listed


def cannot_push(pool: Pool, stage: dict, from_index: list[dict]) -> None:
    rows = [{"glob": WORKFLOW_GLOB,
             "why": "the forge credential cannot push workflow files: say so, do not try"}]
    for glob in stage.get("cannot_push") or []:
        if isinstance(glob, str) and glob.strip():
            rows.append({"glob": glob.strip(), "why": "a lane cannot push this path"})
    rows.extend(from_index)
    for glob in stage.get("hard_stop_paths") or []:
        if isinstance(glob, str) and glob.strip():
            rows.append({"glob": glob.strip(),
                         "why": "a hard-stop path: a change here is held for a human"})
    for row in rows:
        pool.add(Candidate(kind="cannot_push", ref=row["glob"][:200], path=row["glob"][:200],
                           why=row["why"]))


def normalise_bm25(pool: Pool) -> None:
    top = max((c.bm25 for c in pool.rows() if c.bm25), default=0.0)
    if top > 0:
        for cand in pool.rows():
            if cand.bm25:
                cand.bm25 = round(cand.bm25 / top, 4)

