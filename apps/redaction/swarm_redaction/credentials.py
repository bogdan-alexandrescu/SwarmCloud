"""The publish gate's credential check, shared by every process that sends text out.

`credential_in(path, text)` names the first credential in `text` -- the rule
and where its match starts, never the value -- or returns None. It is the
agent worker's publish gate (`agent_worker.lifecycle._credential_in`, which
`agent_worker.publish_scan` runs over every file an agent's commits add), and
it is the check the shared Jev client (`swarm_jev.sanitize`) asks of every
string before a call, in the worker and in swarm-api alike. `is_credential`
is that check at its STRICTEST tier, as a predicate.

WHY IT LIVES HERE (the owner, 4 November 2026). It was the worker's own, in
lifecycle.py, and the worker added it to the Jev client's scan at import. A
Jev call from swarm-api, which never imports the worker, was scanned with the
redaction rules alone, so a headerless private key or a blob of encrypted-key
ciphertext -- which no rule names -- was sent. One credential check now holds
for every Jev call, whoever makes it.

The patterns are `swarm_redaction.rules.RULES`, which read-time masking
shares; this module only judges their matches more finely (by tier, by
reference shape, by comment) and adds the headerless-key detection. Standard
library only, like the rest of the package. The comments below name the
worker's callers (`Worker._leaks_in_added_text`, `_DiffLeakScanner`) where the
publish gate is what a decision was made for.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import math
import os
import re
from typing import Any, NamedTuple

from .rules import KEY_VALUE as CREDENTIAL_KEY_VALUE
from .rules import RULES as CREDENTIAL_RULES


class CredentialHit(NamedTuple):
    """What a credential check found in a text: the rule that matched (a
    `swarm_redaction` rule name, `HEADERLESS_KEY_RULE`, or the worker's
    `REGISTERED_SECRET_RULE`) and where the match starts. Never the value."""

    rule: str
    offset: int


# -- the tiered, path-aware publish guard (#373) ------------------------------
#
# OWNER DECISION, 2026-09-30 (#373). The publish scans refused any added text
# the generic credential rules match, and a test of the redaction rules has to
# hold exactly such text (`"password": "********"`, `secret="<name>"`), so no
# SwarmCloud agent could publish one (#327's two runs, #376, #379). The guard
# is tiered by the file's path:
#
# * TIER 1, every file, always refused: a registered secret (checked by the
#   caller, `Worker._leaks_in_added_text`, never relaxed); a vendor-shaped key,
#   whose tail must carry an explicit placeholder IN A TEST PATH, so an
#   `sk-` fixture whose tail is `test-` and a run of one letter passes
#   there and is refused anywhere else; a JWT
#   whose header decodes to JSON naming `alg`; any private key.
#   PRIVATE KEYS FOLLOW MAIN'S RULE IN EVERY PATH AND TIER: refused exactly
#   when `swarm_redaction.rules.mask_private_keys` masks something, so any
#   BEGIN ... PRIVATE KEY marker and any orphan-END tail is refused, a stub
#   marker under `tests/` included. Owner decision, 2026-10-01: four review
#   rounds of relaxing it (body length, entropy, DER shape, windows) each
#   found inputs main refuses and the branch published. #373's real blocker
#   was the generic key=value rule in redaction tests, not PEM markers; a
#   test builds its marker at runtime.
#   SECURITY PASS, 2026-10-09 (owner decision: un-deferred): main's rule is
#   also laid over the WHOLE new file wherever an added line falls inside a
#   key it holds (`_added_inside_a_private_key`, #361 box 79), and a free
#   base64 blob that is key material without any marker -- encrypted-key
#   ciphertext or a plaintext DER key -- is refused as
#   `headerless_private_key` (`_headerless_key_at`, #361 box 80).
# * TIER 2, outside test paths: the generic rules refuse as before, except
#   for a REFERENCE (owner decisions, 2026-10-02; `_is_a_reference`): a value
#   that is wholly one `${name}` slot, or -- not under a password name and
#   not after an `Authorization` scheme -- one short lowercase word or a
#   KNOWN reference shape. Vendor rules are not relaxed.
# * TIER 3, in test paths: a generic match refuses only when its value looks
#   like a credential (`_looks_like_a_credential`). The loose tier is test
#   CODE only (`is_test_path`), never vendored third-party code (#361 box
#   73), and a placeholder word counts only standing alone, beside nothing
#   credential-shaped (#361 boxes 72 and 74).
#
# `swarm_redaction.RULES` is unchanged: read-time masking shares it, and a
# false positive there costs one masked word, not a refused publish.
#
# RESIDUAL RISK, accepted by the owner: a weak real password committed under a
# test path is published. Since 2026-10-02, outside test paths, tier 2 also
# publishes, as a reference, a WHOLE value (`_ends_the_value`: nothing joined
# to it by `+`, `,` or another literal, and an escaped quote does not close
# it) that is one of:
#   - a single lowercase word of ANY length (no cap since the owner's decision
#     of 2026-10-02; an all-lowercase, digitless random token outside test
#     paths therefore publishes, a residual risk the owner accepted), no `-`,
#     `.`, `_` or digit (`anthropic`, `retained`) -- so a weak
#     one-word secret under a `token`/`secret`/`api_key` name, or after a
#     prose `Bearer`/`Basic` (`http_authorization`), publishes. There is no
#     dictionary: `letmein` under `token = ` publishes. Under a name ending
#     `password`/`passwd` it does NOT: that name is assigned the password
#     itself, so only a slot is a reference there;
#   - a `swarm-tenant-` secret name, or a hyphen-joined lowercase name ending
#     `-api-key`, `-token`, `-secret` or `-git` (`hunter-horse-token` too);
#   - a dotted path under `var.`, `local.`, `user_config.`, `data.` or
#     `module.`.
# Any other hyphen- or dot-joined value (`correct-horse-battery-staple`) is
# refused, and so is every bare word after an `Authorization:` header's
# scheme, whatever its case. A `${name}` slot holds no value, so it adds no
# risk. CI's trivy secret scan still runs after the push.
#
# Since 2026-10-23 (#2030, the owner), NEITHER tier reads a comment line or a
# template literal of runtime slots with the generic rules
# (`_on_a_comment_line`, `_runtime_template`); the vendor and key rules do.
#
# Since 2026-10-23 (#2030, SCAN-IDENT, the owner), an UNQUOTED word-segment
# identifier holding one short digit run (`sessionToken2`, `api_key_v2`) is
# not a literal either, outside password names (`_is_an_identifier`).

#: The `swarm_redaction` rules that match a NAME followed by a value rather
#: than a provider's own token format. Tiers 2 and 3 apply to these. Any rule
#: not named here, `jwt` or `private_key_block` is treated as a vendor rule --
#: the stricter tier -- so a rule added to `RULES` is never silently relaxed.
GENERIC_CREDENTIAL_RULES = frozenset(
    {"key_value_assignment", "http_authorization", "http_authorization_scheme"}
)

#: A vendor key's own prefix, stripped before its tail is judged.
_VENDOR_PREFIX = re.compile(r"(?:sk-|ya29\.|AIza|gh[pousr]_|github_pat_|xox[abprs]-|AKIA|ASIA)")

#: What a value needs to look like a credential (owner decision, 2026-09-30):
#: at least this many characters...
CREDENTIAL_MIN_CHARS = 16
#: ...and at least this much Shannon entropy per character. Random base62 of
#: 16 distinct characters is 4.0 bits; English words and repeated fixtures
#: (`p4ssw0rd-p4ssw0rd` is 3.0) sit below.
CREDENTIAL_MIN_ENTROPY_BITS = 3.5

#: What marks a value as a placeholder rather than a credential: a mask, a run
#: of x, a `<name>` or `${VAR}` slot, or a word that says so. A WORD COUNTS
#: ONLY STANDING ALONE (#361 box 72, security pass 2026-10-09): bounded by a
#: character that is not a letter or digit, or by the value's edge. As a
#: substring it let `latest`, `attest` and any real value with `test` glued
#: into it read as a fixture in a test path. And a placeholder does not hide
#: what stands BESIDE it: `_looks_like_a_credential` judges each segment left
#: once it is removed, so `db-test-<24 random characters>` is a credential and
#: `fake-unmasked-credential-000` is not.
_PLACEHOLDER = re.compile(
    r"\*+|x{4,}|<[^>]*>|\$\{[^}]*\}|(?<![A-Za-z0-9])(?:fake|test|dummy|example)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: Where a refusal names a private key: its first BEGIN or END marker.
_PEM_MARKER = re.compile(r"-----(?:BEGIN|END) [A-Z ]*PRIVATE KEY-----")

#: Directory names that make a CODE file under them a test file (compared
#: lower-cased, so `Tests/x.py` is the same as `tests/x.py`). `fixtures/` and
#: `testdata/` are NOT here: they hold data, and data is judged strictly.
_TEST_DIRS = frozenset({"tests", "test", "__tests__"})
#: Only these extensions can be test CODE (owner decision, 2026-10-01). The
#: loose tier exists so a test may assert on `password="hunter2"`; a config or
#: data file (.env, .yaml, .json, .toml, .pem, no extension...) is where real
#: credentials live, so it is judged strictly even under `tests/`.
_TEST_CODE_EXTENSIONS = frozenset(
    {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
     ".kt", ".rb", ".sh", ".bats"}
)
#: File names that are test files wherever they sit (matched lower-cased).
_TEST_FILE = re.compile(r"(?:test_.+\..+|.+_test\..+|.+\.(?:test|spec)\..+)")
#: NEVER a test path, even under `tests/`: the files real credentials live in.
#: Matched against the file name; `*credential*` also against every directory.
_NEVER_TEST_FILES = (".env*", "*.pem", "*.key", "*credential*", "*secret*.json")
#: Directories that hold THIRD-PARTY code (#361 box 73, security pass
#: 2026-10-09). A `test/` inside `node_modules/` or `vendor/` is a package's
#: tests, not the agent's: the loose tier exists so the agent's own tests can
#: assert on fixtures, and a vendored tree is copied in, not written to that
#: end. Anything under one of these is judged strictly (compared lower-cased).
_THIRD_PARTY_DIRS = frozenset(
    {"node_modules", "vendor", "third_party", "third-party", "bower_components", "site-packages"}
)


def is_test_path(path: str) -> bool:
    """True when `path` (repository-relative, `/`-separated) is test CODE for
    the publish guard's loose tier (owner decisions, 2026-09-30 and
    2026-10-01, #373).

    Both must hold: the file has a code extension (`_TEST_CODE_EXTENSIONS`),
    and it sits under a `tests/`, `test/` or `__tests__/` directory at any
    depth (case-insensitive) or is named `test_*`, `*_test.*`, `*.test.*` or
    `*.spec.*`. A file named `*credential*` -- or under a `*credential*`
    directory, or under a third-party directory (`_THIRD_PARTY_DIRS`) --
    never is. An empty or unknown path is not one: the guard falls to its
    stricter tier when it cannot tell.
    """
    if not path:
        return False
    parts = path.split("/")
    name = parts[-1].lower()
    if os.path.splitext(name)[1] not in _TEST_CODE_EXTENSIONS:
        return False
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _NEVER_TEST_FILES):
        return False
    if any("credential" in part.lower() for part in parts[:-1]):
        return False
    if any(part.lower() in _THIRD_PARTY_DIRS for part in parts[:-1]):
        return False
    if any(part.lower() in _TEST_DIRS for part in parts[:-1]):
        return True
    return _TEST_FILE.fullmatch(name) is not None


def _shannon_bits(value: str) -> float:
    """Shannon entropy of `value`'s characters, in bits per character."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    size = len(value)
    return -sum((n / size) * math.log2(n / size) for n in counts.values())


