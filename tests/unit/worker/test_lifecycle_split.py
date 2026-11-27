# source-text: contract - lifecycle.py's line count must stay at or under the ceiling the issue 68 split set, a ratchet
"""Concerns moved out of `lifecycle.py` (part of issue 68): the publish guard into
`publish_guard.py`, then a runner's run and spend into `spend.py`, then the
account pool into `accountpool.py`, then the parks into `parking.py`, then the
publish's retryable ends into `publish_retry.py` (issue 367), then the git
harvest and publish into `gitpublish.py`.

A mechanical move: each name below lives in `publish_guard` now. The names
`lifecycle` itself still calls, or another module or a test still reads
through it, stay bound there as the SAME object, so a
`monkeypatch.setattr(lifecycle, ...)` of one the worker calls still takes
effect. The rest are not bound in `lifecycle` at all, so a stale patch of one
raises `AttributeError` rather than succeeding and changing nothing (issue
193). The line ceiling is a ratchet: `lifecycle.py` only gets shorter.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

from agent_worker import lifecycle, publish_guard

LIFECYCLE = Path(lifecycle.__file__)

#: Set just above `lifecycle.py`'s length after the move. Lower it as the
#: split goes on; never raise it.
LIFECYCLE_LINE_CEILING = 15_363

#: Moved, and still read through `lifecycle` (by itself, another module or a
#: test): bound there as the same object.
KEPT = [
    "UNCOMMITTED_COMMIT_SUBJECT",
    "_carries_attribution",
    "_ROBOT_FACE",
    "strip_attribution",
    "SCAN_WINDOW_CHARS",
    "SCAN_OVERLAP_CHARS",
    "ScanHit",
    "REGISTERED_SECRET_RULE",
    "_DiffLeakScanner",
    "_private_key_spans",
    "_scan_diff_stream",
    "is_test_path",
    "HEADERLESS_KEY_RULE",
    "GENERIC_CREDENTIAL_RULES",
    "CREDENTIAL_MIN_CHARS",
    "CREDENTIAL_MIN_ENTROPY_BITS",
    "_looks_like_a_credential",
    "_first_leaking_commit",
    "_commits_leak",
    "final_tree_leak",
    "CREDENTIAL_ADVICE",
    "credential_reason",
    "credential_refusal",
    "replay_agent_commits",
    "_recheck_runner_input",
    "_error_chain",
    "_unavailable_cause",
]

#: Moved, and read only inside `publish_guard`: not bound in `lifecycle`.
DROPPED = [
    "MAX_KEPT_COMMITS",
    "COMMIT_MESSAGE_MAX_BYTES",
    "_is_attribution_line",
    "_TRAILER_LINE",
    "clean_commit_message",
    "_is_trailer_block",
    "LeakPredicate",
    "_HUNK_NEW_START",
    "_c_unquote",
    "KEY_CONTEXT_MAX_BYTES",
    "_KEY_CONTEXT_PATHS_PER_GREP",
    "_added_inside_a_private_key",
    "REASON_PATH_MAX_CHARS",
    "_PATH_TOKEN",
    "_reason_path",
    "_published_merge_parents",
    "_REFUSED_NAMES_SHOWN",
    "_REFUSED_NAME_CHARS",
    "_refused_key_names",
    # An import only `_published_merge_parents` called: patch it in
    # `publish_guard`.
    "merged_head_kept",
]


def _defined_in_publish_guard(name: str) -> bool:
    """A function or class is defined there, unless it is a binding of the
    shared credential guard's (`swarm_redaction.credentials`)."""
    value = getattr(publish_guard, name)
    if not (inspect.isfunction(value) or inspect.isclass(value)):
        return True
    return value.__module__ in (publish_guard.__name__, "swarm_redaction.credentials")


@pytest.mark.parametrize("name", KEPT + DROPPED[:-1])
def test_each_moved_name_lives_in_publish_guard(name):
    assert hasattr(publish_guard, name), f"publish_guard does not define {name}"
    assert _defined_in_publish_guard(name), (
        f"publish_guard.{name} is defined in {getattr(publish_guard, name).__module__}"
    )


