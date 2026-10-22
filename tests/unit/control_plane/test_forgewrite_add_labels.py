"""`GitHubWriter.add_labels`: a label added to an issue that already exists
(lane FR3-HOLDLABEL, part of #1933; docs/design/failure-review.md §6).

WHAT IS HELD HERE
-----------------
* ONLY A LABEL THE REPOSITORY ALREADY HAS IS POSTED. GitHub's
  `POST issues/{n}/labels` CREATES a label it does not know, so a misspelt or
  deleted label would appear on the repository unasked. The repository's
  labels are read once; the rest are dropped and returned, as `issue_create`
  reports `labels_dropped`.
* NOTHING TO ADD IS NO WRITE.
* A REFUSAL IS THE WRITER'S OWN MAPPING: a 403 is `ForgeWriteForbidden`, a 404
  is `ForgeWriteNotFound`, as for every other call it makes.

No cloud: GitHub is the in-memory fake under the real `GitHubWriter`.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from swarm_api import forgewrite
from swarm_api.validation import IssueRef

from . import forge_fakes

REF = IssueRef(owner="example-org", repo="widgets", number=0)
LABELS_PATH = "/repos/example-org/widgets/issues/42/labels"


def posts(writes: forge_fakes.GitHubWrites) -> list[tuple[str, str]]:
    return [w for w in writes.writes() if w[1].endswith("/labels")]


def test_only_labels_the_repository_has_are_posted_and_the_rest_are_returned_dropped() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.labels = ["bug", "needs-owner"]
    writer = forgewrite.GitHubWriter(send=writes)

    dropped = writer.add_labels(REF, 42, ["needs-owner", "made-up-label"], forge_fakes.make_token())

    # MUTATION: post the requested labels unfiltered and GitHub creates "made-up-label".
    assert writes.labelled == [(42, ["needs-owner"])]
    # MUTATION: return the kept labels (or nothing) and the caller cannot say what was dropped.
    assert dropped == ("made-up-label",)
    # MUTATION: read the labels once per requested label and this is two reads.
    reads = [u for m, u, _, _ in writes.calls if m == "GET" and urlparse(u).path.endswith("/widgets/labels")]
    assert len(reads) == 1


def test_a_label_the_repository_lacks_is_never_written() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.labels = ["bug"]
    writer = forgewrite.GitHubWriter(send=writes)

    dropped = writer.add_labels(REF, 42, ["needs-owner"], forge_fakes.make_token())

    # MUTATION: post an empty (or the unfiltered) list and a write is made for nothing.
    assert posts(writes) == []
    assert dropped == ("needs-owner",)


def test_a_refused_label_write_is_the_writers_forbidden() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.refuse[("POST", LABELS_PATH)] = 403
    writer = forgewrite.GitHubWriter(send=writes)

    # MUTATION: map the POST without `_call` (or swallow its answer) and no error is raised.
    with pytest.raises(forgewrite.ForgeWriteForbidden) as refused:
        writer.add_labels(REF, 42, ["needs-owner"], forge_fakes.make_token())
    assert forgewrite.ISSUES_WRITE in refused.value.message


def test_an_issue_that_is_gone_is_the_writers_not_found() -> None:
    writes = forge_fakes.GitHubWrites()
    writes.refuse[("POST", LABELS_PATH)] = 404
    writer = forgewrite.GitHubWriter(send=writes)

    # MUTATION: treat a 404 as success and a label on a deleted issue reads as applied.
    with pytest.raises(forgewrite.ForgeWriteNotFound):
        writer.add_labels(REF, 42, ["needs-owner"], forge_fakes.make_token())
