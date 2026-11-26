"""The `routes` group: the API's paths, the console links it builds, and the
outcome ledger's sections.

  * `api_routes`: every method and path template of swarm-api's OpenAPI
    document (`create_app().openapi()["paths"]`), built offline by
    `scripts/gen_api_types.py`'s own `openapi_document()`, sorted by path then
    method. The console's request functions are held to this list by
    `apps/swarm-ui/src/__tests__/contract.routes.test.ts`, so a path the
    console calls and the API does not serve fails there, not on a probe of
    the deployed service.
  * `console_links`: the console path each link builder in `swarm_api.codec`
    returns (the API is the one source of a console link), its id replaced by
    a named placeholder; `console_link_examples`: what each builder returns
    for an id that needs encoding (the run and workflow links), so the console
    test holds the spelling too.
  * `outcome_sections`: `swarm_api.outcomes.SECTIONS`, the values
    `GET /v1/outcomes` accepts for `section`, in their order.

Read from live objects; never from source text.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_GENERATOR = Path(__file__).resolve().parents[1] / "gen_api_types.py"


def _generator() -> ModuleType:
    """`scripts/gen_api_types.py`, loaded by path (its module level imports
    the standard library alone), so this group builds the OpenAPI document
    exactly as the generator does instead of restating how."""
    name = "_api_constants_gen_api_types"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, _GENERATOR)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


#: What building the OpenAPI document reads (the generator's own `INPUTS`,
#: less the generated types file, which is an output, not an input of this
#: group), and this module.
INPUTS = (
    "scripts/api_constants/routes.py",
    *(path for path in _generator().INPUTS if not path.startswith("apps/swarm-ui/")),
)

#: The HTTP methods an OpenAPI path item may carry; any other key of a path
#: item (`parameters`, `summary`) is not a route.
_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

#: A plain id each link builder is called with; replaced by the placeholder.
_SAMPLE_ID = "sampleid0"

#: An id that needs encoding: a space, a slash and encodeURIComponent's
#: unreserved punctuation, which Python's quote() escapes by default.
_AWKWARD_ID = "a b/c(d)!"


def _link_builders() -> dict[str, tuple[Any, str]]:
    from swarm_api.codec import agent_console_url, run_console_url, workflow_console_url

    return {
        "agent": (agent_console_url, "task_id"),
        "run": (run_console_url, "run_id"),
        "workflow": (workflow_console_url, "workflow_id"),
    }


#: Any origin: the builders put it before the path, and it is cut off again.
_ORIGIN = "https://console.example.test"


def _path(builder: Any, ident: str) -> str:
    url = builder(_ORIGIN, ident)
    if not isinstance(url, str) or not url.startswith(_ORIGIN + "/"):
        raise SystemExit(f"{builder.__name__} built {url!r}, not a link under {_ORIGIN}")
    return url[len(_ORIGIN):]


def console_links() -> dict[str, str]:
    """Each builder's path, its id replaced by `{<name>_id}`."""
    links = {}
    for kind, (builder, placeholder) in _link_builders().items():
        path = _path(builder, _SAMPLE_ID)
        if path.count(_SAMPLE_ID) != 1:
            raise SystemExit(f"{builder.__name__} built {path!r}: the id must appear once")
        links[kind] = path.replace(_SAMPLE_ID, "{" + placeholder + "}")
    return links


#: The links whose id is caller-chosen text and so is URI-encoded. A task id
#: is minted by the API (`task_<hex>`) and its link carries it as it is.
_ENCODED_LINKS = ("run", "workflow")


def console_link_examples() -> dict[str, dict[str, str]]:
    """Each encoding builder's path for an id that needs encoding."""
    builders = _link_builders()
    return {
        kind: {"id": _AWKWARD_ID, "path": _path(builders[kind][0], _AWKWARD_ID)}
        for kind in _ENCODED_LINKS
    }


def api_routes(document: dict[str, Any]) -> list[dict[str, str]]:
    """Every (method, path) of an OpenAPI document, sorted by path then method."""
    routes = [
        {"method": method.upper(), "path": path}
        for path, item in document["paths"].items()
        for method in item
        if method in _METHODS
    ]
    return sorted(routes, key=lambda r: (r["path"], r["method"]))


def build() -> dict[str, Any]:
    from swarm_api.outcomes import SECTIONS

    return {
        "api_routes": api_routes(_generator().openapi_document()),
        "console_links": console_links(),
        "console_link_examples": console_link_examples(),
        "outcome_sections": list(SECTIONS),
    }
