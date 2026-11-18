#!/usr/bin/env node
// A scripted stand-in for the Anthropic Messages API (and for TypeSafe's Jev endpoint), so the
// spike drives the real Claude Code CLI with no model credentials and the same turns every run.
//
//   node mock-api.mjs <port-file> <log-file> <plan-file>
//
// It listens on 127.0.0.1 on a free port, writes the port to <port-file>, and appends one JSON
// line per request to <log-file>: the path, and for /v1/messages the whole request body (the
// CLI's system prompt, tools and messages: what the model would have seen). Every header is
// dropped except whether an Authorization or x-api-key was present.
//
// The plan is a JSON list of turns for the main conversation, chosen by how many assistant
// messages the request already carries. A turn is {"text": "..."} or {"tool": "<name>" |
// "tool_suffix": "<end of an MCP tool's name>", "input": {...}}. Past the end it says "Done.".
// A request whose system prompt holds PROBE-AGENT-BODY is the probe sub-agent, answered with
// one line of text; a request with no tools (the CLI's small side queries) gets "ok".
import { createServer } from "node:http";
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";

const [portFile, logFile, planFile] = process.argv.slice(2);
const plan = JSON.parse(readFileSync(planFile, "utf8"));
let ids = 0;
const nextId = (prefix) => `${prefix}_spike${String(++ids).padStart(6, "0")}`;

function systemText(body) {
  if (typeof body.system === "string") return body.system;
  return Array.isArray(body.system) ? body.system.map((b) => b.text || "").join("\n") : "";
}

function choose(body) {
  const tools = Array.isArray(body.tools) ? body.tools : [];
  if (systemText(body).includes("PROBE-AGENT-BODY")) return { text: "PROBE-AGENT-REPLY: the probe agent ran." };
  if (!tools.length) return { text: "ok" };
  const turn = (body.messages || []).filter((m) => m.role === "assistant").length;
  const step = plan[turn];
  if (!step) return { text: "Done." };
  if (step.text) return { text: step.text };
  let name = step.tool;
  if (step.tool_suffix) {
    const hit = tools.find((t) => typeof t.name === "string" && t.name.endsWith(step.tool_suffix));
    name = hit ? hit.name : `missing${step.tool_suffix}`;
  }
  return { tool: name, input: step.input || {} };
}

function message(reply, model) {
  const content = reply.text
    ? [{ type: "text", text: reply.text }]
    : [{ type: "tool_use", id: nextId("toolu"), name: reply.tool, input: reply.input }];
  return {
    id: nextId("msg"), type: "message", role: "assistant", model: model || "claude-spike",
    content, stop_reason: reply.text ? "end_turn" : "tool_use", stop_sequence: null,
    usage: { input_tokens: 10, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
  };
}

function sse(res, msg) {
  const send = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
  res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
  send("message_start", { message: { ...msg, content: [], stop_reason: null } });
  msg.content.forEach((block, index) => {
    if (block.type === "text") {
      send("content_block_start", { index, content_block: { type: "text", text: "" } });
      send("content_block_delta", { index, delta: { type: "text_delta", text: block.text } });
    } else {
      send("content_block_start", { index, content_block: { ...block, input: {} } });
      send("content_block_delta", { index, delta: { type: "input_json_delta", partial_json: JSON.stringify(block.input) } });
    }
    send("content_block_stop", { index });
  });
  send("message_delta", { delta: { stop_reason: msg.stop_reason, stop_sequence: null }, usage: { output_tokens: 10 } });
  send("message_stop", {});
  res.end();
}

const server = createServer((req, res) => {
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const raw = Buffer.concat(chunks).toString("utf8");
    let body = null;
    try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }
    const path = (req.url || "").split("?")[0];
    const auth = Boolean(req.headers.authorization || req.headers["x-api-key"]);
    const entry = { ts: new Date().toISOString(), method: req.method, path, auth };
    if (path === "/v1/messages" && body) {
      const reply = choose(body);
      entry.body = body;
      entry.reply = reply;
      appendFileSync(logFile, `${JSON.stringify(entry)}\n`);
      const msg = message(reply, body.model);
      if (body.stream) sse(res, msg);
      else { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify(msg)); }
      return;
    }
    if (path === "/v1/messages/count_tokens") {
      appendFileSync(logFile, `${JSON.stringify(entry)}\n`);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ input_tokens: 10 }));
      return;
    }
    if (path === "/jev/v1/systemone" || path.startsWith("/jev/")) {
      // jev-belay's call, with JEV_BASE_URL pointed here. The answer is a refusal, so the hook
      // takes its fail-open path; what the spike needs is that it called, with a key.
      entry.jev = true;
      entry.body_keys = body && typeof body === "object" ? Object.keys(body) : [];
      appendFileSync(logFile, `${JSON.stringify(entry)}\n`);
      res.writeHead(401, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { type: "authentication_error", message: "spike: fake key" } }));
      return;
    }
    appendFileSync(logFile, `${JSON.stringify(entry)}\n`);
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { type: "not_found_error", message: "spike mock" } }));
  });
});

server.listen(0, "127.0.0.1", () => writeFileSync(portFile, String(server.address().port)));
