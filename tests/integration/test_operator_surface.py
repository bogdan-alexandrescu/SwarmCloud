"""Three properties of the operator-facing surface that only CI can hold.

All three are things a person reads, copies or relies on, so a regression is
invisible in a diff review and expensive afterwards.

1. No document teaches an ID token in argv. docs/security.md forbids it in prose;
   this is the check that keeps the rest of the documentation from teaching the
   opposite, which is exactly what had happened -- README.md's "submitting work"
   snippet and seven docs all showed the one-liner.

2. `terraform fmt -check` covers tests/terraform. `terraform fmt` takes a single
   directory, so naming only `terraform` left the test suite `make test` depends
   on as the one Terraform in the tree with unguarded formatting.

3. Every guard self-test `make test` runs is also run by a workflow. `make test`
   is what a person runs before saying they are finished; a workflow is what runs
   when they do not. A guard in only the first is a guard that a pull request can
   break and merge green -- and these particular guards are the ones standing
   between a routine command and another team's production.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = sorted((REPO / "docs").glob("*.md")) + [REPO / "README.md"]
WORKFLOWS = REPO / ".github" / "workflows"

FENCE = re.compile(r"^\s*```")

#: `scripts/lib/plan-guard.sh --self-test`, and the name alone out of it. The
#: Makefile writes `$(SCRIPTS)/lib/...` and the workflows write `./scripts/lib/...`,
#: so the basename is the only spelling both sides share.
SELF_TEST = re.compile(r"([A-Za-z0-9_.-]+\.sh)\s+--self-test")


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


def _make_recipe(makefile: str, target: str) -> str:
    """The recipe lines of one make target.

    Read by TAB rather than by blank line: GNU make defines a recipe as the
    tab-indented lines following the target, and this Makefile's `test` recipe
    contains multi-line `if` blocks with continuations. Anything looser would
    either stop early or swallow the next target.
    """
    recipe: list[str] = []
    collecting = False
    for line in makefile.splitlines():
        if line.startswith(f"{target}:"):
            collecting = True
            continue
        if not collecting:
            continue
        if line.startswith("\t"):
            recipe.append(line)
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        break
    return "\n".join(recipe)


def test_every_guard_self_test_in_make_test_is_also_run_by_a_workflow() -> None:
    """A guard that only `make test` exercises is a guard CI cannot defend.

    Each of these scripts exists because its subject failed silently once: the
    plan guard because a plan touching another team's resources was approved,
    the kubectl guard because a bare `kubectl` reached another team's live
    cluster and returned resources aged 133 days, the auth guard because an
    expired session was reported as an absent resource. They take no arguments,
    reach no network and need no credentials, so there is no cost to running
    them in CI and no reason for the two gates to disagree about which ones
    matter.

    `make test` is the list, because CLAUDE.md names it as the completeness
    check; the workflows are asserted against it rather than the other way
    round, so adding a guard to `make test` alone fails here.
    """
    makefile = (REPO / "Makefile").read_text()
    guards = {Path(name).name for name in SELF_TEST.findall(_make_recipe(makefile, "test"))}

    # A pattern that silently matches nothing is how a gate comes to enforce
    # nothing, so the extraction is asserted before what it extracted is.
    assert guards, (
        "no `--self-test` invocation was found in the `test` target of the "
        "Makefile; either the guards left `make test` or this parser stopped "
        "matching it, and both are worth failing on"
    )

    workflows = {path.name: path.read_text() for path in sorted(WORKFLOWS.glob("*.yml"))}
    assert workflows, f"no workflows found under {WORKFLOWS}"

    missing = sorted(
        guard
        for guard in guards
        if not any(
            re.search(re.escape(guard) + r"\s+--self-test", text)
            for text in workflows.values()
        )
    )

    assert not missing, (
        "`make test` runs these guard self-tests and no workflow does, so a pull "
        "request that breaks one merges green: "
        + ", ".join(missing)
        + ". They are offline and need no credentials; add a step for each to the "
        "`shell` job in application.yml."
    )
