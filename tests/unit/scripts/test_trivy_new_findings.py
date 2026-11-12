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
import sys
from datetime import datetime, timezone
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
    # Its own job, not folded into `checks` by CI-FOLD (#1799): it uploads
    # SARIF, so it alone holds security-events: write (owner, 2026-10-21).
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
    # One speech-lockfile acceptance of its own, so the accept list the scans
    # read is never the empty one, whatever the real list holds.
    accept = tmp_path / "accept.yaml"
    accept.write_text(yaml.safe_dump({"vulnerabilities": [{
        "id": "CVE-2026-0101", "statement": "pinned upstream",
        "paths": ["apps/swarm-speech/uv.lock"], "expired_at": "2099-12-31"}]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env.update(SWARM_TRIVY=str(trivy), FAKE_TRIVY_LOG=str(tmp_path / "trivy.log"),
               SWARM_TRIVY_IGNOREFILE=str(accept),
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
    # Both scans read the same accept list: the speech-lockfile part of
    # .trivyignore.yaml, never the whole list the image scans read.
    ignores = {s.split("--ignorefile ", 1)[1].split()[0] for s in scans}
    assert len(ignores) == 1 and str(REPO / ".trivyignore.yaml") not in ignores, scans

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


# --- the accept list the repository gate reads -------------------------------
# The owner, 2026-11-05 (#2688): the repository gate accepts ONLY the .trivyignore.yaml
# entries scoped to apps/swarm-speech/uv.lock; every other acceptance keeps not
# applying to it, as before, while the image scans keep reading the whole list.

SPEECH_LOCK = "apps/swarm-speech/uv.lock"

#: Applies the --ignorefile it is given the way trivy does by id and by path --
#: but NOT by expiry, so an expired entry that still passes is the script's
#: fault, not the fake's -- and copies that file to FAKE_TRIVY_IGNORES.
FAKE_ACCEPTING_TRIVY = """
import json, os, shutil, sys
import yaml

args = sys.argv[1:]
with open(os.environ["FAKE_TRIVY_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
out, scanned = args[args.index("--output") + 1], args[-1]
entries = []
if "--ignorefile" in args:
    ignorefile = args[args.index("--ignorefile") + 1]
    shutil.copy(ignorefile, os.environ["FAKE_TRIVY_IGNORES"])
    entries = (yaml.safe_load(open(ignorefile)) or {}).get("vulnerabilities") or []
report = json.load(open(os.path.join(scanned, "findings.json")))
for result in report["Results"]:
    result["Vulnerabilities"] = [
        v for v in result.get("Vulnerabilities", [])
        if not any(e["id"] == v["VulnerabilityID"] and result["Target"] in e.get("paths", [result["Target"]])
                   for e in entries)]
json.dump(report, open(out, "w"))
"""


def _accept_gate(tmp_path: Path, accept: list[dict[str, Any]] | None,
                 results: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    """The gate with no base (every finding is new) over `results`, reading
    `accept` as its .trivyignore.yaml, or the repository's own when None."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "findings.json").write_text(json.dumps(_report(results)))
    trivy = tmp_path / "trivy"
    trivy.write_text(f"#!{sys.executable}\n" + FAKE_ACCEPTING_TRIVY)
    trivy.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env.update(SWARM_TRIVY=str(trivy), FAKE_TRIVY_LOG=str(tmp_path / "trivy.log"),
               FAKE_TRIVY_IGNORES=str(tmp_path / "ignored.yaml"),
               SWARM_ENV_FILE=str(tmp_path / "no.env"), NO_COLOR="1", TMPDIR=str(tmp_path))
    if accept is not None:
        (tmp_path / "accept.yaml").write_text(yaml.safe_dump({"vulnerabilities": accept}))
        env["SWARM_TRIVY_IGNOREFILE"] = str(tmp_path / "accept.yaml")
    return subprocess.run(["bash", str(SCRIPT), "gate", "--base", ""], cwd=repo, capture_output=True,
                          text=True, env=env, timeout=120, check=False)


def _speech_entry(cve: str, *, expires: str = "2099-12-31") -> dict[str, Any]:
    return {"id": cve, "statement": "pinned upstream", "paths": [SPEECH_LOCK], "expired_at": expires}


def test_a_speech_lockfile_finding_in_the_accept_list_passes_the_repo_gate(tmp_path):
    result = _accept_gate(tmp_path, [_speech_entry("CVE-2026-0101")], [
        {"Target": SPEECH_LOCK, "Vulnerabilities": [_vuln("CVE-2026-0101", "vllm")]}])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "head 0, base 0, new 0" in result.stderr, result.stderr


def test_a_finding_accepted_only_without_the_speech_lockfile_path_still_fails_the_repo_gate(tmp_path):
    accept = [
        _speech_entry("CVE-2026-0101"),
        # Unscoped, as the agent-CLI and Go entries are: the image scans' only.
        {"id": "CVE-2026-0201", "statement": "image only", "expired_at": "2099-12-31"},
        # Scoped to another path.
        {"id": "CVE-2026-0202", "statement": "other lockfile", "paths": ["apps/api/uv.lock"],
         "expired_at": "2099-12-31"},
        # Names the speech lockfile AND another path: not scoped to the speech lockfile.
        {"id": "CVE-2026-0203", "statement": "both", "paths": [SPEECH_LOCK, "apps/api/uv.lock"],
         "expired_at": "2099-12-31"},
    ]
    result = _accept_gate(tmp_path, accept, [
        {"Target": SPEECH_LOCK, "Vulnerabilities": [_vuln("CVE-2026-0101", "vllm")]},
        {"Target": "package-lock.json", "Vulnerabilities": [_vuln("CVE-2026-0201", "brace-expansion")]},
        {"Target": "apps/api/uv.lock", "Vulnerabilities": [
            _vuln("CVE-2026-0202", "urllib3"), _vuln("CVE-2026-0203", "pillow")]}])
    assert result.returncode == 1, result.stdout + result.stderr
    assert sorted(line.split("\t")[1] for line in result.stdout.splitlines()) == [
        "CVE-2026-0201", "CVE-2026-0202", "CVE-2026-0203"], result.stdout
    assert "CVE-2026-0101" not in result.stdout


def test_an_expired_speech_entry_no_longer_passes_the_repo_gate(tmp_path):
    result = _accept_gate(tmp_path, [_speech_entry("CVE-2026-0101", expires="2026-01-01")], [
        {"Target": SPEECH_LOCK, "Vulnerabilities": [_vuln("CVE-2026-0101", "vllm")]}])
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [f"{SPEECH_LOCK}\tCVE-2026-0101\tvllm\tHIGH\tvllm: something"]


def test_the_repo_gate_reads_exactly_the_speech_entries_of_the_real_accept_list(tmp_path):
    whole = yaml.safe_load((REPO / ".trivyignore.yaml").read_text())["vulnerabilities"]
    speech = [e for e in whole if e.get("paths") == [SPEECH_LOCK]]
    # 45 until the speech image's upgrade (PR 3538) cleared every one of them
    # (see the test below); none is left for the gate to accept.
    assert len(speech) == 0 and len(whole) > len(speech)
    # An entry past its expiry is dropped (see the test above).
    speech = [e for e in speech if e["expired_at"] > datetime.now(timezone.utc).date()]
    result = _accept_gate(tmp_path, None, [])
    assert result.returncode == 0, result.stdout + result.stderr
    if not speech:
        # Nothing kept: the gate passes no --ignorefile, so trivy accepts nothing.
        assert "--ignorefile" not in (tmp_path / "trivy.log").read_text()
        return
    read = yaml.safe_load((tmp_path / "ignored.yaml").read_text())
    assert list(read) == ["vulnerabilities"], read
    # Each entry as written, statement and expiry included; nothing else.
    assert read["vulnerabilities"] == speech


#: The swarm-speech acceptances the image's upgrade of 2026-11-06 (PR 3538:
#: vllm 0.33.0, pillow 13.0.0, qwen-asr, gradio and ray dropped) cleared, each
#: with its package and the first fixed version (trivy 0.76.1's DB, read
#: 2026-11-07), or None where the package left the image altogether.
CLEARED_SPEECH: dict[str, tuple[str, str | None]] = {
    **{cve: ("pillow", "13.0.0") for cve in (
        "CVE-2026-112390", "CVE-2026-112392", "CVE-2026-112393", "CVE-2026-112397", "CVE-2026-112398",
        "CVE-2026-112399", "CVE-2026-112400", "CVE-2026-112402", "CVE-2026-112403", "CVE-2026-112404",
        "CVE-2026-112405", "CVE-2026-112406", "CVE-2026-112892", "CVE-2026-112893", "CVE-2026-112894",
        "CVE-2026-112895", "CVE-2026-112896", "CVE-2026-112897", "CVE-2026-112900", "CVE-2026-112902",
        "CVE-2026-96637", "CVE-2026-96639", "CVE-2026-96640", "CVE-2026-96642", "CVE-2026-96643",
        "CVE-2026-96644", "CVE-2026-96778", "GHSA-78g4-xqwv-q3g8")},
    "CVE-2026-22778": ("vllm", "0.14.1"), "CVE-2026-24779": ("vllm", "0.14.1"),
    "CVE-2026-27893": ("vllm", "0.18.0"), "CVE-2026-5497": ("vllm", "0.19.0"),
    "CVE-2026-41523": ("vllm", "0.22.0"), "CVE-2026-48746": ("vllm", "0.22.0"),
    "CVE-2026-55514": ("vllm", "0.24.0"), "CVE-2026-55574": ("vllm", "0.24.0"),
    "CVE-2026-114053": ("vllm", "0.31.0"), "CVE-2026-114055": ("vllm", "0.31.0"),
    "CVE-2026-115912": ("vllm", "0.32.0"), "CVE-2026-115915": ("vllm", "0.32.0"),
    "CVE-2026-4372": ("transformers", "5.3.0"), "CVE-2026-5241": ("transformers", "5.5.0"),
    "CVE-2026-9856": ("transformers", "5.10.0"),
    "CVE-2026-24747": ("torch", "2.10.0"), "CVE-2026-25048": ("xgrammar", "0.1.32"),
    # ray_dist.jar's jackson and guava; gradio's bundled follow-redirects.
    **{cve: ("ray", None) for cve in (
        "CVE-2026-89407", "CVE-2026-89425", "CVE-2026-68497", "CVE-2026-91776", "CVE-2026-91777",
        "CVE-2026-102554")},
    **{cve: ("gradio", None) for cve in ("CVE-2026-111755", "CVE-2026-111756", "CVE-2026-116689")},
}


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def test_no_swarm_speech_acceptance_outlives_the_finding_it_accepted():
    import tomllib

    lock = tomllib.loads((REPO / "apps" / "swarm-speech" / "uv.lock").read_text())
    installed = {p["name"]: p["version"] for p in lock["package"]}
    for cve, (pkg, fixed) in CLEARED_SPEECH.items():
        # The table is only true while the lock keeps each fix: a downgrade
        # brings the finding back, and with it the question of its acceptance.
        if fixed is None:
            assert pkg not in installed, (cve, pkg, installed.get(pkg))
        else:
            assert pkg in installed and _release(installed[pkg]) >= _release(fixed), (cve, pkg, installed.get(pkg))
    whole = yaml.safe_load((REPO / ".trivyignore.yaml").read_text())["vulnerabilities"]
    stale = [
        e["id"] for e in whole if e["id"] in CLEARED_SPEECH and (
            SPEECH_LOCK in e.get("paths", [])
            or any(p.startswith(f"pkg:pypi/{CLEARED_SPEECH[e['id']][0]}@") for p in e.get("purls", []))
            or any(f"site-packages/{CLEARED_SPEECH[e['id']][0]}/" in p for p in e.get("paths", [])))]
    assert stale == [], stale


# --- accept-list: the same filtered list, written for the SARIF scan ---------
# The owner, 2026-11-05 (#2688): code scanning's `trivy-fs` alerts honour the gate's
# accept list, so the workflow's SARIF scan passes what this subcommand writes
# as --ignorefile. One filter (gate_ignores), never a second hand-kept list.

def _accept_list(tmp_path: Path, accept: list[dict[str, Any]] | None) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / "out" / "accept.trivyignore.yaml"
    out.parent.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env.update(SWARM_ENV_FILE=str(tmp_path / "no.env"), NO_COLOR="1", TMPDIR=str(tmp_path),
               SWARM_TRIVY_IGNOREFILE=str(tmp_path / "accept.yaml"))
    if accept is not None:
        (tmp_path / "accept.yaml").write_text(yaml.safe_dump({"vulnerabilities": accept}))
    result = subprocess.run(["bash", str(SCRIPT), "accept-list", str(out)], cwd=tmp_path, capture_output=True,
                            text=True, env=env, timeout=60, check=False)
    return result, out


def test_the_accept_list_holds_only_unexpired_entries_naming_the_speech_lockfile_alone(tmp_path):
    kept = [_speech_entry("CVE-2026-0101"), _speech_entry("CVE-2026-0102")]
    result, out = _accept_list(tmp_path, [
        kept[0],
        # No paths: it would match every target.
        {"id": "CVE-2026-0201", "statement": "image only", "expired_at": "2099-12-31"},
        # Another path.
        {"id": "CVE-2026-0202", "statement": "other lockfile", "paths": ["apps/api/uv.lock"],
         "expired_at": "2099-12-31"},
        # The speech lockfile and another path.
        {"id": "CVE-2026-0203", "statement": "both", "paths": [SPEECH_LOCK, "apps/api/uv.lock"],
         "expired_at": "2099-12-31"},
        # Expired.
        _speech_entry("CVE-2026-0301", expires="2026-01-01"),
        kept[1],
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "2\n", result.stdout
    assert yaml.safe_load(out.read_text()) == {"vulnerabilities": kept}


def test_an_accept_list_that_keeps_nothing_is_still_a_list_trivy_reads(tmp_path):
    result, out = _accept_list(tmp_path, [
        {"id": "CVE-2026-0201", "statement": "image only", "expired_at": "2099-12-31"},
        _speech_entry("CVE-2026-0301", expires="2026-01-01")])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0\n", result.stdout
    assert yaml.safe_load(out.read_text()) == {}
    # No list at all accepts nothing, the same way.
    none = tmp_path / "none"
    none.mkdir()
    result, out = _accept_list(none, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0\n" and yaml.safe_load(out.read_text()) == {}


def test_the_accept_list_of_the_real_list_is_the_one_the_gate_reads(tmp_path):
    whole = yaml.safe_load((REPO / ".trivyignore.yaml").read_text())["vulnerabilities"]
    speech = [e for e in whole if e.get("paths") == [SPEECH_LOCK]
              and e["expired_at"] > datetime.now(timezone.utc).date()]
    out = tmp_path / "accept.trivyignore.yaml"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env.update(SWARM_ENV_FILE=str(tmp_path / "no.env"), NO_COLOR="1", TMPDIR=str(tmp_path))
    result = subprocess.run(["bash", str(SCRIPT), "accept-list", str(out)], cwd=REPO, capture_output=True,
                            text=True, env=env, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"{len(speech)}\n", result.stdout
    written = yaml.safe_load(out.read_text())
    assert written == ({"vulnerabilities": speech} if speech else {}), written


def test_accept_list_needs_exactly_one_path():
    result = subprocess.run(["bash", str(SCRIPT), "accept-list"], capture_output=True, text=True,
                            timeout=60, check=False)
    assert result.returncode == 2, result.stdout + result.stderr


# --- the vulnerability DB download: retried, then the upstream fallback ------
# #2351: trivy (repo) read red when mirror.gcr.io answered 404 for the DB, a
# transient fault with no finding behind it, and the merge step's free re-run
# often read red again for the same reason. The owner, 2026-10-27 ("Both halves"):
# the job retries the download, then falls back to ghcr.io/aquasecurity/trivy-db.
# A real finding still fails the job, through the gate, exactly as before.

MIRROR_DB = "mirror.gcr.io/aquasec/trivy-db:2"
UPSTREAM_DB = "ghcr.io/aquasecurity/trivy-db:2"

#: Fails a `--download-db-only` call for the first FAKE_DB_FAILS calls, and
#: every call against FAKE_DB_DOWN (a repository that stays down); logs its
#: arguments, one call a line.
FAKE_DB_TRIVY = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_TRIVY_LOG:?}"
calls="$(wc -l <"${FAKE_TRIVY_LOG}")"
case " $* " in *" ${FAKE_DB_DOWN:-none} "*) echo "FATAL 404 Not Found" >&2; exit 1 ;; esac
if [ "${calls}" -le "${FAKE_DB_FAILS:-0}" ]; then echo "FATAL 404 Not Found" >&2; exit 1; fi
"""


def _db_step() -> dict[str, Any]:
    steps = _filesystem_steps()
    found = [s for s in steps if "--download-db-only" in str(s.get("run", ""))]
    assert len(found) == 1, "the filesystem job downloads the vulnerability DB in one step of its own"
    return found[0]


def _download(tmp_path: Path, *, fails: int = 0, down: str = "none") -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    step = _db_step()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "trivy").write_text(FAKE_DB_TRIVY)
    # The backoff is real in CI; here `sleep` only records how long it would wait.
    (bindir / "sleep").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$1" >>"${FAKE_SLEEP_LOG:?}"\n')
    for tool in ("trivy", "sleep"):
        (bindir / tool).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "TRIVY_"))}
    env.update({str(k): str(v) for k, v in (step.get("env") or {}).items()})
    env.update(PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}", FAKE_TRIVY_LOG=str(tmp_path / "trivy.log"),
               FAKE_SLEEP_LOG=str(tmp_path / "sleep.log"), FAKE_DB_FAILS=str(fails), FAKE_DB_DOWN=down)
    result = subprocess.run(["bash", "-c", str(step["run"])], cwd=tmp_path, capture_output=True,
                            text=True, env=env, timeout=60, check=False)
    log = tmp_path / "trivy.log"
    sleeps = tmp_path / "sleep.log"
    return (result, log.read_text().splitlines() if log.exists() else [],
            sleeps.read_text().splitlines() if sleeps.exists() else [])


