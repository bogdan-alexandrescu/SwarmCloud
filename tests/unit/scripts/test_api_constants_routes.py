"""The `routes` constants group, against the objects it reads (part of #72).

`scripts/api_constants/routes.py` writes `generated/constants/routes.json`:
swarm-api's routes, the console links its codec builds and the outcome
ledger's sections. The console's `contract.routes.test.ts` holds its requests
and its router to that file; these tests hold the file to the API, so the two
ends meet in one committed list. That the committed file is what the group
builds now is `test_api_constants.py`'s, for every group.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/gen_api_types.py"

_spec = importlib.util.spec_from_file_location("gen_api_types_routes", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _routes() -> dict:
    return json.loads((gen.constants_dir() / "routes.json").read_text())


def test_every_method_and_path_of_the_openapi_document_is_listed_once():
    document = gen.openapi_document()
    expected = {
        (method.upper(), path)
        for path, item in document["paths"].items()
        for method in item
        if method in ("get", "put", "post", "delete", "patch", "options", "head", "trace")
    }
    listed = [(r["method"], r["path"]) for r in _routes()["api_routes"]]
    assert len(listed) == len(set(listed))
    assert set(listed) == expected


def test_every_route_the_accounts_router_serves_is_listed():
    """The routes the console's accounts screens call (the seam that broke
    three times in one day) are in the list the console is held to."""
    from swarm_api.routes.accounts import router

    listed = {(r["method"], r["path"]) for r in _routes()["api_routes"]}
    served = {(method, route.path) for route in router.routes for method in route.methods}
    assert served, "the accounts router serves nothing; this check would compare nothing"
    assert served <= listed, sorted(served - listed)


def test_the_outcome_sections_are_the_ones_the_route_accepts():
    from swarm_api.outcomes import SECTIONS

    assert _routes()["outcome_sections"] == list(SECTIONS)


def test_the_console_links_are_what_the_codec_builds():
    from swarm_api.codec import agent_console_url, run_console_url, workflow_console_url

    origin = "https://console.example.test"
    data = _routes()
    for kind, builder, placeholder in (
        ("agent", agent_console_url, "{task_id}"),
        ("run", run_console_url, "{run_id}"),
        ("workflow", workflow_console_url, "{workflow_id}"),
    ):
        assert builder(origin, "x_1") == origin + data["console_links"][kind].replace(placeholder, "x_1")
    for kind, example in data["console_link_examples"].items():
        builder = {"run": run_console_url, "workflow": workflow_console_url}[kind]
        assert builder(origin, example["id"]) == origin + example["path"]
