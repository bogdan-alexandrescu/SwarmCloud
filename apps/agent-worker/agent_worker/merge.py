"""The `merge` worker action: merge the one pull request a `single-pr` chain opened (#295).

A step that runs no agent (`WorkerAction.MERGE`, contract request 33). Its
service account, `swarm-<tenant>-merge`, is the sole reader of the merge App's
key, so the credential that can land code on `main` is never in a container an
agent has run in (docs/merge-step.md §0, §1.3). It is DISABLED for every
tenant (owner decision, 2026-10-01): the profile is `available=False` until
#342 is enforced and the owner has created the review and merge Apps.

THE ORDER IS §2.2's, AND EVERY CHECK IS §5's AND §6's. A step that fails stops
everything after it; a refusal ends the task FAILED with MERGE_REFUSED and its
code in `result_summary.merge.refusal`, never retried -- the next attempt would
read the same facts (§6). In order:

  credential-free, before any secret is read (§2.2 steps 1-5):
    worker_unprotected, processes_alive, merges_invalid, spec_unverified
    (the union of author, review, post-verdict, fix and proof, row 42),
    verdict_unreadable (review.json at the verdicts path, proof.json staged),
    not_proved, review_not_at_head, heads_disagree, pull_request_unknown,
    title_placeholder;
  the Job's forge record (row 14, CANNOT_START): forge_host_invalid;
  the secret and the token (rows 15-17, CANNOT_START);
  the forge reads (§5.1, §5.2, §5.2a):
    already merged (row 34 SUCCEEDED, or merged_at_other_head),
    pull_request_closed, pull_request_not_this_workflows, base_not_default,
    draft, head_moved, title_placeholder, title_changed,
    touches_protected_paths / too_many_files, no_required_checks,
    required_check_unpinned, checks_pending, checks_failed, verdict_mismatch,
    verdict_not_approved, conflict, mergeability_unknown;
  the human gate (B13r): SUCCEEDED awaiting a person, nothing merged;
  re-check fencing, cancel and base.ref (§2.2 step 8): base_retargeted;
  the merge (§5.3): head_moved on 409, MERGE_FAILED forge_refused on 405,
    forge_redirect_refused (REFUSED on a read, FAILED on the merge call);
  the record (§5.4) and the post-merge base.ref read (row 41);
  revoke, whatever happened (§2.2 step 11).

THE RULES `auto-merge.yml` ALSO STATES ARE THE MODULE-LEVEL FUNCTIONS BELOW
(`title_is_placeholder`, `other_check_blocks`, `required_check_state`), so
tests/unit/scripts/test_auto_merge_workflow.py can hold them to the gate's
shell from one table of cases (§8). Where they differ, this is the stricter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from swarm_common.models import EndCause
from swarm_common.states import TaskState

from . import forge as forge_mod
from .errors import InputUnavailable
from .post_verdict import (
    ActionContext,
    ActionOutcome,
    Review,
    VerdictUnreadable,
    _outcome,
    _task_id,
    cancelled,
    cannot_start,
    pull_request_belongs,
    pull_request_number,
    read_verdict,
    refusal,
    repo_path,
    unavailable,
    verdict_key,
)
from .specverify import UpstreamSpecUnverified

#: The keys of the signed `dispatch.merges` block, in its order (swarm-api's
#: `validation.MERGES_KEYS`, restated: the worker image carries no control
#: plane; tests/unit/worker/test_merge_action.py holds the two equal).
MERGES_KEYS = ("author", "review", "post-verdict", "fix", "proof")
#: Every key but `fix`, which is present only when the chain has an amender.
REQUIRED_MERGES_KEYS = ("author", "review", "post-verdict", "proof")
#: The one file the merge stages, from the proof (swarm-api's PROOF_FILENAME).
PROOF_FILENAME = "proof.json"
MAX_PROOF_BYTES = 256 * 1024

#: The worker's own placeholder title, compared case- and leading-whitespace-
#: insensitively: auto-merge.yml gate 1's rule.
PLACEHOLDER_TITLE_PREFIX = "[swarm] task_"
#: The worker's `pr-title.txt` rule (#214): the retired `[swarm] task_...`
#: shape ANYWHERE in the title, any spacing, any case. `lifecycle.
#: _RETIRED_TITLE_RE`, restated because lifecycle imports this module;
#: tests/unit/worker/test_merge_action.py holds the two equal.
RETIRED_TITLE_RE = re.compile(r"\[swarm\]\s*task_", re.IGNORECASE)

#: The paths an unattended merge may never land (§5.1, §7 T6, M4). A change to
#: any of these exercises the deploy identity `release.yml` runs the merge
#: under, not only CI's configuration.
PROTECTED_PREFIXES = (".github/", "scripts/", "terraform/", "kubernetes/", "images/")
#: Matched by file name at any depth: a nested pyproject.toml or Makefile is a
#: build definition as much as the root's is, so the stricter reading is taken.
PROTECTED_NAMES = ("Makefile", "pyproject.toml", "uv.lock", "conftest.py")
#: GitHub's own cap on `pulls/{n}/files` (§5, row 25).
MAX_PULL_REQUEST_FILES = 3000

#: §5.2 3: what counts as green for a REQUIRED check. The owner's rule:
#: success or skipped, nothing else -- `neutral` included.
REQUIRED_GREEN = frozenset({"success", "skipped"})
#: §5.2 4: what an OTHER check run may conclude without holding the merge,
#: auto-merge.yml gate 5's own tolerance plus nothing.
OTHER_TOLERATED = frozenset({"success", "skipped", "neutral"})

#: `mergeable: null` is read again at most twice, 2 s apart (§5.1): a bounded
#: pause in seconds, not a provider wait (invariant 4).
MERGEABLE_REREADS = 2
MERGEABLE_REREAD_SECONDS = 2.0

#: The merge App's token: this repository, and these two permissions (§2.1).
#: No `workflows`: an unattended chain must not land a workflow change (T6).
MERGE_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

_SHA = re.compile(r"^[0-9a-f]{40}$")


# ---------------------------------------------------------------------------
# The rules auto-merge.yml also states (§8)
# ---------------------------------------------------------------------------


def title_is_placeholder(title: str) -> bool:
    """Gate 1's prefix, OR the worker's own `pr-title.txt` rule.

    Gate 1 refuses `[swarm] task_` after leading whitespace, in any case;
    the author's worker refuses a `pr-title.txt` carrying the retired shape
    anywhere, with any spacing (#214). Every gate-1 refusal is also the
    worker's, so the union is the worker's rule: a title the author's worker
    accepted is never refused here as a placeholder, and nothing gate 1
    refuses is merged. STRICTER than gate 1 (`Fix [swarm] task_ handling`
    passes the gate and is refused here) -- §8 allows the merge to be the
    stricter, never the looser.
    """
    return (
        title.lstrip().lower().startswith(PLACEHOLDER_TITLE_PREFIX)
        or RETIRED_TITLE_RE.search(title) is not None
    )


def other_check_blocks(run: Mapping[str, Any]) -> bool:
    """§5.2 4 / gate 5: a check run at the head that holds the merge.

    Not completed, or completed with a conclusion other than success,
    skipped or neutral. STRICTER than gate 5, which holds only on failure,
    timed_out, action_required and cancelled: `stale`, `startup_failure` and
    an absent conclusion hold here and pass there (§8, "where they
    deliberately differ").
    """
    if run.get("status") != "completed":
        return True
    return (run.get("conclusion") or "") not in OTHER_TOLERATED


def required_check_state(runs: list[Mapping[str, Any]]) -> str:
    """§5.2 2-3 over the runs of ONE required check by its pinned App:
    "green", "pending" (none, or not completed) or "failed"."""
    if not runs:
        return "pending"
    if any(run.get("status") != "completed" for run in runs):
        return "pending"
    if all((run.get("conclusion") or "") in REQUIRED_GREEN for run in runs):
        return "green"
    return "failed"


def protected_paths(files: list[Mapping[str, Any]]) -> list[str]:
    """The files of the pull request that touch a protected path, old names included."""
    hits: list[str] = []
    for entry in files:
        for name in (entry.get("filename"), entry.get("previous_filename")):
            if not isinstance(name, str) or not name:
                continue
            base = name.rsplit("/", 1)[-1]
            if name.startswith(PROTECTED_PREFIXES) or base in PROTECTED_NAMES:
                hits.append(name)
    return sorted(set(hits))


# ---------------------------------------------------------------------------
# What the chain's workers recorded, and what the agents wrote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proof:
    outcome: str
    sha: str
    evidence: str


def parse_proof(raw: bytes) -> Proof:
    """proof.json: `{"outcome": "PROVED" | "NOT_PROVED", "sha", "evidence"}` (§4.1)."""
    if len(raw) > MAX_PROOF_BYTES:
        raise VerdictUnreadable(f"{PROOF_FILENAME} is larger than {MAX_PROOF_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise VerdictUnreadable(f"{PROOF_FILENAME} is not JSON") from None
    if not isinstance(data, dict):
        raise VerdictUnreadable(f"{PROOF_FILENAME} is not a JSON object")
    outcome = data.get("outcome")
    if outcome not in ("PROVED", "NOT_PROVED"):
        raise VerdictUnreadable(f"{PROOF_FILENAME}'s outcome is not PROVED or NOT_PROVED")
    sha = data.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise VerdictUnreadable(f"{PROOF_FILENAME}'s sha is not a 40-hex commit")
    evidence = data.get("evidence", "")
    return Proof(outcome=outcome, sha=sha, evidence=evidence if isinstance(evidence, str) else "")


def parse_merges(dispatch: Mapping[str, Any]) -> dict[str, str]:
    """The signed `dispatch.merges` block, `{key: task id}`, or VerdictUnreadable."""
    raw = dispatch.get("merges")
    if not isinstance(raw, Mapping):
        raise VerdictUnreadable("this step's signed dispatch block has no merges block")
    unknown = sorted(str(k) for k in raw if k not in MERGES_KEYS)
    missing = [k for k in REQUIRED_MERGES_KEYS if k not in raw]
    if unknown or missing:
        raise VerdictUnreadable(
            "the merges block must name " + ", ".join(REQUIRED_MERGES_KEYS)
            + " (and optionally fix)"
            + (f"; it lacks {', '.join(missing)}" if missing else "")
            + (f"; it names {', '.join(unknown)}" if unknown else "")
        )
    merges: dict[str, str] = {}
    for key in MERGES_KEYS:
        if key in raw:
            task_id = _task_id(raw[key])
            if task_id is None:
                raise VerdictUnreadable(f"merges.{key} is not a task id")
            merges[key] = task_id
    return merges


def _git(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = doc.get("result_summary")
    git = summary.get("git") if isinstance(summary, Mapping) else None
    return git if isinstance(git, Mapping) else {}


def _sha_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


def run_merge(ctx: ActionContext) -> ActionOutcome:
    """Merge the chain's pull request, or refuse with the first failed check."""
    summary: dict[str, Any] = {"action": "merge", "merged_by_this_task": False}

    def refuse(code: str, message: str, **kwargs: Any) -> ActionOutcome:
        return refusal(summary, EndCause.MERGE_REFUSED, code, message, **kwargs)

    # ---- §2.2 1 and 4: no credential in a process that is dumpable, or
    # beside a process the design says cannot exist.
    if ctx.unprotected:
        return refuse("worker_unprotected", ctx.unprotected)
    survivors = ctx.reap()
    if survivors:
        return refuse("processes_alive", f"{len(survivors)} process(es) survived the reap")

    # ---- §2.2 5: every claim that needs no credential.
    try:
        merges = parse_merges(ctx.dispatch)
    except VerdictUnreadable as exc:
        return refuse("merges_invalid", str(exc))
    summary["merges"] = dict(merges)

    docs: dict[str, dict[str, Any]] = {}
    try:
        for key, task_id in merges.items():
            doc = ctx.fetch_upstream(task_id)
            ctx.verify_upstream(task_id, doc)
            docs[key] = doc
    except UpstreamSpecUnverified as exc:
        return refuse("spec_unverified", str(exc), spec_check=exc.spec_check())
    except InputUnavailable as exc:
        summary["refusal"] = {"code": "upstream_unreadable", "message": str(exc)}
        return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc))

    author_id = merges["author"]
    author_branch = f"{ctx.branch_prefix}{author_id}"
    try:
        review = read_verdict(
            ctx.store, verdict_key(ctx.tenant_id, ctx.workflow_id or "", merges["review"])
        )
    except VerdictUnreadable as exc:
        return refuse("verdict_unreadable", str(exc))
    except InputUnavailable as exc:
        if getattr(exc, "retryable", False):
            summary["refusal"] = {"code": "verdict_unreadable", "message": str(exc)}
            return _outcome(TaskState.FAILED, EndCause.INPUTS_UNAVAILABLE, summary, str(exc),
                            retryable=True)
        return refuse("verdict_unreadable", str(exc))
    staged = ctx.staged.get(PROOF_FILENAME)
    try:
        if staged is None:
            raise VerdictUnreadable(f"{PROOF_FILENAME} was not staged")
        proof = parse_proof(Path(staged).read_bytes())
    except (VerdictUnreadable, OSError) as exc:
        return refuse("verdict_unreadable", str(exc))

    if proof.outcome != "PROVED":
        return refuse("not_proved", f"{PROOF_FILENAME} says {proof.outcome}")

    # §4.2: `pinned` is the commit the review WORKER cloned.
    pinned = _sha_or_none(_git(docs["review"]).get("clone_commit"))
    if pinned is None:
        return refuse("heads_disagree", "the review task recorded no clone_commit")
    summary["pinned"] = pinned
    fix_pushed = _sha_or_none(_git(docs["fix"]).get("pushed_head")) if "fix" in docs else None
    if fix_pushed is not None and fix_pushed != pinned:
        return refuse("review_not_at_head",
                      f"the fix step pushed {fix_pushed} after the review judged {pinned}")
    last_pushed = fix_pushed or _sha_or_none(_git(docs["author"]).get("pushed_head"))
    disagreeing = [
        name for name, value in (
            ("review.json.sha", review.sha),
            ("proof.clone_commit", _sha_or_none(_git(docs["proof"]).get("clone_commit"))),
            ("proof.json.sha", proof.sha),
            ("last pushed_head", last_pushed),
        ) if value != pinned
    ]
    if disagreeing:
        return refuse("heads_disagree",
                      f"{', '.join(disagreeing)} disagree with the review's clone {pinned}")
    number = pull_request_number(docs["author"])
    if number is None:
        return refuse("pull_request_unknown",
                      f"the author task {author_id} recorded no pull request number")
    summary["pull_request"] = number
    if title_is_placeholder(review.title):
        return refuse("title_placeholder", "the title the review saw is the worker's placeholder")

    # ---- the Job's forge record (row 14).
    try:
        target = forge_mod.forge_target_from_env(ctx.environ, need_bot_id=True)
    except forge_mod.ForgeHostRefused as exc:
        return cannot_start(summary, "forge_host_invalid", str(exc))
    summary["repository"] = target.full_name

    # ---- §2.2 6-7: the secret, the token, and the key dropped.
    from .secrets import CredentialMissing

    try:
        key = ctx.read_app_key()
    except CredentialMissing as exc:
        return _outcome(TaskState.PARKED, None, summary, str(exc), credential_missing=exc)
    except Exception as exc:  # noqa: BLE001 - never the value; the type is enough
        return cannot_start(summary, "credential_unreadable",
                            f"the merge App secret could not be read ({type(exc).__name__})")
    try:
        token = forge_mod.mint_installation_token(
            key=key, owner=target.owner, repo=target.repo,
            permissions=MERGE_PERMISSIONS, transport=ctx.transport,
        )
    except forge_mod.AppRejected as exc:
        return cannot_start(summary, "app_rejected", str(exc))
    except forge_mod.ForgeUnavailable as exc:
        return unavailable(summary, EndCause.MERGE_FAILED, str(exc), exc.retry_after_seconds)
    except forge_mod.ForgeError as exc:
        return refuse(getattr(exc, "code", "forge_refused"), str(exc))
    finally:
        del key
    ctx.register_secret(token.token)
    summary.update({"app_id": token.app_id, "installation_id": token.installation_id,
                    "token_expires_at": token.expires_at})
    client = forge_mod.PinnedForgeClient(token=token.token, transport=ctx.transport)
    del token
    try:
        return _with_forge(ctx, client, target, summary, refuse, review=review, proof=proof,
                           pinned=pinned, number=number, merges=merges,
                           author_branch=author_branch)
    except forge_mod.ForgeRedirectRefused as exc:
        # Row 33: REFUSED on a read, FAILED on the merge call itself.
        if summary.get("merge_called"):
            return refusal(summary, EndCause.MERGE_FAILED, exc.code, str(exc))
        return refuse(exc.code, str(exc))
    except forge_mod.PaginationCapReached as exc:
        return refuse("too_many_files" if "/files" in str(exc) else exc.code, str(exc))
    except forge_mod.ForgeUnavailable as exc:
        return unavailable(summary, EndCause.MERGE_FAILED, str(exc), exc.retry_after_seconds)
    except forge_mod.ForgeError as exc:
        cause = EndCause.MERGE_FAILED if summary.get("merge_called") else EndCause.MERGE_REFUSED
        return refusal(summary, cause, getattr(exc, "code", "forge_refused"), str(exc))
    finally:
        summary["token_revoked"] = forge_mod.revoke_installation_token(client)
        del client


