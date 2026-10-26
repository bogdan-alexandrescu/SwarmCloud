"""QAENV-CRAWL (docs/design/qa-environments.md §3.1, §3.5, §4, §10; part of
#2136): `swarm_qa capture --crawl` discovers while it captures.

BGD, 2026-10-23: discovery runs inside every capture step (Q3 (a)), the
crawl may click disclosure controls only (Q4 (a)), and a step captures at
most `found_per_step` new routes, 8 by default (§10, not the design's 4).
These are the row's red-first tests in §8:

* `capture --crawl` harvests links from the pages it captured;
* the owner of a path is the same in every step for a given G, and a step
  never captures a route it does not own, nor past its cap;
* `found.json` is §3.5's shape and lists `not_owned` and `over_cap`;
* `--hash-routes` is on in a crawl: `#/x` is a route, `#x` is not;
* a control inside a form, with an exclusion word, or of `type=submit` is
  never clicked;
* a click the gate refused marks the control `write_attempting`, and it is
  never clicked again;
* a `reveal` with a Playwright selector is refused;
* the crawl stops at its time bound and says so;
* `found_per_step` defaults to 8.

No Chromium and no network: test_swarm_qa's fake page, extended with the
controls a page draws and what clicking each does, a fake clock for the
crawl's time bound and navigation spacing, and a fake of the gate's refusal
record. The page's own scripts (LINKS_JS, CONTROLS_JS, CLICK_JS) run under
node against a small DOM of this file's own.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from test_swarm_qa import FakeBrowser, FakeContext, FakePage, _manifest, _node, qa, world  # noqa: F401  (fixtures)

APP = "https://app.example.test"


# --------------------------------------------------------------------------
# A page with controls, a clock, a gate
# --------------------------------------------------------------------------


class _Keyboard:
    def __init__(self, log):
        self.log = log

    def press(self, key):
        self.log.append(("key", key))


class CrawlPage(FakePage):
    """FakePage plus what the crawl reads: `controls[path]` (CONTROLS_JS's
    facts), `effects[(path, text)]` (what clicking that control does:
    `links` it reveals, a `url` the router pushes, `refuse` a write the gate
    refuses, `text` True when the section's drawn text changes), and links
    drawn only in the dark theme (`dark_links`)."""

    def __init__(self, module, clock, gate):
        super().__init__(module)
        self.clock_ = clock
        self.gate = gate
        self.controls: dict[str, list[dict]] = {}
        self.effects: dict[tuple[str, str], dict] = {}
        self.dark_links: dict[str, list[str]] = {}
        self.revealed: list[str] = []
        self.drawn = 0
        self.goto_seconds = 0.0
        self.keyboard = _Keyboard(self.log)

    def goto(self, url, **kw):
        self.clock_.t += self.goto_seconds
        self.log.append(("at", self.clock_.t))
        self.revealed = []
        self.drawn = 0
        return super().goto(url, **kw)

    def evaluate(self, expression, arg=None):
        path = self._path()
        if expression == getattr(self.qa, "LINKS_JS", None):
            self.evaluated.append(expression)
            dark = (self.media or {}).get("color_scheme") == "dark"
            extra = self.dark_links.get(path, []) if dark else []
            return list(self.links.get(path, [])) + list(extra) + list(self.revealed)
        if expression == getattr(self.qa, "CONTROLS_JS", None):
            self.evaluated.append(expression)
            return [dict(c) for c in self.controls.get(path, [])]
        if expression == getattr(self.qa, "CLICK_JS", None):
            self.evaluated.append(expression)
            self.log.append(("click", path, arg["text"], self.clock_.t))
            effect = self.effects.get((path, arg["text"]), {})
            self.revealed.extend(effect.get("links", []))
            if effect.get("text"):
                self.drawn += 1
            if effect.get("refuse"):
                self.gate.append({"n": len(self.gate) + 1, "at": time.time(), "method": "POST",
                                  "origin": APP, "path": "/api/things", "code": "qa_write_refused"})
            if effect.get("url"):
                # A router push: the address changes, no document loads.
                self.url = effect["url"]
            return True
        if expression == getattr(self.qa, "TEXT_JS", None):
            self.evaluated.append(expression)
            return [24, format(self.drawn, "08x"), None]
        return super().evaluate(expression, arg)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def crawl(world, qa, monkeypatch):
    """`world` with a CrawlPage, a fake clock and gate, and no robots.txt."""
    clock = Clock()
    gate: list[dict[str, Any]] = []
    page = CrawlPage(qa, clock, gate)
    world.page = page
    world.clock = clock
    world.gate = gate
    world.robots = None
    monkeypatch.setattr(qa, "_connect", lambda: (world.playwright, FakeBrowser(), FakeContext(page)))
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": list(gate),
                                                       "dropped": 0})
    monkeypatch.setattr(qa, "_now", clock.now, raising=False)
    monkeypatch.setattr(qa, "_sleep", clock.sleep, raising=False)
    monkeypatch.setattr(qa, "_fetch_robots", lambda url: world.robots)
    world.stored = {"format": 1, "groups": {"g1": [{"id": "a", "path": "/a", "ready": "body"}],
                                            "g2": [{"id": "b", "path": "/b", "ready": "body"}]}}
    return world


def _run(qa, w, *extra, group="g1", step="1", steps="2", stored=None, themes="light"):
    routes = w.tmp / "routes.json"
    routes.write_text(json.dumps(stored if stored is not None else w.stored), encoding="utf-8")
    out = w.tmp / f"capture-{group}-{step}"
    bundle = w.tmp / f"capture-{group}-{step}.tar"
    code = qa.main(["capture", "--routes", str(routes), "--group", group, "--base", APP,
                    "--presets", "desktop", "--themes", themes, "--settle", "generic", "--keep-storage",
                    "--out", str(out), "--bundle", str(bundle),
                    "--crawl", "--step", step, "--steps", steps, *extra])
    found_file = out / "found.json"
    found = json.loads(found_file.read_text(encoding="utf-8")) if found_file.exists() else None
    return code, found, bundle


def _owner(path: str, steps: int) -> int:
    """§3.1's rule, restated: the test holds the tool to the design's formula."""
    return 1 + int(hashlib.sha256(path.encode("utf-8")).hexdigest()[:8], 16) % steps


def _paths_owned_by(step: int, steps: int, n: int, prefix: str = "/p") -> list[str]:
    out, i = [], 0
    while len(out) < n:
        path = f"{prefix}{i}"
        if _owner(path, steps) == step:
            out.append(path)
        i += 1
    return out


def _captured_paths(bundle: Path) -> list[str]:
    return sorted({r["path"] for r in _manifest(bundle)["routes"]})


def _clicks(page) -> list[tuple[str, str]]:
    return [(e[1], e[2]) for e in page.log if e[0] == "click"]


def _control(text: str, **facts) -> dict:
    """One CONTROLS_JS fact: a disclosure button unless `facts` say otherwise."""
    return {"css": f"body > button.{abs(hash(text)) % 1000}", "text": text, "label": "", "tag": "button",
            "type": "button", "role": "", "expanded": "false", "haspopup": None, "in_form": False,
            "href": None, "excluded": False, "visible": True, **facts}


# --------------------------------------------------------------------------
# The row's red-first tests
# --------------------------------------------------------------------------


def test_found_per_step_defaults_to_8(qa):
    # MUTATION: keep the design's 4 and BGD's §10 answer is not what runs.
    assert qa.FOUND_PER_STEP == 8
    args = qa._parser().parse_args(["capture", "--routes", "r", "--group", "g1", "--base", APP,
                                    "--out", "o", "--bundle", "b", "--crawl", "--step", "1", "--steps", "1"])
    # MUTATION: a parser default other than FOUND_PER_STEP and the CLI runs another cap.
    assert args.found_per_step == 8
    # MUTATION: raise the ceiling past 8 and a step may capture more than §3.1 allows.
    assert qa.MAX_FOUND_PER_STEP == 8


def test_a_crawl_captures_eight_owned_new_routes_by_default(qa, crawl):
    owned = _paths_owned_by(1, 1, 10)
    crawl.page.links = {"/": [APP + p for p in owned]}
    code, found, bundle = _run(qa, crawl, step="1", steps="1",
                               stored={"format": 1, "groups": {"g1": [{"id": "home", "path": "/", "ready": "body"}]}})
    # MUTATION: exit non-zero on a crawl with images and the step fails its attempt.
    assert code == 0
    # MUTATION: default to 4 and only four new routes are captured.
    assert len(found["captured_new"]) == 8, found["captured_new"]
    # MUTATION: drop owned routes past the cap and the next run never has them stored.
    assert [r["path"] for r in found["over_cap"]] == owned[8:]
    # MUTATION: capture an over-cap route and the manifest holds more than the cap.
    assert _captured_paths(bundle) == sorted(["/", *owned[:8]])


def test_capture_crawl_harvests_links_from_the_pages_it_captured(qa, crawl):
    # /hidden is linked from /a only in the dark theme (a menu drawn only
    # there): the capture shoots dark then light, and the crawl runs after
    # it, in light, so only the capture's own read of /a shows the link.
    crawl.page.dark_links = {"/a": [APP + "/hidden"]}
    code, found, _bundle = _run(qa, crawl, step="1", steps="1", themes="dark,light",
                                stored={"format": 1, "groups": {"g1": [{"id": "a", "path": "/a", "ready": "body"}]}})
    # MUTATION: let the harvest raise and the capture exits non-zero.
    assert code == 0
    new = [r["path"] for r in found["captured_new"] + found["over_cap"] + found["not_owned"]]
    # MUTATION: read links only on the crawl's own loads and /hidden is never found.
    assert "/hidden" in new, found


def test_the_owner_of_a_path_is_the_designs_hash_the_same_in_every_step(qa):
    paths = [f"/route/{i}" for i in range(200)] + ["/", "/#/active", "/a b", "/Docs/Intro"]
    for steps in range(1, 9):
        for path in paths:
            # MUTATION: any other hash, slice or offset and two steps disagree with the design.
            assert qa.owner(path, steps) == _owner(path, steps), (path, steps)
    # Every step 1..G is somebody's: the rule spreads paths over all of them.
    # MUTATION: drop the `1 +` and step G owns nothing while a step 0 does.
    assert {qa.owner(p, 4) for p in paths} == {1, 2, 3, 4}


def test_two_steps_over_one_graph_capture_disjoint_shares_and_list_the_rest_as_not_owned(qa, crawl):
    one, two = _paths_owned_by(1, 2, 3), _paths_owned_by(2, 2, 3)
    crawl.page.links = {"/": [APP + p for p in one + two], "/a": [APP + "/"], "/b": [APP + "/"]}
    _code, found1, bundle1 = _run(qa, crawl, group="g1", step="1", steps="2")
    _code, found2, bundle2 = _run(qa, crawl, group="g2", step="2", steps="2")
    new1 = {r["path"] for r in found1["captured_new"]}
    new2 = {r["path"] for r in found2["captured_new"]}
    # MUTATION: capture every new route found and both steps capture all six.
    assert new1 == set(one) | ({"/"} if _owner("/", 2) == 1 else set()), found1
    # MUTATION: capture every new route found and step 2 captures step 1's share too.
    assert new2 == set(two) | ({"/"} if _owner("/", 2) == 2 else set()), found2
    # MUTATION: a step-dependent owner rule and both steps capture one route.
    assert not new1 & new2
    # MUTATION: drop not_owned and the report cannot say what a step saw and left.
    assert {r["path"] for r in found1["not_owned"]} == new2
    # MUTATION: list only one side's not-owned routes and the report misses the other's.
    assert {r["path"] for r in found2["not_owned"]} == new1
    # MUTATION: drop the owner and the report cannot say who should have captured it.
    assert all(r["owner"] == 2 for r in found1["not_owned"])
    # MUTATION: capture a not-owned route and it is in this step's manifest.
    assert not set(two) & set(_captured_paths(bundle1))
    # MUTATION: capture a not-owned route and it is in step 2's manifest.
    assert not set(one) & set(_captured_paths(bundle2))
    # Each step still captures its own stored routes, and only its own.
    # MUTATION: capture every stored route of the run and step 1 shoots /b, step 2's.
    assert "/a" in _captured_paths(bundle1) and "/b" not in _captured_paths(bundle1)


def test_a_step_never_captures_past_its_cap_and_lists_the_rest_over_cap(qa, crawl):
    owned = _paths_owned_by(1, 2, 5)
    crawl.page.links = {"/": [APP + p for p in owned]}
    stored = {"format": 1, "groups": {"g1": [{"id": "home", "path": "/", "ready": "body"}]}}
    _code, found, bundle = _run(qa, crawl, "--found-per-step", "2", stored=stored)
    owned_new = [r["path"] for r in found["captured_new"]]
    # MUTATION: ignore --found-per-step and all five are captured.
    assert owned_new == owned[:2], found
    # MUTATION: drop routes past the cap and the next run never has them stored.
    assert [r["path"] for r in found["over_cap"]] == owned[2:]
    # MUTATION: capture an over-cap route and it is in the manifest.
    assert _captured_paths(bundle) == sorted(["/", *owned[:2]])


def test_stored_plus_new_never_passes_sixty_a_leg(qa, crawl):
    stored_routes = [{"id": "home", "path": "/", "ready": "body"}]
    stored_routes += [{"id": f"s{i}", "path": f"/s{i}", "ready": "body"} for i in range(56)]
    owned = _paths_owned_by(1, 2, 6)
    crawl.page.links = {"/": [APP + p for p in owned]}
    stored = {"format": 1, "groups": {"g1": stored_routes[:8], "g2": stored_routes[8:]}}
    _code, found, _bundle = _run(qa, crawl, stored=stored)
    # 57 stored leave 3 screens for 2 steps: one new route each.
    # MUTATION: cap at found_per_step alone and step 1 takes all three.
    assert [r["path"] for r in found["captured_new"]] == owned[:1], found["captured_new"]
    # MUTATION: drop the routes past the 60-screen cut and they are never stored.
    assert [r["path"] for r in found["over_cap"]] == owned[1:3]


def test_found_json_is_the_designs_shape(qa, crawl):
    crawl.page.links = {"/": [APP + "/x", "https://other.example.test/y"]}
    _code, found, bundle = _run(qa, crawl)
    # MUTATION: any other key set and the report and QAENV-SETTLE read nothing.
    assert set(found) == {"format", "stored", "captured_new", "not_owned", "over_cap", "skipped", "crawl"}
    # MUTATION: another format number and QAENV-SETTLE refuses the file.
    assert found["format"] == 1
    # MUTATION: list only this step's stored routes and the owner rule's graph is not the run's.
    assert found["stored"] == ["/a", "/b"]
    # MUTATION: rename a skip reason and the coverage section reads none of it.
    assert set(found["skipped"]) == {"off_origin", "outside_base", "excluded", "robots", "http_error",
                                     "failed", "write_attempting"}
    # MUTATION: stop counting off-origin links and the coverage section says none were seen.
    assert found["skipped"]["off_origin"] == 1
    # MUTATION: another crawl record and the report cannot say why the crawl stopped.
    assert set(found["crawl"]) == {"pages", "depth", "seconds", "stopped_by"}
    # MUTATION: a stop reason outside §3.5's four and the settle cannot read it.
    assert found["crawl"]["stopped_by"] in ("done", "pages", "depth", "time")
    for entry in found["captured_new"] + found["over_cap"] + found["not_owned"]:
        # MUTATION: drop a route's id, path or depth and QAENV-SETTLE cannot store it.
        assert {"id", "path", "depth"} <= set(entry)
    # MUTATION: write it outside the capture directory and the tarball the
    # report reads does not carry it.
    import tarfile
    with tarfile.open(bundle) as tar:
        # MUTATION: write found.json outside the capture directory and the tarball lacks it.
        assert "found.json" in tar.getnames()


def test_new_route_ids_are_stable_with_a_sha256_collision_suffix(qa):
    # MUTATION: route_id's counter and /a-b's id depends on what was found first.
    ids = qa.found_ids(["/a/b", "/a-b", "/c"], taken={"c"})
    suffix = hashlib.sha256(b"/a/b").hexdigest()[:6]
    # MUTATION: suffix with route_id's counter and the id depends on discovery order.
    assert ids["/a/b"] == f"a-b-{suffix}"
    # MUTATION: suffix only the second of two colliding paths and the first's id changes with order.
    assert ids["/a-b"] == "a-b-" + hashlib.sha256(b"/a-b").hexdigest()[:6]
    # A stored id is taken: the new route gets the suffix.
    # MUTATION: ignore the stored ids and a new route shares a stored route's id.
    assert ids["/c"] == "c-" + hashlib.sha256(b"/c").hexdigest()[:6]
    # MUTATION: make the id depend on order and a second step names it otherwise.
    assert qa.found_ids(["/c", "/a-b", "/a/b"], taken={"c"}) == ids
    # MUTATION: suffix every id and a path's id is not route_id's slug.
    assert qa.found_ids(["/x/y"], taken=set()) == {"/x/y": "x-y"}


def test_a_crawl_keeps_hash_routes_and_drops_anchors(qa, crawl):
    crawl.page.links = {"/": [APP + "/#/active", APP + "/#section", APP + "/#access_token=x"]}
    _code, found, _bundle = _run(qa, crawl, step="1", steps="1")
    paths = {r["path"] for r in found["captured_new"] + found["over_cap"]}
    # MUTATION: leave --hash-routes off in a crawl and #/active is never a route.
    assert "/#/active" in paths, found
    # MUTATION: keep any fragment and #section and a token are routes.
    assert not any("section" in p or "token" in p for p in paths)


def test_a_click_refusal_names_every_never_click_rule(qa):
    words = list(qa.DEFAULT_EXCLUDED_CONTROLS)
    origin = qa._origin(APP)
    # MUTATION: refuse every control and the crawl clicks nothing.
    assert qa.click_refusal(_control("Menu"), words, origin) is None
    # MUTATION: drop any one rule below and that control is clicked.
    assert qa.click_refusal(_control("Menu", in_form=True), words, origin)
    # MUTATION: drop the submit rule and a submit outside a form is clicked.
    assert qa.click_refusal(_control("Menu", type="submit"), words, origin)
    # MUTATION: drop the reset rule and a reset is clicked.
    assert qa.click_refusal(_control("Menu", type="reset"), words, origin)
    # MUTATION: drop the input rule and an input is clicked.
    assert qa.click_refusal(_control("Menu", tag="input"), words, origin)
    # MUTATION: drop the off-origin rule and a link to another site is clicked.
    assert qa.click_refusal(_control("More", tag="a", href="https://other.example.test/x",
                                     haspopup="menu"), words, origin)
    # MUTATION: drop the exclusion-selector rule and a control the target excluded is clicked.
    assert qa.click_refusal(_control("Menu", excluded=True), words, origin)
    for text in ("Delete project", "REMOVE", "Pay now", "Buy", "Purchase", "Send", "Invite people",
                 "Unsubscribe", "Cancel subscription", "Logout"):
        # MUTATION: match words exactly, or case-sensitively, and a destructive control is clicked.
        assert qa.click_refusal(_control(text), words, origin), text
    # The label counts as the control's text too.
    # MUTATION: read only the drawn text and an icon button labelled Delete is clicked.
    assert qa.click_refusal(_control("", label="Delete"), words, origin)
    # MUTATION: click any button and a plain action button is clicked.
    assert qa.click_refusal(_control("Save", expanded=None), words, origin)
    for facts in ({"role": "tab", "expanded": None}, {"haspopup": "menu", "expanded": None},
                  {"tag": "summary", "expanded": None}):
        # MUTATION: refuse a disclosure kind and its section is never reached.
        assert qa.click_refusal(_control("Open", **facts), words, origin) is None, facts


def test_a_crawl_never_clicks_a_form_control_an_excluded_word_or_a_submit(qa, crawl):
    crawl.page.controls = {"/": [
        _control("Filters", in_form=True), _control("Delete all"), _control("Go", type="submit"),
        _control("Account", label="Remove account"), _control("Docs", haspopup="menu", expanded=None),
    ]}
    crawl.page.effects = {("/", "Docs"): {"links": [APP + "/docs/intro"]}}
    _code, found, _bundle = _run(qa, crawl, "--exclude-control", "Account", step="1", steps="1")
    # MUTATION: skip click_refusal and Filters, Delete all and Go are clicked.
    assert _clicks(crawl.page) == [("/", "Docs")], _clicks(crawl.page)
    # A disclosure click's revealed links are harvested.
    # MUTATION: read links only once a page loads and a menu's links stay unseen.
    assert "/docs/intro" in {r["path"] for r in found["captured_new"] + found["over_cap"]}


def test_a_click_the_gate_refused_marks_the_control_write_attempting_and_it_is_never_clicked_again(qa, crawl):
    sneaky = _control("Options", css="header > button")
    crawl.page.links = {"/": [APP + "/next"]}
    crawl.page.controls = {"/": [sneaky], "/next": [sneaky]}
    crawl.page.effects = {("/", "Options"): {"refuse": True}}
    _code, found, _bundle = _run(qa, crawl, step="1", steps="1")
    # MUTATION: never read the gate after a click and Options is clicked on /next too.
    assert _clicks(crawl.page) == [("/", "Options")], _clicks(crawl.page)
    # MUTATION: drop the record and the coverage section cannot list it.
    assert found["skipped"]["write_attempting"] == [{"path": "/", "text": "Options"}]


def test_a_click_that_pushes_a_route_records_it_and_restores_the_page(qa, crawl):
    crawl.page.controls = {"/": [_control("Projects", haspopup="menu", expanded=None)]}
    crawl.page.effects = {("/", "Projects"): {"url": APP + "/projects/1"}}
    _code, found, _bundle = _run(qa, crawl, step="1", steps="1")
    # MUTATION: ignore a router push and /projects/1 is never a route.
    assert "/projects/1" in {r["path"] for r in found["captured_new"] + found["over_cap"]}


def test_a_tab_that_changes_its_section_is_recorded_as_a_reveal_state(qa, crawl):
    crawl.page.controls = {"/": [_control("Billing", role="tab", expanded=None)]}
    crawl.page.effects = {("/", "Billing"): {"text": True}}
    _code, found, _bundle = _run(qa, crawl, step="1", steps="1")
    states = [r for r in found["captured_new"] + found["over_cap"] if r.get("reveal")]
    # MUTATION: never record a tab's section and the Billing tab is in no screen.
    assert states and states[0]["path"] == "/" and states[0]["reveal"][0]["text"] == "Billing", found
    # A state's reveal is start-click form: plain CSS, exact text.
    # MUTATION: record a Playwright selector or an extra field and load_routes refuses the state.
    assert set(states[0]["reveal"][0]) <= {"text", "css"}


def test_clicks_are_bounded_at_twenty_a_page(qa, crawl):
    crawl.page.controls = {"/": [_control(f"Section {i}", css=f"body > button:nth-of-type({i + 1})")
                                 for i in range(30)]}
    _run(qa, crawl, step="1", steps="1")
    # MUTATION: no per-page bound and all thirty are clicked.
    assert len(_clicks(crawl.page)) == 20


def test_the_crawl_stops_at_its_time_bound_and_says_so(qa, crawl):
    crawl.page.goto_seconds = 40.0
    crawl.page.links = {"/": [APP + f"/p{i}" for i in range(20)]}
    for i in range(20):
        crawl.page.links[f"/p{i}"] = [APP + f"/p{i}/q"]
    _code, found, _bundle = _run(qa, crawl, "--crawl-seconds", "100", step="1", steps="1")
    # MUTATION: no time bound and the crawl walks all twenty pages.
    assert found["crawl"]["stopped_by"] == "time", found["crawl"]
    # MUTATION: walk on past the bound and all twenty pages load.
    assert found["crawl"]["pages"] < 20
    # MUTATION: accept a bound past 900 s and a step's crawl outlives its timeout.
    code, _found, _bundle = _run(qa, crawl, "--crawl-seconds", "901", step="1", steps="1")
    # MUTATION: accept a bound past 900 s and a step's crawl outlives its timeout.
    assert code == qa.EXIT_USAGE


def test_crawl_navigations_are_spaced_by_the_legs_spacing_and_never_under_a_second(qa, crawl):
    crawl.page.links = {"/": [APP + "/x", APP + "/y"], "/x": [], "/y": []}
    _run(qa, crawl, "--navigation-ms", "3000", step="1", steps="1")
    times = [e[1] for e in crawl.page.log if e[0] == "at"]
    # The capture's load of /a, then the crawl's five: /, /a, /b, /x, /y.
    crawl_times = times[1:6]
    gaps = [b - a for a, b in zip(crawl_times, crawl_times[1:])]
    # MUTATION: no spacing and the crawl's loads come back to back.
    assert len(gaps) == 4 and all(g >= 3.0 for g in gaps), (times, crawl.clock.slept)
    # MUTATION: drop the floor and a leg may ask the crawl to run faster than 1 s.
    code, _found, _bundle = _run(qa, crawl, "--navigation-ms", "999", step="1", steps="1")
    # MUTATION: drop the floor and a leg may ask the crawl to run faster than 1 s.
    assert code == qa.EXIT_USAGE


def test_a_crawl_honours_robots_txt_and_the_exclusions(qa, crawl):
    crawl.robots = "User-agent: *\nDisallow: /private\n"
    crawl.page.links = {"/": [APP + "/private/x", APP + "/logout", APP + "/admin/x", APP + "/ok"]}
    _code, found, _bundle = _run(qa, crawl, "--exclude", "/admin/*", step="1", steps="1")
    paths = {r["path"] for r in found["captured_new"] + found["over_cap"]}
    # MUTATION: skip robots in the crawl and /private/x is captured.
    assert paths == {"/", "/ok"}, paths
    # MUTATION: count robots or exclusion skips wrongly and the coverage section misreports them.
    assert found["skipped"]["robots"] == 1 and found["skipped"]["excluded"] == 2
    gotos = [e[1] for e in crawl.page.log if e[0] == "goto"]
    # MUTATION: load a disallowed or excluded path before refusing to list it.
    assert not any("/private" in g or "/logout" in g or "/admin" in g for g in gotos)


def test_load_routes_holds_reveal_to_the_start_click_rules(qa, tmp_path):
    def routes(reveal):
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"format": 1, "groups": {"g1": [
            {"id": "x", "path": "/x", "ready": "body", "reveal": reveal}]}}), encoding="utf-8")
        return path

    # MUTATION: refuse plain CSS and exact text and no tab state can be replayed.
    assert qa.load_routes(routes([{"text": "Billing", "css": "[role=\"tab\"]"}]))
    # MUTATION: accept Playwright syntax and the replay asks the page for a selector it cannot take.
    for css in ("button:has-text('Billing')", "text=Billing", "div >> button", "button:visible"):
        # MUTATION: accept Playwright syntax and the replay asks the page for a selector it cannot take.
        with pytest.raises(qa.UsageError):
            qa.load_routes(routes([{"text": "Billing", "css": css}]))
    # MUTATION: accept an exclusion word and a capture clicks Delete before it shoots.
    with pytest.raises(qa.UsageError):
        qa.load_routes(routes([{"text": "Delete workspace"}]))
    # MUTATION: no count bound and a route list may chain any number of clicks.
    with pytest.raises(qa.UsageError):
        qa.load_routes(routes([{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}]))
    # MUTATION: accept an empty reveal and a state is the same as its route.
    with pytest.raises(qa.UsageError):
        qa.load_routes(routes([]))
    # MUTATION: accept another key and a reveal smuggles fields the replay never checked.
    with pytest.raises(qa.UsageError):
        qa.load_routes(routes([{"text": "Billing", "selector": "x"}]))


def test_capture_replays_a_routes_reveal_before_it_shoots(qa, crawl):
    stored = {"format": 1, "groups": {"g1": [{"id": "billing", "path": "/settings", "ready": "body",
                                              "reveal": [{"text": "Billing", "css": "[role=\"tab\"]"}]}]}}
    _code, _found, bundle = _run(qa, crawl, "--crawl-seconds", "1", step="1", steps="1", stored=stored)
    log = crawl.page.log
    click = next(i for i, e in enumerate(log) if e[0] == "click" and e[1] == "/settings")
    shot = next(i for i, e in enumerate(log) if e[0] == "shot" and e[1].endswith("/settings"))
    # MUTATION: never replay reveal and the tab's section is in no image.
    assert click < shot, log
    # MUTATION: replay reveal under another id and the review names another screen.
    assert _manifest(bundle)["routes"][0]["id"] == "billing"


def test_the_standalone_discover_reads_the_new_sources_through_clicks(qa, crawl):
    crawl.page.controls = {"/": [_control("Docs", haspopup="menu", expanded=None)]}
    crawl.page.effects = {("/", "Docs"): {"links": [APP + "/docs/intro"]}}
    out = crawl.tmp / "screens.json"
    code = qa.main(["discover", "--base", APP, "--out", str(out)])
    # MUTATION: exit non-zero on a discovery that listed routes.
    assert code == 0
    document = qa.load_routes(out)
    paths = {r["path"] for routes in document["groups"].values() for r in routes}
    # MUTATION: leave discover without clicks and /docs/intro is not found.
    assert "/docs/intro" in paths, paths


def test_a_crawl_step_needs_a_step_and_steps(qa, crawl):
    routes = crawl.tmp / "routes.json"
    routes.write_text(json.dumps(crawl.stored), encoding="utf-8")
    base = ["capture", "--routes", str(routes), "--group", "g1", "--base", APP,
            "--out", str(crawl.tmp / "o"), "--bundle", str(crawl.tmp / "o.tar"), "--crawl"]
    # MUTATION: default the step and two steps both own every path.
    assert qa.main(base) == qa.EXIT_USAGE
    # MUTATION: accept a step past --steps and it owns nothing any step captures.
    assert qa.main([*base, "--step", "3", "--steps", "2"]) == qa.EXIT_USAGE
    # MUTATION: accept more steps than a leg has and a share is nobody's.
    assert qa.main([*base, "--step", "1", "--steps", "9"]) == qa.EXIT_USAGE


def test_a_crawl_step_with_no_stored_routes_crawls_from_the_base(qa, crawl):
    crawl.page.links = {"/": [APP + "/x"]}
    code, found, bundle = _run(qa, crawl, step="1", steps="1", stored={"format": 1, "groups": {}})
    # MUTATION: refuse an empty stored list and a target with only `discover` never runs.
    assert code == 0
    # MUTATION: list routes a run with no stored screen does not have.
    assert found["stored"] == []
    # MUTATION: capture nothing without stored routes and a target with only `discover` captures nothing.
    assert _captured_paths(bundle) == ["/", "/x"]


# --------------------------------------------------------------------------
# The page's own scripts, under node
# --------------------------------------------------------------------------

#: A DOM of elements built from `nodes` ({tag, attrs, text, parent, hidden}),
#: with the selectors the scripts use: tag, [attr], [attr=v], [attr~=v],
#: :nth-of-type(n), compound, `>` and descendant, and comma lists. Each
#: element logs what is dispatched on it.
_JS_DOM = r"""
const spec = JSON.parse(process.argv[1]);
const log = [];
const els = spec.nodes.map((n, i) => ({
  i, localName: n.tag, _attrs: Object.fromEntries(Object.entries(n.attrs || {}).map(([k, v]) => [k.toLowerCase(), v])),
  _text: n.text || '', _hidden: !!n.hidden, children: [], parentElement: null,
}));
spec.nodes.forEach((n, i) => {
  if (n.parent !== undefined) { els[i].parentElement = els[n.parent]; els[n.parent].children.push(els[i]); }
});
const contains = (a, b) => { for (let x = b; x; x = x.parentElement) if (x === a) return true; return false; };
const simple = (el, s) => {
  const m = s.match(/^([a-z0-9*]*)((?:\[[^\]]+\]|:nth-of-type\(\d+\))*)$/);
  if (!m) throw new SyntaxError('unsupported selector ' + s);
  if (m[1] && m[1] !== '*' && m[1] !== el.localName) return false;
  for (const part of m[2].match(/\[[^\]]+\]|:nth-of-type\(\d+\)/g) || []) {
    if (part.startsWith(':')) {
      const k = Number(part.match(/\d+/)[0]);
      const same = el.parentElement ? el.parentElement.children.filter((c) => c.localName === el.localName) : [el];
      if (same.indexOf(el) + 1 !== k) return false;
      continue;
    }
    const a = part.slice(1, -1).match(/^([\w-]+)(?:(~?)=["']?([^"']*)["']?)?$/);
    const v = el._attrs[a[1].toLowerCase()];
    if (v === undefined) return false;
    if (a[3] !== undefined && (a[2] === '~' ? !v.split(/\s+/).includes(a[3]) : v !== a[3])) return false;
  }
  return true;
};
const one = (el, sel) => {
  const tokens = sel.trim().replace(/\s*>\s*/g, ' > ').split(/\s+/);
  const match = (e, k) => {
    if (!simple(e, tokens[k])) return false;
    if (k === 0) return true;
    if (tokens[k - 1] === '>') return !!e.parentElement && match(e.parentElement, k - 2);
    for (let up = e.parentElement; up; up = up.parentElement) if (match(up, k - 1)) return true;
    return false;
  };
  return match(el, tokens.length - 1);
};
const all = (sel) => els.filter((el) => sel.split(',').some((s) => one(el, s)));
els.forEach((el) => {
  el.getAttribute = (k) => (el._attrs[k.toLowerCase()] === undefined ? null : el._attrs[k.toLowerCase()]);
  el.hasAttribute = (k) => el._attrs[k.toLowerCase()] !== undefined;
  el.matches = (s) => one(el, s);
  el.closest = (s) => { for (let x = el; x; x = x.parentElement) if (one(x, s)) return x; return null; };
  el.getClientRects = () => (el._hidden ? [] : [{}]);
  el.dispatchEvent = (e) => { log.push([el.i, e.type]); return true; };
  el.click = () => log.push([el.i, 'click']);
  Object.defineProperty(el, 'innerText', { get: () => (el._hidden ? '' : els.filter((o) => contains(el, o) && o._text)
    .map((o) => o._text).join(' ')) });
  Object.defineProperty(el, 'textContent', { get: () => els.filter((o) => contains(el, o) && o._text)
    .map((o) => o._text).join(' ') });
});
globalThis.window = globalThis;
class MouseEvent { constructor(type, init) { this.type = type; Object.assign(this, init || {}); } }
globalThis.MouseEvent = MouseEvent;
globalThis.PointerEvent = class extends MouseEvent {};
globalThis.document = { baseURI: spec.base, documentElement: els[0], body: els[1],
  querySelectorAll: all, querySelector: (s) => all(s)[0] || null };
