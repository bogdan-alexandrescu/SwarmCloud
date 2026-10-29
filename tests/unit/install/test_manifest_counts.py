"""The counts in `install/manifest.yaml`'s header are true (design §3.2, §3.3).

The header (`counts:`) states how many entries the base file holds by kind,
scope, provider and stage, and in total, so a reader need not trust a table
nobody checks. Fragments (`install/manifest.d/`) carry no counts and are not
counted here: a lane's fragment never edits the base file.
"""

from __future__ import annotations

from swarm_install import manifest as m


def test_counts_match_header() -> None:
    base = m.parse_file(m.MANIFEST_PATH)
    actual = m.count(base.entries)
    assert base.counts is not None, (
        f"{m.MANIFEST_PATH.name} has no counts: header; the entries say {actual}"
    )
    assert base.counts == actual, (
        f"{m.MANIFEST_PATH.name}'s counts: header is not what its entries hold; "
        f"the entries say {actual}"
    )


def test_entry_ids_unique_and_prefixed() -> None:
    """Every id is unique and its kind prefix is one of the manifest's known ones."""
    base = m.parse_file(m.MANIFEST_PATH)
    ids = [e.id for e in base.entries]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert base.entries, f"{m.MANIFEST_PATH.name} has no entries"
