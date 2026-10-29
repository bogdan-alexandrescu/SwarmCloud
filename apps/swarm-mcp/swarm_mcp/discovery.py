"""The deployment's discovery document, and the version skew it reveals.

ONE VALUE CONFIGURES THE PLUGIN (plan §6, D28). A teammate types the
deployment's address and nothing else. Every other value the bridge needs --
the Desktop OAuth client id, the IAP audience, the oldest plugin the
deployment accepts, and (only when the deployment's operator switched it on,
plan §6.3) the Desktop client's secret -- is read from

    GET https://<H>/.well-known/swarmcloud

which `swarm-discovery` serves without IAP (`apps/swarm-api/swarm_api/
discovery.py`, `schema: 1`). A value the user DID configure still wins: the
live deployment's three-value installs keep working whether or not their
deployment serves the document yet.

THE PLUGIN CAN RUN AHEAD OF ITS DEPLOYMENT (plan §6.5). The plugin is installed
from the release bucket, not from the deployment, so the two versions can
disagree. The rule: **a skew is always named, with both versions and the fix;
it is never a silent failure.** Two directions:

* the plugin is older than the deployment's `plugin.min_version`;
* the deployment is outside the range the plugin's release supports, which
  RELEASE-1 writes into the plugin bundle as `compat.json`
  (`{"plugin": V, "deployments": {"min": ..., "max_exclusive": ...}}`). A
  plugin run from a working copy has no `compat.json` and checks only the
  first direction.

The document is cached for an hour per deployment, and fetched again when the
API answers 401 (the client may have been rotated), 404 or 410 (the
deployment may have moved past the plugin). A document that cannot be read
is named with its address and status -- never replaced by a guessed version.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .client import SwarmError

PATH = "/.well-known/swarmcloud"

#: The document schema this bridge reads (`swarm_api.discovery.SCHEMA`).
SCHEMA = 1

#: How long one deployment's document is trusted (plan §6.5).
TTL_SECONDS = 3600.0

#: The API statuses that make the bridge read the document again before it
#: answers: 401 (a rotated client), 404 and 410 (a route the deployment no
#: longer has, the first sign it moved past this plugin).
REFETCH_STATUSES = (401, 404, 410)

#: The exit status of a command that could not read the document (plan §6.5).
EXIT_UNREACHABLE = 3

#: The fixes the skew messages name. One spelling each.
UPDATE_PLUGIN = "claude plugin update sc@swarmcloud"
RELEASE_MARKETPLACE = "https://storage.googleapis.com/swarmcloud/releases/{version}/marketplace.json"

#: Where the plugin's bundle keeps its release's supported deployment range.
COMPAT_FILE = "compat.json"
PLUGIN_ROOT_ENV = "SWARM_SC_PLUGIN_ROOT"


class DiscoveryUnreachable(SwarmError):
    """The document could not be read, or read as something it is not."""

    exit_code = EXIT_UNREACHABLE


class VersionSkew(SwarmError):
    """The plugin and the deployment do not support each other."""


@dataclass(frozen=True)
class Document:
    url: str
    version: str
    name: str = ""
    console_url: str = ""
    api_url: str = ""
    oauth_client_id: str = ""
    iap_audience: str = ""
    plugin_min_version: str = ""
    #: Served only while `discovery_serves_client_secret` is on (plan §6.3).
    oauth_client_secret: str = field(default="", repr=False)


@dataclass(frozen=True)
class Supported:
    """The deployment versions a plugin release supports: `min` inclusive,
    `max_exclusive` exclusive; an empty bound is open."""

    min: str = ""
    max_exclusive: str = ""


def document_url(deployment_url: str) -> str:
    parts = urllib.parse.urlsplit(deployment_url.strip())
    return urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, PATH, "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is IAP's sign-in page: the document is not being served."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def http_get(url: str, timeout: float) -> tuple[int, bytes]:
    """GET `url` with no credential; (status, body). Never follows a redirect."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return int(response.status), response.read(65536)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DiscoveryUnreachable(f"cannot read {url} ({reason})") from None


Getter = Callable[[str, float], "tuple[int, bytes]"]


