#!/usr/bin/env python3
"""A task's agent queries its caller's registered MCP server: #1433's four checks, on a deployed environment.

docs/design/registered-mcp-servers.md §6, lanes MCP-ACCEPT and MCP-ACCEPT-B,
and owner decision Q10. `scripts/acceptance/groups/mcp.sh` submits, waits and
saves what the task left into one directory; this file MINTS the per-run
credential and the canary, BUILDS the two request bodies (so neither value is
ever a shell variable or a process argument), PROBES the server without a
credential, and JUDGES the directory.

THE CREDENTIAL IS A GOOGLE ID TOKEN (owner decision 2026-10-18: nothing in
Terraform is public). The echo server's only invoker is the acceptance
identity, the account accept.yml federates as (vars.GCP_ACCEPT_SA;
terraform/infra/mcp_test_server.tf). `token` asks IAM Credentials
`generateIdToken` for that account's ID token, audience the echo service's
URL, using swarm-verify's own access token from the metadata server -- so
swarm-verify must hold roles/iam.serviceAccountOpenIdTokenCreator on the
acceptance account (a terraform/bootstrap grant; until it exists the mint is
refused 403 and every task check SKIPS naming it). Google issues the token
for one hour; it goes out as the task's `mcp_credentials` value with
`expires_in` its remaining life, so the relay sends it as the Authorization
header exactly as it sends a caller's credential, and Cloud Run's IAM check
on it is what admits the call.

  credential_seen       a claude-code task names the registered echo server
                        (tests/acceptance/mcp_echo_server/server.py) with the
                        per-run ID token; its `whoami` answer carries the
                        sha256 of the bearer the server received (or, should
                        Cloud Run hand the container the token without its
                        signature, the sha256 of its claims segment), which
                        the agent cannot compute because it never holds the
                        value. A stripped token is not the value the relay
                        registered, so the relay could not scrub an echo of
                        it: the server withholds that echo and says so
                        (`signature_removed`). The answer names this task and one of its
                        attempts (the relay's own headers), the relay logged
                        `mcp_call tools/call whoami` with status 200, and
                        swarm-api recorded the attach.
  unregistered_refused  the same dispatch naming a server nobody registered is
                        refused `mcp_server_unregistered` at submission, so no
                        task exists to admit. The probe carries a reserved
                        metadata key (lib.sh acc_door): a door that let the
                        name through is refused for the metadata instead, and
                        nothing is created either way.
  no_leak               the credential -- whole, its signature or its claims
                        segment, or the masked form a JWT takes on a redacted
                        read where only this credential goes -- is in no log
                        route (the runner's and the agent's four streams of
                        every attempt, and the
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
                        checkpointed): the credential cannot be read back.
  unauthenticated_refused
                        the same server, called with no credential, is
                        refused 403 by Cloud Run. The echo server answers a
                        missing bearer 401 and never 403, so a 401 means the
                        request reached the container: the service is public.

WHAT THE CANARY CONTROLS. Every text read here is redacted at read time by
swarm-api. The canary is `secrets.token_hex(24)`, which no redaction rule
matches, so finding it through a route proves the route was read and shows
what was stored. A JWT is not so lucky: the `jwt` rule serves it as its first
ten characters and a mask, so a token stored in the clear reads back masked.
The search therefore also looks for that masked form -- but only where
nothing but this credential goes, under its server's name and in the echo
server's `credential_echo`. The ten characters kept are the header's, the
same `eyJhbGciOi` for every Google ID token, so anywhere else the masked
form names no credential at all. On a working platform it is never there:
the worker redacts the per-run credential as a literal, which leaves a bare
mask and nothing of the token. A route that shows the masked form in one of
those places held the token in a path the literal missed.
What this does NOT read: Cloud Logging (swarm-verify holds no logging read in
a project it shares with another team), and Secret Manager (the credential is
meant to be there until the sweep, §4.4).

STANDARD LIBRARY ONLY: it runs in the swarm-verify image (alpine, python3 with
no pip). It is a program the suite runs; its ONE pytest test,
`test_the_selftest_catches_every_defect_it_names`, runs `selftest` when pytest
is pointed at this file (pyproject's testpaths is tests/unit, so a plain
`pytest` never collects it).

    test_caller_mcp.py mint --dir DIR
    test_caller_mcp.py probe --dir DIR
    test_caller_mcp.py token --dir DIR --invoker EMAIL
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
import base64
import binascii
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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

#: The checks, in the order mcp.sh lists them.
CHECKS = ("credential_seen", "unregistered_refused", "no_leak", "worker_gone",
          "unauthenticated_refused")

PROFILE = "claude-code"
#: The echo server's one tool and its report's fields (server.py).
TOOL = "whoami"
ANSWER_ARTIFACT = "mcp-answer.json"
CANARY_FILE = "mcp-canary.txt"
#: The worker's relay file (agent_worker.mcp_attach.RELAY_FILE): under
#: `private/`, never checkpointed. Its presence in a checkpoint is a defect.
RELAY_FILE = "mcp-relay.json"
#: How long the door probe's credential claims to live (§4.3: 60-86400 s).
#: The task's credential gets the ID token's own remaining life instead.
EXPIRES_IN = 3600
#: An ID token's `expires_in` is its `exp` less now, within §4.3's bounds and
#: never past the hour Google issues it for.
MIN_TOKEN_LIFE, MAX_TOKEN_LIFE = 60, 3600
#: swarm-verify's own access token, and IAM's mint of the acceptance
#: identity's ID token with it.
METADATA_TOKEN_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                      "service-accounts/default/token")
ID_TOKEN_URL = ("https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                "{account}:generateIdToken")
#: A user-managed service account's address, nothing else: the invoker is
#: interpolated into ID_TOKEN_URL's path.
INVOKER = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]"
                     r"\.iam\.gserviceaccount\.com$")
#: swarm_redaction's mask, and how much of a JWT its `jwt` rule keeps.
MASK = "********"
JWT_KEPT = 10
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
    """The run's own values. The task's credential is not one of them: it is
    the acceptance identity's ID token, which `token` adds once the
    registration names the URL it is for. The door probe gets its own random
    credential, so the one under test is sent exactly once, to the one task
    that may use it."""
    values = {
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
        "mcp_credentials": {server: {"value": values["credential"],
                                     "expires_in": int(values["credential_expires_in"])}},
        "metadata": metadata,
        "priority": 10,
        "max_attempts": 1,
    }
    if repository_url:
        task["repository_url"] = repository_url
        task["repository_ref"] = ref or "main"
    return task


# ---------------------------------------------------------------------------
# The ID token, and the call without it. Every request goes through `fetch`,
# so the selftest drives both with no network; no reason ever quotes a body.
# ---------------------------------------------------------------------------

#: (method, url, headers, body) -> (HTTP status, body); status 0 when no
#: answer came.
Fetch = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, never followed: a bearer must not travel on
    to a host it was not minted for, and the probe judges the first hop."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_fetch(method: str, url: str, headers: dict[str, str],
               data: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        # The metadata server, IAM Credentials, or the registered run.app URL.
        with opener.open(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)
    except (urllib.error.URLError, OSError):
        return 0, b""


def jwt_parts(token: str) -> tuple[str, str, str] | None:
    """A compact JWT's three segments, or None when it is not one."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].startswith("eyJ") or not parts[1]:
        return None
    return parts[0], parts[1], parts[2]


