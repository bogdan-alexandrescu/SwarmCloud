"""`push-images.sh --scan-only` checks a release exactly as a promotion would,
and moves nothing.

WHY. Owner decision, 2026-09-24 (docs/ci.md, "A prod release waits for
approval before anything prod-facing"): a prod release waits for the owner's
approval before anything prod-facing happens, and moving `:prod` sits behind
that same approval. Scanning is read-only, so it may happen BEFORE the
approval -- the reviewer then approves a set that has already passed the scan,
instead of approving a set that is refused a minute later. release.yml's
images job runs this mode before the approval; the promotion after the
approval runs the full script, scanning again.

What makes the pre-approval step safe is this script's promise that
`--scan-only` moves no tag. release.yml's own test
(test_release_prod_gate.py) exempts a `push-images.sh --scan-only` step from
the approval on exactly that promise, so it is asserted here against the real
script, with the fake `gcloud` and `trivy` from
test_push_images_promotes_recorded_digests.py:

  * every recorded digest is confirmed and scanned, as a promotion does;
  * no channel tag moves, and no deploy manifest is written;
  * it refuses exactly what a promotion would refuse, naming the image;
  * it works for a first prod release, when `:prod` has never existed;
  * `--scan-only --no-scan` is refused rather than checking nothing;
  * a vulnerability DB that cannot be downloaded is a scanner error, not a
    finding, and still refuses; one failed download is retried (#2379).

WHAT THIS CANNOT PROVE: anything about the real registry or the real trivy
beyond what the fakes model -- see the sibling file's own limits.
"""

from __future__ import annotations

import json

import pytest

from . import test_push_images_promotes_recorded_digests as recorded_digests
from .test_push_images_promotes_recorded_digests import (
    IMAGES,
    REGISTRY,
    _channel,
    _last_line,
    _manifest,
    _registry,
    _run,
    pytestmark,  # noqa: F401 -- the same skip when bash or jq is missing
    recorded,
)


def test_scan_only_confirms_and_scans_every_recorded_digest_and_moves_nothing(tmp_path):
    before = _registry()
    root, proc, registry, log = _run(tmp_path, before, _manifest(), flags=("--scan-only",))
    assert proc.returncode == 0, proc.stderr[-3000:]

    scanned = {e["image"]: e["ref"] for e in log if e["event"] == "scan"}
    assert scanned == {i: f"{REGISTRY}/{i}@{recorded(i)}" for i in IMAGES}, (
        f"scanned {scanned}: not every digest the build recorded"
    )
    confirmed = sorted(e["image"] for e in log if e["event"] == "describe")
    assert confirmed == sorted(IMAGES), f"confirmed {confirmed} in the registry, not every recorded image"

    moved = [e for e in log if e["event"] == "tag"]
    assert not moved, f"--scan-only moved channel tags: {moved}"
    assert _channel(registry) == _channel(before), ":dev changed under --scan-only"
    assert not (root / "build" / "deployed-images-dev.json").exists(), (
        "--scan-only wrote a deploy manifest: the infrastructure job would read it as a promotion"
    )


@pytest.mark.parametrize(
    "registry_kw, fakes, refused",
    [
        ({"missing": ("swarm-scheduler",)}, {}, "swarm-scheduler"),
        ({}, {"FAKE_DESCRIBE_ERROR": "swarm-ui"}, "swarm-ui"),
        ({}, {"FAKE_TRIVY_FAIL": "swarm-api"}, "swarm-api"),
    ],
    ids=["digest-missing", "registry-unreadable", "scan-refused"],
)
def test_scan_only_refuses_what_the_promotion_would_refuse(tmp_path, registry_kw, fakes, refused):
    before = _registry(**registry_kw)
    _, proc, registry, log = _run(tmp_path, before, _manifest(), flags=("--scan-only",), **fakes)
    assert proc.returncode != 0, f"{refused} would be refused by the promotion and --scan-only passed it"
    assert refused in _last_line(proc), f"the last line does not name {refused}: {_last_line(proc)!r}"
    assert not [e for e in log if e["event"] == "tag"]
    assert _channel(registry) == _channel(before)


