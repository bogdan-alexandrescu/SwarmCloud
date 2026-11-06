"""A visual-check brief names the venv's Python by its full path (part of #2688).

On 3 November 2026 a production capture leg on `claude-code-browser` ran the
brief's `python -m swarm_qa capture ...` and got "/usr/local/bin/python: No
module named swarm_qa": its shell resolved `python` outside /opt/venv, where
the browser image installs the tool. So every brief that runs the capture or
discovery tool names the venv's interpreter by its full path, built from the
one constant the runner puts first on PATH (`swarm_rollup.agentimage`), and
never runs a bare `python -m swarm_qa`.
"""

from __future__ import annotations

import re
from typing import Any

from swarm_rollup import agentimage

from agent_worker.runners import claude_code
from swarm_api import qaspec


def _target(**over: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        "id": "qat_3f9a",
        "revision": 4,
        "name": "Pricing site",
        "tenant_id": "tenant-a",
        "base_url": "https://app.example.com",
        "repository": "https://github.com/example-org/pricing-site",
        "sign_in": {"kind": "none"},
        "screens": [{"id": "home", "path": "/"}, {"id": "pricing", "path": "/pricing"}],
        "flows": [],
        "environment": "production",
    }
    target.update(over)
    return target


def _prompts(spec: dict[str, Any]) -> list[str]:
    return [step["prompt"] for step in spec["steps"]]


def _tool_runs(prompt: str) -> list[str]:
    """Every command in a brief that runs the swarm_qa module, as written."""
    return re.findall(r"`([^`]*-m swarm_qa [^`]*)`", prompt)


def test_the_constant_is_the_runners_venv() -> None:
    # The runner's PATH prefix and the brief's interpreter are one constant.
    assert claude_code.VENV_BIN is agentimage.VENV_BIN
    assert agentimage.VENV_PYTHON == f"{agentimage.VENV_BIN}/python"
    assert agentimage.QA_TOOL == f"{agentimage.VENV_PYTHON} -m swarm_qa"


def test_every_capture_brief_runs_the_tool_with_the_venvs_python() -> None:
    captures = [p for p in _prompts(qaspec.render(_target())) if _tool_runs(p)]
    assert captures, "the rendered run has no capture step"
    for prompt in captures:
        (command,) = _tool_runs(prompt)
        # MUTATION: write `python -m swarm_qa capture` again and this fails.
        assert command.startswith(f"{agentimage.QA_TOOL} capture ")
        assert command.startswith("/opt/venv/bin/python -m swarm_qa capture ")


def test_the_discovery_brief_runs_the_tool_with_the_venvs_python() -> None:
    spec = qaspec.render_discovery(_target(screens=None, discover={"max_pages": 30, "max_depth": 2}))
    (prompt,) = _prompts(spec)
    (command,) = _tool_runs(prompt)
    # MUTATION: write `python -m swarm_qa discover` again and this fails.
    assert command.startswith(f"{agentimage.QA_TOOL} discover --base https://app.example.com ")


def test_no_brief_runs_a_bare_python() -> None:
    prompts = _prompts(qaspec.render(_target())) + _prompts(
        qaspec.render_discovery(_target(screens=None, discover={"max_pages": 30, "max_depth": 2})))
    for prompt in prompts:
        assert not re.search(r"(?<![/\w])python3? -m swarm_qa", prompt), prompt


def test_the_runner_recognises_the_briefs_it_checks() -> None:
    (prompt,) = _prompts(qaspec.render_discovery(
        _target(screens=None, discover={"max_pages": 30, "max_depth": 2})))
    assert agentimage.runs_qa_tool(prompt)
    assert all(agentimage.runs_qa_tool(p) for p in _prompts(qaspec.render(_target()))
               if _tool_runs(p))
    assert not agentimage.runs_qa_tool("Review the captures; run no tool.")
    assert not agentimage.runs_qa_tool(None)
