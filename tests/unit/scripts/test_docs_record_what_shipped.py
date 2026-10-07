"""Five documents said what the code did before it changed (wave 14, lane C4I).

WHY THIS EXISTS. A read-only triage of 2026-10-07 found sentences that had
been true and stopped being true, each pinned here against the value it
describes, read from the code or the tests wherever it can be:

  * docs/contract-change-requests.md's `AsymmetricSign` quota bullet told the
    reader to run `gcloud services quota list`, which gcloud 586 refuses
    (#453; the runbook was fixed by #456, this copy was not).
  * Contract request 30 still described the CI fixer's reach as "any
    `direct-pr` task"; since the owner's decision of 2026-10-06 (#754) it
    also continues an `integrate` workflow's integrator (#263).
  * docs/mirrored-values.md had no row for the GKE metadata server's address,
    which the dispatcher, three worker templates and the egress policy all
    restate (#76).
  * docs/issue-runs.md said auto-merge was "refused until #342 and #295/#352
    land" and "not buildable yet", and cited two tests that no longer exist;
    since contract request 47 the merge is built (#454).
  * docs/ci.md said the deployer holds no role on the fixer's account, but
    not that it holds no IAP role at all, nor where IAP membership lives (#76).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
REQUESTS = DOCS / "contract-change-requests.md"
MIRRORED = DOCS / "mirrored-values.md"
ISSUE_RUNS = DOCS / "issue-runs.md"
CI = DOCS / "ci.md"
DISPATCH = REPO / "apps" / "scheduler" / "scheduler" / "dispatch.py"
CONTINUATION = REPO / "apps" / "swarm-api" / "swarm_api" / "continuation.py"
ISSUERUNS = REPO / "apps" / "swarm-api" / "swarm_api" / "issueruns.py"
ISSUECI = REPO / "apps" / "swarm-api" / "swarm_api" / "issueci.py"
BOOTSTRAP_TEST = REPO / "tests" / "terraform" / "bootstrap.tftest.hcl"
WIF = REPO / "terraform" / "bootstrap" / "wif.tf"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin: text.index(end, begin + len(start))]


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of any level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^#{1,4} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _module_constant(path: Path, name: str) -> str:
    for node in ast.parse(_text(path)).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant), name
            return node.value.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            assert isinstance(node.value, ast.Constant), name
            return node.value.value
    raise AssertionError(f"{path.relative_to(REPO)} defines no {name}")


def _defines(path: Path, name: str) -> bool:
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
        for n in ast.walk(ast.parse(_text(path)))
    )


# --------------------------------------------------------------------------
# #453: the KMS quota command
# --------------------------------------------------------------------------

WORKING_QUOTA_COMMAND = "gcloud quotas info list --service=cloudkms.googleapis.com"


def test_the_kms_quota_bullet_names_the_command_gcloud_runs():
    """MUTATION: put `gcloud services quota list` back as the check to run."""
    bullet = _between(_text(REQUESTS), "* **`AsymmetricSign` quota, not verified.**", "#### 4.")
    assert WORKING_QUOTA_COMMAND in bullet, bullet
    # The control: the runbook this bullet now agrees with says the same.
    assert WORKING_QUOTA_COMMAND.split(" --")[0] in _text(
        DOCS / "runbooks" / "spec-signing-rollout.md"
    )


def test_the_dead_quota_command_is_only_named_as_dead():
    text = _text(REQUESTS)
    hits = [m.start() for m in re.finditer(r"gcloud services quota list", text)]
    for at in hits:
        around = text[at: at + 200]
        assert "does not exist" in around, (
            f"contract-change-requests.md names `gcloud services quota list` as a "
            f"command to run; gcloud 586 answers `Invalid choice: 'quota'`: {around!r}"
        )


# --------------------------------------------------------------------------
# #263: request 30 records the integrator widening
# --------------------------------------------------------------------------


def test_request_30_records_the_widening_to_an_integrate_workflows_integrator():
    """MUTATION: delete the 2026-10-06 addendum from request 30."""
    entry = _between(_text(REQUESTS), "## 30. ", "## 31. ")
    addendum = _between(entry, "**2026-10-06", "### What is true today")
    role = _module_constant(CONTINUATION, "INTEGRATOR_ROLE")
    strategy = _module_constant(CONTINUATION, "INTEGRATE_STRATEGY")
    for needle in (f"`{strategy}`", role, "#754", "contributor", "merge"):
        assert needle in addendum, (needle, addendum)
    # The symbol the addendum points at is the one that decides it.
    assert "`_has_own_pull_request`" in addendum
    assert _defines(CONTINUATION, "_has_own_pull_request")
    # The test it names exists.
    test = REPO / "tests" / "unit" / "control_plane" / "test_continuation_scope_is_narrow.py"
    name = "test_the_listed_account_can_continue_an_integrate_workflows_integrator"
    assert name in addendum and _defines(test, name)


# --------------------------------------------------------------------------
# #76: the metadata server address in the register
# --------------------------------------------------------------------------


def test_the_register_lists_the_gke_metadata_server_address():
    """MUTATION: delete the row, or let it name an address the dispatcher does not send."""
    address = _module_constant(DISPATCH, "GKE_METADATA_SERVER_IP")
    covered = _section(_text(MIRRORED), "## Covered elsewhere, deliberately not moved here")
    rows = [line for line in covered.splitlines() if line.startswith("|") and address in line]
    assert len(rows) == 1, f"expected one row naming {address}, got {rows}"
    row = rows[0]
    assert "`GKE_METADATA_SERVER_IP`" in row
    for path in (
        "tests/unit/control_plane/test_gke_worker_metadata_env.py",
        "tests/unit/worker/test_worker_templates_name_the_metadata_server.py",
    ):
        assert f"`{path}`" in row and (REPO / path).is_file(), path
    for template in ("worker-job.yaml", "worker-job-v2.yaml", "worker-job-browser.yaml"):
        assert template in row
        assert address in _text(REPO / "kubernetes" / "worker-templates" / template), template
    assert f"{address}/32" in _text(REPO / "kubernetes" / "network-policies" / "allow-egress.yaml")


# --------------------------------------------------------------------------
# #454: issue-runs says the merge is built
# --------------------------------------------------------------------------

_STALE_ISSUE_RUN_CLAIMS = (
    "refused until #342 and #295/#352 land",
    "not buildable yet",
    "visible but refused",
    "swarm-api\nhas no merge call",
    "Auto-merge: visible, refused, and why",
)


def test_issue_runs_no_longer_says_auto_merge_is_refused():
    """MUTATION: restore any of the stale sentences."""
    text = _text(ISSUE_RUNS)
    flat = " ".join(text.split())
    for claim in _STALE_ISSUE_RUN_CLAIMS:
        assert " ".join(claim.split()) not in flat, claim
    # The premise: the refusal really is conditional on a disabled profile, and
    # the CI loop really submits the merge.
    assert _defines(ISSUERUNS, "_auto_merge_refusal")
    assert _defines(ISSUECI, "merge_workflow") and _defines(ISSUECI, "_merge")


def test_issue_runs_says_the_merge_is_built_and_not_proven_live():
    section = _section(_text(ISSUE_RUNS), "### 4. ")
    assert "Built; not proven live" in section, section
    assert "`issueci._merge`" in section and "`issueruns._auto_merge_refusal`" in section


_CITED_TEST = re.compile(r"`((?:tests/[\w/]+/)?test_\w+\.py)::(\w+)`")


def test_every_python_test_issue_runs_cites_exists():
    """The two auto-merge refusal tests were renamed away; the doc kept citing them."""
    cited = _CITED_TEST.findall(_text(ISSUE_RUNS))
    # The anchor: the doc's acceptance tables were read, not an empty match.
    assert len(cited) > 40, len(cited)
    missing = []
    for file, name in cited:
        path = REPO / file if file.startswith("tests/") else (
            REPO / "tests" / "unit" / "control_plane" / file
        )
        if not path.is_file() or not _defines(path, name):
            missing.append(f"{file}::{name}")
    assert not missing, missing


def test_the_auto_merge_ui_tests_issue_runs_quotes_exist():
    section = _section(_text(ISSUE_RUNS), "### 4. ")
    tsx = _text(REPO / "apps" / "swarm-ui" / "src" / "__tests__" / "intake.issue.test.tsx")
    rows = [line for line in section.splitlines() if "intake.issue.test.tsx" in line]
    titles = [t for row in rows for t in re.findall(r'"([^"|]+)"', row)]
    assert titles, section
    for title in titles:
        assert title in tsx.replace("\\'", "'"), title


# --------------------------------------------------------------------------
# #76: the deployer holds no IAP role
# --------------------------------------------------------------------------


def test_ci_md_says_the_deployer_holds_no_iap_role_and_where_membership_lives():
    """MUTATION: delete the sentence from "What the owner configures, once"."""
    section = _section(_text(CI), "### What the owner configures, once")
    flat = " ".join(section.split())
    assert "holds no IAP role" in flat, flat
    assert "`frontend_accessors`" in flat and "frontend_accessors" in _text(WIF)
    run = "iap_membership_is_bootstrap_owned_and_the_deployer_has_no_iap_role"
    assert f"`{run}`" in flat and f'run "{run}"' in _text(BOOTSTRAP_TEST)
