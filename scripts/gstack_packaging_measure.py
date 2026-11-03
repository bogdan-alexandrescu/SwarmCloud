#!/usr/bin/env python3
"""What baking gstack into an agent image costs, and what its skills need (the packaging spike).

The owner, 3 November 2026 (docs/design/agent-plugins.md, on the
agent-plugins-plan branch): gstack is baked into the agent images at a pinned
version, and the packaging is MEASURED before any image changes. This file is
that measurement. It reads a gstack install that `./setup --host claude
--no-browser` has already built (the shell around it fetches Bun and the
release tag and runs setup; nothing here downloads anything), and reports:

  * `size`     -- the whole install's bytes (without `.git`), per top-level
                  entry, and every compiled binary (an ELF file outside
                  `node_modules`); the native add-ons under `node_modules`
                  are totalled apart.
  * `closure`  -- the minimal set the offered skills need. It starts from each
                  skill's own directory and follows, to a fixed point, every
                  reference that names a file that exists in the install:
                  `~/.claude/skills/gstack/<path>` (the literal path the
                  rendered skills use), `$VAR/<path>` and `$(dirname "$0")/<path>`
                  in helper scripts (resolved against the referring file's
                  directory and the install root), bare `gstack-*` helper names
                  that exist in `bin/`, and relative TypeScript/JavaScript
                  imports. A bare import names a package; packages close over
                  their own `dependencies` and `optionalDependencies` that are
                  present in `node_modules`. A reference built at run time from
                  pieces is not seen by this walk -- the smoke run is what
                  catches that, so the closure is a proposal to test, never a
                  claim on its own.
  * `smoke`    -- each offered skill's helper scripts that need no model, run
                  the way a headless agent would: HOME is a workspace folder
                  holding `.claude/skills/gstack` as a link to the install,
                  GSTACK_SESSION_KIND=unattended, GSTACK_HOME a temporary
                  folder whose config turns the update check, telemetry and
                  outside reviews off, the working directory a fresh git
                  repository. A bash block of a SKILL.md is run when it has no
                  placeholder for the model to fill (`<base>`, `SESSION_ID`,
                  ...); a block with one is listed as skipped, never as passed.
                  A skill with no runnable block has the helpers its
                  `allowed-tools` names probed with `--help`.

READ-ONLY is a claim the run proves: `--read-only` makes the smoke run write a
probe file into the install first and refuses to label the run read-only if
the write succeeds. A writable run instead snapshots the install before and
after each skill, so every file a helper writes into it is named -- exactly
what breaks once the install is root-owned on a read-only root filesystem.

Unknown is never zero: a skill whose directory is missing is `missing`, a
block that timed out is `timeout`, and a size that could not be read is
absent from the totals with the path named in `unreadable`.

Unit tests: tests/unit/scripts/test_gstack_packaging_measure.py (no network,
no Bun: they build small installs in a temporary folder).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# The skills docs/design/agent-plugins.md offers (reviewer, investigator,
# planner and designer rows of its catalogue table).
OFFERED_SKILLS = (
    "review",
    "investigate",
    "health",
    "cso",
    "qa-only",
    "qa",
    "design-review",
    "document-generate",
    "document-release",
    "ship",
    "plan-eng-review",
    "plan-design-review",
    "autoplan",
    "office-hours",
    "plan-ceo-review",
    "plan-devex-review",
    "spec",
    "design-consultation",
    "design-html",
)

ELF_MAGIC = b"\x7fELF"
# Text files larger than this are not scanned for references: the largest
# reference-bearing file in v1.92.55.0 is under 300 KB, and the files above it
# are bundles (diagram-render.html) and the changelog.
SCAN_LIMIT_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules"}

LITERAL_ROOT = "~/.claude/skills/gstack/"
_PATH_CHARS = r"A-Za-z0-9_.@+-"
# `~/.claude/skills/gstack/x`, `$HOME/.claude/...` and `$_ROOT/.claude/...` all
# name the install.
_LITERAL_RE = re.compile(r"/\.claude/skills/gstack/([" + _PATH_CHARS + r"/]+)")
_COMMENT_RE = re.compile(r"^\s*(?:#(?!!)|//|\*|/\*)")
_VAR_RE = re.compile(r"(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$0\"?\)|\)\"?)/((?:\.\./)*[" + _PATH_CHARS + r"]+(?:/[" + _PATH_CHARS + r"]+)*)")
_HELPER_RE = re.compile(r"(?<![" + _PATH_CHARS + r"/])(gstack-[a-z0-9-]+(?:\.sh|\.ts|\.py)?)(?![A-Za-z0-9_-])")
_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s+|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s+)['"]([^'"\n]+)['"]""",
    re.MULTILINE,
)
_TS_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".cjs", ".json")
_CODE_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".cjs")
_BUILTIN_PREFIXES = ("node:", "bun:")
_NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "bun", "child_process", "cluster", "console", "constants", "crypto",
    "dgram", "dns", "events", "fs", "http", "http2", "https", "module", "net", "os", "path", "perf_hooks",
    "process", "querystring", "readline", "stream", "string_decoder", "timers", "tls", "tty", "url", "util",
    "v8", "vm", "worker_threads", "zlib",
}
_PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")

