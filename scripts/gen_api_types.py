#!/usr/bin/env python3
"""Write the console's TypeScript types from swarm-api's OpenAPI document.

    uv run python scripts/gen_api_types.py           # (re)write the file
    uv run python scripts/gen_api_types.py --check   # exit 1 if it is stale

THE SOURCE. swarm-api's own `create_app()`, built OFFLINE: no cloud
credentials, no emulator, no network. Every client `build_context` would make
from the environment is replaced by an in-memory one, and the database is
`OfflineDatabase`, which refuses every use -- building the document must read
nothing, and a factory that started to would fail here rather than quietly
reach for Application Default Credentials. `app.openapi()` is the same
document the service serves at `/openapi.json`.

THE OUTPUT. `apps/swarm-ui/src/generated/api-types.ts`: one TypeScript
declaration per schema in the document's `components.schemas`, sorted by name,
an object with properties as an `interface`, anything else as a `type`. A
schema's and a property's `description` becomes its doc comment. The emitter is
here rather than an npm package because the console's devDependencies carry no
OpenAPI generator, and keeping it in Python lets the drift test
(`tests/unit/scripts/test_api_types_drift.py`) run in the offline unit suite
with no `node_modules`. It covers what pydantic's JSON Schema emits: `$ref`,
`anyOf`/`oneOf`/`allOf`, `enum`/`const`, the scalar types, arrays (fixed-length
`prefixItems` as tuples) and objects (`properties`, `additionalProperties`); a
schema it does not recognise is `unknown`, never a guess.

Part of #72: the console's `Task` is the generated `Task` (see
`apps/swarm-api/swarm_api/wire_types.py`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import warnings
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "apps/swarm-ui/src/generated/api-types.ts"
REGENERATE = "uv run python scripts/gen_api_types.py"

HEADER = f"""\
// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Generated from swarm-api's OpenAPI document (`create_app().openapi()`, the
// document the service serves at /openapi.json) by scripts/gen_api_types.py.
// To change a type, change the API's model and regenerate, from the
// repository root:
//
//   {REGENERATE}
//
// tests/unit/scripts/test_api_types_drift.py fails while this file differs
// from what that command writes.
"""

IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


class OfflineDatabase:
    """A Firestore stand-in that refuses every use.

    `create_app` must not read the database to describe its routes; if a
    factory ever does, the generator fails loudly here instead of building a
    real client or emitting a document shaped by whatever it happened to read.
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            f"building the OpenAPI document touched the database ({name!r}); "
            "it must read nothing"
        )


def openapi_document() -> dict[str, Any]:
    """swarm-api's OpenAPI document, built with no credentials."""
    # The Google client libraries warn about the interpreter's support window
    # on import; that is noise in a generator's output, not a finding.
    warnings.filterwarnings("ignore", category=FutureWarning)

    from swarm_common.config import Settings

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.objects import InMemoryObjectReader
    from swarm_api.settings import ApiSettings
    from swarm_api.waker import NullWaker

    # Placeholders only: nothing here is contacted. They are the generic
    # values the unit tests use, and none of them reaches the document.
    core = Settings(
        project_id="example-project",
        region="us-central1",
        environment="test",
        firestore_database="swarm",
        artifact_bucket="example-project-swarm-artifacts",
        allowed_domains=("example.com",),
    )
    settings = ApiSettings(core=core, dispatch_topic="")
    ctx = build_context(
        settings=settings,
        db=OfflineDatabase(),
        verifier=StaticTokenVerifier({}),
        groups=StaticGroups({}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        objects=InMemoryObjectReader(bucket=core.artifact_bucket),
    )
    return create_app(ctx).openapi()


# --------------------------------------------------------------------------
# JSON Schema -> TypeScript
# --------------------------------------------------------------------------

def type_name(ref_or_name: str) -> str:
    """The TypeScript name of a component: the last segment of its `$ref`,
    with anything that is not an identifier character made `_`."""
    name = ref_or_name.rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9_$]", "_", name)
    return name if IDENTIFIER.match(name) else f"_{name}"


