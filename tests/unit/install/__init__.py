"""Tests for apps/swarm-install (the Day One installer).

apps/swarm-install is not yet a member of the root uv workspace: adding a
package to uv.lock obliges every service Dockerfile's `uv export` to `--prune`
it (tests/unit/scripts/test_service_image_install.py), which is outside lane
DAYONE-MANIFEST-1's territory and asked as a question in its PR. Until then
these tests import `swarm_install` from its source directory. Its own
dependencies (pydantic, pyyaml) are already in the workspace through the
services that use them.
"""

import sys
from pathlib import Path

_SOURCE = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-install")
if _SOURCE not in sys.path:
    sys.path.insert(0, _SOURCE)
