"""The speech machine's files agree with each other and with the server
(docs/design/voice-it-own-speech.md §4.5, §5.1-§5.3; apps/swarm-speech/vm/).

The machine lane installs vm/files.json's files on the machine; these hold
that every one exists, that the systemd units run what files.json installs,
that the launcher passes the container exactly the variables the server reads
and mounts the directory the server and the watchdog use, and that the image
runs as the uid the launcher gives that directory to.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import yaml

import speech_fakes  # noqa: F401 -- puts apps/swarm-speech on sys.path
from swarm_speech import activity, main, watchdog

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "swarm-speech"
VM = APP / "vm"
LAUNCHER = VM / "swarm-speech-launch.sh"
FILES = json.loads((VM / "files.json").read_text(encoding="utf-8"))


def _dest(src: str) -> str:
    return next(f["dest"] for f in FILES["files"] if f["src"] == src)


def _exec_start(unit: str) -> str:
    return re.search(r"^ExecStart=(.+)$", (VM / unit).read_text(encoding="utf-8"), re.M).group(1)


def test_every_file_the_machine_installs_is_in_the_tree():
    srcs = [f["src"] for f in FILES["files"]]
    assert all((APP / s).is_file() for s in srcs), srcs
    assert {p.name for p in VM.iterdir() if p.name != "files.json"} == {
        Path(s).name for s in srcs if s.startswith("vm/")}, "a file under vm/ that nothing installs"
    for unit in FILES["enable"]:
        assert f"vm/{unit}" in srcs and _dest(f"vm/{unit}") == f"/etc/systemd/system/{unit}"


def test_the_units_run_what_files_json_installs():
    assert _exec_start("swarm-speech.service") == _dest("vm/swarm-speech-launch.sh")
    assert _exec_start("swarm-speech-watchdog.service") == (
        "/usr/bin/python3 " + _dest("swarm_speech/watchdog.py"))
    assert next(f["mode"] for f in FILES["files"] if f["src"] == "vm/swarm-speech-launch.sh") == "0755"
    service = (VM / "swarm-speech.service").read_text(encoding="utf-8")
    assert re.search(r"^Restart=always$", service, re.M), "a crashed server is started again (§4.5)"


def test_the_launcher_is_a_house_script():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert os.access(LAUNCHER, os.X_OK)
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [ln for ln in text.splitlines()[1:] if ln.strip() and not ln.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"


def _env_read_by_main() -> set[str]:
    tree = ast.parse((APP / "swarm_speech" / "main.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant)):
            names.add(node.args[0].value)
    return names


def test_the_launcher_passes_exactly_what_the_server_reads_and_the_manifest_points_at_it():
    lines = LAUNCHER.read_text(encoding="utf-8").splitlines()
    passed = {m.group(1): n for n, ln in enumerate(lines, 1) for m in [re.search(r"--env ([A-Z_]+)=", ln)] if m}
    assert set(passed) == _env_read_by_main() == {"SWARM_SPEECH_CALLER", "SWARM_SPEECH_MODELS", "PORT"}
    fragment = yaml.safe_load((REPO / "install" / "manifest.d" / "voice-speech-server.yaml").read_text())
    for name, var in fragment["env"].items():
        assert var["setter"] == f"apps/swarm-speech/vm/swarm-speech-launch.sh:{passed[name]}", name


def test_the_launcher_runs_only_a_digest_pinned_image_with_the_gpu_and_the_state_disk():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "swarm-speech@sha256:[0-9a-f]{64}$" in text
    assert "--gpus all" in text and "--publish 8080:8080" in text
    root = re.search(r'^readonly ROOT="([^"]+)"$', text, re.M).group(1)
    assert Path(root) == main.MODEL_ROOT.parent == main.STATE_DIR.parent
    assert watchdog.STATE_FILE == main.STATE_DIR / activity.FILENAME
    assert '--volume "${ROOT}:${ROOT}"' in text


def test_the_image_runs_as_the_uid_that_owns_the_state_disk():
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    uid = re.search(r'^readonly UID_GID="(\d+)"$', LAUNCHER.read_text(encoding="utf-8"), re.M).group(1)
    assert f"--uid {uid} --gid {uid}" in dockerfile and "USER swarm:swarm" in dockerfile
    assert dockerfile.rstrip().endswith('ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_speech.main"]')
