"""The intake job's own output, finished by swarm-api (docs/design/managed-plugins.md §2.3).

The job (apps/plugin-intake, its `main` as deployed) writes
`staging/<intake_id>/runnable.tar` and `report.json`; swarm-api's finish path
(the callback and the console's intake GET) parses the report, streams the
staged tar to hash it itself, and copies it under the scope prefix.

The bucket is faked one level below swarm-api's `GcsPluginBucket`, at the
google-cloud-storage objects it calls: the library's own `Blob` and
`BlobReader` run, with only the transport replaced by bytes in memory, so a
call the installed library refuses fails here as it fails against the real
bucket. No credentials, no emulator, no socket.

And what swarm-api does with a report it cannot use (it ends the intake
refused with `report_invalid`, and the callback answers 200 so Pub/Sub stops
redelivering), and with a bucket it cannot read (503, so Pub/Sub retries),
each named in the log by a fixed reason code beside the exception's class.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sys
from pathlib import Path
from typing import Any

import pytest
from google.api_core import exceptions as gexc
from google.cloud import storage

from swarm_api import pluginstore as ps
from swarm_api.routes import plugin_catalogue as pc

from .conftest import auth_header
from .test_plugin_catalogue_routes import PLATFORM, github_install, notify, world  # noqa: F401 -- `world` is a fixture

# The job is not a member of the root uv project; its tests put it on sys.path
# the same way (apps/plugin-intake/tests/conftest.py).
INTAKE_APP = Path(__file__).resolve().parents[3] / "apps" / "plugin-intake"
for _path in (str(INTAKE_APP), str(INTAKE_APP / "tests")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fake_bucket import FakeBucket as JobBucket  # noqa: E402
from intake_archives import file, folder, tar_gz  # noqa: E402
from plugin_intake import main as job  # noqa: E402
from plugin_intake import nonet  # noqa: E402

LOGGER = "swarm_api.routes.plugin_catalogue"


# --- the plugins bucket, below GcsPluginBucket -------------------------------


class MemoryBlob(storage.Blob):
    """google-cloud-storage's Blob with its transport replaced by bytes in memory."""

    def __init__(self, name: str, bucket: "MemoryBucket", generation: int | None = None) -> None:
        super().__init__(name, bucket, generation=generation)
        self._memory = bucket

    def _held(self) -> bytes:
        held = self._memory.objects.get(self.name)
        if held is None or (self.generation is not None and held[1] != self.generation):
            raise gexc.NotFound(self.name)
        return held[0]

    def download_as_bytes(self, client: Any = None, start: int | None = None, end: int | None = None,
                          raw_download: bool = False, if_generation_match: int | None = None,
                          **_kwargs: Any) -> bytes:
        self._memory.reads.append(self.name)
        if self._memory.read_error is not None:
            raise self._memory.read_error
        data = self._held()
        if if_generation_match is not None and self._memory.objects[self.name][1] != if_generation_match:
            raise gexc.PreconditionFailed(self.name)
        start = start or 0
        return data[start:] if end is None else data[start:end + 1]  # `end` is inclusive, as in the JSON API

    def upload_from_file(self, file_obj: Any, rewind: bool = False, size: int | None = None,
                         content_type: str | None = None, if_generation_match: int | None = None,
                         **_kwargs: Any) -> None:
        if if_generation_match == 0 and self.name in self._memory.objects:
            raise gexc.PreconditionFailed(self.name)
        self._memory.put(self.name, file_obj.read() if size is None else file_obj.read(size))

    def delete(self, client: Any = None, **_kwargs: Any) -> None:
        if self._memory.objects.pop(self.name, None) is None:
            raise gexc.NotFound(self.name)