def _with_forge(
    ctx: ActionContext,
    client: forge_mod.PinnedForgeClient,
    target: forge_mod.ForgeTarget,
    summary: dict[str, Any],
    refuse: Any,
    *,
    review: Review,
    proof: Proof,
    pinned: str,
    number: int,
    merges: Mapping[str, str],
    author_branch: str,
) -> ActionOutcome:
    """§2.2 8-10 and §5, with the token in hand. Raises the forge's errors."""
    # A cancel or a reclaim that arrived while the secret was read and the
    # token minted is honoured before the forge is asked anything (§2.2 8),
    # and again immediately before the merge call, below.
    if ctx.recheck():
        return cancelled(summary)
    repository = client.get_ok(repo_path(target))
    if not isinstance(repository, Mapping):
        return refuse("forge_refused", "the repository did not read as one")
    permissions = repository.get("permissions")
    if not isinstance(permissions, Mapping) or permissions.get("push") is not True:
        # Row 17: the App's installation cannot write here.
        return cannot_start(summary, "token_cannot_push",
                            "the merge App's token cannot write to this repository")
    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        return refuse("base_not_default", "the repository's default branch could not be read")
    summary["default_branch"] = default_branch

    pr_path = repo_path(target, f"/pulls/{number}")
    pr = client.get_ok(pr_path)
    if not isinstance(pr, Mapping):
        return refuse("pull_request_unknown", f"pull request {number} did not read as one")
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
    head_sha = head.get("sha")

    # §5.1, rows 34 and 35: already merged.
    if pr.get("merged") is True:
        summary["merged_by"] = (pr.get("merged_by") or {}).get("login") \
            if isinstance(pr.get("merged_by"), Mapping) else None
        if head_sha == pinned:
            summary["merge_commit"] = pr.get("merge_commit_sha")
            summary["already_merged"] = True
            return _outcome(TaskState.SUCCEEDED, None, summary, "")
        return refuse("merged_at_other_head",
                      f"pull request {number} was merged at {head_sha}, not {pinned}")
    if pr.get("state") != "open":
        return refuse("pull_request_closed", f"pull request {number} is closed and not merged")
    if not pull_request_belongs(pr, author_branch=author_branch):
        return refuse("pull_request_not_this_workflows",
                      f"pull request {number} is not {author_branch} from this repository")
    base_ref = base.get("ref")
    if base_ref != default_branch:
        return refuse("base_not_default", f"pull request {number} is based on {base_ref!r}, "
                      f"not {default_branch!r}")
    if pr.get("draft") is True:
        return refuse("draft", f"pull request {number} is a draft")
    if head_sha != pinned:
        return refuse("head_moved", f"pull request {number}'s head is {head_sha}, not {pinned}")
    title = pr.get("title") if isinstance(pr.get("title"), str) else ""
    if title_is_placeholder(title):
        return refuse("title_placeholder", "the pull request's title is the worker's placeholder")
    if title != review.title:
        return refuse("title_changed", "the pull request's title is not the one the review saw")

    files = client.paginate(f"{pr_path}/files", max_items=MAX_PULL_REQUEST_FILES)
    touched = protected_paths([f for f in files if isinstance(f, Mapping)])
    if touched:
        return refuse("touches_protected_paths",
                      "it touches " + ", ".join(touched[:20])
                      + (f" and {len(touched) - 20} more" if len(touched) > 20 else ""))

    # §5.2: the required set, each at `pinned` by its pinned App, then every
    # other run at `pinned`.
    required = forge_mod.required_status_checks(
        client.rules_for_branch(target.owner, target.repo, default_branch)
    )
    if not required:
        return refuse("no_required_checks", f"{default_branch} has no required status checks")
    unpinned = sorted({c.context for c in required if c.app_id is None})
    if unpinned:
        return refuse("required_check_unpinned",
                      "required checks with no app_id: " + ", ".join(unpinned))
    runs_path = repo_path(target, f"/commits/{pinned}/check-runs")
    pending: list[str] = []
    failed: list[str] = []
    for check in required:
        runs = client.paginate(runs_path, query={"check_name": check.context,
                                                 "filter": "latest"}, key="check_runs")
        mine = [r for r in runs if isinstance(r, Mapping)
                and isinstance(r.get("app"), Mapping) and r["app"].get("id") == check.app_id]
        state = required_check_state(mine)
        if state == "pending":
            pending.append(check.context)
        elif state == "failed":
            failed.append(f"{check.context} ({', '.join(sorted({str(r.get('conclusion')) for r in mine}))})")
    names = {c.context for c in required}
    for run in client.paginate(runs_path, key="check_runs"):
        if not isinstance(run, Mapping) or run.get("name") in names:
            continue
        if other_check_blocks(run):
            label = f"{run.get('name')} ({run.get('conclusion') or run.get('status')})"
            (pending if run.get("status") != "completed" else failed).append(label)
    if failed:
        return refuse("checks_failed", f"at {pinned}: " + ", ".join(failed))
    if pending:
        return refuse("checks_pending", f"at {pinned}: " + ", ".join(pending))

    # §5.2a: the latest review by the review App's bot user, at `pinned`.
    reviews = client.paginate(f"{pr_path}/reviews")
    by_app = [r for r in reviews if isinstance(r, Mapping)
              and isinstance(r.get("user"), Mapping)
              and r["user"].get("id") == target.review_app_bot_id]
    latest = by_app[-1] if by_app else None
    latest_state = latest.get("state") if latest else None
    if latest_state in ("APPROVED", "CHANGES_REQUESTED") and \
            (latest_state == "APPROVED") != (review.verdict == "MERGE"):
        return refuse("verdict_mismatch",
                      f"review.json says {review.verdict}; the review App's review is {latest_state}")
    if latest is None or latest_state != "APPROVED" or latest.get("commit_id") != pinned:
        return refuse("verdict_not_approved",
                      "no APPROVED review from the review App at " + pinned
                      + (f"; its latest is {latest_state} at {latest.get('commit_id')}"
                         if latest else ""))
    summary["review_id"] = latest.get("id")

    # §5.1: mergeable, read again at most twice when GitHub has not computed it.
    mergeable = pr.get("mergeable")
    rereads = 0
    while mergeable is None and rereads < MERGEABLE_REREADS:
        rereads += 1
        ctx.sleep(MERGEABLE_REREAD_SECONDS)
        again = client.get_ok(pr_path)
        mergeable = again.get("mergeable") if isinstance(again, Mapping) else None
    if mergeable is False:
        return refuse("conflict", f"pull request {number} conflicts with {default_branch}")
    if mergeable is not True:
        return refuse("mergeability_unknown",
                      f"GitHub had not computed mergeability after {MERGEABLE_REREADS} rereads")

    # ---- THE HUMAN GATE (B13r). Owner decision for B13r, built there:
    # `merge_human_gate`, when on, ends the step here, SUCCEEDED, with every
    # check above passed and nothing merged -- a person merges. Its value is
    # B13r's (the Terraform default and how it reaches the Job); the
    # lifecycle passes it as `ActionContext.human_gate`.
    if ctx.human_gate:
        summary["awaiting_human"] = True
        return _outcome(TaskState.SUCCEEDED, None, summary, "")

    # ---- §2.2 8: fencing, cancel and base.ref, immediately before the call.
    if ctx.recheck():
        return cancelled(summary)
    reread = client.get_ok(pr_path)
    reread_base = ((reread.get("base") or {}) if isinstance(reread, Mapping) else {}).get("ref")
    if reread_base != default_branch:
        return refuse("base_retargeted",
                      f"pull request {number} was retargeted to {reread_base!r}")

    # ---- §5.3: the merge, pinned to the reviewed head.
    message = provenance(ctx, target, summary, review=review, proof=proof, pinned=pinned,
                         number=number, merges=merges, required=sorted(names))
    summary["merge_called"] = True
    merged = client.request(
        "PUT", f"{pr_path}/merge",
        payload={"merge_method": "squash", "sha": pinned,
                 "commit_title": f"{title} (#{number})", "commit_message": message},
    )
    if merged.status == 409:
        return refuse("head_moved", f"GitHub answered 409: the head is no longer {pinned}")
    if merged.status != 200 or not isinstance(merged.data, Mapping) \
            or merged.data.get("merged") is not True:
        return refusal(summary, EndCause.MERGE_FAILED, "forge_refused",
                       f"the merge call answered {merged.status}"
                       + (f": {forge_mod._message_of(merged.data)}"
                          if forge_mod._message_of(merged.data) else ""))
    summary["merged_by_this_task"] = True
    summary["merge_commit"] = merged.data.get("sha")

    # ---- §2.2 10, §5.3, §5.4: the post-merge read and the record. The merge
    # stands whatever these answer.
    try:
        after = client.get_ok(pr_path)
        after_base = ((after.get("base") or {}) if isinstance(after, Mapping) else {}).get("ref")
        if after_base != default_branch:
            summary["base_mismatch_recorded"] = {"before": default_branch, "after": after_base}
    except forge_mod.ForgeError as exc:
        summary["base_recheck_failed"] = str(exc)[:300]
    try:
        comment = client.request("POST", repo_path(target, f"/issues/{number}/comments"),
                                 payload={"body": message})
        summary["recorded_on_pull_request"] = comment.status == 201
        if comment.status != 201:
            summary["recorded_on_pull_request_reason"] = f"the forge answered {comment.status}"
    except forge_mod.ForgeError as exc:
        summary["recorded_on_pull_request"] = False
        summary["recorded_on_pull_request_reason"] = str(exc)[:300]
    return _outcome(TaskState.SUCCEEDED, None, summary, "")


def provenance(
    ctx: ActionContext,
    target: forge_mod.ForgeTarget,
    summary: Mapping[str, Any],
    *,
    review: Review,
    proof: Proof,
    pinned: str,
    number: int,
    merges: Mapping[str, str],
    required: list[str],
) -> str:
    """The squash commit's body and the pull request comment (§5.4). No attribution."""
    return "\n".join([
        f"Merged by SwarmCloud task {ctx.task_id} (workflow {ctx.workflow_id}),",
        f"attempt {ctx.attempt_id}, tenant {ctx.tenant_id}, into {target.full_name}#{number}.",
        f"Reviewed at {pinned} by task {merges['review']}: APPROVED by the review App",
        f"via task {merges['post-verdict']} (review id {summary.get('review_id')}).",
        f"Proved at {pinned} by task {merges['proof']}: {proof.outcome} (staged",
        "artifact, not App-anchored).",
        f"Required checks green at {pinned}: {', '.join(required)}.",
    ])
