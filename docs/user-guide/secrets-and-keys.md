# Set secrets and keys

Agents sometimes need a key: an API key for a service, a sign-in for a test site, or the TypeSafe key. You type the value into the console yourself; nobody else sees it, and it is never shown again.

## Add a secret

1. Open [Config › Secrets](/config/secrets).
2. Pick the scope: **Yours** (only your agents) or **Repository** (agents working in one repository).
3. Press **Add a secret**.
4. Fill in **Name** (for example `example-service-key`), **Provider**, **Kind** and **Value**.
5. Press **Add**.
   The secret shows in the list by name only. Use **Rotate** to change its value or **Delete** to remove it.

## Set the TypeSafe key

1. On [Config › Secrets](/config/secrets), press **Set TypeSafe key**.
2. Paste the key and save.
   Browser agents and Jev read it from here.

## From Claude Code

* `/sc keys` says whether a TypeSafe key is set for you and for your workspace.
* `/sc keys set typesafe` gives you a one-time console link where you paste the key yourself.
* `/sc secrets list` lists secret names, never values.
* `/sc secrets link personal example-service-key` gives you a one-time link to type that secret's value.

The key never passes through your Claude Code session.

## Next

[Invite people and set roles](people-and-roles.md).
