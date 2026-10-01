"""The agent image carries the toolbox an agent needs to work on this repository.

WHY THIS EXISTS. docs/BUILD_PROMPT_V2.md §2.12 lists what an agent in
`agent-runtime-base` must be able to run: `gh` to open a pull request at all,
`gcloud` and `kubectl` to inspect the infrastructure it works on, the
Terraform/OpenTofu/tflint/checkov/trivy set that `make lint` and `make security`
call, `shellcheck` and `make` because CLAUDE.md's definition of done is written
in them, and the `docker` CLI. The gap audit of 2026-10-01 (S34) found the image
installed none of them. An agent asked to "fix the failing lint" in that image
gets `command not found` and -- worse -- may report success it never checked.

WHAT IS ASSERTED, and why each one is a test and not a review comment:

  * every required tool is named by the build's own smoke step, and that step,
    EXECUTED against stand-in binaries, fails the build when any one of them is
    missing and prints how many it checked. A smoke step that iterates once over
    a word-split-less list has reported "clean" here before (CLAUDE.md, rule
    zero), so the count is asserted, not just the exit code;
  * the smoke step runs as the agent user, after the last `USER`, and nothing
    is installed after it -- no tool needs root at agent runtime;
  * every artifact the runtime stage downloads is fetched at a pinned version
    and checked against a sha256 written in the Dockerfile BEFORE it is
    unpacked, and the comment above each checksum says where it came from;
  * checkov is installed with `uv tool install` into its own environment at a
    pinned version and a pinned resolution date, so it can neither change the
    worker's /opt/venv nor resolve a different dependency tree next month;
  * gcloud's bundled Python is removed, because that copy (cryptography 46.0.7,
    urllib3 2.7.0, msgpack 1.1.2, setuptools 70.3.0 in google-cloud-cli
    587.0.0) carries fixable HIGH advisories the promote scan refuses;
  * tools held back from the default build are only the three that had no
    clean release on 2026-10-01, and the hold is a build argument whose install
    path is real, not a TODO;
  * docs/versions.md states the same versions the Dockerfile pins.

NOT asserted: that the pinned versions are free of advisories. That is a fact
about a vulnerability database on a date; the release's trivy step decides it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
VERSIONS_DOC = REPO / "docs" / "versions.md"

#: BUILD_PROMPT_V2 §2.12, in the order it lists them.
REQUIRED_TOOLS = (
    "gh",
    "gcloud",
    "kubectl",
    "terraform",
    "tofu",
    "tflint",
    "checkov",
    "trivy",
    "shellcheck",
    "make",
    "docker",
)

#: The only tools allowed behind the default-off build argument. Each had a
#: fixable HIGH advisory in every published release on 2026-10-01 (see the
#: Dockerfile). Anything else added here is a tool quietly dropped.
MAY_BE_HELD = {"tofu", "tflint", "trivy"}

HOLD_ARG = "INSTALL_TOFU_TFLINT_TRIVY"


# --------------------------------------------------------------------------- #
# A small Dockerfile reader: stages, logical instructions, preceding comments.
# --------------------------------------------------------------------------- #


class Instruction:
    def __init__(self, keyword: str, body: str, comments: list[str], index: int):
        self.keyword = keyword
        self.body = body
        self.comments = comments
        self.index = index

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.keyword} {self.body[:60]!r}"


def _instructions(text: str) -> list[Instruction]:
    out: list[Instruction] = []
    comments: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("#"):
            comments.append(stripped.lstrip("#").strip())
            i += 1
            continue
        parts = [raw.rstrip()]
        while parts[-1].endswith("\\") and i + 1 < len(lines):
            i += 1
            parts.append(lines[i].rstrip())
        joined = "\n".join(parts)
        keyword, _, body = joined.strip().partition(" ")
        out.append(Instruction(keyword.upper(), body, comments, len(out)))
        comments = []
        i += 1
    return out


def _runtime_stage() -> list[Instruction]:
    instrs = _instructions(DOCKERFILE.read_text())
    start = next(
        i.index for i in instrs if i.keyword == "FROM" and re.search(r"\bAS runtime\b", i.body)
    )
    return [i for i in instrs if i.index > start]


def _arg_defaults(stage: list[Instruction]) -> dict[str, Instruction]:
    args: dict[str, Instruction] = {}
    for ins in stage:
        if ins.keyword == "ARG" and "=" in ins.body:
            args[ins.body.split("=", 1)[0].strip()] = ins
    return args


def _arg_value(ins: Instruction) -> str:
    return ins.body.split("=", 1)[1].strip()


def _shell_text(ins: Instruction) -> str:
    """A RUN body with line continuations removed, as /bin/sh sees it."""
    return re.sub(r"\\\n", " ", ins.body)


def _smoke_step() -> Instruction:
    runs = [
        i for i in _runtime_stage() if i.keyword == "RUN" and "toolbox smoke" in _shell_text(i)
    ]
    assert len(runs) == 1, "expected exactly one RUN that prints 'toolbox smoke'"
    return runs[0]


def _substitute_args(text: str, values: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return values.get(name, m.group(0))

    return re.sub(r"\$\{([A-Z0-9_]+)\}|\$([A-Z][A-Z0-9_]+)\b", repl, text)


# --------------------------------------------------------------------------- #
# The smoke step: names every tool, fails on a missing one, counts what it ran.
# --------------------------------------------------------------------------- #


def _smoke_tool_lists() -> tuple[list[str], list[str]]:
    text = _shell_text(_smoke_step())
    always = re.search(r'\btools="([^"]+)"', text)
    held = re.search(r'\btools="\$tools ([^"]+)"', text)
    assert always, "the smoke step must name its tools in a tools=\"...\" list"
    assert held, "the smoke step must add the held tools when the build argument is on"
    return always.group(1).split(), held.group(1).split()


def test_the_smoke_step_names_every_required_tool() -> None:
    always, held = _smoke_tool_lists()
    missing = [t for t in REQUIRED_TOOLS if t not in always and t not in held]
    assert not missing, f"the build never checks {missing}"


def test_only_the_three_tools_without_a_clean_release_are_held() -> None:
    _, held = _smoke_tool_lists()
    assert set(held) <= MAY_BE_HELD, (
        f"{sorted(set(held) - MAY_BE_HELD)} moved behind {HOLD_ARG}: a tool with a "
        "clean release goes in the default build"
    )
    gate = _arg_defaults(_runtime_stage()).get(HOLD_ARG)
    assert gate is not None, f"{HOLD_ARG} must be a declared build argument"
    assert _arg_value(gate) in {"0", "1"}


def _fake_tools(bin_dir: Path, names: list[str]) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\necho "{name} fake 1.0"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_smoke(tmp_path: Path, present: list[str], hold_on: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _fake_tools(bin_dir, present)
    script = _substitute_args(_shell_text(_smoke_step()), {HOLD_ARG: hold_on})
    # The stand-ins first, then only what sh itself needs; a real gh or
    # terraform on this machine must not satisfy the check.
    sys_bins = [d for d in ("/usr/bin", "/bin") if os.path.isdir(d)]
    env = {"PATH": os.pathsep.join([str(bin_dir), *sys_bins]), "HOME": str(tmp_path)}
    return subprocess.run(
        [*_shell_for(_smoke_step()), script], capture_output=True, text=True, env=env, check=False
    )


def _shell_for(target: Instruction) -> list[str]:
    """The shell Docker runs `target` with: the last SHELL before it, else sh -c."""
    shell = ["/bin/sh", "-c"]
    for ins in _instructions(DOCKERFILE.read_text()):
        if ins.index >= target.index:
            break
        if ins.keyword == "FROM":
            shell = ["/bin/sh", "-c"]  # SHELL does not cross a stage boundary
        elif ins.keyword == "SHELL":
            shell = json.loads(ins.body)
    return shell


@pytest.mark.parametrize("hold_on", ["0", "1"])
def test_the_smoke_step_passes_and_counts_when_every_tool_is_present(
    tmp_path: Path, hold_on: str
) -> None:
    always, held = _smoke_tool_lists()
    expected = always + (held if hold_on == "1" else [])
    proc = _run_smoke(tmp_path, expected, hold_on)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"toolbox smoke: {len(expected)} tools ok" in proc.stdout, proc.stdout


@pytest.mark.parametrize("hold_on", ["0", "1"])
def test_the_smoke_step_fails_the_build_when_any_tool_is_missing(
    tmp_path: Path, hold_on: str
) -> None:
    always, held = _smoke_tool_lists()
    expected = always + (held if hold_on == "1" else [])
    visited = 0
    for absent in expected:
        if Path("/usr/bin", absent).exists() or Path("/bin", absent).exists():
            continue  # this machine's copy would satisfy it; covered where absent
        case = tmp_path / absent
        proc = _run_smoke(case, [t for t in expected if t != absent], hold_on)
        assert proc.returncode != 0, f"build passed with {absent} missing:\n{proc.stdout}"
        assert absent in proc.stdout + proc.stderr, "the failure must name the missing tool"
        visited += 1
    assert visited >= len(expected) - 2, f"only {visited} of {len(expected)} cases ran"


def test_the_smoke_step_runs_as_the_agent_user_and_nothing_installs_after_it() -> None:
    stage = _runtime_stage()
    users = [i for i in stage if i.keyword == "USER"]
    assert users, "the runtime stage must drop to a non-root user"
    last_user = users[-1]
    assert not re.match(r"(root|0)\b", last_user.body.strip()), last_user.body
    smoke = _smoke_step()
    assert smoke.index > last_user.index, "the smoke step must run as the agent user"
    installers = re.compile(r"\b(curl|wget|apt-get|dpkg|uv tool install|npm install|pip install)\b")
    late = [
        i for i in stage if i.index > last_user.index and i.keyword == "RUN"
        and installers.search(_shell_text(i))
    ]
    assert not late, f"installed as the agent user, after USER: {late}"


# --------------------------------------------------------------------------- #
# Downloads: pinned version, sha256 written here, checked before unpacking.
# --------------------------------------------------------------------------- #

_CURL_OUT = re.compile(r'curl\s[^&;]*?-o\s+"?([^"\s]+)"?\s+"?([^"\s]+)"?')


def _downloads() -> list[tuple[Instruction, re.Match[str]]]:
    found = []
    for ins in _runtime_stage():
        if ins.keyword == "RUN":
            found.extend((ins, m) for m in _CURL_OUT.finditer(_shell_text(ins)))
    return found


def test_every_download_is_pinned_by_version_and_verified_before_use() -> None:
    args = _arg_defaults(_runtime_stage())
    downloads = _downloads()
    # The four npm patch tarballs predate this file; the toolbox adds the rest.
    toolbox = [d for d in downloads if "registry.npmjs.org" not in d[1].group(2)]
    assert len(toolbox) >= 9, f"expected the toolbox downloads, found {len(toolbox)}"
    for ins, fetch in downloads:
        text = _shell_text(ins)
        out, url = fetch.group(1), fetch.group(2)
        assert url.startswith("https://"), f"{url} is not HTTPS"
        # `latest` and dl.k8s.io's stable*.txt pointers move; a pool path
        # that merely contains `/stable/` (Docker's channel name) does not.
        assert not re.search(r"latest|stable[-.\d]*\.txt", url), f"{url} floats"
        version_vars = re.findall(r"\$\{([A-Z0-9_]*VERSION)\}", url)
        assert version_vars, f"{url} does not carry a pinned *_VERSION"
        for var in version_vars:
            assert var in args, f"{var} in {url} is not a build argument of the runtime stage"
            assert re.fullmatch(r"\d+(\.\d+){1,2}", _arg_value(args[var])), (
                f"{var}={_arg_value(args[var])} is not an exact version"
            )
        check = re.search(
            r'echo\s+"\$\{([A-Z0-9_]+)\}\s+' + re.escape(out) + r'"\s*\|\s*sha(256|512)sum -c',
            text,
        )
        assert check, f"{out} is never checked against a written checksum"
        sha_var = check.group(1)
        assert sha_var in args, f"{sha_var} is not a build argument"
        width = 64 if check.group(2) == "256" else 128
        assert re.fullmatch(rf"[0-9a-f]{{{width}}}", _arg_value(args[sha_var])), sha_var
        for use in re.finditer(re.escape(out), text):
            inside_fetch = fetch.start() <= use.start() < fetch.end()
            inside_check = check.start() <= use.start() < check.end()
            if not (inside_fetch or inside_check):
                assert use.start() > check.end(), f"{out} is used before it is verified"


def test_every_toolbox_checksum_says_where_it_came_from() -> None:
    stage = _runtime_stage()
    args = _arg_defaults(stage)
    toolbox_shas = [n for n in args if n.endswith("_SHA256")]
    assert len(toolbox_shas) >= 9, toolbox_shas
    for name in toolbox_shas:
        # The provenance comment sits above the contiguous run of ARGs (version,
        # checksum, pool id) that the checksum belongs to.
        pos = stage.index(args[name])
        block: list[str] = []
        while pos >= 0 and stage[pos].keyword == "ARG":
            block = stage[pos].comments + block
            pos -= 1
        assert any("https://" in line for line in block), (
            f"{name} has no comment naming the URL its checksum was read from"
        )


def test_the_toolbox_adds_no_apt_repository() -> None:
    # Vendor .debs are fetched by exact filename and checked like any other
    # download; a sources.list entry would float with whatever the repo serves.
    for ins in _runtime_stage():
        text = _shell_text(ins)
        assert "sources.list" not in text and "signed-by" not in text, text[:120]
    assert any("google-cloud-cli" in _shell_text(i) for i in _runtime_stage())


def test_gclouds_bundled_python_is_removed_before_gcloud_is_configured() -> None:
    runs = [
        i for i in _runtime_stage()
        if i.keyword == "RUN" and "google-cloud-cli" in _shell_text(i)
    ]
    assert len(runs) == 1
    text = _shell_text(runs[0])
    unpack = text.find("dpkg --unpack")
    removed = text.find("platform/bundledpythonunix")
    configure = text.find("dpkg --configure")
    assert -1 not in (unpack, removed, configure), text
    assert unpack < removed < configure, "remove the bundled Python between unpack and configure"
    assert "rm -rf" in text[unpack:configure]


def test_apt_upgrade_stays_and_every_base_is_pinned_by_digest() -> None:
    text = DOCKERFILE.read_text()
    runtime_runs = [_shell_text(i) for i in _runtime_stage() if i.keyword == "RUN"]
    assert any("apt-get upgrade" in r for r in runtime_runs)
    image_args = re.findall(r"^ARG ([A-Z_]+_IMAGE)=(\S+)$", text, flags=re.M)
    assert image_args
    for name, ref in image_args:
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), f"{name} is not pinned by digest"
    for line in re.findall(r"^FROM (\S+)", text, flags=re.M):
        assert line.startswith("${") or "@sha256:" in line, line


# --------------------------------------------------------------------------- #
# checkov: its own environment, its own pinned resolution.
# --------------------------------------------------------------------------- #


def test_checkov_is_isolated_from_the_workers_python() -> None:
    stage = _runtime_stage()
    args = _arg_defaults(stage)
    runs = [i for i in stage if i.keyword == "RUN" and "checkov" in _shell_text(i)]
    installs = [i for i in runs if "uv tool install" in _shell_text(i)]
    assert len(installs) == 1, "checkov is installed with `uv tool install`, once"
    text = _shell_text(installs[0])
    assert '"checkov==${CHECKOV_VERSION}"' in text
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg_value(args["CHECKOV_VERSION"]))
    assert "--exclude-newer" in text, "pin the dependency resolution date, not just checkov"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", _arg_value(args["CHECKOV_EXCLUDE_NEWER"])
    )
    tool_dir = re.search(r"UV_TOOL_DIR=(\S+)", text)
    assert tool_dir and not tool_dir.group(1).startswith("/opt/venv")
    assert "UV_PYTHON_DOWNLOADS=never" in text, "use the image's interpreter, never fetch one"
    for i in runs:
        assert not re.search(r"pip install[^&]*checkov", _shell_text(i)), "checkov via pip"


# --------------------------------------------------------------------------- #
# docs/versions.md says what the Dockerfile pins.
# --------------------------------------------------------------------------- #

#: tool name in the doc's table -> the Dockerfile ARG that pins it.
DOC_ROWS = {
    "gh": "GH_VERSION",
    "gcloud": "GCLOUD_VERSION",
    "kubectl": "KUBECTL_VERSION",
    "terraform": "TERRAFORM_VERSION",
    "tofu": "TOFU_VERSION",
    "tflint": "TFLINT_VERSION",
    "checkov": "CHECKOV_VERSION",
    "trivy": "TRIVY_VERSION",
    "shellcheck": "SHELLCHECK_VERSION",
    "docker": "DOCKER_CLI_VERSION",
}


def test_versions_doc_lists_the_image_toolbox_at_the_pinned_versions() -> None:
    doc = VERSIONS_DOC.read_text()
    section = doc.split("### Agent image toolbox", 1)
    assert len(section) == 2, "docs/versions.md needs an 'Agent image toolbox' section"
    body = section[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    args = _arg_defaults(_runtime_stage())
    for tool, var in DOC_ROWS.items():
        row = re.search(rf"^\|\s*`{re.escape(tool)}`\s*\|\s*\*\*([^*]+)\*\*", body, flags=re.M)
        assert row, f"no row for `{tool}` in the agent image toolbox table"
        want = _arg_value(args[var]).split("-")[0]
        assert row.group(1).strip() == want, (
            f"docs/versions.md says {tool} {row.group(1)}, the Dockerfile pins {want}"
        )
