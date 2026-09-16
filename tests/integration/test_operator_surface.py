"""Two properties of the operator-facing surface that only CI can hold.

Both are things a person reads and copies, so a regression is invisible in a diff
review and expensive afterwards.

1. No document teaches an ID token in argv. docs/security.md forbids it in prose;
   this is the check that keeps the rest of the documentation from teaching the
   opposite, which is exactly what had happened -- README.md's "submitting work"
   snippet and seven docs all showed the one-liner.

2. `terraform fmt -check` covers tests/terraform. `terraform fmt` takes a single
   directory, so naming only `terraform` left the test suite `make test` depends
   on as the one Terraform in the tree with unguarded formatting.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]

FENCE = re.compile(r"^\s*```")


def _fenced_lines(text: str):
    """Yield (line number, line) for every line inside a fenced code block."""
    inside = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            inside = not inside
            continue
        if inside:
            yield number, line


def test_no_document_teaches_an_id_token_in_argv() -> None:
    """A token in argv is readable by every process on the box, and by shell history.

    The ID token is the only input from which the API derives tenant identity, so
    whoever captures one is that tenant for the next hour: its provider keys, its
    GCS prefix, its budget. `/proc/<pid>/cmdline` is world-readable on Linux, which
    is the same objection that stops create-secrets.sh taking a key as an argument.
    Prose may name the anti-pattern (docs/security.md does, in order to forbid it);
    a runnable snippet may not, because a snippet is what gets pasted.
    """
    offenders = []
    for doc in DOCS:
        if not doc.exists():
            continue
        for number, line in _fenced_lines(doc.read_text()):
            if "print-identity-token" in line or re.search(
                r"Authorization:\s*Bearer\s+\$", line
            ):
                offenders.append(f"{doc.relative_to(REPO)}:{number}: {line.strip()}")

    assert not offenders, (
        "a runnable snippet puts an ID token in argv. Use ./scripts/api.sh, which "
        "builds the header with a shell builtin and passes it to `curl -K -` on "
        "stdin:\n  " + "\n  ".join(offenders)
    )


def test_terraform_fmt_check_covers_the_terraform_test_suite() -> None:
    """Unchecked formatting in tests/terraform breaks the job that runs the suite.

    `terraform fmt -check` exits 3 on an unformatted file, so a stray tftest file
    fails the fmt step -- but only if that step names the directory. It named only
    `terraform`, while `make test` and the `terraform test` CI job both run
    tests/terraform, so a file could be wired into the gate and outside the linter
    at the same time.
    """
    makefile = (REPO / "Makefile").read_text()
    workflow = (REPO / ".github" / "workflows" / "terraform.yml").read_text()

    for name, text in (("Makefile", makefile), (".github/workflows/terraform.yml", workflow)):
        fmt_checks = [
            line.strip()
            for line in text.splitlines()
            if "fmt" in line and "-check" in line and "terraform" in line
        ]
        assert any("tests/terraform" in line for line in fmt_checks), (
            f"{name} runs `terraform fmt -check` but never over tests/terraform; "
            "fmt takes one directory, so each root needs its own invocation:\n  "
            + "\n  ".join(fmt_checks)
        )
