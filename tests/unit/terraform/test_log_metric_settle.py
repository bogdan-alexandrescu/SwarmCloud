"""An alert policy never races the log-based metric it reads.

On 2026-11-23 a dev release created two alert policies in the same apply as
the log-based metrics they read (terraform/infra/voice_alerts.tf) and both
failed with a 404: a new log-based metric is not readable by Monitoring at
once (the provider's message says it can take up to ten minutes). A fresh
install, and any release that adds a metric and a policy together, meets it.

The fix is a create-time `local-exec` sleep on every log-based metric a
policy reads. A create-time provisioner runs only when its resource is
created or replaced, so a release that creates no metric waits zero seconds,
and adding the block to an existing metric changes nothing in its state.

These read the Terraform as text: no provider, no plan, no credentials. They
find the policies themselves, in terraform/infra and every module, so a new
policy on a new metric is held without being named here.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TF = ROOT / "terraform"
MONITORING = TF / "modules" / "monitoring"

_HEADER = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"\s*\{', re.M)
_METRIC_REF = re.compile(r'google_logging_metric\.([A-Za-z0-9_]+)(?:\["([A-Za-z0-9_]+)"\])?')
_SLEEP = re.compile(r'command\s*=\s*"sleep \$\{(.+)\}"')


def _code(text: str) -> str:
    """The text with `#` comments removed (only whole-line and trailing
    comments outside strings appear in these files' resource headers)."""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        out.append("" if stripped.startswith("#") else line)
    return "\n".join(out)


def _blocks(directory: Path) -> dict[tuple[str, str], str]:
    """(type, name) -> the block body, braces balanced, for every resource in
    the directory's .tf files."""
    found: dict[tuple[str, str], str] = {}
    for path in sorted(directory.glob("*.tf")):
        text = _code(path.read_text(encoding="utf-8"))
        for m in _HEADER.finditer(text):
            depth = 0
            for j in range(m.end() - 1, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        found[(m.group(1), m.group(2))] = text[m.end() : j]
                        break
            else:
                raise AssertionError(f"unbalanced block {m.group(0)!r} in {path}")
    return found


def _block(body: str, header: str) -> str | None:
    m = re.search(header + r"\s*\{", body)
    if not m:
        return None
    depth = 0
    for j in range(m.end() - 1, len(body)):
        if body[j] == "{":
            depth += 1
        elif body[j] == "}":
            depth -= 1
            if depth == 0:
                return body[m.end() : j]
    raise AssertionError(f"unbalanced block {header!r}")


def _directories() -> list[Path]:
    return [TF / "infra"] + sorted(p for p in (TF / "modules").iterdir() if p.is_dir())


def _reads() -> list[tuple[Path, str, str, str | None]]:
    """(directory, policy, metric, for_each key or None) for every alert
    policy that reads a log-based metric defined beside it."""
    reads = []
    for directory in _directories():
        blocks = _blocks(directory)
        for (rtype, name), body in blocks.items():
            if rtype != "google_monitoring_alert_policy":
                continue
            for m in _METRIC_REF.finditer(body):
                assert ("google_logging_metric", m.group(1)) in blocks, (
                    f"{directory.name}: policy {name} reads google_logging_metric.{m.group(1)}, "
                    "which is not defined in the same directory"
                )
                reads.append((directory, name, m.group(1), m.group(2)))
    return reads


def test_the_sweep_finds_the_policies_it_must_hold() -> None:
    """Not an empty sweep: the three in terraform/infra and the module's are found."""
    reads = _reads()
    policies = {(d.name, p) for d, p, _, _ in reads}
    assert {
        ("infra", "doctor_findings"),
        ("infra", "voice_session_idle"),
        ("infra", "speech_machine_unheld"),
        ("monitoring", "safety_tick_absent"),
        ("monitoring", "generation_fenced"),
        ("monitoring", "tasks_dead_lettered"),
        ("monitoring", "reconciler_blind"),
        ("monitoring", "pr_stranded"),
        ("monitoring", "tenant_namespace_mismatch"),
        ("monitoring", "reconciler_pass_slow"),
    } <= policies, sorted(policies)
    assert len(policies) >= 14, sorted(policies)


def test_every_metric_a_policy_reads_waits_on_create() -> None:
    reads = _reads()
    missing = []
    for directory, policy, metric, _key in reads:
        body = _blocks(directory)[("google_logging_metric", metric)]
        prov = _block(body, r'provisioner\s+"local-exec"')
        if prov is None:
            missing.append(f"{directory.name}: {metric} (read by {policy}) has no local-exec wait")
            continue
        if re.search(r"\bwhen\s*=", prov):
            missing.append(f"{directory.name}: {metric}'s wait is not a create-time provisioner")
        if not re.search(r"\bon_failure\s*=\s*continue\b", prov):
            # A failed provisioner taints the metric, and the next apply would
            # REPLACE it: its history gone for the sake of a sleep.
            missing.append(f"{directory.name}: {metric}'s wait lacks on_failure = continue")
        if not _SLEEP.search(prov):
            missing.append(f"{directory.name}: {metric}'s wait is not a bounded `sleep`")
    assert not missing, missing


def test_the_wait_is_one_bounded_number_and_zero_without_policies() -> None:
    locals_text = _code((MONITORING / "metrics.tf").read_text(encoding="utf-8"))
    m = re.search(
        r"^\s*log_metric_settle_seconds\s*=\s*var\.create_alerts\s*\?\s*(\d+)\s*:\s*0\s*$",
        locals_text,
        re.M,
    )
    assert m, "monitoring's log_metric_settle_seconds is not `var.create_alerts ? N : 0`"
    seconds = int(m.group(1))
    # The provider's own message bounds the delay at ten minutes; more than
    # that waits for nothing.
    assert 0 < seconds <= 600, seconds

    outputs = _code((MONITORING / "outputs.tf").read_text(encoding="utf-8"))
    output = _block(outputs, r'output\s+"log_metric_settle_seconds"')
    assert output is not None, "monitoring does not output log_metric_settle_seconds"
    assert re.search(r"^\s*value\s*=\s*local\.log_metric_settle_seconds\s*$", output, re.M), output

    infra_locals = "\n".join(
        _code(p.read_text(encoding="utf-8")) for p in sorted((TF / "infra").glob("*.tf"))
    )
    assert re.search(
        r"^\s*log_metric_settle_seconds\s*=\s*module\.monitoring\.log_metric_settle_seconds\s*$",
        infra_locals,
        re.M,
    ), "terraform/infra restates the wait instead of reading the module's"


def test_every_wait_sleeps_the_shared_number() -> None:
    for directory, _policy, metric, key in _reads():
        prov = _block(_blocks(directory)[("google_logging_metric", metric)], r'provisioner\s+"local-exec"')
        assert prov is not None
        expr = _SLEEP.search(prov)
        assert expr, metric
        assert "local.log_metric_settle_seconds" in expr.group(1), (directory.name, metric, expr.group(1))
        if key is not None:
            # A for_each metric waits only for the instances a policy reads:
            # the rest are never read by a policy and need no wait.
            assert "local.alerted_events" in expr.group(1), (metric, expr.group(1))


def test_every_alerted_event_is_listed() -> None:
    text = _code((MONITORING / "metrics.tf").read_text(encoding="utf-8"))
    m = re.search(r"^\s*alerted_events\s*=\s*\[([^\]]*)\]", text, re.M)
    assert m, "monitoring has no local alerted_events"
    listed = set(re.findall(r'"([a-z0-9_]+)"', m.group(1)))
    read = {key for d, _p, metric, key in _reads() if d == MONITORING and metric == "events" and key}
    assert read, "no policy reads an events instance: the sweep did not run"
    assert read <= listed, sorted(read - listed)
