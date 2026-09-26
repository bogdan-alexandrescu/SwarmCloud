"""`GET /v1/tasks/{id}/input` -- the task's input, masked at read time, with a count.

#184 follow-up. The owner decided on 2026-09-25 that the Artifacts pane's
Inputs and the drawer's Details show "a read-time-redacted copy of the task's
input, with 'masked N', like every other output". Until this route, both drew
`task.input` straight off `GET /v1/tasks/{id}` -- the Artifacts pane under an
honest `as submitted · not masked`, Details with no qualifier at all -- so a
token pasted into a prompt was drawn in clear on two screens that mask every
other byte they show.

What is pinned:

  * THE SAME REDACTOR, NOT A SECOND ONE. The prompt is what
    `swarm_api.redaction.redact` returns for it as a decoded string (as
    `/answer` and `/transcript` treat theirs), count included, when nothing
    else in the input names a secret it holds. The rest of the input goes
    through the same rules, string by string, and by its structure: a value
    under a credential's name is masked whole (`redaction.JsonMasker`). Where
    the two agree -- a string under `api_token` -- it is exactly what one
    `redact()` over the JSON text returns. A test that only looked for
    `********` would pass a hand-rolled masker with a different reach.
  * NEVER RAW. No planted secret appears anywhere in the response body --
    including one QUOTED inside a string, which one `redact()` over the JSON
    text served in clear (the PR #210 review; the section at the end).
  * THE KEY/VALUE RULE REACHES THE REST OF THE INPUT. `"api_token": "<no
    recognisable prefix>"` is caught only because the JSON text keeps the key
    beside its value; masking each value on its own would serve it in clear.
  * ONE MASK, ONE COUNT: `full`'s count is `prompt`'s plus `rest`'s.
  * THE STRUCTURE DECIDES (the PR #210 re-review, the section at the end): a
    list, an object or a number under a credential's name is masked whole and
    the text is still JSON; `null`, `""`, `[]`, `{}` and booleans are not
    counted; a key is masked as a string; a private key stored as a list of
    lines is masked BEGIN to END; what one block masks, every block masks;
    and the key/value rule is linear on a 256 KiB run.
  * THE SHAPE OF THE INPUT IS SAID, not left for the client to re-derive from
    the raw document: a string prompt, a missing one, or one that is not a
    string.
  * TENANT-SCOPED: another tenant's task is a 404, as every task route is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

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
    # A string under `api_token`, and nothing quoted: masking by the input's
    # structure serves exactly what one `redact()` over the JSON text serves,
    # the same text and the same count.
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
    # property pinned is the COUNT: masking the strings and the structure must
    # not count one secret twice, and the whole input's count is its prompt's
    # plus its rest's -- the Artifacts pane draws that sum.
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
    # A number under `password` is masked too -- and the text is still JSON,
    # which the second pass over the JSON text broke: it served
    # `"password": ********` (the PR #210 re-review).
    a_task(db, {"prompt": "hi", "password": 918273645})
    body = get(client).json()
    assert "918273645" not in body["full"]["text"]
    assert body["rest"]["redaction_count"] == 1
    assert json.loads(body["rest"]["text"]) == {"password": "********"}
    assert json.loads(body["full"]["text"]) == {"prompt": "hi", "password": "********"}


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


# ---------------------------------------------------------------------------
# The input's STRUCTURE decides what is masked (PR #210 re-review)
# ---------------------------------------------------------------------------
#
# THE HOLE. The fix-up above masked every string, then ran the rules over the
# JSON text with each string stood in, so `"api_token": "..."` still had its
# key beside its value. But the key/value rule masks the first run of
# characters after the key, and what that run is depends on the document's
# shape: under `password`, a list's `[` or an object's `{` was masked, the
# strings inside were served in clear, and the block said `masked 1`; `null`,
# `true`, `[]` and `{}` were counted as credentials; the text stopped being
# JSON; and a key that itself held `=` pushed the mask onto the `:` after it.
# Blocks drawn side by side also disagreed: a password named in the rest was
# masked there and drawn in clear in the prompt above it.
#
# Each value below has no prefix any rule knows, so only the structure, or a
# literal carried from where the structure found it, can mask it.

#: Bare values with no recognisable prefix. Not real credentials.
LIST_BARE = "S3cr3t!Pass-2026"
NESTED_OBJECT_BARE = "hunter2-Correct-Horse-9"
PROMPT_BARE = "Tr0ub4dor&3xyzQ"


def _pem(which: str) -> str:
    """A private key's marker, assembled so no scanner reads one in this file."""
    return "-----" + which + " RSA PRIVATE " + "KEY-----"


