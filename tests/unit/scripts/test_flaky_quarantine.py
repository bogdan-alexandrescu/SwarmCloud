"""The flaky-test quarantine: tests/flaky.txt, its parser, its wiring into
collection and into the unit job (docs/ci.md, "Flaky tests and the quarantine").

What is held here:
  * the list parses, and a malformed line is refused with its line number --
    a list that half-parsed would quarantine the wrong thing in silence;
  * every listed test exists: a renamed test would leave its entry
    quarantining nothing while the real flake gates again;
  * the `flaky` marker is registered, and tests/unit/conftest.py puts it on
    exactly the listed items;
  * the unit job deselects the marker from the gating run and runs it in a
    step that cannot fail the gate.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import tomllib
from pathlib import Path

import pytest
import yaml

import flaky_quarantine as fq

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
CONFTEST = REPO / "tests" / "unit" / "conftest.py"

HERE_ID = "tests/unit/scripts/test_flaky_quarantine.py"


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
def test_a_well_formed_list_parses_with_comments_and_blank_lines():
    text = (
        "# header\n"
        "\n"
        "tests/unit/a/test_x.py::test_one   #12   2026-10-14\n"
        "   # an indented comment\n"
        "tests/unit/a/test_x.py::TestK::test_two[p-1]\t#7\t2026-01-02\n"
    )
    entries = fq.parse(text)
    assert [(e.nodeid, e.issue, e.added, e.line) for e in entries] == [
        ("tests/unit/a/test_x.py::test_one", 12, dt.date(2026, 10, 14), 3),
        ("tests/unit/a/test_x.py::TestK::test_two[p-1]", 7, dt.date(2026, 1, 2), 5),
    ]


def test_an_empty_list_is_an_empty_quarantine():
    assert fq.parse("") == []
    assert fq.parse("# only comments\n\n") == []


@pytest.mark.parametrize(
    "line, says",
    [
        ("tests/unit/a/test_x.py::test_one #12", "got 2 field"),
        ("tests/unit/a/test_x.py::test_one #12 2026-10-14 extra", "got 4 field"),
        ("test_x.py::test_one #12 2026-10-14", "not a pytest node id"),
        ("tests/unit/a/test_x.py #12 2026-10-14", "not a pytest node id"),
        ("tests/unit/a/test_x.txt::test_one #12 2026-10-14", "not a .py file"),
        ("tests/unit/a/test_x.py::test_one 12 2026-10-14", "not an issue number"),
        ("tests/unit/a/test_x.py::test_one #0 2026-10-14", "not an issue number"),
        ("tests/unit/a/test_x.py::test_one #12 14/10/2026", "not a YYYY-MM-DD"),
        ("tests/unit/a/test_x.py::test_one #12 2026-02-30", "2026-02-30"),
    ],
)
def test_a_malformed_line_is_refused_with_its_line_number(line, says):
    with pytest.raises(fq.QuarantineListError) as refused:
        fq.parse("# header\n" + line + "\n")
    assert "tests/flaky.txt:2:" in str(refused.value), refused.value
    assert says in str(refused.value), refused.value


def test_a_test_listed_twice_is_refused():
    text = "tests/unit/a/test_x.py::t #1 2026-10-14\ntests/unit/a/test_x.py::t #2 2026-10-14\n"
    with pytest.raises(fq.QuarantineListError, match="already listed on line 1"):
        fq.parse(text)


# ---------------------------------------------------------------------------
# Matching and marking
# ---------------------------------------------------------------------------
def _entries(*ids: str) -> list[fq.Entry]:
    return fq.parse("".join(f"{i} #1 2026-10-14\n" for i in ids))


def test_a_bare_id_covers_every_parameter_and_a_parametrised_one_only_itself():
    bare = _entries("tests/unit/a/test_x.py::test_p")
    assert fq.is_quarantined("tests/unit/a/test_x.py::test_p", bare)
    assert fq.is_quarantined("tests/unit/a/test_x.py::test_p[a]", bare)
    assert not fq.is_quarantined("tests/unit/a/test_x.py::test_p2", bare)
    one = _entries("tests/unit/a/test_x.py::test_p[a]")
    assert fq.is_quarantined("tests/unit/a/test_x.py::test_p[a]", one)
    assert not fq.is_quarantined("tests/unit/a/test_x.py::test_p[b]", one)
    assert not fq.is_quarantined("tests/unit/a/test_x.py::test_p", one)


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[object] = []

    def add_marker(self, marker: object) -> None:
        self.markers.append(marker)


def test_apply_marks_exactly_the_listed_items():
    items = [_Item("tests/unit/a/test_x.py::test_one"), _Item("tests/unit/a/test_x.py::test_two"),
             _Item("tests/unit/b/test_y.py::test_one")]
    marked = fq.apply(items, _entries("tests/unit/a/test_x.py::test_one"), "M")
    assert marked == ["tests/unit/a/test_x.py::test_one"]
    assert [i.markers for i in items] == [["M"], [], []]


def test_the_conftest_marks_listed_items_flaky(monkeypatch):
    """The wiring, run: tests/unit/conftest.py's collection hook, over items
    the list names and one it does not. MUTATION: drop the hook, or mark with
    anything but `pytest.mark.flaky` -- the job's `-m` would select nothing."""
    spec = importlib.util.spec_from_file_location("_quarantine_conftest_under_test", CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(fq, "load", lambda: _entries(HERE_ID + "::test_listed"))
    listed, other = _Item(HERE_ID + "::test_listed[x]"), _Item(HERE_ID + "::test_other")
    module.pytest_collection_modifyitems(None, [listed, other])
    assert [getattr(m, "name", None) for m in listed.markers] == ["flaky"], listed.markers
    assert other.markers == []


def test_the_conftest_stops_collection_on_a_malformed_list(monkeypatch):
    spec = importlib.util.spec_from_file_location("_quarantine_conftest_refuses", CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def broken() -> list[fq.Entry]:
        return fq.parse("not-a-node-id #1 2026-10-14\n")

    monkeypatch.setattr(fq, "load", broken)
    with pytest.raises(pytest.UsageError, match="tests/flaky.txt:1"):
        module.pytest_collection_modifyitems(None, [_Item(HERE_ID + "::t")])


def test_the_flaky_marker_is_registered():
    markers = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.split(":", 1)[0].strip() == "flaky" for m in markers), markers


# ---------------------------------------------------------------------------
# The checked-in list
# ---------------------------------------------------------------------------
def test_the_checked_in_list_parses():
    assert fq.LIST == REPO / "tests" / "flaky.txt"
    assert fq.LIST.is_file(), "tests/flaky.txt is gone: docs/ci.md and the unit job name it"
    fq.load()


def test_every_quarantined_test_exists():
    """A renamed or deleted test leaves its entry quarantining nothing. Remove
    the line, or point it at the test's new id."""
    entries = fq.load()
    assert fq.missing(entries) == [], (
        "tests/flaky.txt names tests that do not exist: " + ", ".join(fq.missing(entries)))


def test_the_existence_check_finds_a_test_that_is_not_there():
    """The check above must be able to fail: proven on ids that exist and ids
    that do not, in this very file."""
    real = _entries(
        HERE_ID + "::test_every_quarantined_test_exists",
        HERE_ID + "::test_a_malformed_line_is_refused_with_its_line_number[a-b]",
        HERE_ID + "::_Item::add_marker",
    )
    assert fq.missing(real) == []
    gone = _entries(
        HERE_ID + "::test_that_was_renamed",
        HERE_ID + "::_Item::no_such_method",
        HERE_ID + "::NoSuchClass::test_one",
        "tests/unit/scripts/test_no_such_file.py::test_one",
    )
    assert fq.missing(gone) == [e.nodeid for e in gone]


# ---------------------------------------------------------------------------
# The unit job
# ---------------------------------------------------------------------------
def _unit_steps() -> list[dict]:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]["python-unit"]["steps"]


def test_the_gating_run_deselects_the_quarantine():
    """MUTATION: drop `-m "not flaky"` and a listed flake gates again."""
    (gate,) = [s for s in _unit_steps() if "uv run pytest" in str(s.get("run", ""))
               and "--junitxml" in str(s.get("run", ""))]
    assert '-m "not flaky"' in gate["run"], gate["run"]
    assert not gate.get("continue-on-error"), "the gating run must be able to fail"


def test_the_quarantined_tests_still_run_and_never_fail_the_gate():
    """MUTATION: drop the step (the flakes are skipped for good), drop
    `continue-on-error`, or let the run's exit status become the step's."""
    (quarantine,) = [s for s in _unit_steps() if "uv run pytest" in str(s.get("run", ""))
                     and "-m flaky" in str(s.get("run", ""))]
    assert quarantine.get("continue-on-error") is True, quarantine
    run = quarantine["run"]
    assert "|| rc=$?" in run, run
    assert "::warning" in run and "GITHUB_STEP_SUMMARY" in run, run
    assert '"${files[@]}"' in run, "it runs this shard's files, not the whole suite"
    assert (quarantine.get("env") or {}).get("SWARM_REQUIRE_REAL_REAP") == "1"