def _looks_like_a_credential(value: str) -> bool:
    """True when `value` is shaped like a real credential rather than a
    fixture: at least `CREDENTIAL_MIN_CHARS` characters, letters AND digits,
    at least `CREDENTIAL_MIN_ENTROPY_BITS` of entropy per character.

    A value holding a placeholder (`_PLACEHOLDER`) is a fixture only when no
    segment beside it -- what is left once the placeholders are removed,
    split at every character that is not a letter or digit -- is itself
    credential-shaped (#361 box 72): `test-password-123` and
    `fake-unmasked-credential-000` are fixtures, `db-test-<random>` is not.
    """
    if len(value) < CREDENTIAL_MIN_CHARS:
        return False
    if _PLACEHOLDER.search(value) is not None:
        rest = _PLACEHOLDER.sub(" ", value)
        return any(_looks_like_a_credential(part) for part in _NOT_ALNUM.split(rest))
    if not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        return False
    return _shannon_bits(value) >= CREDENTIAL_MIN_ENTROPY_BITS


_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def _decodes_as_a_jwt(token: str) -> bool:
    """True when `token`'s first segment base64url-decodes to a JSON object
    naming `alg`, which every JWT header does. `eyword_only_args`, or an
    `ey...` run in a fixture, is not one.

    A HEADER NESTED PAST THE PARSER'S DEPTH COUNTS AS ONE (#361). `json.loads`
    raises RecursionError, not ValueError, on an array or object nested about a
    thousand deep, and that escaped the publish guard and failed the attempt
    with a traceback. Such a header base64url-decodes to JSON-shaped text, which
    an identifier never does, so the guard fails CLOSED on it: a finding, which
    folds or refuses. Text that does not decode at all stays "not a JWT" -- that
    is what keeps `eyword_only_args` from folding every history that names it.
    """
    header = token.split(".", 1)[0]
    try:
        decoded = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
    except ValueError:  # binascii.Error, UnicodeDecodeError and JSONDecodeError all are
        return False
    except RecursionError:
        return True
    return isinstance(decoded, dict) and "alg" in decoded