#: Synthetic body lines: base64 alphabet, no marker, each unique.
PEM_LINES = ["MIIEowIBAAKCAQEAu1SU1LfVsynth01", "VTLw7onLRnrq0IzW7yWsynth02", "LxoS2tFczGkPLPgisynth03"]


def _parses(text: str):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise AssertionError(f"the served text is not JSON ({exc}):\n{text}") from None


@pytest.mark.parametrize(
    "doc,secret",
    [
        ({"prompt": "deploy", "password": [LIST_BARE]}, LIST_BARE),
        ({"prompt": "deploy", "token": {"v": LIST_BARE}}, LIST_BARE),
        ({"prompt": "deploy", "db": {"password": {"value": NESTED_OBJECT_BARE}}}, NESTED_OBJECT_BARE),
        ({"prompt": "deploy", "secret": {"value": NESTED_OBJECT_BARE, "rotated": True}}, NESTED_OBJECT_BARE),
    ],
    ids=["list-under-password", "object-under-token", "nested-object-under-password", "object-under-secret"],
)
def test_a_list_or_object_under_a_credential_name_is_masked_whole_and_the_text_is_still_json(
    client, db, doc, secret
):
    a_task(db, doc)
    response = get(client)
    assert response.status_code == 200, response.text
    assert secret not in response.text, "a value inside a list or object under a credential name was served"
    body = response.json()
    full = _parses(body["full"]["text"])
    _parses(body["rest"]["text"])
    # One credential, one mask: the whole value is one leaf.
    assert body["rest"]["redaction_count"] == 1
    assert body["full"]["redaction_count"] == 1
    assert full["prompt"] == "deploy"


def test_null_empty_and_boolean_values_under_a_credential_name_are_drawn_as_sent_and_not_counted(client, db):
    # None of these can be a credential, and each was drawn `masked 1`: four
    # credentials to rotate for an input that holds none. The number is the
    # control: it is still masked, and it is the one count.
    doc = {
        "prompt": "empty",
        "password": "",
        "github_token": None,
        "secret": [],
        "api_key": {},
        "authorization": True,
        "credential": "   ",
        "pin_password": 918273645,
    }
    a_task(db, doc)
    body = get(client).json()
    rest = _parses(body["rest"]["text"])
    assert rest == {
        "password": "",
        "github_token": None,
        "secret": [],
        "api_key": {},
        "authorization": True,
        "credential": "   ",
        "pin_password": "********",
    }
    assert body["rest"]["redaction_count"] == 1
    assert body["full"]["redaction_count"] == 1


def test_a_key_holding_an_assignment_or_a_json_pair_is_masked_and_the_text_is_still_json(client, db):
    # A key that ends in `=` pushed the mask onto the `:` after it and served
    # the value; a key holding `PASSWORD="<bare>"` had its backslash masked;
    # a key holding a JSON pair was never read. Keys are masked as the decoded
    # strings they are.
    a_task(
        db,
        {
            "a_token=": "SECRETVALUE1234",
            'PASSWORD="hunter2-very-secret"': True,
            '{"api_key": "q8Zr7Lm2Xv9T"}': 1,
        },
    )
    response = get(client)
    for secret in ("SECRETVALUE1234", "hunter2-very-secret", "q8Zr7Lm2Xv9T"):
        assert secret not in response.text, f"{secret[:6]}... in a key, or beside one, was served"
    body = response.json()
    full = _parses(body["full"]["text"])
    assert full["a_token="] == "********"
    assert full['PASSWORD="********"'] is True
    assert full['{"api_key": "********"}'] == 1
    assert body["full"]["redaction_count"] == 3


