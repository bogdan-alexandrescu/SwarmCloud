#!/usr/bin/env python3
"""A task's agent queries its caller's registered MCP server: #1433's four checks, on a deployed environment.

docs/design/registered-mcp-servers.md §6, lane MCP-ACCEPT, and owner decision
Q10. `scripts/acceptance/groups/mcp.sh` submits, waits and saves what the task
left into one directory; this file MINTS the per-run credential and the
canary, BUILDS the two request bodies (so neither value is ever a shell
variable or a process argument), and JUDGES the directory:

  credential_seen       a claude-code task names the registered echo server
                        (tests/acceptance/mcp_echo_server/server.py) with a
                        per-run credential; its `whoami` answer carries the
                        sha256 of the credential the server received, which
                        the agent cannot compute because it never holds the
                        value. The answer names this task and one of its
                        attempts (the relay's own headers), the relay logged
                        `mcp_call tools/call whoami` with status 200, and
                        swarm-api recorded the attach.
  unregistered_refused  the same dispatch naming a server nobody registered is
                        refused `mcp_server_unregistered` at submission, so no
                        task exists to admit. The probe carries a reserved
                        metadata key (lib.sh acc_door): a door that let the
                        name through is refused for the metadata instead, and
                        nothing is created either way.
  no_leak               the credential is in no log route (the runner's and
                        the agent's four streams of every attempt, and the
                        transcript), no artifact, no checkpoint archive, and
                        no event or Firestore document of the task. CONTROL:
                        a canary minted exactly as the credential is, which
                        the prompt asks the agent to print, write to an
                        artifact and write into the workspace, IS found by
                        the same search in every one of those surfaces -- so
                        a search that is blind to a surface (redacted, not
                        read, or empty) fails instead of passing.
  worker_gone           after the attempt, every attempt recorded its end and
                        every lease is released (the worker exits at the end
                        of every attempt and takes the relay and its copy of
                        the credential with it, design §4.7 step 6), and the
                        final checkpoint -- what survives of the workspace --
                        holds no copy, holds the canary file (the control),
                        and holds no relay file (`private/` is never
                        checkpointed).

WHY THE CANARY IS SHAPED LIKE THE CREDENTIAL. Every text read here is redacted
at read time by swarm-api. A credential that redaction would mask could pass
this search while being stored in the clear. Both values are
`secrets.token_hex(24)`, which no redaction rule matches, so the canary being
found through a route proves that route would have shown the credential too.
What this does NOT read: Cloud Logging (swarm-verify holds no logging read in
a project it shares with another team), and Secret Manager (the credential is
meant to be there until the sweep, §4.4).

STANDARD LIBRARY ONLY: it runs in the swarm-verify image (alpine, python3 with
no pip). No function here is named `test_*`: this is a program the suite
runs, not a pytest module (pyproject's testpaths is tests/unit).

    test_caller_mcp.py mint --dir DIR
    test_caller_mcp.py body --dir DIR --kind task|door --server NAME --run-id ID
                            --metadata JSON [--repository-url URL --ref REF]
    test_caller_mcp.py judge --dir DIR --results FILE
    test_caller_mcp.py selftest

`judge` writes one JSON line per check, in CHECKS order:
`{"check": key, "verdict": "PASS"|"FAIL"|"SKIP", "reason": "..."}`. No reason
ever holds the credential or the canary; a finding names the surface and the
item, never the value. `selftest` runs the echo server on loopback and the
judge over evidence it builds, and exits non-zero when any check fails to
catch the defect it exists for.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: The checks, in the order mcp.sh lists them.
CHECKS = ("credential_seen", "unregistered_refused", "no_leak", "worker_gone")

PROFILE = "claude-code"
#: The echo server's one tool and its report's fields (server.py).
TOOL = "whoami"
ANSWER_ARTIFACT = "mcp-answer.json"
CANARY_FILE = "mcp-canary.txt"
#: The worker's relay file (agent_worker.mcp_attach.RELAY_FILE): under
#: `private/`, never checkpointed. Its presence in a checkpoint is a defect.
RELAY_FILE = "mcp-relay.json"
#: How long the per-run credential lives (§4.3: 60-86400 s). Long enough for
#: the suite's admission wait and timeout, short enough to die with the run.
EXPIRES_IN = 3600
#: The reserved metadata key that makes the door probe create nothing
#: (lib.sh acc_door; swarm-api checks `input.mcp_servers` before metadata).
DOOR_METADATA = {"expected_outputs": ["acceptance-door-probe.txt"]}

LOG_STREAMS = ("stdout", "stderr", "agent_stdout", "agent_stderr")
#: Surfaces the no_leak search reads, each of which must show the canary.
SURFACES = ("logs", "artifacts", "checkpoints", "events")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")
_HELD = ("PARKED:", "HELD:", "UPSTREAM:")
_UNREGISTERED_PREFIX = "accnoreg-"


@dataclass
class Verdict:
    check: str
    verdict: str
    reason: str

    def line(self) -> str:
        return json.dumps({"check": self.check, "verdict": self.verdict, "reason": self.reason})


# ---------------------------------------------------------------------------
# Mint and build: the values never leave files this process writes 0600.
# ---------------------------------------------------------------------------

def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def mint(base: Path) -> dict[str, str]:
    """The run's values. The credential and the canary are minted the same
    way (see the module's note); the door probe gets its own credential so the
    one under test is sent exactly once, to the one task that may use it."""
    values = {
        "credential": secrets.token_hex(24),
        "canary": secrets.token_hex(24),
        "door_credential": secrets.token_hex(24),
        "unregistered": _UNREGISTERED_PREFIX + secrets.token_hex(4),
    }
    _write_private(base / "secrets.json", json.dumps(values))
    return values


def prompt(server: str, run_id: str, canary: str) -> str:
    return (
        f"This task checks a registered MCP server. 1. Call the tool `{TOOL}` of the MCP server "
        f"`{server}` exactly once, with the argument nonce set to `{run_id}`. 2. Write the tool's "
        f"text result, exactly as you received it, to $SWARM_ARTIFACTS_DIR/{ANSWER_ARTIFACT}. "
        f"3. Write the single line {canary} to a new file {CANARY_FILE} at the root of the "
        f"repository, and the same line to $SWARM_ARTIFACTS_DIR/{CANARY_FILE}. 4. End your reply "
        f"with the line `canary: {canary}`. Change no other file, do not commit and do not run "
        f"anything else. If the server or its tool is not available to you, write the single word "
        f"unavailable to $SWARM_ARTIFACTS_DIR/{ANSWER_ARTIFACT} and stop."
    )


def body(values: dict[str, str], *, kind: str, server: str, run_id: str,
         metadata: dict[str, Any], repository_url: str = "", ref: str = "") -> dict[str, Any]:
    """POST /v1/tasks for the real task (`task`) or the door probe (`door`)."""
    if kind == "door":
        name = values["unregistered"]
        return {
            "runner_profile": PROFILE,
            "input": {"prompt": "acceptance door probe", "mcp_servers": [name]},
            "mcp_credentials": {name: {"value": values["door_credential"],
                                       "expires_in": EXPIRES_IN}},
            "metadata": {**metadata, **DOOR_METADATA},
        }
    task: dict[str, Any] = {
        "runner_profile": PROFILE,
        "input": {"prompt": prompt(server, run_id, values["canary"]), "mcp_servers": [server]},
        "mcp_credentials": {server: {"value": values["credential"], "expires_in": EXPIRES_IN}},
        "metadata": metadata,
        "priority": 10,
        "max_attempts": 1,
    }
    if repository_url:
        task["repository_url"] = repository_url
        task["repository_ref"] = ref or "main"
    return task


# ---------------------------------------------------------------------------
# Reading the evidence. An absent file is UNREAD, never clean.
# ---------------------------------------------------------------------------

def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read_json(path: Path) -> Any:
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def _read_text(path: Path) -> str:
    raw = _read_bytes(path)
    return "" if raw is None else raw.decode("utf-8", errors="replace").strip()


def _jsonl(path: Path) -> list[Any]:
    raw = _read_bytes(path)
    out: list[Any] = []
    for line in (raw or b"").decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(None)
    return out


@dataclass
class Blob:
    surface: str
    item: str
    data: bytes


@dataclass
class Gathered:
    blobs: list[Blob]
    unread: list[str]


def _attempts(base: Path) -> list[dict[str, Any]]:
    doc = _read_json(base / "attempts.json")
    rows = doc.get("attempts") if isinstance(doc, dict) else doc
    return [r for r in rows or [] if isinstance(r, dict)]


def _gather_logs(base: Path, attempt_ids: list[str]) -> Gathered:
    blobs: list[Blob] = []
    unread: list[str] = []
    if not attempt_ids:
        unread.append("logs: no attempt to read logs for")
    for attempt in attempt_ids:
        for stream in LOG_STREAMS:
            path = base / "logs" / f"{attempt}.{stream}.jsonl"
            pages = _jsonl(path)
            if not pages:
                unread.append(f"logs: {attempt} {stream} was not read")
                continue
            content = []
            for page in pages:
                status = page.get("status") if isinstance(page, dict) else None
                if status == "ok":
                    content.append(str(page.get("content") or ""))
                elif status in ("absent", "not_applicable"):
                    continue
                else:
                    unread.append(f"logs: {attempt} {stream} answered {status or 'nothing'}")
            blobs.append(Blob("logs", f"{attempt} {stream}", "".join(content).encode("utf-8")))
        transcript = _read_bytes(base / "logs" / f"transcript.{attempt}.json")
        if transcript is None:
            unread.append(f"logs: {attempt} transcript was not read")
        else:
            blobs.append(Blob("logs", f"{attempt} transcript", transcript))
    return Gathered(blobs, unread)


def _gather_artifacts(base: Path) -> Gathered:
    blobs: list[Blob] = []
    unread: list[str] = []
    listing = _read_json(base / "artifacts" / "names.json")
    if not isinstance(listing, list):
        return Gathered([], ["artifacts: the artifact list was not read"])
    for row in listing:
        name = str(row.get("name")) if isinstance(row, dict) else "?"
        data = _read_bytes(base / "artifacts" / str(row.get("file", ""))) \
            if isinstance(row, dict) and row.get("http") == "200" else None
        if data is None:
            unread.append(f"artifacts: {name} was not downloaded "
                          f"(HTTP {row.get('http') if isinstance(row, dict) else '?'})")
            continue
        blobs.append(Blob("artifacts", name, data))
    if not listing:
        unread.append("artifacts: the task listed none")
    return Gathered(blobs, unread)


@dataclass
class Checkpoint:
    checkpoint_id: str
    attempt_id: str
    order: tuple[str, int]
    members: dict[str, bytes] | None
    problem: str | None


def _checkpoints(base: Path) -> list[Checkpoint]:
    listing = _read_json(base / "checkpoints.json")
    rows = listing.get("checkpoints") if isinstance(listing, dict) else None
    out: list[Checkpoint] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("checkpoint_id"):
            continue
        cid, attempt = str(row["checkpoint_id"]), str(row.get("attempt_id") or "")
        order = (str(row.get("created_at") or ""), int(row.get("seq") or 0))
        raw = _read_bytes(base / "checkpoints" / f"{cid}.tgz")
        if raw is None:
            out.append(Checkpoint(cid, attempt, order, None, "its archive was not downloaded"))
            continue
        try:
            members: dict[str, bytes] = {}
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
                for member in archive:
                    if member.isfile():
                        handle = archive.extractfile(member)
                        members[member.name] = handle.read() if handle is not None else b""
            out.append(Checkpoint(cid, attempt, order, members, None))
        except (tarfile.TarError, OSError, EOFError) as exc:
            out.append(Checkpoint(cid, attempt, order, None,
                                  f"its archive could not be read ({type(exc).__name__})"))
    return out


def _gather_checkpoints(checkpoints: list[Checkpoint], listed: bool) -> Gathered:
    blobs: list[Blob] = []
    unread: list[str] = []
    if not listed:
        unread.append("checkpoints: the checkpoint list was not read")
    elif not checkpoints:
        unread.append("checkpoints: the task wrote none, so the workspace cannot be judged")
    for cp in checkpoints:
        if cp.members is None:
            unread.append(f"checkpoints: {cp.checkpoint_id} {cp.problem}")
            continue
        for name, data in cp.members.items():
            blobs.append(Blob("checkpoints", f"{cp.checkpoint_id}:{name}", data))
    return Gathered(blobs, unread)


def _gather_events(base: Path) -> Gathered:
    blobs: list[Blob] = []
    unread: list[str] = []
    for name in ("doc.json", "events.json", "attempts.json", "mcp-rows.json"):
        data = _read_bytes(base / name)
        if data is None:
            unread.append(f"events: {name} was not read")
        else:
            blobs.append(Blob("events", name, data))
    return Gathered(blobs, unread)


def _found(blobs: Iterable[Blob], value: str) -> list[Blob]:
    needle = value.encode("ascii")
    return [b for b in blobs if needle in b.data]


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------

@dataclass
class Run:
    base: Path
    values: dict[str, str]
    server: str
    registration: dict[str, Any] | None
    registration_status: str
    submit: dict[str, Any]
    door: dict[str, Any]
    task: str
    state: str


def _load(base: Path) -> Run:
    values = _read_json(base / "secrets.json") or {}
    registration = _read_json(base / "registration.json")
    server = registration.get("server") if isinstance(registration, dict) else None
    submit = _read_json(base / "submit.json")
    door = _read_json(base / "door.json")
    return Run(
        base=base,
        values=values if isinstance(values, dict) else {},
        server=_read_text(base / "server"),
        registration=server if isinstance(server, dict) else None,
        registration_status=_read_text(base / "registration.status"),
        submit=submit if isinstance(submit, dict) else {},
        door=door if isinstance(door, dict) else {},
        task=_read_text(base / "task"),
        state=_read_text(base / "state"),
    )


def _not_measured(run: Run) -> str | None:
    """Why nothing about the task can be judged, or None when it ran."""
    if not run.values.get("credential") or not run.values.get("canary"):
        return "the run's values were never minted"
    if run.registration is None:
        return (f"the smoke tenant has no MCP server named {run.server!r} "
                f"(GET /v1/mcp-servers/{run.server} answered HTTP "
                f"{run.registration_status or '?'}); register the echo server "
                "(terraform output mcp_test_server) as a dispatch server first")
    auth = run.registration.get("auth") if isinstance(run.registration.get("auth"), dict) else {}
    if run.registration.get("enabled") is not True:
        return f"the registration {run.server!r} is disabled"
    if auth.get("mode") != "dispatch":
        return (f"the registration {run.server!r} is auth.mode {auth.get('mode')!r}; "
                "this group sends a per-run credential, so it must be 'dispatch'")
    allowed = run.registration.get("allowed_profiles") or []
    if allowed and PROFILE not in allowed:
        return f"the registration {run.server!r} does not allow {PROFILE}"
    if run.submit.get("code") == "mcp_unavailable":
        return "swarm-api refused the dispatch mcp_unavailable: the feature is dark (SWARM_MCP_SERVERS is not on)"
    if not run.task:
        return None
    if run.state.startswith(_HELD):
        return f"the platform held the task on purpose ({run.state})"
    return None


def _no_task(run: Run) -> Verdict | None:
    """FAIL when the dispatch never became a task for a reason not skipped above."""
    if run.task:
        return None
    return Verdict("", "FAIL", f"the dispatch naming {run.server!r} was refused HTTP "
                   f"{run.submit.get('status', '?')} {run.submit.get('code') or ''}".rstrip())


def _parse_answer(text: str) -> dict[str, Any] | None:
    """The whoami report the agent saved: the whole text, or the first JSON
    object in it that carries the report's key."""
    for candidate in [text, *re.findall(r"\{[^{}]*\}", text)]:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "credential_sha256" in parsed:
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("content"), list):
            for item in parsed["content"]:
                inner = _parse_answer(str(item.get("text", ""))) if isinstance(item, dict) else None
                if inner is not None:
                    return inner
    return None


