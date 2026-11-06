#!/usr/bin/env node
// The probe's MCP server: stdio, newline-delimited JSON-RPC, one tool, probe_ping. It records to
// $SPIKE_PROBE_LOG that it started and which value each of its two option-fed variables holds
// (by digest, never the value), and the ping's answer carries PROBE-MCP-PONG so the spike can find it.
import { createHash } from "node:crypto";
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

const log = process.env.SPIKE_PROBE_LOG;
const state = (v) => (v === undefined ? "unset" : v === "" ? "empty"
  : createHash("sha256").update(v).digest("hex") === process.env.SPIKE_TOKEN_SHA256 ? "the secret"
    : v === "spike-region" ? "the configured region" : v.includes("${") ? "a literal placeholder" : "another value");
if (log) {
  appendFileSync(log, `${JSON.stringify({
    event: "mcp-server-start", argv: process.argv.slice(2),
    plugin_root_env: process.env.CLAUDE_PLUGIN_ROOT || null,
    region: state(process.env.PROBE_REGION),
    token: state(process.env.PROBE_TOKEN),
    option_env_names: Object.keys(process.env).filter((k) => k.startsWith("CLAUDE_PLUGIN_OPTION_")).sort(),
  })}\n`);
}
const send = (msg) => process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...msg })}\n`);
createInterface({ input: process.stdin }).on("line", (line) => {
  let req;
  try { req = JSON.parse(line); } catch { return; }
  if (req.id === undefined) return;
  if (req.method === "initialize") {
    send({ id: req.id, result: { protocolVersion: req.params?.protocolVersion || "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "probe", version: "0.0.1" } } });
  } else if (req.method === "tools/list") {
    send({ id: req.id, result: { tools: [{ name: "probe_ping", description: "The probe's ping", inputSchema: { type: "object", properties: {} } }] } });
  } else if (req.method === "tools/call") {
    if (log) appendFileSync(log, `${JSON.stringify({ event: "mcp-tool-call", tool: req.params?.name })}\n`);
    send({ id: req.id, result: { content: [{ type: "text", text: "PROBE-MCP-PONG" }] } });
  } else {
    send({ id: req.id, error: { code: -32601, message: "method not found" } });
  }
});
