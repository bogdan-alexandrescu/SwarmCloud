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

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api.redaction import MASK, RULES, redact, redact_detail

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)

REPO = Path(__file__).resolve().parents[3]

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
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----MIIEpAIBAAKCAQEA1234", "MIIEpAIBAAKC"),
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