def _relay_calls(base: Path, attempt_ids: list[str], server: str) -> list[dict[str, Any]]:
    """The relay's `mcp_call` lines for `server`, from the runner's stdout stream."""
    calls = []
    for attempt in attempt_ids:
        for page in _jsonl(base / "logs" / f"{attempt}.stdout.jsonl"):
            if not isinstance(page, dict) or page.get("status") != "ok":
                continue
            for line in str(page.get("content") or "").splitlines():
                start = line.find("{")
                if start < 0:
                    continue
                try:
                    record = json.loads(line[start:])
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("message") == "mcp_call" \
                        and record.get("server") == server:
                    calls.append(record)
    return calls


def check_credential_seen(run: Run) -> Verdict:
    key = "credential_seen"
    skip = _not_measured(run)
    if skip:
        return Verdict(key, "SKIP", f"not measured: {skip}")
    refused = _no_task(run)
    if refused:
        return Verdict(key, "FAIL", refused.reason)
    attempts = _attempts(run.base)
    attempt_ids = [str(a.get("attempt_id")) for a in attempts if a.get("attempt_id")]
    doc = _read_json(run.base / "doc.json") or {}
    if run.state != "SUCCEEDED":
        check = ((doc.get("result_summary") or {}).get("mcp_check")
                 if isinstance(doc, dict) else None)
        detail = f"; mcp_check {json.dumps(check)}" if check else ""
        return Verdict(key, "FAIL", f"the task ended {run.state or 'unsettled'}{detail}")
    answer_path = run.base / "artifacts" / "answer.txt"
    text = _read_text(answer_path)
    if not text:
        return Verdict(key, "FAIL", f"the task saved no {ANSWER_ARTIFACT} artifact")
    if text == "unavailable":
        return Verdict(key, "FAIL", f"the agent reported the server {run.server!r} unavailable")
    report = _parse_answer(text)
    if report is None:
        return Verdict(key, "FAIL", f"{ANSWER_ARTIFACT} holds no {TOOL} report")
    want = hashlib.sha256(run.values["credential"].encode("ascii")).hexdigest()
    if report.get("credential_sha256") != want:
        return Verdict(key, "FAIL", "the server's credential_sha256 is not the dispatched "
                       "credential's: the server did not see the per-run credential")
    if report.get("task") != run.task:
        return Verdict(key, "FAIL", f"the call carried task {report.get('task')!r}, not this "
                       "task's id: it did not come through this task's relay")
    if report.get("attempt") not in attempt_ids:
        return Verdict(key, "FAIL", f"the call carried attempt {report.get('attempt')!r}, "
                       "which is none of this task's attempts")
    calls = [c for c in _relay_calls(run.base, attempt_ids, run.server)
             if c.get("rpc_method") == "tools/call" and c.get("tool") == TOOL]
    if not any(c.get("status") == 200 and not c.get("refused") for c in calls):
        return Verdict(key, "FAIL", f"the relay logged no mcp_call tools/call {TOOL} with "
                       f"status 200 for {run.server!r} ({len(calls)} such line(s) logged)")
    rows = _read_json(run.base / "mcp-rows.json")
    attach = "attach rows unread"
    if isinstance(rows, list):
        mine = [r for r in rows if isinstance(r, dict) and r.get("name") == run.server]
        if not mine:
            return Verdict(key, "FAIL", f"swarm-api recorded no mcp_attach_releases row for "
                           f"{run.server!r} on this task")
        attach = f"{len(mine)} attach row(s), auth_mode {mine[0].get('auth_mode')}"
    echo = report.get("credential_echo")
    scrubbed = "scrubbed" if echo != run.values["credential"] else "NOT scrubbed"
    return Verdict(key, "PASS", f"{run.server} saw the dispatched credential (sha256 matches) on "
                   f"{TOOL} from attempt {report.get('attempt')} through the relay "
                   f"(mcp_call 200; {attach}); its echo of the value reached the agent {scrubbed}")


