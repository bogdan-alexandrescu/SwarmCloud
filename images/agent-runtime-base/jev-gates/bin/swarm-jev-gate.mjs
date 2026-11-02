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
//     (`https://api.typesafe.ai/v1/systemone`, or the shim), runs the request
//     body through the OUTBOUND FILTER below, and counts the calls.
// After the tool: one line of counts -- hook, Jev calls, blocked, rules
// flagged, strings dropped, objects denied, keys withheld; never text --
// appended to the counts file for the worker's event.
//
// THE OUTBOUND FILTER (lane jev-outbound-redaction, owner decision 2026-11-02,
// CLD's Jev report). The worker's own judgments go through
// agent_worker/judge/sanitize.py; these hooks and the Jev browser are
// third-party programs that call Jev themselves, so the same rules are applied
// here to every request body before it leaves, at every depth of the JSON:
//   1. path deny: an object whose `path` or `file` names a secret-shaped file
//      (`.env*`, `*.pem`, `*.key`, `*.p12`, `*.tfvars`, `*.tfstate*`, anything
//      under `secrets/` or `credentials/`) or one of the repository's
//      hard-stop paths (the config's `hard_stop_paths`) keeps its identity keys
//      only: the name is sent, the content never. jev-enforce sends each
//      edited file as `{kind, file, text}`, so an edited `.env` goes as its
//      name;
//   2. the redaction rule families: a string any rule of
//      apps/redaction/swarm_redaction/rules.py would mask is DROPPED. The
//      rules are asked of the image's own copy of that module (/opt/venv,
//      installed in every image built FROM agent-runtime-base), not ported:
//      the key/value rule is built on conditional groups (`(?(name)...)`) a
//      JavaScript RegExp cannot spell, and a hand-written second definition is
//      a copy that drifts. tests/unit/control_plane/test_log_redaction.py runs
//      the parity fixture through `credentialFlags` and holds it equal;
//   3. the registered secrets and the key itself are replaced with
//      `<redacted>`, as before (`scrub`);
//   4. never sent: the keys `author(s)`, `email(s)`, `user`, `login`, `env`,
//      `environ`, `environment` and `transcript` are removed wherever they are.
// Dropped, never masked, as in the worker: a masked credential's
// neighbourhood can still identify it. A string that is itself JSON text (the
// browser's internal backend sends `state` that way) is filtered as its
// structure. If the rules cannot be asked, NOTHING is sent: the call fails,
// and the tool fails open as it always has, with no judgment rather than an
// unfiltered one. tests/unit/worker/test_jev_outbound_filter.py holds it.
//
// Fails open, like both tools: a config or key that cannot be read runs
// nothing and exits 0, so the turn ends as it would have without the gate.

import { spawnSync } from "node:child_process";
import { appendFileSync, readFileSync, realpathSync } from "node:fs";
import { dirname, join, posix } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone";
export const SHIM_URL = /^http:\/\/127\.0\.0\.1:[0-9]{1,5}\/v1\/systemone$/;
export const REDACTED = "<redacted>";
// A registered value shorter than this is not scrubbed: "1" or "true" would
// erase half the message. The judge's sanitizer draws the same line.
export const MIN_SECRET_CHARS = 8;
const STDOUT_CAP = 64 * 1024;

// The outbound filter's rules: agent_worker/judge/sanitize.py's, which
// tests/unit/worker/test_jev_outbound_filter.py holds equal to these.
export const DENIED_NAME_GLOBS = [".env", ".env*", "*.pem", "*.key", "*.p12", "*.tfvars", "*.tfstate*"];
export const DENIED_DIRECTORIES = ["secrets", "credentials"];
export const IDENTITY_KEYS = ["i", "kind", "path", "file", "symbol", "name", "line", "step_id"];
export const NEVER_SENT_KEYS = ["author", "authors", "email", "emails", "user", "login",
  "env", "environ", "environment", "transcript"];
export const PATH_KEYS = ["path", "file"];
// The image's interpreter, which holds swarm_redaction (images/agent-runtime-base/Dockerfile).
export const REDACTION_PYTHON = "/opt/venv/bin/python";
const FLAG_SCRIPT = [
  "import json, sys",
  "from swarm_redaction.rules import redact",
  "texts = json.load(sys.stdin)",
  "print(json.dumps([redact(t).count > 0 for t in texts]))",
].join("\n");
const FLAG_TIMEOUT_MS = 30_000;
const DROP = Symbol("drop");

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

/** For each text, whether a redaction rule would mask any of it (rule 2).
 * Throws when the rules cannot be asked: the caller then sends nothing. */
