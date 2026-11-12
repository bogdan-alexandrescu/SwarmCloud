"""The managed plugin catalogue's routes (docs/design/managed-plugins.md, lane 5 of section 17).

Two scopes, the same verbs (`{base}` is `/v1/admin/plugin-catalogue` for the
platform catalogue, a platform admin's, and `/v1/workspaces/{tenant_id}/
plugin-catalogue` for one workspace's private plugins):

    GET    {base}                                   the catalogue: built-ins, installs
    POST   {base}/installs                          install from GitHub or a marketplace entry
    POST   {base}/uploads                           an intake and its signed upload URL
    POST   {base}/intakes/{intake_id}/start         the upload arrived: run the intake
    GET    {base}/intakes/{intake_id}               an intake's state
    GET    {base}/installs/{install_id}/versions    an install and its versions
    POST   {base}/installs/{install_id}/update      a candidate version, by the same intake path
    GET    {base}/installs/{install_id}/diff        current -> a candidate, and the diff's digest
    GET    {base}/installs/{install_id}/usage       who uses it, for Remove to show (section 9.2)
    POST   {base}/installs/{install_id}/apply       make the candidate current (quotes the digest);
                                                    a bundled install's starts its bundle build
    DELETE {base}/installs/{install_id}?confirm=    remove it: later submissions naming it are refused

    POST   /v1/internal/plugin-intake/report        the intake's callback (Pub/Sub push, OIDC)

WHO (the owner's answers of 6 November 2026, Q3). The platform catalogue is
a platform admin's (`admin_auth`). A workspace's private plugins are its
owner's or admin's: install, update and apply need that role, read fresh; a
platform admin who is neither may read them and may Remove one (an incident,
section 1.2), nothing else. A member reads the workspace's catalogue only.

SOURCES (section 2.1, Q9, Q10). A GitHub repository at a tag, a branch or a
commit, resolved once to a 40-hex commit; a marketplace entry, resolved
through the marketplace's `marketplace.json` at its commit to a path in the
same commit or a `github` source; or an uploaded `.zip`, `.tar.gz` or
`.tgz`. Dependencies ship bundled: a marketplace entry that installs from
npm or pip, or names a bare git URL, is refused, and there is no other kind.
A workspace's private repository is read with a token minted from that
workspace's own GitHub App installation for the one repository, contents
read and nothing else, used once and never stored; the platform catalogue
reads public repositories.

QUARANTINE, THEN THE NETWORK-LESS INTAKE (section 2.2, the owner's Q7).
swarm-api streams the archive of exactly the resolved commit into
`quarantine/<intake_id>`, stopping at 100 MiB, without unpacking it; an
upload goes there through a V4 signed URL bound to its content type and a
`0,<100 MiB>` length range, valid ten minutes, so the bytes never pass
through swarm-api. swarm-api then starts one execution of
`swarm-plugin-intake` naming the intake. The job unpacks and pins with no
network and writes `staging/<intake_id>/report.json`.

THE CALLBACK. The job cannot reach swarm-api; the bucket's finalize
notification on that report is pushed by Pub/Sub with an OIDC token for
SWARM_PLUGIN_INTAKE_AUDIENCE from an account in SWARM_PLUGIN_INTAKE_CALLERS,
and nothing else is admitted (with neither configured it admits nobody,
503). The intake is also finished when its state is read and its report is
there, and refused once it is past its deadline with no report: nothing
waits on the job (invariant 4). Finishing binds the report to the MAC'd
intake document, re-hashes the staged tar, copies it by digest into the
scope's prefix (`ifGenerationMatch=0`) and writes the version
(swarm_api.pluginstore). For a bundled install's candidate it first keeps
the quarantined source archive the job checked, re-hashed, at
`platform/sources/<sha256>.tar` (once per digest), where the bundle check
reads it back (section 1.4.3).

BUNDLED INSTALLS (sections 1.4.1, 1.4.8). With SWARM_BUILTINS_MANAGED on,
the first catalogue read seeds `builtin:gstack` and `builtin:security-audit`
(plugincatalogue.seed), listed among the platform installs. Update works on
them as on any plugin (its repository named by the admin while the current
version names none) and Apply starts a bundle build; Remove is refused
(`plugin_built_in`): a workspace disallows one instead.

UPDATE (section 9.1, the owner's Q4). A candidate never runs until one admin
applies it, quoting the digest of the diff GET answered, so what applies is
exactly what they read; a change to hooks, servers, hosts or secret fields
also needs the plugin's name typed. A step already submitted keeps the
version it was signed with.

Nothing here takes a command, an image, a resource spec or a backend
parameter from a caller, and nothing returns a credential (invariant 10).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, TypeVar

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from swarm_common.models import Tenant

from .. import pluginstore as ps
from ..auth import AuthContext, GoogleTokenVerifier
from ..deps import AppContext, admin_auth, current_auth, get_context
from ..errors import Forbidden, NotFound, RoleAdminRequired, Unauthenticated, UpstreamUnavailable
from ..judgment import MAC_FIELD, block_verifies, settings_mac
from ..mergesettings import group_admin, is_owner
from .. import pluginbundle
from ..plugincatalogue import BUILT_IN_PREFIX, builtin_entries, builtins_managed, ensure_seeded
from ..qasettings import authorize_read, can_edit
from ..roles import ADMIN
from ..schemas import StrictModel
from . import plugin_bundles

log = logging.getLogger(__name__)

router = APIRouter(tags=["plugins"])

PLATFORM_BASE = "/v1/admin/plugin-catalogue"
WORKSPACE_BASE = "/v1/workspaces/{tenant_id}/plugin-catalogue"
CALLBACK_ROUTE = "/v1/internal/plugin-intake/report"

BUCKET_ENV = "SWARM_PLUGINS_BUCKET"
JOB_ENV = "SWARM_PLUGIN_INTAKE_JOB"
AUDIENCE_ENV = "SWARM_PLUGIN_INTAKE_AUDIENCE"
CALLERS_ENV = "SWARM_PLUGIN_INTAKE_CALLERS"
#: terraform/infra/plugins.tf's job name.
DEFAULT_JOB = "swarm-plugin-intake"

UPLOAD_URL_SECONDS = 600
#: An upload's content type, by its name's ending; the signed URL binds it.
UPLOAD_TYPES = ((".tar.gz", "application/gzip"), (".tgz", "application/gzip"), (".zip", "application/zip"))
MARKETPLACE_FILE = ".claude-plugin/marketplace.json"
MAX_MARKETPLACE_BYTES = 1024 * 1024

_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._@+-]{1,255}$")
_REPORT_OBJECT = re.compile(r"^staging/([0-9a-f]{32})/report\.json$")


# --- configuration -----------------------------------------------------------


@dataclass(frozen=True)
class IntakeSettings:
    bucket: str
    job: str
    audience: str
    callers: frozenset[str]


def intake_settings(environ: Mapping[str, str] | None = None) -> IntakeSettings:
    # Each read spelled `(os.environ if environ is None else environ).get(...)`,
    # the shape scripts/lib/check-env-parity.sh matches against what
    # terraform/infra/locals.tf sets.
    callers = (os.environ if environ is None else environ).get(CALLERS_ENV, "")
    return IntakeSettings(
        bucket=(os.environ if environ is None else environ).get(BUCKET_ENV, "").strip(),
        job=(os.environ if environ is None else environ).get(JOB_ENV, "").strip() or DEFAULT_JOB,
        audience=(os.environ if environ is None else environ).get(AUDIENCE_ENV, "").strip(),
        callers=frozenset(c.strip().lower() for c in callers.split(",") if c.strip()),
    )


# --- what this module talks to ------------------------------------------------


class ArchiveTooLarge(Exception):
    """The source is larger than the packed cap; nothing was stored."""


class SourceUnreachable(Exception):
    """GitHub did not answer as expected. Carries no upstream text."""


#: Why an intake's report was not finished, as the log names it: fixed codes
#: beside the exception's class, never its message (which can carry report
#: content or a transport's text).
REASON_STORAGE = "storage_unavailable"
REASON_REPORT = "report_invalid"
REASON_UNEXPECTED = "finish_failed"


class IntakeUnavailable(Exception):
    """The plugins bucket could not be read or copied while finishing an
    intake. Transient: the intake stays running, the callback answers 503 so
    Pub/Sub redelivers, the console's status read answers 503."""

    def __init__(self, reason: str, cause: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


_T = TypeVar("_T")


def _storage(step: Callable[[], _T]) -> _T:
    try:
        return step()
    except Exception as exc:  # noqa: BLE001 -- the message may carry the transport's text; never passed on
        raise IntakeUnavailable(REASON_STORAGE, type(exc).__name__) from None


class PluginBucket(Protocol):
    def put_quarantine(self, intake_id: str, chunks: Iterable[bytes], limit: int) -> int: ...

    def quarantine_size(self, intake_id: str) -> int | None: ...

    def upload_url(self, intake_id: str, content_type: str, limit: int, seconds: int) -> str: ...

    def read_report(self, intake_id: str, limit: int) -> bytes | None: ...

    def staged_digest(self, intake_id: str) -> tuple[str, int, int] | None: ...

    def pin(self, intake_id: str, object_name: str, generation: int, sha256: str) -> bool: ...

    def keep_source(self, intake_id: str, object_name: str, sha256: str) -> bool: ...

    def clean(self, intake_id: str) -> None: ...


class IntakeJobs(Protocol):
    def start(self, intake_id: str, env: Mapping[str, str]) -> None: ...


class GitHubSource(Protocol):
    def resolve(self, repository: str, ref: str, token: str | None) -> str | None: ...

    def read_file(self, repository: str, commit: str, path: str, token: str | None, limit: int) -> bytes | None: ...

    def tarball(self, repository: str, commit: str, token: str | None) -> Iterable[bytes]: ...


def _quarantine(intake_id: str) -> str:
    return f"quarantine/{intake_id}"


def _staged(intake_id: str, name: str) -> str:
    return f"staging/{intake_id}/{name}"


class GcsPluginBucket:
    """The plugins bucket through google-cloud-storage, built lazily so
    create_app() needs no credentials."""

    CHUNK = 1024 * 1024

    def __init__(self, bucket: str, project: str) -> None:
        self._name = bucket
        self._project = project
        self._bucket: Any = None

    def _b(self) -> Any:
        if self._bucket is None:
            from google.cloud import storage  # lazy: no client at import

            self._bucket = storage.Client(project=self._project).bucket(self._name)
        return self._bucket

    def put_quarantine(self, intake_id: str, chunks: Iterable[bytes], limit: int) -> int:
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=8 * self.CHUNK) as spool:
            for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise ArchiveTooLarge()
                spool.write(chunk)
            spool.seek(0)
            self._b().blob(_quarantine(intake_id)).upload_from_file(
                spool, size=size, content_type="application/octet-stream", if_generation_match=0)
        return size

    def quarantine_size(self, intake_id: str) -> int | None:
        blob = self._b().get_blob(_quarantine(intake_id))
        return None if blob is None else int(blob.size or 0)

    def upload_url(self, intake_id: str, content_type: str, limit: int, seconds: int) -> str:
        import google.auth
        from google.auth.transport.requests import Request as AuthRequest

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(AuthRequest())
        # On Cloud Run the credentials hold no private key: the URL is signed
        # through IAM signBlob as swarm-api's own account.
        return self._b().blob(_quarantine(intake_id)).generate_signed_url(
            version="v4", method="PUT", expiration=timedelta(seconds=seconds), content_type=content_type,
            headers={"x-goog-content-length-range": f"0,{limit}"},
            service_account_email=getattr(credentials, "service_account_email", None),
            access_token=credentials.token)

    def read_report(self, intake_id: str, limit: int) -> bytes | None:
        blob = self._b().get_blob(_staged(intake_id, "report.json"))
        if blob is None:
            return None
        if int(blob.size or 0) > limit:
            return b"\0" * (limit + 1)
        return blob.download_as_bytes(if_generation_match=blob.generation)

    def staged_digest(self, intake_id: str) -> tuple[str, int, int] | None:
        import hashlib

        blob = self._b().get_blob(_staged(intake_id, "runnable.tar"))
        if blob is None:
            return None
        digest, size = hashlib.sha256(), 0
        # get_blob's blob already names its generation, so the reader reads
        # exactly that one; BlobReader takes no `generation` argument (it
        # raises ValueError for one), only the generation preconditions.
        with blob.open("rb", chunk_size=self.CHUNK, if_generation_match=blob.generation) as handle:
            while True:
                chunk = handle.read(self.CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size, int(blob.generation)

    def pin(self, intake_id: str, object_name: str, generation: int, sha256: str) -> bool:
        import hashlib

        from google.api_core import exceptions as gexc

        bucket = self._b()
        source = bucket.blob(_staged(intake_id, "runnable.tar"), generation=generation)
        try:
            bucket.copy_blob(source, bucket, object_name, if_generation_match=0, source_generation=generation)
            return True
        except gexc.PreconditionFailed:
            pass
        # Already there: only swarm-api writes the scope prefixes, after
        # hashing, so it is the same bytes from an earlier intake; checked.
        existing = bucket.get_blob(object_name)
        if existing is None:
            return False
        digest = hashlib.sha256()
        with existing.open("rb", chunk_size=self.CHUNK, if_generation_match=existing.generation) as handle:
            for chunk in iter(lambda: handle.read(self.CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest() == sha256

    def keep_source(self, intake_id: str, object_name: str, sha256: str) -> bool:
        """Copy the quarantined archive the job checked to `object_name`,
        once: re-hashed first, at the generation hashed; a copy already
        there (`ifGenerationMatch=0` failed) is read back and compared. False
        when either digest is not `sha256`."""
        import hashlib

        from google.api_core import exceptions as gexc

        bucket = self._b()

        def digest_of(blob: Any) -> str:
            digest = hashlib.sha256()
            with blob.open("rb", chunk_size=self.CHUNK, if_generation_match=blob.generation) as handle:
                for chunk in iter(lambda: handle.read(self.CHUNK), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        existing = bucket.get_blob(object_name)
        if existing is not None:
            return digest_of(existing) == sha256
        source = bucket.get_blob(_quarantine(intake_id))
        if source is None or digest_of(source) != sha256:
            return False
        try:
            bucket.copy_blob(source, bucket, object_name, if_generation_match=0,
                             source_generation=source.generation)
            return True
        except gexc.PreconditionFailed:
            pass
        # Another finish of the same archive wrote it first: checked.
        existing = bucket.get_blob(object_name)
        return existing is not None and digest_of(existing) == sha256

    def clean(self, intake_id: str) -> None:
        from google.api_core import exceptions as gexc

        for name in (_staged(intake_id, "runnable.tar"), _staged(intake_id, "report.json"), _quarantine(intake_id)):
            try:
                self._b().blob(name).delete()
            except gexc.NotFound:
                pass


class CloudRunIntakeJobs:
    """Starts one execution of the intake job with this intake's environment
    (`run.jobs.runWithOverrides` on that job alone, plugins.tf)."""

    def __init__(self, project: str, region: str, job: str) -> None:
        self._url = (f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
                     f"/jobs/{job}:run")
        self._session: Any = None

    def start(self, intake_id: str, env: Mapping[str, str]) -> None:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        body = {"overrides": {"taskCount": 1, "containerOverrides": [
            {"env": [{"name": k, "value": v} for k, v in sorted(env.items())]}]}}
        answer = self._session.post(self._url, json=body, timeout=30)
        if answer.status_code >= 300:
            raise RuntimeError(f"intake job not started: HTTP {answer.status_code}")


class GitHubApi:
    """GitHub's REST API over urllib: resolve a ref, read one file, stream a tarball."""

    BASE = "https://api.github.com"
    TIMEOUT = 30

    def _open(self, path: str, token: str | None, accept: str) -> Any:
        request = urllib.request.Request(self.BASE + path, headers={
            "Accept": accept, "User-Agent": "swarm-api", "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {})})
        return urllib.request.urlopen(request, timeout=self.TIMEOUT)  # noqa: S310 -- fixed https host

    def resolve(self, repository: str, ref: str, token: str | None) -> str | None:
        try:
            with self._open(f"/repos/{repository}/commits/{urllib.parse.quote(ref, safe='')}", token,
                            "application/vnd.github.sha") as answer:
                commit = answer.read(64).decode("ascii", "replace").strip()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 422):
                return None
            raise SourceUnreachable() from None
        except (urllib.error.URLError, OSError):
            raise SourceUnreachable() from None
        return commit if ps.COMMIT.match(commit) else None

    def read_file(self, repository: str, commit: str, path: str, token: str | None, limit: int) -> bytes | None:
        quoted = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
        try:
            with self._open(f"/repos/{repository}/contents/{quoted}?ref={commit}", token,
                            "application/vnd.github.raw") as answer:
                data = answer.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise SourceUnreachable() from None
        except (urllib.error.URLError, OSError):
            raise SourceUnreachable() from None
        return None if len(data) > limit else data

    def tarball(self, repository: str, commit: str, token: str | None) -> Iterator[bytes]:
        try:
            answer = self._open(f"/repos/{repository}/tarball/{commit}", token, "application/vnd.github+json")
        except (urllib.error.URLError, OSError):
            raise SourceUnreachable() from None
        with answer:
            while True:
                try:
                    chunk = answer.read(1024 * 1024)
                except OSError:
                    raise SourceUnreachable() from None
                if not chunk:
                    return
                yield chunk


def _bucket(request: Request, ctx: AppContext) -> PluginBucket:
    bucket = getattr(request.app.state, "plugin_bucket", None)
    if bucket is None:
        name = intake_settings().bucket
        if not name:
            raise UpstreamUnavailable(f"the plugins bucket is not configured ({BUCKET_ENV})")
        bucket = GcsPluginBucket(name, ctx.settings.project_id)
        request.app.state.plugin_bucket = bucket
    return bucket


def _jobs(request: Request, ctx: AppContext) -> IntakeJobs:
    jobs = getattr(request.app.state, "plugin_intake_jobs", None)
    if jobs is None:
        jobs = CloudRunIntakeJobs(ctx.settings.project_id, ctx.settings.core.region, intake_settings().job)
        request.app.state.plugin_intake_jobs = jobs
    return jobs


def _github(request: Request) -> GitHubSource:
    github = getattr(request.app.state, "plugin_github", None)
    if github is None:
        github = GitHubApi()
        request.app.state.plugin_github = github
    return github


def _store(ctx: AppContext) -> ps.PluginStore:
    return ps.PluginStore(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings))


def _by(auth: AuthContext) -> str:
    return (auth.email or "").strip().lower()


# --- who --------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    kind: str
    tenant_id: str | None

    def owns(self, record: Mapping[str, Any]) -> bool:
        return record.get("scope") == self.kind and record.get("tenant_id") == self.tenant_id


PLATFORM_SCOPE = Scope(ps.PLATFORM, None)


def _workspace_read(auth: AuthContext, ctx: AppContext, tenant_id: str) -> tuple[Scope, Tenant]:
    tenant = authorize_read(auth, ctx.store.get_tenant(tenant_id), tenant_id)
    return Scope(ps.WORKSPACE, tenant_id), tenant


def _is_workspace_admin(auth: AuthContext, ctx: AppContext, tenant: Tenant, *, fresh: bool) -> bool:
    """The workspace's owner or admin (Q3): a platform admin alone is neither."""
    return is_owner(auth, tenant) or group_admin(ctx.roles, auth, tenant, fresh=fresh)


def _workspace_admin(auth: AuthContext, ctx: AppContext, tenant_id: str) -> Scope:
    scope, tenant = _workspace_read(auth, ctx, tenant_id)
    if not _is_workspace_admin(auth, ctx, tenant, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin may install, update or apply its private plugins. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})
    return scope


def _workspace_remover(auth: AuthContext, ctx: AppContext, tenant_id: str) -> Scope:
    scope, tenant = _workspace_read(auth, ctx, tenant_id)
    if not can_edit(auth, tenant, ctx.roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may remove its private plugins. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})
    return scope


# --- bodies -----------------------------------------------------------------


class SourceBody(StrictModel):
    #: `github` or `marketplace`; anything else (a bare git URL, npm, pip) is refused.
    kind: str = Field(max_length=32)
    repository: str = Field(max_length=141)
    ref: str = Field(max_length=200)
    #: The plugin's folder inside the repository (github only).
    subdir: str = Field(default="", max_length=1024)
    #: The entry's name in the marketplace (marketplace only).
    plugin: str | None = Field(default=None, max_length=64)


class InstallBody(StrictModel):
    source: SourceBody
    #: The admin's overlay (section 6.3), checked by the intake job.
    overlay: dict[str, Any] | None = None


class UploadBody(StrictModel):
    filename: str = Field(max_length=255)
    size: int = Field(ge=1)
    subdir: str = Field(default="", max_length=1024)
    overlay: dict[str, Any] | None = None


class UpdateBody(StrictModel):
    #: A GitHub or marketplace install: the ref to resolve (default: its own).
    ref: str | None = Field(default=None, max_length=200)
    #: A bundled install only (section 1.4.3): the GitHub repository the new
    #: version comes from. Its seed names none (the release image's pins
    #: carry no upstream location), so the first update names one; later
    #: ones default to the current version's.
    repository: str | None = Field(default=None, max_length=141)
    #: An upload install: the new archive.
    upload: UploadBody | None = None
    #: Default: the overlay the current version was installed with.
    overlay: dict[str, Any] | None = None


class ApplyBody(StrictModel):
    version_id: str = Field(max_length=16)
    #: The `diff_sha256` GET .../diff answered: what the admin read.
    diff_sha256: str = Field(max_length=64)
    #: The plugin's name, typed, when the diff is marked.
    confirm_name: str | None = Field(default=None, max_length=64)


# --- the steps ----------------------------------------------------------------


def _overlay_text(overlay: Mapping[str, Any] | None) -> str | None:
    if overlay is None:
        return None
    text = json.dumps(overlay, sort_keys=True, separators=(",", ":"))
    if len(text.encode("utf-8")) > ps.MAX_OVERLAY_BYTES:
        raise ps.PluginRefused("overlay_too_large")
    return text


def _relative(path: str, code: str) -> str:
    path = path.strip().strip("/")
    if not path:
        return ""
    parts = path.split("/")
    if any(p in ("", ".", "..") or not _SEGMENT.match(p) for p in parts):
        raise ps.PluginRefused(code)
    return "/".join(parts)


def _check_github(repository: str, ref: str) -> None:
    if not _REPOSITORY.match(repository):
        raise ps.PluginRefused("repository_invalid")
    if not _REF.match(ref) or ".." in ref:
        raise ps.PluginRefused("ref_invalid")


def _token(ctx: AppContext, scope: Scope, repository: str) -> str | None:
    """A workspace's private repository: a contents-only read token from ITS
    installation, for this one repository, used once
    (`InstallationTokens.contents_token`). None reads it as public."""
    source = ctx.forge_installations
    if scope.kind != ps.WORKSPACE or source is None or not hasattr(source, "contents_token"):
        return None
    owner, repo = repository.split("/", 1)
    try:
        return source.contents_token(scope.tenant_id, owner, repo)
    except Exception:  # noqa: BLE001 -- no installation: the repository is read as public
        return None


def _resolve(github: GitHubSource, repository: str, ref: str, token: str | None) -> str:
    try:
        commit = github.resolve(repository, ref, token)
    except SourceUnreachable:
        raise ps.PluginRefused("source_unreachable", status=502) from None
    if commit is None or not ps.COMMIT.match(commit):
        raise ps.PluginRefused("ref_not_found")
    return commit


def marketplace_source(document: Any, plugin: str) -> dict[str, Any]:
    """The marketplace entry named `plugin`, as `{kind: path, path}` or
    `{kind: github, repository, ref|sha, path}`. Refuses npm, pip, a bare git
    URL and any other source (the owner's answer to Q10)."""
    if not isinstance(document, dict) or not isinstance(document.get("plugins"), list):
        raise ps.PluginRefused("marketplace_missing")
    entry = next((p for p in document["plugins"] if isinstance(p, dict) and p.get("name") == plugin), None)
    if entry is None:
        raise ps.PluginRefused("marketplace_plugin_missing")
    source = entry.get("source")
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    if isinstance(source, str):
        root = metadata.get("pluginRoot") if isinstance(metadata.get("pluginRoot"), str) else ""
        if source.startswith(("http://", "https://", "git@", "git://", "ssh://")) or source.endswith(".git"):
            raise ps.PluginRefused("marketplace_source_git")
        joined = "/".join(p for p in (root.strip("./"), source.strip()) if p)
        return {"kind": "path", "path": _relative(joined.removeprefix("./"), "marketplace_source_unknown")}
    if not isinstance(source, dict):
        raise ps.PluginRefused("marketplace_source_unknown")
    kind = source.get("source")
    if kind == "npm":
        raise ps.PluginRefused("marketplace_source_npm")
    if kind == "pip":
        raise ps.PluginRefused("marketplace_source_pip")
    if kind in ("url", "git", "git-subdir"):
        raise ps.PluginRefused("marketplace_source_git")
    if kind != "github":
        raise ps.PluginRefused("marketplace_source_unknown")
    repository, ref, sha = source.get("repo"), source.get("ref"), source.get("sha")
    if not isinstance(repository, str) or not _REPOSITORY.match(repository):
        raise ps.PluginRefused("marketplace_source_unknown")
    if sha is not None and not (isinstance(sha, str) and ps.COMMIT.match(sha)):
        raise ps.PluginRefused("marketplace_source_unknown")
    if ref is not None and not (isinstance(ref, str) and _REF.match(ref) and ".." not in ref):
        raise ps.PluginRefused("marketplace_source_unknown")
    path = source.get("path") if isinstance(source.get("path"), str) else ""
    return {"kind": "github", "repository": repository, "ref": ref, "sha": sha,
            "path": _relative(path, "marketplace_source_unknown")}


def _resolved_source(ctx: AppContext, github: GitHubSource, scope: Scope,
                     body: SourceBody) -> tuple[dict[str, Any], str | None]:
    """The requested source resolved to one repository and commit (and the
    folder inside it), and the token that reads it."""
    if body.kind not in ("github", "marketplace"):
        raise ps.PluginRefused("source_kind_refused", kind=None)
    repository, ref = body.repository.strip(), body.ref.strip()
    _check_github(repository, ref)
    subdir = _relative(body.subdir, "subdir_invalid")
    token = _token(ctx, scope, repository)
    commit = _resolve(github, repository, ref, token)
    if body.kind == "github":
        return {"kind": "github", "repository": repository, "ref": ref, "commit": commit,
                "subdir": subdir}, token
    if not body.plugin:
        raise ps.PluginRefused("marketplace_plugin_missing")
    try:
        raw = github.read_file(repository, commit, MARKETPLACE_FILE, token, MAX_MARKETPLACE_BYTES)
    except SourceUnreachable:
        raise ps.PluginRefused("source_unreachable", status=502) from None
    try:
        document = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        document = None
    entry = marketplace_source(document, body.plugin)
    marketplace = {"marketplace": repository, "marketplace_ref": ref, "marketplace_commit": commit,
                   "plugin": body.plugin}
    if entry["kind"] == "path":
        return {"kind": "marketplace", "repository": repository, "ref": ref, "commit": commit,
                "subdir": entry["path"], **marketplace}, token
    target = entry["repository"]
    target_token = token if target.lower() == repository.lower() else _token(ctx, scope, target)
    target_commit = entry["sha"] or _resolve(github, target, entry["ref"] or "HEAD", target_token)
    return {"kind": "marketplace", "repository": target, "ref": entry["ref"] or entry["sha"] or "HEAD",
            "commit": target_commit, "subdir": entry["path"], **marketplace}, target_token


def _job_env(intake: Mapping[str, Any]) -> dict[str, str]:
    env = {"SWARM_INTAKE_ID": intake["intake_id"], "SWARM_INTAKE_SCOPE": intake["scope"]}
    if intake.get("tenant_id"):
        env["SWARM_INTAKE_TENANT"] = intake["tenant_id"]
    if intake.get("subdir"):
        env["SWARM_INTAKE_SUBDIR"] = intake["subdir"]
    if intake.get("overlay"):
        env["SWARM_INTAKE_OVERLAY"] = intake["overlay"]
    return env


def _start_job(request: Request, ctx: AppContext, store: ps.PluginStore, intake: Mapping[str, Any]) -> None:
    try:
        _jobs(request, ctx).start(intake["intake_id"], _job_env(intake))
    except Exception as exc:  # noqa: BLE001 -- the message may carry the transport's text; never passed on
        log.warning("plugin intake job not started intake_id=%s (%s)", intake["intake_id"], type(exc).__name__)
        store.refuse(intake["intake_id"], "intake_start_failed")
        _clean(request, ctx, intake["intake_id"])
        raise ps.PluginRefused("intake_start_failed", status=503, intake_id=intake["intake_id"]) from None


def _clean(request: Request, ctx: AppContext, intake_id: str) -> None:
    try:
        _bucket(request, ctx).clean(intake_id)
    except Exception as exc:  # noqa: BLE001 -- leftovers age out after a day (plugins.tf)
        log.warning("plugin intake leftovers not deleted intake_id=%s (%s)", intake_id, type(exc).__name__)


def _fetch_and_start(request: Request, ctx: AppContext, scope: Scope, *, install_id: str | None,
                     source: SourceBody, overlay: str | None, by: str) -> dict[str, Any]:
    store = _store(ctx)
    store.require_key()
    github = _github(request)
    resolved, token = _resolved_source(ctx, github, scope, source)
    bucket = _bucket(request, ctx)
    intake_id = ps.new_intake_id()
    try:
        bucket.put_quarantine(intake_id, github.tarball(resolved["repository"], resolved["commit"], token),
                              ps.MAX_PACKED_BYTES)
    except ArchiveTooLarge:
        raise ps.PluginRefused("archive_too_large") from None
    except SourceUnreachable:
        raise ps.PluginRefused("source_unreachable", status=502) from None
    intake = store.open_intake(scope=scope.kind, tenant_id=scope.tenant_id, install_id=install_id,
                               source=resolved, subdir=resolved["subdir"], overlay=overlay, by=by,
                               awaiting_upload=False, intake_id=intake_id)
    _start_job(request, ctx, store, intake)
    return intake_view(intake)


def _upload_type(filename: str) -> str:
    name = filename.strip().lower()
    if "/" in name or "\\" in name:
        raise ps.PluginRefused("upload_name_invalid")
    for ending, content_type in UPLOAD_TYPES:
        if name.endswith(ending) and len(name) > len(ending):
            return content_type
    raise ps.PluginRefused("upload_name_invalid")


def _open_upload(request: Request, ctx: AppContext, scope: Scope, *, install_id: str | None,
                 body: UploadBody, overlay: str | None, by: str) -> dict[str, Any]:
    store = _store(ctx)
    store.require_key()
    content_type = _upload_type(body.filename)
    if body.size > ps.MAX_PACKED_BYTES:
        raise ps.PluginRefused("archive_too_large")
    subdir = _relative(body.subdir, "subdir_invalid")
    bucket = _bucket(request, ctx)
    intake = store.open_intake(
        scope=scope.kind, tenant_id=scope.tenant_id, install_id=install_id,
        source={"kind": "upload", "filename": body.filename.strip(), "commit": "upload", "uploaded_by": by},
        subdir=subdir, overlay=overlay, by=by, awaiting_upload=True)
    url = bucket.upload_url(intake["intake_id"], content_type, ps.MAX_PACKED_BYTES, UPLOAD_URL_SECONDS)
    return {
        "intake": intake_view(intake),
        "upload": {"method": "PUT", "url": url, "expires_in": UPLOAD_URL_SECONDS,
                   "headers": {"Content-Type": content_type,
                               "x-goog-content-length-range": f"0,{ps.MAX_PACKED_BYTES}"}},
    }


def finish_intake(request: Request, ctx: AppContext, store: ps.PluginStore,
                  intake: Mapping[str, Any]) -> dict[str, Any] | None:
    """Act on the intake job's report, if it is there: refuse, or pin and
    write the version. None while there is no report yet."""
    intake_id = intake["intake_id"]
    bucket = _bucket(request, ctx)
    raw = _storage(lambda: bucket.read_report(intake_id, ps.MAX_REPORT_BYTES))
    if raw is None:
        return None
    try:
        report = ps.parse_report(raw, intake)
    except ps.PluginRefused as refusal:
        log.warning("plugin intake report refused intake_id=%s reason=%s (PluginRefused)", intake_id, refusal.code)
        ended = store.refuse(intake_id, refusal.code)
        _clean(request, ctx, intake_id)
        return ended
    except _UNUSABLE as exc:
        return _unusable(request, ctx, store, intake_id, exc)
    if report["outcome"] == "refused":
        ended = store.refuse(intake_id, report["code"], entry_index=report["entry_index"])
        _clean(request, ctx, intake_id)
        return ended
    staged = _storage(lambda: bucket.staged_digest(intake_id))
    if staged is None or staged[0] != report["runnable_sha256"] or staged[1] != report["runnable_bytes"]:
        ended = store.refuse(intake_id, "intake_digest_mismatch")
        _clean(request, ctx, intake_id)
        return ended
    if not intake.get("install_id") and store.name_taken(intake["scope"], intake.get("tenant_id"),
                                                         report["manifest"]["name"]):
        ended = store.refuse(intake_id, "plugin_name_taken")
        _clean(request, ctx, intake_id)
        return ended
    if _keeps_source(store, intake):
        # A bundled install's candidate (section 1.4.3): the source archive
        # the job checked is kept by digest, once, where the bundle check
        # reads it back (pluginbundle.source_object) to compare the
        # security-audit layer against it.
        kept = _storage(lambda: bucket.keep_source(
            intake_id, pluginbundle.source_object(report["source_sha256"]), report["source_sha256"]))
        if not kept:
            ended = store.refuse(intake_id, "intake_digest_mismatch")
            _clean(request, ctx, intake_id)
            return ended
    object_name = f"{ps.scope_prefix(intake['scope'], intake.get('tenant_id'))}/{report['runnable_sha256']}.tar"
    if not _storage(lambda: bucket.pin(intake_id, object_name, staged[2], report["runnable_sha256"])):
        ended = store.refuse(intake_id, "pinned_copy_differs")
        _clean(request, ctx, intake_id)
        return ended
    try:
        store.pin(intake_id, {**report, "overlay_requested": intake.get("overlay")}, object_name)
    except ps.PluginRefused as refusal:
        store.refuse(intake_id, refusal.code)
    except _UNUSABLE as exc:
        return _unusable(request, ctx, store, intake_id, exc)
    _clean(request, ctx, intake_id)
    return store.intake(intake_id)


def _keeps_source(store: ps.PluginStore, intake: Mapping[str, Any]) -> bool:
    """An update intake of a bundled install, the one kind whose source
    archive is kept: its version is built from that archive (section 1.4.3)."""
    install_id = intake.get("install_id")
    return intake.get("scope") == ps.PLATFORM and isinstance(install_id, str) \
        and ps.is_bundled(store.install(install_id))


#: What a report swarm-api cannot use raises past `parse_report`: the same
#: bytes raise it again, so the intake ends refused instead of being retried.
#: A storage or Firestore failure is none of these and stays transient.
_UNUSABLE = (ValueError, TypeError, KeyError, RecursionError)


def _unusable(request: Request, ctx: AppContext, store: ps.PluginStore, intake_id: str,
              exc: BaseException) -> dict[str, Any] | None:
    log.warning("plugin intake report refused intake_id=%s reason=%s (%s)",
                intake_id, REASON_REPORT, type(exc).__name__)
    ended = store.refuse(intake_id, REASON_REPORT)
    _clean(request, ctx, intake_id)
    return ended or store.intake(intake_id)


def intake_view(intake: Mapping[str, Any]) -> dict[str, Any]:
    refusal = intake.get("refusal")
    code = refusal.get("code") if isinstance(refusal, Mapping) else None
    return {
        "intake_id": intake.get("intake_id"),
        "scope": intake.get("scope"),
        "tenant_id": intake.get("tenant_id"),
        "install_id": intake.get("install_id"),
        "state": intake.get("state"),
        "source": intake.get("source"),
        "version_id": intake.get("version_id"),
        # The code, and swarm-api's own sentence for it; a code the job
        # reported is looked up by the console, never echoed from the job.
        "refusal": None if code is None else {
            "code": code, "sentence": ps.REFUSALS.get(code),
            "entry_index": refusal.get("entry_index")},
        "deadline": intake.get("deadline"),
    }


def _intake_for(request: Request, ctx: AppContext, store: ps.PluginStore, scope: Scope,
                intake_id: str) -> dict[str, Any]:
    intake = store.intake(intake_id)
    if intake is None or not scope.owns(intake):
        raise NotFound("no such plugin intake", detail={"intake_id": intake_id})
    return intake


def _install_for(store: ps.PluginStore, scope: Scope, install_id: str, *, active: bool = False) -> dict[str, Any]:
    install = store.install(install_id)
    # A `builtin:` id is a code entry, refused, unless it is the bundled
    # install seeded with the built-ins switch on (section 1.4.1).
    if install_id.startswith(BUILT_IN_PREFIX) and not ps.is_bundled(install):
        raise ps.PluginRefused("plugin_built_in")
    if install is None or not scope.owns(install):
        raise NotFound("no such plugin install", detail={"install_id": install_id})
    if active and install.get("state") != ps.STATE_ACTIVE:
        raise ps.PluginRefused("install_not_active", status=409, install_id=install_id)
    return install


def _allowed_ids(ctx: AppContext, tenant_id: str) -> set[str]:
    """The install ids the workspace's signed `plugins.allowed` names."""
    snap = ctx.db.collection("workspace_settings").document(tenant_id).get()
    block = (snap.to_dict() or {}).get("plugins") if snap.exists else None
    if not isinstance(block, Mapping) or MAC_FIELD not in block:
        return set()
    if not block_verifies(tenant_id, "plugins", block, settings_mac(ctx.settings).keys):
        return set()
    return {n for n in block.get("allowed") or () if isinstance(n, str)}


def _is_allowed(install: Mapping[str, Any], allowed: set[str]) -> bool:
    """Allowed by id, or a bundled install by the name every Allowed list
    already holds (section 1.4.1: no workspace setting is rewritten)."""
    return install["install_id"] in allowed or (ps.is_bundled(install) and install.get("name") in allowed)


def _views(store: ps.PluginStore, installs: Iterable[Mapping[str, Any]], *,
           allowed: set[str] | None = None) -> list[dict[str, Any]]:
    return [ps.install_view(i, store.current(i), allowed=None if allowed is None else _is_allowed(i, allowed))
            for i in installs if i.get("state") == ps.STATE_ACTIVE]


def _built_in(store: ps.PluginStore) -> list[dict[str, object]]:
    """The code entries, as today, with the built-ins switch off. On, the
    two bundled installs are seeded on this read (section 1.4.1) and listed
    among the platform installs instead, so there is no code entry to show.
    With no settings key nothing can be seeded: the code entries stay."""
    if not builtins_managed():
        return builtin_entries()
    try:
        ensure_seeded(store)
    except ps.PluginRefused as refusal:
        log.warning("bundled plugins not seeded reason=%s", refusal.code)
        return builtin_entries()
    return []


def _versions_answer(store: ps.PluginStore, install: Mapping[str, Any]) -> dict[str, Any]:
    current = install.get("current_version")
    return {
        "install": ps.install_view(install, store.current(install)),
        "versions": [ps.version_view(v, current=v["version_id"] == current)
                     for v in store.versions(install["install_id"])],
    }


def _diff(store: ps.PluginStore, install: Mapping[str, Any], version_id: str) -> dict[str, Any]:
    target = store.version(install["install_id"], version_id)
    if target is None:
        raise ps.PluginRefused("version_not_found", status=404, version_id=version_id)
    current = store.current(install) or {}
    diff = ps.version_diff(current, target)
    return {"install_id": install["install_id"], "diff": diff, "diff_sha256": ps.diff_digest(diff)}


def _update(request: Request, ctx: AppContext, scope: Scope, install_id: str, body: UpdateBody,
            by: str) -> dict[str, Any]:
    store = _store(ctx)
    install = _install_for(store, scope, install_id, active=True)
    current = store.current(install) or {}
    overlay = _overlay_text(body.overlay) if body.overlay is not None else current.get("overlay_requested")
    source = install.get("source") or {}
    if ps.is_bundled(install):
        return _update_bundled(request, ctx, scope, install, current, body, overlay, by)
    if body.repository is not None:
        # Only a bundled install's update names its repository.
        raise ps.PluginRefused("repository_invalid")
    if source.get("kind") == "upload":
        if body.upload is None:
            raise ps.PluginRefused("upload_missing")
        upload = body.upload.model_copy(update={"overlay": None})
        return _open_upload(request, ctx, scope, install_id=install_id, body=upload, overlay=overlay, by=by)
    if source.get("kind") == "marketplace":
        requested = SourceBody(kind="marketplace", repository=source.get("marketplace") or "",
                               ref=body.ref or source.get("marketplace_ref") or "",
                               plugin=source.get("plugin"))
    else:
        requested = SourceBody(kind="github", repository=source.get("repository") or "",
                               ref=body.ref or source.get("ref") or "", subdir=source.get("subdir") or "")
    return _fetch_and_start(request, ctx, scope, install_id=install_id, source=requested,
                            overlay=overlay, by=by)


def _update_bundled(request: Request, ctx: AppContext, scope: Scope, install: Mapping[str, Any],
                    current: Mapping[str, Any], body: UpdateBody, overlay: str | None, by: str) -> dict[str, Any]:
    """An update of a bundled install (section 1.4.3): the same intake as any
    plugin's, from an upload or a GitHub repository at a ref. The repository
    is the body's, else the current version's when that came from GitHub;
    the seeded first version names none. Applying the candidate starts its
    bundle build (platform_apply)."""
    if body.upload is not None:
        upload = body.upload.model_copy(update={"overlay": None})
        return _open_upload(request, ctx, scope, install_id=install["install_id"], body=upload,
                            overlay=overlay, by=by)
    previous = current.get("source") if isinstance(current.get("source"), Mapping) else {}
    from_github = previous.get("kind") == "github"
    repository = body.repository or (previous.get("repository") if from_github else None)
    if not repository:
        raise ps.PluginRefused("repository_invalid")
    if not body.ref:
        raise ps.PluginRefused("ref_invalid")
    subdir = previous.get("subdir") if from_github and repository == previous.get("repository") else ""
    requested = SourceBody(kind="github", repository=repository, ref=body.ref, subdir=subdir or "")
    return _fetch_and_start(request, ctx, scope, install_id=install["install_id"], source=requested,
                            overlay=overlay, by=by)


def _apply(ctx: AppContext, scope: Scope, install_id: str, body: ApplyBody, by: str) -> dict[str, Any]:
    store = _store(ctx)
    install = _install_for(store, scope, install_id, active=True)
    shown = _diff(store, install, body.version_id)
    if shown["diff_sha256"] != body.diff_sha256.strip().lower():
        raise ps.PluginRefused("diff_changed", status=409, install_id=install_id)
    if shown["diff"]["needs_confirmation"]:
        if not body.confirm_name:
            raise ps.PluginRefused("confirmation_required", install_id=install_id)
        if body.confirm_name != install.get("name"):
            raise ps.PluginRefused("confirmation_mismatch", install_id=install_id)
    applied = store.apply(install_id, body.version_id, by=by)
    return _versions_answer(store, applied)


def _remove(ctx: AppContext, scope: Scope, install_id: str, confirm: str | None, by: str) -> dict[str, Any]:
    store = _store(ctx)
    install = _install_for(store, scope, install_id, active=True)
    if ps.is_bundled(install):
        # No Remove (section 1.4.8, Q17): a workspace disallows it instead,
        # and security_review.py and the step templates naming it keep working.
        raise ps.PluginRefused("plugin_built_in", install_id=install_id)
    if not confirm:
        raise ps.PluginRefused("confirmation_required", install_id=install_id)
    if confirm != install.get("name"):
        raise ps.PluginRefused("confirmation_mismatch", install_id=install_id)
    removed = store.remove(install_id, by=by)
    return ps.install_view(removed, store.current(removed))


def _status(request: Request, ctx: AppContext, scope: Scope, intake_id: str) -> dict[str, Any]:
    store = _store(ctx)
    intake = _intake_for(request, ctx, store, scope, intake_id)
    if intake.get("state") == ps.INTAKE_RUNNING:
        try:
            intake = finish_intake(request, ctx, store, intake) or intake
        except IntakeUnavailable as exc:
            log.warning("plugin intake status not finished intake_id=%s reason=%s (%s)",
                        intake_id, exc.reason, exc.cause)
            raise UpstreamUnavailable("the plugin intake report could not be read; try again") from None
    if store.timed_out(intake):
        code = "upload_missing" if intake.get("state") == ps.INTAKE_AWAITING_UPLOAD else "intake_timed_out"
        intake = store.refuse(intake_id, code) or intake
        _clean(request, ctx, intake_id)
    return intake_view(intake)


def _start(request: Request, ctx: AppContext, scope: Scope, intake_id: str) -> dict[str, Any]:
    store = _store(ctx)
    intake = _intake_for(request, ctx, store, scope, intake_id)
    if intake.get("state") != ps.INTAKE_AWAITING_UPLOAD:
        return intake_view(intake)
    size = _bucket(request, ctx).quarantine_size(intake_id)
    if size is None:
        raise ps.PluginRefused("upload_missing", status=409, intake_id=intake_id)
    if size > ps.MAX_PACKED_BYTES:
        store.refuse(intake_id, "archive_too_large")
        _clean(request, ctx, intake_id)
        raise ps.PluginRefused("archive_too_large", intake_id=intake_id)
    running = store.mark_running(intake_id)
    if running is None:
        return intake_view(store.intake(intake_id) or intake)
    _start_job(request, ctx, store, running)
    return intake_view(running)


# --- the platform catalogue (a platform admin's) ---------------------------------


@router.get(PLATFORM_BASE)
def platform_catalogue(auth: AuthContext = Depends(admin_auth), ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    workspace = [ps.install_view(i, store.current(i)) for i in store.installs(ps.WORKSPACE)
                 if i.get("state") == ps.STATE_ACTIVE]
    return {"scope": ps.PLATFORM, "built_in": _built_in(store),
            "installs": _views(store, store.installs(ps.PLATFORM)),
            # Read-only here except Remove, through the workspace's own route.
            "workspace_installs": workspace, "limits": dict(ps.LIMITS), "can_install": True}


@router.post(PLATFORM_BASE + "/installs", status_code=202)
def platform_install(body: InstallBody, request: Request, auth: AuthContext = Depends(admin_auth),
                     ctx: AppContext = Depends(get_context)) -> dict:
    return _fetch_and_start(request, ctx, PLATFORM_SCOPE, install_id=None, source=body.source,
                            overlay=_overlay_text(body.overlay), by=_by(auth))


@router.post(PLATFORM_BASE + "/uploads", status_code=201)
def platform_upload(body: UploadBody, request: Request, auth: AuthContext = Depends(admin_auth),
                    ctx: AppContext = Depends(get_context)) -> dict:
    return _open_upload(request, ctx, PLATFORM_SCOPE, install_id=None, body=body,
                        overlay=_overlay_text(body.overlay), by=_by(auth))


@router.post(PLATFORM_BASE + "/intakes/{intake_id}/start", status_code=202)
def platform_start(intake_id: str, request: Request, auth: AuthContext = Depends(admin_auth),
                   ctx: AppContext = Depends(get_context)) -> dict:
    return _start(request, ctx, PLATFORM_SCOPE, intake_id)


@router.get(PLATFORM_BASE + "/intakes/{intake_id}")
def platform_intake(intake_id: str, request: Request, auth: AuthContext = Depends(admin_auth),
                    ctx: AppContext = Depends(get_context)) -> dict:
    return _status(request, ctx, PLATFORM_SCOPE, intake_id)


@router.get(PLATFORM_BASE + "/installs/{install_id}/versions")
def platform_versions(install_id: str, auth: AuthContext = Depends(admin_auth),
                      ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    return _versions_answer(store, _install_for(store, PLATFORM_SCOPE, install_id))


@router.get(PLATFORM_BASE + "/installs/{install_id}/usage")
def platform_usage(install_id: str, auth: AuthContext = Depends(admin_auth),
                   ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    return ps.install_usage(ctx.db, _install_for(store, PLATFORM_SCOPE, install_id), store.keys)


@router.post(PLATFORM_BASE + "/installs/{install_id}/update", status_code=202)
def platform_update(install_id: str, body: UpdateBody, request: Request,
                    auth: AuthContext = Depends(admin_auth), ctx: AppContext = Depends(get_context)) -> dict:
    return _update(request, ctx, PLATFORM_SCOPE, install_id, body, _by(auth))


@router.get(PLATFORM_BASE + "/installs/{install_id}/diff")
def platform_diff(install_id: str, version_id: str = Query(max_length=16),
                  auth: AuthContext = Depends(admin_auth), ctx: AppContext = Depends(get_context)) -> dict:
    store = _store(ctx)
    return _diff(store, _install_for(store, PLATFORM_SCOPE, install_id), version_id)


@router.post(PLATFORM_BASE + "/installs/{install_id}/apply")
def platform_apply(install_id: str, body: ApplyBody, request: Request, response: Response,
                   auth: AuthContext = Depends(admin_auth), ctx: AppContext = Depends(get_context)) -> dict:
    # A bundled install's Apply starts its bundle build (section 1.4.3; the
    # owner's pick on Q21): the same path as the bundle route's Apply, so
    # `current_version` moves only when that bundle is ready. 202, as there.
    if ps.is_bundled(_store(ctx).install(install_id)):
        response.status_code = 202
        bundled = plugin_bundles.ApplyBody(**body.model_dump())
        return plugin_bundles.start_bundled_apply(request, ctx, install_id, bundled, _by(auth))
    return _apply(ctx, PLATFORM_SCOPE, install_id, body, _by(auth))


@router.delete(PLATFORM_BASE + "/installs/{install_id}")
def platform_remove(install_id: str, confirm: str | None = Query(default=None, max_length=64),
                    auth: AuthContext = Depends(admin_auth), ctx: AppContext = Depends(get_context)) -> dict:
    return _remove(ctx, PLATFORM_SCOPE, install_id, confirm, _by(auth))


# --- one workspace's catalogue ----------------------------------------------------


@router.get(WORKSPACE_BASE)
def workspace_catalogue(tenant_id: str, auth: AuthContext = Depends(current_auth),
                        ctx: AppContext = Depends(get_context)) -> dict:
    scope, tenant = _workspace_read(auth, ctx, tenant_id)
    store = _store(ctx)
    return {"scope": ps.WORKSPACE, "tenant_id": tenant_id, "built_in": _built_in(store),
            "platform_installs": _views(store, store.installs(ps.PLATFORM), allowed=_allowed_ids(ctx, tenant_id)),
            "installs": _views(store, store.installs(ps.WORKSPACE, tenant_id)),
            "limits": dict(ps.LIMITS),
            "can_install": _is_workspace_admin(auth, ctx, tenant, fresh=False)}


@router.post(WORKSPACE_BASE + "/installs", status_code=202)
def workspace_install(tenant_id: str, body: InstallBody, request: Request,
                      auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    scope = _workspace_admin(auth, ctx, tenant_id)
    return _fetch_and_start(request, ctx, scope, install_id=None, source=body.source,
                            overlay=_overlay_text(body.overlay), by=_by(auth))


@router.post(WORKSPACE_BASE + "/uploads", status_code=201)
def workspace_upload(tenant_id: str, body: UploadBody, request: Request,
                     auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    scope = _workspace_admin(auth, ctx, tenant_id)
    return _open_upload(request, ctx, scope, install_id=None, body=body,
                        overlay=_overlay_text(body.overlay), by=_by(auth))


@router.post(WORKSPACE_BASE + "/intakes/{intake_id}/start", status_code=202)
def workspace_start(tenant_id: str, intake_id: str, request: Request,
                    auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    return _start(request, ctx, _workspace_admin(auth, ctx, tenant_id), intake_id)


@router.get(WORKSPACE_BASE + "/intakes/{intake_id}")
def workspace_intake(tenant_id: str, intake_id: str, request: Request,
                     auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    scope, _tenant = _workspace_read(auth, ctx, tenant_id)
    return _status(request, ctx, scope, intake_id)


@router.get(WORKSPACE_BASE + "/installs/{install_id}/versions")
def workspace_versions(tenant_id: str, install_id: str, auth: AuthContext = Depends(current_auth),
                       ctx: AppContext = Depends(get_context)) -> dict:
    scope, _tenant = _workspace_read(auth, ctx, tenant_id)
    store = _store(ctx)
    return _versions_answer(store, _install_for(store, scope, install_id))


@router.get(WORKSPACE_BASE + "/installs/{install_id}/usage")
def workspace_usage(tenant_id: str, install_id: str, auth: AuthContext = Depends(current_auth),
                    ctx: AppContext = Depends(get_context)) -> dict:
    scope, _tenant = _workspace_read(auth, ctx, tenant_id)
    store = _store(ctx)
    return ps.install_usage(ctx.db, _install_for(store, scope, install_id), store.keys)


@router.post(WORKSPACE_BASE + "/installs/{install_id}/update", status_code=202)
def workspace_update(tenant_id: str, install_id: str, body: UpdateBody, request: Request,
                     auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    return _update(request, ctx, _workspace_admin(auth, ctx, tenant_id), install_id, body, _by(auth))


@router.get(WORKSPACE_BASE + "/installs/{install_id}/diff")
def workspace_diff(tenant_id: str, install_id: str, version_id: str = Query(max_length=16),
                   auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    scope, _tenant = _workspace_read(auth, ctx, tenant_id)
    store = _store(ctx)
    return _diff(store, _install_for(store, scope, install_id), version_id)


@router.post(WORKSPACE_BASE + "/installs/{install_id}/apply")
def workspace_apply(tenant_id: str, install_id: str, body: ApplyBody,
                    auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    return _apply(ctx, _workspace_admin(auth, ctx, tenant_id), install_id, body, _by(auth))


@router.delete(WORKSPACE_BASE + "/installs/{install_id}")
def workspace_remove(tenant_id: str, install_id: str, confirm: str | None = Query(default=None, max_length=64),
                     auth: AuthContext = Depends(current_auth), ctx: AppContext = Depends(get_context)) -> dict:
    return _remove(ctx, _workspace_remover(auth, ctx, tenant_id), install_id, confirm, _by(auth))


# --- the intake's callback ---------------------------------------------------------


def _callback_verifier(request: Request, audience: str) -> Any:
    verifier = getattr(request.app.state, "plugin_intake_verifier", None)
    if verifier is None:
        verifier = GoogleTokenVerifier(audience, require_audience=True)
        request.app.state.plugin_intake_verifier = verifier
    return verifier


def _object_of(envelope: Any) -> tuple[str | None, str | None, str | None]:
    """objectId, bucketId and eventType of a Pub/Sub push of a Cloud Storage notification."""
    message = envelope.get("message") if isinstance(envelope, dict) else None
    attributes = message.get("attributes") if isinstance(message, dict) else None
    if not isinstance(attributes, dict):
        return None, None, None
    values = (attributes.get("objectId"), attributes.get("bucketId"), attributes.get("eventType"))
    return tuple(v if isinstance(v, str) else None for v in values)  # type: ignore[return-value]


@router.post(CALLBACK_ROUTE)
async def intake_report(request: Request) -> dict[str, Any]:
    app = request.app
    ctx: AppContext = app.state.ctx
    config = intake_settings()
    if not config.audience or not config.callers:
        raise UpstreamUnavailable("the plugin intake callback has no caller configured")
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("the plugin intake callback needs an ID token")
    try:
        claims = await run_in_threadpool(_callback_verifier(request, config.audience).verify, token.strip())
    except Exception:  # noqa: BLE001 -- the message may echo the token; never passed on
        raise Unauthenticated("the plugin intake callback's ID token was refused") from None
    email = str(claims.get("email") or "").strip().lower()
    if email not in config.callers or claims.get("email_verified") is False:
        raise Forbidden("the plugin intake callback is the bucket notification's alone")
    try:
        envelope = json.loads(await request.body())
    except ValueError:
        return {"ignored": True, "reason": "not_a_notification"}
    object_id, bucket_id, event = _object_of(envelope)
    match = _REPORT_OBJECT.match(object_id or "")
    if match is None or (event is not None and event != "OBJECT_FINALIZE") \
            or (config.bucket and bucket_id != config.bucket):
        # 200, so Pub/Sub does not redeliver a notification that is not ours.
        return {"ignored": True, "reason": "not_an_intake_report"}
    intake_id = match.group(1)

    def _finish() -> dict[str, Any]:
        store = _store(ctx)
        intake = store.intake(intake_id)
        if intake is None:
            return {"ignored": True, "reason": "unknown_intake"}
        if intake.get("state") != ps.INTAKE_RUNNING:
            _clean(request, ctx, intake_id)
            return {"ignored": True, "reason": "intake_ended", "intake_id": intake_id}
        ended = finish_intake(request, ctx, store, intake)
        return {"intake_id": intake_id, "state": (ended or intake).get("state")}

    try:
        return await run_in_threadpool(_finish)
    except ps.PluginRefused:
        raise
    except IntakeUnavailable as exc:
        log.warning("plugin intake report not processed intake_id=%s reason=%s (%s)",
                    intake_id, exc.reason, exc.cause)
        raise UpstreamUnavailable("the plugin intake report could not be processed; it is retried") from None
    except Exception as exc:  # noqa: BLE001 -- 503, so Pub/Sub redelivers
        log.warning("plugin intake report not processed intake_id=%s reason=%s (%s)",
                    intake_id, REASON_UNEXPECTED, type(exc).__name__)
        raise UpstreamUnavailable("the plugin intake report could not be processed; it is retried") from None
