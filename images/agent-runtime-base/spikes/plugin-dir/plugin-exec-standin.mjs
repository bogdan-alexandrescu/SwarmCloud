#!/usr/bin/env node
// A stand-in for /opt/swarm/bin/swarm-plugin-exec (docs/design/managed-plugins.md §4.4), only as
// much of it as the spike needs to measure what the CLI hands a wrapped command:
//
//   plugin-exec-standin.mjs <install_id> -- <the original command> [args...]
//
// It reads $SPIKE_PLUGIN_ENV_DIR/<install_id>.json ({"env": {...}, "secrets": {"VAR": "<file>"}}),
// sets exactly that plugin's variables, drops the runner's own credential, runs the command with
// stdin passed through, scrubs every secret value out of stdout and stderr line by line, exits with
// the child's code, and appends one line to $SPIKE_RUNS_DIR/<install_id>.jsonl: the hook event and
// tool read from stdin, its argv as the CLI expanded it, the CLAUDE_PLUGIN_* variable NAMES it was
// given, the exit code and the duration. Never a value of a secret, an option or the environment.
import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const started = Date.now();
const [installId, sep, ...command] = process.argv.slice(2);
if (!installId || sep !== "--" || !command.length) {
  process.stderr.write("usage: plugin-exec-standin.mjs <install_id> -- <command> [args...]\n");
  process.exit(64);
}

const envDir = process.env.SPIKE_PLUGIN_ENV_DIR || "";
const runsDir = process.env.SPIKE_RUNS_DIR || "";
let config = { env: {}, secrets: {} };
try { config = JSON.parse(readFileSync(join(envDir, `${installId}.json`), "utf8")); } catch { /* no config: no variables */ }

const secrets = [];
const childEnv = { ...process.env };
delete childEnv.ANTHROPIC_API_KEY; // the runner's credential is never a plugin's
for (const [name, value] of Object.entries(config.env || {})) childEnv[name] = String(value);
for (const [name, file] of Object.entries(config.secrets || {})) {
  try {
    const value = readFileSync(file, "utf8").trim();
    if (value) { childEnv[name] = value; secrets.push(value); }
  } catch { /* an unreadable secret file leaves the variable unset */ }
}
secrets.sort((a, b) => b.length - a.length);
const scrub = (text) => secrets.reduce((out, s) => out.split(s).join("[REDACTED]"), text);

function lineScrubber(target) {
  let pending = "";
  return {
    write(chunk) {
      pending += chunk.toString("utf8");
      const cut = pending.lastIndexOf("\n");
      if (cut < 0) return;
      target.write(scrub(pending.slice(0, cut + 1)));
      pending = pending.slice(cut + 1);
    },
    end() { if (pending) target.write(scrub(pending)); pending = ""; },
  };
}

const head = [];
let headBytes = 0;
const child = spawn(command[0], command.slice(1), { env: childEnv, stdio: ["pipe", "pipe", "pipe"] });
const out = lineScrubber(process.stdout);
const err = lineScrubber(process.stderr);
let outBytes = 0;
let errBytes = 0;
const outChunks = [];
const errChunks = [];
child.stdout.on("data", (c) => { outBytes += c.length; if (outBytes < 1 << 16) outChunks.push(c); out.write(c); });
child.stderr.on("data", (c) => { errBytes += c.length; if (errBytes < 1 << 16) errChunks.push(c); err.write(c); });
process.stdin.on("data", (c) => {
  if (headBytes < 1 << 20) { head.push(c); headBytes += c.length; }
  child.stdin.write(c);
});
process.stdin.on("end", () => child.stdin.end());
child.stdin.on("error", () => { /* a hook that exits without reading stdin */ });

function record(code, signal) {
  let event = null;
  let tool = null;
  try {
    const payload = JSON.parse(Buffer.concat(head).toString("utf8"));
    event = payload.hook_event_name ?? null;
    tool = payload.tool_name ?? null;
  } catch { event = headBytes ? "non-json-stdin (an MCP server's stream)" : null; }
  const line = {
    ts: new Date().toISOString(), install_id: installId, event, tool,
    argv: command.map(scrub),
    plugin_env_names: Object.keys(process.env).filter((k) => k.startsWith("CLAUDE_PLUGIN_")).sort(),
    plugin_root: process.env.CLAUDE_PLUGIN_ROOT ?? null,
    exit: code, signal, ms: Date.now() - started, stdout_bytes: outBytes, stderr_bytes: errBytes,
  };
  if (runsDir) {
    mkdirSync(runsDir, { recursive: true });
    if (process.env.SPIKE_CAPTURE_OUTPUT === "1") {
      // The spike only: what the hook said, scrubbed, so the report can show it.
      line.stdout = scrub(Buffer.concat(outChunks).toString("utf8")).slice(0, 2000);
      line.stderr = scrub(Buffer.concat(errChunks).toString("utf8")).slice(0, 2000);
    }
    appendFileSync(join(runsDir, `${installId}.jsonl`), `${JSON.stringify(line)}\n`);
  }
}

child.on("error", (e) => {
  process.stderr.write(`plugin-exec-standin: ${installId}: cannot run ${command[0]}: ${e.code || "error"}\n`);
  record(127, null);
  process.exit(127);
});
child.on("close", (code, signal) => {
  out.end();
  err.end();
  record(code, signal);
  process.exitCode = code ?? 1;
});
