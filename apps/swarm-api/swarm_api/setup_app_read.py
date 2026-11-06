"""The GitHub App and its installation as `swarm upgrade` needs them, read as
the App (lane DAYONE-UPGRADE-APP-VIA-API, part of #2688).

`swarm upgrade` compares release V's `github-app-permissions.json` with what
the App's definition and its installation hold, and opens the page a person
accepts the new permissions on. A private App answers `GET /app` only to the
App itself, so the read is signed with the App's private key; swarm-api is
that key's one accessor (`setup_github.ManifestFlow._signed`), so the read is
here and the installer never reads a secret's value
(tests/unit/install/test_switches.py).

The answer carries the App's slug, its owner and its `permissions`, and the
installation's id, account and `permissions`: never a token, a key, a JWT or
any secret field. With no App yet, or before infra pass 3 lets the key sign,
it answers a state and no App, never an error.
"""

from __future__ import annotations

from typing import Any

from .errors import ApiError
from .setup_github import GitHubSetupApi, ManifestFlow

#: The App is not created yet: Admin › Setup creates it from the release's manifest.
NO_APP = "no_app"
#: The App is recorded, yet its private key cannot sign (before infra pass 3).
KEY_UNAVAILABLE = "key_unavailable"
#: The App and, when it is installed, its installation were read from GitHub.
READ = "read"

#: GitHub's `type` of an organisation account.
ORGANIZATION = "Organization"


def _permissions(found: Any) -> dict[str, str]:
    return {str(k): str(v) for k, v in dict(found or {}).items()}


def _account(found: Any) -> dict[str, Any]:
    return dict(found) if isinstance(found, dict) else {}


def _installation_id(installs: list[dict[str, Any]], wanted: str) -> int | None:
    """The installation on `wanted` (case-blind), else the App's only one."""
    matching = [i for i in installs
                if wanted and str(_account(i.get("account")).get("login") or "").lower() == wanted.lower()]
    chosen = matching or (installs if len(installs) == 1 else [])
    found = chosen[0].get("id") if chosen else None
    return found if isinstance(found, int) and not isinstance(found, bool) else None


def app_permissions(flow: ManifestFlow, github: GitHubSetupApi, *, org: str) -> dict[str, Any]:
    """`{"state", "app", "installation"}`: the App's definition (`GET /app`) and
    the installation on the account Admin › Setup installed it on
    (`installed_on`, else `org`, the deployment's SETUP_GITHUB_ORG, else the
    App's only installation), each with its `permissions`; `installation` is
    None while the App is not installed there. A GitHub error is GitHub's
    refusal (`GitHubRefused`), said with its status only."""
    doc = flow._doc()  # noqa: SLF001 - the flow's own record
    if not doc.get("id"):
        return {"state": NO_APP, "app": None, "installation": None}
    try:
        jwt = flow._signed()  # noqa: SLF001 - the one App JWT signer
    except ApiError:
        return {"state": KEY_UNAVAILABLE, "app": None, "installation": None}
    found = github._call("GET", "/app", jwt=jwt)  # noqa: SLF001 - setup_github's signed call
    found = found if isinstance(found, dict) else {}
    owner = _account(found.get("owner"))
    app = {
        "slug": str(found.get("slug") or doc.get("slug") or ""),
        "owner": str(owner.get("login") or ""),
        "owner_is_org": owner.get("type") == ORGANIZATION,
        "permissions": _permissions(found.get("permissions")),
    }
    wanted = str(doc.get("installed_on") or org or "")
    chosen = _installation_id(github.installations(jwt), wanted)
    installation = None
    if chosen is not None:
        read = github._call("GET", f"/app/installations/{chosen}", jwt=jwt)  # noqa: SLF001
        read = read if isinstance(read, dict) else {}
        account = _account(read.get("account"))
        installation = {
            "id": chosen,
            "account": str(account.get("login") or ""),
            "account_is_org": account.get("type") == ORGANIZATION,
            "permissions": _permissions(read.get("permissions")),
        }
    return {"state": READ, "app": app, "installation": installation}


__all__ = ["KEY_UNAVAILABLE", "NO_APP", "READ", "app_permissions"]
