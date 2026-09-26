"""`GET /v1/tasks/{id}/input` -- the task's input, masked at read time, with a count.

#184 follow-up. The owner decided on 2026-09-25 that the Artifacts pane's
Inputs and the drawer's Details show "a read-time-redacted copy of the task's
input, with 'masked N', like every other output". Until this route, both drew
`task.input` straight off `GET /v1/tasks/{id}` -- the Artifacts pane under an
honest `as submitted · not masked`, Details with no qualifier at all -- so a
token pasted into a prompt was drawn in clear on two screens that mask every
other byte they show.

What is pinned:

  * THE SAME REDACTOR, NOT A SECOND ONE. Every text this route serves is what
    `swarm_api.redaction.redact` returns for it, count included -- the prompt
    as a decoded string (as `/answer` and `/transcript` treat theirs), the rest
    of the input as the JSON text the UI draws. A test that only looked for
    `********` would pass a hand-rolled masker with a different reach.
  * NEVER RAW. No planted secret appears anywhere in the response body.
  * THE KEY/VALUE RULE REACHES THE REST OF THE INPUT. `"api_token": "<no
    recognisable prefix>"` is caught only because the JSON text keeps the key
    beside its value; masking each value on its own would serve it in clear.
  * THE SHAPE OF THE INPUT IS SAID, not left for the client to re-derive from
    the raw document: a string prompt, a missing one, or one that is not a
    string.
  * TENANT-SCOPED: another tenant's task is a 404, as every task route is.
"""

from __future__ import annotations

import json

from swarm_api.redaction import RULES, redact

from .conftest import auth_header, seed_task, seed_tenant

#: Shaped like the families `redaction.RULES` knows. Not real credentials.
OPENAI = "sk-proj0123456789abcdefghijklmnopqrstuv"
GITHUB = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
#: No recognisable prefix: only the key/value rule can catch it, by its key.
BARE = "correct-horse-battery-staple-8812"


def a_task(db, input_doc, *, tenant="eng", task_id="task_a"):
    seed_tenant(db, tenant)
    seed_task(db, task_id=task_id, tenant_id=tenant, runner_profile="claude-code")
    db.docs[f"tasks/{task_id}"]["input"] = input_doc


def get(client, task_id="task_a", user="alice"):
    return client.get(f"/v1/tasks/{task_id}/input", headers=auth_header(user))


def test_the_prompt_is_served_masked_with_its_count(client, db):
    prompt = f"Deploy with OPENAI_API_KEY={OPENAI} and push with {GITHUB}.\n\nThen report."
    a_task(db, {"prompt": prompt})

    response = get(client)
    assert response.status_code == 200, response.text
    body = response.json()

    expected = redact(prompt, decoded=True)
    assert expected.count >= 2, "the fixture no longer plants what the rules catch"
    assert body["prompt_key"] == "string"
    assert body["prompt"] == {"text": expected.text, "redaction_count": expected.count}
    assert body["redaction"] == {"applied_at_read_time": True, "rules": len(RULES)}


def test_no_planted_secret_is_anywhere_in_the_response(client, db):
    a_task(
        db,
        {
            "prompt": f"use {OPENAI}",
            "repo_token": GITHUB,
            "api_token": BARE,
            "nested": {"list": [f"Authorization: Bearer {OPENAI}"]},
        },
    )
    raw = get(client).text
    for secret in (OPENAI, GITHUB, BARE):
        assert secret not in raw, f"{secret[:6]}... was served in clear"


def test_the_rest_of_the_input_is_its_json_text_masked_by_the_same_rules(client, db):
    doc = {"prompt": "summarise", "api_token": BARE, "steps": 3}
    a_task(db, doc)
    body = get(client).json()

    rest_text = json.dumps({"api_token": BARE, "steps": 3}, indent=2, ensure_ascii=False)
    rest = redact(rest_text)
    assert rest.count == 1, "the key/value rule no longer catches a quoted JSON pair"
    assert body["rest"] == {"text": rest.text, "redaction_count": rest.count}

    full_text = json.dumps(doc, indent=2, ensure_ascii=False)
    full = redact(full_text)
    assert body["full"] == {"text": full.text, "redaction_count": full.count}
    # The headline count is the WHOLE input's, each mask counted once.
    assert body["redaction_count"] == full.count
    assert body["redacted"] is True


def test_a_clean_input_is_a_measured_zero(client, db):
    a_task(db, {"prompt": "Audit the capacity code."})
    body = get(client).json()
    assert body["prompt"] == {"text": "Audit the capacity code.", "redaction_count": 0}
    assert body["rest"] is None, "nothing but the prompt was submitted"
    assert body["redaction_count"] == 0
    assert body["redacted"] is False


def test_an_input_without_a_prompt_says_so_and_serves_the_whole_input(client, db):
    a_task(db, {"url": "https://example.com", "password": BARE})
    body = get(client).json()
    assert body["prompt_key"] == "missing"
    assert body["prompt"] is None
    assert body["rest"] is None
    assert BARE not in body["full"]["text"]
    assert body["full"]["redaction_count"] == 1


def test_a_prompt_that_is_not_a_string_is_not_served_as_one(client, db):
    a_task(db, {"prompt": 42})
    body = get(client).json()
    assert body["prompt_key"] == "other"
    assert body["prompt"] is None
    assert body["full"]["text"] == json.dumps({"prompt": 42}, indent=2)


def test_an_empty_prompt_is_a_prompt(client, db):
    a_task(db, {"prompt": ""})
    body = get(client).json()
    assert body["prompt_key"] == "string"
    assert body["prompt"] == {"text": "", "redaction_count": 0}


