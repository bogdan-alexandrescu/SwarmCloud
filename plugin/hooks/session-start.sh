#!/usr/bin/env bash
#
# The sc plugin's SessionStart hook: tell a new session which SwarmCloud
# workflows, and which of the caller's single tasks, are running, so its first
# action is `/sc attach --all`.
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
# SINGLE TASKS TOO (#830, owner 2026-10-07). The same one bridge call also
# lists the caller's running tasks that belong to no workflow (sent with
# swarm_dispatch), and the context names them: `/sc attach --all` gives each a
# row. Both reads run inside the bridge, at once, under this script's one
# timeout -- one process for the listing (the build board, below, is the one
# other).
#
# NOT ON A PLUGIN RELOAD. Claude Code has no hook event for /reload-plugins
# (SessionStart matches startup, resume, clear, compact and fork; ConfigChange
# fires on settings and skill files and cannot add context), so nothing here
# can run then. The README says so: after a reload, `/sc attach --all` by hand
# covers workflows and single tasks alike, and a task this session dispatches
# gets its row from swarm_dispatch's reply.
#
# READ-ONLY. `sc workflows` reads GET /v1/workflows and each running workflow,
# and GET /v1/tasks?state=<s>&submitted_by=me for the single tasks; it submits,
# attaches and cancels nothing.
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
#
# THE TENANT IS THE BRIDGE'S TENANT (SC-TENANT, owner 2026-10-12). The
# `tenant` option reaches the MCP server as SWARM_TENANT, behind a SWARM_TENANT
# already in Claude Code's environment (plugin.json's
# `${SWARM_TENANT:-${user_config.tenant}}`), and `sc` reads SWARM_TENANT. So
# this hook hands the option on as SWARM_TENANT under the same rule -- a
# SWARM_TENANT that is SET, even empty, wins, exactly as Claude Code's `:-`
# expansion lets it win for the server -- and the workflows it names are the
# ones the session's tools will act on.
#
# AND IT RE-ARMS `/sc watch` (SC-WATCH, owner request 2026-10-14). `/sc watch`
# schedules a `/sc check` with Claude Code's CronCreate, whose jobs end with
# the session, and saves "this project watches every N minutes" in
# `<config dir>/sc-watch/<sha256 of the project>.json` (swarm_mcp.watch). This
# hook reads that file -- locally, before and without the bridge -- and when
# it is there the session is told to run `/sc watch <N>m` first: beside the
# attach in the bridge's context (handed over as SWARM_SC_WATCH_MINUTES), or
# on its own when nothing runs, the bridge fails, or `auto_attach` is off.
# The project and the directory are derived exactly as swarm_mcp.watch derives
# them (tests/unit/mcp/test_sc_watch.py holds the two together).
#
# AND IT ASKS TO ARM IT (SC-AUTO-WATCH, owner request 2026-10-14). When the
# bridge finds running workflows or single tasks of the caller and no interval
# was ever saved, its context asks the session to arm the watch at the default
# (CronList first, so never a second job). An explicit `/sc watch off` is
# saved in the same file as `"watch": "off"`; the hook hands that over as
# SWARM_SC_WATCH_MINUTES=off and the context then asks for no arming at all.
#
# AND ONLY THIS SESSION'S WORK (SC-SESSION-SCOPE, owner 2026-10-14). Claude
# Code hands this hook the session's id on stdin (`session_id`). It goes to
# the bridge as SWARM_SC_SESSION_ID, and the context names only workflows and
# tasks tagged with it (`metadata.claude_session`): another session's work in
# the same tenant, and untagged work, is not named. When the id differs from
# the CLAUDE_CODE_SESSION_ID Claude Code was started with -- after /clear --
# it is also recorded under `<config dir>/sc-session/<sha256 of that starting
# id>.json`, so the running MCP server tags and scopes by the session's
# current id (swarm_mcp.session). The matcher includes `clear` for that.
#
# AND THIS PROJECT'S RUNNING WORK, AT SESSION START ONLY (SC-BROWSER-FOLLOWUP,
# BGD 2026-10-25: "at start also try to run attach and see if there are any
# workflows that the project its starting in has any work running in sc that
# needs to be attached and tracked here"). Beside this session's own work the
# context also names what is still running for THIS project -- whatever
# clones this checkout's origin repository, whichever session started it --
# and in the rows layout asks for `/sc attach --all --project`; in the pane
# layout the pane module opens the pane unread at session start and its
# Refresh lists that work. Another repository's work from another session is
# never named, and `/sc check` and the watch stay this session's. The layout
# is the project's saved one, else the plugin's `layout` option, handed on
# as SWARM_PLUGIN_LAYOUT below.
#
# IN THE BAND LAYOUT IT ASKS FOR NOTHING ELSE (SC-BAND, BGD 2026-10-26). With
# the layout `band` the hooks module opens the strip above the prompt at
# session start with this session's and this project's running work, read
# directly, and the plugin starts no /workflows row -- so there is nothing to
# attach, and no bridge listing is run for it. Only a remembered `/sc watch`
# is still re-armed. The layout is read here, locally, as swarm_mcp.layout
# reads it: the project's saved choice (`<config dir>/sc-layout/<sha256 of
# the project>.json`, keyed as the watch's file is), else the plugin's
# `layout` option, else `rows` (tests/unit/mcp/test_sc_band.py holds the two
# together).
#
# AND IT REFRESHES THE BUILD BOARD ONCE (BOARD-WIRE, part of #1773). In a
# project that already has a board folder (`.claude/build-board/`) or a run
# record (`launched.jsonl` in `.sc-swarmcloud-runs/`, or the older
# `.claude/sc-swarmcloud-runs/`), the bridge's `sc build-board --no-open`
# runs beside the listing, at the same time, under the same watchdog and the
# same SWARM_SESSION_START_TIMEOUT -- so the hook still finishes inside its
# budget, and a board slower than that is cut off, its previous page left in
# place (the page is replaced by a rename). Its output goes nowhere: it adds
# nothing to the context, and a failure says nothing. It runs with
# `auto_attach` off too: the board is not an attach. A project with neither
# gets no board from this hook -- one appears with its first dispatch.
set -euo pipefail

