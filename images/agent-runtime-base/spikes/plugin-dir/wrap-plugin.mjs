#!/usr/bin/env node
// The one rewrite the intake job makes to a plugin's files (docs/design/managed-plugins.md §4.4),
// done in place on a copy: every hook command and every MCP server command becomes
//
//   <launcher> <install_id> -- <the original command>
//
//   node wrap-plugin.mjs <plugin-dir> <install_id> <launcher>
//
// A shell-form hook ({"command": "node ${CLAUDE_PLUGIN_ROOT}/x.mjs"}) keeps its text after the
// launcher, so the CLI's own substitution and the shell's word splitting still apply to it. An
// exec-form hook ({"command": "node", "args": [...]}) and an MCP server become the launcher with
// the original command and arguments as its arguments. Prints what it rewrote as JSON.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [dir, installId, launcher] = process.argv.slice(2);
if (!dir || !installId || !launcher) {
  process.stderr.write("usage: wrap-plugin.mjs <plugin-dir> <install_id> <launcher>\n");
  process.exit(64);
}
const rewritten = [];

function wrapHook(hook, where) {
  if (!hook || hook.type !== "command" || typeof hook.command !== "string") return hook;
  if (Array.isArray(hook.args)) {
    rewritten.push({ where, form: "exec", was: hook.command });
    return { ...hook, command: launcher, args: [installId, "--", hook.command, ...hook.args] };
  }
  rewritten.push({ where, form: "shell", was: hook.command });
  return { ...hook, command: `'${launcher}' ${installId} -- ${hook.command}` };
}

function wrapHooks(hooks, where) {
  const out = {};
  for (const [event, groups] of Object.entries(hooks || {})) {
    out[event] = (groups || []).map((group, g) => ({
      ...group,
      hooks: (group.hooks || []).map((h, i) => wrapHook(h, `${where}:${event}[${g}].hooks[${i}]`)),
    }));
  }
  return out;
}

function wrapServers(servers, where) {
  const out = {};
  for (const [name, server] of Object.entries(servers || {})) {
    if (server && typeof server.command === "string") {
      rewritten.push({ where: `${where}:${name}`, form: "server", was: server.command });
      out[name] = { ...server, command: launcher, args: [installId, "--", server.command, ...(server.args || [])] };
    } else {
      out[name] = server;
    }
  }
  return out;
}

function rewrite(file, fn) {
  const path = join(dir, file);
  if (!existsSync(path)) return;
  const doc = JSON.parse(readFileSync(path, "utf8"));
  writeFileSync(path, `${JSON.stringify(fn(doc), null, 2)}\n`);
}

rewrite("hooks/hooks.json", (doc) => ({ ...doc, hooks: wrapHooks(doc.hooks, "hooks/hooks.json") }));
rewrite(".mcp.json", (doc) => (doc.mcpServers
  ? { ...doc, mcpServers: wrapServers(doc.mcpServers, ".mcp.json") }
  : wrapServers(doc, ".mcp.json")));
rewrite(".claude-plugin/plugin.json", (doc) => {
  const next = { ...doc };
  if (doc.hooks && typeof doc.hooks === "object") next.hooks = wrapHooks(doc.hooks.hooks || doc.hooks, "plugin.json");
  if (doc.mcpServers && typeof doc.mcpServers === "object") next.mcpServers = wrapServers(doc.mcpServers, "plugin.json");
  return next;
});
process.stdout.write(`${JSON.stringify({ install_id: installId, rewritten })}\n`);
