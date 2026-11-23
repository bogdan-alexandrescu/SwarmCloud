### `secrets list [scope] [--repo owner/repo]`

ONE call to `swarm_secrets` with `{scope}` (`workspace` when none is given;
`personal`; or `repository` with `repository: "owner/repo"` from `--repo`,
which that scope needs and no other takes), and show its `text`: each row's
section, name, status, who set it and when, and what it is granted to.
Status only — there is no value to show and never a reason to ask for one.
`shadowed_by` on a row means another scope's secret of the same name wins
where it says (the person's, then the repository's, then the workspace's);
say where. A row with a `page` is managed on that console page, not here.
Platform secrets are not in any scope: they are Admin's, and
`swarm_platform_secrets` reads them for a platform admin only — anyone else
gets the API's 403, which is the answer; report it verbatim.

### `secrets link <scope> [name] [--repo owner/repo] [--actions [--env name]]`

**A SECRET'S VALUE NEVER PASSES THROUGH THIS SESSION** (the owner, Q8 of
docs/design/secrets-scopes.md §11): not in chat, not as a tool argument, not
in a command line. Never ask the developer for it, and if they paste one
anyway, do not repeat it, do not pass it to any tool or command, and tell them
to rotate it where it came from, since it is now in this transcript.

* Without `--actions`: call `swarm_secret_set_link` with `{scope, action}` —
  `action: "rotate"` with `name` when a name is given (a secret
  `/sc secrets list` shows), else `action: "add"` and no name: the form asks
  for it. `scope` is `workspace`, `personal` (the developer's own personal
  workspace; tell them to open the link with that workspace selected) or
  `repository`, which needs `--repo` as `repository`.
* With `--actions`: `scope` must be `repository`, and `name` and `--repo` are
  required. Call `swarm_actions_secret_link` with `{repository, name}`, plus
  `environment` from `--env`. The form sends the value to GitHub, sealed;
  SwarmCloud keeps only the name, who set it and when.

Either answers only `url` and `expires_at`. Give the developer the `url` and
tell them to **open it and type the value into the console's form**; it works
once, for ten minutes, and only in their own console session. A `null` url
comes with a `note`: show it verbatim. An error is the API's: report it
verbatim (403 means they may not set that scope: an admin of the workspace,
or for a repository a person GitHub calls its admin; 404 means the
repository is not registered in this workspace).
