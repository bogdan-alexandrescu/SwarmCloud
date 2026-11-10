"""The Cloud Run job's entry point: one execution, one intake.

The execution reads its archive from the plugins bucket and writes its result
back there, through the Cloud Storage JSON API (`bucket.py`): one object GET of
`quarantine/<intake_id>`, then object creates under `staging/<intake_id>/`,
exactly what its account is granted (get under quarantine/, create under
staging/, no list, no delete; docs/design/managed-plugins.md §2.2-§2.3). The
bucket's name is PLUGINS_BUCKET, set on the job by terraform/infra/plugins.tf.
Every other socket the program could open is refused (`nonet`), and the job's
network reaches nothing but Google's APIs.

The execution's environment, set by swarm-api for that one execution:

* SWARM_INTAKE_ID: the 128-bit intake id swarm-api minted (32 lowercase hex);
* SWARM_INTAKE_SCOPE: `platform` or `workspace`; SWARM_INTAKE_TENANT for a
  workspace intake;
* SWARM_INTAKE_SUBDIR: the plugin's folder inside the archive, when asked for;
* SWARM_INTAKE_OVERLAY: the admin's overlay as JSON (§6.3), when there is one.

Writes `staging/<intake_id>/runnable.tar` (pinned only), then
`staging/<intake_id>/report.json`, each created exclusively, never overwritten.
Exit 0 once a report is written, 1 when the job failed (its log line names the
exception's class, and says whether the `intake_internal_error` report reached
the bucket), 2 when its environment is not a valid intake.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Protocol

from . import limits, nonet
from .bucket import Bucket
from .intake import binding, refused_report, run_intake
from .refusals import Refusal

_INTAKE_ID = re.compile(r"^[0-9a-f]{32}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# A bucket name as Cloud Storage allows it (dots included), nothing that could
# turn into another path.
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")


class Objects(Protocol):
    """What the job does with the bucket: `bucket.Bucket`, or a test's fake."""

    def read(self, name: str, cap: int) -> tuple[bytes | None, int]: ...

    def create(self, name: str, data: bytes, content_type: str) -> None: ...


def _log(intake_id: str, outcome: str, code: str | None, **extra: str) -> None:
    # Ids the platform minted, fixed codes and exception class names only:
    # never an exception's message or a traceback, which can carry archive text.
    print(json.dumps({"intake_id": intake_id, "outcome": outcome, "code": code, **extra}, sort_keys=True), flush=True)


def _encode(report: dict) -> bytes:
    return (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("ascii")


def main(environ: dict[str, str] | None = None, bucket: Objects | None = None) -> int:
    env = os.environ if environ is None else environ
    intake_id = env.get("SWARM_INTAKE_ID", "")
    scope = env.get("SWARM_INTAKE_SCOPE", "")
    tenant = env.get("SWARM_INTAKE_TENANT") or None
    bucket_name = env.get("PLUGINS_BUCKET", "")
    if not _INTAKE_ID.match(intake_id) or scope not in ("platform", "workspace"):
        print(json.dumps({"outcome": "invalid_environment"}), file=sys.stderr, flush=True)
        return 2
    if (scope == "workspace") != (tenant is not None) or (tenant is not None and not _TENANT.match(tenant)):
        print(json.dumps({"outcome": "invalid_environment"}), file=sys.stderr, flush=True)
        return 2
    if bucket is None and not _BUCKET.match(bucket_name):
        print(json.dumps({"outcome": "invalid_environment"}), file=sys.stderr, flush=True)
        return 2
    objects: Objects = bucket if bucket is not None else Bucket(bucket_name)
    # After the bucket client is built: from here on only bucket.py's two hosts
    # can be reached, and nothing else in the process opens a socket.
    nonet.install()

    source = f"quarantine/{intake_id}"
    staging = f"staging/{intake_id}"
    bound = binding(intake_id, scope, tenant)
    try:
        packed, size = objects.read(source, limits.MAX_PACKED_BYTES)
        if packed is None:
            # Refused before the archive's bytes are read.
            report, runnable = refused_report(bound, Refusal("archive_too_large"), source_sha256=None, source_bytes=size), None
        else:
            report, runnable = run_intake(
                packed,
                intake_id=intake_id,
                scope=scope,
                tenant_id=tenant,
                subdir=env.get("SWARM_INTAKE_SUBDIR", ""),
                overlay=env.get("SWARM_INTAKE_OVERLAY", ""),
            )
        if runnable is not None:
            objects.create(f"{staging}/runnable.tar", runnable, "application/x-tar")
        objects.create(f"{staging}/report.json", _encode(report), "application/json")
    except Exception as error:  # noqa: BLE001 -- the job's last word is a fixed code and a class name, never a traceback
        written = {"report": "written"}
        try:
            failed = refused_report(bound, Refusal("intake_internal_error"), source_sha256=None, source_bytes=None)
            objects.create(f"{staging}/report.json", _encode(failed), "application/json")
        except Exception as unwritten:  # noqa: BLE001 -- said in the log line, so the failure shows without a report
            written = {"report": "unwritten", "report_error": type(unwritten).__name__}
        _log(intake_id, "failed", "intake_internal_error", error=type(error).__name__, **written)
        return 1
    refusal = report.get("refusal")
    _log(intake_id, report["outcome"], refusal["code"] if refusal else None)
    return 0
