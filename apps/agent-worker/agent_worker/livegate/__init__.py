"""The live gate: the `display` native sidecar's process (docs/design/live-browser.md §3.1).

`python -m agent_worker.livegate` (`__main__.py`) is the sidecar's command,
`scheduler.dispatch.DISPLAY_COMMAND`. The modules beside it are its
components; `__main__` composes them.
"""
