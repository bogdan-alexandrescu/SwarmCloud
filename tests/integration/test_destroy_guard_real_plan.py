"""The destroy guard, judged on a REAL terraform plan instead of a hand-written one.

WHY THIS FILE EXISTS, AND WHY IT IS NOT test_destroy_guard.py.

`make destroy` aborts on anything that belongs to another team and on anything
without `managed-by=swarm-terraform`. It is the only control between this
repository and deleting another team's production in `saga-agents-staging`:
their live GKE cluster `agents-staging`, their VPC, two subnets, three buckets,
twelve service accounts, and Firestore's shared `(default)` database.

Until 2026-09-24 that guard had never been run against a real
`terraform show -json`. Every test of it -- this suite's sibling, and
`destroy.sh --self-test` -- fed it jq fixtures written by hand, and a fixture
agrees with whoever wrote it. The drift was already measurable: the deny-list
restatement in the sibling held 14 of the 20 entries, in the wrong SHAPE (short
service-account names where production passes full emails), and a third copy of
the deny-list transformation inside `destroy.sh --self-test` had lost the
`(default)` entry, so the self-test of the guard protecting the shared Firestore
database could never reach that branch.

So this file judges a RECORDING of a real destroy plan:
`tests/integration/fixtures/destroy-plan-dev-real.json`, 264 real deletions
across 42 real resource types, produced by `scripts/destroy.sh --dry-run`
against `saga-agents-staging` and redacted by
`scripts/verify-destroy-guard.sh --record`. Values survive only for the fields
the guard reads; every other key survives by NAME, so the recording still states
what the real provider emits without carrying a service's environment, a
Firestore document's contents or a 7 KB dashboard.

WHAT THIS PROVES THAT A FIXTURE CANNOT.

* The guard reads fields the real format HAS. `google_container_cluster` spells
  its labels `resource_labels` and `google_monitoring_alert_policy` spells them
  `user_labels`; both branches exist in `destroy-guard.jq` and neither was ever
  exercised by real output. A guard reading a field the format does not have
  passes every hand-written fixture and aborts nothing.
* Every deny-list entry is reachable IN THE SHAPE IT REALLY APPEARS: a service
  account by `.email`, a bucket by `.name`, a cluster by an `.id` of
  `projects/<p>/locations/<loc>/clusters/<name>`, the shared default network by
  a `.network` self-link ending in `/default`.
* `make destroy` ABORTS. The cases below drive `scripts/destroy.sh` itself --
  the real script, its real guard, its real deny-list -- with a stub terraform
  that replays the recording, and assert that the script exits 2, names the
  resource, and never once reaches `terraform apply`.

The stub terraform is the only fake here, and it exists so this runs in CI with
no credentials, no state and nothing to destroy. If it is ever asked to apply,
it writes a marker file and fails; every abort case asserts that marker is
absent, because "the guard refused" and "the guard refused before apply" are
different claims.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The deny-list, parsed out of scripts/lib/common.sh EXACTLY ONCE for this
# directory. Importing the sibling rather than re-parsing is the whole lesson of
# the defect above: a second reader of the same list is a second list.
from test_destroy_guard import GUARD_ARGS, PREFIX, SHARED, UNLABELABLE  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "integration" / "fixtures" / "destroy-plan-dev-real.json"
CASES_FILE = REPO / "scripts" / "lib" / "destroy-guard-proof-cases.json"
GUARD_JQ = REPO / "scripts" / "lib" / "destroy-guard.jq"
PLAN_GUARD = REPO / "scripts" / "lib" / "plan-guard.sh"
COMMON_SH = REPO / "scripts" / "lib" / "common.sh"
DESTROY = REPO / "scripts" / "destroy.sh"
PROJECT = "saga-agents-staging"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or not DESTROY.exists(),
    reason="jq and scripts/destroy.sh are both required",
)

# NOT a skip. The recording is the subject of this file, so its absence is a
# collection error with an instruction, not a quietly green run: "unreadable is
# not the same as clean" is the rule the plan guard itself was fixed to obey.
assert FIXTURE.exists(), (
    f"{FIXTURE} is missing. It is a redacted recording of a real destroy plan; "
    f"regenerate it with:\n"
    f"    scripts/destroy.sh --environment dev --dry-run\n"
    f"    scripts/verify-destroy-guard.sh --record tests/integration/fixtures/destroy-plan-dev-real.json"
)

PLAN = json.loads(FIXTURE.read_text())
RECORDING = PLAN["_recording"]
CENSUS = RECORDING["before_keys_by_type"]
CASES = json.loads(CASES_FILE.read_text())

#: SHARED_DENY_LIST plus Firestore's `(default)`, which `guard_deny_json` adds --
#: the same set `scripts/verify-destroy-guard.sh` walks against a live plan.
ENTRIES = list(SHARED) + ["(default)"]

#: Identity fields destroy-guard.jq reads that NO resource in this plan carries.
#: `cluster` belongs to google_container_node_pool (Autopilot has none), and
#: `job`/`cluster_id` to types this deployment does not use. They are listed so
#: that the assertion below can require them to be genuinely absent: an
#: exception nobody re-checks becomes a hole.
ABSENT_FROM_THIS_PLAN = {"cluster", "job", "cluster_id"}


def _classify(entry: str) -> str:
    """The FIRST matching rule in the shared table, or a hard failure.

    `fnmatch` and bash's `[[ x == $pat ]]` agree on `*`, which is the only
    wildcard the table uses -- stated in the table's own description, because
    the two disagree about `[...]` and `!`.
    """
    for rule in CASES["classify"]:
        if fnmatch.fnmatchcase(entry, rule["glob"]):
            return rule["kind"]
    raise AssertionError(
        f"deny-list entry {entry!r} matches no rule in {CASES_FILE}. Classify it "
        f"there -- an unclassifiable neighbour must stop the proof, not be skipped: "
        f"a proof that quietly covered 14 of 20 entries is the defect this file answers."
    )


def _jq(args: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["jq", *args], capture_output=True, text=True, timeout=120, input=stdin
    )
    assert proc.returncode == 0, f"jq failed: {proc.stderr}"
    return proc.stdout


def _verdict(plan_path: Path) -> dict:
    """The guard's verdict, with every argument the filter declares.

    `GUARD_ARGS` is imported, not rebuilt: when `destroy-guard.jq` grew a
    required `$prefix` on 2026-09-23, four invocations across three files each
    had to learn it separately and three did not, so jq refused to COMPILE the
    filter (exit 3, no verdict) and `make destroy` could not run at all. One
    list, built where the deny-list is already parsed.
    """
    out = _jq(["-f", str(GUARD_JQ), *GUARD_ARGS, str(plan_path)])
    return json.loads(out)


def _index_of_type(type_: str) -> int:
    for i, change in enumerate(PLAN["resource_changes"]):
        if change["type"] == type_:
            return i
    raise AssertionError(f"the recorded plan holds no {type_} to carry a neighbour")


def _plan_with_neighbour(entry: str, tmp_path: Path) -> tuple[Path, str, str]:
    """A copy of the real plan with one REAL resource renamed to a neighbour.

    The patch comes from the shared table and is applied by jq, so this file and
    scripts/verify-destroy-guard.sh mutate identically rather than approximately.
    """
    kind = _classify(entry)
    spec = CASES["kinds"][kind]
    idx = _index_of_type(spec["carrier_type"])
    address = PLAN["resource_changes"][idx]["address"]
    out = tmp_path / f"plan-{kind}.json"
    out.write_text(
        _jq(
            [
                "--argjson", "i", str(idx),
                "--arg", "e", entry,
                "--arg", "p", PROJECT,
                f'.resource_changes[$i].change.before |= ({spec["patch"]})',
                str(FIXTURE),
            ]
        )
    )
    expected = entry if spec["expect_matched"] == "entry" else spec["expect_matched"]
    return out, address, expected


# ---------------------------------------------------------------------------
# What the recording is
# ---------------------------------------------------------------------------


def test_the_recording_is_a_real_destroy_plan():
    """Structure first: every assertion below is worthless over an empty array.

    MUTATION: truncate `resource_changes` to [] and this fails on the count
    instead of reporting a clean sweep over nothing -- the failure mode CLAUDE.md
    names ("Empty output is not success").
    """
    assert PLAN["format_version"], "no format_version; this is not terraform show -json output"
    assert PLAN["terraform_version"].startswith("1."), PLAN["terraform_version"]

    changes = PLAN["resource_changes"]
    assert len(changes) >= 200, (
        f"the recording holds {len(changes)} resource changes; a real dev destroy "
        f"plan for this platform is ~264, so a short one means the recording was "
        f"truncated and everything below is judging almost nothing"
    )
    assert len(changes) == RECORDING["verdict"]["deletions"], (
        "the recording's own verdict disagrees with its contents; re-record with "
        "scripts/verify-destroy-guard.sh --record"
    )

    for change in changes:
        assert change["change"]["actions"] == ["delete"], (
            f"{change['address']} is not a deletion: {change['change']['actions']}"
        )
        for field in ("address", "mode", "type", "name", "provider_name"):
            assert change.get(field), f"{change.get('address')} has no {field}"
        assert change["provider_name"].startswith("registry.terraform.io/"), change["provider_name"]


def test_every_field_the_guard_reads_is_carried_by_the_recording():
    """The recording must carry VALUES for every field destroy-guard.jq reads.

    This is the fixture-drift defect turned into a CI failure. If someone teaches
    the guard to read a new identity field, the recording stops covering the guard
    and this says so, naming the field and the command that re-records it --
    rather than the suite going green over a field that is always null.
    """
    source = GUARD_JQ.read_text()
    token_fields = set(re.findall(r"\$b\.([a-z_]+)\?", source))
    label_fields = set(re.findall(r"\$state\.([a-z_]+)", source))
    before_fields = set(re.findall(r"\.change\.before\.([a-z_]+)", source))
    read = token_fields | label_fields | before_fields

    assert len(token_fields) >= 15, (
        f"only {len(token_fields)} identity fields parsed out of {GUARD_JQ.name}; "
        f"the regex stopped matching and this test is now checking nothing"
    )

    carried = set(RECORDING["value_fields"])
    missing = sorted(read - carried)
    assert not missing, (
        f"destroy-guard.jq reads {missing}, and the recorded plan carries no VALUES "
        f"for those fields -- so every assertion here judges them as null. Re-record:\n"
        f"    scripts/destroy.sh --environment dev --dry-run\n"
        f"    scripts/verify-destroy-guard.sh --record {FIXTURE.relative_to(REPO)}"
    )


def test_the_identity_fields_the_guard_reads_exist_in_real_provider_output():
    """A guard that reads a field the real format does not have aborts nothing.

    Checked against the per-type key census of a real plan, not against the
    guard's own source. The three exceptions are named rather than tolerated: if
    one of them ever appears, this fails and asks for the list to shrink.
    """
    source = GUARD_JQ.read_text()
    token_fields = set(re.findall(r"\$b\.([a-z_]+)\?", source))
    real_keys = {key for keys in CENSUS.values() for key in keys}

    for field in sorted(token_fields - ABSENT_FROM_THIS_PLAN):
        assert field in real_keys, (
            f"destroy-guard.jq matches the deny-list against `before.{field}`, which "
            f"no resource in a real plan carries. Either the provider renamed it or "
            f"the read is dead -- in both cases a neighbour named there is not caught."
        )

    for field in sorted(ABSENT_FROM_THIS_PLAN):
        assert field not in real_keys, (
            f"`{field}` now DOES appear in real plan output, so it must not be "
            f"excused here any more; remove it from ABSENT_FROM_THIS_PLAN"
        )


def test_the_exotic_label_spellings_are_not_theoretical():
    """`resource_labels` and `user_labels` are real, and only real output shows it.

    destroy-guard.jq's label chain falls through five spellings. Two of them exist
    solely for these types, and a hand-written fixture would only ever contain the
    spellings its author remembered.
    """
    cluster_keys = CENSUS.get("google_container_cluster")
    assert cluster_keys, "the recording holds no google_container_cluster"
    assert "resource_labels" in cluster_keys, (
        "google_container_cluster no longer reports resource_labels; the guard's "
        "`.resource_labels` branch is what reads OUR cluster's managed-by label"
    )
    assert "labels" not in cluster_keys, (
        "google_container_cluster now has a plain `labels` too -- worth knowing, "
        "because the guard prefers it over resource_labels"
    )

    alert_keys = CENSUS.get("google_monitoring_alert_policy")
    assert alert_keys, "the recording holds no google_monitoring_alert_policy"
    assert "user_labels" in alert_keys

    source = GUARD_JQ.read_text()
    for spelling in ("labels", "effective_labels", "terraform_labels", "resource_labels", "user_labels"):
        assert f"$state.{spelling}" in source, f"the guard no longer reads {spelling}"


# ---------------------------------------------------------------------------
# The verdict over the real plan
# ---------------------------------------------------------------------------


def test_the_real_plan_is_allowed_and_every_deletion_is_classified():
    """The clean case, plus the partition that makes "0 offenders" mean something.

    223 exempt + 41 labelled = 264 deletions. A guard that classified nothing
    would also report zero offenders, which is why the sum is asserted and not
    just the zeros.
    """
    verdict = _verdict(FIXTURE)
    assert verdict["deletions"] == len(PLAN["resource_changes"])
    for key in ("offenders", "denylist_hits", "denylist_touches", "wrong_project", "unexpected_mutations"):
        assert verdict[key] == [], f"{key}: {json.dumps(verdict[key])[:400]}"

    classified = len(verdict["unlabelable_allowed"]) + len(verdict["labelled_ok"])
    assert classified == verdict["deletions"], (
        f"{classified} of {verdict['deletions']} deletions were classified as either "
        f"labelled or exempt; the rest fell through both and were judged by nothing"
    )
    assert len(verdict["labelled_ok"]) >= 40, (
        "almost nothing in this plan was recognised as carrying managed-by; the "
        "label chain has stopped reading real label maps"
    )
    # The data-bearing warning is part of the guard's real behaviour on this plan:
    # the Firestore database and both buckets.
    assert len(verdict["data_bearing"]) == 3, verdict["data_bearing"]


def test_the_carve_out_is_applied_where_it_should_be_and_nowhere_else():
    """Exempt from the label rule <=> the provider gives the type no label field.

    Read out of the real plan in BOTH directions. The one documented exception is
    google_monitoring_alert_policy, whose `user_labels` is the monitoring
    product's own payload rather than a resource label (see
    scripts/lib/unlabelable-types.json) -- and which carries managed-by there
    anyway in this plan.
    """
    label_fields = {"labels", "effective_labels", "terraform_labels", "resource_labels", "user_labels"}
    iam_shape = re.compile(r"_iam_(member|binding|policy)$")

    exempt_but_labelable = []
    unlabelable_but_not_exempt = []
    for type_, keys in CENSUS.items():
        exempt = type_ in UNLABELABLE or bool(iam_shape.search(type_))
        has_label_field = bool(label_fields & set(keys))
        if exempt and has_label_field:
            exempt_but_labelable.append(type_)
        if not exempt and not has_label_field:
            unlabelable_but_not_exempt.append(type_)

    assert exempt_but_labelable == ["google_monitoring_alert_policy"], (
        f"these types are exempted from the managed-by rule but DO carry a label "
        f"field in real provider output: {exempt_but_labelable}. An exemption for a "
        f"labelable type hides a missing label instead of describing the provider."
    )
    assert unlabelable_but_not_exempt == [], (
        f"these types carry no label field in real output and are not exempt: "
        f"{unlabelable_but_not_exempt}. make destroy will abort on them forever, and "
        f"a guard that can never pass is a guard someone deletes."
    )
    assert len(CENSUS) >= 40, f"only {len(CENSUS)} types in the census; the recording is thin"


def test_no_deletion_is_exempted_by_a_type_nobody_reviewed():
    """Fail-closed: the carve-out is a list plus one shape, never a guess."""
    verdict = _verdict(FIXTURE)
    iam_shape = re.compile(r"_iam_(member|binding|policy)$")
    stray = sorted(
        {
            entry["type"]
            for entry in verdict["unlabelable_allowed"]
            if entry["type"] not in UNLABELABLE and not iam_shape.search(entry["type"])
        }
    )
    assert not stray, f"exempted by neither the allow-list nor the IAM shape: {stray}"
    assert len(verdict["unlabelable_allowed"]) >= 200, (
        "the exemption applied to almost nothing; in this plan 223 of 264 deletions "
        "are IAM edges, API enablements, Firestore objects and network resources"
    )


def test_the_deny_list_still_covers_the_whole_shared_inventory():
    """The floor that a self-referential proof cannot provide for itself.

    Every deny case in this file iterates SHARED_DENY_LIST, so all of them shrink
    silently when the list does. MEASURED on 2026-09-24: deleting one service
    account from SHARED_DENY_LIST left scripts/verify-destroy-guard.sh reporting
    28 green assertions and exit 0. The inventory floors in
    scripts/lib/destroy-guard-proof-cases.json come from what CLAUDE.md and
    CONTRACT.md say is in the shared project, and are the independent statement.
    """
    floors = CASES["inventory_floor"]
    counts: dict[str, int] = {}
    for entry in ENTRIES:
        kind = _classify(entry)
        counts[kind] = counts.get(kind, 0) + 1

    assert len(SHARED) >= floors["total"], (
        f"SHARED_DENY_LIST holds {len(SHARED)} entries, below the floor of "
        f"{floors['total']}: one of the other team's resources has lost its protection"
    )
    for kind, floor in floors.items():
        if kind == "total":
            continue
        assert counts.get(kind, 0) >= floor, (
            f"{kind}: {counts.get(kind, 0)} deny-list entries, below the floor of {floor}"
        )


# ---------------------------------------------------------------------------
# make destroy itself, replaying the real plan
# ---------------------------------------------------------------------------

#: Stands in for terraform. Writes the -out file for `plan`, replays the recorded
#: JSON for `show -json`, and REFUSES to apply -- loudly, and by leaving evidence.
FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
mode=""
for arg in "$@"; do
  case "${arg}" in
    plan|show|apply|output) mode="${arg}"; break ;;
  esac
done
case "${mode}" in
  plan)
    for arg in "$@"; do
      case "${arg}" in -out=*) printf 'replayed plan\n' >"${arg#-out=}" ;; esac
    done
    ;;
  show)
    case " $* " in
      *" -json "*) cat "${SWARM_FAKE_PLAN}" ;;
      *)           printf '# replayed plan (human form)\n' ;;
    esac
    ;;
  apply)
    printf 'apply reached\n' >"${SWARM_FAKE_APPLY_MARKER}"
    printf 'FAKE TERRAFORM WAS ASKED TO APPLY A DESTROY PLAN\n' >&2
    exit 1
    ;;
  output) exit 1 ;;
  *)
    printf 'unexpected terraform invocation: %s\n' "$*" >&2
    exit 9
    ;;
esac
"""

