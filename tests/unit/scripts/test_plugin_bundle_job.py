"""The bundle job's entrypoint: `swarm-bundle job` (images/plugin-bundler).

docs/design/managed-plugins.md, section 1.4.2 (the owner, 11 November 2026:
the bundle build is one execution of the Cloud Run job swarm-plugin-bundle,
under the builder account). Whoever may run the job chooses its arguments,
its environment, its task count and its timeout, so this file holds:

  * a malformed argument (or a command flag edited by hand) stops the
    execution before any work: no token is asked for, nothing is fetched, no
    recipe runs, nothing is pushed;
  * every environment variable the execution was handed is discarded, and a
    recipe sees only the job's own fixed environment;
  * a task other than the first does nothing, and task 0 refuses a count
    other than one;
  * the appended image keeps every base layer by digest, mounted and never
    downloaded, keeps the base config's diff_ids, and adds one layer per
    plugin holding only paths under /opt/swarm/plugins/<name>/ (and its
    opaque whiteout), reproducibly.

No cloud, no emulator, no Docker: the registry, the bucket and the metadata
server are a fake transport, and the recipes a fake runner.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BUNDLER = REPO / "images" / "plugin-bundler"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


bundle = _load("swarm_bundle_job", BUNDLER / "swarm-bundle")

PROJECT = "example-project"
REGION = "us-central1"
BASE_REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-images"
BUNDLE_REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-plugin-bundles"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """The bundler image's /opt/swarm-bundler, laid out as the Dockerfile copies it."""
    out = tmp_path / "opt-swarm-bundler"
    shutil.copytree(BUNDLER / "recipes", out / "recipes")
    inputs = out / "inputs"
    inputs.mkdir()
    shutil.copy(REPO / "images" / "agent-runtime-base" / "gstack-layers", inputs / "gstack-layers")
    shutil.copy(REPO / "images" / "agent-runtime-base" / "gstack-base-files.txt", inputs / "gstack-base-files.txt")
    shutil.copy(REPO / "images" / "agent-runtime-browser" / "gstack-browser-files.txt",
                inputs / "gstack-browser-files.txt")
    shutil.copy(REPO / "scripts" / "gstack_packaging_measure.py", inputs / "gstack_packaging_measure.py")
    shutil.copy(REPO / "images" / "agent-runtime-base" / "bundled-plugins.json", out / "bundled-plugins.json")
    return out


def _command() -> list[str]:
    return [
        f"--project={PROJECT}",
        f"--region={REGION}",
        f"--plugins-bucket={PROJECT}-swarm-plugins",
        f"--build-mirror-bucket=swarm-build-mirror-{PROJECT}",
        f"--base-repository={BASE_REPO}",
        f"--bundle-repository={BUNDLE_REPO}",
        f"--npm-registry=https://{REGION}-npm.pkg.dev/{PROJECT}/swarm-mirror-npm/",
        "--runner-image=agent-runtime-base:base",
        "--runner-image=agent-runtime-browser:browser",
        "--",
    ]


def _args(root: Path, **overrides: str) -> list[str]:
    values = {
        "bundle_id": "b-0123abcd",
        "source": "a" * 64,
        "recipe": bundle.job_recipe_digest(root, "security-audit"),
        "image": "agent-runtime-base",
        "digest": "sha256:" + "b" * 64,
    }
    values.update(overrides)
    return [values["bundle_id"], "security-audit", values["source"], values["recipe"], values["image"], values["digest"]]


def _environ(**extra: str) -> dict[str, str]:
    env = {"CLOUD_RUN_TASK_COUNT": "1", "CLOUD_RUN_TASK_INDEX": "0", "CLOUD_RUN_EXECUTION": "swarm-plugin-bundle-x7k2p"}
    env.update(extra)
    return env