def check_unregistered_refused(run: Run) -> Verdict:
    key = "unregistered_refused"
    status, code = str(run.door.get("status", "")), str(run.door.get("code") or "")
    if not run.door:
        return Verdict(key, "FAIL", "the door probe was not sent")
    if code == "mcp_unavailable":
        return Verdict(key, "SKIP", "not measured: swarm-api refused mcp_unavailable, the feature "
                       "is dark (SWARM_MCP_SERVERS is not on)")
    if status.startswith("created"):
        return Verdict(key, "FAIL", f"a dispatch naming an unregistered server was ACCEPTED "
                       f"({status}); it was cancelled")
    if status == "422" and code == "mcp_server_unregistered":
        control = (f"; the same dispatch naming {run.server!r} became task {run.task}"
                   if run.task else "")
        return Verdict(key, "PASS", "refused 422 mcp_server_unregistered at submission, before "
                       f"anything was stored or admitted{control}")
    return Verdict(key, "FAIL", f"expected 422 mcp_server_unregistered, got {status} {code}")


def _search(run: Run) -> tuple[dict[str, Gathered], list[Checkpoint]]:
    attempt_ids = [str(a.get("attempt_id")) for a in _attempts(run.base) if a.get("attempt_id")]
    listed = isinstance(_read_json(run.base / "checkpoints.json"), dict)
    checkpoints = _checkpoints(run.base)
    return ({
        "logs": _gather_logs(run.base, attempt_ids),
        "artifacts": _gather_artifacts(run.base),
        "checkpoints": _gather_checkpoints(checkpoints, listed),
        "events": _gather_events(run.base),
    }, checkpoints)


