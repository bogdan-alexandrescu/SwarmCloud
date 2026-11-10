"""The job reads its archive from the plugins bucket and writes its report there.

Live QA finding, 10 November 2026: every install stayed "Installing..." because
the job read `quarantine/<intake_id>` from a mount path the deployed job never
had, and wrote its failure report into its own container. The job now uses the
Cloud Storage API with PLUGINS_BUCKET (bucket.py): one GET of the quarantine
object, creates under staging/. These run main() with the bucket faked: no
credentials, no emulator, no socket.
"""

from __future__ import annotations

import io
import json
import secrets
import socket

import pytest
from fake_bucket import FakeBucket
from intake_archives import file, tar_gz
from plugin_intake import bucket as gcs
from plugin_intake import limits
from plugin_intake import main as job

PLUGIN = [
    file("kit-0123abc/.claude-plugin/plugin.json", json.dumps({"name": "kit", "version": "1.0.0", "description": "A kit."})),
    file("kit-0123abc/skills/a/SKILL.md", "---\nname: a\ndescription: A.\n---\n"),
]


def environ(intake_id: str, **extra: str) -> dict[str, str]:
    return {"SWARM_INTAKE_ID": intake_id, "SWARM_INTAKE_SCOPE": "platform", "PLUGINS_BUCKET": "example-plugins", **extra}


