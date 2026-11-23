### `keys`

ONE call to `swarm_keys` (`{provider: "typesafe"}`), and show its `text`:
whether the developer's own key is set, whether their selected tenant's is,
and which one a task they submit there would use — their own key wins over
the tenant's. Status only: `set`, `version`, who set it and when. There is no
value to show and never a reason to ask for one. `available: false` on their
own key means they have no ready personal workspace to keep it in: say so,
and that an admin can set a tenant key instead. `consumed_by` names what
reads the key: `jev`, the jev-browser tools of a live-browser task, which send
page text to TypeSafe. When it is empty, do not describe a key as in use.

### `keys set <provider> [--tenant]`

**THE KEY NEVER PASSES THROUGH THIS SESSION** (owner, 2026-10-14): not in
chat, not as a tool argument, not in a command line. Never ask the developer
for it, and if they paste one anyway, do not repeat it, do not pass it to any
tool or command, and tell them to rotate it at TypeSafe, since it is now in
this transcript.

`<provider>` is `typesafe`, the only one; anything else: say so and stop.
Call `swarm_key_set_link` with `{provider: "typesafe", scope: "user"}` — or
`scope: "tenant"` with `--tenant`, which sets the selected tenant's key and
needs an admin. It answers only `url` and `expires_at`. Give the developer
the `url` and tell them to **open it and paste the key into the console's
form**; it works once, for ten minutes, and only in their own console
session. Then `/sc keys` shows the result. A `null` url comes with a `note`:
show it verbatim. An error is the API's: report it verbatim (a 404 on `user`
means no ready personal workspace; a 403 on `tenant` means not an admin).

If the console answers "keys can't be saved yet", the deployment cannot store
keys until its owner applies the KEYS-IAM bootstrap role; say exactly that,
and do not retry. In a terminal, the developer can instead run
`sc key set typesafe [--tenant]` themselves: it asks for the key at a hidden
prompt and prints only `stored: version N`. Tell them the command; never run
it — it is not granted to this skill, and it would put the key in front of
you.