def check_no_leak(run: Run) -> Verdict:
    key = "no_leak"
    skip = _not_measured(run)
    if skip:
        return Verdict(key, "SKIP", f"not measured: {skip}")
    refused = _no_task(run)
    if refused:
        return Verdict(key, "FAIL", refused.reason)
    surfaces, _ = _search(run)
    leaks = [f"{b.surface} {b.item}" for s in surfaces.values()
             for b in _found(s.blobs, run.values["credential"])]
    if leaks:
        return Verdict(key, "FAIL", "the per-run credential is stored in: " + "; ".join(leaks[:8]))
    unread = [u for s in surfaces.values() for u in s.unread]
    if unread:
        return Verdict(key, "FAIL", "the search could not read: " + "; ".join(unread[:8]))
    blind = [name for name in SURFACES
             if not _found(surfaces[name].blobs, run.values["canary"])]
    if blind:
        return Verdict(key, "FAIL", "control failed: the planted canary was not found in "
                       + ", ".join(blind) + ", so a credential there would not have been either")
    counts = ", ".join(f"{name} {len(surfaces[name].blobs)}" for name in SURFACES)
    return Verdict(key, "PASS", f"no credential in {counts} item(s); the canary was found in "
                   "every one of those surfaces")


def check_worker_gone(run: Run) -> Verdict:
    key = "worker_gone"
    skip = _not_measured(run)
    if skip:
        return Verdict(key, "SKIP", f"not measured: {skip}")
    refused = _no_task(run)
    if refused:
        return Verdict(key, "FAIL", refused.reason)
    if run.state not in TERMINAL:
        return Verdict(key, "FAIL", f"the task is {run.state or 'unsettled'}, not terminal")
    attempts = _attempts(run.base)
    if not attempts:
        return Verdict(key, "FAIL", "the task's attempts were not read, or it has none")
    open_ = [str(a.get("attempt_id")) for a in attempts if not a.get("completed_at")]
    if open_:
        return Verdict(key, "FAIL", "attempt(s) never recorded their end: " + ", ".join(open_))
    leases = _read_json(run.base / "leases.json")
    if not isinstance(leases, dict) or leases.get("ok") is not True:
        detail = leases.get("detail") if isinstance(leases, dict) else "they were not read"
        return Verdict(key, "FAIL", f"the task's leases are not all released: {detail}")
    newest = max(attempts, key=lambda a: str(a.get("created_at") or ""))
    last_id = str(newest.get("attempt_id"))
    mine = [cp for cp in _checkpoints(run.base) if cp.attempt_id == last_id]
    if not mine:
        return Verdict(key, "FAIL", f"attempt {last_id} left no checkpoint, so its workspace "
                       "cannot be judged")
    final = max(mine, key=lambda cp: cp.order)
    if final.members is None:
        return Verdict(key, "FAIL", f"checkpoint {final.checkpoint_id}: {final.problem}")
    held = [n for n, d in final.members.items() if run.values["credential"].encode() in d]
    if held:
        return Verdict(key, "FAIL", f"the final checkpoint {final.checkpoint_id} holds the "
                       "credential in: " + ", ".join(held[:8]))
    relay = [n for n in final.members if n.rsplit("/", 1)[-1] == RELAY_FILE]
    if relay:
        return Verdict(key, "FAIL", f"the final checkpoint carries the relay file: {relay[0]}")
    planted = [n for n, d in final.members.items()
               if n.rsplit("/", 1)[-1] == CANARY_FILE and run.values["canary"].encode() in d]
    if not planted:
        return Verdict(key, "FAIL", f"control failed: the final checkpoint {final.checkpoint_id} "
                       f"has no {CANARY_FILE} holding the canary, so it is not the workspace "
                       "the agent wrote")
    codes = ", ".join(str(a.get("exit_code")) for a in attempts)
    return Verdict(key, "PASS", f"{len(attempts)} attempt(s) ended (exit {codes}), "
                   f"{leases.get('detail')}; final checkpoint {final.checkpoint_id} "
                   f"({len(final.members)} file(s)) holds the canary file, no credential and "
                   "no relay file")