class NoWork:
    """A transport and a runner that fail the test if anything is attempted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def transport(self, method, url, headers, body):  # noqa: ANN001
        self.calls.append(f"{method} {url}")
        raise AssertionError(f"the job reached out before its arguments were checked: {method} {url}")

    def run(self, command, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(" ".join(command))
        raise AssertionError("the job ran a recipe before its arguments were checked")


@pytest.mark.parametrize(
    "overrides",
    [
        {"bundle_id": "B-UPPER"},
        {"bundle_id": "b;rm -rf /"},
        {"bundle_id": "b" * 64},
        {"bundle_id": "--plugins-bucket=elsewhere"},
        {"source": "a" * 63},
        {"source": "A" * 64},
        {"source": "../../tenants/eng/x"},
        {"recipe": "f" * 64},  # a well-formed digest that is not this image's recipe
        {"recipe": "sha256:" + "f" * 64},
        {"image": "agent-runtime-elsewhere"},  # not a runner image the command lists
        {"image": "../swarm-api"},
        {"digest": "b" * 64},
        {"digest": "sha256:" + "B" * 64},
        {"digest": "latest"},
    ],
    ids=lambda o: next(iter(o)) + "=" + next(iter(o.values()))[:24],
)
def test_a_malformed_argument_stops_before_any_work(root: Path, overrides: dict[str, str]) -> None:
    # MUTATION: drop any one pattern check in parse_job and the case that
    # reaches the fake transport fails here.
    work = NoWork()
    rc = bundle.job_main([*_command(), *_args(root, **overrides)], _environ(),
                         root=root, transport=work.transport, run=work.run)
    assert rc == 1
    assert work.calls == []


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["b-1"],
        ["b-1", "agent-runtime-base", "sha256:" + "b" * 64],  # no bundled plugin
        ["b-1", "security-audit", "a" * 64],  # a plugin without its recipe digest
        ["b-1", "not-a-recipe", "a" * 64, "c" * 64, "agent-runtime-base", "sha256:" + "b" * 64],
    ],
)
def test_a_short_or_unknown_argument_list_stops_before_any_work(root: Path, args: list[str]) -> None:
    work = NoWork()
    assert bundle.job_main([*_command(), *args], _environ(), root=root, transport=work.transport, run=work.run) == 1
    assert work.calls == []


def test_a_plugin_named_twice_or_an_image_missing_its_digest_stops(root: Path) -> None:
    work = NoWork()
    good = _args(root)
    twice = [good[0], *good[1:4], *good[1:4], *good[4:]]
    assert bundle.job_main([*_command(), *twice], _environ(), root=root, transport=work.transport, run=work.run) == 1
    assert bundle.job_main([*_command(), *good[:-1]], _environ(), root=root, transport=work.transport, run=work.run) == 1
    assert work.calls == []


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: [x for x in c if x != "--"],
        lambda c: [x.replace(PROJECT, "other-project") if x.startswith("--bundle") else x for x in c],
        lambda c: ["--plugins-bucket=second-bucket", *c],
        lambda c: [x for x in c if not x.startswith("--npm-registry")],
        lambda c: [x.replace("https://", "http://") if x.startswith("--npm") else x for x in c],
        lambda c: [x for x in c if not x.startswith("--runner-image")],
    ],
    ids=["no-separator", "foreign-repository", "flag-twice", "flag-missing", "http-registry", "no-runner-image"],
)
def test_a_command_flag_out_of_shape_stops_before_any_work(root: Path, edit) -> None:  # noqa: ANN001
    work = NoWork()
    assert bundle.job_main([*edit(_command()), *_args(root)], _environ(), root=root,
                           transport=work.transport, run=work.run) == 1
    assert work.calls == []


def test_the_execution_environment_is_discarded(root: Path) -> None:
    # Anything an override could set: the job's own names, a proxy, the
    # loader's and Bun's, a bash function. All gone before parse_job returns.
    overridden = _environ(
        PLUGINS_BUCKET="elsewhere",
        HTTPS_PROXY="http://127.0.0.1:9",
        BUN_CONFIG_REGISTRY="https://registry.invalid/",
        LD_PRELOAD="/tmp/x.so",
        **{"BASH_FUNC_curl%%": "() { :; }"},
    )
    job = bundle.parse_job([*_command(), *_args(root)], overridden, root=root)
    assert job is not None
    assert overridden == {}
    assert job.plugins_bucket == f"{PROJECT}-swarm-plugins"


def test_a_recipe_runs_with_the_fixed_environment_only(root: Path, tmp_path: Path) -> None:
    # MUTATION: pass os.environ (or any part of it) to the recipe and the
    # overridden names appear in the recipe's environment.
    world = FakeWorld(root)
    environ = _environ(PLUGINS_BUCKET="elsewhere", BUN_CONFIG_REGISTRY="https://registry.invalid/",
                       PATH="/tmp/evil:/usr/bin")
    args = _args(root, source=world.source_sha, digest=world.registry.base_digest)
    rc = bundle.job_main([*_command(), *args], environ, root=root, transport=world.transport,
                         run=world.run, scratch_root=str(tmp_path))
    assert rc == 0, world.calls
    assert environ == {}
    assert world.recipe_envs
    for env in world.recipe_envs:
        assert set(env) == {"PATH", "LANG", "HOME", "TMPDIR"}
        assert env["PATH"] == bundle.JOB_ENV["PATH"]
    # The bucket read is the command's, not the override's.
    assert any(f"/b/{PROJECT}-swarm-plugins/o/" in call for call in world.calls)
    assert not any("elsewhere" in call for call in world.calls)


def test_a_second_task_does_nothing_and_task_zero_needs_a_count_of_one(root: Path) -> None:
    work = NoWork()
    assert bundle.job_main([*_command(), *_args(root)], _environ(CLOUD_RUN_TASK_INDEX="1"),
                           root=root, transport=work.transport, run=work.run) == 0
    assert bundle.job_main([*_command(), *_args(root)], _environ(CLOUD_RUN_TASK_COUNT="2"),
                           root=root, transport=work.transport, run=work.run) == 1
    assert work.calls == []


def test_the_job_keeps_its_own_deadline(root: Path, tmp_path: Path) -> None:
    # The clock jumps past 3000 s while the recipe runs: nothing is pushed.
    world = FakeWorld(root)
    ticks = iter([0.0, 10.0, bundle.JOB_DEADLINE_SECONDS + 1.0] + [bundle.JOB_DEADLINE_SECONDS + 2.0] * 50)
    rc = bundle.job_main([*_command(), *_args(root)], _environ(), root=root, transport=world.transport,
                         run=world.run, scratch_root=str(tmp_path), clock=lambda: next(ticks))
    assert rc == 1
    assert world.registry.manifests_put == []
    assert bundle.JOB_DEADLINE_SECONDS == 3000


# ---------------------------------------------------------------------------
# The fake cloud: metadata server, the plugins and mirror buckets, and a
# registry that serves the release image and records every write.
# ---------------------------------------------------------------------------


class FakeRegistry:
    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[str, bytes]] = {}
        self.blob_reads: list[str] = []
        self.mounts: list[tuple[str, str, str]] = []
        self.uploads: list[tuple[str, str]] = []
        self.manifests_put: list[tuple[str, str, bytes]] = []
        base_layers = [b"base layer one " + secrets.token_bytes(8), b"base layer two " + secrets.token_bytes(8)]
        self.base_diff_ids = [_sha(b"diff of " + layer) for layer in base_layers]
        config = {"architecture": "amd64", "os": "linux", "config": {"User": "10001"},
                  "rootfs": {"type": "layers", "diff_ids": list(self.base_diff_ids)},
                  "history": [{"created_by": "release"}, {"created_by": "release"}]}
        config_body = json.dumps(config).encode()
        repo = "example-project/swarm-images/agent-runtime-base"
        for blob in [*base_layers, config_body]:
            self.blobs[(repo, _sha(blob))] = blob
        self.base_layers = [{"mediaType": bundle.LAYER_TYPES[bundle.DOCKER_MANIFEST], "size": len(b), "digest": _sha(b)}
                            for b in base_layers]
        manifest = {"schemaVersion": 2, "mediaType": bundle.DOCKER_MANIFEST,
                    "config": {"mediaType": bundle.CONFIG_TYPES[bundle.DOCKER_MANIFEST], "size": len(config_body),
                               "digest": _sha(config_body)},
                    "layers": self.base_layers}
        body = json.dumps(manifest).encode()
        self.base_digest = _sha(body)
        self.base_config = config
        self.manifests[(repo, self.base_digest)] = (bundle.DOCKER_MANIFEST, body)
        self.uploading: dict[str, str] = {}

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == f"{REGION}-docker.pkg.dev"
        assert headers.get("Authorization", "").startswith("Basic ")
        path = parts.path[len("/v2/"):]
        query = dict(urllib.parse.parse_qsl(parts.query))
        if "/manifests/" in path:
            repo, _, ref = path.partition("/manifests/")
            if method == "GET":
                found = self.manifests.get((repo, ref))
                if found is None:
                    return bundle.Response(404)
                return bundle.Response(200, {"content-type": found[0]}, found[1])
            assert method == "PUT"
            self.manifests_put.append((repo, ref, body))
            for layer in json.loads(body)["layers"]:
                assert (repo, layer["digest"]) in self.blobs, "a manifest named a blob the repository does not hold"
            self.manifests[(repo, ref)] = (headers["Content-Type"], body)
            return bundle.Response(201, {"docker-content-digest": _sha(body)})
        if "/blobs/uploads/" in path:
            repo = path.partition("/blobs/uploads/")[0]
            if method == "POST" and "mount" in query:
                source = (query["from"], query["mount"])
                if source not in self.blobs:
                    return bundle.Response(202, {"location": f"/v2/{repo}/blobs/uploads/u1"})
                self.mounts.append((repo, query["mount"], query["from"]))
                self.blobs[(repo, query["mount"])] = self.blobs[source]
                return bundle.Response(201)
            if method == "POST":
                session = secrets.token_hex(4)
                self.uploading[session] = repo
                return bundle.Response(202, {"location": f"/v2/{repo}/blobs/uploads/{session}"})
            assert method == "PUT"
            session = path.rsplit("/", 1)[-1]
            assert _sha(body) == query["digest"]
            self.blobs[(self.uploading.pop(session), query["digest"])] = body
            self.uploads.append((repo, query["digest"]))
            return bundle.Response(201)
        if "/blobs/" in path:
            repo, _, digest = path.partition("/blobs/")
            found = self.blobs.get((repo, digest))
            if method == "HEAD":
                return bundle.Response(200 if found is not None else 404)
            self.blob_reads.append(digest)
            return bundle.Response(200, {}, found) if found is not None else bundle.Response(404)
        raise AssertionError(f"unexpected registry call {method} {url}")


def _source_archive(plugin: str) -> bytes:
    """A gzip tar with git's pax commit header, as GitHub serves an archive."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "c" * 40}) as tar:
        data = b"0.1.0\n"
        info = tarfile.TarInfo(f"{plugin}-src/VERSION")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


