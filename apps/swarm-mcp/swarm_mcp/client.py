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


@dataclass
class _Token:
    value: str
    minted_at: float


class SwarmClient:
    def __init__(self, base_url: str | None = None, *, audience: str | None = None) -> None:
        self.base_url = (base_url or resolve_api_url()).rstrip("/")
        self._audience = audience or os.environ.get("API_AUDIENCE", "").strip() or None
        self._token: _Token | None = None

    # -- identity ----------------------------------------------------------
    def _id_token(self) -> str:
        override = os.environ.get("SWARM_ID_TOKEN", "").strip()
        if override:
            return override
        now = time.monotonic()
        if self._token is not None and now - self._token.minted_at < _TOKEN_TTL_SECONDS:
            return self._token.value
        argv = ["gcloud", "auth", "print-identity-token"]
        impersonate = os.environ.get("SWARM_IMPERSONATE_SA", "").strip()
        if impersonate:
            argv += [
                f"--impersonate-service-account={impersonate}",
                f"--audiences={self._audience or self.base_url}",
                "--include-email",
            ]
        value = _run(argv)
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
            detail = raw[:400]
            try:
                parsed = json.loads(raw)
                detail = str(parsed.get("detail") or parsed.get("message") or detail)
            except json.JSONDecodeError:
                pass
            if exc.code in (401, 403):
                # The single most common failure on a laptop, and the least
                # self-explanatory: IAP answers before the API does, so the
                # body is an HTML sign-in page rather than anything about
                # permissions.
                detail = (
                    f"{detail} -- if this is an HTML page, IAP rejected the "
                    "request before the API saw it; check `gcloud auth login` "
                    "and that your account is on the IAP access list"
                )
            raise SwarmError(f"{method} {path} -> {exc.code}: {detail}") from exc
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