class MemoryBucket(storage.Bucket):
    """The objects swarm-api's GcsPluginBucket reaches, by name: (bytes, generation)."""

    def __init__(self) -> None:
        super().__init__(client=None, name="plugins-bucket-test")
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.reads: list[str] = []
        self.read_error: Exception | None = None
        self._generation = 1_700_000_000_000_000

    def put(self, name: str, data: bytes) -> None:
        self._generation += 1
        self.objects[name] = (data, self._generation)

    def blob(self, blob_name: str, chunk_size: int | None = None, encryption_key: Any = None,
             kms_key_name: Any = None, generation: int | None = None, **_kwargs: Any) -> MemoryBlob:
        return MemoryBlob(blob_name, self, generation=generation)

    def get_blob(self, blob_name: str, client: Any = None, **_kwargs: Any) -> MemoryBlob | None:
        if self.read_error is not None:
            raise self.read_error
        held = self.objects.get(blob_name)
        if held is None:
            return None
        found = MemoryBlob(blob_name, self, generation=held[1])
        found._properties.update({"size": str(len(held[0])), "generation": str(held[1])})
        return found

    def copy_blob(self, blob: storage.Blob, destination_bucket: Any, new_name: str | None = None,
                  client: Any = None, preserve_acl: bool = True, source_generation: int | None = None,
                  if_generation_match: int | None = None, **_kwargs: Any) -> MemoryBlob:
        held = self.objects.get(blob.name)
        if held is None or (source_generation is not None and held[1] != source_generation):
            raise gexc.NotFound(blob.name)
        target = new_name or blob.name
        if if_generation_match == 0 and target in self.objects:
            raise gexc.PreconditionFailed(target)
        self.put(target, held[0])
        return MemoryBlob(target, self)


def gcs_bucket() -> tuple[pc.GcsPluginBucket, MemoryBucket]:
    memory = MemoryBucket()
    plugins = pc.GcsPluginBucket(memory.name, "example-project")
    plugins._bucket = memory  # the client GcsPluginBucket builds lazily, already built
    return plugins, memory


# --- the job, as deployed -----------------------------------------------------


def plugin_archive(name: str = "lint-kit") -> bytes:
    """A minimal valid plugin, as GitHub's tarball wraps it: one top folder."""
    return tar_gz([
        folder("example-org-lint-kit-1a2b3c4/"),
        file("example-org-lint-kit-1a2b3c4/.claude-plugin/plugin.json",
             json.dumps({"name": name, "version": "1.0.0", "description": "Lints."})),
        file("example-org-lint-kit-1a2b3c4/skills/lint/SKILL.md",
             "---\nname: lint\ndescription: Lints the tree.\n---\nRun the linter.\n"),
    ])


