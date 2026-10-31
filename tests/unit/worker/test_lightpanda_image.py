"""The agent-runtime-lightpanda image, read from its Dockerfile's text
(docs/design/lightpanda-runtime.md sections 5.D and 6.4; lane L2,
LIGHTPANDA-IMAGE, part of #2688).

The image is agent-runtime-base, by digest, plus one layer: the Lightpanda
release binary, fetched through the build's tool mirror and checked against
its pinned sha256, its licence and source notice, and a build-time self-test.
No Chromium. Every assertion here reads the files in the repository; nothing
is built and no network or cloud credential is used.
"""

from __future__ import annotations

import importlib.util
import os
import re
import stat
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
IMAGE_DIR = REPO / "images" / "agent-runtime-lightpanda"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
RECIPE = IMAGE_DIR / "cloudbuild.yaml"
SELFTEST = IMAGE_DIR / "selftest.py"
THIRD_PARTY = IMAGE_DIR / "THIRD_PARTY"
MANIFEST = REPO / "images" / "agent-runtime-base" / "tool-mirror.sha256"

#: Release 1.0.2 (2026-10-23). The two binary digests are the release API's
#: own `digest` for each asset, and the design's section 2 table; the amd64
#: one was also recomputed from the download. The tag is lightweight and
#: points at COMMIT. The source archive is GitHub's tag archive, hashed when
#: it was downloaded on 2026-10-31.
VERSION = "1.0.2"
SHA256_AMD64 = "ae7d01d261efb9e480e18ce3424533332883863bcf70b12a4321b5733f37e19e"
SHA256_ARM64 = "ac546dfc43c533430da92b75ab36f2a29da7d6324d5d38baf606ef2e0d849345"
COMMIT = "c5f7bb29b127040c899d4e181b5709f76fc71a6f"
SOURCE_SHA256 = "6a7a274acfdbf6d87b573d1a90029d2e17f71f718f78b03099d96d48f514d64e"
RELEASE = f"github.com/lightpanda-io/browser/releases/download/{VERSION}"
OBJECTS = {
    SHA256_AMD64: f"{RELEASE}/lightpanda-x86_64-linux",
    SHA256_ARM64: f"{RELEASE}/lightpanda-aarch64-linux",
    SOURCE_SHA256: f"github.com/lightpanda-io/browser/archive/refs/tags/{VERSION}.tar.gz",
}
#: Where the release bundle keeps the Corresponding Source (design 6.4).
MIRRORED_SOURCE = f"https://storage.googleapis.com/swarmcloud/third-party/lightpanda/{VERSION}/source.tar.gz"
BINARY = "/opt/swarm/lightpanda/lightpanda"
DOC = "/usr/share/doc/lightpanda"


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


def _lines() -> list[str]:
    assert DOCKERFILE.is_file(), f"{DOCKERFILE.relative_to(REPO)} does not exist"
    return _code_lines(DOCKERFILE.read_text())


def _args() -> dict[str, str]:
    args: dict[str, str] = {}
    for line in _lines():
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)$", line)
        if m:
            args[m.group(1)] = m.group(2)
    return args


def _install_run() -> str:
    runs = [line for line in _lines() if line.startswith("RUN ") and "/tmp/tool-mirror/" in line]
    assert len(runs) == 1, f"expected one RUN reading the tool mirror, found {len(runs)}"
    return runs[0]


def _render(text: str, args: dict[str, str]) -> str:
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: args.get(m.group(1), m.group(0)), text)


