"""PW-TESTRUNNER (BGD, 2026-10-24, "Test-runner exception on browser"; part of #2253).

task_2791f0996eab4f958252 could not run its repository's own Playwright suite
on claude-code-browser: the agent's `PLAYWRIGHT_BROWSERS_PATH` is the
live-browser stub (BROWSER-ENFORCE, #1030), and the image's /opt/playwright is
read-only, so the revision the repository's `@playwright/test` pins had nowhere
to go. The decision: a headless TEST process may launch its own Chromium; the
agent's own browsing stays on the live browser.

So, on the browser image only:
  * the runner makes a writable browsers directory in the attempt's cache
    (`$XDG_CACHE_HOME/swarm-test-browsers`), seeded with links to the image's
    revisions, and names it to the agent as `SWARM_TEST_BROWSERS_PATH`;
  * `swarm-test-browsers <command>` (shipped in the image) runs ONE command
    with `PLAYWRIGHT_BROWSERS_PATH` pointed there; the agent's own default
    stays the stub;
  * the prompt names the command and says `--with-deps` is never needed.

MUTATION: drop the seeding loop from `prepare_test_browsers`, the
`SWARM_TEST_BROWSERS_PATH` line from `claude_code.body`, the override in the
wrapper or its COPY from the Dockerfile, and the named test below fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
IMAGE_DIR = REPO / "images" / "agent-runtime-browser"
WRAPPER = IMAGE_DIR / "swarm-test-browsers"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
ENDPOINT = "http://127.0.0.1:9222"
REVISION = "chromium-1200"


def _image_browsers(tmp_path: Path) -> Path:
    """A stand-in for /opt/playwright: one installed revision and Playwright's own `.links`."""
    image = tmp_path / "opt-playwright"
    (image / REVISION / "chrome-linux").mkdir(parents=True)
    (image / REVISION / "INSTALLATION_COMPLETE").write_text("")
    (image / ".links").mkdir()
    return image


# --- the wrapper -----------------------------------------------------------------


def _child_env(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    probe = ("import json, os; print(json.dumps({k: os.environ.get(k) for k in "
             "('PLAYWRIGHT_BROWSERS_PATH', 'PLAYWRIGHT_SKIP_BROWSER_GC')}))")
    return subprocess.run(
        [sys.executable, str(WRAPPER), *(args or (sys.executable, "-c", probe))],
        env=env, capture_output=True, text=True, timeout=60)


def test_the_wrapper_points_its_child_at_the_test_directory(tmp_path):
    test_dir = tmp_path / "swarm-test-browsers"
    test_dir.mkdir()
    env = {"PATH": os.environ.get("PATH", ""),
           "PLAYWRIGHT_BROWSERS_PATH": "/opt/swarm/live-browser-only",
           "SWARM_TEST_BROWSERS_PATH": str(test_dir)}
    done = _child_env(env)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen["PLAYWRIGHT_BROWSERS_PATH"] == str(test_dir)
    # The seeded links to the image's revisions stay put across installs.
    assert seen["PLAYWRIGHT_SKIP_BROWSER_GC"] == "1"
    # The caller's own environment is not the child's: it keeps the stub.
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/swarm/live-browser-only"


def test_the_wrapper_refuses_with_deps(tmp_path):
    test_dir = tmp_path / "swarm-test-browsers"
    test_dir.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "SWARM_TEST_BROWSERS_PATH": str(test_dir)}
    done = _child_env(env, "npx", "playwright", "install", "--with-deps", "chromium")
    assert done.returncode == 2
    assert "--with-deps" in done.stderr and "already" in done.stderr


def test_the_wrapper_refuses_without_its_directory(tmp_path):
    env = {"PATH": os.environ.get("PATH", "")}
    done = _child_env(env, sys.executable, "-c", "print('ran')")
    assert done.returncode == 2 and "ran" not in done.stdout
    assert "SWARM_TEST_BROWSERS_PATH" in done.stderr
    env["SWARM_TEST_BROWSERS_PATH"] = str(tmp_path / "absent")
    done = _child_env(env, sys.executable, "-c", "print('ran')")
    assert done.returncode == 2 and "ran" not in done.stdout


