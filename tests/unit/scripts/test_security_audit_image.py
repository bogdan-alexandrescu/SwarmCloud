"""Cloudflare's security audit skill baked into the agent images at a pinned commit, through the build mirror.

The owner, 4 November 2026: github.com/cloudflare/security-audit-skill (MIT)
is the second catalogue plugin, beside gstack, baked into the existing agent
images at commit c1c8a8c1471069fb0e188eeaff69b8e8db6564a8; no new runner
profile, no new image, nothing installed at runtime
(docs/design/agent-plugins-packaging.md, "The second plugin"). This file
holds the image to it:

  * the commit's source archive is a row of
    images/agent-runtime-base/tool-mirror.sha256, and the base Dockerfile's
    ARGs, the row and both catalogues name the same commit;
  * the security-audit-build stage reads the archive only from the tool
    mirror, checks its sha256 and the commit in its pax header, and stages
    ONLY skills/security-audit/ and LICENSE, read-only;
  * the runtime copies that to the catalogue's install path, root-owned;
  * the skill's own two validator test files pass under node on the pinned
    archive, so a bump that breaks them goes red here.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BASE_DIR = REPO / "images" / "agent-runtime-base"
BASE_DOCKERFILE = BASE_DIR / "Dockerfile"
MANIFEST = BASE_DIR / "tool-mirror.sha256"
STAGED_MIRROR = REPO / "build" / "tool-mirror"

COMMIT = "c1c8a8c1471069fb0e188eeaff69b8e8db6564a8"
ROW = (
    "53e708ebf770dfe35b57df0586a5058707081a870cd149ce30a2d57c51ed29b6",
    f"github.com/cloudflare/security-audit-skill/archive/{COMMIT}.tar.gz",
)
PLUGIN_ROOT = "/opt/swarm/plugins/security-audit"
SKILL = "skills/security-audit"
VALIDATOR_TESTS = ("validate-findings.test.cjs", "validate-coverage-ledger.test.cjs")


def _code_lines(text: str) -> list[str]:
    """Logical instructions, continuations joined, comment lines dropped."""
    out: list[str] = []
    cur: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#") or (not s and not cur):
            continue
        cur.append(s.rstrip("\\").rstrip())
        if not s.endswith("\\"):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _stages(path: Path) -> dict[str, list[str]]:
    """Each stage's name -> its instructions (the FROM line first)."""
    stages: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in _code_lines(path.read_text()):
        m = re.match(r"FROM\s+\S+(?:\s+AS\s+(\S+))?", line)
        if m:
            current = [line]
            stages[(m.group(1) or f"stage{len(stages)}").lower()] = current
        elif current is not None:
            current.append(line)
    return stages


def _args(lines: list[str]) -> dict[str, str]:
    args: dict[str, str] = {}
    for line in lines:
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)$", line)
        if m:
            args[m.group(1)] = m.group(2)
    return args


def _manifest_rows() -> set[tuple[str, str]]:
    rows = set()
    for line in MANIFEST.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            sha, obj = line.split(None, 1)
            rows.add((sha, obj.strip()))
    return rows


# --------------------------------------------------------------------------- #
# The pins: the mirror row, the Dockerfile's ARGs and the catalogues agree.
# --------------------------------------------------------------------------- #


def test_the_mirror_lists_the_skills_archive_at_its_commit() -> None:
    assert ROW in _manifest_rows(), f"{MANIFEST.name} has no row {ROW}"


def test_the_build_stage_args_equal_the_mirror_row_and_the_catalogues() -> None:
    from agent_worker import plugins
    from swarm_api import plugincatalogue

    args = _args(_stages(BASE_DOCKERFILE)["security-audit-build"])
    rows = {obj: sha for sha, obj in _manifest_rows()}
    obj = f"github.com/cloudflare/security-audit-skill/archive/{args['SECURITY_AUDIT_COMMIT']}.tar.gz"
    # MUTATION: bump the ARG and forget the row, or the reverse.
    assert rows.get(obj) == args["SECURITY_AUDIT_SHA256"]
    assert args["SECURITY_AUDIT_COMMIT"] == COMMIT
    for catalogue in (plugincatalogue.CATALOGUE, plugins.CATALOGUE):
        entry = catalogue["security-audit"]
        assert (entry.version, entry.install_path, entry.skill_folders) == (
            COMMIT, PLUGIN_ROOT, "skills")


# --------------------------------------------------------------------------- #
# The build stage and the runtime copy.
# --------------------------------------------------------------------------- #


def _stage_run() -> str:
    stage = _stages(BASE_DOCKERFILE)["security-audit-build"]
    runs = [line for line in stage if "/tmp/tool-mirror/" in line]
    assert len(runs) == 1, runs
    return runs[0]


def test_the_build_stage_reads_only_the_mirror_and_checks_the_archive() -> None:
    stage = _stages(BASE_DOCKERFILE)["security-audit-build"]
    run = _stage_run()
    assert run.startswith("RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror")
    assert ('"file:///tmp/tool-mirror/github.com/cloudflare/security-audit-skill/archive/'
            '${SECURITY_AUDIT_COMMIT}.tar.gz"') in run
    # MUTATION: drop either check and a different archive is baked in.
    assert 'echo "${SECURITY_AUDIT_SHA256}  /tmp/security-audit.tar.gz" | sha256sum -c -' in run
    assert ('test "$(gzip -dc /tmp/security-audit.tar.gz | git get-tar-commit-id)" = '
            '"${SECURITY_AUDIT_COMMIT}"') in run
    assert run.index("sha256sum -c") < run.index("tar -xzf")
    assert run.index("git get-tar-commit-id") < run.index("tar -xzf")
    # The Dockerfiles may not fetch from GitHub directly.
    for line in stage:
        assert "https://" not in line and "http://" not in line, line


