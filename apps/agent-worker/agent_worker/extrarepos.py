"""The repositories a step reads beside its own: cloned, linked, never carried back.

docs/design/multi-repository-steps.md §4.4-§4.6 (MULTI-REPO, #1434), the
owner's decisions on PR 1461 (2026-10-17). A task may carry
`additional_repositories` (contract request 95): registered repositories of
its tenant, each pinned by swarm-api at submission to a 40-hex `commit` and
covered by the spec signature (format 4, request 96). This module puts each
one on disk, read-only, before the agent starts:

  1. **Grant.** For an entry cloned with a person's user slot, the person's
     grant on THAT repository is read again by id (`secrets.grant_refusal`,
     `write=False`). A grant deleted since submission ends the attempt
     `additional_repository_not_granted`, terminally: the next attempt reads
     the same grant. Every grant is read before any clone starts, so a
     revoked one costs no clone.
  2. **Clone.** `gitops.clone_at_commit` at the signed commit, depth 1, HEAD
     verified equal to it, into `<cache>/repos/<name>` -- the attempt's
     cache directory, on GKE the pod's own cache volume (`cachedirs`), never
     the workspace volume. The forge token follows the primary clone's path
     exactly: read from Secret Manager at the clone (`token_for`, the entry's
     own signed slot), written only to a credentials file in a per-entry
     directory under `private/`, and that file removed in the clone's
     `finally`; the per-entry directory is removed after it as well. Never in
     an environment, a log line or a checkpoint. At most
     `MAX_PARALLEL_CLONES` run at once.
  3. **Size.** Each checkout is measured on disk (`workspace.tree_bytes`).
     A total over the platform's budget ends the attempt
     `additional_repositories_too_large`, terminally: the forge's size at
     submission was an underestimate, and the next attempt meets the same
     wall.
  4. **Read-only signal.** The push URL is set to one no transport serves and
     write permission is removed from the tree. A SIGNAL, not a boundary: the
     agent runs as the same uid and can undo both. The boundary is that the
     harvest, the publish and every checkpoint read `work/repo` only and
     leave `work/repos` out (§4.5).
  5. **Layout.** `work/repos/` is a real directory of one link per entry to
     its checkout, and `work/.swarm-context/repositories.json` lists
     `{name, repository, ref, commit, path}`, so the agent learns the layout
     from the platform rather than by guessing.

A CLONE THAT FAILS fails the attempt `clone_failed`, retryably, naming the
entry's `name` and never its URL or git's text (which may quote it). The
agent never starts with a partial set: a step that asked for four
repositories and silently got three would answer with false confidence.

A RESUMED ATTEMPT has a new attempt id, hence a new empty cache directory,
and clones the same signed commits again; a checkpoint never carries them.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from swarm_common.models import check_additional_repositories

from .gitops import GitError, GitTransient, clone_at_commit, retry_clone
from .workspace import REPOS_DIR_NAME, Workspace, tree_bytes, walk_tree

#: `<cache>/repos/<name>`: where each checkout lands. On the cache volume, so
#: neither the checkpoint's walk of `work/` nor the artifact upload reaches it.
CACHE_REPOS_DIR = "repos"

#: `work/.swarm-context/repositories.json`: what the agent is told it has.
#: The directory is the context pack's (`graphstage.CONTEXT_DIR_NAME`).
CONTEXT_DIR_NAME = ".swarm-context"
CONTEXT_FILE_NAME = "repositories.json"

#: The agent's environment names `work/repos` here (`Worker._build_child_env`).
REPOS_DIR_ENV = "SWARM_REPOS_DIR"

#: The runner input's key for the same list as `repositories.json`.
INPUT_KEY = "additional_repositories"

#: At most this many clones at once (§4.4 step 3): the forge is one host, and
#: the egress path is shared with the primary's tool installs.
MAX_PARALLEL_CLONES = 3

#: The platform setting the size budget is read from, and its default. The
#: budget is a PLATFORM SETTING an admin edits at run time (owner, 2026-10-17),
#: stored beside the platform's other settings in `control/settings`
#: (`swarm_api.store.SETTINGS_DOC`); swarm-api enforces it on the forge's
#: estimate at submission, and the worker on the real figure here. The
#: default is the design's 6 GiB of the 20 GiB cache volume (§4.6, §7 Q5),
#: which leaves 14 GiB for the tool caches and TMPDIR.
CONTROL_COLLECTION = "control"
SETTINGS_DOC = "settings"
BUDGET_SETTING = "additional_repositories_budget_gib"
DEFAULT_BUDGET_GIB = 6
_GIB = 1024 ** 3

#: What `git push` from an additional checkout is pointed at: no transport
#: serves the scheme, so a push fails before any connection is made.
READ_ONLY_PUSH_URL = "read-only-additional-repository://no-push"

#: The causes this module ends an attempt with (worker vocabulary, as
#: `lifecycle.FORGE_GRANT_REVOKED`; the task's end cause is CANNOT_START).
NOT_GRANTED = "additional_repository_not_granted"
GRANT_UNREADABLE = "additional_repository_grant_unreadable"
TOO_LARGE = "additional_repositories_too_large"
CLONE_FAILED = "clone_failed"
#: No cache volume of its own: the attempt runs where the only disk is the
#: workspace (Cloud Run's memory tmpfs). swarm-api refuses these profiles at
#: submission (§4.3); this is the worker's own refusal of a task that got
#: here anyway, rather than filling the workspace with several codebases.
NO_CACHE_VOLUME = "additional_repositories_unsupported_profile"


class Refused(Exception):
    """The additional repositories cannot be put on disk; the agent must not start.

    `cause` is one of this module's causes; `reason` names entries by their
    `name` only, never a URL or a token. `retryable` says whether the next
    attempt could meet a different answer.
    """

    def __init__(self, cause: str, reason: str, *, retryable: bool) -> None:
        super().__init__(f"{cause}: {reason}")
        self.cause = cause
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class Checkout:
    """One additional repository on disk, as the agent is told of it."""

    name: str
    repository: str
    ref: str
    commit: str
    path: str
    size_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repository": self.repository,
            "ref": self.ref,
            "commit": self.commit,
            "path": self.path,
        }


def entries_of(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The task's `additional_repositories`, shape-checked, or [] for none.

    Checked again here with the frozen contract's own check: the verified
    document is the only source, but a malformed list -- which no writer
    produces -- must not reach a path join or a git argument.
    """
    value = task.get(INPUT_KEY)
    try:
        check_additional_repositories(value)
    except ValueError as exc:
        raise Refused(CLONE_FAILED, f"the task's additional repositories are malformed: {exc}",
                      retryable=False) from None
    return [dict(entry) for entry in value or []]