#: Words that mark a vendor-shaped fixture as one, each STANDING ALONE in the
#: token (#361 box 74): an `sk-` key whose tail is `test-aaaa` says it is a
#: fixture; an AWS key id ending `<12 random>TEST` only has the word glued to
#: a real-shaped key.
_VENDOR_PLACEHOLDER = re.compile(
    r"(?<![A-Za-z0-9])(?:example|test|fake|dummy)(?![A-Za-z0-9])|x{4,}", re.IGNORECASE
)
#: The shortest segment beside a vendor placeholder that can be key material:
#: a key's variable part is 16 to 40 random characters, and a fixture's
#: segments are words, digit runs or a few letters (an `sk-` tail of
#: `ant-test-0000`, a `ya29.` tail of `fake-access-token`).
_VENDOR_SEGMENT_MIN_CHARS = 8
#: The vendors' own documented example values, matched whole. AWS's access key
#: id example glues EXAMPLE into the key, which box 74 no longer accepts from
#: an arbitrary key; this value is the published one, nothing else.
_DOCUMENTED_EXAMPLES = frozenset({"AKIA" + "IOSFODNN7" + "EXAMPLE"})


def _is_explicit_placeholder(tail: str, token: str = "") -> bool:
    """True when a vendor token's variable part says it is a fixture: it is
    one repeated character, the vendor's documented example (`token`), or it
    holds a placeholder word standing alone or a run of four x AND no segment
    left beside them is key-shaped (#361 box 74): at least
    `_VENDOR_SEGMENT_MIN_CHARS` characters, six of them distinct, not only
    digits, and mixing letters with digits or upper with lower case.
    """
    if token in _DOCUMENTED_EXAMPLES:
        return True
    if len(tail) >= 4 and len(set(tail)) == 1:
        return True
    if _VENDOR_PLACEHOLDER.search(tail) is None:
        return False
    return not any(
        len(part) >= _VENDOR_SEGMENT_MIN_CHARS
        and len(set(part)) >= 6
        and not part.isdigit()
        and (
            (any(c.isdigit() for c in part) and any(c.isalpha() for c in part))
            or (any(c.isupper() for c in part) and any(c.islower() for c in part))
        )
        for part in _NOT_ALNUM.split(_VENDOR_PLACEHOLDER.sub(" ", tail))
    )


#: A value that is wholly one interpolation slot holding only a NAME:
#: `${user_config.client_secret}`, `${var.anthropic}`. Anything before or after
#: it, or a second slot, is not. Nor is a slot carrying an operator --
#: `${DB_PW:-<literal>}` (or `-`, `=`, `:=`, `:+`, `:?`) expands to the literal
#: it holds -- which the owner's `^\$\{[^}]+\}$` would have let through.
_INTERPOLATION_SLOT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_.]*\}")
#: One lowercase word of ANY length (owner decision, 2026-10-02: the single-word
#: length cap is removed): no `-`, `.`, `_` or digit to join it to another.
#: RESIDUAL RISK, accepted by the owner: an all-lowercase, digitless random
#: token (32 letters carry ~150 bits, but a weak one-word secret carries few)
#: under a non-password name, outside test paths, publishes. Only
#: `_REFERENCE_MAX_CHARS` bounds it. Under a password name it is still refused.
_REFERENCE_WORD = re.compile(r"[a-z]+")
#: The KNOWN shapes of a multi-segment reference (owner decision, 2026-10-02,
#: #470's review), and no other: a tenant secret's name (`swarm-tenant-eng-git`),
#: a name that says it names a credential (`anthropic-api-key`, `github-token`,
#: `db-secret`, `tenant-git`), and an interpolation path under a root that
#: Terraform, a plugin manifest or a module reads (`var.`, `local.`,
#: `user_config.`, `data.`, `module.`). `correct-horse-battery-staple` and
#: `correct.horse.battery.staple` are none of them, and are refused.
#: RESIDUAL RISK, accepted by the owner (2026-10-02): these shapes are UNCAPPED
#: in segments. A passphrase with a known prefix or suffix publishes outside
#: test paths -- `correct-horse-battery-staple-token`, `swarm-tenant-<words>`,
#: a `var.<words>` path -- up to the 64-character bound. No segment cap.
_REFERENCE_SHAPE = re.compile(
    r"swarm-tenant(?:-[a-z]+)+"
    r"|[a-z]+(?:-[a-z]+)*-(?:api-key|token|secret|git)"
    r"|(?:var|local|user_config|data|module)(?:\.[a-z_]+)+"
)
#: The longest value a shape may be: a name, not a passphrase.
_REFERENCE_MAX_CHARS = 64
#: The words an `Authorization` value opens with. Never a reference: it is
#: the scheme, and the credential follows it (#470's review).
_AUTH_SCHEME_WORDS = frozenset(
    {"bearer", "basic", "token", "digest", "negotiate", "oauth", "ssws"}
)


def _is_a_reference(value: str, rule: Any = None, match: re.Match[str] | None = None) -> bool:
    """True when a generic rule's matched `value` NAMES a credential rather
    than holding one, so tier 2 does not refuse it (owner decisions,
    2026-10-02).

    WHY. Three of six SwarmCloud lanes were refused at publish for text that
    held nothing: plugin.json's unchanged `"..._CLIENT_SECRET":
    "${user_config.oauth_client_secret}"`, re-added by a trailing comma, a
    Terraform local naming the Anthropic key with the provider id
    `anthropic`, and a tenant secret's name `swarm-tenant-eng-git`. The
    relaxation is narrow, for the generic rules outside test paths only:

    * (i) the whole value is ONE `${...}` slot holding only a name
      (`_INTERPOLATION_SLOT`). A slot with anything glued to it
      (`${a}hunter2`), or with a shell default inside it (`${A:-hunter2}`),
      is still refused: the glued part or the default may be the credential.
    * (ii) otherwise, the value is a single lowercase word of any length
      (`_REFERENCE_WORD`) or one of the
      KNOWN reference shapes (`_REFERENCE_SHAPE`), holds no digit, and
      `_looks_like_a_credential` rejects it (with no digit it always does;
      it is asked so a change to it cannot make this rule pass a value it
      accepts). Rule (ii) never applies:
      - after an `Authorization` header's scheme (`http_authorization_scheme`):
        what follows the scheme is the credential, whatever its case;
      - to a scheme word itself (`bearer`, `Basic`, `token`): it names nothing;
      - under a name ending `password`/`passwd` (the key/value rule's `pw`
        group): that name is assigned the password, not a provider id or a
        secret's name, so a password name assigned the one quoted word
        `letmein` is refused. No dictionary.

    Only quotes, their escaping backslashes, whitespace and a trailing `;` or
    `,` are stripped, never a bracket, so a slot's closing brace is kept.
    """
    candidate = value.strip("\"'`\\ \t").rstrip(";,").strip("\"'`\\")
    if _INTERPOLATION_SLOT.fullmatch(candidate):
        return True
    if rule is not None and rule.name == "http_authorization_scheme":
        return False
    if candidate.lower() in _AUTH_SCHEME_WORDS:
        return False
    if (
        rule is CREDENTIAL_KEY_VALUE
        and match is not None
        and match.re is rule.pattern
        and match.group("pw")
    ):
        return False
    if len(candidate) > _REFERENCE_MAX_CHARS or any(c.isdigit() for c in candidate):
        return False
    shaped = (
        _REFERENCE_WORD.fullmatch(candidate) is not None
        or _REFERENCE_SHAPE.fullmatch(candidate) is not None
    )
    return shaped and not _looks_like_a_credential(candidate)