@pytest.mark.parametrize("key", ["lines", "private_key"])
def test_a_private_key_stored_as_a_list_of_lines_is_masked_from_begin_to_end(client, db, key):
    # Each string of a list was masked on its own: the BEGIN element to its
    # end, and every body line and the END served in clear, since none of
    # them holds a marker. The same bytes joined by newlines were masked as a
    # block. Under a neutral key the block is masked element by element, one
    # count; under `private_key` the list is masked whole, one count.
    lines = [_pem("BEGIN"), *PEM_LINES, _pem("END")]
    a_task(db, {"prompt": "rotate it", key: lines, "after": ["ordinary", "words"]})
    response = get(client)
    for line in PEM_LINES:
        assert line not in response.text, "a body line of a key stored as a list was served"
    body = response.json()
    full = _parses(body["full"]["text"])
    assert body["rest"]["redaction_count"] == 1, "one key, one mask"
    assert full["after"] == ["ordinary", "words"], "what follows the key is masked too"
    if key == "lines":
        assert len(full[key]) == len(lines), "the list keeps its length"
        assert full[key][0] == _pem("BEGIN") + "********", "the marker still says what leaked"
        assert all(item == "********" for item in full[key][1:])


def test_a_secret_named_in_the_rest_of_the_input_is_masked_in_the_prompt_too(client, db):
    # The prompt block drew the password in clear under `masked 0`, and the
    # rest block directly below drew `"password": "********"` under `masked 1`.
    a_task(db, {"prompt": f"log in as admin with {LIST_BARE} and rotate it", "password": LIST_BARE})
    response = get(client)
    assert LIST_BARE not in response.text, "the prompt serves the password the rest of the input names"
    body = response.json()
    assert body["prompt"] == {"text": "log in as admin with ******** and rotate it", "redaction_count": 1}
    assert body["rest"]["redaction_count"] == 1
    assert body["full"]["redaction_count"] == 2


def test_a_value_the_prompt_assigns_is_masked_under_a_key_no_rule_knows(client, db):
    # The reverse: `DB_PASSWORD=<value>` was masked in the prompt, and the
    # same literal under `db_pass` -- a key no rule names -- was served.
    a_task(db, {"prompt": f"connect with DB_PASSWORD={PROMPT_BARE}", "db_pass": PROMPT_BARE})
    response = get(client)
    assert PROMPT_BARE not in response.text
    body = response.json()
    assert body["prompt"] == {"text": "connect with DB_PASSWORD=********", "redaction_count": 1}
    assert _parses(body["rest"]["text"]) == {"db_pass": "********"}
    assert body["rest"]["redaction_count"] == 1
    assert body["full"]["redaction_count"] == 2


def test_canonical_credential_names_are_masked_whole_wherever_the_keyword_sits(client, db):
    # The key/value rule needs its keyword to END the name and takes no hyphen
    # in `api_key`, so each of these was served beside a masked neighbour.
    secret_access = "wJalrXUtnFEMI" + "-synthetic-" + "K7MDENGbPxRfiCY"
    api_header = "c2VydmljZS1rZXkt" + "c3ludGhldGlj"
    a_task(
        db,
        {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": secret_access,
            "headers": {"x-api-key": api_header},
            "secrets": {"DB_PASS": "plain-db-pass-0042"},
            "password_hash": "pbkdf2-synthetic-0042",
            "api_keys": ["first-synthetic-key-01", "second-synthetic-key-02"],
        },
    )
    response = get(client)
    for secret in (secret_access, api_header, "plain-db-pass-0042", "pbkdf2-synthetic-0042", "first-synthetic-key-01"):
        assert secret not in response.text, f"{secret[:6]}... under a credential's name was served"
    full = _parses(response.json()["full"]["text"])
    assert full["headers"] == {"x-api-key": "********"}
    assert full["secrets"] == "********"