# Never followed: `.tmpl` skill sources (setup renders them; the model reads
# the rendered SKILL.md), what only installs or upgrades gstack (the image build runs
# setup once; the update check is off), gstack's own test suite, and -- added
# per call -- the directory of every skill that is not offered. Each reference
# cut this way is reported in `cut`, so the edge stays visible.
INSTALL_ONLY = (
    "setup", "setup.ps1", "bin/gstack-upgrade", "gstack-upgrade/", "test/",
    # re-render and relink skills (setup's work, done once in the image build)
    "bin/gstack-relink", "scripts/gen-skill-docs.ts", "scripts/resolvers/",
    # the Capy host's installer and the install doctor, which launches every binary
    "bin/gstack-capy-install", "capy/", "bin/gstack-doctor",
)

# A model fills these in; a block holding one is not run.
_ANGLE_RE = re.compile(r"<[A-Za-z][A-Za-z0-9_| .-]*>")
_CAPS_RE = re.compile(r"(?:(?<=[\s\"'(=,:])|^)([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)(?=[\s\"'),]|$)", re.MULTILINE)
# Upper-case words that are shell or git vocabulary, not placeholders.
_CAPS_ALLOWED = {"HEAD", "ORIG_HEAD", "FETCH_HEAD", "EOF", "PATH", "HOME", "TMPDIR", "PWD", "OK"}


# ── size ──────────────────────────────────────────────────────────────────


