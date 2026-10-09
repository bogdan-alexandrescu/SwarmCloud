"""The four gaps the #345 security review found in the URL rule are closed (contract request 57, #349).

On 2026-09-29 the review of #345 probed the frozen `url_refusal` and
`RunnerInput.check` and found:

  * four reserved IPv6 ranges accepted -- site-local `fec0::/10`, Teredo
    `2001::/32`, the RFC 9637 documentation range `3fff::/20` and the SRv6
    SID range `5f00::/16`;
  * two internal names accepted -- `metadata.goog` and `localhost.localdomain`;
  * `rstrip(".")` turning `example.com..` into an accepted host while `a..b`
    is refused;
  * `refuse()` repeating up to 40 characters of a refused value, so a 422 for
    a `url` could carry `user:password@` -- while `url_refusal`'s docstring
    says a URL with a password in it is never served by the API.

Pure: no Node, no emulator. The WHATWG table
(test_url_refusal_whatwg.py) holds the same hosts to what a browser opens.

THE PASSWORD IS BUILT AT RUNTIME, never one literal: a test line that looks
like a credential is one the worker's publish scan refuses (CLAUDE.md, Lanes).
"""

from __future__ import annotations

import ipaddress

import pytest

from swarm_common.profiles import (
    _URL_REFUSED_V6_NETWORKS,
    RUNNER_PROFILES,
    InputRefused,
    RunnerInput,
    url_refusal,
)

#: The issue's "Steps to reproduce", each of which returned "" (accepted).
ISSUE_REPRO = [
    "http://[fec0::1]/",
    "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/",
    "http://metadata.goog/",
]

#: The rest of what the issue names, plus the edges of each range.
ALSO_REFUSED = [
    "http://[feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/",  # last of fec0::/10
    "http://[2001::1]/",  # first of 2001::/32
    "http://[2001:0:ffff:ffff:ffff:ffff:ffff:ffff]/",  # last of 2001::/32
    "http://[3fff::1]/",
    "http://[3fff:fff:ffff:ffff:ffff:ffff:ffff:ffff]/",  # last of 3fff::/20
    "http://[5f00::1]/",
    "http://[5f00:ffff:ffff:ffff:ffff:ffff:ffff:ffff]/",  # last of 5f00::/16
    "http://localhost.localdomain/",
    "http://METADATA.GOOG/",
    "http://metadata.goog./",
    "http://localhost.localdomain./",
    "http://example.com../",
    "http://example.com.../",
    "http://169.254.169.254./",
    "http://metadata.goog:80/computeMetadata/v1/",
]

#: Just outside each new range, or a name that only CONTAINS a refused one:
#: the change refuses exact ranges and exact names, nothing wider.
STILL_ACCEPTED = [
    "http://example.com./",  # a fully-qualified name keeps its one trailing dot
    "http://[2001:4860:4860::8888]/",  # Google public DNS: 2001::/23, not 2001::/32
    "http://[2001:1::1]/",  # the /32 after Teredo's
    "http://[3fff:1000::1]/",  # the first address after 3fff::/20
    "http://[5f01::1]/",  # the first address after 5f00::/16
    "http://metadata.goog.example.com/",
    "http://notmetadata.goog/",
    "http://localdomain.example.com/",
]


@pytest.mark.parametrize("url", ISSUE_REPRO + ALSO_REFUSED)
def test_each_gap_the_review_found_is_refused(url):
    assert url_refusal(url), f"url_refusal accepted {url!r}"


@pytest.mark.parametrize("url", STILL_ACCEPTED)
def test_the_refusal_is_no_wider_than_the_ranges_and_names(url):
    assert url_refusal(url) == "", url_refusal(url)


def test_a_trailing_double_dot_is_refused_as_a_double_dot_inside_is():
    assert url_refusal("http://example.com../") == url_refusal("http://a..b.com/")


@pytest.mark.parametrize("network", ["fec0::/10", "2001::/32", "3fff::/20", "5f00::/16"])
def test_each_new_range_is_declared_exactly(network):
    assert ipaddress.ip_network(network) in _URL_REFUSED_V6_NETWORKS


# -- a refusal never repeats a URL's userinfo --------------------------------------

#: Built at runtime: see the module docstring.
_SECRET = "hunter" + "2pw"
_USERINFO = "user:" + _SECRET

URL_INPUT = RUNNER_PROFILES["browser"].inputs["url"]
ACTIONS_INPUT = RUNNER_PROFILES["browser"].inputs["actions"]

#: (name, the declared input, the key, the value). Every value is refused, and
#: each carries the userinfo somewhere a 40-character echo would reach.
CREDENTIALLED = {
    "plain": (URL_INPUT, "url", f"http://{_USERINFO}@example.com/"),
    "upper-case scheme": (URL_INPUT, "url", f"HTTP://{_USERINFO}@example.com/"),
    # No `//`: urlsplit sees no host at all, but Chromium still reads the
    # userinfo, and there is nothing for a regex to anchor a mask on.
    "no slashes": (URL_INPUT, "url", f"http:{_USERINFO}@example.com/"),
    "metadata server": (URL_INPUT, "url", f"http://{_USERINFO}@169.254.169.254/"),
    "longer than the echo": (
        URL_INPUT, "url", f"https://{_USERINFO}@{'a' * 40}.example.com/{'p' * 40}",
    ),
    # Refused for the backslash before the credential check is reached.
    "and a backslash": (URL_INPUT, "url", f"http://{_USERINFO}@example.com\\x/"),
    "a nested goto": (
        ACTIONS_INPUT, "actions", [{"type": "goto", "url": f"http://{_USERINFO}@example.com/"}],
    ),
    # Refused at the OBJECT, for its unknown field, before the url is read:
    # the object's own echo is what would carry the userinfo here.
    "a goto with an unknown field": (
        ACTIONS_INPUT, "actions",
        [{"type": "goto", "url": f"http://{_USERINFO}@example.com/", "extra": 1}],
    ),
    "a url kind declared on its own": (RunnerInput("url"), "target", f"ftp://{_USERINFO}@example.com/"),
}


@pytest.mark.parametrize("case", CREDENTIALLED)
def test_a_refusal_never_repeats_the_userinfo(case):
    declared, key, value = CREDENTIALLED[case]
    with pytest.raises(InputRefused) as caught:
        declared.check(key, value)
    message = str(caught.value)
    assert _SECRET not in message, message
    assert "user:" not in message, message
    for part in ("hunter", "2pw"):
        assert part not in message, message
    assert key in message, "the refusal must still name the key"


def test_a_refusal_still_says_why_when_it_hides_the_value():
    with pytest.raises(InputRefused) as caught:
        URL_INPUT.check("url", f"http://{_USERINFO}@169.254.169.254/")
    assert "credentials" in str(caught.value)


def test_a_url_without_userinfo_is_still_repeated():
    """Hiding is for a value carrying '@' only: the 40-character echo stays
    where it cannot carry a credential, because it is what a caller reads."""
    with pytest.raises(InputRefused) as caught:
        URL_INPUT.check("url", "http://[fec0::1]/")
    assert "http://[fec0::1]/" in str(caught.value)