trap 'exit 0' ERR

lower() {
  printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]'
}

# The bridge's per-user directory, as swarm_mcp.config.config_dir derives it.
conf_dir() {
  if [ -n "${SWARM_CONFIG_DIR:-}" ]; then
    printf '%s' "$SWARM_CONFIG_DIR"
  elif [ -n "${XDG_CONFIG_HOME:-}" ]; then
    printf '%s' "$XDG_CONFIG_HOME/swarmcloud"
  elif [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
    printf '%s' "${HOME:-}/Library/Application Support/SwarmCloud"
  else
    printf '%s' "${HOME:-}/.config/swarmcloud"
  fi
}

# sha256 of $1, hex, or nothing.
sha() {
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$1" | sha256sum | cut -c1-64
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$1" | shasum -a 256 | cut -c1-64
  fi
}

# The file a per-project preference is saved in, under `$1` of the bridge's
# config directory (`sc-watch`, `sc-layout`), or nothing.
project_file() {
  local project top conf key
  project="${CLAUDE_PROJECT_DIR:-$PWD}"
  top="$(git -C "$project" rev-parse --show-toplevel 2>/dev/null)" || top=""
  [ -n "$top" ] && project="$top"
  project="$(cd "$project" 2>/dev/null && pwd -P)" || return 0
  conf="$(conf_dir)" || return 0
  key="$(sha "$project")" || return 0
  [ -n "$key" ] || return 0
  printf '%s' "$conf/$1/$key.json"
}

# The file `/sc watch` saves this project's preference in, or nothing.
watch_file() {
  project_file sc-watch
}

# This project's layout: the saved choice, else the plugin's `layout` option,
# else `rows` (swarm_mcp.layout.current).
layout_now() {
  local file saved option
  file="$(project_file sc-layout)" || file=""
  if [ -n "$file" ] && [ -r "$file" ]; then
    saved="$(sed -n 's/.*"layout": *"\([a-z]*\)".*/\1/p' "$file" | head -n 1)" || saved=""
    case "$saved" in
      rows | pane | band)
        printf '%s' "$saved"
        return 0
        ;;
    esac
  fi
  option="$(lower "${SWARM_PLUGIN_LAYOUT:-${CLAUDE_PLUGIN_OPTION_LAYOUT:-}}" | tr -d '[:space:]')"
  case "$option" in
    rows | pane | band) printf '%s' "$option" ;;
    *) printf 'rows' ;;
  esac
}

