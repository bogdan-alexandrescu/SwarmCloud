"""The leak scans and the replay run in a repository the worker made (#259).

WHAT WENT WRONG. Three security reviews of #259 each found the per-commit and
final-tree secret scans bypassable through git state the agent writes in its
own clone: `refs/replace/*`, `.git/info/grafts`, `.git/shallow`, and last a
forged `.git/objects/info/commit-graph`. `git diff` takes a commit's root tree
from the commit-graph when one is present, so an agent that rewrote the graph
entry of a commit showed the scan a clean tree while `commit-tree` and the
push carried the real one, key and all. Each fix closed one file; the next
review found another.

WHAT IS PINNED (owner decision, 2026-09-29). After the agent's processes are
reaped the worker creates a fresh repository outside `work/`, fetches the
agent's HEAD and the clone base into it with object checking, and runs the
kept-commit list, the per-commit scan, the fold, the final-tree scan, the
replay, the authorship check and the push there. No alternates point into
the agent's clone, and nothing the agent wrote under `.git` -- its config,
hooks, grafts, shallow file, replace refs or commit-graph -- is read by any
of those steps. Each attack below gets the secret caught, and nothing secret
is pushed.

Real git against a local `file://` remote; only the HTTP forge is faked.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_agent_commits_are_kept import (
    KEY,
    _all_object_bytes,
    _attempt,
    _branch_exists,
    _commit,
    _random_token,
    _rev,
)
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    forge,
    local_urls,
    origin,
    tree_at,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    """git in the agent's clone, as the agent. `commit-tree` and
    `replace --graft` write commits, and CI has no git identity of its own."""
    ident = ["-c", "user.name=Agent", "-c", "user.email=agent@example.invalid"]
    return subprocess.run(
        ["git", *ident, *args], cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write_commit_graph(repo: Path) -> Path:
    """`git commit-graph write --reachable` in the agent's clone.

    git ignores a commit-graph in a shallow repository, so the agent first
    deletes `.git/shallow` -- which it can: the clone is its to edit, and a
    depth-1 clone of a one-commit origin has no history past the base.
    """
    (repo / ".git" / "shallow").unlink(missing_ok=True)
    _git(repo, "commit-graph", "write", "--reachable")
    graph = repo / ".git" / "objects" / "info" / "commit-graph"
    assert graph.is_file(), "git wrote no commit-graph"
    return graph


def _forge_commit_graph(repo: Path, trees: dict[str, str]) -> None:
    """Point each commit's commit-graph entry at a tree of the agent's choosing.

    The reviewer's forgery (scratchpad/review259b/probe_cg.py): the graph's
    CDAT chunk holds, per commit, the root tree git uses in place of reading
    the commit object. Rewriting it and re-signing the file's trailing hash
    makes `git diff A B` compare the forged trees, while `cat-file commit`,
    `commit-tree` and pack-objects still see the real ones.
    """
    graph = _write_commit_graph(repo)
    data = bytearray(graph.read_bytes())
    chunk_count = data[6]
    chunks = {}
    for i in range(chunk_count + 1):
        entry = 8 + 12 * i
        chunks[bytes(data[entry : entry + 4])] = struct.unpack(">Q", data[entry + 4 : entry + 12])[0]
    fanout, oids, cdat = chunks[b"OIDF"], chunks[b"OIDL"], chunks[b"CDAT"]
    count = struct.unpack(">I", data[fanout + 255 * 4 : fanout + 256 * 4])[0]
    listed = [bytes(data[oids + 20 * i : oids + 20 * i + 20]).hex() for i in range(count)]
    for commit, tree in trees.items():
        index = listed.index(commit)
        data[cdat + 36 * index : cdat + 36 * index + 20] = bytes.fromhex(tree)
    data[-20:] = hashlib.sha1(bytes(data[:-20])).digest()
    graph.chmod(stat.S_IRUSR | stat.S_IWUSR)
    graph.write_bytes(bytes(data))
    # The control that the forgery took: in the clone, a history walk from
    # HEAD now reports the forged tree for each forged commit.
    #
    # Read through a WALK, not `rev-parse <commit>^{tree}`. A commit named on
    # the command line is parsed from its object (`parse_object`), which since
    # git 2.4x takes the graph only with PARSE_OBJECT_SKIP_HASH_CHECK -- so on
    # CI's git 2.55 rev-parse printed the real tree and this control failed
    # before the worker ever ran, while the attack was still live: a commit
    # REACHED by a walk (`repo_parse_commit_internal` -> `parse_commit_in_graph`)
    # still gets its tree from the graph (`repo_get_commit_tree` ->
    # `get_commit_tree_in_graph`), in 2.40 and in 2.55. Every forged commit
    # here is an ancestor of HEAD, so the walk reaches each one.
    walked = dict(
        line.split(" ", 1)
        for line in _git(repo, "log", "--format=%H %T", "HEAD").splitlines()
        if line
    )
    for commit, tree in trees.items():
        assert walked.get(commit) == tree, "the forgery did not take"


# -- the forged commit-graph (#259, third security review) ------------------


def test_a_forged_commit_graph_does_not_hide_an_intermediate_commits_key(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """C1 adds the key, C2 an unrelated file, C3 deletes the key. The agent
    writes a commit-graph saying C1's tree is the base's and C2's is C3's, so
    in the clone every per-commit diff reads clean and the history was kept
    one commit at a time -- C1's rewrite, whose REAL tree holds `.env`, was
    pushed. In a clean repository there is no graph: C1's diff shows the key
    and the history folds to the clean final tree."""
    def edit(repo: Path) -> None:
        base = _rev(repo, "HEAD")
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        _commit(repo, "Configure the client")
        first = _rev(repo, "HEAD")
        (repo / "a.txt").write_text("a\n")
        _commit(repo, "Add a")
        second = _rev(repo, "HEAD")
        (repo / ".env").unlink()
        _commit(repo, "Stop committing the env file")
        third = _rev(repo, "HEAD")
        _forge_commit_graph(
            repo,
            {first: _rev(repo, f"{base}^{{tree}}"), second: _rev(repo, f"{third}^{{tree}}")},
        )

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graph-commit", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert out.get("agent_commits_folded") == 3, out
    assert "agent_commits_kept" not in out, out
    files = tree_at(origin, branch)
    assert "a.txt" in files and ".env" not in files, files
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


def test_a_forged_commit_graph_does_not_hide_a_key_in_the_final_tree(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The agent commits `.env` and keeps it, then forges the BASE's graph
    entry to the final tree. `git diff base HEAD` then reads nothing added, so
    both the per-commit scan and the final-tree scan passed and the key was
    pushed. From a clean repository the final tree adds `.env`, and nothing
    is published."""
    def edit(repo: Path) -> None:
        base = _rev(repo, "HEAD")
        (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
        _commit(repo, "Configure the client")
        _forge_commit_graph(repo, {base: _rev(repo, "HEAD^{tree}")})

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graph-final", edit=edit, register=(KEY,)
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is False, out
    assert out.get("final_tree_leak") == (
        "the final tree adds a credential in .env (rule registered_secret, line 1)"
        ": it is the value of a secret registered for this task; remove the value, or read it at runtime from configuration"
    ), out
    assert not _branch_exists(origin, branch), "a branch was pushed"
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


# -- grafts, replace refs and the shallow file ------------------------------


def _key_added_then_deleted(repo: Path) -> tuple[str, str, str, str]:
    """base, then C1 adds the key, C2 an unrelated file, C3 deletes the key."""
    base = _rev(repo, "HEAD")
    (repo / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    _commit(repo, "Configure the client")
    first = _rev(repo, "HEAD")
    (repo / "a.txt").write_text("a\n")
    _commit(repo, "Add a")
    second = _rev(repo, "HEAD")
    (repo / ".env").unlink()
    _commit(repo, "Stop committing the env file")
    return base, first, second, _rev(repo, "HEAD")


def _assert_folded_and_clean(origin: Path, config, out: dict) -> None:
    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert "agent_commits_kept" not in out, out
    assert out.get("agent_commits_folded"), out
    files = tree_at(origin, branch)
    assert "a.txt" in files and ".env" not in files, files
    assert KEY.encode() not in _all_object_bytes(origin), "the key reached a pushed object"


def test_a_graft_file_and_a_replace_graft_together_still_fold(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The reviewer's `replace_graft` probe: a `git replace --graft` puts C2
    on the base and `.git/info/grafts` puts C3 there too, so any walk that
    honours either skips the key's commit."""
    def edit(repo: Path) -> None:
        base, _first, second, third = _key_added_then_deleted(repo)
        _git(repo, "replace", "--graft", second, base)
        (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{third} {base}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-replace-graft", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


def test_a_graft_that_makes_a_later_commit_a_root_still_folds(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """`.git/info/grafts` naming C2 with no parents: the walk from HEAD stops
    at C2 and never reaches the key's commit."""
    def edit(repo: Path) -> None:
        _base, _first, second, _third = _key_added_then_deleted(repo)
        (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "info" / "grafts").write_text(f"{second}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-graft-root", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


def test_a_replace_ref_over_the_key_commit_still_folds(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """`refs/replace/<C1>` naming a clean commit: any reader that honours
    replacement reads the clean commit's tree for C1."""
    def edit(repo: Path) -> None:
        base, first, _second, _third = _key_added_then_deleted(repo)
        clean = _git(repo, "commit-tree", _rev(repo, f"{base}^{{tree}}"), "-p", base, "-m", "clean")
        _git(repo, "replace", first, clean)

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-replace-commit", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


@pytest.mark.parametrize("which", ["second", "head"])
def test_a_shallow_file_the_agent_edited_does_not_hide_the_key(
    worker_factory, monkeypatch, origin, local_urls, forge, which
):
    """`.git/shallow` naming C2 (or HEAD) makes git treat it as a root: the
    walk from HEAD never reaches the key's commit. The worker's repository
    takes its shallow boundary from the clone base it recorded, never from
    the agent's file."""
    def edit(repo: Path) -> None:
        _base, _first, second, third = _key_added_then_deleted(repo)
        (repo / ".git" / "shallow").write_text(f"{second if which == 'second' else third}\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-shallow-{which}", edit=edit, register=(KEY,)
    )
    _assert_folded_and_clean(origin, config, out)


# -- a hostile .git/config ---------------------------------------------------


def _marker_program(tmp: Path, name: str) -> tuple[Path, Path]:
    """A program that records it ran, and prints nothing."""
    marker = tmp / f"{name}.ran"
    program = tmp / f"{name}.sh"
    program.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 0\n")
    program.chmod(0o755)
    return program, marker


@pytest.mark.parametrize(
    "setting",
    ["diff.external", "core.fsmonitor", "diff.submodule", "core.hooksPath"],
)
def test_a_hostile_git_config_does_not_hide_the_key_or_run(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path, setting
):
    """The agent's `.git/config`: an external diff program that prints
    nothing (so a diff read through it adds nothing), an fsmonitor hook, a
    `diff.submodule=diff`, a hooks directory. None of them reaches the scan,
    none of them runs, and the key is caught."""
    program, marker = _marker_program(tmp_path, setting.replace(".", "-"))

    def edit(repo: Path) -> None:
        _key_added_then_deleted(repo)
        if setting == "diff.submodule":
            _git(repo, "config", "diff.submodule", "diff")
            _git(repo, "config", "diff.external", str(program))
        elif setting == "core.hooksPath":
            hooks = tmp_path / "hooks"
            hooks.mkdir()
            for hook in ("pre-commit", "post-commit", "reference-transaction", "post-checkout"):
                shutil.copy(program, hooks / hook)
            _git(repo, "config", "core.hooksPath", str(hooks))
        else:
            _git(repo, "config", setting, str(program))
        (repo / "notes.txt").write_text("left uncommitted, so the worker commits it\n")

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id=f"t-config-{setting.replace('.', '-')}",
        edit=edit, register=(KEY,),
    )
    _assert_folded_and_clean(origin, config, out)
    assert not marker.exists(), f"the agent's {setting} program ran during the publish"


# -- the structure itself: scan, replay and push share one clean repository --


def test_the_scans_the_replay_and_the_push_run_in_one_worker_owned_repository(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """Every step that decides what is pushed runs in the repository the
    worker built -- not in the clone, and not in a repository that borrows
    the clone's object store through alternates -- and that repository lives
    outside `work/`."""
    seen: dict[str, list[Path]] = {"prepare": [], "replay": [], "final": [], "verify": [], "push": []}
    real = {
        "prepare": lifecycle.prepare_publish_repo,
        "replay": lifecycle.replay_agent_commits,
        "final": lifecycle.final_tree_leak,
        "verify": lifecycle.verify_worker_authorship,
        "push": lifecycle.push_branch,
    }

    def recording(name: str, attribute: str):
        def wrapper(**kwargs):
            result = real[name](**kwargs)
            seen[name].append(Path(result if name == "prepare" else kwargs["repo"]))
            return result
        monkeypatch.setattr(lifecycle, attribute, wrapper)

    recording("prepare", "prepare_publish_repo")
    recording("replay", "replay_agent_commits")
    recording("final", "final_tree_leak")
    recording("verify", "verify_worker_authorship")
    recording("push", "push_branch")

    def edit(repo: Path) -> None:
        (repo / "one.txt").write_text("one\n")
        _commit(repo, "Add one")
        (repo / "two.txt").write_text("two\n")

    worker, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-clean-repo", edit=edit,
    )

    assert out["published"] is True, out.get("publish_reason")
    assert len(seen["prepare"]) == 1, seen
    clean = seen["prepare"][0]
    for step in ("replay", "final", "verify", "push"):
        assert seen[step] == [clean], (step, seen)
    clone = worker.ws.work / lifecycle.REPO_DIR_NAME
    assert clean.resolve() != clone.resolve()
    assert not clean.resolve().is_relative_to(worker.ws.work.resolve()), clean
    assert not (clean / ".git" / "objects" / "info" / "alternates").exists(), (
        "the worker's repository borrows the agent's object store"
    )


# -- the tiered guard, end to end (#373) --------------------------------------
#
# OWNER DECISION, 2026-09-30. A test of the redaction rules holds text the
# rules match, and both publish scans refused it: no SwarmCloud agent could
# publish one (#327's two runs, task_4dac1ded0ffc424a8dcb and
# task_0b453a13d78b4f2da063). In a test path a generic match now counts only
# when its value looks like a credential. The per-commit scan and the
# final-tree scan ask the same question, so the history is kept AND published.

REDACTION_TEST = (
    "from swarm_redaction import redact\n"
    "\n"
    "\n"
    "def test_json_password_is_masked():\n"
    "    masked = redact('{\"password\": \"hunter2\"}').text\n"
    "    assert masked == '{\"password\": \"********\"}'\n"
    "\n"
    "\n"
    "def test_registration_names_are_not_values():\n"
    "    log.info(\"resolved\", extra={\"secret\": \"tenant-git-token\", \"token\": \"anthropic-api-key\"})\n"
    "    resolve(secret=\"swarm-tenant-acme-git\")\n"
)


def test_a_redaction_test_is_kept_commit_by_commit_and_published(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    def edit(repo: Path) -> None:
        tests = repo / "tests" / "unit" / "control_plane"
        tests.mkdir(parents=True)
        (tests / "test_json_artifact_masking.py").write_text(REDACTION_TEST)
        _commit(repo, "Add the masking tests")
        (repo / "notes.txt").write_text("done\n")
        _commit(repo, "Add notes")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-373-kept", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is True, out.get("publish_reason")
    assert "final_tree_leak" not in out, out
    assert out.get("agent_commits_kept") == 2, out
    assert "agent_commits_folded" not in out, out
    assert "tests/unit/control_plane/test_json_artifact_masking.py" in tree_at(origin, branch)


def test_a_credential_shaped_token_in_a_test_file_publishes_nothing_and_names_the_line(
    worker_factory, monkeypatch, origin, local_urls, forge, log_stream
):
    token = _random_token(32)

    def edit(repo: Path) -> None:
        tests = repo / "tests" / "unit"
        tests.mkdir(parents=True)
        (tests / "test_client.py").write_text(
            'MASKED = {"password": "********"}\n'
            f'SENT = {{"token": "{token}"}}\n'
        )
        _commit(repo, "Add the client tests")

    _, config, out = _attempt(worker_factory, monkeypatch, origin, task_id="t-373-refused", edit=edit)

    branch = f"{config.git_branch_prefix}{config.task_id}"
    assert out["published"] is False, out
    assert out.get("final_tree_leak") == (
        "the final tree adds a credential in tests/unit/test_client.py "
        "(rule key_value_assignment, line 2): it is a literal given to a credential's name, "
        "at least 16 characters of letters and digits with at least 3.5 bits of entropy "
        "per character; remove the value, or read it at runtime from configuration"
    ), out
    assert token not in str(out), "the value reached the publish result"
    assert token not in log_stream.getvalue()
    assert not _branch_exists(origin, branch), "a branch was pushed"
    assert token.encode() not in _all_object_bytes(origin), "the token reached a pushed object"


# -- a merge of a branch the agent fetched with --depth (#2559) ----------------
#
# MEASURED 2026-10-26T23:01Z to 2026-10-27T06:01Z: six fix steps FAILED with
# "could not fetch the agent's work into the worker's clean repository"
# (fingerprint fr-ffa78ac1ab9b). Each agent's swarm-git `git_fetch` answered
# `api_unavailable`, so it ran `git fetch --depth=N origin <sibling>` in its own
# shell and merged the sibling. The clone's `.git/shallow` then held the
# sibling's depth cut-offs, but the upload side's boundary held the recorded
# base alone, so the walk from HEAD crossed the merge into sibling history the
# clone never had. A cut-off ORIGIN CONFIRMS it has may join the boundary: the
# history under a commit origin has is origin's, and cannot hide the agent's
# content, which descends from it. Nothing else the agent wrote there may.


def _plain_git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _origin_confirmer(origin_repo: Path, asked: list[list[str]], *, lie: list[str] = ()):
    """swarm-api's answer to `git_fetch` `confirm` (#2413), from a file://
    origin: the asked shas that are commits there -- plus, when `lie` is
    given, shas nobody asked about, which the worker must not believe."""
    def confirm(shas: list[str]) -> list[str]:
        asked.append(list(shas))
        have = [
            sha for sha in shas
            if subprocess.run(["git", "-C", str(origin_repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                              capture_output=True).returncode == 0
        ]
        return [*have, *lie]
    return confirm


def _merged_depth_fetch(tmp_path: Path) -> tuple[Path, Path, str, str, list[str]]:
    """origin: main m1..m3, and X branched at m1 with x1..x5. The agent's clone
    is `--depth 1` of main (base m3); it fetches X `--depth=3` and merges it.
    Returns (origin, clone, base, merge, x shas oldest first)."""
    src = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _git(src, "commit", "-q", "--allow-empty", "-m", "m1")
    _git(src, "checkout", "-q", "-b", "X")
    xs = []
    for i in range(1, 6):
        (src / f"x{i}.txt").write_text(f"x{i}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", f"x{i}")
        xs.append(_rev(src, "HEAD"))
    _git(src, "checkout", "-q", "main")
    for i in (2, 3):
        (src / f"m{i}.txt").write_text(f"m{i}\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-q", "-m", f"m{i}")
    clone = tmp_path / "work" / "repo"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "main",
                    f"file://{src}", str(clone)], check=True, capture_output=True)
    base = _rev(clone, "HEAD")
    _git(clone, "fetch", "-q", "--depth=3", "origin", "X:refs/remotes/origin/X")
    _git(clone, "merge", "-q", "--no-ff", "--allow-unrelated-histories", "-m", "merge X",
         "origin/X")
    merge = _rev(clone, "HEAD")
    shallow = (clone / ".git" / "shallow").read_text().split()
    # The control: the clone is cut off on X (x3) as well as at the base.
    assert sorted(shallow) == sorted([base, xs[2]]), (shallow, base, xs)
    return src, clone, base, merge, xs


def _prepare(tmp_path: Path, clone: Path, base: str, **kwargs) -> Path:
    from agent_worker.gitops import prepare_publish_repo
    from agent_worker.logs import build_logger

    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    return prepare_publish_repo(
        source_repo=clone, work_head=None, base=base, private_dir=tmp_path / "private",
        logs_dir=logs, timeout_seconds=60,
        logger=build_logger(task_id="task_1", attempt_id="att_1", tenant_id="acme",
                            generation=1, runner_profile="mock", stream=io.StringIO()),
        **kwargs,
    )


def test_a_merge_of_a_depth_fetched_branch_publishes_when_origin_confirms_its_shallow_lines(
    tmp_path: Path,
):
    src, clone, base, merge, xs = _merged_depth_fetch(tmp_path)
    asked: list[list[str]] = []
    repo = _prepare(tmp_path, clone, base, confirm_on_origin=_origin_confirmer(src, asked))
    assert _plain_git(repo, "rev-parse", "refs/swarm/head") == merge
    assert _plain_git(repo, "rev-parse", "HEAD") == merge
    # Asked about X's cut-off alone: the base is the worker's own floor.
    assert asked == [[xs[2]]]
    boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
    assert boundary == [base, xs[2]]


def test_an_unconfirmed_shallow_line_is_never_added_and_the_fetch_failure_names_gits_reason(
    tmp_path: Path,
):
    from agent_worker.gitops import GitError

    src, clone, base, _merge, _xs = _merged_depth_fetch(tmp_path)
    for confirmer in (None, lambda shas: []):
        kwargs = {} if confirmer is None else {"confirm_on_origin": confirmer}
        with pytest.raises(GitError) as raised:
            _prepare(tmp_path, clone, base, **kwargs)
        text = str(raised.value)
        # The prefix failurecauses and failure-review fingerprint on, verbatim.
        assert text.startswith("could not fetch the agent's work into the worker's clean "
                               "repository"), text
        assert "git: " in text, text
        assert text.split("git: ", 1)[1].strip(), text

    def refusing(shas: list[str]) -> list[str]:
        raise RuntimeError("the platform did not answer")

    with pytest.raises(GitError):
        _prepare(tmp_path, clone, base, confirm_on_origin=refusing)


def test_a_forged_unconfirmable_or_malformed_shallow_line_never_joins_the_boundary(
    tmp_path: Path,
):
    """The agent's `.git/shallow` is only a question to origin. A sha origin
    does not have, an abbreviated one and a malformed line are not added; a
    confirmer that answers more than it was asked is read as a subset; and
    the key commit stays reachable, so it is fetched (and scanned)."""
    src = tmp_path / "origin"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    _git(src, "commit", "-q", "--allow-empty", "-m", "base")
    clone = tmp_path / "work" / "repo"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{src}", str(clone)],
                   check=True, capture_output=True)
    base, first, second, third = _key_added_then_deleted(clone)
    stranger = hashlib.sha1(os.urandom(16)).hexdigest()
    (clone / ".git" / "shallow").write_text(
        f"{base}\n{second}\n{third}\n{stranger}\n{second[:12]}\nnot-a-sha\n{second}\n"
    )
    asked: list[list[str]] = []
    repo = _prepare(tmp_path, clone, base,
                    confirm_on_origin=_origin_confirmer(src, asked, lie=[first]))
    assert asked == [[second, third, stranger]], asked
    boundary = (repo / ".git" / "swarm-source-shallow").read_text().split()
    assert boundary == [base], boundary
    assert _plain_git(repo, "rev-parse", "HEAD") == third
    # The key's commit was fetched: the walk was not stopped at C2.
    _plain_git(repo, "cat-file", "-e", f"{first}^{{commit}}")


# -- the worker's final publish asks origin through its git tools relay -------
#
# The six fr-ffa78ac1ab9b failures were all at the worker's final publish, so
# the confirmer is exercised there too: `Worker._origin_confirmer` hands
# `prepare_publish_repo` the relay's `confirm_on_origin`. A confirmer that
# raises reads as "origin confirmed nothing", so a broken wiring would only
# show as the merge failing to publish -- which is what the first test pins.


class _ConfirmingRelay:
    """The attempt's git tools relay, answering `git_fetch` `confirm` from the
    file:// origin, or refusing with a fence."""

    def __init__(self, origin_repo: Path, *, fenced: bool = False) -> None:
        self.asked: list[list[str]] = []
        self.request_ids: list[str] = []
        self._confirm = _origin_confirmer(origin_repo, self.asked)
        self._fenced = fenced

    def confirm_on_origin(self, shas: list[str], *, request_id: str) -> list[str]:
        self.request_ids.append(request_id)
        if self._fenced:
            from agent_worker import children as children_mod

            self.asked.append(list(shas))
            raise children_mod.ChildSubmitFenced(
                409, "child_submit_fenced", "", retryable=False
            )
        return self._confirm(shas)


def _origin_with_a_sibling(origin_repo: Path, tmp_path: Path) -> list[str]:
    """Give the bare `origin` a sibling X (x1..x5, off its base) and move main
    on by m2, m3, so a depth-1 clone of main is cut off below the merge base.
    Returns X's shas, oldest first."""
    seed = tmp_path / "sibling-seed"
    subprocess.run(["git", "clone", "-q", str(origin_repo), str(seed)],
                   check=True, capture_output=True)
    _git(seed, "checkout", "-q", "-b", "X")
    xs = []
    for i in range(1, 6):
        (seed / f"x{i}.txt").write_text(f"x{i}\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", f"x{i}")
        xs.append(_rev(seed, "HEAD"))
    _git(seed, "checkout", "-q", "main")
    for i in (2, 3):
        (seed / f"m{i}.txt").write_text(f"m{i}\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-q", "-m", f"m{i}")
    _git(seed, "push", "-q", "origin", "main", "X")
    return xs


def _fetch_sibling_with_depth_and_merge(origin_repo: Path, xs: list[str]):
    """The agent's shell, as in the six failures: `git fetch --depth=3` of X,
    then a `--no-ff` merge of it."""
    def edit(repo: Path) -> None:
        base = _rev(repo, "HEAD")
        _git(repo, "fetch", "-q", "--depth=3", f"file://{origin_repo}",
             "X:refs/remotes/origin/X")
        _git(repo, "merge", "-q", "--no-ff", "--allow-unrelated-histories",
             "-m", "merge X", "refs/remotes/origin/X")
        shallow = (repo / ".git" / "shallow").read_text().split()
        # The control: the clone is cut off on X (x3) as well as at the base.
        assert sorted(shallow) == sorted([base, xs[2]]), (shallow, base, xs)
    return edit


def test_the_final_publish_of_a_depth_fetched_merge_asks_origin_through_the_relay(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    xs = _origin_with_a_sibling(origin, tmp_path)
    relay = _ConfirmingRelay(origin)
    monkeypatch.setattr(lifecycle.Worker, "_workflow_broker", lambda self: relay,
                        raising=False)

    _, config, out = _attempt(
        worker_factory, monkeypatch, origin, task_id="t-2559-merge",
        edit=_fetch_sibling_with_depth_and_merge(origin, xs),
    )

    branch = f"{config.git_branch_prefix}{config.task_id}"
    # MUTATION: `_origin_confirmer` returns None, or calls a renamed relay
    # method -> the boundary is the base alone and the publish-fetch fails.
    assert out["published"] is True, out.get("publish_reason")
    assert relay.asked == [[xs[2]]], relay.asked
    assert all(r.startswith("publish-confirm-") for r in relay.request_ids), relay.request_ids
    files = tree_at(origin, branch)
    assert {"x1.txt", "x5.txt", "m3.txt"} <= files, files


def test_a_fence_from_the_publish_confirmer_is_raised_out_of_build_clean_repo(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    """`prepare_publish_repo` reads any confirmer error as "origin confirmed
    nothing"; a fence is still the fence (invariant 5), so the worker raises
    it rather than going on to publish."""
    from agent_worker import children as children_mod

    xs = _origin_with_a_sibling(origin, tmp_path)
    relay = _ConfirmingRelay(origin, fenced=True)
    monkeypatch.setattr(lifecycle.Worker, "_workflow_broker", lambda self: relay,
                        raising=False)
    worker, config, _ = worker_factory(
        task_id="t-2559-fenced", attempt_id="att-t-2559-fenced",
        lease_id="lease-t-2559-fenced", repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-2559-fenced", "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    repo = worker.ws.work / lifecycle.REPO_DIR_NAME
    base = _rev(repo, "HEAD")
    _fetch_sibling_with_depth_and_merge(origin, xs)(repo)

    # MUTATION: drop the `finally: raise fenced[0]` -> GitError (the fetch
    # failure on the base-only boundary) is raised instead.
    with pytest.raises(children_mod.ChildSubmitFenced):
        worker._build_clean_repo(repo, floor=base)
    assert relay.asked == [[xs[2]]], relay.asked
