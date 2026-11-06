"""Which NARROW callers may reach a route, declared where the route is.

A narrow caller is an identity that may call a few named routes and nothing
else: the rollup sweeper (`auth.ROLLUP_SWEEPER_ROUTES`), the schedule tick
(`auth.SCHEDULE_TICK_ROUTES`) and a continuation-scoped account
(`auth.CONTINUATION_ROUTES`). Those three sets are the gate `current_auth`
applies; this module is what the guards that hold them read.

WHY THE DECLARATION SITS ON THE ROUTE. The guards
(tests/unit/control_plane/test_rollup_sweeper_is_narrow.py,
test_schedule_tick.py, test_continuation_scope_is_narrow.py) used to compare
the gate against one hand-edited literal list each. Two changes that each
added a route and passed against their own base could then fail together
(BGD's pick, 6 November 2026: a whole-tree check two green changes must pass
together). Now each route's own file says who may reach it, and the guards
derive their allow-sets from those declarations, so two changes that each add
a declared route touch no shared line and pass together.

THE RULE, default-deny as the gate itself is:
  * `reachable_by(*callers)` on a handler, under its `@router.<method>(...)`
    line, names exactly the narrow callers that route admits (none, when
    called with none: an exception to a router default);
  * `router_default(router, *callers)`, once after the router is built,
    names the callers every route of that router admits unless the route
    says otherwise;
  * a route with neither is reachable by none of the narrow callers.

A declaration grants nothing at run time: the gate is still auth.py's sets,
and the guards hold each set equal to what the routes declare. It only
records the decision next to the route, where review sees it.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable, Iterable, TypeVar

ROLLUP_SWEEPER = "rollup_sweeper"
SCHEDULE_TICK = "schedule_tick"
CONTINUATION = "continuation"

#: Every narrow caller a declaration may name. A name outside it is refused
#: when the route module is imported, so a misspelt caller fails loudly
#: instead of silently admitting nobody.
NARROW_CALLERS: frozenset[str] = frozenset({ROLLUP_SWEEPER, SCHEDULE_TICK, CONTINUATION})

#: The attribute a declaration sets: on a handler function, or on a router.
ATTRIBUTE = "__swarm_narrow_callers__"

F = TypeVar("F", bound=Callable[..., Any])


def _checked(callers: Iterable[str]) -> frozenset[str]:
    named = frozenset(callers)
    unknown = sorted(named - NARROW_CALLERS)
    if unknown:
        raise ValueError(
            f"unknown narrow caller(s) {', '.join(unknown)}; "
            f"a route may name only {', '.join(sorted(NARROW_CALLERS))}"
        )
    return named


def reachable_by(*callers: str) -> Callable[[F], F]:
    """Declare the narrow callers one route admits; returns the handler unchanged."""
    named = _checked(callers)

    def declare(handler: F) -> F:
        setattr(handler, ATTRIBUTE, named)
        return handler

    return declare


def router_default(router: Any, *callers: str) -> None:
    """Declare the narrow callers every route of `router` admits by default."""
    setattr(router, ATTRIBUTE, _checked(callers))


def callers_of(router: Any, route: Any) -> frozenset[str]:
    """The narrow callers `route` (an APIRoute of `router`) admits: its own
    declaration, else its router's default, else none."""
    own = getattr(route.endpoint, ATTRIBUTE, None)
    if own is not None:
        return own
    return getattr(router, ATTRIBUTE, frozenset())


def route_routers() -> list[Any]:
    """The `router` of every module under `swarm_api.routes`, in name order:
    the same modules the guards sweep."""
    from . import routes as routes_package

    names = sorted(m.name for m in pkgutil.iter_modules(routes_package.__path__))
    modules = [importlib.import_module(f"{routes_package.__name__}.{name}") for name in names]
    return [module.router for module in modules if getattr(module, "router", None) is not None]


def declared(caller: str, routers: Iterable[Any] | None = None) -> frozenset[tuple[str, str]]:
    """Every (HTTP method, route template) the routers declare `caller` may reach.

    The template is the declaring router's `route.path`, the string
    `current_auth` compares at request time.
    """
    from fastapi.routing import APIRoute

    (caller,) = _checked((caller,))
    found: set[tuple[str, str]] = set()
    for router in route_routers() if routers is None else routers:
        for route in router.routes:
            if isinstance(route, APIRoute) and caller in callers_of(router, route):
                found.update((method, route.path) for method in route.methods)
    return frozenset(found)
