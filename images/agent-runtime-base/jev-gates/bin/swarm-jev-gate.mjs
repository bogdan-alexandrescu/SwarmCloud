#!/usr/bin/env node
// swarm-jev-gate: run jev-belay or jev-enforce as a Claude Code hook with the
// workspace's TypeSafe key in THIS process only (JEV-LANES, BGD 2026-10-31).
//
//     node swarm-jev-gate.mjs <config.json> <belay|enforce-stop|enforce-edit>
//
// The claude-code runner names this file in each hook command
// (`claude_code.write_headless_settings`) when the worker handed it the gates'
// config (`agent_worker.qualitygates`, SWARM_JEV_GATES_CONFIG): a 0600 JSON
// file in `private/` naming the key file, the counts file, jev-enforce's data
// directory and the profile's registered secret NAMES. The CLI, its Bash tool
// and the settings file carry the config's PATH, never the key.
//
// What it does, in order, before the pinned tool runs in this process:
//   * reads the key file the worker wrote. With the TypeSafe forwarder on it
//     is the worker's shim file instead (`{shim_url, token}`): then every
//     request goes to the loopback shim with its local token, and no TypeSafe
//     key is in this pod;
//   * puts the key (or token) in TYPESAFE_API_KEY for the tool, and removes
//     every variable that would point it elsewhere (JEV_BASE_URL) or turn on
//     a text log (JEV_BELAY_LOG);
//   * replaces `fetch` with one that sends ONLY to TypeSafe's API
//     (`https://api.typesafe.ai/v1/systemone`, or the shim), replaces every
//     registered secret's value and the key itself in the request body with
//     `<redacted>`, at every depth of the JSON, and counts the calls.
// After the tool: one line of counts -- hook, Jev calls, blocked, rules
// flagged; never text -- appended to the counts file for the worker's event.
//
// Fails open, like both tools: a config or key that cannot be read runs
// nothing and exits 0, so the turn ends as it would have without the gate.

import { appendFileSync, readFileSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone";
export const SHIM_URL = /^http:\/\/127\.0\.0\.1:[0-9]{1,5}\/v1\/systemone$/;
export const REDACTED = "<redacted>";
// A registered value shorter than this is not scrubbed: "1" or "true" would
// erase half the message. The judge's sanitizer draws the same line.
export const MIN_SECRET_CHARS = 8;
const STDOUT_CAP = 64 * 1024;

const HERE = dirname(fileURLToPath(import.meta.url));
// The pinned tools, installed beside this launcher by the image
// (images/agent-runtime-base/Dockerfile, JEV-LANES).
export const MODES = {
  "belay": { hook: "belay", entry: join(HERE, "..", "jev-belay", "belay.mjs"), args: [] },
  "enforce-stop": { hook: "enforce", entry: join(HERE, "..", "jev-enforce", "dist", "src", "cli.js"), args: ["hook", "stop"] },
  "enforce-edit": { hook: "enforce", entry: join(HERE, "..", "jev-enforce", "dist", "src", "cli.js"), args: ["hook", "post-edit"] },
};

/** `{key, url}` from the worker's file: a key, or the shim's `{shim_url, token}`. */
export function credential(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return null;
  if (trimmed.startsWith("{")) {
    let parsed;
    try { parsed = JSON.parse(trimmed); } catch { return null; }
    const keys = parsed && typeof parsed === "object" ? Object.keys(parsed).sort() : [];
    if (keys.join(",") !== "shim_url,token") return null;
    const { shim_url: url, token } = parsed;
    if (typeof url !== "string" || !SHIM_URL.test(url) || typeof token !== "string" || !token) return null;
    return { key: token, url };
  }
  return { key: trimmed, url: TYPESAFE_URL };
}

/** The values to scrub: the key and every registered secret set in `env`. */
export function secretValues(key, names, env) {
  const out = new Set();
  for (const value of [key, ...(Array.isArray(names) ? names : []).map((n) => env[n])]) {
    if (typeof value === "string" && value.trim().length >= MIN_SECRET_CHARS) out.add(value.trim());
  }
  // Longest first, so a secret that contains another is replaced whole.
  return [...out].sort((a, b) => b.length - a.length);
}

export function scrubText(text, secrets) {
  let out = text;
  for (const s of secrets) out = out.split(s).join(REDACTED);
  return out;
}

/** `value` with every string, keys included, scrubbed, at every depth. */
export function scrub(value, secrets) {
  if (typeof value === "string") return scrubText(value, secrets);
  if (Array.isArray(value)) return value.map((v) => scrub(v, secrets));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[scrubText(k, secrets)] = scrub(v, secrets);
    return out;
  }
  return value;
}

