"""An input's size is measured as Firestore stores it, not as JSON text.

THE DEFECT (#232 review, filed on epic #227; wave 2026-09-27). `max_input_bytes`
is there to keep a task document under Firestore's 1 MiB, and the worker's
manifest budget (`agent_worker.artifact_manifest`, "THE CAVEAT") assumes the
input costs at most that many Firestore bytes. `validate_input_size` measured
`json.dumps` bytes instead. Firestore stores every integer in 8 bytes whatever
its digit count, and `0, ` costs 3 bytes of JSON, so a list of small integers
cost about 2.7 times its measured size once stored: a 256 KiB-measured input
could take about 680 KiB of a 1 MiB document.

THE RULE, from Firestore's storage-size documentation: a string is its UTF-8
bytes + 1, an integer or a float 8, a boolean or null 1, an array the sum of
its values, a map the sum of each key (as a string) and its value. The
documentation's own worked example is asserted below, so the rule is held to
the source rather than to a restatement of it.

MUTATIONS: measure `json.dumps` again; count an integer as its digits; drop
the +1 on a string or on a map key; stop descending into lists or maps;
check the metadata with `json.dumps` while the input uses the new measure.
"""

from __future__ import annotations

import json

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.validation import firestore_size, validate_input_size

from .conftest import auth_header, seed_tenant


# --------------------------------------------------------------------------
# 1. The size rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "size"),
    [
        ("", 1),
        ("abc", 4),
        ("é", 3),            # two UTF-8 bytes, + 1
        (0, 8),
        (-(2**63), 8),
        (1.5, 8),
        (True, 1),
        (False, 1),
        (None, 1),
        ([], 0),
        ({}, 0),
        ([1, "a"], 8 + 2),
        ({"ab": 1}, 3 + 8),
        ({"a": {"b": [None, True]}}, 2 + 2 + 1 + 1),
    ],
)
def test_each_value_costs_what_firestore_counts(value, size):
    assert firestore_size(value) == size


def test_the_firestore_documentation_example():
    """The fields of the documented `tasks` example: "type": "Personal" is
    5 + 9, "done": false 5 + 1, "priority": 1 9 + 8, "description":
    "Learn Cloud Firestore" 12 + 22 -- 71 bytes in all."""
    doc = {"type": "Personal", "done": False, "priority": 1, "description": "Learn Cloud Firestore"}
    assert firestore_size(doc) == 71


def test_a_payload_nested_a_thousand_deep_is_measured_without_recursion():
    """Small enough to pass the limit and deep enough to overflow Python's
    stack, the same shape `validate_storable` walks with a stack for."""
    value: object = "x"
    for _ in range(5000):
        value = [value]
    assert firestore_size(value) == 2


# --------------------------------------------------------------------------
# 2. The limit measures the Firestore size
# --------------------------------------------------------------------------

#: Small integers: 3 JSON bytes each (`0, `), 8 Firestore bytes each.
INTEGERS = {"n": [0] * 1000}


def test_an_integer_heavy_input_is_refused_on_its_firestore_size():
    json_bytes = len(json.dumps(INTEGERS).encode("utf-8"))
    stored = firestore_size(INTEGERS)
    # The control: the two measures really do disagree, in the direction that
    # let the input through, so the limit below separates them.
    assert json_bytes < 4000 < stored, (json_bytes, stored)

    with pytest.raises(ValidationFailed) as refused:
        validate_input_size(INTEGERS, 4000)
    assert refused.value.detail == {"bytes": stored, "max_bytes": 4000}
    assert str(stored) in refused.value.message


def test_a_text_input_under_the_limit_is_accepted_and_its_size_returned():
    """The control on the other side: an ordinary prompt still fits, and the
    size reported is the Firestore one."""
    payload = {"prompt": "fix the flaky test in tests/unit"}
    assert validate_input_size(payload, 4000) == firestore_size(payload)


def test_the_limit_is_inclusive():
    payload = {"n": [0] * 10}                # 2 + 80
    assert firestore_size(payload) == 82
    assert validate_input_size(payload, 82) == 82
    with pytest.raises(ValidationFailed):
        validate_input_size(payload, 81)


def test_a_value_that_is_not_json_is_still_refused_as_such():
    loop: list[object] = []
    loop.append(loop)
    with pytest.raises(ValidationFailed, match="not JSON-serialisable"):
        validate_input_size({"x": loop}, 4000)


# --------------------------------------------------------------------------
# 3. Through the API: the caller's metadata, 16 KiB
# --------------------------------------------------------------------------

def test_integer_heavy_metadata_is_refused_by_the_api_and_nothing_is_written(client, db):
    """`metadata` is held to 16 KiB by the same function. 3,000 small integers
    are about 9 KB of JSON and 24 KB once stored."""
    seed_tenant(db, "eng")
    metadata = {"n": [0] * 3000}
    assert len(json.dumps(metadata).encode("utf-8")) < 16 * 1024 < firestore_size(metadata)
    before = set(db.paths("tasks/"))

    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "go"}, "metadata": metadata},
    )

    assert response.status_code == 422, response.text
    assert "metadata" in response.json()["message"], response.text
    assert set(db.paths("tasks/")) == before, "a refused submission wrote a task"


def test_the_same_metadata_at_a_quarter_of_the_count_is_accepted(client, db):
    """The control: the route accepts integer metadata that fits once stored."""
    seed_tenant(db, "eng")
    metadata = {"n": [0] * 750}
    assert firestore_size(metadata) < 16 * 1024
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "go"}, "metadata": metadata},
    )
    assert response.status_code == 201, response.text