def judge(base: str) -> list[Verdict]:
    run = _load(Path(base))
    return [check_credential_seen(run), check_unregistered_refused(run),
            check_no_leak(run), check_worker_gone(run)]


# ---------------------------------------------------------------------------
# selftest: the server on loopback, and every check against its defect.
# ---------------------------------------------------------------------------

def _load_server():
    import importlib.util
    path = Path(__file__).resolve().parent / "mcp_echo_server" / "server.py"
    spec = importlib.util.spec_from_file_location("acceptance_mcp_echo_server", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _post(url: str, payload: Any, headers: dict[str, str]) -> tuple[int, Any]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        **headers})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- loopback
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def _selftest_server(problems: list[str]) -> dict[str, Any]:
    server = _load_server()
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}{server.MCP_PATH}"
    value = secrets.token_hex(24)
    auth = {"Authorization": "Bearer " + value, "X-Swarm-Task": "task_" + "a" * 20,
            "X-Swarm-Attempt": "att_" + "b" * 20}
    report: dict[str, Any] = {}
    try:
        status, _ = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})
        if status != 401:
            problems.append(f"server: a call with no bearer answered {status}, not 401")
        status, init = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "0"}}}, auth)
        if status != 200 or init["result"]["protocolVersion"] != "2025-06-18":
            problems.append(f"server: initialize answered {status} {init}")
        status, _ = _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)
        if status != 202:
            problems.append(f"server: a notification answered {status}, not 202")
        status, listed = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, auth)
        if status != 200 or [t["name"] for t in listed["result"]["tools"]] != [TOOL]:
            problems.append(f"server: tools/list answered {status} {listed}")
        status, called = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": TOOL, "arguments": {"nonce": "n-1"}}}, auth)
        report = json.loads(called["result"]["content"][0]["text"]) if status == 200 else {}
        if report.get("credential_sha256") != hashlib.sha256(value.encode()).hexdigest() \
                or report.get("task") != auth["X-Swarm-Task"] or report.get("nonce") != "n-1":
            problems.append("server: whoami did not report the bearer's hash, the task and the nonce")
        status, other = _post(url, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                    "params": {"name": "rm", "arguments": {}}}, auth)
        if status != 200 or "error" not in other:
            problems.append("server: an unknown tool was not a JSON-RPC error")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return report