def budget_bytes(db: Any, *, logger: Any, call_options: Mapping[str, Any] | None = None) -> int:
    """The platform's size budget for one step's additional checkouts, in bytes.

    Read from `control/settings` at every call, never cached, so an admin's
    change reaches the next attempt. The default when the setting is absent,
    malformed, or cannot be read -- and for a tenant switched to the state
    routes, which builds no Firestore client and has no route serving the
    platform settings (`indexrun.state_routes`) -- said in the log.
    """
    from .indexrun import state_routes  # lazy, as `indexrun.state_routes` imports lazily

    default = DEFAULT_BUDGET_GIB * _GIB
    if db is None or state_routes(db) is not None:
        logger.info("additional repositories: the default size budget applies",
                    budget_gib=DEFAULT_BUDGET_GIB, why="no platform settings read")
        return default
    try:
        snap = db.collection(CONTROL_COLLECTION).document(SETTINGS_DOC).get(
            **dict(call_options or {})
        )
        data = (snap.to_dict() or {}) if snap.exists else {}
    except Exception as exc:  # noqa: BLE001 -- said by type, never fatal
        logger.warning("additional repositories: the platform settings could not be read; "
                       "the default size budget applies", error=type(exc).__name__,
                       budget_gib=DEFAULT_BUDGET_GIB)
        return default
    value = data.get(BUDGET_SETTING)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        logger.warning("additional repositories: the size budget setting is malformed; "
                       "the default applies", budget_gib=DEFAULT_BUDGET_GIB)
        return default
    return int(value * _GIB)


