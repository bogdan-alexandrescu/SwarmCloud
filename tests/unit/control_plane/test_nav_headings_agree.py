"""The heading you land on must be the label of the tab you clicked.

THE DEFECT. Seven of the console's sixteen routes headed their screen with a
different name from the nav entry that opened it. Clicking **Pools** produced a
page headed *Capacity*; **Timeline** produced *Activity*; **Holders** produced
*Capacity holders*; **Pool limits** produced *Admin settings*; **New agent** and
**New workflow** produced *Submit a task* and *Submit a workflow*; and the
utility button **Reference** produced *API surface*.

WHY THAT IS NOT COSMETIC. A reader cannot tell a rename from a redirect. Landing
on a heading you did not click leaves three readings open -- you mis-navigated,
the app moved you, or Pools and Capacity are two different things -- and nothing
on the page settles it. It also breaks every external reference: a runbook step
"go to Pools" names something that is not on the screen when you arrive, and the
back button walks you through pages whose names you never saw.

WHY THIS IS A TEST AND NOT AN AUDIT. The seven were found by reading the source
once. A one-off reading does not cover the eighth -- a route added next month
whose author writes a tab label in App.tsx and a `title=` in a screen file
fifteen hundred lines away and never sees both at once. So this walks the route
table itself: every `case` in `SectionBody`, every tab in `SECTIONS`, and the
utility route, each resolved to the heading its screen actually renders. A new
route is covered the moment it is added, and a route with no case -- or a case
with no tab -- fails here rather than rendering "No such pane" in front of
somebody.

IT READS THE SHIPPED SOURCE, deliberately, the way test_runtimes_screen.py does.
A fixture listing the expected names would be a third copy of the thing whose
two copies already disagreed, and it would keep passing while the app drifted
away from it.

WHAT IT DOES NOT CHECK. Whether a name is a GOOD name. `Screen` renders
`title` into the `<h1>`, and this asserts the two strings are equal -- it cannot
tell you that "Timeline" beats "Activity". That judgement is recorded, per pair,
in a comment beside the value that changed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "apps/swarm-ui/src"
APP_TSX = SRC / "App.tsx"

#: `Screen` (Shell.tsx) renders its `title` prop as the page's `<h1>`, so a
#: screen's heading is either that prop or, for the three screens that draw
#: their own header, a literal `<h1>`. Named here so that a screen which starts
#: rendering its heading some third way fails loudly instead of being skipped.
HEADING_SOURCES = ("<Screen title=", "<h1>")


def _read(path: Path) -> str:
    # A missing or empty file must not skip and must not pass. If the UI is
    # renamed out from under this test, that is the silent hole it exists to
    # close -- and a skip is indistinguishable from a pass in CI output.
    assert path.is_file(), f"{path} is not present; this test would check nothing"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} is empty; this test would check nothing"
    return text


def _strip_line_comments(text: str) -> str:
    """Drop whole-line `//` comments only.

    Not `/* */`, and never a `//` that follows code on the same line: the
    section notes in App.tsx are line comments and they quote route ids in
    prose, while nothing in the arrays this parses puts a trailing comment
    after a value.
    """
    return "\n".join("" if line.lstrip().startswith("//") else line for line in text.splitlines())


def _balanced(text: str, start: int, open_ch: str, close_ch: str) -> str:
    """The slice from `start` (which must be `open_ch`) to its matching close."""
    assert text[start] == open_ch, f"expected {open_ch!r} at {start}"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced {open_ch!r} from offset {start}")


def _resolve(app: str, token: str) -> str:
    """A JSX child that is `{IDENT}` resolves to that module constant's value.

    The Reference entry deliberately uses one constant in both places, so the
    button and the heading cannot drift apart at all. This test still reads
    both sites rather than trusting that -- it just has to follow the name.
    """
    ident = re.fullmatch(r"\{\s*([A-Za-z_$][\w$]*)\s*\}", token)
    if ident is None:
        return token
    m = re.search(rf"^const {re.escape(ident.group(1))} = '([^']*)'", app, re.M)
    assert m is not None, f"{token} is not a module-level string constant in App.tsx"
    return m.group(1)


# --------------------------------------------------------------------------
# The route table, read out of App.tsx


def _sections(app: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """`SECTIONS` as (section id, section label, [(tab id, tab label)])."""
    decl = "const SECTIONS: SectionDef[] = "
    # Past the whole declaration, not `index("[", anchor)`: `SectionDef[]`
    # carries a bracket pair of its own and matching from there yields "[]".
    anchor = app.index(decl) + len(decl)
    block = _balanced(app, app.index("[", anchor), "[", "]")
    block = _strip_line_comments(block)

    out: list[tuple[str, str, list[tuple[str, str]]]] = []
    cursor = 0
    # Every section ends in a `tabs: [...]`, so each tab list closes one
    # section and the id/label before it belong to that section. Walking the
    # text this way survives reformatting, which an indentation rule would not.
    for m in re.finditer(r"tabs:\s*\[", block):
        head = block[cursor : m.start()]
        tabs_block = _balanced(block, block.index("[", m.start()), "[", "]")
        sid = re.findall(r"\bid:\s*'([^']+)'", head)
        slabel = re.findall(r"\blabel:\s*'([^']+)'", head)
        assert sid and slabel, f"a section in SECTIONS has no id/label: {head!r}"
        tabs = re.findall(r"\{\s*id:\s*'([^']+)',\s*label:\s*'([^']+)'", tabs_block)
        assert tabs, f"section {sid[-1]!r} declares no tabs"
        out.append((sid[-1], slabel[-1], tabs))
        cursor = m.start() + len(tabs_block)
    assert out, "SECTIONS parsed to nothing"
    return out


def _switch_cases(app: str) -> dict[str, str]:
    """`SectionBody`'s switch, as {"section/tab": "ScreenComponent"}."""
    body = app[app.index("function SectionBody(") :]
    cases = re.findall(r"case '([\w-]+/[\w-]+)':\s*\n\s*return <(\w+)", body)
    assert cases, "no `case '<section>/<tab>'` found in SectionBody"
    return dict(cases)


