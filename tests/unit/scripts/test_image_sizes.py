"""`scripts/image-sizes.sh` lists every released digest of a worker image with
its compressed size and its layers, so a growth can be bisected (#625).

WHY IT EXISTS. Claude-code container start on Cloud Run Jobs went from a 71 s
p50 on 2026-09-24 to ~115 s on 09-30..10-02 and 166-168 s on 10-04/10-05, while
GKE pods start in 17 s. The suspect was agent-runtime-base growing; nobody had
a list of what each release weighed, so the suspicion could not be checked
against a release. This script is that list, read from Artifact Registry.

The properties asserted here, against the REAL script with a fake `gcloud` and
a fake `curl` on PATH serving a three-release registry:

  * every release is listed oldest first with the sum of its manifest's layer
    sizes (the compressed bytes a node pulls) and the change from the release
    before it, so the release a jump arrived in is the row with the large delta;
  * a multi-platform index is measured by its linux/amd64 manifest, the one
    Cloud Run pulls, never by the sum of every platform;
  * `--layers` names the Dockerfile instruction behind each layer, from the
    image config's history, so a jump reads as the instruction that caused it;
  * `--diff OLD NEW` (a tag or a digest each) prints the layers NEW added and
    the ones it dropped, with the net change -- the bisect step itself;
  * it only READS: the one gcloud listing, the access token, and GETs on the
    registry. Nothing is written, tagged or deleted;
  * the access token never reaches curl's argv, stdout or stderr;
  * an unreadable registry, or an image with no release, fails naming the
    cause and prints no table -- an empty measurement is not a measurement.

WHAT THIS CANNOT PROVE: that Artifact Registry answers in exactly the shapes
the fakes serve. The shapes are the documented ones (the Docker Registry HTTP
API v2 manifest and image index, the OCI image config's `history`, and
`gcloud artifacts docker images list --format=json`), and only a run against
the real registry exercises them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "image-sizes.sh"
PROJECT = "swarm-test-project"
HOST = "us-central1-docker.pkg.dev"
PACKAGE = f"{HOST}/{PROJECT}/swarm-images/agent-runtime-base"
MB = 1_000_000

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="image-sizes.sh needs bash and jq",
)

FAKE_GCLOUD = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps({"tool": "gcloud", "args": args}) + "\n")
if args[:2] == ["auth", "print-access-token"]:
    print(os.environ["FAKE_TOKEN"])
    sys.exit(0)
if args[:4] == ["artifacts", "docker", "images", "list"]:
    if os.environ.get("FAKE_LIST_FAIL"):
        print("ERROR: (gcloud.artifacts.docker.images.list) PERMISSION_DENIED: "
              "Permission 'artifactregistry.versions.list' denied", file=sys.stderr)
        sys.exit(1)
    listing = json.load(open(os.environ["FAKE_LISTING"]))
    print(json.dumps(listing.get(args[4], [])))
    sys.exit(0)
print("fake gcloud: unexpected call " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

# Enough curl for the script: -K - (the Authorization header on stdin, as
# common.sh's auth_config hands it), -H, -o, -w '%{http_code}', -L, -sS, -m.
FAKE_CURL = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
out, fmt, url, config, i = None, "", None, "", 0
flags = []
while i < len(args):
    a = args[i]
    if a in ("-K", "--config"):
        if args[i + 1] == "-":
            config = sys.stdin.read()
        i += 2
        continue
    if a in ("-o", "--output"):
        out = args[i + 1]; i += 2; continue
    if a in ("-w", "--write-out"):
        fmt = args[i + 1]; i += 2; continue
    if a in ("-H", "--header", "-m", "--max-time"):
        flags.append(a); i += 2; continue
    if a.startswith("-"):
        flags.append(a); i += 1; continue
    url = a; i += 1
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps({"tool": "curl", "argv": args, "flags": flags, "url": url,
                         "authorized": "Authorization: Bearer " + os.environ["FAKE_TOKEN"] in config}) + "\n")
registry = json.load(open(os.environ["FAKE_REGISTRY"]))
entry = registry.get(url.split("/v2/", 1)[-1]) if url else None
status, body = (entry["status"], entry["body"]) if entry else (404, '{"errors":[{"code":"MANIFEST_UNKNOWN"}]}')
if "Authorization: Bearer " + os.environ["FAKE_TOKEN"] not in config:
    status, body = 401, '{"errors":[{"code":"UNAUTHORIZED"}]}'
data = body if isinstance(body, str) else json.dumps(body)
if out:
    open(out, "w").write(data)
else:
    sys.stdout.write(data)
if fmt:
    sys.stdout.write(fmt.replace("%{http_code}", str(status)))
sys.exit(0)
'''


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _layer(seed: str, size: int) -> dict:
    return {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": _digest(seed), "size": size}


def _image(name: str, layers: list[tuple[str, int, str]]) -> tuple[dict, dict, str]:
    """A manifest, its config blob, and the config's digest."""
    history = [{"created_by": "ARG PYTHON_IMAGE", "empty_layer": True}]
    for _, _, created_by in layers:
        history.append({"created_by": created_by})
        history.append({"created_by": "ENV X=1", "empty_layer": True})
    config = {"architecture": "amd64", "os": "linux", "history": history}
    config_digest = _digest("config-" + name)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": config_digest, "size": 1234},
        "layers": [_layer(seed, size) for seed, size, _ in layers],
    }
    return manifest, config, config_digest


