"""`GET /v1/schedule-types` serves every default a create would store (live QA AUT-12, 2026-10-11).

Pydantic leaves a `default_factory` out of a model's JSON schema, so the
console drew issue-sweep's `labels_exclude` as an empty field while the
server stored `security`, `needs-owner`, `wontfix` and `question` on create.
`scheduletypes.params_schema` serves the factory's value as the property's
`default`; the console reads it and shows what a blank field means.

No cloud and no emulator: the catalogue is pure.
"""

from __future__ import annotations

from swarm_api import scheduletypes
from swarm_api.scheduletypes import TYPES, describe, params_schema


def _type(name: str) -> scheduletypes.ScheduleType:
    return next(t for t in TYPES if t.name == name)


def test_issue_sweep_serves_its_default_exclusions() -> None:
    props = describe(_type("issue-sweep"))["params_schema"]["properties"]
    # MUTATION: serve `model_json_schema()` as it is and both keys lose `default`.
    assert props["labels_exclude"]["default"] == ["security", "needs-owner", "wontfix", "question"]
    assert props["labels_include"]["default"] == []


def test_every_served_default_is_what_create_stores() -> None:
    for entry in TYPES:
        schema = params_schema(entry.params_model)
        if "spec" in (schema.get("required") or []):
            continue  # custom-prompt: no create without a spec, so no all-default model.
        stored = entry.params_model().model_dump(mode="json")
        for name, prop in schema.get("properties", {}).items():
            assert "default" in prop, f"{entry.name}.{name} serves no default"
            assert prop["default"] == stored[name], f"{entry.name}.{name}"


def test_a_scalar_default_is_left_as_pydantic_wrote_it() -> None:
    props = params_schema(_type("issue-sweep").params_model)["properties"]
    assert props["max_new_per_firing"]["default"] == 3
