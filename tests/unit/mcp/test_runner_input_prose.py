"""The prose that tells a caller what a runner takes is the catalogue's, or says nothing (#213 review).

WHAT WAS WRONG. `RunnerProfile.inputs` is the one declaration, and swarm-api
and the bridge read it. But docs/workflows.md's table said `exit_code` was
"1..255 except 77, 78, 143" and `retry_after_seconds` "1..3600", and the
plugin README said "(1 to 3600)" and "refuses 0, 77, 78 and 143": four copies
of bounds nothing compared with the catalogue. The review of #213 asked for
the copies to be held by a test that reads the catalogue, or replaced by a
generated table. Both, here:

  * THE TABLE IS GENERATED. Each of docs/workflows.md and plugin/README.md
    carries, for every profile that declares inputs, a table between
    `<!-- runner-inputs:<profile> ... -->` markers. It must equal what
    `_table` renders from the catalogue -- each key, `RunnerInput.describe()`
    and `means` -- and on a difference this test prints the table to paste.
  * NO OTHER SENTENCE STATES A BOUND. Outside those tables, a sentence in
    either file, or in the delegate skill, that names a declared number in
    backticks states no number about it. Examples are values, not bounds
    (`{"sleep_seconds": 120}`, `--input sleep_seconds=120`), and each is
    checked against the catalogue instead: it must be one the API accepts.

Read, never imported: the docs are text files, and the catalogue is the frozen
module itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs
from swarm_mcp import profiles as bridge
from swarm_mcp.client import SwarmError

_REPO = Path(__file__).resolve().parents[3]

#: The two files that carry the generated tables.
TABLED = ("docs/workflows.md", "plugin/README.md")
#: Every text a caller reads a runner's inputs from, the delegate skill included.
PROSE = TABLED + ("plugin/skills/delegate/SKILL.md",)

#: The profiles that declare at least one input. `inputs is None` (not declared
#: yet, #218) and `{}` (the prompt only) have no table.
DECLARING = [name for name, profile in RUNNER_PROFILES.items() if profile.inputs]

_BLOCK = re.compile(
    r"^<!-- runner-inputs:(?P<name>[a-z0-9-]+) [^\n]*-->\n(?P<body>.*?)^<!-- /runner-inputs:(?P=name) -->$",
    re.M | re.S,
)
_FENCE = re.compile(r"^```.*?^```", re.M | re.S)


def _table(name: str) -> str:
    """The table for `name`, exactly as the docs must carry it."""
    rows = [
        f"| input | kind and bounds | what the {name} runner does with it |",
        "|---|---|---|",
    ]
    for key, spec in RUNNER_PROFILES[name].inputs.items():
        means = spec.means.replace("|", "\\|")
        rows.append(f"| `{key}` | {spec.describe()} | {means} |")
    return "\n".join(rows) + "\n"


def _read(rel: str) -> str:
    return (_REPO / rel).read_text()


def _outside_tables(text: str) -> str:
    """The text with the generated tables and fenced code taken out."""
    return _FENCE.sub("", _BLOCK.sub("", text))


def test_some_profile_declares_inputs():
    """Guards every parametrisation below against reading nothing."""
    assert "mock" in DECLARING, DECLARING


@pytest.mark.parametrize("rel", TABLED)
def test_each_declaring_profile_has_one_generated_table_equal_to_the_catalogue(rel):
    text = _read(rel)
    blocks: dict[str, list[str]] = {}
    for match in _BLOCK.finditer(text):
        blocks.setdefault(match["name"], []).append(match["body"])
    for name in DECLARING:
        found = blocks.get(name, [])
        assert len(found) == 1, (
            f"{rel} carries {len(found)} runner-inputs table(s) for {name}; it needs "
            f"exactly one, between these markers:\n\n"
            f"<!-- runner-inputs:{name} generated from RUNNER_PROFILES[\"{name}\"].inputs; "
            f"tests/unit/mcp/test_runner_input_prose.py fails when it differs -->\n"
            f"{_table(name)}<!-- /runner-inputs:{name} -->"
        )
        assert found[0] == _table(name), (
            f"{rel}'s table for {name} is not the catalogue's. Replace it with:\n\n{_table(name)}"
        )
    stale = sorted(set(blocks) - set(DECLARING))
    assert not stale, f"{rel} carries a table for {stale}, which declare(s) no inputs"


def _bounded_keys() -> set[str]:
    """Every declared key that has a bound or a refused value to restate."""
    return {
        key
        for name in DECLARING
        for key, spec in RUNNER_PROFILES[name].inputs.items()
        if spec.kind in ("number", "integer")
    }


#: A backticked span that is an example rather than a name: it holds a JSON
#: object or a `key=value` flag.
_EXAMPLE_SPAN = re.compile(r"`[^`]*[{=][^`]*`")
_CODE_SPAN = re.compile(r"`[^`]*`")
_HIDDEN = re.compile("\x00([0-9]+)\x00")


def _clauses(flat: str) -> list[str]:
    """`flat` cut into clauses, never inside a backticked span.

    A clause ends at a full stop, a semicolon or a colon followed by a space,
    so `0.5.3` and `e.g. {` stay whole -- and the colon inside an example like
    `{"quota_exhausted": true}` does not end one, which would leave half an
    example in each clause and a bare number in the second.
    """
    spans: list[str] = []

    def hide(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        return f"\x00{len(spans) - 1}\x00"

    hidden = _CODE_SPAN.sub(hide, flat)
    return [
        _HIDDEN.sub(lambda m: spans[int(m.group(1))], clause)
        for clause in re.split(r"(?<=[.;:])\s+", hidden)
    ]


@pytest.mark.parametrize("rel", PROSE)
def test_no_sentence_outside_the_tables_restates_a_bound(rel):
    keys = _bounded_keys()
    assert keys, "no declared number to look for; this test reads nothing"
    offenders = []
    for paragraph in re.split(r"\n\s*\n", _outside_tables(_read(rel))):
        for clause in _clauses(" ".join(paragraph.split())):
            named = sorted(key for key in keys if f"`{key}`" in clause)
            if named and re.search(r"\d", _EXAMPLE_SPAN.sub("", clause)):
                offenders.append(f"{named}: {clause}")
    assert not offenders, (
        f"{rel} states a bound outside the generated table, where nothing holds it "
        f"to the catalogue. Point at the table instead:\n  " + "\n  ".join(offenders)
    )


def _json_examples(text: str) -> list[dict]:
    found = []
    for span in re.findall(r"`(\{[^`]*\})`", " ".join(text.split())):
        try:
            value = json.loads(span)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


@pytest.mark.parametrize("rel", PROSE)
def test_every_example_value_is_one_the_catalogue_accepts(rel):
    """An example is what a model copies. `{"sleep_seconds": 120}` against a
    ceiling lowered to 60 would be a 422 the skill taught."""
    text = _outside_tables(_read(rel))
    mock = RUNNER_PROFILES["mock"]
    declared = set(mock.inputs)
    checked = 0
    for example in _json_examples(text):
        sent = {k: v for k, v in example.items() if k in declared}
        if not sent:
            continue
        checked += 1
        try:
            check_inputs(mock, sent)
        except InputRefused as refused:
            pytest.fail(f"{rel} shows {example}, which the catalogue refuses: {refused}")
    for key, value in re.findall(r"--input\s+([a-z_]+)=([^\s`]+)", " ".join(text.split())):
        if key not in declared:
            continue
        checked += 1
        try:
            bridge.parse_input_flags("mock", [f"{key}={value}"])
        except SwarmError as refused:
            pytest.fail(f"{rel} shows --input {key}={value}, which the catalogue refuses: {refused}")
    if rel == "plugin/skills/delegate/SKILL.md":
        assert checked, f"{rel} shows no example any more; this test reads nothing there"
