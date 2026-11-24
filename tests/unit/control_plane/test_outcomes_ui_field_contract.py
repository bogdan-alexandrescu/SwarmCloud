"""The Timeline's contract with `GET /v1/outcomes`, held without reading the console (part of #72).

WHY THIS FILE. The route (archived pull request 196) and the page (archived
pull request 197) were written in two lanes at once, against a contract
neither could run. A renamed or retyped field does not fail to compile in the
browser -- it renders as `undefined`, which the page draws as an em dash or,
worse, as the absence-means-zero the honesty rules forbid.

This file once parsed `apps/swarm-ui/src/outcomes.ts` as text. The shapes are
now GENERATED: `swarm_api.wire_types.Outcomes` documents the route, the
console's types alias the generated ones, and the facts moved as follows:

  * every field, both directions, at every depth, and nothing vacuous --
    `test_wire_types.py::test_every_outcomes_payload_is_exactly_the_model_at_every_depth`;
  * the unions the page switches on --
    `test_wire_types.py::test_the_outcomes_unions_are_exactly_the_routes_values`;
  * the parameters the page sends -- the OpenAPI document in
    `test_wire_types.py::test_the_outcomes_route_declares_every_parameter_the_timeline_sends`,
    and the request itself in the console's `contract.shapes.test.ts`;
  * the page's choices, default span and limits -- the generated
    `constants/outcomes.json` (drift-checked), equal to the page's in
    `contract.shapes.test.ts`, and equal to the route's here.

What stays here is about the API alone: the route still imports, and the
constants group the console reads is the route's own values.

Offline: no cloud credentials, no emulator.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _outcomes_group():
    """`scripts/api_constants/outcomes.py`, loaded by path."""
    path = REPO / "scripts/api_constants/outcomes.py"
    spec = importlib.util.spec_from_file_location("api_constants_outcomes_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_generated_choices_defaults_and_limits_are_the_routes():
    """What `outcomes.ts` once restated, the generated file now carries; this
    holds the group to the live values the route validates with."""
    from swarm_api import outcomes as module

    data = _outcomes_group().build()
    assert data["spans"] == list(module.SPANS)
    assert data["default_span"] == module.DEFAULT_SPAN
    assert data["default_span"] in data["spans"]
    assert data["buckets"] == list(module.BUCKETS)
    assert data["kinds"] == list(module.KINDS)
    assert data["groups"] == list(module.GROUPS)
    assert data["row_outcomes"] == list(module.ROW_OUTCOMES)
    for name in ("MAX_BUCKETS", "MONTH_MIN_DAYS", "MAX_SPAN_DAYS", "OUTCOMES_CACHE_S"):
        assert data[name.lower()] == getattr(module, name), name


def test_every_query_parameter_is_declared_on_the_route_function():
    """The OpenAPI document's parameters are the route function's `Query`
    declarations; this keeps the two readings of "declared" one."""
    from fastapi.params import Query

    from swarm_api.routes import outcomes as routes

    declared = {
        name
        for name, p in inspect.signature(routes.get_outcomes).parameters.items()
        if isinstance(p.default, Query)
    }
    assert {"tz", "span", "since", "until", "bucket", "scope", "tenant", "exclude_tenant",
            "profile", "submitted_by", "kind", "group", "compare", "section"} <= declared
