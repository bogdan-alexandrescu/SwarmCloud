"""The console's generated constants are what their groups build NOW (part of #72).

`scripts/gen_api_types.py` writes, beside `api-types.ts`, one JSON file per
constants group (`scripts/api_constants/<group>.py`) under
`apps/swarm-ui/src/generated/constants/`. A group reads live Python objects --
enum members, frozen sets, a registry's values -- so the console compares with
the API's own values instead of a hand copy that drifts silently when a member
is added. These tests hold the committed files equal to what the groups build,
the `--check` mode's refusals (stale, missing, orphan), the `--inputs` mode's
standard-library-only trigger, and the `states` group's content against the
enum objects themselves.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/gen_api_types.py"

_spec = importlib.util.spec_from_file_location("gen_api_types_constants", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _groups() -> dict:
    return dict(gen.constant_groups())


def test_there_is_a_states_group_and_its_file_is_committed():
    assert "states" in _groups()
    assert (gen.constants_dir() / "states.json").is_file()


@pytest.mark.parametrize("name", sorted(_groups()))
def test_each_committed_constants_file_is_what_its_group_builds_now(name):
    module = _groups()[name]
    path = gen.constants_dir() / f"{name}.json"
    assert path.is_file(), f"{gen.relpath(path)} is missing; run: {gen.REGENERATE}"
    assert path.read_text() == gen.constants_text(module.build()), (
        f"{gen.relpath(path)} is stale; regenerate it from the repository root with "
        f"`{gen.REGENERATE}` and commit the result")


def test_no_committed_constants_file_lacks_a_group():
    written = {f"{name}.json" for name in _groups()}
    present = {p.name for p in gen.constants_dir().glob("*.json")}
    assert present <= written, f"no group writes {sorted(present - written)}"


def test_the_text_is_deterministic_and_says_how_to_regenerate():
    text = gen.constants_text({"b": 1, "a": {"y": 2, "x": 1}})
    assert text.endswith("}\n") and not text.endswith("\n\n")
    data = json.loads(text)
    assert list(data) == ["_generated", "a", "b"]
    assert list(data["a"]) == ["x", "y"]
    assert gen.REGENERATE in data["_generated"]


# --------------------------------------------------------------------------
# --check over the constants files
# --------------------------------------------------------------------------

class _Group:
    INPUTS = ("scripts/api_constants/fake.py",)

    @staticmethod
    def build() -> dict:
        return {"values": ["A", "B"]}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A generator writing into tmp_path, with one fake group and an empty
    OpenAPI document, its api-types.ts already current."""
    output = tmp_path / "generated" / "api-types.ts"
    monkeypatch.setattr(gen, "REPO", tmp_path)
    monkeypatch.setattr(gen, "OUTPUT", output)
    monkeypatch.setattr(gen, "openapi_document", lambda: {"components": {"schemas": {}}})
    monkeypatch.setattr(gen, "constant_groups", lambda: [("fake", _Group)])
    assert gen.main([]) == 0
    assert (tmp_path / "generated/constants/fake.json").is_file()
    assert gen.main(["--check"]) == 0
    return tmp_path / "generated/constants"


def test_check_fails_on_a_stale_constants_file(sandbox, capsys):
    stale = sandbox / "fake.json"
    stale.write_text('{"values": ["A"]}\n')
    capsys.readouterr()
    assert gen.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "generated/constants/fake.json is stale" in err
    assert stale.read_text() == '{"values": ["A"]}\n', "--check must write nothing"


def test_check_fails_on_a_missing_constants_file(sandbox, capsys):
    (sandbox / "fake.json").unlink()
    capsys.readouterr()
    assert gen.main(["--check"]) == 1
    assert "generated/constants/fake.json is missing" in capsys.readouterr().err


def test_check_fails_on_a_constants_file_no_group_writes(sandbox, capsys):
    orphan = sandbox / "gone.json"
    orphan.write_text("{}\n")
    capsys.readouterr()
    assert gen.main(["--check"]) == 1
    assert "generated/constants/gone.json is written by no group" in capsys.readouterr().err
    assert orphan.is_file(), "--check must delete nothing"


def test_regenerating_removes_a_constants_file_no_group_writes(sandbox):
    (sandbox / "gone.json").write_text("{}\n")
    assert gen.main([]) == 0
    assert not (sandbox / "gone.json").exists()
    assert gen.main(["--check"]) == 0


