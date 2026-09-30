"""The runners keep their own checks, and they are no looser than the catalogue's (contract request 32).

Request 32 declares `browser`'s and `generic`'s inputs, so swarm-api and the
plugin's bridge refuse a bad value at submission. The runners keep checking
anyway: "a task written before this change still reaches it" (the entry's
*Downstream restatements*). Two of those runner checks were looser than the
rule the catalogue now states, and each is held here.

  * `generic._check_argument` used `_ARGUMENT_SAFE.match`. Without
    `re.MULTILINE`, `$` matches at the end of the string OR just before a
    trailing newline, so `"safe\\n"` passed and became an argv entry or a
    working directory carrying a newline. The catalogue's `_ARGUMENT` uses
    `fullmatch`; the runner does now too (the entry's *Preconditions*).
  * `browser._check_url` checked only the scheme and that there was a host,
    so a `goto` to the metadata server, a private address or a URL carrying a
    password ran, and waited out `timeout_ms` against an address the network
    drops. It calls the catalogue's `url_refusal` now, the one rule swarm-api
    and the bridge call.
"""

from __future__ import annotations

import pytest

from agent_worker.runners import browser, generic
from agent_worker.runners.base import RunnerFailure


@pytest.mark.parametrize("value", ["safe\n", "tests\n", "all\n"])
def test_an_argument_with_a_trailing_newline_is_refused(value):
    with pytest.raises(RunnerFailure):
        generic._check_argument(value, field="input.target")


@pytest.mark.parametrize("value", ["all", "tests/unit", "src/pkg/test_x.py", "_build"])
def test_an_ordinary_argument_still_passes(value):
    assert generic._check_argument(value, field="input.paths") == value


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/computeMetadata/v1/",
        "http://metadata.google.internal/",
        "http://10.0.0.1/",
        "http://kubernetes.default.svc/",
        "http://kubernetes/",
        "http://user:pw@example.com/",
        "http://169.254.169.254\\@example.com/",
        "http://[::ffff:169.254.169.254]/",
        "http://2852039166/",
    ],
)
def test_the_browser_runner_refuses_what_url_refusal_refuses(url):
    with pytest.raises(RunnerFailure) as caught:
        browser._check_url(url)
    assert "user:pw" not in str(caught.value), "a refusal must not echo a credential back"


@pytest.mark.parametrize("url", ["https://example.com/a", "http://example.com", "https://github.com/"])
def test_the_browser_runner_still_opens_a_public_page(url):
    assert browser._check_url(url) == url