#: destroy.sh requires gcloud to exist. Nothing on the paths exercised here calls
#: it -- every case aborts or ends at the dry run, long before the post-destroy
#: verification -- so a stub that answers nothing is the honest shape.
FAKE_GCLOUD = """#!/usr/bin/env bash
printf 'the replay harness never expects a gcloud call: %s\\n' "$*" >&2
exit 9
"""


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    """A throwaway REPO_ROOT holding the real scripts and a stub terraform.

    A copy rather than the repository itself, for one reason: destroy.sh requires
    `terraform/infra/.terraform` to exist, and a test must not create directories
    inside the checkout to satisfy itself.
    """
    root = tmp_path_factory.mktemp("replay-repo")
    shutil.copytree(REPO / "scripts", root / "scripts")
    (root / "terraform" / "infra" / ".terraform").mkdir(parents=True)
    (root / "terraform" / "environments" / "dev").mkdir(parents=True)
    (root / "terraform" / "environments" / "dev" / "dev.tfvars").write_text(
        "# replayed: the stub terraform never reads this\n"
    )

    bin_dir = root / "bin"
    bin_dir.mkdir()
    terraform = bin_dir / "terraform"
    terraform.write_text(FAKE_TERRAFORM)
    terraform.chmod(0o755)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
    env_file = root / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\nREGION=us-central1\nENVIRONMENT=dev\nFIRESTORE_DATABASE=swarm\n"
    )
    env_file.chmod(0o600)
    return {"root": root, "terraform": terraform, "gcloud_dir": bin_dir, "env_file": env_file}


