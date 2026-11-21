"""`.gitleaks.toml`'s allowlist names test fixtures, and nothing broader.

The unit-test half of the credential leak guard the owner approved on
2026-11-07; the other half is the pull-request workflow step that runs
gitleaks over each pull request's commits with this configuration.

WHY THIS IS A TEST. The allowlist is the one place a real credential can be
waved through without anyone reading it: a path entry for `apps/` or
`scripts/`, a regex such as `.*`, or a stopword hides every finding it covers,
and gitleaks then reports "no leaks found" -- the same output as a clean tree.
So each entry is held here to what it is for:

1. A path entry is anchored (`^`) and names a test-only location: a file under
   `tests/` (the test suites and `tests/fixtures/`). Nothing under `apps/`,
   `scripts/`, `terraform/`, `images/`, `plugin/` or `docs/` is allowlisted
   by path -- not even a test directory inside an app; a fake there is joined
   at run time instead (apps/swarm-ui/src/__tests__/ does so).
2. A path entry matches at least one tracked file. A stale entry is a hole
   kept open for whatever file lands at that path next.
3. No path or regex entry is so broad it matches an arbitrary secret: each
   requires a literal anchor of `MIN_LITERAL` characters, and none matches a
   credential-shaped line built here at run time.
4. The `regexes` and `stopwords` entries, and the fingerprints in
   `.gitleaksignore`, are pinned below, so adding one is a reviewed change to
   this file. Every failure names the offending entry.

WHAT IT CANNOT SEE. Whether gitleaks itself finds anything: no gitleaks binary
runs here (unit tests run offline), and the workflow step does that. Nor the
default rules' own allowlists, which `[extend] useDefault = true` takes from
the pinned gitleaks release.
"""

from __future__ import annotations

import re
import re._parser as sre_parse
import secrets
import subprocess
import tomllib
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / ".gitleaks.toml"
IGNORE = REPO / ".gitleaksignore"

#: Where a path entry may point: the test tree, which holds the suites and
#: tests/fixtures/. The only test-only top-level directory the allowlist names.
TEST_ONLY_PREFIXES = ("tests/",)

#: Never allowlisted by path, whatever the entry says it is for.
FORBIDDEN_PREFIXES = ("apps/", "scripts/", "terraform/", "images/", "plugin/", "docs/")

#: Fewest literal characters an entry must require. `ARG ` alone, or `sk-`,
#: would cover too much; every entry today requires 18 or more.
MIN_LITERAL = 12

#: The allowlist regexes, each matched against a whole line (regexTarget =
#: "line"). Adding one means adding it here, with the reason beside it in
#: .gitleaks.toml.
PINNED_REGEXES = frozenset(
    {
        # The Cloud SDK and auth plugin download's public pool ids and checksums.
        r"\bARG (GCLOUD|GKE_AUTH_PLUGIN)_(POOL_ID|SHA256)=",
        # The live-browser test's password alphabet.
        r'\bPASSWORD_ALPHABET = "',
        # An image reference by tag in the deploy-by-digest test.
        r'manifest\(\*\*\{"swarm-api": f"\{REPO_PATH\}/swarm-api:',
    }
)

#: Stopwords drop any finding whose secret contains them; there are none.
PINNED_STOPWORDS: frozenset[str] = frozenset()

#: `.gitleaksignore`: the console tests' synthetic keys as they stood in the
#: replaced history's root commit, before they were joined at run time.
_ROOT = "9ee05db74fcc48276cd1722ad0208b9ec1dcff8d"
_UI = "apps/swarm-ui/src/__tests__"
PINNED_FINGERPRINTS = frozenset(
    {
        f"{_ROOT}:{_UI}/artifacts.pane.test.tsx:generic-api-key:901",
        f"{_ROOT}:{_UI}/details.followup.test.tsx:generic-api-key:50",
        f"{_ROOT}:{_UI}/details.masked.everywhere.test.tsx:generic-api-key:42",
        f"{_ROOT}:{_UI}/repofixture.ts:generic-api-key:162",
        f"{_ROOT}:{_UI}/workflows.textkey.test.ts:generic-api-key:19",
        f"{_ROOT}:{_UI}/workflows.textkey.test.ts:generic-api-key:20",
    }
)

#: The allowlist keys this test knows how to judge. `commits` or a key a later
#: gitleaks adds would pass unexamined, so an unknown key fails.
KNOWN_ALLOWLIST_KEYS = frozenset({"description", "regexTarget", "paths", "regexes", "stopwords"})


def _config() -> dict:
    with CONFIG.open("rb") as fh:
        return tomllib.load(fh)


def _allowlist() -> dict:
    return _config().get("allowlist", {})


def _entries(key: str) -> list[str]:
    return list(_allowlist().get(key, []))


@cache
def _tracked_files() -> tuple[str, ...]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True,
    ).stdout.decode()
    return tuple(name for name in out.split("\0") if name)


def _required_literal(pattern: str) -> int:
    """How many literal characters every match of `pattern` must contain.

    A literal counts; an alternation counts its shortest branch; a group counts
    its contents; a repeat counts its body times its minimum; anything else
    (classes, `.`, anchors) counts nothing. So `.*` and `\\w{20,}` count 0.
    """

    def walk(items) -> int:
        total = 0
        for op, arg in items:
            name = str(op)
            if name == "LITERAL":
                total += 1
            elif name == "SUBPATTERN":
                total += walk(arg[-1])
            elif name == "BRANCH":
                total += min(walk(branch) for branch in arg[1])
            elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
                low, _high, body = arg
                total += low * walk(body)
        return total

    return walk(sre_parse.parse(pattern))