def fetch(deployment_url: str, *, get: Getter | None = None, timeout: float = 10.0) -> Document:
    """The deployment's document, or `DiscoveryUnreachable` naming why."""
    url = document_url(deployment_url)
    status, body = (get or http_get)(url, timeout)
    if status != 200:
        raise DiscoveryUnreachable(f"cannot read {url} ({status})", status=status)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise DiscoveryUnreachable(f"cannot read {url} (200, not JSON)", status=status) from None
    if not isinstance(data, dict):
        raise DiscoveryUnreachable(f"cannot read {url} (200, not an object)", status=status)
    if data.get("schema") != SCHEMA:
        raise DiscoveryUnreachable(
            f"cannot read {url} (schema {data.get('schema')!r}; this plugin reads schema {SCHEMA}). "
            f"Update it: {UPDATE_PLUGIN}",
            status=status,
        )
    version = str(data.get("version") or "").strip()
    if not version:
        raise DiscoveryUnreachable(f"cannot read {url} (200, no version)", status=status)
    plugin = data.get("plugin") if isinstance(data.get("plugin"), dict) else {}
    return Document(
        url=url,
        version=version,
        name=str(data.get("name") or ""),
        console_url=str(data.get("console_url") or ""),
        api_url=str(data.get("api_url") or ""),
        oauth_client_id=str(data.get("oauth_client_id") or "").strip(),
        iap_audience=str(data.get("iap_audience") or "").strip(),
        plugin_min_version=str(plugin.get("min_version") or "").strip(),
        oauth_client_secret=str(data.get("oauth_client_secret") or "").strip(),
    )


class Cache:
    """One document per deployment, trusted for `ttl` seconds."""

    def __init__(
        self,
        *,
        ttl: float = TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        get: Getter | None = None,
    ) -> None:
        self.ttl = ttl
        self.clock = clock
        self.get_url = get
        self._lock = threading.Lock()
        self._held: dict[str, tuple[float, Document]] = {}

    def get(self, deployment_url: str) -> Document:
        key = document_url(deployment_url)
        with self._lock:
            held = self._held.get(key)
            if held is not None and self.clock() - held[0] < self.ttl:
                return held[1]
        document = fetch(deployment_url, get=self.get_url)
        with self._lock:
            self._held[key] = (self.clock(), document)
        return document

    def refetch(self, deployment_url: str) -> Document:
        self.invalidate(deployment_url)
        return self.get(deployment_url)

    def invalidate(self, deployment_url: str) -> None:
        with self._lock:
            self._held.pop(document_url(deployment_url), None)


#: The process's cache: the bridge is one long process, so one read an hour.
CACHE = Cache()


# -- versions -----------------------------------------------------------------

_VERSION = re.compile(r"\A[vV]?(\d+(?:\.\d+)*)(?:[-+.]?(.*))?\Z")


def version_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """A sortable key: the numeric release, then a pre-release before its release.

    `1.2.0-rc1` < `1.2.0` < `1.2.1`; trailing zeros do not count (`1.2` ==
    `1.2.0`). A version with no leading number sorts before every other, so a
    garbled value is never read as new enough.
    """
    match = _VERSION.match((version or "").strip())
    if not match:
        return ((), 0, version or "")
    numbers = [int(n) for n in match.group(1).split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    rest = (match.group(2) or "").strip()
    return (tuple(numbers), 0 if rest else 1, rest)


def older(a: str, b: str) -> bool:
    return version_key(a) < version_key(b)


def plugin_version(environ: Mapping[str, str] | None = None) -> str:
    """The plugin's version: its bundle's `plugin.json`, else the bridge's own."""
    environ = os.environ if environ is None else environ
    root = (environ.get(PLUGIN_ROOT_ENV) or "").strip()
    if root and "${" not in root:
        try:
            manifest = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text())
            version = str(manifest.get("version") or "").strip()
            if version:
                return version
        except (OSError, ValueError):
            pass
    from importlib import metadata

    try:
        return metadata.version("swarm-mcp")
    except metadata.PackageNotFoundError:
        return "0"


def supported(environ: Mapping[str, str] | None = None) -> Supported | None:
    """The deployment range the plugin's release supports, from its bundle's
    `compat.json`; None for a plugin without one (a working copy)."""
    environ = os.environ if environ is None else environ
    root = (environ.get(PLUGIN_ROOT_ENV) or "").strip()
    if not root or "${" in root:
        return None
    try:
        data = json.loads((Path(root) / COMPAT_FILE).read_text())
    except (OSError, ValueError):
        return None
    deployments = data.get("deployments") if isinstance(data, dict) else None
    if not isinstance(deployments, dict):
        return None
    return Supported(
        min=str(deployments.get("min") or "").strip(),
        max_exclusive=str(deployments.get("max_exclusive") or "").strip(),
    )