#: An identifier's shape: optional leading underscores, then letters and
#: digits joined by single underscores (snake_case, camelCase, PascalCase).
_IDENTIFIER_SHAPE = re.compile(r"_*[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")
#: One segment of an identifier: a capitalised or lowercase word, a run of
#: capitals not followed by a lowercase letter (`API`, `KEY`), or a digit run.
_IDENTIFIER_SEGMENT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
#: The longest digit run an identifier may hold: a version or an index
#: (`v2`, `01`, `2024`), not a number long enough to be a secret's body.
_IDENTIFIER_DIGITS_MAX = 4
_VOWELS = frozenset("aeiouyAEIOUY")


def _is_an_identifier(value: str) -> bool:
    """True when an UNQUOTED bare `value` is a code identifier holding a digit
    run, not a credential (SCAN-IDENT, #2030; the owner's decision of 2026-10-23,
    on SCAN-FP's question): `sessionToken2`, `api_key_v2`, `MockUser01`,
    `token2fa`. `_assigns_a_literal` asks it only for a bare value; a quoted
    value is a literal whatever its shape, and this is never asked of one.

    The value is split at `_` and at case changes into segments. It is an
    identifier when every letter segment has at least 2 letters and a vowel
    (`y` counts) -- word-like, not `Xq`, `Zv` -- except that one letter may
    stand right before the digit run (`v2`, `V2`); it holds at most ONE digit
    run, of at most `_IDENTIFIER_DIGITS_MAX` digits, after the first segment;
    it does not open with a vendor key's prefix (`_VENDOR_PREFIX`); and it is
    not at least `CREDENTIAL_MIN_CHARS` long with at least
    `CREDENTIAL_MIN_ENTROPY_BITS` of entropy once its digits are removed.
    So `aB3xQ9` (three digit runs), a five-digit run, and a long run of
    distinct letters are still refused.

    RESIDUAL RISK, accepted by the owner on 2026-10-23: a short real value shaped
    like words plus digits (`hunterHorse42`), assigned bare to a credential
    name in YAML or in an environment file, publishes. There is no dictionary;
    "word-like" is the vowel and length test above. CI's trivy secret scan
    still runs after the push. Under a password name `_assigns_a_literal`
    does not ask this at all: a human picks `myDog2024` as a password.
    """
    if len(value) > _REFERENCE_MAX_CHARS or _IDENTIFIER_SHAPE.fullmatch(value) is None:
        return False
    if _VENDOR_PREFIX.match(value.lstrip("_")) is not None:
        return False
    segments = [s for part in value.split("_") for s in _IDENTIFIER_SEGMENT.findall(part)]
    runs = [i for i, segment in enumerate(segments) if segment.isdigit()]
    if len(runs) > 1 or runs == [0]:
        return False
    if runs and len(segments[runs[0]]) > _IDENTIFIER_DIGITS_MAX:
        return False
    for i, segment in enumerate(segments):
        if segment.isdigit() or (len(segment) == 1 and runs and i == runs[0] - 1):
            continue
        if len(segment) < 2 or not _VOWELS.intersection(segment):
            return False
    letters = "".join(c for c in value if not c.isdigit())
    return not (
        len(value) >= CREDENTIAL_MIN_CHARS
        and _shannon_bits(letters) >= CREDENTIAL_MIN_ENTROPY_BITS
    )


#: What may follow an unquoted reference on its line: nothing, or a closing
#: bracket, separator or (escaped) quote that ends an enclosing string.
_VALUE_END = re.compile(r"[ \t]*(?:$|[;,)\]}]|\\*[\"'`])")


def _odd_backslashes(text: str) -> bool:
    """True when `text` ends in an odd run of backslashes, i.e. a quote right
    after it is escaped."""
    return (len(text) - len(text.rstrip("\\"))) % 2 == 1


def _value_split(match: re.Match[str]) -> tuple[str, str]:
    """A generic match's group 1 and the value after it, as these checks read them.

    The key/value rule's group 1 now ends with a single quote that opens the
    value (`sq`, the owner's pick 'single-quotes-too' on PR #3775), so its
    value runs across blanks to the closing `'`. Every check here reads a
    single-quoted value as one that STARTS with its quote, as the rule's
    value class served it before, so the quote is handed back to the value.
    """
    prefix = match.group(1)
    if "sq" in match.re.groupindex and match.group("sq"):
        prefix = prefix[:-1]
    return prefix, match.group(0)[len(prefix):]


def _ends_the_value(match: re.Match[str]) -> bool:
    """True when a generic match's value is the WHOLE value, not its first word.

    The generic rules' value class stops at whitespace, a comma or a quote, so
    a password name assigned a quoted four-word passphrase matches only its
    first word, a digitless lowercase word `_is_a_reference` would pass. The owner's rule
    (ii) asks the whole value to be one identifier, so a reference counts only
    when it ends the value: a quoted value must close right after it (the
    rule's opening `"`, escaped or not, or a `'`/`` ` `` the value opens with),
    and an unquoted one must end the line or meet a separator, a closing
    bracket or the quote of an enclosing string.
    """
    prefix, value = _value_split(match)
    rest = match.string[match.end():].split("\n", 1)[0]
    opened = prefix.rstrip()
    if opened.endswith('"'):
        # The close must be escaped EXACTLY as the open was: in `"letmein\"
        # <secret>"` the `\"` is part of the value, not its end (#470's review).
        escapes = len(opened[:-1]) - len(opened[:-1].rstrip("\\"))
        close = re.match(r'(\\*)"', rest)
        # The value class swallows a backslash right before the close, so the
        # backslashes the value ends in count with the close's own: `NAME =
        # "x\" <rest>"` reads as the value `x\` and a bare `"`.
        trailing = len(value) - len(value.rstrip("\\"))
        if close is None or trailing + len(close.group(1)) != escapes:
            return False
        return not _value_continues(rest[close.end():], '"', escapes)
    if value[:1] in ("'", "`"):
        quote = value[0]
        body = value[1:].rstrip(";,)]}")
        if body.endswith(quote):
            if not (len(body) > 1 and quote not in body[:-1]):
                return False
            if _odd_backslashes(body[:-1]):
                return False  # the quote is escaped, so it does not close
            return not _value_continues(rest, quote, 0)
        if rest.startswith(quote):
            if _odd_backslashes(body):
                return False
            return not _value_continues(rest[1:], quote, 0)
        return False
    return _VALUE_END.match(rest) is not None and not _value_continues(rest, None, 0)


