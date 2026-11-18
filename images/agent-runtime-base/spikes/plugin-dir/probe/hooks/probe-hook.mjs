#!/usr/bin/env node
// The probe's one hook program. It appends what it was given to $SPIKE_PROBE_LOG (argv, the hook
// event, the NAMES of the CLAUDE_PLUGIN_* variables and which value each option variable holds;
// never a value), then answers in the channel the spike measures for that event:
//   SessionStart, UserPromptSubmit, PreToolUse(Read): stdout text, exit 0
//   PostToolUse(Read): stderr text, exit 1     PostToolUse(Edit|Write): stderr text, exit 2
//   PostToolUse(mcp__*): JSON additionalContext on stdout, exit 0
//   Stop: stderr text, exit 2; then, with stop_hook_active, JSON systemMessage, exit 0
//   everything else: exit 0, no output
import { createHash } from "node:crypto";
import { appendFileSync, readFileSync } from "node:fs";

// Which value an option variable holds, by digest against $SPIKE_TOKEN_SHA256: never the value.
const which = (v) => (!v ? "empty"
  : createHash("sha256").update(v).digest("hex") === process.env.SPIKE_TOKEN_SHA256 ? "the secret"
    : v === "spike-region" ? "the configured region" : v.includes("${") ? "a literal placeholder" : "another value");

let payload = {};
try { payload = JSON.parse(readFileSync(0, "utf8") || "{}"); } catch { payload = {}; }
const event = payload.hook_event_name || "unknown";
const tool = payload.tool_name || null;
const options = Object.fromEntries(Object.keys(process.env)
  .filter((k) => k.startsWith("CLAUDE_PLUGIN_OPTION_"))
  .map((k) => [k, which(process.env[k])]));
const log = process.env.SPIKE_PROBE_LOG;
if (log) {
  appendFileSync(log, `${JSON.stringify({
    event, tool, argv: process.argv.slice(2).map((a) => (a.startsWith("/") || a.includes("${") || a.endsWith("-form") ? a : which(a))),
    plugin_root_env: process.env.CLAUDE_PLUGIN_ROOT || null,
    plugin_data_env: process.env.CLAUDE_PLUGIN_DATA || null,
    options,
  })}\n`);
}

const say = (stream, text) => process[stream].write(`${text}\n`);
switch (event) {
  case "SessionStart": say("stdout", "PROBE-SESSIONSTART-STDOUT"); process.exit(0);
  case "UserPromptSubmit": say("stdout", "PROBE-USERPROMPT-STDOUT"); process.exit(0);
  case "PreToolUse": say("stdout", "PROBE-PRETOOL-STDOUT"); process.exit(0);
  case "PostToolUse":
    if (tool === "Read") { say("stderr", "PROBE-POSTREAD-EXIT1-STDERR"); process.exit(1); }
    if (/^(Edit|Write|MultiEdit)$/.test(tool || "")) { say("stderr", "PROBE-POSTEDIT-EXIT2-STDERR"); process.exit(2); }
    say("stdout", JSON.stringify({ hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: "PROBE-POSTMCP-JSON-CONTEXT" } }));
    process.exit(0);
  case "Stop":
    // The first stop is blocked the way jev-belay blocks (exit 2, the reason on stderr); the
    // stop that follows it carries stop_hook_active and is let through with a systemMessage.
    if (!payload.stop_hook_active) { say("stderr", "PROBE-STOP-EXIT2-STDERR"); process.exit(2); }
    say("stdout", JSON.stringify({ systemMessage: "PROBE-STOP-SYSTEMMESSAGE" }));
    process.exit(0);
  default: process.exit(0);
}
