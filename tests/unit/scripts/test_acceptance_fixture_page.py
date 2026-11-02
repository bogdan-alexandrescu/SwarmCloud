"""The browser acceptance fixture page is ours, public, and shows nothing a URL chooses (#1628).

THE DECISION THIS PINS. The GKE dispatch proof and the smoke suite's browser
check loaded their fixture from httpbin.org's /base64/<page> route, and
acceptance run 39249979130 failed on an httpbin.org 502. The owner's answer on
#1628: serve it ourselves -- scripts/acceptance/fixture.html, uploaded by
terraform/infra/acceptance_fixture.tf as one public object.

THE RISK THIS PINS. A page anyone on the internet can open, on a host we own,
must not render what its URL says: that is a phishing and XSS host. So the
markup is fixed, the only input is `?run=<id>`, the id must match
^[a-z0-9-]{1,80}$, and it goes in as textContent. These tests read the page
statically and, where node is on PATH, run its script against each hostile id.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PAGE = ROOT / "scripts" / "acceptance" / "fixture.html"
TESTLIB = ROOT / "scripts" / "lib" / "testlib.sh"
COMMON = ROOT / "scripts" / "lib" / "common.sh"
TF = ROOT / "terraform" / "infra" / "acceptance_fixture.tf"

RUN_ID_PATTERN = "^[a-z0-9-]{1,80}$"

HOSTILE_IDS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    'run"1',
    "run'1",
    "run<b>",
    "Run-1",
    "run_1",
    "run 1",
    "run\n1",
    "run-1\n",
    "",
    "x" * 81,
    "ünï",
    "javascript:alert(1)",
]
GOOD_IDS = ["test-gke-proof-20261019122639", "a", "0", "-", "x" * 80]


def page() -> str:
    return PAGE.read_text()


def script_body() -> str:
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page(), re.S | re.I)
    assert len(scripts) == 1, "exactly one script, the one the CSP hash admits"
    attrs, body = scripts[0]
    assert attrs.strip() == "", "an inline script with no src: nothing is fetched"
    return body


def read_testlib_value(name: str) -> str:
    match = re.search(rf"^{name}=['\"]([^'\"]*)['\"]$", TESTLIB.read_text(), re.M)
    assert match, f"{name} is not set in testlib.sh"
    return match.group(1)


# -- the run id -------------------------------------------------------------------

def page_pattern() -> str:
    match = re.search(r"/(\^[^/]+\$)/\.test\(id\)", script_body())
    assert match, "the page tests the id against one literal pattern"
    return match.group(1)


def test_the_page_and_testlib_hold_the_same_run_id_pattern():
    assert page_pattern() == RUN_ID_PATTERN
    assert read_testlib_value("BROWSER_FIXTURE_RUN_ID_PATTERN") == RUN_ID_PATTERN


def js_accepts(value: str) -> bool:
    # JavaScript's `$` without the m flag is end of input; Python's also
    # matches before a trailing newline, so test with \Z, which is JS's `$`.
    return re.match(page_pattern()[:-1] + r"\Z", value) is not None


@pytest.mark.parametrize("run_id", HOSTILE_IDS)
def test_the_run_id_pattern_refuses_anything_that_could_be_markup(run_id):
    assert not js_accepts(run_id)


@pytest.mark.parametrize("run_id", GOOD_IDS)
def test_the_run_id_pattern_admits_the_suites_ids(run_id):
    assert js_accepts(run_id)


# -- the markup is fixed ----------------------------------------------------------

@pytest.mark.parametrize(
    "sink",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
     "createContextualFragment", "DOMParser", "eval(", "Function(",
     "setTimeout", "setInterval", "location.href =", "location =",
     ".src", ".href", "setAttribute"],
)
def test_the_page_has_no_markup_or_navigation_sink(sink):
    assert sink not in page(), f"{sink} would let the URL decide what the page is"


def test_the_id_goes_in_as_text():
    body = script_body()
    assert body.count("textContent") == 2
    assert 'line.textContent = "swarm-fixture-" + id;' in body
    assert 'line.textContent = "invalid run id";' in body


def test_the_page_says_invalid_until_its_script_says_otherwise():
    """With no script (CSP refusing it, JavaScript off) the check reads
    "invalid run id" and fails -- a page that loaded is not a pass by itself."""
    assert '<p id="run">invalid run id</p>' in page()


def test_the_page_loads_nothing_from_anywhere():
    without_comments = re.sub(r"<!--.*?-->", "", page(), flags=re.S)
    for tag in ("<a ", "<form", "<iframe", "<img", "<link", "<object", "<embed", "<base", "<input"):
        assert tag not in without_comments.lower()
    assert "http://" not in without_comments and "https://" not in without_comments


def test_the_csp_admits_exactly_the_page_script():
    digest = base64.b64encode(hashlib.sha256(script_body().encode("utf-8")).digest()).decode()
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page())
    assert csp, "the page carries a Content-Security-Policy"
    directives = dict(d.strip().split(" ", 1) for d in csp.group(1).split(";"))
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == f"'sha256-{digest}'", "edit the script and recompute its hash"
    assert directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'none'"


def test_the_page_carries_the_title_and_colour_the_check_asserts():
    title = read_testlib_value("BROWSER_FIXTURE_TITLE")
    rgb = read_testlib_value("BROWSER_FIXTURE_RGB")
    assert f"<title>{title}</title>" in page()
    assert f"background:#{rgb}" in page()


# -- the script, run --------------------------------------------------------------

NODE_HARNESS = r"""
const script = require("fs").readFileSync(0, "utf8");
const cases = JSON.parse(process.argv[1]);
const out = {};
for (const search of cases) {
  const line = { textContent: "invalid run id" };
  const document = { getElementById: (name) => (name === "run" ? line : null) };
  const window = { location: { search } };
  new Function("document", "window", "URLSearchParams", script)(document, window, URLSearchParams);
  out[search] = line.textContent;
}
process.stdout.write(JSON.stringify(out));
"""


def run_page_script(searches: list[str]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH; the static tests above still hold the page")
    result = subprocess.run(
        [node, "-e", NODE_HARNESS, json.dumps(searches)],
        input=script_body(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_script_shows_a_good_id_as_the_fixture_string():
    from urllib.parse import quote

    searches = ["?run=" + quote(i, safe="") for i in GOOD_IDS]
    shown = run_page_script(searches)
    for run_id, search in zip(GOOD_IDS, searches):
        assert shown[search] == "swarm-fixture-" + run_id


def test_the_script_refuses_every_hostile_id():
    from urllib.parse import quote

    searches = ["?run=" + quote(i, safe="") for i in HOSTILE_IDS]
    searches += ["", "?", "?other=1", "?run", "?run=%3Cscript%3E", "?run=a%0A"]
    shown = run_page_script(searches)
    for search in searches:
        assert shown[search] == "invalid run id", search


# -- the Terraform that serves it -------------------------------------------------

def tf_block(kind: str, name: str) -> str:
    text = TF.read_text()
    start = text.index(f'resource "{kind}" "{name}" {{')
    end = text.index("\n}\n", start)
    return text[start:end]


def test_the_bucket_carries_the_managed_by_label():
    bucket = tf_block("google_storage_bucket", "acceptance_fixture")
    assert re.search(r"labels\s*=\s*merge\(local\.labels,", bucket)
    labels = (ROOT / "terraform" / "infra" / "locals.tf").read_text()
    assert '"managed-by" = "swarm-terraform"' in labels


def test_the_object_is_this_page_served_as_html_and_carries_the_label():
    obj = tf_block("google_storage_bucket_object", "acceptance_fixture")
    assert 'name   = "fixture.html"' in obj
    assert 'file("${path.module}/../../scripts/acceptance/fixture.html")' in obj
    assert 'content_type  = "text/html; charset=utf-8"' in obj
    assert re.search(r'"managed-by"\s*=\s*"swarm-terraform"', obj)


def test_the_public_grant_is_read_only_and_on_this_bucket_alone():
    grant = tf_block("google_storage_bucket_iam_member", "acceptance_fixture_public_read")
    assert "bucket = google_storage_bucket.acceptance_fixture[0].name" in grant
    # Owner decision on #1628, 2026-10-19: read an object by name, never list
    # the bucket. objectViewer would also admit storage.objects.list.
    assert 'role   = "roles/storage.legacyObjectReader"' in grant
    assert "objectViewer" not in grant
    assert "member = local.acceptance_fixture_public_reader" in grant
    text = TF.read_text()
    assert text.count('"allUsers"') == 1, "the public member is named once"
    assert text.count("local.acceptance_fixture_public_reader") == 1, "and granted once"


def test_the_bucket_name_matches_what_testlib_builds_the_url_from():
    assert 'acceptance_fixture_bucket = "${var.name_prefix}-acceptance-fixture-${var.project_id}"' in TF.read_text()
    assert 'ACCEPTANCE_FIXTURE_BUCKET="${ACCEPTANCE_FIXTURE_BUCKET:-swarm-acceptance-fixture-${PROJECT_ID}}"' in COMMON.read_text()
