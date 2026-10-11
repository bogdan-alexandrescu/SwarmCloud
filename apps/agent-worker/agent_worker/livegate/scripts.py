"""The agent's page scripts, tracked so control can lift them (live browser L2).

docs/design/live-browser.md section 5.4.3: while a person is in control, a page
that loads must run no listener the agent installed. The agent installs those
two ways over CDP -- `Page.addScriptToEvaluateOnNewDocument` (runs in every new
document of that target) and `Runtime.addBinding` (a function the page can call
that reports to the agent) -- and both are per session. The proxy records each
one the agent makes, by session and identifier; this registry turns that record
into the commands that remove them all when control starts and re-add them on
return.

A restored script gets a NEW identifier from Chromium. The agent still holds
the old one, so the registry keeps old -> current and the proxy translates the
agent's later `Page.removeScriptToEvaluateOnNewDocument`; without that the
agent's remove would name a script Chromium no longer has, and the restored
one would outlive the agent's intent.

Nothing here talks to Chromium. Holding no I/O is what lets the proxy's tests
assert the exact commands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

ADD_SCRIPT = "Page.addScriptToEvaluateOnNewDocument"
REMOVE_SCRIPT = "Page.removeScriptToEvaluateOnNewDocument"
ADD_BINDING = "Runtime.addBinding"
REMOVE_BINDING = "Runtime.removeBinding"


@dataclass
class _Script:
    agent_identifier: str
    current_identifier: str
    params: dict[str, Any]


@dataclass
class _Session:
    scripts: list[_Script] = field(default_factory=list)
    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)


class ScriptRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def _session(self, session_id: str) -> _Session:
        return self._sessions.setdefault(session_id, _Session())

    def added_script(self, session_id: str, identifier: str, params: Mapping[str, Any]) -> None:
        """The agent's add succeeded and Chromium named it `identifier`."""
        self._session(session_id).scripts.append(_Script(identifier, identifier, dict(params)))

    def removed_script(self, session_id: str, agent_identifier: str) -> str:
        """The agent removes a script; returns the identifier Chromium knows it by."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return agent_identifier
        for script in entry.scripts:
            if script.agent_identifier == agent_identifier:
                entry.scripts.remove(script)
                return script.current_identifier
        return agent_identifier

    def added_binding(self, session_id: str, params: Mapping[str, Any]) -> None:
        name = params.get("name")
        if isinstance(name, str):
            self._session(session_id).bindings[name] = dict(params)

    def removed_binding(self, session_id: str, name: str) -> None:
        entry = self._sessions.get(session_id)
        if entry is not None:
            entry.bindings.pop(name, None)

    def forget_session(self, session_id: str) -> None:
        """The session detached: Chromium dropped its scripts with it."""
        self._sessions.pop(session_id, None)

    def removal_commands(self) -> list[tuple[str, str, dict[str, Any]]]:
        """(session, method, params) that lift every agent script and binding."""
        commands: list[tuple[str, str, dict[str, Any]]] = []
        for session_id, entry in self._sessions.items():
            for script in entry.scripts:
                commands.append((session_id, REMOVE_SCRIPT, {"identifier": script.current_identifier}))
            for name in entry.bindings:
                commands.append((session_id, REMOVE_BINDING, {"name": name}))
        return commands

    def restore_commands(self) -> list[tuple[str, str, dict[str, Any], str | None]]:
        """(session, method, params, agent identifier or None) that re-add them.

        The fourth element names the script whose new identifier the caller
        reports back through `restored_script`; it is None for a binding.
        """
        commands: list[tuple[str, str, dict[str, Any], str | None]] = []
        for session_id, entry in self._sessions.items():
            for script in entry.scripts:
                commands.append((session_id, ADD_SCRIPT, dict(script.params), script.agent_identifier))
            for params in entry.bindings.values():
                commands.append((session_id, ADD_BINDING, dict(params), None))
        return commands

    def restored_script(self, session_id: str, agent_identifier: str, new_identifier: str) -> None:
        entry = self._sessions.get(session_id)
        if entry is None:
            return
        for script in entry.scripts:
            if script.agent_identifier == agent_identifier:
                script.current_identifier = new_identifier

    def lost_script(self, session_id: str, agent_identifier: str) -> None:
        """A restore failed (the target navigated away or closed): stop tracking it."""
        self.removed_script(session_id, agent_identifier)
