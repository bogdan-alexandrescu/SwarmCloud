"""An App installation token's push is measured, not read from the repository's roles (#2650).

Dev acceptance, 2026-10-30 17:25Z to 2026-10-31: every service run -- the
claude-code group's direct-pr, the git-tools lanes, the workflow group's
implement step -- ended `publish_failed: ... its branch was not pushed: the
token has none but not push`. A service run pushes with the App installation
token the broker mints (`contents: write`), and `GET /repos/{owner}/{repo}`
answers that token's `permissions` as a collaborator's roles, every one false,
pull included. `probe_repository` read those roles and refused the push.

It measures an installation token's push by the receive-pack advertisement
instead, which GitHub serves only to a credential that may push; a PAT is
still judged by its roles.
"""

from __future__ import annotations

import base64
import email.message
import io
import json
import secrets
import urllib.error
from typing import Any

import pytest

from agent_worker import forge as forge_mod

REPO = "https://github.com/octo/widgets.git"

#: What GitHub answers an installation token on the repository read.
ROLES_OF_NO_COLLABORATOR = {
    "admin": False, "maintain": False, "push": False, "triage": False, "pull": False,
}


def _installation_token() -> str:
    return "ghs" + "_" + secrets.token_hex(18)


def _pat() -> str:
    return "github" + "_pat_" + secrets.token_hex(20)


class _Response(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status


class _Forge:
    """The repository read answers 200 with `ROLES_OF_NO_COLLABORATOR`; the
    receive-pack advertisement answers `advertisement`."""

    def __init__(self, advertisement: int) -> None:
        self.advertisement = advertisement
        self.seen: list[tuple[str, dict[str, str]]] = []

    def open(self, req: Any, timeout: float | None = None) -> Any:
        url = req.full_url
        self.seen.append((url, dict(req.header_items())))
        if url.endswith("/info/refs?service=git-receive-pack"):
            if self.advertisement == 200:
                return _Response(200, b"001f# service=git-receive-pack\n0000")
            raise urllib.error.HTTPError(
                url, self.advertisement, "refused", email.message.Message(),
                io.BytesIO(b"Write access to repository not granted."),
            )
        body = {"default_branch": "main", "permissions": ROLES_OF_NO_COLLABORATOR}
        return _Response(200, json.dumps(body).encode())


def test_an_installation_token_that_may_push_is_read_as_one(monkeypatch):
    forge = _Forge(advertisement=200)
    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", forge)
    token = _installation_token()

    access = forge_mod.probe_repository(url=REPO, token=token)

    # MUTATION: judge the installation token by `permissions.push`: refused,
    # "the token has none but not push".
    assert access is not None
    assert access.can_push is True, access.reason
    assert access.default_branch == "main"
    assert "receive-pack" in access.reason
    url, headers = forge.seen[-1]
    assert url == "https://github.com/octo/widgets.git/info/refs?service=git-receive-pack"
    expected = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert headers["Authorization"] == f"Basic {expected}"


def test_an_installation_token_that_may_not_push_is_refused_with_the_measure(monkeypatch):
    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", _Forge(advertisement=403))

    access = forge_mod.probe_repository(url=REPO, token=_installation_token())

    assert access is not None
    assert access.can_push is False
    assert "HTTP 403" in access.reason


def test_an_outage_on_the_measure_is_an_outage_not_a_refusal(monkeypatch):
    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", _Forge(advertisement=503))

    with pytest.raises(forge_mod.ForgeUnavailable):
        forge_mod.probe_repository(url=REPO, token=_installation_token())


def test_a_personal_token_is_still_judged_by_its_roles(monkeypatch):
    """A PAT's roles are its own: no false roles become a push, and the
    advertisement is never asked."""
    forge = _Forge(advertisement=200)
    monkeypatch.setattr(forge_mod, "_NO_REDIRECT_OPENER", forge)

    access = forge_mod.probe_repository(url=REPO, token=_pat())

    assert access is not None
    assert access.can_push is False
    assert access.reason == "the token has none but not push"
    assert not any(url.endswith("git-receive-pack") for url, _ in forge.seen)
