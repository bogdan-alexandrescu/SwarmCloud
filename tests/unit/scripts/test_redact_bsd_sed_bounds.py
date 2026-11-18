"""Every repetition bound in `redact()`'s sed program is one BSD sed accepts.

THE DEFECT THIS PINS (7 November 2026). BSD regex (macOS sed) refuses a
repetition count over RE_DUP_MAX, which is 255 there: `{0,256}` answers
`RE error: invalid repetition count(s)` and sed prints NOTHING. The filter
fails closed, so no secret leaked, but every laptop script that pipes through
`redact` went silent. GNU sed in CI has no such limit, so CI never saw it;
this test reads the expanded program on Linux and holds every bound to 255.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: RE_DUP_MAX in macOS's <limits.h>; POSIX requires at least 255.
BSD_RE_DUP_MAX = 255


def _expanded_sed_expressions() -> list[str]:
    """The `-e` arguments `redact` hands sed, with every variable expanded."""
    source = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    script = match.group(0) + "sed() { printf '%s\\0' \"$@\"; cat >/dev/null; }\necho x | redact\n"
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    argv = done.stdout.split("\0")
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-e"]


def _repetition_bounds(expression: str) -> list[tuple[int, int | None, str]]:
    """Every `{m}`, `{m,}` and `{m,n}` of an ERE, outside bracket expressions
    and escapes, as (m, n, the text around it)."""
    bounds: list[tuple[int, int | None, str]] = []
    i = 0
    while i < len(expression):
        char = expression[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            # A bracket expression: an optional `^`, a `]` first is literal,
            # and `[:class:]` inside it is one piece.
            j = i + 1
            if j < len(expression) and expression[j] == "^":
                j += 1
            if j < len(expression) and expression[j] == "]":
                j += 1
            while j < len(expression) and expression[j] != "]":
                if expression.startswith("[:", j):
                    end = expression.find(":]", j + 2)
                    j = end + 2 if end != -1 else j + 1
                else:
                    j += 1
            i = j + 1
            continue
        if char == "{":
            bound = re.match(r"\{(\d+)(,(\d*))?\}", expression[i:])
            if bound:
                low = int(bound.group(1))
                high = None if bound.group(2) is None else (int(bound.group(3)) if bound.group(3) else None)
                if bound.group(2) is None:
                    high = low
                bounds.append((low, high, expression[max(0, i - 24) : i + len(bound.group(0))]))
                i += len(bound.group(0))
                continue
        i += 1
    return bounds


def test_the_scanner_finds_bounds_and_skips_brackets_and_escapes():
    found = _repetition_bounds(r"a{2,15}\{9\}[{1,300}]\\{0,256}x{6}y{3,}")
    assert [(low, high) for low, high, _ in found] == [(2, 15), (0, 256), (6, 6), (3, None)]


def test_every_repetition_bound_in_the_shell_filter_fits_bsd_re_dup_max():
    expressions = _expanded_sed_expressions()
    assert len(expressions) >= 15, expressions
    bounds = [b for e in expressions for b in _repetition_bounds(e)]
    assert len(bounds) >= 50, bounds
    over = [
        f"{{{low},{'' if high is None else high}}} in ...{context}"
        for low, high, context in bounds
        if low > BSD_RE_DUP_MAX or (high is not None and high > BSD_RE_DUP_MAX)
    ]
    assert not over, "BSD sed (macOS) refuses a repetition count over 255: " + "; ".join(over)
