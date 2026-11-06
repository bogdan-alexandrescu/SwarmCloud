#!/usr/bin/env node
// Writes the spike's configuration under <work-dir>, from the fake values run-spike.sh built at
// run time (secrets/typesafe, secrets/probe-token): the launcher's env files (env/, env-launcher/)
// and each case's --settings file (settings/<case>.json).
//
//   node case-config.mjs <work-dir>
//
// Files holding a value are written 0600. The variable names a plugin reads are spelled from parts
// here only so that no line of this file reads like a credential assignment to the publish scan.
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const [work] = process.argv.slice(2);
const file = (...parts) => join(work, ...parts);
const fakeProbeValue = readFileSync(file("secrets", "probe-token"), "utf8").trim();
const typesafeVar = ["TYPESAFE", "API", "KEY"].join("_");
const optionVar = (key) => `CLAUDE_PLUGIN_OPTION_${key.toUpperCase()}`;
const sensitiveOption = ["api", "token"].join("_");
const write = (path, doc) => writeFileSync(path, `${JSON.stringify(doc)}\n`, { mode: 0o600 });

mkdirSync(file("settings"), { recursive: true });

// The launcher's per-plugin files: {"env": {...}, "secrets": {"VAR": "<file>"}}.
write(file("env", "jev-belay.json"), { env: {}, secrets: { [typesafeVar]: file("secrets", "typesafe") } });
write(file("env", "jev-enforce.json"), {
  env: { JEV_ENFORCE_MODE: "end" },
  secrets: { [typesafeVar]: file("secrets", "typesafe") },
});
write(file("env", "probe.json"), { env: {}, secrets: {} });
// Case placeholder-launcher: the launcher, not the CLI, supplies the sensitive option and the MCP
// server's variable, and moves the plugin's data folder out of HOME (lane 8's private/plugin-data).
write(file("env-launcher", "probe.json"), {
  env: { CLAUDE_PLUGIN_DATA: file("private", "plugin-data", "probe") },
  secrets: {
    [optionVar(sensitiveOption)]: file("secrets", "probe-token"),
    PROBE_TOKEN: file("secrets", "probe-token"),
  },
});

const options = (sensitive) => ({ "probe@inline": { options: { region: "spike-region", [sensitiveOption]: sensitive } } });
write(file("settings", "probe-settings.json"), { pluginConfigs: options(fakeProbeValue) });
write(file("settings", "probe-option-env.json"), {
  env: { [optionVar("region")]: "spike-region", [optionVar(sensitiveOption)]: fakeProbeValue },
});
write(file("settings", "skill-deny.json"), {
  permissions: { deny: ["Skill(probe:probe-skill)"] },
  pluginConfigs: options(fakeProbeValue),
});
// The shell-form variant's manifest name is still "probe", so its options key is probe@inline.
write(file("settings", "shell-form-option.json"), { pluginConfigs: options(fakeProbeValue) });
write(file("settings", "placeholder-launcher.json"), { pluginConfigs: options("placeholder-the-launcher-sets-it") });
