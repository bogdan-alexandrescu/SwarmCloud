"""Let the bridge's tests reach the control plane's fixtures.

`test_against_the_real_api.py` builds the REAL swarm-api application rather
than a fake of it, and the assembly for that -- settings, the in-memory
Firestore, the tenant seed -- already exists once in
`tests/unit/control_plane/conftest.py`. A second copy here would be a second
statement of what a tenant document looks like, which is the drift this
repository keeps finding.

`tests/unit/control_plane` is a package, so pytest puts `tests/unit` on
`sys.path` when it collects anything from it -- but only then. Running
`pytest tests/unit/mcp` on its own would not, and a suite that passes in one
invocation and fails in another is worse than either. So it is put there
explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)
