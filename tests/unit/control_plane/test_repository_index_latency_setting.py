"""The opt-in graph latency measurement reaches the index run (part of #1840).

The indexer measures the answer battery over the snapshot it just wrote only
when it is told `--measure-latency-hours N` (repo_graph_shards.py). What is
held here, on swarm-api's side:

  * `index.measure_latency_hours` is a registration setting, 1-168 whole
    hours; ABSENT MEANS OFF: a new registration stores no such key, and
    `"off"` removes it again;
  * the poll's index run names it in the SIGNED prompt, on its own line
    (`repoindex.MEASURE_LATENCY_LINE`), which is what the worker reads; a
    registration without it gets no line, and a stored value the settings
    would refuse is never passed on.

No credentials, no network: GitHub is a fake transport under the real client.
"""

from __future__ import annotations

import pytest

from swarm_api import repoindex
from swarm_api.repositories import measure_latency_hours_of

from .conftest import auth_header
from .test_repository_index_cadence import (  # noqa: F401 - fixtures
    ONE,
    _index,
    _poll,
    _register,
    _tasks,
    client,
    clock,
    github,
)


def _record(**index: object) -> dict:
    return {"repo_id": "repo_0123456789abcdef", "tenant_id": "eng", "owner": "eng",
            "repo": "app", "default_branch": "main",
            "repository_url": "https://github.com/eng/app", "index": dict(index)}


def _lines(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines()
            if line.startswith(repoindex.MEASURE_LATENCY_LINE)]


def _patch(client, repo_id, value):
    return client.patch(f"/v1/repositories/{repo_id}",
                        json={"index": {"measure_latency_hours": value}},
                        headers=auth_header("alice"))


# --------------------------------------------------------------------------
# the setting
# --------------------------------------------------------------------------

def test_a_new_registration_stores_no_measurement_setting(client, db):
    repo_id = _register(client)
    # MUTATION: a default in `_index_defaults` turns the measurement on for
    # every registration, or writes a key "absent means off" says is absent.
    assert "measure_latency_hours" not in _index(db, repo_id)
    assert measure_latency_hours_of(_index(db, repo_id)) is None


def test_the_setting_is_stored_served_and_removed_by_off(client, db):
    repo_id = _register(client)
    patched = _patch(client, repo_id, 24)
    assert patched.status_code == 200, patched.text
    assert patched.json()["repository"]["index"]["measure_latency_hours"] == 24
    assert _index(db, repo_id)["measure_latency_hours"] == 24

    off = _patch(client, repo_id, "off")
    assert off.status_code == 200, off.text
    # MUTATION: storing "off" instead of removing the key leaves a value
    # behind that only the reader's range check keeps from being passed on.
    assert "measure_latency_hours" not in _index(db, repo_id)
    assert "measure_latency_hours" not in off.json()["repository"]["index"]


def test_the_setting_can_be_named_at_registration(client, db):
    repo_id = _register(client, measure_latency_hours=6)
    assert _index(db, repo_id)["measure_latency_hours"] == 6


@pytest.mark.parametrize("value", [0, 169, 1.5, "24", True, "on"])
def test_a_value_outside_whole_hours_1_to_168_is_refused(client, db, value):
    repo_id = _register(client)
    assert _patch(client, repo_id, value).status_code == 422
    assert "measure_latency_hours" not in _index(db, repo_id)


@pytest.mark.parametrize(("stored", "read"), [
    (24, 24), (1, 1), (168, 168), (None, None), (0, None), (169, None),
    (True, None), ("24", None), (2.5, None), ("off", None),
])
def test_the_stored_value_is_read_only_when_the_settings_would_accept_it(stored, read):
    index = {} if stored is None else {"measure_latency_hours": stored}
    assert measure_latency_hours_of(index) == read


# --------------------------------------------------------------------------
# the index run's task input
# --------------------------------------------------------------------------

def test_indexer_task_names_the_setting_in_the_signed_prompt():
    task = repoindex.indexer_task(_record(measure_latency_hours=24), ONE)
    # MUTATION: drop the line from indexer_prompt() and the worker never
    # passes --measure-latency-hours: nothing is measured.
    assert _lines(task.input["prompt"]) == [f"{repoindex.MEASURE_LATENCY_LINE}24"]


@pytest.mark.parametrize("stored", [None, 0, True, "24", 500])
def test_an_absent_or_invalid_setting_puts_no_line_on_the_task(stored):
    index = {} if stored is None else {"measure_latency_hours": stored}
    task = repoindex.indexer_task(_record(**index), ONE)
    assert repoindex.MEASURE_LATENCY_LINE not in task.input["prompt"]


def test_the_poll_puts_the_setting_on_the_index_runs_task_input(client, db):
    _register(client, measure_latency_hours=24)
    assert _poll(client)["submitted"] == 1
    [task] = _tasks(db)
    # The prompt is inside the signed `input`; metadata is not signed.
    assert _lines(task["input"]["prompt"]) == [f"{repoindex.MEASURE_LATENCY_LINE}24"]


def test_the_poll_of_a_registration_without_it_stays_unmeasured(client, db):
    _register(client)
    assert _poll(client)["submitted"] == 1
    [task] = _tasks(db)
    assert repoindex.MEASURE_LATENCY_LINE not in task["input"]["prompt"]
