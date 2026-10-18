"""The release TTL policies name the collections and field swarm_api writes.

swarm_api stamps every `workspace_secret_releases` row and every
`workspace_secret_release_marks` document with `expire_at`
(`workspace_secrets.RELEASE_RETENTION`, owner decision Q6, #1347), and
terraform/modules/firestore/workspace_secrets_ttl.tf turns that field into a
Firestore TTL policy. A rename on either side leaves the policy on a field
nothing writes, and the rows keep for ever with every check green; the
tftest cannot see swarm_api, so this file reads the two together.

A small regex rather than python-hcl2, which is not a dependency. Offline:
source text and an import only.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from swarm_api import workspace_secrets

ROOT = Path(__file__).resolve().parents[3]
TTL_TF = ROOT / "terraform" / "modules" / "firestore" / "workspace_secrets_ttl.tf"

RESOURCE = re.compile(
    r'resource\s+"google_firestore_field"\s+"workspace_secret_release_ttl"\s*\{(?P<body>.*?)\n\}',
    re.DOTALL,
)
FOR_EACH = re.compile(r"for_each\s*=\s*toset\(\[(?P<items>[^\]]*)\]\)")
FIELD = re.compile(r'^\s*field\s*=\s*"(?P<field>[^"]+)"', re.MULTILINE)


def _resource_body() -> str:
    match = RESOURCE.search(TTL_TF.read_text())
    assert match, f"{TTL_TF} declares no google_firestore_field.workspace_secret_release_ttl"
    return match.group("body")


def test_the_ttl_covers_exactly_the_two_collections_swarm_api_writes() -> None:
    match = FOR_EACH.search(_resource_body())
    assert match, "the release TTL resource has no for_each = toset([...]) over its collections"
    collections = set(re.findall(r'"([^"]+)"', match.group("items")))
    assert collections == {
        workspace_secrets.RELEASES_COLLECTION,
        workspace_secrets.RELEASE_MARKS_COLLECTION,
    }


def test_the_ttl_field_is_the_one_swarm_api_stamps() -> None:
    fields = FIELD.findall(_resource_body())
    assert fields == ["expire_at"]
    # The rows carry it from release_row (workspace_secrets.py) and the marks
    # from the release route (routes/attempts_secrets.py).
    stamp = f'"{fields[0]}": at + RELEASE_RETENTION'
    package = Path(workspace_secrets.__file__).parent
    for writer in (package / "workspace_secrets.py", package / "routes" / "attempts_secrets.py"):
        assert stamp in writer.read_text(), f"{writer.name} no longer stamps {fields[0]}"


def test_the_retention_is_the_owners_ninety_days() -> None:
    assert workspace_secrets.RELEASE_RETENTION == timedelta(days=90)
