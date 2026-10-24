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
  * tools held back from the default build are only the ones that had no
    clean release when last scanned (tofu and tflint, 2026-10-09; trivy left
    the hold at 0.75.0, #442), each held tool has its OWN build argument whose
    install path is real, not a TODO -- so whichever vendor ships a clean
    release first leaves the hold alone, as trivy did -- and no tool is
    installed by default at a release the scan recorded as failing;
  * docs/versions.md states the same versions the Dockerfile pins.

NOT asserted: that the pinned versions are free of advisories. That is a fact
about a vulnerability database on a date; the release's trivy step decides it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
#: fixable HIGH advisory in every published release when last scanned
#: (2026-10-09, see the Dockerfile). trivy left this set when 0.75.0 scanned
#: clean (#442). Anything else added here is a tool quietly dropped.
MAY_BE_HELD = {"tofu", "tflint"}

#: Each held tool's own default-off build argument. One shared argument meant
#: neither tool could ship until both had a clean release (#442, 2026-10-07).
HOLD_ARG_OF = {"tofu": "INSTALL_TOFU", "tflint": "INSTALL_TFLINT"}

#: The argument both tools used to share. It must not come back, in the
#: Dockerfile or in the docs that tell an operator how to turn the tools on.
COMBINED_HOLD_ARG = "INSTALL_TOFU" + "_TFLINT"

#: Releases the image scan (`trivy rootfs --scanners vuln --severity
#: HIGH,CRITICAL --ignore-unfixed`, the promote gate's filter) found a fixable
#: HIGH in. The Dockerfile records each finding beside the pin. A tool in the
#: default build pinned at one of these is an image the promote gate refuses.
SCANNED_FAILING = {
    "TOFU_VERSION": {"1.12.6", "1.12.7", "1.13.0", "1.13.1"},
    "TFLINT_VERSION": {"0.64.0"},
    "TRIVY_VERSION": {"0.74.0"},
}

#: Which pin each toolbox tool's ARG is.
PIN_OF = {"tofu": "TOFU_VERSION", "tflint": "TFLINT_VERSION", "trivy": "TRIVY_VERSION"}

#: The PostgreSQL server the test services run (docs/design/test-services.md
#: §3.1, §4.1; owner decision 2026-10-23: option A, Postgres 16 only). The
#: worker starts `initdb` then `postgres` as its own child and waits on
#: `pg_isready`; `pg_ctl` stops it. Each must be on the agent's PATH, at this
#: major, or the build fails.
POSTGRES_MAJOR = "16"
POSTGRES_TOOLS = ("postgres", "initdb", "pg_ctl", "pg_isready")

#: The PostgreSQL Global Development Group's apt signing key, ACCC4CF8, by its
#: FULL fingerprint (https://www.postgresql.org/download/linux/debian/; the key
#: file https://www.postgresql.org/media/keys/ACCC4CF8.asc, read 2026-10-24,
#: hashes to it). A short or long key ID can be forged; a fingerprint cannot.
PGDG_KEY_FINGERPRINT = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"
PGDG_KEYRING = "/usr/share/keyrings/pgdg.gpg"
PGDG_SOURCE = "/etc/apt/sources.list.d/pgdg.list"


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


def _smoke_tool_lists() -> tuple[list[str], dict[str, list[str]]]:
    """The tools the smoke step always checks, and the ones each argument adds."""
    text = _shell_text(_smoke_step())
    always = re.search(r'\btools="([^"]+)"', text)
    assert always, "the smoke step must name its tools in a tools=\"...\" list"
    held: dict[str, list[str]] = {}
    for arg, added in re.findall(
        r'if \[ "\$\{([A-Z0-9_]+)\}" = "1" \]; then tools="\$tools ([^"]+)"; fi', text
    ):
        held.setdefault(arg, []).extend(added.split())
    assert held, "the smoke step must add the held tools when their build arguments are on"
    return always.group(1).split(), held


def _all_held(held: dict[str, list[str]]) -> list[str]:
    return [t for tools in held.values() for t in tools]


def test_the_smoke_step_names_every_required_tool() -> None:
    always, held = _smoke_tool_lists()
    missing = [t for t in REQUIRED_TOOLS if t not in always and t not in _all_held(held)]
    assert not missing, f"the build never checks {missing}"


def test_only_the_tools_without_a_clean_release_are_held() -> None:
    _, held = _smoke_tool_lists()
    moved = sorted(set(_all_held(held)) - MAY_BE_HELD)
    assert not moved, (
        f"{moved} moved behind a hold argument: a tool with a clean release goes in "
        "the default build"
    )
    args = _arg_defaults(_runtime_stage())
    for arg in held:
        gate = args.get(arg)
        assert gate is not None, f"{arg} must be a declared build argument"
        assert _arg_value(gate) in {"0", "1"}


def test_the_smoke_step_adds_each_held_tool_under_its_own_argument() -> None:
    always, held = _smoke_tool_lists()
    want = {
        arg: [tool]
        for tool, arg in HOLD_ARG_OF.items()
        if tool in MAY_BE_HELD and tool not in always
    }
    assert held == want, (
        f"the smoke step adds {held}; each held tool must be added alone, under its own "
        f"argument: {want}"
    )
    visited = 0
    for arg in want:
        gate = _arg_defaults(_runtime_stage()).get(arg)
        assert gate is not None, f"{arg} must be a declared build argument"
        visited += 1
    assert visited == len(want) == len(MAY_BE_HELD)


def _held_blocks(text: str, arg: str) -> list[str]:
    """The bodies of every `if [ "${arg}" = "1" ]; then ... fi` in `text`."""
    return re.findall(
        r'if \[ "\$\{' + re.escape(arg) + r'\}" = "1" \]; then(.*?)\bfi\b', text, flags=re.S
    )


def test_trivy_is_in_the_default_build_and_its_install_is_unconditional() -> None:
    always, held = _smoke_tool_lists()
    assert "trivy" in always, "trivy 0.75.0 scanned clean (#442): the default build checks it"
    assert "trivy" not in _all_held(held)
    installs = [
        _shell_text(i) for i in _runtime_stage()
        if i.keyword == "RUN" and "aquasecurity/trivy/releases/download" in _shell_text(i)
    ]
    assert len(installs) == 1, "trivy is downloaded by exactly one RUN"
    text = installs[0]
    assert "/usr/local/bin/trivy" in text
    for arg in HOLD_ARG_OF.values():
        for block in _held_blocks(text, arg):
            assert "trivy" not in block, f"trivy is still installed only behind {arg}"


def test_every_held_install_sits_behind_its_own_hold_argument() -> None:
    # The converse: tofu and tflint still have no clean release, so each one's
    # install must be inside its OWN guarded block, or the default build fails
    # the promote gate over it. Inside the other tool's block would tie the two
    # together again: neither could ship without the other.
    stage_text = "\n".join(_shell_text(i) for i in _runtime_stage() if i.keyword == "RUN")
    visited = 0
    for tool in sorted(MAY_BE_HELD):
        own = "\n".join(_held_blocks(stage_text, HOLD_ARG_OF[tool]))
        assert f"/usr/local/bin/{tool}" in own, (
            f"{tool} is not installed inside its own {HOLD_ARG_OF[tool]} block"
        )
        for other, arg in HOLD_ARG_OF.items():
            if other == tool:
                continue
            theirs = "\n".join(_held_blocks(stage_text, arg))
            assert f"/usr/local/bin/{tool}" not in theirs, (
                f"{tool} is installed inside {arg}, {other}'s hold"
            )
        visited += 1
    assert visited == len(MAY_BE_HELD) == 2


def test_no_default_tool_is_pinned_at_a_release_the_scan_failed() -> None:
    always, _ = _smoke_tool_lists()
    args = _arg_defaults(_runtime_stage())
    checked = 0
    for tool, var in PIN_OF.items():
        pinned = _arg_value(args[var])
        if tool in always:
            assert pinned not in SCANNED_FAILING[var], (
                f"{tool} {pinned} is in the default build, and the scan found a fixable HIGH in it"
            )
            checked += 1
    assert checked >= 1, "no default-build tool was checked against the scan record"


def test_each_hold_defaults_off_while_its_tool_has_no_clean_release() -> None:
    # Per tool: a hold whose tool is pinned at a release the scan failed must
    # default to 0. A tool pinned at a release not in SCANNED_FAILING may default
    # to 1 -- the other tool's record says nothing about it.
    args = _arg_defaults(_runtime_stage())
    visited = 0
    for tool in sorted(MAY_BE_HELD):
        arg = HOLD_ARG_OF[tool]
        assert arg in args, f"{arg} must be a declared build argument"
        default = _arg_value(args[arg])
        assert default in {"0", "1"}, f"{arg}={default}"
        pinned = _arg_value(args[PIN_OF[tool]])
        if pinned in SCANNED_FAILING[PIN_OF[tool]]:
            assert default == "0", (
                f"{arg} defaults on while {tool} {pinned} has a fixable HIGH in the scan"
            )
        visited += 1
    assert visited == len(MAY_BE_HELD)


def test_the_combined_hold_argument_is_gone_from_the_dockerfile_and_docs() -> None:
    files = [
        DOCKERFILE,
        VERSIONS_DOC,
        REPO / "docs" / "worker-images.md",
        REPO / "docs" / "BUILD_PROMPT_V2.md",
    ]
    visited = 0
    for path in files:
        assert COMBINED_HOLD_ARG not in path.read_text(), (
            f"{path.relative_to(REPO)} still names {COMBINED_HOLD_ARG}; the hold is "
            f"{sorted(HOLD_ARG_OF.values())}, one per tool"
        )
        visited += 1
    assert visited == len(files) == 4


def _fake_tools(bin_dir: Path, names: list[str], pg_major: str = POSTGRES_MAJOR) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = bin_dir / name
        # The PostgreSQL binaries print what the real ones do, e.g.
        # "postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+2)".
        if name in POSTGRES_TOOLS:
            line = f"{name} (PostgreSQL) {pg_major}.15 (fake)"
        else:
            line = f"{name} fake 1.0"
        path.write_text(f'#!/bin/sh\necho "{line}"\n')
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_smoke(
    tmp_path: Path, present: list[str], holds: dict[str, str], pg_major: str = POSTGRES_MAJOR
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    _fake_tools(bin_dir, present, pg_major)
    # The build arguments the step reads, at their Dockerfile defaults, then
    # the holds the case sets.
    values = {name: _arg_value(ins) for name, ins in _arg_defaults(_runtime_stage()).items()}
    script = _substitute_args(_shell_text(_smoke_step()), {**values, **holds})
    # The stand-ins first, then ONLY the plumbing the step itself uses, linked
    # into a private directory. Putting /usr/bin on PATH would let a runner's own
    # gh, kubectl, make or docker satisfy the check, and the missing-tool case
    # for each of them would have to be skipped (CI's runner carries several).
    plumbing = bin_dir.parent / "plumbing"
    plumbing.mkdir(exist_ok=True)
    for tool in ("mktemp", "rm", "head"):
        real = shutil.which(tool)
        assert real, f"{tool} is needed by the smoke step and was not found"
        link = plumbing / tool
        if not link.exists():
            link.symlink_to(real)
    env = {"PATH": os.pathsep.join([str(bin_dir), str(plumbing)]), "HOME": str(tmp_path)}
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


#: (INSTALL_TOFU, INSTALL_TFLINT): both off, each alone, both on. The two
#: one-on cases are the point of the split: either tool ships without the other.
HOLD_CASES = [("0", "0"), ("1", "0"), ("0", "1"), ("1", "1")]


def _holds(tofu: str, tflint: str) -> dict[str, str]:
    return {HOLD_ARG_OF["tofu"]: tofu, HOLD_ARG_OF["tflint"]: tflint}


def _expected_tools(holds: dict[str, str]) -> list[str]:
    always, held = _smoke_tool_lists()
    return always + [t for arg, tools in held.items() if holds.get(arg) == "1" for t in tools]


@pytest.mark.parametrize(("tofu", "tflint"), HOLD_CASES)
def test_the_smoke_step_passes_and_counts_when_every_tool_is_present(
    tmp_path: Path, tofu: str, tflint: str
) -> None:
    holds = _holds(tofu, tflint)
    expected = _expected_tools(holds)
    assert ("tofu" in expected) == (tofu == "1")
    assert ("tflint" in expected) == (tflint == "1")
    proc = _run_smoke(tmp_path, expected, holds)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"toolbox smoke: {len(expected)} tools ok" in proc.stdout, proc.stdout


@pytest.mark.parametrize(("tofu", "tflint"), HOLD_CASES)
def test_the_smoke_step_fails_the_build_when_any_tool_is_missing(
    tmp_path: Path, tofu: str, tflint: str
) -> None:
    holds = _holds(tofu, tflint)
    expected = _expected_tools(holds)
    visited = 0
    for absent in expected:
        case = tmp_path / absent
        proc = _run_smoke(case, [t for t in expected if t != absent], holds)
        assert proc.returncode != 0, f"build passed with {absent} missing:\n{proc.stdout}"
        assert absent in proc.stdout + proc.stderr, "the failure must name the missing tool"
        visited += 1
    assert visited == len(expected), f"only {visited} of {len(expected)} cases ran"


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
        if url == "https://www.postgresql.org/media/keys/ACCC4CF8.asc":
            # A signing key, not a release: it is verified by its full
            # fingerprint before apt trusts it, which
            # test_the_pgdg_key_is_pinned_by_its_full_fingerprint asserts.
            continue
        # A release file read from the build mirror (docs/runbooks/build-mirror.md)
        # comes from the bind-mounted tool mirror, and is checked exactly as a
        # download is.
        mirrored = url.startswith("file:///tmp/tool-mirror/")
        assert url.startswith("https://") or mirrored, f"{url} is neither HTTPS nor the tool mirror"
        if mirrored:
            assert text.startswith("--mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror "), (
                f"{url} is read from /tmp/tool-mirror by a RUN that does not bind-mount it"
            )
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


def test_the_only_apt_repository_is_pgdg_and_only_while_the_server_installs() -> None:
    # Vendor .debs are fetched by exact filename and checked like any other
    # download; a sources.list entry floats with whatever the repo serves. The
    # one exception is PGDG, for the PostgreSQL server (bookworm's own archive
    # carries 15, docs/design/test-services.md §4.1), where floating to the
    # newest 16.x on every APT_REFRESH build is the point. The entry and its
    # keyring are removed in the same RUN, so the derived images' own
    # `apt-get update` never reaches apt.postgresql.org.
    sources = [
        i for i in _runtime_stage()
        if "sources.list" in _shell_text(i) or "signed-by" in _shell_text(i)
    ]
    # MUTATION: add a second apt source anywhere in the runtime stage -- two entries here.
    assert len(sources) == 1, [s.body[:120] for s in sources]
    text = _shell_text(sources[0])
    assert "apt-get upgrade" in text, "the PGDG source belongs to the apt upgrade layer"
    entries = re.findall(r'echo "(deb [^"]*)"\s*>\s*(\S+)', text)
    assert entries == [(
        f"deb [signed-by={PGDG_KEYRING}] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main",
        PGDG_SOURCE,
    )], entries
    install = text.find('apt-get install --no-install-recommends -y "postgresql-${POSTGRES_MAJOR}"')
    removed = text.find(f"rm -f {PGDG_SOURCE} {PGDG_KEYRING}")
    # MUTATION: drop the `rm -f` of the source and keyring -- the derived images' apt reads PGDG.
    assert -1 not in (install, removed), text
    assert install < removed, "the PGDG source must outlive the server install, then go"
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


# --------------------------------------------------------------------------- #
# Python bytecode and jq: what a lane container pays on every start (#1334).
# --------------------------------------------------------------------------- #
#
# Measured by lane FIX-TESTS (PR 1415): the base image's stdlib ships no .pyc,
# and PYTHONDONTWRITEBYTECODE=1 stops one being written, so every Python start
# in an agent container recompiles re/json/pathlib (0.21 s per call against
# 0.08 s with a cache), and Debian's jq 1.6 takes 27 ms to start.

#: The release jq is pinned to, and the file of it the image installs.
JQ_VERSION = "1.7.1"
JQ_ASSET = "jq-linux-amd64"
TOOL_MIRROR_MANIFEST = REPO / "images" / "agent-runtime-base" / "tool-mirror.sha256"


def _runtime_env() -> Instruction:
    envs = [i for i in _runtime_stage() if i.keyword == "ENV" and "PYTHONDONTWRITEBYTECODE" in i.body]
    assert len(envs) == 1, "expected one runtime ENV setting PYTHONDONTWRITEBYTECODE"
    return envs[0]


def _compile_step() -> Instruction:
    runs = [i for i in _runtime_stage() if i.keyword == "RUN" and "compileall" in _shell_text(i)]
    assert len(runs) == 1, f"expected exactly one RUN that precompiles Python, found {len(runs)}"
    return runs[0]


def test_the_runtime_stdlib_and_venv_are_precompiled_by_the_interpreter_the_image_runs() -> None:
    stage = _runtime_stage()
    env = _runtime_env()
    # `python` at runtime is the first one on PATH: /opt/venv/bin/python, the
    # ENTRYPOINT's and every agent's. A .pyc carries its interpreter's magic
    # number, so compiling with any other Python would be a cache nobody reads.
    path = re.search(r'PATH="([^"]+)"', env.body)
    assert path and path.group(1).split(":")[0] == "/opt/venv/bin", env.body
    step = _compile_step()
    text = _shell_text(step)
    assert re.search(r"/opt/venv/bin/python3?\b[^&;|]*-m compileall", text) or (
        re.search(r'(\w+)=/opt/venv/bin/python3?\b', text)
        and re.search(r'"\$\{?(\w+)\}?"\s+-m compileall', text)
    ), f"compileall must run under /opt/venv/bin/python: {text}"
    assert " -q" in text, "compileall -q: a listing of every file is log noise"
    # The stdlib (with the base's own site-packages under it) and the venv's
    # site-packages, asked of the same interpreter rather than spelled out.
    assert 'sysconfig.get_path("stdlib")' in text or "sysconfig.get_path('stdlib')" in text, text
    assert 'sysconfig.get_path("purelib")' in text or "sysconfig.get_path('purelib')" in text, text
    assert not re.search(r"\|\|\s*(true|:)", text), "a failed compile must fail the build"
    # After everything that installs into either tree, so nothing lands uncompiled.
    venv_copy = next(i for i in stage if i.keyword == "COPY" and "/opt/venv" in i.body)
    base_pip = next(i for i in stage if i.keyword == "RUN" and "pip install" in _shell_text(i))
    assert step.index > venv_copy.index and step.index > base_pip.index
    late_pip = [
        i for i in stage if i.index > step.index and i.keyword == "RUN"
        and re.search(r"\bpip install\b", _shell_text(i))
    ]
    assert not late_pip, f"installed into Python after the precompile: {late_pip}"
    # And the build proves the cache is there, for the interpreter that runs.
    assert "cache_from_source" in text, "the precompile RUN must check a stdlib .pyc exists"


def test_runtime_bytecode_writes_stay_off_and_the_dockerfile_says_why() -> None:
    env = _runtime_env()
    assert re.search(r"\bPYTHONDONTWRITEBYTECODE=1\b", env.body), env.body
    why = " ".join(env.comments)
    assert "PYTHONDONTWRITEBYTECODE" in why and "compileall" in why, (
        "the comment above the runtime ENV must say why bytecode writes stay off "
        "while the build precompiles"
    )


def _apt_packages() -> list[str]:
    runs = [_shell_text(i) for i in _runtime_stage() if i.keyword == "RUN" and "apt-get install" in _shell_text(i)]
    assert len(runs) == 1, runs
    listed = runs[0].split("apt-get install", 1)[1].split("&&", 1)[0]
    return [w for w in listed.split() if not w.startswith("-")]


def _jq_install() -> Instruction:
    runs = [i for i in _runtime_stage() if i.keyword == "RUN" and "/jqlang/jq/" in _shell_text(i)]
    assert len(runs) == 1, "expected exactly one RUN that installs jq from its release"
    return runs[0]


def test_jq_is_the_pinned_release_not_debians() -> None:
    packages = _apt_packages()
    assert "curl" in packages, f"the apt list did not parse: {packages}"
    assert "jq" not in packages, "Debian bookworm's jq is 1.6; the image installs the pinned release"
    args = _arg_defaults(_runtime_stage())
    assert _arg_value(args["JQ_VERSION"]) == JQ_VERSION
    sha = _arg_value(args["JQ_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    text = _shell_text(_jq_install())
    fetch = re.search(
        r'-o\s+(\S+)\s+"file:///tmp/tool-mirror/github\.com/jqlang/jq/releases/download/'
        r'jq-\$\{JQ_VERSION\}/' + re.escape(JQ_ASSET) + '"',
        text,
    )
    assert fetch, f"jq must be read from the tool mirror at jq-${{JQ_VERSION}}/{JQ_ASSET}: {text}"
    out = fetch.group(1)
    check = text.find(f'echo "${{JQ_SHA256}}  {out}" | sha256sum -c -')
    install = text.find(f"install -o root -g root -m 0755 {out} /usr/local/bin/jq")
    assert -1 not in (check, install), text
    assert check < install, "jq is installed before its sha256 is checked"
    # The installed binary says the pinned version, or the build fails.
    assert 'test "$(/usr/local/bin/jq --version)" = "jq-${JQ_VERSION}"' in text, text
    # /usr/local/bin precedes /usr/bin on the runtime PATH, so the release is
    # the jq every caller gets.
    path = re.search(r'PATH="([^"]+)"', _runtime_env().body).group(1).split(":")
    assert path.index("/usr/local/bin") < path.index("/usr/bin"), path
    # The tool mirror carries it, at the same sha256 (test_build_mirror_switch
    # holds the whole manifest equal to the Dockerfile's reads).
    row = f"{sha}  github.com/jqlang/jq/releases/download/jq-{JQ_VERSION}/{JQ_ASSET}"
    assert row in TOOL_MIRROR_MANIFEST.read_text().splitlines()


# --------------------------------------------------------------------------- #
# PostgreSQL 16 for the declared test services (TS-IMAGE, part of #1627).
# --------------------------------------------------------------------------- #


def _apt_layer() -> Instruction:
    runs = [
        i for i in _runtime_stage() if i.keyword == "RUN" and "apt-get upgrade" in _shell_text(i)
    ]
    assert len(runs) == 1, "expected exactly one apt upgrade layer in the runtime stage"
    return runs[0]


def _flat(ins: Instruction) -> str:
    """A RUN body on one line, every run of whitespace one space."""
    return " ".join(_shell_text(ins).split())


def test_the_smoke_step_checks_the_postgres_binaries_at_the_pinned_major() -> None:
    always, _ = _smoke_tool_lists()
    # MUTATION: drop postgres or initdb from the smoke step's tools= list -- this fails.
    missing = [t for t in POSTGRES_TOOLS if t not in always]
    assert not missing, f"the build never checks {missing}"
    args = _arg_defaults(_runtime_stage())
    # MUTATION: set ARG POSTGRES_MAJOR=17 -- the image no longer carries the catalogue's 16.
    assert _arg_value(args["POSTGRES_MAJOR"]) == POSTGRES_MAJOR
    text = _shell_text(_smoke_step())
    assert '"(PostgreSQL) ${POSTGRES_MAJOR}."' in text, "the smoke step must check the major"


def test_the_smoke_step_fails_the_build_on_a_postgres_of_another_major(tmp_path: Path) -> None:
    holds = _holds("0", "0")
    expected = _expected_tools(holds)
    assert set(POSTGRES_TOOLS) <= set(expected), expected
    ok = _run_smoke(tmp_path / "ok", expected, holds)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "postgres: postgres (PostgreSQL) 16." in ok.stdout, ok.stdout
    # MUTATION: remove the `(PostgreSQL) ${POSTGRES_MAJOR}.` case from the smoke step -- 15 passes.
    wrong = _run_smoke(tmp_path / "wrong", expected, holds, pg_major="15")
    assert wrong.returncode != 0, f"the build passed with PostgreSQL 15:\n{wrong.stdout}"
    out = wrong.stdout + wrong.stderr
    named = [t for t in POSTGRES_TOOLS if f"{t} (not PostgreSQL {POSTGRES_MAJOR})" in out]
    assert named == list(POSTGRES_TOOLS), out


def test_the_pgdg_key_is_pinned_by_its_full_fingerprint() -> None:
    args = _arg_defaults(_runtime_stage())
    # MUTATION: change one hex digit of ARG PGDG_KEY_FINGERPRINT -- this fails (and so would the build).
    assert _arg_value(args["PGDG_KEY_FINGERPRINT"]) == PGDG_KEY_FINGERPRINT
    text = _flat(_apt_layer())
    steps = [
        'curl -fsSL --retry 5 --retry-all-errors --retry-connrefused -o /tmp/pgdg-key.asc '
        '"https://www.postgresql.org/media/keys/ACCC4CF8.asc"',
        'test "$(gpg --batch --with-colons --show-keys /tmp/pgdg-key.asc',
        '= "${PGDG_KEY_FINGERPRINT}"',
        f"gpg --batch --dearmor --output {PGDG_KEYRING} /tmp/pgdg-key.asc",
        f"[signed-by={PGDG_KEYRING}]",
        'apt-get install --no-install-recommends -y "postgresql-${POSTGRES_MAJOR}"',
    ]
    at = [text.find(step) for step in steps]
    # MUTATION: drop the fingerprint `test` -- whatever the URL serves becomes the keyring.
    assert -1 not in at, [s for s, a in zip(steps, at) if a == -1]
    # MUTATION: dearmor into the keyring before the fingerprint `test` -- this fails.
    assert at == sorted(at), "the key must be checked by fingerprint before it becomes apt's keyring"
    # Exactly one primary key, and it is the pinned one: the check prints the
    # fpr line that follows each `pub`, so a second key in the file (or none)
    # fails it. `--show-keys`, never `--import`: an import starts gpg-agent,
    # which `gpg` installed without its recommends does not carry.
    assert "gpg --batch --quiet --import" not in text and "--recv-keys" not in text, text
    assert re.search(r"""awk -F: '\$1 == "fpr" && prev == "pub" \{ print \$10 \} \{ prev = \$1 \}'""", text), text
    # MUTATION: add [trusted=yes] or --allow-unauthenticated -- the signature is no longer checked.
    for unsafe in ("apt-key", "trusted=yes", "--allow-unauthenticated", "--allow-insecure"):
        assert unsafe not in DOCKERFILE.read_text(), unsafe


def test_the_postgres_server_is_installed_in_the_apt_upgrade_layer() -> None:
    stage = _runtime_stage()
    server = [
        i for i in stage
        if i.keyword == "RUN" and re.search(r"postgresql-(\$\{POSTGRES_MAJOR\}|\d+\b)", _shell_text(i))
    ]
    # MUTATION: install the server in a RUN of its own -- APT_REFRESH no longer rebuilds it.
    assert [i.index for i in server] == [_apt_layer().index], [i.body[:80] for i in server]
    text = _flat(_apt_layer())
    assert text.startswith('echo "apt layer of ${APT_REFRESH}"'), text[:80]
    upgrade = text.find("apt-get upgrade")
    install = text.find('apt-get install --no-install-recommends -y "postgresql-${POSTGRES_MAJOR}"')
    assert -1 < upgrade < install, "the server installs after the upgrade, in the same layer"
    # The PGDG archive floats, so the build refuses one older than the release
    # this lane measured (16.15, 2026-10-24) rather than ship a stale mirror's.
    args = _arg_defaults(stage)
    minimum = _arg_value(args["POSTGRES_MIN_VERSION"])
    assert minimum.startswith(POSTGRES_MAJOR + "."), minimum
    # MUTATION: drop the dpkg --compare-versions check -- an older 16.x ships silently.
    assert (
        'dpkg --compare-versions "$(dpkg-query -W -f=\'${Version}\' "postgresql-${POSTGRES_MAJOR}")" '
        'ge "${POSTGRES_MIN_VERSION}"'
    ) in text, text
    for tool in POSTGRES_TOOLS:
        assert tool in text, f"{tool} is not linked onto the PATH"
    assert 'ln -s "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/$t" "/usr/local/bin/$t"' in text, text


def test_the_build_creates_no_cluster_and_starts_no_server() -> None:
    text = _flat(_apt_layer())
    common = text.find("apt-get install --no-install-recommends -y postgresql-common")
    no_cluster = text.find(
        "sed -ri 's/^#?create_main_cluster = .*/create_main_cluster = false/' "
        "/etc/postgresql-common/createcluster.conf"
    )
    held = text.find("grep -qx 'create_main_cluster = false' /etc/postgresql-common/createcluster.conf")
    server = text.find('apt-get install --no-install-recommends -y "postgresql-${POSTGRES_MAJOR}"')
    # MUTATION: drop the create_main_cluster sed -- the server's postinst runs initdb into the image.
    assert -1 not in (common, no_cluster, held, server), text
    assert common < no_cluster < held < server, "the cluster must be refused before the server installs"
    # MUTATION: drop either `test ! -e` -- a data directory baked into the image goes unnoticed.
    assert 'test ! -e "/var/lib/postgresql/${POSTGRES_MAJOR}"' in text[server:], text
    assert 'test ! -e "/etc/postgresql/${POSTGRES_MAJOR}"' in text[server:], text
    # Nothing in the build runs a server or creates a cluster: the worker does
    # that at runtime, as its own child (docs/design/test-services.md §4.1).
    starts = re.compile(r"pg_ctl\s+(start|init)|pg_ctlcluster|pg_createcluster|\binitdb\s+-|postgres\s+-D")
    for ins in _runtime_stage():
        if ins.keyword == "RUN":
            assert not starts.search(_shell_text(ins)), _shell_text(ins)[:120]
    # The snakeoil pair ssl-cert generates at install is the same in every
    # pull; the loopback-only service uses no TLS, so neither ships.
    assert "rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem" in text
