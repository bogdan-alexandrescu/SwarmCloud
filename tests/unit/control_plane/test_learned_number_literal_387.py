"""A literal the task named is masked when it comes back as a JSON NUMBER, on every route (#387).

WHAT BROKE. A task whose input says `{"password": "<8 digits>"}` teaches its
masker that literal (`redaction._learned_literals`), and every string that
holds it is masked. But `JsonMasker._walk` returned every number untouched,
so the same digits written as a number -- `{"n": <digits>}` -- were served
in clear by `/v1/tasks/{id}/input` and `GET /v1/tasks/{id}` (both mask
through `JsonMasker`), and by `/logs` on any JSON line where the walk masked
something else: `redact_lines` skips its text safety pass once the walk has
masked anything (owner decision, 2026-09-30), and the safety pass was the
only thing that ever looked at a number. Where the walk masked nothing the
pass did catch the digits, as TEXT, and served `{"n":********}`, which is not
JSON. The artifact routes masked the number since PR #378, through their own
inline copy of the rule in `json_masking._walk`, so the routes disagreed on a
literal the task itself named.

WHAT IS PINNED.

  * `/input` and `GET /v1/tasks/{id}` serve the number as the JSON string
    `"********"`, and count it;
  * `/logs` masks it as itself, as `N.0` and inside a longer number, on a line
    where the walk masked nothing else and on one where it did, and the line
    is still one JSON document;
  * `redact_lines` and the artifact token path give the same text and count
    for every number form;
  * a number that holds no learned literal, a boolean, and a number shorter
    than a literal can be are served as stored;
  * both paths go through `JsonMasker.number`: with it stubbed to find
    nothing, both serve the digits, and `json_masking` names no
    `_mask_literals` of its own.

MUTATIONS that turn this red: return numbers untouched in `JsonMasker._walk`
(every route test and the parity test); compare `str(node)` without the
float form (the `N.0` case is still found, `Ne0` is the same float -- the
parity test pins both); look in `json.dumps(node)` instead of the stored
token (`Ne9`, `Ne-3` and a long float re-encode without the digits -- the
parity test pins each, alone and beside a masked string); test `int` before `bool` and mask `True` (the
control); put `_mask_literals` back inline in `json_masking._walk` (the
one-rule test).

The digits are built at runtime so no added line looks like a credential.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import json_masking, redaction
from swarm_api.redaction import MASK, JsonMasker

from .conftest import auth_header, seed_task, seed_tenant

DIGITS = "4826" + "1937"
NUMBER = int(DIGITS)
#: A secret the task names, so the masker learns DIGITS from it.
NAMED = {"password": DIGITS}
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

#: Every JSON spelling of a number that holds DIGITS: itself, a float that
#: decodes to it, the same float in exponent form, and a longer number.
FORMS = [DIGITS, DIGITS + ".0", DIGITS + "e0", "9" + DIGITS + "1"]


def _task(db, input_doc: dict[str, Any]) -> dict[str, Any]:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_a", tenant_id="eng", runner_profile="claude-code", state="SUCCEEDED")
    doc["input"] = input_doc
    return doc


def _seed_log(db, objects, body: str, *, input_doc: dict[str, Any]) -> None:
    _task(db, input_doc)
    created = NOW - timedelta(minutes=5)
    db.collection("attempts").document("att_1").set({
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_att_1", "backend": "CLOUD_RUN_JOB", "execution_name": "x",
        "created_at": created, "started_at": created,
        "completed_at": created + timedelta(minutes=1), "exit_code": 0, "error": None,
        "peak_rss_bytes": 1, "oom_near_miss": False, "checkpoints": [],
    })
    objects.put("tenants/eng/tasks/task_a/attempts/att_1/logs/stdout.log", body)


def _stdout(client) -> dict[str, Any]:
    response = client.get("/v1/tasks/task_a/logs?stream=stdout", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return next(s for s in response.json()["streams"] if s["stream"] == "stdout")


def test_input_route_masks_a_learned_literal_written_as_a_number(client, db):
    _task(db, {**NAMED, "rest": {"n": NUMBER}})

    response = client.get("/v1/tasks/task_a/input", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert DIGITS not in response.text, response.text
    body = response.json()
    assert json.loads(body["full"]["text"]) == {"password": MASK, "rest": {"n": MASK}}
    # One for the password masked whole, one for the number holding it.
    assert body["full"]["redaction_count"] == 2, body
    assert body["redaction_count"] == 2, body
    assert body["redacted"] is True


def test_task_route_serves_the_number_as_a_mask(client, db):
    _task(db, {**NAMED, "rest": {"n": NUMBER, "steps": 3}})

    response = client.get("/v1/tasks/task_a", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert DIGITS not in response.text, response.text
    task = response.json()["task"]
    assert task["input"] == {"password": MASK, "rest": {"n": MASK, "steps": 3}}
    assert task["input_redaction_count"] == 2, task


@pytest.mark.parametrize("form", FORMS, ids=["int", "float", "exponent", "inside"])
def test_logs_mask_the_number_even_when_the_walk_masked_another_value(client, db, objects, form):
    alone = '{"n":' + form + "}"
    # The walk masks the string, so `redact_lines` skips its text safety pass:
    # the walk alone decides whether the number is served.
    beside = '{"note":"export PASSWORD=' + DIGITS + '","n":' + form + "}"
    _seed_log(db, objects, alone + "\n" + beside + "\n", input_doc=dict(NAMED))

    entry = _stdout(client)
    assert DIGITS not in entry["content"], entry["content"]
    served = [json.loads(line) for line in entry["content"].splitlines()]
    assert served == [{"n": MASK}, {"note": f"export PASSWORD={MASK}", "n": MASK}], entry["content"]
    assert entry["redaction_count"] == 3, entry


PARITY = [
    '{"n":' + DIGITS + "}",
    '{"n":' + DIGITS + ".0}",
    '{"n":' + DIGITS + "e0}",
    '{"n":9' + DIGITS + "1}",
    "[" + DIGITS + "]",
    '{"a":[{"b":' + DIGITS + "}]}",
    '{"note":"export PASSWORD=' + DIGITS + '","n":' + DIGITS + "}",
    # Floats whose `json.dumps` text is not the token as written: the walk
    # must look in the token, as the artifact path does (#387 review). Alone,
    # and beside a string the walk masks, where `redact_lines` skips its text
    # pass.
    *(
        line
        for written in (DIGITS + "e9", DIGITS + "e-3", DIGITS + "0000000000000000000.5")
        for line in ('{"n":' + written + "}", '{"note":"export PASSWORD=' + DIGITS + '","n":' + written + "}")
    ),
]


@pytest.mark.parametrize("line", PARITY)
def test_logs_and_artifacts_agree_on_every_number_form(line):
    logs = redaction.redact_lines(line, literals=(DIGITS,))
    artifact = json_masking.redact_json_window(line, literals=(DIGITS,))
    assert DIGITS not in logs.text, logs.text
    assert (logs.text, logs.count) == (artifact.text, artifact.count)
    assert json.loads(logs.text) is not None, "still one JSON document"
    assert logs.count == line.count(DIGITS), logs


def test_a_number_without_a_literal_and_booleans_are_served_as_stored(client, db, objects):
    other = "1357" + "2468"
    line = '{"n":' + other + ',"ok":true,"off":false,"none":null,"x":1.5}'
    for served in (
        redaction.redact_lines(line, literals=(DIGITS,)),
        json_masking.redact_json_window(line, literals=(DIGITS,)),
    ):
        assert (served.text, served.count) == (line, 0)

    shaped, count = JsonMasker({}, literals=(DIGITS,)).value({"ok": True, "off": False, "n": int(other)})
    assert count == 0
    assert shaped["ok"] is True and shaped["off"] is False, "a boolean stays a boolean"
    assert shaped["n"] == int(other)

    # A literal shorter than eight characters is never learned, so a short
    # number equal to it is never masked.
    short = DIGITS[:7]
    _seed_log(db, objects, '{"n":' + short + "}\n", input_doc={"password": short})
    entry = _stdout(client)
    assert entry["content"] == '{"n":' + short + "}\n"
    assert entry["redaction_count"] == 0


def test_both_paths_use_the_one_number_rule(monkeypatch):
    # The line where the walk masks the string and the safety pass is
    # skipped: with the one rule stubbed to find nothing, both paths serve
    # the number, so both went through it.
    line = '{"note":"export PASSWORD=' + DIGITS + '","n":' + DIGITS + "}"
    monkeypatch.setattr(JsonMasker, "number", lambda self, text: 0)
    for served in (
        redaction.redact_lines(line, literals=(DIGITS,)),
        json_masking.redact_json_window(line, literals=(DIGITS,)),
    ):
        assert '"n":' + DIGITS in served.text, served.text
        assert served.count == 1, served

    # And json_masking keeps no copy of the rule to fall back on.
    tree = ast.parse(inspect.getsource(json_masking))
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported = {a.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for a in node.names}
    assert "_mask_literals" not in named | imported
