"""One intake, as a pure function: archive bytes in, a report and (when pinned) the runnable tar out.

§2.3 in its order: size, every entry, caps while unpacking, the plugin root,
dependencies, the manifest and its components, the overlay, then the
normalised tree as one canonical tar and its digest. No partial result: a
refusal anywhere returns a report with the refusal and no tar.
"""

from __future__ import annotations

from typing import Any

from . import limits
from .archive import FILE, SYMLINK, read_archive
from .manifest import MANIFEST, has_own_hooks, list_components, parse_manifest, suggest_requires
from .overlay import apply_overlay, parse_overlay
from .refusals import Refusal
from .tree import canonical_tar, check_dependencies, plugin_tree, sha256

REPORT_SCHEMA = "swarm.plugin-intake.report/1"
CANONICAL_TAR = "pax, paths sorted, files 0444 or 0555, folders 0555, links 0777, owners and times zero"


def _limits() -> dict[str, int]:
    return {
        "max_packed_bytes": limits.MAX_PACKED_BYTES,
        "max_unpacked_bytes": limits.MAX_UNPACKED_BYTES,
        "max_files": limits.MAX_FILES,
        "max_expansion_ratio": limits.MAX_EXPANSION_RATIO,
    }


def binding(intake_id: str, scope: str, tenant_id: str | None) -> dict[str, Any]:
    """The fields that bind a report to the one intake swarm-api minted it for."""
    return {
        "schema": REPORT_SCHEMA,
        "intake_id": intake_id,
        "scope": scope,
        "tenant_id": tenant_id,
        "runnable_object": f"staging/{intake_id}/runnable.tar",
        "limits": _limits(),
    }


def refused_report(bound: dict[str, Any], refusal: Refusal, *, source_sha256: str | None, source_bytes: int | None) -> dict[str, Any]:
    return {
        **bound,
        "outcome": "refused",
        "refusal": refusal.as_report(),
        "source_sha256": source_sha256,
        "source_bytes": source_bytes,
    }


def run_intake(
    packed: bytes,
    *,
    intake_id: str,
    scope: str,
    tenant_id: str | None,
    subdir: str = "",
    overlay: str = "",
) -> tuple[dict[str, Any], bytes | None]:
    """The report, and the runnable tar when the archive is pinned (None when refused)."""
    bound = binding(intake_id, scope, tenant_id)
    source_sha256 = sha256(packed)
    try:
        return _pin(packed, bound, source_sha256, subdir, overlay)
    except Refusal as refusal:
        return refused_report(bound, refusal, source_sha256=source_sha256, source_bytes=len(packed)), None


def _pin(packed: bytes, bound: dict[str, Any], source_sha256: str, subdir: str, raw_overlay: str) -> tuple[dict[str, Any], bytes]:
    if len(packed) > limits.MAX_PACKED_BYTES:
        raise Refusal("archive_too_large")
    overlay = parse_overlay(raw_overlay)
    tree = plugin_tree(read_archive(packed), subdir)
    check_dependencies(tree)

    own_manifest = MANIFEST in tree
    document = parse_manifest(tree)[1] if own_manifest else None
    checked = apply_overlay(tree, overlay, has_manifest=own_manifest, has_hooks=has_own_hooks(tree, document))
    if MANIFEST not in tree:
        raise Refusal("manifest_missing")
    manifest, document = parse_manifest(tree)
    components = list_components(tree, document, tree[MANIFEST].index)

    runnable = canonical_tar(tree)
    files = sum(1 for node in tree.values() if node.kind in (FILE, SYMLINK))
    report = {
        **bound,
        "outcome": "pinned",
        "refusal": None,
        "source_sha256": source_sha256,
        "source_bytes": len(packed),
        "runnable_sha256": sha256(runnable),
        "runnable_bytes": len(runnable),
        "runnable_format": CANONICAL_TAR,
        "files": files,
        "unpacked_bytes": sum(len(node.data) for node in tree.values()),
        "manifest": manifest,
        "manifest_source": "plugin" if own_manifest else "overlay",
        "components": components,
        "overlay": checked,
        "requires": {
            "suggested": suggest_requires(tree, components),
            "declared": checked["requires"] if checked else None,
        },
    }
    return report, runnable
