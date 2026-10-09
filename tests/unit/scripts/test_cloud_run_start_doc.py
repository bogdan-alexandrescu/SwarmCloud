"""The Cloud Run start finding of 2026-10-07 is recorded, and the support case is drafted (#625).

WHY THIS EXISTS. docs/worker-images.md explained claude-code's 71 s -> ~167 s
start on Cloud Run Jobs as image weight, and its table tied the 10-04/05 step
to the repo-index toolchain. The operator's registry bisect (2026-10-05)
refuted that, and the measurement of 2026-10-07 (#625's second comment) put
90-95 % of DISPATCHED -> STARTING in Cloud Run's own `ResourcesAvailable ->
Started`, with the 27.6 MB swarm-verify job moving the same way. The owner
decided the same day that a Cloud Run support case is drafted for them to file.
So:

  * worker-images.md names `ResourcesAvailable`, the swarm-verify control and
    scripts/warm-jobs.sh, and no longer ties the step to the toolchain without
    the refutation beside it;
  * every profile the doc's "where each profile starts" table names has the
    backend the catalogue gives it -- read from `swarm_common.profiles`, never
    restated here -- and every profile in the catalogue is in that table;
  * the support-case runbook exists, is indexed under Incidents, and carries
    the figures, the slow windows, the controls and the fields the owner fills;
  * neither file names a resource on SHARED_DENY_LIST, parsed from
    scripts/lib/common.sh, and neither carries anything credential-shaped.

No credentials, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

REPO = Path(__file__).resolve().parents[3]
WORKER_IMAGES = REPO / "docs" / "worker-images.md"
RUNBOOKS = REPO / "docs" / "runbooks"
SUPPORT_CASE = RUNBOOKS / "cloud-run-start-support-case.md"
INDEX = RUNBOOKS / "README.md"
COMMON = REPO / "scripts" / "lib" / "common.sh"
LOCALS = REPO / "terraform" / "infra" / "locals.tf"

SECTION = "## Why the repository index has its own image (#625)"
WHERE = "### Where each profile starts today"

# A row of the "where each profile starts" table: | `name` | `BACKEND` | ...
ROW_RE = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|\s*`([A-Z_]+)`\s*\|(.*)\|\s*$")


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


def _where_rows() -> dict[str, tuple[str, str]]:
    body = _section(_text(WORKER_IMAGES), WHERE, 3)
    rows: dict[str, tuple[str, str]] = {}
    for line in body.splitlines():
        m = ROW_RE.match(line)
        if m:
            assert m.group(1) not in rows, f"{m.group(1)} is in the table twice"
            rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def _profiles_without_a_job() -> set[str]:
    m = re.search(r"^\s*profiles_without_a_job\s*=\s*\[([^\]]*)\]", _text(LOCALS), flags=re.M)
    assert m, "terraform/infra/locals.tf no longer declares profiles_without_a_job"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _deny_list() -> list[str]:
    m = re.search(r"^SHARED_DENY_LIST=\(\n(.*?)^\)", _text(COMMON), flags=re.M | re.S)
    assert m, "scripts/lib/common.sh no longer declares SHARED_DENY_LIST"
    names = re.findall(r'^\s*"([^"]+)"', m.group(1), flags=re.M)
    assert len(names) >= 10, names
    return names


# --------------------------------------------------------------------------
# worker-images.md: the 2026-10-07 finding
# --------------------------------------------------------------------------


def test_worker_images_records_where_the_start_goes():
    body = _flat(_section(_text(WORKER_IMAGES), SECTION, 2))
    assert "ResourcesAvailable" in body and "Started" in body
    for figure in ("p50 138 s", "p90 227 s", "p50 128 s", "p90 212 s", "90-95 %"):
        assert figure in body, figure
    # The population and the controls the figures were measured against.
    for figure in ("874", "497", "811", "446", "313"):
        assert figure in body, figure
    assert "10-02 20:37Z" in body and "10-04 05:14Z" in body


def test_worker_images_records_the_swarm_verify_control():
    body = _flat(_section(_text(WORKER_IMAGES), SECTION, 2))
    assert "swarm-verify" in body
    assert "27.6 MB" in body
    assert "76 -> 153 -> 97 -> 191 s" in body


def test_worker_images_says_image_size_shows_only_in_the_import_warm_jobs_absorbs():
    body = _flat(_section(_text(WORKER_IMAGES), SECTION, 2))
    assert "scripts/warm-jobs.sh" in body
    assert "30-59 s" in body
    assert (REPO / "scripts" / "warm-jobs.sh").is_file()
    assert "warm-jobs.sh" in _text(REPO / ".github" / "workflows" / "release.yml")


def test_worker_images_states_the_confidence_and_why_it_is_medium():
    body = _flat(_section(_text(WORKER_IMAGES), SECTION, 2))
    assert "Direct VPC egress" in body
    assert re.search(r"[Hh]igh\b.{0,80}provisioning", body)
    assert re.search(r"[Mm]edium\b.{0,80}Google", body)


def test_the_bisect_table_no_longer_ties_the_step_to_the_toolchain_alone():
    body = _section(_text(WORKER_IMAGES), SECTION, 2)
    rows = [line for line in body.splitlines() if line.startswith("| 10-04, 10-05 |")]
    assert len(rows) == 1, rows
    row = rows[0]
    # The row may say what landed on main, but only with the refutation in it.
    assert "bisect" in row and "no change" in row, row
    assert "ResourcesAvailable" in row or "Cloud Run" in row, row
    header = next(line for line in body.splitlines() if line.startswith("| days | p50 |"))
    assert "not what was deployed" in header or "not the cause" in header, header


def test_worker_images_points_at_request_53_for_what_returns_claude_code():
    body = _flat(_section(_text(WORKER_IMAGES), SECTION, 2))
    assert "contract-change-requests.md" in body
    assert re.search(r"request 53", body)
    assert "docs/runbooks/cloud-run-start-support-case.md" in body or "cloud-run-start-support-case.md" in body


# --------------------------------------------------------------------------
# worker-images.md: where each profile starts, read from the catalogue
# --------------------------------------------------------------------------


def test_every_profile_in_the_catalogue_is_in_the_where_table():
    rows = _where_rows()
    assert rows, "the 'where each profile starts' table has no rows"
    missing = sorted(set(RUNNER_PROFILES) - set(rows))
    assert not missing, f"the table does not name: {', '.join(missing)}"
    unknown = sorted(set(rows) - set(RUNNER_PROFILES))
    assert not unknown, f"the table names profiles the catalogue does not have: {', '.join(unknown)}"


@pytest.mark.parametrize("name", sorted(RUNNER_PROFILES))
def test_each_stated_backend_is_the_catalogues(name: str):
    rows = _where_rows()
    if name not in rows:
        pytest.skip(f"{name} is not named by the doc (the test above fails for it)")
    stated, _ = rows[name]
    assert stated == resolve_backend(RUNNER_PROFILES[name]).value, (
        f"docs/worker-images.md says {name} starts on {stated}; the catalogue says "
        f"{resolve_backend(RUNNER_PROFILES[name]).value}"
    )


def test_the_table_says_which_cloud_run_profiles_have_no_job():
    rows = _where_rows()
    without = _profiles_without_a_job()
    assert without, "profiles_without_a_job is empty: re-read the premise"
    for name, (backend, rest) in rows.items():
        if backend != Backend.CLOUD_RUN_JOB.value:
            continue
        if name in without:
            assert "no Job" in rest, f"{name} has no Job (locals.tf profiles_without_a_job), the table must say so"
        else:
            assert "no Job" not in rest, f"{name} has a Job; the table says it has none"


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


def test_the_support_case_carries_the_figures_and_the_slow_windows():
    text = _flat(_text(SUPPORT_CASE))
    assert "ResourcesAvailable" in text
    for figure in ("p50 138 s", "p90 227 s", "p50 128 s", "p90 212 s", "90-95 %"):
        assert figure in text, figure
    for window in ("10-04 ~01h", "10-05 ~12h", "10-06 23h", "09-20..22"):
        assert window in text, window
    assert re.search(r"10-60 s apart", text)
    assert "same second" in text


def test_the_support_case_carries_the_controls():
    text = _flat(_text(SUPPORT_CASE))
    assert "swarm-verify" in text and "27.6 MB" in text
    assert "GKE" in text and "p50 15 s" in text and "p90 83 s" in text
    for shape in ("4 CPU", "8 GiB", "gen2", "Direct VPC egress"):
        assert shape in text, shape
    assert re.search(r"same region", text)


def test_the_support_case_says_what_we_ask_and_what_the_owner_fills_in():
    text = _text(SUPPORT_CASE)
    assert re.search(r"^#{2,3} .*[Ww]hat we ask", text, flags=re.M)
    # The fields the owner fills are marked, and the execution names are read live.
    assert "<FILL IN:" in text
    assert re.search(r"<FILL IN:[^>]*project number", text)
    assert re.search(r"<FILL IN:[^>]*execution", text)
    assert "gcloud run jobs executions describe" in text


# --------------------------------------------------------------------------
# both: nothing on the deny-list, nothing credential-shaped
# --------------------------------------------------------------------------

_CREDENTIAL_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|ya29\.[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


def _names_deny_listed(text: str, name: str) -> bool:
    """Whether the text names a deny-listed resource.

    A name with punctuation in it (`agents-staging-vpc`, an account's email)
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
