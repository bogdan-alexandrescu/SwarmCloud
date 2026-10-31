"""The GitHub App manifest flow, run by swarm-api (docs/design/day-one-plan.md
§0 R3, §5.3; design §9.1; lane DAYONE-SETUP-API-1).

Admin › Setup posts GitHub's App-manifest form; GitHub redirects the browser
to `https://<H>/admin/setup/github-app/callback` with `code` and `state`; the
console hands both to swarm-api, which checks `state` BEFORE it calls GitHub,
exchanges the code (`POST /app-manifests/<code>/conversions`), writes `pem`,
`client_secret` and `webhook_secret` straight into their Secret Manager
containers and drops them. Only `id`, `slug`, `client_id` and `name` -- none
of them secret -- are kept, in the console's `github_app` document. Nothing of
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
#: Admin › Setup: the manifest's `setup_url`, where the App's installation
#: returns the person.
SETUP_PATH = "/admin/setup"
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
    reads as a gate that could not decide (test_pool_admin_is_narrow.py).

    It keeps the established code `conflict` (above). What tells a caller
    "not yet" apart from every other 409 is `detail.unset`, the settings this
    revision has not been given: the install engine waits on that while
    swarm-api's next revision, the one that carries them, rolls out."""

    def __init__(self, message: str, *, unset: tuple[str, ...] = ()) -> None:
        super().__init__(message, detail={"unset": list(unset)} if unset else None)


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
        # Where GitHub sends the person after they install the App, and after
        # they change the installation's repositories: Admin › Setup, whose
        # GitHub step checks the installation on arrival (audit 5b M4).
        # Without it GitHub stays on its own installation page.
        "setup_url": base + SETUP_PATH,
        "setup_on_update": True,
        "public": False,
        "default_permissions": dict(PERMISSIONS),
        "default_events": list(EVENTS),
        "hook_attributes": {"url": hooks_url, "active": False},
    }
    return manifest


#: `GET /users/<login>`'s `type` for a personal account. Anything else
#: GitHub answers there (`Organization`) takes the organisations page.
USER_ACCOUNT = "User"


def form_target(org: str, state: str, *, account_type: str = "Organization") -> str:
    """Where the manifest form posts: a user account's own "new App" page, or
    the organisation's. The organisations path answers 404 for a user account
    (audit finding 29), so the account's type picks it."""
    if account_type == USER_ACCOUNT:
        page = "https://github.com/settings/apps/new"
    else:
        page = f"https://github.com/organizations/{quote(org, safe='')}/settings/apps/new"
    return f"{page}?state={quote(state, safe='')}"


class SetupSecrets(Protocol):
    """The Secret Manager calls the flow makes, so tests run on a fake."""

    def add_version(self, secret_id: str, value: str, *,
                    create_labels: dict[str, str] | None = None) -> None: ...

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

    def add_version(self, secret_id: str, value: str, *,
                    create_labels: dict[str, str] | None = None) -> None:
        """Write `value` as a new version of `secret_id`.

        With `create_labels`, the container is created first when it is missing
        (an existing one is kept as it is): an install's team git slot
        `swarm-tenant-<tenant>-git` has no Terraform tenant and no
        create-secrets.sh run to make it (design §5.7). No accessor is granted
        here: a forge token is read by swarm-api's minter, never by a worker
        GSA (CLAUDE.md, forge-token rule)."""
        if create_labels is not None:
            self._create(secret_id, create_labels)
        try:
            self._secret_client().add_secret_version(request={
                "parent": self._name(secret_id),
                "payload": {"data": value.encode("utf-8")},
            })
        except Exception as exc:
            # The type only: an exception's text could quote the request.
            raise UpstreamUnavailable(
                f"{secret_id} could not be written ({type(exc).__name__})") from None

    def _create(self, secret_id: str, labels: dict[str, str]) -> None:
        from google.api_core import exceptions as gexc

        try:
            self._secret_client().create_secret(request={
                "parent": f"projects/{self._project_id}",
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}, "labels": dict(labels)},
            })
        except gexc.AlreadyExists:
            pass
        except Exception as exc:
            raise UpstreamUnavailable(
                f"{secret_id} could not be created ({type(exc).__name__})") from None

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

    def account_type(self, login: str) -> str:
        """`GET /users/<login>`'s `type` (`User` or `Organization`): no
        authentication, the account is public. An answer with no type is
        refused rather than guessed, since the wrong page is GitHub's 404."""
        found = self._call("GET", f"/users/{quote(login, safe='')}")
        kind = found.get("type") if isinstance(found, dict) else None
        if not isinstance(kind, str) or not kind:
            raise GitHubRefused("GitHub's account answer named no type")
        return kind

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
        # The webhook URL is asked for here too: a manifest posted with an
        # empty hook URL is one GitHub may refuse, or convert with no webhook
        # secret, after the App exists and the one-time code is spent.
        unset = tuple(name for name, value in (("SETUP_GITHUB_ORG", org), ("SWARM_HOSTNAME", hostname),
                                               ("GITHUB_HOOKS_URL", hooks_url)) if not value)
        if unset:
            raise SetupUnconfigured(
                "SETUP_GITHUB_ORG, the deployment's hostname and GITHUB_HOOKS_URL must all be set; "
                f"not set: {', '.join(unset)}", unset=unset)
        # Asked before the state is written: nothing starts on a page guessed.
        account_type = self._github.account_type(org)
        state = new_state()
        self._write({"state_hash": state_hash(state)})
        return {
            "manifest": manifest_for(org=org, hostname=hostname, hooks_url=hooks_url, name=name),
            "action": form_target(org, state, account_type=account_type),
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
        answer = {k: answer.get(k) for k in ("id", "slug", "client_id", "name")}
        self._write({
            "id": answer["id"],
            "slug": answer["slug"],
            "client_id": answer["client_id"],
            # Infra pass 3 renders github_app_name from it (E2E-1).
            "name": answer["name"],
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
            raise SetupUnconfigured("GITHUB_HOOKS_URL is not set", unset=("GITHUB_HOOKS_URL",))
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
    "USER_ACCOUNT",
    "WEBHOOK_SECRET_SLOT",
    "WebhookSecretMissing",
    "form_target",
    "manifest_for",
    "state_hash",
]
