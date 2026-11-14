"""The docs and the install manifest name the xfs cache class, and say pods do not claim it yet.

#4061: the cache volume's first class formatted ext4, which ran out of inodes
with gigabytes free, so the dispatcher's CACHE_STORAGE_CLASS became an xfs
class. The release does not apply cluster-scoped manifests, so the claim stays
on the applied ext4 class (CLAIMED_CACHE_STORAGE_CLASS) until the operator
applies the xfs one. A doc that named only one of the two read as either
"still ext4" or "already xfs"; each of these files must say both.

MUTATION: drop the xfs class name, or the sentence about the ext4 claim, from
any one of the three files, and its case fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scheduler.dispatch import (
    CACHE_STORAGE_CLASS,
    CLAIMED_CACHE_STORAGE_CLASS,
    RETIRED_CACHE_STORAGE_CLASS,
)

ROOT = Path(__file__).resolve().parents[3]
DOCS = ("docs/worker-images.md", "docs/browser-startup-speed.md")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _manifest_entry() -> str:
    entries = yaml.safe_load((ROOT / "install" / "manifest.yaml").read_text())["entries"]
    (entry,) = [e for e in entries if e["id"] == "cluster-policy.cache-storage-class"]
    return _flat(" ".join(str(entry.get(key) or "") for key in ("check", "verify", "explain")))


def test_the_claim_is_still_the_retired_ext4_class():
    # What the docs below describe; when the claim moves to xfs, they change with it.
    assert CLAIMED_CACHE_STORAGE_CLASS == RETIRED_CACHE_STORAGE_CLASS != CACHE_STORAGE_CLASS


@pytest.mark.parametrize("doc", DOCS)
def test_each_doc_names_the_xfs_class_and_the_ext4_claim(doc: str):
    text = _flat((ROOT / doc).read_text())
    assert f"`{CACHE_STORAGE_CLASS}`" in text, f"{doc} does not name the xfs class"
    assert f"`{RETIRED_CACHE_STORAGE_CLASS}`" in text, f"{doc} does not name the ext4 class"
    assert "`CLAIMED_CACHE_STORAGE_CLASS`" in text, f"{doc} does not say why the claim waits"
    assert "until the operator applies the xfs class" in text, (
        f"{doc} reads as if pods already claim the xfs class"
    )


def test_the_install_manifest_entry_names_both_classes_and_the_ext4_claim():
    text = _manifest_entry()
    assert CACHE_STORAGE_CLASS in text
    assert RETIRED_CACHE_STORAGE_CLASS in text
    assert "PODS STILL CLAIM THE EXT4 CLASS" in text
    assert "CLAIMED_CACHE_STORAGE_CLASS" in text
