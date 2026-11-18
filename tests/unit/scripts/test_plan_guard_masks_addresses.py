"""What the plan guard prints to the public log names no private address.

The guard runs in the release and the plan workflow, and its stderr is their
public Actions log. A resource address can carry a private address as its key
(the alert email channels were keyed by the address until 2026-11-15, and
deleting the last of them lists it as a resource that does not identify itself
as ours), and an IAM row's member comes from the private tfvars. So every
address the guard prints has an email-shaped key masked, and its IAM lines name
the address and the role only (the owner's pick of 2026-11-14).

The guard runs from a copy of scripts/lib under tmp_path, as
test_plan_guard_metric_replace.py does, so concurrent workers do not share its
build/ verdict.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib"
_SPEC = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_P = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_P)

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

#: Built at runtime, so no address-shaped literal sits in the file.
ADDRESS = "someone" + "@" + "example.com"


def _guard(tmp_path: Path, *changes: dict, args: tuple[str, ...]) -> subprocess.CompletedProcess:
    root = tmp_path / "repo"
    shutil.copytree(LIB, root / "scripts" / "lib")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"format_version": "1.2", "resource_changes": list(changes)}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "GCP_", "PROJECT"))}
    env.update(NO_COLOR="1", SWARM_ENV_FILE=str(tmp_path / "no.env"), PROJECT_ID=_P.PROJECT)
    env.update(_P.deployment().child_env(_P.PROJECT))
    return subprocess.run([str(root / "scripts" / "lib" / "plan-guard.sh"), "--plan", str(plan), *args],
                          capture_output=True, text=True, timeout=60, env=env, check=False)


def test_an_address_keyed_resource_is_printed_masked(tmp_path):
    """MUTATION: drop the gsub from the foreign_touches line in plan-guard.sh."""
    values = {"project": _P.PROJECT, "type": "email",
              "name": f"projects/{_P.PROJECT}/notificationChannels/1234567890",
              "labels": {"email_address": ADDRESS}, "user_labels": {"managed-by": "swarm-terraform"}}
    channel = {
        "address": f'module.monitoring.google_monitoring_notification_channel.email["{ADDRESS}"]',
        "type": "google_monitoring_notification_channel",
        "change": {"actions": ["delete"], "before": values, "after": None},
    }
    result = _guard(tmp_path, channel, args=("--mode", "apply"))
    assert ADDRESS not in result.stderr + result.stdout, result.stderr
    assert 'notification_channel.email["<an address>"]' in result.stderr, result.stderr


def test_the_iam_lines_name_the_role_never_the_member(tmp_path):
    """MUTATION: print `on \\(.target): \\(.detail)` again in the classify step."""
    values = {"project": _P.PROJECT, "role": "roles/viewer", "member": f"user:{ADDRESS}"}
    grant = {
        "address": "module.iam.google_project_iam_member.plain",
        "type": "google_project_iam_member",
        "change": {"actions": ["create"], "before": None, "after": values, "after_unknown": {}},
    }
    result = _guard(tmp_path, grant, args=("--classify-iam",))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true", result.stdout
    assert ADDRESS not in result.stderr, result.stderr
    assert "role: roles/viewer" in result.stderr, result.stderr