export function credentialFlags(texts, python = REDACTION_PYTHON) {
  if (!texts.length) return [];
  const done = spawnSync(python, ["-I", "-c", FLAG_SCRIPT], {
    input: JSON.stringify(texts), encoding: "utf8", timeout: FLAG_TIMEOUT_MS,
    maxBuffer: 16 * 1024 * 1024, env: { PATH: "/usr/bin:/bin", LANG: "C.UTF-8" },
  });
  let flags = null;
  try { flags = JSON.parse(done.stdout); } catch { flags = null; }
  if (done.status !== 0 || !Array.isArray(flags) || flags.length !== texts.length) {
    throw new Error("swarm-jev-gate: refused: the redaction rules could not be asked, so nothing is sent");
  }
  return flags.map((f) => f === true);
}

/** Python's fnmatch.fnmatchcase over one name: `*`, `?` and `[...]`. */
function globMatch(name, glob) {
  let re = "";
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === "*") re += "[\\s\\S]*";
    else if (c === "?") re += "[\\s\\S]";
    else if (c === "[" && glob.indexOf("]", i + 2) > i) {
      const end = glob.indexOf("]", i + 2);
      let set = glob.slice(i + 1, end).replace(/\\/g, "\\\\");
      if (set.startsWith("!")) set = "^" + set.slice(1);
      re += `[${set}]`;
      i = end;
    } else re += c.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  }
  return new RegExp(`^${re}$`).test(name);
}

function hardStopHit(path, pattern) {
  const p = String(pattern).trim();
  if (!p) return false;
  if (p.endsWith("/")) return path.startsWith(p);
  if (globMatch(path, p)) return true;
  return path === p || path.startsWith(p.replace(/\/+$/, "") + "/");
}

/** Whether a file's CONTENT may never be sent (rule 1); its name still may.
 * `cwd`: a path under it is also tried repository-relative, for hard stops. */
export function isDeniedPath(path, hardStop = [], cwd = "") {
  let clean = String(path).trim();
  while (clean.startsWith("./")) clean = clean.slice(2);
  const parts = clean.split("/").filter((s) => s && s !== ".");
  if (!parts.length) return false;
  const name = parts[parts.length - 1];
  if (DENIED_NAME_GLOBS.some((g) => globMatch(name, g))) return true;
  if (parts.slice(0, -1).some((p) => DENIED_DIRECTORIES.includes(p))) return true;
  const stops = (Array.isArray(hardStop) ? hardStop : []).filter((s) => typeof s === "string");
  const root = cwd ? posix.normalize(cwd).replace(/\/+$/, "") + "/" : "";
  const relative = root && clean.startsWith(root) ? clean.slice(root.length) : clean;
  return stops.some((s) => hardStopHit(relative, s) || hardStopHit(clean, s));
}

function structured(text) {
  const head = text.trimStart()[0];
  if (head !== "{" && head !== "[") return undefined;
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : undefined;
  } catch { return undefined; }
}

/** Rules 1, 2 and 4 over `value`, with `flagged(text)` deciding rule 2. */
function walk(value, ctx) {
  if (value === null || typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value === "string") {
    const inner = structured(value);
    if (inner !== undefined) {
      const out = walk(inner, ctx);
      return out === DROP ? DROP : JSON.stringify(out);
    }
    if (ctx.flagged(value)) { ctx.counts.dropped += 1; return DROP; }
    return value;
  }
  if (Array.isArray(value)) return value.map((v) => walk(v, ctx)).filter((v) => v !== DROP);
  if (typeof value !== "object") return DROP; // nothing that is not JSON is sent
  let keys = Object.keys(value).filter((k) => {
    if (!NEVER_SENT_KEYS.includes(k.toLowerCase())) return true;
    ctx.counts.withheld += 1;
    return false;
  });
  if (PATH_KEYS.some((k) => typeof value[k] === "string" && isDeniedPath(value[k], ctx.hardStop, ctx.cwd))) {
    ctx.counts.denied += 1;
    keys = keys.filter((k) => IDENTITY_KEYS.includes(k));
  }
  const out = {};
  for (const k of keys) {
    if (ctx.flagged(k)) { ctx.counts.dropped += 1; continue; }
    const v = walk(value[k], ctx);
    if (v !== DROP) out[k] = v;
  }
  return out;
}

/** The request body `text` as it may leave the pod: scrubbed (rule 3), then
 * rules 1, 2 and 4, at every depth. Adds to `counts`; throws, and nothing is
 * sent, when the rules cannot be asked or nothing of the body survives. */
