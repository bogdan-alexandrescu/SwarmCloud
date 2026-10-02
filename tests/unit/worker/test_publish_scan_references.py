"""The publish guard's tier 2 lets a REFERENCE through (owner decision, 2026-10-02).

WHAT WENT WRONG. Three of six SwarmCloud lanes finished their work and were
refused at publish by the worker's credential scan, for text that names a
credential without holding one:

(a) plugin.json's unchanged `"SWARM_PLUGIN_OAUTH_CLIENT_SECRET":
    "${user_config.oauth_client_secret}"` line, re-added by a trailing comma;
(b) a Terraform local `ANTHROPIC_API_KEY = "anthropic"`, a provider id;
(c) a test fixture shaped like an OpenAI key (a vendor rule: not relaxed here;
    the lane's fix is to build such a value at runtime).

WHAT IS PINNED. Outside test paths, a GENERIC match whose value is wholly a
`${...}` slot, or a bare lowercase identifier that is no credential, is not
refused. A literal password-shaped value, a slot with anything glued to it,
and every vendor-shaped key still are.

Every credential-shaped name and value below is assembled at runtime, so this
file adds no line the scan (or trivy) would take for a real one.
"""

from __future__ import annotations

import random
import secrets
import string

import pytest

from agent_worker import lifecycle

#: Name pieces, joined at runtime.
_SEC = "SEC" + "RET"
_KEY = "K" + "EY"
_PW = "pass" + "word"


def _rule(path: str, text: str) -> str | None:
    hit = lifecycle._credential_in(path, text)
    return hit.rule if hit is not None else None


def _plugin_line(trailing: str = ",") -> str:
    return f'    "SWARM_PLUGIN_OAUTH_CLIENT_{_SEC}": "${{user_config.oauth_client_secret}}"{trailing}\n'


def test_a_plugin_json_interpolation_slot_publishes():
    """(a): the unchanged line, re-added by a trailing comma, is a slot."""
    assert _rule(".claude-plugin/plugin.json", _plugin_line()) is None
    assert _rule("plugin/plugin.json", _plugin_line("")) is None


def test_a_terraform_provider_id_publishes():
    """(b): `ANTHROPIC_API_KEY = "anthropic"` names a provider, it holds nothing."""
    text = f'  ANTHROPIC_API_{_KEY} = "anthropic"\n'
    assert _rule("terraform/modules/secrets/locals.tf", text) is None


#: The trade-off the owner accepted: a digitless lowercase word is a NAME to
#: this rule, so these, which tier 2 refused until 2026-10-02, now publish
#: outside tests -- `retained` and `hunter-correct-horse` included.
@pytest.mark.parametrize(
    "value",
    ["swarm-tenant-acme-git", "openai", "retained", "hunter-correct-horse", "user_config.oauth_client_secret", "anthropic-api-key"],
)
def test_a_bare_lowercase_reference_publishes(value):
    text = f'config = {{"{_SEC.lower()}": "{value}"}}\n'
    assert _rule("apps/swarm-api/swarm_api/routes.py", text) is None


def test_a_password_shaped_literal_outside_tests_is_still_refused():
    """The control: lowercase and low-entropy, but digit-bearing -- a
    password, not a name."""
    text = f'"{_SEC}": "' + "hunter2" * 3 + '"\n'
    assert _rule("apps/swarm-api/swarm_api/config.py", text) == "key_value_assignment"
    assert _rule("deploy/settings.json", text) == "key_value_assignment"


@pytest.mark.parametrize("value", ["hunter2", "gpt-4o", "s3cr3t"])
def test_a_lowercase_value_with_a_digit_is_never_a_reference(value):
    """A digit is what a weak password adds to a word; a name rarely needs one."""
    text = f'login({_PW}="{value}")\n'
    assert _rule("src/app.py", text) == "key_value_assignment"


@pytest.mark.parametrize(
    "value",
    [
        "${user_config.client}" + "hunter2" * 3,
        "${user_config.client}" + "suffix",
        "prefix${user_config.client}",
        "${user_config.a}${user_config.b}",
    ],
    ids=["slot-then-password", "slot-then-suffix", "prefix-then-slot", "two-slots"],
)
def test_a_value_that_is_not_wholly_one_slot_is_still_refused(value):
    text = f'    "CLIENT_{_SEC}": "{value}"\n'
    assert _rule(".claude-plugin/plugin.json", text) == "key_value_assignment"


