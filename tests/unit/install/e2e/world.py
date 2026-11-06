"""The fake world `swarm install` walks through: Google's APIs over a fake
HTTP session, GCS and Firestore clients, the commands it runs (gcloud,
kubectl, terraform), the clock, and the person at the terminal.

Each fake refuses what the real service refuses: a Firestore path with the
wrong number of segments, a tfvars key a Terraform root does not declare, a
call nobody faked (recorded in `World.unhandled` and answered 501, so the walk
stops where a real install would meet something unexpected).
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]


class Clock:
    """The fake clock every wait runs on: `sleep` advances it, never blocks."""

    def __init__(self, start: float = 1_793_000_000.0) -> None:
        self.t = start
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))
        self.slept += max(0.0, float(seconds))

    def monotonic(self) -> float:
        return self.t


@dataclass
class Response:
    """What `requests` hands back, as much of it as the installer reads."""

    status_code: int
    body: Any = None
    reason: str = ""
    url: str = ""

    @property
    def content(self) -> bytes:
        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            return self.body
        return json.dumps(self.body).encode()

    @property
    def text(self) -> str:
        return self.content.decode(errors="replace")

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self.body is None or isinstance(self.body, bytes):
            raise ValueError("no JSON body")
        return self.body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} {self.reason} {self.url}")


def error(status: int, reason: str, message: str) -> Response:
    return Response(status, {"error": {"code": status, "status": reason, "message": message}}, reason)


Route = Callable[["Call"], Any]


@dataclass
class Call:
    method: str
    url: str
    body: Any
    params: dict[str, Any]
    headers: dict[str, str]
    match: re.Match[str] | None = None

    def group(self, n: int | str) -> str:
        assert self.match is not None
        return self.match.group(n)


@dataclass
class World:
    """Everything the fakes hold; the assertions read it back."""

    clock: Clock = field(default_factory=Clock)
    routes: list[tuple[str, re.Pattern[str], Route]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    unhandled: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    installer: str = "ada@acme.example"
    hostname: str = "swarm.acme.example"
    project: str = "acme-swarm-1"
    principal: str = "ada@acme.example"
    signed_in: bool = False
    person: Any = None
    objects: dict[tuple[str, str], tuple[bytes, int]] = field(default_factory=dict)
    buckets: dict[str, dict[str, Any]] = field(default_factory=dict)
    generation: int = 0
    documents: dict[str, tuple[dict[str, Any], int]] = field(default_factory=dict)
    databases: set[str] = field(default_factory=set)
    module_roots: Path | None = None
    tokens_minted: list[tuple[str, str]] = field(default_factory=list)
    command_handlers: list[tuple[str, Callable[..., Any]]] = field(default_factory=list)
    addresses: dict[str, set[str]] = field(default_factory=dict)
    registries: set[str] = field(default_factory=set)
    images: set[str] = field(default_factory=set)

    # -- stores --------------------------------------------------------------

    def require_bucket(self, name: str) -> None:
        if name not in self.buckets:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"bucket {name} does not exist")

    def require_database(self, database: str) -> None:
        if database not in self.databases:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"the Firestore database {database} does not exist")

    def write_document(self, path: str, data: dict[str, Any]) -> None:
        self.generation += 1
        self.documents[path] = (json.loads(json.dumps(data, default=str)), self.generation)

    def storage_client(self) -> Any:
        return StorageClient(self)

    def firestore_client(self, project: str | None, database: str | None) -> Any:
        return FirestoreClient(self, project, database)

    # -- commands, tools, DNS, tokens ------------------------------------------

    def on_command(self, program: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def add(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.command_handlers.append((program, fn))
            return fn

        return add

    def command(self, argv: list[str], stdin: str | None, env: Any, cwd: Any) -> tuple[int, str, str]:
        self.commands.append(list(argv))
        program = Path(str(argv[0])).name
        for want, fn in self.command_handlers:
            if want == program:
                return fn(argv, stdin, dict(env or {}), cwd)
        self.unhandled.append("command " + " ".join(map(str, argv[:4])))
        return 127, "", f"{program}: the fake world has no such command"

    def which(self, name: str) -> str | None:
        return f"/fake/bin/{name}" if name in ("gcloud", "kubectl", "terraform", "gke-gcloud-auth-plugin",
                                               "jq", "uv", "cosign", "gcrane", "python3", "git", "swarm") else None

    def ensure(self, name: str) -> str:
        return f"/fake/bin/{name}"

    def browse(self, url: str) -> bool:
        """A page opened in the person's browser: they do what it says there."""
        self.pages.append(url)
        if self.person is not None:
            self.person.on_page(url, self)
        return True

    def resolve(self, hostname: str) -> set[str]:
        return set(self.addresses.get(hostname, set()))

    def generate_access_token(self, source: str, target: str) -> None:
        """IAM's generateAccessToken: the source needs Token Creator on the target."""
        if not self.may_impersonate(source, target):
            import google.auth.exceptions

            raise google.auth.exceptions.RefreshError(
                f"403 PERMISSION_DENIED: {source} lacks iam.serviceAccounts.getAccessToken on {target}")
        self.tokens_minted.append((source, target))

    def may_impersonate(self, source: str, target: str) -> bool:
        return True

    def route(self, method: str, pattern: str) -> Callable[[Route], Route]:
        def add(fn: Route) -> Route:
            self.routes.append((method, re.compile(pattern), fn))
            return fn

        return add

    def http(self, method: str, url: str, body: Any = None, params: Any = None,
             headers: Any = None) -> Response:
        method = method.upper()
        bare = url.split("?", 1)[0]
        self.calls.append((method, bare))
        for want, pattern, fn in self.routes:
            if want != method:
                continue
            found = pattern.fullmatch(bare)
            if found is None:
                continue
            call = Call(method, bare, body, dict(params or {}), dict(headers or {}), found)
            answer = fn(call)
            if isinstance(answer, Response):
                answer.url = bare
                return answer
            return Response(200, answer if answer is not None else {}, "OK", bare)
        self.unhandled.append(f"{method} {bare}")
        return error(501, "UNIMPLEMENTED", f"the fake world has no route for {method} {bare}")


