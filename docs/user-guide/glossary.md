# Glossary

**Agent**: one coding agent working on one change, in its own copy of a repository.

**Workflow**: several agents in order. Each one is a **step**; a step can wait for another (`depends_on`).

**Workspace**: your own space to run agents in, with its own identity, storage, secrets and people. Also called a tenant.

**Console**: the SwarmCloud web app.

**sc plugin**: the Claude Code plugin that starts and follows SwarmCloud work from your terminal. Its commands start with `/sc`.

**Runner profile**: the kind of agent and the machine it runs on, picked by name, for example `claude-code`.

**Claude account**: the Claude subscription your agents run on. Yours, or one an admin lends you from the pool.

**GitHub App**: how SwarmCloud reaches GitHub as you. You connect it, then install it on an account or organisation.

**Grant**: a repository you allowed agents to use, with **Read** or **Write** access.

**Collect**: the result comes back as a change you apply in your checkout. No pull request.

**Integrate**: a workflow's steps end in one pull request.

**Parked**: waiting for something outside, such as a quota reset or an answer. Uses no capacity.

**Ready**: waiting for room to run. Uses no capacity.

**Schedule**: work that starts again on a cadence.

**Approval**: a person's yes before a scheduled run goes ahead.

**Role**: what you may do in a workspace: viewer, member or admin.

**TypeSafe key**: a key that browser agents and Jev read from Config › Secrets.

**Voice it**: talking to SwarmCloud instead of typing (platform admins only, today).
