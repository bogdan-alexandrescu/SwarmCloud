# Allow plugins for your agents

Two different things are called plugins here.

## Plugins your agents may load (console)

1. Open [Config › Plugins](/config/plugins).
2. Turn **Allowed here** on for each plugin your agents may use.
3. Press **Save and sign**. You should see "Saved and signed".
4. **Discard** undoes changes you have not saved.

## The sc plugin in your own Claude Code

* Install it: see the [Quick start](quick-start.md).
* Change its settings: `/plugin configure sc@swarmcloud`.
* Check this machine can reach the deployment: `/sc:doctor`.
* Choose how live rows look: `/sc layout rows`, `/sc layout pane` or `/sc layout band`.
