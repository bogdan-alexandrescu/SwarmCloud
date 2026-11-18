"""The console can PUT a plugin archive to its signed URL, and nothing else can.

Config > Plugins > Add plugin > Upload asks swarm-api for an intake and a V4
signed URL (`POST .../uploads`), then the BROWSER PUTs the archive straight to
that URL on the managed plugins bucket (apps/swarm-ui/src/api.ts
`putPluginArchive`). That PUT is cross-origin, so without a CORS rule on the
bucket its preflight never admits it and every upload from the console ends
"The archive could not be sent" (live QA finding, 2026-11-09).

terraform/infra/plugins.tf therefore gives `google_storage_bucket.plugins`
one `cors` rule, held here: its origin is the console's own origin
(`local.console_url`, never a literal, never "*"), its one method is PUT, the
headers it lets through are exactly the two swarm-api signs and the console
sends (`Content-Type`, `x-goog-content-length-range`; swarm_api.routes.
plugin_catalogue), and a deployment with no console renders no rule at all.

These read the Terraform as text: no provider, no plan, no credentials.
tests/terraform/plugins_infra.tftest.hcl plans the same rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGINS_TF = ROOT / "terraform" / "infra" / "plugins.tf"
LOCALS_TF = ROOT / "terraform" / "infra" / "locals.tf"
API_PY = ROOT / "apps" / "swarm-api" / "swarm_api" / "routes" / "plugin_catalogue.py"

# The headers the signed PUT carries: swarm-api signs both and hands them to
# the console in the upload target's `headers`, which putPluginArchive sends.
SIGNED_HEADERS = {"content-type", "x-goog-content-length-range"}


def _code(text: str) -> str:
    """The text with every `#` comment removed."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _block(text: str, opener: re.Pattern[str], what: str) -> str:
    """The body of the `{ ... }` block whose opening line `opener` matches."""
    m = opener.search(text)
    assert m, f"no {what}"
    depth = 0
    for j in range(m.end() - 1, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[m.end() : j]
    raise AssertionError(f"unbalanced {what}")


def _list(body: str, name: str) -> list[str]:
    m = re.search(rf"^\s*{name}\s*=\s*\[([^\]]*)\]", body, re.M)
    assert m, f"the cors rule sets no {name}"
    return [item.strip() for item in m.group(1).split(",") if item.strip()]


@pytest.fixture(scope="module")
def bucket() -> str:
    return _block(
        _code(PLUGINS_TF.read_text()),
        re.compile(r'^resource\s+"google_storage_bucket"\s+"plugins"\s*\{', re.M),
        "google_storage_bucket.plugins",
    )


@pytest.fixture(scope="module")
def cors(bucket: str) -> str:
    rules = re.findall(r'^\s*(?:dynamic\s+"cors"|cors)\s*\{', bucket, re.M)
    assert len(rules) == 1, f"the plugins bucket must carry exactly one cors rule, found {len(rules)}"
    return _block(bucket, re.compile(r'^\s*(?:dynamic\s+"cors"|cors)\s*\{', re.M), "cors rule")


def test_the_rule_exists_only_with_a_console(cors):
    """No console URL, no rule: an empty origin list is not an empty rule."""
    m = re.search(r"^\s*for_each\s*=\s*(.+?)\s*$", cors, re.M)
    assert m, "the cors rule is a dynamic block over the console's origin, so a deployment without one plans no rule"
    assert re.fullmatch(r'local\.console_url\s*==\s*""\s*\?\s*\[\]\s*:\s*\[local\.console_url\]', m.group(1)), (
        f"for_each must be `local.console_url == \"\" ? [] : [local.console_url]`, found {m.group(1)!r}"
    )


def test_the_one_origin_is_the_consoles_own(cors):
    assert _list(cors, "origin") == ["cors.value"], "origin is the console's origin and nothing else"
    assert "*" not in cors and "http" not in cors, "no wildcard and no literal origin"


def test_the_console_url_is_an_origin():
    """`https://<host>` with no path or trailing slash: what a browser sends as Origin."""
    m = re.search(r"^\s*console_url\s*=\s*(.+?)\s*$", _code(LOCALS_TF.read_text()), re.M)
    assert m, "locals.tf has no console_url"
    assert '"https://${var.frontend_hostname}"' in m.group(1)


def test_put_is_the_only_method(cors):
    assert _list(cors, "method") == ['"PUT"']


def test_the_allowed_headers_are_the_signed_ones(cors):
    headers = {h.strip('"').lower() for h in _list(cors, "response_header")}
    assert headers == SIGNED_HEADERS


def test_swarm_api_still_signs_exactly_those_headers():
    """If swarm-api signs another header, the rule above must allow it too."""
    text = API_PY.read_text()
    m = re.search(r'"headers":\s*\{([^}]*)\}', text)
    assert m, "plugin_catalogue.py hands the console no upload headers"
    sent = {k.lower() for k in re.findall(r'"([A-Za-z-]+)":', m.group(1))}
    assert sent == SIGNED_HEADERS


def test_the_preflight_is_cached_for_minutes_only(cors):
    m = re.search(r"^\s*max_age_seconds\s*=\s*(\d+)\s*$", cors, re.M)
    assert m, "the cors rule sets no max_age_seconds"
    assert 60 <= int(m.group(1)) <= 900
