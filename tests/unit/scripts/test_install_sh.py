"""`install/install.sh` leaves `swarm` callable and ends with plain `swarm
install` (DAYONE-INSTALL-SH; part of #2688).

* When uv's tool bin directory is not on the person's PATH, one line is added
  to their shell's rc file (zsh: ~/.zshrc; bash: ~/.bashrc, or ~/.bash_profile
  on macOS), written once however often the script runs, and the script says
  which file changed.
* SWARM_CHANNEL picks `channels/<channel>`: `stable` by default, `rc` allowed,
  anything else refused before anything is fetched (decision 5).
* The last line printed is exactly `Next: swarm install` when this terminal
  finds `swarm`; when it does not (no writable directory of the person's own on
  PATH: macOS zsh's default), it names the path installed, `Next:
  ~/.local/bin/swarm install`, which works in the same terminal (audit 3, A9).

No network: a fake `curl` serves the release home from a directory, a fake
`uv` records its calls and puts `swarm` and `sc` where uv would, and HOME is a
temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "install" / "install.sh"
HOME_URL = "storage.googleapis.com/swarmcloud"
STABLE = "2026.10.1"
RC = "2026.11.1-rc.2"
DISTS = ("swarm_mcp", "swarm_install", "swarm_common", "swarm_redaction")
NEXT = "Next: swarm install"
#: The last line when this terminal's PATH does not find swarm: uv's default
#: tool bin directory, under HOME, named so the same terminal can type it.
NEXT_BY_PATH = "Next: ~/.local/bin/swarm install"

FAKE_CURL = r'''#!{python}
"""A fake curl: serves https://<host>/<path> from FAKE_ROOT/<host>/<path>."""
import os, sys

args = sys.argv[1:]
out = args[args.index("-o") + 1] if "-o" in args else None
url = args[-1]
with open(os.environ["FAKE_CURL_LOG"], "a") as fh:
    fh.write(url + "\n")
path = os.path.join(os.environ["FAKE_ROOT"], url.split("://", 1)[1])
if not os.path.isfile(path):
    sys.exit(22)
data = open(path, "rb").read()
if out:
    open(out, "wb").write(data)
else:
    sys.stdout.buffer.write(data)
'''

# uv's `tool install` puts the tool's entry points in its tool bin directory:
# $UV_TOOL_BIN_DIR, else ~/.local/bin.
FAKE_UV = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_UV_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] == ["tool", "install"]:
    bindir = os.environ.get("UV_TOOL_BIN_DIR") or os.path.join(os.environ["HOME"], ".local", "bin")
    os.makedirs(bindir, exist_ok=True)
    for name in ("swarm", "sc"):
        path = os.path.join(bindir, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\necho " + name + "\n")
        os.chmod(path, 0o755)
'''

FAKE_UNAME = r'''#!/bin/sh
case "$1" in
  -s) echo "${FAKE_UNAME_S}" ;;
  -m) echo arm64 ;;
  *) echo "${FAKE_UNAME_S}" ;;
esac
'''


def _publish(root: Path, version: str) -> None:
    wheel_version = {STABLE: "2026.10.1", RC: "2026.11.1rc2"}[version]
    release = root / HOME_URL / "releases" / version
    release.mkdir(parents=True)
    sums = []
    for dist in DISTS:
        wheel = f"{dist}-{wheel_version}-py3-none-any.whl"
        data = secrets.token_bytes(32)
        (release / wheel).write_bytes(data)
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {wheel}")
    (release / "SHA256SUMS").write_text("\n".join(sums) + "\n")


def _sandbox(tmp_path: Path, *, shell: str = "/bin/zsh", system: str = "Linux") -> dict[str, str]:
    root = tmp_path / "web"
    channels = root / HOME_URL / "channels"
    channels.mkdir(parents=True)
    (channels / "stable").write_text(STABLE + "\n")
    (channels / "rc").write_text(RC + "\n")
    _publish(root, STABLE)
    _publish(root, RC)

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, text in (("curl", FAKE_CURL), ("uv", FAKE_UV), ("uname", FAKE_UNAME)):
        (bindir / name).write_text(text.replace("{python}", sys.executable))
        (bindir / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    for log in ("curl.log", "uv.log"):
        (tmp_path / log).write_text("")
    return {"PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home), "SHELL": shell,
            "TMPDIR": str(tmp_path), "FAKE_ROOT": str(root), "FAKE_UNAME_S": system,
            "FAKE_CURL_LOG": str(tmp_path / "curl.log"), "FAKE_UV_LOG": str(tmp_path / "uv.log")}


def _run(tmp_path: Path, env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], list[str], list[list[str]]]:
    (tmp_path / "curl.log").write_text("")
    (tmp_path / "uv.log").write_text("")
    proc = subprocess.run(["sh", str(INSTALL_SH)], env=env, capture_output=True, text=True,
                          timeout=120, check=False)
    urls = (tmp_path / "curl.log").read_text().split()
    calls = [json.loads(line) for line in (tmp_path / "uv.log").read_text().splitlines() if line.strip()]
    return proc, urls, calls