def test_an_empty_input_is_served_as_the_empty_object(client, db):
    a_task(db, {})
    body = get(client).json()
    assert body["prompt_key"] == "missing"
    assert body["full"] == {"text": "{}", "redaction_count": 0}


def test_another_tenants_task_is_not_found(client, db):
    a_task(db, {"prompt": f"use {OPENAI}"}, tenant="research", task_id="task_r")
    response = get(client, task_id="task_r", user="alice")
    assert response.status_code == 404, response.text
    assert OPENAI not in response.text


# ---------------------------------------------------------------------------
# A secret QUOTED inside a string of the input (PR #210 review)
# ---------------------------------------------------------------------------
#
# THE HOLE. `rest` and `full` were one `redact()` over the pretty-printed JSON
# text. Inside JSON text every quote in a string VALUE is written `\"`, so the
# key/value rule, `"?[ \t]*[:=][ \t]*"?` then `[^",\s]+`, either did not match
# at all (`\"api_key\": \"<bare>\"`) or stopped its value at the backslash
# (`PASSWORD=\"<bare>\"` masked only the `\`). The prompt, masked as the
# decoded string it is, came out masked; the same prompt inside `full` came
# out in clear, under a count that said nothing was found -- and Details drew
# `full` right under the masked prompt.
#
# The bare values below have no prefix any rule knows, so only the key/value
# rule can catch them, and only with the quote seen as a quote.

#: Three bare values, one per shape. Not real credentials.
JSON_BARE = "q8Zr7Lm2Xv9T"
QUOTED_BARE = "hunter2-very-secret"
NESTED_BARE = "Zq81-bare-value-7Tx"


def test_a_secret_written_as_json_inside_the_prompt_is_nowhere_in_the_response(client, db):
    prompt = f'Call it with {{"api_key": "{JSON_BARE}"}} please'
    a_task(db, {"prompt": prompt})
    response = get(client)
    assert response.status_code == 200, response.text
    assert JSON_BARE not in response.text, "the full input serves the prompt's secret in clear"
    body = response.json()
    assert body["prompt"]["redaction_count"] == 1
    # One secret, one mask, wherever it is counted: `full` is the prompt and
    # nothing else here, so its count is the prompt's.
    assert body["full"]["redaction_count"] == 1
    assert body["redaction_count"] == 1
    assert body["redacted"] is True


def test_a_quoted_assignment_inside_the_prompt_is_nowhere_in_the_response(client, db):
    prompt = f'export DB_PASSWORD="{QUOTED_BARE}" then run'
    a_task(db, {"prompt": prompt})
    response = get(client)
    assert QUOTED_BARE not in response.text, "the full input serves the quoted value in clear"
    body = response.json()
    assert body["prompt"]["redaction_count"] == 1
    assert body["full"]["redaction_count"] == 1


def test_a_quoted_secret_in_a_nested_string_value_is_nowhere_in_the_response(client, db):
    a_task(
        db,
        {
            "prompt": "run the script",
            "env": {"script": f'export API_TOKEN="{NESTED_BARE}" && ./deploy'},
            "steps": [f'curl -d \'{{"password": "{QUOTED_BARE}"}}\''],
        },
    )
    response = get(client)
    assert response.status_code == 200, response.text
    for secret in (NESTED_BARE, QUOTED_BARE):
        assert secret not in response.text, f"{secret[:6]}... was served in clear"
    body = response.json()
    assert body["rest"]["redaction_count"] == 2
    assert body["full"]["redaction_count"] == 2


def test_a_mask_is_counted_once_however_many_blocks_draw_it(client, db):
    # `PASSWORD=` with no quotes: every version of this route caught it. The
    # property pinned is the COUNT: masking the strings first and the JSON text
    # second must not count one secret twice, and the whole input's count is
    # its prompt's plus its rest's -- the Artifacts pane draws that sum.
    a_task(
        db,
        {
            "prompt": "export PASSWORD=plainvalue-no-quotes then run",
            "api_token": BARE,
            "notes": {"deep": [f"use {GITHUB}"]},
        },
    )
    body = get(client).json()
    assert body["prompt"]["redaction_count"] == 1
    assert body["rest"]["redaction_count"] == 2, "the key beside its value, and the token in a list"
    assert body["full"]["redaction_count"] == 3
    assert body["redaction_count"] == 3


def test_a_value_beside_a_credential_key_is_masked_whatever_its_type(client, db):
    # The key/value rule over the document's own keys still reaches a value
    # that is not a string.
    a_task(db, {"prompt": "hi", "password": 918273645})
    body = get(client).json()
    assert "918273645" not in body["full"]["text"]
    assert body["rest"]["redaction_count"] == 1


def test_the_masked_text_is_still_the_input_as_json(client, db):
    # What is served is the input's JSON with its secrets masked: every string
    # still a string, and nothing of the masking machinery left behind.
    doc = {
        "prompt": f'Call it with {{"api_key": "{JSON_BARE}"}} please',
        "env": {"script": f'export API_TOKEN="{NESTED_BARE}"'},
        "steps": 3,
    }
    a_task(db, doc)
    body = get(client).json()
    full = json.loads(body["full"]["text"])
    assert full["prompt"] == body["prompt"]["text"]
    assert full["env"]["script"] == 'export API_TOKEN="********"'
    assert full["steps"] == 3
    assert json.loads(body["rest"]["text"]) == {"env": full["env"], "steps": 3}