def log_line(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


# --- main() against the bucket --------------------------------------------------------


def test_the_job_reads_its_quarantine_object_and_writes_its_report_in_the_bucket(capsys):
    intake_id = secrets.token_hex(16)
    bucket = FakeBucket({f"quarantine/{intake_id}": tar_gz(PLUGIN)})
    assert job.main(environ(intake_id), bucket=bucket) == 0
    assert bucket.reads == [(f"quarantine/{intake_id}", limits.MAX_PACKED_BYTES)]
    assert [name for name, _ in bucket.creates] == [f"staging/{intake_id}/runnable.tar", f"staging/{intake_id}/report.json"]
    report = json.loads(bucket.objects[f"staging/{intake_id}/report.json"])
    assert report["outcome"] == "pinned" and report["intake_id"] == intake_id
    assert log_line(capsys) == {"intake_id": intake_id, "outcome": "pinned", "code": None}


def test_a_refusal_creates_only_the_report():
    intake_id = secrets.token_hex(16)
    bucket = FakeBucket({f"quarantine/{intake_id}": b"not an archive"})
    assert job.main(environ(intake_id), bucket=bucket) == 0
    assert [name for name, _ in bucket.creates] == [f"staging/{intake_id}/report.json"]
    assert json.loads(bucket.objects[f"staging/{intake_id}/report.json"])["refusal"]["code"] == "archive_format_unknown"


def test_an_archive_over_the_cap_is_refused_from_its_size():
    intake_id = secrets.token_hex(16)
    bucket = FakeBucket({f"quarantine/{intake_id}": b"\0" * (limits.MAX_PACKED_BYTES + 1)})
    assert job.main(environ(intake_id), bucket=bucket) == 0
    report = json.loads(bucket.objects[f"staging/{intake_id}/report.json"])
    assert report["refusal"]["code"] == "archive_too_large" and report["source_bytes"] == limits.MAX_PACKED_BYTES + 1


def test_no_bucket_name_is_an_invalid_environment():
    environ_ = environ(secrets.token_hex(16))
    del environ_["PLUGINS_BUCKET"]
    assert job.main(environ_) == 2


# --- the failure log ------------------------------------------------------------------


def test_the_failure_log_names_the_exception_class_never_its_message(capsys, monkeypatch):
    intake_id = secrets.token_hex(16)
    marker = "archive-text-" + secrets.token_hex(8)

    class ParserBroke(Exception):
        pass

    def broken(*_args, **_kwargs):
        raise ParserBroke(marker)

    monkeypatch.setattr(job, "run_intake", broken)
    bucket = FakeBucket({f"quarantine/{intake_id}": tar_gz(PLUGIN)})
    assert job.main(environ(intake_id), bucket=bucket) == 1
    out = capsys.readouterr().out
    assert marker not in out and "Traceback" not in out
    line = json.loads(out.strip())
    assert line == {"intake_id": intake_id, "outcome": "failed", "code": "intake_internal_error", "error": "ParserBroke", "report": "written"}
    assert json.loads(bucket.objects[f"staging/{intake_id}/report.json"])["refusal"]["code"] == "intake_internal_error"


def test_a_missing_quarantine_object_is_named_in_the_log(capsys):
    intake_id = secrets.token_hex(16)
    bucket = FakeBucket()
    assert job.main(environ(intake_id), bucket=bucket) == 1
    assert log_line(capsys)["error"] == "ObjectNotFound"


def test_a_report_the_bucket_refuses_is_said_in_the_log(capsys):
    intake_id = secrets.token_hex(16)
    bucket = FakeBucket({f"quarantine/{intake_id}": tar_gz(PLUGIN)}, create_error=gcs.AccessDenied())
    assert job.main(environ(intake_id), bucket=bucket) == 1
    line = log_line(capsys)
    assert line["code"] == "intake_internal_error"
    assert line["error"] == "AccessDenied" and line["report"] == "unwritten" and line["report_error"] == "AccessDenied"


# --- the Cloud Storage calls themselves, against a fake connection ---------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def getheader(self, name: str, default=None):
        return self._headers.get(name.lower(), default)


class FakeConnection:
    def __init__(self, host: str, calls: list, responses: dict) -> None:
        self.host = host
        self.calls = calls
        self.responses = responses

    def request(self, method, path, body=None, headers=None):
        self.calls.append((self.host, method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.responses[self.host].pop(0)

    def close(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    token = secrets.token_hex(16)
    calls: list = []
    responses = {gcs.METADATA_HOST: [FakeResponse(200, json.dumps({"access_token": token}).encode())], gcs.STORAGE_HOST: []}
    monkeypatch.setattr(gcs, "_connection", lambda host: FakeConnection(host, calls, responses))
    return token, calls, responses


def test_the_read_is_one_get_of_the_named_object(wire):
    token, calls, responses = wire
    intake_id = secrets.token_hex(16)
    responses[gcs.STORAGE_HOST].append(FakeResponse(200, b"bytes", {"x-goog-stored-content-length": "5"}))
    assert gcs.Bucket("example-plugins").read(f"quarantine/{intake_id}", 100) == (b"bytes", 5)
    (meta, get) = calls
    assert meta[:3] == (gcs.METADATA_HOST, "GET", "/computeMetadata/v1/instance/service-accounts/default/token")
    assert meta[4]["Metadata-Flavor"] == "Google"
    assert get[:3] == (gcs.STORAGE_HOST, "GET", f"/storage/v1/b/example-plugins/o/quarantine%2F{intake_id}?alt=media")
    assert get[4]["Authorization"] == f"Bearer {token}"


def test_an_object_over_the_cap_is_not_read(wire):
    _token, _calls, responses = wire
    big = FakeResponse(200, b"x" * 50, {"x-goog-stored-content-length": "50"})
    responses[gcs.STORAGE_HOST].append(big)
    assert gcs.Bucket("example-plugins").read("quarantine/x", 10) == (None, 50)
    assert big.read() == b"x" * 50  # nothing was consumed


def test_a_create_never_overwrites(wire):
    _token, calls, responses = wire
    intake_id = secrets.token_hex(16)
    responses[gcs.STORAGE_HOST].append(FakeResponse(200, b"{}"))
    gcs.Bucket("example-plugins").create(f"staging/{intake_id}/report.json", b"{}", "application/json")
    post = calls[-1]
    assert post[1] == "POST"
    assert post[2] == f"/upload/storage/v1/b/example-plugins/o?uploadType=media&ifGenerationMatch=0&name=staging%2F{intake_id}%2Freport.json"
    assert post[3] == b"{}"


@pytest.mark.parametrize(("status", "error"), [(404, gcs.ObjectNotFound), (403, gcs.AccessDenied), (401, gcs.AccessDenied), (412, gcs.ObjectExists), (503, gcs.StorageUnavailable)])
def test_a_status_becomes_a_named_class_without_the_token(wire, status, error):
    token, _calls, responses = wire
    responses[gcs.STORAGE_HOST].append(FakeResponse(status, b"upstream text"))
    with pytest.raises(error) as raised:
        gcs.Bucket("example-plugins").create("staging/x/report.json", b"{}", "application/json")
    assert token not in repr(raised.value) and "upstream" not in str(raised.value)


def test_only_the_two_hosts_can_be_reached():
    for address in (("192.0.2.1", 443), ("example.invalid", 443), (gcs.STORAGE_HOST, 80), (gcs.METADATA_HOST, 443)):
        with pytest.raises(gcs.HostRefused):
            gcs._connect(address)


def test_the_guard_still_refuses_every_other_socket():
    from plugin_intake import nonet

    nonet.install()
    with pytest.raises(nonet.NetworkForbidden):
        socket.create_connection(("192.0.2.1", 443))