def _tgz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _evidence(base: Path, values: dict[str, str], report: dict[str, Any]) -> None:
    """What mcp.sh saves from a working platform: every check PASSES on it."""
    task, attempt, server = report["task"], report["attempt"], "acc-echo"
    canary = values["canary"]
    _write_private(base / "secrets.json", json.dumps(values))
    (base / "server").write_text(server + "\n")
    (base / "registration.status").write_text("200\n")
    (base / "registration.json").write_text(json.dumps({"server": {
        "name": server, "enabled": True, "auth": {"mode": "dispatch"},
        "allowed_profiles": [PROFILE]}}))
    (base / "door.json").write_text(json.dumps({"status": "422", "code": "mcp_server_unregistered"}))
    (base / "submit.json").write_text(json.dumps({"status": "201", "code": None, "task": task}))
    (base / "task").write_text(task + "\n")
    (base / "state").write_text("SUCCEEDED\n")
    (base / "doc.json").write_text(json.dumps({"id": task, "state": "SUCCEEDED",
                                               "input": {"prompt": f"... {canary} ..."}}))
    (base / "events.json").write_text(json.dumps([{"type": "lease_acquired", "lease_id": "l1"}]))
    (base / "attempts.json").write_text(json.dumps({"attempts": [{
        "attempt_id": attempt, "created_at": "2026-10-18T10:00:00Z",
        "completed_at": "2026-10-18T10:05:00Z", "exit_code": 0}]}))
    (base / "mcp-rows.json").write_text(json.dumps([{"name": server, "auth_mode": "dispatch",
                                                     "task_id": task}]))
    (base / "leases.json").write_text(json.dumps({"ok": True, "detail": "1 lease(s) released"}))
    logs = base / "logs"
    logs.mkdir()
    relay_line = json.dumps({"severity": "INFO", "message": "mcp_call", "server": server,
                             "rpc_method": "tools/call", "tool": TOOL, "status": 200,
                             "refused": None})
    contents = {"stdout": relay_line + "\n", "stderr": "",
                "agent_stdout": f"canary: {canary}\n", "agent_stderr": ""}
    for stream, content in contents.items():
        (logs / f"{attempt}.{stream}.jsonl").write_text(json.dumps(
            {"stream": stream, "status": "ok", "content": content, "next_offset": None}) + "\n")
    (logs / f"transcript.{attempt}.json").write_text(json.dumps({"steps": []}))
    scrubbed = {**report, "credential_echo": "********"}
    arts = base / "artifacts"
    arts.mkdir()
    (arts / "answer.txt").write_text(json.dumps(scrubbed))
    (arts / "0.bin").write_text(json.dumps(scrubbed))
    (arts / "1.bin").write_text(canary + "\n")
    (arts / "names.json").write_text(json.dumps([
        {"name": ANSWER_ARTIFACT, "file": "0.bin", "http": "200"},
        {"name": CANARY_FILE, "file": "1.bin", "http": "200"}]))
    (base / "checkpoints.json").write_text(json.dumps({"checkpoints": [
        {"checkpoint_id": "cp-1", "attempt_id": attempt, "created_at": "2026-10-18T10:04:00Z",
         "seq": 1}]}))
    (base / "checkpoints").mkdir()
    (base / "checkpoints" / "cp-1.tgz").write_bytes(_tgz({
        f"work/repo/{CANARY_FILE}": (canary + "\n").encode(),
        "work/.claude-mcp.json": b'{"mcpServers": {}}'}))


