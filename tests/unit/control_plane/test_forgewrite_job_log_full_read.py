"""`job_log_tail`'s larger read is an explicit opt-in, and still bounded (#3170).

The CI-fix excerpt (`issueci._check_section`) re-reads a vitest shard's log
with `cap=MAX_LOG_FULL_READ_BYTES` when the 16 KiB tail holds fewer FAIL
headers than vitest's `Tests N failed`. Every other caller -- gittools,
release_health, dependency_cve_refresh -- passes no `cap` and keeps the 16 KiB
it had. The log storage is read without the tenant's token either way.

Nothing touches GitHub: `_locate` and `_fetch` are fakes recording what they
were sent.
"""

from __future__ import annotations

import secrets
from typing import Any

from swarm_api import forgewrite
from swarm_api.validation import IssueRef

REF = IssueRef(owner="acme", repo="widgets", number=3146)
LOG_URL = "https://pipelines.actions.githubusercontent.com/log/123?sig=signed"


class _Wire:
    def __init__(self, log: bytes):
        self.log = log
        self.located: list[tuple[str, dict[str, str]]] = []
        self.fetched: list[tuple[str, dict[str, str]]] = []

    def locate(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        self.located.append((url, dict(headers)))
        return 302, LOG_URL

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.fetched.append((url, dict(headers)))
        asked = int(headers["Range"].removeprefix("bytes=-"))
        return 206, self.log[-asked:]


def _ranges(wire: _Wire) -> list[str]:
    return [headers["Range"] for _url, headers in wire.fetched]


def test_the_default_cap_is_unchanged_and_the_full_read_is_bounded_and_tokenless():
    wire = _Wire(b"".join(b"line %07d ......................\n" % i for i in range(80_000)))
    writer = forgewrite.GitHubWriter(send=lambda *a: (200, b"{}"), locate=wire.locate, fetch=wire.fetch)
    token = "ghs_" + secrets.token_hex(18)
    tail = forgewrite.MAX_LOG_TAIL_BYTES
    full = forgewrite.MAX_LOG_FULL_READ_BYTES

    # MUTATION: raise MAX_LOG_TAIL_BYTES, or let the default `cap` be the full-read cap -> red.
    assert tail == 16 * 1024
    writer.job_log_tail(REF, 7, token)
    writer.job_log_tail(REF, 7, token, limit=full)
    writer.job_log_tail(REF, 7, token, limit=10 ** 9)
    assert _ranges(wire) == [f"bytes=-{tail}"] * 3

    wire.fetched.clear()
    # MUTATION: ignore `cap` (the re-read stays at 16 KiB) -> red.
    text: Any = writer.job_log_tail(REF, 7, token, limit=full, cap=full)
    # MUTATION: drop the clamp of `cap` to MAX_LOG_FULL_READ_BYTES -> red.
    writer.job_log_tail(REF, 7, token, limit=10 ** 9, cap=10 ** 9)
    assert _ranges(wire) == [f"bytes=-{full}"] * 2
    assert full == 1024 * 1024 and isinstance(text, str) and len(text.encode()) <= full

    # MUTATION: send the tenant's token to the log storage -> red.
    for url, headers in wire.fetched:
        assert url == LOG_URL
        assert "Authorization" not in headers and token not in str(headers)
    # The token goes to api.github.com only.
    assert all(url.startswith("https://api.github.com/") for url, _headers in wire.located)
    assert all(token in headers.get("Authorization", "") for _url, headers in wire.located)
