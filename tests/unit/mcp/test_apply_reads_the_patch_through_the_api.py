"""`swarm_apply` reads a task's patch through swarm-api's artifact route (#2834).

WHAT BROKE. `swarm_apply` downloaded `swarm-work.patch` straight from Cloud
Storage with the developer's own gcloud access token. On 2026-10-30 that login
needed reauthentication, which cannot prompt from a non-interactive bridge, so
`swarm_apply` failed on task_3ed834efdaa14baab798 while `swarm_artifact` read
the same 10,921-byte patch through the API with the bridge's own credential.

WHAT THIS HOLDS. The patch is read through `/v1/tasks/{id}/artifacts/content`
(`SwarmClient.artifact_content`), window after window until the API says it is
complete; the bytes are checked before `git apply` -- a window the API
redacted, one with bytes that are not UTF-8, or windows that do not join end to
end are refused with the reason, and nothing is applied. No access token is
minted and no request leaves through urllib on that path: the fake client's
`access_token` and `urllib.request.urlopen` both fail the test if touched.

Offline: a fake API that answers from a dict, and real git for the apply.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request

import pytest

from swarm_mcp import patches, server
from swarm_mcp.client import SwarmError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

PATCH_NAME = "swarm-work.patch"


class FakeApi:
    """swarm-api's artifact-content route, from a dict, in small windows.

    The window is the SERVER's choice (the route clamps `limit_bytes`), so this
    serves `window` bytes whatever the caller asked for -- which is what makes
    a patch of a few hundred bytes a multi-window read here.
    """

    def __init__(self, patch: bytes, *, window: int = 64, redact_window: int | None = None,
                 invalid_window: int | None = None, skip_bytes: int = 0):
        self.patch = patch
        self.window = window
        self.redact_window = redact_window
        self.invalid_window = invalid_window
        self.skip_bytes = skip_bytes
        self.reads: list[tuple[str, str, int]] = []
        self.task_doc = {
            "id": "task_a",
            "state": "SUCCEEDED",
            "result_summary": {
                "git": {"patch": PATCH_NAME},
                "artifacts": [{"name": PATCH_NAME, "uri": "gs://bucket/tenants/t/swarm-work.patch"}],
            },
        }

    def task(self, task_id):
        assert task_id == "task_a"
        return self.task_doc

    def artifact_content(self, task_id, name, *, offset=0, limit_bytes=None):
        index = len(self.reads)
        self.reads.append((task_id, name, offset))
        size = len(self.patch)
        stop = min(size, offset + self.window)
        data = self.patch[offset:stop]
        complete = stop >= size
        row = {
            "name": name,
            "status": "ok",
            "content": data.decode("utf-8"),
            "offset": offset,
            "returned_bytes": len(data),
            "total_bytes": size,
            "next_offset": None if complete else stop + self.skip_bytes,
            "truncated": not complete,
            "redacted": False,
            "redaction_count": 0,
            "invalid_utf8_bytes": 0,
        }
        if index == self.redact_window:
            row.update(content="[REDACTED]", redacted=True, redaction_count=1)
        if index == self.invalid_window:
            row["invalid_utf8_bytes"] = 2
        return row

    def access_token(self):
        raise AssertionError("swarm_apply minted a gcloud access token; the patch must come through the API")


@pytest.fixture(autouse=True)
def _no_direct_storage(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a request left through urllib: the patch must be read through the API client")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(subprocess, "Popen", _only_git(subprocess.Popen))


def _only_git(real):
    """Every subprocess here is git; a gcloud call fails the test."""

    class OnlyGit(real):
        def __init__(self, args, *rest, **kwargs):
            program = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
            assert "gcloud" not in str(program), f"a gcloud call was made: {args}"
            super().__init__(args, *rest, **kwargs)

    return OnlyGit


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        check=True, capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=main")
    (path / "types.ts").write_text("export interface A {\n  x: number\n}\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "base")
    return path


def _patch(repo) -> bytes:
    """A real patch of several hundred bytes, made by a commit and then undone."""
    body = "".join(f"export const line{n} = {n}\n" for n in range(30))
    (repo / "added.ts").write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "work")
    patch = subprocess.run(
        ["git", "-C", str(repo), "format-patch", "--stdout", "HEAD~1"],
        check=True, capture_output=True,
    ).stdout
    _git(repo, "reset", "--quiet", "--hard", "HEAD~1")
    return patch


def test_swarm_apply_reads_a_multi_window_patch_through_the_api_and_applies_it(repo):
    patch = _patch(repo)
    api = FakeApi(patch, window=64)
    assert len(patch) > 3 * api.window, "the fixture must span several windows"

    answer = json.loads(server._call(api, "swarm_apply", {"task_id": "task_a", "repo": str(repo)}))

    assert answer["applied"] is True, answer
    assert answer["conflicts"] == []
    assert (repo / "added.ts").read_text().endswith("export const line29 = 29\n")
    # Every window, in order, each from where the last one ended, by NAME.
    expected = list(range(0, len(patch), api.window))
    assert api.reads == [("task_a", PATCH_NAME, offset) for offset in expected]


def test_a_redacted_patch_is_refused_with_its_reason_and_never_applied(repo):
    patch = _patch(repo)
    api = FakeApi(patch, window=64, redact_window=2)

    answer = json.loads(server._call(api, "swarm_apply", {"task_id": "task_a", "repo": str(repo)}))

    assert answer["applied"] is False
    assert "redact" in answer["reason"]
    assert not (repo / "added.ts").exists()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout
    assert status == ""


def test_a_window_with_bytes_that_are_not_utf8_is_refused(repo):
    api = FakeApi(_patch(repo), window=64, invalid_window=1)

    answer = json.loads(server._call(api, "swarm_apply", {"task_id": "task_a", "repo": str(repo)}))

    assert answer["applied"] is False
    assert "UTF-8" in answer["reason"]
    assert not (repo / "added.ts").exists()


def test_windows_that_do_not_join_end_to_end_are_refused(repo):
    """A gap between two windows would apply a patch with bytes missing."""
    api = FakeApi(_patch(repo), window=64, skip_bytes=3)

    answer = json.loads(server._call(api, "swarm_apply", {"task_id": "task_a", "repo": str(repo)}))

    assert answer["applied"] is False
    assert "next_offset" in answer["reason"]
    assert not (repo / "added.ts").exists()


@pytest.mark.parametrize("status", ["absent", "unreadable", "binary"])
def test_a_patch_the_route_does_not_serve_as_text_is_refused_with_the_routes_detail(status):
    api = FakeApi(b"diff --git a/x b/x\n")
    api.artifact_content = lambda *a, **k: {"status": status, "content": None, "detail": "why"}

    with pytest.raises(patches.PatchRefused, match=status):
        patches.read_patch(api, api.task_doc)


def test_read_patch_returns_the_exact_bytes_across_windows(repo):
    patch = _patch(repo)
    api = FakeApi(patch, window=50)

    assert patches.read_patch(api, api.task_doc) == patch
    assert len(api.reads) > 1


def test_integrate_reads_each_patch_through_the_api_too(repo):
    patch = _patch(repo)
    api = FakeApi(patch, window=64)

    result = patches.integrate(api, ["task_a"], repo, branch="swarm/batch")

    assert [r.task_id for r in result.results] == ["task_a"]
    assert result.results[0].clean, result.results[0].detail
    assert (repo / "added.ts").exists()


def test_integrate_reports_a_refused_patch_as_failed_not_skipped(repo):
    api = FakeApi(_patch(repo), window=64, redact_window=0)

    result = patches.integrate(api, ["task_a"], repo, branch="swarm/batch")

    assert result.results[0].applied is False
    assert "redact" in result.results[0].detail
    assert "FAILED" in result.render()
    assert not (repo / "added.ts").exists()


def test_a_refusal_is_a_swarm_error():
    assert issubclass(patches.PatchRefused, SwarmError)


# The terminal command `swarm apply` (cli.cmd_apply) read the patch the old way
# after PR 2843 moved the plugin tool: `download(patch_uri(task))`, a Cloud
# Storage read with the developer's gcloud login (#2834). The same fake API
# holds it to the same route: `access_token`, urllib and gcloud fail the test.


def _cli_apply(api, repo):
    from argparse import Namespace

    from swarm_mcp import cli

    return cli.cmd_apply(api, Namespace(task_id="task_a", into=str(repo)))


def test_swarm_apply_on_the_terminal_reads_the_patch_through_the_api_and_applies_it(repo, capsys):
    from swarm_mcp import cli

    patch = _patch(repo)
    api = FakeApi(patch, window=64)

    code = _cli_apply(api, repo)

    assert code == cli.EXIT_OK, capsys.readouterr()
    assert (repo / "added.ts").read_text().endswith("export const line29 = 29\n")
    expected = list(range(0, len(patch), api.window))
    assert api.reads == [("task_a", PATCH_NAME, offset) for offset in expected]


def test_swarm_apply_on_the_terminal_refuses_a_redacted_patch_with_its_reason(repo, capsys):
    from swarm_mcp import cli

    api = FakeApi(_patch(repo), window=64, redact_window=1)

    code = _cli_apply(api, repo)

    assert code == cli.EXIT_FAIL
    assert "redact" in capsys.readouterr().err
    assert not (repo / "added.ts").exists()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout
    assert status == ""
