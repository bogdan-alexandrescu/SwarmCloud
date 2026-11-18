"""No resource address carries an alert email, and no channel a policy names is deleted.

A resource's address reaches this repository's public Actions logs and run
summaries (a plan's address list, a failed apply's resource), and
`alert_emails` comes from the private tfvars, so the email notification
channels are keyed by a hash of the address (terraform/modules/monitoring/alerts.tf).

The first release that keyed them by a hash failed: Terraform deleted the old
address-keyed channel before it repointed the alert policies, and Cloud
Monitoring refused (400 "still being referenced"). So the move took two
releases: one kept the address-keyed channels while every policy moved to the
hashed ones (2026-11-15), the next dropped them, and no address-keyed map is
left.

These read the Terraform as text: no provider, no plan, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TF_DIRS = (ROOT / "terraform" / "infra", ROOT / "terraform" / "modules", ROOT / "terraform" / "bootstrap")
ALERTS_TF = ROOT / "terraform" / "modules" / "monitoring" / "alerts.tf"

#: A `{ for X in <...var.something_emails...> : KEY => ... }` map, whatever
#: assignment it sits in (a local, or a for_each).
EMAIL_MAP = re.compile(r"\{\s*for\s+\w+\s+in\s+[^:}]*var\.[a-z0-9_]*emails?\b[^:}]*:\s*([^=}]+?)\s*=>", re.S)

#: The one address-keyed map allowed, for the one release the move takes.
RETIRING = re.compile(r"^\s*retiring_email_channels\s*=", re.M)


def _code(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _email_maps() -> list[tuple[Path, str, bool]]:
    """Each email map's file, its key expression, and whether it is the retiring one."""
    found = []
    for d in TF_DIRS:
        for tf in sorted(d.rglob("*.tf")):
            text = _code(tf.read_text())
            for m in EMAIL_MAP.finditer(text):
                line_start = text.rfind("\n", 0, m.start()) + 1
                retiring = bool(RETIRING.match(text[line_start:m.start()] + "="))
                found.append((tf, m.group(1).strip(), retiring))
    return found


def test_the_email_maps_are_found() -> None:
    """The scan reads what it is meant to: the monitoring module's hashed map."""
    hashed = [(p.name, k) for p, k, retiring in _email_maps() if not retiring]
    assert ("alerts.tf", "substr(sha256(e), 0, 12)") in hashed, hashed


def test_every_email_map_keys_by_a_hash() -> None:
    """The one-release retiring map is gone too.
    MUTATION: key email_channels by the address (`e => e`), or bring back
    `retiring_email_channels`."""
    bad = [(str(p.relative_to(ROOT)), k) for p, k, _retiring in _email_maps() if "sha256(" not in k]
    assert not bad, f"a map keyed by an email puts the email in a public resource address: {bad}"


def test_the_policies_name_only_the_hashed_channels() -> None:
    """Every policy's channel list is built from the hashed channels alone, so
    the retiring ones are named by nothing when the next release deletes them.
    MUTATION: drop the `if contains(keys(local.email_channels), k)` filter."""
    text = _code(ALERTS_TF.read_text())
    m = re.search(r"notification_channels\s*=\s*concat\((.*?)\n\s*\)", text, re.S)
    assert m, "alerts.tf has no notification_channels local"
    assert re.search(
        r"for\s+k\s*,\s*c\s+in\s+google_monitoring_notification_channel\.email\s*:\s*c\.id\s+if\s+contains\(keys\(local\.email_channels\),\s*k\)",
        m.group(1)), m.group(1)
