"""OWNER_PINNED_JOBS names exactly the jobs terraform/bootstrap pins.

deploy.sh reads `OWNER_PINNED_JOBS` (scripts/lib/common.sh) to decide which
Cloud Run jobs may lag the release's promoted digest -- "awaiting owner
re-pin", a warning -- instead of failing the deploy (owner decision
2026-10-11). The list is the one place that set is named, so it has to match
what terraform/bootstrap actually declares:

  * a bootstrap job missing from it fails every release that rebuilds its
    image, the defect of runs 38108979337, 38109627282 and 38110028000;
  * an entry with no bootstrap job behind it, or naming a variable that does
    not pin that job's image, excuses a lag the owner cannot fix -- and tells
    them to set the wrong variable.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO / "terraform" / "bootstrap"
COMMON = REPO / "scripts" / "lib" / "common.sh"


def owner_pinned_jobs() -> dict[str, str]:
    proc = subprocess.run(
        ["bash", "-c", 'source "$1" >/dev/null 2>&1; printf "%s\\n" "${OWNER_PINNED_JOBS[@]}"', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    entries = [line for line in proc.stdout.splitlines() if line]
    assert entries, "OWNER_PINNED_JOBS is empty or did not load from common.sh"
    return dict(entry.split("=", 1) for entry in entries)


def bootstrap_jobs() -> dict[str, str]:
    """{job name: the variable its container image is}, from the HCL."""
    text = "\n".join(p.read_text() for p in sorted(BOOTSTRAP.glob("*.tf")))
    locals_ = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"$]+)"\s*$', text, re.M))
    jobs: dict[str, str] = {}
    for match in re.finditer(r'^resource "google_cloud_run_v2_job" "\w+" \{\n(.*?)^\}', text, re.M | re.S):
        body = match.group(1)
        name = re.search(r'^\s*name\s*=\s*(?:local\.(\w+)|"([^"]+)")', body, re.M)
        image = re.search(r'^\s*image\s*=\s*var\.(\w+)\s*$', body, re.M)
        assert name and image, f"a bootstrap job whose name or image this test cannot read:\n{body[:400]}"
        jobs[locals_[name.group(1)] if name.group(1) else name.group(2)] = image.group(1)
    assert jobs, "found no google_cloud_run_v2_job in terraform/bootstrap; the parser is not reading it"
    return jobs


def test_owner_pinned_jobs_match_the_jobs_terraform_bootstrap_pins():
    assert owner_pinned_jobs() == bootstrap_jobs()


def test_the_lookup_answers_for_listed_jobs_only():
    script = 'source "$1" >/dev/null 2>&1; owner_pinned_job_variable swarm-workspace-apply; owner_pinned_job_variable swarm-verify || echo none'
    proc = subprocess.run(["bash", "-c", script, "_", str(COMMON)], capture_output=True, text=True, timeout=60)
    assert proc.stdout.split() == ["workspace_apply_image", "none"], proc.stdout + proc.stderr
