"""An `Authorization` value that only NAMES a variable is code, and is kept.

AI Studio's session reported it on 6 November 2026 (part of #2688): applying
a lane's swarm-work.patch back, the redaction had masked three test lines
holding `"Authorization": f"Bearer {TOKEN}"` -- a template with a variable
placeholder and no value -- so the patch no longer applied. Two rules did it:
the key/value rule took the f-string's `f` prefix for the value under the
`authorization` key, and `http_authorization_scheme` masked `${TOKEN}`,
`$TOKEN` and `%(token)s` after `Bearer`.

A placeholder (`{TOKEN}`, `${TOKEN}`, `$TOKEN`, `%(token)s`) alone after the
scheme word now leaves the line whole. Every literal is still masked, and so
is a literal glued to a placeholder. The fourth span AI Studio saw, a CI
service's `POSTGRES_PASSWORD: <literal>`, stays MASKED: it is a literal value
in a file, and no allow list is added for it.

Every fake value is built at runtime, so nothing here looks like a credential.
"""

from __future__ import annotations

import secrets

import pytest

from swarm_redaction import MASK, redact

PLACEHOLDERS = ("{TOKEN}", "${TOKEN}", "$TOKEN", "%(token)s", "{self.token}")


def _literal(length: int) -> str:
    return secrets.token_hex(length)[:length]


# --------------------------------------------------------------------------
# 1. A placeholder alone is kept, the line byte for byte.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        # AI Studio's three test lines.
        '    response = client.get("/v1/runs", headers={"Authorization": f"Bearer {TOKEN}"})',
        '        headers = {"Authorization": f"Bearer {TOKEN}"}',
        '+    headers={"Authorization": f"Bearer {TOKEN}"},',
        # The same template in the other spellings a patch holds.
        'authorization = f"Bearer {TOKEN}"',
        '"Authorization": f"token {TOKEN}"',
        "curl -H 'Authorization: Bearer ${TOKEN}' https://example.invalid",
        'curl -H "Authorization: Bearer $TOKEN" https://example.invalid',
        "Authorization: Bearer ${GITHUB_TOKEN}",
        "Authorization: token $GH_TOKEN",
        'headers = {"Authorization": "Bearer %(token)s" % values}',
        'headers = {"Authorization": f"Bearer {self.token}"}',
    ],
)
def test_a_placeholder_alone_is_kept(line: str) -> None:
    result = redact(line)
    assert result.text == line
    assert result.count == 0


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
@pytest.mark.parametrize(
    "template",
    [
        '"Authorization": f"Bearer {p}"',
        '"Authorization": "Bearer {p}"',
        "Authorization: Bearer {p}",
        "Authorization: Basic {p}",
    ],
)
def test_every_placeholder_shape_is_kept(template: str, placeholder: str) -> None:
    line = template.replace("{p}", placeholder)
    assert redact(line).text == line


def test_a_patch_hunk_of_the_template_is_served_whole() -> None:
    hunk = (
        "@@ -1,3 +1,4 @@\n"
        " def test_runs(client):\n"
        '-    response = client.get("/v1/runs")\n'
        '+    response = client.get("/v1/runs", headers={"Authorization": f"Bearer {TOKEN}"})\n'
        "     assert response.status_code == 200\n"
    )
    assert redact(hunk).text == hunk


# --------------------------------------------------------------------------
# 2. A literal is masked, as it was before.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "Authorization: Bearer {v}",
        "Authorization: token {v}",
        '"Authorization": "Bearer {v}"',
        'headers = {"Authorization": f"Bearer {v}"}',
        "curl -H 'Authorization: Bearer {v}'",
    ],
)
@pytest.mark.parametrize("length", [6, 40])
def test_a_literal_is_masked(template: str, length: int) -> None:
    value = _literal(length)
    result = redact(template.replace("{v}", value))
    assert value not in result.text
    assert MASK in result.text
    assert result.count >= 1


def test_a_short_literal_in_an_f_string_is_masked() -> None:
    # Before this change the key/value rule masked the `f` and served the
    # string after it: `"Authorization": ********"Bearer <v>"`.
    value = _literal(6)
    result = redact('headers = {"Authorization": f"Bearer ' + value + '"}')
    assert value not in result.text


# --------------------------------------------------------------------------
# 3. A literal mixed with a placeholder is masked.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
@pytest.mark.parametrize(
    "template",
    [
        '"Authorization": f"Bearer {v}{p}"',
        '"Authorization": f"Bearer {p}:{v}"',
        "Authorization: Bearer {v}{p}",
        "Authorization: Bearer {p}:{v}",
        "curl -H 'Authorization: Bearer {p}:{v}'",
        # Punctuation that could end a value, with a literal glued after it.
        "Authorization: Bearer {p};{v}",
        "Authorization: Bearer {p}){v}",
        "Authorization: Bearer {p}]{v}",
        "Authorization: Bearer {p}}{v}",
        '"Authorization": f"Bearer {p};{v}"',
        "authorization={p};{v}",
        "authorization={p}){v}",
    ],
)
def test_a_literal_mixed_with_a_placeholder_is_masked(template: str, placeholder: str) -> None:
    # After the placeholder the literal follows a `:` (a Basic pair's shape)
    # or a `;`, `)`, `]` or `}` that is not where the value ends: glued
    # straight on, `$TOKEN` and a run of letters is one longer name.
    if template.startswith("authorization=") and placeholder == "${TOKEN}":
        # Not this rule's: `<key>=${NAME}` is the shell substitution every
        # key word already keeps, before this change too (a question for the
        # owner in the PR body).
        pytest.skip("`<key>=${NAME}` is kept by the shell-substitution exemption")
    value = _literal(8)
    line = template.replace("{v}", value).replace("{p}", placeholder)
    result = redact(line)
    assert value not in result.text
    assert MASK in result.text


def test_a_default_value_inside_the_placeholder_is_masked() -> None:
    # `${TOKEN:-<v>}` carries a literal default: it is not only a name.
    value = _literal(12)
    result = redact("Authorization: Bearer ${TOKEN:-" + value + "}")
    assert value not in result.text


# --------------------------------------------------------------------------
# 4. The CI service's database password stays masked: no allow list.
# --------------------------------------------------------------------------


def test_the_ci_database_password_stays_masked() -> None:
    value = _literal(9)
    workflow = (
        "    services:\n"
        "      postgres:\n"
        "        image: postgres:16\n"
        "        env:\n"
        f"          POSTGRES_PASSWORD: {value}\n"
    )
    result = redact(workflow)
    assert value not in result.text
    assert f"POSTGRES_PASSWORD: {MASK}" in result.text
    assert result.count == 1
