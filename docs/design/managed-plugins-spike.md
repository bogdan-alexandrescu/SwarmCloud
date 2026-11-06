# Managed plugins: the `--plugin-dir` loading spike

Lane 2 of [managed-plugins.md](managed-plugins.md) §17, measured on 6 November 2026. It does not merge into the
product path. It answers the questions the design left to measurement, using the Claude Code CLI the image pins
(2.1.283, `ARG CLAUDE_CODE_VERSION` in `images/agent-runtime-base/Dockerfile`). Each answer gives the evidence and
what lane 8 (worker install, load and launcher) must do because of it.

## How it was measured

`images/agent-runtime-base/spikes/plugin-dir/run-spike.sh` runs the whole spike again and prints the report the
quotes below come from. It needs node, git, network to github.com and `claude` at the pinned version. It refuses any
other version. It writes everything under `$TMPDIR/plugin-dir-spike` (`$WORK` below), never into the repository.

* **The CLI is real and the model is not.** `mock-api.mjs` is a scripted Messages API on 127.0.0.1. The CLI runs with
  `ANTHROPIC_BASE_URL` pointed at it, so the spike needs no model credentials and every run makes the same tool
  calls (Read, Edit, the probe's MCP tool, Skill, Agent). The mock logs every request body, which is exactly what
  the model would have seen. That log is how the spike can tell what reached the model and what did not.
* **No keychain, no `~/.claude`.** Every case runs under `env -i` with `HOME` set to an empty folder.
* **The design's wrapping, done for real.** `wrap-plugin.mjs` makes the intake's one rewrite (§4.4): every hook
  command and MCP server command becomes `<launcher> <install_id> -- <the original>`. `plugin-exec-standin.mjs`
  stands in for `swarm-plugin-exec`. It sets one plugin's variables and secrets from
  `<env-dir>/<install_id>.json`, drops `ANTHROPIC_API_KEY`, passes stdin through and scrubs stdout and stderr. It
  also records each run: the event, the tool, the argv as the CLI expanded it, the exit code and the time taken.
* **A probe plugin with one of each component** (`spikes/plugin-dir/probe/`): two skills, a command, an agent, a stdio MCP
  server, hooks on SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStop, Stop and SessionEnd. It
  also declares two required `userConfig` options, `region` and the sensitive `api_token`. Each component leaves a
  marker (`PROBE-…`) so the report can tell where its output went.
* **jev-belay and jev-enforce, wrapped.** The script clones `valentynkit/jev-belay` at `v0.2.0` and checks the commit
  is `ef719db7eaadc56aa4def86c4da4ffff5bcbca35`. It clones `erkamyaman/jev-enforce` at `v1.1.1` and checks
  `ed9c7d79bad2bfdea474df5e414b8b601c0e92cd`; `v1.1.1` is the newest tag on that remote, as `git ls-remote --tags`
  showed on 6 November 2026. BGD's own fork of jev-enforce is on no remote, so the spike could not run it. The
  TypeSafe key is fake, built at run time (`spike-typesafe-` plus 16 random bytes as hex), and only the launcher
  ever hands it to a plugin. `case-config.mjs` writes the launcher's env files and each case's `--settings` from
  the fake values' files, so no file in the repository holds one.

The seven cases, each one `claude` command (from the report's "Cases"; `$WORK` is the work dir):

```
probe-settings:       claude -p /probe:probe-cmd hello.txt --output-format stream-json --verbose --permission-mode bypassPermissions --settings $WORK/cases/probe-settings/settings.json --plugin-dir $WORK/plugins/probe
probe-no-options:     claude -p Ping the probe. ... --plugin-dir $WORK/plugins/probe
probe-option-env:     claude -p Ping the probe. ... --settings <env block with CLAUDE_PLUGIN_OPTION_REGION/_API_TOKEN> --plugin-dir $WORK/plugins/probe
skill-deny:           claude -p Use the probe skill. ... --settings <deny Skill(probe:probe-skill) + options> --plugin-dir $WORK/plugins/probe
shell-form-option:    claude -p Say hi. ... --settings <options> --plugin-dir $WORK/plugins/probe-shellform
placeholder-launcher: claude -p Ping the probe. ... --settings <region + a placeholder api_token> --plugin-dir $WORK/plugins/probe
jev:                  claude -p Edit hello.txt so it says hello, plugins. ... --plugin-dir $WORK/plugins/jev-belay --plugin-dir $WORK/plugins/jev-enforce
```

All seven exited 0.

## 1. Does headless `--plugin-dir` load hooks, MCP servers, commands and agents, not only skills?

**Yes: every component loads.** Each one was also used in the run, not just listed.

The `init` event of `probe-settings`, and what the model saw:

```
plugins: [{"name":"probe","path":"$WORK/plugins/probe","source":"probe@inline","version":"0.0.1"}, ...]
mcp_servers: [{"name":"plugin:probe:probe","status":"connected","source":"plugin"}]
mcp_tools: ["mcp__plugin_probe_probe__probe_ping"]
agents: ["probe:probe-agent"]
skills: ["probe:probe-other","probe:probe-skill"]
commands: ["probe:probe-cmd","probe:probe-other","probe:probe-skill"]
command_body_in_first_prompt: <command-name>/probe:probe-cmd</command-name> ... "PROBE-COMMAND-BODY. Read hello.txt, ..."
mcp_tool_result: {"tool_use_id":"toolu_spike000005","type":"tool_result","content":[{"type":"text","text":"PROBE-MCP-PONG"}]}
skill_body: "Base directory for this skill: $WORK/plugins/probe/skills/probe-skill\n\nPROBE-SKILL-BODY. ..."
agent_body_as_subagent_system_prompt: request 6, in "system"
hook_events_fired: ["PostToolUse","PreToolUse","SessionEnd","SessionStart","Stop","SubagentStop","UserPromptSubmit"]
```

Three details lane 8 must design around:

* **Names come from `plugin.json`, not from the folder.** The `probe-shellform` folder loaded as `probe@inline`.
  Components are namespaced the same way: `probe:probe-skill`, `mcp__plugin_probe_probe__probe_ping`.
* **Skills are also listed as slash commands.**
* **The probe agent ran in the background.** It started with `"is_backgrounded": true`, and its result arrived as a
  `task_notification` and a second turn. That is because the probe's `env -i` dropped
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`. The runner sets that variable, so this is not a plugin behaviour.

**Lane 8 must:**

1. Key everything per plugin by the manifest's `name`, as the CLI shows it in `<name>@inline`.
2. Refuse a step whose signed set holds two plugins with the same manifest name. This spike did not measure what
   the CLI does then.
3. Expect a plugin's skills to appear as slash commands too.

## 2. Does it take `userConfig` values with no keychain (environment or settings file)?

**From a settings file, yes, sensitive values included. From the environment alone, no.** Values reach the CLI
through `pluginConfigs["<name>@inline"].options` in a `--settings` file. The CLI's own strings say it reads that key
"in user, --settings or managed settings (project settings are not read)". Sensitive and non-sensitive values both
work from there, with no keychain. The CLI then wrote no value under HOME: `fake_token_found_in` lists no
`HOME/...` file in any case.

| Case | What the run was given | MCP server | Hook env `CLAUDE_PLUGIN_OPTION_*` | Exec-form hook naming `${user_config.*}` |
|---|---|---|---|---|
| `probe-settings` | both options in `--settings` `pluginConfigs` | connected; `PROBE_REGION` = the region, `PROBE_TOKEN` = the secret | both, the right values | ran; argv `["exec-form","the configured region","the secret"]` |
| `probe-no-options` | nothing | **not started** (`mcp_servers: []`, no error event) | none | **failed**: `SessionStart exit 1: Failed to run: Plugin option "region" isn't set. Open /plugin manage to configure it, ...` |
| `probe-option-env` | `CLAUDE_PLUGIN_OPTION_REGION/_API_TOKEN` in the CLI's environment | **not started** | both (inherited) | **failed**, same message |
| `placeholder-launcher` | `region` and a placeholder `api_token` in `pluginConfigs`; the launcher sets the real `CLAUDE_PLUGIN_OPTION_API_TOKEN` and `PROBE_TOKEN` | connected; `PROBE_TOKEN` = **the secret** | `API_TOKEN` = **the secret** | ran; argv carries **the placeholder** (`"another value"`) |

What else the cases show:

* **A missing required option never stops the plugin from loading.** Its other hooks, skills, commands and agents
  all ran. What the CLI drops, without a word, is each MCP server that names a missing option. Each hook that names
  one fails at run time with exit 1.
* **The environment does not stand in for `pluginConfigs` in `${user_config.*}` substitution.** In
  `probe-option-env` the variables reached the hooks, but the server still did not start.
* **Exec-form arguments get the substituted value.** In `probe-settings` the fake token sat in the hook's argv, in
  the process table. The launcher's record caught it: `fake_token_found_in: ["launcher record runs/probe.jsonl"]`.
* **A shell-form command naming `${user_config.*}` is refused, hook by hook** (`shell-form-option`):
  `Failed to run: Hook from plugin probe@inline references ${user_config.*} in a shell-form command. The substituted
  value would be re-parsed by the shell. Use exec form instead — ... — or read $CLAUDE_PLUGIN_OPTION_<KEY> from the
  hook's environment.`

**Lane 8 must** (and §4.4's "if so" branch holds, measured):

1. **Write `pluginConfigs["<name>@inline"].options` into the runner's `--settings` file for every managed plugin.**
   Each non-secret value goes in as itself. Each sensitive field gets a fixed placeholder. Without an entry for each
   required field, the plugin's MCP servers vanish without any error.
2. **Supply every real secret through the launcher only.** Measured: the launcher's
   `CLAUDE_PLUGIN_OPTION_<KEY>` overrides the placeholder for a hook, and it overrides the server's own `env`
   variable for an MCP server. So the launcher's env file must name both: the option variable
   (`CLAUDE_PLUGIN_OPTION_API_TOKEN`) and every server `env` entry whose value is `${user_config.<sensitive>}`
   (here `PROBE_TOKEN`). Lane 4 (the intake) records that mapping when it parses `.mcp.json`.
3. **Never let a sensitive value travel in argv.** The CLI puts `${user_config.<sensitive>}` from an exec-form `args`
   list into argv, where every process can read it. The launcher only ever sees the placeholder there, and it cannot
   fix that without putting the secret on its own command line. Lane 4 should refuse, or route through the overlay, a
   hook or server whose `args` name a sensitive option. That is a question for the owner (questions.json, Q1).
4. **Name no secret in the `--settings` file.** The file is on disk for the whole attempt, and every hook of the
   plugin sees its values as `CLAUDE_PLUGIN_OPTION_*`.

## 3. Does `${CLAUDE_PLUGIN_ROOT}` expand inside a wrapped hook command?

**Yes.** The launcher, as the first word of the command, received the expanded path in every run. For a shell-form
command the expansion is the shell's work, from the environment, not a text substitution by the CLI: a
single-quoted `'${CLAUDE_PLUGIN_ROOT}'` reached the program literally. For exec form (`command` + `args`) and for MCP
server `args`, the CLI substitutes the value itself.

```
wrapped:  node "${CLAUDE_PLUGIN_ROOT}/dist/src/cli.js" hook stop     (jev-enforce, shell form)
launcher argv: ["node","$WORK/plugins/jev-enforce/dist/src/cli.js","hook","stop"]
launcher argv: ["node","$WORK/plugins/jev-belay/belay.mjs"]
launcher argv: ["node","$WORK/plugins/probe/hooks/probe-hook.mjs","${CLAUDE_PLUGIN_ROOT}"]   (the single-quoted probe)
launcher argv: ["node","$WORK/plugins/probe/hooks/probe-hook.mjs","exec-form","spike-region","<FAKE_TOKEN>"]   (exec form)
plugin_root (the launcher's environment): $WORK/plugins/probe
```

`CLAUDE_PLUGIN_DATA` defaults to a folder under HOME:

```
plugin_data_from_cli:              ["$WORK/cases/probe-settings/home/.claude/plugins/data/probe-inline"]
plugin_data_with_launcher_override: ["$WORK/private/plugin-data/probe"]
```

**Lane 8 must:**

1. Rewrite a shell-form hook as `/opt/swarm/bin/swarm-plugin-exec <install_id> -- <the original text>`, exactly as
   `wrap-plugin.mjs` does. The shell still expands `${CLAUDE_PLUGIN_ROOT}` and still splits words.
2. Give an exec-form hook and an MCP server the launcher as `command`, with the original command and `args` after
   `<install_id> --`.
3. Have the launcher set `CLAUDE_PLUGIN_DATA` to `private/plugin-data/<install_id>/`. The CLI's default is HOME's
   `.claude/plugins/data/<name>-inline`, and in the worker HOME is checkpointed. Measured: the launcher's value is
   the one the hook sees. jev-enforce keeps its pending edits there in `JEV_ENFORCE_MODE=end`. The CLI still creates
   the empty default folder.
4. Not use `${CLAUDE_PLUGIN_DATA}` as text inside a skill or command. The override only reaches processes.

## 4. How do a plugin hook's exit codes and output reach the agent?

Measured per channel with the probe. "Model" means the text appeared in a later request body. "Stream" means it
appeared in the CLI's `stream-json` output, which is the worker's transcript.

| Hook, channel | Model | Stream |
|---|---|---|
| SessionStart, stdout, exit 0 | yes: `SessionStart:startup hook success: PROBE-SESSIONSTART-STDOUT` | yes: `hook_response` with stdout, stderr, exit_code |
| UserPromptSubmit, stdout, exit 0 | yes: `UserPromptSubmit hook success: PROBE-USERPROMPT-STDOUT` | no |
| PreToolUse(Read), stdout, exit 0 | no | no |
| PostToolUse(Read), stderr, exit 1 | no | no |
| PostToolUse(Edit), stderr, exit 2 | yes: `PostToolUse:Edit hook blocking error from command: "'<launcher>' probe -- node \"$WORK/plugins/probe/hooks/probe-hook.mjs\"": [...]: PROBE-POSTEDIT-EXIT2-STDERR\n\nThis hook comes from the probe@inline plugin.` | no |
| PostToolUse(mcp), JSON `additionalContext`, exit 0 | yes: `PostToolUse:mcp__plugin_probe_probe__probe_ping hook additional context: PROBE-POSTMCP-JSON-CONTEXT` | no |
| Stop, stderr, exit 2 (the first stop) | yes, as a new user turn: `Stop hook feedback:\n['<launcher>' probe -- node "$WORK/plugins/probe/hooks/probe-hook.mjs"]: PROBE-STOP-EXIT2-STDERR\n\nThis hook comes from the probe@inline plugin.` The agent then took another turn. | no |
| Stop, JSON `systemMessage`, exit 0 (the next stop, `stop_hook_active`) | no | yes: `{"subtype":"informational","content":"Stop says: PROBE-STOP-SYSTEMMESSAGE"}` |

```
hook reporting: {"hook_response_events_in_stream":{"SessionStart":2},
                 "launcher_records":{"SessionStart":2,"UserPromptSubmit":2,"PreToolUse":1,"PostToolUse":3,"SubagentStop":1,"Stop":4,"SessionEnd":1}}
```

**Lane 8 must:**

1. **Count hook runs from the launcher's records, never from the transcript.** `stream-json` reports only
   SessionStart hooks (`hook_response`). The other twelve runs left no event in it. §4.5's per-event counts therefore
   come from `private/plugin-runs/<install_id>.jsonl` and nowhere else.
2. **Scrub before the CLI sees any output.** Exit-2 stderr, SessionStart and UserPromptSubmit stdout, and
   `additionalContext` all go to the model. SessionStart output also goes into the transcript, and a Stop
   `systemMessage` goes there too. Exit 1 and plain stdout from PreToolUse and PostToolUse go nowhere.
3. **Keep secrets out of the hook's command line.** The exit-2 feedback quotes the whole command line, launcher and
   install id included, to the model. That is harmless here, and it is one more reason a secret must never be in
   argv (§2 above).
4. **Show `systemMessage` text as the user's, not the model's.** jev-belay's shadow mode uses it.
5. **Count a Stop exit 2 as a block, not a failure.** That is how jev-belay blocks an unverified "done": the CLI
   gives the agent the stderr as `Stop hook feedback` and runs another turn. The next Stop carries
   `stop_hook_active`. A block is therefore one more turn, which the step's budget pays for. The record should count
   blocks (exit 2) apart from errors (any other non-zero exit).

## 5. Can the CLI's permission rules deny one plugin skill?

**Yes, but deny only stops the call: the skill is still listed to the model.** With
`{"permissions":{"deny":["Skill(probe:probe-skill)"]}}` in `--settings`, under `--permission-mode bypassPermissions`:

```
still_listed: ["probe:probe-other","probe:probe-skill"]
results: [{"content":"Skill execution blocked by permission rules","is_error":true},
          {"content":"Launching skill: probe:probe-other","is_error":false}]
permission_denied_events: ["Skill execution blocked by permission rules"]
```

**Lane 8 must** write a step's `disabled_skills` as `Skill(<plugin>:<skill>)` deny rules in the runner's `--settings`.
The builder's switch (§4.3) can then be offered: the skill cannot run. One question is left for the owner
(questions.json, Q2). The model still sees a disabled skill's name and description and may try it once. Is that
acceptable, or must lane 8 also hide the skill? Hiding it would mean linking the plugin's other skills one by one,
the way gstack's skills are linked today.

## 6. jev-belay 0.2.0 and jev-enforce 1.1.1, wrapped, against a one-file edit

Both loaded from their own upstream manifests. **Both repositories already ship a `.claude-plugin/plugin.json` and a
`hooks/hooks.json` at those tags**, so neither needs an overlay to load. That corrects §16, which says jev-belay has no
`plugin.json`. jev-belay's manifest declares a `userConfig`: `typesafe_api_key` (sensitive), `threshold`, `log`,
`shadow` and `check`. Its code reads `CLAUDE_PLUGIN_OPTION_<KEY>` first and falls back to `TYPESAFE_API_KEY` /
`JEV_BELAY_*`. jev-enforce declares no `userConfig`. It reads `TYPESAFE_API_KEY`, `JEV_ENFORCE_MODE`,
`JEV_ENFORCE_THRESHOLD` and `JEV_ENFORCE_OFF` from its environment. Its `package.json` lists only
`devDependencies`, so the `node_modules` refusal does not apply to it.

The launcher's env files: `jev-belay` has `{"secrets": {"TYPESAFE_API_KEY": <file>}}`; `jev-enforce` has
`{"env": {"JEV_ENFORCE_MODE": "end"}, "secrets": {"TYPESAFE_API_KEY": <file>}}`. In this case the CLI's environment
has `JEV_BASE_URL` pointing at the mock, so jev-belay's call is recorded instead of reaching TypeSafe. jev-enforce
calls `api.typesafe.ai` at a fixed URL, so its call went out with the fake key and was refused.

```
plugins: [{"name":"jev-belay",...,"source":"jev-belay@inline","version":"0.2.0"},{"name":"jev-enforce",...,"source":"jev-enforce@inline","version":"1.1.1"}, ...]
runs (times vary by run): [{"install_id":"jev-belay","event":"Stop","exit":0,"ms":109,"stdout":"","stderr":""},
       {"install_id":"jev-enforce","event":"PostToolUse","tool":"Edit","exit":0,"ms":55,"stdout":"","stderr":""},
       {"install_id":"jev-enforce","event":"Stop","exit":0,"ms":229,"stdout":"",
        "stderr":"jev-enforce: Jev 401: {\"detail\":{\"error_type\":\"authentication_error\",\"message\":\"Cannot authenticate with the server. Please check your API key and try again.\"}}\n"}]
jev_calls_to_mock: [{"path":"/jev/v1/systemone","authorization_sent":true,"body_keys":["model","state","questions"]}]
fake_key_found_in: []
```

**Hook events that fired:**

* jev-belay: `Stop`, once. It sent the request with the launcher's key, got a refusal and failed open (exit 0).
* jev-enforce: `PostToolUse` on `Edit`, once. In `end` mode that queued the edit. Then `Stop`, once, which called
  TypeSafe, got 401 and failed open (exit 0).

The fake key appears in no transcript, no CLI output, no request the model saw, nothing under HOME and no launcher
record.

**Lane 8 must** (and lane 12's smoke reads the same records):

1. Take jev-belay's sensitive `typesafe_api_key` through the placeholder-plus-launcher path from §2. The launcher
   sets `CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY`, or `TYPESAFE_API_KEY`, which the code also reads.
2. Give jev-enforce its declared settings as plain launcher variables.
3. Record `api.typesafe.ai` as both plugins' host. jev-enforce cannot be pointed anywhere else (`JEV_URL` is a
   constant in `dist/src/jev.js`), which matters for the later lane that enforces hosts.
4. Expect a refused or unreachable Jev call to read as exit 0 with stderr only. A fail-open run is invisible to the
   model, so the record's per-event exit codes and run counts are the only evidence that the gate ran.

## What this spike did not measure

* **The real image.** The CLI was `/usr/local/share/npm-global/bin/claude` 2.1.283 in a SwarmCloud agent container.
  That is the same version the image pins (the script checks), but it was not run inside `agent-runtime-base`.
* **A real TypeSafe key or a real verdict.** The point was that the hooks ran, not what Jev answered.
* **BGD's jev-enforce fork (`1.1.1-bgd.3`).** It is on no remote. The upstream `v1.1.1` was used instead.
* **Some CLI behaviours:**
  * what the CLI does with two `--plugin-dir` plugins that share a manifest name;
  * managed settings' `disableSideloadFlags`, which the CLI's strings say drops `@inline` plugins. No file in this
    repository sets it today; lane 8 should check for it in the image's managed settings at start.
  * a plugin hook alongside the runner's own `--settings` hooks. The design says plugin hooks run in addition and
    never instead; the spike's settings carried no hooks.
* **Network hosts.** Recording and enforcing them is a later lane by BGD's answer of 6 November 2026.