@pytest.mark.parametrize("name", KEPT)
def test_a_name_still_read_through_lifecycle_is_the_same_object(name):
    assert getattr(lifecycle, name) is getattr(publish_guard, name)


@pytest.mark.parametrize("name", DROPPED)
def test_a_name_nothing_reads_through_lifecycle_is_not_bound_there(name):
    assert not hasattr(lifecycle, name), (
        f"lifecycle.{name} is bound; read or patch publish_guard.{name}"
    )


def test_a_patch_of_a_kept_name_reaches_the_worker(monkeypatch):
    """The worker calls the guard through `lifecycle`'s binding, so a patch
    there is what it calls."""
    sentinel = object()
    monkeypatch.setattr(lifecycle, "final_tree_leak", sentinel)
    assert lifecycle.Worker.__init__.__globals__["final_tree_leak"] is sentinel


def test_lifecycle_stays_under_its_line_ceiling():
    with LIFECYCLE.open(encoding="utf-8") as handle:
        lines = sum(1 for _ in handle)
    assert lines <= LIFECYCLE_LINE_CEILING, (
        f"lifecycle.py has {lines} lines, over its ceiling of {LIFECYCLE_LINE_CEILING}; "
        "move a concern out rather than raising the ceiling"
    )


# -- a runner's run and its spend: `spend.SpendMixin` ---------------------------


def _spend():
    """`agent_worker.spend`, imported when a test runs so each test below says
    on its own what is missing."""
    return importlib.import_module("agent_worker.spend")


#: Methods `Worker` inherits from `SpendMixin`; `Worker` defines none of them.
SPEND_METHODS = [
    "_child_ended",
    "_open_run",
    "_close_run",
    "_collect_spend",
    "_note_model_mismatch",
    "_note_runner_spend_start",
    "_stopped_run_usage",
    "_record_spend",
]

#: Module names only the spend methods read: they live in `spend` and are not
#: bound in `lifecycle`, so a stale patch of one there raises.
SPEND_DROPPED = [
    "_RUN_TOKEN_KEYS",
    "_reread_of",
]

#: `usage` names the spend methods read from `spend`'s binding. `lifecycle`
#: bound them only for `_collect_spend` and binds them no more.
USAGE_DROPPED_FROM_LIFECYCLE = [
    "_unpriced_sessions",
    "_cost_incomplete",
]

#: `usage` names a test still reads through `lifecycle`: the same object in
#: `lifecycle`, `spend` and `usage`.
USAGE_KEPT = [
    "_add_spend",
    "_stream_usage",
    "_usage_summary",
]


@pytest.mark.parametrize("name", SPEND_METHODS)
def test_each_spend_method_is_inherited_from_spend_mixin(name):
    spend = _spend()
    assert name in vars(spend.SpendMixin), f"SpendMixin does not define {name}"
    assert name not in vars(lifecycle.Worker), f"Worker still defines {name} itself"
    assert getattr(lifecycle.Worker, name) is vars(spend.SpendMixin)[name]


@pytest.mark.parametrize("name", SPEND_DROPPED)
def test_a_spend_helper_lives_in_spend_alone(name):
    assert hasattr(_spend(), name), f"spend does not define {name}"
    assert not hasattr(lifecycle, name), f"lifecycle.{name} is bound; read or patch spend.{name}"


@pytest.mark.parametrize("name", USAGE_DROPPED_FROM_LIFECYCLE)
def test_a_usage_name_only_spend_reads_is_not_bound_in_lifecycle(name):
    from agent_worker import usage

    assert getattr(_spend(), name) is getattr(usage, name)
    assert not hasattr(lifecycle, name), f"lifecycle.{name} is bound; patch spend.{name}"


@pytest.mark.parametrize("name", USAGE_KEPT)
def test_a_usage_name_a_test_reads_is_one_object_everywhere(name):
    from agent_worker import usage

    assert getattr(lifecycle, name) is getattr(usage, name)
    assert getattr(_spend(), name) is getattr(usage, name)


