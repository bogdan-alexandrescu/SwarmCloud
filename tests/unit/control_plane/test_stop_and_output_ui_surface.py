# source-text: contract - the stop control and ArtifactViewer.tsx must offer cancel on the states the API accepts and render artifact content without HTML injection
"""The two console surfaces added for B28 and B29, held to what the API does.

Read as TEXT, like `test_blocker_ui_surface.py` and `test_runtimes_screen.py`
and for the reason those files give: `make lint` and `make test` never run
`tsc`, `check-contract-parity.sh` reads shell and jq and no `.ts` file at all,
and a mock of the UI would agree with whatever the UI happens to do.

WHAT IS PINNED HERE, and why each one is a defect waiting rather than a style
preference:

  1. THE STOP CONTROL IS ACTUALLY WIRED. `POST /v1/tasks/{id}/cancel` worked
     for weeks with no caller. A component that exists and is rendered by
     nothing is the same hole with a nicer filename. That each cancel route
     has one client function, called from one confirming file, is held by
     the source-text rules `one-client-function-per-cancel-route`,
     `cancel-task-only-from-stop-run` and `cancel-workflow-only-from-workflows`
     (scripts/source_text_rules/control_plane_nz.py).
  2. A STOP ALREADY REQUESTED IS NOT OFFERED AGAIN. That the control is offered
     on exactly the states the API accepts is `contract.constants.test.ts`'s,
     against the constants generated from the frozen states (part of #72); that
     the API refuses those states and no other is
     test_cancel_request_is_not_a_cancel.py's.
  3. THE VIEWER NEVER BUILDS A LOCATION. The content route's whole security
     argument is that a caller names an artifact and the server resolves it. A
     client that assembled a key or a `gs://` uri into a request would be
     asking for the route to grow the parameter that makes it a traversal hole.
  4. THE VIEWER NEVER TURNS ARTIFACT BYTES INTO HTML. An artifact is a string
     an agent wrote. `dangerouslySetInnerHTML` anywhere near it is script
     injection with a review comment attached.
  5. TRUNCATION AND REDACTION REACH THE SCREEN. The server reports both; a
     viewer that dropped either would undo the reporting it depends on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
UI = ROOT / "apps/swarm-ui/src"

pytestmark = pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui is not checked out")


def src(name: str) -> str:
    return (UI / name).read_text()


def code(name: str) -> str:
    """The file with its comments removed.

    Necessary rather than tidy: this house writes long explanatory headers, and
    several of the rules below are spelled out in prose in the very file they
    govern -- "there is no `dangerouslySetInnerHTML` in this file" would fail a
    naive substring check against itself.

    Block comments only, plus lines that START with `//`. A general `//`
    stripper would eat the `https://` inside a string literal, which is exactly
    the text some of these checks are looking for.
    """
    text = re.sub(r"/\*.*?\*/", "", src(name), flags=re.DOTALL)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


# --------------------------------------------------------------------------
# 1. The control exists AND is rendered
# --------------------------------------------------------------------------

@pytest.mark.parametrize("screen", ["AgentDetail.tsx", "Workflows.tsx"])
def test_the_stop_control_is_rendered_by_the_screens_that_need_it(screen) -> None:
    """B28 names both surfaces: a running node, and the run detail."""
    text = src(screen)
    assert "from './StopRun'" in text, (
        f"{screen} does not import StopRun; the cancel route has a component "
        "and no caller again."
    )
    assert "<StopRun" in text, f"{screen} imports StopRun and never renders it."


# --------------------------------------------------------------------------
# 2. It is offered once
# --------------------------------------------------------------------------

def test_the_control_does_not_reappear_once_a_stop_is_requested() -> None:
    """A second press would imply the first had not taken.

    `request_cancel` is idempotent server-side, so this is about the sentence
    the screen makes rather than about the write -- but "stop" offered again
    over a task that is already stopping reads as a failed first attempt.
    """
    body = src("types.ts")
    match = re.search(
        r"export function canBeStopped\([^)]*\)[^{]*\{(.*?)\n\}", body, re.DOTALL
    )
    assert match is not None
    assert "cancel_requested" in match.group(1), (
        "canBeStopped ignores cancel_requested, so the control stays offered on "
        "a task whose stop is already recorded."
    )


# --------------------------------------------------------------------------
# 3. The viewer names an artifact; it never builds a location
# --------------------------------------------------------------------------

def test_the_content_request_carries_a_name_and_nothing_else() -> None:
    """The route's security argument restated as a property of the client.

    `loadArtifactContent` may put `name`, `offset` and `limit_bytes` on the
    query and nothing else. A `key`, a `uri`, a `prefix` or a `path` parameter
    appearing here would be the client half of a path-traversal hole.
    """
    api = code("api.ts")
    match = re.search(
        r"export async function loadArtifactContent\((.*?)\n\}", api, re.DOTALL
    )
    assert match is not None, "loadArtifactContent is not in api.ts"
    body = match.group(1)
    params = set(re.findall(r"query\.set\('([a-z_]+)'", body))
    params |= set(re.findall(r"URLSearchParams\(\{\s*([a-z_]+)", body))
    assert params == {"name", "offset", "limit_bytes"}, (
        f"loadArtifactContent sends {sorted(params)}. Only an artifact NAME and "
        "the window bounds may be sent; the server resolves the object."
    )
    assert "gs://" not in body, (
        "loadArtifactContent mentions a gs:// uri. The client must never send a "
        "location, and building one here is how it starts."
    )


def test_the_viewer_never_constructs_a_gcs_location_for_a_request() -> None:
    """`uri` and `key` are for DISPLAY in the viewer, never for a request."""
    viewer = code("ArtifactViewer.tsx")
    assert "loadArtifactContent(taskId, artifact.name)" in viewer, (
        "the viewer does not pass the manifest name straight through; whatever "
        "it passes instead is a value this test has not checked."
    )
    for forbidden in ("attempts/", "tenants/", "artifacts/'"):
        assert forbidden not in viewer, (
            f"ArtifactViewer.tsx contains {forbidden!r}, which means it is "
            "assembling an object key. It must not know the key layout at all."
        )


# --------------------------------------------------------------------------
# 4. Artifact bytes never become HTML
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    # Artifacts.tsx (#184) renders the agent's answer and every transcript
    # step -- the most agent-written text on any screen.
    "name", ["ArtifactViewer.tsx", "AgentDetail.tsx", "Workflows.tsx", "StopRun.tsx", "Artifacts.tsx"]
)
def test_no_artifact_bytes_are_turned_into_html(name) -> None:
    """An artifact is the least trustworthy string in this application."""
    assert "dangerouslySetInnerHTML" not in code(name), (
        f"{name} uses dangerouslySetInnerHTML. Artifact and transcript content "
        "is written by an agent; rendering it as HTML is script injection."
    )


def test_only_http_links_in_rendered_markdown_become_anchors() -> None:
    """`javascript:` in an agent's markdown must not become a clickable link."""
    viewer = src("ArtifactViewer.tsx")
    match = re.search(
        r"function link\(href: string, text: string, key: number\): ReactNode \{(.*?)\n\}",
        viewer,
        re.DOTALL,
    )
    assert match is not None, "the link() helper is not in ArtifactViewer.tsx"
    body = match.group(1)
    assert re.search(r"\^https\?", body), (
        "link() does not test the href against an http(s) allow-list, so any "
        "scheme an artifact names becomes an anchor."
    )
    assert "return <span" in body, (
        "link() has no non-anchor branch, so a rejected scheme renders as "
        "nothing instead of as its own text."
    )