def test_the_db_download_takes_the_mirror_when_it_answers(tmp_path):
    result, calls, sleeps = _download(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 1 and MIRROR_DB in calls[0], calls
    assert sleeps == []


def test_a_mirror_404_is_retried_with_a_short_backoff_before_it_fails(tmp_path):
    result, calls, sleeps = _download(tmp_path, fails=2)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 3 and all(MIRROR_DB in c for c in calls), calls
    assert len(sleeps) == 2 and all(0 < int(s) <= 30 for s in sleeps), sleeps


def test_a_mirror_down_for_three_attempts_falls_back_to_the_upstream_db(tmp_path):
    result, calls, sleeps = _download(tmp_path, down=MIRROR_DB)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [MIRROR_DB in c for c in calls] == [True, True, True, False], calls
    assert UPSTREAM_DB in calls[3], calls
    assert len(sleeps) == 2, "no wait after the last mirror attempt"


def test_both_db_repositories_down_fails_the_job(tmp_path):
    result, calls, _ = _download(tmp_path, fails=99)
    assert result.returncode != 0, result.stdout + result.stderr
    assert len(calls) == 4 and UPSTREAM_DB in calls[3], calls
    assert "::error::" in result.stdout


def test_the_scans_after_the_download_use_its_db_and_findings_still_fail_the_job():
    steps = _filesystem_steps()
    names = [s.get("name", s.get("uses", "")) for s in steps]
    db = steps.index(_db_step())
    scan = next(i for i, s in enumerate(steps) if "trivy fs --scanners" in str(s.get("run", "")))
    gate = next(i for i, s in enumerate(steps) if "scripts/trivy-new-findings.sh gate" in str(s.get("run", "")))
    assert db < scan < gate, names
    # Neither scan downloads the DB again, so neither can meet the mirror's 404.
    assert "--skip-db-update" in str(steps[scan]["run"]), steps[scan]
    assert str((steps[gate].get("env") or {}).get("TRIVY_SKIP_DB_UPDATE")).lower() == "true", steps[gate]
    # A finding still fails the job: no step is allowed to fail quietly, and
    # the gate step is the script's exit status, unchanged.
    assert all(not s.get("continue-on-error") for s in steps), names
    assert str(steps[gate]["run"]).rstrip().endswith('scripts/trivy-new-findings.sh gate --base "${base}"')