class FakeWorld:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = FakeRegistry()
        self.calls: list[str] = []
        self.recipe_envs: list[dict[str, str]] = []
        self.objects: dict[str, bytes] = {}
        self.archive = _source_archive("security-audit")
        self.source_sha = hashlib.sha256(self.archive).hexdigest()
        self.objects[f"{PROJECT}-swarm-plugins/platform/sources/{self.source_sha}.tar"] = self.archive

    def transport(self, method, url, headers, body):  # noqa: ANN001
        self.calls.append(f"{method} {url}")
        if url.startswith("http://metadata.google.internal/"):
            assert headers == {"Metadata-Flavor": "Google"}
            return bundle.Response(200, {}, json.dumps({"access_token": secrets.token_hex(16)}).encode())
        if url.startswith("https://storage.googleapis.com/"):
            parts = urllib.parse.urlsplit(url).path.split("/")
            bucket, name = parts[4], urllib.parse.unquote(parts[6])
            found = self.objects.get(f"{bucket}/{name}")
            return bundle.Response(200, {}, found) if found is not None else bundle.Response(404)
        return self.registry(method, url, headers, body)

    def run(self, command, *, env, cwd, timeout, check, stdin):  # noqa: ANN001
        self.recipe_envs.append(dict(env))
        out = Path(command[command.index("--out") + 1])
        skill = out / "skills" / "security-audit"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# security audit\n")
        (out / "LICENSE").write_text("license\n")
        pins = Path(command[command.index("--pins") + 1])
        assert json.loads(pins.read_text())["plugins"]["security-audit"]["source"]["sha256"] == self.source_sha
        (out / ".swarm-bundle.json").write_text(json.dumps({"plugin": "security-audit"}) + "\n")
        return subprocess.CompletedProcess(command, 0)