def _credential_shaped_lines() -> list[str]:
    """Lines a real leak would put in a file, built here so none is a literal."""
    hex40 = secrets.token_hex(20)
    url = secrets.token_urlsafe(30)
    return [
        f'api_key = "{hex40}"',
        f"export GITHUB_TOKEN={'ghp' + '_'}{url}",
        f'"password": "{url}",',
        f"secret: {hex40}",
        f"Authorization: Bearer {url}",
        f"AWS_SECRET_ACCESS_KEY={url}",
        f"const token = '{'sk' + '-'}{url}'",
    ]


@pytest.mark.parametrize("key", sorted(_allowlist()))
def test_the_allowlist_holds_only_keys_this_test_judges(key):
    assert key in KNOWN_ALLOWLIST_KEYS, (
        f".gitleaks.toml [allowlist] has `{key}`, which this test does not judge; "
        "add a check for it here before using it"
    )


def test_no_rule_carries_its_own_allowlist():
    config = _config()
    for key in ("rules", "allowlists"):
        assert key not in config, (
            f".gitleaks.toml declares [[{key}]]: a rule-level allowlist escapes the checks here; "
            "use the global [allowlist] or extend this test"
        )


def test_regexes_are_matched_against_the_line():
    # The broadness checks below judge each regex against a whole line.
    assert _allowlist().get("regexTarget") == "line", (
        f".gitleaks.toml [allowlist] regexTarget is {_allowlist().get('regexTarget')!r}, not 'line'"
    )


@pytest.mark.parametrize("entry", _entries("paths"))
def test_a_path_entry_names_a_test_only_location(entry):
    assert entry.startswith("^"), f"allowlist path {entry!r} is not anchored with ^: it matches anywhere"
    matched = [name for name in _tracked_files() if re.search(entry, name)]
    for name in matched:
        assert not name.startswith(FORBIDDEN_PREFIXES), (
            f"allowlist path {entry!r} covers {name}, under {FORBIDDEN_PREFIXES}; "
            "build that file's fakes at run time instead"
        )
        assert name.startswith(TEST_ONLY_PREFIXES), (
            f"allowlist path {entry!r} covers {name}, which is not under {TEST_ONLY_PREFIXES}"
        )
    # The prefix it names, not only the files there today: an entry for
    # `^apps/` is wrong before any file lands there.
    for prefix in FORBIDDEN_PREFIXES:
        assert not re.match(entry, prefix + "x"), f"allowlist path {entry!r} can cover {prefix}"


@pytest.mark.parametrize("entry", _entries("paths"))
def test_a_path_entry_matches_a_tracked_file(entry):
    matched = [name for name in _tracked_files() if re.search(entry, name)]
    assert matched, f"allowlist path {entry!r} matches no tracked file: remove the stale entry"


@pytest.mark.parametrize("entry", _entries("paths") + _entries("regexes"))
def test_an_entry_requires_a_literal_anchor(entry):
    required = _required_literal(entry)
    assert required >= MIN_LITERAL, (
        f"allowlist entry {entry!r} requires {required} literal characters, fewer than {MIN_LITERAL}: "
        "it is too broad"
    )


@pytest.mark.parametrize("entry", _entries("regexes"))
def test_a_regex_does_not_match_an_arbitrary_secret(entry):
    for line in _credential_shaped_lines():
        assert not re.search(entry, line), (
            f"allowlist regex {entry!r} matches a credential-shaped line; it would hide a real leak"
        )


@pytest.mark.parametrize("entry", _entries("regexes"))
def test_a_regex_matches_a_tracked_line(entry):
    pattern = re.compile(entry)
    for name in _tracked_files():
        if name == ".gitleaks.toml":
            continue
        try:
            text = (REPO / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        if pattern.search(text):
            return
    pytest.fail(f"allowlist regex {entry!r} matches no line of any tracked file: remove the stale entry")


def test_the_regexes_are_the_pinned_set():
    regexes = _entries("regexes")
    assert len(regexes) == len(set(regexes)), f"allowlist regexes repeat an entry: {regexes}"
    for entry in regexes:
        assert entry in PINNED_REGEXES, (
            f"allowlist regex {entry!r} is not in PINNED_REGEXES; adding one is a reviewed change to this test"
        )
    for entry in PINNED_REGEXES - set(regexes):
        assert False, f"PINNED_REGEXES holds {entry!r}, which .gitleaks.toml no longer lists; drop it here"


def test_the_stopwords_are_the_pinned_set():
    for entry in _entries("stopwords"):
        assert entry in PINNED_STOPWORDS, (
            f"allowlist stopword {entry!r} is not in PINNED_STOPWORDS; adding one is a reviewed change to this test"
        )
    for entry in PINNED_STOPWORDS - set(_entries("stopwords")):
        assert False, f"PINNED_STOPWORDS holds {entry!r}, which .gitleaks.toml no longer lists; drop it here"


def test_the_ignored_fingerprints_are_the_pinned_set():
    listed = set()
    if IGNORE.exists():
        for line in IGNORE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                listed.add(line)
    for entry in sorted(listed - PINNED_FINGERPRINTS):
        assert False, (
            f".gitleaksignore lists {entry!r}, not in PINNED_FINGERPRINTS; adding one is a reviewed change to this test"
        )
    for entry in sorted(PINNED_FINGERPRINTS - listed):
        assert False, f"PINNED_FINGERPRINTS holds {entry!r}, which .gitleaksignore no longer lists; drop it here"


def test_the_checks_reject_what_they_exist_to_reject():
    # The measures above, on entries the allowlist must never hold.
    assert _required_literal(".*") == 0
    assert _required_literal(r"(?i)\w{20,}") == 0
    assert _required_literal(r"key|^tests/fixtures/x\.json$") < MIN_LITERAL
    assert any(re.search(r"(?i)api_key", line) for line in _credential_shaped_lines())