def test_it_is_agent_runtime_base_by_digest() -> None:
    lines = _lines()
    froms = [line for line in lines if line.startswith("FROM ")]
    assert froms == ["FROM ${BASE_IMAGE} AS runtime"], froms
    # A base named by tag is refused at build time, not trusted.
    assert any(line.startswith("RUN case \"${BASE_IMAGE}\" in") and "*@sha256:*)" in line and "exit 1" in line
               for line in lines), "the Dockerfile does not refuse a BASE_IMAGE that is not a digest"
    recipe = yaml.safe_load(RECIPE.read_text())
    steps = {step["id"]: step for step in recipe["steps"]}
    resolve = " ".join(steps["resolve-base"]["args"])
    assert 'base="$${registry}/agent-runtime-base:$${tag}"' in resolve, resolve
    assert "RepoDigests" in resolve
    build = " ".join(steps["build"]["args"])
    assert '--build-arg "BASE_IMAGE=$${base}"' in build, build
    # The base WITH codex, like agent-runtime-indexer: the profile is
    # claude-code's in every field but its name, image and secret slots.
    uncommented = "\n".join(l for l in RECIPE.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert "agent-runtime-base-nocodex" not in uncommented


def test_the_version_and_both_sha256s_are_pinned_in_full() -> None:
    args = _args()
    assert args.get("LIGHTPANDA_VERSION") == VERSION
    assert args.get("LIGHTPANDA_SHA256_AMD64") == SHA256_AMD64
    assert args.get("LIGHTPANDA_SHA256_ARM64") == SHA256_ARM64
    assert args.get("LIGHTPANDA_COMMIT") == COMMIT
    assert args.get("LIGHTPANDA_SOURCE_SHA256") == SOURCE_SHA256


def test_the_tool_mirror_lists_the_same_files_at_the_same_sha256s() -> None:
    rows = {}
    for line in MANIFEST.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            sha, obj = line.split(None, 1)
            rows[obj.strip()] = sha
    for sha, obj in OBJECTS.items():
        assert rows.get(obj) == sha, f"{MANIFEST.relative_to(REPO)} does not list {obj} at {sha}"
    assert [obj for obj in rows if "lightpanda" in obj] == list(OBJECTS.values()), (
        "the manifest's Lightpanda rows are exactly the two binaries and the source archive, in that order"
    )


def test_every_file_is_read_from_the_mirror_and_checked_against_its_pin() -> None:
    run = _install_run()
    assert run.startswith("RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror "), run[:120]
    args = _args()
    reads = {}
    for m in re.finditer(r'-o\s+(\S+)\s+"file:///tmp/tool-mirror/([^"]+)"', run):
        out, obj = m.group(1), _render(m.group(2), args)
        check = re.search(r'echo\s+"\$\{([A-Z0-9_]+)\}\s+' + re.escape(out) + r'"\s*\|\s*sha256sum -c -', run)
        assert check, f"{out} (from {obj}) is never checked against its sha256"
        reads[obj] = args[check.group(1)]
    assert reads == {obj: sha for sha, obj in OBJECTS.items()}, reads
    # Nothing is fetched from anywhere but the mirror.
    assert not re.search(r"https?://", _render(run, args).replace(MIRRORED_SOURCE, "")), run
    # The architecture picks the binary; an unknown one fails the build.
    assert "ARG TARGETARCH" in _lines()
    assert re.search(r'case "\$\{TARGETARCH\}" in amd64\)', run), run
    assert re.search(r"\*\) echo [^;]*>&2; exit 1 ;;", run), run


def test_the_binary_is_root_owned_0755_at_its_fixed_path() -> None:
    run = _install_run()
    assert "install -d -o root -g root -m 0755 /opt/swarm/lightpanda" in run, run
    assert re.search(rf"install -o root -g root -m 0755 \S+ {re.escape(BINARY)}(\s|$)", run), run


def test_telemetry_and_core_dumps_are_off() -> None:
    envs = " ".join(line for line in _lines() if line.startswith("ENV "))
    assert re.search(r"\bLIGHTPANDA_DISABLE_TELEMETRY=true\b", envs), envs
    assert re.search(r"\bLIGHTPANDA_DISABLE_CORE_DUMP=1\b", envs), envs


def test_the_licence_source_notice_and_third_party_notices_are_installed() -> None:
    run = _install_run()
    args = _args()
    rendered = _render(run, args)
    # LICENSE is the AGPL text from the tag's own source archive, not a copy
    # kept in this repository.
    assert re.search(r"tar -xzf /tmp/lightpanda/source\.tar\.gz .*browser-1\.0\.2/LICENSE", rendered), rendered
    for name in ("LICENSE", "LICENSING.md", "SOURCE", "THIRD_PARTY"):
        assert re.search(rf"(install -o root -g root -m 0644 \S+|>) {re.escape(DOC)}/{re.escape(name)}(\s|$)", run), name
    assert "/tmp/swarm-lightpanda-build/THIRD_PARTY" in run
    # SOURCE names the tag, the commit, and the mirrored archive's URL and sha256.
    source = re.search(r"printf '([^']*)'((?: \"[^\"]*\")+) > " + re.escape(f"{DOC}/SOURCE"), run)
    assert source, run
    values = _render(source.group(2), args)
    for want in (VERSION, COMMIT, MIRRORED_SOURCE, SOURCE_SHA256):
        assert want in values, f"SOURCE does not name {want}: {values}"
    third_party = THIRD_PARTY.read_text()
    for component in ("V8", "libcurl", "html5ever", "BoringSSL", "AGPL-3.0-only", COMMIT):
        assert component in third_party, component


def test_there_is_no_chromium_and_no_playwright_browser_install() -> None:
    code = "\n".join(_lines()).lower()
    assert "chromium" not in code
    assert "playwright install" not in code
    assert "chrome" not in code


def test_the_self_test_runs_once_as_the_agent_and_checks_the_version() -> None:
    lines = _lines()
    users = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    runs = [i for i, line in enumerate(lines) if line.startswith("RUN ") and "selftest.py" in line]
    assert len(runs) == 1, "one self-test RUN: two check-only RUNs can write a duplicate layer"
    assert lines[users[-1]] == "USER swarm:swarm" and users[-1] < runs[0]
    run = lines[runs[0]]
    assert 'LIGHTPANDA_VERSION="${LIGHTPANDA_VERSION}"' in run, run
    assert "--mount=type=bind,source=images/agent-runtime-lightpanda,target=/tmp/swarm-lightpanda-build" in run
    assert not any(line.startswith("COPY") and "selftest.py" in line for line in lines), "selftest.py is not shipped"
    assert lines[-1] == 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]'