@pytest.mark.parametrize("operator", [":-", ":=", "-", "=", ":+", ":?"])
def test_a_slot_carrying_a_default_value_is_still_refused(operator):
    """`${DB_PW:-<literal>}` is one slot by shape, but the shell expands it to
    the literal: a slot is a reference only when it holds a NAME and nothing
    else."""
    literal = secrets.token_urlsafe(24)
    text = f'db_{_PW} = "${{DB_PW{operator}{literal}}}"\n'
    assert _rule("deploy/run.sh", text) == "key_value_assignment"


@pytest.mark.parametrize(
    "value",
    ["Swarm-Tenant-Git", "UPPERCASE_NAME", "9starts-with-digit", "a" * 65, "-leading-dash"],
    ids=["mixed-case", "upper-case", "leading-digit", "too-long", "leading-dash"],
)
def test_a_value_that_is_not_a_lowercase_identifier_is_still_refused(value):
    text = f"{_SEC.lower()} = '{value}'\n"
    assert _rule("src/app.py", text) == "key_value_assignment"


def test_a_high_entropy_lowercase_value_is_still_refused():
    """An identifier-shaped value `_looks_like_a_credential` accepts is one."""
    value = "k" + "".join(random.Random(7).sample(string.ascii_lowercase + string.digits, 36))
    assert lifecycle._looks_like_a_credential(value)
    text = f'{_SEC.lower()} = "{value}"\n'
    assert _rule("src/app.py", text) == "key_value_assignment"


def test_a_vendor_key_in_a_file_of_slots_is_still_refused():
    """Vendor rules are untouched: a slot beside the key relaxes nothing."""
    key = "sk-" + "proj-" + secrets.token_urlsafe(36).replace("-", "a").replace("_", "b")
    text = _plugin_line() + f'    "OPENAI_API_{_KEY}": "{key}"\n'
    assert _rule(".claude-plugin/plugin.json", text) == "openai_key"
    assert _rule("terraform/infra/main.tf", f'  token = "${{var.x}}"\n  k = "{key}"\n') == "openai_key"


def test_an_authorization_scheme_with_a_slot_publishes_and_a_literal_does_not():
    header = "Author" + "ization: token "
    assert _rule("deploy/client.py", f'h = "{header}${{github.token}}"\n') is None
    literal = secrets.token_urlsafe(30)
    assert _rule("deploy/client.py", f'h = "{header}{literal}"\n') is not None


def test_the_reference_rule_is_tier_two_only():
    """Tier 3 already passes these, and a test path is not made stricter."""
    assert _rule("tests/unit/test_plugin.py", _plugin_line()) is None


@pytest.mark.parametrize(
    "text",
    [
        f'{_PW} = "' + " ".join(["correct", "horse", "battery", "staple"]) + '"\n',
        f'"{_SEC}": "' + "open sesame " + "4ever" + '"\n',
        f"{_PW} = '" + "correct " + "horse" + "'\n",
    ],
    ids=["double-quoted-words", "json-words", "single-quoted-words"],
)
def test_a_multi_word_value_whose_first_word_is_a_name_is_still_refused(text):
    """The generic value class stops at whitespace, so only the first word is
    matched; a reference counts only when it is the WHOLE value."""
    assert _rule("src/app.py", text) == "key_value_assignment"


def test_a_bearer_value_followed_by_more_words_is_still_refused():
    header = "Author" + "ization: Bearer "
    text = f'h = "{header}' + "abcdefghijklmnop" + " more" + '"\n'
    assert _rule("src/app.py", text) is not None
    assert _rule("src/app.py", f'h = "{header}' + "abcdefghijklmnop" + '"\n') is None


def test_a_single_quoted_whole_reference_publishes():
    assert _rule("src/app.py", f"{_SEC.lower()} = 'anthropic'\n") is None
    assert _rule("src/app.py", f"{_SEC.lower()} = 'anthropic';\n") is None
