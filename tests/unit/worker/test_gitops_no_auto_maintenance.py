"""No git command `gitops` runs starts automatic maintenance in its repository
(#2922): the whole-module guard.

git ends some commands with `git maintenance run --auto` (`gc --auto` before
2.29), which git 2.56 -- CI's runner -- detaches. Its repack can hold or
rewrite `.git/shallow` while the next git command over the same repository
runs ("shallow file has changed since we read it"). Three lanes each found one
more such command (the fetches, the integrator's merge, then the auto-commit
and the fold's `git commit` ahead of `prepare_publish_repo`'s
`--update-shallow` fetch); this file ends the hunt by checking every argv the
module builds:

* `test_every_git_command_the_module_runs_is_classified_and_the_starters_carry_the_settings`
  reads `gitops.py`'s source, finds every git argv in it and its subcommand,
  and fails on a command `_MEASURED` has no entry for (measure it below
  first) or a maintenance starter without `*_NO_AUTO_MAINTENANCE`.
* `test_the_classification_holds_on_this_git` runs one representative
  invocation per `_MEASURED` entry under `GIT_TRACE`, with the module's own
  flags, so the table is re-measured on whatever git runs the suite.
* the two `*_starts_no_background_maintenance` tests run `commit_dirty` and
  `fold_agent_commits` themselves under `GIT_TRACE`.
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
from pathlib import Path

import pytest

from agent_worker import gitops
from agent_worker.logs import build_logger

#: A git that starts automatic maintenance, as `GIT_TRACE` writes it:
#: `maintenance run --auto` (git 2.29 on) or `gc --auto` before.
_AUTO_MAINTENANCE = re.compile(r"run_command: git (maintenance run|gc) --auto")

STARTS = "starts maintenance in this repository"
NONE = "starts none"
REMOTE = "starts it only in the receiving repository"

#: Every git command `gitops` runs, keyed by its subcommand and the literal
#: tokens after it (the longest key that prefixes an argv's tokens wins), as
#: measured with GIT_TRACE on git 2.39.5 and re-measured on every run by
#: `test_the_classification_holds_on_this_git`. A STARTS command carries
#: `*_NO_AUTO_MAINTENANCE`. The push's `gc --auto` is `receive-pack`'s, in the
#: repository pushed to: git unsets `GIT_CONFIG_PARAMETERS` for that child, so
#: no `-c` on the push reaches it, and in production that repository is the
#: forge's.
_MEASURED: dict[tuple[str, ...], str] = {
    ("add",): NONE,
    ("cat-file",): NONE,
    ("check-ignore",): NONE,
    ("checkout",): NONE,
    ("cherry-pick",): NONE,
    ("clone",): NONE,
    ("commit",): STARTS,
    ("commit-tree",): NONE,
    ("config",): NONE,
    ("count-objects",): NONE,
    ("diff",): NONE,
    ("fetch",): STARTS,
    ("init",): NONE,
    ("log",): NONE,
    ("ls-tree",): NONE,
    ("merge",): STARTS,
    ("merge", "--abort"): NONE,
    ("merge-base",): NONE,
    ("merge-tree",): NONE,
    ("push",): REMOTE,
    ("remote",): NONE,
    ("reset",): NONE,
    ("rev-list",): NONE,
    ("rev-parse",): NONE,
    ("rm",): NONE,
    ("show",): NONE,
    ("status",): NONE,
    ("symbolic-ref",): NONE,
    ("var",): NONE,
    ("write-tree",): NONE,
}

#: Starred names that only add options before the subcommand.
_OPTION_LISTS = {"_NO_HOOKS", "_TOKEN_SAFE", "_NO_AUTO_MAINTENANCE", "config_args"}
_OPTION_CALLS = {"_worker_identity"}
#: Options that take the next argv element as their value.
_VALUED = {"-c", "-C"}


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _assigned(name: str, scope: ast.AST) -> list[ast.List | ast.Tuple]:
    """Every list or tuple literal bound to `name` in `scope`."""
    found = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                found.append(node.value)
    return found


def _is_git_argv(node: ast.List | ast.Tuple) -> bool:
    if not node.elts:
        return False
    first = node.elts[0]
    if isinstance(first, ast.Name):
        return first.id == "git_binary"
    return isinstance(first, ast.Starred) and isinstance(first.value, ast.Name) \
        and first.value.id == "g"


def _tails(elts: list[ast.expr], scope: ast.AST, module: ast.Module,
           where: str) -> list[tuple[tuple[str, ...], bool]]:
    """(subcommand and its literal tokens, carries `*_NO_AUTO_MAINTENANCE`)
    for one argv; several when a starred local is bound more than once."""
    carries = False
    skip = False
    for index, element in enumerate(elts):
        if skip:
            skip = False
            continue
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            if element.value in _VALUED:
                skip = True
                continue
            if element.value.startswith("-"):
                continue
            tokens = []
            for rest in elts[index:]:
                if not (isinstance(rest, ast.Constant) and isinstance(rest.value, str)):
                    break
                tokens.append(rest.value)
            return [(tuple(tokens), carries)]
        if isinstance(element, ast.Name) and element.id == "git_binary":
            continue
        if isinstance(element, ast.Starred):
            value = element.value
            if isinstance(value, ast.Name) and value.id in _OPTION_LISTS:
                carries = carries or value.id == "_NO_AUTO_MAINTENANCE"
                continue
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and value.func.id in _OPTION_CALLS:
                continue
            if isinstance(value, ast.Name):
                # `*g` (options only) or a starred argv tail: read what the
                # name is bound to, in the function first, then the module.
                bound = _assigned(value.id, scope) or _assigned(value.id, module)
                assert bound, f"{where}: cannot read `*{value.id}`"
                if value.id == "g":
                    for g in bound:
                        assert _tails(list(g.elts), scope, module, where) == [], \
                            f"{where}: `g` names a subcommand"
                        carries = carries or any(
                            isinstance(e, ast.Starred) and isinstance(e.value, ast.Name)
                            and e.value.id == "_NO_AUTO_MAINTENANCE" for e in g.elts)
                    continue
                return [(tail, carries or inner)
                        for b in bound
                        for tail, inner in _tails(list(b.elts) + elts[index + 1:], scope,
                                                  module, where)]
        raise AssertionError(f"{where}: cannot read argv element {ast.unparse(element)}")
    return []


def _git_argvs() -> list[tuple[str, tuple[str, ...], bool]]:
    source = Path(gitops.__file__).read_text()
    module = ast.parse(source)
    parents = _parents(module)
    found = []
    for node in ast.walk(module):
        if isinstance(node, (ast.List, ast.Tuple)) and _is_git_argv(node):
            scope = _enclosing_function(node, parents) or module
            where = f"gitops.py:{node.lineno}"
            for tail, carries in _tails(list(node.elts), scope, module, where):
                found.append((where, tail, carries))
    return found


def _classify(tail: tuple[str, ...]) -> tuple[str, ...] | None:
    keys = [k for k in _MEASURED if tail[:len(k)] == k]
    return max(keys, key=len) if keys else None


def test_every_git_command_the_module_runs_is_classified_and_the_starters_carry_the_settings(
) -> None:
    """MUTATION: drop `*_NO_AUTO_MAINTENANCE` from `commit_dirty`'s or
    `fold_agent_commits`' `git commit` (or any fetch, or the integrator's
    merge), or add a git command `_MEASURED` does not list, and this fails
    naming the line."""
    argvs = _git_argvs()
    seen = {tail[0] for _, tail, _ in argvs}
    # The reader found the module's commands, not an empty walk.
    assert {"commit", "fetch", "merge", "push", "cherry-pick", "reset"} <= seen, seen
    unmeasured = [f"{where}: git {' '.join(tail)}" for where, tail, _ in argvs
                  if _classify(tail) is None]
    assert not unmeasured, (
        "measure these with GIT_TRACE and add them to _MEASURED: " + "; ".join(unmeasured))
    bare = [f"{where}: git {' '.join(tail)}" for where, tail, carries in argvs
            if _MEASURED[_classify(tail)] == STARTS and not carries]
    assert not bare, "starts automatic maintenance without *_NO_AUTO_MAINTENANCE: " + \
        "; ".join(bare)
    commits = [where for where, tail, _ in argvs if _classify(tail) == ("commit",)]
    assert len(commits) == 2, commits


_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
_ID = ("-c", "user.name=T", "-c", "user.email=t@example.com",
       "-c", "init.defaultBranch=main")


def _git(cwd: Path, *args: str, trace: Path | None = None, check: bool = True) -> str:
    env = dict(_ENV)
    if trace is not None:
        env["GIT_TRACE"] = str(trace)
    done = subprocess.run(["git", *_ID, *args], cwd=str(cwd), env=env, check=False,
                          capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


def _commit(work: Path, name: str, content: str | None = None) -> str:
    (work / name).write_text(content or f"{name}\n")
    _git(work, "add", name)
    _git(work, "commit", "--quiet", "-m", name)
    return _git(work, "rev-parse", "HEAD")


def test_the_classification_holds_on_this_git(tmp_path: Path) -> None:
    """Each `_MEASURED` entry, run once under GIT_TRACE with the flags
    `gitops` gives it: a STARTS command starts maintenance without the settings
    and none with them, a NONE command starts none, and the push's starts only
    after `receive-pack`, with the settings or without."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet")
    for name in ("one", "two", "three"):
        _commit(seed, name)
    _git(seed, "checkout", "--quiet", "-b", "side")
    _commit(seed, "s1")
    _commit(seed, "s2")
    _git(seed, "checkout", "--quiet", "main")
    _git(seed, "checkout", "--quiet", "-b", "clash", "HEAD~1")
    _commit(seed, "three", "clash\n")
    _git(seed, "checkout", "--quiet", "main")
    _commit(seed, "four")
    forge = tmp_path / "forge.git"
    _git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(forge))
    url = f"file://{forge}"
    work = tmp_path / "work"
    _git(tmp_path, "clone", "--quiet", url, str(work))
    fresh = tmp_path / "fresh"
    settings = list(gitops._NO_AUTO_MAINTENANCE)
    measured: dict[tuple[str, ...], list[str]] = {}

    def step(key: tuple[str, ...], cwd: Path, *args: str, check: bool = True,
             quiet: bool = False) -> None:
        trace = tmp_path / "trace.log"
        trace.write_text("")
        _git(cwd, *(settings if quiet else []), *args, trace=trace, check=check)
        text = trace.read_text()
        assert "built-in: git" in text, f"the trace did not see git {' '.join(args)}"
        measured.setdefault(key, []).append(text)
        started = bool(_AUTO_MAINTENANCE.search(text))
        if quiet or _MEASURED[key] == NONE:
            want = _MEASURED[key] == REMOTE
        else:
            want = True
        assert started == want, f"git {' '.join(args)} (settings={quiet}): {text}"
        if _MEASURED[key] == REMOTE:
            receive = text.index("built-in: git receive-pack")
            assert not _AUTO_MAINTENANCE.search(text[:receive]), text

    step(("clone",), tmp_path, "clone", "--quiet", "--depth", "1", "--no-tags",
         "--single-branch", url, str(tmp_path / "shallow"))
    step(("init",), tmp_path, "init", "--quiet", str(fresh))
    step(("remote",), fresh, "remote", "add", "origin", url)
    step(("config",), fresh, "config", "--replace-all", "remote.origin.fetch",
         "+refs/heads/*:refs/remotes/origin/*")
    step(("fetch",), fresh, "fetch", "--depth", "1", "--no-tags", "origin", "main")
    step(("fetch",), fresh, "fetch", "--quiet", "--no-tags", "--depth=2147483647",
         "--update-shallow", "origin", "side", quiet=True)
    step(("checkout",), fresh, "checkout", "--quiet", "FETCH_HEAD")
    (work / "new.txt").write_text("new\n")
    step(("add",), work, "add", "--all", "--intent-to-add", "--", ".")
    step(("add",), work, "add", "--all", "--", ".")
    step(("status",), work, "status", "--porcelain")
    step(("diff",), work, "diff", "--cached", "--quiet", check=False)
    step(("reset",), work, "reset", "-q", "--", "new.txt")
    step(("check-ignore",), work, "check-ignore", "-q", "new.txt", check=False)
    _git(work, "add", "new.txt")
    step(("commit",), work, "commit", "--no-verify", "--message", "dirty")
    (work / "again.txt").write_text("again\n")
    _git(work, "add", "again.txt")
    step(("commit",), work, "commit", "--no-verify", "--message", "again", quiet=True)
    step(("write-tree",), work, "write-tree")
    tree = _git(work, "write-tree")
    step(("commit-tree",), work, "commit-tree", tree, "-p", "HEAD", "-m", "made")
    made = _git(work, "commit-tree", tree, "-p", "HEAD", "-m", "made")
    step(("reset",), work, "reset", "--soft", made)
    step(("rev-parse",), work, "rev-parse", "HEAD")
    step(("rev-list",), work, "rev-list", "--count", "HEAD")
    step(("log",), work, "log", "--format=%H", "-n", "2")
    step(("ls-tree",), work, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    step(("cat-file",), work, "cat-file", "commit", "HEAD")
    step(("merge-base",), work, "merge-base", "--is-ancestor", "origin/side", "HEAD",
         check=False)
    step(("merge-tree",), work, "merge-tree", "--write-tree", "HEAD", "origin/side")
    step(("show",), work, "show", "-s", "--format=%H %ct", "HEAD")
    step(("var",), work, "var", "GIT_COMMITTER_IDENT")
    step(("symbolic-ref",), work, "symbolic-ref", "HEAD")
    step(("count-objects",), work, "count-objects", "-v")
    step(("cherry-pick",), work, "cherry-pick", "--no-commit", "origin/side")
    step(("cherry-pick",), work, "cherry-pick", "--quit")
    step(("reset",), work, "reset", "--quiet", "--hard", "HEAD")
    step(("checkout",), work, "checkout", "HEAD", "--", "one")
    step(("rm",), work, "rm", "--quiet", "-f", "--", "one")
    _git(work, "reset", "--quiet", "--hard", "HEAD")
    step(("fetch",), work, "fetch", "--no-tags", "origin", "side")
    step(("merge",), work, "merge", "--no-ff", "--no-edit", "-m", "integrate side",
         "FETCH_HEAD")
    _git(work, "reset", "--quiet", "--hard", "HEAD~1")
    _git(work, "fetch", "--quiet", "--no-tags", "origin", "side")
    step(("merge",), work, "merge", "--no-ff", "--no-edit", "-m", "integrate side",
         "FETCH_HEAD", quiet=True)
    merge = _git(work, "rev-parse", "HEAD")
    _git(work, "reset", "--quiet", "--hard", "HEAD~1")
    step(("cherry-pick",), work, "cherry-pick", "-m", "1", "--no-commit", merge)
    _git(work, "reset", "--quiet", "--hard", "HEAD")
    # A merge that stops on a conflict commits nothing; with the settings, as
    # `merge_branches` runs it, it starts none either.
    step(("merge",), work, "merge", "--no-ff", "--no-edit", "origin/clash", check=False,
         quiet=True)
    step(("merge", "--abort"), work, "merge", "--abort")
    step(("push",), work, "push", "--no-verify", "--no-follow-tags", url,
         "HEAD:refs/heads/pushed")
    _commit(work, "five")
    step(("push",), work, "push", "--no-verify", "--no-follow-tags", url,
         "HEAD:refs/heads/pushed-quiet", quiet=True)

    assert set(measured) == set(_MEASURED), set(_MEASURED) ^ set(measured)


def _logger():
    return build_logger(task_id="task_1", attempt_id="att_1", tenant_id="tenant-a",
                        generation=1, runner_profile="mock", stream=io.StringIO())


def _traced(monkeypatch: pytest.MonkeyPatch, trace: Path, command: str) -> list[list[str]]:
    """Set GIT_TRACE on every argv whose command is `command`; return them."""
    seen: list[list[str]] = []
    run_child = gitops.run_child

    def traced(argv, **kwargs):
        if command in argv:
            seen.append(list(argv))
            kwargs["env"] = {**kwargs["env"], "GIT_TRACE": str(trace)}
        return run_child(argv, **kwargs)

    monkeypatch.setattr(gitops, "run_child", traced)
    return seen


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "publish"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    base = _commit(repo, "base")
    return repo, base


def _assert_no_maintenance(trace: Path, commits: list[list[str]]) -> None:
    text = trace.read_text()
    assert len(commits) == 1, commits
    assert "built-in: git commit" in text  # the trace saw the commit
    assert not _AUTO_MAINTENANCE.findall(text), text
    settings = commits[0][:commits[0].index("commit")]
    assert "maintenance.auto=false" in settings and "gc.auto=0" in settings, commits[0]


def test_commit_dirty_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop `*_NO_AUTO_MAINTENANCE` from `commit_dirty`'s commit and
    the trace shows `git maintenance run --auto` after it."""
    repo, base = _repo(tmp_path)
    (repo / "left.txt").write_text("uncommitted\n")
    trace = tmp_path / "git-trace.log"
    commits = _traced(monkeypatch, trace, "commit")
    sha = gitops.commit_dirty(
        repo=repo, message="swarm: work", author_name="swarm",
        author_email="swarm@example.invalid", private_dir=tmp_path / "private",
        logs_dir=tmp_path, timeout_seconds=60, logger=_logger(), base=base,
    )
    assert sha and sha != base
    _assert_no_maintenance(trace, commits)


def test_fold_agent_commits_starts_no_background_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MUTATION: drop `*_NO_AUTO_MAINTENANCE` from `fold_agent_commits`'
    commit and the trace shows `git maintenance run --auto` after it."""
    repo, base = _repo(tmp_path)
    _commit(repo, "agent-one")
    _commit(repo, "agent-two")
    trace = tmp_path / "git-trace.log"
    commits = _traced(monkeypatch, trace, "commit")
    count = gitops.fold_agent_commits(
        repo=repo, base=base, keep=None, message="swarm: work", author_name="swarm",
        author_email="swarm@example.invalid", private_dir=tmp_path / "private",
        logs_dir=tmp_path, timeout_seconds=60, logger=_logger(),
    )
    assert count == 2
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    _assert_no_maintenance(trace, commits)