/** A `fetch` that sends only to TypeSafe's API (or the shim), scrubbed, counted. */
export function guardedFetch(realFetch, { url, key, secrets, counter }) {
  return async (input, init = {}) => {
    const target = typeof input === "string" ? input : String(input?.url ?? input);
    if (target !== TYPESAFE_URL) {
      throw new Error("swarm-jev-gate: refused a request that is not to TypeSafe's API");
    }
    let body = init.body;
    if (typeof body !== "string") {
      throw new Error("swarm-jev-gate: refused a request whose body is not JSON text");
    }
    try {
      body = JSON.stringify(scrub(JSON.parse(body), secrets));
    } catch {
      body = scrubText(body, secrets);
    }
    counter.calls += 1;
    const headers = { "Content-Type": "application/json", Authorization: `Bearer ${key}` };
    return realFetch(url, { method: "POST", body, headers, signal: init.signal });
  };
}

/** What one hook run says it did, from its exit code and what it printed. */
export function outcome(hook, exitCode, stdout) {
  if (hook === "belay") return { blocked: exitCode === 2, rules: 0 };
  let parsed = null;
  try { parsed = JSON.parse(stdout); } catch { parsed = null; }
  if (!parsed || parsed.decision !== "block") return { blocked: false, rules: 0 };
  const m = /(\d+) CLAUDE\.md rule/.exec(String(parsed.systemMessage || ""));
  return { blocked: true, rules: m ? Number(m[1]) : 0 };
}

function isEntryPoint() {
  try { return pathToFileURL(realpathSync(process.argv[1])).href === import.meta.url; } catch { return false; }
}

async function main(argv) {
  const [configPath, modeName] = argv;
  const mode = Object.hasOwn(MODES, modeName || "") ? MODES[modeName] : null;
  let config, found;
  try {
    config = JSON.parse(readFileSync(configPath, "utf8"));
    found = credential(readFileSync(config.key_file, "utf8"));
  } catch {
    found = null;
  }
  if (!mode || !found) process.exit(0);
  // Not installed in this image: nothing to run, and nothing to count.
  const entry = realpathSync(mode.entry);

  const env = process.env;
  const secrets = secretValues(found.key, config.secret_env_names, env);
  const counter = { calls: 0 };
  globalThis.fetch = guardedFetch(globalThis.fetch.bind(globalThis), { url: found.url, key: found.key, secrets, counter });
  for (const name of Object.keys(env)) {
    if (name.startsWith("CLAUDE_PLUGIN_OPTION_")) delete env[name];
  }
  for (const name of ["JEV_BASE_URL", "JEV_API_KEY", "JEV_BELAY_LOG", "JEV_BELAY_SHADOW", "JEV_ENFORCE_OFF"]) delete env[name];
  env.TYPESAFE_API_KEY = found.key;
  env.JEV_ENFORCE_MODE = "end";
  if (typeof config.data_dir === "string" && config.data_dir) env.CLAUDE_PLUGIN_DATA = config.data_dir;

  let printed = "";
  const write = process.stdout.write.bind(process.stdout);
  process.stdout.write = (chunk, ...rest) => {
    if (printed.length < STDOUT_CAP) printed += String(chunk);
    return write(chunk, ...rest);
  };
  process.on("exit", (code) => {
    const { blocked, rules } = outcome(mode.hook, code, printed);
    try {
      appendFileSync(config.counts_file, `${JSON.stringify({ hook: mode.hook, calls: counter.calls, blocked, rules })}\n`, { mode: 0o600 });
    } catch { /* a count is never worth a turn */ }
  });

  process.argv = [process.argv[0], entry, ...mode.args];
  await import(pathToFileURL(entry).href);
}

if (isEntryPoint()) {
  main(process.argv.slice(2)).catch(() => process.exit(0));
}
