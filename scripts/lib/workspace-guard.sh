#!/usr/bin/env bash
# The call guard of the workspace job (docs/workspaces.md §2.5, lane W3 of #847).
#
# WHY THIS EXISTS. A person's workspace is made by one Cloud Build job running
# `scripts/register-tenant.sh --workspace <w-id>` as `swarm-workspace-deployer`.
# That identity holds project-wide account-IAM power Google cannot narrow
# (§2.4): it could set IAM on any account in this shared project, the other
# team's twelve included. What bounds it is this file. Every gcloud, kubectl and
# curl the job runs goes through scripts/lib/guard-bin/, whose shims hand the
# call here, and a call runs only if it matches one shape in
# scripts/lib/workspace-calls.json WHOLE -- every argument and every flag --
# with the values of the one workspace the job is for. Anything else stops the
# run before the call reaches Google or the cluster, and the owner decides.
#
# THE EXPECTATION FILE ($SWARM_CALL_GUARD) is what "this workspace" means. The
# build writes it with `init` from the opaque id alone, so until A1 has read the
# approved record only A1's own reads can match. `expect --record` then fills
# in every name from that record: the tenant id is RE-DERIVED from the
# record's principal by the frozen swarm_common.identity, and a record whose
# tenant id differs is refused, so an edited record cannot point the job at
# someone else's resources. Mode 0600, written whole or not at all.
#
# A REFUSAL exits 86 before the real binary runs, prints the rule and the call
# (through `redact`) to the private build log, and writes the stop reason to
# "$SWARM_CALL_GUARD.stop" for the job to record as needs_owner. The stop file,
# not the exit code, is the signal: 86 is just a number curl and gcloud do not
# use for anything this job meets. After a stop every later call is refused
# too, except what recording needs_owner on the workspace record takes -- so a
# script that swallows a non-zero exit (`|| true`, `if gcloud ...`) cannot
# carry on past the refusal as if the resource were simply absent.
#
# KINDS (docs/design/workspace-kinds.md §4.3, lane WK-JOB). The record's kind
# -- absent is personal -- decides what `expect` fills in. A group's tenant is
# its slug (tenant_id_for_group, never a `u-` id), its forge slot the team slot
# `swarm-tenant-<g>-git` and twin, and its Namespace must carry the job's label
# (namespace_label, C7). A service account's tenant is its personal id, its
# slot the team slot with no twin, its Namespace labelled too, and it alone
# gets the two values rule C10 is made of: the account (service_principal,
# only when it is this project's, not swarm-*, not on the shared deny-list) and
# the API backend (front_door_backend, from workspace-calls.json, never the
# record or the environment). A shape naming a value the expectation does not
# hold matches nothing, so C10 admits nothing for a person or a group.
#
# --report-only is for the OWNER, running the script with their own credentials
# after a stop (§2.5, "What the owner does then"): it prints what it would
# refuse and lets it run. The build file sets SWARM_CALL_GUARD_ENFORCE=1, and
# while that is set report-only is ignored, so the deployer identity is never
# given an unguarded path.
#
# Usage:
#   scripts/lib/workspace-guard.sh init --workspace-id w-3f9a2c
#   scripts/lib/workspace-guard.sh expect --record record.json
#   scripts/lib/workspace-guard.sh check [--report-only] -- TOOL ARGS...
#   scripts/lib/workspace-guard.sh run [--report-only] TOOL ARGS...   (the shims)
#   scripts/lib/workspace-guard.sh self-test [--json] [--case NAME]
#   scripts/lib/workspace-guard.sh self-test --replay PLAN.jsonl      (judges only)
#
# The owner's report-only run, from the repository root:
#   PATH="$PWD/scripts/lib/guard-bin:$PATH" SWARM_CALL_GUARD=<file> \
#     SWARM_CALL_GUARD_MODE=report-only scripts/register-tenant.sh --workspace w-…
set -euo pipefail
# This file's directory by parameter expansion, not dirname: every judged call
# is one start of this file, and each process it starts is paid ~129 times a
# workspace (GUARD-SPEED, #1334). `cd` and `pwd` normalise what is left.
case "${BASH_SOURCE[0]}" in
  */*) GUARD_SELF_DIR="${BASH_SOURCE[0]%/*}"; GUARD_SELF_DIR="${GUARD_SELF_DIR:-/}" ;;
  *)   GUARD_SELF_DIR="." ;;
esac
# self-test judges the fixture cases and touches no project, and says so, so
# common.sh (no default project, DAYONE-DESAGA-2) does not stop on an unset
# PROJECT_ID. Every other subcommand runs inside a workspace build that names
# one.
case "${1:-}" in
  self-test|--self-test) SWARM_NO_PROJECT=1 ;;
esac
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "${GUARD_SELF_DIR}" && pwd)/common.sh"

GUARD_RULES="${SWARM_LIB_DIR}/workspace-calls.json"
GUARD_CASES="${SWARM_LIB_DIR}/workspace-guard-cases.json"
GUARD_BIN_DIR="${SWARM_LIB_DIR}/guard-bin"
# The rules the judge reads. Only the self-test points it elsewhere (a copy with
# the WD9 fallback on and one with it off, to prove C4's bucket-metadata shape
# and C5 both ways whichever the live switch says); it is a shell variable and not an
# environment knob, so nothing outside this process can swap the rules.
GUARD_RULES_ACTIVE="${GUARD_RULES}"
GUARD_REFUSED_RC=86
GUARD_WORKSPACE_RE='^w-[0-9a-f]{6}$'
# A personal tenant id as swarm_common.identity.tenant_id_for_user mints it:
# a personal and a service workspace's (docs/design/workspace-kinds.md §4.1).
GUARD_TENANT_RE='^u-[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
# A group workspace's, as tenant_id_for_group mints it: a slug with no `u-`,
# which is a person's (§2.1: `u-eng@` would derive the id of eng@'s personal
# workspace; swarm-api refuses it as WORKSPACE_GROUP_ID_RESERVED, and so does this).
GUARD_GROUP_TENANT_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'

GUARD_STDIN=""
GUARD_HAS_STDIN=0
GUARD_VERDICT=""
GUARD_RULE=""
GUARD_COMMAND=""
GUARD_REASON=""
GUARD_MANIFEST=""
GUARD_MANIFEST_ERR=""
GUARD_MANIFEST_GIVEN=0

# ---------------------------------------------------------------------------
# The judge. One jq program over (rules, expectation, call). jq because the
# repository's other guards are jq (destroy-guard.jq, iam-plan.jq) and the rules
# are JSON; kept in this file so the guard is one reviewed unit.
#
# It is the ONLY jq a judged call starts. The call's arguments arrive as jq's
# positional arguments, the condition files as --rawfile pairs, the manifest
# reader's output as text and the deny-list as common.sh's lines, and the
# program builds from them the values seven separate jq starts used to (lane
# GUARD-SPEED, #1334). tests/unit/scripts/test_workspace_guard_corpus.py holds
# every verdict to what the guard said before that change, byte for byte.
# ---------------------------------------------------------------------------
# shellcheck disable=SC2016  # a jq program: its $names are jq's, not the shell's
GUARD_JQ='
def has_el($x): any(.[]; . == $x);
def first_of(f): [limit(1; f)] | .[0];
def unresolved: type == "string" and contains("\u0001");

# The deny-list twice, from SHARED_DENY_LIST in common.sh (one entry a line)
# and nowhere else: whole, for naming the cluster of a context, and as
# destroy-guard.jq takes it, for scanning arguments -- guard_deny_json in
# common.sh, which drops the bare `default` and adds `(default)`.
# test_workspace_guard_corpus.py holds this to what guard_deny_json prints,
# entry by entry.
def deny_whole: $deny_list | split("\n");
def deny_scan: [deny_whole[] | select(. != "default")] + ["(default)"];

# "{key}" -> the expectation value. A key the expectation does not hold (yet)
# becomes a byte no real argument carries, so the shape cannot match.
def tpl($e):
  if type == "string" then
    gsub("\\{(?<k>[a-z_]+)\\}"; (($e[.k]) as $v | if ($v | type) == "string" then $v else "\u0001" end))
  else . end;

def mval($v; $e; $c; $rules):
  if . == "*" then true
  elif type == "string" then (tpl($e)) as $t | (($t | unresolved) | not) and $t == $v
  elif type == "array" then any(.[]; mval($v; $e; $c; $rules))
  elif type == "object" and has("rules_list") then (($rules[.rules_list] // []) | has_el($v))
  elif type == "object" and has("file_expression") then
    (.file_expression | tpl($e)) as $t
    | (($t | unresolved) | not)
      and ((($c.files[$v] // null) | if type == "string" then (try fromjson catch null) else null end)
           | (type == "object") and (.expression == $t))
  else false end;

def show($e; $rules):
  if type == "array" then map(show($e; $rules)) | join(" or ")
  elif type == "object" and has("rules_list") then "one of " + (($rules[.rules_list] // []) | join(","))
  elif type == "object" and has("file_expression") then "a condition file whose expression is the one register-tenant.sh renders for this tenant"
  elif type == "string" then (tpl($e) | if unresolved then "<not in the expectation yet>" else . end)
  else tostring end;

# argv -> {pos, fl: {flag: [values]}, bad}. A flag the table does not name is
# a refusal; for finding the command words it is read as a boolean.
def parse($argv; $flags):
  reduce $argv[] as $t ({pos: [], fl: {}, want: null, bad: null, unknown: []};
    if .want != null then .fl[.want] += [$t] | .want = null
    elif ($t | startswith("--")) and (($t | length) > 2) then
      ($t | index("=")) as $i
      | (if $i == null then $t else $t[:$i] end) as $n
      | (if $i == null then null else $t[$i + 1:] end) as $v
      | if ($flags | has($n)) | not then .unknown += [$n]
        elif $flags[$n] == true then
          (if $v == null then .fl[$n] += [true] else .bad = (.bad // ("flag " + $n + " takes no value")) end)
        elif $v != null then .fl[$n] += [$v]
        else .want = $n end
    elif ($t | startswith("-")) and (($t | length) > 1) then
      if ($flags | has($t)) | not then .unknown += [$t]
      elif $flags[$t] == true then .fl[$t] += [true]
      else .want = $t end
    else .pos += [$t] end)
  | if .want != null then .bad = (.bad // ("flag " + .want + " has no value")) else . end
  | if (.unknown | length) > 0 then .bad = (.bad // ("flag " + .unknown[0] + " is not one this call may take")) else . end;

# Every flag any shape of this tool names, so the C9 scan and the command words
# read a flag value as a value and not as a word.
def tool_flags($rules; $tool):
  ($rules.tools[$tool].flags // {})
  + ([$rules.rules[] | (.shapes // [])[] | select(.tool == $tool) | (.flags // {})] | add // {});

def ctx_fail($c; $e; $p):
  ($p.fl["--context"] // []) as $flag
  | (if ($flag | length) > 0 then $flag[-1] else $c.context end) as $ctx
  | if ($flag | length) > 1 then "--context is given more than once"
    elif ($ctx // "") == "" then "no kubectl context is selected"
    elif (deny_whole | has_el($ctx | split("_") | last)) then
      "context \($ctx) is the cluster \($ctx | split("_") | last), which belongs to another team (SHARED_DENY_LIST)"
    elif (($e.contexts // []) | has_el($ctx)) | not then
      "context \($ctx) is not the swarm cluster (\(($e.contexts // []) | join(" or ") | if . == "" then "<not in the expectation yet>" else . end))"
    else empty end;

def obj_fail($ns; $flagns; $rules; $label):
  . as $o
  | ($o.kind // "") as $k
  | if $k == "Namespace" then
      (if $o.name != $ns or ($o.namespace // null) != null then "Namespace \(($o.name // "?") | tojson) is not \($ns)"
       # A group or service namespace is admitted by the deployer scope
       # policy only with the job label (workspace-kinds §4.3, C7).
       elif ($label // null) != null and ($o.workspace_label // null) != $label then
         "Namespace \($ns) does not carry swarm.saga.xyz/workspace: \($label)"
       else empty end)
    elif ($rules.kube_kinds | has_el($k)) | not then "kind \($k | tojson) is not one a tenant namespace holds"
    elif ($o.name // "") == "" then "a \($k) has no name"
    elif (($o.namespace // $flagns) // "") != $ns then
      "\($k) \($o.name) is in namespace \((($o.namespace // $flagns) // "<none>") | tojson), not \($ns)"
    elif $k == "Role" and (($rules.kube_role_names | has_el($o.name)) | not) then
      "Role \($o.name) is not one of \($rules.kube_role_names | join(", "))"
    elif $k == "RoleBinding" and ((($o.role_ref.kind // "") != "Role")
                                  or (($rules.kube_role_names | has_el($o.role_ref.name // "")) | not)) then
      "RoleBinding \($o.name) binds \($o.role_ref.kind // "?") \($o.role_ref.name // "?"), not one of the tenant Roles"
    else empty end;

def objects_fail($c; $e; $rules; $p):
  ($e.namespace // null) as $ns
  | ((($p.fl["-n"] // []) + ($p.fl["--namespace"] // [])) | last) as $flagns
  | if $ns == null then "the expectation names no namespace yet"
    elif ($c.objects | type) == "object" then ($c.objects.error // "the manifest could not be read")
    elif ($c.objects_error // null) != null then $c.objects_error
    elif ($c.objects | type) != "array" or ($c.objects | length) == 0 then "the manifest holds no object the guard could read"
    else (first_of($c.objects[] | obj_fail($ns; $flagns; $rules; $e.namespace_label)) // empty)
    end;

def eval_cli($s; $c; $e; $rules):
  (($rules.tools[$s.tool].flags // {}) + ($s.flags // {})) as $F
  | parse($c.argv; $F) as $p
  | ($s.command | length) as $n
  | if ($p.pos[:$n]) != $s.command then {near: false, ok: false}
    else
      ($p.pos[$n:]) as $args
      | ($s.args // []) as $want
      | first_of(
          (if $p.bad then $p.bad else empty end),
          (if ($args | length) != ($want | length) then
             "\($s.command | join(" ")) takes \($want | length) argument(s) here, and was given \($args | length)"
           else empty end),
          (range($want | length) as $i
             | if ($want[$i] | mval($args[$i]; $e; $c; $rules)) then empty
               else "argument \($args[$i] | tojson) is not \($want[$i] | show($e; $rules))" end),
          ($p.fl | to_entries[] | .key as $k | .value[] as $v
             | ($F[$k]) as $mm
             | if $mm == true then empty
               elif ($mm | mval($v; $e; $c; $rules)) then empty
               else "\($k) \($v | tojson) is not \($mm | show($e; $rules))" end),
          (($s.require // [])[] | select(. as $k | ($p.fl | has($k)) | not) | "\(.) is required"),
          (($s.require_one // []) as $one
             | if ($one | length) > 0 and ((any($one[]; . as $k | $p.fl | has($k))) | not)
               then "one of \($one | join(", ")) is required" else empty end),
          (if $s.tool == "gcloud" and (($rules.tools.gcloud.require_project // []) | has_el($s.command[0]))
              and (($p.fl | has("--project")) | not) then
             "--project is required here: without it the configured project decides where the call lands"
           else empty end),
          (if $s.tool == "kubectl" and (($s.local // false) | not) then ctx_fail($c; $e; $p) else empty end),
          (if ($s.objects // false) then objects_fail($c; $e; $rules; $p) else empty end)
        ) as $why
      | {near: true, ok: ($why == null), reason: $why}
    end;

def curl_method($p):
  (($p.fl["-X"] // []) + ($p.fl["--request"] // [])) as $x
  | if ($x | length) > 0 then $x[-1]
    elif (($p.fl["--data-binary"] // []) | length) > 0 then "POST"
    else "GET" end;

def curl_target($e; $p):
  ($p.pos[0] // "") as $url
  | ($url | split("?")) as $q
  | {url: $url, n: ($p.pos | length), path: ($q[0] // ""), query: ($q[1:] | join("?"))}
  | . + (if ($e.firestore_base // null) != null and (.path | startswith($e.firestore_base)) then
           (.path | ltrimstr($e.firestore_base)) as $rest
           | if ($rest | startswith(":")) then {kind: "op", op: ($rest | ltrimstr(":"))}
             elif ($rest | startswith("/")) then {kind: "doc", doc: ($rest | ltrimstr("/") | gsub("%3[Aa]"; ":"))}
             else {kind: "other"} end
         else {kind: "other"} end);

def body_json($p):
  ($p.fl["--data-binary"] // []) as $d
  | if ($d | length) == 0 then {none: true}
    elif ($d | length) > 1 then {err: "--data-binary is given more than once"}
    elif ($d[0] | startswith("@")) then {err: "--data-binary names a file; the guard judges only a body it can read"}
    else (($d[0] | try fromjson catch null) as $j
          | if ($j | type) == "object" then {json: $j} else {err: "the body is not a JSON object"} end)
    end;

def keys_within($allowed): ((keys - $allowed) | length) == 0;

def query_fail($s; $t):
  if $t.query == "" then empty
  else (first_of($t.query | split("&")[] | select(. != "") | (split("=")[0]) as $k
          | if (($s.query_keys // []) | has_el($k)) then empty
            else "query parameter \($k) is not one this call takes" end) // empty)
  end;

def doc_fail($s; $t; $e):
  ($t.doc // "") as $d
  | ([($s.doc // [])[] | tpl($e) | select(unresolved | not)]) as $docs
  | ($s.doc_prefix // []) as $pre
  | if $d == "" or ($d | test("%|//|\\.\\.|^/|/$")) then "the document path \($d | tojson) is not one the guard can judge"
    elif ($docs | has_el($d)) then query_fail($s; $t)
    elif any($pre[]; . as $x | ($d | startswith($x)) and (($d | ltrimstr($x)) | test("^[^/]+$"))) then query_fail($s; $t)
    else "the document \($d) is not \(($docs + ($pre | map(. + "<id>"))) | join(" or ") | if . == "" then "<not in the expectation yet>" else . end)" end;

def body_fail($s; $b; $e; $rules):
  ($s.body // "none") as $kind
  | if $kind == "none" then (if $b.none then empty else "this call takes no body" end)
    elif $b.err then $b.err
    elif $b.none then (if $kind == "transaction_options" then empty else "this call needs a body" end)
    else ($b.json) as $j
    | if $kind == "document" then
        (if ($j | keys_within(["fields", "name"])) then empty else "a document body may hold only its fields" end)
      elif $kind == "transaction_options" then
        (if ($j | keys_within(["options"])) then empty else "beginTransaction takes only options" end)
      elif $kind == "transaction_id" then
        (if ($j | keys_within(["transaction"])) then empty else "rollback takes only a transaction" end)
      elif $kind == "marker" then
        # The .tenant marker of A5: the tenant and the workspace it is for, nothing else.
        (if $j == {tenant_id: ($e.tenant_id // null), workspace_id: ($e.workspace_id // null)} then empty
         else "the marker names \($j | tojson), not the tenant and workspace this job is for" end)
      elif $kind == "runquery_workspace" then
        if ($j | keys_within(["structuredQuery", "transaction", "newTransaction", "readTime"])) | not then
          "runQuery carries a field the guard does not judge"
        elif ($j.structuredQuery | type) != "object" or (($j.structuredQuery | keys_within(["from", "where", "limit", "select"])) | not) then
          "a runQuery on workspaces may only filter, limit and select"
        elif $j.structuredQuery.from != [{collectionId: "workspaces"}] then
          "runQuery reads \($j.structuredQuery.from | tojson), not workspaces"
        elif ($e.workspace_id // null) == null then "the expectation names no workspace"
        elif $j.structuredQuery.where != {fieldFilter: {field: {fieldPath: "workspace_id"}, op: "EQUAL", value: {stringValue: $e.workspace_id}}} then
          "the runQuery on workspaces is not filtered to workspace_id == \($e.workspace_id)"
        else empty end
      elif $kind == "runquery_collection" then
        if ($j | keys_within(["structuredQuery", "transaction", "newTransaction", "readTime"])) | not then
          "runQuery carries a field the guard does not judge"
        elif ($j.structuredQuery | type) != "object" then "runQuery has no structuredQuery"
        elif ([($s.collections // [])[] | [{collectionId: .}]] | has_el($j.structuredQuery.from)) | not then
          "runQuery reads \($j.structuredQuery.from | tojson), not \(($s.collections // []) | join(" or "))"
        else empty end
      elif $kind == "commit" then
        ([($s.docs // [])[] | tpl($e) | select(unresolved | not) | (($e.firestore_docs_prefix // "\u0001") + "/" + .)]) as $allowed
        | if ($j | keys_within(["writes", "transaction"])) | not then "a commit carries a field the guard does not judge"
          elif ($j.writes | type) != "array" or ($j.writes | length) == 0 then "a commit with no writes"
          else (first_of($j.writes[]
                 | if type != "object" then "a write is not an object"
                   elif (keys_within(["update", "updateMask", "currentDocument", "updateTransforms"])) | not then
                     "a write carries \((keys - ["update", "updateMask", "currentDocument", "updateTransforms"]) | join(", ")), and only an update of a named document may run"
                   elif ((.update.name // "") as $n | $allowed | has_el($n)) | not then
                     "a write updates \((.update.name // "?") | split("/documents/") | last), not \(($allowed | map(split("/documents/") | last)) | join(" or ") | if . == "" then "<not in the expectation yet>" else . end)"
                   else empty end) // empty)
          end
      else "unknown body kind \($kind)" end
    end;

def eval_curl($s; $c; $e; $rules):
  parse($c.argv; $rules.tools.curl.flags) as $p
  | curl_target($e; $p) as $t
  | curl_method($p) as $m
  | ($m == $s.method
     and (if $s.url then ($t.kind == "other" and $t.path == ($s.url | tpl($e) | split("?") | .[0]))
          elif $s.op then ($t.kind == "op" and $t.op == $s.op)
          elif $s.doc then $t.kind == "doc"
          else false end)) as $near
  | if ($near | not) then {near: false, ok: false}
    else first_of(
        (if $p.bad then $p.bad else empty end),
        (if $t.n != 1 then "curl names \($t.n) URLs here; the guard judges exactly one" else empty end),
        ((($p.fl["-H"] // []) + ($p.fl["--header"] // []))[] as $h
           | if (($s.headers // $rules.tools.curl.default_headers) | has_el($h)) then empty
             else "header \($h | split(":")[0] | tojson) is not one this call sends" end),
        ((($p.fl["-K"] // []) + ($p.fl["--config"] // [])) as $k
           | if $s.url and (($s.auth // false) | not) then (if ($k | length) > 0 then "the metadata server is asked with no curl config" else empty end)
             elif ($k | length) != 1 or $k[0] != "-" then "an authenticated call reads its bearer header from -K -, exactly once"
             elif $c.config != "ok" then "the curl config on stdin holds a line other than the one Authorization header"
             else empty end),
        (if $s.url then (if $t.url == ($s.url | tpl($e)) then empty else "the URL is not \($s.url)" end)
         elif $s.op then (if $t.query == "" then empty else "\($s.op) takes no query string" end)
         else doc_fail($s; $t; $e) end),
        body_fail($s; body_json($p); $e; $rules)
      ) as $why
    | {near: true, ok: ($why == null), reason: $why}
    end;

def never($c; $rules):
  ([$rules.rules[] | select(.id == "C9") | .never] | .[0]) as $n
  | parse($c.argv; tool_flags($rules; $c.tool)) as $p
  | if $c.tool == "gcloud" then
      first_of(
        ($p.pos[] | select(. as $w | $n.gcloud_words | has_el($w)) | "gcloud \(.) is never run by the workspace job"),
        ($n.gcloud_sequences[] as $sq | range(0; ($p.pos | length)) as $i
           | select($p.pos[$i:($i + ($sq | length))] == $sq) | "gcloud \($sq | join(" ")) is never run by the workspace job"))
    elif $c.tool == "kubectl" then
      first_of(
        (($p.pos[0] // "") | select(. as $w | $n.kubectl_verbs | has_el($w)) | "kubectl \(.) is never run by the workspace job"),
        ($c.argv[] | select(. as $a | any($n.kubectl_flags[]; $a == . or ($a | startswith(. + "="))))
           | "kubectl \(.) is never run by the workspace job"))
    elif $c.tool == "curl" then
      first_of(
        (curl_method($p) | select(. as $m | $n.curl_methods | has_el($m)) | "an HTTP \(.) is never sent by the workspace job"),
        (body_json($p) | (.json // {}) | (.writes // []) | if type == "array" then .[] else empty end
           | select(type == "object" and (has("delete") or has("transform")))
           | "a commit that deletes or transforms a document is never sent by the workspace job"))
    else null end;

def deny_hit($c):
  first_of($c.argv[] as $a | deny_scan[] as $d
    | if ($d | test("^[A-Za-z0-9.@-]+$")) then
        (if any(($a | [splits("[^A-Za-z0-9.@-]+")])[]; . == $d) then $d else empty end)
      else (if ($a | contains($d)) then $d else empty end) end);

def command_words($c; $rules):
  parse($c.argv; tool_flags($rules; $c.tool)) as $p
  | if $c.tool == "curl" then curl_method($p) + " " + (curl_target($e_in[0]; $p) | if .kind == "op" then ":" + .op elif .kind == "doc" then "document" else "url" end)
    else ($p.pos[:3] | join(" ")) end;

($rules_in[0]) as $rules
| ($e_in[0]) as $e
| $ARGS.positional as $argv
# {path: content} for every --condition-from-file, in argument order.
| (reduce range($ncond) as $i ({}; . + {($ARGS.named["cp\($i)"]): $ARGS.named["cf\($i)"]})) as $files
# What the manifest reader printed, read as JSON; output that is not a JSON
# value, or is null or false, is a manifest that could not be read.
| (if $objects_read then
     (($objects_text | try fromjson catch null)) as $o
     | if $o == null or $o == false then {o: null, err: "the manifest could not be read"} else {o: $o, err: $objects_error} end
   else {o: null, err: $objects_error} end) as $read
| $read.o as $objects
| $read.err as $objects_error
| {tool: $tool, argv: $argv, files: $files, objects: $objects,
   objects_error: (if $objects_error == "" then null else $objects_error end),
   context: $context, config: $config, stopped: $stopped} as $c
| ($rules.wd9_fallback == true) as $fallback
| command_words($c; $rules) as $words
| never($c; $rules) as $nv
| (if $nv != null then {verdict: "refuse", rule: "C9", reason: $nv}
   else
     deny_hit($c) as $dh
     | if $dh != null then
         {verdict: "refuse", rule: "C0", reason: "an argument names \($dh), which belongs to another team (SHARED_DENY_LIST in scripts/lib/common.sh)"}
       elif $c.tool == "gcloud" and $env_impersonate != "" then
         {verdict: "refuse", rule: "C0", reason: "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT is set, so gcloud would act as an identity no argument shows"}
       elif $c.tool == "gcloud" and $env_project != "" and $env_project != ($e.project_id // "") then
         {verdict: "refuse", rule: "C0", reason: "CLOUDSDK_CORE_PROJECT names \($env_project), not \($e.project_id // "<not in the expectation yet>")"}
       else
         [ $rules.rules[] | .id as $id | (.shapes // [])[] | select(.tool == $c.tool) | . as $s
           | (if $s.tool == "curl" then eval_curl($s; $c; $e; $rules) else eval_cli($s; $c; $e; $rules) end)
           | . + {rule: $id,
                  unusable: (if ($s.fallback_only // false) and ($fallback | not) then
                               "the WD9 fallback is off, so this grant is not the job to make: every personal worker holds it once, through the principal-set grant (docs/workspaces.md §2.3)"
                             elif $c.stopped and (($s.after_stop // false) | not) then
                               "an earlier call was refused, so only recording needs_owner on the workspace record may still run"
                             else null end)} ] as $ev
         | ([$ev[] | select(.ok and .unusable == null)] | .[0]) as $win
         | if $win != null then {verdict: "allow", rule: $win.rule, reason: ""}
           else ((([$ev[] | select(.near and .ok)] | .[0]) // ([$ev[] | select(.near)] | .[0])) as $nr
                 | if $nr != null then {verdict: "refuse", rule: $nr.rule, reason: (if $nr.ok then $nr.unusable else $nr.reason end)}
                   else {verdict: "refuse", rule: "C0", reason: "no rule allows \($c.tool) \($words)"} end)
           end
       end
   end)
| if .verdict == "refuse" and ($e.phase // "") == "bootstrap" then
    .reason += " (before A1 writes the expectation, only its own reads may run)" else . end
| [.verdict, .rule, $words, .reason] | @tsv
'

# Pulls kind, metadata.name, metadata.namespace and roleRef out of a manifest,
# with the standard library only (the job image is not promised PyYAML). It
# reads block-style YAML of the shape kubernetes/render.py writes, or JSON, and
# REFUSES what it cannot read with certainty -- flow mappings, anchors, aliases,
# tags, a quoted value spanning lines, a key twice, a List -- because a reader
# that guesses is a reader a manifest can fool. The scope admission policy of
# docs/workspaces.md §2.3 checks the same objects server-side.
GUARD_MANIFEST_PY='
import json
import re
import sys

text = sys.stdin.read()


def out(value):
    sys.stdout.write(json.dumps(value))
    sys.exit(0)


def refuse(message):
    out({"error": message})


NAME = re.compile(r"^[A-Za-z0-9._:-]*$")
KEY = re.compile(r"^( *)([A-Za-z][A-Za-z0-9_.-]*):(?:[ \t]+(.*))?$")
QUOTES = "\"\x27"


def plain(value):
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if len(value) >= 2 and value[0] in QUOTES and value[-1] == value[0]:
        value = value[1:-1]
    if not NAME.match(value):
        refuse("a manifest name or kind is not a plain value: " + json.dumps(value[:60]))
    return value


def child_values(entry, what):
    inline, lines = entry
    if inline.strip() and not inline.strip().startswith("#"):
        refuse(what + " is written inline; the guard reads it only as a block")
    found = {}
    if not lines:
        return found
    indent = min(len(line) - len(line.lstrip(" ")) for line in lines)
    for line in lines:
        if len(line) - len(line.lstrip(" ")) != indent:
            continue
        match = KEY.match(line.rstrip())
        if not match:
            refuse(what + " holds a line the guard cannot read: " + json.dumps(line.strip()[:60]))
        key = match.group(2)
        if key in found:
            refuse(what + "." + key + " appears twice")
        found[key] = match.group(3) or ""
    return {k: plain(v) for k, v in found.items() if k in ("name", "namespace", "kind")}


LABEL = "swarm.saga.xyz/workspace"
LABEL_LINE = re.compile(r"^( +)([A-Za-z0-9][A-Za-z0-9_./-]*):(?:[ \t]+(.*))?$")


def block_label(entry):
    """The workspace label of a block-style metadata, or None. Read from the
    labels block one level under metadata only; a label key twice is
    refused, as any key twice is."""
    inline, lines = entry
    found = None
    indent = min((len(l) - len(l.lstrip(" ")) for l in lines), default=0)
    in_labels = False
    label_indent = None
    for line in lines:
        depth = len(line) - len(line.lstrip(" "))
        if depth == indent:
            in_labels = line.strip().startswith("labels:")
            label_indent = None
            continue
        if not in_labels:
            continue
        if label_indent is None:
            label_indent = depth
        if depth != label_indent:
            continue
        match = LABEL_LINE.match(line.rstrip())
        if match and match.group(2) == LABEL:
            if found is not None:
                refuse("metadata.labels." + LABEL + " appears twice")
            found = plain(match.group(3) or "")
    return found


def obj(kind, meta, role_ref, label=None):
    item = {"kind": kind, "name": meta.get("name"), "namespace": meta.get("namespace"),
            "workspace_label": label}
    if role_ref is not None:
        item["role_ref"] = {"kind": role_ref.get("kind"), "name": role_ref.get("name")}
    return item


stripped = text.strip()
if stripped.startswith("{") or stripped.startswith("["):
    try:
        data = json.loads(stripped)
    except ValueError:
        refuse("the manifest is not valid JSON")
    objects = []
    for doc in data if isinstance(data, list) else [data]:
        if not isinstance(doc, dict):
            refuse("a manifest document is not an object")
        if "items" in doc or str(doc.get("kind", "")).endswith("List"):
            refuse("a List is refused: the guard judges objects one by one")
        meta = doc.get("metadata")
        role_ref = doc.get("roleRef") if "roleRef" in doc else None
        if not isinstance(meta, dict) or (role_ref is not None and not isinstance(role_ref, dict)):
            refuse("an object has no readable metadata")
        values = [doc.get("kind"), meta.get("name"), meta.get("namespace")]
        if role_ref is not None:
            values += [role_ref.get("kind"), role_ref.get("name")]
        if not all(v is None or (isinstance(v, str) and NAME.match(v)) for v in values):
            refuse("a manifest name or kind is not a plain value")
        labels = meta.get("labels")
        label = labels.get(LABEL) if isinstance(labels, dict) else None
        if label is not None and not (isinstance(label, str) and NAME.match(label)):
            refuse("a manifest label is not a plain value")
        objects.append(obj(doc.get("kind"), meta, role_ref, label))
    out(objects)

objects = []
for doc in re.split(r"(?m)^---[ \t]*$", text):
    top = {}
    current = None
    for raw in doc.split("\n"):
        bare = raw.strip()
        if not bare or bare.startswith("#"):
            continue
        lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in lead:
            refuse("a manifest line is indented with a tab")
        if re.search(r"(^|[\s\[{,])[&*!][^\s,\]}]", raw):
            refuse("YAML anchors, aliases and tags are refused")
        match = KEY.match(raw.rstrip())
        value = (match.group(3) or "") if match else ""
        if value and value[0] in QUOTES:
            closed = value.split(" #", 1)[0].rstrip()
            if len(closed) < 2 or closed[-1] != closed[0]:
                refuse("a quoted manifest value spans lines")
        if not lead:
            if not match:
                refuse("a top-level manifest line is not a plain key: " + json.dumps(bare[:60]))
            key = match.group(2)
            if key in top:
                refuse("the key " + key + " appears twice in one document")
            top[key] = (value, [])
            current = key
        else:
            if current is None:
                refuse("a manifest document starts indented")
            top[current][1].append(raw)
    if not top:
        continue
    if "items" in top:
        refuse("a List is refused: the guard judges objects one by one")
    if "kind" not in top or top["kind"][1]:
        refuse("a manifest document has no plain kind")
    kind = plain(top["kind"][0])
    meta = child_values(top["metadata"], "metadata") if "metadata" in top else {}
    role_ref = child_values(top["roleRef"], "roleRef") if "roleRef" in top else None
    label = block_label(top["metadata"]) if "metadata" in top else None
    objects.append(obj(kind, meta, role_ref, label))
out(objects)
'

# Derives the names from the record's principal with the FROZEN contract
# (swarm_common.identity), never a shell restatement of it. The forge slot
# suffix is swarm_api.gittokens.provider_suffix for a user: `git-u-` and 16 hex
# of sha256 of the trimmed, lower-cased email.
# tests/unit/scripts/test_workspace_guard.py holds this to that function,
# because importing swarm_api here would need its dependencies in the job image.
GUARD_DERIVE_PY='
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.identity import tenant_id_for_group, tenant_id_for_user, worker_service_account_id

principal = sys.argv[2]
kind = sys.argv[3] if len(sys.argv) > 3 else "personal"
# A group is filed under its own slug; a person and a service account under
# their personal id (docs/design/workspace-kinds.md §4.1). A person gets their
# App slot; a group and a service the team slot, git (gittokens.Scope.TENANT).
tenant = tenant_id_for_group(principal) if kind == "group" else tenant_id_for_user(principal)
digest = hashlib.sha256(principal.strip().lower().encode("utf-8")).hexdigest()[:16]
print(json.dumps({
    "tenant_id": tenant,
    "worker_id": worker_service_account_id(tenant),
    "forge_suffix": "git-u-" + digest if kind == "personal" else "git",
}))
'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

guard_expectation_path() {
  [[ -n "${SWARM_CALL_GUARD:-}" ]] \
    || die "SWARM_CALL_GUARD is not set; it must name the guard's expectation file"
  printf '%s' "${SWARM_CALL_GUARD}"
}

# True when someone other than the owner could rewrite the file.
guard_file_is_loose() {
  local perms
  perms="$(ls -ld -- "$1")"
  perms="${perms%% *}"
  [[ "${perms:5:1}" == "w" || "${perms:8:1}" == "w" ]]
}

# Writes stdin to FILE, mode 0600, whole or not at all.
guard_write_private() {
  local file="$1" tmp
  tmp="${file}.tmp.$$"
  (umask 077 && cat >"${tmp}")
  chmod 600 "${tmp}"
  mv -f -- "${tmp}" "${file}"
}

# The kubeconfig's current-context, read from the file rather than asked of
# kubectl, so judging a call never runs one. kubectl takes it from the first
# file in $KUBECONFIG that sets it.
#
# Read by the shell, line by line, rather than `sed | sed | tr` (three starts a
# kubectl call, GUARD-SPEED): the first line that starts `current-context:`,
# what follows it with the leading whitespace dropped, then every quote YAML may
# put around the name and every whitespace byte deleted. No context name
# carries either. Sets GUARD_CONTEXT.
GUARD_CONTEXT=""
guard_current_context() {
  local files="${KUBECONFIG:-${HOME}/.kube/config}" file line ctx drop
  local -a list=()
  GUARD_CONTEXT=""
  IFS=':' read -r -a list <<<"${files}"
  for file in ${list[@]+"${list[@]}"}; do
    [[ -n "${file}" && -f "${file}" && -r "${file}" ]] || continue
    ctx=""
    while IFS= read -r line || [[ -n "${line}" ]]; do
      [[ "${line}" == current-context:* ]] || continue
      ctx="${line#current-context:}"
      ctx="${ctx#"${ctx%%[![:space:]]*}"}"
      for drop in '"' "'" ' ' $'\t' $'\n' $'\v' $'\f' $'\r'; do
        ctx="${ctx//"${drop}"/}"
      done
      break
    done <"${file}"
    if [[ -n "${ctx}" ]]; then
      GUARD_CONTEXT="${ctx}"
      return 0
    fi
  done
  return 0
}

# Does this call read stdin the guard must judge? kubectl's manifest (`-f -`)
# and curl's config (`-K -`). Anything else keeps its stdin untouched.
guard_wants_stdin() {
  local tool="$1"; shift
  local prev="" arg
  for arg in "$@"; do
    case "${tool}" in
      kubectl)
        if [[ ( "${prev}" == "-f" || "${prev}" == "--filename" ) && "${arg}" == "-" ]] \
           || [[ "${arg}" == "--filename=-" || "${arg}" == "-f=-" ]]; then
          return 0
        fi ;;
      curl)
        if [[ ( "${prev}" == "-K" || "${prev}" == "--config" ) && "${arg}" == "-" ]] \
           || [[ "${arg}" == "--config=-" ]]; then
          return 0
        fi ;;
    esac
    prev="${arg}"
  done
  return 1
}

# Into memory, never a file: curl's config carries the bearer token. The `x`
# keeps trailing newlines that $( ) would strip.
guard_read_stdin() {
  GUARD_STDIN="$(cat; printf x)"
  GUARD_STDIN="${GUARD_STDIN%x}"
  GUARD_HAS_STDIN=1
}

# The one line fs_request writes, and nothing else. Its shape is asked of
# common.sh's auth_config itself, around a placeholder, rather than restated
# here: the producer and the judge cannot drift. The token is compared by [[ ]]
# in this shell; it is never an argument to a process, so it never reaches a
# /proc cmdline.
guard_curl_config_ok() {
  local line seen=0 shape head tail middle
  local placeholder="@SWARM-GUARD-TOKEN@"
  local token_re='^[A-Za-z0-9._~+/=-]+$'
  shape="$(auth_config "${placeholder}")"
  head="${shape%%"${placeholder}"*}"
  tail="${shape#*"${placeholder}"}"
  [[ "${GUARD_HAS_STDIN}" -eq 1 && "${head}" != "${shape}" ]] || return 1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -n "${line}" ]] || continue
    if [[ "${line}" == "${head}"*"${tail}" ]]; then
      middle="${line#"${head}"}"
      middle="${middle%"${tail}"}"
      if [[ "${middle}" =~ ${token_re} ]]; then
        seen=$((seen + 1))
        continue
      fi
    fi
    return 1
  done <<<"${GUARD_STDIN}"
  [[ "${seen}" -eq 1 ]]
}

# Every readable --condition-from-file, as jq arguments the judge turns into
# {path: content}, so C4 can compare the condition's expression with the one
# this tenant's grant must carry: `--arg cpN PATH --rawfile cfN PATH` each, in
# GUARD_COND_ARGS, and their count in GUARD_COND_N. The judge's own jq reads
# the files; no jq of its own a file (GUARD-SPEED).
GUARD_COND_N=0
GUARD_COND_ARGS=()
guard_condition_files() {
  local next=0 arg path
  GUARD_COND_N=0
  GUARD_COND_ARGS=()
  for arg in "$@"; do
    path=""
    if [[ "${next}" -eq 1 ]]; then
      path="${arg}"; next=0
    elif [[ "${arg}" == "--condition-from-file" ]]; then
      next=1; continue
    elif [[ "${arg}" == --condition-from-file=* ]]; then
      path="${arg#--condition-from-file=}"
    else
      continue
    fi
    if [[ -f "${path}" && -r "${path}" ]]; then
      GUARD_COND_ARGS+=(--arg "cp${GUARD_COND_N}" "${path}" --rawfile "cf${GUARD_COND_N}" "${path}")
      GUARD_COND_N=$((GUARD_COND_N + 1))
    fi
  done
}

# Every manifest a kubectl call names, concatenated: -f FILE, -f - (stdin).
# A directory or a URL is refused rather than read: kubectl applies a
# directory's every file, and the guard would be judging a different set.
guard_kubectl_manifest() {
  local next=0 arg value content
  GUARD_MANIFEST=""; GUARD_MANIFEST_ERR=""; GUARD_MANIFEST_GIVEN=0
  for arg in "$@"; do
    value=""
    if [[ "${next}" -eq 1 ]]; then
      value="${arg}"; next=0
    elif [[ "${arg}" == "-f" || "${arg}" == "--filename" ]]; then
      next=1; continue
    elif [[ "${arg}" == --filename=* ]]; then
      value="${arg#--filename=}"
    elif [[ "${arg}" == -f=* ]]; then
      value="${arg#-f=}"
    else
      continue
    fi
    GUARD_MANIFEST_GIVEN=1
    if [[ "${value}" == "-" ]]; then
      if [[ "${GUARD_HAS_STDIN}" -ne 1 ]]; then
        GUARD_MANIFEST_ERR="the manifest on stdin was not captured, so it cannot be judged"
        continue
      fi
      GUARD_MANIFEST+="${GUARD_STDIN}"$'\n---\n'
    elif [[ "${value}" == *://* ]]; then
      GUARD_MANIFEST_ERR="a manifest named by URL is refused: the guard judges only what it reads"
    elif [[ -f "${value}" && -r "${value}" ]]; then
      content="$(<"${value}")"
      GUARD_MANIFEST+="${content}"$'\n---\n'
    else
      GUARD_MANIFEST_ERR="-f ${value} is not a readable file (a directory is applied whole), so it is refused"
    fi
  done
}

# dirname(1) and basename(1), by parameter expansion: a dirname was a process
# start, seven of them a judged call (GUARD-SPEED). Same answers as coreutils,
# trailing and repeated slashes included; test_workspace_guard_corpus.py holds
# them to it. Set GUARD_DIRNAME / GUARD_BASENAME.
guard_dirname() {
  local p="$1"
  while [[ "${p}" == */ && "${p}" != / ]]; do p="${p%/}"; done
  case "${p}" in
    */*) p="${p%/*}"
         while [[ "${p}" == */ ]]; do p="${p%/}"; done
         GUARD_DIRNAME="${p:-/}" ;;
    *)   GUARD_DIRNAME="." ;;
  esac
}
guard_basename() {
  local p="$1"
  while [[ "${p}" == */ && "${p}" != / ]]; do p="${p%/}"; done
  if [[ "${p}" == / ]]; then GUARD_BASENAME="/"; else GUARD_BASENAME="${p##*/}"; fi
}