def prepare(
    *,
    ws: Workspace,
    entries: list[dict[str, Any]],
    grant_refusal: Callable[[dict[str, Any]], str | None],
    token_for: Callable[[dict[str, Any]], str | None],
    budget: int,
    logger: Any,
    timeout_seconds: int,
    max_wait_seconds: float,
    remaining_seconds: Callable[[], float],
    sleep: Callable[[float], Any],
    egress: Any = None,
) -> list[Checkout]:
    """Put every entry on disk, read-only, and link it under `work/repos`.

    Returns one `Checkout` per entry, in the task's order, or raises `Refused`
    with nothing linked under `work/` and no credential file left behind.
    `grant_refusal(entry)` returns why the submitter's grant no longer covers
    the entry, or None; an exception from it is an unreadable grant.
    `token_for(entry)` reads the entry's forge token, fresh, at its clone.
    """
    if not entries:
        return []
    if ws.cache is None or not ws.cache_on_own_volume():
        raise Refused(
            NO_CACHE_VOLUME,
            "this attempt has no cache volume of its own to clone additional "
            "repositories into; only GKE agent profiles take them",
            retryable=False,
        )
    # 1. Every grant, before any clone.
    for entry in entries:
        try:
            refused = grant_refusal(entry)
        except Exception as exc:  # noqa: BLE001 -- a read failure, said by type
            raise Refused(
                GRANT_UNREADABLE,
                f"the submitter's grant on additional repository {entry['name']!r} "
                f"could not be read ({type(exc).__name__})",
                retryable=True,
            ) from None
        if refused is not None:
            raise Refused(
                NOT_GRANTED,
                f"additional repository {entry['name']!r}: {refused}",
                retryable=False,
            )

    root = ws.cache / CACHE_REPOS_DIR
    release_tree(root)
    _remove(root)
    root.mkdir(mode=0o700, parents=True)
    private_root = ws.private / CACHE_REPOS_DIR
    logs_root = ws.logs / CACHE_REPOS_DIR

    def clone_one(entry: dict[str, Any]) -> tuple[float, Exception | None]:
        name = entry["name"]
        destination = root / name
        destination.mkdir(mode=0o700)
        private_dir = private_root / name
        logs_dir = logs_root / name
        logs_dir.mkdir(parents=True, exist_ok=True)
        ref = entry["ref"]
        # A ref that is itself a sha names no branch to fall back on.
        branch = None if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref) else ref
        try:
            result = retry_clone(
                lambda: clone_at_commit(
                    url=entry["repository_url"],
                    branch=branch,
                    commit=entry["commit"],
                    destination=destination,
                    private_dir=private_dir,
                    logs_dir=logs_dir,
                    timeout_seconds=timeout_seconds,
                    logger=logger,
                    token=token_for(entry),
                    egress=egress,
                ),
                destination=destination,
                max_wait_seconds=max_wait_seconds,
                remaining_seconds=remaining_seconds,
                logger=logger,
                sleep=sleep,
            )
            return float(result.duration_seconds), None
        except (GitError, GitTransient, OSError) as exc:
            return 0.0, exc
        finally:
            # `clone_at_commit` removed its credentials file in its own
            # `finally`; the whole per-entry directory goes too, traces and all.
            _remove(private_dir)

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CLONES,
                            thread_name_prefix="extra-clone") as pool:
        outcomes = list(pool.map(clone_one, entries))
    _remove(private_root)

    for entry, (seconds, failed) in zip(entries, outcomes):
        if failed is not None:
            release_tree(root)
            _remove(root)
            # The class only: git's own text can quote the URL.
            raise Refused(
                CLONE_FAILED,
                f"additional repository {entry['name']!r} could not be cloned at its "
                f"signed commit ({type(failed).__name__})",
                retryable=True,
            )
        logger.info("additional repository cloned at its signed commit",
                    name=entry["name"], commit=entry["commit"], seconds=round(seconds, 2))

    # 3. The real figure, against the platform's budget.
    sizes = [tree_bytes(root / entry["name"]) for entry in entries]
    total = sum(sizes)
    if total > budget:
        release_tree(root)
        _remove(root)
        raise Refused(
            TOO_LARGE,
            f"the additional repositories take {total} bytes on disk, over the "
            f"platform's budget of {budget} bytes",
            retryable=False,
        )

    # 4. The read-only signal, then 5. the layout.
    for entry in entries:
        _mark_read_only(root / entry["name"], ws.private, logger)
    links = ws.work / REPOS_DIR_NAME
    _remove(links)
    links.mkdir(mode=0o700)
    checkouts: list[Checkout] = []
    for entry, size in zip(entries, sizes):
        link = links / entry["name"]
        os.symlink(root / entry["name"], link, target_is_directory=True)
        checkouts.append(Checkout(
            name=entry["name"], repository=entry["repository"], ref=entry["ref"],
            commit=entry["commit"], path=str(link), size_bytes=size,
        ))
    context = ws.work / CONTEXT_DIR_NAME
    if context.is_symlink() or context.is_file():
        context.unlink()
    context.mkdir(mode=0o700, parents=True, exist_ok=True)
    (context / CONTEXT_FILE_NAME).write_text(
        json.dumps([c.as_dict() for c in checkouts], indent=2) + "\n", encoding="utf-8"
    )
    logger.info("additional repositories ready", count=len(checkouts), total_bytes=total,
                budget_bytes=budget)
    return checkouts