def token() -> str:
    """A fake bearer token, built at runtime (CLAUDE.md: nothing that looks like a credential)."""
    return "fake-" + secrets.token_hex(8)


# -- GCS --------------------------------------------------------------------------


class NotFoundError(Exception):
    pass


class _Blob:
    def __init__(self, world: World, bucket: str, name: str) -> None:
        self.world, self.bucket_name, self.name = world, bucket, name
        found = world.objects.get((bucket, name))
        self.generation = found[1] if found else None

    def download_as_bytes(self, if_generation_match: int | None = None, **_kw: Any) -> bytes:
        found = self.world.objects.get((self.bucket_name, self.name))
        if found is None:
            from google.api_core import exceptions

            raise exceptions.NotFound(f"gs://{self.bucket_name}/{self.name}")
        if if_generation_match is not None and found[1] != if_generation_match:
            from google.api_core import exceptions

            raise exceptions.PreconditionFailed(self.name)
        return found[0]

    def upload_from_string(self, data: bytes | str, content_type: str = "", if_generation_match: int | None = None,
                           **_kw: Any) -> None:
        from google.api_core import exceptions

        self.world.require_bucket(self.bucket_name)
        found = self.world.objects.get((self.bucket_name, self.name))
        current = found[1] if found else 0
        if if_generation_match is not None and current != if_generation_match:
            raise exceptions.PreconditionFailed(f"{self.name}: generation {current} != {if_generation_match}")
        self.world.generation += 1
        payload = data.encode() if isinstance(data, str) else bytes(data)
        self.world.objects[(self.bucket_name, self.name)] = (payload, self.world.generation)
        self.generation = self.world.generation

    def delete(self, if_generation_match: int | None = None, **_kw: Any) -> None:
        from google.api_core import exceptions

        found = self.world.objects.get((self.bucket_name, self.name))
        if found is None:
            raise exceptions.NotFound(self.name)
        if if_generation_match is not None and found[1] != if_generation_match:
            raise exceptions.PreconditionFailed(self.name)
        del self.world.objects[(self.bucket_name, self.name)]

    def exists(self, *_a: Any, **_kw: Any) -> bool:
        return (self.bucket_name, self.name) in self.world.objects


