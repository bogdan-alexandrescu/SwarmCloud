"""The Cloud Run start finding of 2026-10-07: the code-only check left after
issue #71 dropped the doc-prose assertions.

No credentials, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

REPO = Path(__file__).resolve().parents[3]
LOCALS = REPO / "terraform" / "infra" / "locals.tf"


def _profiles_without_a_job() -> set[str]:
    m = re.search(r"^\s*profiles_without_a_job\s*=\s*\[([^\]]*)\]",
                  LOCALS.read_text(encoding="utf-8"), flags=re.M)
    assert m, "terraform/infra/locals.tf no longer declares profiles_without_a_job"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


# --------------------------------------------------------------------------
# terraform/infra/locals.tf: profiles_without_a_job can only name Cloud Run profiles
# --------------------------------------------------------------------------


def test_profiles_without_a_job_are_all_backed_by_cloud_run():
    """locals.tf's profiles_without_a_job can only list a profile the catalogue backs by
    Cloud Run Jobs: a GKE profile never gets a Cloud Run Job in the first place, so
    naming one here would be a silent no-op rather than suppressing anything."""
    without = _profiles_without_a_job()
    assert without, "profiles_without_a_job is empty: re-read the premise"
    for name in without:
        # MUTATION: add a GKE-backed profile to profiles_without_a_job in locals.tf -> red.
        assert resolve_backend(RUNNER_PROFILES[name]) is Backend.CLOUD_RUN_JOB, name
