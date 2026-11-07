"""The Cloud Run job's entry point: one execution, one intake.

The execution reads and writes only files. The plugins bucket is mounted as a
Cloud Storage volume at SWARM_INTAKE_MOUNT (lane 3's job definition), so the
program itself never opens a socket and holds no credential: the mount's own
identity gets `quarantine/<intake_id>` and creates `staging/<intake_id>/...`.

The execution's environment, set by swarm-api for that one execution:

* SWARM_INTAKE_ID: the 128-bit intake id swarm-api minted (32 lowercase hex);
* SWARM_INTAKE_SCOPE: `platform` or `workspace`; SWARM_INTAKE_TENANT for a
  workspace intake;
* SWARM_INTAKE_SUBDIR: the plugin's folder inside the archive, when asked for;
* SWARM_INTAKE_OVERLAY: the admin's overlay as JSON (§6.3), when there is one.

Writes `staging/<intake_id>/runnable.tar` (pinned only), then
`staging/<intake_id>/report.json`, each created exclusively, never overwritten.
Exit 0 once a report is written, 1 when the job failed and wrote
`intake_internal_error`, 2 when its environment is not a valid intake.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import limits, nonet
from .intake import binding, refused_report, run_intake
from .refusals import Refusal

_INTAKE_ID = re.compile(r"^[0-9a-f]{32}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DEFAULT_MOUNT = "/plugins"


def _log(intake_id: str, outcome: str, code: str | None) -> None:
    # Ids the platform minted and fixed codes only: never an upstream string.
    print(json.dumps({"intake_id": intake_id, "outcome": outcome, "code": code}, sort_keys=True), flush=True)


def _write(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)


def _encode(report: dict) -> bytes:
    return (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("ascii")


def main(environ: dict[str, str] | None = None) -> int:
    nonet.install()
    env = os.environ if environ is None else environ
    intake_id = env.get("SWARM_INTAKE_ID", "")
    scope = env.get("SWARM_INTAKE_SCOPE", "")
    tenant = env.get("SWARM_INTAKE_TENANT") or None
    if not _INTAKE_ID.match(intake_id) or scope not in ("platform", "workspace"):
        print(json.dumps({"outcome": "invalid_environment"}), file=sys.stderr, flush=True)
        return 2
    if (scope == "workspace") != (tenant is not None) or (tenant is not None and not _TENANT.match(tenant)):
        print(json.dumps({"outcome": "invalid_environment"}), file=sys.stderr, flush=True)
        return 2

    mount = Path(env.get("SWARM_INTAKE_MOUNT", DEFAULT_MOUNT))
    source = mount / "quarantine" / intake_id
    staging = mount / "staging" / intake_id
    bound = binding(intake_id, scope, tenant)
    try:
        size = source.stat().st_size
        if size > limits.MAX_PACKED_BYTES:
            # Refused before a byte is read.
            report, runnable = refused_report(bound, Refusal("archive_too_large"), source_sha256=None, source_bytes=size), None
        else:
            with open(source, "rb") as handle:
                packed = handle.read(limits.MAX_PACKED_BYTES + 1)
            report, runnable = run_intake(
                packed,
                intake_id=intake_id,
                scope=scope,
                tenant_id=tenant,
                subdir=env.get("SWARM_INTAKE_SUBDIR", ""),
                overlay=env.get("SWARM_INTAKE_OVERLAY", ""),
            )
        staging.mkdir(parents=True, exist_ok=True)
        if runnable is not None:
            _write(staging / "runnable.tar", runnable)
        _write(staging / "report.json", _encode(report))
    except Exception:  # noqa: BLE001 -- the job's last word is a fixed code, never a traceback
        try:
            staging.mkdir(parents=True, exist_ok=True)
            failed = refused_report(bound, Refusal("intake_internal_error"), source_sha256=None, source_bytes=None)
            _write(staging / "report.json", _encode(failed))
        except OSError:
            pass
        _log(intake_id, "failed", "intake_internal_error")
        return 1
    refusal = report.get("refusal")
    _log(intake_id, report["outcome"], refusal["code"] if refusal else None)
    return 0
