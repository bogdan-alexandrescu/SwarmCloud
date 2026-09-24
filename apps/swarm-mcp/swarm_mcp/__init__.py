"""Drive SwarmCloud from a laptop the way you drive a local agent.

The platform could always run an agent. What it could not do was let you WATCH
one: work was dispatched over HTTP, its logs appeared in a bucket when it was
over, and the code it wrote sat in a checkpoint nobody read. Dispatching to
SwarmCloud therefore felt nothing like running an agent locally, even though
the agent was the same agent.

Three things close that gap, and they are the three modules here:

    client    the API, with the same identity `scripts/lib/common.sh` uses
    patches   the code an agent wrote, back into a working tree
    cli       dispatch / tail / apply / integrate, for a human or for Bash

`server` exposes the same operations over MCP so they sit beside a local
subagent in a Claude Code session. It is a WRAPPER over `cli`'s primitives and
holds no logic of its own -- two implementations of "what does integrate mean"
is how the CLI and the tool quietly start disagreeing.
"""