const fn = (0, eval)('(' + process.argv[2] + ')');
Promise.resolve(fn(JSON.parse(process.argv[3]))).then((v) => process.stdout.write(JSON.stringify({ result: v, log })));
"""


def _js(expression: str, nodes: list[dict], arg=None) -> dict:
    dom = {"base": APP + "/app/", "nodes": [{"tag": "html"}, {"tag": "body", "parent": 0}, *nodes]}
    done = subprocess.run([_node(), "-e", _JS_DOM, json.dumps(dom), expression, json.dumps(arg)],
                          capture_output=True, text=True, timeout=30, check=False)
    # MUTATION: (harness) a DOM program that crashes passes as an empty answer.
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_links_js_reads_every_link_source_the_design_names_hidden_ones_included(qa):
    nodes = [
        {"tag": "a", "attrs": {"href": "/one"}, "parent": 1},
        {"tag": "area", "attrs": {"href": "two"}, "parent": 1},
        {"tag": "span", "attrs": {"role": "link", "href": "/three"}, "parent": 1},
        {"tag": "button", "attrs": {"routerlink": "/four"}, "parent": 1},
        {"tag": "div", "attrs": {"data-href": "/five"}, "parent": 1, "hidden": True},
        {"tag": "link", "attrs": {"rel": "next", "href": "/six"}, "parent": 1},
        {"tag": "link", "attrs": {"rel": "prefetch", "href": "/seven"}, "parent": 1},
        {"tag": "link", "attrs": {"rel": "prerender", "href": "/eight"}, "parent": 1},
        {"tag": "link", "attrs": {"rel": "stylesheet", "href": "/style.css"}, "parent": 1},
        {"tag": "span", "attrs": {"href": "/not-a-link"}, "parent": 1},
    ]
    got = set(_js(qa.LINKS_JS, nodes)["result"])
    # MUTATION: read only a[href] and every other source is lost.
    assert got == {APP + p for p in ("/one", "/app/two", "/three", "/four", "/five", "/six", "/seven", "/eight")}, got


def test_controls_js_reports_the_facts_each_never_click_rule_reads(qa):
    nodes = [
        {"tag": "form", "parent": 1},
        {"tag": "button", "attrs": {"aria-expanded": "false"}, "parent": 2},
        {"tag": "span", "text": "Filters", "parent": 3},
        {"tag": "button", "attrs": {"aria-haspopup": "menu", "type": "button"}, "text": "Menu", "parent": 1},
        {"tag": "div", "attrs": {"role": "tab", "class": "danger"}, "text": "Billing", "parent": 1},
        {"tag": "summary", "text": "More", "parent": 1, "hidden": True},
        {"tag": "p", "text": "plain", "parent": 1},
    ]
    facts = _js(qa.CONTROLS_JS, nodes, {"selectors": ["[class~=danger]"], "limit": 50})["result"]
    by_text = {f["text"]: f for f in facts}
    # MUTATION: report a control outside the disclosure kinds and `plain` is a candidate.
    assert set(by_text) == {"Filters", "Menu", "Billing", "More"}, facts
    # MUTATION: read in_form from anything but the form ancestor and a control in a form is clicked.
    assert by_text["Filters"]["in_form"] is True and by_text["Menu"]["in_form"] is False
    # MUTATION: misreport aria-haspopup or aria-expanded and a disclosure control is refused or an action clicked.
    assert by_text["Menu"]["haspopup"] == "menu" and by_text["Filters"]["expanded"] == "false"
    # MUTATION: skip the exclusion selector and a control the target excluded is clicked.
    assert by_text["Billing"]["role"] == "tab" and by_text["Billing"]["excluded"] is True
    # MUTATION: report hidden controls as drawn and the crawl clicks what has no box.
    assert by_text["More"]["visible"] is False and by_text["More"]["tag"] == "summary"
    # The css each answers with finds that control again.
    clicked = _js(qa.CLICK_JS, nodes, {"css": by_text["Menu"]["css"], "text": "Menu"})
    # MUTATION: a CSS path that does not find its control again and nothing is clicked.
    assert clicked["result"] is True
    # MUTATION: click() alone and a menu that opens on pointerdown never opens.
    assert [e[1] for e in clicked["log"] if e[0] == 5] == ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]


def test_click_js_refuses_a_control_in_a_form_and_one_whose_text_differs(qa):
    nodes = [
        {"tag": "form", "parent": 1},
        {"tag": "button", "attrs": {"aria-expanded": "false"}, "text": "Filters", "parent": 2},
        {"tag": "button", "attrs": {"type": "submit", "aria-expanded": "false"}, "text": "Go", "parent": 1},
        {"tag": "button", "attrs": {"role": "tab"}, "text": "Billing", "parent": 1},
    ]
    # MUTATION: drop the in-page form rule and a control in a form is clicked.
    assert _js(qa.CLICK_JS, nodes, {"css": "form > button", "text": "Filters"})["log"] == []
    # MUTATION: drop the in-page submit rule and a submit is clicked.
    assert _js(qa.CLICK_JS, nodes, {"css": "button", "text": "Go"})["log"] == []
    # MUTATION: click by css alone and a control that changed under the crawl is clicked.
    assert _js(qa.CLICK_JS, nodes, {"css": "[role=tab]", "text": "Billings"})["log"] == []
    # No css: the exact visible text names it (start-click form).
    # MUTATION: require css and a stored start-click-form state with text only is never replayed.
    assert _js(qa.CLICK_JS, nodes, {"text": "Billing"})["result"] is True


# --------------------------------------------------------------------------
# What the image restates from swarm-api, held equal
# --------------------------------------------------------------------------


def _qaspec_constant(name: str):
    """A constant of apps/swarm-api/swarm_api/qaspec.py, read without
    importing swarm-api (the image cannot either)."""
    import ast
    import re

    tree = ast.parse((Path(__file__).resolve().parents[3] / "apps" / "swarm-api" / "swarm_api" / "qaspec.py")
                     .read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            if isinstance(node.value, ast.Call):
                # re.compile(<pattern>): the pattern.
                return re.compile(ast.literal_eval(node.value.args[0]))
            return ast.literal_eval(node.value)
    raise AssertionError(f"qaspec has no {name}")


def test_the_reveal_rules_are_qaspecs_start_click_rules(qa):
    # MUTATION: let the image's Playwright pattern drift and a reveal swarm-api refuses is replayed.
    assert qa.PLAYWRIGHT_SELECTOR.pattern == _qaspec_constant("_PLAYWRIGHT_SELECTOR").pattern
    # MUTATION: drop a default excluded control from the image and the crawl clicks it.
    assert set(_qaspec_constant("DEFAULT_EXCLUDED_CONTROLS")) <= set(qa.DEFAULT_EXCLUDED_CONTROLS)
    # MUTATION: drop an exit from the crawl's words and it clicks Sign out.
    assert set(_qaspec_constant("START_CLICK_REFUSED")) <= set(qa.DEFAULT_EXCLUDED_CONTROLS)
    # MUTATION: another bound than the start clicks' and a state's replay differs from a sign-in's.
    assert (qa.MAX_REVEAL_CLICKS, qa.REVEAL_TEXT_MAX, qa.REVEAL_CSS_MAX) == (
        _qaspec_constant("MAX_START_CLICKS"), _qaspec_constant("START_CLICK_TEXT_MAX"),
        _qaspec_constant("START_CLICK_CSS_MAX"))
    # MUTATION: a screen cap above swarm-api's and a step captures past what a leg stores.
    assert qa.MAX_SCREENS == _qaspec_constant("MAX_SCREENS") and qa.MAX_STEPS == _qaspec_constant("MAX_GROUPS")


# --------------------------------------------------------------------------
# Dismissed routes (lane QA-CRAWL-SKIP; BGD 2026-10-25, part of #2157)
# --------------------------------------------------------------------------


def _dismissed(*keys: str) -> list[str]:
    """The route list's `dismissed`: each dismissed route's key as sha256 hex,
    the way qaspec.render writes it."""
    return sorted(hashlib.sha256(k.encode("utf-8")).hexdigest() for k in keys)


def _gotos(page) -> list[str]:
    return [e[1][len(APP):] for e in page.log if e[0] == "goto"]


def test_a_dismissed_route_is_neither_crawled_nor_captured(qa, crawl):
    crawl.page.links = {"/": [APP + "/keep", APP + "/gone"], "/gone": [APP + "/behind-gone"]}
    stored = {"format": 1, "groups": {"g1": [{"id": "home", "path": "/", "ready": "body"}]},
              "dismissed": _dismissed("/gone")}
    code, found, bundle = _run(qa, crawl, step="1", steps="1", stored=stored)
    assert code == 0
    listed = [r["path"] for r in found["captured_new"] + found["over_cap"] + found["not_owned"]]
    # MUTATION: ignore `dismissed` and /gone is listed and captured again.
    assert "/gone" not in listed and "/keep" in listed, found
    assert "/gone" not in _captured_paths(bundle)
    # MUTATION: list it and only drop it from the capture, and the walk still loads it.
    assert "/gone" not in _gotos(crawl.page)
    assert "/behind-gone" not in listed
    # MUTATION: skip it silently and the coverage section cannot say why it is absent.
    assert found["skipped"]["dismissed"] == 1


def test_a_dismissed_state_is_skipped_and_its_path_is_not(qa, crawl):
    crawl.page.controls = {"/": [_control("Billing", role="tab", expanded=None)]}
    crawl.page.effects = {("/", "Billing"): {"text": True}}
    stored = {"format": 1, "groups": {}, "dismissed": _dismissed("/" + qa.STATE_MARK + "Billing")}
    _code, found, _bundle = _run(qa, crawl, step="1", steps="1", stored=stored)
    # MUTATION: key a dismissal by its path alone and / itself is barred too.
    assert [r["path"] for r in found["captured_new"]] == ["/"], found
    # MUTATION: ignore a dismissed state and the Billing tab is captured again.
    assert not any(r.get("reveal") for r in found["captured_new"] + found["over_cap"])


def test_the_found_route_cap_counts_only_routes_not_dismissed(qa, crawl):
    owned = _paths_owned_by(1, 1, 5)
    crawl.page.links = {"/": [APP + p for p in owned]}
    stored = {"format": 1, "groups": {"g1": [{"id": "home", "path": "/", "ready": "body"}]},
              "dismissed": _dismissed(*owned[:2])}
    _code, found, bundle = _run(qa, crawl, "--found-per-step", "2", step="1", steps="1", stored=stored)
    # MUTATION: count the dismissed routes toward the cap and the two slots go to them.
    assert [r["path"] for r in found["captured_new"]] == owned[2:4], found
    assert [r["path"] for r in found["over_cap"]] == owned[4:]
    assert _captured_paths(bundle) == sorted(["/", *owned[2:4]])


def test_dismissed_routes_do_not_spend_the_walks_page_bound(qa, crawl):
    owned = _paths_owned_by(1, 1, 4)
    crawl.page.links = {"/": [APP + p for p in owned]}
    stored = {"format": 1, "groups": {}, "dismissed": _dismissed(*owned[:2])}
    _code, found, _bundle = _run(qa, crawl, "--max-pages", "3", step="1", steps="1", stored=stored)
    # MUTATION: list a dismissed route before barring it and it takes a page slot.
    assert [r["path"] for r in found["captured_new"]] == ["/", *owned[2:]], found


def test_a_run_list_with_no_dismissed_routes_writes_found_json_as_before(qa, crawl):
    crawl.page.links = {"/": [APP + "/x"]}
    _code, found, _bundle = _run(qa, crawl)
    # MUTATION: always add the `dismissed` count and a run with none writes another shape.
    assert "dismissed" not in found["skipped"]


@pytest.mark.parametrize("dismissed", ["abc", ["not-hex"], ["A" * 64], [1]])
def test_load_routes_refuses_a_dismissed_list_that_is_not_sha256_hex(qa, tmp_path, dismissed):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"format": 1, "groups": {}, "dismissed": dismissed}), encoding="utf-8")
    # MUTATION: accept any value and a path could reach the tool in place of its hash.
    with pytest.raises(qa.UsageError):
        qa.load_routes(path, allow_empty=True)
