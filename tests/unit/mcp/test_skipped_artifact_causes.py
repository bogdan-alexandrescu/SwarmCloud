"""A skipped artifact is shown with the cause the worker recorded (#165).

The worker stores `result_summary.artifacts_skipped` entries as
`{name, cause}`; the listing route answers names with
`artifacts_skipped_causes` beside them. `outputs_of` reads either source, and
a bare name from a summary that predates causes is not described as the cap.
"""

from __future__ import annotations

from swarm_mcp import render
from swarm_mcp.client import outputs_of


def _finished(summary: dict) -> dict:
    return {"state": "SUCCEEDED", "result_summary": {"artifacts": [], **summary}}


def test_a_summary_entry_with_a_cause_is_named_with_it() -> None:
    produced = outputs_of(
        _finished(
            {
                "artifacts_skipped": [
                    {"name": "core.dump", "cause": "cap"},
                    {"name": "notes.md", "cause": "upload_error"},
                    "old.bin",
                ]
            }
        )
    )
    assert produced["artifacts_skipped"] == ["core.dump", "notes.md", "old.bin"]
    assert produced["artifacts_skipped_causes"] == {"core.dump": "cap", "notes.md": "upload_error"}

    text = "\n".join(render.produced_lines(produced, render.PLAIN))
    assert "core.dump (over the artifact size cap)" in text
    assert "notes.md (the upload failed)" in text
    assert "old.bin (cause not recorded)" in text
    assert "{" not in text, "an entry was stringified as a dict"


def test_the_listing_routes_causes_are_read() -> None:
    listing = {
        "artifacts": [],
        "artifacts_skipped": ["notes.md"],
        "artifacts_skipped_causes": {"notes.md": "upload_error"},
        "complete": True,
    }
    produced = outputs_of(_finished({}), listing)
    assert produced["artifacts_skipped_causes"] == {"notes.md": "upload_error"}
    assert "notes.md (the upload failed)" in "\n".join(render.produced_lines(produced, render.PLAIN))
