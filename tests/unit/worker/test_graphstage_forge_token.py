"""Graph staging deepens a private repository's clone with the workspace's forge token (lane GRAPH-TOKEN-2563, part of #2540).

The ancestry fetch (`gitops.fetch_base_history`, lane GRAPH-ANCESTRY) was
anonymous, so it proved only a public repository's steps. BGD, 2026-10-27:
hand it the forge token. This is a credential path, so what this file holds is
mostly where the token must NOT go:

* the lifecycle passes the token reader when the worker may hold the token,
  and None when `_git_token_refusal` names a reason;
* the token file is 0600, in the worker's `private/` (never the context dir
  the agent reads) and gone after the fetch; with no `private_dir` the fetch
  is anonymous;
* a credential helper, an `insteadOf` rewrite or a remote named after the URL,
  planted in the clone's own `.git/config`, withholds the token -- no `-c`
  can undo an `insteadOf`, so the token is never handed to a fetch that has
  one -- and a clean clone still gets it;
* a failed fetch, or a token read that raises, is a reason in `stage.json`
  and the attempt goes on.

Offline: local git repositories, a recording stand-in for the fetch (which
hands `config` to the real git), the worker's FakeFirestore and a
LocalObjectStore. Every fake token is built at runtime. Each assertion names
the MUTATION it catches.
"""

from __future__ import annotations

import json
import secrets
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, graphstage, lifecycle
from agent_worker.hardening import FAILED, MemoryProtection

from test_graphstage import TENANT, URL, _no_ab, _promote
from test_graphstage_ancestry import _ENV, _Lane
from worker_seeds import seed_attempt

_REAL_GIT = shutil.which("git") or "git"


