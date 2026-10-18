"""A head the platform's PR-side CI fixer pushed for THIS pull request is a fix round's (#1073).

Measured on PR 1071 (workflow wf_ca1fa9e2701642db890e, merge task
task_f745c31a39014359bccd): scripts/ci-fix.sh's fix task
task_b87630a30d7b43428d8f pushed ed1b9056... to the pull request's branch
while the merge step was parked, and the step's next attempt refused
`head_moved`. The fixer is a `continues_task` workflow that swarm-api
stamps with `metadata.ci_fix` (`swarm_api.cifix.stamp`); its pushed head is
its own `result_summary`.

What the worker holds here:

  1. A head the CI fixer recorded for this pull request -- its task this
     tenant's, its spec signed, continuing the branch's root, in this
     repository, its `ci_fix` record naming this pull request and stamped
     by the API -- is accepted, by itself or under GitHub's own base merge.
  2. The same head without that record is still `head_moved`; so is one the
     fixer recorded for ANOTHER pull request, or one any single fact about
     the fixer's task fails. Never the commit's author or message: the fake
     forge here serves no author at all.
  3. The finder is asked for this pull request and this branch's root only.

No credentials, no network. The fake forge is `merge_world.MergeWorld`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_worker import inputs, merge
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import NUMBER, OPENER, REPO_URL, TENANT, MergeWorld

FIXER = "task_b87630a30d7b43428d8f"
FIXED = "ed1b9056" + "0" * 32


def _fixer(*, number: int = NUMBER, continues: str = OPENER, pushed: str | None = FIXED,
           tenant: str = TENANT, repository_url: str = REPO_URL,
           postback: str | None = "posted", workflow_id: str = "wf_cifix") -> dict[str, Any]:
    """The fix task scripts/ci-fix.sh submitted, as swarm-api stored it."""
    record: dict[str, Any] = {"pull_request": number, "attempt": 1, "max_attempts": 2,
                              "head_sha": "c" * 40}
    if postback is not None:
        record["postback"] = postback
    return {
        "tenant_id": tenant, "workflow_id": workflow_id, "state": "SUCCEEDED",
        "repository_url": repository_url,
        "metadata": {"dispatch": {"strategy": "direct-pr", "continues": continues},
                     "ci_fix": record},
        "result_summary": {"git": {"pushed_head": pushed} if pushed else {}},
    }


def _at(world: MergeWorld, head: str) -> None:
    world.pr["head"]["sha"] = head
    world.serve_checks(head)


def test_a_head_the_ci_fixer_recorded_for_this_pull_request_is_accepted(tmp_path):
    world = MergeWorld(tmp_path)
    world.ci_fixes = [(FIXER, _fixer())]
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == FIXED
    assert outcome.summary["head"] == FIXED
    assert outcome.summary["head_pushed_by"] == FIXER
    # Asked for this pull request of this branch, and nothing wider.
    assert world.ci_fix_queries == [(NUMBER, OPENER)]
    # Verified against the fixer's OWN signed workflow: it is not this one's.
    assert (FIXER, "wf_cifix") in world.verified


def test_the_ci_fixers_head_under_githubs_own_base_merge_is_accepted(tmp_path):
    world = MergeWorld(tmp_path)
    world.ci_fixes = [(FIXER, _fixer())]
    updated = world.github_merge(FIXED)
    world.pr["head"]["sha"] = updated

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (call,) = world.merge_calls()
    assert call.body["sha"] == updated
    assert outcome.summary["updates"] == 1


def _no_record(world: MergeWorld) -> None:
    world.ci_fixes = []


def _no_finder(world: MergeWorld) -> None:
    world.ci_fixes = None


def _another_pull_request(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(number=NUMBER + 1))]


def _another_root(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(continues="task_other"))]


def _another_repository(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(repository_url="https://github.com/octo-org/other.git"))]


def _another_tenant(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(tenant="research"))]


def _not_stamped_by_the_api(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(postback=None))]


def _unsigned(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer())]
    world.unverified[FIXER] = "signature_mismatch"


def _pushed_another_head(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(pushed="9" * 40))]


def _pushed_nothing(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer(pushed=None))]


def _finder_fails(world: MergeWorld) -> None:
    world.ci_fixes = [(FIXER, _fixer())]
    real = world.context

    def broken():
        ctx = real()

        def fail(_number: int, _root: str):
            raise RuntimeError("firestore unavailable")

        ctx.find_ci_fix_tasks = fail
        return ctx

    world.context = broken  # type: ignore[method-assign]


REFUSED = [
    ("no_ci_fixer_record", _no_record),
    ("no_finder", _no_finder),
    ("recorded_for_another_pull_request", _another_pull_request),
    ("continues_another_root", _another_root),
    ("in_another_repository", _another_repository),
    ("of_another_tenant", _another_tenant),
    ("ci_fix_record_not_stamped_by_the_api", _not_stamped_by_the_api),
    ("spec_unverified", _unsigned),
    ("pushed_another_head", _pushed_another_head),
    ("pushed_nothing", _pushed_nothing),
    ("finder_unreadable", _finder_fails),
]


@pytest.mark.parametrize(("case", "mutate"), REFUSED, ids=[c[0] for c in REFUSED])
def test_any_other_moved_head_is_still_head_moved(tmp_path, case, mutate):
    """The control is the first test of this file: the same head, accepted
    there, refused here for exactly one missing or broken record."""
    world = MergeWorld(tmp_path)
    mutate(world)
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED, (case, outcome.message)
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "head_moved", (case, outcome.summary)
    assert "CI fixer" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == [] and world.update_calls() == []


def test_a_named_pull_request_never_asks_for_the_ci_fixer(tmp_path):
    """A `merge_pr` target has no task branch for a fixer to continue."""
    world = MergeWorld(tmp_path)
    world.target = {"number": NUMBER, "head_sha": "c" * 40}
    world.ci_fixes = [(FIXER, _fixer())]
    _at(world, FIXED)

    outcome = merge.run_merge(world.context())

    assert outcome.summary["refusal"]["code"] == "head_moved"
    assert world.ci_fix_queries == []


def test_the_worker_reads_the_records_swarm_api_writes():
    from swarm_api import cifix

    assert merge.CI_FIX_METADATA_FIELD == cifix.CI_FIX_METADATA_FIELD
    assert merge.CI_FIX_POSTBACK_FIELD == cifix.POSTBACK_FIELD
    assert merge.CI_FIX_POSTBACK_STATES == {cifix.PENDING, cifix.POSTED, cifix.GAVE_UP}
    assert merge.CI_FIXER_TASKS_READ > cifix.MAX_ATTEMPTS_CEILING


# ---------------------------------------------------------------------------
# The query itself
# ---------------------------------------------------------------------------

class _Snap:
    def __init__(self, doc_id: str, doc: dict[str, Any]) -> None:
        self.id = doc_id
        self._doc = doc

    def to_dict(self) -> dict[str, Any]:
        return dict(self._doc)


class _Query:
    """Records the filters asked; answers with every document, so the
    function's own tenant check is what is tested."""

    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs
        self.filters: list[tuple[str, str, Any]] = []
        self.limited: int | None = None
        self.options: dict[str, Any] = {}

    def collection(self, name: str) -> "_Query":
        assert name == "tasks"
        return self

    def where(self, *, filter: Any) -> "_Query":
        self.filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def limit(self, count: int) -> "_Query":
        self.limited = count
        return self

    def stream(self, **options: Any):
        self.options = options
        return iter([_Snap(k, v) for k, v in self.docs.items()])


def test_the_query_asks_for_this_tenant_pull_request_and_branch_and_rechecks_the_tenant():
    db = _Query({FIXER: _fixer(), "task_theirs": _fixer(tenant="research")})

    found = inputs.find_ci_fix_tasks(db, tenant_id=TENANT, pull_request=NUMBER,
                                     root_task_id=OPENER, limit=10, call_options={"timeout": 5})

    assert db.filters == [("tenant_id", "==", TENANT),
                          ("metadata.ci_fix.pull_request", "==", NUMBER),
                          ("metadata.dispatch.continues", "==", OPENER)]
    assert db.limited == 10 and db.options == {"timeout": 5}
    assert [task_id for task_id, _doc in found] == [FIXER]
