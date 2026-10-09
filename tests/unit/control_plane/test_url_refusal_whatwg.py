"""`url_refusal` agrees with a real WHATWG URL parser, not with itself (contract request 32).

WHY A SECOND PARSER. `url_refusal` reads a URL with `urllib.parse.urlsplit`,
which follows RFC 3986. Chromium, which opens the URL, follows the WHATWG URL
Standard. A security review of request 32 on 2026-09-29 found four places the
two disagree -- a backslash in the authority, a percent-encoded host, a
full-width host, and U+3002 as a dot -- and each disagreement was a bypass:
`urlsplit` saw a host the rule did not recognise, while Chromium normalised
the same string to the metadata server and opened it. A test that builds its
expected verdict with `urlsplit` and checks `url_refusal` against that same
`urlsplit` output "proves nothing about Chromium; it is the rule re-checking
itself in a mirror" (the entry's own words). So every case below is also
parsed by a WHATWG-conformant parser, and the rule is held to what THAT parser
resolves the host to.

THE PARSER IS NODE'S OWN `URL` CLASS, driven as a subprocess. The decision,
and why: the entry named Node's `URL` or "a maintained Python WHATWG binding
if one is vendored". None is vendored here, and adding one would put a new
dependency into every `pyproject.toml` that resolves the unit suite, for one
test. Node's `URL` is the WHATWG Standard's reference-grade implementation
(it runs the standard's own web-platform-tests), and Node is already present
wherever this suite runs: the GitHub-hosted `ubuntu-24.04` image the python
job runs on ships Node, and `agent-runtime-base`, where a SwarmCloud agent
runs these tests, installs it (images/agent-runtime-base/Dockerfile copies
`/usr/local/bin/node`). Without Node the table SKIPS on a laptop and FAILS
under CI (`CI` is set), because a WHATWG table that silently stopped running
is exactly the mirror test this file replaces.

WHAT IS ASSERTED, per case:

  * Node resolves the URL to the host the table says -- so the table's
    reading of each bypass (what Chromium would actually open) is itself
    measured, not assumed;
  * `url_refusal` refuses or accepts it as the table says;
  * THE PROPERTY: whenever `url_refusal` ACCEPTS a URL, the host a WHATWG
    parser resolves it to is the host the rule checked, and that host, on
    its own, is accepted by the rule too. A URL the rule accepts can never
    open a host the rule would refuse.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
from urllib.parse import urlsplit

import pytest

from swarm_common.profiles import url_refusal

#: Prints `[url, hostname]` per argument, or `[url, null]` when the URL
#: Standard's parser refuses it.
_NODE_PROGRAM = (
    "for (const u of process.argv.slice(1)) {"
    "  let h = null;"
    "  try { h = new URL(u).hostname; } catch (e) { h = null; }"
    "  console.log(JSON.stringify([u, h]));"
    "}"
)

#: (url, the host a WHATWG parser resolves it to -- None when it refuses the
#: URL outright -- and whether `url_refusal` must refuse it).
REFUSED = [
    # The four bypasses the 2026-09-29 review found. Each OPENS the metadata
    # server, or its name, once a browser has parsed it.
    ("http://169.254.169.254\\@example.com/", "169.254.169.254", True),
    ("http://metadata.google.interna%6c/", "metadata.google.internal", True),
    ("http://metadata.google.ｉｎｔｅｒｎａｌ/", "metadata.google.internal", True),
    ("http://169.254.169.254。/", "169.254.169.254", True),
    ("http://169254169254。/", None, True),
    # The `.svc` bypass: three labels, so the single-label rule missed it,
    # and the pod's `ndots:5` search path completes it into the cluster.
    ("http://kubernetes.default.svc/", "kubernetes.default.svc", True),
    ("http://swarm-api.swarm-system.svc/", "swarm-api.swarm-system.svc", True),
    # A last label that is a number: a browser reads the host as IPv4.
    ("http://2852039166/", "169.254.169.254", True),
    ("http://0xa9.254.169.254/", "169.254.169.254", True),
    # Host shapes that are not a domain.
    ("http://kubernetes/", "kubernetes", True),
    ("http://localhost/", "localhost", True),
    ("http://a..b.com/", "a..b.com", True),
    ("http://---.com/", "---.com", True),
    ("http://a_b.example.com/", "a_b.example.com", True),
    # Names that never leave the node or the cluster.
    ("http://metadata.google.internal/", "metadata.google.internal", True),
    ("http://printer.local/", "printer.local", True),
    ("http://foo.localhost/", "foo.localhost", True),
    # Credentials, which would be stored and shown with the task.
    ("http://user:pw@example.com/", "example.com", True),
    # Addresses that are not public.
    ("http://169.254.169.254/", "169.254.169.254", True),
    ("http://10.0.0.1/", "10.0.0.1", True),
    ("http://100.64.0.1/", "100.64.0.1", True),
    ("http://199.36.153.4/", "199.36.153.4", True),
    ("http://[::1]/", "[::1]", True),
    ("http://[fe80::1]/", "[fe80::1]", True),
    # All six IPv6 forms that embed an IPv4 address, each embedding the
    # metadata server's.
    ("http://[::ffff:169.254.169.254]/", "[::ffff:a9fe:a9fe]", True),  # IPv4-mapped
    ("http://[64:ff9b::a9fe:a9fe]/", "[64:ff9b::a9fe:a9fe]", True),  # NAT64 well-known, RFC 6052
    ("http://[64:ff9b:1:a9fe:a9:fe00::]/", "[64:ff9b:1:a9fe:a9:fe00::]", True),  # NAT64 local-use, RFC 8215
    ("http://[2002:a9fe:a9fe::]/", "[2002:a9fe:a9fe::]", True),  # 6to4, RFC 3056
    ("http://[::ffff:0:a9fe:a9fe]/", "[::ffff:0:a9fe:a9fe]", True),  # SIIT, RFC 6052 2.1
    ("http://[::a9fe:a9fe]/", "[::a9fe:a9fe]", True),  # IPv4-compatible, deprecated
    # Reserved IPv6 ranges the #345 review found accepted (request 57, #349).
    ("http://[fec0::1]/", "[fec0::1]", True),  # site-local, RFC 3879
    (
        "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/",
        "[2001:0:4136:e378:8000:63bf:3fff:fdd2]",
        True,
    ),  # Teredo, RFC 4380
    ("http://[3fff::1]/", "[3fff::1]", True),  # documentation, RFC 9637
    ("http://[5f00::1]/", "[5f00::1]", True),  # SRv6 SIDs, RFC 9602
    # Internal names, refused exactly (request 57).
    ("http://metadata.goog/", "metadata.goog", True),
    ("http://localhost.localdomain/", "localhost.localdomain", True),
    # A trailing double dot is an empty label, as `a..b` is (request 57).
    ("http://example.com../", "example.com..", True),
    ("http://169.254.169.254./", "169.254.169.254", True),
    # Not a URL a browser task opens at all.
    ("file:///etc/passwd", "", True),
    ("http://example.com:99999/", None, True),
    ("http://exa mple.com/", None, True),
]

ACCEPTED = [
    ("https://www.example.com/", "www.example.com", False),
    ("https://github.com/", "github.com", False),
    ("HTTP://WWW.EXAMPLE.COM/", "www.example.com", False),
    ("http://example.com./", "example.com.", False),
    ("http://8.8.8.8/", "8.8.8.8", False),
    # The same six IPv6 embeddings, each carrying a PUBLIC address (8.8.8.8):
    # the unwrapping refuses by what is embedded, not by the form itself.
    ("http://[::ffff:8.8.8.8]/", "[::ffff:808:808]", False),
    ("http://[64:ff9b::808:808]/", "[64:ff9b::808:808]", False),
    ("http://[64:ff9b:1:808:8:800::]/", "[64:ff9b:1:808:8:800::]", False),
    ("http://[2002:808:808::]/", "[2002:808:808::]", False),
    ("http://[::ffff:0:808:808]/", "[::ffff:0:808:808]", False),
    ("http://[::808:808]/", "[::808:808]", False),
    # Just outside each range request 57 added, so the ranges are shown not
    # to be wider than named. fec0::/10 has no such neighbour: fe80::/10 is
    # directly below it and ff00::/8 directly above, both already refused.
    ("http://[2001:4860:4860::8888]/", "[2001:4860:4860::8888]", False),  # 2001::/23, not the /32
    ("http://[2001:1::1]/", "[2001:1::1]", False),  # the /32 after Teredo's
    ("http://[3fff:1000::1]/", "[3fff:1000::1]", False),  # first after 3fff::/20
    ("http://[5f01::1]/", "[5f01::1]", False),  # first after 5f00::/16
    # Only the exact names are refused, not a name that contains one.
    ("http://metadata.goog.example.com/", "metadata.goog.example.com", False),
]

CASES = REFUSED + ACCEPTED


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail(
                "node is not on PATH in CI, so the WHATWG table cannot run; it must not "
                "pass by skipping (see this file's docstring)"
            )
        pytest.skip("node is not installed here; CI runs this table")
    return node


@pytest.fixture(scope="module")
def whatwg() -> dict[str, str | None]:
    """Every case's hostname as Node's WHATWG `URL` resolves it."""
    result = subprocess.run(
        [_node(), "-e", _NODE_PROGRAM, *(url for url, _, _ in CASES)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    parsed = dict(json.loads(line) for line in result.stdout.splitlines() if line.strip())
    assert len(parsed) == len(CASES), f"node parsed {len(parsed)} of {len(CASES)} cases"
    return parsed


def test_the_table_covers_every_named_bypass_and_both_verdicts():
    """Guards the parametrisations below against reading nothing."""
    assert len(REFUSED) >= 30 and len(ACCEPTED) >= 10, (len(REFUSED), len(ACCEPTED))


@pytest.mark.parametrize("url,host,refused", CASES, ids=[c[0] for c in CASES])
def test_a_whatwg_parser_resolves_the_host_the_table_says(whatwg, url, host, refused):
    """The table's claim about what a browser opens is measured, not assumed."""
    assert whatwg[url] == host, f"a WHATWG parser reads {url!r} as host {whatwg[url]!r}"


