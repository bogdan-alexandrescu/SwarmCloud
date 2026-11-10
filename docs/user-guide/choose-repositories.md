# Choose repositories and verify access

Agents only reach the repositories you choose here.

## Choose repositories

1. Open [Config › GitHub access](/config/github) and the tab **Choose repositories**.
2. Find a repository with **Search**, for example `example-org/example-app`.
3. Set it to **Read** (agents can read it) or **Write** (agents can push a branch and open a pull request).
4. Not in the list? Type it in **Not listed? Type owner/repo** and press **Grant**.
   The checklist step **Choose repositories** shows "1 granted: …".

## Verify access

1. Open [Config › Repositories](/config/repositories), or press **Verify access** on the checklist.
2. In the card **Repositories SwarmCloud may reach as you**, press **Verify** on the repository, or **Re-check all**.
   SwarmCloud checks that it can clone the repository and open a pull request.
3. For a **Write** repository, press **Push test** to also check a push. It asks first.
4. The checklist shows **Verify access** done, then **Ready**.

## Register a repository for code search (optional)

1. On [Config › Repositories](/config/repositories), press **Register repository**.
2. Choose the repository and how often to re-index it, then **Next: review** and **Register example-org/example-app**.

## From Claude Code

`/sc:setup` lists your repositories, grants the ones you pick (read or write) and runs the check.

## Next

[Add secrets and keys](secrets-and-keys.md), or go straight to the [Quick start](quick-start.md).