BASE_LAYERS = [("os", 30 * MB, "/bin/sh -c #(nop) ADD file:debian in /"),
               ("worker", 50 * MB, "COPY /opt/venv /opt/venv # buildkit")]
TOOLBOX = ("toolbox", 200 * MB, "RUN /bin/sh -c mkdir -p /tmp/toolbox && curl gcloud # buildkit")
GO = ("go", 70 * MB, "COPY /usr/local/go /usr/local/go # buildkit")
SERVERS = ("servers", 60 * MB, "RUN /bin/sh -c cd /opt/repo-index/servers && npm ci # buildkit")
WORKER2 = ("worker2", 52 * MB, "COPY /opt/venv /opt/venv # buildkit")

RELEASES = {
    # name: (createTime, tags, layers, as an index?)
    "r1": ("2026-09-24T10:00:00Z", ["aaa111"], BASE_LAYERS, False),
    "r2": ("2026-10-01T10:00:00Z", ["bbb222"], BASE_LAYERS + [TOOLBOX], False),
    "r3": ("2026-10-05T10:00:00Z", ["ccc333", "dev"],
           [BASE_LAYERS[0], WORKER2, TOOLBOX, GO, SERVERS], True),
}


def _registry() -> tuple[dict, dict, dict[str, str]]:
    served: dict[str, dict] = {}
    listing = []
    digests: dict[str, str] = {}
    prefix = f"{PROJECT}/swarm-images/agent-runtime-base"
    for name, (created, tags, layers, as_index) in RELEASES.items():
        manifest, config, config_digest = _image(name, layers)
        manifest_digest = _digest("manifest-" + name)
        served[f"{prefix}/blobs/{config_digest}"] = {"status": 200, "body": config}
        if as_index:
            arm, arm_config, arm_config_digest = _image(name + "-arm", [("arm", 999 * MB, "RUN arm")])
            arm_digest = _digest("manifest-arm-" + name)
            served[f"{prefix}/manifests/{arm_digest}"] = {"status": 200, "body": arm}
            served[f"{prefix}/blobs/{arm_config_digest}"] = {"status": 200, "body": arm_config}
            served[f"{prefix}/manifests/{manifest_digest}"] = {"status": 200, "body": manifest}
            top = _digest("index-" + name)
            served[f"{prefix}/manifests/{top}"] = {"status": 200, "body": {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": arm_digest, "size": 1,
                     "mediaType": "application/vnd.oci.image.manifest.v1+json",
                     "platform": {"architecture": "arm64", "os": "linux"}},
                    {"digest": manifest_digest, "size": 1,
                     "mediaType": "application/vnd.oci.image.manifest.v1+json",
                     "platform": {"architecture": "amd64", "os": "linux"}},
                ],
            }}
            manifest_digest = top
        else:
            served[f"{prefix}/manifests/{manifest_digest}"] = {"status": 200, "body": manifest}
        digests[name] = manifest_digest
        listing.append({
            "package": PACKAGE, "version": manifest_digest, "tags": tags,
            "createTime": created, "updateTime": created,
            "metadata": {"imageSizeBytes": str(sum(size for _, size, _ in layers))},
        })
    # The listing arrives newest first, as the registry's default order does
    # not promise one; the script must sort.
    listing.reverse()
    return served, {PACKAGE: listing}, digests


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        path = bindir / name
        path.write_text(body.replace("{python}", sys.executable))
        path.chmod(0o755)
    served, listing, digests = _registry()
    (tmp_path / "registry.json").write_text(json.dumps(served))
    (tmp_path / "listing.json").write_text(json.dumps(listing))
    # Built at run time: nothing token-shaped is a literal in this file.
    token = "ya29." + secrets.token_urlsafe(48)
    env = {k: v for k, v in os.environ.items()
           if k not in ("K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
        "FAKE_EVENTS": str(tmp_path / "events.jsonl"),
        "FAKE_REGISTRY": str(tmp_path / "registry.json"),
        "FAKE_LISTING": str(tmp_path / "listing.json"),
        "FAKE_TOKEN": token,
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "PROJECT_ID": PROJECT,
        "REGION": "us-central1",
        "NO_COLOR": "1",
    })
    return {"env": env, "tmp": tmp_path, "digests": digests, "token": token}