def _run_destroy(replay, plan: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    marker = tmp_path / "apply-was-reached"
    env = dict(os.environ)
    env["PATH"] = f"{replay['gcloud_dir']}{os.pathsep}{env['PATH']}"
    # The override common.sh exposes for exactly this: PATH would not work, since
    # prefer_local_bin picks ~/.local/bin/terraform ahead of it.
    env["SWARM_TERRAFORM"] = str(replay["terraform"])
    env["SWARM_ENV_FILE"] = str(replay["env_file"])
    env["SWARM_FAKE_PLAN"] = str(plan)
    env["SWARM_FAKE_APPLY_MARKER"] = str(marker)
    env["NO_COLOR"] = "1"
    proc = subprocess.run(
        [str(replay["root"] / "scripts" / "destroy.sh"), "--environment", "dev", "--dry-run", *args],
        cwd=replay["root"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    assert not marker.exists(), (
        "destroy.sh reached `terraform apply`. Refusing late is not refusing:\n"
        + proc.stdout
        + proc.stderr
    )
    return proc


def test_the_real_plan_passes_make_destroy_end_to_end(replay, tmp_path):
    """The whole script, on the real plan, with nothing mutated.

    --include-data because the plan really does delete the Firestore database and
    both buckets, and destroy.sh stops on that first; the dry run still applies
    every safety assertion before it.
    """
    proc = _run_destroy(replay, FIXTURE, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript
    assert "all assertions passed" in transcript, transcript
    assert "no deny-listed shared resource appears in the plan" in transcript, transcript
    assert f"{len(PLAN['resource_changes'])} resource(s) marked for deletion" in transcript, transcript


def test_the_data_guard_stops_the_real_plan_when_data_was_not_requested(replay, tmp_path):
    """Three real data-bearing resources, named, with nothing destroyed."""
    proc = _run_destroy(replay, FIXTURE, tmp_path)
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 1, transcript
    assert "ABORTED to protect data" in transcript, transcript
    for address in (
        "module.firestore.google_firestore_database.this",
        "module.storage.google_storage_bucket.artifacts",
        "module.storage.google_storage_bucket.access_logs",
    ):
        assert address in transcript, f"{address} was not named:\n{transcript}"


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_neighbour_inside_a_real_resource_aborts_make_destroy(entry, replay, tmp_path):
    """EVERY deny-list entry, substituted into the REAL resource of its own type.

    Not a synthetic resource_change: the carrier is the actual
    google_service_account / google_storage_bucket / google_container_cluster /
    google_compute_network / google_compute_subnetwork / google_firestore_database
    out of the recorded plan, with the neighbour's identity written into the
    fields the provider really populates.

    This drives `scripts/destroy.sh`, so it exercises `.denylist_hits` -- the
    verdict key `make destroy` reads. The CI plan guard reads `.denylist_touches`
    instead, a different definition in the same jq file; on 2026-09-24 breaking
    `deny_hits` left every plan-guard assertion green while `make destroy` would
    have failed open on a deny-listed resource.
    """
    plan, address, expected = _plan_with_neighbour(entry, tmp_path)
    proc = _run_destroy(replay, plan, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr

    assert proc.returncode == 2, (
        f"make destroy did NOT abort on {entry} placed in {address}:\n{transcript}"
    )
    assert "belong to other teams" in transcript, transcript
    assert address in transcript, f"the refusal never named {address}:\n{transcript}"
    assert expected in transcript, f"the refusal never said what it matched:\n{transcript}"


def test_a_real_resource_stripped_of_managed_by_aborts_make_destroy(replay, tmp_path):
    """The label half, on a real label map rather than an invented one."""
    idx = next(
        i
        for i, change in enumerate(PLAN["resource_changes"])
        if (change["change"]["before"].get("labels") or {}).get("managed-by") == "swarm-terraform"
    )
    address = PLAN["resource_changes"][idx]["address"]
    plan = tmp_path / "unlabelled.json"
    plan.write_text(
        _jq(
            [
                "--argjson", "i", str(idx),
                ".resource_changes[$i].change.before |= (del(.labels[\"managed-by\"]) "
                "| del(.effective_labels[\"managed-by\"]) | del(.terraform_labels[\"managed-by\"]))",
                str(FIXTURE),
            ]
        )
    )
    proc = _run_destroy(replay, plan, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 2, transcript
    assert "do not carry managed-by=swarm-terraform" in transcript, transcript
    assert address in transcript, transcript


def test_a_near_miss_label_value_aborts_make_destroy(replay, tmp_path):
    """`managed-by=other-terraform` is somebody else's resource, not ours."""
    idx = next(
        i
        for i, change in enumerate(PLAN["resource_changes"])
        if (change["change"]["before"].get("labels") or {}).get("managed-by") == "swarm-terraform"
    )
    address = PLAN["resource_changes"][idx]["address"]
    plan = tmp_path / "nearmiss.json"
    plan.write_text(
        _jq(
            [
                "--argjson", "i", str(idx),
                '.resource_changes[$i].change.before |= (.labels["managed-by"] = "other-terraform" '
                '| .effective_labels["managed-by"] = "other-terraform" '
                '| .terraform_labels["managed-by"] = "other-terraform")',
                str(FIXTURE),
            ]
        )
    )
    proc = _run_destroy(replay, plan, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 2, transcript
    assert address in transcript, transcript
    assert "not swarm-terraform" in transcript, transcript


def test_an_empty_labels_map_with_the_truth_in_effective_labels_still_passes(replay, tmp_path):
    """The 2026-09-19 regression, asserted on real label maps.

    The google provider reports `labels: {}` on an update to a resource whose
    labels did not change, keeping the real values in `effective_labels`. An empty
    object is TRUTHY in jq, so `{} // .effective_labels` stops at `{}` and every
    such resource read as unlabelled -- the guard refused a legitimate plan. A
    guard that is wrong about ordinary work is one people learn to bypass.
    """
    idx = next(
        i
        for i, change in enumerate(PLAN["resource_changes"])
        if (change["change"]["before"].get("labels") or {}).get("managed-by") == "swarm-terraform"
        and (change["change"]["before"].get("effective_labels") or {}).get("managed-by")
        == "swarm-terraform"
    )
    plan = tmp_path / "emptylabels.json"
    plan.write_text(
        _jq(["--argjson", "i", str(idx), ".resource_changes[$i].change.before.labels = {}", str(FIXTURE)])
    )
    proc = _run_destroy(replay, plan, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, transcript
    assert "all assertions passed" in transcript, transcript


def test_a_destroy_plan_that_also_updates_something_aborts(replay, tmp_path):
    """State and reality disagree; that is not a destroy plan."""
    address = PLAN["resource_changes"][0]["address"]
    plan = tmp_path / "mutation.json"
    plan.write_text(_jq(['.resource_changes[0].change.actions = ["update"]', str(FIXTURE)]))
    proc = _run_destroy(replay, plan, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 2, transcript
    assert "state and reality disagree" in transcript, transcript
    assert address in transcript, transcript


def test_a_resource_in_another_project_aborts_make_destroy(replay, tmp_path):
    """`wrong_project` on real shape.

    Worth its own case because most IAM edges in a real plan carry no `project`
    field at all -- `google_storage_bucket_iam_member` and
    `google_service_account_iam_member` do not -- so a guard that required one
    would refuse every plan this platform produces.
    """
    address = PLAN["resource_changes"][0]["address"]
    plan = tmp_path / "wrongproject.json"
    plan.write_text(
        _jq(['.resource_changes[0].change.before.project = "some-other-project"', str(FIXTURE)])
    )
    proc = _run_destroy(replay, plan, tmp_path, "--include-data")
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 2, transcript
    assert "live in a different project" in transcript, transcript
    assert address in transcript, transcript


# ---------------------------------------------------------------------------
# The two defects a real plan found in the guard itself
# ---------------------------------------------------------------------------


def test_every_caller_of_the_guard_passes_every_argument_it_requires():
    """The defect that made `make destroy` impossible to run.

    `destroy-guard.jq` declares four arguments, and jq refuses to COMPILE a
    filter that references an undefined variable -- so a caller missing one does
    not get a lenient default, it gets exit 3 and no verdict at all. On
    2026-09-23 `$prefix` was added to the filter and to `plan-guard.sh`;
    `scripts/destroy.sh` invokes the same filter TWICE and got neither, so
    `destroy.sh --self-test` (a CI step) failed with

        jq: error: $prefix is not defined at <top-level>, line 217

    and `make destroy` could not reach a safety assertion at all. It failed
    closed, which is the right direction and is not a substitute for running.

    MUTATION: delete one `--arg prefix` from destroy.sh or plan-guard.sh and this
    fails, naming the file -- instead of the failure arriving at teardown, in
    front of another team's production.

    SINCE THE MERGE WITH MAIN (#17), `prefix` alone is read as
    `$ARGS.named.prefix // "swarm-"`, so a caller omitting it compiles and is
    judged by the filter's own default rather than failing. That is main's fix
    for the same outage and it stays; this case now asserts the other half --
    that nobody RELIES on the default -- and the case below it asserts the
    default still equals `guard_name_prefix`. The pattern accepts both
    spellings so the argument set is read from how the filter uses it, not
    from the name of a parameter.
    """
    required = set(
        re.findall(r"\$(?:ARGS\.named\.)?(deny|allow_types|project|prefix)\b", GUARD_JQ.read_text())
    )
    assert required == {"deny", "allow_types", "project", "prefix"}, (
        f"destroy-guard.jq's argument set is now {sorted(required)}; every caller below "
        f"must pass all of them, because jq will not compile the filter otherwise"
    )

    # The Python half, checked on the LIST rather than on the text: both test
    # modules build their invocation from one `GUARD_ARGS`, so this is the
    # statement that the list is complete.
    passed = {GUARD_ARGS[i] for i in range(1, len(GUARD_ARGS), 3)}
    assert passed == required, (
        f"GUARD_ARGS passes {sorted(passed)} but destroy-guard.jq declares "
        f"{sorted(required)}. jq will not compile the filter, so every case in "
        f"both of these files fails with '$X is not defined' -- which is how "
        f"CI run 35959558515 reported 56 failures for one missing argument."
    )
    assert str(COMMON_SH.read_text()).count("guard_name_prefix()") == 1, (
        f"{COMMON_SH.relative_to(REPO)} no longer defines exactly one "
        f"guard_name_prefix; the argument that broke every caller is back to "
        f"having more than one spelling"
    )

    flags = {
        "deny": "--argjson deny",
        "allow_types": "--argjson allow_types",
        "project": "--arg project",
        "prefix": "--arg prefix",
    }
    callers = (DESTROY, PLAN_GUARD, REPO / "scripts" / "verify-destroy-guard.sh")
    for caller in callers:
        # COMMENTS STRIPPED FIRST. All three of these files now explain the
        # argument in prose next to the call -- house style, and correct -- and a
        # count that included those lines would let a comment stand in for the
        # flag it is describing. destroy.sh mentions `--arg prefix` three times
        # and passes it twice.
        source = "\n".join(
            line for line in caller.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        invocations = source.count('jq -f "${GUARD_JQ}"')
        assert invocations >= 1, (
            f"{caller.relative_to(REPO)} no longer invokes the guard filter directly; "
            f"if it moved, move this assertion with it rather than deleting it"
        )
        for arg, flag in flags.items():
            assert source.count(flag) >= invocations, (
                f"{caller.relative_to(REPO)} runs destroy-guard.jq {invocations} time(s) "
                f"but passes `{flag}` only {source.count(flag)} time(s). The invocation "
                f"that misses it exits 3 with '${arg} is not defined' and judges nothing."
            )

    # And the two Python callers, which are where the 56 failures actually
    # landed. Every `-f <guard>` here must be followed by the shared list; a
    # hand-rolled invocation is how one of these two came to pass three of the
    # four arguments.
    for module in (Path(__file__), REPO / "tests" / "integration" / "test_destroy_guard.py"):
        source = module.read_text()
        invocations = source.count('"-f", str(GUARD_JQ)')
        assert invocations >= 1, (
            f"{module.relative_to(REPO)} no longer runs the guard filter; if the "
            f"invocation moved, move this assertion with it"
        )
        assert source.count("*GUARD_ARGS") >= invocations, (
            f"{module.relative_to(REPO)} runs destroy-guard.jq {invocations} time(s) "
            f"but spreads GUARD_ARGS only {source.count('*GUARD_ARGS')} time(s); the "
            f"hand-rolled invocation is the one that will miss the next argument"
        )


def test_the_filters_fallback_prefix_is_guard_name_prefix():
    """Two fixes for one outage, and the copy the pair of them leaves behind.

    The `$prefix is not defined` outage was fixed twice, on two branches that
    met in one merge: main made the argument optional inside destroy-guard.jq
    (`$ARGS.named.prefix // "swarm-"`), and this lane made every caller pass it
    from `guard_name_prefix` in common.sh. Both stay -- a new caller that
    forgets the argument still compiles -- but the filter's default is now a
    second spelling of `guard_name_prefix`'s. If they drift, a caller that
    omits the argument judges ownership by a prefix nobody chose, and nothing
    fails.

    Asserted on BEHAVIOUR rather than by reading the literal: the verdict with
    no `--arg prefix` must equal the verdict with `guard_name_prefix`'s default.
    The recording has foreign touches whose `reason` names the prefix, so any
    difference in the value is a difference in the verdict.

    MUTATION: change the default in destroy-guard.jq to "swarm" and this fails.
    """
    env = {key: value for key, value in os.environ.items() if key != "SWARM_NAME_PREFIX"}
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; guard_name_prefix', "_", str(COMMON_SH)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 0, f"guard_name_prefix is not callable: {proc.stderr}"
    default = proc.stdout.strip()
    assert default, "guard_name_prefix printed nothing with SWARM_NAME_PREFIX unset"

    at = GUARD_ARGS.index("prefix") - 1
    assert GUARD_ARGS[at] == "--arg", GUARD_ARGS
    omitted = json.loads(
        _jq(["-f", str(GUARD_JQ), *GUARD_ARGS[:at], *GUARD_ARGS[at + 3:], str(FIXTURE)])
    )
    derived = json.loads(
        _jq(["-f", str(GUARD_JQ), *GUARD_ARGS[:at], "--arg", "prefix", default,
             *GUARD_ARGS[at + 3:], str(FIXTURE)])
    )
    assert omitted["foreign_touches"], (
        "the recording no longer produces a foreign touch, so this comparison "
        "cannot see the prefix at all; re-measure before trusting it"
    )
    assert omitted == derived, (
        f"destroy-guard.jq's fallback prefix no longer matches guard_name_prefix "
        f"({default!r}): a caller that omits --arg prefix is judged differently "
        f"from one that passes it. First reasons: "
        f"{[t['reason'] for t in omitted['foreign_touches'][:1]]} vs "
        f"{[t['reason'] for t in derived['foreign_touches'][:1]]}"
    )


def test_the_ownership_predicate_is_still_warn_only_while_a_real_plan_flags_our_own_resources():
    """`foreign_touches` against a real destroy plan: 178 of 264, all of them ours.

    The predicate arrived warn-only on 2026-09-23 with its promotion condition
    written into scripts/lib/plan-guard.sh: "once a real plan reports this empty,
    set abort=1". A real dev destroy plan was judged on 2026-09-24 and reports
    178 of 264, in TWO classes, both measured rather than reasoned:

      * 114 have NO `name` in `terraform show -json` at all -- IAM members, API
        enablements, bucket IAM edges. `is_ours` falls back to `name_of`, which
        yields "", and `"" | startswith("swarm-")` is false. These are also
        exactly the unlabelable types, which cannot carry managed-by either, so
        they have nothing left to identify themselves with;
      * 64 DO have a name, and it simply is not `swarm-`-prefixed: the platform's
        own Firestore database is named `swarm` (no dash), its custom roles are
        camelCase (`swarmImagePuller`), its Firestore documents are keyed by pool
        id (`provider:anthropic:tenant:eng`), its log-based metrics by event
        (`checkpoint-completed`), and its Firestore indexes carry
        server-generated ids.

    The second class is the one that makes this a design question rather than a
    bug: `is_ours` asks whether a name starts with the prefix, and this platform
    does not name everything that way. Promoting the check would refuse every
    plan it produces.

    This does not demand the count be zero -- that would demand a design change
    from whoever runs it. It fails if the check is made FATAL while a real plan
    still flags our own resources.
    """
    verdict = _verdict(FIXTURE)
    foreign = verdict["foreign_touches"]
    if not foreign:
        pytest.skip(
            "foreign_touches is empty on the recorded real plan: the promotion "
            "condition in scripts/lib/plan-guard.sh is met and this case is moot"
        )

    nameless = [entry for entry in foreign if entry["name"] == ""]
    named = [entry for entry in foreign if entry["name"] != ""]
    assert nameless, (
        "foreign_touches flags resources but none of them lack a name, so the cause "
        "measured on 2026-09-24 has changed; re-read is_ours before trusting this"
    )
    assert named, (
        "foreign_touches flags only nameless resources now, so the SECOND half of "
        "the 2026-09-24 measurement is gone; re-read is_ours before trusting this"
    )
    # The sharpest single fact, and the one a reader will not believe without it:
    # our own resources whose names begin with the prefix MINUS its separator.
    # `swarm-` is not how this platform names a Firestore database or an IAM
    # custom role, so no amount of tightening the prefix rescues them.
    stem = PREFIX.rstrip("-_")
    near_miss = sorted(
        entry["name"] for entry in named
        if entry["name"].startswith(stem) and not entry["name"].startswith(PREFIX)
    )
    assert near_miss, (
        f"no flagged resource is named like ours-but-not-{PREFIX!r} any more. On "
        f"2026-09-24 the Firestore database {stem!r} and eight camelCase custom "
        f"roles were exactly that, and they are the reason a prefix test cannot "
        f"express ownership here. Re-measure before relying on this case."
    )

    guard = subprocess.run(
        [str(PLAN_GUARD), "--mode", "destroy", "--plan", str(FIXTURE)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            **os.environ,
            "NO_COLOR": "1",
            "PROJECT_ID": PROJECT,
            "REGION": "us-central1",
            "ENVIRONMENT": "dev",
        },
    )
    transcript = guard.stdout + guard.stderr
    assert guard.returncode == 0, (
        f"the plan guard now REFUSES a real, legitimate destroy plan. "
        f"{len(foreign)} of {verdict['deletions']} deletions are flagged as not ours: "
        f"{len(nameless)} have no name in real provider output at all (our own IAM "
        f"edges, API enablements and bucket bindings) and {len(named)} are named in a "
        f"way this platform genuinely uses, e.g. {near_miss[:3]}. Making "
        f"foreign_touches fatal refuses every plan this platform produces; teach "
        f"is_ours to recognise a type whose real output carries no .name, and a "
        f"name this repository really gives its own resources, first.\n"
        + transcript
    )
    assert "NOT FATAL YET" in transcript, (
        "plan-guard.sh no longer says the ownership check is warn-only, but it also "
        "did not abort. Say which it is:\n" + transcript
    )
