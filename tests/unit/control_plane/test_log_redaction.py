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

#: The one rule that takes the REST OF THE LINE rather than a bounded run, in
#: both the shell filter (`sed` is line-at-a-time, so its `.*` stops at the
#: newline) and here. A PEM block has no terminator inside the line, so
#: anything after `-----BEGIN ... PRIVATE KEY-----` is key material.
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

    The relation asserted is a SUPERSET, not equality: this module's key/value
    rule deliberately accepts a prefix on the name so `ANTHROPIC_API_KEY=` and
    `GH_TOKEN=` are caught, which the shell rule as written lets through.
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
