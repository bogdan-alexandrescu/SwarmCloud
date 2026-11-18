"""The deployment itself, as far as the engine reaches it: swarm-api behind
IAP at https://<hostname> (the two routes the engine is admitted on), and
the console's Admin › Setup, which writes the install-record documents
swarm-api writes, through swarm-api's own path helpers."""

from __future__ import annotations

import json
from typing import Any

from swarm_install.api_client import GROUPS_PROBE_ROUTE, HOOK_ROUTE, IAM_API, deployer_email

from .google import Cloud
from .world import Call, World, error


def register(world: World, cloud: Cloud) -> None:
    route = world.route
    host = world.hostname.replace(".", r"\.")

    @route("POST", rf"{IAM_API}/projects/-/serviceAccounts/([^/:]+):generateIdToken")
    def id_token(call: Call) -> Any:
        if not world.may_impersonate(world.principal, call.group(1)):
            return error(403, "PERMISSION_DENIED", f"{world.principal} lacks iam.serviceAccounts.getOpenIdToken")
        audience = (call.body or {}).get("audience")
        return {"token": "e2e-id-token." + json.dumps({"sub": call.group(1), "aud": audience})}

    def through_iap(call: Call) -> dict[str, Any] | None:
        """IAP: an ID token minted for the API backend's audience, for a member IAP admits."""
        bearer = str(call.headers.get("Authorization", "")).removeprefix("Bearer ")
        if not bearer.startswith("e2e-id-token."):
            return None
        claims = json.loads(bearer.removeprefix("e2e-id-token."))
        if claims.get("aud") not in world.tf_outputs["infra"].get("frontend_iap_audiences", []):  # type: ignore[attr-defined]
            return None
        return claims

    def groups_auth() -> str:
        """GROUPS_AUTH as the last infra apply rendered it (the variable's
        default, `delegation`, when no pass set it)."""
        rendered = [inputs for root, inputs, _targets in world.tf_applies if root == "infra"]  # type: ignore[attr-defined]
        return str((rendered[-1] if rendered else {}).get("groups_auth") or "delegation")

    @route("POST", rf"https://{host}{GROUPS_PROBE_ROUTE}")
    def groups_probe(call: Call) -> Any:
        claims = through_iap(call)
        if claims is None:
            return error(401, "UNAUTHENTICATED", "IAP refused the token")
        if claims["sub"] != deployer_email(world.project):
            return error(403, "PERMISSION_DENIED", "swarm-api admits only the deployer here")
        api = f"swarm-api@{world.project}.iam.gserviceaccount.com"
        unique = cloud.accounts.get(api, {}).get("uniqueId")
        reader = next(r["roleId"] for r in cloud.admin_roles if r["roleName"] == "_GROUPS_READER_ROLE")
        held = any(a["roleId"] == reader and a["assignedTo"] == unique for a in cloud.role_assignments)
        results = []
        for probe in (call.body or {}).get("groups", []):
            group, member = probe["group"], probe["member"]
            if not held:
                results.append({"group": group, "state": "unmet", "reason": "forbidden",
                                "detail": "swarm-api holds no Groups Reader role"})
            elif group not in cloud.groups:
                results.append({"group": group, "state": "unmet", "reason": "not_found", "detail": ""})
            else:
                results.append({"group": group, "state": "met" if member in cloud.members[group] else "unmet",
                                "reason": "", "detail": ""})
        return {"mode": groups_auth(), "results": results}

    @route("POST", rf"https://{host}{HOOK_ROUTE}")
    def hook(call: Call) -> Any:
        if through_iap(call) is None:
            return error(401, "UNAUTHENTICATED", "IAP refused the token")
        world.hook_active = True  # type: ignore[attr-defined]
        return {"active": True}