def _range_text(span: Supported) -> str:
    low = span.min or "any"
    high = f"before {span.max_exclusive}" if span.max_exclusive else "any later"
    return f"{low} to {high}"


def skew(document: Document, plugin: str, span: Supported | None) -> str | None:
    """The skew message for this plugin against this deployment, or None.

    Both messages name both versions and the fix (plan §6.5).
    """
    deployment = document.version
    if document.plugin_min_version and older(plugin, document.plugin_min_version):
        return (
            f"sc plugin {plugin} is older than this deployment ({deployment}) accepts "
            f"(needs ≥ {document.plugin_min_version}). Update it: {UPDATE_PLUGIN}"
        )
    if span is not None and (
        (span.min and older(deployment, span.min))
        or (span.max_exclusive and not older(deployment, span.max_exclusive))
    ):
        return (
            f"this deployment runs SwarmCloud {deployment}; plugin {plugin} supports "
            f"{_range_text(span)}. Ask your admin to run: swarm upgrade --to {plugin}. "
            "Or pin the plugin to your deployment: claude plugin marketplace add "
            + RELEASE_MARKETPLACE.format(version=deployment)
        )
    return None


class Checker:
    """The skew check one client makes before each API call.

    `required` is True when the document supplies a value the user did not
    configure (the one-value install): then an unreadable document stops
    every call. When the user configured every value, a deployment that does
    not serve the document yet (one that predates it) is still reached, and
    the skew is reported as not checked, with the reason, by `status`.
    """

    def __init__(
        self,
        deployment_url: str,
        *,
        required: bool,
        cache: Cache | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.deployment_url = deployment_url
        self.required = required
        self.cache = cache if cache is not None else CACHE
        self.plugin = plugin_version(environ)
        self.span = supported(environ)
        #: Why the last check could not be made, or "" when it was.
        self.unchecked = ""
        #: When it could not, so a configured install asks again only after
        #: the same hour a document is trusted for, not on every call.
        self._unchecked_at: float | None = None

    def check(self, *, refetch: bool = False) -> Document | None:
        """Raise `VersionSkew` (or `DiscoveryUnreachable` when required)."""
        if (
            not refetch
            and self._unchecked_at is not None
            and self.cache.clock() - self._unchecked_at < self.cache.ttl
        ):
            return None
        try:
            document = (
                self.cache.refetch(self.deployment_url) if refetch else self.cache.get(self.deployment_url)
            )
        except DiscoveryUnreachable as exc:
            if self.required:
                raise
            self.unchecked = str(exc)
            self._unchecked_at = self.cache.clock()
            return None
        self.unchecked = ""
        self._unchecked_at = None
        message = skew(document, self.plugin, self.span)
        if message:
            raise VersionSkew(message)
        return document

    def status(self) -> str:
        """One line for `sc doctor`: both versions and the range."""
        try:
            document = self.cache.get(self.deployment_url)
        except DiscoveryUnreachable as exc:
            return f"plugin {self.plugin}; deployment version unknown: {exc}"
        span = _range_text(self.span) if self.span else "not published with this plugin"
        return (
            f"plugin {self.plugin} (supports deployments {span}); deployment {document.version} "
            f"(accepts plugins ≥ {document.plugin_min_version or 'any'})"
        )


def fill(
    url: str,
    *,
    client_id: str,
    client_secret: str,
    iap_audience: str = "",
    cache: Cache | None = None,
) -> tuple[str, str, str]:
    """(client_id, client_secret, iap_audience) with every empty value read
    from the document. A configured value always wins; the secret is taken
    only alongside the document's own client id."""
    document = (cache if cache is not None else CACHE).get(url)
    chosen_id = client_id or document.oauth_client_id
    chosen_secret = client_secret
    if not chosen_secret and chosen_id == document.oauth_client_id:
        chosen_secret = document.oauth_client_secret
    return chosen_id, chosen_secret, iap_audience or document.iap_audience

