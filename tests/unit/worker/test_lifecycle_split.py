"""The publish guard moved out of `lifecycle.py` into `publish_guard.py` (part of issue 68).

A mechanical move: each name below lives in `publish_guard` now. The names
`lifecycle` itself still calls, or another module or a test still reads
through it, stay bound there as the SAME object, so a
`monkeypatch.setattr(lifecycle, ...)` of one the worker calls still takes
effect. The rest are not bound in `lifecycle` at all, so a stale patch of one
raises `AttributeError` rather than succeeding and changing nothing (issue
193). The line ceiling is a ratchet: `lifecycle.py` only gets shorter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker import lifecycle, publish_guard

LIFECYCLE = Path(lifecycle.__file__)

#: Set just above `lifecycle.py`'s length after the move. Lower it as the
#: split goes on; never raise it.
LIFECYCLE_LINE_CEILING = 19_150

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
    value = getattr(publish_guard, name)
    module = getattr(value, "__module__", None)
    if callable(value) and module is not None and not module.startswith("swarm_redaction"):
        return module == publish_guard.__name__
    return True


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
