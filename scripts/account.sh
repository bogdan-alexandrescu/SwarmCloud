#!/usr/bin/env bash
# Onboard and manage the Claude subscription accounts agents run on.
#
# One account at a time, one command each, and after `add` nobody has to touch
# it again: the quota broker refreshes every account on its sweep -- including
# idle ones, because an unexchanged refresh token expires on its own and a pool
# where three accounts are busy and two are idle is a pool where the idle two
# quietly rot until the day they are wanted.
#
# CREDENTIAL HANDLING is the same rule as create-secrets.sh, for the same
# reasons: never an argument (argv is world-readable through ps), never echoed,
# never in terraform state, never in git. It reaches gcloud through a 0600 file
# in a private temp directory removed on every exit path.
#
# WHERE `add` GETS THE CREDENTIAL
#   --from-claudeswitch <id>   read it from the local claudeswitch vault, which
#                              is where five accounts already are
#   --stdin                    paste it (the `claudeAiOauth` JSON, or the whole
#                              keychain item -- both shapes are accepted)
#
# In both cases the value must contain a refreshToken. A credential with only an
# access token cannot be refreshed, so it would work for a few hours and then
# stop, and the outage would look nothing like the paste that caused it.
#
# Usage:
#   scripts/account.sh add --tenant u-bogdan --label personal --from-claudeswitch saga-personal
#   scripts/account.sh add --tenant u-bogdan --label team --stdin
#   scripts/account.sh add --tenant u-bogdan --label shared --stdin --lend-to eng
#   scripts/account.sh list [--tenant u-bogdan]
#   scripts/account.sh pause  --tenant u-bogdan --label personal
#   scripts/account.sh resume --tenant u-bogdan --label personal
#   scripts/account.sh drain  --tenant u-bogdan --label personal
#   scripts/account.sh remove --tenant u-bogdan --label personal

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

COMMAND="${1:-}"
[[ -n "${COMMAND}" ]] && shift || true

TENANT=""
LABEL=""
PROVIDER="anthropic"
FROM_CLAUDESWITCH=""
READ_STDIN=0
LEND_TO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)            TENANT="$2"; shift 2 ;;
    --label)             LABEL="$2"; shift 2 ;;
    --provider)          PROVIDER="$2"; shift 2 ;;
    --from-claudeswitch) FROM_CLAUDESWITCH="$2"; shift 2 ;;
    --stdin)             READ_STDIN=1; shift ;;
    --lend-to)           LEND_TO="$2"; shift 2 ;;
    -h|--help)           sed -n '2,34p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# The label becomes part of a Secret Manager name and a Kubernetes annotation,
