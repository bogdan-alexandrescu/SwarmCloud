"""Reads of the served OpenAPI document, one named path at a time.

WHY A HELPER. The test selectors (scripts/lib/select_unit_tests.py, and
scripts/lib/related_tests.py through it) keep a test reached through an app
that mounts a changed router only when its text requests one of that router's
URLs or walks every route the app mounts; a test whose own text calls the
app's OpenAPI builder counts as walking every route (_ENUMERATES_ROUTES).

The wire-shape tests (test_wire_types.py, test_artifacts_tab_field_contract.py,
test_outcomes_rows.py, test_runtimes_screen.py) read only the paths they name,
as string literals the selectors match, plus the component schemas of
swarm_api.wire_types models. Calling the builder in their own text made a
change to any router -- routes/budget_settings.py, which documents none of
those paths or models -- reach all four, which took that route's selection
over the one-run weight cap so it fell back to the capped list
(test_related_tests.py::test_the_script_selects_one_swarm_api_routes_tests_and_none_of_the_workers).

`operation` takes the path to read, so nothing here walks the paths. A test
that does walk every route calls the builder in its own text, as the
credential and admin surface guards do, and stays selected for every router.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _document(client: TestClient) -> dict[str, Any]:
    return client.app.openapi()


def operation(client: TestClient, path: str, method: str = "get") -> dict[str, Any]:
    """The operation the served document gives `method path`; KeyError when it
    documents none, so a missing route fails the test that named it."""
    return _document(client)["paths"][path][method.lower()]


def has_path(client: TestClient, path: str) -> bool:
    """Whether the served document has `path` at all."""
    return path in _document(client)["paths"]


def component_schemas(client: TestClient) -> dict[str, Any]:
    """The document's component schemas by name (the generated types' source)."""
    return _document(client)["components"]["schemas"]