# Resolves symlinks by hand: macOS readlink has no -f. Sets GUARD_RESOLVED_DIR
# (physical, from pwd -P) and GUARD_RESOLVED (that directory, a slash, and the
# name), so a caller compares the directory without a dirname of its own.
guard_resolve() {
  local path="$1" link
  while [[ -L "${path}" ]]; do
    link="$(readlink "${path}")"
    case "${link}" in
      /*) path="${link}" ;;
      *)  guard_dirname "${path}"; path="${GUARD_DIRNAME}/${link}" ;;
    esac
  done
  guard_dirname "${path}"
  guard_basename "${path}"
  GUARD_RESOLVED_DIR="$(cd -- "${GUARD_DIRNAME}" && pwd -P)"
  GUARD_RESOLVED="${GUARD_RESOLVED_DIR}/${GUARD_BASENAME}"
}

# The real binary, by absolute path, never a shim: the first TOOL on $PATH
# outside guard-bin/ (kubectl: $SWARM_KUBECTL first, as kubectl_bin reads it).
# Sets GUARD_REAL_BIN; returns 1 when there is none.
guard_real_bin() {
  local tool="$1" guard_dir dir cand
  GUARD_REAL_BIN=""
  guard_dir="$(cd -- "${GUARD_BIN_DIR}" && pwd -P)"
  local -a dirs=()
  if [[ "${tool}" == "kubectl" && "${SWARM_KUBECTL:-}" == /* && -x "${SWARM_KUBECTL}" ]]; then
    guard_resolve "${SWARM_KUBECTL}"
    if [[ "${GUARD_RESOLVED_DIR}" != "${guard_dir}" ]]; then
      GUARD_REAL_BIN="${GUARD_RESOLVED}"
      return 0
    fi
  fi
  IFS=':' read -r -a dirs <<<"${PATH}"
  for dir in ${dirs[@]+"${dirs[@]}"}; do
    [[ "${dir}" == /* && -d "${dir}" ]] || continue
    cand="${dir}/${tool}"
    [[ -f "${cand}" && -x "${cand}" ]] || continue
    guard_resolve "${cand}"
    [[ "${GUARD_RESOLVED_DIR}" == "${guard_dir}" ]] && continue
    GUARD_REAL_BIN="${GUARD_RESOLVED}"
    return 0
  done
  return 1
}

# The deny-list from common.sh and nowhere else, one entry a line, for the
# judge to read whole and as destroy-guard.jq takes it (deny_whole and
# deny_scan in GUARD_JQ). Joined by the shell: the four jq and the grep the
# two JSON forms took are gone (GUARD-SPEED).
GUARD_DENY_LINES=""
guard_deny_lists() {
  local IFS=$'\n'
  GUARD_DENY_LINES="${SHARED_DENY_LIST[*]+${SHARED_DENY_LIST[*]}}"
}

# ---------------------------------------------------------------------------
# guard_judge TOOL ARGS...  ->  GUARD_VERDICT (allow|refuse), GUARD_RULE,
# GUARD_COMMAND, GUARD_REASON. Never exits: every failure to judge is a refusal.
# ---------------------------------------------------------------------------
guard_judge() {
  local tool="${1:-}"
  [[ $# -gt 0 ]] && shift
  GUARD_VERDICT="refuse"; GUARD_RULE="C0"; GUARD_COMMAND="${tool}"; GUARD_REASON=""

  local exp="${SWARM_CALL_GUARD:-}"
  if [[ -z "${exp}" ]]; then
    GUARD_REASON="SWARM_CALL_GUARD names no expectation file, and nothing runs unguarded"
    return 0
  fi
  if [[ ! -f "${exp}" || ! -r "${exp}" ]]; then
    GUARD_REASON="the expectation file ${exp} is not a readable file"
    return 0
  fi
  if guard_file_is_loose "${exp}"; then
    GUARD_REASON="the expectation file ${exp} is writable by others; it must be mode 0600"
    return 0
  fi
  case "${tool}" in
    gcloud|kubectl|curl) ;;
    *) GUARD_REASON="the guard wraps gcloud, kubectl and curl, not '${tool}'"; return 0 ;;
  esac

  # The arguments go to the judge as jq's positional arguments, after --args
  # --, so an argument that looks like a jq option is still only an argument.
  local objects_text="" objects_read=false objects_err="" context="" config="none"
  GUARD_COND_N=0
  GUARD_COND_ARGS=()

  case "${tool}" in
    gcloud)
      guard_condition_files "$@" ;;
    kubectl)
      guard_current_context
      context="${GUARD_CONTEXT}"
      guard_kubectl_manifest "$@"
      if [[ "${GUARD_MANIFEST_GIVEN}" -eq 1 ]]; then
        if [[ -n "${GUARD_MANIFEST_ERR}" ]]; then
          objects_err="${GUARD_MANIFEST_ERR}"
        elif objects_text="$(printf '%s' "${GUARD_MANIFEST}" | python3 -c "${GUARD_MANIFEST_PY}")"; then
          # The judge reads it as JSON; what is not JSON, null or false is "the
          # manifest could not be read", as `jq -e .` used to decide here.
          objects_read=true
        else
          objects_text=""
          objects_err="the manifest could not be read"
        fi
      fi ;;
    curl)
      if guard_wants_stdin curl "$@"; then
        if guard_curl_config_ok; then config="ok"; else config="bad"; fi
      fi ;;
  esac

  local stopped=false result
  [[ -e "${exp}.stop" ]] && stopped=true
  guard_deny_lists
  if ! result="$(jq -rn \
        --slurpfile rules_in "${GUARD_RULES_ACTIVE}" \
        --slurpfile e_in "${exp}" \
        --arg tool "${tool}" \
        ${GUARD_COND_ARGS[@]+"${GUARD_COND_ARGS[@]}"} \
        --argjson ncond "${GUARD_COND_N}" \
        --arg objects_text "${objects_text}" \
        --argjson objects_read "${objects_read}" \
        --arg objects_error "${objects_err}" \
        --arg context "${context}" \
        --arg config "${config}" \
        --argjson stopped "${stopped}" \
        --arg env_impersonate "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:-}" \
        --arg env_project "${CLOUDSDK_CORE_PROJECT:-}" \
        --arg deny_list "${GUARD_DENY_LINES}" \
        "${GUARD_JQ}" --args -- "$@" 2>&1)"; then
    GUARD_REASON="the guard could not judge this call: $(printf '%s' "${result}" | sed -n '1p')"
    return 0
  fi
  IFS=$'\t' read -r GUARD_VERDICT GUARD_RULE GUARD_COMMAND GUARD_REASON <<<"${result}"
  [[ "${GUARD_VERDICT}" == "allow" ]] || GUARD_VERDICT="refuse"
}

# Prints the refusal to the private build log, through redact, and (unless
# report-only) writes the stop reason the job records as needs_owner. The first
# refusal's file is kept: it names the call that actually stopped the run.
guard_report_refusal() {
  local report_only="$1" tool="$2"; shift 2
  local call
  call="${tool}"
  [[ $# -eq 0 ]] || call+="$(printf ' %q' "$@")"
  if [[ "${report_only}" -eq 1 ]]; then
    printf 'workspace-guard: REPORT-ONLY, would refuse (rule %s): %s\n  call: %s\n' \
      "${GUARD_RULE}" "${GUARD_REASON}" "${call}" | redact >&2
    return 0
  fi
  printf 'workspace-guard: REFUSED (rule %s): %s\n  call: %s\n  nothing this call would have done has happened; the job records needs_owner.\n' \
    "${GUARD_RULE}" "${GUARD_REASON}" "${call}" | redact >&2
  local stop="${SWARM_CALL_GUARD:-}"
  [[ -n "${stop}" ]] || return 0
  stop="${stop}.stop"
  [[ -e "${stop}" ]] && return 0
  local workspace=""
  if [[ -r "${SWARM_CALL_GUARD}" ]]; then
    workspace="$(jq -r '.workspace_id // ""' "${SWARM_CALL_GUARD}" 2>/dev/null || true)"
  fi
  jq -n --arg rule "${GUARD_RULE}" --arg reason "${GUARD_REASON}" --arg tool "${tool}" \
        --arg command "${GUARD_COMMAND}" --arg w "${workspace}" --arg at "$(iso_now)" \
        '{state: "needs_owner", rule: $rule, reason: $reason, tool: $tool,
          command: $command, workspace_id: $w, at: $at}' \
    | guard_write_private "${stop}" \
    || err "could not write the stop file ${stop}; the refusal above still stands"
}

# Sets GUARD_REPORT_ONLY (0 or 1). A variable, not $( ): no subshell a call.
GUARD_REPORT_ONLY=0
guard_report_only_wanted() {
  local flag="$1"
  local want=0
  [[ "${flag}" -eq 1 || "${SWARM_CALL_GUARD_MODE:-}" == "report-only" ]] && want=1
  if [[ "${want}" -eq 1 && "${SWARM_CALL_GUARD_ENFORCE:-}" == "1" ]]; then
    warn "workspace-guard: report-only was asked for under SWARM_CALL_GUARD_ENFORCE=1; enforcing"
    want=0
  fi
  GUARD_REPORT_ONLY="${want}"
}

# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

cmd_run() {
  local flag=0
  if [[ "${1:-}" == "--report-only" ]]; then flag=1; shift; fi
  [[ $# -ge 1 ]] || die "usage: workspace-guard.sh run [--report-only] TOOL ARGS..."
  local report_only tool="$1"; shift
  guard_report_only_wanted "${flag}"
  report_only="${GUARD_REPORT_ONLY}"
  if guard_wants_stdin "${tool}" "$@"; then guard_read_stdin; fi
  guard_judge "${tool}" "$@"
  if [[ "${GUARD_VERDICT}" != "allow" ]]; then
    guard_report_refusal "${report_only}" "${tool}" "$@"
    [[ "${report_only}" -eq 1 ]] || exit "${GUARD_REFUSED_RC}"
  fi
  local real
  guard_real_bin "${tool}" \
    || die "workspace-guard: no ${tool} found on PATH outside scripts/lib/guard-bin/"
  real="${GUARD_REAL_BIN}"
  # curl reads ~/.curlrc unless -q comes first, and a .curlrc line (a url, a
  # request method, data) would be part of the call without being in the
  # arguments this guard just judged.
  if [[ "${tool}" == "curl" ]]; then
    set -- -q "$@"
  fi
  if [[ "${GUARD_HAS_STDIN}" -eq 1 ]]; then
    exec "${real}" "$@" < <(printf '%s' "${GUARD_STDIN}")
  fi
  exec "${real}" "$@"
}

cmd_check() {
  local flag=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --report-only) flag=1; shift ;;
      --) shift; break ;;
      *) break ;;
    esac
  done
  [[ $# -ge 1 ]] || die "usage: workspace-guard.sh check [--report-only] -- TOOL ARGS..."
  local report_only tool="$1"; shift
  guard_report_only_wanted "${flag}"
  report_only="${GUARD_REPORT_ONLY}"
  if guard_wants_stdin "${tool}" "$@"; then guard_read_stdin; fi
  guard_judge "${tool}" "$@"
  if [[ "${GUARD_VERDICT}" == "allow" ]]; then
    printf 'allow %s\n' "${GUARD_RULE}"
    return 0
  fi
  guard_report_refusal "${report_only}" "${tool}" "$@"
  [[ "${report_only}" -eq 1 ]] && return 0
  exit "${GUARD_REFUSED_RC}"
}

# The expectation before A1: the opaque id and where Firestore is, nothing more.
cmd_init() {
  local workspace=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workspace-id) workspace="${2:-}"; shift 2 ;;
      *) die "unknown argument to init: $1" ;;
    esac
  done
  [[ "${workspace}" =~ ${GUARD_WORKSPACE_RE} ]] \
    || die "init needs --workspace-id w-<6 hex>, got '${workspace}'"
  local file
  file="$(guard_expectation_path)"
  if [[ -e "${file}" ]]; then
    local have
    have="$(jq -r '.workspace_id // ""' "${file}" 2>/dev/null || true)"
    [[ "${have}" == "${workspace}" ]] \
      || die "${file} already names workspace '${have}', not ${workspace}; one job guards one workspace"
    ok "expectation for ${workspace} already present"
    return 0
  fi
  load_env
  [[ "${FIRESTORE_DATABASE}" != "(default)" ]] \
    || die "FIRESTORE_DATABASE is (default), which belongs to whoever got there first in this shared project"
  jq -n --arg w "${workspace}" --arg p "${PROJECT_ID}" --arg d "${FIRESTORE_DATABASE}" '
    ("projects/\($p)/databases/\($d)/documents") as $docs
    | {phase: "bootstrap", workspace_id: $w, project_id: $p, firestore_database: $d,
       firestore_docs_prefix: $docs,
       firestore_base: ("https://firestore.googleapis.com/v1/" + $docs)}' \
    | guard_write_private "${file}"
  ok "expectation for ${workspace}: before A1, only A1's reads may run"
}

# The full expectation, from the approved record A1 read.
cmd_expect() {
  local record=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --record) record="${2:-}"; shift 2 ;;
      *) die "unknown argument to expect: $1" ;;
    esac
  done
  [[ -n "${record}" && -f "${record}" && -r "${record}" ]] || die "expect needs --record <readable file>"
  local file
  file="$(guard_expectation_path)"
  [[ -f "${file}" ]] || die "${file} does not exist; run init first"
  local workspace phase
  workspace="$(jq -r '.workspace_id // ""' "${file}")"
  phase="$(jq -r '.phase // ""' "${file}")"
  [[ "${workspace}" =~ ${GUARD_WORKSPACE_RE} ]] || die "${file} names no workspace"

  # A Firestore document (runQuery's `.document`, or fs_get) or a plain object.
  local decoded
  decoded="$(jq -c "${FS_JQ} if has(\"fields\") then doc else . end" "${record}")" \
    || die "the record is not readable JSON"
  local rec_workspace rec_tenant rec_principal rec_id rec_kind
  rec_workspace="$(printf '%s' "${decoded}" | jq -r '.workspace_id // ""')"
  rec_tenant="$(printf '%s' "${decoded}" | jq -r '.tenant_id // ""')"
  rec_principal="$(printf '%s' "${decoded}" | jq -r '.principal // ""')"
  rec_id="$(printf '%s' "${decoded}" | jq -r '.id // ""')"
  # The kind (docs/design/workspace-kinds.md §4.1): absent is personal, so
  # every record written before kinds reads as it did. Anything else this
  # build does not know is refused, never guessed at.
  rec_kind="$(printf '%s' "${decoded}" | jq -r 'if (.kind // null) == null then "personal" else (.kind | tostring) end')"
  [[ "${rec_workspace}" == "${workspace}" ]] \
    || die "the record is workspace '${rec_workspace}', but this job guards ${workspace}"
  case "${rec_kind}" in
    personal|service)
      [[ "${rec_tenant}" =~ ${GUARD_TENANT_RE} ]] \
        || die "the record's tenant id is not a personal tenant id" ;;
    group)
      [[ "${rec_tenant}" =~ ${GUARD_GROUP_TENANT_RE} && "${rec_tenant}" != u-* ]] \
        || die "the record's tenant id is not a group tenant id (a slug with no u-, which is a person's)" ;;
    *) die "the record's kind '${rec_kind}' is not personal, group or service" ;;
  esac
  [[ -n "${rec_principal}" ]] || die "the record names no principal"
  # THE APPROVAL images/workspace-apply/entry.py VERIFIED (lane
  # WK-SIGNED-APPROVAL, R8 of docs/design/workspace-kinds.md §4.5). entry.py
  # checks swarm-api's signature on a group or service record before step 1
  # and pins what it read in SWARM_APPROVAL_PIN, in the fixed environment no
  # override reaches; it always sets it. A record that differs from the pin --
  # swapped by whoever can write Firestore between that check and A1's read --
  # or a group or service the pin does not call verified, gets no
  # expectation, so the guard allows no call that grants anything for it. A
  # run outside the job (an operator's own) sets no pin and is judged as before.
  if [[ -n "${SWARM_APPROVAL_PIN:-}" ]]; then
    local pinned
    pinned="$(jq -nr --arg pin "${SWARM_APPROVAL_PIN}" --arg w "${rec_workspace}" --arg t "${rec_tenant}" \
        --arg k "${rec_kind}" --arg p "${rec_principal}" '
      (try ($pin | fromjson) catch null) as $v
      | if ($v | type) != "object" then "unreadable"
        elif $v.workspace_id != $w or $v.tenant_id != $t or $v.kind != $k or $v.principal != $p then "differs"
        elif ($k == "group" or $k == "service") and $v.verified != true then "unverified"
        else "ok" end')" || pinned="unreadable"
    case "${pinned}" in
      ok) ;;
      differs) die "APPROVAL_UNSIGNED: the record is not the one whose approval the job verified; refusing" ;;
      unverified) die "APPROVAL_UNSIGNED: a ${rec_kind} record whose approval the job did not verify; refusing" ;;
      *) die "APPROVAL_UNSIGNED: SWARM_APPROVAL_PIN is not readable; refusing" ;;
    esac
  fi
  # A group's or a service's principal becomes an IAM member (C10) or a
  # tenant document's principal: lower-cased as swarm-api stores it, and
  # narrow enough to be safe in a member, a document path and a jq string.
  # A personal record is judged exactly as before kinds existed.
  if [[ "${rec_kind}" != "personal" ]]; then
    [[ "${rec_principal}" =~ ^[a-z0-9._+-]+@[a-z0-9.-]+$ ]] || die "the record's principal is not a lower-cased email address"
  fi
  load_env
  local service_principal=""
  case "${rec_kind}" in
    group)
      [[ "${rec_principal}" != *.gserviceaccount.com ]] || die "a group record names a service account" ;;
    service)
      # Q7 (a): only an account of THIS project, never a platform one
      # (swarm-*), never one on the shared deny-list, is ever named as the
      # member C10 may admit or the account C1 may read. Any other is left out
      # of the expectation, so the guard admits no call about it, and S1
      # stops the run for the owner (needs_owner) before any write: refused
      # here instead, A1 would end with the record still `approved` and nobody
      # told (the 2026-10-10 incident, #847).
      if [[ "${rec_principal}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]@${PROJECT_ID}\.iam\.gserviceaccount\.com$ \
            && "${rec_principal}" != swarm-* ]] && ! is_shared_resource "${rec_principal}"; then
        service_principal="${rec_principal}"
      fi
      # The API backend, from the rules in the image (never the record or the
      # environment), and never the console's: a service calls the API (§4.5).
      [[ "$(jq -r '.front_door_backend // ""' "${GUARD_RULES}")" =~ ^swarm[a-z0-9-]*-backend$ \
         && "$(jq -r '.front_door_backend // ""' "${GUARD_RULES}")" != *-ui-ui-backend ]] \
        || die "workspace-calls.json names no API backend for the front door (front_door_backend)" ;;
  esac
  [[ -z "${rec_id}" || "${rec_id}" == "${rec_tenant}" ]] \
    || die "the record is filed under '${rec_id}' but names tenant ${rec_tenant}"
  # The tenant's Firestore switch (#1457 design B, lane ISO-WS-FIRESTORE), as
  # swarm-api writes it on the record from WORKER_FIRESTORE_OFF_TENANTS: the
  # job-made path's twin of terraform/modules/tenancy `worker_firestore_access`
  # and register-tenant.sh --worker-firestore-access. Absent is true, today's
  # grant, so every record written before the switch reads as it did. A false
  # expectation holds no `firestore_role`, so C5 admits no database binding for
  # the worker. Anything but a boolean is refused: a typo must fall through to
  # neither answer, least of all to a grant the record meant to withhold.
  local fs_access
  fs_access="$(printf '%s' "${decoded}" | jq -r '
    if has("worker_firestore_access") | not then "true"
    elif .worker_firestore_access == null then "true"
    elif (.worker_firestore_access | type) == "boolean" then (.worker_firestore_access | tostring)
    else "invalid" end')"
  [[ "${fs_access}" == "true" || "${fs_access}" == "false" ]] \
    || die "the record's worker_firestore_access is not true or false; refusing"

  local derived tenant worker_id forge_suffix
  derived="$(python3 -c "${GUARD_DERIVE_PY}" "${REPO_ROOT}" "${rec_principal}" "${rec_kind}")" \
    || die "swarm_common.identity could not derive a tenant id from the record's principal"
  tenant="$(printf '%s' "${derived}" | jq -r .tenant_id)"
  worker_id="$(printf '%s' "${derived}" | jq -r .worker_id)"
  forge_suffix="$(printf '%s' "${derived}" | jq -r .forge_suffix)"
  [[ "${tenant}" == "${rec_tenant}" ]] \
    || die "the record says tenant ${rec_tenant}, but its principal derives ${tenant}; refusing (WORKSPACE_ID_TAKEN)"
  # A group's or a service's tenant is never one this environment's Terraform
  # declares (§4.7, Q8 (a)): swarm-api refuses the request
  # (WORKSPACE_ID_TERRAFORM), and so does the guard, so a record that reaches
  # the job anyway names nothing of theirs. An environment the rules do not
  # list is refused rather than read as declaring nothing.
  if [[ "${rec_kind}" != "personal" ]]; then
    jq -e --arg env "${ENVIRONMENT}" '(.terraform_tenants // {}) | has($env)' "${GUARD_RULES}" >/dev/null \
      || die "workspace-calls.json lists no Terraform tenants for environment '${ENVIRONMENT}'; refusing a ${rec_kind} workspace"
    if jq -e --arg env "${ENVIRONMENT}" --arg t "${tenant}" '.terraform_tenants[$env] | index($t) != null' \
         "${GUARD_RULES}" >/dev/null; then
      die "the tenant ${tenant} is Terraform's in ${ENVIRONMENT}; refusing (WORKSPACE_ID_TERRAFORM)"
    fi
  fi
  if [[ "${phase}" == "ready" ]]; then
    [[ "$(jq -r '.tenant_id // ""' "${file}")" == "${tenant}" ]] \
      || die "${file} already names another tenant for ${workspace}"
  fi

  local scheduler reconciler
  scheduler="$(platform_account_email swarm-scheduler)" \
    || die "terraform/modules/service_account_ids lists no swarm-scheduler"
  reconciler="$(platform_account_email swarm-reconciler)" \
    || die "terraform/modules/service_account_ids lists no swarm-reconciler"
  # swarm-api, the one account V-1 and V-2 bind on a person's QA secret
  # (docs/design/visual-qa-any-app.md §2.1, §7). Only a personal expectation
  # carries it, below.
  local api_email
  api_email="$(platform_account_email swarm-api)" \
    || die "terraform/modules/service_account_ids lists no swarm-api"

  # These five lines are register-tenant.sh's, character for character, so the
  # condition C4 accepts is the one the script renders (and terraform's, which
  # tests/unit/scripts/test_register_tenant_merge_step.py holds it to).
  # tests/unit/scripts/test_workspace_guard.py fails if either copy moves.
  local GCS_PREFIX="tenants/${tenant}"
  local OBJECT_PREFIX_EXPR LIST_PREFIX_EXPR VERDICTS_PREFIX_EXPR READ_EXPR WRITE_EXPR
  OBJECT_PREFIX_EXPR="resource.name.startsWith(\"projects/_/buckets/${ARTIFACT_BUCKET}/objects/${GCS_PREFIX}/\")"
  LIST_PREFIX_EXPR="api.getAttribute(\"storage.googleapis.com/objectListPrefix\", \"\").startsWith(\"${GCS_PREFIX}/\")"
  VERDICTS_PREFIX_EXPR="resource.name.startsWith(\"projects/_/buckets/${ARTIFACT_BUCKET}/objects/${GCS_PREFIX}/verdicts/\")"
  READ_EXPR="${OBJECT_PREFIX_EXPR} || ${LIST_PREFIX_EXPR}"
  WRITE_EXPR="${OBJECT_PREFIX_EXPR} && !${VERDICTS_PREFIX_EXPR}"
  # register-tenant.sh's role ids, and its suffix (which a test pins to the module).
  local CUSTOM_ROLE_SUFFIX_MODULE=""
  local ROLE_ID_SUFFIX="${CUSTOM_ROLE_SUFFIX_MODULE:+_${CUSTOM_ROLE_SUFFIX_MODULE}}"
  local FIRESTORE_ROLE_ID="swarmTenantWorkerFirestore${ROLE_ID_SUFFIX}"
  local BUCKET_METADATA_ROLE_ID="swarmBucketMetadataReader${ROLE_ID_SUFFIX}"

  jq -n \
    --argjson base "$(cat "${file}")" \
    --arg tenant "${tenant}" --arg worker_id "${worker_id}" \
    --arg namespace "$(tenant_namespace "${tenant}")" \
    --arg scheduler "${scheduler}" --arg reconciler "${reconciler}" \
    --arg bucket "${ARTIFACT_BUCKET}" --arg prefix "${GCS_PREFIX}" \
    --arg read_expr "${READ_EXPR}" --arg write_expr "${WRITE_EXPR}" \
    --arg forge_slot "swarm-tenant-${tenant}-${forge_suffix}" \
    --arg fs_role_id "${FIRESTORE_ROLE_ID}" --arg meta_role_id "${BUCKET_METADATA_ROLE_ID}" \
    --arg env "${ENVIRONMENT}" --arg cluster "${GKE_CLUSTER}" --arg location "${GKE_LOCATION}" \
    --arg kind "${rec_kind}" --arg service_principal "${service_principal}" \
    --arg api_email "${api_email}" --arg qa_name "swarm-tenant-${tenant}-qa" \
    --arg front_door "$(jq -r '.front_door_backend // ""' "${GUARD_RULES}")" \
    --argjson fs_access "${fs_access}" '
    ($base.project_id) as $p
    | $base + {
        phase: "ready",
        kind: $kind,
        tenant_id: $tenant,
        environment: $env,
        worker_id: $worker_id,
        worker_email: "\($worker_id)@\($p).iam.gserviceaccount.com",
        namespace: $namespace,
        scheduler_email: $scheduler,
        reconciler_email: $reconciler,
        artifact_bucket: $bucket,
        bucket_url: "gs://\($bucket)",
        gcs_prefix: $prefix,
        marker_url: "gs://\($bucket)/\($prefix)/.tenant",
        marker_upload_url: "https://storage.googleapis.com/upload/storage/v1/b/\($bucket)/o?uploadType=media&ifGenerationMatch=0&name=\("\($prefix)/.tenant" | @uri)",
        read_expr: $read_expr,
        write_expr: $write_expr,
        forge_slot: $forge_slot,
        firestore_role_id: $fs_role_id,
        worker_firestore_access: $fs_access,
        bucket_metadata_role_id: $meta_role_id,
        bucket_metadata_role: "projects/\($p)/roles/\($meta_role_id)",
        gke_cluster: $cluster,
        gke_location: $location,
        contexts: ["swarm-\($env)", "gke_\($p)_\($location)_\($cluster)"],
        tenant_doc: "tenants/\($tenant)",
        pool_doc: "pools/tenant:\($tenant)",
        workspace_doc: "workspaces/\($tenant)"
      }
      # A service account is a caller, not a person: its team slot has no
      # refresh twin (an App refresh token belongs to a person, §4.6).
      + (if $kind == "service" then {} else {forge_slot_twin: "\($forge_slot)-refresh"} end)
      # The label the deployer scope policy admits a job-made namespace by
      # (§4.3 A7); C7 requires it on the Namespace.
      + (if $kind == "personal" then {} else {namespace_label: $base.workspace_id} end)
      # The QA target credential secret of a person, and the one account C6
      # may bind on it (§2.1, §7 V-1/V-2; BGD 2026-10-22: the deployer job
      # makes it). A PERSONAL expectation only: the secret binder of the
      # deployer stops at swarm-tenant-u-, so no group or service has one.
      + (if $kind == "personal" then {qa_secret: $qa_name, api_email: $api_email} else {} end)
      # The worker database role C5 admits, only when the switch is on.
      + (if $fs_access then {firestore_role: "projects/\($p)/roles/\($fs_role_id)"} else {} end)
      # The two values C10 is made of, only for the kind that is admitted.
      + (if $kind == "service" then {front_door_backend: $front_door} else {} end)
      + (if $kind == "service" and $service_principal != "" then {service_principal: $service_principal} else {} end)
      ' | guard_write_private "${file}"
  ok "expectation for ${workspace}: every name derived from the approved record"
}

# ---------------------------------------------------------------------------
# Self-test: every case in workspace-guard-cases.json, through the same judge.
# ---------------------------------------------------------------------------

# One case as shell assignments, its {names} filled from the ready expectation
# (plus {casedir}, {fake_token} and {auth_config}). Top-level, not inline in $( ): bash 3.2
# scans a command substitution's body for quotes and parentheses.
# shellcheck disable=SC2016  # jq programs: their $names are jq's
GUARD_CASE_JQ='
      (.cases[$i].kind // "personal") as $kind
      | ((if $kind == "group" then $eg elif $kind == "service" then $es else $e end)[0]
       + {casedir: $casedir, fake_token: $tok, auth_config: $auth}) as $v
      | def fill: if type == "string" then gsub("\\{(?<k>[a-z_]+)\\}"; (($v[.k]) as $x | if ($x | type) == "string" then $x else "{" + .k + "}" end))
                  elif type == "array" then map(fill)
                  elif type == "object" then with_entries(.value |= fill)
                  else . end;
      .cases[$i] | fill
      | "CASE_NAME=\(.name | @sh)",
        "CASE_RULE=\(.rule | @sh)",
        "CASE_EXPECT=\(.expect | @sh)",
        "CASE_BY=\((.by // .rule) | @sh)",
        "CASE_PHASE=\((.phase // "ready") | @sh)",
        "CASE_KIND=\($kind | @sh)",
        "CASE_TOOL=\(.tool | @sh)",
        "CASE_FALLBACK=\((.wd9_fallback // false) | tostring | @sh)",
        "CASE_HAS_STDIN=\((has("stdin")) | if . then "1" else "0" end | @sh)",
        "CASE_STDIN=\((.stdin // "") | @sh)",
        "CASE_CONTEXT=\((.kube_current_context // "swarm-dev") | @sh)",
        "CASE_FILES=\((.files // {}) | tojson | @sh)",
        "CASE_ARGV=(\(.argv | map(@sh) | join(" ")))"'

# Every rule has a case it must refuse and one it must allow.
# shellcheck disable=SC2016
GUARD_COVERAGE_JQ='
    [.cases[] | {rule, expect}] as $c
    | $r[0].rules[].id as $id
    | ["allow", "refuse"][] as $x
    | select(([$c[] | select(.rule == $id and .expect == $x)] | length) == 0)
    | "\($id) has no case it must \($x)"'
# One line of a replay plan as shell assignments (see guard_replay).
# shellcheck disable=SC2016
GUARD_REPLAY_JQ='
      "R_TOOL=\(.tool | @sh)",
      "R_EXPECT=\(.expect | @sh)",
      "R_KUBECONFIG=\(.kubeconfig | @sh)",
      "R_FALLBACK=\(.wd9_fallback | tostring | @sh)",
      "R_HAS_STDIN=\(if .stdin == null then "0" else "1" end | @sh)",
      "R_STDIN=\((.stdin // "") | @sh)",
      "R_IMPERSONATE=\(.env_impersonate | @sh)",
      "R_PROJECT=\(.env_project | @sh)",
      "R_ARGV=(\(.argv | map(@sh) | join(" ")))"'

# The verdict corpus (tests/unit/scripts/test_workspace_guard_corpus.py): every
# call the guard's tests have fed it, judged again in this one process, one
# verdict per line as JSON. The plan names, per call, the expectation file, the
# kubeconfig and the stdin the call had, all of which the test writes; the
# rules are the shipped ones with the WD9 switch as the call saw it. It judges
# and prints; nothing is run, and no stop file is written.
guard_replay() {
  local plan="$1" line assignments
  [[ -f "${plan}" && -r "${plan}" ]] || die "self-test --replay needs a readable plan file"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/swarm-workspace-guard.XXXXXX")"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "rm -rf '${tmp}'" EXIT
  jq '.wd9_fallback = true' "${GUARD_RULES}" >"${tmp}/rules-fallback.json"
  jq '.wd9_fallback = false' "${GUARD_RULES}" >"${tmp}/rules-principal-set.json"
  local R_TOOL R_EXPECT R_KUBECONFIG R_FALLBACK R_HAS_STDIN R_STDIN R_IMPERSONATE R_PROJECT
  local -a R_ARGV=()
  local visited=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -n "${line}" ]] || continue
    assignments="$(printf '%s' "${line}" | jq -r "${GUARD_REPLAY_JQ}")"
    eval "${assignments}"
    GUARD_RULES_ACTIVE="${tmp}/rules-principal-set.json"
    [[ "${R_FALLBACK}" == "true" ]] && GUARD_RULES_ACTIVE="${tmp}/rules-fallback.json"
    GUARD_STDIN="${R_STDIN}"
    GUARD_HAS_STDIN="${R_HAS_STDIN}"
    SWARM_CALL_GUARD="${R_EXPECT}" KUBECONFIG="${R_KUBECONFIG}" \
      CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT="${R_IMPERSONATE}" CLOUDSDK_CORE_PROJECT="${R_PROJECT}" \
      guard_judge "${R_TOOL}" ${R_ARGV[@]+"${R_ARGV[@]}"}
    GUARD_RULES_ACTIVE="${GUARD_RULES}"
    jq -nc --arg verdict "${GUARD_VERDICT}" --arg rule "${GUARD_RULE}" \
          --arg command "${GUARD_COMMAND}" --arg reason "${GUARD_REASON}" \
      '[$verdict, $rule, $command, $reason]'
    visited=$((visited + 1))
  done <"${plan}"
  [[ "${visited}" -gt 0 ]] || die "self-test --replay visited no call"
}

cmd_self_test() {
  local json=0 only=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json=1; shift ;;
      --case) only="${2:-}"; shift 2 ;;
      --replay) guard_replay "${2:-}"; return 0 ;;
      *) die "unknown argument to self-test: $1" ;;
    esac
  done
  [[ -f "${GUARD_CASES}" ]] || die "missing ${GUARD_CASES}"

  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/swarm-workspace-guard.XXXXXX")"
  # shellcheck disable=SC2064  # expand now: the path is fixed for this run
  trap "rm -rf '${tmp}'" EXIT

  # A fixed, credential-free environment: the self-test judges names, so the
  # names must not depend on whoever runs it.
  export SWARM_ENV_FILE="${tmp}/no.env"
  export PROJECT_ID="saga-agents-staging" REGION="us-central1" ENVIRONMENT="dev"
  export FIRESTORE_DATABASE="swarm" GKE_CLUSTER="swarm-autopilot" GKE_LOCATION="us-central1"
  unset ARTIFACT_BUCKET SWARM_CALL_GUARD_MODE SWARM_KUBECTL SWARM_APPROVAL_PIN
  export SWARM_CALL_GUARD="${tmp}/expect.json"

  local workspace
  workspace="$(jq -r '.workspace_id' "${GUARD_CASES}")"
  cmd_init --workspace-id "${workspace}" 2>/dev/null
  cp "${SWARM_CALL_GUARD}" "${tmp}/bootstrap.json"
  jq '.record' "${GUARD_CASES}" >"${tmp}/record.json"
  cmd_expect --record "${tmp}/record.json" 2>/dev/null
  cp "${SWARM_CALL_GUARD}" "${tmp}/ready.json"
  # A group's and a service's expectation (docs/design/workspace-kinds.md
  # §4.3), for the cases that name a `kind`: the same init, their own record.
  local kind
  for kind in group service; do
    rm -f "${SWARM_CALL_GUARD}"
    cmd_init --workspace-id "${workspace}" 2>/dev/null
    jq --arg k "${kind}" '.kind_records[$k]' "${GUARD_CASES}" >"${tmp}/record-${kind}.json"
    cmd_expect --record "${tmp}/record-${kind}.json" 2>/dev/null
    cp "${SWARM_CALL_GUARD}" "${tmp}/ready-${kind}.json"
  done
  jq '.wd9_fallback = true' "${GUARD_RULES}" >"${tmp}/rules-fallback.json"
  jq '.wd9_fallback = false' "${GUARD_RULES}" >"${tmp}/rules-principal-set.json"

  # Built at run time, never a literal: nothing here may look like a credential.
  # {auth_config} is the config line fs_request really writes, around it.
  local fake_token auth_line
  fake_token="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  auth_line="$(auth_config "${fake_token}")"$'\n'

  local total visited=0 passed=0 failed=0 i
  total="$(jq '.cases | length' "${GUARD_CASES}")"
  [[ "${total}" -gt 0 ]] || die "workspace-guard-cases.json holds no case"

  local CASE_NAME CASE_RULE CASE_EXPECT CASE_BY CASE_PHASE CASE_KIND CASE_TOOL CASE_FALLBACK
  local CASE_HAS_STDIN CASE_STDIN CASE_CONTEXT CASE_FILES
  local -a CASE_ARGV=()
  for ((i = 0; i < total; i++)); do
    local casedir="${tmp}/case-${i}" assignments
    mkdir -p "${casedir}"
    assignments="$(jq -r --argjson i "${i}" --slurpfile e "${tmp}/ready.json" \
        --slurpfile eg "${tmp}/ready-group.json" --slurpfile es "${tmp}/ready-service.json" \
        --arg casedir "${casedir}" --arg tok "${fake_token}" --arg auth "${auth_line}" \
        "${GUARD_CASE_JQ}" "${GUARD_CASES}")"
    eval "${assignments}"
    if [[ -n "${only}" && "${CASE_NAME}" != "${only}" ]]; then
      continue
    fi
    visited=$((visited + 1))

    # The files a case names, written where its arguments say ({casedir}).
    local fname
    while IFS= read -r fname; do
      [[ -n "${fname}" ]] || continue
      printf '%s' "${CASE_FILES}" | jq -r --arg f "${fname}" '.[$f] | if type == "string" then . else tojson end' \
        >"${casedir}/${fname}"
    done < <(printf '%s' "${CASE_FILES}" | jq -r 'keys[]')
    printf 'apiVersion: v1\nkind: Config\ncurrent-context: %s\n' "${CASE_CONTEXT}" >"${casedir}/kubeconfig"

    case "${CASE_PHASE}" in
      bootstrap) cp "${tmp}/bootstrap.json" "${SWARM_CALL_GUARD}" ;;
      ready|stopped)
        if [[ "${CASE_KIND}" == "personal" ]]; then
          cp "${tmp}/ready.json" "${SWARM_CALL_GUARD}"
        else
          cp "${tmp}/ready-${CASE_KIND}.json" "${SWARM_CALL_GUARD}"
        fi ;;
      *) die "case ${CASE_NAME}: unknown phase ${CASE_PHASE}" ;;
    esac
    chmod 600 "${SWARM_CALL_GUARD}"
    rm -f "${SWARM_CALL_GUARD}.stop"
    if [[ "${CASE_PHASE}" == "stopped" ]]; then
      printf '{"state":"needs_owner","rule":"C2"}\n' >"${SWARM_CALL_GUARD}.stop"
    fi
    # A case without wd9_fallback is judged under the principal-set grant, not
    # under whatever the live switch says: the owner turned the fallback on
    # (2026-10-12), and the cases proving refusal without it must still refuse.
    GUARD_RULES_ACTIVE="${tmp}/rules-principal-set.json"
    [[ "${CASE_FALLBACK}" == "true" ]] && GUARD_RULES_ACTIVE="${tmp}/rules-fallback.json"
    GUARD_STDIN="${CASE_STDIN}"
    GUARD_HAS_STDIN="${CASE_HAS_STDIN}"

    KUBECONFIG="${casedir}/kubeconfig" guard_judge "${CASE_TOOL}" ${CASE_ARGV[@]+"${CASE_ARGV[@]}"}
    GUARD_RULES_ACTIVE="${GUARD_RULES}"

    local pass=0
    if [[ "${CASE_EXPECT}" == "allow" && "${GUARD_VERDICT}" == "allow" && "${GUARD_RULE}" == "${CASE_BY}" ]] \
       || [[ "${CASE_EXPECT}" == "refuse" && "${GUARD_VERDICT}" == "refuse" && "${GUARD_RULE}" == "${CASE_BY}" ]]; then
      pass=1
    fi
    if [[ "${pass}" -eq 1 ]]; then passed=$((passed + 1)); else failed=$((failed + 1)); fi
    if [[ "${json}" -eq 1 ]]; then
      jq -nc --arg name "${CASE_NAME}" --arg rule "${CASE_RULE}" --arg expect "${CASE_EXPECT}" \
            --arg by "${CASE_BY}" --arg verdict "${GUARD_VERDICT}" --arg got "${GUARD_RULE}" \
            --arg reason "${GUARD_REASON}" --argjson pass "$([[ "${pass}" -eq 1 ]] && echo true || echo false)" \
        '{name: $name, rule: $rule, expect: $expect, by: $by, verdict: $verdict, got_rule: $got, reason: $reason, pass: $pass}'
    elif [[ "${pass}" -eq 1 ]]; then
      ok "${CASE_RULE} ${CASE_EXPECT}: ${CASE_NAME}"
    else
      err "${CASE_RULE} ${CASE_EXPECT}: ${CASE_NAME} -- got ${GUARD_VERDICT} by ${GUARD_RULE}: ${GUARD_REASON}"
    fi
  done

  # The expectation itself: a record that names another workspace, or a tenant
  # its principal does not derive, must never become an expectation.
  local j n_ref name
  n_ref="$(jq '.expect_refusals | length' "${GUARD_CASES}")"
  if [[ -z "${only}" ]]; then
    for ((j = 0; j < n_ref; j++)); do
      name="$(jq -r --argjson j "${j}" '.expect_refusals[$j].name' "${GUARD_CASES}")"
      jq --argjson j "${j}" '(.expect_refusals[$j].kind // null) as $k
        | (if $k == null then .record else .kind_records[$k] end) * .expect_refusals[$j].change' \
        "${GUARD_CASES}" >"${tmp}/bad-record.json"
      cp "${tmp}/bootstrap.json" "${SWARM_CALL_GUARD}"
      visited=$((visited + 1))
      local verdict="refuse" pass=true
      if (cmd_expect --record "${tmp}/bad-record.json") >/dev/null 2>&1; then
        verdict="allow"; pass=false
        failed=$((failed + 1))
      else
        passed=$((passed + 1))
      fi
      if [[ "${json}" -eq 1 ]]; then
        jq -nc --arg name "${name}" --arg verdict "${verdict}" --argjson pass "${pass}" \
          '{name: $name, rule: "expect", expect: "refuse", verdict: $verdict, pass: $pass}'
      elif [[ "${pass}" == "true" ]]; then
        ok "expect refuse: ${name}"
      else
        err "expect refuse: ${name} -- the record became an expectation"
      fi
    done
  fi

  # Every rule has a case it must refuse and one it must allow.
  local missing
  missing="$(jq -r --slurpfile r "${GUARD_RULES}" "${GUARD_COVERAGE_JQ}" "${GUARD_CASES}")"
  if [[ -n "${missing}" && -z "${only}" ]]; then
    printf '%s\n' "${missing}" | while IFS= read -r line; do err "${line}"; done
    failed=$((failed + 1))
  fi

  [[ "${visited}" -gt 0 ]] || die "self-test visited no case"
  if [[ "${json}" -eq 0 ]]; then
    info "workspace guard self-test: ${passed} passed, ${failed} failed, ${visited} visited (${total} call cases, ${n_ref} record cases)"
  fi
  [[ "${failed}" -eq 0 ]]
}

[[ $# -ge 1 ]] || die "usage: workspace-guard.sh init|expect|check|run|self-test ... (see the header)"
SUBCOMMAND="$1"; shift
case "${SUBCOMMAND}" in
  init)      cmd_init "$@" ;;
  expect)    cmd_expect "$@" ;;
  check)     cmd_check "$@" ;;
  run)       cmd_run "$@" ;;
  self-test|--self-test) cmd_self_test "$@" ;;
  -h|--help) sed -n '2,48p' "$0" ;;
  *) die "unknown subcommand: ${SUBCOMMAND}" ;;
esac
