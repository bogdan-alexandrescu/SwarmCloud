"""The CodeRabbit CLI in the agent image, and its key (CODERABBIT-IMAGE, part of #2442).

Owner decision 2026-10-26: the claude-code runner image carries the
`coderabbit` CLI so a lane can run the local review its repository asks for.
Its API key is a workspace secret the workspace admin stores; it is never in
the image, the repository, a Job environment or a log.

What each case pins:

  * the Dockerfile fetches the CLI at an exact version over HTTPS and checks
    the archive against a sha256 written beside the pin before unzipping it;
    the binary is root-owned outside the agent's reach, and the build's smoke
    step runs `coderabbit --version` as the agent user;
  * the launcher on PATH hands the CLI `--api-key` ONLY when the worker
    released the secret `coderabbit-api-key` in slot `files` to this step,
    and only on the commands that take one; with no such secret the CLI runs
    keyless and nothing fails;
  * the key never reaches the CLI's environment, the launcher's output or a
    file it writes, and the worker's logger -- whose registered set scrubs
    every uploaded artifact -- knows the value from the moment it is released;
  * the docs name the same secret and slot the launcher reads.

Every fake key is built at runtime (CLAUDE.md: nothing added may look like a
credential).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker import secretrelease as release_mod
from agent_worker.logs import StructuredLogger
from swarm_common.profiles import RUNNER_PROFILES

REPO = Path(__file__).resolve().parents[3]
IMAGE_DIR = REPO / "images" / "agent-runtime-base"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
LAUNCHER = IMAGE_DIR / "coderabbit"
DOC = REPO / "docs" / "worker-images.md"
VERSIONS_DOC = REPO / "docs" / "versions.md"

#: The pinned release. Its sha256 is the vendor's SHA256SUMS entry for
#: coderabbit-linux-x64.zip, whose signature verified against the release key
#: in https://cli.coderabbit.ai/install.sh on 2026-10-26.
VERSION = "0.9.1"
SHA256 = "9558e02f1b6570ade33d8191b33e10ffae2472ab2680d4518d03261bcebc80bc"


def fake_key() -> str:
    return "cr" + "-" + secrets.token_hex(20)


def _launcher() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("coderabbit_launcher", str(LAUNCHER))
    spec = importlib.util.spec_from_loader("coderabbit_launcher", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _dockerfile_code() -> str:
    """The Dockerfile without comments, continuation lines joined."""
    lines = [ln for ln in DOCKERFILE.read_text().splitlines() if not ln.lstrip().startswith("#")]
    return re.sub(r"\\\n", " ", "\n".join(lines))


def _arg(name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)$", _dockerfile_code(), flags=re.M)
    assert m, f"no ARG {name} in {DOCKERFILE.relative_to(REPO)}"
    return m.group(1)


def _install_run() -> str:
    runs = [r for r in re.findall(r"^RUN (.*)$", _dockerfile_code(), flags=re.M)
            if "cli.coderabbit.ai" in r]
    assert len(runs) == 1, f"expected one RUN that installs the CodeRabbit CLI, found {len(runs)}"
    return runs[0]


# --------------------------------------------------------------------------- #
# The image definition.
# --------------------------------------------------------------------------- #


def test_the_image_pins_the_cli_version_and_its_checksum() -> None:
    assert _arg("CODERABBIT_VERSION") == VERSION
    assert _arg("CODERABBIT_SHA256") == SHA256


def test_the_archive_is_fetched_over_https_and_verified_before_it_is_unzipped() -> None:
    run = _install_run()
    fetch = run.index('"https://cli.coderabbit.ai/releases/${CODERABBIT_VERSION}/coderabbit-linux-x64.zip"')
    check = run.index('echo "${CODERABBIT_SHA256}  /tmp/toolbox/coderabbit.zip" | sha256sum -c -')
    unzip = run.index("unzip")
    assert fetch < check < unzip
    assert "--retry-all-errors" in run
    # The build refuses a binary that is not the pinned release.
    assert '= "${CODERABBIT_VERSION}"' in run


def test_the_binary_is_root_owned_and_the_launcher_is_what_path_finds() -> None:
    run = _install_run()
    assert "install -o root -g root -m 0755 /tmp/toolbox/coderabbit /opt/coderabbit/coderabbit" in run
    code = _dockerfile_code()
    assert "COPY --chmod=0555 images/agent-runtime-base/coderabbit /usr/local/bin/coderabbit" in code
    assert _launcher().REAL == "/opt/coderabbit/coderabbit"
    # Python with -I, from the image's own interpreter, not whatever PATH says.
    assert LAUNCHER.read_text().splitlines()[0] == "#!/usr/local/bin/python3 -I"


def test_the_smoke_step_runs_the_cli_as_the_agent_user() -> None:
    smoke = re.search(r'\btools="([^"]+)"', _dockerfile_code())
    assert smoke, "the toolbox smoke step names its tools in tools=\"...\""
    assert "coderabbit" in smoke.group(1).split()


def test_versions_doc_lists_the_cli_at_the_pinned_version() -> None:
    body = VERSIONS_DOC.read_text().split("### Agent image toolbox", 1)[1].split("\n## ", 1)[0]
    row = re.search(r"^\|\s*`coderabbit`\s*\|\s*\*\*([^*]+)\*\*", body, flags=re.M)
    assert row and row.group(1).strip() == VERSION


def test_the_docs_name_the_secret_and_slot_the_launcher_reads() -> None:
    launcher = _launcher()
    section = DOC.read_text().split("## CodeRabbit", 1)
    assert len(section) == 2, "docs/worker-images.md needs a '## CodeRabbit' section"
    body = section[1].split("\n## ", 1)[0]
    assert f"`{launcher.SECRET_NAME}`" in body
    assert f"`{launcher.SLOT}`" in body
    assert "coderabbit review" in body
    assert "scripts/create-secrets.sh --stdin" in body
    assert launcher.MANIFEST_ENV == release_mod.MANIFEST_ENV


# --------------------------------------------------------------------------- #
# The launcher: the key only when released, only where the CLI takes it.
# --------------------------------------------------------------------------- #


def _manifest(tmp_path: Path, entries: list[tuple[str, str, str]]) -> Path:
    """A worker-shaped manifest: `(name, slot, value)` each a 0600 file."""
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    listed = []
    for name, slot, value in entries:
        (root / slot).mkdir(mode=0o700, exist_ok=True)
        path = root / slot / name
        path.write_text(value)
        path.chmod(0o600)
        listed.append({"name": name, "slot": slot, "kind": "api_key", "path": str(path)})
    target = root / "manifest.json"
    target.write_text(json.dumps({"secrets": listed}))
    return target


class _Exec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def __call__(self, path: str, argv: list[str], env: dict[str, str]) -> None:
        self.calls.append((path, argv, env))


def _run(args: list[str], environ: dict[str, str], capsys: Any) -> tuple[list[str], dict[str, str], str]:
    launcher = _launcher()
    exe = _Exec()
    launcher.main(["coderabbit", *args], environ, execve=exe)
    assert len(exe.calls) == 1
    path, argv, env = exe.calls[0]
    assert path == launcher.REAL and argv[0] == launcher.REAL
    return argv[1:], env, capsys.readouterr().err


def test_a_released_key_is_passed_to_review_as_its_api_key(tmp_path, capsys) -> None:
    key = fake_key()
    manifest = _manifest(tmp_path, [("other-secret", "files", fake_key()),
                                    ("coderabbit-api-key", "files", key + "\n")])
    argv, env, err = _run(["review", "--agent"], {"SWARM_SECRETS_MANIFEST": str(manifest)}, capsys)
    assert argv == ["review", "--agent", "--api-key", key]
    assert key not in err
    assert all(key not in v for v in env.values())


def test_the_default_mode_is_a_review_and_takes_the_key(tmp_path, capsys) -> None:
    key = fake_key()
    manifest = _manifest(tmp_path, [("coderabbit-api-key", "files", key)])
    environ = {"SWARM_SECRETS_MANIFEST": str(manifest)}
    assert _run([], environ, capsys)[0] == ["--api-key", key]
    assert _run(["--agent"], environ, capsys)[0] == ["--agent", "--api-key", key]
    assert _run(["review", "findings"], environ, capsys)[0] == ["review", "findings", "--api-key", key]
    assert _run(["review", "--dir", "src", "--", "x"], environ, capsys)[0] == [
        "review", "--dir", "src", "--api-key", key, "--", "x"]


@pytest.mark.parametrize("args", [
    ["--version"], ["-v"], ["--help"], ["review", "--help"], ["auth", "login"], ["stats"],
    ["help", "review"], ["doctor"],
])
def test_commands_that_take_no_key_are_given_none(tmp_path, capsys, args) -> None:
    key = fake_key()
    manifest = _manifest(tmp_path, [("coderabbit-api-key", "files", key)])
    argv, _, err = _run(args, {"SWARM_SECRETS_MANIFEST": str(manifest)}, capsys)
    assert argv == args
    assert key not in err


def test_a_key_the_caller_passed_is_left_alone(tmp_path, capsys) -> None:
    mine, released = fake_key(), fake_key()
    manifest = _manifest(tmp_path, [("coderabbit-api-key", "files", released)])
    environ = {"SWARM_SECRETS_MANIFEST": str(manifest)}
    assert _run(["review", "--api-key", mine], environ, capsys)[0] == ["review", "--api-key", mine]
    assert _run(["review", f"--api-key={mine}"], environ, capsys)[0] == ["review", f"--api-key={mine}"]


@pytest.mark.parametrize("case", ["no_manifest_env", "missing_file", "not_json", "other_name",
                                  "other_slot", "empty_value", "unreadable_path"])
def test_a_step_without_the_secret_runs_the_cli_keyless_and_says_so(tmp_path, capsys, case) -> None:
    environ: dict[str, str] = {}
    if case == "missing_file":
        environ["SWARM_SECRETS_MANIFEST"] = str(tmp_path / "absent.json")
    elif case == "not_json":
        bad = tmp_path / "manifest.json"
        bad.write_text("{not json")
        environ["SWARM_SECRETS_MANIFEST"] = str(bad)
    elif case == "other_name":
        environ["SWARM_SECRETS_MANIFEST"] = str(_manifest(tmp_path, [("coderabbit", "files", fake_key())]))
    elif case == "other_slot":
        environ["SWARM_SECRETS_MANIFEST"] = str(
            _manifest(tmp_path, [("coderabbit-api-key", "typesafe", fake_key())]))
    elif case == "empty_value":
        environ["SWARM_SECRETS_MANIFEST"] = str(_manifest(tmp_path, [("coderabbit-api-key", "files", "\n")]))
    elif case == "unreadable_path":
        manifest = _manifest(tmp_path, [("coderabbit-api-key", "files", fake_key())])
        data = json.loads(manifest.read_text())
        data["secrets"][0]["path"] = str(tmp_path / "gone")
        manifest.write_text(json.dumps(data))
        environ["SWARM_SECRETS_MANIFEST"] = str(manifest)
    argv, _, err = _run(["review", "--agent"], environ, capsys)
    assert argv == ["review", "--agent"]
    assert "--api-key" not in argv
    assert "coderabbit-api-key" in err and "files" in err


def test_the_cli_environment_carries_no_manifest_and_switches_off_updates(tmp_path, capsys) -> None:
    manifest = _manifest(tmp_path, [("coderabbit-api-key", "files", fake_key())])
    _, env, _ = _run(["review"], {"SWARM_SECRETS_MANIFEST": str(manifest), "HOME": "/w"}, capsys)
    assert "SWARM_SECRETS_MANIFEST" not in env
    assert env["HOME"] == "/w"
    assert env["CODERABBIT_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert env["CR_CLI_TELEMETRY_DISABLED"] == "true"


FAKE_CLI = r'''
import json, os, sys
with open(os.environ["FAKE_CLI_RECORD"], "w") as handle:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, handle)
'''


def test_the_launcher_execs_the_vendor_binary(tmp_path) -> None:
    """The real `os.execve`, in a child: the CLI is started with the key in its
    argv and nowhere in its environment."""
    key = fake_key()
    manifest = _manifest(tmp_path, [("coderabbit-api-key", "files", key)])
    fake = tmp_path / "vendor-coderabbit"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
    fake.chmod(0o755)
    record = tmp_path / "record.json"
    driver = (
        "import importlib.machinery, importlib.util, os, sys\n"
        f"loader = importlib.machinery.SourceFileLoader('cr', {str(LAUNCHER)!r})\n"
        "spec = importlib.util.spec_from_loader('cr', loader)\n"
        "m = importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
        f"m.REAL = {str(fake)!r}\n"
        "m.main(['coderabbit', 'review', '--agent'], os.environ)\n"
    )
    env = {**os.environ, "SWARM_SECRETS_MANIFEST": str(manifest), "FAKE_CLI_RECORD": str(record)}
    proc = subprocess.run([sys.executable, "-c", driver], env=env, capture_output=True,
                          text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(record.read_text())
    assert seen["argv"] == ["review", "--agent", "--api-key", key]
    assert all(key not in v for v in seen["env"].values())
    assert "SWARM_SECRETS_MANIFEST" not in seen["env"]
    assert key not in proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# The worker: released only when granted, and redacted from the moment it is.
# --------------------------------------------------------------------------- #


class _Routes:
    """swarm-api's child-key and release routes, answering `items`."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    def call(self, method, path, *, body=None, headers=None):
        if path.endswith("/secrets:release"):
            return 200, {"secrets": self.items, "withheld": []}
        return 200, {}


