"""The discovery app: `GET /.well-known/swarmcloud`, and nothing else.

docs/design/day-one-install.md §9.2 and docs/design/day-one-plan.md §6 (lane
DAYONE-DISCOVERY-1, part of #2688). The plugin is configured with one value,
the deployment URL; its bridge reads this document for the rest.

Entrypoint (the `swarm-discovery` Cloud Run service, DAYONE-TF-2: the swarm-api
image with this as its command):

    uvicorn swarm_api.discovery:create_app --factory --host 0.0.0.0 --port $PORT

WHO CAN READ IT: ANYONE. The service sits behind the front door's third
backend, the one without IAP, with the invoker IAM check disabled. So:

* **Exactly the public set** (plan §6.2): `schema`, `name`, `version`,
  `console_url`, `api_url`, `oauth_client_id`, `iap_audience`,
  `plugin.min_version`. Never another secret, a tenant, a group, an email or
  the install state. tests/unit/control_plane/test_discovery.py fails on any
  extra key.
* **`oauth_client_secret` only when `DISCOVERY_SERVE_CLIENT_SECRET=true`**:
  BGD's Q1 decision (design §14 item 1, plan §6.3), switched on by the engine
  only if Google refuses a PKCE-only exchange for the Desktop client. The value
  is then read from Secret Manager (`swarm-iap-desktop-client-secret`, the only
  secret `swarm-discovery` may access), NEVER from an environment variable: a
  Job or service environment is printed by `gcloud run services describe`.
  Google classifies a Desktop client's secret as non-confidential; the Internal
  brand, IAP membership and tenant resolution stay the boundary (design §9.3).
* **Every other path is 404**, including FastAPI's `/docs` and
  `/openapi.json`, which are switched off.

BUILT ONCE, AT START, from the environment Terraform renders. No Firestore, no
store, no settings module: nothing here touches the API's state. A missing or
malformed variable, or a switched-on secret that cannot be read, raises
`DiscoveryConfigError` from `create_app`, so the revision fails to start and
the deploy says so, rather than serving every plugin a document with a hole.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

PATH = "/.well-known/swarmcloud"

#: The document's version. A plugin reading a schema it does not know names it.
SCHEMA = 1

#: The Desktop client's secret (manifest `secret.swarm-iap-desktop-client-secret`).
#: Fixed, not configurable: TF-2 grants `secretAccessor` on this one secret only.
CLIENT_SECRET_ID = "swarm-iap-desktop-client-secret"

#: A hostname: no scheme, no path, no port. `console_url` and `api_url` are
#: built from it, so the two can never disagree about the deployment.
_HOST = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


class DiscoveryConfigError(RuntimeError):
    """The environment cannot make a correct document; the app does not start."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise DiscoveryConfigError(f"{name} is not set; swarm-discovery cannot start without it")
    return value


def _switch(environ: Mapping[str, str], name: str) -> bool:
    """Terraform renders a bool as `true`/`false`. Anything else is refused:
    a typo must not silently decide whether a secret is served."""
    value = (environ.get(name) or "").strip().lower()
    if value in ("", "false"):
        return False
    if value == "true":
        return True
    raise DiscoveryConfigError(f"{name} must be true or false, not {value!r}")


def read_secret_manager(name: str) -> str:
    """The latest enabled version of one secret, by full resource name.
    Imported lazily: with the switch off the library is never loaded."""
    from google.api_core import exceptions as gexc
    from google.cloud import secretmanager

    try:
        version = secretmanager.SecretManagerServiceClient().access_secret_version(
            request={"name": name}
        )
    except (gexc.NotFound, gexc.FailedPrecondition):
        raise DiscoveryConfigError(f"{name} has no enabled version") from None
    except gexc.PermissionDenied:
        raise DiscoveryConfigError(f"swarm-discovery may not read {name}") from None
    except Exception as exc:
        raise DiscoveryConfigError(f"{name} could not be read ({type(exc).__name__})") from None
    try:
        return bytes(version.payload.data).decode("utf-8")
    except UnicodeDecodeError:
        raise DiscoveryConfigError(f"{name} is not UTF-8") from None


def build_document(
    environ: Mapping[str, str], read_secret: Callable[[str], str] = read_secret_manager
) -> dict[str, Any]:
    """The discovery document, from the environment and (when switched) the secret."""
    host = _required(environ, "DISCOVERY_HOST")
    if not _HOST.match(host):
        raise DiscoveryConfigError(
            f"DISCOVERY_HOST must be a bare hostname (no scheme, path or port), not {host!r}"
        )
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "name": _required(environ, "DISCOVERY_NAME"),
        "version": _required(environ, "DISCOVERY_VERSION"),
        "console_url": f"https://{host}/",
        "api_url": f"https://{host}/v1",
        "oauth_client_id": _required(environ, "DISCOVERY_OAUTH_CLIENT_ID"),
        "iap_audience": _required(environ, "DISCOVERY_IAP_AUDIENCE"),
        "plugin": {"min_version": _required(environ, "DISCOVERY_PLUGIN_MIN_VERSION")},
    }
    if _switch(environ, "DISCOVERY_SERVE_CLIENT_SECRET"):
        project = _required(environ, "PROJECT_ID")
        value = read_secret(f"projects/{project}/secrets/{CLIENT_SECRET_ID}/versions/latest").strip()
        if not value:
            raise DiscoveryConfigError(f"{CLIENT_SECRET_ID} is empty")
        document["oauth_client_secret"] = value
    return document


def create_app(
    environ: Mapping[str, str] | None = None,
    read_secret: Callable[[str], str] = read_secret_manager,
):
    """The app, its document built here once. `environ` and `read_secret` are
    for tests; production passes neither (uvicorn `--factory`)."""
    import os

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    document = build_document(os.environ if environ is None else environ, read_secret)

    app = FastAPI(
        title="swarm-discovery", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get(PATH, include_in_schema=False)
    def well_known() -> JSONResponse:
        # The plugin's bridge caches it for an hour (plan §6.5); a short shared
        # cache here keeps a release's new version visible within minutes.
        return JSONResponse(document, headers={"Cache-Control": "public, max-age=300"})

    return app


def main() -> None:  # pragma: no cover - process entrypoint
    import os

    import uvicorn

    uvicorn.run(
        "swarm_api.discovery:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
