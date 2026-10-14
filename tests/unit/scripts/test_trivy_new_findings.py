"""The PR security gate fails only on Trivy findings the base branch does not have (OPS-SMALL, #1194).

Measured on PR 1186 (2026-10-12): editing Dockerfile `FROM` lines made the
scan report "new" alerts that already existed on main, re-attributed to the
edited lines, because the match was by location. `scripts/trivy-new-findings.sh`
compares a head scan with the base branch's scan of the same target by
(class, target, rule id, package) -- never by line -- and the `filesystem` job
in .github/workflows/security.yml gates on it, while still uploading SARIF.

These tests run the comparison on Trivy JSON fixtures (no trivy, no network)
and hold the workflow to calling it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "trivy-new-findings.sh"
WORKFLOW = REPO / ".github" / "workflows" / "security.yml"


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _vuln(cve: str, pkg: str, *, version: str = "1.0.0", line: int | None = None) -> dict[str, Any]:
    found: dict[str, Any] = {
        "VulnerabilityID": cve,
        "PkgName": pkg,
        "InstalledVersion": version,
        "FixedVersion": "9.9.9",
        "Severity": "HIGH",
        "Title": f"{pkg}: something",
    }
    if line is not None:
        found["PkgIdentifier"] = {"PURL": f"pkg:pypi/{pkg}@{version}"}
        found["Locations"] = [{"StartLine": line, "EndLine": line}]
    return found


def _report(results: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"SchemaVersion": 2, "ArtifactName": ".", "ArtifactType": "filesystem", "Results": results}


def _compare(tmp_path: Path, base: dict[str, Any], head: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    (tmp_path / "base.json").write_text(json.dumps(base))
    (tmp_path / "head.json").write_text(json.dumps(head))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env["SWARM_ENV_FILE"] = str(tmp_path / "no.env")
    env["NO_COLOR"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT), "compare", str(tmp_path / "base.json"), str(tmp_path / "head.json")],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )


def test_the_script_has_the_house_shape():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.MULTILINE)


def test_an_alert_on_base_and_head_is_not_new_even_on_another_line(tmp_path):
    """The PR 1186 case: the same finding, moved to the line the PR edited,
    with a bumped (still vulnerable) version. Not new."""
    base = _report([{"Target": "apps/api/Dockerfile", "Class": "lang-pkgs",
                     "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl", line=3)]}])
    head = _report([{"Target": "apps/api/Dockerfile", "Class": "lang-pkgs",
                     "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl", version="1.0.1", line=1)]}])
    result = _compare(tmp_path, base, head)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CVE-2026-0001" not in result.stdout
    # Not an empty success: it says what it compared (CLAUDE.md rule zero).
    assert "head 1, base 1, new 0" in result.stderr, result.stderr


def test_an_alert_only_on_head_is_new_and_fails_the_gate(tmp_path):
    base = _report([{"Target": "uv.lock", "Class": "lang-pkgs",
                     "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl")]}])
    head = _report([{"Target": "uv.lock", "Class": "lang-pkgs",
                     "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl"), _vuln("CVE-2026-0002", "urllib3")]}])
    result = _compare(tmp_path, base, head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["uv.lock\tCVE-2026-0002\turllib3\tHIGH\turllib3: something"], result.stdout
    assert "head 2, base 1, new 1" in result.stderr, result.stderr


def test_the_same_rule_in_another_target_or_package_is_new(tmp_path):
    base = _report([{"Target": "uv.lock", "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl")]}])
    head = _report([
        {"Target": "uv.lock", "Vulnerabilities": [_vuln("CVE-2026-0001", "libssl")]},
        {"Target": "apps/swarm-ui/package-lock.json", "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl")]},
    ])
    result = _compare(tmp_path, base, head)
    assert result.returncode == 1
    assert len(result.stdout.splitlines()) == 2, result.stdout


def test_a_secret_is_compared_by_rule_and_file_and_its_match_is_never_printed(tmp_path):
    match = "token = " + "x" * 6 + "*" * 20
    secret = {"RuleID": "generic-token", "Category": "Generic", "Severity": "HIGH",
              "Title": "Generic token", "StartLine": 4, "EndLine": 4, "Match": match}
    base = _report([{"Target": "a.py", "Class": "secret", "Secrets": [secret]}])
    head = _report([
        {"Target": "a.py", "Class": "secret", "Secrets": [{**secret, "StartLine": 9, "EndLine": 9}]},
        {"Target": "b.py", "Class": "secret", "Secrets": [secret]},
    ])
    result = _compare(tmp_path, base, head)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["b.py\tgeneric-token\t-\tHIGH\tGeneric token"], result.stdout
    assert match not in result.stdout + result.stderr


def test_an_empty_base_makes_every_head_finding_new_and_a_clean_head_passes(tmp_path):
    head = _report([{"Target": "uv.lock", "Vulnerabilities": [_vuln("CVE-2026-0003", "jinja2")]}])
    assert _compare(tmp_path, _report(None), head).returncode == 1
    clean = _compare(tmp_path, head, _report(None))
    assert clean.returncode == 0
    assert "head 0, base 1, new 0" in clean.stderr, clean.stderr


def test_an_unreadable_report_is_not_a_pass(tmp_path):
    (tmp_path / "base.json").write_text("{}")
    (tmp_path / "head.json").write_text("not json")
    result = subprocess.run(
        ["bash", str(SCRIPT), "compare", str(tmp_path / "base.json"), str(tmp_path / "head.json")],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "SWARM_ENV_FILE": str(tmp_path / "no.env")},
    )
    assert result.returncode == 2, result.stdout + result.stderr


def _filesystem_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["filesystem"]["steps"]


def test_the_filesystem_job_gates_on_new_findings_and_still_uploads_sarif():
    steps = _filesystem_steps()
    uploads = [s for s in steps if str(s.get("uses", "")).startswith("github/codeql-action/upload-sarif@")]
    assert uploads and uploads[0].get("if") == "always()", "SARIF is still uploaded for visibility"
    gates = [s for s in steps if "scripts/trivy-new-findings.sh gate" in str(s.get("run", ""))]
    assert len(gates) == 1, "the PR gate is the base-branch comparison"
    base = str((gates[0].get("env") or {}).get("BASE_SHA", ""))
    assert "github.event.pull_request.base.sha" in base and "github.event.merge_group.base_sha" in base, base
    # The old absolute gate (exit-code 1 over every finding) is gone.
    absolute = [s for s in steps if str((s.get("with") or {}).get("exit-code", "")) == "1"]
    assert absolute == [], absolute


# --- the gate, end to end, with a fake trivy ---------------------------------

#: Reports whatever `findings.json` the scanned tree holds, as trivy would
#: report what is in it; logs its arguments.
FAKE_TRIVY = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_TRIVY_LOG:?}"
out="" dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    --scanners|--severity|--format|--exit-code) shift 2 ;;
    -*) shift ;;
    fs) shift ;;
    *) dir="$1"; shift ;;
  esac
done
if [[ -f "${dir}/findings.json" ]]; then cp "${dir}/findings.json" "${out}"; else printf '{}\n' >"${out}"; fi
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _gate(tmp_path: Path, repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    trivy = tmp_path / "trivy"
    trivy.write_text(FAKE_TRIVY)
    trivy.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env.update(SWARM_TRIVY=str(trivy), FAKE_TRIVY_LOG=str(tmp_path / "trivy.log"),
               SWARM_ENV_FILE=str(tmp_path / "no.env"), NO_COLOR="1", TMPDIR=str(tmp_path))
    return subprocess.run(["bash", str(SCRIPT), "gate", *argv], cwd=repo, capture_output=True,
                          text=True, env=env, timeout=120, check=False)


def test_the_gate_scans_the_base_commit_and_fails_only_on_what_head_adds(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "gate")
    old = {"Target": "Dockerfile", "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl", line=3)]}
    (repo / "findings.json").write_text(json.dumps(_report([old])))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    # The PR 1186 shape: main's finding, on the line the PR edited.
    moved = {"Target": "Dockerfile", "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl", line=1)]}
    (repo / "findings.json").write_text(json.dumps(_report([moved])))
    _git(repo, "commit", "-qam", "edit FROM")
    result = _gate(tmp_path, repo, "--base", base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head 1, base 1, new 0" in result.stderr, result.stderr
    scans = (tmp_path / "trivy.log").read_text().splitlines()
    assert len(scans) == 2 and "--skip-db-update" in scans[1], scans
    assert all("--ignore-unfixed" in s and "vuln,secret" in s for s in scans), scans

    added = {"Target": "Dockerfile", "Vulnerabilities": [
        _vuln("CVE-2026-0001", "openssl", line=1), _vuln("CVE-2026-0009", "zlib")]}
    (repo / "findings.json").write_text(json.dumps(_report([added])))
    _git(repo, "commit", "-qam", "add zlib")
    result = _gate(tmp_path, repo, "--base", base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["Dockerfile\tCVE-2026-0009\tzlib\tHIGH\tzlib: something"]
    # The base worktree is cleaned up.
    assert _git(repo, "worktree", "list").count("\n") == 0


def test_the_gate_with_no_base_fails_on_every_finding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "findings.json").write_text(json.dumps(_report([
        {"Target": "uv.lock", "Vulnerabilities": [_vuln("CVE-2026-0001", "openssl")]}])))
    result = _gate(tmp_path, repo, "--base", "")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CVE-2026-0001" in result.stdout
