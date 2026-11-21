"""The shared Claude allowlist pre-approves nothing destructive or secret-reading (part of #65).

`.claude/settings.json` is checked in, so its `permissions.allow` list runs
without a prompt in every session and every trusted lane checkout. It used to
approve `terraform apply`/`destroy`, `gcloud run deploy`, `gcloud secrets:*`
(which includes `versions access`, a command that prints a secret) and
`kubectl:*` (which includes `get secret` and `delete`), while CLAUDE.md forbids
anything that can print a secret. These tests hold the list to read-only and
plan-shaped verbs:

* NO ALLOW ENTRY NAMES A DESTRUCTIVE VERB: apply, destroy, deploy, delete.
* NO ALLOW ENTRY READS A SECRET: `gcloud secrets versions access`,
  `kubectl get secret(s)`.
* NO ALLOW ENTRY IS A BARE OR GROUP WILDCARD that reaches one of those: an
  entry is a command prefix, so `kubectl:*`, `kubectl get:*` or
  `gcloud secrets:*` approve everything a forbidden command would do.
* WHAT WAS REMOVED STILL PROMPTS: each forbidden command is listed in `ask`.
* NO ASK ENTRY SHADOWS AN ALLOW ENTRY: Claude Code ranks ask above allow, so a
  broad `ask` such as `kubectl:*` would silently turn every narrowed read-only
  verb back into a prompt.

Offline: these read the JSON from disk.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / ".claude" / "settings.json"

# Commands that change infrastructure or print a secret. None may be reachable
# from an allow entry; each must prompt.
FORBIDDEN_COMMANDS = (
    "terraform apply",
    "terraform destroy",
    "gcloud run deploy",
    "gcloud secrets versions access",
    "kubectl get secret",
    "kubectl get secrets",
    "kubectl delete",
    "make deploy",
    "make destroy",
    "make tf-apply",
)

# A tool that reaches a forbidden command through any subcommand.
WILDCARD_TOOLS = ("gcloud", "kubectl", "terraform", "make")

FORBIDDEN_WORDS = re.compile(r"\b(apply|destroy|deploy|delete)\b|secrets\s+versions\s+access|get\s+secrets?\b")


def _permissions() -> dict:
    return json.loads(SETTINGS.read_text())["permissions"]


def _bash_prefix(entry: str) -> str | None:
    """The command prefix a `Bash(...)` rule approves, or None for another tool.

    `Bash(x:*)` and `Bash(x *)` approve every command starting with `x`; a bare
    `Bash` (or `Bash(*)`) approves every command, the empty prefix.
    """
    if entry == "Bash":
        return ""
    match = re.fullmatch(r"Bash\((.*)\)", entry)
    if not match:
        return None
    body = match.group(1).strip()
    for suffix in (":*", " *", "*"):
        if body.endswith(suffix):
            return body[: -len(suffix)].strip()
    return body


def _reaches(prefix: str, command: str) -> bool:
    """Whether a rule approving PREFIX approves COMMAND."""
    return command == prefix or command.startswith(prefix)


def _bash_entries(kind: str) -> list[tuple[str, str]]:
    entries = []
    for entry in _permissions().get(kind, []):
        prefix = _bash_prefix(entry)
        if prefix is not None:
            entries.append((entry, prefix))
    return entries


def test_settings_has_allow_and_ask_lists() -> None:
    perms = _permissions()
    assert _bash_entries("allow"), "no Bash allow entries were read: the test would pass vacuously"
    assert _bash_entries("ask"), "no Bash ask entries: removed commands must still prompt"
    assert isinstance(perms.get("deny", []), list)


@pytest.mark.parametrize("command", FORBIDDEN_COMMANDS)
def test_no_allow_entry_reaches_a_forbidden_command(command: str) -> None:
    offenders = [entry for entry, prefix in _bash_entries("allow") if _reaches(prefix, command)]
    assert not offenders, f"{command!r} runs without a prompt through {offenders}"


def test_no_allow_entry_names_a_destructive_or_secret_verb() -> None:
    offenders = [entry for entry, prefix in _bash_entries("allow") if FORBIDDEN_WORDS.search(prefix)]
    assert not offenders, f"allow entries that apply, destroy, deploy, delete or read a secret: {offenders}"


def test_no_bare_tool_wildcard_in_allow() -> None:
    offenders = [entry for entry, prefix in _bash_entries("allow") if prefix == "" or prefix in WILDCARD_TOOLS]
    assert not offenders, f"bare tool wildcards in allow: {offenders}"


@pytest.mark.parametrize("command", FORBIDDEN_COMMANDS)
def test_forbidden_command_prompts(command: str) -> None:
    covering = [entry for entry, prefix in _bash_entries("ask") if _reaches(prefix, command)]
    assert covering, f"{command!r} is neither allowed nor listed in ask: list it so it prompts"


def test_no_ask_entry_shadows_an_allow_entry() -> None:
    shadowed = [
        (allow, ask)
        for allow, allow_prefix in _bash_entries("allow")
        for ask, ask_prefix in _bash_entries("ask")
        if _reaches(ask_prefix, allow_prefix)
    ]
    assert not shadowed, f"ask outranks allow, so these allow entries always prompt: {shadowed}"


@pytest.mark.parametrize(
    ("entry", "prefix"),
    [
        ("Bash(kubectl:*)", "kubectl"),
        ("Bash(gcloud secrets *)", "gcloud secrets"),
        ("Bash(terraform plan)", "terraform plan"),
        ("Bash", ""),
        ("Read(./x)", None),
    ],
)
def test_bash_prefix_parses_rule_shapes(entry: str, prefix: str | None) -> None:
    assert _bash_prefix(entry) == prefix


def test_group_wildcards_are_caught() -> None:
    """The checks see through a wildcard one level down, not only a bare tool."""
    for entry in ("Bash(kubectl:*)", "Bash(kubectl get:*)", "Bash(gcloud secrets:*)", "Bash(gcloud:*)"):
        prefix = _bash_prefix(entry)
        assert prefix is not None
        assert any(_reaches(prefix, command) for command in FORBIDDEN_COMMANDS), entry
