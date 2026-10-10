"""An orphan END PRIVATE KEY marker with nothing to take above it is refused.

#361 box 82 (docs/epic-triage-2026-10-09.md row 82): a diff that writes a key
as code -- the BEGIN marker split across two string pieces, the base64 body in
a variable, the END marker whole -- carried no key material beside its END,
so `mask_private_keys` counted 0 and the publish guard, which refuses exactly
when that count is not 0 (owner, 2026-10-01), published it. The marker is
assembled at runtime, so no line of this file is one.
"""

from __future__ import annotations

from agent_worker.lifecycle import _credential_in

DASHES = "-" * 5


def test_an_end_marker_with_the_body_in_a_variable_is_refused():
    end = f"{DASHES}END RSA PRIVATE KEY{DASHES}"
    added = (
        f'BEGIN = "{DASHES}BEGIN RSA " + "PRIVATE KEY{DASHES}"\n'
        f'pem = "\\n".join([BEGIN, body, "{end}"])\n'
    )
    for path in ("app/keys.py", "tests/test_keys.py"):
        hit = _credential_in(path, added)
        assert hit is not None and hit.rule == "private_key_block", (path, hit)


def test_a_file_with_no_marker_is_not_refused_by_the_key_rule():
    hit = _credential_in("app/keys.py", 'pem = "\\n".join([BEGIN, body, END])\n')
    assert hit is None or hit.rule != "private_key_block", hit
