"""A person's role in a workspace: the one seam (docs/design/user-profile.md §4.2, Q7).

`role_in(ctx, tenant_id)` is the ONLY place a person's role in a workspace is
computed. `GET /v1/profile` serves its answer as `workspaces[].role` verbatim,
and the page renders `role.name` as given and `via` when present, branching on
neither.

TODAY'S ROLES ARE DERIVED, not assigned. There is no per-workspace role yet
(`routes/provider_keys.py` and `routes/gittokens.py` both say so): a person
OWNS their confirmed personal workspace and is a MEMBER of a group workspace
through the group's address. Per-workspace roles are a separate design (#1518,
lane ROLES-DESIGN). That build replaces this function's body, serves
`source: "assigned"` and may serve any role name; `profile.py` and the page
need no change.

NEVER `admin`. A platform admin is not a workspace role: it is
`identity.platform_role`, shown once on the page. Folding it in here would
repeat it on every row and make it read as something each workspace granted.

Asked only of `ctx.tenant_choices`, which the request already resolved from
the verified identity: no read, no Cloud Identity call. A workspace the caller
cannot act in has no role here (None), whatever its documents say.
"""

from __future__ import annotations

from typing import Any

from .auth import AuthContext

#: What `source` says of a role computed from memberships rather than stored.
DERIVED = "derived"
OWNER = "owner"
MEMBER = "member"


def role_in(ctx: AuthContext, tenant_id: str) -> dict[str, Any] | None:
    """`{"name", "source", "via"}` for a workspace in `ctx.tenant_choices`, else None.

    The personal entry is the one whose principal is the caller themselves, as
    `routes/tenants.py::_choice` reads it: `Authenticator._with_own_workspace`
    is the only place such an entry is made, after confirming the tenant's
    documents name this email."""
    for choice_id, principal in ctx.tenant_choices:
        if choice_id != tenant_id:
            continue
        if principal == ctx.email:
            return {"name": OWNER, "source": DERIVED, "via": None}
        return {"name": MEMBER, "source": DERIVED, "via": principal}
    return None
