"""The rule that keeps the signed field list honest (contract request 34).

"A metadata key the worker starts reading is added to SIGNED_METADATA_KEYS in
the same change, with a format bump." A worker that acts on an unsigned key
acts on something any agent of the tenant can write, so this test finds every
metadata key the worker reads -- by an AST scan of `agent_worker`, not by a
list restated here -- and holds each one inside the signature. It also holds
the signed keys to swarm-api's reserved ones: every key the platform writes
into `metadata` is signed, except the reconciler's `startup_refunds`, which
changes after submission by design.

It also holds two properties of WHAT the bytes can be (contract requests 58
and 59, accepted 2026-10-11): an integer field rewritten to a float has no
canonical form, so the rewrite cannot keep the signature; and a spec nested
past `specsign.MAX_DEPTH` is `SpecNotCanonical`, never `RecursionError`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from swarm_common import specsign
from swarm_common.specsign import SIGNED_METADATA_KEYS, SpecNotCanonical, canonical_step_spec, jcs

import agent_worker
from agent_worker import expected_outputs, inputs
from swarm_common.models import Task

WORKER = Path(agent_worker.__file__).resolve().parent


def _module_constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def _key_of(node: ast.expr, constants: dict[str, str], imported: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return imported.get(f"{node.value.id}.{node.attr}")
    return None


def _reads_metadata(receiver: ast.expr) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id == "metadata"
    if isinstance(receiver, ast.Attribute):
        return receiver.attr == "metadata"
    # task.get("metadata").get(...) / (task.get("metadata") or {}).get(...)
    if isinstance(receiver, ast.BoolOp):
        return any(_reads_metadata(v) for v in receiver.values)
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute):
        return (
            receiver.func.attr == "get"
            and bool(receiver.args)
            and isinstance(receiver.args[0], ast.Constant)
            and receiver.args[0].value == "metadata"
        )
    return False


def worker_metadata_keys() -> set[str]:
    imported = {
        "inputs_mod.METADATA_KEY": inputs.METADATA_KEY,
        "inputs.METADATA_KEY": inputs.METADATA_KEY,
        "expected_mod.METADATA_KEY": expected_outputs.METADATA_KEY,
        "expected_outputs.METADATA_KEY": expected_outputs.METADATA_KEY,
    }
    found: set[str] = set()
    for path in sorted(WORKER.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        constants = _module_constants(tree)
        for node in ast.walk(tree):
            receiver = key = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "get" and node.args:
                    receiver, key = node.func.value, node.args[0]
            elif isinstance(node, ast.Subscript):
                receiver, key = node.value, node.slice
            if receiver is None or not _reads_metadata(receiver):
                continue
            name = _key_of(key, constants, imported)
            if name is not None and name != "metadata":
                found.add(name)
    return found


def test_the_scan_sees_the_keys_the_worker_is_known_to_read():
    """Proof the scan RAN over real code, not an empty walk."""
    keys = worker_metadata_keys()
    assert {inputs.METADATA_KEY, expected_outputs.METADATA_KEY, "dispatch"} <= keys, keys


def test_every_metadata_key_the_worker_reads_is_signed():
    # The two child counters/markers the worker reads are written after
    # submission by design, so they are named here and nowhere else. So is a
    # merge step's CI wait (lane MS2), written by its own park and the wake
    # tick: reserved from callers, and read only to count refunds and carry
    # `first_parked_at` from park to park. And the live-browser hand-off's
    # park-refund counter (lane LB-L4), read by the HUMAN_REQUIRED park exactly
    # as the await park reads `child_await_resumes`. And the person's return
    # that park reads (LB-RETURN), with the "I'm ready" mark it writes in its
    # place: both written while the attempt runs, never at submission.
    # And the owner's answers to a blocking question with the wait they
    # answer (lane AU-7), written by swarm-api while the task is parked, and
    # handed to the resumed agent only as data between nonce lines.
    unsigned = worker_metadata_keys() - set(SIGNED_METADATA_KEYS) - {
        "child_await_resumes",
        "child_cascade",
        "human_wait_resumes",
        "human_returned",
        "human_ready",
        "merge_wait",
        "owner_answers",
        "question_wait",
        # The release waits refunded so far (AU7-REFUND), read by the
        # release-wait park exactly as `child_await_resumes` is by the await's.
        "release_wait_resumes",
        # The quota parks refunded so far (#2350), read by the quota park the
        # same way.
        "quota_park_resumes",
    }
    assert not unsigned, (
        f"agent_worker reads metadata keys {sorted(unsigned)} that the step-spec signature "
        "does not cover. Add them to swarm_common.specsign.SIGNED_METADATA_KEYS with a "
        "SPEC_FORMAT bump (contract request 34, section 1)."
    )


#: The platform's metadata keys that change AFTER submission, or that no
#: worker acts on, and so cannot be inside a signature made at submission:
#: the reconciler's refund counter, the worker's await-refund counter and the
#: cascade marker (written on a running child), the child's request id,
#: which only swarm-api's dedupe reads (docs/design/child-tasks.md §6.4), and
#: a merge step's CI wait, written by its park and its wake tick (lane MS2),
#: and the reconciler's disk-eviction count, written on an evicted attempt (#893),
#: and a schedule firing's mark on its work, which only swarm-api reads
#: (docs/schedules.md §2.7, lane S2): no worker acts on it. And the
#: live-browser hand-off's three (docs/design/live-browser.md §4.3, lane
#: LB-L10): the park-refund counter, the "a person is ready" mark and the
#: per-attempt hand-off count, each written during or between attempts; and
#: a person's return during an attempt, which its park reads (LB-RETURN).
#: And the reconciler's `start_problem` (F1-POD), written while a pod fails to
#: start and read only by swarm-api. And the owner's answers to the agent's
#: questions (TASK-ANSWERS), written by swarm-api after the attempt asked them.
#: And whose GitHub credential a service submission runs with (INDEX-CRED): a
#: label only swarm-api's codec reads, beside the signed `forge_credential`.
#: And the pull requests a workflow waits for (`after`, SPEED-D3): rewritten
#: by swarm-api's wake tick while the step is parked, read by the scheduler
#: and swarm-api, acted on by no worker. And a QA run's mark on its browser
#: steps (VQ2-RESERVE, docs/design/visual-qa-any-app.md §1.1): written by
#: `:run`, read only by swarm-api's worker route, which serves a target only
#: to a task the run record names; no worker acts on the mark itself.
#: And the release waits refunded so far (AU7-REFUND), written by the
#: worker's release-wait park between attempts, as `child_await_resumes` is.
#: And the quota parks refunded so far (#2350), written by the quota park.
UNSIGNED_PLATFORM_KEYS = {
    "schedule",
    "human_wait_resumes",
    "human_ready",
    "human_handoffs",
    "human_returned",
    "startup_refunds",
    "disk_evictions",
    "child_await_resumes",
    "child_cascade",
    "child_request_id",
    "merge_wait",
    "start_problem",
    "owner_answers",
    "forge_credential_source",
    "after_prs",
    "qa_target",
    "release_wait_resumes",
    "quota_park_resumes",
}


def test_the_signed_keys_are_swarm_apis_reserved_keys_but_the_counters():
    from swarm_api.validation import (
        AFTER_PRS_METADATA_KEY,
        CHILD_AWAIT_RESUMES_METADATA_KEY,
        CHILD_CASCADE_METADATA_KEY,
        CHILD_REQUEST_ID_METADATA_KEY,
        DISK_EVICTIONS_METADATA_KEY,
        MERGE_WAIT_METADATA_KEY,
        OWNER_ANSWERS_METADATA_KEY,
        RESERVED_METADATA_KEYS,
        SCHEDULE_METADATA_KEY,
        FORGE_SOURCE_METADATA_KEY,
        STARTUP_REFUNDS_METADATA_KEY,
        HUMAN_HANDOFFS_METADATA_KEY,
        HUMAN_READY_METADATA_KEY,
        HUMAN_RETURNED_METADATA_KEY,
        HUMAN_WAIT_RESUMES_METADATA_KEY,
        START_PROBLEM_METADATA_KEY,
        OWNER_ANSWERS_METADATA_KEY,
        QA_TARGET_METADATA_KEY,
        RELEASE_WAIT_RESUMES_METADATA_KEY,
        QUOTA_PARK_RESUMES_METADATA_KEY,
    )

    # MUTATION: drop QA_TARGET_METADATA_KEY from RESERVED_METADATA_KEYS and this is red.
    assert QA_TARGET_METADATA_KEY in RESERVED_METADATA_KEYS
    assert set(SIGNED_METADATA_KEYS) == set(RESERVED_METADATA_KEYS) - UNSIGNED_PLATFORM_KEYS
    assert {
        HUMAN_HANDOFFS_METADATA_KEY,
        HUMAN_READY_METADATA_KEY,
        HUMAN_RETURNED_METADATA_KEY,
        HUMAN_WAIT_RESUMES_METADATA_KEY,
        STARTUP_REFUNDS_METADATA_KEY,
        CHILD_AWAIT_RESUMES_METADATA_KEY,
        CHILD_CASCADE_METADATA_KEY,
        CHILD_REQUEST_ID_METADATA_KEY,
        MERGE_WAIT_METADATA_KEY,
        DISK_EVICTIONS_METADATA_KEY,
        SCHEDULE_METADATA_KEY,
        START_PROBLEM_METADATA_KEY,
        OWNER_ANSWERS_METADATA_KEY,
        FORGE_SOURCE_METADATA_KEY,
        AFTER_PRS_METADATA_KEY,
        QA_TARGET_METADATA_KEY,
        RELEASE_WAIT_RESUMES_METADATA_KEY,
        QUOTA_PARK_RESUMES_METADATA_KEY,
    } == UNSIGNED_PLATFORM_KEYS


# ---------------------------------------------------------------------------
# Contract request 58: an integer field is an integer, at every format
# ---------------------------------------------------------------------------

_DOC: dict[str, Any] = {
    "tenant_id": "eng",
    "runner_profile": "claude-code",
    "resource_class": "standard",
    "timeout_seconds": 3600,
    "max_attempts": 3,
    "input": {"prompt": "review the change", "issue": 7},
    "metadata": {},
}

#: One document per format, each the oldest format that covers what it sets.
_DOCS_BY_FORMAT: dict[int, dict[str, Any]] = {
    1: _DOC,
    2: {**_DOC, "parent_task_id": "task_parent", "parent_attempt_id": "att_parent"},
    3: {**_DOC, "forge_credential": "git-u-" + "0" * 16, "forge_access": "read"},
    4: {
        **_DOC,
        "additional_repositories": [
            {
                "name": "web",
                "repo_id": "repo_" + "0" * 16,
                "repository": "acme/web",
                "repository_url": "https://github.com/acme/web",
                "ref": "main",
                "commit": "0" * 40,
                "forge_credential": "git",
                "forge_credential_source": "tenant",
                "size_kib": 0,
            }
        ],
    },
    5: {**_DOC, "model_class": "sonnet"},
    # Contract request 120 (#2459): the resolved budget.
    6: {**_DOC, "budget": {"usd": 1.0}},
}


def test_every_format_has_a_document_here():
    """A format added without a document here escapes every test below."""
    assert sorted(_DOCS_BY_FORMAT) == list(specsign.SPEC_FORMATS)


def test_each_document_is_signed_at_its_format():
    """The control: each document below is one a signer signs at that format."""
    for spec_format, doc in _DOCS_BY_FORMAT.items():
        assert specsign.signing_format(doc) == spec_format


@pytest.mark.parametrize("spec_format", sorted(_DOCS_BY_FORMAT))
@pytest.mark.parametrize(
    "key,rewritten", [("timeout_seconds", 3600.0), ("max_attempts", 3.0)], ids=["timeout", "attempts"]
)
def test_an_integer_field_rewritten_to_a_float_breaks_the_signature(spec_format, key, rewritten):
    """JCS writes 3600 and 3600.0 as the same bytes; Firestore stores them as
    different types. A rewrite of one to the other must not keep the
    signature, so the float has no canonical form at all."""
    doc = _DOCS_BY_FORMAT[spec_format]
    canonical_step_spec(doc, task_id="task_1", spec_format=spec_format)  # the int verifies
    assert jcs(rewritten) == jcs(int(rewritten)), "the premise: JCS cannot tell them apart"
    with pytest.raises(SpecNotCanonical, match=f"^{key} is a float, not an integer$"):
        canonical_step_spec({**doc, key: rewritten}, task_id="task_1", spec_format=spec_format)


@pytest.mark.parametrize("spec_format", sorted(_DOCS_BY_FORMAT))
@pytest.mark.parametrize("key", ["timeout_seconds", "max_attempts"])
@pytest.mark.parametrize("value", [True, "3600", 3600.5], ids=["bool", "string", "fraction"])
def test_an_integer_field_that_is_no_integer_is_refused(spec_format, key, value):
    """A bool is an int to Python and is still refused; so is anything else."""
    doc = {**_DOCS_BY_FORMAT[spec_format], key: value}
    with pytest.raises(SpecNotCanonical):
        canonical_step_spec(doc, task_id="task_1", spec_format=spec_format)


@pytest.mark.parametrize("spec_format", sorted(_DOCS_BY_FORMAT))
def test_an_absent_integer_field_still_has_its_bytes(spec_format):
    """None (or absent) is what a document from before either field had."""
    doc = {k: v for k, v in _DOCS_BY_FORMAT[spec_format].items() if k != "max_attempts"}
    absent = canonical_step_spec(doc, task_id="task_1", spec_format=spec_format)
    assert absent == canonical_step_spec(
        {**doc, "max_attempts": None}, task_id="task_1", spec_format=spec_format
    )
    assert b'"max_attempts":null' in absent


# ---------------------------------------------------------------------------
# Contract request 59: depth is bounded, and the bound is a refusal
# ---------------------------------------------------------------------------


def _lists(levels: int) -> Any:
    value: Any = 0
    for _ in range(levels):
        value = [value]
    return value


def _maps(levels: int) -> Any:
    value: Any = 0
    for _ in range(levels):
        value = {"a": value}
    return value


@pytest.mark.parametrize("nest", [_lists, _maps], ids=["lists", "maps"])
def test_a_spec_2000_deep_is_not_canonical_not_a_recursion_error(nest):
    """Past Python's recursion limit: the one exception both callers catch."""
    with pytest.raises(SpecNotCanonical, match="nested deeper than 32 levels"):
        jcs(nest(2000))
    with pytest.raises(SpecNotCanonical):
        canonical_step_spec({**_DOC, "input": {"prompt": "x", "deep": nest(2000)}}, task_id="t")


