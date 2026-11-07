"""The three leaks the attacker-lens review of PR #3448 found, closed in both filters.

the owner chose on 6 November 2026 to fix them now (part of #2688). Main served each
literal whole, in `swarm_redaction` and in `redact` in scripts/lib/common.sh:

1. a literal inside a double-quoted string after a `;` `)` `]` or `}` run:
   `token=${NAME};"<v>"`, `Authorization: Bearer {TOKEN});"<v>` -- the
   key/value mask stopped at the first double quote;
2. text glued after `$(cmd ...)` or `${{ expr }}`: `token = $(cat f);<v>` --
   only the opening `$(` or `${{ ` was read, so the rest was kept;
3. a literal glued to `${NAME}` with no separator, or after a comma or a quote:
   `${NAME}<v>`, `${NAME}"<v>`, `${NAME},<v>`.

What is kept does not widen: a substitution alone is still kept, byte for byte.
Every fake value is built at runtime, so nothing here looks like a credential.
"""

from __future__ import annotations

import re
import secrets
import subprocess
from pathlib import Path

import pytest

from swarm_redaction import MASK, redact

REPO = Path(__file__).resolve().parents[3]


def _literal() -> str:
    return secrets.token_hex(8)


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
    assert len(out) == len(lines), (lines, out)
    return out


#: `{v}` is the literal. One shape per leak the review named, and the
#: neighbours a reader would try next.
LEAKS = {
    # 1. After a ; ) ] } run, inside a double-quoted string.
    "semicolon-double-quote": 'token=${NAME};"{v}"',
    "semicolon-double-quote-unclosed": 'token=${NAME};"{v}',
    "authorization-paren-double-quote": 'Authorization: Bearer {TOKEN});"{v}',
    "authorization-paren-double-quote-closed": 'Authorization: Bearer {TOKEN});"{v}"',
    "escaped-double-quote": 'token=${NAME};\\"{v}\\"',
    # 2. Glued after a closed $(cmd ...) or ${{ expr }}.
    "command-substitution-glued": "token = $(cat f);{v}",
    "command-substitution-glued-no-space": "token=$(cat f){v}",
    "actions-expression-glued": "token = ${{ secrets.T }};{v}",
    "actions-expression-glued-no-space": "token: ${{ secrets.T }}{v}",
    # 3. Glued to ${NAME}: no separator, a quote, a comma.
    "glued-no-separator": "token=${NAME}{v}",
    "glued-after-quote": 'token=${NAME}"{v}',
    "glued-after-comma": "token=${NAME},{v}",
    "authorization-glued-after-comma": "Authorization: Bearer ${NAME},{v}",
    "authorization-glued-after-quote": 'Authorization: Bearer ${NAME}"{v}',
}


@pytest.mark.parametrize("template", LEAKS.values(), ids=LEAKS.keys())
def test_the_api_filter_masks_the_literal(template: str) -> None:
    value = _literal()
    result = redact(template.replace("{v}", value))
    assert value not in result.text, result.text
    assert MASK in result.text


@pytest.mark.parametrize("template", LEAKS.values(), ids=LEAKS.keys())
def test_the_shell_filter_masks_the_literal(template: str) -> None:
    value = _literal()
    (shell,) = _shell_redact([template.replace("{v}", value)])
    assert value not in shell, shell
    assert MASK in shell


#: Kept on main and kept now: a whole, closed construct where the value ends.
KEPT = [
    "token = $(cat f)",
    "token=$(gcloud auth print-access-token)",
    'TOKEN="$(gh auth token)"',
    'export GH_TOKEN="$(gh auth token)"',
    "f(token=$(cat f))",
    # Nested quoting and a line continuation, both kept on main.
    "'TOKEN=\"$(gh auth token)\"',",
    'ACCESS_TOKEN="$(gcloud auth print-access-token \\',
    "token = $(cat f); echo done",
    "token = ${{ secrets.T }}",
    "token: ${{ secrets.GITHUB_TOKEN }}",
    'token: "${{ secrets.GITHUB_TOKEN }}"',
    "token=${NAME}",
    'token="${NAME}"',
    "token=${NAME:-}",
    "token=${NAME}, other=1",
    "f(token=${NAME}),'json'",
    '{"token":"${NAME}","other":"x"}',
    "Authorization: Bearer ${NAME}",
    '{"Authorization":"Bearer ${TOKEN}","Accept":"json"}',
]


@pytest.mark.parametrize("line", KEPT)
def test_a_closed_substitution_alone_is_still_kept_by_both(line: str) -> None:
    assert redact(line).text == line
    assert _shell_redact([line]) == [line]


def test_the_two_filters_give_the_same_line() -> None:
    value = _literal()
    lines = [template.replace("{v}", value) for template in LEAKS.values()] + KEPT
    for line, shell in zip(lines, _shell_redact(lines)):
        assert shell == redact(line).text, line
