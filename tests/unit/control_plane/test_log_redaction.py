"""Redaction of served log content, and the finding that makes it necessary.

WHAT WAS ESTABLISHED BY READING THE WORKER
------------------------------------------
`agent_worker.lifecycle._publish_live_logs` DOES scrub the tail before it
uploads it, and `_redact_before_upload` DOES scrub `stdout.log` and
`stderr.log` before the final upload. Both facts are true and neither is enough:

  * the scrub is `agent_worker.redact.scrub_text`, which replaces REGISTERED
    LITERAL VALUES and matches no patterns at all;
  * the only things ever registered are credentials this platform itself
    resolved out of Secret Manager -- `secrets.py` (twice), `lifecycle.py`
    (once) and `runners/cliagent.py` (once) are every call site of
    `register_secret` in the repository;
  * both paths begin `if ... not self.log.has_secrets: return`, so for a run
    that registered nothing -- every `mock`-profile run -- the pass does not
    execute at all.

So a GitHub token the agent minted mid-run, an `Authorization:` header a
`curl -v` echoed, or a `.env` printed out of a cloned repository reaches the
bucket in the clear and stays there. That is not a criticism of the worker's
pass; it is a statement of what it covers.

The route therefore redacts at READ time, unconditionally, whatever happened at
write time. These tests put credentials into log objects and assert they do not
come back out.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api.redaction import MASK, RULES, redact, redact_detail

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

REPO = Path(__file__).resolve().parents[3]

#: ASSEMBLED, NOT WRITTEN OUT. A credential-shaped literal in a tracked file is
#: a thing every scanner in the pipeline is paid to find, and ours are shaped
#: like the real article on purpose -- `redaction.py` matches families BY
#: PREFIX, so a sentinel of `xxxx` would pass this suite while proving nothing
#: about the rules that matter.
#:
#: That collision has now cost this repository twice in one day. GitHub push
#: protection rejected a 178-commit push over two Slack-shaped fixtures in this
#: corpus and in test_no_route_serves_credentials.py, which took a
#: filter-branch over the whole range to clear. Then the repository's OWN
#: secret scan -- security.yml, "no service account keys in the repository" --
#: failed CI on the PEM header below.
#:
#: Both scanners are right and the fixture is right. The way out is for the
#: VALUE to survive while the LITERAL stops existing: `_shape` joins fragments
#: at import time, so `redact()` receives a byte-identical string and no grep
#: over the source ever sees one. Anything added here that a scanner would
#: recognise goes through `_shape` too.
def _shape(*parts: str) -> str:
    """Join fragments into a credential-shaped value at import time.

    The point is only that no single fragment matches a secret-detection
    pattern, so the assembled value exists at runtime and nowhere on disk.
    """
    return "".join(parts)


#: One credential of each shape the house filter recognises, and the substring
#: that must never survive. These are SYNTHETIC -- every one is a random string
#: in the right alphabet, none is or ever was a real credential.
CORPUS = [
    ("openai", "sk-proj-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk", "Dd4Ee5Ff6Gg7"),
    ("anthropic", "sk-ant-api03-Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1", "Ww6Vv5Uu4Tt3"),
    ("google-oauth", "ya29.a0AfB_byC1dEfGhIjKlMnOpQrStUvWxYz", "AfB_byC1dEfG"),
    ("jwt", "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.c2ln", "InR5cCI6Ikp"),
    ("google-api-key", "AIzaSyD1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p", "SyD1a2b3c4d5"),
    ("github-classic", "ghp_1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r", "1a2b3c4d5e6f"),
    ("github-pat", "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ", "aBcDeFgHiJkL"),
    ("slack", "xoxb-SYNTHETIC-NOT-A-REAL-TOKEN-AbCdEfGhIjKlMnOp", "AbCdEfGhIjKl"),
    ("aws", "AKIAIOSFODNN7EXAMPLE", "OSFODNN7EXAM"),
    # The PEM opening line is what security.yml greps for, and it found it
    # here -- written out, in this comment's place, as a literal. Assembled,
    # the value is identical and the grep finds nothing, while redaction.py
    # still receives the whole header it matches on. Note the comment cannot
    # quote the pattern either: these scanners read comments too.
    ("private-key", _shape("-----", "BEGIN", " RSA PRIVATE KEY", "-----",
                           "MIIEpAIBAAKCAQEA1234"), "MIIEpAIBAAKC"),
    ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345", "mnopqrstuvwx"),
    ("basic", "Authorization: Basic YWxpY2U6c3VwZXJzZWNyZXQ=", "YWxpY2U6c3Vw"),
    ("assignment", 'api_key="s0m3-0p4qu3-str1ng-n0b0dy-guess3s"', "0p4qu3-str1ng"),
    ("env-dump", "ANTHROPIC_API_KEY=xyzzy-plugh-frobozz-quux", "plugh-frobozz"),
    ("env-dump-gh", "GH_TOKEN=abcdef0123456789abcdef0123456789", "0123456789abc"),
    ("json-field", '{"access_token": "q1w2e3r4t5y6u7i8o9p0"}', "e3r4t5y6u7i8"),
]


# --------------------------------------------------------------------------
# The filter itself
# --------------------------------------------------------------------------

#: The one rule that takes at least the REST OF THE LINE rather than a bounded
#: run, in both filters: a PEM block has no terminator inside the line, so
#: anything after `-----BEGIN ... PRIVATE KEY-----` is key material (and the
#: lines after it, while they look like a key's body -- #188, #206).
LINE_TERMINAL = {"private-key"}


@pytest.mark.parametrize("name,line,secret", CORPUS, ids=[c[0] for c in CORPUS])
def test_every_credential_shape_the_house_recognises_is_masked(name, line, secret):
    result = redact(f"prefix {line} suffix")
    assert secret not in result.text, f"{name} survived redaction: {result.text}"
    assert MASK in result.text
    assert result.count >= 1
    assert "prefix" in result.text, "text before the credential survives"
    if name not in LINE_TERMINAL:
        assert "suffix" in result.text, "text after the credential survives"


def test_a_whole_environment_dump_comes_back_with_nothing_usable_in_it():
    """The single most likely way a credential reaches a log object: an agent
    that prints its own environment, or a tool that does it on a crash.

    THE VALUES HERE CARRY NO RECOGNISABLE PROVIDER PREFIX, deliberately. An
    earlier version of this test used `sk-ant-...` and `ghp_...`, which the
    provider rules catch on their own -- so deleting the key/value rule
    entirely left it green. It is the NAME on the left of the `=` that has to
    do the work, because a self-hosted endpoint's token, a database password
    and an internal service's shared secret look like nothing in particular.
    """
    dump = "\n".join(
        [
            "PATH=/usr/local/bin:/usr/bin",
            "HOME=/home/swarm",
            "ANTHROPIC_API_KEY=xyzzy-plugh-frobozz-quux-1234",
            "GITHUB_TOKEN=correct-horse-battery-staple-99",
            "DB_PASSWORD=Tr0ub4dor&3-and-a-bit-more",
            "INTERNAL_SHARED_SECRET=zork-grue-lantern-brass",
            "SWARM_TASK_ID=task_27a0faef396c43f58878",
            "GOOGLE_APPLICATION_CREDENTIALS=/var/run/secrets/key.json",
        ]
    )
    result = redact(dump)
    for leaked in (
        "plugh-frobozz",
        "horse-battery",
        "Tr0ub4dor",
        "grue-lantern",
    ):
        assert leaked not in result.text, result.text
    # The non-secret lines are untouched, or the output is unreadable.
    assert "PATH=/usr/local/bin:/usr/bin" in result.text
    assert "SWARM_TASK_ID=task_27a0faef396c43f58878" in result.text


def test_the_identifying_prefix_survives_so_the_right_key_gets_rotated():
    """`sk-abc123********` still says which provider. A redactor that erased
    the whole string would leave an operator unable to tell which of four
    credentials to rotate."""
    assert redact("sk-proj-Aa1Bb2Cc3Dd4Ee5Ff6").text.startswith("sk-proj")


def test_a_literal_known_to_be_secret_is_masked_whatever_shape_it_has():
    """The same idea as the worker's registered-secret set, available to a
    caller that has one: a literal is KNOWN to be a credential, while a pattern
    only guesses."""
    result = redact("the password is hunter2-and-then-some", extra=["hunter2-and-then-some"])
    assert "hunter2" not in result.text
    assert result.count == 1


# --------------------------------------------------------------------------
# Private keys: a BLOCK, not a line (#188 review)
# --------------------------------------------------------------------------
#
# The house rule is `(BEGIN marker).*`, and `.` stops at a newline, so it masks
# the rest of the BEGIN line and nothing after it. On a raw NDJSON line that
# is the whole key -- JSON writes the key's newlines as the two characters
# `\n`. Printed as text, or DECODED out of that line, the newlines are real,
# and the line rule served the body in clear.

def _pem_marker(which: str) -> str:
    """The BEGIN or END marker of an RSA key, assembled (see `_shape`)."""
    return _shape("-----", which, " RSA PRIVATE KEY", "-----")


def _pem_body(lines: int = 24) -> list[str]:
    """Synthetic key lines: 64 base64-alphabet characters, each line unique."""
    return [f"K{n:04d}" * 12 + "QQQQ" for n in range(lines)]


def _pem_text(body: list[str]) -> str:
    return "\n".join([_pem_marker("BEGIN"), *body, _pem_marker("END")])


#: Two consecutive units of any key line: a leak of even part of one line.
KEY_LEAK = re.compile(r"K\d{4}K\d{4}")


def test_a_private_key_printed_over_many_lines_is_masked_as_a_block():
    result = redact(f"before the key\n{_pem_text(_pem_body())}\nafter the key\n")

    assert not KEY_LEAK.search(result.text), result.text
    assert result.text.startswith("before the key\n")
    assert result.text.endswith("\nafter the key\n")
    assert "PRIVATE KEY" in result.text, "the marker still says WHAT leaked"
    assert MASK in result.text
    assert result.count == 1, "one key, one mask"


def test_a_key_written_on_one_json_line_is_masked_to_its_end_and_no_further():
    """The raw NDJSON shape. Masked exactly as the line rule always masked it,
    and the next line is still readable."""
    line = json.dumps({"type": "user", "content": _pem_text(_pem_body())})
    result = redact(line + "\nthe next line\n")

    assert not KEY_LEAK.search(result.text), result.text
    assert result.text.endswith("\nthe next line\n")


def test_text_that_starts_inside_a_key_masks_the_body_above_its_end_marker():
    """A page, or a tool that printed from an offset, holds a key's END and
    not its BEGIN."""
    text = "\n".join([*_pem_body()[10:], _pem_marker("END"), "after"]) + "\n"
    result = redact(text)

    assert not KEY_LEAK.search(result.text), result.text
    assert result.text.endswith("after\n")


def test_a_key_cut_short_masks_its_body_and_stops_at_the_first_line_that_is_not_key():
    lines = [_pem_marker("BEGIN"), *_pem_body()[:12], "", "Error: the file ended early"]
    result = redact("\n".join(lines) + "\n")

    assert not KEY_LEAK.search(result.text), result.text
    assert "Error: the file ended early" in result.text


def test_a_numbered_listing_of_a_key_is_masked_with_or_without_its_end():
    """An agent's file-read tool prints `     2<TAB>MIIE...`; `cat -n` too."""
    body = _pem_body()
    whole = [_pem_marker("BEGIN"), *body, _pem_marker("END")]
    cut = [_pem_marker("BEGIN"), *body[:8]]
    for lines in (whole, cut):
        text = "".join(f"{n + 1:6d}\t{line}\n" for n, line in enumerate(lines))
        result = redact(text)
        assert not KEY_LEAK.search(result.text), result.text
        assert result.text.startswith("     1\t")


