"""The GitHub App manifest flow, run by swarm-api (docs/design/day-one-plan.md
§0 R3, §5.3; design §9.1; lane DAYONE-SETUP-API-1).

Admin › Setup posts GitHub's App-manifest form; GitHub redirects the browser
to `https://<H>/admin/setup/github-app/callback` with `code` and `state`; the
console hands both to swarm-api, which checks `state` BEFORE it calls GitHub,
exchanges the code (`POST /app-manifests/<code>/conversions`), writes `pem`,
`client_secret` and `webhook_secret` straight into their Secret Manager
containers and drops them. Only `id`, `slug` and `client_id` -- none of them
secret -- are kept, in the console's `github_app` document. Nothing of
GitHub's answer is logged.

The webhook is registered inactive (`hook_attributes.active = false`); it is
turned on by `POST /v1/admin/setup/github-app/hook`, which the engine calls
after the self-check, and only once the webhook secret has an enabled version
(key before switch). swarm-api signs that call's App JWT itself, so the
private key never leaves it (review finding 10).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

from .errors import ApiError, Conflict, Forbidden, UpstreamUnavailable
from .forgeapp import CLIENT_SECRET_SLOT, PRIVATE_KEY_SLOT, HttpSend, app_jwt, urllib_send

WEBHOOK_SECRET_SLOT = "swarm-github-app-webhook-secret"

#: Which field of GitHub's conversion answer goes to which container. Each
#: value is written there and nowhere else, and is never returned.
SECRET_FIELDS: dict[str, str] = {
    "pem": PRIVATE_KEY_SLOT,
    "client_secret": CLIENT_SECRET_SLOT,
    "webhook_secret": WEBHOOK_SECRET_SLOT,
}

#: The tenant App's repository permissions: docs/runbooks/github-app.md
#: ("Repository permissions", owner decision D8), plus `statuses: read`, the
#: Commit statuses permission the live App lacked (lesson 46). GitHub's keys:
#: https://docs.github.com/en/rest/overview/permissions-required-for-github-apps
PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
    "metadata": "read",
    "actions": "write",
    "workflows": "write",
    "secrets": "write",
    "environments": "write",
    "statuses": "read",
}
#: The runbook's Step 9 subscribes `push` and nothing else.
EVENTS: tuple[str, ...] = ("push",)

CALLBACK_PATH = "/admin/setup/github-app/callback"
#: The console's GitHub sign-in callback (`GITHUB_CALLBACK_PATH`, apps/swarm-ui/src/paths.ts).
SIGN_IN_CALLBACK_PATH = "/onboarding/github/callback"
API = "https://api.github.com"
TIMEOUT_SECONDS = 15.0


# These refusals keep the established codes `conflict` and `forbidden`: a
# new 4xx code ships report-only behind a refusal switch (swarm_api.refusals,
# owner decision 2026-10-08), and continuing past a state mismatch or a
# missing webhook secret is the breach itself. The served sentence says which.


class SetupUnconfigured(Conflict):
    """A setting the install has not rendered yet (SETUP_GITHUB_ORG, the
    hostname, GITHUB_HOOKS_URL). 409, not 503: the deployment is reachable
    and answering; it is not yet at the step that sets the value, and a 503
    reads as a gate that could not decide (test_pool_admin_is_narrow.py)."""


class StateMismatch(Forbidden):
    pass


class WebhookSecretMissing(Conflict):
    pass


class GitHubRefused(UpstreamUnavailable):
    code = "github_refused"


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def state_matches(state: str, stored_hash: str | None) -> bool:
    if not state or not stored_hash:
        return False
    return hmac.compare_digest(state_hash(state), stored_hash)


def new_state() -> str:
    return secrets.token_urlsafe(32)


def manifest_for(*, org: str, hostname: str, hooks_url: str, name: str) -> dict[str, Any]:
    """The App manifest GitHub registers (design §9.1, R3)."""
    base = f"https://{hostname}"
    manifest: dict[str, Any] = {
        "name": name,
        "url": base,
        "redirect_url": base + CALLBACK_PATH,
        "callback_urls": [base + SIGN_IN_CALLBACK_PATH],
        "public": False,
        "default_permissions": dict(PERMISSIONS),
        "default_events": list(EVENTS),
        "hook_attributes": {"url": hooks_url, "active": False},
    }
    return manifest


def form_target(org: str, state: str) -> str:
    return (f"https://github.com/organizations/{quote(org, safe='')}/settings/apps/new"
            f"?state={quote(state, safe='')}")


class SetupSecrets(Protocol):
    """The Secret Manager calls the flow makes, so tests run on a fake."""

    def add_version(self, secret_id: str, value: str) -> None: ...

    def has_enabled_version(self, secret_id: str) -> bool: ...

    def private_key(self) -> str: ...


class SecretManagerSetupSecrets:
    """`SetupSecrets` on Secret Manager. The client is built on first use."""

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _name(self, secret_id: str) -> str:
        return f"projects/{self._project_id}/secrets/{secret_id}"

    def add_version(self, secret_id: str, value: str) -> None:
        try:
            self._secret_client().add_secret_version(request={
                "parent": self._name(secret_id),
                "payload": {"data": value.encode("utf-8")},
            })
        except Exception as exc:
            # The type only: an exception's text could quote the request.
            raise UpstreamUnavailable(
                f"{secret_id} could not be written ({type(exc).__name__})") from None

    def has_enabled_version(self, secret_id: str) -> bool:
        from google.cloud import secretmanager

        try:
            versions = self._secret_client().list_secret_versions(
                request={"parent": self._name(secret_id), "filter": "state:ENABLED"})
            return any(v.state == secretmanager.SecretVersion.State.ENABLED for v in versions)
        except Exception as exc:
            raise UpstreamUnavailable(
                f"{secret_id}'s versions could not be listed ({type(exc).__name__})") from None

    def private_key(self) -> str:
        from .forgeapp import SecretManagerAppSecrets

        return SecretManagerAppSecrets(self._project_id, client=self._client).private_key()


class GitHubSetupApi:
    """The three GitHub calls of the flow, over `forgeapp.urllib_send`
    (https to github.com hosts only, no redirects)."""

    def __init__(self, send: HttpSend = urllib_send) -> None:
        self._send = send

    def _call(self, method: str, path: str, *, jwt: str | None = None,
              body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        try:
            answer = self._send(method, API + path, headers, data, TIMEOUT_SECONDS)
        except Exception as exc:
            raise GitHubRefused(f"GitHub did not answer ({type(exc).__name__})") from None
        if not 200 <= answer.status < 300:
            # The status only: GitHub's body is not repeated anywhere.
            raise GitHubRefused(f"GitHub answered {answer.status}")
        return answer.json()

    def convert(self, code: str) -> dict[str, Any]:
        """`POST /app-manifests/<code>/conversions`: no authentication."""
        found = self._call("POST", f"/app-manifests/{quote(code, safe='')}/conversions")
        if not isinstance(found, dict):
            raise GitHubRefused("GitHub's conversion answer was not an object")
        return found

    def installations(self, jwt: str) -> list[dict[str, Any]]:
        found = self._call("GET", "/app/installations", jwt=jwt)
        return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []

    def update_hook(self, jwt: str, config: dict[str, Any]) -> None:
        """`PATCH /app/hook/config`."""
        self._call("PATCH", "/app/hook/config", jwt=jwt, body=config)


class ManifestFlow:
    """The console's GitHub step. `record` is `install_record.InstallRecord`."""

    def __init__(self, *, record: Any, secrets_: SetupSecrets, github: GitHubSetupApi,
                 now: Callable[[], datetime]) -> None:
        self._record = record
        self._secrets = secrets_
        self._github = github
        self._now = now

    def _doc(self) -> dict[str, Any]:
        return self._record.read(self._record.console_path("github_app")) or {}

    def _write(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self._record.write(self._record.console_path("github_app"), fields)

    def start(self, *, org: str, hostname: str, hooks_url: str, name: str) -> dict[str, Any]:
        if not org or not hostname:
            raise SetupUnconfigured(
                "SETUP_GITHUB_ORG and the deployment's hostname must both be set")
        state = new_state()
        self._write({"state_hash": state_hash(state)})
        return {
            "manifest": manifest_for(org=org, hostname=hostname, hooks_url=hooks_url, name=name),
            "action": form_target(org, state),
        }

    def exchange(self, *, code: str, state: str) -> dict[str, Any]:
        """Check `state`, then exchange; return `{id, slug}` only."""
        if not state_matches(state, self._doc().get("state_hash")):
            raise StateMismatch("this callback's state is not the one Admin › Setup issued")
        answer = self._github.convert(code)
        values = {field: answer.get(field) for field in SECRET_FIELDS}
        if any(not isinstance(v, str) or not v for v in values.values()):
            raise GitHubRefused("GitHub's conversion answer lacked a secret")
        for field, slot in SECRET_FIELDS.items():
            self._secrets.add_version(slot, values[field])
        values.clear()
        answer = {k: answer.get(k) for k in ("id", "slug", "client_id")}
        self._write({
            "id": answer["id"],
            "slug": answer["slug"],
            "client_id": answer["client_id"],
            "secrets_written_at": self._now(),
            "state_hash": None,
        })
        return {"id": answer["id"], "slug": answer["slug"]}

    def _signed(self) -> str:
        app_id = self._doc().get("id")
        if not app_id:
            raise Conflict("the GitHub App has not been created yet")
        try:
            return app_jwt(str(app_id), self._secrets.private_key(), self._now())
        except ApiError:
            raise
        except Exception:
            raise SetupUnconfigured("the App's private key could not sign") from None

    def installation(self, org: str) -> dict[str, Any]:
        """Whether the App is installed on `org`; records `installed_on`."""
        doc = self._doc()
        installs = self._github.installations(self._signed())
        logins = sorted({str((i.get("account") or {}).get("login") or "") for i in installs} - {""})
        installed = any(login.lower() == org.lower() for login in logins)
        if installed and doc.get("installed_on") != org:
            self._write({"installed_on": org})
        return {"installed": installed, "org": org, "slug": doc.get("slug")}

    def activate_hook(self, hooks_url: str) -> dict[str, Any]:
        """Turn the webhook on, once its secret has a version."""
        if not hooks_url:
            raise SetupUnconfigured("GITHUB_HOOKS_URL is not set")
        if not self._secrets.has_enabled_version(WEBHOOK_SECRET_SLOT):
            raise WebhookSecretMissing(
                f"{WEBHOOK_SECRET_SLOT} has no enabled version; the webhook stays off")
        self._github.update_hook(self._signed(), {"url": hooks_url, "content_type": "json",
                                                  "insecure_ssl": "0", "active": True})
        return {"active": True}


__all__ = [
    "EVENTS",
    "GitHubSetupApi",
    "ManifestFlow",
    "PERMISSIONS",
    "SECRET_FIELDS",
    "SecretManagerSetupSecrets",
    "SetupSecrets",
    "SetupUnconfigured",
    "StateMismatch",
    "WEBHOOK_SECRET_SLOT",
    "WebhookSecretMissing",
    "manifest_for",
    "state_hash",
]
