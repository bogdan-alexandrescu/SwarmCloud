### Layouts: rows, the pane, or the band

How this session follows SwarmCloud work in Claude Code (SC-BROWSER-BUILD,
archived issue 2318; the owner picked proposal B, Two columns, on 2026-10-25, and added the band
on 2026-10-26, SC-BAND). Three layouts, and a toggle between them that is
remembered per project and switches without restarting the session:

* **`rows`** (the default) -- today's: /sc:swarmcloud shows each workflow in
  /workflows as the plugin's `rows` setting says, one `sc:summary` row per
  workflow (`summary`) or a row per unfinished step (`per-step`). Every row
  is a model agent that polls for as long as its work runs.
* **`pane`** -- the **SwarmCloud pane** beside the chat, opened with
  `/sc-pane` (or by `/sc layout pane`), plus ONE quiet `sc:finish` row per
  workflow in /workflows, labelled `SC ∴ <name>`. At session start the pane
  opens by itself, drawing "not read yet" with Refresh, after ONE
  `swarm_layout` call and no list read (the owner, 2026-10-25: "open it with no
  read"); the engine seats a pane nobody asked for only from 144 terminal
  columns, so on a narrower terminal it waits, a one-line toast says so, and
  `/sc-pane` opens it. The pane is drawn as B
  draws it: Level 1 this session's workflows -- and, after session start,
  this project's running work too (see "Session start") -- (state, stages
  done of total, duration, tokens) with Refresh; open one and its **stages** are on the
  left -- the console's own, `Stage N` being a step's dependency level plus
  one (`levelsOf`), with the console's page-head shape (`15 steps in 3 stages
  (1 → 13 → 1)`) -- and the selected stage's census on the right; open a
  stage and its **agents** (name, state, one line) replace the census; open
  an agent and its **details** (state, attempts, duration, cost, tokens, last
  error, PR, artifact names, console link) replace the agents. Refresh at
  each level. Every read is ONE direct `swarm_browse` call the pane makes to
  this plugin's server, so **browsing costs no model tokens**, and nothing is
  read on a timer or while the pane is closed. A read that fails draws
  "SwarmCloud unreachable" with the reason the bridge gave, keeps the last
  good read on screen marked as not current, and retries only on Retry. On a
  narrow terminal (about 60 columns) the pane shows one column at a time.
  The `sc:finish` row's only job is to notice the workflow's finish so this
  session learns of it: it writes a line only when the workflow's STAGE
  changes (`stage 2 · 1 failed · 3 running · 1 parked`, then `succeeded ·
  3/3 stages`) and holds each `swarm_follow` (`format: "stage"`) until it
  does, so a quiet stage costs one call per half hour, not one per step
  change. A single task keeps its own `sc:task` row, one per task.
* **`band`** -- the same levels in a **strip above the prompt**, with ONE
  summary line **below** the prompt, and **no /workflows row at all** (the owner,
  2026-10-26: in Claude Code's fullscreen layout a pane docks on the right,
  where a plugin cannot move it, and a /workflows row is a model agent). The
  strip's first line sums it up, `SwarmCloud · 6 running · 1 waits on you ·
  2 PRs`; its `[Fold]` leaves that line alone, the engine scrolls a taller
  tree, and it gives way while a survey is open. Under the prompt,
  `SC 6 running · 1 waits on you · 2 PRs`, cleared when nothing of this
  session or project runs. Every read is ONE direct `swarm_browse` call, no
  model tokens. While anything runs the strip re-reads the list once a
  minute (and stops when nothing runs, when the layout changes, and when the
  session ends), and when a workflow finishes, a step waits for a person, a
  merge parks on owner questions, or new owner questions appear, it queues
  ONE chat turn, `/sc status <wf_id> · <what changed>` (see `status`). In
  this layout `swarm_workflow_launch` answers an attach, `attach: "all"` and
  a dispatch's rows with `launches: []` and `followed_by: "band"`: launch
  nothing for them, and say the strip follows the work. A spec's launch is
  still launched -- that run submits the workflow -- and starts no follow
  row. At session start the strip opens with this session's and this
  project's running work, and the hook asks for no attach.

The toggle: **`/sc layout rows`**, **`/sc layout pane`** or **`/sc layout
band`** -- one
`swarm_layout {layout}` call, which saves the choice for this project under
the bridge's config directory (it outranks the plugin's `layout` option) --
or the pane's own `[Layout]` control. Bare `/sc layout` says which is set
(`swarm_layout {}`). A switch ALSO RE-ATTACHES (the owner, 2026-10-25: "Also
re-attach automatically"): once `/sc layout rows|pane` (or the pane's
`[Layout]` control) saves the choice, it queues `/sc attach --all`, one chat
turn, so rows in the new layout start beside the old ones, which keep running
until they end (a plugin cannot stop a running /workflows row). With function
hooks on, the pane answers `/sc layout` itself and the same run goes on as
`/sc attach --all`; otherwise make the `swarm_layout {layout}` call, say its
reply, then queue the re-attach by doing `attach --all` in the same turn.
Say both: the old rows keep going, and the attach starts the new ones; never
claim the rows changed at once. `/sc layout rows` returns to whatever the
plugin's `rows` setting says (`summary` or `per-step`). Bare `/sc layout`
saves nothing and attaches nothing. **`/sc layout band`** is the exception to
the re-attach: the band starts no rows, so it saves, opens the strip with
one read, and queues nothing; leaving `band` for `rows` or `pane` clears the
strip and the line under the prompt and re-attaches as above.

The pane and the band need Claude Code's **function hooks**, early access in
this build (`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1`). Where they are off neither
loads and `/sc-pane` does not exist -- so do not choose `band` there: it
starts no rows and nothing would follow the work; the `pane` layout's one row per workflow
still works, and `/sc status <wf_id>` reads a workflow on request.