def test_a_decoded_string_masks_an_unterminated_key_to_its_end():
    """One string decoded out of a JSON document. The raw line rule masked
    everything after BEGIN on that line -- the rest of this string -- and
    decoding it first must not make it weaker."""
    body = _pem_body()
    value = (
        "\n".join([_pem_marker("BEGIN"), *body[:6]])
        + "\n 1 | ordinary words, not key material\n"
        + "\n".join(body[6:12])
    )
    decoded = redact(value, decoded=True)
    assert not KEY_LEAK.search(decoded.text), decoded.text
    assert "ordinary words" not in decoded.text

    raw = redact(json.dumps({"content": value}))
    assert "ordinary words" not in raw.text, "the raw line rule masked it too"


def test_text_the_caller_knows_begins_inside_a_key_is_masked_to_where_the_key_ends():
    text = "\n".join(_pem_body()[5:15]) + "\nordinary text after a page boundary\n"
    result = redact(text, inside_key=True)

    assert not KEY_LEAK.search(result.text), result.text
    assert "ordinary text after a page boundary" in result.text
    assert redact(text).text == text, "without that knowledge, base64 is just base64"


def test_an_upstream_error_string_is_redacted_and_bounded():
    """A storage client's exception can quote the request it failed on, and a
    signed URL is a credential with an expiry."""
    raw = (
        "Forbidden: GET https://storage.googleapis.com/b/x/o/y"
        "?X-Goog-Signature=abcdef0123456789 Authorization: Bearer "
        "abcdefghijklmnopqrstuvwxyz012345 " + "padding " * 200
    )
    cleaned = redact_detail(raw)
    assert "mnopqrstuvwx" not in cleaned
    assert len(cleaned) <= 400


