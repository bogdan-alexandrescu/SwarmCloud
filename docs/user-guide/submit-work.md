# Start an agent or a workflow

| In the console | In Claude Code |
|---|---|
| [Submit › Start an agent](/submit/task) | Ask: "Run this in SwarmCloud: …" |
| [Submit › Submit a workflow](/submit/workflow) | `/sc run my.workflow.json` |
| [Submit › Submit from a GitHub issue](/submit/issue) | `/sc run --issue example-org/example-app#42` |

## In the console

1. Open [Submit](/submit) and press **Start an agent**.
2. **Choose a runner**: pick a runner profile, for example `claude-code`.
3. **Say what it should do**: describe the change in plain words. Add the **Repository url**, for example `https://github.com/example-org/example-app`.
4. **Choose what happens to the result**: keep it to collect, or open a pull request.
5. Press **Start one agent**.
   You should land on the agent's page, with its state moving from waiting to running.

## In Claude Code

1. Open a checkout of a granted repository.
2. Ask in plain words, for example "Run these three fixes in SwarmCloud in parallel".
3. Claude Code says what it will start, starts it and shows a live row for each agent.

## From a GitHub issue

1. `/sc run --issue example-org/example-app#42` plans the issue first.
2. `/sc plan show <run>` shows the plan; `/sc plan approve <run>` starts it.
3. `/sc runs` lists your issue runs.