def test_a_patch_of_a_spend_name_reaches_the_worker(monkeypatch):
    """The spend methods read their module names from `spend`, so a patch
    there is what the worker calls."""
    spend = _spend()
    sentinel = object()
    monkeypatch.setattr(spend, "agent_stream_files", sentinel)
    assert lifecycle.Worker._stopped_run_usage.__globals__["agent_stream_files"] is sentinel


# -- the account an attempt runs on: `accountpool.AccountPoolMixin` -------------


def _accountpool():
    """`agent_worker.accountpool`, imported when a test runs so each test below
    says on its own what is missing."""
    return importlib.import_module("agent_worker.accountpool")


#: Methods `Worker` inherits from `AccountPoolMixin`; `Worker` defines none of them.
ACCOUNT_METHODS = [
    "_pool_credential_env",
    "_lease_account",
    "_tenant_supplies_key",
    "_budget_env",
    "_account_channel_paths",
    "_account_channel_env",
    "_session_file_path",
    "_session_channel_env",
    "_read_session_file",
    "_read_account_channel",
    "_note_session",
    "_forward_readings",
    "_watch_account",
    "_move_account_after",
    "_report_runner_error",
    "_dead_credential",
    "_swap_account",
    "_stay_on_key",
    "_hold_began",
    "_spend_total",
    "_hold_spend",
    "_keep_credential",
    "_report_credit_exhausted",
    "_quota_account",
    "_cannot_swap",
    "_emit_swapped",
    "_emit_moved",
    "_decline_pool",
    "_emit_pool_declined",
    "_account_credential_env",
    "_reject_account",
    "_give_back",
    "_emit_account_released",
    "_release_account",
]

#: Module names only the account methods read: they live in `accountpool` and
#: are not bound in `lifecycle`, so a stale patch of one there raises.
ACCOUNT_DROPPED = [
    "MOVE_TOKEN_UNCHANGED",
    "MOVE_REFUSED_AFTER_RELOAD",
    "MOVE_REAUTH_REQUIRED",
    "MOVE_TOKEN_UNREADABLE",
    "ACCOUNT_STATE_REAUTH_REQUIRED",
    "POOL_ASKS",
    # Imports only the account methods called: patch them in `accountpool`.
    "TENANT_KEY_FIRST",
    "AccountUnreadable",
    "BrokerRefused",
    "BrokerUnavailable",
    "NoAccount",
    "SOURCE_ACCOUNT_POOL",
    "SOURCE_TENANT_OWN",
    "SWAP_PLAN_AVAILABLE",
    "credential_env_from_account",
    "spend_figure",
    "takes_account",
    "takes_key",
    "ACCOUNT_ERROR_KINDS",
    "OUTCOME_CREDIT_EXHAUSTED",
    "ACCOUNT_MOVE_ENV",
    "ACCOUNT_STREAM_ENV",
    "RESUME_MOVED",
    "SESSION_FILE_ENV",
    "STOP_DRAIN",
    "STOP_EXHAUSTED",
]

#: Names the account methods read from `lifecycle` when they run, because
#: `lifecycle` uses them outside this concern or a test reads or patches them
#: there: still defined in `lifecycle`, not bound in `accountpool`.
ACCOUNT_READ_FROM_LIFECYCLE = [
    "MAX_ACCOUNT_TRIES",
    "MAX_ACCOUNT_SWAPS",
    "MAX_CREDENTIAL_RELOADS",
    "POOL_UNANSWERED",
    "SWAP_EXHAUSTED",
    "SWAP_DRAIN",
    "SWAP_UNUSABLE",
    "_rfc3339_now",
]


