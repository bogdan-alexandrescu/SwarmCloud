#!/usr/bin/env bash
#
# The sc plugin's SessionStart hook: tell a new session which SwarmCloud
# workflows are running, so its first action is `/sc attach --all`.
#
# Owner decision, 2026-10-02: running SwarmCloud workflows show in Claude Code
# AUTOMATICALLY, as live [SwarmCloud] rows, without attaching each by id. A
# hook cannot start a Workflow itself, so this one asks the bridge's CLI for
# the caller's tenant's running workflows and hands the session
# `additionalContext` naming them and telling it to run `/sc attach --all`.
#
# IT NEVER STANDS IN A SESSION'S WAY. Every path out of this script exits 0;
# stderr is never written, because Claude Code shows a hook's stderr; and it
# prints nothing at all when:
#   * the plugin's `auto_attach` option is off (CLAUDE_PLUGIN_OPTION_AUTO_ATTACH,
#     default on per the owner);
#   * the plugin has no deployment configured -- the session is not in a
#     SwarmCloud-configured setup, and there is nobody to ask;
#   * uv, the manifest's pin, or the bridge is missing, fails, or is slower
#     than SWARM_SESSION_START_TIMEOUT seconds (default 10; the first fetch of
#     a new bridge version can take longer, and that session simply gets no
#     notice);
#   * the bridge answers anything but the hook's JSON -- including nothing,
#     which is what `sc workflows --session-start` prints when none run.
#
# READ-ONLY. `sc workflows` reads GET /v1/workflows and each running workflow;
# it submits, attaches and cancels nothing.
#
# THE BRIDGE IS THE PLUGIN'S OWN, from the pin in plugin.json -- the same
# `${SWARM_MCP_FROM:-<pinned requirement>}` the MCP server runs, read out of the
# manifest here rather than restated, because the pin has exactly one home
# (tests/unit/mcp/test_plugin_bridge_install.py). SWARM_MCP_FROM, the
# developer escape hatch, wins here as it does there.
#
# The plugin's settings reach a hook as CLAUDE_PLUGIN_OPTION_<KEY>; the bridge
# reads them as SWARM_PLUGIN_<KEY> (swarm_mcp.config), so they are handed on
# under that name. Nothing here prints any of them.
set -euo pipefail

trap 'exit 0' ERR

lower() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

case "$(lower "${CLAUDE_PLUGIN_OPTION_AUTO_ATTACH:-true}")" in
  false | 0 | no | off) exit 0 ;;
esac

url="${SWARM_PLUGIN_DEPLOYMENT_URL:-${CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL:-}}"
[ -n "$url" ] || exit 0

root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd "$(dirname "$0")/.." && pwd)" || exit 0
fi

from="${SWARM_MCP_FROM:-}"
if [ -z "$from" ]; then
  manifest="$root/.claude-plugin/plugin.json"
  [ -r "$manifest" ] || exit 0
  # The literal `${SWARM_MCP_FROM:-...}` text in the manifest, not an expansion.
  # shellcheck disable=SC2016
  from="$(sed -n 's/.*"\${SWARM_MCP_FROM:-\([^}]*\)}".*/\1/p' "$manifest" | head -n 1)" || exit 0
fi
[ -n "$from" ] || exit 0
command -v uv >/dev/null 2>&1 || exit 0

limit="${SWARM_SESSION_START_TIMEOUT:-10}"
case "$limit" in
  '' | *[!0-9]*) limit=10 ;;
esac

out="$(mktemp "${TMPDIR:-/tmp}/sc-session-start.XXXXXX")" || exit 0
trap 'rm -f "$out"' EXIT

export SWARM_PLUGIN_DEPLOYMENT_URL="$url"
# Each remaining setting under the bridge's name, unless the environment
# already names it. By indirection, so no value is spelled or printed here.
for key in OAUTH_CLIENT_ID DEFAULT_TARGET OAUTH_CLIENT_SECRET; do
  given="CLAUDE_PLUGIN_OPTION_$key"
  wanted="SWARM_PLUGIN_$key"
  if [ -n "${!given:-}" ] && [ -z "${!wanted:-}" ]; then
    export "$wanted=${!given}"
  fi
done

# A timeout in bash 3.2 terms: macOS ships no `timeout`. The watchdog's output
# goes nowhere, so a sleep it leaves behind holds no pipe Claude Code reads.
#
# The bridge runs in a PROCESS GROUP OF ITS OWN (job control on for that one
# launch), and the watchdog kills the whole group: killing only uv's pid would
# leave the python child uv spawned running after the hook has returned. The
# shell's own stderr is closed first, because job control reports a killed
# job there, and Claude Code shows a hook's stderr.
exec 2>/dev/null
set -m
uv tool run --from "$from" sc workflows --session-start </dev/null >"$out" 2>/dev/null &
bridge=$!
set +m
(sleep "$limit" && { kill -TERM -- "-$bridge" || kill -TERM "$bridge"; }) </dev/null >/dev/null 2>&1 &
watchdog=$!
status=0
wait "$bridge" 2>/dev/null || status=$?
kill "$watchdog" 2>/dev/null || true

[ "$status" -eq 0 ] || exit 0
[ -s "$out" ] || exit 0
grep -q '"hookSpecificOutput"' "$out" || exit 0
cat "$out"
exit 0
