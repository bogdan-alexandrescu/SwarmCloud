"""Release blockers are caught before the merge (C3), the measured download
flakes retry (PR 1281's table), and ruff runs in CI (owner decisions
2026-10-14; docs/ci.md "A pull request scans the images it builds").

  * a pull request's `build changed images without pushing` scans every image
    it builds with the release's own policy, at the release's trivy pin, and a
    finding fails the build;
  * it checks the tool mirror's manifest against the Dockerfiles' pins and
    names every object a merge would need uploaded first;
  * the three downloads that failed runs retry, bounded;
  * `ruff check` is a job of application.yml.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .test_build_images_local import _builds, _local, _value
from .test_build_mirror import _refresh

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"
SCAN = REPO / "scripts" / "scan-image-layout.sh"
POLICY = REPO / "scripts" / "lib" / "image-scan.sh"
TAG = "feed00c0ffee"


def _jobs() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]


def _step(job: str, name: str) -> dict:
    (step,) = [s for s in _jobs()[job]["steps"] if s.get("name") == name]
    return step


# --------------------------------------------------------------------------
# The scan.
# --------------------------------------------------------------------------
def test_the_pull_request_scans_at_the_releases_trivy_pin():
    """MUTATION: bump one pin and not the other."""
    release_pin = yaml.safe_load(RELEASE.read_text())["env"]["TRIVY_VERSION"]
    assert _jobs()["build-check"]["env"]["TRIVY_VERSION"] == release_pin


def test_the_build_step_scans_with_the_shared_policy():
    env = _step("build-check", "build with docker buildx, pushing nothing")["env"]
    assert env["BUILD_ONLY_SCAN"] == "scripts/scan-image-layout.sh", env
    assert SCAN.exists() and os.access(SCAN, os.X_OK)
    # One policy: both scans read lib/image-scan.sh, neither restates its flags.
    for script in (SCAN, REPO / "scripts" / "push-images.sh"):
        text = script.read_text()
        assert "lib/image-scan.sh" in text and "image_scan_args" in text, script
        assert "--ignore-unfixed" not in text, f"{script.name} restates the policy"
    assert "--ignore-unfixed" in POLICY.read_text()


FAKE_TRIVY = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_TRIVY_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
sys.exit(int(os.environ.get("FAKE_TRIVY_EXIT", "0")))
'''


@pytest.mark.parametrize("exit_code", [0, 1])
def test_the_layout_scan_passes_the_policy_and_fails_on_a_finding(tmp_path, exit_code):
    trivy = tmp_path / "trivy"
    trivy.write_text(FAKE_TRIVY.replace("{python}", sys.executable))
    trivy.chmod(0o755)
    layout = tmp_path / "layout"
    layout.mkdir()
    log = tmp_path / "trivy.log"
    env = {**os.environ, "SWARM_TRIVY": str(trivy), "FAKE_TRIVY_LOG": str(log),
           "FAKE_TRIVY_EXIT": str(exit_code), "SWARM_ENV_FILE": str(tmp_path / "none.env"),
           "PROJECT_ID": "swarm-test-project", "REGION": "us-central1", "NO_COLOR": "1"}
    proc = subprocess.run(["bash", str(SCAN), str(layout), "swarm-api"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == exit_code, proc.stderr
    (argv,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert argv[0] == "image" and "--ignore-unfixed" in argv and "--exit-code" in argv, argv
    assert argv[argv.index("--severity") + 1] == "HIGH,CRITICAL", argv
    assert argv[-2:] == ["--input", str(layout)], argv


FAKE_SCAN = r'''#!/bin/sh
echo "$1 $2" >> "$FAKE_SCAN_LOG"
if [ -n "$FAKE_SCAN_REFUSE" ]; then case "$2" in *"$FAKE_SCAN_REFUSE"*) exit 1 ;; esac; fi
exit 0
'''


def _scan_build(tmp_path: Path, monkeypatch, refuse: str = ""):
    scan = tmp_path / "fake-scan.sh"
    scan.write_text(FAKE_SCAN)
    scan.chmod(0o755)
    log = tmp_path / "scan.log"
    log.write_text("")
    monkeypatch.setenv("BUILD_ONLY_SCAN", str(scan))
    monkeypatch.setenv("FAKE_SCAN_LOG", str(log))
    monkeypatch.setenv("FAKE_SCAN_REFUSE", refuse)
    root, proc, events = _local(tmp_path, "--build-only", "--local", "--tag", TAG, "swarm-api")
    return root, proc, events, [line.split() for line in log.read_text().splitlines() if line]


def test_every_image_a_pull_request_builds_is_exported_and_scanned(tmp_path, monkeypatch):
    root, proc, events, scans = _scan_build(tmp_path, monkeypatch)
    assert proc.returncode == 0, proc.stderr[-3000:]
    (build,) = [b for b in _builds(events) if "swarm-api" in " ".join(b)]
    (output,) = _value(build, "--output")
    assert output.startswith("type=oci,") and ".scan.oci" in output, output
    assert [s[1].split("/")[-1].split(":")[0] for s in scans] == ["swarm-api"], scans
    assert scans[0][0].endswith(".scan.oci"), scans


def test_a_refused_scan_fails_the_build_naming_the_image(tmp_path, monkeypatch):
    """MUTATION: ignore the scan's exit code in the replay."""
    _, proc, _, scans = _scan_build(tmp_path, monkeypatch, refuse="swarm-api")
    assert scans, "nothing was scanned"
    assert proc.returncode != 0
    assert "the image scan refused it" in proc.stdout + proc.stderr