def _layer_members(blob: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob)), mode="r:") as tar:
        return tar.getmembers()


def test_the_appended_image_keeps_every_base_layer_and_adds_only_the_plugin_root(root: Path, tmp_path: Path) -> None:
    world = FakeWorld(root)
    args = _args(root, source=world.source_sha, digest=world.registry.base_digest)
    rc = bundle.job_main([*_command(), *args], _environ(), root=root, transport=world.transport,
                         run=world.run, scratch_root=str(tmp_path))
    assert rc == 0, world.calls
    registry = world.registry

    # One manifest, pushed last, under <runner image name>:<bundle_id>.
    [(repo, tag, body)] = registry.manifests_put
    assert (repo, tag) == ("example-project/swarm-plugin-bundles/agent-runtime-base", "b-0123abcd")
    manifest = json.loads(body)

    # Every base layer, in order, by digest; mounted from the platform
    # repository, never read.
    assert manifest["layers"][: len(registry.base_layers)] == registry.base_layers
    assert [m[1] for m in registry.mounts] == [layer["digest"] for layer in registry.base_layers]
    assert all(m[2] == "example-project/swarm-images/agent-runtime-base" for m in registry.mounts)
    assert not set(registry.blob_reads) & {layer["digest"] for layer in registry.base_layers}

    # The config keeps the base diff_ids and adds one per new layer.
    config = json.loads(registry.blobs[(repo, manifest["config"]["digest"])])
    new_layers = manifest["layers"][len(registry.base_layers):]
    assert len(new_layers) == 1
    assert config["rootfs"]["diff_ids"][: len(registry.base_diff_ids)] == registry.base_diff_ids
    assert config["config"] == registry.base_config["config"]
    blob = registry.blobs[(repo, new_layers[0]["digest"])]
    assert config["rootfs"]["diff_ids"][-1] == _sha(gzip.decompress(blob))

    # The new layer: only /opt/swarm/plugins/security-audit/, its opaque
    # whiteout, folders and regular files, root-owned, time 0.
    members = _layer_members(blob)
    names = [m.name for m in members]
    assert all(n == "opt/swarm/plugins/security-audit" or n.startswith("opt/swarm/plugins/security-audit/")
               for n in names), names
    assert "opt/swarm/plugins/security-audit/.wh..wh..opq" in names
    assert [n for n in names if ".wh." in n] == ["opt/swarm/plugins/security-audit/.wh..wh..opq"]
    assert "opt/swarm/plugins/security-audit/.swarm-bundle.json" in names
    assert "opt/swarm/plugins/security-audit/skills/security-audit/SKILL.md" in names
    assert all((m.isfile() or m.isdir()) and m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in members)
    assert not any(".." in n.split("/") for n in names)


