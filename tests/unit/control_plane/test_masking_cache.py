"""A task is masked once per document, not once per read.

THE PR #229 REVIEW. The list route masks every row's input, and the UI reads it
with limit=200. `JsonMasker` over a 256 KiB input was measured at 57 ms with no
credential in it and 69 ms with 256 learned literals, so one page of large
prompts cost about 11 s of CPU on every refresh, under the GIL of an instance
every tenant shares.

`task_input.masking_for` keeps each task's masker, keyed on the task AND a
digest of its input and metadata: a second read of the same document is a hit,
and a document that changed is a miss -- never a stale masking. The cache is
bounded by size as well as by count.

MUTATIONS: key on the task id alone (the changed-document case serves the old
masking); drop the size bound (the eviction case goes red); hand out the cached
object rather than a copy (the mutation case goes red).
"""

from __future__ import annotations

from datetime import datetime, timezone

from swarm_common.models import Task
from swarm_common.states import TaskState

from swarm_api.redaction import MASK
from swarm_api import task_input

from .conftest import auth_header, seed_task, seed_tenant

MOMENT = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _task(task_id: str, prompt: str, metadata: dict | None = None) -> Task:
    return Task(
        id=task_id, tenant_id="eng", created_at=MOMENT, updated_at=MOMENT,
        state=TaskState.QUEUED, runner_profile="mock", resource_class="standard",
        input={"prompt": prompt}, submitted_by="alice@saga.xyz", metadata=dict(metadata or {}),
    )


def test_a_second_list_read_masks_nothing_again(client, db):
    seed_tenant(db, "eng")
    for n in range(5):
        doc = seed_task(db, task_id=f"task_{n}", tenant_id="eng", state="QUEUED")
        doc["input"] = {"prompt": f"PASSWORD=value-number-{n}-long"}
    task_input.MASKING_CACHE.clear()

    first = client.get("/v1/tasks?limit=50", headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    misses = task_input.MASKING_CACHE.misses
    assert misses == 5 and task_input.MASKING_CACHE.hits == 0

    second = client.get("/v1/tasks?limit=50", headers=auth_header("alice"))
    assert second.json()["tasks"] == first.json()["tasks"]
    assert task_input.MASKING_CACHE.misses == misses, "the second read of the same page masked again"
    assert task_input.MASKING_CACHE.hits == 5


def test_a_changed_document_is_a_miss_never_a_stale_masking(client, db):
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_x", tenant_id="eng", state="QUEUED")
    doc["input"] = {"prompt": "nothing secret"}
    before = client.get("/v1/tasks/task_x", headers=auth_header("alice")).json()["task"]
    assert before["input"] == {"prompt": "nothing secret"}

    doc["input"] = {"prompt": "PASSWORD=now-there-is-one"}
    after = client.get("/v1/tasks/task_x", headers=auth_header("alice")).json()["task"]
    assert after["input"] == {"prompt": f"PASSWORD={MASK}"}
    assert after["input_redaction_count"] == 1


def test_what_a_reader_is_handed_is_its_own_copy():
    cache = task_input.MaskingCache(max_bytes=1 << 20, max_entries=10)
    task = _task("task_c", "PASSWORD=copy-me-please")
    served, _ = cache.masking_for(task).input_value()
    served["prompt"] = "changed by a caller"

    again, count = cache.masking_for(task).input_value()
    assert again == {"prompt": f"PASSWORD={MASK}"} and count == 1
    assert cache.hits == 1


def test_the_cache_is_bounded_by_size_and_drops_the_least_recently_used():
    big = "x" * 4000
    # Each entry is charged 3 x its serialised length + 1 KiB, about 13 KB here.
    cache = task_input.MaskingCache(max_bytes=40_000, max_entries=100)
    for n in range(4):
        cache.masking_for(_task(f"task_{n}", big))
    assert len(cache) < 4, "four ~13 KB entries fit a 40 KB budget"
    cache.masking_for(_task("task_3", big))
    assert cache.hits == 1, "the most recent entry is kept"
    cache.masking_for(_task("task_0", big))
    assert cache.misses == 5, "the oldest entry was dropped first"


def test_the_cache_is_bounded_by_count_too():
    cache = task_input.MaskingCache(max_bytes=1 << 30, max_entries=3)
    for n in range(10):
        cache.masking_for(_task(f"task_{n}", "short"))
    assert len(cache) == 3
