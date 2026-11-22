"""The Cloud Run start finding of 2026-10-07: code-only and link/security checks
left after slice 1 of issue #71 dropped the doc-prose assertions.

No credentials, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

REPO = Path(__file__).resolve().parents[3]
WORKER_IMAGES = REPO / "docs" / "worker-images.md"
RUNBOOKS = REPO / "docs" / "runbooks"
SUPPORT_CASE = RUNBOOKS / "cloud-run-start-support-case.md"
INDEX = RUNBOOKS / "README.md"
DENY_LIST_EXAMPLE = REPO / "scripts" / "lib" / "deny-list.example.json"
LOCALS = REPO / "terraform" / "infra" / "locals.tf"

SECTION = "## Why the repository index has its own image"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _section(text: str, heading: str, level: int) -> str:
    """The body under a heading, up to the next heading of the same or a higher level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _profiles_without_a_job() -> set[str]:
    m = re.search(r"^\s*profiles_without_a_job\s*=\s*\[([^\]]*)\]", _text(LOCALS), flags=re.M)
    assert m, "terraform/infra/locals.tf no longer declares profiles_without_a_job"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _deny_list() -> list[str]:
    body = json.loads(_text(DENY_LIST_EXAMPLE))
    names = body["entries"]
    assert len(names) >= body["floors"]["total"] > 0, names
    return names


def test_worker_images_says_image_size_shows_only_in_the_import_warm_jobs_absorbs():
    body = _flat(_section(_text(WORKER_IMAGES), SECTION, 2))
    assert "scripts/warm-jobs.sh" in body
    assert "30-59 s" in body
    assert (REPO / "scripts" / "warm-jobs.sh").is_file()
    assert "warm-jobs.sh" in _text(REPO / ".github" / "workflows" / "release.yml")


# --------------------------------------------------------------------------
# terraform/infra/locals.tf: profiles_without_a_job can only name Cloud Run profiles
# --------------------------------------------------------------------------


def test_profiles_without_a_job_are_all_backed_by_cloud_run():
    """locals.tf's profiles_without_a_job can only list a profile the catalogue backs by
    Cloud Run Jobs: a GKE profile never gets a Cloud Run Job in the first place, so
    naming one here would be a silent no-op rather than suppressing anything."""
    without = _profiles_without_a_job()
    assert without, "profiles_without_a_job is empty: re-read the premise"
    for name in without:
        # MUTATION: add a GKE-backed profile to profiles_without_a_job in locals.tf -> red.
        assert resolve_backend(RUNNER_PROFILES[name]) is Backend.CLOUD_RUN_JOB, name


# --------------------------------------------------------------------------
# the support-case runbook
# --------------------------------------------------------------------------


def test_the_support_case_runbook_exists_and_is_indexed_under_incidents():
    assert SUPPORT_CASE.is_file()
    index = _text(INDEX)
    incidents = _section(index, "## Incidents", 2)
    lines = [line for line in incidents.splitlines() if f"]({SUPPORT_CASE.name})" in line]
    assert len(lines) == 1, lines
    assert lines[0].lstrip().startswith("- ["), lines[0]


# --------------------------------------------------------------------------
# both: nothing on the deny-list, nothing credential-shaped
# --------------------------------------------------------------------------

_CREDENTIAL_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|ya29\.[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _names_deny_listed(text: str, name: str) -> bool:
    """Whether the text names a deny-listed resource.

    A name with punctuation in it (`peer-vpc`, an account's email)
    cannot occur in prose, so any occurrence counts. A bare word (`default`,
    the other team's VPC network) is an ordinary English word, so it counts
    only where it is written as a resource: in backticks, or as `default`
    network / VPC / subnet.
    """
    if re.search(r"[^A-Za-z0-9]", name):
        return name in text
    escaped = re.escape(name)
    return bool(
        re.search(rf"`{escaped}`", text)
        or re.search(rf"\b{escaped}\s+(network|VPC|vpc|subnet)", text)
        or re.search(rf"networks?/{escaped}\b", text)
    )


@pytest.mark.parametrize("doc", [WORKER_IMAGES, SUPPORT_CASE], ids=lambda p: p.name)
def test_neither_file_names_a_deny_listed_resource(doc: Path):
    text = _text(doc)
    named = [name for name in _deny_list() if _names_deny_listed(text, name)]
    assert not named, f"{doc.name} names deny-listed resources: {', '.join(named)}"


def test_the_deny_list_check_can_fail():
    # The control: the check above is not vacuous for either kind of name.
    names = _deny_list()
    punctuated = next(n for n in names if "-" in n)
    assert _names_deny_listed(f"the {punctuated} cluster", punctuated)
    assert _names_deny_listed("the `default` network", "default")
    assert not _names_deny_listed("the default build", "default")


@pytest.mark.parametrize("doc", [WORKER_IMAGES, SUPPORT_CASE], ids=lambda p: p.name)
def test_neither_file_carries_anything_credential_shaped(doc: Path):
    assert not _CREDENTIAL_SHAPES.search(_text(doc))