def jwt_claims(token: str) -> dict[str, Any] | None:
    parts = jwt_parts(token)
    if parts is None:
        return None
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def audience(url: str) -> str | None:
    """The ID token's audience for a Cloud Run URL: its https origin. Only a
    run.app host: the token is the acceptance identity's, and a registration
    edited to point elsewhere must not get one minted for its host."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password \
            or parsed.port is not None or not parsed.hostname.endswith(".run.app"):
        return None
    return f"https://{parsed.hostname}"


def _google_error(raw: bytes) -> str:
    """Google's error status word (PERMISSION_DENIED, ...), never the message."""
    try:
        status = json.loads(raw.decode("utf-8", errors="replace"))["error"]["status"]
    except (ValueError, KeyError, TypeError):
        return ""
    return status if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,64}", status) else ""


@dataclass
class Minted:
    token: str | None
    expires_in: int
    #: "" on success; otherwise why, naming no value.
    reason: str
    #: IAM refused the mint: the owner's grant is missing, not a regression.
    refused: bool = False


def mint_id_token(invoker: str, aud: str, fetch: Fetch = http_fetch,
                  now: Callable[[], float] = time.time) -> Minted:
    """The acceptance identity's ID token for `aud`, minted with swarm-verify's
    own access token. Neither token is ever returned in a reason."""
    if not INVOKER.match(invoker):
        return Minted(None, 0, "the invoker is not a service account address")
    status, raw = fetch("GET", METADATA_TOKEN_URL, {"Metadata-Flavor": "Google"}, None)
    try:
        access = json.loads(raw.decode("utf-8")).get("access_token") if status == 200 else None
    except (ValueError, UnicodeDecodeError, AttributeError):
        access = None
    if not isinstance(access, str) or not access:
        return Minted(None, 0, f"the metadata server gave swarm-verify no access token (HTTP {status})")
    status, raw = fetch(
        "POST", ID_TOKEN_URL.format(account=invoker),
        {"Authorization": "Bearer " + access, "Content-Type": "application/json"},
        json.dumps({"audience": aud, "includeEmail": True}).encode())
    del access
    if status != 200:
        word = _google_error(raw)
        return Minted(None, 0, f"IAM refused to mint an ID token as {invoker}: HTTP {status}"
                      + (f" {word}" if word else "")
                      + ("; swarm-verify needs roles/iam.serviceAccountOpenIdTokenCreator on "
                         "that account (terraform/bootstrap)" if status == 403 else ""),
                      refused=status == 403)
    try:
        token = json.loads(raw.decode("utf-8")).get("token")
    except (ValueError, UnicodeDecodeError, AttributeError):
        token = None
    claims = jwt_claims(token) if isinstance(token, str) else None
    if claims is None:
        return Minted(None, 0, "IAM answered 200 without a JWT")
    if claims.get("aud") != aud or claims.get("email") != invoker:
        return Minted(None, 0, "the minted token is not for this audience and invoker")
    exp = claims.get("exp")
    life = int(exp - now()) if isinstance(exp, (int, float)) else 0
    if life < MIN_TOKEN_LIFE:
        return Minted(None, 0, f"the minted token lives {life} s, under {MIN_TOKEN_LIFE} s")
    return Minted(token, min(life, MAX_TOKEN_LIFE), "")