# --------------------------------------------------------------------------
# Parity with the house filter in scripts/lib/common.sh
# --------------------------------------------------------------------------

def _shell_redact_rules() -> list[str]:
    """The `-e 's/.../.../'` expressions inside `redact()` in common.sh.

    Parsed rather than executed. Running the real `sed` would be a better test
    on one machine and a flaky one everywhere else -- the filter uses the `I`
    flag, which GNU sed supports and BSD sed does not, and `make test` has to
    be identical on a laptop and in CI.
    """
    text = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    return re.findall(r"-e\s+'([^']*)'", match.group(1))


def test_every_rule_in_the_house_filter_has_a_counterpart_here():
    """DRIFT CHECK, and the reason it is worth having.

    `scripts/lib/common.sh` is Track D's and is where an operator's own
    redaction lives. If a credential family is added there -- a new provider,
    a new token prefix -- and not here, then the terminal is protected and the
    browser is not, silently, and nobody compares the two files unless
    something makes them.

    This checks only that no family is missing here. That the two filters
    give the SAME output is `test_both_filters_give_the_fixtures_output_exactly`,
    over `tests/fixtures/redaction-parity.json`.
    """
    shell_rules = _shell_redact_rules()
    assert len(shell_rules) >= 11, "common.sh lost rules; that is the interesting direction too"

    markers = {rule.shell_marker for rule in RULES}
    missing = [
        expression
        for expression in shell_rules
        if not any(marker in expression for marker in markers)
    ]
    assert not missing, (
        "scripts/lib/common.sh redacts credential families that swarm_api.redaction "
        "does not, so the terminal is protected and the API is not:\n  "
        + "\n  ".join(missing)
    )


def test_the_shell_filter_is_not_wider_than_this_one_on_the_corpus():
    """The other half of the superset claim, checked where it is checkable: no
    corpus line that the house filter would catch survives this one."""
    for name, line, secret in CORPUS:
        assert secret not in redact(line).text, name


# --------------------------------------------------------------------------
# The house filter, run (#195)
# --------------------------------------------------------------------------
#
# `redact` in scripts/lib/common.sh rewrote `SWARM_ID_TOKEN: not set` as
# `SWARM_ID_TOKEN: ******** set`, so redacted output said a missing token was
# set. Measured 2026-09-25 on `swarm doctor | redact`; `swarm_mcp.auth` emits
# `("SWARM_ID_TOKEN", "not set")`. The assignment rule masks the first run of
# non-space characters after `token:`, and for `not set` that run is `not`.
#
# These RUN the function, where the parity tests above only parse it: what is
# being checked is what sed does to a line, and no parse says that. They run in
# CI, which is the gate (CLAUDE.md), on the GNU sed the filter's `I` flag is
# written for.