def _selftest():
    spec = importlib.util.spec_from_file_location("lightpanda_selftest", SELFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_binary(tmp_path: Path, prints: str) -> Path:
    fake = tmp_path / "lightpanda"
    fake.write_text(f"#!/bin/sh\n[ \"$1\" = version ] || exit 2\necho '{prints}'\n")
    fake.chmod(0o755)
    return fake


def test_the_self_test_accepts_the_pinned_version(tmp_path: Path) -> None:
    _selftest().check_version(_fake_binary(tmp_path, VERSION), VERSION)


@pytest.mark.parametrize("prints", ["1.0.1", "1.0.2-dev", ""])
def test_the_self_test_refuses_any_other_version(tmp_path: Path, prints: str) -> None:
    with pytest.raises(SystemExit) as failed:
        _selftest().check_version(_fake_binary(tmp_path, prints), VERSION)
    assert VERSION in str(failed.value)


def test_the_self_test_refuses_a_binary_the_agent_could_replace(tmp_path: Path) -> None:
    fake = _fake_binary(tmp_path, VERSION)
    fake.chmod(0o775)
    with pytest.raises(SystemExit) as failed:
        _selftest().check_owner_and_mode(fake, uid=fake.stat().st_uid, gid=None)
    assert "0755" in str(failed.value)
    fake.chmod(0o755)
    _selftest().check_owner_and_mode(fake, uid=fake.stat().st_uid, gid=fake.stat().st_gid)
    with pytest.raises(SystemExit):
        _selftest().check_owner_and_mode(fake, uid=fake.stat().st_uid + 1, gid=None)


def test_the_self_test_finds_a_chromium(tmp_path: Path) -> None:
    selftest = _selftest()
    # A protocol library and a non-executable file of the same name are not a browser.
    (tmp_path / "node_modules" / "chromium-bidi").mkdir(parents=True)
    (tmp_path / "node_modules" / "chrome").write_text("")
    assert selftest.find_chromium([tmp_path]) == []
    playwright = tmp_path / "ms-playwright" / "chromium-1187"
    playwright.mkdir(parents=True)
    assert selftest.find_chromium([tmp_path]) == [str(playwright)]
    playwright.rmdir()
    browser = tmp_path / "lib" / "chromium" / "chrome"
    browser.parent.mkdir(parents=True)
    browser.write_text("")
    browser.chmod(browser.stat().st_mode | stat.S_IXUSR)
    assert selftest.find_chromium([tmp_path]) == [str(browser)]


def test_the_version_check_hands_the_binary_no_inherited_environment(tmp_path: Path, monkeypatch) -> None:
    import secrets

    monkeypatch.setenv("SWARM_FAKE_TOKEN", secrets.token_hex(16))
    fake = tmp_path / "lightpanda"
    fake.write_text("#!/bin/sh\n[ -z \"$SWARM_FAKE_TOKEN\" ] || exit 3\n"
                    "[ \"$LIGHTPANDA_DISABLE_TELEMETRY\" = true ] || exit 4\necho 1.0.2\n")
    fake.chmod(0o755)
    _selftest().check_version(fake, VERSION)
    assert os.environ["SWARM_FAKE_TOKEN"]