def test_check_names_every_stale_file_not_only_the_first(sandbox, capsys):
    (sandbox.parent / "api-types.ts").write_text("// stale\n")
    (sandbox / "fake.json").write_text("{}\n")
    capsys.readouterr()
    assert gen.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "api-types.ts is stale" in err and "fake.json is stale" in err


# --------------------------------------------------------------------------
# --inputs and the groups' imports
# --------------------------------------------------------------------------

def test_inputs_lists_every_groups_inputs(capsys, monkeypatch):
    def refuse() -> dict:
        raise AssertionError("--inputs must not build the document")

    monkeypatch.setattr(gen, "openapi_document", refuse)
    assert gen.main(["--inputs"]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == len(set(printed))
    for name, module in _groups().items():
        for path in module.INPUTS:
            assert path in printed, f"group {name}: {path} is not printed"
        assert f"apps/swarm-ui/src/generated/constants/{name}.json" in printed
    assert "scripts/api_constants/" in printed
    assert "apps/swarm-ui/src/generated/constants/" in printed


_IMPORTED = r"""
import json, subprocess, sys, runpy
sys.argv = [sys.argv[1], *sys.argv[2:]]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    assert not exc.code, exc.code
apps = sorted(m for m in sys.modules if m.split(".")[0].startswith("swarm_")
              or m.split(".")[0] in {"agent_worker", "fastapi", "pydantic"})
print(json.dumps(apps), file=sys.stderr)
"""


def test_inputs_mode_imports_no_application_package_in_a_fresh_interpreter():
    """The gate's trigger runs with plain python3; a group that imported
    swarm_common at module level would make it import the application."""
    run = subprocess.run([sys.executable, "-c", _IMPORTED, str(SCRIPT), "--inputs"],
                         check=True, capture_output=True, text=True, cwd=REPO)
    assert json.loads(run.stderr.strip().splitlines()[-1]) == []
    assert "apps/common/swarm_common/" in run.stdout.splitlines()


_GROUP_IMPORTS = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("g", sys.argv[1])
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)
names = [name for name, _ in gen.constant_groups()]
apps = sorted(m for m in sys.modules if m.split(".")[0].startswith("swarm_"))
print(json.dumps({"groups": names, "apps": apps}))
"""


def test_importing_a_group_module_imports_no_application_package():
    out = subprocess.run([sys.executable, "-c", _GROUP_IMPORTS, str(SCRIPT)],
                         check=True, capture_output=True, text=True, cwd=REPO).stdout
    seen = json.loads(out)
    assert "states" in seen["groups"]
    assert seen["apps"] == []


def test_groups_are_discovered_in_sorted_order():
    names = [name for name, _ in gen.constant_groups()]
    assert names == sorted(names)
    on_disk = sorted(p.stem for p in (REPO / "scripts/api_constants").glob("*.py")
                     if not p.name.startswith("_"))
    assert names == on_disk


# --------------------------------------------------------------------------
# The states group, against the enum objects
# --------------------------------------------------------------------------

def test_the_states_file_holds_exactly_the_members_of_each_enum():
    from swarm_common.models import CREDENTIAL_TIERS, EndCause
    from swarm_common.profiles import RESOURCE_CLASSES
    from swarm_common.states import (
        CONCURRENCY_STATES,
        TERMINAL_STATES,
        BlockedReason,
        EventType,
        ParkReason,
        TaskState,
    )

    data = json.loads((gen.constants_dir() / "states.json").read_text())
    for key, enum in (("task_state", TaskState), ("park_reason", ParkReason),
                      ("blocked_reason", BlockedReason), ("event_type", EventType),
                      ("end_cause", EndCause)):
        assert data[key] == [m.value for m in enum], key
    assert data["terminal_states"] == sorted(s.value for s in TERMINAL_STATES)
    assert data["concurrency_states"] == sorted(s.value for s in CONCURRENCY_STATES)
    assert data["credential_tiers"] == list(CREDENTIAL_TIERS)
    assert data["resource_class_units"] == {n: rc.units for n, rc in RESOURCE_CLASSES.items()}
    # Invariant 1/3: concurrency counts from LEASED; nothing pending costs.
    assert "LEASED" in data["concurrency_states"]
    assert "QUEUED" not in data["concurrency_states"]