# An id-shaped value, or nothing (swarm_mcp.session's shape).
session_shaped() {
  case "$1" in
    '' | [!A-Za-z0-9]* | *[!A-Za-z0-9._:-]*) return 0 ;;
  esac
  [ "${#1}" -ge 8 ] && [ "${#1}" -le 128 ] || return 0
  printf '%s' "$1"
}

# Record the session's CURRENT id for the id this Claude Code process was
# started with (swarm_mcp.session): after /clear the MCP server keeps the
# environment it started with, and this record is how it learns the new id.
# The same id as the environment's removes any older record instead.
record_session() {
  local started conf key file temporary
  started="$(session_shaped "${CLAUDE_CODE_SESSION_ID:-}")" || return 0
  [ -n "$started" ] && [ -n "$1" ] || return 0
  conf="$(conf_dir)" || return 0
  key="$(sha "$started")" || return 0
  [ -n "$key" ] || return 0
  file="$conf/sc-session/$key.json"
  if [ "$1" = "$started" ]; then
    rm -f "$file" 2>/dev/null || true
    return 0
  fi
  mkdir -p "$conf/sc-session" 2>/dev/null || return 0
  temporary="$(mktemp "$conf/sc-session/.record.XXXXXX" 2>/dev/null)" || return 0
  # Id-shaped characters only, so the JSON needs no escaping.
  printf '{"session_id": "%s"}\n' "$1" >"$temporary" 2>/dev/null && mv -f "$temporary" "$file" 2>/dev/null \
    || rm -f "$temporary" 2>/dev/null || true
}

# The minutes `/sc watch` saved for this project, or nothing.
watch_minutes() {
  local file minutes
  file="$1"
  [ -n "$file" ] && [ -r "$file" ] || return 0
  minutes="$(sed -n 's/.*"interval_minutes": *\([0-9][0-9]*\).*/\1/p' "$file" | head -n 1)" || return 0
  case "$minutes" in
    '' | *[!0-9]*) return 0 ;;
  esac
  [ "$minutes" -ge 5 ] || return 0
  printf '%s' "$minutes"
}

# `off` when `/sc watch off` was saved for this project, or nothing.
watch_off() {
  [ -n "$1" ] && [ -r "$1" ] || return 0
  if grep -q '"watch": *"off"' "$1" 2>/dev/null; then
    printf 'off'
  fi
}

# `yes` when this project has a build board or a run record, or nothing: the
# project as swarm_mcp.launch resolves it, CLAUDE_PROJECT_DIR else here.
board_wanted() {
  local project
  project="${CLAUDE_PROJECT_DIR:-$PWD}"
  if [ -d "$project/.claude/build-board" ] \
    || [ -f "$project/.sc-swarmcloud-runs/launched.jsonl" ] \
    || [ -f "$project/.claude/sc-swarmcloud-runs/launched.jsonl" ]; then
    printf 'yes'
  fi
}

