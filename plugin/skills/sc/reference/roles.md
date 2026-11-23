### `roles`

ONE call to `swarm_roles`, and show its `text`: the workspace this session
acts in, your own role there with its `source` (`override`, `default`,
`personal`, `platform_admin`) and `via`, the workspace's default role, every
person with an override, what each role permits, and the recent role and
platform-admin events. `swarm_tenants` carries the same `role` and `via` for
every workspace you may choose. `via: platform_admin` means you are acting as a
platform admin in a group workspace you are not in: say that every action
there is recorded in its audit log.

A `viewer` sees everything and changes nothing; a `member` submits, approves,
answers and cancels; an `admin` also manages secrets, MCP servers, provider
keys, the workspace's settings and its roles. A refusal such as
`role_member_required` is that rule, not a fault: report it verbatim.

**NO TOOL CHANGES A ROLE, AND YOU NEVER DO** (owner answer Q11). If the
developer wants one changed, tell them the command to run in their own
terminal — `sc roles set <email> <role>`, `sc roles clear <email>` (back to
the default) or `sc roles default <viewer|member>` — which asks them to type
the email and the role back at the terminal and sends nothing otherwise; or
the console's Config › People and roles. It needs `admin` in that workspace
(`can_manage`). Never run it: it is not granted to this skill, and a
confirmation it could read from you would not be the developer's.