def _walk(root: Path, skip: set[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == ELF_MAGIC
    except OSError:
        return False


def measure_size(root: Path) -> dict:
    """Bytes of the install without .git: total, per top-level entry, binaries."""
    root = root.resolve()
    per_top: dict[str, int] = {}
    binaries: list[dict] = []
    native_addons = {"count": 0, "bytes": 0}
    unreadable: list[str] = []
    total = 0
    for path in _walk(root, {".git"}):
        if path.is_symlink():
            continue
        rel = path.relative_to(root)
        try:
            size = path.stat().st_size
        except OSError:
            unreadable.append(str(rel))
            continue
        total += size
        top = rel.parts[0]
        per_top[top] = per_top.get(top, 0) + size
        if _is_elf(path):
            if rel.parts[0] == "node_modules":
                native_addons["count"] += 1
                native_addons["bytes"] += size
            else:
                binaries.append({"path": str(rel), "bytes": size})
    return {
        "total_bytes": total,
        "per_top_level": dict(sorted(per_top.items(), key=lambda kv: (-kv[1], kv[0]))),
        "binaries": sorted(binaries, key=lambda b: (-b["bytes"], b["path"])),
        "node_modules_native_addons": native_addons,
        "unreadable": unreadable,
    }


# ── closure ───────────────────────────────────────────────────────────────


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > SCAN_LIMIT_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if data[:4] == ELF_MAGIC or b"\x00" in data[:1024]:
        return None
    return data.decode("utf-8", errors="replace")


def _inside(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _resolve_module(root: Path, base: Path, spec: str) -> Path | None:
    target = base / spec
    candidates = [target] + [Path(str(target) + s) for s in _TS_SUFFIXES]
    candidates += [target / ("index" + s) for s in _TS_SUFFIXES]
    for candidate in candidates:
        inside = _inside(root, candidate)
        if inside is not None and inside.is_file():
            return inside
    return None


def _package_name(spec: str) -> str | None:
    if spec.startswith((".", "/")) or spec.startswith(_BUILTIN_PREFIXES):
        return None
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    if name in _NODE_BUILTINS or not _PACKAGE_RE.match(name):
        return None
    return name


def references(root: Path, path: Path, text: str) -> tuple[set[Path], set[str]]:
    """Files inside root and package names that `text` (the content of `path`) names."""
    files: set[Path] = set()
    packages: set[str] = set()

    def add(candidate: Path) -> None:
        inside = _inside(root, candidate)
        if inside is not None and inside.is_file():
            files.add(inside)

    if path.suffix != ".md":
        # A helper's comments mention files it never runs (gstack-config's
        # comment names gstack-doctor, which launches every binary).
        text = "\n".join(line for line in text.splitlines() if not _COMMENT_RE.match(line))
    for rel in _LITERAL_RE.findall(text):
        add(root / rel.rstrip("."))
    for rel in _VAR_RE.findall(text):
        rel = rel.rstrip(".")
        add(path.parent / rel)
        add(root / rel)
    for name in _HELPER_RE.findall(text):
        add(root / "bin" / name)
    if path.suffix in _CODE_SUFFIXES or text.startswith("#!/usr/bin/env bun"):
        for spec in _IMPORT_RE.findall(text):
            if spec.startswith("."):
                found = _resolve_module(root, path.parent, spec)
                if found is not None:
                    files.add(found)
            else:
                name = _package_name(spec)
                if name:
                    packages.add(name)
    files.discard(path)
    return files, packages


def package_closure(root: Path, names: set[str]) -> dict:
    """Packages present in node_modules reachable from `names`, and their bytes."""
    modules = root / "node_modules"
    seen: set[str] = set()
    absent: set[str] = set()
    queue = sorted(names)
    while queue:
        name = queue.pop()
        if name in seen or name in absent:
            continue
        manifest = modules / name / "package.json"
        if not manifest.is_file():
            absent.add(name)
            continue
        seen.add(name)
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for field in ("dependencies", "optionalDependencies"):
            for dep in sorted((meta.get(field) or {}).keys()):
                if dep not in seen:
                    queue.append(dep)
    sizes = {}
    for name in sorted(seen):
        sizes[name] = sum(p.stat().st_size for p in _walk(modules / name, set()) if not p.is_symlink())
    return {
        "packages": sizes,
        "bytes": sum(sizes.values()),
        "not_installed": sorted(absent),
    }


def _excluded_prefixes(root: Path, skills) -> tuple[str, ...]:
    others = sorted(
        d.name + "/"
        for d in root.iterdir()
        if d.is_dir() and d.name not in skills and ((d / "SKILL.md").is_file() or (d / "SKILL.md.tmpl").is_file())
    )
    return tuple(others)


def _cut(rel: str, others: tuple[str, ...]) -> bool:
    """Whether the walk stops at `rel`.

    Install-only paths and `.tmpl` sources always; inside a skill that is not
    offered only its pages (`.md`), because an offered skill reaching another
    skill's page would offer that skill too, while its tools -- browse's
    compiled browser among them -- are what the offered skill runs.
    """
    if rel.endswith(".tmpl") or rel in INSTALL_ONLY or rel.startswith(INSTALL_ONLY):
        return True
    return rel.endswith(".md") and rel.startswith(others)


def _is_leaf(root: Path, path: Path, skills) -> bool:
    """A markdown file outside the offered skills is documentation: kept, not followed.

    The model reads a skill's own pages and sections, so their references are
    followed; README.md, CLAUDE.md and docs/ mention paths without running them.
    """
    if path.suffix != ".md":
        return False
    top = path.relative_to(root).parts[0]
    return top not in skills


def _chain(root: Path, parent: dict, node: Path) -> list[str]:
    chain: list[str] = []
    while node is not None and len(chain) < 12:
        chain.append(str(node.relative_to(root)))
        node = parent.get(node)
    return chain


def skill_closure(root: Path, skills: tuple[str, ...] | list[str]) -> dict:
    """The files and packages the named skills reach, per skill and in union."""
    root = root.resolve()
    skills = list(skills)
    excluded = _excluded_prefixes(root, skills)
    per_skill: dict[str, dict] = {}
    union_files: set[Path] = set()
    union_packages: set[str] = set()
    for skill in skills:
        skill_dir = root / skill
        if not (skill_dir / "SKILL.md").is_file():
            per_skill[skill] = {"status": "missing"}
            continue
        pending = [p.resolve() for p in _walk(skill_dir, SKIP_DIRS) if p.is_file() and p.suffix != ".tmpl"]
        parent: dict[Path, Path | None] = {p: None for p in pending}
        seen: set[Path] = set()
        packages: set[str] = set()
        cut: set[tuple[str, str]] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            text = _read_text(current)
            if text is None or _is_leaf(root, current, skills):
                continue
            found, pkgs = references(root, current, text)
            packages |= pkgs
            for target in sorted(found - seen):
                rel = str(target.relative_to(root))
                if _cut(rel, excluded):
                    cut.add((str(current.relative_to(root)), rel))
                    continue
                parent.setdefault(target, current)
                pending.append(target)
        rels = sorted(str(p.relative_to(root)) for p in seen)
        binaries = sorted(r for r in rels if _is_elf(root / r))
        per_skill[skill] = {
            "status": "ok",
            "files": len(rels),
            "bytes": sum(p.stat().st_size for p in seen),
            "helpers": sorted(r for r in rels if r.startswith("bin/")),
            "binaries": binaries,
            "binary_via": {b: _chain(root, parent, (root / b).resolve()) for b in binaries},
            "packages": sorted(packages),
            "cut": [list(edge) for edge in sorted(cut)],
        }
        union_files |= seen
        union_packages |= packages
    rels = sorted(str(p.relative_to(root)) for p in union_files)
    by_top: dict[str, int] = {}
    for rel in rels:
        top = rel.split("/", 1)[0] if "/" in rel else "(root files)"
        by_top[top] = by_top.get(top, 0) + (root / rel).stat().st_size
    return {
        "skills": per_skill,
        "union": {
            "files": len(rels),
            "bytes": sum(by_top.values()),
            "by_top_level": dict(sorted(by_top.items(), key=lambda kv: (-kv[1], kv[0]))),
            "binaries": [
                {"path": r, "bytes": (root / r).stat().st_size} for r in rels if _is_elf(root / r)
            ],
            "packages": package_closure(root, union_packages),
            "file_list": rels,
        },
    }


# The worker's gstack config: the update check, telemetry and outside reviews
# off (docs/design/agent-plugins.md), and the design detector off -- with it on
# (`auto`, gstack's default), design-review's install block downloads the
# impeccable engine from github.com into HOME at run time and records that "the
# user accepted the install offer", although no person was asked.
GSTACK_CONFIG = "update_check: false\ntelemetry: off\ncodex_reviews: disabled\ndesign_detector: off\n"


# ── smoke ─────────────────────────────────────────────────────────────────


def bash_blocks(markdown: str) -> list[str]:
    """The ```bash fenced blocks of a SKILL.md, in order."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        stripped = line.strip()
        if current is None:
            if stripped in ("```bash", "```sh"):
                current = []
        elif stripped.startswith("```"):
            blocks.append("\n".join(current))
            current = None
        else:
            current.append(line)
    return blocks


_BRACKET_RE = re.compile(r"^\s*(\[[a-z][a-z ]*\])\s*$", re.MULTILINE)
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)(:?[-+=]|:\?|\?)?")
_ASSIGN_RE = re.compile(r"(?:^|[\s;&|(`])(?:export\s+|local\s+)?([A-Za-z_][A-Za-z0-9_]*)\+?=")
_LOOP_RE = re.compile(r"\b(?:for|read(?:\s+-[a-z]+)*)\s+([A-Za-z_][A-Za-z0-9_ ]*)")
# Set by the shell or by the run (HOME, PATH, TMPDIR, the GSTACK_* variables).
_SHELL_VARS = {"HOME", "PATH", "PWD", "OLDPWD", "TMPDIR", "OSTYPE", "RANDOM", "LANG", "USER", "SHELL", "BASH_SOURCE",
               "EUID", "UID", "IFS", "PPID", "SECONDS", "LINENO", "REPLY"}


def unassigned(block: str) -> list[str]:
    """Variables `block` reads without a default that it never assigns.

    Such a block runs after an earlier block or a model turn set them (`$B`,
    the browser binary; `$NEW_VERSION`); run alone it tests nothing.
    """
    assigned = set(_ASSIGN_RE.findall(block))
    for names in _LOOP_RE.findall(block):
        assigned.update(names.split())
    needed = set()
    for name, op in _VAR_REF_RE.findall(block):
        if op in (":-", "-", ":+", "+", ":=", "="):
            continue
        if name in assigned or name in _SHELL_VARS or name.startswith("GSTACK_"):
            continue
        needed.add("$" + name)
    return sorted(needed)


def placeholders(block: str) -> list[str]:
    """What in `block` a model would fill in; empty when it runs as written."""
    found = _ANGLE_RE.findall(block) + _BRACKET_RE.findall(block) + unassigned(block)
    heredoc_ends = set(re.findall(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)", block))
    for word in _CAPS_RE.findall(block):
        if word in _CAPS_ALLOWED or word in heredoc_ends or len(word) < 2:
            continue
        found.append(word)
    return sorted(set(found))


def _frontmatter_helpers(markdown: str) -> list[str]:
    if not markdown.startswith("---"):
        return []
    end = markdown.find("\n---", 3)
    head = markdown[: end if end > 0 else 0]
    names = re.findall(r"Bash\(~/\.claude/skills/gstack/(bin/[" + _PATH_CHARS + r"]+)", head)
    return sorted(set(n for n in names if not n.endswith(".exe")))


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    shot = {}
    for path in _walk(root, {".git", "node_modules"}):
        try:
            st = path.lstat()
        except OSError:
            continue
        shot[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return shot


def _probe_read_only(root: Path) -> bool:
    probe = root / ".swarm-read-only-probe"
    try:
        probe.write_text("x")
    except OSError:
        return True
    probe.unlink()
    return False


_BREAK_RE = re.compile(
    r"(Read-only file system|Permission denied|EACCES|EROFS|command not found|No such file or directory"
    r"|cannot find module|Cannot find module|not found)",
)


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _prepare_workspace(work: Path, install: Path, skills: list[str]) -> tuple[Path, Path, Path]:
    home = work / "home"
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    link = skills_dir / "gstack"
    if not link.exists():
        link.symlink_to(install)
    for skill in skills:
        if (install / skill / "SKILL.md").is_file():
            (skills_dir / skill).mkdir(exist_ok=True)
            target = skills_dir / skill / "SKILL.md"
            if not target.exists():
                target.symlink_to(install / skill / "SKILL.md")
    state = work / "gstack-home"
    state.mkdir(exist_ok=True)
    (state / "config.yaml").write_text(GSTACK_CONFIG, encoding="utf-8")
    project = work / "project"
    project.mkdir(exist_ok=True)
    git_env = dict(os.environ, HOME=str(home), GIT_CONFIG_NOSYSTEM="1")
    ident = ["-c", "user.name=spike", "-c", "user.email=spike@example.invalid"]
    if not (project / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True, env=git_env)
        (project / "README.md").write_text("spike\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True, env=git_env)
        subprocess.run(
            ["git", "-C", str(project), *ident, "commit", "-q", "-m", "spike"], check=True, env=git_env
        )
        # The worker's checkout has an origin; a bare repository stands in for it.
        origin = work / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=git_env)
        subprocess.run(["git", "-C", str(project), "remote", "add", "origin", str(origin)], check=True, env=git_env)
        subprocess.run(["git", "-C", str(project), "push", "-q", "-u", "origin", "main"], check=True, env=git_env)
    return home, state, project


def _run(command: list[str], cwd: Path, env: dict, timeout: float) -> dict:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
            start_new_session=True,
        )
    except OSError as exc:
        # The helper itself is absent or not executable: a failure, never a skip.
        return {"status": "failed", "rc": None, "seconds": round(time.monotonic() - started, 2),
                "stdout_tail": "", "stderr_tail": str(exc), "signals": [str(exc)[:240]]}
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {"status": "timeout", "rc": None, "seconds": round(time.monotonic() - started, 2),
                "stdout_tail": _tail(out), "stderr_tail": _tail(err), "signals": []}
    combined = proc.stdout + "\n" + proc.stderr
    signals = sorted({line.strip()[:240] for line in combined.splitlines() if _BREAK_RE.search(line)})
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "rc": proc.returncode,
        "seconds": round(time.monotonic() - started, 2),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
        "signals": signals,
    }


def smoke(
    install: Path,
    skills: list[str] | tuple[str, ...],
    work: Path,
    *,
    read_only: bool,
    bun_dir: Path | None = None,
    timeout: float = 60.0,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Run each skill's model-free bash blocks headless and unattended."""
    install = install.resolve()
    work.mkdir(parents=True, exist_ok=True)
    work = work.resolve()
    if read_only and not _probe_read_only(install):
        raise SystemExit(f"--read-only: {install} accepted a write; make it read-only first")
    skills = list(skills)
    home, state, project = _prepare_workspace(work, install, skills)
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if bun_dir is not None:
        path = f"{bun_dir.resolve()}{os.pathsep}{path}"
    env = {
        "PATH": path,
        "HOME": str(home),
        "GSTACK_HOME": str(state),
        "GSTACK_SESSION_KIND": "unattended",
        "LANG": "C.UTF-8",
        "TMPDIR": str(work / "tmp"),
        # The worker sets the agent's git identity; this stands in for it.
        "GIT_AUTHOR_NAME": "spike",
        "GIT_AUTHOR_EMAIL": "spike@example.invalid",
        "GIT_COMMITTER_NAME": "spike",
        "GIT_COMMITTER_EMAIL": "spike@example.invalid",
    }
    (work / "tmp").mkdir(exist_ok=True)
    env.update(extra_env or {})
    results: dict[str, dict] = {}
    for skill in skills:
        skill_md = install / skill / "SKILL.md"
        if not skill_md.is_file():
            results[skill] = {"status": "missing"}
            continue
        markdown = skill_md.read_text(encoding="utf-8")
        before = None if read_only else _snapshot(install)
        runs = []
        for index, block in enumerate(bash_blocks(markdown), start=1):
            if "gstack" not in block:
                # A plain git or shell action, not one of gstack's helpers.
                runs.append({"block": index, "status": "skipped", "placeholders": [], "reason": "no gstack helper"})
                continue
            holes = placeholders(block)
            if holes:
                runs.append({"block": index, "status": "skipped", "placeholders": holes})
                continue
            run = _run(["bash", "-c", block], project, env, timeout)
            run.update({"block": index, "first_line": block.strip().splitlines()[0][:160] if block.strip() else ""})
            runs.append(run)
        if not any(r["status"] != "skipped" for r in runs):
            for helper in _frontmatter_helpers(markdown):
                run = _run([str(home / ".claude/skills/gstack" / helper), "--help"], project, env, timeout)
                run.update({"helper": helper, "first_line": f"{helper} --help"})
                runs.append(run)
        written = []
        if before is not None:
            after = _snapshot(install)
            written = sorted(k for k, v in after.items() if before.get(k) != v)
        ran = [r for r in runs if r["status"] != "skipped"]
        results[skill] = {
            "status": "not-run" if not ran else ("ok" if all(r["status"] == "ok" for r in ran) else "failed"),
            "blocks": len([r for r in runs if "block" in r]),
            "ran": len(ran),
            "skipped": len(runs) - len(ran),
            "failed": len([r for r in ran if r["status"] != "ok"]),
            "install_writes": written,
            "runs": runs,
        }
    project_status = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain", "--ignored"],
        capture_output=True, text=True, env=dict(os.environ, HOME=str(home)),
    ).stdout.splitlines()
    home_files = sorted(
        str(p.relative_to(home)) for p in _walk(home, {".git"}) if not str(p.relative_to(home)).startswith(".claude/skills")
    )
    return {
        "read_only": read_only,
        "env": {k: v for k, v in env.items() if k.startswith("GSTACK_")},
        "skills": results,
        "project_untracked": project_status,
        "home_writes": home_files,
        "gstack_home_files": sorted(str(p.relative_to(state)) for p in _walk(state, set())),
    }


# ── report ────────────────────────────────────────────────────────────────


def _mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def summary(report: dict) -> str:
    """A short plain-text reading of a report, for the log and the doc."""
    out: list[str] = []
    size = report.get("size")
    if size:
        out.append(f"install: {_mb(size['total_bytes'])} without .git")
        for top, n in list(size["per_top_level"].items())[:8]:
            out.append(f"  {top}: {_mb(n)}")
        for b in size["binaries"]:
            out.append(f"  binary {b['path']}: {_mb(b['bytes'])}")
    closure = report.get("closure")
    if closure:
        u = closure["union"]
        out.append(
            f"offered-skill closure: {u['files']} files, {_mb(u['bytes'])}; "
            f"{len(u['packages']['packages'])} packages, {_mb(u['packages']['bytes'])}; "
            f"{len(u['binaries'])} binaries, {_mb(sum(b['bytes'] for b in u['binaries']))}"
        )
        missing = [s for s, v in closure["skills"].items() if v["status"] != "ok"]
        if missing:
            out.append(f"  missing skills: {', '.join(missing)}")
    for key in ("smoke_writable", "smoke_read_only"):
        run = report.get(key)
        if not run:
            continue
        out.append(f"{key}:")
        for skill, res in run["skills"].items():
            if res["status"] == "missing":
                out.append(f"  {skill}: missing")
                continue
            line = f"  {skill}: {res['status']} (ran {res['ran']}, skipped {res['skipped']}, failed {res['failed']})"
            if res["install_writes"]:
                line += f"; wrote into install: {', '.join(res['install_writes'][:5])}"
            out.append(line)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--install", type=Path, required=True, help="a gstack checkout after ./setup")
    parser.add_argument("--skills", default=",".join(OFFERED_SKILLS))
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--smoke-work", type=Path, help="run the smoke in this folder (HOME, GSTACK_HOME, project)")
    parser.add_argument("--read-only", action="store_true", help="the install is read-only; proved by a probe write")
    parser.add_argument("--bun-dir", type=Path, help="directory holding the bun binary, put first on PATH")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    skills = [s for s in args.skills.split(",") if s]
    report: dict = {"install": str(args.install.resolve()), "skills": skills}
    version = args.install / "VERSION"
    report["version"] = version.read_text().strip() if version.is_file() else None
    if args.smoke_work is None:
        report["size"] = measure_size(args.install)
        report["closure"] = skill_closure(args.install, skills)
    else:
        key = "smoke_read_only" if args.read_only else "smoke_writable"
        report[key] = smoke(
            args.install, skills, args.smoke_work, read_only=args.read_only,
            bun_dir=args.bun_dir, timeout=args.timeout,
        )
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
