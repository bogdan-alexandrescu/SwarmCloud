#!/usr/bin/env node
// `playwright-mcp-guard` on claude-code-browser: the pinned Playwright MCP
// server behind a guard that restarts it when its browser backend is poisoned.
// Installed at /opt/swarm/playwright-mcp/playwright-mcp-guard.mjs; the
// claude-code runner registers it as the `playwright` MCP server with the real
// server's path as its first argument (`claude_code.PLAYWRIGHT_MCP_GUARD`):
//
//     playwright-mcp-guard.mjs /usr/local/share/npm-global/bin/playwright-mcp --cdp-endpoint ...
//
// WHY. On a QA leg, after the page had loaded, every browser_snapshot,
// browser_wait_for, browser_find and browser_click answered
// "TypeError: Cannot read properties of undefined (reading 'length')" and the
// flow never ran a step. The throw is inside the third-party server
// (@playwright/mcp 0.0.83, which runs playwright-core
// 1.64.0-alpha-1790635538000): BrowserBackend.callTool in
// playwright-core/lib/coreBundle.js catches it and answers `isError`, and the
// MCP server there keeps that backend -- its Context and Tabs -- until the
// browser disconnects, so the state that threw once throws on every later
// call. Nothing in this repository wrapped the server, so the agent had no way
// out short of ending its session.
//
// WHAT. A newline-delimited JSON-RPC proxy between the CLI and the server.
// Every line passes through unchanged, except a `tools/call` answer whose
// error text is that TypeError (POISONED). Then the guard:
//
//   * kills the server with SIGKILL, never SIGTERM: a graceful exit runs the
//     server's dispose, which closes the browser context, and over CDP the
//     default context is the live browser the person watches. A killed client
//     only drops its CDP connection; the browser, its context and its tabs
//     keep running;
//   * starts a fresh server with the same arguments and replays the CLI's
//     `initialize` and `notifications/initialized` to it (the replayed
//     initialize's answer is swallowed). Its first tool call connects to the
//     same endpoint and drives the same default context, so the tabs are kept;
//   * retries the call once when the tool only reads (READ_ONLY), and answers
//     a clear error otherwise: an action may already have happened before the
//     throw, so repeating a click is the agent's call, after a snapshot;
//   * answers every other call that was in flight on the old server with the
//     same clear error, and holds the CLI's lines until the new server is up.
//
// A retried call that throws again on the fresh server is answered with a
// clear error and no further restart: the page itself triggers it. After
// MAX_RESTARTS restarts the server's own error passes through, with a line
// saying so. The guard exits with the server's status when the server exits
// on its own, so a server that dies is still a dead server to the CLI.
//
// This is a recovery, not a fix of the server: the throwing read is upstream.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

const POISONED = /Cannot read properties of (?:undefined|null) \(reading '?length'?\)/;
// Tools that only read the page: a retry repeats nothing the person would see.
const READ_ONLY = new Set([
  "browser_snapshot", "browser_wait_for", "browser_find", "browser_tabs", "browser_take_screenshot",
  "browser_console_messages", "browser_network_requests", "browser_generate_locator",
  "browser_verify_element_visible", "browser_verify_list_visible", "browser_verify_text_visible",
  "browser_verify_value", "browser_webmcp_list", "browser_get_config",
]);
const MAX_RESTARTS = 3;

const REINIT_ID = "swarm-playwright-mcp-guard-reinit";

const [real, ...serverArgs] = process.argv.slice(2);
if (!real) {
  process.stderr.write("playwright-mcp-guard: refused: the Playwright MCP server's path is the first argument, and none was given.\n");
  process.exit(2);
}

let child = null;
let restarts = 0;
let restarting = false;
let initialize = null;        // the CLI's initialize request, replayed on a restart
let initialized = false;      // whether the CLI sent notifications/initialized
let retryAfterInit = null;    // the read-only call to resend once the new server is up
const held = [];              // the CLI's lines while the new server initializes
const pending = new Map();    // JSON id -> the CLI's tools/call request still unanswered
const retried = new WeakSet();

const keyOf = id => JSON.stringify(id);
const toCli = message => process.stdout.write(`${typeof message === "string" ? message : JSON.stringify(message)}\n`);
const toServer = line => child.stdin.write(`${line}\n`);

function errorText(message) {
  const parts = [];
  for (const item of message.result?.content ?? []) if (typeof item?.text === "string") parts.push(item.text);
  if (typeof message.error?.message === "string") parts.push(message.error.message);
  return parts.join("\n");
}

