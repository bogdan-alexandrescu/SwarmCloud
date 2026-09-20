"""The SwarmCloud API, from outside the platform.

IDENTITY IS BORROWED, NOT REINVENTED. The API sits behind IAP, so every request
needs a Google-signed ID token, and `scripts/lib/common.sh::id_token` already
knows the four places one can come from. Re-deriving that here would be a fifth
place for the rule to drift, so this asks `gcloud` the same question the shell
does and honours the same `SWARM_ID_TOKEN` override.

NOTHING IS CACHED TO DISK. An ID token is a bearer credential with an hour of
life. It lives in this process and nowhere else.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

#: An ID token is good for an hour. Re-minting a few minutes early costs one
#: subprocess and avoids the failure mode where a long `tail` dies at the
#: 59-minute mark with a 401 that looks like a permission problem.
_TOKEN_TTL_SECONDS = 45 * 60


class SwarmError(RuntimeError):
    """Something the operator needs to read, not a stack trace."""


def _run(argv: list[str], *, timeout: int = 60) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SwarmError(f"{argv[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise SwarmError(f"{argv[0]} timed out after {timeout}s") from exc
    if done.returncode != 0:
        raise SwarmError(f"{' '.join(argv[:2])} failed: {done.stderr.strip()[:400]}")
    return done.stdout.strip()


def resolve_api_url() -> str:
    """Where the control plane is.

    `SWARM_API_URL` wins, then `API_URL` -- the same variable the scripts read,
    so a shell that is already configured for `make smoke` needs nothing extra.
    Otherwise Cloud Run is asked, which is the one answer that cannot be stale.
    """
    for name in ("SWARM_API_URL", "API_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.rstrip("/")
    project = os.environ.get("PROJECT_ID", "").strip()
    region = os.environ.get("REGION", "").strip() or "us-central1"
    service = os.environ.get("API_SERVICE", "").strip() or "swarm-api"
    if not project:
        raise SwarmError(
            "set SWARM_API_URL, or PROJECT_ID so the URL can be read from Cloud Run"
        )
    url = _run([
        "gcloud", "run", "services", "describe", service,
        "--project", project, "--region", region, "--format=value(status.url)",
    ])
    if not url:
        raise SwarmError(f"Cloud Run has no URL for {service} in {region}")
    return url.rstrip("/")



#: Google's edges answer before the application does, and they answer in HTML.
#: Printing that page verbatim buries the one useful line under a document, and
#: the code alone is worse: a 404 here almost never means "no such route".
_HTML = ("<html", "<!doctype html", "<HTML")


def _explain(status: int, body: str) -> str:
    """Turn an edge's HTML refusal into the sentence it was trying to be."""
    stripped = body.strip()
    if not stripped.lower().startswith(_HTML):
        try:
            parsed = json.loads(stripped)
            return str(parsed.get("detail") or parsed.get("message") or stripped[:300])
        except json.JSONDecodeError:
            return stripped[:300] or f"HTTP {status}"

    if status == 404:
        return (
            "an HTML 404 from Google's edge, not from the API. This is what "
            "Cloud Run returns when a service's INGRESS refuses the caller -- "
            "`internal-and-cloud-load-balancing` rejects a direct or proxied "
            "call from outside the VPC. Reach it through its load balancer, or "
            "deploy the solo profile, which does not restrict ingress"
        )
    if status in (401, 403):
        return (
            "an HTML sign-in page, so IAP rejected this before the API saw it. "
            "A bearer token minted for anything other than the IAP OAuth client "
            "id reads as `Invalid JWT audience`; set SWARM_IAP_CLIENT_ID and "
            "SWARM_IMPERSONATE_SA, and check your account is on the IAP list"
        )
    return f"an HTML error page from Google's edge (HTTP {status}), not from the API"


@dataclass
class _Token:
    value: str
    minted_at: float


def service_name() -> str:
    return os.environ.get("API_SERVICE", "").strip() or "swarm-api"


def region() -> str:
    return os.environ.get("REGION", "").strip() or "us-central1"


def project_id() -> str:
    value = os.environ.get("PROJECT_ID", "").strip()
    if not value:
        value = _run(["gcloud", "config", "get-value", "project"])
    if not value or value == "(unset)":
        raise SwarmError("no project: set PROJECT_ID, or run `gcloud config set project`")
    return value