@pytest.mark.parametrize("name", ACCOUNT_METHODS)
def test_each_account_method_is_inherited_from_account_pool_mixin(name):
    accountpool = _accountpool()
    assert name in vars(accountpool.AccountPoolMixin), f"AccountPoolMixin does not define {name}"
    assert name not in vars(lifecycle.Worker), f"Worker still defines {name} itself"
    assert getattr(lifecycle.Worker, name) is vars(accountpool.AccountPoolMixin)[name]


@pytest.mark.parametrize("name", ACCOUNT_DROPPED)
def test_an_account_name_lives_in_accountpool_alone(name):
    assert hasattr(_accountpool(), name), f"accountpool does not bind {name}"
    assert not hasattr(lifecycle, name), (
        f"lifecycle.{name} is bound; read or patch accountpool.{name}"
    )


@pytest.mark.parametrize("name", ACCOUNT_READ_FROM_LIFECYCLE)
def test_a_name_the_account_methods_read_from_lifecycle_stays_there(name):
    assert hasattr(lifecycle, name), f"lifecycle no longer binds {name}"
    assert not hasattr(_accountpool(), name), (
        f"accountpool binds {name}: a patch of lifecycle.{name} would not reach it"
    )


def test_the_account_methods_import_what_stayed_from_lifecycle_when_they_run():
    """Each method that reads a name left in `lifecycle` imports it there in
    its body, at call time, so a patch of `lifecycle.MAX_ACCOUNT_SWAPS` (as
    `test_account_hot_swap` makes) is what `_swap_account` reads."""
    for method, names in {
        "_pool_credential_env": "MAX_ACCOUNT_TRIES",
        "_lease_account": "POOL_UNANSWERED",
        "_watch_account": "SWAP_UNUSABLE",
        "_move_account_after": "SWAP_DRAIN, SWAP_EXHAUSTED, SWAP_UNUSABLE",
        "_dead_credential": "MAX_CREDENTIAL_RELOADS",
        "_swap_account": "MAX_ACCOUNT_SWAPS, MAX_ACCOUNT_TRIES, SWAP_UNUSABLE",
        "_cannot_swap": "SWAP_EXHAUSTED, SWAP_UNUSABLE",
        "_emit_swapped": "_rfc3339_now",
    }.items():
        source = inspect.getsource(getattr(lifecycle.Worker, method))
        assert f"from .lifecycle import {names}\n" in source, method