def _run(world: dict, *args: str, **extra: str) -> subprocess.CompletedProcess:
    env = dict(world["env"], **extra)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=120, check=False)


def _events(world: dict) -> list[dict]:
    path = world["tmp"] / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _rows(stdout: str) -> list[dict]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    header = lines[0].split("\t")
    assert header[:2] == ["created", "digest"], lines[0]
    return [dict(zip(header, line.split("\t"))) for line in lines[1:] if "\t" in line]


def test_each_release_is_listed_oldest_first_with_its_size_and_delta(world):
    done = _run(world)
    assert done.returncode == 0, done.stderr
    rows = _rows(done.stdout)
    assert [row["digest"] for row in rows] == [world["digests"][n] for n in ("r1", "r2", "r3")]
    assert [row["tags"] for row in rows] == ["aaa111", "bbb222", "ccc333,dev"]
    assert [row["compressed_mb"] for row in rows] == ["80.0", "280.0", "412.0"]
    assert [row["delta_mb"] for row in rows] == ["+0.0", "+200.0", "+132.0"]
    assert [row["layers"] for row in rows] == ["2", "3", "5"]
    # The count is printed, so a listing that visited one release cannot read
    # as a clean sweep of three.
    assert "3 releases of agent-runtime-base measured" in done.stderr


def test_a_multi_platform_index_is_measured_by_its_linux_amd64_manifest(world):
    rows = _rows(_run(world).stdout)
    # The arm64 manifest in r3's index holds a 999 MB layer; it is not counted.
    assert rows[-1]["compressed_mb"] == "412.0"


def test_layers_name_the_instruction_that_made_each(world):
    done = _run(world, "--layers")
    assert done.returncode == 0, done.stderr
    r3 = done.stdout.split(world["digests"]["r3"], 1)[1]
    assert re.search(r"70\.0\s+\S+\s+COPY /usr/local/go /usr/local/go", r3), r3
    assert re.search(r"60\.0\s+\S+\s+RUN /bin/sh -c cd /opt/repo-index/servers", r3), r3
    # An empty_layer history entry is never paired with a real layer.
    assert "ENV X=1" not in done.stdout and "ARG PYTHON_IMAGE" not in done.stdout