def _recorder(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A `git` that runs `config` with the real git and, for anything else,
    records its argv and the mode of every `--file=` it names, then exits
    `exit_code`."""
    binary = tmp_path / "bin" / "git"
    binary.parent.mkdir(exist_ok=True)
    log = tmp_path / "fetch.json"
    binary.write_text(
        "#!/usr/bin/env python3\nimport json, os, stat, sys\n"
        "argv = sys.argv[1:]\n"
        "if 'config' in argv:\n"
        f"    os.execv({_REAL_GIT!r}, [{_REAL_GIT!r}, *argv])\n"
        "files = [a.split('--file=', 1)[1] for a in argv if '--file=' in a]\n"
        "modes = [stat.S_IMODE(os.stat(f).st_mode) if os.path.exists(f) else None"
        " for f in files]\n"
        f"json.dump({{'argv': argv, 'files': files, 'modes': modes}}, open({str(log)!r}, 'w'))\n"
        f"sys.exit({exit_code})\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, log


def _files_holding(root: Path, token: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()
            and token in p.read_text(errors="replace")]


# --------------------------------------------------------------------------
# the lifecycle hands staging the token reader, or None
# --------------------------------------------------------------------------


def _forge_token_seen(db: Any, worker_factory: Any, monkeypatch: Any,
                      **kwargs: Any) -> tuple[Any, list[Any]]:
    seen: list[Any] = []

    def capture(*_args: Any, **kw: Any) -> dict[str, str]:
        seen.append(kw.get("forge_token", "absent"))
        return {}

    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", capture)
    seed_attempt(db)
    worker, _, _ = worker_factory(**kwargs)
    worker.run()
    return worker, seen


def test_the_lifecycle_passes_the_token_reader_when_the_token_is_not_refused(
        db, worker_factory, monkeypatch):
    worker, seen = _forge_token_seen(db, worker_factory, monkeypatch)
    assert worker._git_token_refusal() is None
    # MUTATION: `_graph_context_env` without `forge_token=` -> ["absent"], and
    # a private repository's steps never prove their ancestry.
    # MUTATION: passing `self._git_token()` (the value) reads the token for
    # every step, even one whose proof needs no fetch -> not callable.
    assert len(seen) == 1 and callable(seen[0])
    assert seen[0] == worker._git_token


def test_the_lifecycle_withholds_the_token_when_the_worker_must_not_hold_it(
        db, worker_factory, monkeypatch):
    worker, seen = _forge_token_seen(
        db, worker_factory, monkeypatch,
        memory=MemoryProtection(FAILED, "prctl refused in this test"))
    assert worker._git_token_refusal() is not None
    # MUTATION: dropping the `_git_token_refusal()` check reads the token
    # into the heap of a worker the agent can read.
    assert seen == [None]


# --------------------------------------------------------------------------
# where the token is held
# --------------------------------------------------------------------------


def test_the_token_file_is_0600_in_the_private_dir_and_gone_after_the_fetch(tmp_path):
    lane = _Lane(tmp_path)
    binary, log = _recorder(tmp_path)
    token = secrets.token_hex(20)

    why = gitops.fetch_base_history(lane.checkout, url=URL, base_sha=lane.base,
                                    private_dir=lane.private, token=token,
                                    git_binary=str(binary))

    assert why is None
    seen = json.loads(log.read_text())
    # MUTATION: a clean clone withholding the token (the scan reading every
    # config as unsafe) leaves a private repository unprovable.
    assert len(seen["files"]) == 1
    held = Path(seen["files"][0])
    # MUTATION: a file created with the default mode is readable by anyone
    # until a chmod.
    assert seen["modes"] == [0o600]
    # MUTATION: the credential file written anywhere but `private/`.
    assert held.parent == lane.private
    # MUTATION: skipping `_remove_credentials` leaves the token on disk for
    # the agent that starts next.
    assert not held.exists() and _files_holding(tmp_path, token) == []
    argv = seen["argv"]
    # MUTATION: a token in argv is readable through /proc.
    assert all(token not in a for a in argv)
    # MUTATION: dropping `-c http.proxy=` lets a configured proxy carry the
    # fetch. The reset precedes the store helper.
    assert argv[argv.index("http.proxy=") - 1] == "-c"
    assert argv.index("credential.helper=") < next(
        i for i, a in enumerate(argv) if a.startswith("credential.helper=store"))


def test_prepare_holds_the_token_only_with_a_private_dir(db, store, tmp_path, monkeypatch):
    lane = _Lane(tmp_path)
    _no_ab(db)
    _promote(db, store, lane.main[4])
    token = secrets.token_hex(20)
    asked: list[dict[str, Any]] = []

    def capture(checkout: Path, **kwargs: Any) -> str | None:
        asked.append(kwargs)
        return "the history fetch failed (git exit 128)"

    monkeypatch.setattr(gitops, "fetch_base_history", capture)
    for private_dir in (lane.private, None):
        graphstage.prepare(lane.checkout.parent, profile_name="claude-code",
                           runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                           db=db, store=store, tenant_id=TENANT, repository_url=URL,
                           task_id="task_b", checkout=lane.checkout, base_sha=lane.base,
                           private_dir=private_dir, forge_token=lambda: token)

    assert len(asked) == 2
    # MUTATION: `history_fetcher` not handing `token()` on -> None, and a
    # private repository is never proven.
    assert asked[0]["token"] == token and asked[0]["private_dir"] == lane.private
    # MUTATION: the context-dir fallback holding the token writes it beside
    # the files the agent reads.
    assert asked[1]["token"] is None
    assert asked[1]["private_dir"] == lane.checkout.parent / graphstage.CONTEXT_DIR_NAME


# --------------------------------------------------------------------------
# a clone's own config cannot redirect the token
# --------------------------------------------------------------------------


def _plant(lane: _Lane, *pairs: tuple[str, str]) -> None:
    for key, value in pairs:
        subprocess.run([_REAL_GIT, "-C", str(lane.checkout), "config", "--add", key, value],
                       check=True, capture_output=True, env={**_ENV, "HOME": str(lane.private)})


def test_a_planted_helper_or_insteadof_withholds_the_token(tmp_path):
    planted = {
        "credential": ("credential.https://github.com.helper", "!cat >> /dev/null"),
        "url": ("url.https://collector.invalid/.insteadOf", "https://github.com/"),
        "http": ("http.https://github.com.proxy", "http://collector.invalid:3128"),
        "remote": (f"remote.{URL}.url", "https://collector.invalid/x"),
    }
    for section, pair in planted.items():
        root = tmp_path / section
        root.mkdir()
        lane = _Lane(root)
        _plant(lane, pair)
        binary, log = _recorder(root, exit_code=128)
        token = secrets.token_hex(20)

        why = gitops.fetch_base_history(lane.checkout, url=URL, base_sha=lane.base,
                                        private_dir=lane.private, token=token,
                                        git_binary=str(binary))

        seen = json.loads(log.read_text())
        # MUTATION: no `_token_unsafe_config` scan (or one that misses this
        # section) adds the store helper, and git hands the token to the
        # planted helper, the rewritten host or the proxy.
        assert seen["files"] == [], section
        assert _files_holding(root, token) == [], section
        # MUTATION: a withheld token that fails silently: the reason must say
        # the token was withheld and which section caused it.
        assert why is not None and "forge token withheld" in why, (section, why)
        assert f"{section}.*" in why, (section, why)
        # The value (a URL) never reaches the reason.
        assert "collector.invalid" not in why


def test_a_planted_insteadof_never_sees_the_token_with_real_git(tmp_path):
    """End to end with the real git: the planted rewrite points the forge URL
    at the local origin, so the fetch lands -- anonymously. The credential
    file never exists, so nothing the rewrite reaches could be handed it."""
    lane = _Lane(tmp_path)
    _plant(lane, (f"url.{lane.url}.insteadOf", URL))
    seen_files: list[Path] = []
    real_clone_env = gitops._clone_env

    def watching(url: str, token: str | None, private_dir: Path, logger: Any):
        env, args, cred = real_clone_env(url, token, private_dir, logger)
        if cred is not None:
            seen_files.append(cred)
        return env, args, cred

    token = secrets.token_hex(20)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gitops, "_clone_env", watching)
        why = gitops.fetch_base_history(lane.checkout, url=URL, base_sha=lane.base,
                                        private_dir=lane.private, token=token)
    # The control: the rewrite really is live, so the fetch reached the origin.
    assert why is None
    assert graphstage.is_ancestor(lane.checkout, lane.main[3], lane.base) is True
    # MUTATION: holding the token despite the rewrite writes a credential file
    # for a fetch whose destination the repository's config chose.
    assert seen_files == []
    assert _files_holding(tmp_path, token) == []


# --------------------------------------------------------------------------
# a failure is a reason, never the attempt's
# --------------------------------------------------------------------------


def test_a_failed_token_fetch_leaves_a_reason_and_no_file(tmp_path):
    lane = _Lane(tmp_path)
    binary, log = _recorder(tmp_path, exit_code=128)
    token = secrets.token_hex(20)

    why = gitops.fetch_base_history(lane.checkout, url=URL, base_sha=lane.base,
                                    private_dir=lane.private, token=token,
                                    git_binary=str(binary))

    # MUTATION: a raised GitError instead of a returned reason ends staging.
    assert why == "the history fetch failed (git exit 128)"
    # MUTATION: removing the file only on success leaves it after a failure.
    assert len(json.loads(log.read_text())["files"]) == 1
    assert _files_holding(tmp_path, token) == []


def test_a_token_read_that_raises_is_a_reason_and_the_attempt_goes_on(db, store, tmp_path,
                                                                       monkeypatch):
    lane = _Lane(tmp_path)
    _no_ab(db)
    _promote(db, store, lane.main[4])
    asked: list[dict[str, Any]] = []

    def capture(checkout: Path, **kwargs: Any) -> str | None:
        asked.append(kwargs)
        return "the history fetch failed (git exit 128)"

    def unreadable() -> str | None:
        raise PermissionError("secret manager said no")

    monkeypatch.setattr(gitops, "fetch_base_history", capture)
    env = graphstage.prepare(lane.checkout.parent, profile_name="claude-code",
                             runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                             db=db, store=store, tenant_id=TENANT, repository_url=URL,
                             task_id="task_b", checkout=lane.checkout, base_sha=lane.base,
                             private_dir=lane.private, forge_token=unreadable)

    # MUTATION: a raising read aborting the fetch -> no anonymous try, and a
    # public repository loses its proof to a Secret Manager blip.
    assert len(asked) == 1 and asked[0]["token"] is None
    record = json.loads((lane.checkout.parent / graphstage.CONTEXT_DIR_NAME /
                         graphstage.STAGE_RECORD_NAME).read_text())
    # MUTATION: the read's failure not named leaves "git exit 128" alone,
    # which reads as a repository problem.
    assert record["staged"] is False
    assert "forge token not read: PermissionError" in record["reason"]
    # The exception's text (which may name a secret's path) is not recorded.
    assert "secret manager said no" not in record["reason"]
    assert graphstage.UNSTAGED_ENV in env
