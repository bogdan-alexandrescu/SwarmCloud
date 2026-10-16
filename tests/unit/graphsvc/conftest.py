"""swarm-graph's tests put its package on the path, as tests/unit/typesafe_fwd
does for swarm-typesafe-fwd: the app is not a member of the root uv workspace
(its image has its own lock, apps/swarm-graph/uv.lock), so nothing installs it
here. tests/unit/worker goes on the path too, so the hand-written graph and the
question battery GS-CORE's tests use (test_graphmcp_source.py,
test_graphmcp_tools_version.py) are the ones these tests serve, not a copy."""

from __future__ import annotations

import sys
from pathlib import Path

_UNIT = Path(__file__).resolve().parents[1]
for _path in (str(_UNIT.parents[1] / "apps" / "swarm-graph"), str(_UNIT / "worker")):
    if _path not in sys.path:
        sys.path.insert(0, _path)
