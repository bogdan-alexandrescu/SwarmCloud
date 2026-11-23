"""The console's generated API types are what the generator writes NOW (part of #72).

`apps/swarm-ui/src/generated/api-types.ts` is written by
`scripts/gen_api_types.py` from swarm-api's OpenAPI document. A change to an
API model that is not followed by a regeneration leaves the console compiling
against the old shape, so this test regenerates in-process and compares with
the committed file, byte for byte. Offline: the generator builds `create_app()`
over in-memory clients and a database that refuses every use.

The emitter cases below are behaviour tests of the JSON Schema -> TypeScript
translation itself, so a regression there is named rather than surfacing as a
one-line diff in a 2,000-line file.
"""

from __future__ import annotations

import difflib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/gen_api_types.py"

_spec = importlib.util.spec_from_file_location("gen_api_types", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


@pytest.fixture(scope="module")
def document() -> dict:
    return gen.openapi_document()


def test_the_committed_types_are_what_the_generator_writes_now(document):
    assert gen.OUTPUT.is_file(), f"{gen.OUTPUT} is missing; run: {gen.REGENERATE}"
    committed = gen.OUTPUT.read_text()
    fresh = gen.render(document)
    if committed != fresh:
        diff = "".join(list(difflib.unified_diff(
            committed.splitlines(keepends=True), fresh.splitlines(keepends=True),
            "committed", "generated now"))[:80])
        pytest.fail(f"{gen.OUTPUT.relative_to(REPO)} is stale; regenerate it from the "
                    f"repository root with `{gen.REGENERATE}` and commit the result.\n{diff}")


def test_the_file_says_it_is_generated_and_how_to_regenerate():
    head = gen.OUTPUT.read_text()[:800]
    assert "GENERATED FILE -- DO NOT EDIT BY HAND" in head
    assert gen.REGENERATE in head


def test_the_task_shape_is_in_the_document_and_on_its_route(document):
    """Without the route's documented response the schema would not be in the
    document at all -- `task_to_api` returns a plain dict."""
    schemas = document["components"]["schemas"]
    assert "Task" in schemas and "TaskEnvelope" in schemas
    ok = document["paths"]["/v1/tasks/{task_id}"]["get"]["responses"]["200"]
    # Beside the `object` FastAPI infers from the route's `-> dict`, which is
    # kept: it is what serialises the payload, and dropping it would change
    # how a datetime is written on the wire.
    schema = ok["content"]["application/json"]["schema"]
    assert schema["$ref"] == "#/components/schemas/TaskEnvelope"
    generated = gen.OUTPUT.read_text()
    assert "\nexport interface Task {\n" in generated


def test_building_the_document_reads_no_database():
    with pytest.raises(RuntimeError, match="touched the database"):
        gen.OfflineDatabase().collection("tasks")


def test_check_mode_reports_a_stale_file(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "api-types.ts"
    stale.write_text("// stale\n")
    monkeypatch.setattr(gen, "OUTPUT", stale)
    monkeypatch.setattr(gen, "REPO", tmp_path)
    monkeypatch.setattr(gen, "openapi_document", lambda: {"components": {"schemas": {}}})
    assert gen.main(["--check"]) == 1
    assert "is stale" in capsys.readouterr().err
    assert stale.read_text() == "// stale\n", "--check must write nothing"
    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0


# --------------------------------------------------------------------------
# The emitter
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({}, "unknown"),
        ({"type": "string", "format": "date-time"}, "string"),
        ({"type": "integer"}, "number"),
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, "string | null"),
        ({"anyOf": [{}, {"type": "null"}]}, "unknown"),
        ({"enum": ["read", "write"], "type": "string"}, "'read' | 'write'"),
        ({"const": "it's"}, "'it\\'s'"),
        ({"$ref": "#/components/schemas/TaskLinks"}, "TaskLinks"),
        ({"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
         "(string | number)[]"),
        ({"type": "array"}, "unknown[]"),
        ({"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}]},
         "[string, number]"),
        ({"type": "object"}, "{ [key: string]: unknown }"),
        ({"type": "object", "additionalProperties": {"type": "integer"}},
         "{ [key: string]: number }"),
        ({"type": ["string", "null"]}, "string | null"),
    ],
)
def test_a_schema_becomes_this_type(schema, expected):
    assert gen.ts_type(schema) == expected


def test_an_object_component_is_an_interface_with_its_optional_fields_marked():
    text = gen.declaration("Thing", {
        "type": "object",
        "description": "A thing.",
        "properties": {
            "id": {"type": "string", "description": "Its id. Never */ closes early."},
            "count": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
            "odd-key": {"type": "boolean"},
        },
        "required": ["id", "odd-key"],
    })
    assert text == (
        "/** A thing. */\n"
        "export interface Thing {\n"
        "  /** Its id. Never *\\/ closes early. */\n"
        "  id: string\n"
        "  count?: number | null\n"
        "  'odd-key': boolean\n"
        "}"
    )


def test_two_schemas_with_one_typescript_name_are_refused():
    with pytest.raises(SystemExit, match="one TypeScript name"):
        gen.render({"components": {"schemas": {"a.B": {}, "a_B": {}}}})


# --------------------------------------------------------------------------
# The inputs the generator declares (the pre-publish gate's trigger)
# --------------------------------------------------------------------------

_LOADED = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("gen_api_types", sys.argv[1])
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)
gen.openapi_document()
repo = Path(sys.argv[2]).resolve()
found = set()
for module in list(sys.modules.values()):
    path = getattr(module, "__file__", None)
    if not path:
        continue
    path = Path(path).resolve()
    if path.is_relative_to(repo) and ".venv" not in path.parts:
        found.add(str(path.relative_to(repo)))
print(json.dumps(sorted(found)))
"""


def test_the_declared_inputs_are_what_building_the_document_loads():
    """The gate runs the drift check only when a change touches one of
    `INPUTS`, so the list is held equal, both ways, to the repository's
    modules that building the document imports -- in a fresh interpreter,
    so no other test's imports count."""
    import json
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-c", _LOADED, str(SCRIPT), str(REPO)],
                         check=True, capture_output=True, text=True, cwd=REPO).stdout
    loaded = [p for p in json.loads(out) if p.startswith("apps/")]
    packages = {"/".join(p.split("/")[:3]) + "/" for p in loaded}
    declared = {p for p in gen.INPUTS if p.startswith("apps/") and p.endswith("/")}
    # MUTATION: a package swarm-api starts importing that is not declared
    # here is a model change the gate would not check.
    assert packages == declared
    assert gen.relpath(gen.OUTPUT) in gen.INPUTS
    assert "scripts/gen_api_types.py" in gen.INPUTS and "uv.lock" in gen.INPUTS


def test_inputs_mode_prints_them_without_importing_the_api(capsys, monkeypatch):
    def refuse() -> dict:
        raise AssertionError("--inputs must not build the document")

    monkeypatch.setattr(gen, "openapi_document", refuse)
    assert gen.main(["--inputs"]) == 0
    assert capsys.readouterr().out.splitlines() == list(gen.INPUTS)