def _last_line(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _path_lines(rc: Path, bindir: Path) -> list[str]:
    if not rc.exists():
        return []
    return [line for line in rc.read_text().splitlines() if str(bindir) in line and "PATH" in line]


@pytest.mark.parametrize(("shell", "system", "rc_name"), [
    ("/bin/zsh", "Linux", ".zshrc"),
    ("/usr/local/bin/zsh", "Darwin", ".zshrc"),
    ("/bin/bash", "Linux", ".bashrc"),
    ("/bin/bash", "Darwin", ".bash_profile"),
])
def test_tool_bin_not_on_path_is_added_to_the_shell_rc_file(tmp_path: Path, shell: str, system: str,
                                                            rc_name: str) -> None:
    env = _sandbox(tmp_path, shell=shell, system=system)
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    home = Path(env["HOME"])
    rc = home / rc_name
    bindir = home / ".local" / "bin"
    assert len(_path_lines(rc, bindir)) == 1, rc.read_text() if rc.exists() else "no rc file"
    assert str(rc) in proc.stderr, "the script says which file it changed"
    others = {".zshrc", ".bashrc", ".bash_profile", ".profile"} - {rc_name}
    assert not [name for name in others if (home / name).exists()], "only the person's own shell's file"
    # The line it wrote puts the directory on PATH for that shell.
    if shutil.which("bash") and rc_name != ".zshrc":
        probe = subprocess.run(["bash", "-c", f'. "{rc}"; command -v swarm'],
                               env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
                               capture_output=True, text=True, check=False)
        assert probe.stdout.strip() == str(bindir / "swarm"), probe.stderr


def test_rc_line_is_written_once_however_often_it_runs(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, shell="/bin/bash")
    rc = Path(env["HOME"]) / ".bashrc"
    rc.write_text("# the person's own settings\nalias ll='ls -l'\n")
    for _ in range(3):
        proc, _, _ = _run(tmp_path, env)
        assert proc.returncode == 0, proc.stderr
        assert _last_line(proc) == NEXT_BY_PATH
    text = rc.read_text()
    assert text.startswith("# the person's own settings\nalias ll='ls -l'\n"), "the person's lines are kept"
    assert len(_path_lines(rc, Path(env["HOME"]) / ".local" / "bin")) == 1, text


def test_tool_bin_already_on_path_changes_no_file(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, shell="/bin/zsh")
    bindir = Path(env["HOME"]) / ".local" / "bin"
    env["PATH"] = f"{env['PATH']}{os.pathsep}{bindir}"
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".zshrc").exists()
    assert _last_line(proc) == NEXT


def test_uv_tool_bin_dir_is_the_directory_put_on_path(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, shell="/bin/zsh")
    custom = tmp_path / "tools-bin"
    env["UV_TOOL_BIN_DIR"] = str(custom)
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    rc = Path(env["HOME"]) / ".zshrc"
    assert len(_path_lines(rc, custom)) == 1, rc.read_text() if rc.exists() else "no rc file"


def test_a_writable_home_directory_on_path_gets_swarm_for_this_terminal(tmp_path: Path) -> None:
    """`curl | sh` cannot change the running shell's PATH, so when the person's
    PATH already has a directory of their own, swarm and sc are linked there:
    the very next command in this terminal finds them."""
    env = _sandbox(tmp_path, shell="/bin/zsh")
    own = Path(env["HOME"]) / "bin"
    own.mkdir()
    env["PATH"] = f"{env['PATH']}{os.pathsep}{own}"
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    bindir = Path(env["HOME"]) / ".local" / "bin"
    for name in ("swarm", "sc"):
        assert (own / name).is_symlink(), name
        assert os.path.realpath(own / name) == os.path.realpath(bindir / name)
    assert _last_line(proc) == NEXT


def test_default_channel_is_stable(tmp_path: Path) -> None:
    env = _sandbox(tmp_path)
    proc, urls, calls = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert f"https://{HOME_URL}/channels/stable" in urls
    assert f"https://{HOME_URL}/channels/rc" not in urls
    (call,) = calls
    assert any("swarm_mcp-2026.10.1-py3-none-any.whl" in arg for arg in call)


def test_swarm_channel_rc_installs_the_release_candidate(tmp_path: Path) -> None:
    env = _sandbox(tmp_path)
    env["SWARM_CHANNEL"] = "rc"
    proc, urls, calls = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert f"https://{HOME_URL}/channels/rc" in urls
    assert f"https://{HOME_URL}/channels/stable" not in urls
    assert f"https://{HOME_URL}/releases/{RC}/SHA256SUMS" in urls
    (call,) = calls
    assert any("swarm_mcp-2026.11.1rc2-py3-none-any.whl" in arg for arg in call)
    assert _last_line(proc) == NEXT_BY_PATH


