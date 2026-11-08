// jev-adapter: the Jev adapter for a managed plugin bound to the workspace's
// TypeSafe key (docs/design/managed-plugins.md section 6.2, "A binding brings
// its adapter"; lane 8).
//
//     node --import file:///opt/swarm/jev-gates/bin/jev-adapter.mjs <the hook's script> ...
//
// `swarm-plugin-exec` preloads this into each `node` command of a plugin whose
// signed set binds a secret to the workspace's TypeSafe key, and names the
// plugin's 0600 env file in SWARM_JEV_ADAPTER_CONFIG. Before the hook's first
// module runs, it does in this process exactly what the baked gates' launcher
// does (`adaptProcess` in swarm-jev-gate.mjs, one definition for both): reads
// the key file the worker wrote (the key, or the forwarder's shim), replaces
// `fetch` with one that sends only to TypeSafe's API or the shim, every body
// through THE OUTBOUND FILTER, every call recorded in the calls file the
// worker already settles; removes the variables that would point the tool
// elsewhere; and sets the key in the bound variables only. At exit, one line
// of counts, never text, to the plugin's counts file.
//
// Replacing `fetch` stops accidental leakage only: a hook that uses
// `node:https`, `net` or `child_process` is not filtered (section 12; egress
// is Q1's). Fails closed for the key: a config or key that cannot be read
// sets no key, and the hook runs without one, as a hook with no key fails
// open.

import { appendFileSync, readFileSync } from "node:fs";
import { adaptProcess, credential } from "./swarm-jev-gate.mjs";

export const CONFIG_ENV = "SWARM_JEV_ADAPTER_CONFIG";
const VARIABLE = /^[A-Z_][A-Z0-9_]{0,127}$/;

/** Install the adapter in this process from `env`; returns the counter, or null. */
export function installAdapter(env = process.env) {
  const path = env[CONFIG_ENV] || "";
  delete env[CONFIG_ENV];
  let adapter, found;
  try {
    adapter = JSON.parse(readFileSync(path, "utf8")).adapter;
    found = adapter && adapter.kind === "typesafe" ? credential(readFileSync(adapter.key_file, "utf8")) : null;
  } catch {
    found = null;
  }
  if (!found) return null;
  const variables = (Array.isArray(adapter.variables) ? adapter.variables : []).filter((v) => typeof v === "string" && VARIABLE.test(v));
  const hook = typeof adapter.hook === "string" ? adapter.hook : "managed_plugin";
  const counter = adaptProcess({
    found, env, secretNames: [], hardStop: adapter.hard_stop_paths, cwd: process.cwd(),
    python: adapter.redaction_python, callsFile: adapter.calls_file, hook, variables, keepOptions: true,
  });
  const countsFile = typeof adapter.counts_file === "string" ? adapter.counts_file : "";
  process.on("exit", () => {
    if (!countsFile) return;
    try {
      const { calls, dropped, denied, withheld } = counter;
      appendFileSync(countsFile, `${JSON.stringify({ hook, calls, dropped, denied, withheld })}\n`, { mode: 0o600 });
    } catch { /* a count is never worth a turn */ }
  });
  return counter;
}

installAdapter();
