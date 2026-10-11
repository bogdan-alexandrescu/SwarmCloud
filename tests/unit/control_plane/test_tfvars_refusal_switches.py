"""Every `api_refusals` key in an environment's tfvars names a refusal switch.

On 2026-10-11 PR 1044 put `observer_file_issues_unavailable = true` in
dev.tfvars' `api_refusals`, and PR 1054 then removed that code from
`swarm_api.refusals.SWITCHES`. Both merged green, because nothing read the
two together. Terraform rendered REFUSAL_OBSERVER_FILE_ISSUES_UNAVAILABLE=on
(terraform/infra/locals.tf, `local.api_refusal_env`), release 38126514258's
swarm-api revision crashed at start in `refusals.validate_environment`
("names no refusal switch"), and every deploy was blocked until PR 1082.

`validate_environment` is right to refuse: a switch that matches nothing is a
refusal someone believes is on. This file moves that refusal from the deploy
to CI. It reads each terraform/environments/*/*.tfvars, renders each key to
its variable the way locals.tf does (`refusals.env_name`), and fails on one no
switch owns.

A small regex rather than python-hcl2, which is not a dependency. It cannot
silently read less than is there: a file that assigns `api_refusals` and
yields no block fails, and so does a block line that is not a `key = bool`
entry. Offline: source text and an import only.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

from swarm_api import refusals

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = ROOT / "terraform" / "environments"

ASSIGNMENT = re.compile(r"^\s*api_refusals\s*=", re.MULTILINE)
BLOCK = re.compile(r"^\s*api_refusals\s*=\s*\{(?P<body>[^}]*)\}", re.MULTILINE)
ENTRY = re.compile(r'^\s*"?(?P<key>[A-Za-z][A-Za-z0-9_]*)"?\s*=\s*(?:true|false)\s*,?\s*$')


def tfvars_files() -> list[Path]:
    return sorted(ENVIRONMENTS.glob("*/*.tfvars"))


def refusal_keys(text: str, where: str) -> list[str]:
    """The keys of every `api_refusals = { ... }` map in `text`."""
    blocks = list(BLOCK.finditer(text))
    assert len(blocks) == len(ASSIGNMENT.findall(text)), (
        f"{where}: assigns api_refusals in a shape this test cannot read; "
        "write it as a one-level `api_refusals = { code = true }` map"
    )
    keys: list[str] = []
    for block in blocks:
        for line in block.group("body").splitlines():
            line = line.split("#", 1)[0].split("//", 1)[0]
            if not line.strip():
                continue
            entry = ENTRY.match(line)
            assert entry, f"{where}: api_refusals line {line.strip()!r} is not `code = true|false`"
            keys.append(entry.group("key"))
    return keys


def test_the_parse_and_the_switches_are_not_empty() -> None:
    # An empty pass is not a pass: with no files or no switches the
    # comparison below would hold over nothing.
    assert tfvars_files(), f"found no tfvars under {ENVIRONMENTS}"
    assert refusals.SWITCHES, "swarm_api.refusals.SWITCHES is empty"


def test_the_parser_reads_a_block_and_refuses_what_it_cannot_read() -> None:
    text = 'x = 1\napi_refusals = {\n  index_refresh_failed = true  # on\n  "Other_Code" = false\n}\n'
    assert refusal_keys(text, "sample") == ["index_refresh_failed", "Other_Code"]
    assert refusal_keys("api_refusals = {}\n", "sample") == []
    with pytest.raises(AssertionError, match="cannot read"):
        refusal_keys("api_refusals = merge(local.a, {})\n", "sample")
    with pytest.raises(AssertionError, match="is not `code"):
        refusal_keys("api_refusals = {\n  a = { b = true\n}\n", "sample")


@pytest.mark.parametrize("path", tfvars_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_tfvars_refusal_names_a_switch(path: Path) -> None:
    where = str(path.relative_to(ROOT))
    owned = {switch.env: code for code, switch in refusals.SWITCHES.items()}
    unknown = [
        key for key in refusal_keys(path.read_text(), where) if refusals.env_name(key) not in owned
    ]
    messages = [
        f"{where}: api_refusals key {key!r} renders {refusals.env_name(key)} (locals.tf "
        "api_refusal_env), which names no switch in swarm_api.refusals.SWITCHES, so swarm-api "
        "refuses to start (validate_environment). Nearest existing codes: "
        f"{difflib.get_close_matches(key, list(refusals.SWITCHES), n=3, cutoff=0) or 'none'}"
        for key in unknown
    ]
    assert not messages, "\n".join(messages)