#: An opening quote, with the backslashes that escape it.
_OPEN_QUOTE = re.compile(r"(\\*)([\"'`])")


#: A comment start, which may follow a value only after whitespace.
_COMMENT_START = re.compile(r"(?:#|//|--)")

#: The next key or argument after a `,`: an identifier then `=` (not `==`) or `:`.
_NEXT_KEY = re.compile(r"[A-Za-z_][\w.\-]*[ \t]*(?::|=(?!=))")


def _comment_or_nothing(text: str) -> bool:
    """True when `text` is empty, whitespace, or whitespace then a comment."""
    if not text.strip():
        return True
    return text[:1] in " \t" and _COMMENT_START.match(text.lstrip(" \t")) is not None


def _value_continues(after: str, closed: str | None, escapes: int) -> bool:
    """True unless the text `after` a value's end on its line PROVES the value
    ended there. An allow-list (#470's review: every deny-list of joiners, `+`,
    `,`-literal, adjacency, missed `.`, `&`, `||`, `~`, `|`, a bare token, a
    conditional and `;`). After the close the value is whole only when what
    follows is the end of the line, whitespace, a `;` ending the statement, a
    closing bracket (then `,`, `;` or the line's end), a comment after
    whitespace, or a `,` and then the next key or argument. Anything else is
    read as more of the value, so the generic rule refuses.

    `closed` is the quote the value just closed with (None when it was
    unquoted) and `escapes` how many backslashes escaped it. A quote of
    another kind, or one escaped less, straight after it closes an ENCLOSING
    string (`'"secret": "anthropic"'`, `"{\\"secret\\": \\"anthropic\\"}"`),
    and nothing after that belongs to this value.
    """
    straight = _OPEN_QUOTE.match(after)
    if straight is not None and (
        closed is None or straight.group(2) != closed or len(straight.group(1)) < escapes
    ):
        return False
    rest = after.lstrip(" \t")
    if not rest:
        return False
    if len(rest) != len(after) and _COMMENT_START.match(rest):
        return False
    while rest[:1] in (")", "]", "}") and rest:
        rest = rest[1:]
        if not rest:
            return False
        if rest[:1] in " \t":
            return not _comment_or_nothing(rest)
        if _OPEN_QUOTE.match(rest):
            return False  # the bracket ends a literal inside an enclosing string
    if rest[:1] == ";":
        return not _comment_or_nothing(rest[1:])
    if rest[:1] != ",":
        return True
    following = rest[1:]
    if _comment_or_nothing(following):
        return False
    following = following.lstrip(" \t")
    if _NEXT_KEY.match(following):
        return False
    literal = _OPEN_QUOTE.match(following)
    if literal is None:
        return True
    end = following.find(literal.group(2), literal.end())
    if end < 0:
        return True
    return not following[end + 1:].lstrip(" \t").startswith(":")


# -- what the GENERIC rules do not read (SCAN-FP, #2030) ----------------------
#
# The owner's decision, 2026-10-23: fix the false positives and the advice, add no
# setting. A peer application's task was refused three attempts in a row over a
# doc comment that quoted its fixture's name in prose (the reference was
# followed by more words, so `_ends_the_value` read it as the first word of a
# passphrase) and over a template literal whose one slot calls `mockUuid()`, a
# value built at runtime (the value class stops at `(`, so no reference shape
# could ever match it). Two things are therefore not read by the GENERIC rules
# -- `key_value_assignment` and the `Authorization` rules -- in either tier.
# The vendor rules, the JWT rule and the private-key rules read everything,
# comments included: a vendor key is one by its own format, wherever it sits.
#
# RESIDUAL RISK, accepted with the decision: a generic-shaped credential
# written into a comment (an assignment behind a `#`, a commented-out line of
# an `.env`) publishes; CI's trivy secret scan still reads it. And on a
# single line longer than a scan window the window may start mid-line
# (`_DiffLeakScanner`), where text that opens like a comment is read as one.

#: A line that opens a comment: its first non-space characters are `//`, `#`,
#: `--` then a space or the line's end, or a block opener (`/*`, a block's
#: continuation `*`, `<!--`). NOT `#name =`: that is a JavaScript private
#: field, code. NOT `--name=`: a command-line flag on a continuation line.
_COMMENT_LINE = re.compile(
    r"[ \t]*(?:(?P<line>//|#(?![A-Za-z_$][\w$]*[ \t]*=(?!=))|--(?=[ \t\r]|$))"
    r"|(?P<block>/\*|<!--|\*))"
)


def _on_a_comment_line(text: str, at: int) -> bool:
    """True when the line of `text` holding offset `at` is a comment line
    (`_COMMENT_LINE`). A block comment closed on the line with code after the
    close (a `/* note */` and then an assignment) is not one: the code is read."""
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    line = text[start : len(text) if end < 0 else end]
    opened = _COMMENT_LINE.match(line)
    if opened is None:
        return False
    if opened.group("line"):
        return True
    block = opened.group("block")
    close, search_from = (
        ("-->", opened.end()) if block == "<!--"
        else ("*/", opened.start("block") if block == "*" else opened.end())
    )
    closed_at = line.find(close, search_from)
    return closed_at < 0 or not line[closed_at + len(close) :].strip()


def _quoted_end(text: str, at: int) -> int | None:
    """Where the string opening with the quote at `text[at]` closes on its
    line (backslash escapes honoured), or None when it does not."""
    quote, i = text[at], at + 1
    while i < len(text) and text[i] != "\n":
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return None


def _slot_end(text: str, at: int) -> tuple[int, int] | None:
    """For a `${` slot whose body starts at `at`: where its closing `}` is and
    how many characters of string literal its body holds, or None when it does
    not close on its line. Braces inside a string in the body do not count."""
    depth, literal, i = 1, 0, at
    while i < len(text) and text[i] != "\n":
        char = text[i]
        if char in "\"'`":
            close = _quoted_end(text, i)
            if close is None:
                return None
            literal, i = literal + close - i - 1, close + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i, literal
        i += 1
    return None