def test_the_build_stage_stages_only_the_skill_folder_and_the_licence_read_only() -> None:
    run = _stage_run()
    copies = re.findall(r"\bcp\s+(?:-R\s+)?(\S+)\s+(\S+)", run)
    # MUTATION: copy the whole archive (README.md, anything a bump adds).
    assert copies == [
        ('"${scratch}/skills/security-audit"', "/out/security-audit/skills/security-audit"),
        ('"${scratch}/LICENSE"', "/out/security-audit/LICENSE"),
    ]
    assert "find /out/security-audit -type d -exec chmod 0755 {} +" in run
    assert "find /out/security-audit -type f -exec chmod 0644 {} +" in run
    # No symlink, device or fifo reaches the image.
    assert 'test -z "$(find /out/security-audit ! -type f ! -type d)"' in run


def test_the_base_image_ships_it_at_the_catalogues_path_root_owned() -> None:
    runtime = _stages(BASE_DOCKERFILE)["runtime"]
    copies = [line for line in runtime if line.startswith("COPY --from=security-audit-build")]
    assert copies == [f"COPY --from=security-audit-build /out/security-audit/ {PLUGIN_ROOT}/"]
    # No --chown: COPY writes uid 0, so the agent's uid cannot change it.
    assert all("--chown" not in line for line in copies)
    # Nothing in the runtime installs it again, or fetches it, at run time.
    assert not any("security-audit-skill" in line for line in runtime)


# --------------------------------------------------------------------------- #
# The skill's own validator tests, on the pinned archive.
# --------------------------------------------------------------------------- #


def _archive_bytes() -> bytes:
    """The pinned archive: build/tool-mirror's copy when a build staged one,
    else the upstream URL scripts/refresh-build-mirror.sh fills the mirror
    from. Either way checked against the row's sha256 before it is used."""
    sha, obj = ROW
    staged = STAGED_MIRROR / obj
    if staged.is_file():
        data = staged.read_bytes()
    else:
        last: Exception | None = None
        data = b""
        for attempt in range(4):
            try:
                with urllib.request.urlopen(f"https://{obj}", timeout=60) as response:
                    data = response.read()
                break
            except OSError as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        else:
            pytest.fail(f"https://{obj} could not be read ({last}); main's build fills the "
                        "mirror from the same URL, so it would fail too")
    assert hashlib.sha256(data).hexdigest() == sha, f"{obj} does not match its pinned sha256"
    return data


@pytest.fixture(scope="module")
def skill_copy(tmp_path_factory) -> Path:
    """skills/security-audit/ and LICENSE from the pinned archive, as the
    build stage stages them."""
    out = tmp_path_factory.mktemp("security-audit")
    with tarfile.open(fileobj=io.BytesIO(_archive_bytes()), mode="r:gz") as archive:
        assert archive.pax_headers.get("comment") == COMMIT, "the archive is not the pinned commit"
        prefix = f"security-audit-skill-{COMMIT}/"
        wanted = [m for m in archive.getmembers()
                  if m.name == prefix + "LICENSE" or m.name.startswith(prefix + SKILL + "/")]
        assert all(m.isfile() or m.isdir() for m in wanted)
        for member in wanted:
            member.name = member.name[len(prefix):]
        archive.extractall(out, members=wanted, filter="data")
    return out


def _node() -> str:
    found = shutil.which("node")
    if found is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the validators cannot run; "
                        "they must not pass by skipping")
        pytest.skip("node is not installed here; CI runs these")
    return found


def test_the_pinned_copy_is_the_skill_and_its_licence(skill_copy: Path) -> None:
    skill = skill_copy / SKILL
    assert (skill_copy / "LICENSE").read_text().startswith("MIT License")
    assert (skill / "SKILL.md").read_text().startswith("---\nname: security-audit\n")
    for name in ("report-schema.json", "validate-findings.cjs", "validate-coverage-ledger.cjs",
                 *VALIDATOR_TESTS):
        assert (skill / name).is_file(), name
    # SKILL.md plus the fourteen method files at the pinned commit.
    assert len(list(skill.glob("*.md"))) == 15


def test_the_skills_own_validator_tests_pass_on_the_pinned_copy(skill_copy: Path) -> None:
    node = _node()
    skill = skill_copy / SKILL
    proc = subprocess.run(
        [node, "--test", *VALIDATOR_TESTS], cwd=skill, capture_output=True, text=True,
        timeout=300, check=False,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(skill_copy)},
    )
    # MUTATION: bump the pin to a commit whose validators fail their own tests.
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]
    summary = dict(re.findall(r"^ℹ (tests|pass|fail) (\d+)$", proc.stdout, re.M))
    assert int(summary.get("tests", 0)) > 0 and summary.get("fail") == "0", proc.stdout[-2000:]