def _release(tmp_path: Path, items: list[dict[str, Any]], profile: str = "claude-code"
             ) -> tuple[release_mod.Released, StructuredLogger]:
    log = StructuredLogger(stream=io.StringIO())
    children = type("P", (), {"_key": children_mod.AttemptKey(), "_api": _Routes(items),
                              "keyed": True})()
    cfg = type("C", (), {"attempt_id": "att_cr", "generation": 2})()
    released = release_mod.SecretRelease(cfg, children=children, logger=log).release(
        profile=RUNNER_PROFILES[profile], inputs={}, private_dir=tmp_path)
    return released, log


def _item(name: str, value: str) -> dict[str, Any]:
    return {"name": name, "slot": "files", "kind": "api_key", "value": value,
            "version": "projects/p/secrets/s/versions/1"}


@pytest.mark.parametrize("profile", ["claude-code", "codex", "claude-code-browser"])
def test_a_granted_key_reaches_the_cli_and_is_redacted(tmp_path, capsys, profile) -> None:
    key = fake_key()
    released, log = _release(tmp_path, [_item("coderabbit-api-key", key)], profile)
    assert released.delivered == [("coderabbit-api-key", "files")]
    # The runner's environment holds the manifest's path, never the value.
    assert set(released.env) == {release_mod.MANIFEST_ENV}
    assert all(key not in v for v in released.env.values())
    path = tmp_path / "secrets" / "files" / "coderabbit-api-key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    argv, env, _ = _run(["review", "--agent"], dict(released.env), capsys)
    assert argv[-2:] == ["--api-key", key]

    # The worker's logger knows the value, so a transcript that echoed the
    # CLI's command line is scrubbed before it is uploaded.
    assert key not in log.scrub_text(f"ran {' '.join(argv)}")
    transcript = tmp_path / "stdout.jsonl"
    transcript.write_text(json.dumps({"tool_result": f"ps: coderabbit {' '.join(argv)}"}) + "\n")
    log.scrub_file(transcript)
    assert key not in transcript.read_text()

    release_mod.remove(tmp_path)
    assert not path.exists()


def test_a_workspace_without_the_secret_gives_the_cli_no_key(tmp_path, capsys) -> None:
    released, _ = _release(tmp_path, [_item("some-other-key", fake_key())])
    argv, _, err = _run(["review"], dict(released.env), capsys)
    assert argv == ["review"]
    assert "coderabbit-api-key" in err
    released, _ = _release(tmp_path / "none", [])
    assert released.env == {}
    assert _run(["review"], dict(released.env), capsys)[0] == ["review"]
