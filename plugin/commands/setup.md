---
description: Connect GitHub as yourself, enable your orgs and choose the repositories SwarmCloud may use — the onboarding checklist, resumed wherever it stands
argument-hint: "[status]"
allowed-tools:
  - mcp__swarmcloud__swarm_setup_status
  - mcp__swarmcloud__swarm_setup_connect
  - mcp__swarmcloud__swarm_setup_orgs
  - mcp__swarmcloud__swarm_setup_repos
  - mcp__swarmcloud__swarm_setup_grant
  - mcp__swarmcloud__swarm_setup_revoke
  - mcp__swarmcloud__swarm_setup_verify
  - mcp__swarmcloud__swarm_access
  - mcp__plugin_sc_swarmcloud__swarm_setup_status
  - mcp__plugin_sc_swarmcloud__swarm_setup_connect
  - mcp__plugin_sc_swarmcloud__swarm_setup_orgs
  - mcp__plugin_sc_swarmcloud__swarm_setup_repos
  - mcp__plugin_sc_swarmcloud__swarm_setup_grant
  - mcp__plugin_sc_swarmcloud__swarm_setup_revoke
  - mcp__plugin_sc_swarmcloud__swarm_setup_verify
  - mcp__plugin_sc_swarmcloud__swarm_access
---

Walk the person through SwarmCloud's onboarding checklist, the same one the
console draws (docs/onboarding.md §4.3): connect GitHub as themselves, enable
the GitHub owners SwarmCloud's App is installed on, choose repositories read
or write, verify. It is resumable: start from wherever `swarm_setup_status`
says it stands, and stop when it says ready.

If the arguments are `status`, call `swarm_setup_status`, show its
`checklist` exactly as returned, and stop.

Otherwise, in this order, skipping a step the checklist already shows done:

1. **Where it stands.** Call `swarm_setup_status` and show its `checklist`
   verbatim. A failed step's recovery copy is the API's, word for word: show
   it as it came, never paraphrased.
2. **Connect GitHub**, when `connected_as_you` is null. Say first that
   SwarmCloud will act as them through its GitHub App and that the token goes
   from GitHub to Secret Manager, never to this session. Call
   `swarm_setup_connect`. Show `authorize_url` once, in case the browser did
   not open, and say the link lasts 10 minutes and works once. Then call
   `swarm_setup_status` with `wait_for_github_seconds: 600` (or what is left
   of `expires_in_seconds`). If `connected_as_you` is still null, show
   `not_connected` as it came and stop: the person runs `/sc:setup` again for
   a fresh link.
3. **Owners.** Call `swarm_setup_orgs` and show each owner with its install
   state. Ask which installed owners to enable, then call `swarm_setup_orgs`
   with `enable` for each one they name. For an owner the App is not
   installed on, give them `install_url` and say that an org they do not own
   sends its owners a request, which one of them must approve. If no owner
   is enabled, say so and stop: setup resumes here next time.
4. **Repositories.** For each enabled owner they want, call
   `swarm_setup_repos` (pass `q` when they name part of a repository, and
   `next_page` to page on) and show the page. Ask which repositories, and
   read or write for each. `can_push` false means write will be refused:
   say so before they choose. Call `swarm_setup_grant` once per repository
   **they** named, in the mode **they** named. Never grant one they did not
   name, and never pick a mode for them.
5. **Verify.** Call `swarm_setup_verify` and show its `text`. For a failure,
   show its recovery copy and offer to re-check once they have fixed it (an
   SSO sign-in, an org owner's approval); re-check only when they say so.
6. **Done.** Call `swarm_setup_status` and show the `checklist`. When
   `complete` is true, say SwarmCloud can clone, push and open pull requests
   as them only in the repositories they chose, and that the Access page or
   `swarm_access` changes that. Otherwise say which step it paused at and
   that `/sc:setup` resumes there.

To remove a repository they chose, call `swarm_setup_revoke` with the one
they name.

Never ask for, accept or repeat a token, a code or a state, and never put the
authorize URL anywhere but the one line that shows it. If the person pastes a
token, tell them to revoke it at GitHub: it is now in this session's
transcript.

**Not run here.** Disabling an org (`uv run sc access remove-org <owner>`)
deletes every grant under it, and disconnecting
(`uv run sc access disconnect`) revokes SwarmCloud's access as them at
GitHub. Both are theirs to type, in a terminal, where each asks for a typed
confirmation; tell them the command and stop. The same wizard runs in a
terminal without Claude Code as `uv run sc setup`, and
`uv run sc setup status` prints the checklist.

If a tool answers that the person is not signed in, tell them to run
`uv run sc login` themselves (it opens a browser and waits for them) and stop.