def _runtime_template(text: str, at: int) -> bool:
    """True when the backtick at `text[at]` opens a template literal that is a
    RUNTIME EXPRESSION, not a credential (#2030): it closes on its line, holds
    at least one `${...}` slot, every slot holds an expression (any, not only
    a name, as `_INTERPOLATION_SLOT` reads a whole value), and its LITERAL text
    -- what stands outside the slots, plus any string literal inside one --
    totals fewer than `CREDENTIAL_MIN_CHARS`. A slot does not hide what stands
    beside it (`_is_explicit_placeholder` reasons the same way): `sk-${x}`
    followed by 40 random characters, `${"<40 random>"}`, or 24 characters cut
    in two by slots are all still read by the rule. Nor may more value be
    joined after the close (`_value_continues`): `` `${x}` + "<literal>" ``.
    """
    literal, slots, i = 0, 0, at + 1
    while i < len(text):
        char = text[i]
        if char == "\n":
            return False
        if char == "`":
            break
        if char == "\\":
            literal, i = literal + 2, i + 2
            continue
        if text.startswith("${", i):
            found = _slot_end(text, i + 2)
            if found is None or not text[i + 2 : found[0]].strip():
                return False
            slots, literal, i = slots + 1, literal + found[1], found[0] + 1
            continue
        literal, i = literal + 1, i + 1
    else:
        return False
    if slots == 0 or literal >= CREDENTIAL_MIN_CHARS:
        return False
    return not _value_continues(text[i + 1 :].split("\n", 1)[0], "`", 0)


def _match_counts(rule: Any, match: re.Match[str], in_tests: bool) -> bool:
    """Whether one token-starting match of `rule` is a credential, by tier."""
    if rule.name == "jwt":
        return _decodes_as_a_jwt(match.group(0))
    if rule.name in GENERIC_CREDENTIAL_RULES:
        if _on_a_comment_line(match.string, match.start()):
            return False
        if rule is CREDENTIAL_KEY_VALUE and not _assigns_a_literal(match):
            return False
        prefix, value = _value_split(match)
        value_at = match.start() + len(prefix)
        if value.startswith("`") and _runtime_template(match.string, value_at):
            return False
        if not in_tests:  # tier 2
            return not (_is_a_reference(value, rule, match) and _ends_the_value(match))
        return _looks_like_a_credential(value.strip("\"'`;,)]}\\ \t"))
    # A vendor key: refused outside tests as before; in a test path it passes
    # ONLY with an explicit placeholder (owner decision, 2026-10-01). Entropy
    # and digits cannot tell a fixture from a real key: 22% of real-shaped
    # AKIA tokens passed that judgement.
    if not in_tests:
        return True
    prefix = _VENDOR_PREFIX.match(match.group(0))
    tail = match.group(0)[prefix.end():] if prefix is not None else match.group(0)
    return not _is_explicit_placeholder(tail, match.group(0))


# -- a version is never a credential value, in a package manifest -------------
#
# PUBLISH SCAN, MERGED-MAIN FLOOR (reported 2026-11-10). `key_value_assignment`
# takes any key holding `token`, `secret` or `key`, so a dependency named for
# one -- `"tiktoken": "1.0.22"` in a package.json -- refused a lane in the
# strict tier: the value is a literal and not a reference. In the npm family's
# manifests and lockfiles (`PACKAGE_MANIFEST_FILES`; the rule special-cases no
# other ecosystem's) such a key names a package, and a value that is WHOLLY a
# version specifier is not a credential, whatever the key says.
#
# What a version specifier is, judged on the whole value (to its closing quote,
# or the line's end unquoted), never on the run the rule matched:
#
# * a range: comparators (`<`, `<=`, `>`, `>=`, `=`, `~`, `~>`, `^`) on a version
#   `v?N[.N[.N]]` (each part digits, `x`, `X` or `*`), joined by spaces, `||` or
#   ` - `; or `*`, `x` or `X` alone;
# * `workspace:` then `*`, `^`, `~` or a range;
# * an npm alias, `npm:[@scope/]name[@range]`;
# * a git dependency pinned to a version: `github:`, `gitlab:`, `bitbucket:`,
#   `git://`, `git+https://` or `git+ssh://` then a path with no user, password
#   or port in it (no `@`, no `:`), then `#semver:<range>` or `#<version>`.
#
# WHAT IT CANNOT LET THROUGH. A pre-release or build tag (`-beta.2`, `+exp`) is
# made of identifiers each digits (at most 20, a date), letters (at most 20, a
# word) or at most 12 of letters and digits (a short commit hash), and at most
# two of those last per version; a name or path part is lowercase letters, at
# most 4 digits after them, and none `_looks_like_a_credential`. So no run of
# random characters long enough to be a token fits. Vendor-shaped keys, private
# keys and JWTs are other rules and are still refused here.
#
# RESIDUAL RISK: an agent set on smuggling a value can always spell it in
# short pieces; this, like the rest of the guard, stops a key put there by
# mistake or habit.

