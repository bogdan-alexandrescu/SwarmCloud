"""The metadata-server paths common.sh asks for, pinned by spelling.

WHY A TEST FOR A STRING. `access_token` and `id_token` are the only way a
process inside Cloud Run proves who it is -- there is no key file and no
gcloud in those images by design. Both asked for
`/instance/service_accounts/default/...`, with an UNDERSCORE.

The metadata server serves `service-accounts`, hyphenated. The underscore
spelling returns `404 page not found` -- its answer for an unknown path, not
for a refused one -- so `curl -sf` failed and access_token died with "the
metadata server refused an access token", which reads like a permissions
problem and is not one.

Measured from inside the swarm-verify job against the live metadata server on
2026-09-22:

    service_accounts -> 404
    service-accounts -> 200

It was the fifth and final thing stopping the in-VPC verification gate from
executing a single assertion, after: gcloud in an image with no gcloud, a
service account with no Firestore read, PRIVATE_RANGES_ONLY egress at a public
URL, and an identity the API's domain check refused.

Nothing else could have caught it. The images carry no Cloud SDK, so there is
no client library whose own constant would disagree; shellcheck cannot know a
URL; and the gate that would have exercised it is the gate this broke.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[3] / "scripts" / "lib" / "common.sh"
METADATA_RE = re.compile(
    r"http://metadata\.google\.internal/computeMetadata/v1/instance/([A-Za-z_-]+)/"
)


def _paths() -> list[str]:
    return METADATA_RE.findall(COMMON.read_text())


def test_the_helpers_are_still_there():
    """If both disappear, the assertions below pass vacuously."""
    assert len(_paths()) >= 2, f"expected access_token and id_token, found {_paths()}"


@pytest.mark.parametrize("segment", _paths())
def test_never_the_underscore_spelling(segment):
    assert segment != "service_accounts", (
        "the metadata server answers 404 for `service_accounts`. A 404 here is "
        "not a permission problem, and the error it produces -- 'the metadata "
        "server refused an access token' -- says it is."
    )


def test_the_hyphenated_spelling_is_what_is_asked_for():
    assert set(_paths()) == {"service-accounts"}, (
        f"unexpected metadata paths: {sorted(set(_paths()))}"
    )


def test_both_helpers_carry_the_metadata_flavor_header():
    """Without it the metadata server answers 403, which at least fails loudly
    -- but a helper that lost the header would be a different silent failure in
    the same two functions."""
    text = COMMON.read_text()
    for name in ("access_token", "id_token"):
        start = text.index(f"{name}()")
        end = text.index("\n}\n", start)
        body = text[start:end]
        if "metadata.google.internal" not in body:
            continue
        assert "Metadata-Flavor: Google" in body, f"{name} lost the Metadata-Flavor header"