def test_a_map_or_list_that_contains_itself_is_not_canonical():
    cyclic_map: dict[str, Any] = {}
    cyclic_map["self"] = cyclic_map
    cyclic_list: list[Any] = []
    cyclic_list.append(cyclic_list)
    for value in (cyclic_map, cyclic_list):
        with pytest.raises(SpecNotCanonical):
            jcs(value)


@pytest.mark.parametrize("nest", [_lists, _maps], ids=["lists", "maps"])
def test_32_levels_of_containers_encode_and_33_do_not(nest):
    assert specsign.MAX_DEPTH == 32
    assert jcs(nest(32)).count(b"0") == 1
    with pytest.raises(SpecNotCanonical):
        jcs(nest(33))


def test_a_stored_document_20_deep_still_has_its_bytes():
    """Firestore's own limit is 20 levels: the document is one, so its input
    holds 19 more. Nothing Firestore can store is refused by the bound."""
    doc = {**_DOC, "input": _maps(19)}
    canonical = canonical_step_spec(doc, task_id="task_1")
    assert canonical.count(b'{"a":') == 19


# ---------------------------------------------------------------------------
# Every Task field is signed or named as unsigned (contract request 96)
# ---------------------------------------------------------------------------

#: The `Task` fields outside the signature, each for a reason: the document's
#: own `id` (the signature binds the id the worker READ BY, see
#: `canonical_step_spec`), the signature's own three fields, and attempt or
#: lifecycle state that changes after submission and so cannot be inside a
#: signature made at it. A new field lands in one of the two sets in the same
#: change: signed with a format bump, or here with its reason.
UNSIGNED_TASK_FIELDS = {
    "id",
    "created_at",
    "updated_at",
    "state",
    "priority",
    "attempt_count",
    "next_eligible_at",
    "park_reason",
    "blocked_by",
    "current_lease_id",
    "current_generation",
    "cancel_requested",
    "started_at",
    "completed_at",
    "last_error",
    "result_summary",
    "latest_checkpoint",
    "end_cause",
    "spec_signature",
    "spec_key_version",
    "spec_format",
    "human_wait",
    # Contract request 123: the scheduler counts refused dispatches after
    # submission, so it cannot be inside a signature made at it.
    "platform_refusals",
    # Contract request 131: a record of the timeout a spec asked for when
    # swarm-api stored a lower one. It limits nothing and selects nothing
    # (the limit is the signed `timeout_seconds`), so a forged value can only
    # add a line to the runner's report.
    "timeout_seconds_asked",
}


def test_every_task_field_is_signed_or_named_unsigned():
    newest = canonical_step_spec({}, task_id="t", spec_format=max(specsign.SPEC_FORMATS))
    projected = set(json.loads(newest)) - {"purpose", "format", "task_id"}
    fields = set(Task.__dataclass_fields__)
    assert "additional_repositories" in projected
    assert projected <= fields, sorted(projected - fields)
    assert not projected & UNSIGNED_TASK_FIELDS, sorted(projected & UNSIGNED_TASK_FIELDS)
    assert fields - projected == UNSIGNED_TASK_FIELDS, (
        f"Task fields neither signed nor named unsigned: {sorted(fields - projected - UNSIGNED_TASK_FIELDS)}; "
        f"named unsigned but no Task field: {sorted(UNSIGNED_TASK_FIELDS - fields)}"
    )
