#!/usr/bin/env bash
set -euo pipefail
# Re-runs the managed-plugins loading spike (docs/design/managed-plugins-spike.md).
#
# It drives the Claude Code CLI the image pins, headless (`claude -p`) with `--plugin-dir`,
# against mock-api.mjs (a scripted Messages API on 127.0.0.1), so it needs no model credentials
# and every run takes the same turns. HOME is an empty folder: no keychain, no ~/.claude.
#
#   images/agent-runtime-base/spikes/plugin-dir/run-spike.sh [work-dir]
#
# Needs: node >= 22, git, network to github.com (jev-belay v0.2.0 and jev-enforce v1.1.1 are
# cloned at their tags and checked against the commits below), and `claude` at the pinned
# version (CLAUDE_BIN overrides). Writes everything under the work dir (default
# $TMPDIR/plugin-dir-spike), never into the repository, and prints the report (report.mjs).
#
# The fake TypeSafe key is built here at run time, written 0600 under the work dir and handed
# only to the wrapped plugins through the stand-in launcher; the report checks that it appears
# in no transcript, no CLI output and nothing the CLI wrote under HOME.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/../../../.." && pwd)"
WORK="${1:-${TMPDIR:-/tmp}/plugin-dir-spike}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"

JEV_BELAY_TAG="v0.2.0"
JEV_BELAY_COMMIT="ef719db7eaadc56aa4def86c4da4ffff5bcbca35"
JEV_ENFORCE_TAG="v1.1.1"
JEV_ENFORCE_COMMIT="ed9c7d79bad2bfdea474df5e414b8b601c0e92cd"

die() { printf 'run-spike: %s\n' "$*" >&2; exit 1; }

[ -n "${CLAUDE_BIN}" ] && [ -x "${CLAUDE_BIN}" ] || die "no claude CLI on PATH (set CLAUDE_BIN)"
command -v node >/dev/null || die "node is required"
command -v git >/dev/null || die "git is required"

PINNED="$(sed -n 's/^ARG CLAUDE_CODE_VERSION=//p' "${REPO}/images/agent-runtime-base/Dockerfile" | head -n 1)"
ACTUAL="$("${CLAUDE_BIN}" --version | awk '{print $1}')"
[ "${ACTUAL}" = "${PINNED}" ] || die "claude is ${ACTUAL}; the image pins ${PINNED}"