export function filterBody(text, { secrets = [], hardStop = [], cwd = "", python = REDACTION_PYTHON, counts } = {}) {
  const tally = counts ?? { dropped: 0, denied: 0, withheld: 0 };
  let value;
  try { value = scrub(JSON.parse(text), secrets); } catch { value = scrubText(String(text), secrets); }
  // First pass: every string the second could ask about, asked at once.
  const seen = new Set();
  walk(value, { flagged: (s) => { seen.add(s); return false; }, hardStop, cwd,
    counts: { dropped: 0, denied: 0, withheld: 0 } });
  const texts = [...seen];
  const flags = credentialFlags(texts, python);
  const hits = new Set(texts.filter((_, i) => flags[i]));
  const out = walk(value, { flagged: (s) => hits.has(s), hardStop, cwd, counts: tally });
  if (out === DROP) throw new Error("swarm-jev-gate: refused: nothing of the request may be sent");
  return typeof out === "string" ? out : JSON.stringify(out);
}

/** A `fetch` that sends only to TypeSafe's API (or the shim), filtered, counted. */
export function guardedFetch(realFetch, { url, key, secrets, counter, hardStop = [], cwd = "", python = REDACTION_PYTHON }) {
  for (const name of ["calls", "dropped", "denied", "withheld"]) counter[name] ??= 0;
  return async (input, init = {}) => {
    const target = typeof input === "string" ? input : String(input?.url ?? input);
    if (target !== TYPESAFE_URL) {
      throw new Error("swarm-jev-gate: refused a request that is not to TypeSafe's API");
    }
    if (typeof init.body !== "string") {
      throw new Error("swarm-jev-gate: refused a request whose body is not JSON text");
    }
    const body = filterBody(init.body, { secrets, hardStop, cwd, python, counts: counter });
    counter.calls += 1;
    const headers = { "Content-Type": "application/json", Authorization: `Bearer ${key}` };
    return realFetch(url, { method: "POST", body, headers, signal: init.signal });
  };
}

/** The jev-browser MCP server's filter (images/agent-runtime-browser/
 * jev-browser/bin/jev-browser-mcp runs `node --import` of this before the
 * server's first module, so upstream src/jev.mjs, which calls the global
 * `fetch` for every Jev call, gets this one). Every request body leaves
 * filtered; where it may go stays the wrapper's decision (SWARM_JEV_BACKEND).
 * The key it was given is scrubbed with the registered secrets. Each call
 * appends one line of counts, never text, to SWARM_JEV_BROWSER_COUNTS when the
 * runner names one, and to stderr, which the CLI keeps as the server's log. */
export function installBrowserFilter(env = process.env) {
  const realFetch = globalThis.fetch.bind(globalThis);
  const python = env.SWARM_JEV_REDACTION_PYTHON || REDACTION_PYTHON;
  const countsFile = env.SWARM_JEV_BROWSER_COUNTS || "";
  globalThis.fetch = async (input, init = {}) => {
    if (typeof input !== "string" && !(input instanceof URL)) {
      throw new Error("jev-browser-mcp: refused a request that is not a URL and a body");
    }
    if (init.body === undefined || init.body === null) return realFetch(input, init);
    if (typeof init.body !== "string") {
      throw new Error("jev-browser-mcp: refused a request whose body is not JSON text");
    }
    const names = String(env.SWARM_JEV_SECRET_ENV_NAMES || "").split(",").filter(Boolean);
    const counts = { dropped: 0, denied: 0, withheld: 0 };
    const body = filterBody(init.body, { secrets: secretValues(env.TYPESAFE_API_KEY, names, env), python, counts });
    const line = JSON.stringify({ hook: "browser", calls: 1, ...counts });
    process.stderr.write(`jev-browser-mcp: outbound filter ${line}\n`);
    if (countsFile) {
      try { appendFileSync(countsFile, `${line}\n`, { mode: 0o600 }); } catch { /* a count is never worth a call */ }
    }
    return realFetch(input, { ...init, body });
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
  const counter = { calls: 0, dropped: 0, denied: 0, withheld: 0 };
  globalThis.fetch = guardedFetch(globalThis.fetch.bind(globalThis), {
    url: found.url, key: found.key, secrets, counter,
    hardStop: Array.isArray(config.hard_stop_paths) ? config.hard_stop_paths : [],
    cwd: process.cwd(),
    python: typeof config.redaction_python === "string" && config.redaction_python ? config.redaction_python : REDACTION_PYTHON,
  });
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
      const { calls, dropped, denied, withheld } = counter;
      appendFileSync(config.counts_file, `${JSON.stringify({ hook: mode.hook, calls, blocked, rules, dropped, denied, withheld })}\n`, { mode: 0o600 });
    } catch { /* a count is never worth a turn */ }
  });

  process.argv = [process.argv[0], entry, ...mode.args];
  await import(pathToFileURL(entry).href);
}

if (isEntryPoint()) {
  main(process.argv.slice(2)).catch(() => process.exit(0));
}
