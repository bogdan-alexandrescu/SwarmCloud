"""A secret with spaces inside one quoted value is masked whole, in both filters.

The owner accepted on 6 November 2026 (question 2 of the pull request that
masked a literal glued after `$(...)`, `${{ }}` or `${NAME}`): a value that
opens with a quote runs, across spaces, to the quote that closes it. Both
filters ended every value at whitespace, so `password="correct horse <v>"`
served `horse <v>` from `swarm_redaction` and from `redact` in
scripts/lib/common.sh alike.

Inside a quoted value an escaped quote does not close it; text after the
closing quote is untouched; an unquoted value still ends at whitespace. A value
opened by an escaped quote (`\\"`, a stream-json line) still stops at its next
backslash (#221), and now runs across spaces up to it.

The owner's picks on PR #3775: a single-quoted value runs to its closing quote
the same way ('single-quotes-too'), and a comma inside any quoted value does
not end it ('span-commas'); an unquoted value still ends at a comma.
Every word here is built at runtime, so nothing looks like a credential.
"""

from __future__ import annotations

import re
import secrets
import subprocess
from pathlib import Path

import pytest

from swarm_redaction import MASK, redact

REPO = Path(__file__).resolve().parents[3]


def _word() -> str:
    return "w" + secrets.token_hex(5)


