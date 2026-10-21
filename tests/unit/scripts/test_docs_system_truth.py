"""The docs describe the system that runs (lane DOCS-TRUTH, part of #1822).

The 2026-10-21 system analysis read the live project and found three docs
describing a different one:

  * docs/disaster-recovery.md §1 gave Firestore's recovery point as "last
    export". No export was ever taken and no backup schedule exists; what the
    database `swarm` actually has is 7 days of point-in-time recovery, with
    delete protection off, so those versions die with the database.
  * docs/scaling.md said the control plane "autoscales on its own" and listed
    no network, regional-quota or namespace-Job ceiling.
  * docs/architecture.md called Cloud Run Jobs the primary backend and every
    workspace a tmpfs, when every agent profile but `mock` runs on GKE, where
    the workspace is disk.

Each test reads the value it asserts from the code or the Terraform where one
exists there, so a later change to that value turns this red instead of
leaving the doc quietly wrong again.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
DR = DOCS / "disaster-recovery.md"
SCALING = DOCS / "scaling.md"
ARCHITECTURE = DOCS / "architecture.md"
BACKENDS = DOCS / "execution-backends.md"
REQUESTS = DOCS / "contract-change-requests.md"
CONTRACT = REPO / "CONTRACT.md"
MAIN_TF = REPO / "terraform" / "infra" / "main.tf"
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"
NETWORK_TF = REPO / "terraform" / "modules" / "network" / "main.tf"
RENDER_PY = REPO / "kubernetes" / "render.py"
DISPATCH_PY = REPO / "apps" / "scheduler" / "scheduler" / "dispatch.py"

ANALYSIS = "2026-10-21"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of the same or a higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _flat(markdown: str) -> str:
    return " ".join(re.sub(r"^[ \t]*>[ \t]?", "", markdown, flags=re.M).split())


def _tf_number(text: str, pattern: str) -> int:
    match = re.search(pattern, text, flags=re.M | re.S)
    assert match, f"no match for {pattern!r}"
    return int(match.group(1))


# --------------------------------------------------------------------------
# docs/disaster-recovery.md: Firestore's recovery point
# --------------------------------------------------------------------------


def test_dr_gives_firestores_recovery_point_as_point_in_time_not_an_export():
    text = _text(DR)
    objectives = _section(text, "## 1. Recovery objectives")
    row = next(line for line in objectives.splitlines() if line.startswith("| Firestore data |"))
    # MUTATION: restore "| Firestore data | last export |" -> red.
    assert "last export" not in row, row
    assert "point-in-time" in row and "7 days" in row, row
    body = _flat(objectives)
    # What was read live, with its date and where.
    for fact in (ANALYSIS, "`gcloud firestore backups schedules list`", "`[]`",
                 "gs://swarm-artifacts-saga-agents-staging/backups/", "no export has ever been taken",
                 "deleted with the database", "#1818"):
        # MUTATION: drop any one of these facts -> red.
        assert fact in body, fact


def test_dr_says_what_delete_protection_and_abandon_do_from_the_terraform():
    main_tf = _text(MAIN_TF)
    assert re.search(r'module "firestore" \{.*?deletion_policy\s*=\s*"ABANDON"', main_tf, flags=re.S)
    assert re.search(r"^deletion_protection\s*=\s*false", _text(DEV_TFVARS), flags=re.M)
    body = _flat(_text(DR))
    # MUTATION: say ABANDON protects the database from deletion -> red (only from terraform destroy).
    assert '`deletion_policy = "ABANDON"`' in body
    assert "protects only against `terraform destroy`" in body
    assert "delete protection is off" in body
    assert "`deletion_protection = false`" in body


def test_dr_restores_point_in_time_into_a_new_database_never_swarm():
    restore = _section(_text(DR), "### Restore")
    # MUTATION: import straight into --database=swarm -> red.
    for line in restore.splitlines():
        assert not re.search(r"--database=swarm(?![-\w])", line) or "export" in line, line
    assert "--snapshot-time" in restore
    body = _flat(restore)
    assert "a NEW database id, never `swarm`" in body
    assert "--database=swarm-restore-" in restore


# --------------------------------------------------------------------------
# docs/scaling.md: the ceilings below the pools
# --------------------------------------------------------------------------

CEILINGS = "### Ceilings below the pools, measured 2026-10-21"


def test_scaling_no_longer_says_the_control_plane_autoscales_on_its_own():
    # MUTATION: restore "autoscales on its own" -> red.
    assert "autoscales on its own" not in _text(SCALING)
    assert CEILINGS in _text(SCALING)


def test_scaling_ceilings_are_the_terraform_and_code_values():
    import dataclasses

    from swarm_common.config import Settings

    tfvars = _text(DEV_TFVARS)
    api_max = _tf_number(tfvars, r'^\s*"swarm-api"\s*=\s*(\d+)')
    scheduler_max = _tf_number(tfvars, r'^\s*"swarm-scheduler"\s*=\s*(\d+)')
    nat_ips = _tf_number(tfvars, r"^nat_static_ip_count\s*=\s*(\d+)")
    main_tf = _text(MAIN_TF)
    scheduler_concurrency = _tf_number(main_tf, r'"swarm-scheduler" = \{.*?\bconcurrency\s*=\s*(\d+)')
    api_cpu = _tf_number(main_tf, r'"swarm-api" = \{.*?\bcpu\s*=\s*"(\d+)"')
    quota_jobs = _tf_number(_text(RENDER_PY), r'"--quota-jobs", type=int, default=(\d+)')
    rps = next(f.default for f in dataclasses.fields(Settings) if f.name == "requests_per_second")

    body = _flat(_section(_text(SCALING), CEILINGS))
    # MUTATION: write any number other than the Terraform's or the code's -> red.
    assert f"max {api_max} instances of {api_cpu} vCPU" in body
    assert f"{rps} req/s per principal per instance" in body
    assert f"concurrency {scheduler_concurrency}, max {scheduler_max} instances" in body
    assert f"{nat_ips} static IP" in body
    assert f"{quota_jobs} Jobs and {quota_jobs} PVCs" in body
    # The measured numbers, each with its source.
    for fact in ("3,044", "no available instance", "64,512", "655", "#1471", "216/3,000 vCPU",
                 "3,299/40,960 GB", "200 instances", "119 Jobs", "#1820", "1.6 admissions/s",
                 "`global`", ANALYSIS):
        # MUTATION: drop any one row's number or source -> red.
        assert fact in body, fact


# --------------------------------------------------------------------------
# docs/architecture.md and execution-backends.md: where each profile runs
# --------------------------------------------------------------------------


def _profiles_on(backend) -> list[str]:
    from swarm_common.profiles import RUNNER_PROFILES, resolve_backend

    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is backend)


def _fallback_profiles() -> list[str]:
    match = re.search(r"^\s*cloud_run_fallback_profiles\s*=\s*\[([^\]]*)\]", _text(LOCALS_TF), flags=re.M)
    assert match
    return re.findall(r'"([^"]+)"', match.group(1))


def test_architecture_no_longer_calls_cloud_run_the_primary_backend():
    text = _text(ARCHITECTURE)
    # MUTATION: restore the heading or the diagram's "(primary)" -> red.
    assert "### Cloud Run Jobs is the primary backend, not GKE Autopilot" not in text
    assert "(primary)" not in _section(text, "## 1. The shape")
    assert "Cloud Run Jobs is the default;" not in text


def test_architecture_dispatch_names_every_profile_on_its_backend():
    from swarm_common.profiles import Backend

    dispatch = _flat(_section(_text(ARCHITECTURE), "### Dispatch"))
    gke = _profiles_on(Backend.GKE_AUTOPILOT)
    cloud_run = _profiles_on(Backend.CLOUD_RUN_JOB)
    assert "mock" in cloud_run and "claude-code" in gke
    gke_part = dispatch[dispatch.index("GKE Autopilot runs"): dispatch.index("Cloud Run Jobs runs")]
    run_part = dispatch[dispatch.index("Cloud Run Jobs runs"):]
    for name in gke:
        # MUTATION: move a GKE profile into the Cloud Run sentence -> red.
        assert f"`{name}`" in gke_part, name
    for name in cloud_run:
        assert f"`{name}`" in run_part, name
    for name in _fallback_profiles():
        assert f"`{name}`" in run_part, f"the Cloud Run sentence does not name {name}'s fallback Job"
    assert "fallback" in run_part


def test_architecture_says_gke_workspaces_are_disk_and_tmpfs_is_cloud_run_only():
    from scheduler.dispatch import CACHE_VOLUME_GIB

    assert re.search(rf"^CACHE_VOLUME_GIB = {CACHE_VOLUME_GIB}$", _text(DISPATCH_PY), flags=re.M)
    text = _text(ARCHITECTURE)
    dispatch = _flat(_section(text, "### Dispatch"))
    # MUTATION: say the GKE workspace is a tmpfs, or drop the cache PVC -> red.
    for fact in ("disk `emptyDir`", "`/tmp`", "HOME", f"{CACHE_VOLUME_GIB} GiB cache PVC",
                 "tmpfs workspaces apply to Cloud Run only"):
        assert fact in dispatch, fact
    # The CLAUDE.md correction about Cloud Run ephemeral disk stays, unsoftened.
    retraction = _flat(_section(text, "### A known risk, accepted deliberately"))
    for phrase in ("**Preview**", "disables live migration", '`empty_dir.medium` accepts only `"MEMORY"`',
                   "fully-GA path, **with** live migration", "Cloud Run only"):
        assert phrase in retraction, phrase


def test_execution_backends_intro_names_where_each_profile_runs():
    intro = _flat(_text(BACKENDS).split("\n---\n", 1)[0])
    # MUTATION: restore "Cloud Run Jobs runs almost everything" -> red.
    assert "Cloud Run Jobs runs almost everything" not in intro
    assert "tmpfs" in intro and "Cloud Run only" in intro


# --------------------------------------------------------------------------
# CONTRACT.md's "Primary backend" line is a request, not an edit
# --------------------------------------------------------------------------


def test_contract_primary_backend_drift_is_a_request_not_an_edit():
    # CONTRACT.md is not edited by this lane: the line is still there.
    assert "- Primary backend Cloud Run Jobs." in _text(CONTRACT)
    text = _text(REQUESTS)
    heading = next((line for line in text.splitlines()
                    if line.startswith("## ") and "Primary backend Cloud Run Jobs" in line), None)
    # MUTATION: drop the entry -> red.
    assert heading is not None, "no request records CONTRACT.md's primary-backend drift"
    number = re.match(r"## (\d+)\.", heading).group(1)
    assert re.search(rf"^\| {number} \| .*Primary backend Cloud Run Jobs.* \| open", text, flags=re.M)
    entry = text[text.index(heading):]
    entry = entry[: entry.find("\n## ", 1)] if "\n## " in entry[1:] else entry
    assert "**Status:** open" in entry
    for part in ("### What is true today", "### The requested change", "### If it is declined", "#1822"):
        assert part in entry, part
