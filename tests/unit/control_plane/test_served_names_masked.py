"""The filenames in `metadata.input_from` and `metadata.expected_outputs` are masked (#227).

THE HOLE. `input_from` and `expected_outputs` are reserved: submission refuses
them from every caller, so `TaskMasking` served them exactly as stored. But
their VALUES are not the platform's words: they are the filenames a caller
wrote into the workflow spec, copied through by workflow expansion. A secret
written into a filename -- `notes-<the prompt's password>.md` -- was served in
clear beside a prompt that masked the same value.

WHAT IS PINNED:

  * a clean name, including ones the rules would take for a credential
    (`eye-tracking-summary.md` is a JWT to the JWT rule, `task-report-final.md`
    an OpenAI key to the `sk-` rule), is served byte for byte: the UI's
    workflow graph matches staged names against declared ones by equality;
  * a name carrying the task's learned literal, or a credential-shaped run, is
    masked, and masked IDENTICALLY under both keys, so the two still pair;
  * a word-shaped secret the task names that a prefix rule touches
    (`monkey-business-2024`, which the JWT rule sees at `ey`) is masked in a
    name too, though `redaction._carried` never learns it as a literal;
  * each mask is counted in `metadata_redaction_count`;
  * `input_from`'s keys (upstream task ids) and `dispatch` are served as stored.

MUTATIONS: serve the platform keys as stored again (the secret tests fail);
mask the names with `JsonMasker.text` (the clean-name tests fail); mask
`dispatch` too (the dispatch test fails); leave the name masks uncounted;
drop `TaskMasking.name_literals` (the word-shaped secret tests fail).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api.redaction import MASK
from swarm_api.task_input import TaskMasking

from .conftest import auth_header, seed_task, seed_tenant

#: Assigned in the prompt, so the masker learns it as a literal. No rule
#: masks it on its own.
SECRET = "zebra-quartz-lantern-7731"
PROMPT = f"deploy with DB_PASSWORD={SECRET}"
#: Shaped like the `sk-` family. Not a real credential.
OPENAI = "sk-proj0123456789ABCDEFghijklmnopqrstuv"

#: Clean names the rules would mask: the JWT rule (`ey` + 8), the `sk-` rule.
CLEAN_NAMES = [
    "eye-tracking-summary.md",
    "task-report-final.md",
    "risk-assessment.md",
    "scan-01.md",
    "reports/summary.md",
]


def _masking(metadata: dict[str, Any], prompt: str = PROMPT) -> TaskMasking:
    return TaskMasking({"prompt": prompt}, metadata)


def test_a_clean_name_is_served_byte_for_byte():
    input_from = {f"task_{i}": name for i, name in enumerate(CLEAN_NAMES)}
    masking = _masking({"input_from": input_from, "expected_outputs": list(CLEAN_NAMES)})

    value, count = masking.metadata_value()

    assert value["input_from"] == input_from
    assert value["expected_outputs"] == CLEAN_NAMES
    assert count == 0
    assert json.dumps(value["input_from"]) == json.dumps(input_from)


def test_a_name_holding_the_inputs_secret_is_masked_identically_in_both_keys():
    name = f"notes-{SECRET}.md"
    masking = _masking(
        {"input_from": {"task_up": name}, "expected_outputs": [name, "report.md"]}
    )

    value, count = masking.metadata_value()

    assert SECRET not in json.dumps(value)
    served = value["input_from"]["task_up"]
    assert served == f"notes-{MASK}.md"
    assert value["expected_outputs"] == [served, "report.md"], "the two keys must still pair"
    assert list(value["input_from"]) == ["task_up"], "the upstream task id is served as stored"
    assert count == 2


def test_a_credential_shaped_name_is_masked_identically_in_both_keys():
    name = f"{OPENAI}.txt"
    masking = _masking({"input_from": {"task_up": name}, "expected_outputs": [name]})

    value, count = masking.metadata_value()

    assert OPENAI not in json.dumps(value)
    assert MASK in value["input_from"]["task_up"]
    assert value["expected_outputs"] == [value["input_from"]["task_up"]]
    assert count == 2


def test_a_literal_named_in_the_callers_metadata_is_masked_in_a_name():
    masking = _masking(
        {"deploy_secret": SECRET, "expected_outputs": [f"{SECRET}.md"]},
        prompt="summarise",
    )

    value, count = masking.metadata_value()

    assert value["expected_outputs"] == [f"{MASK}.md"]
    assert value["deploy_secret"] == MASK
    assert count == 2


def test_dispatch_is_served_as_stored_and_the_order_is_kept():
    dispatch = {
        "strategy": "integrate",
        "carrier": "branches",
        "role": "integrator",
        "integrates": ["task_0", "task_1"],
    }
    metadata = {
        "dispatch": dispatch,
        "input_from": {"task_up": f"notes-{SECRET}.md"},
        "expected_outputs": ["eye-tracking-summary.md"],
        "unit": "fanout-3",
    }
    masking = _masking(metadata)

    value, count = masking.metadata_value()

    assert value["dispatch"] == dispatch
    assert list(value) == ["dispatch", "input_from", "expected_outputs", "unit"]
    assert masking.platform_keys() == ["dispatch", "input_from", "expected_outputs"]
    assert count == 1


def test_the_task_route_and_input_route_serve_the_masked_names(client, db):
    name = f"notes-{SECRET}.md"
    dispatch = {"strategy": "integrate", "carrier": "branches", "role": "integrator", "integrates": ["task_0"]}
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="RUNNING", runner_profile="mock")
    db.docs["tasks/task_a"]["input"] = {"prompt": PROMPT}
    db.docs["tasks/task_a"]["metadata"] = {
        "dispatch": dispatch,
        "input_from": {"task_up": name},
        "expected_outputs": [name, "eye-tracking-summary.md"],
    }

    task = client.get("/v1/tasks/task_a", headers=auth_header("alice"))
    assert task.status_code == 200, task.text
    served = task.json()["task"]
    assert SECRET not in json.dumps(served)
    assert served["metadata"]["input_from"] == {"task_up": f"notes-{MASK}.md"}
    assert served["metadata"]["expected_outputs"] == [f"notes-{MASK}.md", "eye-tracking-summary.md"]
    assert served["metadata"]["dispatch"] == dispatch
    assert served["metadata_redaction_count"] == 2
    assert served["dispatch"]["strategy"] == "integrate", "the effective dispatch still reads the stored block"

    copy = client.get("/v1/tasks/task_a/input", headers=auth_header("alice"))
    assert copy.status_code == 200, copy.text
    block = copy.json()["metadata"]
    assert SECRET not in json.dumps(block)
    assert block["value"] == served["metadata"]
    assert block["redaction_count"] == 2


#: Word-shaped passphrases a prefix rule touches (the JWT rule at `ey`), so
#: `redaction._carried` never learns them as literals, and a name made of them
#: is plain words to `_plain_words` (the #227 review's major).
@pytest.mark.parametrize("passphrase", ["monkey-business-2024", "task-force-alpha"])
def test_a_word_shaped_secret_the_rules_touch_is_masked_in_both_keys(passphrase):
    name = f"notes-{passphrase}.md"
    masking = _masking(
        {"input_from": {"task_up": name}, "expected_outputs": [name, "eye-tracking-summary.md"]},
        prompt=f"deploy with DB_PASSWORD={passphrase}",
    )

    value, count = masking.metadata_value()

    assert passphrase not in json.dumps(value)
    assert value["input_from"]["task_up"] == f"notes-{MASK}.md"
    assert value["expected_outputs"] == [f"notes-{MASK}.md", "eye-tracking-summary.md"]
    assert count == 2


def test_a_word_shaped_secret_named_in_the_callers_metadata_is_masked_in_a_name():
    masking = _masking(
        {"db_password": "monkey-business-2024", "expected_outputs": ["monkey-business-2024.md", "risk-assessment.md"]},
        prompt="summarise",
    )

    value, count = masking.metadata_value()

    assert value["expected_outputs"] == [f"{MASK}.md", "risk-assessment.md"]
    assert value["db_password"] == MASK
    assert count == 2