def test_diff_names_the_layers_a_release_added_and_dropped(world):
    done = _run(world, "--diff", "bbb222", world["digests"]["r3"])
    assert done.returncode == 0, done.stderr
    added = [line for line in done.stdout.splitlines() if line.startswith("+")]
    dropped = [line for line in done.stdout.splitlines() if line.startswith("-")]
    assert len(added) == 3, done.stdout
    assert any("COPY /usr/local/go" in line and "70.0" in line for line in added)
    assert any("npm ci" in line and "60.0" in line for line in added)
    assert any("52.0" in line for line in added)
    assert len(dropped) == 1 and "50.0" in dropped[0], done.stdout
    assert re.search(r"^net\s+\+132\.0 MB \(280\.0 MB -> 412\.0 MB\)$", done.stdout, re.M), done.stdout


def test_a_diff_naming_a_release_that_does_not_exist_fails(world):
    done = _run(world, "--diff", "bbb222", "no-such-tag")
    assert done.returncode != 0
    assert "no-such-tag" in done.stderr
    assert not [line for line in done.stdout.splitlines() if line.startswith("+")]


def test_it_only_reads(world):
    assert _run(world, "--layers").returncode == 0
    assert _run(world, "--diff", "aaa111", "dev").returncode == 0
    events = _events(world)
    gcloud = {tuple(e["args"][:4]) for e in events if e["tool"] == "gcloud"}
    assert gcloud <= {("auth", "print-access-token"), ("artifacts", "docker", "images", "list")}, gcloud
    curls = [e for e in events if e["tool"] == "curl"]
    assert curls, "no registry read was made"
    for call in curls:
        assert call["authorized"], call["url"]
        assert call["url"].startswith(f"https://{HOST}/v2/{PROJECT}/swarm-images/"), call["url"]
        writes = {"-X", "--request", "-d", "--data", "--data-binary", "-T", "--upload-file", "-F"}
        assert not writes & set(call["flags"]), call["argv"]


def test_the_token_never_reaches_argv_or_output(world):
    done = _run(world, "--layers")
    assert done.returncode == 0, done.stderr
    token = world["token"]
    assert token not in done.stdout and token not in done.stderr
    for call in _events(world):
        if call["tool"] == "curl":
            assert token not in json.dumps(call["argv"])


def test_an_unreadable_registry_fails_loudly_and_prints_no_table(world):
    done = _run(world, FAKE_LIST_FAIL="1")
    assert done.returncode != 0
    assert "PERMISSION_DENIED" in done.stderr
    assert "artifactregistry.reader" in done.stderr
    assert "compressed_mb" not in done.stdout


def test_an_image_with_no_release_is_not_reported_as_measured(world):
    done = _run(world, "--image", "agent-runtime-indexer")
    assert done.returncode != 0
    assert "no released digest of agent-runtime-indexer" in done.stderr
    assert "measured" not in done.stderr


def test_a_manifest_the_registry_refuses_fails_naming_the_digest(world):
    served = json.loads((world["tmp"] / "registry.json").read_text())
    prefix = f"{PROJECT}/swarm-images/agent-runtime-base/manifests/"
    served[prefix + world["digests"]["r2"]] = {"status": 403, "body": '{"errors":[{"code":"DENIED"}]}'}
    (world["tmp"] / "registry.json").write_text(json.dumps(served))
    done = _run(world)
    assert done.returncode != 0
    assert world["digests"]["r2"] in done.stderr and "403" in done.stderr


def test_an_image_name_is_one_path_segment(world):
    done = _run(world, "--image", "../other-repo/x")
    assert done.returncode != 0
    assert not [e for e in _events(world) if e["tool"] == "curl"]


@pytest.mark.parametrize("name", ["mapfile", "readarray", "declare -A", "${x,,}"])
def test_nothing_bash_3_2_lacks(name):
    code = "\n".join(line for line in SCRIPT.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    needle = {"${x,,}": ",,}"}.get(name, name)
    assert needle not in code


def test_set_euo_pipefail_is_the_first_effective_line():
    for line in SCRIPT.read_text().splitlines()[1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            assert line.strip() == "set -euo pipefail"
            break
    assert os.access(SCRIPT, os.X_OK)
