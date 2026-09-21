"""`GET /v1/runtimes` existed and nothing rendered it.

THE DEFECT CLASS, in its other direction. This repository keeps producing seams
built at both ends with nothing in the middle -- swarm-api had no broker URL,
the worker had no broker URL, the browser asked for `/v1/accounts/authorize` and
swarm-api did not serve it. `test_every_path_the_ui_calls_exists_on_this_api` in
test_account_api.py guards one direction: a path the client calls that the
server does not have.

`/v1/runtimes` was the mirror image. The route was written, tested field for
field against the frozen catalogue, and then no screen read it -- so the facts it
publishes stayed exactly where they were before it existed: in
`swarm_common.profiles`, which the person calling the API over HTTP does not
have. A route with no reader fails silently forever, because nothing about it
looks broken.

These tests pin the wiring end to end and the one property the screen must keep:

  * the route is read by the client, the loader is used by a screen, and the
    router reaches that screen -- three links, and a chain that is missing any
    of them renders nothing while every component looks finished;
  * every `/v1` path the client calls is served, which is the accounts seam test
    widened to the whole router set rather than to one prefix;
  * the TypeScript type is field-for-field what the route sends. Nothing else in
    this repository holds TypeScript to the Python -- `check-contract-parity.sh`
    reads shell and jq and no `.ts` file at all -- and this is the type whose
    drift would silently blank a column;
  * the screen and its fixture restate NO frozen catalogue value. That is the
    whole reason the route was written instead of a table in a `.ts` file, and a
    fixture is a copy nothing ever compares.

Everything here reads the shipped client source deliberately, the same way the
accounts seam test does. A mock would agree with whatever the screen happens to
do.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, Backend

from .conftest import auth_header

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "apps/swarm-ui/src"
API_TS = SRC / "api.ts"
APP_TSX = SRC / "App.tsx"
TYPES_TS = SRC / "types.ts"

ROUTE = "/v1/runtimes"

#: The routers `create_app` mounts. Named rather than discovered so that a
#: router added and never mounted is a failure here instead of a quiet pass --
#: discovering them from the app object would make this test agree with whatever
#: main.py happens to do.
ROUTER_MODULES = ("platform", "tasks", "workflows", "tenants", "admin", "accounts")


def _src(path: Path) -> str:
    # A missing client file must not skip or pass. The UI is checked in, and a
    # rename that this test cannot follow is exactly the silent hole it exists
    # to close -- test_account_api.py's seam test skips on a missing api.ts, and
    # a skip is indistinguishable from a pass in CI output.
    assert path.is_file(), f"{path} is not present; this test would check nothing"
    text = path.read_text()
    assert text.strip(), f"{path} is empty; this test would check nothing"
    return text


def _loader_reading(path: str) -> str:
    """The name of the exported loader in api.ts whose body performs `path`.

    Found rather than hard-coded, so renaming the loader does not turn this into
    a test of a name nobody uses any more.
    """
    api = _src(API_TS)
    # Every loader in this file is `export async function <name>(`, and the next
    # `\nexport ` ends its body for the purposes of "which one reads this path".
    chunks = api.split("\nexport ")
    hits = []
    for chunk in chunks:
        if f"'{path}'" not in chunk:
            continue
        match = re.match(r"async function (\w+)", chunk)
        if match:
            hits.append(match.group(1))
    assert len(hits) == 1, (
        f"expected exactly one exported loader in api.ts to read {path}, found {hits}. "
        "Two readers of one route drift; none means nothing renders it."
    )
    return hits[0]


def test_the_runtimes_route_is_read_by_a_screen_the_router_reaches():
    """THE REGRESSION. Before this, all three links were absent.

    A route nothing calls, a loader no screen imports, or a screen no route
    reaches all produce the same thing -- a finished component that no user can
    ever see -- and none of them fails anywhere.
    """
    loader = _loader_reading(ROUTE)

    screens = {
        path.stem: text
        for path in sorted(SRC.glob("*.tsx"))
        if loader in (text := path.read_text()) and path != APP_TSX
    }
    assert screens, (
        f"api.ts exports {loader}() and no screen imports it. "
        f"{ROUTE} would be read by nothing."
    )

    # The component the screen exports, so the check against App.tsx is about
    # the thing actually rendered rather than about the file name.
    exported: list[str] = []
    for text in screens.values():
        exported += re.findall(r"export function (\w+Screen)\b", text)
    assert exported, "the screen reading this route exports no *Screen component"

    app = _src(APP_TSX)
    rendered = [name for name in exported if f"<{name} " in app or f"<{name}/>" in app or f"<{name} />" in app]
    assert rendered, (
        f"App.tsx renders none of {exported}. The screen exists and no address "
        "reaches it, which is the state this route was already in."
    )
    for name in rendered:
        assert f"import {{ {name} }}" in app, f"{name} is rendered in App.tsx and not imported"


def test_the_screen_is_reachable_by_a_hash_that_resolves_to_it():
    """A screen wired into the switch and into no SECTIONS entry is unreachable.

    App.tsx routes on `${sectionId}/${tab}`, so the case label in SectionBody
    must correspond to a section id and a tab id that the nav actually declares
    -- otherwise the only way to reach it is to type a hash nobody has been told
    about, and the nav's own fallback sends a mistyped hash somewhere else.
    """
    app = _src(APP_TSX)
    loader = _loader_reading(ROUTE)
    screen = next(
        name
        for path in sorted(SRC.glob("*.tsx"))
        if loader in path.read_text()
        for name in re.findall(r"export function (\w+Screen)\b", path.read_text())
    )

    case = re.search(rf"case '([\w-]+)/([\w-]+)':\s*\n\s*return <{screen} ?/>", app)
    assert case, f"App.tsx has no `case '<section>/<tab>'` returning <{screen} />"
    section_id, tab_id = case.group(1), case.group(2)

    section = re.search(rf"id: '{section_id}',\s*\n\s*label: '([^']+)'", app)
    assert section, (
        f"SectionBody routes '{section_id}/{tab_id}' and SECTIONS declares no "
        f"section with id '{section_id}'. Nothing in the nav leads there."
    )
    assert f"id: '{tab_id}'" in app, (
        f"SECTIONS declares section '{section_id}' and no tab '{tab_id}'; the "
        "section would open on a pane that renders the 'No such pane' state."
    )


def test_every_v1_path_the_ui_calls_is_served_by_this_api():
    """The accounts seam test, widened from one prefix to the whole router set.

    Written because the screen under test adds a call to a route in a DIFFERENT
    router than the one that test covers, so the guard that caught the last
    three occurrences of this bug would not have caught a fourth here.
    """
    import importlib

    served: set[str] = set()
    for name in ROUTER_MODULES:
        module = importlib.import_module(f"swarm_api.routes.{name}")
        for route in module.router.routes:
            path = getattr(route, "path", None)
            if path:
                # Parameter NAMES differ between the client's template hole and
                # the server's declaration; the shape is what has to match.
                served.add(re.sub(r"\{[^}]*\}", "{}", path))
    assert served, "no routes were collected; this test would compare nothing"

    api = _src(API_TS)
    called: set[str] = set()
    for raw in re.findall(r"""['"`](/v1/[^'"`]*)['"`]""", api):
        # A query string is not part of the path the router declares.
        path = raw.split("?")[0]
        called.add(re.sub(r"(\$\{[^}]*\}|\{[^}]*\})", "{}", path))
    assert ROUTE in called, f"{ROUTE} is not among the paths api.ts calls"

    missing = sorted(called - served)
    assert not missing, (
        f"the UI calls paths this API does not serve: {missing}. "
        "Both ends exist and the seam does not."
    )


def _interface_fields(source: str, name: str) -> list[str]:
    """Top-level field names of one exported TS interface, in order."""
    marker = f"export interface {name} {{"
    assert marker in source, f"{marker!r} is not in types.ts; this check is vacuous"
    body = source[source.index(marker) :]
    end = body.index("\n}\n")
    fields = re.findall(r"^  (\w+)\??:", body[:end], flags=re.MULTILINE)
    assert fields, f"no fields parsed out of interface {name}; the check is vacuous"
    return fields


def test_the_typescript_type_is_field_for_field_what_the_route_sends(client):
    """The only thing in this repository holding a TypeScript shape to the API.

    `docs/contract-change-requests.md` measures the cost of that gap: two of the
    five type surfaces it audits have already drifted, and neither drift is
    visible anywhere -- `check-contract-parity.sh` reads no `.ts` file and no
    workflow runs `tsc`. A field renamed on the route and not here does not fail
    to compile; it renders as `undefined`, which this UI prints as an em dash.
    A column that quietly reads "not measured" for every row is precisely the
    failure-as-absence this whole client was written to make impossible.
    """
    served = client.get(ROUTE, headers=auth_header("alice")).json()["runtimes"]
    assert served, "the route served no runtimes; this test would compare nothing"
    entry = next(iter(served.values()))

    types = _src(TYPES_TS)
    assert set(_interface_fields(types, "Runtime")) == set(entry), (
        "types.ts `Runtime` and the /v1/runtimes payload disagree about their fields"
    )
    assert set(_interface_fields(types, "ResourceClassSpec")) == set(entry["resources"]), (
        "types.ts `ResourceClassSpec` and the payload's nested `resources` disagree"
    )


#: Every string whose presence in the client would be a restatement of the
#: frozen catalogue: the runtime names, the class names, and the dispatch
#: targets. A screen that recognises any of them by name has a per-name special
#: case in it, and a catalogue that gains an entry gains a wrong row.
#:
#: `Backend.AUTO` is deliberately included. It is a declaration rather than a
#: dispatch target, so a screen never has to name it: the card prints whatever
#: the payload's `backend` says, and the copy beside it explains the idea
#: without spelling the token.
FROZEN_NAMES = (
    set(RUNNER_PROFILES)
    | set(RESOURCE_CLASSES)
    | {b.value for b in Backend}
)


def _screen_path() -> Path:
    loader = _loader_reading(ROUTE)
    hits = [p for p in sorted(SRC.glob("*.tsx")) if loader in p.read_text() and p != APP_TSX]
    assert len(hits) == 1, f"expected one screen reading {ROUTE}, found {hits}"
    return hits[0]


def test_the_screen_restates_no_value_from_the_frozen_catalogue():
    """The reason `/v1/runtimes` was written instead of a table in a `.ts` file.

    Not a style rule. `RESOURCE_UNITS` in types.ts is this copy already made
    once, and it survives only because the numbers in it have not changed
    yet. The route exists so that the screen can hold none of them; this asserts
    it holds none of them.
    """
    path = _screen_path()
    text = path.read_text()

    named = sorted(n for n in FROZEN_NAMES if re.search(rf"\b{re.escape(n)}\b", text))
    assert not named, (
        f"{path} names frozen catalogue entries {named}. Every value on that "
        "screen has to come from the response, or a resized class silently "
        "renders the old figure."
    )

    # A hand-written sizing record. Reading `r.resources.cpu` is property
    # access; writing `cpu: 4` is the copy.
    built = sorted(set(re.findall(r"\b(cpu|memory_gib|disk_gib|units)\s*:\s*[\d.]", text)))
    assert not built, (
        f"{path} constructs a sizing record ({built}). Sizes are served, joined "
        "to the runtime by the route, and are never assembled here."
    )


def _fixture_block() -> str:
    """The source of the development runtimes fixture, on its own.

    A slice that silently came back empty would make both tests below pass
    while reading nothing, which is the shape of defect this file is about --
    so the marker and the size are both asserted.
    """
    api = _src(API_TS)
    marker = "const FIXTURE_RUNTIMES"
    assert marker in api, f"{marker} is not in api.ts; this check would read nothing"
    start = api.index(marker)
    end = api.index("\n}\n", start)
    block = api[start:end]
    assert len(block) > 400, "the fixture block was located and is too small to be it"
    return block


def test_the_development_fixture_is_not_a_copy_of_the_frozen_catalogue():
    """A fixture is a copy that nothing ever compares.

    apps/swarm-ui/README.md says fixtures here are real, and every other one is.
    This one must not be: the values it would have to hold to look real are
    exactly the frozen catalogue, in TypeScript, in the file that reads the
    route written to stop that copy existing. The names in it are invented on
    purpose, and this is what keeps somebody from helpfully correcting them.
    """
    block = _fixture_block()

    # The dispatch targets are display strings here and carry no risk of a wrong
    # number, so only the NAMES that would have to carry sizes are forbidden.
    named = sorted(
        n
        for n in set(RUNNER_PROFILES) | set(RESOURCE_CLASSES)
        if re.search(rf"\b{re.escape(n)}\b", block)
    )
    assert not named, (
        f"the runtimes fixture names real catalogue entries {named}. It would "
        "then be a copy of the frozen sizes in TypeScript, which is the drift "
        "check-contract-parity.sh cannot see."
    )


def test_the_fixture_exercises_the_branches_live_data_never_would(client):
    """A development mode that exercises fewer paths than production hides bugs.

    Two of this screen's branches cannot be reached from the shipped catalogue
    at all: no profile declares a backend the platform has to resolve, so the
    declared-vs-resolved split would ship having never been drawn. The fixture
    is where those get looked at, so the properties that make it useful are
    pinned rather than left to survive an edit.
    """
    block = _fixture_block()

    declared = re.findall(r"backend: '([^']+)', resolved_backend: '([^']+)'", block)
    assert declared, "no backend pairs parsed out of the fixture; this check is vacuous"
    assert any(d != r for d, r in declared), (
        "no fixture runtime declares one backend and resolves to another, so the "
        "declared/resolved split is drawn by nothing in development -- and "
        "nothing in the shipped catalogue draws it either"
    )
    assert len({r for _, r in declared}) > 1, (
        "every fixture runtime resolves to the same backend, so the topology "
        "panel never groups in development"
    )
    assert "provider: null" in block, (
        "no fixture runtime is provider-free, so the 'none needed' credential "
        "branch is never seen"
    )
    assert "secrets_any_of: true" in block and "secrets_any_of: false" in block, (
        "the fixture does not cover both credential shapes; 'any one of' versus "
        "'all of' is the distinction this panel exists to make"
    )

    # And the live route really does serve a catalogue where the split is
    # invisible, which is why the fixture has to carry it.
    served = client.get(ROUTE, headers=auth_header("alice")).json()["runtimes"]
    assert all(r["backend"] == r["resolved_backend"] for r in served.values()), (
        "a shipped profile now declares a backend the platform resolves; this "
        "test's premise has changed and the fixture note should be revisited"
    )


def test_the_screen_reads_every_field_the_route_publishes():
    """A published field no screen reads is the same gap, one field wide.

    `secrets_any_of` is the example that matters: the route serves it because a
    credential list without it tells a tenant who pays for a subscription to buy
    metered access as well. A screen that rendered `secrets` and dropped the
    flag would reproduce that refusal in a browser.
    """
    path = _screen_path()
    text = path.read_text()

    fields = _interface_fields(_src(TYPES_TS), "Runtime")
    unread = [f for f in fields if not re.search(rf"\.{re.escape(f)}\b", text)]
    assert not unread, (
        f"{path} never reads {unread}, which /v1/runtimes publishes. A field "
        "served and not shown is the route's gap moved one layer up."
    )


def test_the_screen_never_coalesces_a_failed_read_to_a_number():
    """This platform's defining bug, in the one place this screen could repeat it.

    Two of the three reads behind this screen are allowed to fail on their own,
    and both feed number columns. `pool?.active ?? 0` in a cell would render a
    capacity read that never completed as a backend sitting at zero units --
    which is what an idle backend looks like, and the reader has no way to tell
    them apart.

    A default of zero that is genuinely a zero is still allowed; it just has to
    be written as a branch that says which zero it is, the way the rest of this
    screen does, rather than as an operator that turns every absence into one.
    """
    path = _screen_path()
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"(\?\?|\|\|)\s*0\b", line)
    ]
    assert not offenders, (
        f"{path} coalesces a possibly-absent value to 0:\n  " + "\n  ".join(offenders)
    )


def _component(source: str, name: str) -> str:
    """One top-level component's source, on its own.

    Same vacuity guard as `_fixture_block`: a slice that silently came back
    empty would make the assertion below pass while reading nothing.
    """
    marker = f"function {name}("
    assert marker in source, (
        f"{marker!r} is not in the screen. If it was renamed, rename it here "
        "too -- a check that cannot find its subject must fail, not pass."
    )
    start = source.index(marker)
    end = source.index("\n}\n", start)
    body = source[start:end]
    assert len(body) > 200, f"{name} was located and is too small to be it"
    return body


def test_interchangeable_credentials_are_distinguished_where_they_are_rendered():
    """`secrets` without `secrets_any_of` is worse than no list at all.

    The route publishes the flag because a runtime may take EITHER metered API
    access OR a subscription token; a panel that lists both names and drops the
    flag tells a tenant who already pays for one to buy the other. That is the
    refusal the flag exists in the frozen catalogue to prevent, reproduced in a
    browser.

    Asserted against the component that renders the credential rather than
    against the file, because the file reads the flag in its comparison lines
    too -- and a screen that used it only to say "one of 2 credentials" in a
    footnote while the credential row still read as a conjunction would pass a
    whole-file check and be exactly as wrong.
    """
    text = _screen_path().read_text()
    credential = _component(text, "Credential")
    assert "secrets_any_of" in credential, (
        "the credential panel does not branch on secrets_any_of, so a runtime "
        "that takes any ONE of its credentials is rendered as needing all of them"
    )


@pytest.mark.parametrize("scale", [1, 3])
def test_a_catalogue_that_gains_entries_needs_no_edit_here(client, monkeypatch, scale):
    """The property the screen's comparisons depend on.

    Every 'what sets it apart' line is computed across the response, so the
    screen has to keep working for a catalogue it has never seen. This asserts
    the API half of that -- the route publishes whatever the frozen catalogue
    holds -- which is what makes the client half safe to write without a name
    in it.
    """
    from swarm_common.profiles import RunnerProfile

    for n in range(scale):
        name = f"probe-{n}"
        monkeypatch.setitem(
            RUNNER_PROFILES,
            name,
            RunnerProfile(
                name=name,
                image=f"probe-image-{n}",
                resource_class=next(iter(RESOURCE_CLASSES)),
                backend=Backend.CLOUD_RUN_JOB,
                command=("python", "-m", "agent_worker.runners.mock"),
            ),
        )

    served = client.get(ROUTE, headers=auth_header("alice")).json()["runtimes"]
    for n in range(scale):
        assert f"probe-{n}" in served, "the route did not publish an added profile"
        assert served[f"probe-{n}"]["image"] == f"probe-image-{n}"