@pytest.mark.parametrize("channel", ["beta", "Stable", "../stable", "rc/x", ""])
def test_any_other_channel_is_refused_before_anything_is_fetched(tmp_path: Path, channel: str) -> None:
    env = _sandbox(tmp_path)
    env["SWARM_CHANNEL"] = channel
    proc, urls, calls = _run(tmp_path, env)
    if channel == "":
        # An empty value is the same as unset: the default channel.
        assert proc.returncode == 0, proc.stderr
        assert f"https://{HOME_URL}/channels/stable" in urls
        return
    assert proc.returncode == 1
    assert "SWARM_CHANNEL" in proc.stderr and "stable" in proc.stderr and "rc" in proc.stderr
    assert urls == [] and calls == []


def test_a_wheel_that_does_not_match_still_installs_nothing(tmp_path: Path) -> None:
    env = _sandbox(tmp_path)
    env["SWARM_CHANNEL"] = "rc"
    wheel = Path(env["FAKE_ROOT"]) / HOME_URL / "releases" / RC / "swarm_install-2026.11.1rc2-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"\0")
    proc, _, calls = _run(tmp_path, env)
    assert proc.returncode == 1
    assert "does not match SHA256SUMS" in proc.stderr
    assert calls == []
    assert not (Path(env["HOME"]) / ".zshrc").exists()


def test_last_line_is_exactly_next_swarm_install(tmp_path: Path) -> None:
    env = _sandbox(tmp_path)
    env["PATH"] = f"{env['PATH']}{os.pathsep}{Path(env['HOME']) / '.local' / 'bin'}"
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert _last_line(proc) == NEXT
    assert "--project" not in proc.stderr and "<" not in proc.stderr, "no flags, no placeholders"


def _runs_in_this_terminal(env: dict[str, str], line: str) -> str:
    """What the person's own shell finds for the command `line` names, with
    the PATH they typed `curl | sh` in (the rc file is not read again)."""
    command = line.removeprefix("Next: ").removesuffix(" install")
    probe = subprocess.run(["sh", "-c", f"command -v {command}"],
                           env={"PATH": env["PATH"], "HOME": env["HOME"]},
                           capture_output=True, text=True, check=False)
    return probe.stdout.strip()


@pytest.mark.parametrize(("shell", "system"), [("/bin/zsh", "Darwin"), ("/bin/bash", "Linux")])
def test_swarm_not_on_this_terminals_path_names_the_installed_path(tmp_path: Path, shell: str,
                                                                    system: str) -> None:
    """With no writable directory of the person's own on PATH (macOS zsh's
    default), `swarm install` is `command not found` in this terminal: the
    last line names the path installed, which this terminal runs (A9)."""
    env = _sandbox(tmp_path, shell=shell, system=system)
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    # MUTATION: print plain `Next: swarm install` here and this fails.
    assert _last_line(proc) == NEXT_BY_PATH, proc.stderr
    bindir = Path(env["HOME"]) / ".local" / "bin"
    assert _runs_in_this_terminal(env, _last_line(proc)) == str(bindir / "swarm")


def test_a_tool_bin_outside_home_is_named_absolutely(tmp_path: Path) -> None:
    env = _sandbox(tmp_path)
    custom = tmp_path / "tools-bin"
    env["UV_TOOL_BIN_DIR"] = str(custom)
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert _last_line(proc) == f"Next: {custom / 'swarm'} install", proc.stderr


def test_a_tool_bin_with_a_space_is_quoted(tmp_path: Path) -> None:
    env = _sandbox(tmp_path)
    custom = tmp_path / "my tools"
    env["UV_TOOL_BIN_DIR"] = str(custom)
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    line = _last_line(proc)
    assert line == f"Next: '{custom / 'swarm'}' install", proc.stderr
    probe = subprocess.run(["sh", "-c", f"command -v {line.removeprefix('Next: ').removesuffix(' install')}"],
                           env={"PATH": env["PATH"]}, capture_output=True, text=True, check=False)
    assert probe.stdout.strip() == str(custom / "swarm")


def test_a_linked_swarm_is_plain_swarm_install(tmp_path: Path) -> None:
    """Linked into a directory this terminal's PATH holds, plain `swarm` works."""
    env = _sandbox(tmp_path)
    own = Path(env["HOME"]) / "bin"
    own.mkdir()
    env["PATH"] = f"{env['PATH']}{os.pathsep}{own}"
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert _last_line(proc) == NEXT


def test_a_swarm_that_is_not_ours_in_the_own_directory_is_not_trusted(tmp_path: Path) -> None:
    """The person's own directory already holds a `swarm` that is not a link:
    it is left alone, so plain `swarm` would run that one; the line names ours."""
    env = _sandbox(tmp_path)
    own = Path(env["HOME"]) / "bin"
    own.mkdir()
    (own / "swarm").write_text("#!/bin/sh\necho another swarm\n")
    (own / "swarm").chmod(0o755)
    env["PATH"] = f"{env['PATH']}{os.pathsep}{own}"
    proc, _, _ = _run(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert _last_line(proc) == NEXT_BY_PATH, proc.stderr
    assert (own / "swarm").read_text() == "#!/bin/sh\necho another swarm\n"