# The re-arm notice on its own. Digits only are interpolated, so the JSON
# needs no escaping.
print_rearm() {
  printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' \
    "The sc plugin's watch is on for this project: a \`/sc check\` every $1 minutes. Claude Code's scheduled prompts end with each session, so run \`/sc watch $1m\` now to re-arm it (\`/sc watch off\` turns it off)."
}

# The session this hook fires for: Claude Code's stdin `session_id`. Read
# first, before any way out, so the record is kept even when nothing else runs.
hook_input="$(head -c 65536 2>/dev/null)" || hook_input=""
session_id="$(printf '%s' "$hook_input" | tr -d '\n' \
  | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)" || session_id=""
session_id="$(session_shaped "$session_id")" || session_id=""
record_session "$session_id" || true

url="${SWARM_PLUGIN_DEPLOYMENT_URL:-${CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL:-}}"
[ -n "$url" ] || exit 0

prefs="$(watch_file)" || prefs=""
rearm="$(watch_minutes "$prefs")" || rearm=""
off="$(watch_off "$prefs")" || off=""

rearm_or_quiet() {
  [ -z "$rearm" ] || print_rearm "$rearm"
  exit 0
}

attach=yes
case "$(lower "${CLAUDE_PLUGIN_OPTION_AUTO_ATTACH:-true}")" in
  false | 0 | no | off) attach="" ;;
esac
# The band layout: the strip lists the work at session start; nothing to attach.
if [ "$(layout_now)" = "band" ]; then
  attach=""
fi
board="$(board_wanted)" || board=""
[ -n "$attach" ] || [ -n "$board" ] || rearm_or_quiet

root="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$root" ]; then
  root="$(cd "$(dirname "$0")/.." && pwd)" || rearm_or_quiet
fi

from="${SWARM_MCP_FROM:-}"
if [ -z "$from" ]; then
  manifest="$root/.claude-plugin/plugin.json"
  [ -r "$manifest" ] || rearm_or_quiet
  # The literal `${SWARM_MCP_FROM:-...}` text in the manifest, not an expansion.
  # shellcheck disable=SC2016
  from="$(sed -n 's/.*"\${SWARM_MCP_FROM:-\([^}]*\)}".*/\1/p' "$manifest" | head -n 1)" || rearm_or_quiet
fi
[ -n "$from" ] || rearm_or_quiet
command -v uv >/dev/null 2>&1 || rearm_or_quiet

limit="${SWARM_SESSION_START_TIMEOUT:-10}"
case "$limit" in
  '' | *[!0-9]*) limit=10 ;;
esac

out="$(mktemp "${TMPDIR:-/tmp}/sc-session-start.XXXXXX")" || rearm_or_quiet
trap 'rm -f "$out"' EXIT

export SWARM_PLUGIN_DEPLOYMENT_URL="$url"
# Each remaining setting under the bridge's name, unless the environment
# already names it. By indirection, so no value is spelled or printed here.
for key in OAUTH_CLIENT_ID DEFAULT_TARGET OAUTH_CLIENT_SECRET LAYOUT; do
  given="CLAUDE_PLUGIN_OPTION_$key"
  wanted="SWARM_PLUGIN_$key"
  if [ -n "${!given:-}" ] && [ -z "${!wanted:-}" ]; then
    export "$wanted=${!given}"
  fi
done

# The remembered watch, so the bridge's one context names the re-arm too.
# Or that it is explicitly off, so the context asks for no arming.
if [ -n "$rearm" ]; then
  export SWARM_SC_WATCH_MINUTES="$rearm"
elif [ -n "$off" ]; then
  export SWARM_SC_WATCH_MINUTES="$off"
fi

# The session the bridge's listing is scoped to (SC-SESSION-SCOPE): only work
# tagged with it is named. Unknown, the bridge names none.
if [ -n "$session_id" ]; then
  export SWARM_SC_SESSION_ID="$session_id"
fi

# The tenant under the variable every client reads, unless one is already set.
if [ -z "${SWARM_TENANT+set}" ] && [ -n "${CLAUDE_PLUGIN_OPTION_TENANT:-}" ]; then
  export SWARM_TENANT="$CLAUDE_PLUGIN_OPTION_TENANT"
fi

# A timeout in bash 3.2 terms: macOS ships no `timeout`. The watchdog's output
# goes nowhere, so a sleep it leaves behind holds no pipe Claude Code reads.
#
# The bridge runs in a PROCESS GROUP OF ITS OWN (job control on for that one
# launch), and the watchdog kills the whole group: killing only uv's pid would
# leave the python child uv spawned running after the hook has returned. The
# shell's own stderr is closed first, because job control reports a killed
# job there, and Claude Code shows a hook's stderr.
#
# The board's refresh is a second launch of its own group, killed by the same
# watchdog at the same moment.
exec 2>/dev/null
groups=()
bridge=""
set -m
if [ -n "$attach" ]; then
  uv tool run --from "$from" sc workflows --session-start </dev/null >"$out" 2>/dev/null &
  bridge=$!
  groups+=("$bridge")
fi
if [ -n "$board" ]; then
  uv tool run --from "$from" sc build-board --no-open </dev/null >/dev/null 2>&1 &
  groups+=("$!")
fi
set +m
(
  sleep "$limit"
  for group in ${groups[@]+"${groups[@]}"}; do
    kill -TERM -- "-$group" || kill -TERM "$group" || true
  done
) </dev/null >/dev/null 2>&1 &
watchdog=$!
status=0
if [ -n "$bridge" ]; then
  wait "$bridge" 2>/dev/null || status=$?
fi
for group in ${groups[@]+"${groups[@]}"}; do
  [ "$group" = "$bridge" ] || wait "$group" 2>/dev/null || true
done
kill "$watchdog" 2>/dev/null || true

[ -n "$attach" ] || rearm_or_quiet
[ "$status" -eq 0 ] || rearm_or_quiet
[ -s "$out" ] || rearm_or_quiet
grep -q '"hookSpecificOutput"' "$out" || rearm_or_quiet
cat "$out"
exit 0