def test_a_value_under_a_credential_name_is_counted_once(client, db):
    # A prefixed token under `api_key` was masked by its own rule and then by
    # its key, and both were counted: `masked 2` over one `"********"`.
    a_task(db, {"prompt": "go", "api_key": OPENAI, "token": f"{GITHUB} {GITHUB} {GITHUB}"})
    body = get(client).json()
    assert _parses(body["rest"]["text"]) == {"api_key": "********", "token": "********"}
    assert body["rest"]["redaction_count"] == 2


def test_the_platforms_own_count_keys_are_not_credentials(client, db):
    # THE CONTROL for the wider names: the mock runner takes
    # `credential_revoked_times` and a `spend` block of `input_tokens`, and a
    # caller may send `max_tokens`. Each holds a keyword and is a count; none
    # is masked, and the input comes back exactly as sent.
    doc = {
        "prompt": "go",
        "steps": 3,
        "credential_revoked_times": 2,
        "max_tokens": 4096,
        "spend": {"usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01},
    }
    a_task(db, doc)
    body = get(client).json()
    assert body["full"]["redaction_count"] == 0
    assert _parses(body["full"]["text"]) == doc


# ---------------------------------------------------------------------------
# The key/value rule is linear on a long run (PR #210 re-review)
# ---------------------------------------------------------------------------
#
# Unanchored, the rule's name prefix `[A-Za-z0-9_.-]*` was rescanned from every
# start position of a run of name characters: 20,000 characters took 14 s and
# 40,000 took 57 s to 127 s. `/input` feeds it the caller's input, up to
# `max_input_bytes` (256 KiB), inside `re`, which holds the GIL of an instance
# every tenant shares. RUN IN A CHILD PROCESS WITH A TIMEOUT, so the quadratic
# rule fails this test in seconds rather than holding the suite for the forty
# minutes one 256 KiB run would take.

_TIMING_PROBE = r"""
import os
from swarm_api.redaction import redact, redact_json
size = 256 * 1024
runs = [
    "a" * size,
    os.urandom(size // 2).hex(),
    "A_" * (size // 2),
    "x.y-" * (size // 4),
]
for run in runs:
    redact(run, decoded=True)
redact_json({"prompt": "verify: " + runs[1], "note": runs[2]})
print("done")
"""


def test_the_key_value_rule_masks_a_256_kib_run_in_bounded_time():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    try:
        done = subprocess.run(
            [sys.executable, "-c", _TIMING_PROBE],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("redacting 256 KiB runs of name characters took over 30 s: the rule is quadratic again")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "done"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("ANTHROPIC_API_KEY=xyzzy-plugh-frobozz-quux", "ANTHROPIC_API_KEY=********"),
        ("GH_TOKEN=abcdef0123456789abcdef0123456789", "GH_TOKEN=********"),
        ('{"db.password": "q1w2e3r4t5y6u7i8o9p0"}', '{"db.password": "********"}'),
        ('PASSWORD="bare-value-123"', 'PASSWORD="********"'),
        ('abc"password": "zzz-secret-9"', 'abc"password": "********"'),
        (
            "x.y-z_token: tok-value-1 and PASSWORD=p2,secret=s3",
            "x.y-z_token: ******** and PASSWORD=********,secret=********",
        ),
        ("apassword=,token=x9x9x9x9", "apassword=,token=********"),
        ("the api_key is not=here", "the api_key is not=here"),
    ],
)
def test_anchoring_the_rule_changes_nothing_it_masks(line, expected):
    # Recorded from the unanchored rule before the change: anchoring the name
    # to the start of its run must mask exactly what it masked.
    assert redact(line).text == expected


def test_an_x_api_key_header_is_masked_in_text(client, db):
    # The rule's `api_?key` took no hyphen, and the module said `x-api-key:`
    # was caught. It is now: in a prompt, and in every log this API serves.
    header = "c2VydmljZS1rZXkt" + "c3ludGhldGlj"
    a_task(db, {"prompt": f'curl -H "x-api-key: {header}" https://example.com'})
    body = get(client).json()
    assert header not in body["prompt"]["text"]
    assert body["prompt"]["redaction_count"] == 1
