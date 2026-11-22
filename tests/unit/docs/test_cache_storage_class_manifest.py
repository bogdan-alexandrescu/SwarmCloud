"""The install manifest names the xfs cache class, and says pods do not claim it yet.

archived issue 4061: the cache volume's first class formatted ext4, which ran out of inodes
with gigabytes free, so the dispatcher's CACHE_STORAGE_CLASS became an xfs
class. The release does not apply cluster-scoped manifests, so the claim stays
on the applied ext4 class (CLAIMED_CACHE_STORAGE_CLASS) until the operator
applies the xfs one. The install check the operator runs must name both.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from scheduler.dispatch import (
    CACHE_STORAGE_CLASS,
    CLAIMED_CACHE_STORAGE_CLASS,
    RETIRED_CACHE_STORAGE_CLASS,
)

ROOT = Path(__file__).resolve().parents[3]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _manifest_entry() -> str:
    entries = yaml.safe_load((ROOT / "install" / "manifest.yaml").read_text())["entries"]
    (entry,) = [e for e in entries if e["id"] == "cluster-policy.cache-storage-class"]
    return _flat(" ".join(str(entry.get(key) or "") for key in ("check", "verify", "explain")))


def test_the_claim_is_still_the_retired_ext4_class():
    # When the claim moves to xfs, the install manifest entry changes with it.
    assert CLAIMED_CACHE_STORAGE_CLASS == RETIRED_CACHE_STORAGE_CLASS != CACHE_STORAGE_CLASS


def test_the_install_manifest_entry_names_both_classes_and_the_ext4_claim():
    text = _manifest_entry()
    assert CACHE_STORAGE_CLASS in text
    assert RETIRED_CACHE_STORAGE_CLASS in text
    assert "PODS STILL CLAIM THE EXT4 CLASS" in text
    assert "CLAIMED_CACHE_STORAGE_CLASS" in text