def child_env(ws: Workspace, checkouts: list[Checkout]) -> dict[str, str]:
    """`SWARM_REPOS_DIR` when the attempt has additional checkouts, else nothing."""
    return {REPOS_DIR_ENV: str(ws.work / REPOS_DIR_NAME)} if checkouts else {}


def release(ws: Workspace) -> None:
    """Give write permission back to the checkouts' folders, so teardown can remove them.

    `prepare` took it away (a signal to the agent); `workspace.destroy` cannot
    unlink a file in a folder it may not write. Best effort, never raises.
    """
    if ws.cache is not None:
        release_tree(ws.cache / CACHE_REPOS_DIR)


def release_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        return
    for dirpath, _dirnames, _filenames in walk_tree(root):
        try:
            mode = os.lstat(dirpath).st_mode
            if stat.S_ISDIR(mode):
                os.chmod(dirpath, stat.S_IMODE(mode) | stat.S_IWUSR)
        except OSError:
            continue


def _mark_read_only(checkout: Path, private: Path, logger: Any) -> None:
    """Point the push URL nowhere and take write permission off the tree."""
    try:
        subprocess.run(
            ["git", "-C", str(checkout), "config", "remote.origin.pushurl", READ_ONLY_PUSH_URL],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private),
                 "GIT_CONFIG_NOSYSTEM": "1"},
            check=True, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("additional repository: the push URL could not be cleared",
                       name=checkout.name, error=type(exc).__name__)
    folders: list[str] = []
    for dirpath, _dirnames, filenames in walk_tree(checkout):
        folders.append(str(dirpath))
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                mode = os.lstat(path).st_mode
                if not stat.S_ISLNK(mode):
                    os.chmod(path, stat.S_IMODE(mode) & ~0o222)
            except OSError:
                continue
    # Folders after their files, deepest first.
    for folder in reversed(folders):
        try:
            os.chmod(folder, stat.S_IMODE(os.lstat(folder).st_mode) & ~0o222)
        except OSError:
            continue


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, ignore_errors=True)
