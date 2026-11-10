"""A version is never a credential value in a package manifest (reported 2026-11-10).

`key_value_assignment` takes any key holding `token`, `secret` or `key`, so a
dependency named for one -- `"tiktoken": "1.0.22"` in a package.json -- refused
a lane's publish in the strict tier. In the npm family's manifests and
lockfiles a value that is WHOLLY a version specifier is not a credential,
whatever the key says; a real token there, and a version with a token glued
into it, are still refused, and the same version elsewhere is judged as before.
"""

from __future__ import annotations

import secrets

import pytest

from agent_worker import lifecycle
from swarm_redaction import credentials

MANIFESTS = ["package.json", "web/package-lock.json", "pnpm-lock.yaml", "apps/x/yarn.lock"]

#: (key, value, how the manifest writes it): each line is built at run time.
VERSIONS = [
    ("tiktoken", "1.0.22", "json"),
    ("tiktoken", "^1.2.3", "json"),
    ("js-tiktoken", "~2.0", "json"),
    ("tiktoken", ">=3 <4", "json"),
    ("tiktoken", "1.x || >=2.5.0 || 5.0.0 - 7.2.3", "json"),
    ("tiktoken", "*", "json"),
    ("@scope/tokens", "workspace:*", "json"),
    ("@scope/tokens", "workspace:^1.0.0", "json"),
    ("tokens", "npm:other-tokens@^1.2.0", "json"),
    ("tokens", "git+https://example.invalid/owner/tokens.git#semver:^1.0.0", "json"),
    ("tokens", "github:owner/tokens#v1.2.0", "json"),
    ("tiktoken", "1.0.22-canary.1a2b3c4d.20240101", "json"),
    ("tiktoken", "1.0.22", "yaml"),
    ("tiktoken", "'^1.0.22'", "yaml"),
]


def _line(key: str, value: str, style: str) -> str:
    return ('"%s": "%s",' if style == "json" else "%s: %s") % (key, value)


def _token() -> str:
    return secrets.token_hex(20)


@pytest.mark.parametrize("path", MANIFESTS)
@pytest.mark.parametrize("key, value, style", VERSIONS)
def test_a_version_specifier_is_not_a_credential_in_a_package_manifest(path, key, value, style):
    assert lifecycle._credential_in(path, "    " + _line(key, value, style) + "\n") is None


@pytest.mark.parametrize("path", MANIFESTS)
def test_a_real_token_assigned_in_a_package_manifest_is_still_refused(path):
    hit = lifecycle._credential_in(path, '  "token": "' + _token() + '"\n')

    assert hit is not None and hit.rule == "key_value_assignment", hit


@pytest.mark.parametrize(
    "value",
    [
        lambda: "1.0.0-" + _token(),
        lambda: "1.0.0+" + _token()[:30],
        lambda: "1" * 40,
        lambda: "npm:" + _token() + "@1.0.0",
        lambda: "git+https://user:" + _token() + "@example.invalid/x.git#v1.0.0",
        lambda: "1.0.0 " + _token(),
        lambda: "1.0.0-a1b2c3d4e5.f6a7b8c9d0.e1f2a3b4c5",
    ],
    ids=["pre-release", "build", "digits", "npm-alias-name", "git-userinfo", "spaced", "three-hashes"],
)
def test_a_token_glued_into_a_version_is_still_refused(value):
    hit = lifecycle._credential_in("package.json", '  "token": "' + value() + '"\n')

    assert hit is not None and hit.rule == "key_value_assignment", hit


def test_a_version_beside_a_real_token_on_one_line_names_the_token():
    line = "  " + _line("tiktoken", "1.0.22", "json") + " " + _line("secret", _token(), "json") + "\n"

    hit = lifecycle._credential_in("package.json", line)

    assert hit is not None and hit.offset == line.index('"secret'), hit


@pytest.mark.parametrize("path", ["config.json", "src/settings.py", "package.json.bak"])
def test_outside_a_package_manifest_a_version_is_judged_as_before(path):
    assert lifecycle._credential_in(path, "  " + _line("tiktoken", "1.0.22", "json") + "\n") is not None


def test_the_shared_check_and_the_worker_guard_are_one_function():
    assert lifecycle._credential_in is credentials.credential_in