case "${WORK}" in
  "${REPO}"|"${REPO}"/*) die "the work dir must be outside the repository" ;;
esac
# The plugin copies are made read-only below; give the write bit back before removing a last run.
if [ -d "${WORK}" ]; then chmod -R u+w "${WORK}"; fi
rm -rf "${WORK}"
mkdir -p "${WORK}"/{upstream,plugins,env,env-launcher,out,secrets,settings,private/plugin-data/probe}
chmod 0700 "${WORK}/secrets" "${WORK}/env" "${WORK}/env-launcher"
LAUNCHER="${HERE}/plugin-exec-standin.mjs"

fetch() { # <dir> <url> <tag> <commit>
  git -c advice.detachedHead=false clone -q --depth 1 --branch "$3" "$2" "$1"
  local got
  got="$(git -C "$1" rev-parse HEAD)"
  [ "${got}" = "$4" ] || die "$2 at $3 is ${got}, expected $4"
  rm -rf "$1/.git"
}
fetch "${WORK}/upstream/jev-belay" https://github.com/valentynkit/jev-belay "${JEV_BELAY_TAG}" "${JEV_BELAY_COMMIT}"
fetch "${WORK}/upstream/jev-enforce" https://github.com/erkamyaman/jev-enforce "${JEV_ENFORCE_TAG}" "${JEV_ENFORCE_COMMIT}"

# The plugins as a worker would hold them: a copy, rewritten once by the intake, then read-only.
cp -R "${HERE}/probe" "${WORK}/plugins/probe"
cp -R "${WORK}/upstream/jev-belay" "${WORK}/plugins/jev-belay"
cp -R "${WORK}/upstream/jev-enforce" "${WORK}/plugins/jev-enforce"
for id in probe jev-belay jev-enforce; do
  node "${HERE}/wrap-plugin.mjs" "${WORK}/plugins/${id}" "${id}" "${LAUNCHER}" >"${WORK}/out/wrap-${id}.json"
  chmod -R a-w "${WORK}/plugins/${id}"
done

# A variant of the probe whose shell-form SessionStart hook names ${user_config.region}: the CLI's
# own text says it refuses that, and the spike records what "refuses" means for the plugin.
cp -R "${HERE}/probe" "${WORK}/plugins/probe-shellform"
chmod -R u+w "${WORK}/plugins/probe-shellform"
# shellcheck disable=SC2016 # the ${...} are the plugin's placeholders, written as text on purpose
node -e '
  const fs = require("fs"); const f = process.argv[1];
  const doc = JSON.parse(fs.readFileSync(f, "utf8"));
  doc.hooks.SessionStart[0].hooks.push({ type: "command", command: "node \"${CLAUDE_PLUGIN_ROOT}/hooks/probe-hook.mjs\" shell-form \"${user_config.region}\"" });
  fs.writeFileSync(f, JSON.stringify(doc, null, 2));
' "${WORK}/plugins/probe-shellform/hooks/hooks.json"

# Values built at run time: the fake TypeSafe key and the probe's sensitive option, each written
# 0600 under the work dir and read from there; case-config.mjs writes every file that names them.
fake_value() { node -e 'process.stdout.write(process.argv[1] + require("crypto").randomBytes(16).toString("hex"))' "$1"; }
( umask 077
  fake_value spike-typesafe- >"${WORK}/secrets/typesafe"
  fake_value spike-probe- >"${WORK}/secrets/probe-token"
)
node "${HERE}/case-config.mjs" "${WORK}"
TOKEN_SHA256="$(node -e 'process.stdout.write(require("crypto").createHash("sha256").update(require("fs").readFileSync(process.argv[1])).digest("hex"))' "${WORK}/secrets/probe-token")"
ENV_DIR="${WORK}/env"

MOCK_PID=""
cleanup() { if [ -n "${MOCK_PID}" ]; then kill "${MOCK_PID}" 2>/dev/null || true; fi; }
trap cleanup EXIT

# run_case <name> <plan-json> <prompt> <settings|""> <plugin-dir>...
# "settings" passes $WORK/settings/<name>.json (case-config.mjs) as --settings.
run_case() {
  local name="$1" plan="$2" prompt="$3" settings="$4"
  shift 4
  local dir="${WORK}/cases/${name}"
  mkdir -p "${dir}/home" "${dir}/repo"
  printf '%s\n' "${plan//\"HELLO\"/\"${dir}/repo/hello.txt\"}" >"${dir}/plan.json"
  printf 'hello\n' >"${dir}/repo/hello.txt"
  printf '# Rules\n\n- Never write the word banana in any file.\n' >"${dir}/repo/CLAUDE.md"
  git -C "${dir}/repo" init -q
  local args=(-p "${prompt}" --output-format stream-json --verbose --permission-mode bypassPermissions)
  if [ -n "${settings}" ]; then
    cp "${WORK}/settings/${name}.json" "${dir}/settings.json"
    args+=(--settings "${dir}/settings.json")
  fi
  local p
  for p in "$@"; do args+=(--plugin-dir "${WORK}/plugins/${p}"); done

  rm -f "${dir}/port"
  node "${HERE}/mock-api.mjs" "${dir}/port" "${dir}/api.jsonl" "${dir}/plan.json" &
  MOCK_PID=$!
  local tries=0
  until [ -s "${dir}/port" ]; do
    tries=$((tries + 1))
    [ "${tries}" -lt 100 ] || die "the mock API did not start"
    sleep 0.1
  done
  local port
  port="$(cat "${dir}/port")"

  printf 'run-spike: case %s: claude %s\n' "${name}" "${args[*]}" >&2
  printf '%s\n' "${args[@]}" >"${dir}/argv.txt"
  local rc=0
  ( cd "${dir}/repo" && env -i \
      HOME="${dir}/home" PATH="${PATH}" TERM=dumb LANG=C.UTF-8 \
      ANTHROPIC_BASE_URL="http://127.0.0.1:${port}" \
      ANTHROPIC_API_KEY="$(fake_value spike-runner-)" \
      DISABLE_TELEMETRY=1 DISABLE_AUTOUPDATER=1 DISABLE_ERROR_REPORTING=1 \
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
      JEV_BASE_URL="http://127.0.0.1:${port}/jev" \
      SPIKE_PROBE_LOG="${dir}/probe.jsonl" SPIKE_RUNS_DIR="${dir}/runs" \
      SPIKE_PLUGIN_ENV_DIR="${ENV_DIR}" SPIKE_TOKEN_SHA256="${TOKEN_SHA256}" SPIKE_CAPTURE_OUTPUT=1 \
      timeout 180 "${CLAUDE_BIN}" "${args[@]}" \
      >"${dir}/stream.jsonl" 2>"${dir}/stderr.txt" ) || rc=$?
  printf '%s\n' "${rc}" >"${dir}/exit-code"
  kill "${MOCK_PID}" 2>/dev/null || true
  wait "${MOCK_PID}" 2>/dev/null || true
  MOCK_PID=""
}

# 1. Every component of the probe, options from a --settings file.
run_case probe-settings '[
  {"tool":"Read","input":{"file_path":"HELLO"}},
  {"tool":"Edit","input":{"file_path":"HELLO","old_string":"hello","new_string":"hello, plugin"}},
  {"tool_suffix":"__probe_ping","input":{}},
  {"tool":"Skill","input":{"skill":"probe:probe-skill"}},
  {"tool":"Agent","input":{"subagent_type":"probe:probe-agent","description":"probe agent","prompt":"Run the probe agent."}},
  {"text":"Done: hello.txt is edited and the probe ran."}
]' "/probe:probe-cmd hello.txt" settings probe

# 2. The same plugin with no option values anywhere.
run_case probe-no-options '[{"tool_suffix":"__probe_ping","input":{}},{"text":"Done."}]' \
  "Ping the probe." "" probe

# 3. Options only as CLAUDE_PLUGIN_OPTION_* in the CLI's own environment: not set by env -i, so
#    this case passes them through a settings env block, the only way into the CLI's environment
#    that does not also reach every other process the runner starts.
run_case probe-option-env '[{"tool_suffix":"__probe_ping","input":{}},{"text":"Done."}]' \
  "Ping the probe." settings probe

# 4. A permission rule denying one plugin skill.
run_case skill-deny '[
  {"tool":"Skill","input":{"skill":"probe:probe-skill"}},
  {"tool":"Skill","input":{"skill":"probe:probe-other"}},
  {"text":"Done."}
]' "Use the probe skill." settings probe

# 5. ${user_config.*} in a shell-form hook command.
run_case shell-form-option '[{"text":"Done."}]' "Say hi." settings probe-shellform

# 6. The design's fallback (§4.4): non-secret options and a PLACEHOLDER for the sensitive one in
#    --settings, the real value supplied by the launcher to the hook and to the MCP server.
ENV_DIR="${WORK}/env-launcher"
run_case placeholder-launcher '[{"tool_suffix":"__probe_ping","input":{}},{"text":"Done."}]' \
  "Ping the probe." settings probe
ENV_DIR="${WORK}/env"

# 7. jev-belay and jev-enforce, wrapped, against a one-file edit.
run_case jev '[
  {"tool":"Read","input":{"file_path":"HELLO"}},
  {"tool":"Edit","input":{"file_path":"HELLO","old_string":"hello","new_string":"hello, plugins: this line is the one-file edit"}},
  {"text":"Done: hello.txt is edited and all the tests pass."}
]' "Edit hello.txt so it says hello, plugins." "" jev-belay jev-enforce

node "${HERE}/report.mjs" "${WORK}" "${PINNED}"
