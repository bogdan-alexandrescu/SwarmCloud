# Set workspace defaults

These settings apply to every agent in your workspace. Each is optional.

## Plugins your agents may use

1. Open [Config › Plugins](/config/plugins).
2. Turn **Allowed here** on for each plugin your agents may load.
3. Press **Save and sign**. You should see "Saved and signed".

## MCP servers

1. Open [Config › MCP servers](/config/mcp-servers).
2. Press **Register an MCP server**.
3. Fill in **Name** (for example `kb`), **URL** (for example `https://mcp.example.com`) and **Description**.
4. Choose how it signs in: no credential, a per-run credential, or a workspace secret.
5. List the **Allowed tools** and save.

## Runner sizes

The size of the machine an agent runs on is part of its **runner profile**, which you pick by name when you start work.

* See the profiles and how busy each is on [Capacity › By runner profile](/capacity/pools/profiles).
* Platform admins change sizes on [Admin › Runner sizes](/admin/runner-sizes).

## Next

You are set up. Start with the [Quick start](quick-start.md) or [Start an agent](submit-work.md).