def run_job(memory: MemoryBucket, intake: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The job's `main` over the intake's quarantined archive; what it wrote
    lands in the plugins bucket, where the bucket notification points."""
    # `main` walls off the process's sockets for good; the test's own HTTP
    # client needs them, so the wall is the only thing left out.
    monkeypatch.setattr(nonet, "install", lambda: None)
    intake_id = intake["intake_id"]
    objects = JobBucket({f"quarantine/{intake_id}": plugin_archive()})
    env = {"SWARM_INTAKE_ID": intake_id, "SWARM_INTAKE_SCOPE": intake["scope"], "PLUGINS_BUCKET": memory.name}
    if intake.get("tenant_id"):
        env["SWARM_INTAKE_TENANT"] = intake["tenant_id"]
    assert job.main(env, objects) == 0
    written = {name: data for name, data in objects.objects.items() if name.startswith("staging/")}
    assert sorted(written) == [f"staging/{intake_id}/report.json", f"staging/{intake_id}/runnable.tar"]
    for name, data in written.items():
        memory.put(name, data)
    return written


@pytest.fixture
def gcs(world):  # noqa: F811 -- the routes' fixture, imported above
    plugins, memory = gcs_bucket()
    world["client"].app.state.plugin_bucket = plugins
    world["memory"] = memory
    return world


def start(w: dict[str, Any]) -> dict[str, Any]:
    started = github_install(w)
    assert started.status_code == 202, started.text
    return started.json()


def status(w: dict[str, Any], intake_id: str):
    return w["client"].get(f"{PLATFORM}/intakes/{intake_id}", headers=auth_header("root"))


# --- the job's report, finished --------------------------------------------------


def test_the_callback_pins_what_the_job_wrote(gcs, monkeypatch, caplog):
    w = gcs
    intake = start(w)
    written = run_job(w["memory"], intake, monkeypatch)
    report = json.loads(written[f"staging/{intake['intake_id']}/report.json"])
    tar = written[f"staging/{intake['intake_id']}/runnable.tar"]
    assert report["runnable_sha256"] == hashlib.sha256(tar).hexdigest()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = notify(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"intake_id": intake["intake_id"], "state": ps.INTAKE_PINNED}
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == []
    # Copied under the scope prefix, by its digest, byte for byte.
    assert w["memory"].objects[f"platform/{report['runnable_sha256']}.tar"][0] == tar
    # Staging and quarantine are gone.
    assert not [name for name in w["memory"].objects if name.startswith(("staging/", "quarantine/"))]
    [entry] = w["client"].get(PLATFORM, headers=auth_header("root")).json()["installs"]
    assert entry["runnable_sha256"] == report["runnable_sha256"]


def test_the_consoles_status_read_pins_what_the_job_wrote(gcs, monkeypatch):
    w = gcs
    intake = start(w)
    run_job(w["memory"], intake, monkeypatch)
    response = status(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json()["state"] == ps.INTAKE_PINNED
    assert response.json()["refusal"] is None


def test_an_object_already_pinned_is_compared_by_its_digest(gcs, monkeypatch):
    w = gcs
    intake = start(w)
    written = run_job(w["memory"], intake, monkeypatch)
    tar = written[f"staging/{intake['intake_id']}/runnable.tar"]
    # The same bytes from an earlier intake in the same scope.
    w["memory"].put(f"platform/{hashlib.sha256(tar).hexdigest()}.tar", tar)
    response = notify(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json()["state"] == ps.INTAKE_PINNED


# --- a report swarm-api cannot use ---------------------------------------------


def _bad_report(w: dict[str, Any], intake: dict[str, Any], raw: bytes) -> None:
    w["memory"].put(f"staging/{intake['intake_id']}/report.json", raw)


def test_a_report_too_deeply_nested_to_parse_refuses_the_intake_and_the_callback_answers_200(gcs, caplog):
    w = gcs
    intake = start(w)
    _bad_report(w, intake, b"[" * 200_000)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = notify(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"intake_id": intake["intake_id"], "state": ps.INTAKE_REFUSED}
    seen = status(w, intake["intake_id"])
    assert seen.status_code == 200, seen.text
    assert seen.json()["refusal"]["code"] == "report_invalid"
    assert seen.json()["refusal"]["sentence"] == ps.REFUSALS["report_invalid"]
    # Delivered again, it is not processed again.
    again = notify(w, intake["intake_id"])
    assert again.status_code == 200 and again.json()["ignored"] is True


def test_a_report_the_status_read_cannot_use_is_a_refusal_not_a_500(gcs):
    w = gcs
    intake = start(w)
    _bad_report(w, intake, b"{" * 200_000)
    seen = status(w, intake["intake_id"])
    assert seen.status_code == 200, seen.text
    assert seen.json()["state"] == ps.INTAKE_REFUSED
    assert seen.json()["refusal"]["code"] == "report_invalid"


def test_a_report_whose_version_cannot_be_written_refuses_the_intake_and_logs_its_reason(gcs, monkeypatch, caplog):
    w = gcs
    intake = start(w)
    run_job(w["memory"], intake, monkeypatch)
    marker = "report-text-" + secrets.token_hex(6)

    def unusable(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError(marker)

    monkeypatch.setattr(ps.PluginStore, "pin", unusable)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = notify(w, intake["intake_id"])
    assert response.status_code == 200, response.text
    assert response.json()["state"] == ps.INTAKE_REFUSED
    assert status(w, intake["intake_id"]).json()["refusal"]["code"] == "report_invalid"
    [line] = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert "reason=report_invalid" in line and "ValueError" in line
    # The exception's message can carry the report's text: never logged.
    assert marker not in caplog.text


# --- a bucket swarm-api cannot read --------------------------------------------


def test_a_storage_error_answers_503_so_pubsub_retries_and_logs_its_reason(gcs, monkeypatch, caplog):
    w = gcs
    intake = start(w)
    run_job(w["memory"], intake, monkeypatch)
    w["memory"].read_error = gexc.ServiceUnavailable("storage-text-" + secrets.token_hex(6))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        response = notify(w, intake["intake_id"])
    assert response.status_code == 503, response.text
    [line] = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert f"intake_id={intake['intake_id']}" in line
    assert "reason=storage_unavailable" in line and "ServiceUnavailable" in line
    assert "storage-text-" not in caplog.text
    # Still running, nothing cleaned: the redelivery finishes it.
    seen = status(w, intake["intake_id"])
    assert seen.status_code == 503, seen.text
    assert seen.json()["code"] == "upstream_unavailable"
    assert f"staging/{intake['intake_id']}/report.json" in w["memory"].objects
    w["memory"].read_error = None
    retried = notify(w, intake["intake_id"])
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == ps.INTAKE_PINNED