def registered_url(base: Path) -> str:
    doc = _read_json(base / "registration.json")
    server = doc.get("server") if isinstance(doc, dict) else None
    url = server.get("url") if isinstance(server, dict) else None
    return url if isinstance(url, str) else ""


def token(base: Path, invoker: str, fetch: Fetch = http_fetch,
          now: Callable[[], float] = time.time) -> bool:
    """Mint the task's credential into secrets.json (0600); token.json says
    what happened, never the value. True when it was minted."""
    values = _read_json(base / "secrets.json")
    if not isinstance(values, dict):
        _write_private(base / "token.json", json.dumps(
            {"minted": False, "refused": False, "reason": "run `mint` first"}))
        return False
    aud = audience(registered_url(base))
    minted = (mint_id_token(invoker, aud, fetch, now) if aud else
              Minted(None, 0, "the registration's URL is not an https run.app URL to mint an "
                     "audience for"))
    if minted.token is not None:
        values = {**values, "credential": minted.token, "credential_expires_in": minted.expires_in}
        _write_private(base / "secrets.json", json.dumps(values))
    _write_private(base / "token.json", json.dumps({
        "minted": minted.token is not None, "refused": minted.refused, "reason": minted.reason,
        "invoker": invoker, "audience": aud or "", "expires_in": minted.expires_in}))
    return minted.token is not None


def probe(base: Path, fetch: Fetch = http_fetch) -> int:
    """POST an MCP `initialize` to the registered URL with NO credential and
    record the status in unauthenticated.json (0: no answer, -1: no URL)."""
    url = registered_url(base)
    status = -1
    if audience(url):
        status, _ = fetch("POST", url, {
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "swarm-acceptance-probe", "version": "1"}}}).encode())
    (base / "unauthenticated.json").write_text(json.dumps({"status": status}))
    return status


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


#: The JSON key under which only this run's credential is ever stored or
#: echoed, besides its server's name: the echo server's field for its bearer.
ECHO_FIELD = "credential_echo"
#: Cloud Run's signature segment on a Google-signed token its IAM check
#: admitted: the container gets `header.claims.SIGNATURE_REMOVED_BY_GOOGLE`.
SIGNATURE_REMOVED = "SIGNATURE_REMOVED_BY_GOOGLE"


def _keyed(key: str, value: str) -> re.Pattern[bytes]:
    """`"<key>": "<value>"` or `"<key>": {"value": "<value>"`, compact or
    spaced, and as it reads inside a JSON string (a log page's content)."""
    quote, colon = rb'\\*"', rb"\s*:\s*"
    inner = quote + rb"value" + quote + colon
    return re.compile(quote + re.escape(key.encode("ascii")) + quote + colon
                      + rb"(?:\{\s*" + inner + rb")?" + quote
                      + re.escape(value.encode("ascii")) + quote)


