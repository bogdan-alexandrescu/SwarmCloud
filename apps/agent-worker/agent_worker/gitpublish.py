"""The git publish: harvest what the agent left in its checkout and, when the
forge allows it, publish it -- the clean repository, the fold onto the branch
tip, the credential scan, the push and the pull request.

Moved out of `lifecycle.py` unchanged (issue 68, a slice of splitting that file
by concern): `Worker` inherits `GitPublishMixin`, so the methods keep their
`self` access and every `worker._publish_git(...)` call and test still
resolves. The names `lifecycle` defines, and the ones a test patches there
(`push_branch`, `open_pull_request`, `prepare_publish_repo`, `final_tree_leak`
and the other git helpers), stay bound in `lifecycle` and are imported where
they are used, at call time, so a `monkeypatch.setattr(lifecycle, ...)` of one
still reaches the publish: a module-level import of `lifecycle` from here would
be circular. Everything else is imported here from where it is defined.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

from . import artifact_manifest as manifest_mod
from . import children as children_mod
from . import publishpr as publishpr_mod
from . import territory as territory_mod
from .errors import FencedError, WorkerError
from . import forge as forge_mod
from .forge import ForgeError, PullRequest, RepoAccess
from .forge import PullHead
from .gitops import (
    EMPTY_CLONE_BASE,
    GitError,
    MergedHeadUnreadable,
    MergeOutcome,
    PublishFetchIncomplete,
    PushRejected,
    PushUnreachable,
    base_merge_tip,
    moved_branch_tip,
    keep_work_bundle,
    WORK_BUNDLE_NAME,
    read_agent_excludes,
    read_head_as_data,
    replay_onto_tip,
    summarize_work,
    verify_merged_default_branch,
)
from .gitops import _NO_HOOKS, _git_text


class GitPublishMixin:
    """`Worker`'s git harvest and publish (`Worker._harvest_git`,
    `Worker._publish_git`), moved from `lifecycle.py` by issue 68."""

    # What `lifecycle.Worker` (or another mixin) supplies to these methods;
    # annotations only (no value, so nothing is bound at runtime), so the type
    # checker resolves them, as `publish_retry.PublishRetryMixin` does.
    _adopt_rebase_base: Any
    _adopt_tool_pushed_tip: Any
    _agent_pull_request_text: Any
    _auto_commit_undid: Any
    _base_pin: Any
    _carrier_fold_onto_tip: Any
    _check_pull_before_push: Any
    _clone_base: Any
    _clone_commit: Any
    _conflict_round: Any
    _default_branch_tip: Any
    _dispatch_carrier: Any
    _dispatch_integrates: Any
    _dispatch_role: Any
    _dispatch_strategy: Any
    _fetch_incomplete_marker: Any
    _forge_holder: Any
    _forge_retry: Any
    _forge_write_refusal: Any
    _generated_pull_request_title: Any
    _git_token: Any
    _git_token_refusal: Any
    _head_moved: Any
    _hidden_checkout_names: Any
    _integrates_with_changes: Any
    _is_default_title: Any
    _keep_moved_head: Any
    _leaks_in_added_text: Any
    _moved_head_result: Any
    _no_change_contributors: Any
    _open_pull_request_reading_again: Any
    _opens_pull_request: Any
    _origin_object_prober: Any
    _post_territory_comment: Any
    _pr_role: Any
    _pr_start_head: Any
    _prepublish_advisory: Any
    _prepublish_section: Any
    _probe_reading_again: Any
    _publish_config_seals: Any
    _publish_continues_a_branch: Any
    _publish_identity: Any
    _publish_is_ancestor: Any
    _publish_rev: Any
    _pull_request_amendment: Any
    _pull_request_body: Any
    _push_branch: Any
    _push_reading_again: Any
    _push_through_broker: Any
    _push_unreachable_marker: Any
    _read_clone_base: Any
    _read_only_contributors: Any
    _read_publish_remote_tip: Any
    _record_forge_credential: Any
    _record_pushed_branch: Any
    _repo_url: Any
    _scan_floor: Any
    _scan_overlap: Any
    _scrub: Any
    _task: Any
    _territory_warning: Any
    _title_carries_task_id: Any
    _unreachable_marker: Any
    _verdict: Any
    _worker_commit_message: Any
    _workflow_broker: Any
    _workflow_paths: Any
    _workflow_push_refused: Any
    cfg: Any
    checkpoints: Any
    control: Any
    log: Any
    reap_before_publish: Any
    ws: Any

    # -- git: harvest, and publish when the forge allows it -----------------
    def _harvest_git(
        self, *, publish: bool, withheld: str = "", defer: bool = False
    ) -> dict[str, Any] | None:
        """Describe the agent's changes, and publish them when permitted.

        Returns the dict that becomes `result_summary["git"]`, or None when
        there is no repository to describe. It NEVER raises: an attempt that
        ran is not a failed attempt because its epilogue could not talk to
        GitHub, and this runs on the teardown path where the lease is about to
        be released either way. Every failure becomes a readable string in the
        returned dict instead.

        `publish` is False on all three park paths. A parked attempt resumes
        from its checkpoint and will reach the terminal path later, so pushing
        a branch and opening a pull request for work that is still in progress
        would put a half-finished change in front of a reviewer -- and do it
        again on every quota bounce. A park passes nothing and is described as
        one.

        `defer` is `_finalise`'s (archived issue 165): everything up to the publish runs --
        the reap, the clean repository, the patch -- and the publish is left
        in `_deferred_publish` for `_publish_checked`, which makes it only
        after the upload manifest has passed the missing-output check. So an
        attempt that check fails, retryably or not, has pushed nothing.
        """
        from .lifecycle import PATCH_NAME, PUBLISH_UNREACHABLE_FIELD, REPO_DIR_NAME
        ws = self.ws
        cfg = self.cfg
        if ws is None or not cfg.git_harvest_enabled:
            return None
        repo = ws.work / REPO_DIR_NAME
        if not (repo / ".git").exists():
            return None

        base = self._clone_base or self._read_clone_base()
        out: dict[str, Any] = {"base": base}
        if self._clone_commit:
            out["clone_commit"] = self._clone_commit
        if self._base_pin is not None:
            out["base_pin"] = dict(self._base_pin)
        if base is None and self._publish_base == EMPTY_CLONE_BASE:
            out["note"] = (
                "the repository was empty when it was cloned, so there is no "
                "base to diff against; uncommitted files are still listed"
            )
        elif base is None:
            out["note"] = (
                "the clone base is unknown, so no diff could be computed; "
                "uncommitted files are still listed"
            )

        # THE HARVEST RUNS NO GIT IN THE CLONE EITHER (archived pull request 259, owner item 3).
        # `summarize_work` used to take its patch with `git diff <base>` in the
        # clone, and a diff of the working tree reads each file through the
        # clean filter the clone's `.gitattributes` and `.git/config` name --
        # the agent's program, run as the worker. So the order is now:
        #
        #   1. REAP. No process the agent started is alive from here on
        #      (`reap_before_publish`); if one survives, nothing below runs.
        #      This used to happen only inside `_publish_git`; it moves here
        #      because the clean repository is built here, and a live agent
        #      process could poison it (see `_publish_git`).
        #   2. BUILD the worker's clean repository: the agent's commits are
        #      fetched in (`prepare_publish_repo`) and its working tree copied
        #      in (`mirror_worktree`), with the patterns it excluded read as
        #      data.
        #   3. HARVEST there: the patch, the commit list and the dirty list
        #      are read from the worker's repository, where a `.gitattributes`
        #      naming a filter is data.
        #   4. PUBLISH from the same repository (`_publish_git`), which
        #      commits the uncommitted work on top and pushes.
        #
        # The clone itself is read only by the object-checked fetch, whose
        # `upload-pack` runs with every program-naming key pinned
        # (`gitops._UPLOAD_PACK_ENV`).
        survivors = self.reap_before_publish()
        if survivors:
            reason = (
                f"refusing to publish: {len(survivors)} process(es) the agent started "
                f"are still alive after the pre-publish reap ({', '.join(str(p) for p in survivors[:10])}); "
                "the tenant credential is not put in hand while agent code can run"
            )
            self.log.error(
                "not harvesting or publishing: agent processes survived the reap",
                surviving_pids=list(survivors),
            )
            out["published"] = False
            out["publish_reason"] = reason
            out["error"] = (
                "the agent's work was not harvested: processes it started survived "
                "the reap, and the worker reads no repository while agent code can run"
            )
            return out

        # The floor the clean repository is built on: the worker's own record
        # when it has one, else the harvest's base (the workspace marker, on a
        # resume from a checkpoint that recorded none -- the publish refuses
        # that case before it pushes, but the work is still described). A floor
        # that is not a full sha is unknown, and then only HEAD is fetched.
        floor = self._publish_base if self._publish_base is not None else base
        fetched_base = (
            floor if floor and re.fullmatch(r"[0-9a-f]{40}", floor) else None
        )
        try:
            publish_repo = self._build_clean_repo(repo, floor=floor, allow_unknown_base=True)
            # A lane the runner rebased onto the base's newer tip (SPEED-D1)
            # is described, scanned and published from where it landed.
            rebased = self._adopt_rebase_base(publish_repo, self._publish_base)
            if rebased is not None:
                out["rebased_from"] = self._publish_base
                self._checkpointed_base = self.checkpoints.clone_base
                out["rebased_onto"] = rebased
                self._publish_base = rebased
                self.checkpoints.clone_base = rebased
                fetched_base = rebased
            # ONLY WHAT THIS STEP MADE ON TOP OF ITS START IS ITS WORK (archived issue 2094).
            # A review that checked out the branch it reviewed used to have
            # that branch's commits counted here and published as its own
            # pull request (peer-app archived pull request 456, archived pull request 465). A HEAD that left the
            # step's branch, or no longer descends from where it started, is
            # described as moved and nothing on it is harvested or published.
            moved = self._head_moved(repo, publish_repo, fetched_base)
            if moved is not None:
                kept = self._keep_moved_head(publish_repo, fetched_base)
                return self._moved_head_result(out, moved, bundle=kept)
            work = summarize_work(
                repo=publish_repo,
                base=fetched_base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                patch_path=ws.artifacts / PATCH_NAME,
                max_patch_bytes=cfg.max_patch_bytes,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
                # `_publish_base`, the worker's own record, not the workspace
                # marker the agent can write: only a clone the worker itself
                # saw land on nothing counts every commit as the agent's.
                empty_base=base is None and self._publish_base == EMPTY_CLONE_BASE,
            )
        except GitError as exc:
            self.log.warning("could not harvest the agent's git changes", error=str(exc))
            out["error"] = self._scrub(str(exc)[:500])
            if publish or defer:
                # Fails closed, and says so where a reader of the publish looks.
                out["published"] = False
                out["publish_reason"] = f"nothing was published: {out['error']}"
                if isinstance(exc, PublishFetchIncomplete):
                    # A partial clone's publish fetch (issue 367): the attempt
                    # ends retryably with the work kept below, and the retry
                    # checks this head and base before it republishes.
                    try:
                        clone_head: str | None = read_head_as_data(repo)
                    except GitError:
                        clone_head = None
                    out[PUBLISH_UNREACHABLE_FIELD] = self._fetch_incomplete_marker(
                        exc, work_head=clone_head
                    )
            # THE WORK IS STILL RECOVERABLE (archived issue 2544, owner decision 2026-10-28).
            # The publish-fetch that failed here took the patch with it: TOKEN-A
            # and U01 lost their integrator's whole run to it. The patch is the
            # agent's TREE against the base, so it is taken from a repository
            # holding the base alone, whatever the clone's history lacks.
            out.update(self._harvest_tree_patch(repo, fetched_base))
            # AND THE BRANCH ITSELF, MERGES INCLUDED (archived issue 3174): the patch is the
            # final tree flat, so a re-dispatch or a hand push from it redid
            # the merge the agent had made. Named in the failure text, so a
            # reader of the failure finds it.
            out.update(self._keep_work_bundle(
                repo, EMPTY_CLONE_BASE if floor == EMPTY_CLONE_BASE else fetched_base
            ))
            if out.get("bundle"):
                kept = (f"; the agent's branch, merge commits included, is kept in "
                        f"{WORK_BUNDLE_NAME} ({out['bundle_commits']} commits on "
                        f"{out['bundle_base']})")
                out["error"] += kept
                if "publish_reason" in out:
                    out["publish_reason"] += kept
            return out

        out.update(
            {
                "head": work.head,
                "commits": [
                    {
                        "sha": c.sha,
                        "subject": self._scrub(c.subject[:200]),
                        "author": c.author,
                        "committed_at": c.committed_at,
                        "files_changed": c.files_changed,
                        "insertions": c.insertions,
                        "deletions": c.deletions,
                        "binary_files": c.binary_files,
                    }
                    for c in work.commits[:100]
                ],
                "commit_count": len(work.commits),
                "insertions": work.insertions,
                "deletions": work.deletions,
                "dirty": [self._scrub(path) for path in work.dirty],
                "dirty_count": len(work.dirty),
                "dirty_truncated": work.dirty_truncated,
                "patch": work.patch_name,
                "patch_bytes": work.patch_bytes,
                "patch_omitted": work.patch_omitted,
            }
        )
        if work.files is not None:
            # Absent rather than [] when there was no base to diff against:
            # [] would claim a measured empty diff.
            out["files"] = [f.as_record(self._scrub) for f in work.files]
            out["files_truncated"] = work.files_truncated
        if work.own_files is not None:
            # The paths this step's own commits and uncommitted work changed,
            # a merge of main or of another branch left out (archived issue 2337): what a
            # review's rendered gate judges. Absent when not measured, so the
            # gate falls back to `files` and the patch rather than to [].
            out["own_files"] = [self._scrub(path) for path in work.own_files]
            out["own_files_truncated"] = work.own_files_truncated
        if work.patch_omitted:
            out["patch_note"] = (
                f"the diff was {work.patch_bytes} bytes, over the "
                f"{cfg.max_patch_bytes} cap, and was discarded rather than "
                "truncated -- a truncated patch applies cleanly and silently "
                "drops the rest of the change"
            )

        # AN INTEGRATOR IS THE ONE STEP WHOSE DELIVERABLE IS NOT ITS OWN WORK.
        #
        # For every other step "changed nothing" means there is nothing to
        # push and nothing to open, and returning here is right. For an
        # integrator it meant the opposite of what the caller was promised: a
        # step whose agent edited no files -- an entirely ordinary outcome for
        # one whose prompt is "bring these together" -- returned before
        # `_publish_git` ran, so no contributor branch was ever merged, no
        # branch was pushed, and the ONE pull request `integrate` promises was
        # never opened. Zero, for a workflow whose contributors had all already
        # run, been billed and pushed their branches.
        #
        # It was invisible because it depended on whether the integrator's
        # agent happened to touch a file. Found by running the strategy against
        # a real repository, not by reading it.
        # A SHUT VERDICT GATE IS THE SAME KIND OF EXCEPTION (PUB-SKIP,
        # 2026-10-12). Its agent never ran, so "changed nothing" is the gate's
        # doing, and the reviewed work is the branch it cloned: a step that
        # owes a pull request still pushes that branch and opens it, and
        # `_skipped_publish_missing` fails the step if it could not.
        if (
            work.is_empty
            and not self._integration_is_pending()
            and not self._shut_gate_owes_publish()
        ):
            out["published"] = False
            out["publish_reason"] = "the agent changed nothing in the repository"
            return out

        if defer:
            self._deferred_publish = {
                "repo": repo, "work_head": work.head, "publish_repo": publish_repo,
            }
            return out
        out.update(
            self._publish_git(
                repo=repo, work_head=work.head, publish=publish, withheld=withheld,
                publish_repo=publish_repo,
            )
        )
        return out

    def _publish_checked(self, summary: dict[str, Any], withheld: str | None) -> None:
        """The publish `_harvest_git` deferred, made now that the check has run (archived issue 165).

        `withheld` is `_publish_withheld`'s answer, None when this attempt may
        publish. Without a deferred publish -- no repository, a harvest that
        returned early (a survivor of the reap, a git error, nothing changed)
        -- the harvest already said why nothing was published, and this does
        nothing. The result is scrubbed into `summary["git"]` like the rest
        of the summary. Raises only `FencedError` (a fence read before a
        forge write, archived issue 2693); otherwise the epilogue cannot fail the attempt.
        """
        from .lifecycle import PUBLISH_FAILED_FIELD
        deferred, self._deferred_publish = self._deferred_publish, None
        git = summary.get("git")
        if deferred is None or not isinstance(git, dict):
            return
        try:
            result = self._publish_git(
                repo=deferred["repo"],
                work_head=deferred["work_head"],
                publish=withheld is None,
                withheld=withheld or "",
                publish_repo=deferred["publish_repo"],
            )
        except FencedError:
            # A superseded attempt read the fence before a forge write
            # (`_own_before_forge_write`, archived issue 2693): not a failed publish but a
            # stale generation, which `run` stands down on.
            raise
        except Exception as exc:  # pragma: no cover - defensive; teardown path
            self.log.exception("the publish raised; continuing without it", exc)
            # Marked failed (G1, archived issue 1225): a step whose branch is its deliverable
            # did not push it, and `_publish_failed` ends it FAILED for that.
            result = {
                "published": False,
                "publish_reason": "the publish failed unexpectedly",
                PUBLISH_FAILED_FIELD: True,
            }
        git.update(self._scrub(result))
        # THE PATCH A REFUSED WORKFLOW PUBLISH STORED, IN THE MANIFEST (archived issue 2023).
        # `_write_workflow_patch` uploads it only now, after the folder upload
        # built `summary["artifacts"]`; added here, before the finish writes
        # the summary, so the manifest is still written once and lists every
        # file the attempt stored. A following step reads it from that list.
        patch = git.get("workflow_patch")
        artifacts = summary.get("artifacts")
        if isinstance(patch, dict) and isinstance(artifacts, list):
            grew = manifest_mod.add_late_entry(artifacts, patch)
            summary["artifact_bytes"] = int(summary.get("artifact_bytes") or 0) + grew
        # The push read the slot again: the kind it read joins the record.
        self._record_forge_credential(summary)

    def _integration_is_pending(self) -> bool:
        """True when this step still owes a merge even having changed nothing.

        Narrow on purpose: `integrate` AND the integrator role AND at least one
        upstream task to merge. A single-step `integrate` workflow that changed
        nothing has genuinely nothing to publish and keeps the short circuit
        above, and no other strategy is affected at all.
        """
        return (
            self._dispatch_strategy() == "integrate"
            and self._dispatch_role() == "integrator"
            and bool(self._dispatch_integrates())
        )

    def _shut_gate_owes_publish(self) -> bool:
        """True when this step's verdict gate kept its agent from running and
        the step owes a pull request (`_opens_pull_request`): the one ending
        whose only deliverable is the reviewed branch, published (PUB-SKIP)."""
        return (
            self._verdict is not None
            and not self._verdict.get("agent_ran", True)
            and self._opens_pull_request()
        )

    def _harvest_tree_patch(self, repo: Path, base: str | None) -> dict[str, Any]:
        """`swarm-work.patch` from the agent's final tree, for a harvest whose
        clean repository could not be built (archived issue 2544). Never raises.

        THE TREES, NOT THE COMMIT GRAPH. The archived issue 2559 shape -- a merge of a
        branch the agent fetched `--depth=N` -- fails the publish-fetch
        because the walk from HEAD crosses into history the clone never had.
        A patch needs none of that history: only the base's tree, which the
        clone holds because it was cloned at the base. So a second clean
        repository fetches the base ALONE (`work_head=base`, its shallow
        boundary the base, the same object checks and no confirmer), the
        agent's working tree is mirrored onto it exactly as the publish does,
        and `summarize_work` takes `git diff --binary <base>` there: committed
        and uncommitted work in one patch, untracked files included, under
        the same cap. `git apply` of it onto the base reproduces the agent's
        tree. No git runs in the clone, as everywhere in the harvest (archived pull request 259).

        Returns the patch fields for `result_summary["git"]`, with
        `patch_note` saying why when no patch was written.
        """
        from .lifecycle import PATCH_NAME, mirror_worktree, prepare_publish_repo
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        out: dict[str, Any] = {"patch": None, "patch_bytes": 0, "patch_omitted": False}
        if base is None:
            out["patch_note"] = (
                "the publish repository could not be built and the clone base is "
                "unknown, so there was no tree to diff the agent's work against"
            )
            return out
        try:
            base_repo = prepare_publish_repo(
                source_repo=repo,
                work_head=base,
                base=base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_clone_timeout_seconds,
                logger=self.log,
                local_branch="swarm-tree-patch",
            )
            mirror_worktree(
                source=repo,
                dest=base_repo,
                logger=self.log,
                hidden_names=self._hidden_checkout_names(repo),
                agent_excludes=read_agent_excludes(
                    clone=repo, workspace_root=ws.root, home=ws.work, logger=self.log
                ),
            )
            work = summarize_work(
                repo=base_repo,
                base=base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                patch_path=ws.artifacts / PATCH_NAME,
                max_patch_bytes=cfg.max_patch_bytes,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )
        except Exception as exc:  # noqa: BLE001 - the teardown path never raises
            reason = self._scrub(str(exc)[:300])
            self.log.warning("could not take the patch from the agent's tree", error=reason)
            out["patch_note"] = (
                "the publish repository could not be built, and the patch could not "
                f"be taken from the agent's tree either: {reason}"
            )
            return out
        out.update(
            {
                "patch": work.patch_name,
                "patch_bytes": work.patch_bytes,
                "patch_omitted": work.patch_omitted,
                # Where the patch came from: the tree, so there is no commit
                # list or own-files row beside it.
                "patch_from": "tree",
            }
        )
        if work.files is not None:
            out["files"] = [f.as_record(self._scrub) for f in work.files]
            out["files_truncated"] = work.files_truncated
        if work.patch_omitted:
            out["patch_note"] = (
                f"the diff was {work.patch_bytes} bytes, over the "
                f"{cfg.max_patch_bytes} cap, and was discarded rather than "
                "truncated -- a truncated patch applies cleanly and silently "
                "drops the rest of the change"
            )
        elif work.patch_name is None:
            out["patch_note"] = (
                "the agent's final tree is the clone base's tree, so there was "
                "no change to write a patch of"
            )
        self.log.info(
            "took the patch from the agent's tree after the publish repository failed",
            patch=work.patch_name, patch_bytes=work.patch_bytes,
        )
        return out

    def _keep_work_bundle(self, repo: Path, base: str | None) -> dict[str, Any]:
        """`swarm-work.bundle`, the agent's branch with its merge commits, for
        a harvest whose clean repository could not be built (archived issue 3174). Never
        raises. Built in a repository of the worker's own and scanned commit
        by commit with the publish's leak test (`gitops.keep_work_bundle`);
        under the patch's cap. Returns the `result_summary["git"]` fields:
        `bundle` (its artifact name, or None) and, when none was kept,
        `bundle_note` saying why."""
        from .lifecycle import _FULL_SHA_RE, _scan_diff_stream
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        git = ["git", *_NO_HOOKS]
        overlap = self._scan_overlap()

        def leaks(bundle_repo: Path, commits: list[str]) -> str | None:
            code, empty_tree = _git_text(
                [*git, "hash-object", "-t", "tree", "/dev/null"], repo=bundle_repo,
                private_dir=ws.private, logs_dir=ws.logs, slug="bundle-scan-empty",
                timeout_seconds=cfg.git_harvest_timeout_seconds, logger=self.log,
            )
            empty_tree = empty_tree.strip()
            if code != 0 or not _FULL_SHA_RE.fullmatch(empty_tree):
                raise GitError("could not name the empty tree")
            for sha in commits:
                code, parents = _git_text(
                    [*git, "rev-list", "--parents", "-n", "1", sha], repo=bundle_repo,
                    private_dir=ws.private, logs_dir=ws.logs, slug="bundle-scan-parents",
                    timeout_seconds=cfg.git_harvest_timeout_seconds, logger=self.log,
                )
                listed = parents.split()
                if code != 0 or not listed or listed[0] != sha:
                    raise GitError("could not read a bundled commit's parents")
                # The first parent, or the empty tree for a root (or a commit
                # the clone was cut off at): its whole tree is what it adds.
                before = listed[1] if len(listed) > 1 else empty_tree
                code, hit = _scan_diff_stream(
                    [
                        *git, "-c", "core.quotePath=false", "diff", "--no-color",
                        "--no-ext-diff", "--no-textconv", "--text", "--no-renames",
                        "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/", "-U0",
                        before, sha, "--",
                    ],
                    leaks=self._leaks_in_added_text, overlap=overlap, new_rev=sha, git=git,
                    repo=bundle_repo, private_dir=ws.private, logs_dir=ws.logs,
                    slug="bundle-scan-diff", timeout_seconds=cfg.git_harvest_timeout_seconds,
                    logger=self.log,
                )
                if code != 0:
                    raise GitError(f"could not diff commit {sha[:12]} against its parent")
                if hit is not None:
                    return sha
            return None

        kept = keep_work_bundle(
            source_repo=repo, base=base, private_dir=ws.private, logs_dir=ws.logs,
            bundle_path=ws.artifacts / WORK_BUNDLE_NAME, max_bytes=cfg.max_patch_bytes,
            timeout_seconds=cfg.git_harvest_timeout_seconds, logger=self.log, leaks=leaks,
        )
        out: dict[str, Any] = {"bundle": kept.name, "bundle_bytes": kept.bytes,
                               "bundle_omitted": kept.omitted}
        if kept.name is not None:
            out["bundle_commits"] = kept.commits
            out["bundle_base"] = kept.base
            out["bundle_ref"] = "swarm-work"
        else:
            out["bundle_note"] = self._scrub(str(kept.note or "no bundle was kept")[:500])
            self.log.warning("kept no bundle of the agent's branch", reason=out["bundle_note"])
        return out

    def _build_clean_repo(
        self, repo: Path, *, floor: str | None, allow_unknown_base: bool = False
    ) -> Path:
        """The worker's clean repository, holding the agent's commits and a copy
        of its working tree (archived pull request 259). Call only after the reap.

        `prepare_publish_repo` fetches the clone's HEAD (and `floor`) in with
        object checking; `mirror_worktree` copies the working tree in, with the
        worker's `./artifacts` link and the patterns the agent excluded
        (`read_agent_excludes`, read as data) hidden from git there. Nothing is
        committed yet: the harvest reads the uncommitted work from here, and
        `_publish_git` commits it here.
        """
        from .lifecycle import _config_seal, mirror_worktree, prepare_publish_repo
        ws = self.ws
        assert ws is not None
        cfg = self.cfg
        fenced: list[children_mod.ChildSubmitFenced] = []
        try:
            publish_repo = prepare_publish_repo(
                source_repo=repo,
                # The clone's HEAD: the agent's last commit, or none at all in an
                # empty repository it never committed in.
                work_head=None,
                base=floor,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_clone_timeout_seconds,
                logger=self.log,
                allow_unknown_base=allow_unknown_base,
                confirm_on_origin=self._origin_confirmer(fenced),
                # A merge the agent left in progress is concluded as a merge
                # by `commit_dirty`, never squashed (archived issue 3359).
                carry_merge=True,
                # What the relay did not answer, fetched from the forge by the
                # worker itself (archived issue 2901): a merge of the base publishes.
                fetch_on_origin=self._origin_line_fetcher(fenced),
                # Whether origin holds the object a partial clone's publish
                # fetch could not read, so the failure says (issue 367).
                probe_on_origin=self._origin_object_prober(fenced),
            )
        finally:
            # `prepare_publish_repo` reads a confirmer or an origin fetch that
            # raised as "nothing answered"; a fence is still the fence
            # (invariant 5).
            if fenced:
                raise fenced[0]
        mirror_worktree(
            source=repo,
            dest=publish_repo,
            logger=self.log,
            hidden_names=self._hidden_checkout_names(repo),
            agent_excludes=read_agent_excludes(
                clone=repo, workspace_root=ws.root, home=ws.work, logger=self.log
            ),
        )
        self._publish_config_seals[str(publish_repo)] = _config_seal(publish_repo)
        return publish_repo

    def _origin_line_fetcher(
        self, fenced: list[Any]
    ) -> Callable[[Path, list[str]], list[str]] | None:
        """The worker's own depth-1 fetch from the forge of the clone's shallow
        cut-offs nothing else answered (archived issue 2901), into the clean repository
        `prepare_publish_repo` hands it, or None when this worker may not.

        Only ever called there, after the reap, on a repository the worker
        initialised; the token is read at the call, and only when a cut-off
        is left over, so a publish the relay answered reads none. The gates
        are the publish's: a repository URL, and the memory protection
        `_git_token_refusal` asks for before any token is read. No token
        (a public repository, a read refused) still fetches, without one. A
        superseded attempt's `FencedError` is appended to `fenced` for the
        caller to raise (invariant 5), as `_origin_confirmer`'s is."""
        from .lifecycle import fetch_shallow_lines_from_origin
        url = self._repo_url or self.cfg.repository_url
        if not url or self._git_token_refusal():
            return None

        def fetch(repo: Path, shas: list[str]) -> list[str]:
            ws = self.ws
            assert ws is not None
            try:
                token = self._git_token()
            except FencedError as exc:
                fenced.append(exc)
                raise
            return fetch_shallow_lines_from_origin(
                repo=repo, url=url, token=token, shas=shas,
                private_dir=ws.private, logs_dir=ws.logs,
                timeout_seconds=self.cfg.git_clone_timeout_seconds, logger=self.log,
            )

        return fetch

    def _origin_confirmer(
        self, fenced: list[Any]
    ) -> Callable[[list[str]], list[str]] | None:
        """The question `prepare_publish_repo` puts to origin about the clone's
        `.git/shallow` lines (archived issue 2559): the attempt's git tools relay's signed
        `git_fetch` `confirm` (archived issue 2413), answered from the forge. None when this
        attempt is not offered the git tools; the boundary is then the
        recorded base alone, as before. A fence is appended to `fenced` for
        the caller to raise."""
        relay = self._workflow_broker()
        if relay is None:
            return None
        request_id = f"publish-confirm-{os.urandom(6).hex()}"

        def confirm(shas: list[str]) -> list[str]:
            try:
                return relay.confirm_on_origin(shas, request_id=request_id)
            except children_mod.ChildSubmitFenced as exc:
                fenced.append(exc)
                raise

        return confirm

    def _publish_onto_own_moved_head(
        self,
        publish_repo: Path,
        pull: PullHead,
        *,
        url: str,
        token: str,
        branch: str,
        protected: tuple[str, ...],
        default_branch: str | None,
        author_name: str,
        author_email: str,
        out: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Publish onto a pull request head moved only by this task's own
        earlier generation (archived issue 2693, task_ffc37f64 on archived pull request 2613); the sha the
        pull request's branch holds afterwards, and the token the steps after
        it use (`_push_reading_again`, archived issue 3223).

        A head whose tree is already this attempt's (the fenced generation
        pushed this same work) is published as it stands: nothing is pushed.
        Otherwise this attempt's commits are replayed onto it through the
        moved-tip path (`_push_onto_base_merge` with `expect_tip`: replay,
        authorship, leak re-scan, the fence, one fast-forward push leased on
        that head, never forced). A replay that leaves nothing over the head
        has nothing to push either.
        """
        from .lifecycle import BASE_MERGE_NO_CHANGE_FIELD
        ws = self.ws
        assert ws is not None
        trees = []
        for rev in ("HEAD", pull.head_sha):
            code, text = _git_text(
                ["git", *_NO_HOOKS, "rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}"],
                repo=Path(publish_repo), private_dir=ws.private, logs_dir=ws.logs,
                slug="publish-to-pr-own-tree",
                timeout_seconds=self.cfg.git_harvest_timeout_seconds, logger=self.log,
            )
            trees.append(text.strip() if code == 0 else "")
        out["publish_to_pr_own_moved_head"] = pull.head_sha
        if trees[0] and trees[0] == trees[1]:
            self.log.info(
                "the pull request's head moved only by this task's own earlier commits "
                "and already holds this attempt's tree; nothing to push",
                number=pull.number, head=pull.head_sha,
            )
            return pull.head_sha, token
        pushed, token = self._push_onto_base_merge(
            publish_repo,
            PushRejected(
                f"{publishpr_mod.HEAD_MOVED}: pull request #{pull.number}'s branch moved "
                f"off {pull.head_sha[:12]} while this step replayed onto it; nothing was "
                "pushed or forced"
            ),
            url=url, token=token, branch=branch, protected=protected,
            default_branch=default_branch, author_name=author_name,
            author_email=author_email, out=out, expect_tip=pull.head_sha,
        )
        if pushed is None:
            # The moved head already holds every change this attempt made.
            out.pop(BASE_MERGE_NO_CHANGE_FIELD, None)
            return pull.head_sha, token
        return pushed, token

    def _own_before_forge_write(self, write: str) -> None:
        """Refuse, with `FencedWriteRefused`, a forge write this attempt no longer owns (archived issue 2693).

        MEASURED: task_ffc37f64's generation 1 was fenced `dead_worker` at
        18:54:19 and pushed onto archived pull request 2613 at 18:54:35, because the publish
        pushed and opened pull requests without reading the fence (only the
        carrier push and the moved-tip replay did); generation 5's publish
        then found the head moved and failed. So every push, pull request
        create or update and comment the publish makes is preceded by this
        read, and a superseded attempt ends as a stale generation (exit 70,
        `_stand_down`) with nothing pushed or opened and its lease untouched
        (invariant 5). It is `ensure_owner`, the carrier push's read: it
        writes nothing.

        IT NARROWS THE WINDOW, IT CANNOT CLOSE IT: the fence can still land
        between this read and the forge's answer, and GitHub cannot be asked
        to check a Firestore document. The broker push's own 409 fence covers
        its path (`_push_through_broker`); a lease on the push covers a head
        that moved.
        """
        self.control.ensure_owner(write=write)

    def _push_over_own_earlier_attempt(
        self,
        publish_repo: Path,
        rejected: PushRejected,
        *,
        url: str,
        token: str,
        branch: str,
        protected: tuple[str, ...],
        author_name: str,
        author_email: str,
    ) -> tuple[str | None, str | None]:
        """Publish onto this task's branch when only its earlier attempt wrote it (archived issue 1438).

        Returns the pushed sha and the token that pushed it
        (`_push_reading_again`, archived issue 3223).

        MEASURED 2026-10-16: attempt 1 pushed `swarm/task_<id>`, then crashed
        before its terminal state landed; the reconciler reclaimed it, and
        attempts 2 and 3 published from a fresh publish repository at the
        same clone base, so their pushes were refused as non-fast-forwards
        against attempt 1's commit and the task failed with no pull request.

        When the refused branch is exactly `<prefix><this task id>` and every
        commit it adds over the clone base is this task's worker's
        (`gitops.own_branch_tip`), this attempt's HEAD replaces it with
        `--force-with-lease` to the sha just verified. Anything else -- a
        commit someone else wrote, a branch this task continues or amends, a
        tip that moved after the check -- re-raises, and the attempt fails as
        it always did.

        FORCE-WITH-LEASE, NOT A COMMIT ON TOP, AND WHY: the branch then holds
        exactly what a first-attempt push would have left -- this attempt's
        own history over the clone base, the one its final-tree scan and
        authorship check just read, its kept commits (a red-first tests
        commit among them) intact -- so the pull request that follows sees
        the branch it would have seen. A commit on the fetched head would
        squash this attempt's history into one commit and stack it on a tree
        this attempt never scanned. What the lease replaces is only commits
        proven this task's own, and it is refused if the branch moved since.
        """
        from .lifecycle import own_branch_tip, push_branch
        cfg = self.cfg
        ws = self.ws
        assert ws is not None
        tip, why = own_branch_tip(
            repo=publish_repo,
            url=url,
            branch=branch,
            token=token,
            task_id=cfg.task_id,
            base=self._publish_base,
            author_name=author_name,
            author_email=author_email,
            also_own=((cfg.git_author_name, cfg.git_author_email),),
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_clone_timeout_seconds,
            logger=self.log,
            branch_prefix=cfg.git_branch_prefix,
        )
        if tip is None:
            self.log.warning(
                "the push was refused and the branch is not this task's own earlier "
                "push; publishing nothing",
                branch=branch,
                reason=str(self._scrub(why)),
            )
            raise rejected
        self._own_before_forge_write("replacing push")
        pushed, token = self._push_reading_again(
            lambda with_token: push_branch(
                repo=publish_repo,
                url=url,
                branch=branch,
                token=with_token,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_clone_timeout_seconds,
                logger=self.log,
                branch_prefix=cfg.git_branch_prefix,
                protected=protected,
                lease=tip,
            ),
            token, branch=branch,
        )
        self.log.info(
            "the task's branch held only its earlier attempt's worker commits; "
            "replaced them with this attempt's (force-with-lease)",
            branch=branch,
            replaced=tip,
            head=pushed or "unknown",
            how="force_with_lease",
        )
        return pushed, token

    def _push_onto_base_merge(
        self,
        publish_repo: Path,
        rejected: PushRejected,
        *,
        url: str,
        token: str,
        branch: str,
        protected: tuple[str, ...],
        default_branch: str | None,
        author_name: str,
        author_email: str,
        out: dict[str, Any],
        expect_tip: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Publish onto a continued branch moved only by merges of the default branch (archived issue 2179).

        MEASURED 2026-10-23 (task_045fda9399ba47289b3a): a CI-fix round for
        archived pull request 2093 cloned its branch at bfdc51ad and made one commit; GitHub's
        "Update branch" meanwhile put a merge of main on the branch
        (c903caca, parents bfdc51ad and main's 3c6bba9b). The push, never
        forced, was a non-fast-forward, and the only recovery,
        `_push_over_own_earlier_attempt`, refuses a branch this task
        continues, so the round failed publish_failed although the moved tip
        was only main merged in -- and already carried the same fix.

        When every commit the fetched tip adds over the clone base is such a
        merge (`gitops.base_merge_tip`), this attempt's commits are replayed
        onto it (`gitops.replay_onto_tip`), the checks that ran before the
        first push run again over the replayed span -- worker authorship,
        the added-text leak scan, and the workflow paths, which this route
        may not newly reach -- and the branch is pushed with no lease: a
        fast-forward over the fetched tip, so it is still never forced, and
        anything that moved it again is refused as before. Returns the
        pushed sha and the token that pushed it (`_push_reading_again`,
        archived issue 3223); a None sha when the moved tip already holds every change, with
        `BASE_MERGE_NO_CHANGE_FIELD` set in `out` (the attempt's `no_change`,
        `_no_change_after_publish`). Any other mover re-raises `rejected`; a
        replay that conflicts, or fails a check, raises its own GitError
        naming the base merge. Nothing is pushed in either case.

        SUPERSEDED BY THE BASE (archived issue 2367, measured 2026-10-25 on four rounds
        whose hand-patched unit_test_durations.json main's archived pull request 2312 rewrote): a
        replay whose every conflict is in a path the base merge changed takes
        the tip's version of it (`replay_onto_tip`), and those paths are
        recorded in `BASE_MERGE_SUPERSEDED_FIELD`. With nothing left over the
        tip, `BASE_MERGE_NO_CHANGE_FIELD` holds a `superseded_by_base:`
        reason naming the paths and the tip, so the round ends `no_change`,
        not publish_failed; with something left, it is checked and pushed
        exactly as above. A conflict in any other path still raises.

        ANY OTHER MOVER, REPLAYED ONTO TOO (archived issue 1821, owner decision 2026-10-27).
        MEASURED 2026-10-21: 14 of 69 ci-fix rounds in 48 h failed
        publish_failed because another fix round, a conflict fix or the merge
        step moved the pull request's branch before their push. When the tip
        is not base merges alone but still descends from the clone base
        (`gitops.moved_branch_tip`), the round's commits are replayed onto it
        with no tip-side resolution of any conflict -- the mover is another
        writer's work, not main's published change -- and the same checks
        run again, the fence among them, before one fast-forward push, never
        forced. A conflict raises a GitError naming the moved tip, and the
        attempt ends publish_failed with nothing pushed; a rewritten branch
        (the clone base no longer under it) re-raises `rejected` as before.
        The tip is recorded in `MOVED_TIP_FIELD`.

        `expect_tip`: a pull request's head moved only by this task's own
        earlier generation (archived issue 2693, `_own_moved_pull_head`). Only that tip is
        replayed onto -- a branch fetched anywhere else re-raises `rejected`
        -- and the push is leased on it, so a commit landing after the proof
        is refused, never replaced.
        """
        from .lifecycle import (
            BASE_MERGE_NO_CHANGE_FIELD,
            BASE_MERGE_SUPERSEDED_FIELD,
            BASE_MERGE_SUPERSEDED_MAX,
            MOVED_TIP_FIELD,
            final_tree_leak,
            push_branch,
            verify_worker_authorship,
        )
        cfg = self.cfg
        ws = self.ws
        assert ws is not None
        base = self._publish_base
        tip: str | None = None
        why = "the pull request's head moved only by this task's own commits"
        if expect_tip is None:
            tip, why = base_merge_tip(
                repo=publish_repo,
                url=url,
                branch=branch,
                default_branch=default_branch or "",
                token=token,
                base=base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_clone_timeout_seconds,
                logger=self.log,
                branch_prefix=cfg.git_branch_prefix,
            )
        by_base_merge = tip is not None
        if tip is None and base is not None:
            tip, other_why = moved_branch_tip(
                repo=publish_repo,
                url=url,
                branch=branch,
                token=token,
                base=base,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_clone_timeout_seconds,
                logger=self.log,
                branch_prefix=cfg.git_branch_prefix,
            )
            why = f"{why}; {other_why}" if tip is None else why
        if expect_tip is not None and tip != expect_tip:
            why = f"{why}; {branch} is no longer at {expect_tip[:12]}"
            tip = None
        if tip is None or base is None:
            self.log.warning(
                "the push was refused and the branch did not move on top of the clone "
                "base; publishing nothing",
                branch=branch,
                reason=str(self._scrub(why)),
            )
            raise rejected
        if by_base_merge:
            moved = (
                f"{branch} moved to {tip[:12]} by a base merge of "
                f"{default_branch or 'the default branch'} only"
            )
        elif expect_tip is not None:
            moved = f"{branch} moved to {tip[:12]} by this task's own earlier commits"
        else:
            moved = f"{branch} moved to {tip[:12]} by commits this round did not write"
        superseded: list[str] = []
        try:
            head = replay_onto_tip(
                repo=publish_repo,
                base=base,
                tip=tip,
                author_name=author_name,
                author_email=author_email,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
                superseded=superseded,
                take_base_paths=by_base_merge,
            )
        except GitError as exc:
            raise GitError(
                f"the push was refused: {moved}, and replaying this round's commits onto "
                f"it conflicted or failed ({str(self._scrub(str(exc)[:200]))}); nothing was "
                "pushed or forced"
            ) from rejected
        if by_base_merge:
            out["base_merge_from"] = base
            out["base_merge_tip"] = tip
        else:
            out["moved_tip_from"] = base
            out[MOVED_TIP_FIELD] = tip
        if superseded:
            out[BASE_MERGE_SUPERSEDED_FIELD] = [
                str(self._scrub(path[:300])) for path in superseded[:BASE_MERGE_SUPERSEDED_MAX]
            ]
        if head is None and superseded:
            named = ", ".join(out[BASE_MERGE_SUPERSEDED_FIELD][:5])
            if len(superseded) > 5:
                named += f" and {len(superseded) - 5} more"
            out[BASE_MERGE_NO_CHANGE_FIELD] = (
                f"superseded_by_base: {moved}, which rewrote {named} that this round "
                "also changed; the base's version stands and nothing was pushed"
            )
            self.log.info("the base merge rewrote every file this round changed; "
                          "nothing to push", branch=branch, tip=tip,
                          superseded=len(superseded))
            return None, token
        if head is None:
            out[BASE_MERGE_NO_CHANGE_FIELD] = (
                f"no_change: {moved}, which already holds every change this round "
                "made; nothing was pushed"
            )
            self.log.info("the moved tip already holds this round's change; nothing to push",
                          branch=branch, tip=tip)
            return None, token
        # The same checks the first push passed, over the span the push now
        # adds: the moved tip is the base from here on.
        verify_worker_authorship(
            repo=publish_repo,
            base=tip,
            author_name=author_name,
            author_email=author_email,
            also_own=((cfg.git_author_name, cfg.git_author_email),),
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
        )
        leak = final_tree_leak(
            repo=publish_repo,
            base=tip,
            leaks=self._leaks_in_added_text,
            private_dir=ws.private,
            logs_dir=ws.logs,
            timeout_seconds=cfg.git_harvest_timeout_seconds,
            logger=self.log,
            overlap=self._scan_overlap(),
        )
        if leak is not None:
            raise GitError(
                f"the push was refused: {moved}, and the replay onto it failed the leak "
                f"check: {str(self._scrub(leak))}; nothing was pushed"
            )
        self._publish_base = tip
        workflow_paths = self._workflow_paths(publish_repo, floor=None)
        if workflow_paths:
            # The first push carried none, or it went to the broker; a replay
            # that reaches `.github/workflows/` is not this route's to push.
            self._publish_base = base
            self.log.warning("the replay onto the moved tip touches workflow files; "
                             "publishing nothing", branch=branch)
            raise rejected
        # The fence, read again after the replay and before the second push
        # (invariant 5, archived issue 1821): a superseded attempt raises FencedError here
        # and stands down with the branch as the mover left it.
        try:
            self.control.validate_generation()
        except BaseException:
            self._publish_base = base
            raise
        try:
            # No lease: HEAD descends from the tip just fetched, so this is
            # a fast-forward, and a branch moved again is refused. Onto a
            # pull request's own moved head, leased on it as every push onto
            # a pull request is (archived issue 2183).
            pushed, token = self._push_reading_again(
                lambda with_token: push_branch(
                    repo=publish_repo,
                    url=url,
                    branch=branch,
                    token=with_token,
                    private_dir=ws.private,
                    logs_dir=ws.logs,
                    timeout_seconds=cfg.git_clone_timeout_seconds,
                    logger=self.log,
                    branch_prefix=cfg.git_branch_prefix,
                    protected=protected,
                    lease=expect_tip,
                ),
                token, branch=branch,
            )
        except GitError:
            self._publish_base = base
            raise
        self.log.info(
            "the continued branch moved only by a merge of the default branch; "
            "pushed this round's commits onto it as a fast-forward"
            if by_base_merge else
            "the continued branch moved by another writer's commits; pushed this "
            "round's commits onto it as a fast-forward",
            branch=branch,
            moved_tip=tip,
            head=pushed or "unknown",
            how="fast_forward",
        )
        return pushed, token

    def _publish_git(
        self,
        *,
        repo: Path,
        work_head: str | None,
        publish: bool,
        withheld: str = "",
        publish_repo: Path | None = None,
    ) -> dict[str, Any]:
        """The push-and-pull-request half. Gated on the forge, not on hope.

        `publish_repo` is the clean repository the harvest already built,
        after its reap (`_harvest_git`). Without one -- a direct call -- this
        reaps and builds it itself, at the same point it always did.
        """
        from .lifecycle import (
            AUTO_COMMIT_UNDID_FIELD,
            BASE_MERGE_NO_CHANGE_FIELD,
            FOLDED_COMMIT_SUBJECT,
            PR_TITLE_FILE,
            PUBLISH_FAILED_FIELD,
            PUBLISH_UNREACHABLE_FIELD,
            PULL_REQUEST_NOT_OPENED_FIELD,
            PULL_REQUEST_REFUSED,
            UNCOMMITTED_COMMIT_SUBJECT,
            WORKFLOW_PATHS_NAMED,
            _FULL_SHA_RE,
            _PullUnreachable,
            _commits_leak,
            _config_seal,
            _neutralise_mentions,
            commit_dirty,
            final_tree_leak,
            fold_agent_commits,
            merge_branches,
            open_pull_request,
            pr_body_with_console_links,
            push_branch,
            replay_agent_commits,
            verify_worker_authorship,
        )
        cfg = self.cfg
        ws = self.ws
        assert ws is not None
        # The clone's HEAD as the harvest read it, before the auto-commit
        # moves `work_head`: what a retry's restored clone is compared with.
        harvested_head = work_head
        self._publish_remote_tip = None

        if not publish:
            return {
                "published": False,
                "publish_reason": withheld
                or "this attempt parked; publishing waits for the run to finish",
            }
        if not cfg.git_publish_enabled:
            return {"published": False, "publish_reason": "publishing is disabled for this worker"}

        # THE CALLER'S STRATEGY IS A PROMISE AND THIS IS WHERE IT IS KEPT.
        #
        # swarm-api accepts `strategy` on submit and returns it in the 201, and
        # `collect` -- the default every caller gets -- states that patches are
        # harvested and NOTHING IS PUSHED. Until this check existed that
        # guarantee held only because the tenant's token happened to lack write
        # scope: granting write scope would have made every `collect` dispatch
        # start opening pull requests while its own API response promised it
        # would not. A guarantee enforced by an unrelated accident is not a
        # guarantee.
        #
        # Read from metadata rather than a typed field because adding one to
        # Task would be a frozen-contract change; the request to type it is
        # recorded in docs/contract-change-requests.md.
        strategy = self._dispatch_strategy()
        # `single-pr` (archived issue 295, docs/merge-step.md §3) publishes by ROLE: the
        # author pushes its own branch and opens the one pull request, as
        # `direct-pr` does; the amender fast-forward pushes to the AUTHOR's
        # branch and opens nothing; a reader pushes nothing at all, whatever
        # it changed -- review and proof only write their JSON.
        pr_role = self._pr_role()
        if pr_role in ("reader", "none"):
            return {
                "strategy": "single-pr",
                "pr_role": pr_role,
                "published": False,
                "publish_reason": (
                    f"strategy is 'single-pr' and this step's pr_role is {pr_role!r}: "
                    "it pushes nothing and opens nothing"
                ),
            }
        if pr_role:
            strategy = "single-pr"
        # `carrier: branches` PUSHES under every strategy (D13): the branch is
        # where the step's work is kept for the next step, so a `collect` step
        # pushes it too -- and opens nothing, below.
        carrier = self._dispatch_carrier()
        if strategy == "collect" and carrier != "branches":
            return {
                "strategy": strategy,
                "published": False,
                "publish_reason": (
                    "strategy is 'collect': the patch is harvested and nothing "
                    "is pushed. Submit with strategy 'direct-pr' to open a pull "
                    "request from this agent"
                ),
            }

        url = self._repo_url or cfg.repository_url
        if not url:
            return {"published": False, "publish_reason": "the repository URL is unknown"}

        # THE GRANT, BEFORE THE TOKEN (docs/onboarding.md §3.3 steps 3-4, OB5).
        # A read grant never reads a push credential, and the submitter's
        # grant is read again here, before this push and pull request. A
        # refusal is recorded as `forge_write_refused`, which ends the attempt
        # PUBLISH_REFUSED after the final checkpoint (`_record_finalised`).
        forge_refused = self._forge_write_refusal()
        if forge_refused is not None:
            cause, reason = forge_refused
            self.log.error("not publishing: the task's forge grant refuses it",
                           cause=cause, reason=reason)
            return {
                "published": False,
                "publish_reason": f"{cause}: {reason}",
                "forge_write_refused": {"cause": cause, "reason": reason},
            }

        # Before the token is read, not after: see `_git_token_refusal`. Asked
        # here as well as inside `_git_token`, so the publish says WHY it did
        # not happen instead of reporting a forge that refused an anonymous probe.
        refusal = self._git_token_refusal()
        if refusal:
            self.log.error("not publishing: the tenant git token is refused", reason=refusal)
            return {"published": False, "publish_reason": f"refusing to publish: {refusal}"}

        token = self._git_token(write=True)
        try:
            token, access = self._probe_reading_again(url, token, "the publish probe")
        except ForgeError as exc:
            out = {
                "published": False,
                "publish_reason": f"could not reach the forge: {self._scrub(str(exc)[:300])}",
            }
            if isinstance(exc, forge_mod.ForgeUnavailable):
                out[PUBLISH_UNREACHABLE_FIELD] = self._unreachable_marker(exc)
            return out
        if access is None:
            return {
                "published": False,
                "publish_reason": "this repository is not on a forge this worker can publish to",
            }

        out: dict[str, Any] = {
            "repository": access.ref.full_name,
            "default_branch": access.default_branch or None,
            "can_push": access.can_push,
        }
        if not access.can_push:
            # THE EXPECTED PATH TODAY, and the reason it reads as a fact rather
            # than a failure. The tenant's secret holds a clone token; the forge
            # was asked and said no. The patch above is the deliverable.
            out["published"] = False
            out["publish_reason"] = access.reason
            self.log.info("not publishing: no write permission", reason=access.reason)
            return out

        if not token:  # pragma: no cover - can_push implies a token
            out["published"] = False
            out["publish_reason"] = "no credential"
            return out

        # The amender's author branch, the pull request's (`publish_to_pr`),
        # or its own `<prefix><task id>` / the branch a fix step continues.
        try:
            branch, onto_pr = self._push_branch()
            if onto_pr is not None:
                out["publish_to_pr"] = {
                    **onto_pr.to_record(cfg.git_branch_prefix),
                    "started_head": self._pr_start_head,
                }
        except WorkerError as exc:
            out["published"] = False
            out["publish_reason"] = f"refusing to publish: {exc}"
            return out
        protected = (access.default_branch,) if access.default_branch else ()

        role = self._dispatch_role() if strategy == "integrate" else ""
        out["role"] = role or None
        if pr_role:
            out["pr_role"] = pr_role

        # NO AGENT PROCESS IS ALIVE ONCE THE TOKEN IS IN HAND. This is the one
        # place the credential-bearing publish begins -- everything below carries
        # or leads directly to the tenant token (`prepare_publish_repo` mkdtemps
        # the clean publish repo and fetches the agent's commits into it,
        # `mirror_worktree` copies its uncommitted files there, and the commit,
        # the scans, the replay or fold and the merge/push all run there). A
        # process the agent double-forked into
        # its own session survives the runner's group kill (procman.py), shares
        # the worker's uid, and could watch `ws.private` to poison the publish
        # repo before the push or read the short-lived credential file while it
        # runs. So every such process is killed and its death verified BEFORE the
        # publish repo is created; if any survives the bounded retry, the whole
        # publish is refused rather than run with agent code still able to act.
        # Reaped AGAIN here even when the harvest already reaped and built the
        # repository (`_harvest_git`), before any step that writes the
        # credential file or pushes: the archived pull request 259 final review (B1) found a git in
        # the clone that could start a process after the first reap. That path
        # is closed (HEAD is read as data), and this second reap keeps the
        # guarantee above from depending on it.
        survivors = self.reap_before_publish()
        if survivors:
            out["published"] = False
            out["publish_reason"] = (
                f"refusing to publish: {len(survivors)} process(es) the agent started "
                f"are still alive after the pre-publish reap ({', '.join(str(p) for p in survivors[:10])}); "
                "the tenant credential is not put in hand while agent code can run"
            )
            self.log.error(
                "refusing to publish: agent processes survived the pre-publish reap",
                surviving_pids=list(survivors),
            )
            return out
        # THE PUBLISH REPOSITORY IS STILL THE ONE THE WORKER BUILT (archived issue 346 box
        # 40, security pass 2026-10-09). A repository the harvest built after
        # the FIRST reap sat on disk until this second one; anything that ran
        # in between could have written its `.git/config` -- a
        # `core.sshCommand`, a credential helper, a remote, an `include` --
        # which every git below reads with the tenant token in hand. No such
        # process should exist (the first reap's escape is closed), so this
        # is the belt: the config must be byte for byte what
        # `_build_clean_repo` left, a regular file and not a link, or nothing
        # is published.
        if publish_repo is not None:
            sealed = self._publish_config_seals.get(str(publish_repo), "unsealed")
            if sealed == "unsealed" or _config_seal(publish_repo) != sealed:
                out["published"] = False
                out["publish_reason"] = (
                    "refusing to publish: the publish repository's .git/config changed "
                    "between the harvest and the pre-publish reap; the tenant credential "
                    "is not put in hand with a configuration the worker did not write"
                )
                self.log.error("refusing to publish: the publish repository's config changed")
                return out

        auto_committed = False
        folded = 0
        kept = 0
        merge: MergeOutcome | None = None
        try:
            # EVERY COMMIT THIS PUSHES IS THE WORKER'S. Whatever the agent
            # committed itself -- possibly as Claude, possibly with a
            # Co-Authored-By trailer and a "Generated with" line, which is what
            # Claude Code adds by default -- is rewritten as a commit the
            # worker writes. See `gitops.fold_agent_commits` for why this is
            # done here and not with a setting in the image.
            #
            # EACH OF THE AGENT'S COMMITS IS KEPT (archived issue 242), in order, with its
            # own tree and its own message cleaned of trailers and footers
            # (`replay_agent_commits`); the worker's commit of the uncommitted
            # work below, if there is one, stays last. Folding them into one
            # made a red-first pull request -- tests, then the fix --
            # impossible: CI never saw the
            # tests alone. The fold is kept for the history that cannot be
            # replayed: one that does not descend from the clone base, or one
            # past `MAX_KEPT_COMMITS`.
            # `_publish_base`, never the workspace marker: see `_maybe_clone`.
            if self._publish_base is None and self._clone_base:
                # Said precisely, because "unknown" would be false: the harvest
                # HAD a base, and a reader comparing the two would think one
                # of them was a bug.
                raise GitError(
                    "the clone base is known only from the marker in the "
                    "workspace, which the agent can write: this attempt resumed "
                    "from a checkpoint that does not record the base, so the "
                    "worker cannot tell which commits are the agent's. Nothing "
                    "was pushed; the harvest still describes the work against "
                    "that marker"
                )
            # A repository the harvest built may stand on no base at all
            # (`allow_unknown_base`, so the dirty list survives); it is never
            # published from. One built below refuses the unknown base itself,
            # in `prepare_publish_repo`.
            if publish_repo is not None and self._publish_base is None:
                raise GitError(
                    "the clone base is unknown, so the worker cannot tell the agent's "
                    "commits from the repository's; nothing was pushed"
                )

            # EVERYTHING THAT DECIDES WHAT IS PUSHED, AND THE PUSH, RUNS IN A
            # REPOSITORY THE WORKER OWNS (owner decision, 2026-09-29, archived pull request 259).
            # The agent's commits are fetched out of the clone into a fresh,
            # worker-owned repository -- object-checked, by sha, with no
            # alternates into the clone and none of its config, hooks, grafts,
            # shallow file, replace refs or commit-graph honoured -- and the
            # kept-commit list, the per-commit scan, the fold, the final-tree
            # scan, the replay, the authorship check, the integrator's merges
            # and the token-bearing push all run there. Three reviews found
            # the scans bypassable through `.git` state the agent writes, one
            # file at a time; this removes the class. The token is still only
            # ever used in this repository (archived pull request 219). See
            # `gitops.prepare_publish_repo` and docs/merge-strategy-live-proof.md §6.
            #
            # WHAT THE AGENT LEFT UNCOMMITTED IS COMMITTED THERE, NOT IN ITS
            # CLONE (archived pull request 259 M1, owner decision 2026-09-28). `git add` in the
            # clone read the clone's `.git/config` and the tree's
            # `.gitattributes`, so a filter driver the agent defined ran as the
            # worker after the reap. The working tree is copied into the
            # publish repository instead (`_build_clean_repo`) -- no `.git`,
            # links as links -- and staged and committed with the worker's
            # configuration alone, where a `.gitattributes` naming a filter is
            # data: no filter of that name is defined. Nothing under the
            # clone's `.git` runs at publish; the clone is read only by the
            # object-checked fetch.
            if publish_repo is None:
                publish_repo = self._build_clean_repo(repo, floor=self._publish_base)
            # EVERY COMMIT BELOW IS WRITTEN AS THE PERSON WHO DISPATCHED THE
            # WORK (archived issue 765, owner decision 2026-10-07): the auto-commit, the
            # replay or the fold, the integrator's merges. The bot only when
            # the task names nobody. See `_publish_identity`.
            author_name, author_email = self._publish_identity()
            # A BRANCH THE AGENT PUSHED THROUGH `git_push_branch` IS PUBLISHED
            # (archived issue 1188). From here the publish diffs from the tip that push left
            # and the push below is leased on it; see `_adopt_tool_pushed_tip`.
            # `carrier: branches` keeps its own fold onto the branch's tip.
            tool_tip = (
                self._adopt_tool_pushed_tip(publish_repo, branch)
                if carrier != "branches" and onto_pr is None else None
            )
            if tool_tip is not None:
                out["tool_pushed_from"] = self._publish_base
                out["tool_pushed_tip"] = tool_tip
                self._publish_base = tool_tip
            # A CONFLICT FIX ROUND READS MAIN'S TIP WHETHER OR NOT IT MERGED
            # (archived issue 1321): the check before the push asks whether the round's
            # branch now stands on it, and a round that merged nothing is the
            # case that check exists to refuse.
            conflict_round = self._conflict_round()
            default_tip = (
                self._default_branch_tip(
                    publish_repo, url=url, token=token, default_branch=access.default_branch
                )
                if conflict_round
                else None
            )
            # Read before the replay or the fold rewrites the agent's commits.
            # See `_scan_floor`: it is also the bound on which merge parents
            # the replay and the fold keep.
            scan_floor = self._scan_floor(
                publish_repo, url=url, token=token, default_branch=access.default_branch,
                tip=default_tip,
            )
            # WHAT ORIGIN'S BRANCH ALREADY HOLDS IS NOT THIS ROUND'S TO PUSH
            # (archived issue 2506). The publish read its range as HEAD not reachable from
            # the clone base or the merged default-branch commit, and never
            # asked where origin's branch stood. Measured on ci-fix
            # task_1297d8f4 (archived pull request 2446): the round cloned at fba856, the owner
            # pushed 89507cf -- a workflow change -- to the pull request's
            # branch, and the agent's clone ended on exactly that commit. The
            # publish counted the owner's workflow file as the round's, sent a
            # push of nothing new through the broker, whose one call answered
            # api_unavailable, and the step ended publish_failed.
            #
            # So origin's tip is read from the FORGE into the publish
            # repository (`_read_publish_remote_tip`, never the agent's
            # clone). When HEAD descends from it, it joins the commits the
            # range starts from (`_workflow_negatives`), and when it also
            # descends from the clone base it becomes the base, as a tool
            # push's tip does (archived issue 1188): the replay, the fold and the scans then
            # read only what origin lacks, and origin's commits are pushed as
            # they are, never rewritten into a non-fast-forward. A HEAD that is
            # the tip, or behind it, pushes nothing: see below `commit_dirty`.
            # Asked only of a publish that continues a branch origin may hold
            # (`_publish_continues_a_branch`), or of a tool push's tip, which
            # is not fetched again: a fresh lane's first attempt is not.
            remote_tip = (
                self._read_publish_remote_tip(
                    publish_repo, url=url, token=token, branch=branch, tool_tip=tool_tip,
                )
                if carrier != "branches" and onto_pr is None
                and (tool_tip is not None or self._publish_continues_a_branch())
                else None
            )
            if remote_tip is not None and self._publish_is_ancestor(
                publish_repo, remote_tip, "HEAD"
            ):
                self._publish_remote_tip = remote_tip
                base = self._publish_base
                if (
                    tool_tip is None and base and remote_tip != base
                    and _FULL_SHA_RE.fullmatch(base)
                    and self._publish_is_ancestor(publish_repo, base, remote_tip)
                ):
                    out["remote_tip_from"] = base
                    out["remote_tip"] = remote_tip
                    self._publish_base = remote_tip
            # Read before the auto-commit moves HEAD: the check below it asks
            # whether that commit undid what the agent's commits did (archived issue 2536).
            agent_head = self._publish_rev(publish_repo, "HEAD")
            left_unstaged: list[str] = []
            new_sha = commit_dirty(
                repo=publish_repo,
                message=self._worker_commit_message(
                    UNCOMMITTED_COMMIT_SUBJECT,
                    "Staged by the worker at the end of the attempt so that work "
                    "the agent edited but did not commit is not lost between the "
                    "workspace and this branch.",
                ),
                author_name=author_name,
                author_email=author_email,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
                # What the agent's commits since the base removed from the
                # index and left on disk is not staged again (archived issue 2536).
                base=self._publish_base,
                left_unstaged=left_unstaged,
            )
            if left_unstaged:
                out["left_unstaged"] = self._scrub(list(left_unstaged))
            if new_sha:
                auto_committed = True
                work_head = new_sha

            # A HEAD ORIGIN'S BRANCH ALREADY HOLDS PUSHES NOTHING (archived issue 2506): the
            # measured case, HEAD the pull request's own tip. It ends as the
            # `no_change` result, as a branch whose base merge already holds
            # the round's change does (`_push_onto_base_merge`, archived issue 2179): no
            # broker call, no token push. Not for a tool push's tip, which the
            # push below re-proves under its lease (archived issue 1188); not for a conflict
            # fix round, which the check before the push refuses when it
            # merged nothing (archived issue 1321); not for an integrator, whose merges are
            # still to come.
            if (
                remote_tip is not None and not new_sha and tool_tip is None
                and not conflict_round and role != "integrator"
                and self._publish_is_ancestor(publish_repo, "HEAD", remote_tip)
            ):
                out[BASE_MERGE_NO_CHANGE_FIELD] = (
                    f"no_change: {branch} on origin already stands at {remote_tip[:12]}, "
                    "which holds every commit this round's head carries; nothing was pushed"
                )
                out["remote_tip"] = remote_tip
                out["published"] = False
                out["auto_committed"] = auto_committed
                out["publish_reason"] = out[BASE_MERGE_NO_CHANGE_FIELD]
                self.log.info("origin's branch already holds this round's head; nothing to push",
                              branch=branch, tip=remote_tip)
                return out

            # THE WORKER'S COMMIT MUST NOT UNDO THE AGENT'S (archived issue 2536). Measured
            # on task_6e4f6aa4: the auto-commit re-staged a file the agent's
            # commit had removed from the index, the branch's net diff was
            # empty, and the step read SUCCEEDED. `commit_dirty` no longer
            # does that; this is the check that a tree the agent's commits
            # changed is not pushed back at its base by the worker's commit
            # for any other reason. Nothing is pushed, and the attempt fails
            # (`_fail_for_auto_commit_undid`).
            undid = (
                self._auto_commit_undid(publish_repo, agent_head=agent_head)
                if new_sha else None
            )
            if undid is not None:
                reason = str(self._scrub(undid))
                out["published"] = False
                out["auto_committed"] = auto_committed
                out[AUTO_COMMIT_UNDID_FIELD] = reason
                out["publish_reason"] = f"refusing to publish: {reason}"
                self.log.error("not publishing: the auto-commit undid the agent's commits",
                               reason=reason)
                return out

            # `carrier: branches` (D13): the branch may already hold this step's
            # earlier pushes (`_push_carrier_branch`), and a replay writes new
            # shas every time, so its line would not descend from them and the
            # push, never forced, would be refused. The final tree goes on the
            # branch's tip as one worker commit instead.
            if carrier == "branches":
                self._carrier_fold_onto_tip(
                    publish_repo, url=url, token=token, branch=branch,
                    message=self._worker_commit_message(
                        FOLDED_COMMIT_SUBJECT,
                        "The step's work as it finished, committed by the worker on "
                        "top of what its checkpoints already pushed (carrier: "
                        "branches), so the branch only ever fast-forwards.",
                    ),
                )
            else:
                # A merged branch the forge already holds stays a parent
                # whatever its commits say: it is published already (archived issue 3359).
                on_forge = self._forge_holder(url=url, token=token)
                try:
                    replayed = replay_agent_commits(
                        repo=publish_repo,
                        base=self._publish_base,
                        keep=new_sha,
                        task_id=cfg.task_id,
                        scrub=lambda text: str(self._scrub(text)),
                        author_name=author_name,
                        author_email=author_email,
                        private_dir=ws.private,
                        logs_dir=ws.logs,
                        timeout_seconds=cfg.git_harvest_timeout_seconds,
                        logger=self.log,
                        # A registered secret OR a credential-shaped run in any kept
                        # commit's diff folds the history instead (archived pull request 259 review; owner
                        # decision 4, 2026-09-28, for the patterns): the scrub
                        # replaces exactly the registered values, so "the scrub
                        # changed it" is "a registered value is in it", and a key the
                        # agent minted or an `.env` it committed is registered nowhere
                        # and caught only by the patterns. Asked about the text each
                        # file ADDS, `+` removed, in overlapping windows.
                        leaks=self._leaks_in_added_text,
                        overlap=self._scan_overlap(),
                        upstream=scan_floor or "",
                        on_forge=on_forge,
                    )
                    if replayed is not None:
                        kept = replayed
                        out["agent_commits_kept"] = kept
                    else:
                        replaced = fold_agent_commits(
                            repo=publish_repo,
                            base=self._publish_base,
                            keep=new_sha,
                            message=self._worker_commit_message(
                                FOLDED_COMMIT_SUBJECT,
                                "Everything the agent changed in this attempt, committed or "
                                "not, as one commit made by the worker. The worker writes "
                                "every commit it pushes, so no author, trailer or footer "
                                "added inside the agent's container reaches this branch.",
                            ),
                            author_name=author_name,
                            author_email=author_email,
                            private_dir=ws.private,
                            logs_dir=ws.logs,
                            timeout_seconds=cfg.git_harvest_timeout_seconds,
                            logger=self.log,
                            # The default branch's commit the lane merged, proven
                            # on the forge, one of the fold's merged heads (archived issue 1321);
                            # the fold reads every other merge itself (archived issue 3359).
                            merged=scan_floor or "",
                            # A merged branch kept as a parent is pushed commit by
                            # commit, so its commits are scanned as the replay's
                            # are, and one that leaks is not kept (archived issue 3359 review).
                            history_leaks=lambda commits: _commits_leak(
                                commits, leaks=self._leaks_in_added_text,
                                overlap=self._scan_overlap(),
                                git=["git", *_NO_HOOKS], repo=publish_repo,
                                private_dir=ws.private, logs_dir=ws.logs,
                                timeout_seconds=cfg.git_harvest_timeout_seconds,
                                logger=self.log,
                            ),
                            on_forge=on_forge,
                        )
                        # The worker's own auto-commit is among what was replaced
                        # when there was one; the rest were the agent's.
                        folded = max(replaced - (1 if new_sha else 0), 0)
                        out["agent_commits_folded"] = folded
                except MergedHeadUnreadable as exc:
                    # A merged branch the clean repository cannot read: a
                    # replay or a fold without it would drop its history, so
                    # nothing is pushed and the branch is kept, merges
                    # included, as the harvest keeps it when the fetch fails
                    # (archived issue 3174, archived issue 3359).
                    out.update(self._keep_work_bundle(repo, self._publish_base))
                    if out.get("bundle"):
                        raise MergedHeadUnreadable(
                            f"{exc}; the agent's branch, merge commits included, "
                            f"is kept in {WORK_BUNDLE_NAME} ({out['bundle_commits']} "
                            f"commits on {out['bundle_base']})"
                        ) from exc
                    raise

            # A CONFLICT FIX ROUND MUST LEAVE THE BRANCH ON MAIN (archived issue 1321). Its
            # whole job is to move the pull request's merge base to the
            # default branch's head; a branch that does not stand on it still
            # conflicts, and pushing it would spend one of the merge step's
            # rounds on a head that changes nothing. Refused here, with the
            # merge base named, as the attempt's error.
            if conflict_round:
                verify_merged_default_branch(
                    repo=publish_repo,
                    base=self._publish_base,
                    tip=default_tip,
                    default_branch=access.default_branch or "the default branch",
                    private_dir=ws.private,
                    logs_dir=ws.logs,
                    timeout_seconds=cfg.git_harvest_timeout_seconds,
                    logger=self.log,
                )

            # THE BRANCH AS IT WILL BE PUSHED IS SCANNED BEFORE ANY PUSH
            # (owner decision, 2026-09-28, archived pull request 259 review M4). The scan above
            # decides only between keeping and folding; the fold's one commit
            # and the worker's commit of uncommitted work carry the FINAL
            # tree, which it never read. A registered value or a
            # credential-shaped literal that tree ADDS publishes nothing, and
            # the attempt fails retryably (`_fail_for_final_tree_leak`),
            # naming the file and never the value.
            leak = final_tree_leak(
                repo=publish_repo,
                base=self._publish_base,
                leaks=self._leaks_in_added_text,
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
                overlap=self._scan_overlap(),
                floor=scan_floor,
            )
            if leak is not None:
                reason = str(self._scrub(leak))
                out["published"] = False
                out["auto_committed"] = auto_committed
                out["final_tree_leak"] = reason
                out["publish_reason"] = f"refusing to publish: {reason}"
                self.log.error("not publishing: the final tree failed the leak check", reason=reason)
                return out

            # EVERYTHING THAT CARRIES THE TOKEN RUNS IN THE SAME WORKER-OWNED
            # REPOSITORY, NOT IN THE CLONE. The push and the integrator's
            # contributor fetch authenticate with the tenant token; run in the
            # clone, a `credential.helper` or `url.*.insteadOf` the agent wrote
            # into `.git/config` would receive or redirect it. The verify runs
            # here too, so it sees the integrator's merge commits.

            # THE INTEGRATOR MERGES BEFORE IT PUSHES.
            #
            # Its own commit has to be in the tree first (transferred above), and
            # every contributor's branch has to be in it before the push, or the
            # single pull request this strategy promises would contain only
            # the integrator's own step.
            #
            # The branch names are DERIVED from the upstream task ids with the
            # same prefix the contributors pushed under -- never taken from
            # metadata as names -- so nothing in a task document can point this
            # at an arbitrary ref.
            if role == "integrator":
                upstream = [
                    f"{cfg.git_branch_prefix}{tid}" for tid in self._integrates_with_changes()
                ]
                if upstream:
                    merge = merge_branches(
                        repo=publish_repo,
                        url=url,
                        branches=upstream,
                        token=token,
                        private_dir=ws.private,
                        logs_dir=ws.logs,
                        timeout_seconds=cfg.git_clone_timeout_seconds,
                        logger=self.log,
                        author_name=author_name,
                        author_email=author_email,
                        branch_prefix=cfg.git_branch_prefix,
                    )
                    out["integrated"] = {
                        "merged": list(merge.merged),
                        "conflicted": list(merge.conflicted),
                        "missing": list(merge.missing),
                        "complete": merge.complete,
                    }
                if self._no_change_contributors:
                    # Not merged, and not missing: they had nothing to change
                    # (`allow_empty_diff`), so they pushed no branch.
                    out.setdefault("integrated", {})["no_change"] = [
                        f"{cfg.git_branch_prefix}{tid}" for tid in self._no_change_contributors
                    ]
                if self._read_only_contributors:
                    # Not merged, and not missing: they SUCCEEDED having
                    # published nothing by design (archived issue 760).
                    out.setdefault("integrated", {})["read_only"] = [
                        f"{cfg.git_branch_prefix}{tid}" for tid in self._read_only_contributors
                    ]

            # THE PROPERTY, CHECKED WHERE THE WORK LEAVES. The fold and the
            # identity arguments are how every pushed commit is made the
            # worker's; this reads the commits the push is about to add and
            # refuses the push if any is not, whatever route got it there. It
            # runs in the publish repo, so an integrator's merge commits are in
            # its first-parent span.
            verify_worker_authorship(
                repo=publish_repo,
                base=self._publish_base,
                author_name=author_name,
                author_email=author_email,
                also_own=((cfg.git_author_name, cfg.git_author_email),),
                private_dir=ws.private,
                logs_dir=ws.logs,
                timeout_seconds=cfg.git_harvest_timeout_seconds,
                logger=self.log,
            )

            # A WORKFLOW FILE IS PUSHED BY THE BROKER, NEVER BY THIS TOKEN (AU-3,
            # docs/design/agent-autonomy.md §3.3, owner decision Q3). The tenant
            # slot has no `workflow` scope, so GitHub refuses the push, and lanes
            # ended SUCCEEDED with their workflow change unpushed. A branch whose
            # new commits touch `.github/workflows/` or `.github/actions/` goes
            # to swarm-api's `git_push_branch`, which pushes it with a GitHub App
            # token minted for that one repository, only where the registration's
            # `workflow_files` switch is on (archived issue 2029), deciding from the same
            # `git log` it reads the bundle with; a
            # refusal publishes nothing, fails the step and leaves the change as
            # `workflow-lines.patch`. Every check above has already run here.
            # ONTO AN EXISTING PULL REQUEST, ONLY AS A FAST-FORWARD OF THE HEAD
            # THIS STEP STARTED FROM (archived issue 2183): read again, checked again, and
            # the push below is leased on that head. Refused here, as the
            # attempt's error, before anything is pushed.
            onto_pull, own_moved, token = (
                self._check_pull_before_push(
                    publish_repo, onto_pr, access=access, token=token, url=url,
                    branch=branch,
                )
                if onto_pr is not None else (None, False, token)
            )
            workflow_paths = self._workflow_paths(publish_repo, floor=scan_floor)
            if workflow_paths and onto_pr is not None:
                out["workflow_paths"] = workflow_paths[:WORKFLOW_PATHS_NAMED]
                return self._workflow_push_refused(
                    out, publish_repo,
                    {
                        "code": "publish_to_pr_workflow_files",
                        "message": (
                            "this step publishes onto an existing pull request's branch, "
                            "and a workflow file is pushed only by swarm-api's "
                            "git_push_branch, which pushes a task's own branch"
                        ),
                    },
                    floor=scan_floor, auto_committed=auto_committed,
                )
            if workflow_paths:
                out["workflow_paths"] = workflow_paths[:WORKFLOW_PATHS_NAMED]
                self._own_before_forge_write("workflow push")
                pushed, refused = self._push_through_broker(
                    publish_repo, branch=branch, floor=scan_floor
                )
                if refused is not None:
                    return self._workflow_push_refused(
                        out, publish_repo, refused, floor=scan_floor,
                        auto_committed=auto_committed,
                    )
                out["workflow_publish"] = "broker"
            elif onto_pull is not None and own_moved:
                pushed, token = self._publish_onto_own_moved_head(
                    publish_repo, onto_pull, url=url, token=token, branch=branch,
                    protected=protected, default_branch=access.default_branch,
                    author_name=author_name, author_email=author_email, out=out,
                )
            else:
                # Not inside the `try`: a fence is not a refused push, and
                # `except GitError` below must not turn it into publish_failed.
                self._own_before_forge_write("branch push")
                try:
                    # A stale credential reads the slot again once and pushes
                    # once more; the token it pushed with goes on to the pull
                    # request and any replay (archived issue 3223).
                    pushed, token = self._push_reading_again(lambda with_token: push_branch(
                        repo=publish_repo,
                        url=url,
                        branch=branch,
                        token=with_token,
                        private_dir=ws.private,
                        logs_dir=ws.logs,
                        timeout_seconds=cfg.git_clone_timeout_seconds,
                        logger=self.log,
                        branch_prefix=cfg.git_branch_prefix,
                        protected=protected,
                        # The tip this attempt's own tool push left (archived issue 1188):
                        # a head equal to it is an up-to-date no-op that
                        # still proves the branch holds it; a branch moved
                        # since, by anyone, is refused. Onto an existing
                        # pull request (archived issue 2183): its head as this step
                        # started from it, which HEAD was just shown to
                        # descend from -- so the push is a fast-forward
                        # or nothing.
                        lease=onto_pull.head_sha if onto_pull is not None else tool_tip,
                    ), token, branch=branch)
                except PushRejected as rejected:
                    if onto_pull is not None:
                        raise GitError(
                            f"{publishpr_mod.HEAD_MOVED}: pull request #{onto_pull.number}'s "
                            f"branch moved off {onto_pull.head_sha[:12]} while this step "
                            "pushed; the forge refused the push and nothing was forced"
                        ) from rejected
                    if tool_tip is not None:
                        # Somebody moved the branch past this task's own push.
                        # Their work is not replaced, and no other lease is tried.
                        self.log.warning(
                            "the branch moved past the tip this task pushed through "
                            "git_push_branch; publishing nothing",
                            branch=branch, tool_tip=tool_tip,
                        )
                        raise
                    if branch == f"{cfg.git_branch_prefix}{cfg.task_id}":
                        pushed, token = self._push_over_own_earlier_attempt(
                            publish_repo, rejected, url=url, token=token, branch=branch,
                            protected=protected, author_name=author_name,
                            author_email=author_email,
                        )
                    else:
                        # A branch this task continues (archived issue 2179): GitHub's
                        # "Update branch" merging the default branch into
                        # it mid-run is the measured mover. Only that mover
                        # is taken, as a fast-forward onto it; see
                        # `_push_onto_base_merge`. A round whose conflicts
                        # lie only in files that merge rewrote ends no_change
                        # `superseded_by_base` when nothing else is left (archived issue 2367).
                        pushed, token = self._push_onto_base_merge(
                            publish_repo, rejected, url=url, token=token, branch=branch,
                            protected=protected, default_branch=access.default_branch,
                            author_name=author_name, author_email=author_email,
                            out=out,
                        )
                        if pushed is None:
                            out["published"] = False
                            out["auto_committed"] = auto_committed
                            out["publish_reason"] = out[BASE_MERGE_NO_CHANGE_FIELD]
                            return out
        except GitError as exc:
            out["published"] = False
            out["auto_committed"] = auto_committed
            out["publish_reason"] = self._scrub(str(exc)[:500])
            if isinstance(exc, _PullUnreachable):
                # An outage, not a refusal: the attempt ends retryably
                # (`_publish_unreachable`), and nothing was pushed (archived issue 2183).
                out[PUBLISH_UNREACHABLE_FIELD] = self._unreachable_marker(exc.cause)
                self.log.warning("could not read the pull request before the push",
                                 error=str(exc))
                return out
            if isinstance(exc, PublishFetchIncomplete):
                # A partial clone's publish fetch (issue 367): retried with
                # the work kept, never `publish_failed`.
                out[PUBLISH_UNREACHABLE_FIELD] = self._fetch_incomplete_marker(
                    exc, work_head=harvested_head
                )
                self.log.warning("the publish fetch met an object the partial clone never "
                                 "downloaded; the attempt is retried with its work kept",
                                 missing=exc.missing, on_origin=exc.on_origin)
                return out
            if isinstance(exc, PushUnreachable):
                # The forge did not answer the push through every try (archived issue 2833):
                # an outage, which the next attempt may not meet, so the
                # ATTEMPT ends retryably (`_publish_unreachable`) and the
                # retry publishes the work kept here (`_push_to_republish`).
                out[PUBLISH_UNREACHABLE_FIELD] = self._push_unreachable_marker(
                    exc, work_head=harvested_head
                )
                self.log.warning("the forge did not answer the push; the attempt is "
                                 "retried with its work kept", tries=exc.tries)
                return out
            # Read at the finish (archived issue 872): a pull-request step whose push failed
            # has not delivered, however cleanly its agent exited.
            out[PUBLISH_FAILED_FIELD] = True
            self.log.warning("could not publish the branch", error=str(exc))
            return out

        out.update(
            {
                "branch": branch,
                "pushed_head": pushed or work_head,
                "auto_committed": auto_committed,
            }
        )

        # A CONTRIBUTOR PUSHES AND STOPS. This is the whole difference between
        # `integrate` and `direct-pr`, and its absence is what made them the
        # same run: every step used to reach the code below and open its own
        # pull request, so a six-step `integrate` workflow produced six of them
        # against an API whose schema, validator and docs all say it produces
        # ONE. The branch is the deliverable here; the integrator merges it.
        self._record_pushed_branch(publish_repo, branch=branch, head=str(pushed or work_head or ""))
        if carrier == "branches":
            self._carrier_pushed = {"name": branch, "head": str(pushed or work_head or "")}
        if onto_pull is not None:
            return self._publish_onto_pull_text(
                out, access=access, token=token, pull=onto_pull, branch=branch,
            )
        if pr_role == "amender":
            out["published"] = True
            out["publish_reason"] = (
                "strategy is 'single-pr' and this step is the amender: it fast-forward "
                f"pushed the author's branch {branch} and opened nothing; the author's "
                "pull request carries the push"
            )
            return out
        if strategy == "collect":
            # Reached only with `carrier: branches` (D13): the branch is the
            # carrier, and `collect` still opens nothing.
            out["published"] = True
            out["publish_reason"] = (
                "carrier is 'branches': the step's branch was pushed; strategy "
                "'collect' opens no pull request"
            )
            return out
        if role == "contributor":
            out["published"] = True
            out["publish_reason"] = (
                "strategy is 'integrate' and this step is a contributor: its "
                "branch was pushed and no pull request was opened. The "
                "integrator step merges this branch and opens the single pull "
                "request for the whole workflow."
            )
            return out

        if not access.default_branch:
            out["published"] = True
            out["publish_reason"] = (
                "the branch was pushed; no pull request was opened because the "
                "repository's default branch could not be read"
            )
            return out

        # THE AGENT'S OWN TITLE AND BODY WHEN IT WROTE THEM (archived issue 214), so a
        # platform pull request can say `Closes #N`. The platform's block --
        # provenance, the commit note, an integration's missing branches --
        # follows the agent's text, whole: a reviewer still learns what ran
        # and what this pull request does NOT contain. Read here, after the
        # pre-publish reap, so no agent process is left to change the files.
        generated = self._pull_request_body(
            branch=branch, auto_committed=auto_committed, merge=merge, folded=folded, kept=kept
        )
        agent_title, agent_body, refused = self._agent_pull_request_text()
        title = agent_title or self._generated_pull_request_title()
        # WHAT THE PRE-PUBLISH GATE COULD NOT CLEAR (SPEED-D1): published
        # anyway, never dropped, and said where the operator reads first.
        prepublish = self._prepublish_section()
        if prepublish:
            generated = f"{prepublish}\n\n{generated}"
            out["prepublish_checks_failed"] = True
        # WHAT THE GATE ADVISES (CTXQ-G): test reach and the self-check, for
        # the reader. It sets nothing in `out` that a merge path reads.
        advisory = self._prepublish_advisory()
        if advisory:
            generated = f"{generated}\n\n{advisory}"
        body = f"{agent_body}\n\n---\n\n{generated}" if agent_body else generated
        # OUTSIDE THE LANE'S TERRITORY (TERRITORY-WARN, part of archived issue 1468): the
        # pull request still opens, its body STARTING with the paths and
        # carrying the label (owner decision, 2026-10-19). The territory is
        # the task's stored input, never anything the agent wrote.
        outside, labels = self._territory_warning(
            publish_repo, head=str(pushed or work_head or ""), out=out
        )
        outside_header = territory_mod.warning_header(outside) if outside else ""
        if outside_header:
            body = f"{outside_header}\n\n---\n\n{body}"
        # The generated block quotes the step's prompt, which is anyone's
        # text too; the whole body is neutralised, so no part of what the
        # platform posts can page anyone (owner decision, 2026-09-29). The
        # agent's part already was, and a second pass changes nothing.
        body = _neutralise_mentions(body)
        # The console links go on AFTER the neutralising pass, so no joiner
        # can land inside a URL; they are built from the platform's own task
        # and workflow ids and the scheduler's console origin, and a workflow
        # id is percent-encoded, so they carry no `@` of anyone's.
        body = pr_body_with_console_links(
            body,
            origin=cfg.console_url,
            task_id=cfg.task_id,
            workflow_id=str((self._task or {}).get("workflow_id") or "") or None,
            enabled=cfg.pr_console_links,
        )
        # WHAT AN ADOPTED PULL REQUEST GAINS (archived issue 807): its body is kept and this
        # one marked section is appended -- the agent's body, if it wrote one,
        # then a short provenance block. `body` above is for a pull request
        # this attempt OPENS; a CI fixer's republish that sent it as a
        # replacement is what dropped archived pull request 775's `Closes #N` lines.
        amendment = pr_body_with_console_links(
            _neutralise_mentions(
                self._pull_request_amendment(
                    agent_body=agent_body, branch=branch, head=str(pushed or work_head or "")
                )
                + (f"\n\n{prepublish}" if prepublish else "")
                # A push onto an open pull request says WHERE its warning is,
                # not the warning: that is one comment, posted below once the
                # pull request is adopted, since a section at the bottom of a
                # long body is easy to miss (CIFIX-TERRITORY-2, part of archived issue 1499).
                + (f"\n\n{territory_mod.COMMENTED_NOTE}" if outside else "")
            ),
            origin=cfg.console_url,
            task_id=cfg.task_id,
            workflow_id=str((self._task or {}).get("workflow_id") or "") or None,
            enabled=cfg.pr_console_links,
        )
        out["pull_request_text"] = {
            "title": "agent" if agent_title else "platform",
            "body": "agent" if agent_body else "platform",
        }
        if refused:
            out["pull_request_text_refused"] = refused
        if title is None:
            # NO TITLE IS INVENTED (owner decision, 2026-09-28). The branch is
            # pushed; the pull request waits for a title the agent writes.
            # An attempt with retries left never gets here without the file:
            # `_title_owed` made `pr-title.txt` an expected output, so its
            # absence failed the attempt retryably before the publish.
            out["published"] = True
            out["publish_reason"] = (
                f"the branch was pushed; no pull request was opened because the "
                f"agent wrote no usable {PR_TITLE_FILE}. Write a one-line title "
                f"to $SWARM_ARTIFACTS_DIR/{PR_TITLE_FILE} (no task id, no "
                f"attribution) and retry"
            )
            self.log.warning("no pull request opened: no usable agent title", refused=refused)
            return out
        # The token the pull request was last asked with: after a 401 re-read
        # (`_open_pull_request_reading_again`) the first one is dead, and the
        # territory comment below goes with the one that worked.
        asked_with = [token]

        def open_with(with_token: str) -> PullRequest:
            asked_with[0] = with_token
            # Here, so the open after a 401 re-read reads the fence again too.
            self._own_before_forge_write("pull request")
            # Retried on an outage: a POST that timed out after GitHub made
            # the pull request is answered 422 on the next try, which
            # `open_pull_request` resolves by adopting the open one.
            return forge_mod.retry_transient(
                lambda: open_pull_request(
                    access=access,
                    token=with_token,
                    head=branch,
                    base=access.default_branch,
                    title=title,
                    body=body,
                    # A reused pull request keeps its body and gains this
                    # section; nothing it already says is replaced (archived issue 807).
                    amendment=amendment,
                    # An adopted pull request is retitled only when its title
                    # carries the task id (the OLD generated `[swarm] <task
                    # id>`: the owner's rule is that a title never does, and
                    # that has to reach a pull request this attempt only
                    # adopts), or when the agent wrote `pr-title.txt` and the
                    # title is the worker's own default (archived issue 807). The
                    # implementer's title, or a human's, is kept.
                    retitle_if=lambda current: self._title_carries_task_id(current) or (
                        agent_title is not None and self._is_default_title(current)
                    ),
                    # Only when there is one: a step inside its territory, or
                    # with none declared, asks exactly what it always asked.
                    **({"labels": labels} if labels else {}),
                ),
                policy=self._forge_retry(),
                what="the pull request",
            )

        try:
            # A 401 reads the slot again once and tries once more (archived issue 1623).
            pr = self._open_pull_request_reading_again(open_with, token, branch=branch)
        except ForgeError as exc:
            message = str(self._scrub(str(exc)[:600]))
            out["published"] = True
            out["publish_reason"] = (
                f"the branch was pushed but no pull request was opened: {message}"
            )
            if message.startswith(f"branch {branch} pushed; pull request not opened"):
                # The 401 re-read did not open it (twice refused, or the
                # re-read refused): the step's pull request does not exist,
                # and the finish fails it saying where the work is
                # (`_published_nothing`). Not retried: the next attempt
                # would read the same slot.
                out[PULL_REQUEST_NOT_OPENED_FIELD] = message
            elif message.startswith(PULL_REQUEST_REFUSED):
                # The forge answered and said no (`forge.open_pull_request`'s
                # 422, e.g. "No commits between main and swarm/..."): a fact
                # about this branch that a retry would meet again, so the
                # finish fails the step for it (`_published_nothing`). An
                # unreachable forge is not a refusal.
                out["pull_request_refused"] = message
            elif isinstance(exc, forge_mod.ForgeUnavailable):
                # An outage that outlasted the in-process retry: the finish
                # fails the ATTEMPT retryably for it (`_publish_unreachable`),
                # and the next attempt's 422 adopts a pull request this one
                # may have opened before its answer was lost.
                out[PUBLISH_UNREACHABLE_FIELD] = self._unreachable_marker(exc)
            return out

        if labels:
            out["out_of_territory"]["labelled"] = list(getattr(pr, "labelled", ()) or ())
            label_error = str(getattr(pr, "label_error", "") or "")
            if label_error:
                out["out_of_territory"]["label_error"] = str(self._scrub(label_error[:300]))
                self.log.warning("the out-of-territory label was not set", error=label_error[:300])
            if pr.number and not pr.created:
                self._post_territory_comment(
                    access=access, token=asked_with[0], number=pr.number,
                    outside=outside, out=out,
                )
        updated = bool(getattr(pr, "updated", False))
        retitled = bool(getattr(pr, "retitled", False))
        appended = bool(getattr(pr, "appended", False))
        refused_lines = list(getattr(pr, "body_refused", ()) or ())
        if refused_lines:
            # Never reached by the worker's own append, which keeps every line;
            # said out loud if it ever is, since it means a body was withheld.
            out["pull_request_body_refused"] = (
                f"{len(refused_lines)} closing-keyword line(s) would have been dropped; "
                "the body was not sent"
            )
            self.log.warning(
                "an adopted pull request's body was not replaced: it would have "
                "dropped closing-keyword lines",
                lines=len(refused_lines),
            )
        out.update(
            {
                "published": True,
                "pull_request": {
                    "number": pr.number,
                    "url": pr.url,
                    "state": pr.state,
                    "created": pr.created,
                    "updated": updated,
                    "retitled": retitled,
                    "appended": appended,
                },
                "publish_reason": (
                    "opened"
                    if pr.created
                    else "an open pull request already existed and was reused, its body kept"
                    + (", with this attempt's section appended" if appended else "")
                    + (", retitled" if retitled else "")
                ),
            }
        )
        self.log.info("pull request ready", number=pr.number, url=pr.url, created=pr.created)
        return out

    def _publish_onto_pull_text(
        self, out: dict[str, Any], *, access: RepoAccess, token: str,
        pull: PullHead, branch: str,
    ) -> dict[str, Any]:
        """The end of a publish onto an existing pull request (archived issue 2183).

        The branch is pushed; no pull request is opened, and the pull
        request's title and body are NEVER edited (part of archived issue 4101). A squash
        merge takes the title as the commit subject on main: on 2026-11-14 a
        one-line follow-up's `pr-title.txt` retitled a feature pull request
        to that fix. A `pr-title.txt` the agent wrote is therefore ignored,
        and a usable `pr-body.md` is posted as one comment instead
        (`_post_pull_body_comment`); a forge that does not take it leaves the
        push published, saying so.
        """
        from .lifecycle import _neutralise_mentions
        agent_title, agent_body, refused = self._agent_pull_request_text()
        if refused:
            out["pull_request_text_refused"] = refused
        body = _neutralise_mentions(agent_body) if agent_body else None
        out["pull_request_text"] = {
            "title": "kept",
            "body": "kept, the agent's posted as a comment" if body else "kept",
        }
        if agent_title:
            self.log.info("publish_to_pr: the agent's pr-title.txt is not applied; "
                          "the pull request keeps its title", number=pull.number)
        commented = bool(body) and self._post_pull_body_comment(
            access=access, token=token, number=pull.number, body=body or "", out=out,
        )
        out.update({
            "published": True,
            "pull_request": {
                "number": pull.number,
                "url": pull.url,
                "state": pull.state,
                "created": False,
                "updated": False,
                "retitled": False,
                "body_replaced": False,
                "commented": commented,
            },
            "publish_reason": (
                f"publish_to_pr: fast-forward pushed {branch}, the head of pull request "
                f"#{pull.number}, and opened no pull request; its title and body kept"
                + (", the agent's body posted as a comment" if commented else "")
            ),
        })
        self.log.info("published onto the existing pull request", number=pull.number,
                      branch=branch)
        return out

    def _post_pull_body_comment(
        self, *, access: RepoAccess, token: str, number: int, body: str,
        out: dict[str, Any],
    ) -> bool:
        """ONE comment on pull request `number` carrying the agent's
        `pr-body.md` (part of archived issue 4101); True when it is there.

        Ends with a hidden marker naming this task and attempt, so a publish
        of the same attempt again finds it and posts nothing, as
        `_post_territory_comment` does. The fence is read again just before
        the POST (`_own_before_forge_write`, archived issue 2693, invariant 5). A comment
        the forge refuses is recorded in `out["pull_request_comment_error"]`
        and logged; the push stands.
        """
        marker = (
            f"<!-- swarm:publish-to-pr-body task={self.cfg.task_id} "
            f"attempt={self.cfg.attempt_id} -->"
        )
        ref = access.ref
        try:
            # A read is retried on an outage; the POST is not, since one whose
            # answer was lost may have made the comment (the marker finds it).
            existing = forge_mod.retry_transient(
                lambda: forge_mod.list_issue_comments(ref=ref, token=token, number=number),
                policy=self._forge_retry(),
                what="the pull request's comments",
            )
        except ForgeError as exc:
            # Unread is not "none there": a second copy is better than none.
            existing = []
            self.log.warning("the pull request's comments could not be read",
                             error=str(self._scrub(str(exc)[:300])))
        if any(marker in text for text in existing):
            return True
        self._own_before_forge_write("pull request comment")
        try:
            forge_mod.create_issue_comment(
                ref=ref, token=token, number=number, body=f"{body}\n\n{marker}"
            )
        except ForgeError as exc:
            error = str(self._scrub(str(exc)[:300]))
            out["pull_request_comment_error"] = error
            self.log.warning("the agent's pull request body was not posted as a comment",
                             error=error)
            return False
        return True