def _mutations(values: dict[str, str]) -> list[tuple[str, str, str, Any]]:
    """(what, check that must change, verdict it must become, mutate(base))."""
    cred, canary = values["credential"], values["canary"]

    def write(rel: str, text: str):
        return lambda base: (base / rel).write_text(text)

    def edit_json(rel: str, fn):
        def apply(base: Path) -> None:
            data = json.loads((base / rel).read_text())
            (base / rel).write_text(json.dumps(fn(data)))
        return apply

    def log_line(stream: str, text: str):
        def apply(base: Path) -> None:
            for path in (base / "logs").glob(f"*.{stream}.jsonl"):
                page = json.loads(path.read_text())
                page["content"] += text
                path.write_text(json.dumps(page) + "\n")
        return apply

    def checkpoint(files: dict[str, bytes]):
        return lambda base: (base / "checkpoints" / "cp-1.tgz").write_bytes(_tgz(files))

    def report_field(field: str, value: Any):
        def apply(base: Path) -> None:
            report = json.loads((base / "artifacts" / "answer.txt").read_text())
            report[field] = value
            (base / "artifacts" / "answer.txt").write_text(json.dumps(report))
        return apply

    good_cp = {f"work/repo/{CANARY_FILE}": (canary + "\n").encode()}
    return [
        ("the server saw another credential", "credential_seen", "FAIL",
         report_field("credential_sha256", hashlib.sha256(b"other").hexdigest())),
        ("the call came from another task", "credential_seen", "FAIL",
         report_field("task", "task_" + "c" * 20)),
        ("the call came from no attempt of the task", "credential_seen", "FAIL",
         report_field("attempt", "att_" + "d" * 20)),
        ("the relay logged no call", "credential_seen", "FAIL",
         lambda base: [p.write_text(json.dumps({"status": "ok", "content": ""}) + "\n")
                       for p in (base / "logs").glob("*.stdout.jsonl")]),
        ("swarm-api recorded no attach", "credential_seen", "FAIL", write("mcp-rows.json", "[]")),
        ("the agent saved no answer", "credential_seen", "FAIL",
         lambda base: (base / "artifacts" / "answer.txt").unlink()),
        ("the task failed", "credential_seen", "FAIL", write("state", "FAILED\n")),
        ("the unregistered name was accepted", "unregistered_refused", "FAIL",
         write("door.json", json.dumps({"status": "created:task_x", "code": ""}))),
        ("the unregistered name was refused for something else", "unregistered_refused", "FAIL",
         write("door.json", json.dumps({"status": "422", "code": "invalid_dispatch"}))),
        ("the credential is in the agent's stdout", "no_leak", "FAIL",
         log_line("agent_stdout", cred + "\n")),
        ("the credential is in the runner's stdout", "no_leak", "FAIL",
         log_line("stdout", json.dumps({"message": "x", "v": cred}) + "\n")),
        ("the credential is in an artifact", "no_leak", "FAIL",
         lambda base: (base / "artifacts" / "0.bin").write_text(cred)),
        ("the credential is in an event", "no_leak", "FAIL",
         write("events.json", json.dumps([{"type": "x", "detail": cred}]))),
        ("the credential is in the task document", "no_leak", "FAIL",
         edit_json("doc.json", lambda d: {**d, "result_summary": {"x": cred}})),
        ("the credential is in the checkpoint", "no_leak", "FAIL",
         checkpoint({**good_cp, "work/.mcp.json": cred.encode()})),
        ("an artifact was not downloaded", "no_leak", "FAIL",
         edit_json("artifacts/names.json",
                   lambda d: d + [{"name": "x.txt", "file": "9.bin", "http": "503"}])),
        ("a log stream was unreadable", "no_leak", "FAIL",
         lambda base: next((base / "logs").glob("*.stderr.jsonl")).write_text(
             json.dumps({"status": "unreadable", "content": None}) + "\n")),
        ("control: the logs are blind to the canary", "no_leak", "FAIL",
         lambda base: [p.write_text(p.read_text().replace(canary, "*" * 8))
                       for p in (base / "logs").glob("*.jsonl")]),
        ("control: the artifacts are blind to the canary", "no_leak", "FAIL",
         lambda base: (base / "artifacts" / "1.bin").write_text("********\n")),
        ("control: the events are blind to the canary", "no_leak", "FAIL",
         write("doc.json", json.dumps({"state": "SUCCEEDED"}))),
        ("no checkpoint was written", "no_leak", "FAIL",
         write("checkpoints.json", json.dumps({"checkpoints": []}))),
        ("the final checkpoint holds the credential", "worker_gone", "FAIL",
         checkpoint({**good_cp, "work/notes.txt": cred.encode()})),
        ("the final checkpoint carries the relay file", "worker_gone", "FAIL",
         checkpoint({**good_cp, f"private/{RELAY_FILE}": b"{}"})),
        ("control: the final checkpoint has no canary file", "worker_gone", "FAIL",
         checkpoint({"work/repo/README": b"x"})),
        ("an attempt never recorded its end", "worker_gone", "FAIL",
         edit_json("attempts.json", lambda d: {"attempts": [
             {**d["attempts"][0], "completed_at": None}]})),
        ("a lease is still held", "worker_gone", "FAIL",
         write("leases.json", json.dumps({"ok": False, "detail": "lease l1 still holds capacity"}))),
        ("the task never became terminal", "worker_gone", "FAIL", write("state", "RUNNING\n")),
        ("the feature is dark", "credential_seen", "SKIP",
         write("submit.json", json.dumps({"status": "503", "code": "mcp_unavailable", "task": ""}))),
        ("the server is not registered", "no_leak", "SKIP",
         lambda base: (base / "registration.json").unlink()),
        ("the registration takes no per-run credential", "worker_gone", "SKIP",
         edit_json("registration.json", lambda d: {"server": {**d["server"],
                                                               "auth": {"mode": "none"}}})),
        ("the platform held the task", "no_leak", "SKIP",
         write("state", "PARKED:CREDENTIAL_MISSING\n")),
        ("the door met the dark feature", "unregistered_refused", "SKIP",
         write("door.json", json.dumps({"status": "503", "code": "mcp_unavailable"}))),
    ]