function isPoisoned(message) {
  return (message.result?.isError === true || message.error !== undefined) && POISONED.test(errorText(message));
}

function toolError(id, text) {
  return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: `### Error\n${text}` }], isError: true } };
}

function notRetried(tool, cause) {
  return `The Playwright MCP server hit an internal error (${cause}) that would have failed every later call, `
    + "so it was restarted and reconnected to the same live browser; the browser and its tabs were kept. "
    + `"${tool}" was not repeated: it may or may not have taken effect. Run browser_snapshot to see the page, `
    + "then repeat the step if it is still needed.";
}

function start() {
  const proc = spawn(real, serverArgs, { stdio: ["pipe", "pipe", "inherit"] });
  proc.stdin.on("error", () => {});
  proc.on("error", error => {
    if (proc !== child) return;
    process.stderr.write(`playwright-mcp-guard: could not start the Playwright MCP server: ${error.message}\n`);
    process.exit(1);
  });
  proc.on("exit", (code, signal) => {
    if (proc !== child) return;
    process.exit(code ?? (signal ? 1 : 0));
  });
  createInterface({ input: proc.stdout, crlfDelay: Infinity }).on("line", line => fromServer(proc, line));
  child = proc;
}

function restart(retry) {
  restarting = true;
  restarts += 1;
  const old = child;
  for (const request of pending.values()) {
    if (request === retry) continue;
    toCli(toolError(request.id, notRetried(request.params?.name ?? "the call", "on another call")));
  }
  pending.clear();
  start();
  old.kill("SIGKILL");
  retryAfterInit = retry;
  if (initialize) {
    toServer(JSON.stringify({ ...initialize, id: REINIT_ID }));
  } else {
    serverUp();
  }
}

function serverUp() {
  if (initialized) toServer(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }));
  restarting = false;
  if (retryAfterInit) {
    pending.set(keyOf(retryAfterInit.id), retryAfterInit);
    toServer(JSON.stringify(retryAfterInit));
    retryAfterInit = null;
  }
  for (const line of held.splice(0)) fromCli(line);
}

function poisoned(request, message) {
  const tool = request.params?.name ?? "the call";
  const cause = errorText(message).split("\n").find(line => POISONED.test(line))?.trim() ?? "TypeError";
  if (retried.has(request)) {
    toCli(toolError(request.id, `"${tool}" failed again with the same internal error (${cause}) on a freshly `
      + "restarted Playwright MCP server, so the page itself triggers it; it was not restarted again. "
      + "Navigate the tab again (browser_navigate) or open a new one (browser_tabs) and continue there."));
    return;
  }
  if (restarts >= MAX_RESTARTS) {
    const content = [...(message.result?.content ?? []), { type: "text", text:
      `playwright-mcp-guard: the server was already restarted ${restarts} times this session after this error; it is not restarted again.` }];
    toCli({ ...message, result: { ...message.result, content, isError: true } });
    return;
  }
  if (READ_ONLY.has(tool)) {
    retried.add(request);
    restart(request);
  } else {
    restart(null);
    toCli(toolError(request.id, notRetried(tool, cause)));
  }
}

function fromServer(proc, line) {
  if (proc !== child) return;   // a killed server's last words
  let message;
  try { message = JSON.parse(line); } catch { toCli(line); return; }
  if (message === null || typeof message !== "object" || Array.isArray(message)) { toCli(line); return; }
  if (restarting && message.id === REINIT_ID && message.method === undefined) {
    serverUp();
    return;
  }
  if (message.id !== undefined && message.method === undefined) {
    const key = keyOf(message.id);
    const request = pending.get(key);
    if (request) {
      pending.delete(key);
      if (isPoisoned(message)) { poisoned(request, message); return; }
    }
  }
  toCli(line);
}

function fromCli(line) {
  if (restarting) { held.push(line); return; }
  let message;
  try { message = JSON.parse(line); } catch { toServer(line); return; }
  if (message && typeof message === "object" && !Array.isArray(message)) {
    if (message.method === "initialize" && message.id !== undefined) initialize = message;
    if (message.method === "notifications/initialized") initialized = true;
    if (message.method === "tools/call" && message.id !== undefined) pending.set(keyOf(message.id), message);
  }
  toServer(line);
}

start();
createInterface({ input: process.stdin, crlfDelay: Infinity })
  .on("line", fromCli)
  .on("close", () => child.stdin.end());
for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) process.on(signal, () => child.kill(signal));
