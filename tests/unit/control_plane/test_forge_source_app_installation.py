"""The task API's words for a service run's forge credential when the broker
mints a GitHub App installation token for it (DAYONE-CODEC-LABEL, part of
#2688).

`SubmissionService` labels a platform service account's run on a repository
whose owner has the App installed with `metadata.forge_credential_source` =
`app_installation` (SERVICE-APP-TOKEN). The slot it names is still the
tenant's, but the token that runs is the installation's, so the task API must
not say "tenant token" for it.
"""

from __future__ import annotations

import pytest

from swarm_api.codec import forge_credential_source
from swarm_api.gittokens import Scope, provider_suffix

APP_WORDS = "GitHub App installation token, minted for the run"


@pytest.mark.parametrize("slot", [
    "git",
    provider_suffix(Scope.USER, user="owner@example.com"),
    "git-r-0123abcd",
])
def test_an_app_installation_run_names_the_installation_token(slot):
    assert forge_credential_source(slot, "app_installation") == APP_WORDS


def test_an_app_installation_label_without_a_forge_credential_names_none():
    # No repository, not GitHub: there is no credential to describe.
    assert forge_credential_source(None, "app_installation") is None


def test_the_existing_labels_keep_their_words():
    user_slot = provider_suffix(Scope.USER, user="owner@example.com")
    assert forge_credential_source("git") == "tenant token, service submission"
    assert forge_credential_source("git", "tenant") == "tenant token, service submission"
    assert forge_credential_source("git", "owner") == "tenant token, service submission"
    assert forge_credential_source(user_slot) == "the submitter's GitHub credential"
    assert forge_credential_source(user_slot, "tenant") == "the submitter's GitHub credential"
    assert forge_credential_source(user_slot, "owner") == (
        "owner's GitHub connection, service submission")
    assert forge_credential_source("git-r-0123abcd") == "the repository's token"
    assert forge_credential_source("git-r-0123abcd", "owner") == "the repository's token"
    assert forge_credential_source("not-a-slot") is None
    assert forge_credential_source(None) is None
