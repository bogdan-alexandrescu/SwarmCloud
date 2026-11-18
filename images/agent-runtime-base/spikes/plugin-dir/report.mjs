#!/usr/bin/env node
// Reads what run-spike.sh left under <work-dir> and answers the spike's questions with the lines
// that prove each answer. Writes <work-dir>/report.json and prints the same as Markdown.
//
//   node report.mjs <work-dir> <pinned-cli-version>
//
// Every string it prints is scrubbed first: the work dir becomes $WORK, and the run's fake
// TypeSafe key and fake probe token become <FAKE_KEY> and <FAKE_TOKEN>. It also checks that
// neither appears where it must not (transcript, CLI output, HOME) and says so in the report.
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";

const [work, pinned] = process.argv.slice(2);
const fakeKey = readFileSync(join(work, "secrets/typesafe"), "utf8").trim();
const fakeToken = readFileSync(join(work, "secrets/probe-token"), "utf8").trim();
const scrub = (s) => String(s).split(fakeKey).join("<FAKE_KEY>").split(fakeToken).join("<FAKE_TOKEN>").split(work).join("$WORK");
const caseDir = (c) => join(work, "cases", c);
const lines = (file) => (existsSync(file) ? readFileSync(file, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []);
const stream = (c) => lines(join(caseDir(c), "stream.jsonl"));
const api = (c) => lines(join(caseDir(c), "api.jsonl"));
const probe = (c) => lines(join(caseDir(c), "probe.jsonl"));
const runs = (c) => {
  const dir = join(caseDir(c), "runs");
  return existsSync(dir) ? readdirSync(dir).flatMap((f) => lines(join(dir, f))) : [];
};
const init = (c) => stream(c).find((e) => e.type === "system" && e.subtype === "init") || {};

function walk(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}
// Where in a case's files a value appears: transcript (stream), CLI stderr, the requests the
// model saw, anything under HOME, the launcher's records.
function whereIs(c, value) {
  const hits = [];
  const check = (label, file) => { if (existsSync(file) && readFileSync(file, "utf8").includes(value)) hits.push(label); };
  check("stream.jsonl (the CLI's output, the worker's transcript)", join(caseDir(c), "stream.jsonl"));
  check("stderr.txt", join(caseDir(c), "stderr.txt"));
  check("api.jsonl (what the model saw)", join(caseDir(c), "api.jsonl"));
  for (const f of walk(join(caseDir(c), "home"))) check(`HOME/${relative(join(caseDir(c), "home"), f)}`, f);
  for (const f of walk(join(caseDir(c), "runs"))) check(`launcher record ${relative(caseDir(c), f)}`, f);
  return hits;
}

const messagesText = (body) => JSON.stringify(body?.messages || []);
function seenByModel(c, marker) {
  for (const [i, e] of api(c).entries()) {
    if (!e.body) continue;
    const text = messagesText(e.body);
    const at = text.indexOf(marker);
    if (at >= 0) return { request: i, excerpt: scrub(text.slice(Math.max(0, at - 160), at + marker.length + 40)) };
    const sys = JSON.stringify(e.body.system || "");
    if (sys.includes(marker)) return { request: i, in: "system" };
  }
  return null;
}
function seenInStream(c, marker) {
  const hit = stream(c).find((e) => JSON.stringify(e).includes(marker) && e.type === "system");
  return hit ? scrub(JSON.stringify({ subtype: hit.subtype, hook_event: hit.hook_event, content: hit.content, exit_code: hit.exit_code })) : null;
}

const report = { cli: { pinned, version: init("probe-settings").claude_code_version }, cases: {} };
for (const c of readdirSync(join(work, "cases"))) {
  report.cases[c] = {
    exit_code: Number(readFileSync(join(caseDir(c), "exit-code"), "utf8")),
    argv: scrub(readFileSync(join(caseDir(c), "argv.txt"), "utf8").trim().split("\n").join(" ")),
  };
}

// Q1: every component from --plugin-dir.
const i1 = init("probe-settings");
report.components = {
  plugins: scrub(JSON.stringify(i1.plugins)),
  mcp_servers: i1.mcp_servers,
  mcp_tools: (i1.tools || []).filter((t) => t.includes("probe")),
  agents: (i1.agents || []).filter((a) => a.startsWith("probe:")),
  skills: (i1.skills || []).filter((a) => a.startsWith("probe:")),
  commands: (i1.slash_commands || []).filter((a) => a.startsWith("probe:")),
  used: {
    command_body_in_first_prompt: seenByModel("probe-settings", "PROBE-COMMAND-BODY"),
    mcp_tool_result: seenByModel("probe-settings", "PROBE-MCP-PONG"),
    skill_body: seenByModel("probe-settings", "PROBE-SKILL-BODY"),
    agent_body_as_subagent_system_prompt: seenByModel("probe-settings", "PROBE-AGENT-BODY"),
  },
  hook_events_fired: [...new Set(runs("probe-settings").map((r) => r.event))].sort(),
};

// Q2: options with no keychain.
report.options = {};
for (const c of ["probe-settings", "probe-no-options", "probe-option-env", "placeholder-launcher", "shell-form-option"]) {
  if (!report.cases[c]) continue;
  const p = probe(c);
  const hookOptions = p.find((e) => e.options)?.options ?? null;
  const execForm = p.find((e) => (e.argv || [])[0] === "exec-form") || null;
  report.options[c] = {
    mcp_servers: init(c).mcp_servers,
    mcp_server_env: p.find((e) => e.event === "mcp-server-start") || "the server did not start",
    hook_option_env: hookOptions,
    exec_form_hook_argv: execForm ? execForm.argv : "the exec-form hook did not run",
    failed_hooks: stream(c).filter((e) => e.subtype === "hook_response" && e.exit_code !== 0)
      .map((e) => scrub(`${e.hook_event} exit ${e.exit_code}: ${e.stderr}`)),
    fake_token_found_in: whereIs(c, fakeToken),
    home_files: walk(join(caseDir(c), "home")).map((f) => relative(join(caseDir(c), "home"), f)).filter((f) => f.startsWith(".claude")),
  };
}

// Q3: ${CLAUDE_PLUGIN_ROOT} inside a wrapped command.
report.plugin_root = {
  wrapped: JSON.parse(readFileSync(join(work, "out/wrap-jev-enforce.json"), "utf8")).rewritten.map((r) => scrub(r.was)),
  launcher_argv: [...new Map([...runs("probe-settings"), ...runs("jev")]
    .map((r) => [JSON.stringify(r.argv), { install_id: r.install_id, event: r.event, argv: r.argv.map(scrub), plugin_root: scrub(r.plugin_root) }])).values()],
  plugin_data_from_cli: [...new Set(probe("probe-settings").map((e) => e.plugin_data_env).filter(Boolean))].map(scrub),
  plugin_data_with_launcher_override: [...new Set(probe("placeholder-launcher").map((e) => e.plugin_data_env).filter(Boolean))].map(scrub),
};

// Q4: how hook exit codes and output reach the agent.
const markers = [
  ["SessionStart, stdout, exit 0", "PROBE-SESSIONSTART-STDOUT"],
  ["UserPromptSubmit, stdout, exit 0", "PROBE-USERPROMPT-STDOUT"],
  ["PreToolUse(Read), stdout, exit 0", "PROBE-PRETOOL-STDOUT"],
  ["PostToolUse(Read), stderr, exit 1", "PROBE-POSTREAD-EXIT1-STDERR"],
  ["PostToolUse(Edit), stderr, exit 2", "PROBE-POSTEDIT-EXIT2-STDERR"],
  ["PostToolUse(mcp), JSON additionalContext, exit 0", "PROBE-POSTMCP-JSON-CONTEXT"],
  ["Stop, stderr, exit 2 (the first stop)", "PROBE-STOP-EXIT2-STDERR"],
  ["Stop, JSON systemMessage, exit 0 (stop_hook_active)", "PROBE-STOP-SYSTEMMESSAGE"],
];
report.hook_output = markers.map(([what, marker]) => ({
  what, marker, model: seenByModel("probe-settings", marker), stream: seenInStream("probe-settings", marker),
}));
const reported = {};
for (const e of stream("probe-settings")) if (e.subtype === "hook_response") reported[e.hook_event] = (reported[e.hook_event] || 0) + 1;
const ran = {};
for (const r of runs("probe-settings")) ran[r.event] = (ran[r.event] || 0) + 1;
report.hook_reporting = { hook_response_events_in_stream: reported, launcher_records: ran };

// Q5: a permission rule over one plugin skill.
report.skill_deny = {
  settings: scrub(readFileSync(join(caseDir("skill-deny"), "settings.json"), "utf8").trim()),
  still_listed: (init("skill-deny").skills || []).filter((s) => s.startsWith("probe:")),
  results: stream("skill-deny").filter((e) => e.type === "user")
    .flatMap((e) => (Array.isArray(e.message?.content) ? e.message.content : []))
    .filter((b) => b.type === "tool_result").map((b) => scrub(JSON.stringify({ content: b.content, is_error: b.is_error || false }))),
  permission_denied_events: stream("skill-deny").filter((e) => e.subtype === "permission_denied").map((e) => scrub(e.message)),
};

// Q6: jev-belay and jev-enforce, wrapped.
report.jev = {
  plugins: scrub(JSON.stringify(init("jev").plugins)),
  runs: runs("jev").map((r) => ({
    install_id: r.install_id, event: r.event, tool: r.tool, exit: r.exit, ms: r.ms,
    stdout: scrub(r.stdout ?? ""), stderr: scrub(r.stderr ?? ""),
  })),
  jev_calls_to_mock: api("jev").filter((e) => e.jev).map((e) => ({ path: e.path, authorization_sent: e.auth, body_keys: e.body_keys })),
  fake_key_found_in: whereIs("jev", fakeKey),
  plugin_data_written: walk(join(caseDir("jev"), "home/.claude/plugins/data")).map((f) => relative(caseDir("jev"), f)),
};

writeFileSync(join(work, "report.json"), scrub(`${JSON.stringify(report, null, 2)}\n`));

const out = [];
const h = (t) => out.push(`\n## ${t}\n`);
const kv = (k, v) => out.push(`- ${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`);
out.push(`# Plugin-dir spike report\n\nClaude Code ${report.cli.version} (the image pins ${pinned}).`);
h("Cases");
for (const [c, v] of Object.entries(report.cases)) kv(c, `exit ${v.exit_code}: claude ${v.argv}`);
h("1. Components loaded from --plugin-dir");
for (const [k, v] of Object.entries(report.components)) kv(k, v);
h("2. userConfig with no keychain");
for (const [c, v] of Object.entries(report.options)) { out.push(`\n### ${c}`); for (const [k, x] of Object.entries(v)) kv(k, x); }
h("3. ${CLAUDE_PLUGIN_ROOT} inside a wrapped command");
for (const [k, v] of Object.entries(report.plugin_root)) kv(k, v);
h("4. Hook exit codes and output");
for (const r of report.hook_output) kv(r.what, { model: r.model, stream: r.stream });
kv("hook reporting", report.hook_reporting);
h("5. A deny rule over one plugin skill");
for (const [k, v] of Object.entries(report.skill_deny)) kv(k, v);
h("6. jev-belay and jev-enforce, wrapped");
for (const [k, v] of Object.entries(report.jev)) kv(k, v);
process.stdout.write(scrub(`${out.join("\n")}\n`));
