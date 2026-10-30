"""`swarm install` on a fresh laptop (lane DAYONE-PREFLIGHT-RECORD, part of
#2688): the lease holder is the account the installer-fetched gcloud is signed
in as, and the summary names the release this installer will record.

No credentials, no emulator and no real terminal (the fixtures are
test_install_asks.py's).
"""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from swarm_install import cli
from swarm_install.exitcodes import ExitCode
from swarm_install.steps import preflight
from swarm_install.verbs import install

from .test_install_asks import _terminal, world  # noqa: F401 - `world` is a fixture


def test_holder_identity_resolves_gcloud_through_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit #3: the SDK preflight fetched lives in ~/.swarm, not on PATH, so
    the holder is read through `preflight.find_gcloud`, never `which`."""
    ran: list[list[str]] = []

    def run(argv, **_kw):
        ran.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="admin@acme.example\n")

    monkeypatch.setattr(shutil, "which", lambda name: pytest.fail(f"looked {name} up on PATH"))
    monkeypatch.setattr(preflight, "find_gcloud", lambda: "/home/a/.swarm/google-cloud-sdk/bin/gcloud")
    monkeypatch.setattr(subprocess, "run", run)
    assert install.holder_identity() == "admin@acme.example"
    assert ran == [["/home/a/.swarm/google-cloud-sdk/bin/gcloud", "config", "get-value", "account"]]

    monkeypatch.setattr(preflight, "find_gcloud", lambda: None)
    assert "@" in install.holder_identity()  # the local user at this host, as before


def test_summary_names_the_installers_own_release(world, monkeypatch, capsys):  # noqa: F811
    """Audit #2 and #30: with no --version the summary names the release
    preflight.release will record, the installed wheel's own."""
    monkeypatch.setattr(preflight, "installed_release", lambda: "2026.11.3-rc.2")
    _terminal(monkeypatch, "acme-swarm-1\nswarm.acme.example\neurope-west1\n\n")
    assert cli.main(["install"]) == ExitCode.OK
    out = capsys.readouterr().out
    summary = out[out.index("? Region"):out.index("Continue? [Y/n]")]
    assert "Release:   2026.11.3-rc.2" in summary
    assert "stable" not in summary


def test_summary_names_the_channel_when_the_wheel_has_no_release(world, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(preflight, "installed_release", lambda: None)
    monkeypatch.setenv("SWARM_CHANNEL", "rc")
    _terminal(monkeypatch, "acme-swarm-1\nswarm.acme.example\neurope-west1\n\n")
    assert cli.main(["install"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "Release:   the current rc release" in out


def test_the_run_sends_budget_calls_with_its_project_as_quota_project(world, monkeypatch):  # noqa: F811
    """Audit #8: from stage 1 on, the shared Gcp sends billing-budget calls
    with the install's project as quota project."""
    shared = preflight._Lazy()
    monkeypatch.setattr(preflight, "GCP", shared)
    _terminal(monkeypatch, "", tty=False)
    cli.main(["install", "--project", "acme-swarm-1"])
    assert shared.quota_project == "acme-swarm-1"