def _import_map(app: str) -> dict[str, Path]:
    """Component name -> the file App.tsx imports it from."""
    out: dict[str, Path] = {}
    for names, module in re.findall(r"import \{([^}]*)\} from '\./([\w.]+)'", app):
        for name in (n.strip() for n in names.split(",")):
            if name and not name.startswith("type "):
                out[name] = SRC / f"{module}.tsx"
    return out


# --------------------------------------------------------------------------
# The heading a screen actually renders


def _component_body(text: str, component: str) -> str:
    """The source of one component, up to the next top-level declaration.

    `export` is optional: the screens live in their own modules and are
    exported, but `ReferenceScreen` is declared and rendered inside App.tsx and
    is never exported.
    """
    m = re.search(rf"^(?:export )?function {re.escape(component)}\(", text, re.M)
    assert m is not None, f"{component} is not a top-level function in that file"
    rest = text[m.end() :]
    nxt = re.search(r"^(?:export )?(?:function|const|interface|type) ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _heading_of(component: str, imports: dict[str, Path], app: str) -> str:
    """The `<h1>` this screen puts on the page."""
    path = imports.get(component, APP_TSX)
    text = app if path == APP_TSX else _read(path)
    body = _component_body(text, component)

    # `<Screen ... title="X">` first: Shell.tsx renders that prop as the h1,
    # and it is how thirteen of the sixteen screens set their heading. Other
    # `title=` props on this screen are tooltips, so the prop is only read
    # after a `<Screen` tag has been seen.
    screen = body.find("<Screen")
    if screen != -1:
        m = re.search(r'title=(?:"([^"]*)"|(\{[^}]*\}))', body[screen:])
        assert m is not None, f"{component} renders <Screen> with no title prop"
        return _resolve(app, m.group(1) if m.group(1) is not None else m.group(2))

    m = re.search(r"<h1>(.*?)</h1>", body, re.S)
    assert m is not None, (
        f"{component} renders neither {HEADING_SOURCES[0]!r} nor {HEADING_SOURCES[1]!r}, "
        "so this test cannot see the heading it puts on the page"
    )
    return _resolve(app, m.group(1).strip())


# --------------------------------------------------------------------------


def _routes() -> list[tuple[str, str, str]]:
    """Every reachable route as (hash, the label that opens it, component)."""
    app = _read(APP_TSX)
    cases = _switch_cases(app)
    imports = _import_map(app)

    rows: list[tuple[str, str, str]] = []
    for sid, _slabel, tabs in _sections(app):
        for tid, tlabel in tabs:
            route = f"{sid}/{tid}"
            assert route in cases, (
                f"tab {tlabel!r} points at {route}, which SectionBody has no case for: "
                "clicking it renders the 'No such pane' panel"
            )
            rows.append((route, tlabel, cases[route]))

    # The utility route. Not a section -- it is one button beside the section
    # bar -- so it is named here rather than discovered, and its label and its
    # screen are read from the source exactly like every row above.
    ref = re.search(r"const REFERENCE = '([^']+)'", app)
    assert ref is not None, "REFERENCE is not declared in App.tsx"
    util = app[app.index('className="ctl-nav-util"') :]
    label = re.search(r">\s*(\{[^}]*\}|[^<>{}]+?)\s*</button>", util)
    assert label is not None, "could not read the utility button's label"
    rows.append((ref.group(1), _resolve(app, label.group(1)), "ReferenceScreen"))

    assert len(rows) == len(cases) + 1, (
        f"SectionBody has {len(cases)} cases but the tabs reach {len(rows) - 1}: "
        "a case no tab points at is a screen nobody can open from the nav"
    )
    return rows


ROUTES = _routes()


@pytest.mark.parametrize("route,label,component", ROUTES, ids=[r[0] for r in ROUTES])
def test_the_heading_matches_the_tab_that_opens_it(
    route: str, label: str, component: str
) -> None:
    """Click a tab, land on a page headed with that tab's own words."""
    app = _read(APP_TSX)
    heading = _heading_of(component, _import_map(app), app)
    assert heading == label, (
        f"#{route}: the nav entry says {label!r} and {component} heads the page "
        f"{heading!r}. A reader cannot tell whether they navigated wrong, whether "
        f"the app moved them, or whether those are two different things -- and a "
        f"runbook saying 'go to {label}' names nothing on the screen it lands on. "
        f"Pick whichever of the two is the better name and use it on BOTH sides."
    )


def test_every_route_is_covered() -> None:
    """The table is the whole nav, not a sample of it.

    Guards the parser rather than the app: a regex that quietly stopped
    matching would empty ROUTES and every parametrised case above would vanish
    from the run without failing anything -- collecting nothing looks exactly
    like passing.
    """
    assert len(ROUTES) >= 16, f"only {len(ROUTES)} routes parsed out of App.tsx"
    assert len({r[0] for r in ROUTES}) == len(ROUTES), "a route is listed twice"
    # The panes the redesign note names, so a section silently losing its tabs
    # is a failure here and not a shorter table nobody looks at.
    for expected in ("overview/now", "pools/pools", "history/timeline", "admin/limits"):
        assert expected in {r[0] for r in ROUTES}, f"{expected} is not in the route table"
