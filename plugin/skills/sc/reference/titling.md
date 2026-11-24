### Every run is titled after its workflow

A SwarmCloud workflow run here is titled `SC ∴ <name> · N steps` (at most
150 characters), not `swarmcloud`: <name> is the spec's `title`, else its
`label` cut at a word, else the title or label SwarmCloud stored, else the
workflow id, cut to fit, never the step count. N is the number of steps
SwarmCloud runs, read from the served workflow; a spec launched before it is
submitted is titled `SC ∴ <name>`, with no count, because SwarmCloud may
still append steps. A single task's run is titled `SC · <label>`: every
single task has a run of its own. The mark right after `SC` is the run's
KIND, seen even where Claude Code's panel cuts a title at about 28
characters: ONE dot for one task, THREE dots (`∴`, a graph of three nodes)
for a workflow.
NO NAME CARRIES A WORKSPACE -- not the run's, not a row's (owner,
2026-10-14: "workflows in here should not have the [Engineering] or
[eng@example.com] prefix in front of the names"). The run's DESCRIPTION starts
with its DETAILS, one line, six fields in this order:
`Workspace: <workspace> (<tenant>) · Started by: <submitted_by> · Profiles: <runner profiles> · LLM: <pool account> (<provider>), model <model> · Browser tool: <browser_tool> · Session: <claude_session>`
-- <workspace> is the workspace (tenant) the API's document says it runs in,
by its display name when the API gives one, and <tenant> its tenant id
(`Workspace: <tenant> · ` alone when there is no display name); `Started by`
is the `submitted_by` the API stores (an email, or the principal a key acts
as); `LLM` is the pool account each step's attempt runs on, by the label its
owner gave it, and the model; `Browser tool` is a `claude-code-browser`
step's tool (`auto` when it named none, `not used` when no step runs that
profile); `Session` is the Claude Code session tag, `this session (<id>)`
when it is this one. Each field says `not reported` when the API serves
none -- `Workspace: not reported` included -- and the workspace stays first,
so clipping a long description never drops it. Nothing credential-shaped is
ever shown: a value that looks like a key is `withheld`. A run started
before an account was assigned says `LLM: not assigned yet`; each of its
rows then says `LLM: <account>` once, on its first progress line after the
assignment. Every row INSIDE the run is `[SwarmCloud] <name> · stage <n> ·
<step>` (a single task: `[SwarmCloud] <label> · task`) with its console
link, and shows ITS OWN details line when expanded (the last line of its
prompt, `details: ...`); every result object and every listing line names
the workspace. A run's name is fixed when it is launched, so `run` and `attach`
first call `swarm_workflow_launch`, which writes a copy of the
`/sc:swarmcloud` script with that name and returns its `script_path`, and
then launch THAT copy with the Workflow tool, `{scriptPath: <script_path>,
args: <the reply's `args`, as given>}` -- an attach's are its `launches`
entries' own, named there once -- `spec_handle` included: the session
never builds those args itself, and never adds the spec to them -- a spec's
`args` carry its handle, not the spec, so they stay small however large the
spec is (archived issue 2875). If `swarm_workflow_launch` answers an error, say so in one
line and run the `/sc:swarmcloud` workflow by name with `{attach: <wf_id>}`
for an attach, or for a run the spec file's path (the bridge reads it and
answers an outline) or, with no file, `{spec: <the spec object>}` (no handle
exists then, so the spec goes by value and the digest check still refuses
one that changed): the rows are the same, only the run's title is the
generic one.
