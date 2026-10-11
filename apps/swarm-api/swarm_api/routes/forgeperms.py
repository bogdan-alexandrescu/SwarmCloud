"""GitHub permissions health (#1086, PERM-1): `GET /v1/forge/permissions`.

The route and the read live together in `swarm_api.forgeperms`; this module
only names its router where `create_app` and the UI route seam
(tests/unit/control_plane/test_runtimes_screen.py) look for every router.
"""

from __future__ import annotations

from ..forgeperms import router

__all__ = ["router"]