class _Bucket:
    def __init__(self, world: World, name: str) -> None:
        self.world, self.name = world, name

    def get_blob(self, name: str, *_a: Any, **_kw: Any) -> _Blob | None:
        self.world.require_bucket(self.name)
        return _Blob(self.world, self.name, name) if (self.name, name) in self.world.objects else None

    def blob(self, name: str, *_a: Any, **_kw: Any) -> _Blob:
        return _Blob(self.world, self.name, name)

    def exists(self, *_a: Any, **_kw: Any) -> bool:
        return self.name in self.world.buckets

    def list_blobs(self, prefix: str = "", **_kw: Any) -> list[_Blob]:
        return [_Blob(self.world, b, n) for (b, n) in sorted(self.world.objects) if b == self.name
                and n.startswith(prefix)]


class StorageClient:
    def __init__(self, world: World) -> None:
        self.world = world

    def bucket(self, name: str) -> _Bucket:
        return _Bucket(self.world, name)

    def lookup_bucket(self, name: str) -> _Bucket | None:
        return _Bucket(self.world, name) if name in self.world.buckets else None


# -- Firestore ----------------------------------------------------------------------


def verify_document_path(path: str) -> tuple[str, ...]:
    """google-cloud-firestore's own rule: a document path has an even number
    of non-empty segments (collection/doc[/collection/doc...])."""
    from google.cloud.firestore_v1 import _helpers

    parts = tuple(path.split("/"))
    _helpers.verify_path(parts, is_collection=False)  # raises ValueError as the real client does
    return parts


class _Snapshot:
    def __init__(self, data: dict[str, Any] | None, update_time: Any) -> None:
        self._data, self.update_time = data, update_time

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return json.loads(json.dumps(self._data)) if self._data is not None else None


class _WriteOption:
    def __init__(self, last_update_time: Any) -> None:
        self.last_update_time = last_update_time


class _Document:
    def __init__(self, client: FirestoreClient, path: str) -> None:
        verify_document_path(path)
        self.client, self.path = client, path

    @property
    def _docs(self) -> dict[str, tuple[dict[str, Any], int]]:
        return self.client.world.documents

    def get(self, *_a: Any, **_kw: Any) -> _Snapshot:
        self.client.world.require_database(self.client.database)
        found = self._docs.get(self.path)
        return _Snapshot(found[0] if found else None, found[1] if found else None)

    def create(self, data: dict[str, Any]) -> None:
        from google.api_core import exceptions

        self.client.world.require_database(self.client.database)
        if self.path in self._docs:
            raise exceptions.AlreadyExists(self.path)
        self.client.world.write_document(self.path, data)

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        self.client.world.require_database(self.client.database)
        current = dict(self._docs[self.path][0]) if merge and self.path in self._docs else {}
        current.update(data)
        self.client.world.write_document(self.path, current)

    def update(self, data: dict[str, Any], option: _WriteOption | None = None) -> None:
        from google.api_core import exceptions

        self.client.world.require_database(self.client.database)
        found = self._docs.get(self.path)
        if found is None:
            raise exceptions.NotFound(self.path)
        if option is not None and option.last_update_time != found[1]:
            raise exceptions.FailedPrecondition(f"{self.path} changed since it was read")
        merged = dict(found[0])
        merged.update(data)
        self.client.world.write_document(self.path, merged)


class FirestoreClient:
    def __init__(self, world: World, project: str | None, database: str | None) -> None:
        self.world, self.project, self.database = world, project, database or "(default)"

    def document(self, *path: str) -> _Document:
        return _Document(self, "/".join(path))

    def write_option(self, last_update_time: Any = None, **_kw: Any) -> _WriteOption:
        return _WriteOption(last_update_time)
