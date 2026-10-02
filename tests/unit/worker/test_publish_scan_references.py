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

import base64
import json
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


def test_a_tenant_secret_name_publishes():
    """The third original false positive: a secret's NAME, not its value."""
    assert _rule("deploy/resolve.py", f'{_SEC.lower()} = "swarm-tenant-eng-git"\n') is None


#: What tier 2 reads as a NAME since the owner narrowed the rule (2026-10-02,
#: #470's review): one short lowercase word, or a KNOWN reference shape -- a
#: `swarm-tenant-` secret name, a name ending `-api-key`/`-token`/`-secret`/
#: `-git`, or a `var.`/`local.`/`user_config.`/`data.`/`module.` path.
#: `retained` is a single word, so it still publishes: the accepted residue.
@pytest.mark.parametrize(
    "value",
    [
        "swarm-tenant-acme-git", "openai", "retained", "user_config.oauth_client_secret",
        "anthropic-api-key", "github-token", "db-secret", "tenant-git", "var.anthropic",
        "local.provider_key", "data.vault_name", "module.secrets.name",
        # Owner, 2026-10-02: a bare lowercase word is a reference at any length
        # outside password names; the residual risk is accepted.
        "".join(random.Random(3).choice(string.ascii_lowercase) for _ in range(32)),
        "abcdefghijklmnop",
    ],
    ids=["tenant-secret", "openai", "retained", "user-config-path", "api-key-name",
         "token-name", "db-secret", "tenant-git", "var-path", "local-path", "data-path",
         "module-path", "32-lowercase", "16-lowercase"],
)
def test_a_bare_lowercase_reference_publishes(value):
    text = f'config = {{"{_SEC.lower()}": "{value}"}}\n'
    assert _rule("apps/swarm-api/swarm_api/routes.py", text) is None


#: MAJOR 2 of #470's review: any digitless lowercase name passed, so a
#: diceware passphrase joined by `-` or `.`, or a long lowercase random token,
#: published outside tests. None of these is a known reference shape.
@pytest.mark.parametrize(
    "value",
    [
        "-".join(["correct", "horse", "battery", "staple"]),
        ".".join(["correct", "horse", "battery", "staple"]),
        "hunter-correct-horse",
        "vars.anthropic",
        "swarm-tenant",
    ],
    ids=["hyphen-words", "dot-words", "three-words", "unknown-root", "prefix-alone"],
)
def test_a_lowercase_value_of_no_known_reference_shape_is_refused(value):
    text = f'config = {{"{_SEC.lower()}": "{value}"}}\n'
    assert _rule("apps/swarm-api/swarm_api/routes.py", text) == "key_value_assignment"


@pytest.mark.parametrize(
    "word",
    ["".join(random.Random(3).choice(string.ascii_lowercase) for _ in range(32)), "abcdefghijklmnop"],
    ids=["32-lowercase", "16-lowercase"],
)
def test_a_long_lowercase_word_under_a_password_name_is_refused(word):
    assert _rule("src/app.py", f'{_PW} = "{word}"\n') == "key_value_assignment"


@pytest.mark.parametrize("quote", ['"', "'"])
def test_a_single_word_under_a_password_name_is_its_value_not_a_reference(quote):
    """A name ending `password`/`passwd` is assigned the password itself, never
    a provider id or a secret's name, so only a `${...}` slot is a reference
    there. No dictionary: the key name decides."""
    assert _rule("src/app.py", f"{_PW} = {quote}letmein{quote}\n") == "key_value_assignment"
    assert _rule("src/app.py", f'db_{_PW} = "${{var.db}}"\n') is None


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


