"""The agent image's Python, named once for the brief and the runner.

A visual-check brief (`swarm_api.qaspec`) tells its agent to run the capture
or discovery tool, `swarm_qa`, which the browser image installs into its venv
(images/agent-runtime-browser/Dockerfile). The claude-code runner puts
`VENV_BIN` first on the CLI's PATH (`agent_worker.runners.claude_code`), but
an agent's own shell does not always keep it: on 3 November 2026 a production
capture leg's bare `python -m swarm_qa capture` resolved
/usr/local/bin/python and got "No module named swarm_qa", while another leg on
the same profile found /opt/venv/bin/python3 first. So the brief names the
venv's interpreter by its full path, and the runner checks, before it starts
the agent, that the same interpreter imports the module.

WHY HERE. swarm-api and the agent worker are installed in different images,
neither imports the other, and `apps/common/swarm_common/` is frozen; this
package is the one both images already build (images/swarm-api/Dockerfile,
images/agent-runtime-base/Dockerfile). One constant, so the brief's path and
the runner's PATH cannot drift apart. Pure: no I/O.
"""

from __future__ import annotations

import re

#: The image's venv, first on the agent's PATH on the browser image.
VENV_BIN = "/opt/venv/bin"

#: The venv's interpreter: the one with Playwright, the live-browser helper
#: and `swarm_qa`.
VENV_PYTHON = f"{VENV_BIN}/python"

#: The capture and discovery tool's module (images/agent-runtime-browser/swarm_qa.py).
QA_TOOL_MODULE = "swarm_qa"

#: How a brief runs the tool: `{QA_TOOL} capture ...`, `{QA_TOOL} discover ...`.
QA_TOOL = f"{VENV_PYTHON} -m {QA_TOOL_MODULE}"

#: A brief that runs the tool, in whatever form (a bare `python`, `python3` or
#: the full path), names one of these verbs after the module.
_RUNS_QA_TOOL = re.compile(rf"-m\s+{QA_TOOL_MODULE}\s+(capture|discover)\b")


def runs_qa_tool(prompt: object) -> bool:
    """Whether a brief tells its agent to run the capture or discovery tool."""
    return isinstance(prompt, str) and _RUNS_QA_TOOL.search(prompt) is not None