# so it is checked here -- where a person is watching -- rather than at dispatch,
# where nobody is. Same expression as quota_broker.accounts.validate_label.
validate_label() {
  local label="$1"
  [[ "${label}" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] \
    || die "label '${label}' must be lowercase letters, digits and dashes, starting and ending alphanumeric, at most 40 characters"
}

# Refuse anything that is not a plain tenant id before it reaches a filter
# expression or a resource name.
validate_tenant() {
  local tenant="$1"
  [[ "${tenant}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
    || die "tenant '${tenant}' is not a plain tenant id"
}

# Resolved once: the registration steps need google-cloud-firestore, which the
# system python does not have.
SWARM_PY="$(swarm_python)"

secret_base() {
  printf 'swarm-account-%s-%s' "$1" "$2"
}

# Grant exactly what each identity needs on the two secrets, and nothing more.
#
# These secrets are created out of band -- terraform manages the CONTAINER for
# tenant provider keys but knows nothing about accounts, which are added by an
# operator at any time -- so the grant has to live with the thing that creates
# them. create-secrets.sh only WARNS when no binding exists, which is why the
# first three accounts onboarded with no bindings at all and the broker could
# not have refreshed any of them.
#
# The split is the same one the tenant secrets use, and for the same reason: a
# refresh token is standing access to the account, an access token expires.
#
#   <base>-refresh   broker reads AND writes. The worker is absent.
#   <base>           broker writes, worker reads.
grant_access() {
  local base="$1" refresh_secret="$2"
  local broker="serviceAccount:swarm-quota-broker@${PROJECT_ID}.iam.gserviceaccount.com"
  local worker="serviceAccount:swarm-agent-worker-${TENANT}@${PROJECT_ID}.iam.gserviceaccount.com"

  step "Access"

  # --condition=None is required, not cosmetic: without it gcloud prompts for a
  # condition choice, and a prompt in a script that may run unattended is a hang.
  _bind() {
    gcloud secrets add-iam-policy-binding "$1" \
      --project "${PROJECT_ID}" --member "$2" --role "$3" \
      --condition=None >/dev/null 2>&1
  }

  # if/else rather than `&& ok || warn`: with the latter, a failing `ok` would
  # run the warn too, and the report would contradict itself.
  _report() {
    if _bind "$1" "$2" "$3"; then ok "$4"; else warn "could not grant: $4"; fi
  }

  _report "${refresh_secret}" "${broker}" roles/secretmanager.secretAccessor \
    "broker may read ${refresh_secret}"
  _report "${refresh_secret}" "${broker}" roles/secretmanager.secretVersionAdder \
    "broker may write ${refresh_secret} (rotated tokens)"
  _report "${base}" "${broker}" roles/secretmanager.secretVersionAdder \
    "broker may publish access tokens to ${base}"

  # The worker reads the SHORT-LIVED half only. Its absence from the refresh
  # secret is the isolation property, not an oversight: an agent that could read
  # a refresh token could mint itself credentials long after its job ended.
  if gcloud iam service-accounts describe "${worker#serviceAccount:}" \
       --project "${PROJECT_ID}" --format='value(email)' >/dev/null 2>&1; then
    _report "${base}" "${worker}" roles/secretmanager.secretAccessor \
      "tenant worker may read ${base}"
  else
    warn "no worker service account for tenant ${TENANT} yet"
    dim "run: scripts/register-tenant.sh --tenant ${TENANT}"
  fi
}

cmd_add() {
  [[ -n "${TENANT}" ]] || die "add requires --tenant"
  [[ -n "${LABEL}" ]]  || die "add requires --label"
  validate_tenant "${TENANT}"
  validate_label "${LABEL}"

  if [[ -n "${FROM_CLAUDESWITCH}" && "${READ_STDIN}" -eq 1 ]]; then
    die "choose one source: --from-claudeswitch or --stdin"
  fi
  if [[ -z "${FROM_CLAUDESWITCH}" && "${READ_STDIN}" -eq 0 ]]; then
    die "add needs a credential source: --from-claudeswitch <id> or --stdin"
  fi

  local base refresh_secret work key_file
  base="$(secret_base "${TENANT}" "${LABEL}")"
  refresh_secret="${base}-refresh"

  work="$(mktemp -d "${TMPDIR:-/tmp}/swarm-account.XXXXXX")"
  chmod 0700 "${work}"
  # Every exit path, including a failure between writing and uploading.
  trap 'rm -rf "${work}"' EXIT INT TERM
  key_file="${work}/credential.json"

  step "Reading the credential"
  if [[ -n "${FROM_CLAUDESWITCH}" ]]; then
    # Read the vault entry from where claudeswitch keeps it. There is no
    # `claudeswitch export`, so this depends on its storage layout --
    # internal/credstore names a vault item `claudeswitch:<id>`, a keychain item
    # on macOS and a file beside the live credential elsewhere.
    #
    # That is a real coupling, and the reason it is acceptable is that the
    # failure is loud and the fallback is one flag away: if the layout changes
    # this stops finding anything and says to use --stdin, rather than finding
    # something wrong.
    #
    # Redirected straight to a 0600 file: a shell variable would put the
    # credential in the environment of every command run afterwards.
    case "$(uname -s)" in
      Darwin)
        ( umask 077; security find-generic-password \
            -s "claudeswitch:${FROM_CLAUDESWITCH}" -w >"${key_file}" 2>/dev/null ) \
          || die "no claudeswitch vault entry '${FROM_CLAUDESWITCH}'.
     \`claudeswitch accounts\` lists what is there. macOS may also be asking to
     approve keychain access with nobody watching -- run \`claudeswitch status\`
     once in a terminal and approve it. Or paste it with --stdin." ;;
      *)
        local vault="${HOME}/.claude/claudeswitch/${FROM_CLAUDESWITCH}.json"
        [[ -r "${vault}" ]] || die "no claudeswitch vault entry at ${vault}; use --stdin instead"
        ( umask 077; cat "${vault}" >"${key_file}" ) ;;
    esac
    ok "read '${FROM_CLAUDESWITCH}' from the claudeswitch vault"
  else
    ( umask 077; cat >"${key_file}" )
    ok "read from stdin"
  fi
  chmod 0600 "${key_file}"

  [[ -s "${key_file}" ]] || die "refusing to store an empty credential"

  step "Checking the credential"
  # BLOCKS rather than warns. The likeliest mistake is pasting the access token
  # alone: it passes every check that only counts bytes, works for a few hours,
  # and then the account stops with nothing connecting the outage to a paste
  # made that morning. The refresh token is the part that matters.
  if ! python3 - "${key_file}" <<'PYEOF'; then
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    sys.exit("not JSON")
if isinstance(data, dict) and isinstance(data.get("claudeAiOauth"), dict):
    data = data["claudeAiOauth"]          # Claude Code's own wrapper
if not isinstance(data, dict):
    sys.exit("not a JSON object")
if not (data.get("refreshToken") or data.get("refresh_token")):
    sys.exit("no refreshToken")
if not (data.get("accessToken") or data.get("access_token")):
    sys.exit("no accessToken")
PYEOF
    die "the value must be a Claude credential containing BOTH accessToken and refreshToken.
     A credential with no refresh token cannot be refreshed: it would work for a few
     hours and then stop, and the outage would look nothing like this paste.
     macOS: security find-generic-password -s 'Claude Code-credentials' -w"
  fi
  ok "credential has both halves"

  step "Secret ${refresh_secret}"
  local describe_err
  if describe_err="$(gcloud secrets describe "${refresh_secret}" --project "${PROJECT_ID}" \
       --format='value(name)' 2>&1 >/dev/null)"; then
    ok "secret exists; adding a new version"
  else
    die_if_auth_failure "${describe_err}"
    info "creating secret"
    gcloud secrets create "${refresh_secret}" \
      --project "${PROJECT_ID}" \
      --replication-policy=user-managed \
      --locations="${REGION}" \
      --labels="managed-by=swarm-secrets,component=swarm-account,tenant=${TENANT},account=${LABEL},provider=${PROVIDER},environment=${ENVIRONMENT}" \
      >/dev/null
    ok "created ${refresh_secret}"
  fi

  gcloud secrets versions add "${refresh_secret}" \
    --project "${PROJECT_ID}" --data-file="${key_file}" --format='value(name)' >/dev/null
  ok "stored the long-lived half"

  # The short-lived half. Created empty: the broker is its only writer and will
  # publish an access token to it on the next sweep. Creating it here rather
  # than letting the broker create it keeps the labels and the accessor binding
  # in one place -- a secret the broker invented would have neither.
  step "Secret ${base}"
  if gcloud secrets describe "${base}" --project "${PROJECT_ID}" \
       --format='value(name)' >/dev/null 2>&1; then
    ok "secret exists"
  else
    gcloud secrets create "${base}" \
      --project "${PROJECT_ID}" \
      --replication-policy=user-managed \
      --locations="${REGION}" \
      --labels="managed-by=swarm-secrets,component=swarm-account,tenant=${TENANT},account=${LABEL},provider=${PROVIDER},environment=${ENVIRONMENT}" \
      >/dev/null
    ok "created ${base} (empty; the broker publishes the access token)"
  fi

  rm -rf "${work}"
  trap - EXIT INT TERM

  grant_access "${base}" "${refresh_secret}"

  step "Registering the account"
  register_account
  hr
  ok "account '${LABEL}' is onboarded for tenant ${TENANT}"
  dim "the broker refreshes it on its next sweep; nothing else to do"
}

# Registration is a Firestore write, and it goes through the same python the
# broker uses so the document shape cannot drift from the code that reads it.
register_account() {
  local lend="${LEND_TO}"
  PROJECT_ID="${PROJECT_ID}" FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-swarm}" \
  SWARM_TENANT="${TENANT}" SWARM_LABEL="${LABEL}" \
  SWARM_PROVIDER="${PROVIDER}" SWARM_LEND_TO="${lend}" \
  ${SWARM_PY} - <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "common"))
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "quota-broker"))
from google.cloud import firestore
from quota_broker.accountstore import AccountStore

db = firestore.Client(
    project=os.environ["PROJECT_ID"],
    database=os.environ.get("FIRESTORE_DATABASE", "swarm"),
)
lend = [t.strip() for t in os.environ.get("SWARM_LEND_TO", "").split(",") if t.strip()]
account = AccountStore(db).register(
    os.environ["SWARM_TENANT"],
    os.environ["SWARM_LABEL"],
    provider=os.environ.get("SWARM_PROVIDER", "anthropic"),
    lend_to=lend,
)
print(f"  registered {account.account_id} (lent to: {', '.join(account.lend_to) or 'nobody'})")
PYEOF
}

cmd_list() {
  PROJECT_ID="${PROJECT_ID}" FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-swarm}" \
  SWARM_TENANT="${TENANT}" ${SWARM_PY} - <<'PYEOF'
import os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "common"))
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "quota-broker"))
from google.cloud import firestore
from quota_broker.accountstore import AccountStore

db = firestore.Client(
    project=os.environ["PROJECT_ID"],
    database=os.environ.get("FIRESTORE_DATABASE", "swarm"),
)
now = datetime.now(timezone.utc)
want = os.environ.get("SWARM_TENANT", "").strip()
accounts = AccountStore(db).list()
if want:
    accounts = [a for a in accounts if a.may_serve(want)]

if not accounts:
    print("  no accounts registered")
    raise SystemExit(0)

print(f"  {'ACCOUNT':28} {'OWNER':12} {'STATE':16} {'HEADROOM':>9}  {'LENT TO':14} RESETS")
for a in sorted(accounts, key=lambda a: (a.owner_tenant, a.label)):
    head = a.headroom(now)
    reset = a.next_reset(now)
    when = "-" if reset is None else reset.strftime("%m-%d %H:%M")
    mark = "!" if head < 0.15 else " "
    print(f"  {a.label:28} {a.owner_tenant:12} {a.state.value:16} "
          f"{head * 100:7.0f}%{mark} {', '.join(a.lend_to) or '-':14} {when}")
    if a.reason:
        print(f"      {a.reason}")
PYEOF
}

cmd_state() {
  local state="$1" reason="$2"
  [[ -n "${TENANT}" ]] || die "requires --tenant"
  [[ -n "${LABEL}" ]]  || die "requires --label"
  validate_tenant "${TENANT}"; validate_label "${LABEL}"
  PROJECT_ID="${PROJECT_ID}" FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-swarm}" \
  SWARM_TENANT="${TENANT}" SWARM_LABEL="${LABEL}" \
  SWARM_STATE="${state}" SWARM_REASON="${reason}" ${SWARM_PY} - <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "common"))
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "quota-broker"))
from google.cloud import firestore
from quota_broker.accounts import AccountState, account_id_for
from quota_broker.accountstore import AccountStore

db = firestore.Client(
    project=os.environ["PROJECT_ID"],
    database=os.environ.get("FIRESTORE_DATABASE", "swarm"),
)
account_id = account_id_for(os.environ["SWARM_TENANT"], os.environ["SWARM_LABEL"])
store = AccountStore(db)
if store.get(account_id) is None:
    raise SystemExit(f"no such account: {account_id}")
store.set_state(account_id, AccountState(os.environ["SWARM_STATE"]), os.environ["SWARM_REASON"])
print(f"  {account_id} -> {os.environ['SWARM_STATE']}")
PYEOF
}

cmd_remove() {
  [[ -n "${TENANT}" ]] || die "remove requires --tenant"
  [[ -n "${LABEL}" ]]  || die "remove requires --label"
  validate_tenant "${TENANT}"; validate_label "${LABEL}"

  confirm_destructive "remove account ${TENANT}/${LABEL}" \
    "This stops the account being used. The SECRET is left in place."

  PROJECT_ID="${PROJECT_ID}" FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-swarm}" \
  SWARM_TENANT="${TENANT}" SWARM_LABEL="${LABEL}" ${SWARM_PY} - <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "common"))
sys.path.insert(0, os.path.join(os.getcwd(), "apps", "quota-broker"))
from google.cloud import firestore
from quota_broker.accounts import account_id_for
from quota_broker.accountstore import AccountStore

db = firestore.Client(
    project=os.environ["PROJECT_ID"],
    database=os.environ.get("FIRESTORE_DATABASE", "swarm"),
)
account_id = account_id_for(os.environ["SWARM_TENANT"], os.environ["SWARM_LABEL"])
AccountStore(db).remove(account_id)
print(f"  removed {account_id}")
PYEOF
  # Deliberately NOT deleting the secret. A credential destroyed by a mistyped
  # label cannot be recovered, and Secret Manager's delayed destruction is what
  # makes that reversible. Say so rather than leaving it to be discovered.
  dim "the secret swarm-account-${TENANT}-${LABEL} was left in place"
  dim "delete it deliberately with: gcloud secrets delete swarm-account-${TENANT}-${LABEL}"
}

# A typed confirmation, and it ignores SWARM_ASSUME_YES by house rule: anything
# destructive is confirmed by a person, not by an environment variable set in a
# CI job three months ago.
confirm_destructive() {
  local what="$1" detail="$2"
  warn "${what}"
  dim "${detail}"
  printf 'Type the account label to confirm: ' >&2
  local typed
  read -r typed
  [[ "${typed}" == "${LABEL}" ]] || die "aborted"
}

case "${COMMAND}" in
  add)    cmd_add ;;
  list)   cmd_list ;;
  pause)  cmd_state PAUSED "paused by an operator" ;;
  resume) cmd_state AVAILABLE "" ;;
  drain)  cmd_state DRAINING "draining: running agents are being moved off" ;;
  remove) cmd_remove ;;
  ""|-h|--help) sed -n '2,34p' "$0" ;;
  *) die "unknown command '${COMMAND}'; try: add list pause resume drain remove" ;;
esac