#: The file names (the last path segment, exactly) a version is not a
#: credential value in: the npm family's manifest and lockfiles.
PACKAGE_MANIFEST_FILES = frozenset(
    {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
)

_VERSION_PART = r"(?:[0-9]{1,10}|[xX*])"
_VERSION_TAG = r"[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*"
_VERSION = re.compile(
    r"v?" + _VERSION_PART + r"(?:\." + _VERSION_PART + r"){0,2}"
    r"(?P<pre>-" + _VERSION_TAG + r")?(?P<build>\+" + _VERSION_TAG + r")?"
)
_COMPARATOR = re.compile(r"(?:<=|>=|<|>|=|~>|~|\^)?[ \t]*")
_RANGE_JOIN = re.compile(r"[ \t]*\|\|[ \t]*|[ \t]+-[ \t]+|[ \t]+")
_TAG_DIGITS = re.compile(r"[0-9]{1,20}")
_TAG_WORD = re.compile(r"[A-Za-z]{1,20}")
_TAG_MIXED = re.compile(r"[0-9A-Za-z]{1,12}")
#: How many letter-and-digit tag identifiers (a short commit hash) one version holds.
_TAG_MIXED_MAX = 2
_NAME_PART = re.compile(r"[a-z]{1,32}[0-9]{0,4}|[0-9]{1,4}")
_NAME_SPLIT = re.compile(r"[-._/]")
_NPM_ALIAS = re.compile(r"npm:(?P<name>(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)(?:@(?P<range>.+))?")
_GIT_ALIAS = re.compile(
    r"(?:github:|gitlab:|bitbucket:|git://|git\+https://|git\+ssh://)"
    r"(?P<path>[A-Za-z0-9._/-]+)#(?:semver:)?(?P<range>.+)"
)


def _a_tame_tag(tag: str | None) -> bool:
    """Whether a version's pre-release or build tag (with its `-` or `+`) is
    made of identifiers too short to hide a token in."""
    if not tag:
        return True
    mixed = 0
    for part in re.split(r"[.-]", tag[1:]):
        if _TAG_DIGITS.fullmatch(part) or _TAG_WORD.fullmatch(part):
            continue
        if not _TAG_MIXED.fullmatch(part):
            return False
        mixed += 1
    return mixed <= _TAG_MIXED_MAX


def _a_version_range(text: str) -> bool:
    """Whether `text` is wholly an npm version range (see above)."""
    text = text.strip()
    if text in ("*", "x", "X"):
        return True
    if not text or len(text) > 256:
        return False
    for piece in _RANGE_JOIN.split(text):
        comparator = _COMPARATOR.match(piece)
        rest = piece[comparator.end():] if comparator else piece
        version = _VERSION.fullmatch(rest)
        if version is None:
            return False
        if not (_a_tame_tag(version.group("pre")) and _a_tame_tag(version.group("build"))):
            return False
    return True


def _a_tame_name(name: str) -> bool:
    """Whether a package name or a git path is made of word-like parts."""
    parts = [part for part in _NAME_SPLIT.split(name.lstrip("@")) if part]
    return bool(parts) and all(
        _NAME_PART.fullmatch(part) and not _looks_like_a_credential(part) for part in parts
    )


def _is_a_version_specifier(value: str) -> bool:
    """Whether `value` -- a package manifest's whole value -- is a version
    specifier: a range, a `workspace:` range, or an npm or git alias of one."""
    if value.startswith("workspace:"):
        rest = value[len("workspace:"):]
        return rest in ("*", "^", "~") or _a_version_range(rest)
    alias = _NPM_ALIAS.fullmatch(value)
    if alias is not None:
        return _a_tame_name(alias["name"]) and (
            alias["range"] is None or _a_version_range(alias["range"])
        )
    alias = _GIT_ALIAS.fullmatch(value)
    if alias is not None:
        return _a_tame_name(alias["path"]) and _a_version_range(alias["range"])
    return _a_version_range(value)


def _in_a_package_manifest(path: str) -> bool:
    return path.replace("\\", "/").rsplit("/", 1)[-1] in PACKAGE_MANIFEST_FILES


def _assigns_a_version(match: re.Match[str]) -> bool:
    """Whether a `key_value_assignment` match's WHOLE value is a version
    specifier: read from where the value starts to its closing quote on the
    line, or to the line's end (or a ` #` comment) unquoted."""
    text = match.string
    at = match.start() + len(match.group(1))
    line_end = text.find("\n", at)
    line = text[at:] if line_end < 0 else text[at:line_end]
    line = line.rstrip("\r")
    quote = text[at - 1] if at and text[at - 1] in "\"'" else ""
    if not quote and line[:1] in ("'", '"'):
        quote, line = line[0], line[1:]
    if quote:
        close = line.find(quote)
        if close < 0:
            return False
        value, after = line[:close], line[close + 1:]
        # What follows is the line's end, or what ends a JSON or YAML value;
        # a key after it is a match of its own, judged on its own.
        if after.strip() and after.lstrip()[0] not in ",}:":
            return False
    else:
        value = line.split(" #", 1)[0].strip()
    return "\\" not in value and _is_a_version_specifier(value)


def credential_in(path: str, added: str) -> CredentialHit | None:
    """The first credential in `added` -- text the file at `path` adds, its
    `+` removed -- tiered by whether `path` is a test path (#373), or None.

    The patterns are `swarm_redaction.RULES`, the same families swarm-api
    masks at read time (owner decision 4, 2026-09-28), so "credential-shaped"
    means one thing on the platform.

    A MATCH MUST START A TOKEN. swarm-api masks a run wherever it starts,
    because there a false positive costs one masked word. Here it costs the
    agent's whole history (a fold), and code is full of identifiers that hold
    a family's prefix in the middle: `keyword_only` holds `eyword_only`, which
    the JWT rule takes for a token. So a pattern match counts only when the
    character before it is not a letter, a digit or `_`. The private-key
    block is exempt: its marker is never part of an identifier. Rules are
    asked in `swarm_redaction.RULES` order, and the answer names the rule and
    where its match starts, never the value.
    """
    if not added:
        return None
    in_tests = is_test_path(path)
    in_manifest = _in_a_package_manifest(path)
    for rule in CREDENTIAL_RULES:
        if rule.apply is not None:  # the private-key block
            # Main's rule, in every path and tier: refused exactly when
            # `mask_private_keys` would mask something (owner, 2026-10-01).
            if rule.apply(added, False, False)[1]:
                marker = _PEM_MARKER.search(added)
                return CredentialHit(rule.name, marker.start() if marker else 0)
            continue
        for match in rule.pattern.finditer(added):
            start = match.start()
            if start and (added[start - 1].isalnum() or added[start - 1] == "_"):
                continue
            if _match_counts(rule, match, in_tests):
                if in_manifest and rule is CREDENTIAL_KEY_VALUE and _assigns_a_version(match):
                    continue
                return CredentialHit(rule.name, start)
    at = _headerless_key_at(added)
    if at is not None:
        return CredentialHit(HEADERLESS_KEY_RULE, at)
    return None


# -- key material with no marker in the added text (#361 box 80) --------------
#
# SECURITY PASS, 2026-10-09 (owner decision: the pass is un-deferred). The
# private-key rule is main's `mask_private_keys`, which needs a BEGIN or END
# marker in the text it is given. Traditional encrypted PEM ciphertext held in
# a variable, with its `Proc-Type`/`DEK-Info` headers and markers built
# somewhere else (another file, an unchanged line, a runtime join), carries no
# marker, and was published. So the guard also refuses a free base64 blob
# that IS key material on its own terms, in every path and tier:
#
# * ciphertext: at least `CIPHERTEXT_MIN_BYTES` decoded, a whole number of
#   8-byte cipher blocks (DES, 3DES and AES-CBC, every cipher a traditional
#   PEM names), random on both readings -- the base64 text at least
#   `CIPHERTEXT_MIN_CHAR_BITS` per character, which no word, path or
#   identifier reaches, and the bytes as varied as random bytes are -- and not
#   a file format's magic (an image, an archive, a font);
# * a plaintext DER private key: one DER SEQUENCE spanning the blob that opens
#   with a key's version INTEGER (PKCS#1, SEC1, PKCS#8) or an encrypted
#   PKCS#8's PBES algorithm. A certificate opens otherwise, and passes.
#
# A blob is the base64 runs a wrapped or concatenated literal is made of --
# joined across whitespace, quotes, `+` and escaped newlines, never across a
# `,` or `:` that separates two values -- and ends at its `=` padding.
#
# RESIDUAL RISK: a blob under 128 bytes, one cut by anything but those
# joiners, base64url or any other encoding, and ciphertext deliberately padded
# off the block size all pass. The guard stops a key committed by mistake or
# by habit; an agent set on smuggling bytes past a pattern can always encode
# them past it, and CI's secret scan still runs after the push.

#: The rule name a refusal gives for headerless key material.
HEADERLESS_KEY_RULE = "headerless_private_key"
#: The smallest encrypted key: an EC P-256 key's DER is 121 bytes, padded to 128.
CIPHERTEXT_MIN_BYTES = 128
#: Uniform base64 over 172 characters measures about 5.7 bits a character;
#: prose, identifiers and paths stay well under 5.
CIPHERTEXT_MIN_CHAR_BITS = 5.2
#: Leading bytes of formats that are high-entropy and are not keys.
_NOT_KEY_MAGIC = (
    b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"\x1f\x8b", b"PK\x03\x04", b"%PDF", b"wOFF",
    b"wOF2", b"RIFF", b"\x00asm", b"BZh", b"\xfd7zXZ", b"\x28\xb5\x2f\xfd", b"OggS",
    b"ID3", b"fLaC", b"7z\xbc\xaf", b"\x00\x00\x01\x00", b"\x1aE\xdf\xa3",
)
#: The DER that opens a private key's SEQUENCE: version 0 or 1 (PKCS#1, PKCS#8,
#: SEC1), or an encrypted PKCS#8's PBES1/PBES2 (1.2.840.113549.1.5) or PKCS#12
#: PBE (1.2.840.113549.1.12.1) algorithm.
_DER_KEY_VERSION = (b"\x02\x01\x00", b"\x02\x01\x01")
_DER_PBE_OIDS = (b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x05", b"\x06\x0a\x2a\x86\x48\x86\xf7\x0d\x01\x0c\x01")
#: One run of standard base64, at least 16 characters, with its padding.
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}(={1,2})?")
#: What may join two runs of one wrapped or concatenated literal.
_BASE64_JOIN = re.compile(r"(?:[ \t\r\n\"'`+]|\\[nr])*")
#: How far apart two runs of one literal may be.
_BASE64_JOIN_MAX = 64
_HEX_ONLY = re.compile(r"[0-9A-Fa-f]+")


def _der_total(data: bytes) -> tuple[int, int] | None:
    """(header length, total length) of the DER SEQUENCE `data` opens, or None."""
    if len(data) < 2 or data[0] != 0x30:
        return None
    first = data[1]
    if first < 0x80:
        return 2, 2 + first
    width = first - 0x80
    if width == 0 or width > 3 or len(data) < 2 + width:
        return None
    return 2 + width, 2 + width + int.from_bytes(data[2 : 2 + width], "big")


def _is_key_material(blob: str) -> bool:
    """Whether one joined base64 blob is a private key or its ciphertext (box 80)."""
    if len(blob) < (CIPHERTEXT_MIN_BYTES * 4 + 2) // 3:
        return False
    if len(blob.rstrip("=")) % 4 == 1 or _HEX_ONLY.fullmatch(blob):
        return False
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4))
    except ValueError:
        return False
    der = _der_total(raw)
    if der is not None and der[1] == len(raw):
        inner = raw[der[0] :]
        if inner.startswith(_DER_KEY_VERSION):
            return True
        nested = _der_total(inner)
        return nested is not None and inner[nested[0] :].startswith(_DER_PBE_OIDS)
    if len(raw) < CIPHERTEXT_MIN_BYTES or len(raw) % 8 or raw.startswith(_NOT_KEY_MAGIC):
        return False
    if not (any(c.isupper() for c in blob) and any(c.islower() for c in blob)
            and any(c.isdigit() for c in blob)):
        return False
    if _shannon_bits(blob.rstrip("=")) < CIPHERTEXT_MIN_CHAR_BITS:
        return False
    # As varied as random bytes: half the distinct values a uniform draw of
    # this many bytes is expected to show, and no more printable ASCII than
    # three in five (uniform bytes are 37% printable).
    expected = 256 * (1 - math.exp(-len(raw) / 256))
    printable = sum(32 <= b < 127 for b in raw)
    return len(set(raw)) >= expected / 2 and printable * 5 < len(raw) * 3