def credential_needles(credential: str, server: str) -> list[tuple[str, re.Pattern[bytes]]]:
    """(what, pattern) for every form of the credential a store could hold:
    the whole value; for a JWT also its signature and its claims segment
    (each is the token's own, and neither is anything else's); and the masked
    form a redacted read serves for it, which a literal redaction never
    leaves. The `jwt` rule keeps only the first ten characters, the header's,
    which every Google ID token shares (`eyJhbGciOi`): on its own that form
    names no credential. It counts as this one's only where nothing but this
    credential goes -- the value stored under its server's name (the task's
    `mcp_credentials`) and the echo server's report of its bearer."""
    out = [("the credential", re.compile(re.escape(credential.encode("ascii"))))]
    parts = jwt_parts(credential)
    if parts is not None:
        if len(parts[2]) >= 16:
            out.append(("its signature", re.compile(re.escape(parts[2].encode("ascii")))))
        out.append(("its claims", re.compile(re.escape(parts[1].encode("ascii")))))
        masked = credential[:JWT_KEPT] + MASK
        out.extend(("its masked JWT form", _keyed(key, masked)) for key in (server, ECHO_FIELD))
    return out


def _holds(data: bytes, needles: list[tuple[str, re.Pattern[bytes]]]) -> list[str]:
    return list(dict.fromkeys(what for what, needle in needles if needle.search(data)))


def _holding(blobs: Iterable[Blob], credential: str, server: str) -> list[str]:
    """`<surface> <item> (<what>)` for every blob holding a form of it."""
    needles = credential_needles(credential, server)
    out = []
    for blob in blobs:
        found = _holds(blob.data, needles)
        if found:
            out.append(f"{blob.surface} {blob.item} ({', '.join(found)})")
    return out


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
    token: dict[str, Any]
    unauthenticated: dict[str, Any]


def _load(base: Path) -> Run:
    values = _read_json(base / "secrets.json") or {}
    registration = _read_json(base / "registration.json")
    server = registration.get("server") if isinstance(registration, dict) else None
    submit = _read_json(base / "submit.json")
    door = _read_json(base / "door.json")
    minted = _read_json(base / "token.json")
    unauthenticated = _read_json(base / "unauthenticated.json")
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
        token=minted if isinstance(minted, dict) else {},
        unauthenticated=unauthenticated if isinstance(unauthenticated, dict) else {},
    )


def _not_measured(run: Run) -> str | None:
    """Why nothing about the task can be judged, or None when it ran."""
    if not run.values.get("canary"):
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
    if not run.values.get("credential") and run.token.get("refused") is True:
        return str(run.token.get("reason") or "IAM refused to mint the ID token")
    if not run.task:
        return None
    if run.state.startswith(_HELD):
        return f"the platform held the task on purpose ({run.state})"
    return None


def _no_task(run: Run) -> Verdict | None:
    """FAIL when the dispatch never became a task for a reason not skipped above."""
    if run.task:
        return None
    if not run.values.get("credential"):
        return Verdict("", "FAIL", "no task was dispatched: the ID token was not minted ("
                       + str(run.token.get("reason") or "token.json was not written") + ")")
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
    credential = run.values["credential"]
    whole = hashlib.sha256(credential.encode("ascii")).hexdigest()
    parts = jwt_parts(credential)
    claims = hashlib.sha256(parts[1].encode("ascii")).hexdigest() if parts else None
    if report.get("credential_sha256") == whole:
        seen = "sha256 matches"
    elif claims is not None and report.get("jwt_claims_sha256") == claims:
        seen = "its claims' sha256 matches; Cloud Run handed the container the token unsigned"
    else:
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
    return Verdict(key, "PASS", f"{run.server} saw the dispatched credential ({seen}) on "
                   f"{TOOL} from attempt {report.get('attempt')} through the relay "
                   f"(mcp_call 200; {attach}); {_echo(report, credential, run.server)}")


def _echo(report: dict[str, Any], credential: str, server: str) -> str:
    """What became of the server's echo of its bearer. Scrubbed means the
    agent got it holding no form of the credential at all -- not merely
    something other than the signed token, which a token Cloud Run stripped
    of its signature also is."""
    echo = report.get(ECHO_FIELD)
    if echo is None and report.get("signature_removed") is True:
        return ("the server withheld its echo: Cloud Run removed the token's signature, so the "
                "relay had no registered value to scrub and its scrub was not exercised")
    if not isinstance(echo, str):
        return "the server reported no echo of the value"
    held = _holds(json.dumps({ECHO_FIELD: echo}).encode("utf-8"),
                  credential_needles(credential, server))
    if held:
        return f"its echo of the value reached the agent NOT scrubbed ({', '.join(held)})"
    return "its echo of the value reached the agent scrubbed"


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
    leaks = [hit for s in surfaces.values() for hit in _holding(s.blobs, run.values["credential"],
                                                                     run.server)]
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
    needles = credential_needles(run.values["credential"], run.server)
    held = [n for n, d in final.members.items() if _holds(d, needles)]
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