# --------------------------------------------------------------------------
# 5. Truncation and redaction reach the screen
# --------------------------------------------------------------------------

def test_the_viewer_renders_the_truncation_the_server_reports() -> None:
    """The server caps the window and says so. Dropping that undoes the point."""
    viewer = src("ArtifactViewer.tsx")
    assert "data.truncated" in viewer, (
        "the viewer never reads `truncated`, so a capped window renders as the "
        "whole artifact -- which is the failure the server route was shaped to "
        "make impossible."
    )
    assert "not the whole artifact" in viewer, (
        "the viewer reads `truncated` and does not say so in words."
    )


def test_the_viewer_reports_when_a_credential_was_masked() -> None:
    """The count is the difference between 'clean' and 'rotate these'."""
    viewer = src("ArtifactViewer.tsx")
    assert "data.redaction_count" in viewer and "data.redacted" in viewer, (
        "the viewer does not surface read-time redaction, so an artifact with "
        "four credentials in it looks identical to a clean one."
    )


def test_the_viewer_distinguishes_all_four_content_statuses() -> None:
    """`ok`, `absent`, `unreadable`, `binary` -- four facts, four panels."""
    viewer = src("ArtifactViewer.tsx")
    for status in ("absent", "unreadable", "binary"):
        assert f"data.status === '{status}'" in viewer, (
            f"the viewer has no branch for status {status!r}, so it renders as "
            "an ordinary empty document."
        )
    assert "content === ''" in viewer, (
        "the viewer does not separate a zero-byte artifact from a missing one; "
        "`''` is a file the agent created and left blank."
    )