def literal(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return json.dumps(value)


def union(parts: list[str]) -> str:
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    if "unknown" in seen:
        return "unknown"
    return " | ".join(seen) if seen else "never"


def wrapped(text: str) -> str:
    """`text` safe to stand alone inside a larger type expression."""
    return f"({text})" if (" | " in text or " & " in text) else text


def property_key(name: str) -> str:
    return name if IDENTIFIER.match(name) else literal(name)


def doc_comment(description: str | None, indent: str) -> list[str]:
    if not description:
        return []
    text = description.replace("*/", "*\\/")
    lines: list[str] = []
    for paragraph in text.strip().split("\n\n"):
        if lines:
            lines.append("")
        lines.extend(textwrap.wrap(" ".join(paragraph.split()), 76) or [""])
    if len(lines) == 1:
        return [f"{indent}/** {lines[0]} */"]
    return [f"{indent}/**", *(f"{indent} *{' ' + line if line else ''}" for line in lines),
            f"{indent} */"]


def ts_type(schema: Any, indent: str = "") -> str:
    if not isinstance(schema, dict) or not schema:
        return "unknown"
    if "$ref" in schema:
        return type_name(schema["$ref"])
    if "const" in schema:
        return literal(schema["const"])
    if "enum" in schema:
        return union([literal(v) for v in schema["enum"]])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return union([ts_type(s, indent) for s in schema[key]])
    if "allOf" in schema:
        parts = [ts_type(s, indent) for s in schema["allOf"]]
        return parts[0] if len(parts) == 1 else " & ".join(wrapped(p) for p in parts)
    kind = schema.get("type")
    if isinstance(kind, list):
        return union([ts_type({**schema, "type": k}, indent) for k in kind])
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        if "prefixItems" in schema:
            items = [ts_type(s, indent) for s in schema["prefixItems"]]
            return f"[{', '.join(items)}]"
        return f"{wrapped(ts_type(schema.get('items'), indent))}[]"
    if kind == "object" or "properties" in schema:
        return object_type(schema, indent)
    return "unknown"


def object_type(schema: dict[str, Any], indent: str) -> str:
    properties = schema.get("properties") or {}
    extra = schema.get("additionalProperties", True)
    if not properties:
        value = "unknown" if extra is True or extra == {} else (
            "never" if extra is False else ts_type(extra, indent))
        return f"{{ [key: string]: {value} }}"
    inner = indent + "  "
    lines = ["{"]
    lines.extend(property_lines(schema, inner))
    if extra not in (True, False, {}, None):
        lines.append(f"{inner}[key: string]: {ts_type(extra, inner)}")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def property_lines(schema: dict[str, Any], indent: str) -> list[str]:
    required = set(schema.get("required") or ())
    lines: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        lines.extend(doc_comment(prop.get("description") if isinstance(prop, dict) else None,
                                 indent))
        optional = "" if name in required else "?"
        lines.append(f"{indent}{property_key(name)}{optional}: {ts_type(prop, indent)}")
    return lines


def declaration(name: str, schema: dict[str, Any]) -> str:
    lines = doc_comment(schema.get("description"), "")
    ident = type_name(name)
    plain_object = (schema.get("type") == "object" and schema.get("properties")
                    and schema.get("additionalProperties", True) in (True, False, {}))
    if plain_object:
        lines.append(f"export interface {ident} {{")
        lines.extend(property_lines(schema, "  "))
        lines.append("}")
    else:
        lines.append(f"export type {ident} = {ts_type(schema)}")
    return "\n".join(lines)


def render(document: dict[str, Any]) -> str:
    schemas: dict[str, Any] = (document.get("components") or {}).get("schemas") or {}
    names = sorted(schemas)
    idents = [type_name(n) for n in names]
    clashes = sorted({i for i in idents if idents.count(i) > 1})
    if clashes:
        raise SystemExit(f"two schemas map to one TypeScript name: {clashes}")
    blocks = [HEADER.rstrip("\n")]
    blocks.extend(declaration(name, schemas[name]) for name in names)
    return "\n\n".join(blocks) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if the committed file is stale")
    args = parser.parse_args(argv)
    text = render(openapi_document())
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.is_file() else ""
        if current != text:
            print(f"{OUTPUT.relative_to(REPO)} is stale; regenerate it: {REGENERATE}",
                  file=sys.stderr)
            return 1
        print(f"{OUTPUT.relative_to(REPO)} is current")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text)
    print(f"wrote {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