def _alnum(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    rng = random.Random(n)
    return "".join(rng.choice(alphabet) for _ in range(n))


def _jwt() -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return ".".join([part({"alg": "HS256", "typ": "JWT"}), part({"sub": "u1"}), _alnum(43)])


def _pem() -> str:
    marker = "PRIVATE " + "KEY"
    body = "\n".join(_alnum(64, string.ascii_letters + string.digits + "+/") for _ in range(4))
    return f"-----BEGIN RSA {marker}-----\n{body}\n-----END RSA {marker}-----\n"


@pytest.mark.parametrize(
    ("make", "rule"),
    [
        (lambda: "sk-" + "proj-" + secrets.token_urlsafe(36).replace("-", "a").replace("_", "b"),
         "openai_key"),
        (lambda: "AK" + "IA" + _alnum(16, string.ascii_uppercase.replace("E", "") + string.digits),
         "aws_access_key_id"),
        (lambda: "gh" + "p_" + _alnum(36), "github_token"),
        (_jwt, "jwt"),
    ],
    ids=["openai", "akia", "ghp", "jwt"],
)
def test_a_vendor_key_in_a_file_of_slots_is_still_refused(make, rule):
    """Vendor rules are untouched: a slot beside the key relaxes nothing."""
    key = make()
    text = _plugin_line() + f'    "OPENAI_API_{_KEY}": "{key}"\n'
    assert _rule(".claude-plugin/plugin.json", text) == rule
    assert _rule("terraform/infra/main.tf", f'  token = "${{var.x}}"\n  k = "{key}"\n') == rule


def test_a_private_key_in_a_file_of_slots_is_still_refused():
    text = _plugin_line() + _pem()
    assert _rule(".claude-plugin/plugin.json", text) == "private_key_block"
    assert _rule("terraform/infra/main.tf", f'  token = "${{var.x}}"\n' + _pem()) == "private_key_block"


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
    # A value after an `Authorization` header's scheme is the credential
    # itself: no bare word there is a reference.
    assert _rule("src/app.py", f'h = "{header}' + "abcdefghijklmnop" + '"\n') is not None


@pytest.mark.parametrize("scheme", ["bearer", "Bearer", "BEARER", "basic", "Basic", "token", "Token"])
def test_an_authorization_value_is_never_a_reference_whatever_the_scheme_case(scheme):
    """#470's review: `Authorization: bearer abcdefghij` published -- ten
    lowercase letters are too short for the Bearer rule, and the header rule's
    value was read as a reference. The scheme word never names anything."""
    header = "Author" + "ization: " + scheme + " "
    assert _rule("src/app.py", f'h = "{header}abcdefghij"\n') is not None
    assert _rule("src/app.py", f"{header}abcdefghij\n") is not None
    assert _rule("src/app.py", f'h = "{header}${{github.token}}"\n') is None


@pytest.mark.parametrize("scheme", ["bearer", "Basic", "token"])
def test_a_scheme_word_assigned_to_a_credential_name_is_not_a_reference(scheme):
    assert _rule("src/app.py", f'{_SEC.lower()} = "{scheme}"\n') == "key_value_assignment"


def _secret_value() -> str:
    return "".join(random.Random(19).sample(string.ascii_letters + string.digits, 24))


@pytest.mark.parametrize(
    "text",
    [
        f'token = "x" + "{_secret_value()}"\n',
        f'token = "x", "{_secret_value()}"\n',
        f"{_PW} = 'letmein' + '{_secret_value()}'\n",
        f"{_SEC.lower()} = 'anthropic' + '{_secret_value()}'\n",
        f'{_SEC.lower()} = "anthropic" "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic" +\n',
        f'{_SEC.lower()} = ["anthropic", "{_secret_value()}"]\n',
        f'{_SEC.lower()} = "anthropic" . "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic" & "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic" || "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic" ~ "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic"|"{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic"{_secret_value()}\n',
        f'{_SEC.lower()} = "anthropic" {_secret_value()}\n',
        f'{_SEC.lower()} = "anthropic" if x else "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic"; other = "{_secret_value()}"\n',
        f'{_SEC.lower()} = "anthropic"#"{_secret_value()}"\n',
    ],
    ids=["plus", "comma-literal", "single-quoted-plus", "reference-plus", "adjacent-literal",
         "plus-at-line-end", "list-of-literals", "dot", "ampersand", "double-pipe", "tilde",
         "pipe-no-space", "bare-token-no-space", "bare-token", "conditional",
         "semicolon-then-more", "hash-without-space"],
)
def test_a_reference_followed_by_more_of_the_value_is_refused(text):
    """#470's review: `_ends_the_value` looked at the first quoted string only,
    so a short word joined to the real secret by `+`, `,` or plain adjacency
    published. A reference must be the WHOLE value."""
    assert _rule("src/app.py", text) == "key_value_assignment"


@pytest.mark.parametrize(
    "text",
    [
        f'{{"{_SEC.lower()}": "anthropic", "region": "us"}}\n',
        f'f({_SEC.lower()}="anthropic", region="us")\n',
        f'x = "{{\\"{_SEC.lower()}\\": \\"anthropic\\"}}"\n',
        f"x = '\"{_SEC.lower()}\": \"anthropic\"'\n",
        f'{_SEC.lower()} = "anthropic"  # the provider name\n',
        f'f({_SEC.lower()}="anthropic")\n',
        f'{_SEC.lower()} = "anthropic";\n',
        f'{_SEC.lower()} = "anthropic",\n',
        f'{_SEC.lower()} = "${{ANTHROPIC_REF}}"\n',
    ],
    ids=["json-next-key", "next-kwarg", "escaped-json-in-a-string", "json-in-single-quotes",
         "trailing-comment", "closing-bracket","semicolon-eol",
         "trailing-comma", "interpolation-slot"],
)
def test_a_whole_reference_followed_by_the_next_entry_publishes(text):
    """The control: what follows is the next key or argument, not more value."""
    assert _rule("src/app.py", text) is None


@pytest.mark.parametrize("name", [_PW, _SEC.lower()])
def test_an_escaped_quote_does_not_close_the_value(name):
    """#470's review: `\\"` was taken as the closing quote, so `letmein\\"
    <secret>"` was read as the whole value `letmein`."""
    text = f'{name} = "anthropic\\" {_secret_value()}"\n'
    assert _rule("src/app.py", text) == "key_value_assignment"


@pytest.mark.parametrize("quote", ["'", "`"])
@pytest.mark.parametrize("name", [_PW, _SEC.lower()])
def test_an_escaped_single_or_backtick_quote_does_not_close_the_value(name, quote):
    """The same defect in the other quote kinds: a body ending in an odd run of
    backslashes is not closed, so the rest of the line is still the value."""
    text = f"{name} = {quote}anthropic\\{quote} {_secret_value()}{quote}\n"
    assert _rule("src/app.py", text) == "key_value_assignment"


def test_a_single_quoted_whole_reference_publishes():
    assert _rule("src/app.py", f"{_SEC.lower()} = 'anthropic'\n") is None
    assert _rule("src/app.py", f"{_SEC.lower()} = 'anthropic';\n") is None
