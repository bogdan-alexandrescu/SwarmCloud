"""JUDGE-ADAPT (owner decision 2026-10-14; part of #1030).

jev-browser talks to TypeSafe's API. Saga's internal decision API (Internal
Jev, Internal Laya) answers in the same shape but refuses two request shapes
jev-browser sends, measured by the operator over Tailscale on 2026-10-14
(docs/design/live-browser.md, "Judgment backends"):

  * an object `state` -> 400 "state: Input should be a valid string";
  * a `choice` whose options carry no description (jev-browser's element and
    group picks send `{"7": null}`) -> 400 "criteria Field required";
  * a `score` with dict criteria -> 400 "criteria Input should be a valid
    list" (jev-browser asks none).

The image's patch adds src/internal-compat.mjs to the pinned package and two
lines to src/jev.mjs that route every request through it. With
JEV_API_COMPAT unset nothing changes; with JEV_API_COMPAT=internal the state
becomes sorted, whitespace-free JSON, undescribed options are described from
`page.elements` (by `i`) or `page.groups` (by `g`), a choice nothing can
describe fails with its reason, a dict score is refused, and the key sent is
a fixed placeholder, never TYPESAFE_API_KEY.

The module is taken out of the patch itself -- a new file's hunk is the whole
file -- and run with node, so what is tested is what the image installs. The
rest of the package needs Playwright, which a unit test does not have
offline: the image's build-time self-test (selftest.py `jev_compat`, through
jev-browser/compat-probe.mjs) runs the patched package's own decide() and
resolve() over the recorded internal-API answers in
jev-browser/fixtures/internal-decide.json.

Like tests/unit/control_plane/test_url_refusal_whatwg.py, this needs `node`
and FAILS under CI without it rather than skipping.

MUTATION: drop the stringify of an object state, the fill from page.elements,
the refusal of an undescribable choice, the placeholder line in apiKey(), or
the TypeSafe-URL refusal, and the named test below fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
JEV_DIR = REPO / "images" / "agent-runtime-browser" / "jev-browser"
PATCH = JEV_DIR / "patches" / "jev-browser+0.2.0.patch"
FIXTURE = JEV_DIR / "fixtures" / "internal-decide.json"
COMPAT = "node_modules/jev-browser/src/internal-compat.mjs"
JEV = "node_modules/jev-browser/src/jev.mjs"
INDEX = "node_modules/jev-browser/src/index.mjs"
INTERNAL_URL = "https://decision-api-staging.example.invalid/jev/v1/decide"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
INTERNAL = {"JEV_API_COMPAT": "internal"}

TARGET_Q = "Which entry of `page.elements` (by its `i`) should the next action toward `task.goal` act on?"
GROUP_Q = ("Which entry of `page.groups` (by its `g`) contains the element the next action toward `task.goal` "
           "should act on? Each group summarises consecutive page elements.")
PAGE = {
    "url": "https://example.test/", "title": "Shop", "text": "Search the shop",
    "elements": [
        {"i": 0, "tag": "a", "text": "Home", "href": "/"},
        {"i": 1, "tag": "input:text", "label": "Search"},
        {"i": 2, "tag": "button", "text": "Go"},
        {"i": 3, "tag": "button", "label": "x" * 200},
    ],
}


def _added() -> dict[str, list[str]]:
    files: dict[str, list[str]] = {}
    current = ""
    for line in PATCH.read_text().splitlines():
        m = re.match(r"diff --git a/(\S+) b/(\S+)$", line)
        if m:
            current = m.group(1)
            files[current] = []
        elif line.startswith("+") and not line.startswith("+++"):
            files[current].append(line[1:])
    return files


@pytest.fixture(scope="module")
def node() -> str:
    found = shutil.which("node")
    if not found:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the adapter cannot run; it must not pass by skipping")
        pytest.skip("node is not installed here; CI runs these")
    return found


@pytest.fixture(scope="module")
def module(tmp_path_factory) -> Path:
    text = PATCH.read_text()
    assert f"diff --git a/{COMPAT} b/{COMPAT}\nnew file mode 100644\n" in text, "the patch adds the adapter module"
    out = tmp_path_factory.mktemp("compat") / "internal-compat.mjs"
    out.write_text("\n".join(_added()[COMPAT]) + "\n")
    return out


def _adapt(node: str, module: Path, state: Any, questions: dict, *, env: dict | None = None,
           url: str = INTERNAL_URL) -> dict:
    """adaptForBackend(state, questions, url, env) -> {state, questions} or {error}, plus whether
    the inputs were mutated."""
    code = (
        "import { readFileSync } from 'node:fs';"
        "const m = await import(process.argv[1]);"
        "const c = JSON.parse(readFileSync(0, 'utf8'));"
        "const before = JSON.stringify([c.state, c.questions]);"
        "let out;"
        "try { const r = m.adaptForBackend(c.state, c.questions, c.url, c.env);"
        "  out = { state: r.state, questions: r.questions, same: r.state === c.state && r.questions === c.questions }; }"
        "catch (e) { out = { error: String(e.message) }; }"
        "out.mutated = JSON.stringify([c.state, c.questions]) !== before;"
        "console.log(JSON.stringify(out));"
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", code, module.as_uri()],
        input=json.dumps({"state": state, "questions": questions, "url": url, "env": env if env is not None else INTERNAL}),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out.pop("mutated") is False, "the adapter changed the caller's objects"
    return out


# --- the shapes the internal API refused (2026-10-14) --------------------------------


def test_an_object_state_becomes_sorted_whitespace_free_json(node, module):
    state = {"task": {"history": [], "goal": "press Go"}, "page": {"url": "u", "elements": [{"tag": "a", "i": 0}]}}
    out = _adapt(node, module, state, {"q": {"type": "noul", "instructions": "Is it?"}})
    assert out["state"] == '{"page":{"elements":[{"i":0,"tag":"a"}],"url":"u"},"task":{"goal":"press Go","history":[]}}'
    assert json.loads(out["state"]) == state
    assert out["questions"] == {"q": {"type": "noul", "instructions": "Is it?"}}


def test_a_string_state_is_sent_as_it_is(node, module):
    out = _adapt(node, module, "already a string", {"q": {"type": "noul", "instructions": "Is it?"}})
    assert out["state"] == "already a string"


def test_the_target_pick_is_described_from_page_elements(node, module):
    criteria = {str(e["i"]): None for e in PAGE["elements"]}
    out = _adapt(node, module, {"page": PAGE, "task": {"goal": "g"}},
                 {"target": {"type": "choice", "instructions": TARGET_Q, "criteria": criteria}})
    got = out["questions"]["target"]
    assert got["instructions"] == TARGET_Q and got["type"] == "choice"
    assert got["criteria"] == {"0": "a Home", "1": "input:text Search", "2": "button Go", "3": "button " + "x" * 73}
    assert all(len(v) <= 80 for v in got["criteria"].values())


def test_a_choice_with_no_criteria_at_all_gets_every_element(node, module):
    out = _adapt(node, module, {"page": PAGE}, {"target": {"type": "choice", "instructions": TARGET_Q}})
    assert list(out["questions"]["target"]["criteria"]) == ["0", "1", "2", "3"]


def test_the_second_stage_keeps_only_the_keys_it_was_asked(node, module):
    out = _adapt(node, module, {"page": PAGE},
                 {"target": {"type": "choice", "instructions": TARGET_Q, "criteria": {"2": None, "0": None}}})
    assert out["questions"]["target"]["criteria"] == {"2": "button Go", "0": "a Home"}


def test_the_group_pick_is_described_from_page_groups(node, module):
    groups = [{"g": 0, "summary": "Home | Search | Go"}, {"g": 1, "summary": "Item 30 | Item 31"}]
    out = _adapt(node, module, {"page": {"url": "u", "groups": groups}},
                 {"group": {"type": "choice", "instructions": GROUP_Q, "criteria": {"0": None, "1": None}}})
    assert out["questions"]["group"]["criteria"] == {"0": "Home | Search | Go", "1": "Item 30 | Item 31"}


def test_a_choice_nothing_can_describe_fails_and_is_never_guessed(node, module):
    out = _adapt(node, module, {"page": PAGE}, {"q": {"type": "choice", "instructions": "Which one is best?"}})
    assert "names neither `page.elements` nor `page.groups`" in out["error"] and '"q"' in out["error"]


def test_an_option_that_is_not_on_the_page_fails(node, module):
    out = _adapt(node, module, {"page": PAGE},
                 {"target": {"type": "choice", "instructions": TARGET_Q, "criteria": {"2": None, "9": None}}})
    assert 'option "9" is not an entry of page.elements' in out["error"]


def test_a_page_reference_without_that_page_part_fails(node, module):
    out = _adapt(node, module, {"page": {"url": "u"}}, {"group": {"type": "choice", "instructions": GROUP_Q}})
    assert "the state has no page.groups" in out["error"]


def test_described_options_pass_unchanged_and_keys_only_options_describe_themselves(node, module):
    tools = {"click": "Click the target", "none": "No action"}
    out = _adapt(node, module, {"page": PAGE}, {
        "tool": {"type": "choice", "instructions": "What is the next action toward `task.goal` on `page`?", "criteria": tools},
        "key": {"type": "choice", "instructions": "Which keyboard key should be pressed next?",
                "criteria": {"Escape": None, "Tab": None}},
    })
    assert out["questions"]["tool"]["criteria"] == tools
    assert out["questions"]["key"]["criteria"] == {"Escape": "Escape", "Tab": "Tab"}


def test_a_score_with_dict_criteria_is_refused_and_a_list_passes(node, module):
    out = _adapt(node, module, {"page": PAGE}, {"s": {"type": "score", "instructions": "Rate it", "criteria": {"a": "x"}}})
    assert "criteria Input should be a valid list" in out["error"]
    listed = {"s": {"type": "score", "instructions": "Rate it", "criteria": ["a", "b"]}}
    assert _adapt(node, module, {"page": PAGE}, listed)["questions"] == listed


# --- the switch ----------------------------------------------------------------------


def test_with_compat_unset_the_typesafe_request_is_untouched(node, module):
    # Even shapes the internal API refuses: TypeSafe accepts them, so nothing is changed.
    questions = {
        "target": {"type": "choice", "instructions": TARGET_Q, "criteria": {"0": None}},
        "q": {"type": "choice", "instructions": "Which one is best?"},
        "s": {"type": "score", "instructions": "Rate it", "criteria": {"a": "x"}},
    }
    state = {"page": PAGE}
    for env in ({}, {"JEV_API_COMPAT": ""}):
        out = _adapt(node, module, state, questions, env=env, url=TYPESAFE_URL)
        assert out == {"state": state, "questions": questions, "same": True}


def test_an_unknown_compat_value_is_refused(node, module):
    out = _adapt(node, module, {"page": PAGE}, {}, env={"JEV_API_COMPAT": "laya"})
    assert "JEV_API_COMPAT=" in out["error"] and 'set "internal"' in out["error"]


def test_internal_toward_typesafe_is_refused(node, module):
    for url in (TYPESAFE_URL, "https://eu.typesafe.ai/v1/systemone", "not a url"):
        out = _adapt(node, module, {"page": PAGE}, {}, url=url)
        assert "JEV_API_URL" in out["error"], url


# --- the recorded internal answers ---------------------------------------------------


def test_the_fixture_answers_in_the_shape_jev_browser_reads():
    fixture = json.loads(FIXTURE.read_text())
    assert set(fixture) == {"_about", "single", "group", "target"}
    for name in ("single", "group", "target"):
        for q, a in fixture[name]["answers"].items():
            if "noul" in a:
                assert set(a) == {"noul"} and 0 <= a["noul"] <= 1, (name, q)
                continue
            assert set(a) == {"choice", "confidence", "probabilities"}, (name, q)
            assert a["choice"] in a["probabilities"], (name, q)
            assert abs(sum(a["probabilities"].values()) - 1) < 1e-3, (name, q)
            assert a["confidence"] == a["probabilities"][a["choice"]], (name, q)
    assert fixture["single"]["answers"]["target"]["choice"] == "2"
    assert sorted(fixture["group"]["answers"]["group"]["probabilities"], key=int) == [str(g) for g in range(9)]
    assert sorted(fixture["target"]["answers"]["target"]["probabilities"], key=int) == [str(i) for i in range(120, 150)]


# --- the wiring in the patch ---------------------------------------------------------


def test_jev_routes_every_request_through_the_adapter_and_never_sends_the_typesafe_key_inside():
    files = _added()
    jev = files[JEV]
    assert 'import { adaptForBackend, compatMode, INTERNAL_PLACEHOLDER_KEY } from "./internal-compat.mjs";' in jev
    assert "  if (compatMode() === \"internal\") return INTERNAL_PLACEHOLDER_KEY;" in jev
    assert "  ({ state, questions } = adaptForBackend(state, questions, API_URL));" in jev
    # The placeholder comes before any read of TYPESAFE_API_KEY, and the adapter before apiKey():
    # the context lines of the hunk say where each landed.
    text = PATCH.read_text()
    assert ("export function apiKey() {\n+  // swarm:" in text
            and "+  if (compatMode() === \"internal\") return INTERNAL_PLACEHOLDER_KEY;\n   if (cachedKey) return cachedKey;" in text)
    assert "+  ({ state, questions } = adaptForBackend(state, questions, API_URL));\n   const key = apiKey();" in text
    assert any(ln.startswith("export { adaptForBackend, adaptInternal, compatMode") for ln in files[INDEX])


def test_the_placeholder_is_plainly_not_a_credential():
    line = next(ln for ln in _added()[COMPAT] if ln.startswith("export const INTERNAL_PLACEHOLDER_KEY"))
    value = re.search(r'"([^"]*)"', line).group(1)
    assert value == "internal-backend-takes-no-key"