def test_without_the_scan_nothing_is_exported(tmp_path, monkeypatch):
    """The control: unset, a leaf is cache-only, as before C3."""
    monkeypatch.delenv("BUILD_ONLY_SCAN", raising=False)
    _, proc, events = _local(tmp_path, "--build-only", "--local", "--tag", TAG, "swarm-api")
    assert proc.returncode == 0, proc.stderr[-3000:]
    (build,) = [b for b in _builds(events) if "swarm-api" in " ".join(b)]
    assert _value(build, "--output") == ["type=cacheonly"], build


# --------------------------------------------------------------------------
# The tool mirror.
# --------------------------------------------------------------------------
def test_the_mirror_step_runs_the_consistency_check_that_exists():
    run = _step("build-check", "the tool mirror holds what this pull request pins")["run"]
    (node,) = re.findall(r"tests/unit/scripts/(test_\w+\.py)::(\w+)", run)
    path = REPO / "tests" / "unit" / "scripts" / node[0]
    assert f"def {node[1]}(" in path.read_text(), node
    assert "tool-mirror.sha256" in run and "refresh-build-mirror.sh --tools-only" in run, run


def test_the_mirror_step_names_every_added_row(tmp_path):
    """The diff-reading line of the step, run over a fixture diff: an added row
    is named, a removed or unchanged one is not."""
    run = _step("build-check", "the tool mirror holds what this pull request pins")["run"]
    (sed,) = [line.strip() for line in run.splitlines() if line.strip().startswith("| sed -nE")]
    sed = sed[1:].strip().split(" > ")[0]
    diff = "\n".join([
        "--- a/images/agent-runtime-base/tool-mirror.sha256",
        "+++ b/images/agent-runtime-base/tool-mirror.sha256",
        "+" + "1" * 64 + "  github.com/new/tool/releases/download/v1/tool.tar.gz",
        "-" + "2" * 64 + "  github.com/old/tool/releases/download/v0/tool.tar.gz",
        " " + "3" * 64 + "  github.com/same/tool/releases/download/v1/same.tar.gz",
    ]) + "\n"
    proc = subprocess.run(["bash", "-c", sed], input=diff, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["github.com/new/tool/releases/download/v1/tool.tar.gz"], proc.stdout


# --------------------------------------------------------------------------
# The download flakes.
# --------------------------------------------------------------------------
def test_the_emulator_download_retries_bounded():
    run = _step("integration", "install the emulator")["run"]
    assert "for attempt in 1 2 3 4" in run and "sleep" in run, run
    assert run.count("gcloud components install cloud-firestore-emulator") == 1, run


def test_every_image_download_retries_on_an_http_error():
    """curl's exit 22 (an HTTP error) is retried only with --retry-all-errors.
    MUTATION: drop it from any `curl -fsSL` in a Dockerfile."""
    bare = []
    for df in sorted([*REPO.glob("images/*/Dockerfile"), *REPO.glob("apps/*/Dockerfile")]):
        for n, line in enumerate(df.read_text().splitlines(), 1):
            if re.search(r"\bcurl -fsSL\b", line) and "--retry-all-errors" not in line and not line.lstrip().startswith("#"):
                bare.append(f"{df.relative_to(REPO)}:{n}")
    assert not bare, bare


def test_the_go_module_fetches_retry():
    text = (REPO / "images" / "agent-runtime-indexer" / "Dockerfile").read_text()
    assert "three_tries build_server golang.org/x/tools/gopls" in text


# --------------------------------------------------------------------------
# ruff.
# --------------------------------------------------------------------------
def test_ruff_check_is_a_gated_job():
    jobs = _jobs()
    job = jobs["ruff"]
    assert job["name"] == "ruff check"
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "uv run --frozen --only-dev ruff check" in runs, runs
    assert "ruff" in jobs["ci-gate"]["needs"]


# --------------------------------------------------------------------------
# Main fills the tool mirror (owner decision 2026-10-14, on PR 1298).
# --------------------------------------------------------------------------
def test_main_fills_the_tool_mirror_before_it_builds_and_a_pull_request_never_does():
    """MUTATION: drop the step, move it after the build, or drop --tools-only."""
    job = _jobs()["build"]
    names = [s.get("name") for s in job["steps"]]
    fill = names.index("fill the tool mirror with what this commit pins")
    assert fill < names.index("build with Cloud Build"), names
    assert job["steps"][fill]["run"].strip() == "./scripts/refresh-build-mirror.sh --tools-only"
    assert job["if"] == "github.event_name != 'pull_request' && github.event_name != 'merge_group'", job["if"]
    for other_id, other in _jobs().items():
        if other_id != "build":
            for st in other.get("steps", []):
                for line in str(st.get("run", "")).splitlines():
                    assert not line.strip().startswith(("./scripts/refresh-build-mirror.sh", "scripts/refresh-build-mirror.sh")), (other_id, line)


def test_tools_only_uploads_checked_files_and_touches_no_image_and_deletes_nothing(tmp_path):
    """--tools-only: only `storage objects describe` and `storage cp` to the
    bucket, every upload matching its pinned sha256; no artifacts call, no
    delete. MUTATION: let --tools-only fall through to the image half."""
    import hashlib

    rows, bucket, proc, calls, _ = _refresh(tmp_path, "--tools-only")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert calls, "the refresh made no gcloud call"
    for argv in calls:
        assert argv[0] == "storage" and argv[1] in {"objects", "cp"}, argv
        assert not any(word in {"rm", "delete"} for word in argv), argv
    for sha, obj in rows:
        assert hashlib.sha256((bucket / "swarm-build-mirror-swarm-test-project" / obj).read_bytes()).hexdigest() == sha