def check_unauthenticated_refused(run: Run) -> Verdict:
    key = "unauthenticated_refused"
    if run.registration is None:
        return Verdict(key, "SKIP", f"not measured: the smoke tenant has no MCP server named "
                       f"{run.server!r}, so there is no URL to call")
    status = run.unauthenticated.get("status")
    if not isinstance(status, int) or isinstance(status, bool):
        return Verdict(key, "FAIL", "the call without a credential was not made")
    if status == 403:
        return Verdict(key, "PASS", f"{run.server} called with no credential: Cloud Run refused "
                       "403 before the container saw it (the echo server's own refusal is 401)")
    if status == -1:
        return Verdict(key, "FAIL", f"the registration {run.server!r} has no https URL to call")
    if status == 0:
        return Verdict(key, "FAIL", f"the call without a credential got no answer from {run.server}")
    if status == 401:
        return Verdict(key, "FAIL", "the call without a credential reached the echo server "
                       "(its own 401): Cloud Run let an unauthenticated request in, so the "
                       "service is public")
    return Verdict(key, "FAIL", f"the call without a credential answered HTTP {status}, not "
                   "Cloud Run's 403")


def judge(base: str) -> list[Verdict]:
    run = _load(Path(base))
    return [check_credential_seen(run), check_unregistered_refused(run),
            check_no_leak(run), check_worker_gone(run), check_unauthenticated_refused(run)]


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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def fake_id_token(aud: str, email: str, life: int = 3600, now: float | None = None) -> str:
    """A token shaped like a Google ID token, built at run time with a random
    key id and signature (CLAUDE.md: no credential-shaped literal)."""
    issued = int(time.time() if now is None else now)
    header = {"alg": "RS256", "kid": secrets.token_hex(20), "typ": "JWT"}
    claims = {"aud": aud, "azp": secrets.token_hex(10), "email": email, "email_verified": True,
              "exp": issued + life, "iat": issued, "iss": "https://accounts.google.com",
              "sub": str(secrets.randbelow(10 ** 20))}
    return ".".join([_b64url(json.dumps(header).encode()), _b64url(json.dumps(claims).encode()),
                     _b64url(secrets.token_bytes(256))])


def fake_google(aud: str, invoker: str, *, metadata: int = 200, iam: int = 200,
                token_value: str | None = None, cloud_run: int = 403,
                calls: list[tuple[str, str, dict[str, str]]] | None = None) -> Fetch:
    """The metadata server, IAM Credentials and the Cloud Run front door, as
    `fetch` sees them: what the deployed group talks to, with no network."""
    access = secrets.token_hex(16)
    minted = token_value if token_value is not None else fake_id_token(aud, invoker)

    def fetch(method: str, url: str, headers: dict[str, str],
              data: bytes | None) -> tuple[int, bytes]:
        if calls is not None:
            calls.append((method, url, dict(headers)))
        if url == METADATA_TOKEN_URL:
            body_ = {"access_token": access, "expires_in": 3599, "token_type": "Bearer"}
            return metadata, json.dumps(body_).encode() if metadata == 200 else b""
        if url == ID_TOKEN_URL.format(account=invoker):
            if headers.get("Authorization") != "Bearer " + access:
                return 401, json.dumps({"error": {"status": "UNAUTHENTICATED"}}).encode()
            if iam != 200:
                return iam, json.dumps({"error": {"status": "PERMISSION_DENIED",
                                                  "message": "denied"}}).encode()
            asked = json.loads(data or b"{}")
            if asked.get("audience") != aud or asked.get("includeEmail") is not True:
                return 400, b"{}"
            return 200, json.dumps({"token": minted}).encode()
        if url.startswith(aud + "/"):
            return cloud_run, b"<html>Forbidden</html>" if cloud_run == 403 else b"{}"
        return 0, b""
    return fetch


