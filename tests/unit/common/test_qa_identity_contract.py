"""Contract request 106: `claude-code-browser` declares `qa_identity` (QI-CONTRACT, part of #1649).

docs/design/qa-identity.md §5 request QI-1, accepted by the owner on
2026-10-19 with option B (a QA service account whose IAP JWT the live gate
adds). What is held here, the three tests §5 names and the filing:

  * the key is declared on `claude-code-browser`, a string with exactly the
    one choice `console`;
  * no other profile declares it: only `claude-code-browser` has a live gate
    (`live_browser=True`), and option B needs the gate;
  * `check_inputs` accepts `"console"` and refuses `"admin"`, `""` and a
    non-string, each as a bound broken on the declared key (invariant 10: a
    name from a closed list, never an account, a host or a token);
  * the request is filed in docs/contract-change-requests.md as entry 106.

No cloud, no emulator: the contract is pure.

MUTATION: add `"admin"` to the choices and
`test_check_inputs_refuses_what_is_not_the_one_choice["admin"]` fails;
declare the key on `claude-code` too and
`test_no_other_profile_declares_qa_identity` fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs

PROFILE = "claude-code-browser"
KEY = "qa_identity"
REQUESTS = Path(__file__).resolve().parents[3] / "docs" / "contract-change-requests.md"


def test_claude_code_browser_declares_qa_identity_with_the_one_choice_console():
    declared = RUNNER_PROFILES[PROFILE].inputs
    assert KEY in declared, f"{PROFILE} declares {sorted(declared)}; request 106 adds {KEY}"
    spec = declared[KEY]
    assert spec.kind == "string"
    assert spec.choices == ("console",)
    assert not spec.required, "absent means no QA identity; a task without it is unchanged"
    assert spec.means, "the catalogue serves what the input does to every picker"
    # The profile it rides on is the one with a live gate, which B needs.
    assert RUNNER_PROFILES[PROFILE].live_browser is True


def test_no_other_profile_declares_qa_identity():
    declaring = sorted(name for name, profile in RUNNER_PROFILES.items() if KEY in profile.inputs)
    assert declaring == [PROFILE], declaring


@pytest.mark.parametrize("name", sorted(set(RUNNER_PROFILES) - {PROFILE}))
def test_every_other_profile_refuses_qa_identity_as_undeclared(name):
    with pytest.raises(InputRefused) as caught:
        check_inputs(RUNNER_PROFILES[name], {KEY: "console"})
    assert caught.value.key == KEY
    # Undeclared, not out of bounds: the key is this one profile's alone.
    assert caught.value.expected is None


def test_check_inputs_accepts_console():
    assert check_inputs(RUNNER_PROFILES[PROFILE], {KEY: "console"}) == {KEY: "console"}
    assert check_inputs(
        RUNNER_PROFILES[PROFILE], {"issue": 7, "browser_tool": "playwright", KEY: "console"}
    ) == {"browser_tool": "playwright", "issue": 7, KEY: "console"}


@pytest.mark.parametrize(
    "value",
    ["admin", "", "Console", " console", 1, True, None, ["console"], {"name": "console"}],
    ids=["admin", "empty", "case", "padded", "int", "bool", "null", "list", "object"],
)
def test_check_inputs_refuses_what_is_not_the_one_choice(value):
    with pytest.raises(InputRefused) as caught:
        check_inputs(RUNNER_PROFILES[PROFILE], {KEY: value})
    # Refused as a declared key whose value broke its bound, so the caller is
    # told the one choice rather than that the key does not exist.
    assert caught.value.key == KEY
    assert caught.value.expected is not None
    assert "console" in str(caught.value)


def test_request_qi_1_is_filed_as_entry_106():
    """The change the tests above hold has its entry, under the design's §5
    name (CLAUDE.md rule 1)."""
    text = REQUESTS.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| 106 |")]
    assert len(rows) == 1, rows
    assert "QI-CONTRACT" in rows[0] and "QA identity request QI-1" in rows[0]
    assert re.search(r"^## 106\. .*\(QI-CONTRACT, QA identity request QI-1\)$", text, re.M)
