# Follow your work

| In the console | In Claude Code |
|---|---|
| [Work › Agents](/agents) | `/sc agents` |
| [Work › Workflows](/workflows) | `/sc workflows` |
| [Work › Timeline](/timeline) | `/sc check` |
| A workflow's page | `/sc status wf_…` |
| An agent's page | `/sc status task_…` |

## In the console

1. Open [Work › Agents](/agents). Each row is one agent with its state.
2. Press a row to open the agent: its steps, its logs and its result.
3. [Work › Workflows](/workflows) lists workflows; open one to see each step.

## In Claude Code

* `/sc` alone shows today's state of the platform.
* `/sc attach wf_…` shows live rows for a workflow again, for example after a restart.
* `/sc watch` runs `/sc check` every 30 minutes in this session. `/sc watch off` stops it.

## What the states mean

* **ready** or **parked**: waiting. It uses no capacity while it waits.
* **leased**, **starting**, **running**: it holds capacity and is working.
* **succeeded**, **failed**, **cancelled**: finished.

Waiting reasons and what to do: [Troubleshooting](troubleshooting.md#waiting).
