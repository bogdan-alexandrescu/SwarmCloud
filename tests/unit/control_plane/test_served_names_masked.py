"""The filenames in `metadata.input_from` and `metadata.expected_outputs` are masked (#227).

THE HOLE. `input_from` and `expected_outputs` are reserved: submission refuses
them from every caller, so `TaskMasking` served them exactly as stored. But
their VALUES are not the platform's words: they are the filenames a caller
wrote into the workflow spec, copied through by workflow expansion. A secret
written into a filename -- `notes-<the prompt's password>.md` -- was served in
clear beside a prompt that masked the same value.

WHAT IS PINNED:

  * a clean name, including ones a rule's short prefix turns up inside
    MID-WORD (`eye-tracking-summary.md` and `task-report-final.md`; and, after
    the second review, `key-results-OKR2024.md`, `Survey-results-FY2024.xlsx`,
    `hockey-stats-2023Q4.csv`, `eyeTracking.md`), is served byte for byte: the
    UI's workflow graph matches staged names against declared ones by
    equality;
  * a name carrying the task's learned literal, or a credential-shaped run
    that OPENS the name (`sk-abcdefghijklmnop`, `ghp_...`, even though the run
    after the prefix is itself a plain lowercase word), is masked, and masked
    IDENTICALLY under both keys, so the two still pair;
  * a word-shaped secret the task names that a prefix rule touches
    (`monkey-business-2024`, which the JWT rule sees at `ey`) is masked in a
    name too, though `redaction._carried` never learns it as a literal;
  * a real key or JWT GLUED directly onto a preceding letter or digit, with no
    separator at all (`notesAKIA...`, `xghp_...`, `keyAIzaSy...`,
    `tokeneyJ....…...…`), is still masked: only `sk-` carries a boundary;
  * a fake `sk-` token preceded by a word that itself contains "sk-" mid-word
    (`task-sk-...`, `risk-sk-...`, `desk-sk-...`) is not SWALLOWED by that
    embedded false match -- the real prefix past it is still found and masked;
  * each mask is counted in `metadata_redaction_count`;
  * `input_from`'s keys (upstream task ids) and `dispatch` are served as stored;
  * the workflow route's own step map (`GET /v1/workflows/{id}`, and the
    create response) masks `input_from` the same way `codec._step_to_api`
    masks a step's `input` -- by the step's own task's masker (owner decision
    2026-09-28).

MUTATIONS: serve the platform keys as stored again (the secret tests fail);
mask the names with `JsonMasker.text` (the clean-name tests fail); mask
`dispatch` too (the dispatch test fails); leave the name masks uncounted;
drop `TaskMasking.name_literals` (the word-shaped secret tests fail); go back
to a per-segment plain-word check (the provider-token-shape tests fail,
because a token's tail is itself plain letters); drop the lookbehind from
`_NAME_SK`'s pattern, or apply it as a post-match check instead of inside the
pattern (the swallow tests fail: the real prefix past a fake mid-word one goes
unmasked); add a lookbehind to `_NAME_JWT`, or apply a boundary check to any
of the other specific-shape families (AKIA/ASIA, `ghp_`, `github_pat_`,
`AIza`, `xox?-`, `ya29.`, `Bearer`) (the glued tests fail: a real credential
stuck onto a preceding word goes unmasked); serve `step.input_from` as stored
in `_step_to_api` (the workflow-route tests fail).
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
#: Shaped like the `ghp_` family. Not a real credential.
GITHUB = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
#: Two lowercase-letter runs after a REAL provider prefix -- indistinguishable
#: from an English word by shape alone, which is exactly why the prefix, not a
#: word check, is what has to decide (the #227 review, second round).
FAKE_SK = "sk-abcdefghijklmnop"

#: Clean names a LOOSE prefix rule would mash into the credential family whose
#: two- or three-character marker it happens to contain MID-WORD: the JWT
#: rule's `ey` inside "key", "Survey" and "hockey", and the `sk-` rule inside
#: "task"+"report" and "risk"+"assessment". None of these open a token -- the
#: marker is never at the run's start or right after a separator -- which is
#: the anchoring rule the fix applies (owner decision 2026-09-28, second
#: review of #227).
CLEAN_NAMES = [
    "eye-tracking-summary.md",
    "task-report-final.md",
    "risk-assessment.md",
    "scan-01.md",
    "reports/summary.md",
    "key-results-OKR2024.md",
    "Survey-results-FY2024.xlsx",
    "hockey-stats-2023Q4.csv",
    "eyeTracking.md",
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


@pytest.mark.parametrize("token", [OPENAI, GITHUB, FAKE_SK])
def test_a_provider_token_shape_is_masked_even_when_the_tail_is_plain_letters(token):
    """`sk-abcdefghijklmnop` is, letter for letter, a run of lowercase words --
    the same shape a per-segment plain-word check gives "task-report-final".
    What tells them apart is that the prefix OPENS the token, at the run's
    start; that is what the fix checks, not whether the letters after it read
    like a dictionary word (the #227 review, second round: the plain-word
    check let a token this shape through unmasked).
    """
    name = f"{token}.md"
    masking = _masking({"input_from": {"task_up": name}, "expected_outputs": [name]})

    value, count = masking.metadata_value()

    assert token not in json.dumps(value)
    assert value["input_from"]["task_up"].endswith(".md"), "the extension must survive the mask"
    assert MASK in value["input_from"]["task_up"]
    assert value["expected_outputs"] == [value["input_from"]["task_up"]]
    assert count == 2


#: A word that itself contains "sk-" ("task", "risk", "desk") placed BEFORE
#: the real prefix, so the leftmost `sk-` a naive scan finds is the fake one,
#: two characters short of the real one.
@pytest.mark.parametrize("prefix", ["task", "risk", "desk"])
def test_the_real_prefix_past_a_fake_mid_word_one_is_not_swallowed(prefix):
    """SWALLOW (the #227 review, third round): a boundary check applied to the
    match `re.sub` happens to find first cannot make the engine retry inside
    the span it already consumed. `task-sk-proj-<key>.md` matches first at
    `ta[sk-]proj-<key>` -- the embedded `sk-` inside "task", reaching across
    the separator to swallow the REAL `sk-` two characters later -- so a
    boundary check that rejects that match, as `_mask_anchored` did, never
    gets a second attempt starting at the real prefix. The fix bakes the
    boundary into the pattern itself (`_NAME_SK`), so the engine skips the
    illegal start on its own and finds the real one.
    """
    name = f"{prefix}-{FAKE_SK}.md"
    masking = _masking({"input_from": {"task_up": name}, "expected_outputs": [name]})

    value, count = masking.metadata_value()

    assert FAKE_SK not in json.dumps(value)
    assert MASK in value["input_from"]["task_up"]
    assert value["input_from"]["task_up"].endswith(".md")
    assert value["expected_outputs"] == [value["input_from"]["task_up"]]
    assert count == 2


#: Specific-shape credentials glued directly onto a preceding letter or
#: digit, with no separator at all.
GLUED_TOKENS = [
    "notesAKIAIOSFODNN7EXAMPLE",
    "7AKIAIOSFODNN7EXAMPLE",
    "fooASIAIOSFODNN7EXAMPLE",
    "xghp_0123456789abcdefgh",
    "mygithub_pat_0123456789abcdefgh",
    "keyAIzaSy0123456789012345678901",
    "slackxoxb-0123456789abcdef",
]


@pytest.mark.parametrize("token", GLUED_TOKENS)
def test_a_specific_shape_glued_to_a_preceding_word_is_still_masked(token):
    """GLUED (the #227 review, third round): these prefixes (`AKIA`/`ASIA`,
    `ghp_`, `github_pat_`, `AIza`, `xox?-`) are distinctive enough that a
    filename does not produce one by accident, so gluing one onto a preceding
    letter or digit is a real credential, not a false positive. Anchoring
    THESE families -- as the second cut of this fix did, uniformly -- served
    them in clear the moment they were glued. Only `sk-` keeps a boundary;
    every other family, including these, runs exactly as `RULES` applies it
    elsewhere.
    """
    name = f"{token}.md"
    masking = _masking({"input_from": {"task_up": name}, "expected_outputs": [name]})

    value, count = masking.metadata_value()

    assert token not in json.dumps(value)
    assert MASK in value["input_from"]["task_up"]
    assert value["input_from"]["task_up"].endswith(".md")
    assert value["expected_outputs"] == [value["input_from"]["task_up"]]
    assert count == 2


#: A real (fake-payload) JWT shape glued directly onto a preceding word, no
#: separator at all -- the same GLUED shape as `GLUED_TOKENS`, for the family
#: the fourth review round un-anchored.
GLUED_JWT = "tokeneyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123"


def test_a_jwt_glued_to_a_preceding_word_is_masked():
    """The fourth round: JWT does not need a boundary at all.

    `_NAME_JWT` requires `eyJ` -- not the loose two-letter `ey` `RULES`' own
    pattern uses -- plus the full three dot-separated segments. No filename in
    `CLEAN_NAMES` (including `eyeTracking.md`) produces that shape by
    accident, so leaving it unanchored is safe, and it is what catches a JWT
    stuck directly onto a preceding word, which an anchored version -- the
    third round's `_NAME_JWT` -- would still have missed.
    """
    name = f"{GLUED_JWT}.md"
    masking = _masking({"input_from": {"task_up": name}, "expected_outputs": [name]})

    value, count = masking.metadata_value()

    assert GLUED_JWT not in json.dumps(value)
    assert MASK in value["input_from"]["task_up"]
    assert value["input_from"]["task_up"].endswith(".md")
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


def test_the_workflow_route_masks_a_credential_shaped_input_from_name(client):
    """The step map `GET /v1/workflows/{id}` serves, not only the task's own copy.

    `codec._step_to_api` served `step.input_from` exactly as stored, so the
    same secret filename `test_the_task_route_and_input_route_serve_the_masked_names`
    proves is masked in `task.metadata.input_from` arrived in clear one field
    over, in the workflow's own step list (owner decision 2026-09-28). Proven
    on the real create response AND the real read, because `create_workflow`
    and `get_workflow` build the step map through two different call sites.
    """
    body = {
        "steps": [
            {"step_id": "research", "runner_profile": "mock", "input": {"prompt": "gather"}, "depends_on": []},
            {
                "step_id": "report",
                "runner_profile": "mock",
                "input": {"prompt": "write"},
                "depends_on": ["research"],
                "input_from": {"research": f"{OPENAI}.md"},
            },
        ],
    }
    created = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    created_steps = {s["step_id"]: s for s in created.json()["workflow"]["steps"]}
    assert OPENAI not in json.dumps(created_steps)
    assert MASK in created_steps["report"]["input_from"]["research"]
    assert created_steps["report"]["input_from"]["research"].endswith(".md")

    fetched = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    assert fetched.status_code == 200, fetched.text
    fetched_steps = {s["step_id"]: s for s in fetched.json()["workflow"]["steps"]}
    assert OPENAI not in json.dumps(fetched_steps)
    assert fetched_steps["report"]["input_from"] == created_steps["report"]["input_from"]


def test_the_workflow_route_leaves_a_clean_input_from_name_unchanged(client):
    """The regression this fix must not cause: a clean name stays clean here too."""
    body = {
        "steps": [
            {"step_id": "research", "runner_profile": "mock", "input": {"prompt": "gather"}, "depends_on": []},
            {
                "step_id": "report",
                "runner_profile": "mock",
                "input": {"prompt": "write"},
                "depends_on": ["research"],
                "input_from": {"research": "eyeTracking.md"},
            },
        ],
    }
    created = client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    steps = {s["step_id"]: s for s in created.json()["workflow"]["steps"]}
    assert steps["report"]["input_from"] == {"research": "eyeTracking.md"}

    fetched = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    assert fetched.status_code == 200, fetched.text
    fetched_steps = {s["step_id"]: s for s in fetched.json()["workflow"]["steps"]}
    assert fetched_steps["report"]["input_from"] == {"research": "eyeTracking.md"}


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
