# Quick start for engineers

From nothing to a running workflow, from Claude Code, in about 15 minutes.

You need: Claude Code, `uv`, a work account, and the deployment's address (for example `https://swarm.example.com`) from your platform team.

## 1. Install the sc plugin (2 minutes)

In Claude Code, type:

```text
/plugin marketplace add example-org/swarmcloud
/plugin install sc@swarmcloud
```

`example-org/swarmcloud` stands for the repository your platform team gives you. When asked, enter the **deployment URL**. Ask your platform team for the **OAuth client ID** and **secret** if the deployment needs them.

Then type `/reload-plugins` and `/mcp`. You should see `plugin:sc:swarmcloud` as **connected**.

## 2. Sign in (1 minute)

In a terminal. If you have only the plugin (no SwarmCloud checkout), run `sc` from the same package the plugin uses; `<version>` is the plugin's version, shown by `/plugin`:

```bash
uv tool run --from 'swarm-mcp @ git+https://github.com/example-org/swarmcloud@sc-v<version>#subdirectory=apps/swarm-mcp' sc login
uv tool run --from 'swarm-mcp @ git+https://github.com/example-org/swarmcloud@sc-v<version>#subdirectory=apps/swarm-mcp' sc whoami
```

Inside a SwarmCloud checkout, the short form works:

```bash
uv run sc login     # a browser window opens: pick your work account
uv run sc whoami    # shows the deployment, you and your workspace
```

## 3. Set up your workspace (5-10 minutes)

In Claude Code, type `/sc:setup`. It walks the same checklist as the console:

1. Requests your workspace (an admin approves it).
2. Adds a Claude account, or asks for a loan.
3. Gives you a GitHub link; open it and approve.
4. Enables your organisations and grants the repositories you pick.
5. Verifies access.

Run `/sc:setup status` at any time to see where you are. It picks up where it stopped.

## 4. Start one agent (1 minute)

In Claude Code, inside a checkout of a granted repository, ask in plain words:

```text
Run this in SwarmCloud: add a unit test for the date parser in src/dates.ts
```

The plugin starts one agent and shows its id, for example `task_8f2c…`, with a live row.

## 5. Run one workflow (2 minutes)

Save this file as `hello.workflow.json` in your checkout:

```json
{
  "title": "Tidy the README",
  "repository_url": "https://github.com/example-org/example-app",
  "repository_ref": "main",
  "strategy": "integrate",
  "steps": [
    {"step_id": "plan", "runner_profile": "claude-code",
     "prompt": "Read README.md and list what is out of date."},
    {"step_id": "fix", "runner_profile": "claude-code", "depends_on": ["plan"],
     "prompt": "Fix what the plan step listed."}
  ]
}
```

What each line means:

* `repository_url` and `repository_ref`: the repository and branch the agents work on.
* `strategy`: `integrate` opens one pull request for all the steps; `collect` brings the change back to your checkout instead.
* `steps`: the agents, in order. `depends_on` makes `fix` wait for `plan`.
* `runner_profile`: which kind of agent runs the step, by name. `claude-code` is the usual one.

Then type:

```text
/sc run hello.workflow.json
```

You should see one live row per step.

## 6. Follow it

* `/sc status wf_…` shows one workflow (use the id you were given).
* `/sc check` lists what is stuck or stale in this project, with the next command.
* `/sc workflows` lists your running workflows.
* In the console: [Work › Workflows](/workflows) and [Work › Agents](/agents).

## 7. Answer a question

A step may stop and ask you something.

* `/sc inputs` lists the steps waiting for you.
* `/sc answer wf_… fix` opens a form in Claude Code; you type the answer.
* In the console, the question shows on [Home](/) under **Needs you**.

## 8. Find the pull request

With `integrate`, one pull request opens on `example-org/example-app` for the whole workflow. The reply to `/sc run` names the step that opens it, and the link shows in `/sc status` and on the workflow's page in the console.

A single agent started with `collect` opens no pull request: ask Claude Code to apply the agent's change, and it lands in your checkout.

## Next

* [Everyday work](index.md#everyday-work)
* Stuck? [Troubleshooting](troubleshooting.md)