def selftest() -> int:
    problems: list[str] = []
    report = _selftest_server(problems)
    if not report:
        problems.append("server: no whoami report, so the judge cannot be exercised")
        print("\n".join(problems), file=sys.stderr)
        return 1
    values = mint(Path(tempfile.mkdtemp(prefix="mcp-mint-")))
    # The server hashed its own loopback bearer; the evidence is built for
    # the minted credential, as the deployed server would have reported it.
    report = {**report, "credential_sha256": hashlib.sha256(
        values["credential"].encode()).hexdigest()}
    visited = 0
    with tempfile.TemporaryDirectory(prefix="mcp-judge-") as root:
        good = Path(root) / "good"
        good.mkdir()
        _evidence(good, values, report)
        baseline = {v.check: v for v in judge(str(good))}
        for check in CHECKS:
            if baseline[check].verdict != "PASS":
                problems.append(f"baseline {check}: {baseline[check].verdict} "
                                f"{baseline[check].reason}")
        for index, (what, check, want, mutate) in enumerate(_mutations(values)):
            visited += 1
            base = Path(root) / f"m{index}"
            base.mkdir()
            _evidence(base, values, report)
            mutate(base)
            got = {v.check: v for v in judge(str(base))}
            if got[check].verdict != want:
                problems.append(f"{what}: {check} is {got[check].verdict}, expected {want} "
                                f"({got[check].reason})")
            for verdict in got.values():
                if values["credential"] in verdict.reason or values["canary"] in verdict.reason:
                    problems.append(f"{what}: {verdict.check}'s reason quotes a minted value")
    print(f"selftest: the echo server on loopback, the baseline and {visited} defects judged; "
          f"{len(problems)} problem(s)")
    for problem in problems:
        print("  " + problem, file=sys.stderr)
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# The command line mcp.sh calls.
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mint")
    m.add_argument("--dir", required=True)
    b = sub.add_parser("body")
    b.add_argument("--dir", required=True)
    b.add_argument("--kind", choices=("task", "door"), required=True)
    b.add_argument("--server", required=True)
    b.add_argument("--run-id", required=True)
    b.add_argument("--metadata", required=True)
    b.add_argument("--repository-url", default="")
    b.add_argument("--ref", default="")
    j = sub.add_parser("judge")
    j.add_argument("--dir", required=True)
    j.add_argument("--results", required=True)
    sub.add_parser("selftest")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "mint":
        mint(Path(args.dir))
        return 0
    if args.command == "body":
        values = _read_json(Path(args.dir) / "secrets.json")
        if not isinstance(values, dict):
            print("body: run `mint` first", file=sys.stderr)
            return 1
        payload = body(values, kind=args.kind, server=args.server, run_id=args.run_id,
                       metadata=json.loads(args.metadata), repository_url=args.repository_url,
                       ref=args.ref)
        _write_private(Path(args.dir) / f"{args.kind}-body.json", json.dumps(payload))
        return 0
    if args.command == "judge":
        verdicts = judge(args.dir)
        Path(args.results).write_text("".join(v.line() + "\n" for v in verdicts))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