def _house_filter(lines: list[str]) -> list[str]:
    """`redact()` exactly as common.sh defines it, fed `lines` on stdin.

    The function's own text is lifted out and run by itself, rather than
    sourcing common.sh, whose load-time work (project, region, `.env`) is not
    what is under test and would need a configured machine.
    """
    import subprocess

    text = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    done = subprocess.run(
        ["bash", "-c", match.group(0) + "redact\n"],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert len(out) == len(lines), (lines, out)
    return out


def _house_key_words() -> list[str]:
    """The key words the house assignment rule masks after, read off the rule,
    each in one literal spelling: `api_?key` and `api[-_]?key` as `api_key`,
    `private[-_]?key` as `private_key`. The first expression whose group
    OPENS with the api-key word is the rule; the `Authorization:` scheme rule
    names `api[-_]?key` too, inside a group that does not open with it."""
    for rule in _shell_redact_rules():
        words = re.search(r"\((api(?:_|\[-_\])\?key(?:\|[a-z?_|\[\]-]+)?)\)", rule)
        if words:
            return [re.sub(r"\[-_\]\?|_\?", "_", word) for word in words.group(1).split("|")]
    raise AssertionError("no expression in redact() carries the assignment rule's key words")


@pytest.mark.parametrize(
    "line",
    [
        "SWARM_ID_TOKEN: not set",
        "SWARM_ID_TOKEN: set",
        "GITHUB_TOKEN: not set",
        "  checked   SWARM_ID_TOKEN: not set",
        "password: none",
        "api_key=(none)",
        "secret: missing",
        "token: unset",
        '{"token": "not set", "password": "none"}',
    ],
)
def test_the_house_filter_passes_a_status_word_through(line):
    """A status word is not a credential, and masking it changes what the line
    says: `not set` and `set` came out differing only by a trailing word."""
    assert _house_filter([line]) == [line]


def test_every_key_the_house_filter_masks_after_lets_not_set_through():
    """The status words are protected for EVERY key word the assignment rule
    knows -- read off that rule, so a key word added there without its status
    words comes out as `******** set` again and goes red here."""
    words = _house_key_words()
    assert len(words) >= 8, words
    lines = [f"{word.upper()}: not set" for word in words]
    assert _house_filter(lines) == lines


@pytest.mark.parametrize(
    "line,secret",
    [
        ("SWARM_ID_TOKEN: nothing-to-see-here-1234", "nothing-to-see"),
        ("token: settings-are-secret-99", "settings-are"),
        ("password: none-of-your-business", "of-your-business"),
        ("GITHUB_TOKEN=unset_me_now_123", "unset_me_now"),
        ("SWARM_ID_TOKEN: not-set-but-a-real-value", "but-a-real-value"),
        ("secret: missing_link_42", "missing_link"),
    ],
)
def test_the_house_filter_still_masks_a_value_that_starts_like_a_status_word(line, secret):
    """The fix must not weaken masking: this filter is the last of three layers
    and a regression here puts a credential in a CI log."""
    (out,) = _house_filter([line])
    assert secret not in out, out
    assert "********" in out, out


def test_the_house_filter_still_masks_the_whole_corpus():
    """Every credential shape above, through the shell filter itself.

    One input per shape: the private-key line holds a BEGIN with no END, and
    the shell filter now masks a key as a block, as this module's does (#206)
    -- so fed as one stream, the header-shaped `Authorization:` lines after it
    are taken as the key's RFC 1421 headers and masked with it, which is right
    for a key and says nothing about the Bearer rule."""
    for name, line, secret in CORPUS:
        redacted = _house_filter_text(line)
        assert secret not in redacted, f"{name} survived the house filter: {redacted}"


def test_a_planted_sentinel_cannot_shield_a_value():
    """The fix marks a protected status word with a control byte for one pass.
    A line that already carries that byte must not be able to use it to keep a
    real value out of the masking rule."""
    (out,) = _house_filter(["TOKEN\x01: hunter2-hunter2-hunter2"])
    assert "hunter2" not in out, repr(out)


# --------------------------------------------------------------------------
# An escaped quote is a quote, in both filters (#221)
# --------------------------------------------------------------------------
#
# In JSON TEXT a quote inside a string is `\"`. A claude-code stream-json log
# line holds an agent's `export DB_PASSWORD="<v>"` as `DB_PASSWORD=\"<v>\"`,
# and a curl body's `{"api_key": "<v>"}` as `{\"api_key\": \"<v>\"}`. Both
# key/value rules read a quote as a quote: the first shape had its BACKSLASH
# masked and `<v>` served under a count of 1, and the second did not match at
# all -- on `/logs`, and in the terminal. The owner decided on 2026-09-26 that
# the rule itself takes the escaped quote, and that `scripts/lib/common.sh`
# changes in the same PR so the two stay one rule. So these RUN both filters
# over the same lines and hold their output EQUAL, not merely both masked: a
# shell rule that masked the value and left a stray backslash, or the Python
# one that masked one character more, is the drift this exists to catch.
#
# MUTATIONS: drop `\\?` from either side of the key in either filter; let the
# escaped value run past a backslash; drop the escaped expression from the
# shell filter; let the plain rule take a value that starts with `\"`; drop
# the optional `[` from either filter.

#: No recognisable prefix: only the key/value rule can catch these.
_KV_SECRET = "hunter2-very-secret"
_KV_BARE = "q8Zr7Lm2Xv9T"

#: The PR #229 review's two lines, built rather than hand-escaped: a tool
#: call's command that quotes its own quotes, as one stream-json line.
_BASH_C = json.dumps(
    {"command": 'bash -c "export DB_PASSWORD=\\"' + _KV_SECRET + '\\" && ./deploy.sh"'}
)
_CURL_D = json.dumps(
    {"command": 'curl -d "{\\"api_key\\": \\"' + _KV_BARE + '\\"}" https://api'}
)
assert '\\\\\\"' + _KV_SECRET in _BASH_C, "the fixture must hold the value two escapes deep"

#: THE CASES LIVE IN ONE FIXTURE FILE NOW (wave 2026-09-27): the escaped-quote
#: cases above this comment used to be two lists here, and the shell parity
#: check had none of them. `tests/fixtures/redaction-parity.json` holds every
#: case both filters must agree on -- these, #224's names, #206's private keys,
#: `api-key`, `Authorization: token` -- and `scripts/lib/check-contract-parity.sh`
#: reads the same file, so the terminal and the API are held to one list.
PARITY_FIXTURE = REPO / "tests/fixtures/redaction-parity.json"
PARITY_CASES = json.loads(PARITY_FIXTURE.read_text())["cases"]


def _house_filter_text(text: str) -> str:
    """`redact()` from common.sh over `text` as ONE input, so a case that spans
    lines (a private key) reaches the filter as the lines a terminal would see."""
    import subprocess

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
    assert done.stdout.endswith("\n"), repr(done.stdout)
    return done.stdout[:-1]


def test_the_parity_fixture_is_the_one_set_it_claims_to_be():
    """Read, non-empty, and covering every family this wave's issues named --
    a fixture that silently lost its private-key or #224 cases would leave the
    parametrized test below green over nothing."""
    names = [case["name"] for case in PARITY_CASES]
    assert len(names) == len(set(names)), "two cases share a name"
    for family in ("kv-", "control-", "authorization-", "pem-"):
        assert any(name.startswith(family) for name in names), family
    assert len(PARITY_CASES) >= 40, len(PARITY_CASES)


@pytest.mark.parametrize("case", PARITY_CASES, ids=[c["name"] for c in PARITY_CASES])
def test_both_filters_give_the_fixtures_output_exactly(case):
    """EQUAL, not merely both masked: a shell rule that masked the value and
    left a stray backslash, or a Python one that masked one character more,
    is the drift this exists to catch."""
    python = redact(case["text"])
    shell = _house_filter_text(case["text"])
    for secret in case["never"]:
        assert secret not in python.text, f"the API served it: {python.text!r}"
        assert secret not in shell, f"the terminal printed it: {shell!r}"
    assert python.text == case["expected"], python.text
    assert shell == case["expected"], f"the two filters are not one rule: {shell!r} vs {python.text!r}"
    assert python.count == case["count"], python.count


# --------------------------------------------------------------------------
# A key's extent is decided by DISTANCE, in both filters (#206)
# --------------------------------------------------------------------------
#
# Too large for the fixture file, so built here -- and held to the same rule:
# both filters give the constructed output exactly. `PEM_BLOCK_MAX_CHARS` is
# how far after a BEGIN its END is looked for, and how far above an END with
# no BEGIN its key's lines are looked for; the shell filter streams, so it can
# only look as far as it holds, and it holds exactly that far.

def _both(text: str) -> tuple[str, str]:
    return redact(text).text, _house_filter_text(text)


def test_an_end_within_reach_masks_everything_since_its_begin_in_both_filters():
    """Not only lines shaped like a key: an END within reach closes the block
    whatever lies between, as the line rule on one JSON line always did."""
    body = _pem_body(8)
    text = "\n".join([_pem_marker("BEGIN"), *body[:4], "an ordinary line, not key material",
                      *body[4:], _pem_marker("END"), "after"])
    python, shell = _both(text)
    assert python == shell == "\n".join([_pem_marker("BEGIN") + MASK, "after"]), (python, shell)


def test_an_end_out_of_reach_is_an_orphan_and_the_key_stops_at_its_body_in_both_filters():
    from swarm_api.redaction import PEM_BLOCK_MAX_CHARS

    body = _pem_body(4)
    ordinary = [f"ordinary line {n:05d} of padding, not key material" for n in range(1500)]
    assert sum(len(line) + 1 for line in ordinary) > PEM_BLOCK_MAX_CHARS
    text = "\n".join([_pem_marker("BEGIN"), *body, *ordinary, _pem_marker("END"), "after"])
    python, shell = _both(text)
    expected = "\n".join([_pem_marker("BEGIN") + MASK, *ordinary, _pem_marker("END"), "after"])
    assert python == expected, python[:300]
    assert shell == expected, shell[:300]


def test_the_lines_above_an_orphan_end_are_taken_only_as_far_as_a_key_reaches():
    """1,100 lines of base64 and an END: a key is under 13 KB, so lines more
    than `PEM_BLOCK_MAX_CHARS` above the marker are not its lines. The bound
    is what lets the shell filter hold a run of such lines at all -- it cannot
    buffer an unbounded one -- and the API's filter keeps the same bound so
    the two agree."""
    from swarm_api.redaction import PEM_BLOCK_MAX_CHARS

    lines = [f"L{n:04d}" * 12 + "QQQQ" for n in range(1100)]
    reach = PEM_BLOCK_MAX_CHARS // (64 + 1)
    kept = len(lines) - reach
    assert 0 < kept < len(lines)
    text = "\n".join([*lines, _pem_marker("END")])
    python, shell = _both(text)
    expected = "\n".join([*lines[:kept], MASK + _pem_marker("END")])
    assert python == expected, python[-300:]
    assert shell == expected, shell[-300:]


# --------------------------------------------------------------------------
# #206 as its author wrote it, and the two guarantees no case above pins
# --------------------------------------------------------------------------
#
# The fix landed earlier: `redact()` runs an awk stage ahead of its sed rules
# that masks a key as a block, the way `mask_private_keys` does. What these
# hold is the issue's own repro, the streaming `make logs FOLLOW=1` depends on,
# and a drift check that can still see the private-key rule now that it is no
# longer one of the `-e` expressions `_shell_redact_rules()` reads.

def _house_redact_function() -> str:
    source = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    return match.group(0)


def test_the_issue_206_repro_masks_the_whole_block_and_the_control_line():
    """Steps to reproduce, line for line: a `password:` control line, a BEGIN
    marker, the two base64 body lines of `fake-key-line-1` and `-2`, an END.
    Measured before the fix, the body and the END came out in clear."""
    import base64

    control = "password: " + "notareal" + "value123"
    body = [base64.b64encode(f"fake-key-line-{n}".encode()).decode() for n in (1, 2)]
    text = "\n".join([control, _pem_marker("BEGIN"), *body, _pem_marker("END")])

    shell = _house_filter_text(text)

    assert shell == "\n".join(["password: " + MASK, _pem_marker("BEGIN") + MASK]), shell
    for line in body:
        assert line not in shell, f"a body line came through: {shell!r}"
    assert "END" not in shell, f"the END line came through: {shell!r}"
    assert shell == redact(text).text, "the terminal and the API differ on the issue's input"


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pseudo-terminal")
def test_the_house_filter_streams_a_line_outside_a_key_before_its_input_ends():
    """`make logs FOLLOW=1` pipes a tail that never ends through `redact` to a
    terminal. A line outside a key must reach the terminal while the input is
    still open -- held until EOF, a followed log shows nothing at all -- and a
    line inside an open key must not reach it in clear, then or later.

    stdout is a pseudo-terminal because that is where the command writes, and
    because a stage that is block-buffered on a pipe is line-buffered on one:
    the test sees what an operator sees."""
    import os
    import pty
    import select
    import subprocess
    import tty

    def read_for(fd: int, seconds: float, until: str | None = None) -> str:
        got = b""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                break
            try:
                chunk = os.read(fd, 65536)
            except OSError:  # EIO on Linux once the last writer has gone
                break
            if not chunk:
                break
            got += chunk
            if until is not None and until in got.decode(errors="replace"):
                break
        return got.decode(errors="replace").replace("\r\n", "\n")

    master, slave = pty.openpty()
    tty.setraw(slave)
    proc = subprocess.Popen(
        ["bash", "-c", _house_redact_function() + "redact\n"],
        stdin=subprocess.PIPE,
        stdout=slave,
        stderr=subprocess.DEVNULL,
    )
    os.close(slave)
    try:
        control = "password: " + "notareal" + "value123"
        proc.stdin.write((control + "\n").encode())
        proc.stdin.flush()
        seen = read_for(master, 10, until="\n")
        assert seen == "password: " + MASK + "\n", (
            f"a line outside any key did not arrive while the input was open: {seen!r}"
        )

        key_line = _pem_body(1)[0]
        proc.stdin.write((_pem_marker("BEGIN") + "\n" + key_line + "\n").encode())
        proc.stdin.flush()
        held = read_for(master, 1)
        assert key_line not in held, f"a line inside an open key was printed: {held!r}"

        proc.stdin.close()
        proc.wait(timeout=30)
        rest = held + read_for(master, 10)
        assert key_line not in rest, f"the key's body came out at EOF: {rest!r}"
        assert rest == _pem_marker("BEGIN") + MASK + "\n", rest
        assert proc.returncode == 0
    finally:
        proc.kill()
        proc.wait()
        os.close(master)


def test_the_drift_check_still_finds_the_private_key_rule_in_the_awk_stage():
    """`_shell_redact_rules()` reads the `-e` expressions, and the private-key
    rule left them for the awk stage, so the drift check above can no longer
    see it. This finds it where it lives: delete the awk stage, stop piping
    through it, or rename the API's rule, and this fails."""
    function = _house_redact_function()
    program = re.search(r"^  local pem='\n(.*?)^  '\n", function, re.MULTILINE | re.DOTALL)
    assert program, "redact() no longer defines its private-key awk program as `local pem='...'`"
    awk = program.group(1)
    assert re.search(r'awk\b[^\n|]*"\$\{pem\}"\s*\|\s*sed\b', function), (
        "redact() defines the awk program but no longer runs its input through it"
    )

    begre = re.search(r'^\s*BEGRE = "([^"]*)"', awk, re.MULTILINE)
    endre = re.search(r'^\s*ENDRE = "([^"]*)"', awk, re.MULTILINE)
    assert begre and endre, "the awk stage lost its BEGRE/ENDRE markers"
    assert "PRIVATE KEY" in begre.group(1), begre.group(1)
    assert "PRIVATE KEY" in endre.group(1), endre.group(1)

    rules = [rule for rule in RULES if rule.name == "private_key_block"]
    assert len(rules) == 1, "swarm_api.redaction no longer has its private_key_block rule"
    assert rules[0].shell_marker in begre.group(1), (rules[0].shell_marker, begre.group(1))
    assert rules[0].shell_marker in endre.group(1), (rules[0].shell_marker, endre.group(1))


def test_an_escaped_empty_value_is_left_alone_by_both_filters():
    """The value an escaped quote opens must hold something. Masked, an empty
    one became `\\"********"` -- the backslash gone and a credential counted
    that nobody sent."""
    line = '{\\"password\\": \\"\\", \\"note\\": \\"none here\\"}'
    assert redact(line).text == line
    assert redact(line).count == 0
    assert _house_filter([line]) == [line]
    # And two escapes deep (the PR #229 review), where a run of backslashes
    # opens the value: still nothing in it, so still nothing masked.
    deeper = json.dumps(json.dumps(json.dumps({"password": "", "note": "none here"})))
    assert '\\\\\\"password\\\\\\": \\\\\\"\\\\\\"' in deeper, deeper
    assert redact(deeper).text == deeper
    assert redact(deeper).count == 0
    assert _house_filter([deeper]) == [deeper]


def test_a_long_run_of_backslashes_is_linear_not_quadratic():
    """The run before the key is tried at every position of the text, so it is
    bounded (`\\\\{0,15}`); unbounded, 256 KiB of backslashes -- text an agent
    chooses, on an instance every tenant shares -- rescans the rest of the run
    from each of its positions."""
    import time

    run = "\\" * (256 * 1024)
    started = time.monotonic()
    for text in (run, "password=" + run + "x", '"' + run + '"api_key": "v"', run + '"'):
        redact(text)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"four 256 KiB runs of backslashes took {elapsed:.1f}s"


def test_the_transcripts_old_call_on_a_tool_input_no_longer_serves_the_value():
    """#221's repro, on the pure function: the transcript used to redact a
    tool's input as `json.dumps(input, indent=2)` with `decoded=True`. The
    transcript no longer does that (it masks by structure), but any caller
    that still redacts JSON text now gets the value masked."""
    quoted = json.dumps(
        {"command": f'export DB_PASSWORD="{_KV_SECRET}" && ./deploy.sh'}, indent=2
    )
    got = redact(quoted, decoded=True)
    assert _KV_SECRET not in got.text, got.text
    assert got.count == 1
    body = json.dumps({"command": f"curl -d '{{\"api_key\": \"{_KV_BARE}\"}}' https://x"}, indent=2)
    got = redact(body, decoded=True)
    assert _KV_BARE not in got.text, got.text
    assert got.count == 1


def test_the_log_route_masks_an_escaped_quoted_value_in_a_stream_json_line(client, db, objects):
    """The `/logs` half of #221, through the route: a raw NDJSON line holds the
    tool input as escaped JSON, and `inspect.py` redacts the window as text."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {
                "command": f'export DB_PASSWORD="{_KV_SECRET}" && curl -d \'{{"api_key": "{_KV_BARE}"}}\' x'
            }}]},
        }
    )
    assert f'\\"{_KV_SECRET}\\"' in line, "the fixture must hold the value behind escaped quotes"
    _seed(db, objects, body=line + "\n")

    response = client.get("/v1/tasks/task_a/logs?stream=stdout", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert _KV_SECRET not in response.text
    assert _KV_BARE not in response.text
    entry = next(s for s in response.json()["streams"] if s["stream"] == "stdout")
    assert entry["redaction_count"] == 2, entry["content"]


# --------------------------------------------------------------------------
# End to end, through the route
# --------------------------------------------------------------------------

def _attempt(db, attempt_id, tenant, task_id):
    created = NOW - timedelta(minutes=5)
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": 1,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": "swarm-job-eng-mock-1",
        "created_at": created,
        "started_at": created,
        "completed_at": created + timedelta(minutes=1),
        "exit_code": 0,
        "error": None,
        "peak_rss_bytes": 1,
        "oom_near_miss": False,
        "checkpoints": [],
    })


def _seed(db, objects, *, key_suffix="logs/stdout.log", body=""):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED")
    _attempt(db, "att_1", "eng", "task_a")
    objects.put(f"tenants/eng/tasks/task_a/attempts/att_1/{key_suffix}", body)


def test_a_credential_in_a_completed_log_does_not_come_out_of_the_route(client, db, objects):
    """The end-to-end proof. A real provider key is written into the object
    exactly as an agent would have printed it, and the response is searched for
    it."""
    leak = "sk-ant-api03-Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1"
    _seed(db, objects, body=f"resolving credentials\nANTHROPIC_API_KEY={leak}\ndone\n")

    response = client.get("/v1/tasks/task_a/logs", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert leak not in response.text
    assert "Zz9Yy8Xx7Ww6" not in response.text

    entry = next(s for s in response.json()["streams"] if s["stream"] == "stdout")
    assert entry["redacted"] is True
    assert entry["redaction_count"] >= 1
    assert "resolving credentials" in entry["content"], "the surrounding log is still readable"


def test_a_credential_in_a_live_tail_does_not_come_out_either(client, db, objects):
    """The live tail is the stream the worker's pass is LEAST likely to have
    covered: it is published during the run, and for a `mock` run nothing is
    registered so the pass does not execute."""
    leak = "ghp_1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r"
    _seed(
        db,
        objects,
        key_suffix="logs/live/stdout.tail.log",
        body=f"#swarm-tail offset=0 size=80\ngit push https://{leak}@github.com/x/y\n",
    )

    response = client.get("/v1/tasks/task_a/logs", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert leak not in response.text
    assert "1a2b3c4d5e6f" not in response.text


def test_the_response_states_that_redaction_ran(client, db, objects):
    """Stated rather than assumed. A deployment where this somehow stopped
    would otherwise look identical to one where it is working."""
    _seed(db, objects, body="nothing sensitive here\n")

    body = client.get("/v1/tasks/task_a/logs", headers=auth_header("alice")).json()
    assert body["redaction"]["applied_at_read_time"] is True
    assert body["redaction"]["rules"] == len(RULES)
    entry = next(s for s in body["streams"] if s["stream"] == "stdout")
    assert entry["redacted"] is False, "a clean log is reported clean, not 'redacted'"
    assert entry["redaction_count"] == 0


def test_a_credential_is_not_split_across_a_page_boundary(client, db, objects):
    """THE HOLE THIS TEST FOUND, and it was a real one.

    `redact` is a set of patterns over the text it is handed. A token cut in
    half by paging matches nothing in either half -- `sk-proj-Aa1Bb2Cc3` and
    `Dd4Ee5Ff6Gg7Hh8` are, to every rule in the table, ordinary words. The
    first implementation cut a truncated window back to the last NEWLINE, which
    is correct whenever the window contains one and useless when it does not;
    sweeping the window size across a log with a key in it returned the key in
    two clean halves.

    `inspect._align` now moves both ends of every window onto whitespace, and
    `limit_bytes` is clamped up so a window is always big enough to have a
    boundary in it. This sweep is what keeps that true.
    """
    leak = "sk-proj-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk"
    filler = "".join(f"noise line {n:05d} of an ordinary agent log\n" for n in range(400))
    body = f"{filler}ANTHROPIC_API_KEY={leak}\n{filler}"
    _seed(db, objects, body=body)

    windows = [1, 100, 4096, 4097, 5000, 8191, 8192, len(body) - 1, len(body), len(body) + 1]
    for window in windows:
        seen, offset, pages = "", 0, 0
        while True:
            response = client.get(
                f"/v1/tasks/task_a/logs?stream=stdout&offset={offset}&limit_bytes={window}",
                headers=auth_header("alice"),
            )
            assert response.status_code == 200, response.text
            assert "Dd4Ee5Ff6Gg7" not in response.text, (window, offset)
            entry = response.json()["streams"][0]
            seen += entry["content"]
            pages += 1
            if entry["next_offset"] is None:
                break
            offset = entry["next_offset"]
            assert pages < 500, "the offset is not advancing"

        # Paging is LOSSLESS as well as safe: every byte that was not part of
        # the credential comes back, in order, across the windows.
        assert seen.replace(MASK, "") == body.replace(leak, ""), window


def test_a_caller_supplied_offset_landing_mid_credential_still_does_not_leak(
    client, db, objects
):
    """The one case the line-boundary cut cannot control: the caller picks the
    START of the window. Every possible offset into a log containing a key is
    tried, and none of them may return an unmasked fragment long enough to be
    the key."""
    leak = "sk-proj-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk"
    _seed(db, objects, body=f"before\nANTHROPIC_API_KEY={leak}\nafter\n")
    total = len(f"before\nANTHROPIC_API_KEY={leak}\nafter\n")

    survivors = []
    for offset in range(total):
        response = client.get(
            f"/v1/tasks/task_a/logs?stream=stdout&offset={offset}",
            headers=auth_header("alice"),
        )
        content = response.json()["streams"][0]["content"] or ""
        if leak in content:
            survivors.append(offset)
    assert not survivors, (
        "an offset landing inside the key returned it whole: " + repr(survivors)
    )


def _page_through(client, *, offset: int, window: int) -> list[dict]:
    """Every page of stdout from `offset` to the end, `window` bytes at a time."""
    pages: list[dict] = []
    while True:
        response = client.get(
            f"/v1/tasks/task_a/logs?stream=stdout&offset={offset}&limit_bytes={window}",
            headers=auth_header("alice"),
        )
        assert response.status_code == 200, response.text
        entry = response.json()["streams"][0]
        pages.append(entry)
        if entry["next_offset"] is None:
            return pages
        offset = entry["next_offset"]
        assert len(pages) < 200, "the offset is not advancing"


def test_a_key_longer_than_the_window_leaks_from_no_page_and_no_offset(client, db, objects):
    """A plain-text key bigger than the smallest window (4 KiB) spans pages,
    and the pages in its MIDDLE hold neither marker. Each page looks back
    past its own start, so a page that begins inside a key knows it does --
    from a boundary the previous page chose or from an offset the caller
    chose."""
    before = "".join(f"line {n:03d} before\n" for n in range(40))
    after = "".join(f"line {n:03d} after\n" for n in range(40))
    log = before + _pem_text(_pem_body(200)) + "\n" + after
    _seed(db, objects, body=log)

    pages = _page_through(client, offset=0, window=4096)
    assert len(pages) > 3, "the key must span several pages for this to test anything"
    for page in pages:
        assert not KEY_LEAK.search(page["content"]), (page["offset"], page["content"][:300])
    seen = "".join(page["content"] for page in pages)
    assert seen.startswith(before), "the text before the key is whole"
    assert seen.endswith(after), "and so is the text after it"

    # Offsets across the key, and every few bytes of its BEGIN line: the
    # marker holds spaces, so a window can begin in the middle of it.
    start = len(before)
    offsets = [*range(start, start + 13_000, 211), *range(start + 1, start + 40, 3)]
    for offset in offsets:
        response = client.get(
            f"/v1/tasks/task_a/logs?stream=stdout&offset={offset}&limit_bytes=4096",
            headers=auth_header("alice"),
        )
        assert response.status_code == 200, response.text
        assert not KEY_LEAK.search(response.text), offset


def test_a_log_window_that_is_not_utf8_says_how_many_bytes_it_replaced(client, db, objects):
    """Shown as U+FFFD -- a JSON string cannot carry the byte -- and COUNTED,
    so a mangled window is never mistaken for the agent's own text."""
    _seed(db, objects, body=b"caf\xe9 au lait\nplain line\n")

    entry = client.get("/v1/tasks/task_a/logs?stream=stdout", headers=auth_header("alice")).json()[
        "streams"
    ][0]
    assert entry["status"] == "ok"
    assert entry["invalid_utf8_bytes"] == 1
    assert "\ufffd" in entry["content"]
    assert "UTF-8" in entry["detail"]

    clean = client.get(
        "/v1/tasks/task_a/logs?stream=stderr", headers=auth_header("alice")
    ).json()["streams"][0]
    assert clean["status"] == "absent"
    assert clean["invalid_utf8_bytes"] is None, "no object read, so nothing measured -- not zero"
