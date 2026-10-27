"""An EXPIRED OAuth access token is a refused credential, not an ordinary error (#1855).

Task task_109f1389d53342a1a2f0 failed after 6,160 s with "Failed to
authenticate. API Error: 401 OAuth access token has expired. Re-authenticate
to continue." None of `_CREDENTIAL_MARKERS` matched it -- the list named a
REVOKED token, never an expired one -- so the run failed as an ordinary error
and spent one of the task's attempts, instead of raising
`CredentialRevokedSignal` and reaching the worker's reload-and-resume path,
where the broker's refresh of the held account (#626) has already written the
replacement.

The two markers added name the authentication failure explicitly; a bare
"401" still is not one (`test_credential_reload`).

MUTATION: drop "oauth access token has expired" and "failed to
authenticate" from `_CREDENTIAL_MARKERS` and every case below goes red.
"""

from __future__ import annotations

import json

import pytest

from agent_worker.runners import cliagent
from agent_worker.runners.cliagent import detect_credential_failure, detect_rate_limit

#: The text the CLI printed, exactly as the issue quotes it.
OBSERVED = (
    "Failed to authenticate. API Error: 401 OAuth access token has expired. "
    "Re-authenticate to continue."
)


@pytest.mark.parametrize(
    "text",
    [
        OBSERVED,
        # As claude-code's stream-json `result` event carries it, with no
        # `api_error_status` field beside it.
        json.dumps({"type": "result", "subtype": "error_during_execution",
                    "is_error": True, "result": OBSERVED}),
        # The two halves alone: either is the provider refusing the credential.
        "API Error: 401 OAuth access token has expired.",
        "Failed to authenticate.",
    ],
)
def test_an_expired_access_token_is_a_refused_credential(text):
    hit, marker = detect_credential_failure(text)
    assert hit is True, f"not recognised as a refused credential: {text!r}"
    assert marker in ("oauth access token has expired", "failed to authenticate")


def test_the_observed_text_is_not_a_rate_limit():
    """The runner asks `detect_rate_limit` first: an expiry read as a 429
    would park a run that one reload fixes."""
    assert detect_rate_limit(OBSERVED)[0] is False


def test_the_new_markers_are_explicit_authentication_phrases():
    """Never a bare status: "401" alone occurs in ordinary agent output."""
    for marker in ("oauth access token has expired", "failed to authenticate"):
        assert marker in cliagent._CREDENTIAL_MARKERS
        assert marker != "401"
        assert marker == marker.lower(), "detection lower-cases the text, not the marker"
