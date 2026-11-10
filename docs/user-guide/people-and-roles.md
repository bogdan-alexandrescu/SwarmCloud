# Invite people and set roles

You need to be an admin of the workspace for this page.

Roles: **viewer** (reads), **member** (starts and answers work), **admin** (also changes settings and people).

## Give a person a role

1. Open [Admin › Workspaces](/admin/workspaces) and pick your workspace.
2. Open the tab **People**.
3. In **Give a person their own role**, type their **Email** (for example `dev@example.com`) and pick the role.
4. Add a **Reason (optional, shown in the events)** and press **Set role**.
   The person shows under **Roles in this workspace**.

## Set the default role

1. On the same tab, pick a **New default role**.
2. Press **Set default**. Everyone without their own role gets it.

## Share a join link

1. On the tab **People**, in **Join link**, press **Turn the link on**.
2. Press **Copy the join link**. It is shown once.
3. Send it to your team. Requests to join arrive on [Admin › Platform people](/admin/people) under **Waiting for you**; press **Approve** or **Deny**.
4. **Make a new link** stops the old one working. **Turn the link off** stops all of them.

## From Claude Code

* `/sc roles` shows the roles of your workspace and your own role.
* `/sc:team add team@example.com` adds a whole team as a workspace, from a Google group. It needs the SwarmCloud installer on your machine and an admin of the deployment; it runs a test agent at the end.

## Next

[Set workspace defaults](workspace-defaults.md).
