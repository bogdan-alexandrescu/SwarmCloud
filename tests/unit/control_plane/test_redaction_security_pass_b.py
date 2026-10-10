"""The security pass on the redaction rules (lane SEC-REDACT-B, 2026-10-09).

Each test below served a credential in clear before its fix, through the API's
filter (`swarm_redaction.redact`), the shell twin (`redact()` in
scripts/lib/common.sh) or the filename masker (`TaskMasking.name`). The rows
are #476's triage of the wave epics (docs/epic-triage-2026-10-09.md):

  * 62 -- a short value with a digit and a credential word (`token_3fa9c2e1`)
          read as a name and was served;
  * 63 -- `passphrase` was not a credential key word;
  * 64 -- a literal riding after code on the same line was served:
          `os.getenv('X', '<v>')`, `x["<v>"]`, `token: str = "<v>"`;
  * 82 -- an END PRIVATE KEY marker with nothing to take above it (the BEGIN
          split across string pieces, the body in a variable) counted 0;
  * 35 -- an `sk-` key glued to a preceding letter was served in a filename;
  * 36 -- an unsigned JWT (empty signature) was served in a filename.

Both filters must give the SAME output: the shell twin is held to the Python
one here as `tests/fixtures/redaction-parity.json` holds it, because a rule that
moved in one and not the other is the drift `check-contract-parity.sh` exists
to catch. Every value is assembled at runtime, so no line of this file is
shaped like a credential.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from swarm_api.redaction import MASK, redact
from swarm_api.task_input import TaskMasking

REPO = Path(__file__).resolve().parents[3]

#: A value no rule masks on its own shape: short, letters and digits.
WORD = "zebra" + "42" + "quokka"
#: Eight hex digits after a credential word, the shape box 62 measured.
HEX = "3fa9" + "c2e1"
DASHES = "-" * 5


def _shell(text: str) -> str:
    """`redact()` from common.sh over `text`, as a terminal would run it."""
    source = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    done = subprocess.run(
        ["bash", "-c", match.group(0) + "redact\n"],
        input=text + "\n",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout[:-1]


def _both(text: str, expected: str, count: int) -> None:
    served = redact(text)
    shell = _shell(text)
    assert served.text == expected, served.text
    assert shell == expected, f"the shell twin is not the same rule: {shell!r}"
    assert served.count == count, served.count


# --------------------------------------------------------------------------
# 62: a short value holding a digit and a credential word
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, value",
    [("token", "token_" + HEX), ("TOKEN", "TOKEN_" + HEX.upper()), ("secret", "key_" + HEX)],
)
def test_a_credential_word_beside_hex_is_not_a_name(name, value):
    """`token_3fa9c2e1` read as a snake_case name holding the word `token`, so it
    was exempt as code: every word after the credential word was any run of 16
    lowercase letters or digits. A word is now letters and up to three trailing
    digits, or up to three digits alone (`token_v2`, `key_1`)."""
    _both(f"{name} = {value}", f"{name} = {MASK}", 1)


@pytest.mark.parametrize(
    "line",
    [
        "token = token_v2",
        "token = token_1",
        "token = fetch_token",
        "page_token = page_token",
        "secret = sha256_secret",
        "api_key = API_KEY_V2",
    ],
)
def test_a_credential_name_with_a_short_number_is_still_code(line):
    _both(line, line, 0)


# --------------------------------------------------------------------------
# 63: passphrase is a credential key word, with password's rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template, expected",
    [
        ("passphrase = {v}", "passphrase = {m}"),
        ("DB_PASSPHRASE={v}", "DB_PASSPHRASE={m}"),
        ('{{"passphrase": "{v}"}}', '{{"passphrase": "{m}"}}'),
        ("ssh_passphrase: {v}", "ssh_passphrase: {m}"),
        # A human's choice, like a password: a call is not exempt under it.
        ("passphrase = read_passphrase()", "passphrase = {m}"),
    ],
)
def test_a_passphrase_is_masked(template, expected):
    _both(template.format(v=WORD), expected.format(m=MASK), 1)


@pytest.mark.parametrize("line", ["passphrase = None", "passphrase: str", "passphrase = ${PASSPHRASE}"])
def test_a_passphrase_that_is_code_is_served(line):
    _both(line, line, 0)


# --------------------------------------------------------------------------
# 64: a literal after an expression
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "api_key = os.getenv('API_KEY', '{v}')",
        "api_key = os.getenv('API_KEY','{v}')",
        'token = cfg["{v}"]',
        'token: str = "{v}"',
        "token: Optional[str] = '{v}'",
        'secret_key: SecretStr = SecretStr("{v}")',
        'token = settings.get("GH_TOKEN", default="{v}")',
        'client_secret = load(name("CLIENT_SECRET"), "{v}")',
    ],
)
def test_a_literal_after_code_is_masked_in_place(template):
    """The call, subscript or annotation around the literal is still code and is
    still served; only the literal is masked, so a patch holding it still reads
    as the code it was."""
    _both(template.format(v=WORD), template.format(v=MASK), 1)


def test_every_literal_in_one_call_is_masked():
    template = "token = make('{v}', '{w}')"
    other = "otter" + "77" + "lynx"
    _both(template.format(v=WORD, w=other), template.format(v=MASK, w=MASK), 2)


@pytest.mark.parametrize(
    "line",
    [
        # The names code reads a credential BY: an environment variable's name,
        # a key holding a credential word, an empty default.
        'token = os.environ["GH_TOKEN"]',
        'api_key = os.getenv("API_KEY", "")',
        'token = data["access_token"]',
        'api_key = request.headers.get("X-Api-Key")',
        # Prose is not a literal value: a value never holds a space.
        'password: str = Field(description="the database password")',
        # Not code at all: a JSON document's next key is not an argument.
        '{"token": null, "other": "value"}',
        '{"token": count(1), "other": "value"}',
        "token == 'abc'",
        # Code a served patch has to keep (#370): a regular expression, a
        # format string, a short code string.
        '_TOKEN = re.compile(r"[a-z0-9]+")',
        'token = stamp.strftime("%Y-%m-%d")',
        '_MASK_TOKEN = json.dumps("mask")',
        'token = raw.decode("utf-8")',
    ],
)
def test_a_name_inside_code_is_served(line):
    _both(line, line, 0)


def test_a_subscript_of_a_one_letter_name_is_masked_whole():
    """`x[...]` was never exempt (a callee has two characters or more), so the
    key/value rule masked `x[` and stopped at the quote: the literal after it
    was served. Now it is masked too."""
    _both(f'token = x["{WORD}"]', f'token = {MASK}"{MASK}"]', 2)


def test_a_literal_after_code_in_escaped_json_is_masked():
    """A stream-json line carries the same code with its quotes escaped."""
    text = 'command: \\"api_key = os.getenv(\\"API_KEY\\", \\"' + WORD + '\\")\\"'
    served = redact(text)
    shell = _shell(text)
    assert WORD not in served.text and WORD not in shell, (served.text, shell)
    assert shell == served.text


# --------------------------------------------------------------------------
# 82: an orphan END marker with nothing above it to take
# --------------------------------------------------------------------------


def test_an_orphan_end_with_the_body_in_a_variable_is_counted():
    """The BEGIN marker split across two string pieces, the body in a variable,
    the END written whole: `mask_private_keys` found an END with no key
    material before it, masked nothing and counted 0 -- and the worker's
    publish guard refuses only on a count. An orphan END is a key's tail
    whatever precedes it, so it is masked and counted."""
    end = f"{DASHES}END RSA PRIVATE KEY{DASHES}"
    text = f'pem = "\\n".join([begin, body, "{end}"])'
    _both(text, f'pem = "\\n".join([begin, body, "{MASK}{end}"])', 1)


def test_an_orphan_end_alone_on_its_line_is_counted():
    end = f"{DASHES}END EC PRIVATE KEY{DASHES}"
    _both(f"key tail follows\n{end}\ndone", f"key tail follows\n{MASK}{end}\ndone", 1)


# --------------------------------------------------------------------------
# 35 and 36: filenames
# --------------------------------------------------------------------------


def _name(value: str) -> tuple[str, int]:
    return TaskMasking({"prompt": "x"}, {"expected_outputs": [value]}).name(value)


def test_an_sk_key_glued_to_a_preceding_word_is_masked_in_a_filename():
    """`_NAME_SK` only started a match at a separator, so `desk` + a real key
    was served whole. A glued key is taken when the run after `sk-` is at least
    32 characters and mixes a digit, a capital and a lowercase letter -- what a
    generated key is, and what a descriptive filename is not."""
    body = "proj-" + "Ab3x" * 10
    key = "sk-" + body
    shown, count = _name(f"desk{key}.md")
    assert body not in shown and MASK in shown, shown
    assert shown.endswith(".md") and count == 1


@pytest.mark.parametrize(
    "clean",
    [
        "task-summary-of-the-integration-tests-2024.md",
        "risk-assessment-for-the-quarterly-planning-cycle.md",
        "desk-sk-notes.md",
    ],
)
def test_a_long_descriptive_name_holding_sk_is_served(clean):
    assert _name(clean) == (clean, 0)


def test_an_unsigned_jwt_is_masked_in_a_filename():
    """`alg: none` -- a JWT whose signature segment is empty, `header.payload.`
    -- did not match `_NAME_JWT`, which required a third segment."""
    header = "eyJ" + "hbGciOiJub25lIn0"
    payload = "eyJ" + "zdWIiOiIxIn0"
    jwt = f"{header}.{payload}."
    shown, count = _name(f"{jwt}.md")
    assert payload not in shown and MASK in shown, shown
    assert count == 1


def test_the_served_names_still_pair():
    body = "proj-" + "Ab3x" * 10
    name = f"desksk-{body}.md"
    masking = TaskMasking({"prompt": "x"}, {"input_from": {"t": name}, "expected_outputs": [name]})
    value, count = masking.metadata_value()
    assert body not in json.dumps(value)
    assert value["expected_outputs"] == [value["input_from"]["t"]]
    assert count == 2