def test_the_wrapper_needs_a_command(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "SWARM_TEST_BROWSERS_PATH": str(tmp_path)}
    done = subprocess.run([sys.executable, str(WRAPPER)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "usage" in done.stderr.lower()


def test_the_image_ships_the_wrapper_read_only_on_path():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert ("COPY --chmod=0555 images/agent-runtime-browser/swarm-test-browsers "
            "/usr/local/bin/swarm-test-browsers") in text


# --- the writable directory --------------------------------------------------------


def test_the_directory_is_seeded_with_links_to_the_images_revisions(tmp_path):
    from agent_worker.runners import claude_code

    image = _image_browsers(tmp_path)
    cache = tmp_path / "cache" / "xdg"
    made = claude_code.prepare_test_browsers(image, cache)
    assert made == cache / claude_code.TEST_BROWSERS_DIR_NAME
    link = made / REVISION
    assert link.is_symlink() and link.resolve() == (image / REVISION).resolve()
    # Playwright's own bookkeeping is written here, not linked to the read-only one.
    assert not (made / ".links").exists()
    assert os.access(made, os.W_OK)
    # A second attempt's call, and a revision the repository installed, are kept.
    (made / "chromium-1300").mkdir()
    assert claude_code.prepare_test_browsers(image, cache) == made
    assert (made / "chromium-1300").is_dir() and link.is_symlink()


def test_an_unreadable_image_directory_offers_no_test_directory(tmp_path):
    from agent_worker.runners import claude_code

    assert claude_code.prepare_test_browsers(tmp_path / "absent", tmp_path / "xdg") is None


# --- the runner ---------------------------------------------------------------------


def _run_body(monkeypatch, tmp_path, *, browsers: str | None, endpoint: str | None,
              xdg: Path | None) -> dict[str, Any]:
    from agent_worker.runners import claude_code
    from agent_worker.runners.base import RunnerContext

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["env"] = dict(extra_env or {})
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in (claude_code.GIT_SPOOL_ENV, claude_code.SEARCH_SPOOL_ENV,
                 claude_code.STATE_SPOOL_ENV, claude_code.HANDOFF_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in ((claude_code.BROWSERS_PATH_ENV, browsers),
                        (claude_code.CDP_ENDPOINT_ENV, endpoint),
                        ("XDG_CACHE_HOME", None if xdg is None else str(xdg))):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "artifacts").mkdir()
    claude_code.body(RunnerContext(
        work_dir=work, artifacts_dir=tmp_path / "artifacts", input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json", quota_path=tmp_path / "quota.json",
        payload={"prompt": "Run the repository's end-to-end suite."}))
    return seen


def test_the_live_browser_agent_keeps_the_stub_and_gets_the_test_directory(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    image = _image_browsers(tmp_path)
    xdg = tmp_path / "cache" / "xdg"
    seen = _run_body(monkeypatch, tmp_path, browsers=str(image), endpoint=ENDPOINT, xdg=xdg)
    assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == claude_code.AGENT_BROWSERS_PATH
    test_dir = Path(seen["env"]["SWARM_TEST_BROWSERS_PATH"])
    # In the attempt's cache (never checkpointed, off the workspace volume on
    # GKE), never /opt, and under no name Playwright looks in by itself.
    assert test_dir == xdg / "swarm-test-browsers"
    assert test_dir.name != "ms-playwright"
    assert (test_dir / REVISION).is_symlink()
    assert claude_code.LIVE_BROWSER_NOTE in seen["prompt"]
    assert claude_code.TEST_RUNNER_NOTE in seen["prompt"]


def test_the_note_names_the_command_and_never_with_deps():
    from agent_worker.runners import claude_code

    note = claude_code.TEST_RUNNER_NOTE
    assert "swarm-test-browsers npx playwright install chromium" in note
    assert "swarm-test-browsers npx playwright test" in note
    assert "never" in note and "--with-deps" in note
    assert "only" in note  # not for the agent's own browsing


def test_without_a_cache_directory_it_goes_under_work(monkeypatch, tmp_path):
    image = _image_browsers(tmp_path)
    seen = _run_body(monkeypatch, tmp_path, browsers=str(image), endpoint=ENDPOINT, xdg=None)
    test_dir = Path(seen["env"]["SWARM_TEST_BROWSERS_PATH"])
    assert test_dir.is_relative_to(tmp_path / "work") and (test_dir / REVISION).is_symlink()


def test_the_headless_browser_image_gets_it_too(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    image = _image_browsers(tmp_path)
    seen = _run_body(monkeypatch, tmp_path, browsers=str(image), endpoint=None,
                     xdg=tmp_path / "xdg")
    assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(image)
    assert seen["env"]["SWARM_TEST_BROWSERS_PATH"] == str(tmp_path / "xdg" / "swarm-test-browsers")
    assert seen["prompt"].endswith(claude_code.TEST_RUNNER_NOTE)


def test_claude_code_without_the_browser_image_is_unchanged(monkeypatch, tmp_path):
    from agent_worker.runners import claude_code

    xdg = tmp_path / "xdg"
    seen = _run_body(monkeypatch, tmp_path, browsers=None, endpoint=None, xdg=xdg)
    assert "SWARM_TEST_BROWSERS_PATH" not in seen["env"]
    assert "PLAYWRIGHT_BROWSERS_PATH" not in seen["env"]
    assert "swarm-test-browsers" not in seen["prompt"]
    assert not xdg.exists()
    assert not hasattr(claude_code, "TEST_RUNNER_NOTE") or claude_code.TEST_RUNNER_NOTE not in seen["prompt"]


def test_the_note_names_the_test_path_and_how_a_runner_uses_it():
    # FIX-2253: a test runner the wrapper cannot prefix (a script that starts
    # its own `npx`, a Makefile target) is pointed at the directory by name.
    from agent_worker.runners import claude_code

    note = claude_code.TEST_RUNNER_NOTE
    # MUTATION: drop the variable from the note -- the agent learns no path to give a runner.
    assert f"${claude_code.TEST_BROWSERS_ENV}" in note
    assert f'PLAYWRIGHT_BROWSERS_PATH="${claude_code.TEST_BROWSERS_ENV}" npx playwright test' in note