def _selftest_token(problems: list[str]) -> None:
    """`token` and `probe` against fake_google: the happy path, every refusal,
    and that no value reaches a file other than secrets.json."""
    invoker = "swarm-accept@saga-agents-staging.iam.gserviceaccount.com"
    url = "https://swarm-mcp-echo-" + secrets.token_hex(5) + "-uc.a.run.app/mcp"
    aud = url[:-len("/mcp")]
    with tempfile.TemporaryDirectory(prefix="mcp-token-") as root:
        base = Path(root)

        def fresh() -> None:
            for name in ("secrets.json", "token.json", "unauthenticated.json"):
                (base / name).unlink(missing_ok=True)
            mint(base)
            (base / "registration.json").write_text(json.dumps({"server": {"url": url}}))

        fresh()
        calls: list[tuple[str, str, dict[str, str]]] = []
        if not token(base, invoker, fake_google(aud, invoker, calls=calls)):
            problems.append("token: a granted mint was not recorded as minted")
        values = json.loads((base / "secrets.json").read_text())
        claims = jwt_claims(values.get("credential", "")) or {}
        if claims.get("aud") != aud or claims.get("email") != invoker:
            problems.append("token: the credential is not the acceptance identity's token "
                            "for the echo service's origin")
        if not 3500 <= int(values.get("credential_expires_in", 0)) <= MAX_TOKEN_LIFE:
            problems.append("token: expires_in is not the token's own remaining hour")
        if (base / "secrets.json").stat().st_mode & 0o077:
            problems.append("token: secrets.json is readable by others")
        for name in ("token.json",):
            if values["credential"] in (base / name).read_text():
                problems.append(f"token: {name} holds the minted token")
        if [c[1] for c in calls] != [METADATA_TOKEN_URL, ID_TOKEN_URL.format(account=invoker)]:
            problems.append("token: the mint did not go metadata server, then IAM Credentials")
        if calls and calls[0][2].get("Metadata-Flavor") != "Google":
            problems.append("token: the metadata request lacks Metadata-Flavor: Google")
        task_body = body(values, kind="task", server="acc-echo", run_id="r1", metadata={})
        sent = task_body["mcp_credentials"]["acc-echo"]
        if sent["value"] != values["credential"] or sent["expires_in"] != values["credential_expires_in"]:
            problems.append("body: the task does not carry the minted token as its MCP credential")
        if values["credential"] in json.dumps(task_body["input"]):
            problems.append("body: the minted token is in the task's input, where the agent reads it")

        refusals = [
            ("IAM refuses the mint", fake_google(aud, invoker, iam=403), True),
            ("the metadata server is down", fake_google(aud, invoker, metadata=500), False),
            ("IAM answers a token for another audience",
             fake_google(aud, invoker, token_value=fake_id_token("https://elsewhere.example", invoker)),
             False),
            ("IAM answers a token for another identity",
             fake_google(aud, invoker, token_value=fake_id_token(aud, "other@x.iam.gserviceaccount.com")),
             False),
            ("IAM answers a token that is about to expire",
             fake_google(aud, invoker, token_value=fake_id_token(aud, invoker, life=30)), False),
            ("IAM answers something that is not a JWT",
             fake_google(aud, invoker, token_value=secrets.token_hex(24)), False),
        ]
        for what, fetch, refused in refusals:
            fresh()
            if token(base, invoker, fetch):
                problems.append(f"token: {what}, and it was recorded as minted")
            state = json.loads((base / "token.json").read_text())
            if state.get("minted") is not False or state.get("refused") is not refused:
                problems.append(f"token: {what}: token.json says {state}")
            if "credential" in json.loads((base / "secrets.json").read_text()):
                problems.append(f"token: {what}, and secrets.json holds a credential anyway")
        fresh()
        if token(base, "not an address/../x", fake_google(aud, invoker)):
            problems.append("token: an invoker that is not a service account address was used")
        (base / "registration.json").write_text(json.dumps({"server": {"url": "http://" + aud[8:]}}))
        if token(base, invoker, fake_google(aud, invoker)):
            problems.append("token: a token was minted for a plain-http URL")
        (base / "registration.json").write_text(json.dumps({"server": {
            "url": "https://mcp.elsewhere.example/mcp"}}))
        if token(base, invoker, fake_google("https://mcp.elsewhere.example", invoker)):
            problems.append("token: a token was minted for a host that is not Cloud Run's")

        fresh()
        for status in (403, 401, 0):
            if probe(base, fake_google(aud, invoker, cloud_run=status)) != status:
                problems.append(f"probe: a {status} from the front door was not recorded")
        calls = []
        probe(base, fake_google(aud, invoker, calls=calls))
        if [c[1] for c in calls] != [url] or any(k.lower() == "authorization" for k in calls[0][2]):
            problems.append("probe: the unauthenticated call carried a credential or went elsewhere")


