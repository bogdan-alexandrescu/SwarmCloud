"""swarm-egress's tests put its package on the path, as tests/unit/live does
for swarm-live: the app is not a member of the root uv workspace (its image
has its own lock, apps/swarm-egress/uv.lock), so nothing installs it here."""

from __future__ import annotations

import sys
from pathlib import Path

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-egress")
if _APP not in sys.path:
    sys.path.insert(0, _APP)