@pytest.mark.parametrize("url,host,refused", CASES, ids=[c[0] for c in CASES])
def test_url_refusal_gives_the_tables_verdict(url, host, refused):
    reason = url_refusal(url)
    if refused:
        assert reason, f"{url!r} opens {host!r} in a browser and url_refusal accepted it"
    else:
        assert reason == "", f"{url!r} is a public page and url_refusal refused it: {reason}"


def _same_host(whatwg_host: str, checked: str) -> bool:
    """`whatwg_host` (bracketed IPv6, trailing dot kept) is `checked` (urlsplit's)."""
    bare = whatwg_host.strip("[]").rstrip(".")
    try:
        return ipaddress.ip_address(bare) == ipaddress.ip_address(checked)
    except ValueError:
        return bare == checked


@pytest.mark.parametrize("url,host,refused", CASES, ids=[c[0] for c in CASES])
def test_an_accepted_url_opens_only_a_host_the_rule_accepts(whatwg, url, host, refused):
    """THE PROPERTY, independent of the table's own verdicts: a URL the rule
    accepts resolves, under the WHATWG Standard, to the very host the rule
    checked, and that host passes the rule on its own. A parser differential
    on an ACCEPTED url is the bypass shape the review found."""
    if url_refusal(url):
        return
    opened = whatwg[url]
    assert opened, f"url_refusal accepted {url!r}, which a WHATWG parser refuses to parse"
    checked = (urlsplit(url).hostname or "").rstrip(".")
    assert _same_host(opened, checked), (
        f"url_refusal checked host {checked!r} in {url!r}, but a browser opens {opened!r}"
    )
    assert url_refusal(f"http://{opened}/") == "", (
        f"url_refusal accepted {url!r}, whose WHATWG host {opened!r} it refuses on its own"
    )