def test_scan_only_checks_a_first_prod_release_before_prod_has_ever_existed(tmp_path):
    """The case the pre-approval scan exists for. No image has ever carried
    :prod, and a prod release built for prod is checked without moving it."""
    before = _registry()
    assert all(d is None for d in _channel(before, "prod").values())
    root, proc, registry, log = _run(
        tmp_path, before, _manifest(environment="prod"), channel="prod", flags=("--scan-only",)
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert sorted(e["image"] for e in log if e["event"] == "scan") == sorted(IMAGES)
    assert not [e for e in log if e["event"] == "tag"], ":prod moved before anyone approved it"
    assert all(d is None for d in _channel(registry, "prod").values())
    assert not (root / "build" / "deployed-images-prod.json").exists()


def test_scan_only_with_no_scan_is_refused_before_anything_is_read(tmp_path):
    _, proc, _, log = _run(tmp_path, _registry(), _manifest(), flags=("--scan-only", "--no-scan"))
    assert proc.returncode != 0, "--scan-only --no-scan checked nothing and exited 0"
    assert not log, f"the registry or the scanner was used before the refusal: {json.dumps(log)[:500]}"
    last = _last_line(proc)
    assert "--scan-only" in last and "--no-scan" in last, f"the refusal does not say why: {last!r}"


# The sibling's trivy, plus a vulnerability DB that can fail to download:
# FAKE_TRIVY_DB_FAILS=<n|all> fails the first n downloads as a ghcr.io outage
# does, and a scan that would fetch the DB itself (no --skip-db-update) fails
# the same way -- which is what every scan on main did. Findings exit with the
# --exit-code the policy asked for; trivy's own errors exit 1.
DB_TRIVY = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
events = os.environ["FAKE_EVENTS"]
failed = 0
if os.path.exists(events):
    with open(events) as fh:
        failed = sum(1 for line in fh if json.loads(line)["event"] == "db-failed")
spec = os.environ.get("FAKE_TRIVY_DB_FAILS", "0")
if "--download-db-only" in args or "--skip-db-update" not in args:
    if spec == "all" or failed < int(spec):
        with open(events, "a") as fh:
            fh.write(json.dumps({"event": "db-failed"}) + "\n")
        print("FATAL Fatal error init error: DB error: failed to download vulnerability DB", file=sys.stderr)
        sys.exit(1)
    if "--download-db-only" in args:
        with open(events, "a") as fh:
            fh.write(json.dumps({"event": "db"}) + "\n")
        sys.exit(0)
ref = args[-1]
image = ref.rsplit("/", 1)[-1].split("@", 1)[0]
with open(events, "a") as fh:
    fh.write(json.dumps({"event": "scan", "image": image, "ref": ref}) + "\n")
if image in set(filter(None, os.environ.get("FAKE_TRIVY_FAIL", "").split(","))):
    print(f"{image}: CVE-0000-0000 HIGH fixed in 9.9.9")
    sys.exit(int(args[args.index("--exit-code") + 1]))
sys.exit(0)
'''


@pytest.fixture
def db_trivy(monkeypatch):
    monkeypatch.setattr(recorded_digests, "FAKE_TRIVY", DB_TRIVY)


def _out(proc) -> str:
    return proc.stdout + proc.stderr


def test_a_db_download_that_fails_refuses_as_a_scanner_error_not_a_finding(tmp_path, db_trivy):
    """MUTATION: treat every non-zero trivy exit as findings, and this fails."""
    before = _registry()
    _, proc, registry, log = _run(tmp_path, before, _manifest(), flags=("--scan-only",),
                                  FAKE_TRIVY_DB_FAILS="all", IMAGE_SCAN_DB_BACKOFF="0")
    assert proc.returncode != 0, "nothing could be scanned and --scan-only passed: the gate must fail closed"
    assert "fixable" not in _out(proc), _out(proc)[-3000:]
    assert "trivy could not scan" in _out(proc) and "this is not a finding" in _out(proc), _out(proc)[-3000:]
    assert len([e for e in log if e["event"] == "db-failed"]) > 1, "the DB download was not retried"
    assert not [e for e in log if e["event"] == "scan"], "scanned without a vulnerability DB"
    assert not [e for e in log if e["event"] == "tag"]
    assert _channel(registry) == _channel(before)


def test_a_db_download_that_fails_once_is_retried_and_every_image_is_scanned(tmp_path, db_trivy):
    _, proc, _, log = _run(tmp_path, _registry(), _manifest(), flags=("--scan-only",),
                           FAKE_TRIVY_DB_FAILS="1", IMAGE_SCAN_DB_BACKOFF="0")
    assert proc.returncode == 0, _out(proc)[-3000:]
    assert sorted(e["image"] for e in log if e["event"] == "scan") == sorted(IMAGES)


def test_a_finding_keeps_the_findings_message(tmp_path, db_trivy):
    _, proc, _, _ = _run(tmp_path, _registry(), _manifest(), flags=("--scan-only",),
                         FAKE_TRIVY_FAIL="swarm-api", IMAGE_SCAN_DB_BACKOFF="0")
    assert proc.returncode != 0
    assert "swarm-api" in _last_line(proc)
    assert "fixable" in _out(proc) and "could not scan" not in _out(proc), _out(proc)[-3000:]