def _selftest_server(problems: list[str]) -> dict[str, Any]:
    server = _load_server()
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}{server.MCP_PATH}"
    value = fake_id_token("https://swarm-mcp-echo.a.run.app", "swarm-accept@p.iam.gserviceaccount.com")
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
        claims_hash = hashlib.sha256(value.split(".")[1].encode()).hexdigest()
        if report.get("jwt_claims_sha256") != claims_hash:
            problems.append("server: whoami did not report the JWT bearer's claims hash")
        if report.get(ECHO_FIELD) != value or report.get("signature_removed") is not False:
            problems.append("server: whoami did not echo a signed bearer for the relay to scrub")
        for unsigned in (value.rsplit(".", 1)[0] + ".",
                         value.rsplit(".", 1)[0] + "." + SIGNATURE_REMOVED):
            status, called = _post(url, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                         "params": {"name": TOOL, "arguments": {}}},
                                   {**auth, "Authorization": "Bearer " + unsigned})
            stripped = json.loads(called["result"]["content"][0]["text"]) if status == 200 else {}
            if stripped.get("jwt_claims_sha256") != claims_hash:
                problems.append("server: the claims hash changed when the token came without "
                                "its signature")
            if stripped.get(ECHO_FIELD) is not None or stripped.get("signature_removed") is not True:
                problems.append("server: whoami echoed a token without its signature, which the "
                                "relay cannot scrub")
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
    (base / "token.json").write_text(json.dumps({"minted": True, "refused": False, "reason": "",
                                                 "expires_in": values["credential_expires_in"]}))
    (base / "unauthenticated.json").write_text(json.dumps({"status": 403}))
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
    _, claims_segment, signature = cred.split(".")

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

    def answer_field(field: str, value: Any, **more: Any):
        """The answer as the agent saved it: the judge's copy and the artifact."""
        def apply(base: Path) -> None:
            for rel in ("artifacts/answer.txt", "artifacts/0.bin"):
                report = json.loads((base / rel).read_text())
                report.update({field: value, **more})
                (base / rel).write_text(json.dumps(report))
        return apply

    stripped = cred.rsplit(".", 1)[0] + "." + SIGNATURE_REMOVED
    masked = cred[:JWT_KEPT] + MASK
    good_cp = {f"work/repo/{CANARY_FILE}": (canary + "\n").encode()}
    return [
        ("the server saw another credential", "credential_seen", "FAIL",
         lambda base: [report_field("credential_sha256", hashlib.sha256(b"other").hexdigest())(base),
                       report_field("jwt_claims_sha256", hashlib.sha256(b"other").hexdigest())(base)]),
        ("Cloud Run handed the container the token without its signature", "credential_seen",
         "PASS", answer_field("credential_sha256", hashlib.sha256(stripped.encode()).hexdigest(),
                              credential_echo=None, signature_removed=True)),
        ("the server withheld its echo of the token Cloud Run stripped", "no_leak", "PASS",
         answer_field("credential_sha256", hashlib.sha256(stripped.encode()).hexdigest(),
                      credential_echo=None, signature_removed=True)),
        ("the server echoed the token Cloud Run stripped of its signature", "no_leak", "FAIL",
         answer_field("credential_sha256", hashlib.sha256(stripped.encode()).hexdigest(),
                      credential_echo=stripped, signature_removed=False)),
        ("the server's echo reached the agent as the credential's masked JWT form", "no_leak",
         "FAIL", answer_field(ECHO_FIELD, masked)),
        ("the server saw an unsigned token with other claims", "credential_seen", "FAIL",
         lambda base: [report_field("credential_sha256", hashlib.sha256(b"x.y.").hexdigest())(base),
                       report_field("jwt_claims_sha256", hashlib.sha256(b"y").hexdigest())(base)]),
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
        ("a masked JWT tied to no credential is in the agent's stdout", "no_leak", "PASS",
         log_line("agent_stdout", "Authorization: Bearer " + masked + "\n")),
        ("the credential's masked JWT form is stored under its server's name", "no_leak", "FAIL",
         edit_json("doc.json", lambda d: {**d, "mcp_credentials": {
             "acc-echo": {"value": masked, "expires_in": 3599}}})),
        ("the credential's masked JWT form is under its server's name in a log line", "no_leak",
         "FAIL", log_line("stdout", json.dumps({"message": "x", "credentials": {
             "acc-echo": masked}}) + "\n")),
        ("the final checkpoint holds the echo as the credential's masked JWT form",
         "worker_gone", "FAIL",
         checkpoint({**good_cp, "work/answer.json": json.dumps({ECHO_FIELD: masked}).encode()})),
        ("the credential's signature alone is in an artifact", "no_leak", "FAIL",
         lambda base: (base / "artifacts" / "0.bin").write_text(signature)),
        ("the credential's claims are in an event", "no_leak", "FAIL",
         write("events.json", json.dumps([{"type": "x", "detail": claims_segment}]))),
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
        ("the final checkpoint holds the credential's signature", "worker_gone", "FAIL",
         checkpoint({**good_cp, "work/notes.txt": signature.encode()})),
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
        ("a call with no credential reached the echo server", "unauthenticated_refused", "FAIL",
         write("unauthenticated.json", json.dumps({"status": 401}))),
        ("a call with no credential was answered", "unauthenticated_refused", "FAIL",
         write("unauthenticated.json", json.dumps({"status": 200}))),
        ("a call with no credential got no answer", "unauthenticated_refused", "FAIL",
         write("unauthenticated.json", json.dumps({"status": 0}))),
        ("the call with no credential was never made", "unauthenticated_refused", "FAIL",
         lambda base: (base / "unauthenticated.json").unlink()),
        ("there is no registration to probe", "unauthenticated_refused", "SKIP",
         lambda base: (base / "registration.json").unlink()),
        ("IAM refused to mint the ID token", "credential_seen", "SKIP",
         lambda base: [_write_private(base / "secrets.json", json.dumps(
             {k: v for k, v in values.items() if not k.startswith("credential")})),
             write("token.json", json.dumps({"minted": False, "refused": True,
                                             "reason": "IAM refused: HTTP 403"}))(base),
             write("submit.json", json.dumps({"status": "not sent", "code": None, "task": ""}))(base),
             write("task", "\n")(base)]),
        ("the ID token could not be minted for another reason", "no_leak", "FAIL",
         lambda base: [_write_private(base / "secrets.json", json.dumps(
             {k: v for k, v in values.items() if not k.startswith("credential")})),
             write("token.json", json.dumps({"minted": False, "refused": False,
                                             "reason": "IAM answered 200 without a JWT"}))(base),
             write("submit.json", json.dumps({"status": "not sent", "code": None, "task": ""}))(base),
             write("task", "\n")(base)]),
    ]