def _headerless_key_at(added: str) -> int | None:
    """Where the first headerless key blob in `added` starts, or None (box 80)."""
    start = end = -1
    pieces: list[str] = []
    for run in _BASE64_RUN.finditer(added):
        joined = (
            pieces
            and run.start() - end <= _BASE64_JOIN_MAX
            and not pieces[-1].endswith("=")
            and _BASE64_JOIN.fullmatch(added, end, run.start()) is not None
        )
        if not joined:
            if pieces and _is_key_material("".join(pieces)):
                return start
            start, pieces = run.start(), []
        pieces.append(run.group())
        end = run.end()
    if pieces and _is_key_material("".join(pieces)):
        return start
    return None


#: A bare value shaped like a credential: twelve or more token characters,
#: with at least one letter and one digit. `get_token()`, `str`, `self.token`
#: and `DEFAULT_TOKEN` are not; `a1b2c3d4e5f6g7h8` is.
_BARE_CREDENTIAL_RE = re.compile(r"(?=[^\s]*[A-Za-z])(?=[^\s]*[0-9])[A-Za-z0-9_\-+/=~]{12,}")


def _assigns_a_literal(match: re.Match[str]) -> bool:
    """True when a `KEY_VALUE` match assigns a LITERAL to the credential's name.

    OWNER DECISION, 2026-09-28 (#259): in a commit's code, `token =
    get_token()` and `password: str` name a credential without holding one,
    and folding a history for them would fold nearly every history that
    touches authentication. So the key/value rule counts only for a quoted
    string -- the rule's group 1 ends with the opening quote, or the value
    opens with a single quote the rule's value class takes as a character --
    or for a bare value shaped like a credential (`_BARE_CREDENTIAL_RE`) that
    is not a code identifier (`_is_an_identifier`; the owner, 2026-10-23, #2030),
    or that sits under a password name, where an identifier-shaped value is a
    plausible password. The specific families (AWS keys, private keys, JWTs, provider tokens) and the
    registered literals count wherever they appear.
    """
    prefix, value = _value_split(match)
    # A backtick opens a literal too (`_ends_the_value` already reads one as a
    # quoted value); without it `` secret = `x\` <token>` `` was never counted.
    if prefix.rstrip().endswith('"') or value.startswith(("'", "`")):
        return True
    bare = value.rstrip(";)}]'")
    if _BARE_CREDENTIAL_RE.fullmatch(bare) is None:
        return False
    if "pw" in match.re.groupindex and match.group("pw"):
        return True
    return not _is_an_identifier(bare)


def is_credential(text: str) -> bool:
    """True when `credential_in` finds a credential in `text` at its STRICTEST
    tier: the path it is given is not a test path, so a placeholder only a
    test path would excuse counts too. The publish gate tiers by path because
    a test fixture must be committable; nothing a scan of outgoing text reads
    needs to be sent, so it never asks for the loose tier."""
    return credential_in("", text) is not None