def test_a_layer_is_reproducible_from_its_tree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "skills").mkdir(parents=True)
    (tree / "skills" / "a.md").write_text("a\n")
    (tree / ".swarm-bundle.json").write_text("{}\n")
    first = bundle.gzip_layer(bundle.layer_tar("security-audit", [tree]))
    second = bundle.gzip_layer(bundle.layer_tar("security-audit", [tree]))
    assert first == second


def test_a_layer_refuses_a_link_in_the_tree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".swarm-bundle.json").write_text("{}\n")
    (tree / "escape").symlink_to("/etc")
    with pytest.raises(SystemExit):
        bundle.layer_tar("security-audit", [tree])


def test_a_base_layer_that_cannot_be_mounted_is_never_downloaded(root: Path, tmp_path: Path) -> None:
    # A registry that would not mount answers 202 (an upload to fill): the job
    # refuses rather than read the layer and send it again.
    world = FakeWorld(root)
    registry = world.registry
    first = registry.base_layers[0]["digest"]
    registry.blobs[("elsewhere", first)] = registry.blobs.pop(("example-project/swarm-images/agent-runtime-base", first))
    args = _args(root, source=world.source_sha, digest=registry.base_digest)
    rc = bundle.job_main([*_command(), *args], _environ(), root=root, transport=world.transport,
                         run=world.run, scratch_root=str(tmp_path))
    assert rc == 1
    assert registry.manifests_put == []
    assert first not in registry.blob_reads
