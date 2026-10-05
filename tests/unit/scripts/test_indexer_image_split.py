"""The repository-index toolchain ships in its own image, agent-runtime-indexer,
and not in agent-runtime-base, which every claude-code and codex start pulls (#625).

WHY. Claude-code container start on Cloud Run Jobs went from a 71 s p50 on
2026-09-24 to ~115 s after the 10-01 toolbox and 166-168 s on 10-04/10-05,
the days the repo-index toolchain landed in agent-runtime-base: the Go
toolchain gopls runs `go list` with, gopls, terraform-ls, pyright and
typescript-language-server, and the tree-sitter environment. Measured from
the pinned artifacts on 2026-10-05 (docs/worker-images.md): about 133 MB
compressed and 409 MB unpacked, all of it pulled by every agent start and
used only by an index run.

The properties asserted here, read from the files that ship:

  * agent-runtime-base carries none of the toolchain;
  * agent-runtime-indexer carries all of it, proves each piece runs as the
    agent user at build time, and runs as uid 10001, never root;
  * the indexer is built FROM agent-runtime-base BY DIGEST: its build config
    resolves the digest, and the Dockerfile refuses a base given by tag;
  * every other FROM in it is pinned by digest, to the SAME Python the base
    is built on;
  * the build and the promotion both carry it, and the scheduled scan reads it.

WHAT THIS CANNOT PROVE: that the image builds, or what it weighs in the
registry. CI's `build images` job builds it; scripts/image-sizes.sh measures
it after release.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
BASE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
INDEXER_DIR = REPO / "images" / "agent-runtime-indexer"
INDEXER = INDEXER_DIR / "Dockerfile"
CLOUDBUILD = INDEXER_DIR / "cloudbuild.yaml"

#: What the toolchain is, as the Dockerfiles spell it.
TOOLCHAIN = (
    "/opt/repo-index",
    "swarm-repo-index",
    "swarm-repo-graph",
    "gopls",
    "terraform-ls",
    "/usr/local/go",
    "GO_VERSION",
    "npm ci",
    "repo-index/",
    "AS languageservers",
)


def _code(path: Path) -> str:
    """The Dockerfile without its comments: a comment naming a tool is not the tool."""
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _args(code: str) -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", code, re.M))


def test_the_base_image_carries_none_of_the_repo_index_toolchain():
    code = _code(BASE)
    present = [marker for marker in TOOLCHAIN if marker in code]
    assert present == [], f"agent-runtime-base still installs {present}"


def test_the_indexer_image_carries_the_whole_toolchain():
    code = _code(INDEXER)
    missing = [marker for marker in TOOLCHAIN if marker not in code]
    assert missing == [], f"agent-runtime-indexer does not install {missing}"
    # Its sources live beside it, not in the base image's directory.
    assert "COPY images/agent-runtime-indexer/repo-index/" in code
    assert (INDEXER_DIR / "repo-index" / "repo_index_extract.py").is_file()
    assert not (REPO / "images" / "agent-runtime-base" / "repo-index").exists()


def test_the_indexer_proves_every_tool_runs_as_the_agent_user():
    code = _code(INDEXER)
    user = code.rindex("USER swarm:swarm")
    for check in ("RUN swarm-repo-index --self-test",
                  "RUN swarm-repo-index --lsp-self-test",
                  "RUN swarm-repo-graph --self-test"):
        assert check in code, check
        assert code.index(check) > user, f"{check} runs before USER swarm:swarm"


def test_the_indexer_never_runs_as_root():
    users = re.findall(r"^USER (\S+)$", _code(INDEXER), re.M)
    assert users and users[-1] == "swarm:swarm", users


def test_the_indexer_keeps_the_base_entrypoint():
    pattern = re.compile(r"^ENTRYPOINT (.+)$", re.M)
    assert pattern.findall(_code(INDEXER))[-1] == pattern.findall(_code(BASE))[-1]


def test_every_from_is_the_base_by_digest_or_a_digest_pinned_arg():
    code = _code(INDEXER)
    args = _args(code)
    froms = re.findall(r"^FROM (\S+)(?: AS (\S+))?$", code, re.M)
    assert froms, "no FROM in the indexer Dockerfile"
    for ref, _ in froms:
        name = re.fullmatch(r"\$\{(\w+)\}", ref)
        assert name, f"FROM {ref} is not an ARG, so nothing pins it"
        if name.group(1) == "BASE_IMAGE":
            continue
        assert "@sha256:" in args[name.group(1)], f"{name.group(1)} is not pinned by digest"
    # The image is the base plus the toolchain: its last stage is the base.
    assert froms[-1][0] == "${BASE_IMAGE}"


def test_the_base_is_never_taken_by_tag():
    code = _code(INDEXER)
    # No default: a build that was not handed a base fails at FROM ...
    assert "BASE_IMAGE" not in _args(code)
    # ... and one handed a tag fails before anything is installed on it.
    guard = re.search(r'case "\$\{BASE_IMAGE\}" in\s*\\?\s*\*@sha256:\*\)', code)
    assert guard, "the Dockerfile does not refuse a BASE_IMAGE without a digest"
    assert code.index(guard.group(0)) < code.index("COPY images/agent-runtime-indexer/repo-index/")


def test_the_languageservers_stage_builds_on_the_bases_python():
    assert _args(_code(INDEXER))["PYTHON_IMAGE"] == _args(_code(BASE))["PYTHON_IMAGE"]


def test_the_build_config_resolves_the_base_to_a_digest():
    config = yaml.safe_load(CLOUDBUILD.read_text(encoding="utf-8"))
    steps = {step["id"]: step for step in config["steps"]}
    resolve = "\n".join(steps["resolve-base"]["args"])
    assert "agent-runtime-base:$${tag}" in resolve
    assert "RepoDigests" in resolve
    build = "\n".join(steps["build"]["args"])
    assert '--build-arg "BASE_IMAGE=$${base}"' in build
    assert config["substitutions"]["_DOCKERFILE"] == "images/agent-runtime-indexer/Dockerfile"
    assert config["images"] == ["${_IMAGE}:${_TAG}"]


def _targets(path: Path) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", path.read_text(encoding="utf-8"), re.M)
    assert found, f"no ALL_TARGETS in {path}"
    return found.group(1).split()


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_indexer(script):
    assert "agent-runtime-indexer" in _targets(REPO / "scripts" / script)


def test_build_and_promotion_name_the_same_images():
    assert _targets(REPO / "scripts" / "build-images.sh") == _targets(REPO / "scripts" / "push-images.sh")


def test_the_scheduled_scan_reads_the_indexer():
    text = (REPO / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
    loop = re.search(r"for image in ([^;]+); do\n\s*echo \"==> \$\{image\}:dev\"", text)
    assert loop, "the scheduled channel scan loop moved"
    assert "agent-runtime-indexer" in loop.group(1).split()


def test_the_base_still_carries_what_claude_code_and_codex_start():
    code = _code(BASE)
    assert "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in code
    assert "@openai/codex@${CODEX_VERSION}" in code
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "agent_worker"]' in code