class SwarmClient:
    """A connection to one SwarmCloud, over whichever tier this machine has.

    THE TIER IS DETECTED, NOT CONFIGURED. The commonest failure for someone
    trying this for the first time is not choosing the wrong tier -- it is not
    knowing tiers exist, and reading "Invalid JWT audience" as a bug in the
    platform rather than as "your laptop cannot mint that kind of token".

    On the PROXY tier this object owns a subprocess, so it is a context manager.
    Using it without `with` still works for a single call; the proxy is then
    reaped when the process exits.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        audience: str | None = None,
        connect: bool = True,
    ) -> None:
        from . import auth as _auth  # local: avoids a circular import at module load

        self.detection = _auth.detect()
        self.tier = self.detection.tier
        self._proxy: _auth.Proxy | None = None
        self._token: _Token | None = None
        self._explicit_url = base_url or os.environ.get("SWARM_API_URL", "").strip() or None

        if self.tier is _auth.Tier.IAP:
            # The audience IAP accepts is its OAuth client id, never the
            # service URL. Getting this wrong is the "Invalid JWT audience"
            # that sends people looking at their IAM policy.
            self._audience = os.environ.get("SWARM_IAP_CLIENT_ID", "").strip()
        else:
            self._audience = audience or os.environ.get("API_AUDIENCE", "").strip() or None

        self.base_url = ""
        if connect:
            self.connect()

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        from . import auth as _auth

        if self.tier is _auth.Tier.PROXY and not self._explicit_url:
            self._proxy = _auth.Proxy(service_name(), region(), project_id())
            self.base_url = self._proxy.start().rstrip("/")
        else:
            self.base_url = (self._explicit_url or resolve_api_url()).rstrip("/")
        if not self._audience:
            self._audience = self.base_url

    def close(self) -> None:
        if self._proxy is not None:
            self._proxy.stop()
            self._proxy = None

    def __enter__(self) -> "SwarmClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- identity ----------------------------------------------------------
    @property
    def sends_own_token(self) -> bool:
        """False on the proxy tier, where gcloud supplies the Authorization
        header itself. Sending a second one would replace the only credential
        Cloud Run will accept with one it will not."""
        from . import auth as _auth

        return self.tier is not _auth.Tier.PROXY

    def _id_token(self) -> str:
        from . import auth as _auth

        now = time.monotonic()
        if self._token is not None and now - self._token.minted_at < _TOKEN_TTL_SECONDS:
            return self._token.value
        value = _auth.id_token_for(self._audience or self.base_url, tier=self.tier)
        if not value:
            raise SwarmError("no ID token available; run: gcloud auth login")
        self._token = _Token(value=value, minted_at=now)
        return value

    def access_token(self) -> str:
        """An OAuth access token, for GCS. Distinct from the ID token above:
        one proves WHO you are to IAP, the other authorises a bucket read."""
        override = os.environ.get("SWARM_ACCESS_TOKEN", "").strip()
        if override:
            return override
        return _run(["gcloud", "auth", "print-access-token"])

    # -- transport ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int = 60,
    ) -> Any:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if self.sends_own_token:
            req.add_header("Authorization", f"Bearer {self._id_token()}")
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise SwarmError(
                f"{method} {path} -> {exc.code}: {_explain(exc.code, raw)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SwarmError(f"could not reach {self.base_url}: {exc.reason}") from exc

    # -- operations --------------------------------------------------------
    def dispatch(
        self,
        *,
        prompt: str,
        runner_profile: str = "claude-code",
        repository_url: str | None = None,
        repository_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Submit one task.

        `prompt` goes in `input`, never in a command line. Invariant 10: the
        caller picks a runner profile BY NAME and supplies data; it cannot
        supply an image, a command or a resource spec, and this client has no
        parameter that would let it try.
        """
        payload: dict[str, Any] = {
            "runner_profile": runner_profile,
            "input": {"prompt": prompt},
            "metadata": {"origin": "swarm-mcp", **(metadata or {})},
        }
        if repository_url:
            payload["repository_url"] = repository_url
        if repository_ref:
            payload["repository_ref"] = repository_ref
        if timeout_seconds:
            payload["timeout_seconds"] = timeout_seconds
        if model:
            payload["model"] = model
        return self.request("POST", "/v1/tasks", payload=payload)

    def dispatch_batch(self, tasks: list[dict[str, Any]]) -> Any:
        return self.request("POST", "/v1/tasks/batch", payload={"tasks": tasks})

    def task(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/tasks/{task_id}")

    def events(self, task_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v1/tasks/{task_id}/events?limit={limit}")
        if isinstance(data, dict):
            return list(data.get("events") or [])
        return list(data or [])

    def cancel(self, task_id: str) -> Any:
        return self.request("POST", f"/v1/tasks/{task_id}/cancel", payload={})