def _shell_redact(lines: list[str]) -> list[str]:
    """`redact()` from scripts/lib/common.sh, lifted out and run by itself."""
    source = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    done = subprocess.run(
        ["bash", "-c", match.group(0) + "redact\n"],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert len(out) == len(lines), "the shell filter changed the number of lines"
    return out


#: (template, expected): `{a}`, `{b}`, `{c}` are words built at runtime; the
#: words named in `kept` must survive, every other one must not.
CASES = {
    "two-words": ('password="{a} {b}"', f'password="{MASK}"', ""),
    "three-words": ('password="{a} {b} {c}"', f'password="{MASK}"', ""),
    "escaped-quote-inside": ('password="{a} \\"{b}\\" {c}"', f'password="{MASK}"', ""),
    "followed-by-another-key": ('password="{a} {b}" next={c}', f'password="{MASK}" next={{c}}', "c"),
    "unquoted-ends-at-whitespace": ("password={a} {b}", f"password={MASK} {{b}}", "b"),
    "json": ('{{"password": "{a} {b}", "next": "{c}"}}', f'{{{{"password": "{MASK}", "next": "{{c}}"}}}}', "c"),
    "secret-key-two-words": ('SECRET_KEY = "{a} {b}"', f'SECRET_KEY = "{MASK}"', ""),
    "token-colon-tab": ('token:\t"{a}\t{b}"', f'token:\t"{MASK}"', ""),
    "plural-two-words": ('secrets: "{a} {b}"', f'secrets: "{MASK}"', ""),
    "leading-space": ('password=" {a} {b}"', f'password="{MASK}"', ""),
    "unclosed-to-the-end": ('password="{a} {b}', f'password="{MASK}', ""),
    "escaped-backslash-then-close": ('password="{a} \\\\" {b}', f'password="{MASK}" {{b}}', "b"),
    "string-prefix": ('token = f"{a} {b}"', f'token = f"{MASK}"', ""),
    "list-element": ('"password": ["{a} {b}", "{c}"]', f'"password": ["{MASK}", "{{c}}"]', "c"),
    "stream-json-escaped": (
        '{{"command": "export DB_PASSWORD=\\"{a} {b}\\" && ./x"}}',
        f'{{{{"command": "export DB_PASSWORD=\\"{MASK}\\" && ./x"}}}}',
        "",
    ),
    # The house style of #260 holds: a scheme word and one already-masked
    # run up to the closing quote keep their two masks; another word does not.
    "authorization-scheme-and-one-run": ('Authorization: "token {a}"', f'Authorization: "{MASK} {MASK}"', ""),
    "authorization-scheme-and-two-words": ('Authorization: "token {a} {b}"', f'Authorization: "{MASK}"', ""),
    "authorization-escaped-scheme-and-two-words": (
        '{{\\"Authorization\\": \\"token {a} {b}\\"}}',
        f'{{{{\\"Authorization\\": \\"{MASK}\\"}}}}',
        "",
    ),
    # A single-quoted value runs to its closing quote the same way (the
    # owner's pick 'single-quotes-too' on PR #3775).
    "single-two-words": ("password='{a} {b}'", f"password='{MASK}'", ""),
    "single-followed-by-another-key": ("password='{a} {b}' next={c}", f"password='{MASK}' next={{c}}", "c"),
    "single-escaped-quote-inside": ("password='{a} \\'{b} {c}'", f"password='{MASK}'", ""),
    "single-double-quote-inside": ("password='{a} \"{b}\" {c}'", f"password='{MASK}'", ""),
    "single-string-prefix": ("token = f'{a} {b}'", f"token = f'{MASK}'", ""),
    "single-plural": ("secrets: '{a} {b}'", f"secrets: '{MASK}'", ""),
    "single-unclosed-to-the-end": ("password='{a} {b}", f"password='{MASK}", ""),
    # A comma inside a quoted value does not end it (the owner's pick
    # 'span-commas' on PR #3775); an unquoted value still ends at one.
    "comma-inside-double": ('password="{a}, {b}" next={c}', f'password="{MASK}" next={{c}}', "c"),
    "comma-glued-inside-double": ('password="{a},{b}"', f'password="{MASK}"', ""),
    "comma-inside-single": ("password='{a}, {b}'", f"password='{MASK}'", ""),
    "comma-first-inside-double": ('password=",{a}"', f'password="{MASK}"', ""),
    "comma-inside-json": (
        '{{"password": "{a}, {b}", "next": "{c}"}}',
        f'{{{{"password": "{MASK}", "next": "{{c}}"}}}}',
        "c",
    ),
    "comma-inside-stream-json-escaped": (
        '{{"command": "export DB_PASSWORD=\\"{a}, {b}\\" && ./x"}}',
        f'{{{{"command": "export DB_PASSWORD=\\"{MASK}\\" && ./x"}}}}',
        "",
    ),
    "comma-ends-unquoted": ("password={a},{b}", f"password={MASK},{{b}}", "b"),
}


def _instance(template: str, expected: str) -> tuple[str, str, dict[str, str]]:
    words = {"a": _word(), "b": _word(), "c": _word()}
    return template.format(**words), expected.format(**words), words


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
def test_both_filters_mask_a_quoted_value_to_its_closing_quote(case: tuple[str, str, str]) -> None:
    template, expected, kept = case
    line, want, words = _instance(template, expected)
    python = redact(line).text
    (shell,) = _shell_redact([line])
    for name, word in words.items():
        if name in kept or "{" + name + "}" not in template:
            continue
        assert word not in python, "the API filter served a word of the quoted value"
        assert word not in shell, "the shell filter printed a word of the quoted value"
    assert python == want, "the API filter's line is not the one expected"
    assert shell == want, "the shell filter's line is not the one expected"


#: Not values: left exactly as they were.
KEPT = [
    'password=""',
    'password=" "',
    '"password": ""',
    'token: "${NAME}"',
    'export GH_TOKEN="$(gh auth token)"',
    'secrets: "not set"',
]


@pytest.mark.parametrize("line", KEPT)
def test_an_empty_or_exempt_quoted_value_is_still_kept_by_both(line: str) -> None:
    assert redact(line).text == line
    assert _shell_redact([line]) == [line]


CRAFTED = {
    "spaces-and-backslashes": ('password="' + ("a \\\\ " * 40_000) + '"', f'password="{MASK}"'),
    "unclosed-blanks": ('password="a' + (" " * 200_000), f'password="{MASK}'),
    "glued-runs": ('password="' + ('a;"b ' * 40_000) + '"', f'password="{MASK}"'),
    "commas-and-glued-runs": ('password="' + ("a;,b, " * 40_000) + '"', f'password="{MASK}"'),
    "single-spaces-and-backslashes": ("password='" + ("a \\\\ " * 40_000) + "'", f"password='{MASK}'"),
    "scheme-and-long-run": (
        'Authorization: "token ' + ("a" * 200_000) + '"',
        f'Authorization: "{MASK} {MASK}"',
    ),
}


@pytest.mark.parametrize("case", CRAFTED.values(), ids=CRAFTED.keys())
def test_a_crafted_quoted_value_is_masked_in_linear_time(case: tuple[str, str]) -> None:
    """Bounded: a crafted value cannot make either filter hang."""
    import time

    line, want = case
    start = time.monotonic()
    python = redact(line).text
    (shell,) = _shell_redact([line])
    assert python == want, "the API filter left part of the value"
    assert shell == want, "the shell filter left part of the value"
    assert time.monotonic() - start < 20
