"""A review step's runtime, chosen when it is readied from what its parents changed (#2322).

BGD's decision, 2026-10-25: "if a review only does code review and a UI
review or UI QA is not needed, it should not dispatch a review task with a
browser runtime." Until then the submitter named every step's profile at
submission, before anyone knew whether the change touched UI: 43 browser pods
in a day ran reviews of scheduler code, and 8 reviews of `apps/swarm-ui` ran
on `claude-code` and were forced NOT_YET by the worker's rendered-review gate
(`agent_worker.verdict.gate_rendered_review`).

SO A STEP MAY ASK FOR "BROWSER ONLY IF THE CHANGE IS UI". swarm-api accepts
`metadata.runtime = {"policy": "browser_if_ui", "browser":
"claude-code-browser", "code": "claude-code"}` on a workflow step
(`swarm_api.validation.resolve_runtime_policy`), stores the step on the
BROWSER profile -- the review that can see, which is also what a step that is
never resolved here runs -- and signs the spec under BOTH profiles: the
stored one as always, and the other into `metadata.runtime.alternate`
(`swarm_api.specsigning.sign_task_specs`). The scheduler holds no signing
key, and the worker refuses a spec whose `runner_profile` swarm-api did not
sign (`agent_worker.specverify`), so a swap is only ever to a spec swarm-api
already signed for this task id.

WHEN. In `Scheduler._promote_dependant`, in the SAME guarded transaction that
writes PARKED -> READY (`SchedulerStore.promote_to_ready`'s `patch_from`).
The step holds nothing then and has never been admitted: admission reads the
task after this write, so its pools, its lease and its dispatch are all the
resolved profile's (invariants 2 and 3). Once resolved, `runtime.resolved` is
set and a later promotion (a retry, a manual pause) never resolves again.

WHAT IS READ: the parents' `result_summary.git.files` -- the record the
worker's gate reads for its `builds_on` step (`ui_paths_in_files`) -- for
every `depends_on` parent and, along each parent's own `builds_on`, the steps
whose branches it started from: a chain's last step lists only its own diff.
A parent that cannot be read, has no file list, or whose list was cut with no
UI path in what it kept resolves to the BROWSER: a review that could not be
told what it reviews is not one that saw it.

THE RE-REVIEW (`swarm_api.validation.rereview_step_for`) carries
`runtime.inherit`: it runs on whatever its review RESOLVED to, read from the
review's task, so a lane's second look sees what its first did.

THE UI-PATH RULE is `is_ui_path` below, held equal to the worker's
`agent_worker.verdict.is_ui_path` by
tests/unit/control_plane/test_runtime_by_diff.py. It is restated, not shared:
the scheduler image carries `swarm_common` and `quota_broker` only, the
worker image `swarm_common` and `swarm_redaction` only, and `swarm_common` is
frozen. Test files are not UI (fix 6 of the 2026-10-25 audit): a change to
`__tests__/`, a `tests/` directory or a `*.test.*` file renders nothing new.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: The task metadata key the policy lives under. Restated from
#: `swarm_api.validation.RUNTIME_METADATA_KEY` (the scheduler image does not
#: carry swarm-api); held equal by the parity test.
RUNTIME_METADATA_KEY = "runtime"
POLICY_BROWSER_IF_UI = "browser_if_ui"
#: The one pair the policy may name, by catalogue name (invariant 10).
BROWSER_PROFILE = "claude-code-browser"
CODE_PROFILE = "claude-code"

#: The task fields a profile decides, swapped together with the signature
#: that covers them. Restated from `swarm_api.validation.RUNTIME_SWAP_FIELDS`.
RUNTIME_SWAP_FIELDS = (
    "runner_profile", "resource_class", "timeout_seconds", "provider", "model", "model_class",
)
SIGNATURE_FIELDS = ("spec_signature", "spec_key_version", "spec_format")

#: The repository paths whose change makes a review need a browser. The
#: worker's `agent_worker.verdict.UI_PATH_PREFIXES`, restated.
UI_PATH_PREFIXES = ("apps/swarm-ui/", "docs/web-ui/")
#: Directories whose files are tests, wherever they sit under a UI prefix.
TEST_DIRECTORIES = ("__tests__", "tests")
#: A file under `apps/swarm-ui/` whose name carries one of these is a vitest
#: file (`App.test.tsx`, `Accounts.spec.tsx`). Only there: a `docs/web-ui/`
#: page is rendered whatever it is called (#2379, F03).
TEST_FILE_MARKS = (".test.", ".spec.")
TEST_FILE_MARK_PREFIX = "apps/swarm-ui/"

#: Task documents read per resolution: the parents and the `builds_on` chain
#: behind them. A workflow has at most 50 parents per step; a chain longer
#: than this resolves to the browser rather than reading on.
MAX_DOCUMENTS_READ = 40
#: UI paths kept in the event and on the task.
MAX_UI_PATHS_SHOWN = 20

#: Why a step resolved as it did, in the event.
REASON_UI = "ui_paths"
REASON_NO_UI = "no_ui_paths"
REASON_UNREADABLE = "file_list_unreadable"
REASON_CUT = "file_list_cut"
REASON_NO_PARENTS = "no_parent_changes"
REASON_INHERITED = "inherited"
REASON_INHERIT_UNREADABLE = "inherited_unreadable"


def is_test_path(path: str) -> bool:
    """Whether a repository path is a test file.

    Under a `__tests__/` or `tests/` directory, or, under `apps/swarm-ui/`,
    named `*.test.*` or `*.spec.*`.
    """
    text = path.lstrip("/")
    parts = text.split("/")
    if any(part in TEST_DIRECTORIES for part in parts[:-1]):
        return True
    return text.startswith(TEST_FILE_MARK_PREFIX) and any(
        mark in parts[-1] for mark in TEST_FILE_MARKS)


def is_ui_path(path: Any) -> bool:
    """Whether a changed path is one a rendered review must see (test files are not)."""
    if not isinstance(path, str):
        return False
    text = path.lstrip("/")
    return text.startswith(UI_PATH_PREFIXES) and not is_test_path(text)


def policy_of(metadata: Any) -> dict[str, Any] | None:
    """The step's `browser_if_ui` block, or None for any other step.

    The exact pair only: a block naming any other profile is not a policy this
    scheduler acts on, whatever a tenant wrote into its own task document.
    """
    block = metadata.get(RUNTIME_METADATA_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(block, Mapping) or block.get("policy") != POLICY_BROWSER_IF_UI:
        return None
    if block.get("browser") != BROWSER_PROFILE or block.get("code") != CODE_PROFILE:
        return None
    return dict(block)


@dataclass(frozen=True)
class Resolution:
    """What a policy step resolved to, and why."""

    profile: str
    reason: str
    ui_paths: tuple[str, ...] = ()
    #: Task ids read, in order, and those that could not be told.
    read: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = field(default=())

    def to_detail(self) -> dict[str, Any]:
        return {
            "policy": POLICY_BROWSER_IF_UI,
            "profile": self.profile,
            "reason": self.reason,
            "ui_paths": list(self.ui_paths[:MAX_UI_PATHS_SHOWN]),
            "ui_paths_count": len(self.ui_paths),
            "parents_read": list(self.read),
            "unreadable": list(self.unreadable),
        }


def files_of(document: Any) -> tuple[list[str], str | None]:
    """`(UI paths, why unknown)` from one task document's `result_summary.git`.

    Unknown (`REASON_UNREADABLE`) when there is no file list to read -- no
    summary, no git record, a harvest that failed -- and `REASON_CUT` when
    the list was cut at its bound and the part kept names no UI path.
    """
    summary = document.get("result_summary") if isinstance(document, Mapping) else None
    git = summary.get("git") if isinstance(summary, Mapping) else None
    rows = git.get("files") if isinstance(git, Mapping) else None
    if not isinstance(rows, list):
        return [], REASON_UNREADABLE
    found: dict[str, None] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for key in ("path", "old_path"):
            value = row.get(key)
            if is_ui_path(value):
                found.setdefault(value.lstrip("/"), None)
    if git.get("files_truncated") is True and not found:
        return [], REASON_CUT
    return list(found), None


def _builds_on(document: Mapping[str, Any]) -> str | None:
    metadata = document.get("metadata")
    block = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
    raw = block.get("builds_on") if isinstance(block, Mapping) else None
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def resolve(
    task_depends_on: list[str],
    policy: Mapping[str, Any],
    read: Callable[[str], Mapping[str, Any] | None],
) -> Resolution:
    """The profile a `browser_if_ui` step runs, from its parents' changed files.

    `read(task_id)` is the tenant-gated document read (None for a missing
    document or another tenant's). Every failure resolves to the browser.
    """
    inherit = policy.get("inherit")
    if isinstance(inherit, Mapping):
        task_id = inherit.get("task_id")
        document = read(task_id) if isinstance(task_id, str) and task_id in task_depends_on else None
        profile = document.get("runner_profile") if document is not None else None
        if profile in (BROWSER_PROFILE, CODE_PROFILE):
            return Resolution(profile, REASON_INHERITED, read=(task_id,))
        return Resolution(BROWSER_PROFILE, REASON_INHERIT_UNREADABLE,
                          unreadable=(str(task_id),))
    queue = list(dict.fromkeys(task_depends_on))
    seen: set[str] = set()
    found: dict[str, None] = {}
    visited: list[str] = []
    unknown: list[str] = []
    cut = False
    while queue:
        task_id = queue.pop(0)
        if task_id in seen:
            continue
        seen.add(task_id)
        if len(visited) >= MAX_DOCUMENTS_READ:
            unknown.append(task_id)
            break
        visited.append(task_id)
        document = read(task_id)
        if document is None:
            unknown.append(task_id)
            continue
        paths, why = files_of(document)
        found.update(dict.fromkeys(paths))
        if why is not None:
            cut = cut or why == REASON_CUT
            unknown.append(task_id)
        upstream = _builds_on(document)
        if upstream and upstream not in seen:
            queue.append(upstream)
    ui = tuple(found)
    if ui:
        return Resolution(BROWSER_PROFILE, REASON_UI, ui, tuple(visited), tuple(unknown))
    if unknown:
        return Resolution(BROWSER_PROFILE, REASON_CUT if cut else REASON_UNREADABLE, (),
                          tuple(visited), tuple(unknown))
    if not visited:
        return Resolution(BROWSER_PROFILE, REASON_NO_PARENTS)
    return Resolution(CODE_PROFILE, REASON_NO_UI, (), tuple(visited))


def swap_patch(stored: Mapping[str, Any], resolution: Resolution, at: Any) -> dict[str, Any]:
    """The fields to write with the promotion, from the document AS STORED in the transaction.

    The stored profile already the resolution: only `runtime.resolved` is
    written. The stored `alternate` the resolution: every profile field and
    the signature are swapped with it, so the pair stays whole and the step
    is the spec swarm-api signed for that profile. Anything else -- an
    alternate that is missing, or names another profile -- swaps nothing and
    the step runs as stored (the browser, as swarm-api stored it).
    """
    metadata = dict(stored.get("metadata") or {})
    block = dict(metadata.get(RUNTIME_METADATA_KEY) or {})
    record = {**resolution.to_detail(), "at": at}
    patch: dict[str, Any] = {}
    alternate = block.get("alternate")
    if stored.get("runner_profile") == resolution.profile:
        record["swapped"] = False
    elif isinstance(alternate, Mapping) and alternate.get("runner_profile") == resolution.profile:
        previous = {name: stored.get(name) for name in (*RUNTIME_SWAP_FIELDS, *SIGNATURE_FIELDS)}
        for name in (*RUNTIME_SWAP_FIELDS, *SIGNATURE_FIELDS):
            patch[name] = alternate.get(name)
        block["alternate"] = previous
        record["swapped"] = True
    else:
        record["swapped"] = False
        record["not_swapped_because"] = "no signed alternate for this profile"
        record["profile"] = stored.get("runner_profile")
    block["resolved"] = record
    metadata[RUNTIME_METADATA_KEY] = block
    patch["metadata"] = metadata
    return patch