def test_importing_accountpool_does_not_import_lifecycle():
    """No module-level import of `lifecycle` from `accountpool`: that would be
    circular, as `lifecycle` imports `accountpool` for `Worker`'s bases."""
    import subprocess
    import sys

    code = (
        "import sys, agent_worker.accountpool\n"
        "assert 'agent_worker.lifecycle' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# -- parking: `parking.ParkMixin` ------------------------------------------------


def _parking():
    """`agent_worker.parking`, imported when a test runs so each test below
    says on its own what is missing."""
    return importlib.import_module("agent_worker.parking")


#: Methods `Worker` inherits from `ParkMixin`; `Worker` defines none of them.
PARK_METHODS = [
    "_park_for_owner_questions",
    "_park_ci_pending",
    "_await_children",
    "_park_awaiting",
    "_await_owner_or_release",
    "_question_bridge_on",
    "_question_bridge_env",
    "_resume_parked_question",
    "_checkout_revision",
    "_park_for_bridged_question",
    "_bridge_checkpoint_gaps",
    "_fail_bridged_question",
    "_park_for_answers",
    "_park_for_release",
    "_incomplete_parents",
    "_park_dependency_incomplete",
    "_park_no_account",
    "_park_credential_missing",
    "_park_for_human",
    "_time_limit_continuations",
    "_time_limit_refusal",
    "_continuation_note_path",
    "_write_continuation_note",
    "_read_continuation_note",
    "_continuation_env",
    "_continue_at_time_limit",
    "_park_for_quota",
    "_quota_from_child",
    "_park_mcp_credential",
]

#: Module names only the park methods read: they live in `parking` and are
#: not bound in `lifecycle`, so a stale patch of one there raises.
PARK_DROPPED = [
    "QUESTION_ANSWER_NAME",
    "NO_CONTINUATION_CANCEL",
    "_parse_iso",
    # Imports only the park methods called: patch them in `parking`.
    "ACCOUNTS_FULL",
    "ACCOUNT_UNREADABLE",
    "BROKER_REFUSED",
    "NO_RECENT_READING",
    "POOL_PAUSED",
    "RELEASE_WAIT_MAX_RESUMES",
    "TIME_LIMIT_DETAIL_KEY",
    "ControlPlaneError",
    "QuotaDecision",
    "read_runner_signal",
    "question_bridge",
    "EXIT_QUOTA_EXHAUSTED",
    "QUESTION_ANSWER_FILE_ENV",
    "RESUME_ANSWERED",
    "RESUME_REASON_ENV",
    "RESUME_TIME_LIMIT",
    "subprocess",
    "timedelta",
]

#: Names the park methods read from `lifecycle` when they run, because
#: `lifecycle` uses them outside this concern or a test reads or patches them
#: there: still defined in `lifecycle`, not bound in `parking` (`Outcome` only
#: under `TYPE_CHECKING`, for the annotations).
PARK_READ_FROM_LIFECYCLE = [
    "_FULL_SHA_RE",
    "CONTINUATION_NOTE_NAME",
    "NO_CONTINUATION_CHECKPOINT",
    "NO_CONTINUATION_CAP",
    "NO_CONTINUATION_BUDGET",
    "NO_ACCOUNT_RETRY_SECONDS",
    "STALE_READING_RETRY_SECONDS",
    "POOL_UNANSWERED",
    "agent_stream_files",
    "Outcome",
]


@pytest.mark.parametrize("name", PARK_METHODS)
def test_each_park_method_is_inherited_from_park_mixin(name):
    parking = _parking()
    assert name in vars(parking.ParkMixin), f"ParkMixin does not define {name}"
    assert name not in vars(lifecycle.Worker), f"Worker still defines {name} itself"
    assert getattr(lifecycle.Worker, name) is vars(parking.ParkMixin)[name]


@pytest.mark.parametrize("name", PARK_DROPPED)
def test_a_park_name_lives_in_parking_alone(name):
    assert hasattr(_parking(), name), f"parking does not bind {name}"
    assert not hasattr(lifecycle, name), f"lifecycle.{name} is bound; read or patch parking.{name}"


@pytest.mark.parametrize("name", PARK_READ_FROM_LIFECYCLE)
def test_a_name_the_park_methods_read_from_lifecycle_stays_there(name):
    assert hasattr(lifecycle, name), f"lifecycle no longer binds {name}"
    assert not hasattr(_parking(), name), (
        f"parking binds {name}: a patch of lifecycle.{name} would not reach it"
    )


def test_the_park_methods_import_what_stayed_from_lifecycle_when_they_run():
    """Each method that reads a name left in `lifecycle` imports it there in
    its body, at call time, so a patch of `lifecycle.agent_stream_files` (as
    `test_bridged_question_park` makes) is what `_park_for_bridged_question`
    reads."""
    multi_line = {
        "_park_no_account": (
            "NO_ACCOUNT_RETRY_SECONDS", "POOL_UNANSWERED", "STALE_READING_RETRY_SECONDS", "Outcome",
        ),
        "_time_limit_refusal": (
            "NO_CONTINUATION_BUDGET", "NO_CONTINUATION_CAP", "NO_CONTINUATION_CHECKPOINT",
        ),
    }
    one_line = {
        "_checkout_revision": "_FULL_SHA_RE",
        "_park_for_bridged_question": "agent_stream_files",
        "_continuation_note_path": "CONTINUATION_NOTE_NAME",
    }
    for method in (
        "_park_for_owner_questions", "_park_ci_pending", "_park_awaiting",
        "_fail_bridged_question", "_park_for_answers", "_park_for_release",
        "_park_dependency_incomplete", "_park_credential_missing", "_park_for_human",
        "_continue_at_time_limit", "_park_for_quota", "_park_mcp_credential",
    ):
        one_line[method] = "Outcome"
    for method, names in one_line.items():
        source = inspect.getsource(getattr(lifecycle.Worker, method))
        assert f"from .lifecycle import {names}\n" in source, method
    for method, listed in multi_line.items():
        source = inspect.getsource(getattr(lifecycle.Worker, method))
        block = "".join(f"            {name},\n" for name in listed)
        assert f"from .lifecycle import (\n{block}        )\n" in source, method


def test_a_patch_of_lifecycle_agent_stream_files_reaches_the_bridged_park(monkeypatch):
    """`_park_for_bridged_question` reads `agent_stream_files` from `lifecycle`
    when it runs: `parking` does not bind it, so the patch is the one it reads."""
    sentinel = object()
    monkeypatch.setattr(lifecycle, "agent_stream_files", sentinel)
    from agent_worker.lifecycle import agent_stream_files

    assert agent_stream_files is sentinel
    assert "agent_stream_files" not in vars(_parking())


def test_importing_parking_does_not_import_lifecycle():
    """No module-level import of `lifecycle` from `parking`: that would be
    circular, as `lifecycle` imports `parking` for `Worker`'s bases."""
    import subprocess
    import sys

    code = (
        "import sys, agent_worker.parking\n"
        "assert 'agent_worker.lifecycle' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# -- the publish's retryable ends: `publish_retry.PublishRetryMixin` (issue 367) --

PUBLISH_RETRY_METHODS = [
    "_push_unreachable_marker",
    "_fetch_incomplete_marker",
    "_fail_for_publish_unreachable",
    "_fail_for_fetch_incomplete",
    "_origin_object_prober",
]


@pytest.mark.parametrize("name", PUBLISH_RETRY_METHODS)
def test_each_publish_retry_method_is_inherited_from_publish_retry_mixin(name):
    publish_retry = importlib.import_module("agent_worker.publish_retry")
    assert name in vars(publish_retry.PublishRetryMixin)
    assert name not in vars(lifecycle.Worker)
    assert getattr(lifecycle.Worker, name) is getattr(publish_retry.PublishRetryMixin, name)


def test_importing_publish_retry_does_not_import_lifecycle():
    """No module-level import of `lifecycle` from `publish_retry`: circular."""
    import subprocess
    import sys

    code = (
        "import sys, agent_worker.publish_retry\n"
        "assert 'agent_worker.lifecycle' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# -- the git harvest and publish: `gitpublish.GitPublishMixin` (issue 68) -------


def _gitpublish():
    """`agent_worker.gitpublish`, imported when a test runs so each test below
    says on its own what is missing."""
    return importlib.import_module("agent_worker.gitpublish")


#: Methods `Worker` inherits from `GitPublishMixin`; `Worker` defines none of them.
GITPUBLISH_METHODS = [
    "_harvest_git",
    "_publish_checked",
    "_integration_is_pending",
    "_shut_gate_owes_publish",
    "_harvest_tree_patch",
    "_keep_work_bundle",
    "_build_clean_repo",
    "_origin_line_fetcher",
    "_origin_confirmer",
    "_publish_onto_own_moved_head",
    "_own_before_forge_write",
    "_push_over_own_earlier_attempt",
    "_push_onto_base_merge",
    "_publish_git",
    "_publish_onto_pull_text",
    "_post_pull_body_comment",
]

#: `gitops` names only the publish methods read: imported in `gitpublish` and
#: not bound in `lifecycle`, so a stale patch of one there raises.
GITPUBLISH_DROPPED = [
    "MergedHeadUnreadable",
    "PublishFetchIncomplete",
    "PushUnreachable",
    "base_merge_tip",
    "moved_branch_tip",
    "keep_work_bundle",
    "read_agent_excludes",
    "replay_onto_tip",
    "summarize_work",
    "verify_merged_default_branch",
]

#: Names the publish methods read from `lifecycle` when they run, because a
#: test patches them there (or `lifecycle` defines them): still bound in
#: `lifecycle`, not in `gitpublish`.
GITPUBLISH_READ_FROM_LIFECYCLE = [
    "open_pull_request",
    "commit_dirty",
    "fetch_shallow_lines_from_origin",
    "fold_agent_commits",
    "merge_branches",
    "mirror_worktree",
    "own_branch_tip",
    "prepare_publish_repo",
    "push_branch",
    "verify_worker_authorship",
    "replay_agent_commits",
    "final_tree_leak",
    "_commits_leak",
    "_scan_diff_stream",
    "_config_seal",
    "pr_body_with_console_links",
    "PATCH_NAME",
    "REPO_DIR_NAME",
    "PUBLISH_FAILED_FIELD",
]


@pytest.mark.parametrize("name", GITPUBLISH_METHODS)
def test_each_publish_method_is_inherited_from_git_publish_mixin(name):
    gitpublish = _gitpublish()
    assert name in vars(gitpublish.GitPublishMixin), f"GitPublishMixin does not define {name}"
    assert name not in vars(lifecycle.Worker), f"Worker still defines {name} itself"
    assert getattr(lifecycle.Worker, name) is vars(gitpublish.GitPublishMixin)[name]


@pytest.mark.parametrize("name", GITPUBLISH_DROPPED)
def test_a_publish_name_lives_in_gitpublish_alone(name):
    assert hasattr(_gitpublish(), name), f"gitpublish does not bind {name}"
    assert not hasattr(lifecycle, name), (
        f"lifecycle.{name} is bound; read or patch gitpublish.{name}"
    )


@pytest.mark.parametrize("name", GITPUBLISH_READ_FROM_LIFECYCLE)
def test_a_name_the_publish_methods_read_from_lifecycle_stays_there(name):
    assert hasattr(lifecycle, name), f"lifecycle no longer binds {name}"
    assert not hasattr(_gitpublish(), name), (
        f"gitpublish binds {name}: a patch of lifecycle.{name} would not reach it"
    )


def test_the_publish_methods_import_what_stayed_from_lifecycle_when_they_run():
    """A method that reads a name a test patches in `lifecycle` imports it
    there in its body, at call time, so a `monkeypatch.setattr(lifecycle,
    "push_branch", ...)` (as `test_carrier_branches` makes) is what
    `_publish_git` calls."""
    for method, names in {
        "_harvest_tree_patch": ("mirror_worktree", "prepare_publish_repo"),
        "_origin_line_fetcher": ("fetch_shallow_lines_from_origin",),
        "_push_over_own_earlier_attempt": ("own_branch_tip", "push_branch"),
        "_publish_git": (
            "commit_dirty", "final_tree_leak", "fold_agent_commits", "merge_branches",
            "open_pull_request", "push_branch", "replay_agent_commits", "verify_worker_authorship",
        ),
    }.items():
        source = inspect.getsource(getattr(lifecycle.Worker, method))
        head, _, rest = source.partition("from .lifecycle import ")
        assert rest, method
        imported = rest.split(")\n", 1)[0] if rest.startswith("(") else rest.split("\n", 1)[0]
        for name in names:
            assert name in imported.replace("(", " ").replace(",", " ").split(), (method, name)


def test_a_patch_of_lifecycle_push_branch_is_what_the_publish_reads(monkeypatch):
    """`gitpublish` does not bind `push_branch`: the publish reads the one in
    `lifecycle`, so the patch is the one it calls."""
    sentinel = object()
    monkeypatch.setattr(lifecycle, "push_branch", sentinel)
    from agent_worker.lifecycle import push_branch

    assert push_branch is sentinel
    assert "push_branch" not in vars(_gitpublish())


def test_importing_gitpublish_does_not_import_lifecycle():
    """No module-level import of `lifecycle` from `gitpublish`: that would be
    circular, as `lifecycle` imports `gitpublish` for `Worker`'s bases."""
    import subprocess
    import sys

    code = (
        "import sys, agent_worker.gitpublish\n"
        "assert 'agent_worker.lifecycle' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