def selftest() -> int:
    problems: list[str] = []
    _selftest_token(problems)
    report = _selftest_server(problems)
    if not report:
        problems.append("server: no whoami report, so the judge cannot be exercised")
        print("\n".join(problems), file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="mcp-mint-") as minted:
        values: dict[str, Any] = mint(Path(minted))
    values["credential"] = fake_id_token("https://swarm-mcp-echo.a.run.app",
                                         "swarm-accept@p.iam.gserviceaccount.com")
    values["credential_expires_in"] = 3599
    _, claims_segment, signature = values["credential"].split(".")
    # The server hashed its own loopback bearer; the evidence is built for
    # the minted credential, as the deployed server would have reported it.
    report = {**report,
              "credential_sha256": hashlib.sha256(values["credential"].encode()).hexdigest(),
              "jwt_claims_sha256": hashlib.sha256(claims_segment.encode()).hexdigest()}
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
                if any(v in verdict.reason for v in (values["credential"], values["canary"],
                                                     claims_segment, signature)):
                    problems.append(f"{what}: {verdict.check}'s reason quotes a minted value")
    print(f"selftest: the echo server on loopback, the baseline and {visited} defects judged; "
          f"{len(problems)} problem(s)")
    for problem in problems:
        print("  " + problem, file=sys.stderr)
    return 1 if problems else 0


def test_the_selftest_catches_every_defect_it_names() -> None:
    """pytest's entry: the echo server answers on loopback, and every check
    PASSES on a working platform's evidence and FAILS or SKIPS on each defect."""
    assert selftest() == 0


# ---------------------------------------------------------------------------
# The command line mcp.sh calls.
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mint")
    m.add_argument("--dir", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--dir", required=True)
    t = sub.add_parser("token")
    t.add_argument("--dir", required=True)
    t.add_argument("--invoker", required=True)
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
    if args.command == "probe":
        probe(Path(args.dir))
        return 0
    if args.command == "token":
        # Exit 0 either way: token.json carries the outcome to mcp.sh and the
        # judge, and a refused mint is reported there, by name.
        token(Path(args.dir), args.invoker)
        return 0
    if args.command == "body":
        values = _read_json(Path(args.dir) / "secrets.json")
        if not isinstance(values, dict):
            print("body: run `mint` first", file=sys.stderr)
            return 1
        if args.kind == "task" and not values.get("credential"):
            print("body: no ID token was minted; run `token` first", file=sys.stderr)
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
