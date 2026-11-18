# Start here

Read this before you have a workspace. It takes you from nothing to your first agent, in order, and links the page that explains each step in depth.

Read in the console, the commands below carry your deployment's own values. A value shown as `<like-this>` is one this deployment does not publish: ask your platform team for it and type it in its place.

## 1. Before you start

You need three things, and no workspace yet:

1. **Claude Code**, installed on your machine.
2. **uv**, the Python tool runner (`uv --version` prints its version).
3. **A work account** on the domain your platform team allows.

The console you are reading is at:

```text
{{console}}
```

You should see this guide when you open that address and sign in with your work account.

## 2. Install the sc plugin

In Claude Code, type:

```text
/plugin marketplace add {{plugin-home}}
/plugin install sc@swarmcloud
```

When asked for the **deployment URL**, give the console's address above.

Then type `/reload-plugins` and `/mcp`.

You should see `plugin:sc:swarmcloud` as **connected**.

## 3. Sign in and check who you are

In a terminal:

```bash
uv tool run --from 'swarm-mcp @ git+https://github.com/{{plugin-home}}@sc-v{{plugin-version}}#subdirectory=apps/swarm-mcp' sc login
uv tool run --from 'swarm-mcp @ git+https://github.com/{{plugin-home}}@sc-v{{plugin-version}}#subdirectory=apps/swarm-mcp' sc whoami
```

A browser window opens at `sc login`: pick your work account.

You should see, from `sc whoami`, the deployment's address and your work account, and no workspace: that is expected until one is approved. More in [Sign in to the console](sign-in.md).

## 4. Request your workspace and finish the checklist

1. In Claude Code, type `/sc:setup`, or open [the setup checklist](/setup) in the console.
2. At **Request your workspace**, request it.
   You should see "Workspace requested — waiting for an admin to approve".
3. While the request waits, do the GitHub steps: [Connect GitHub and install the App](connect-github.md), then [Choose repositories and verify access](choose-repositories.md).
   You should see your repositories as granted on [Config › Repositories](/config/repositories).
4. When an admin approves, finish the rest of the checklist: [Add a Claude account](claude-account.md).
   You should see every required step of `/sc:setup status` done.

Depth: [Request your workspace](request-workspace.md).

## 5. Three demos

Run these inside a checkout of a repository you granted.

### One agent, in plain words

In Claude Code, type:

```text
Run this in SwarmCloud: add a unit test for the date parser in src/dates.ts
```

You should see the agent's id, for example `task_8f2c…`, with a live row.

### A two-step workflow

Save this as `hello.workflow.json`:

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

Put your own repository in `repository_url`, then type:

```text
/sc run hello.workflow.json
```

You should see one live row per step. What each line means: [the quick start](quick-start.md).

### A GitHub issue

```text
/sc run --issue example-org/example-app#42
```

Use your own repository and issue number.

You should see a planner start; approve its plan with `/sc plan approve` and the issue's work runs. More in [Start an agent or a workflow](submit-work.md).

## 6. Where to go next

* [Connect GitHub and install the App](connect-github.md)
* [Choose repositories and verify access](choose-repositories.md)
* [Add a Claude account](claude-account.md)
* [Allow plugins for your agents](plugins.md)
* [Set secrets and keys](secrets-and-keys.md)
* [Follow your work](follow-work.md)
* [Troubleshooting](troubleshooting.md)
